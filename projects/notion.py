import logging
import os
from contextlib import contextmanager
from datetime import date

import httpx
from notion_client import Client
from notion_client.errors import HTTPResponseError, RequestTimeoutError

logger = logging.getLogger(__name__)

PROJECTS_DB = "87ad1e35b3344ed49c1ba977664bb087"
TASKS_DB = "f22abd16a92d48598c04be76f35c6b1d"

# #225: how many closed projects get_historical_projects reads. A cap rather
# than pagination, because everything this read returns goes whole into every
# planner prompt (_format_history, planner.py:11) — the bound is a
# prompt-size decision, not a display one. 40 covers roughly two years at
# ~50 closed projects a year, so every season and every recurring event
# appears at least twice as a calibration reference.
HISTORY_PROJECT_LIMIT = 40


class NotionUnavailableError(Exception):
    """Raised when Notion can't be reached or returns an error. notion-client
    has no retry of its own (unlike the Anthropic SDK), so this covers
    everything from a single failed attempt — see the failure table in #29.
    """


@contextmanager
def translate_notion_errors():
    """Wraps a Notion call site.

    HTTPResponseError/RequestTimeoutError are notion-client's own exception
    types; httpx.HTTPError is a safety net underneath them — notion-client
    converts a timeout to RequestTimeoutError but lets a raw connection
    failure (httpx.ConnectError and friends) straight through unwrapped.
    """
    try:
        yield
    except (HTTPResponseError, RequestTimeoutError, httpx.HTTPError) as exc:
        logger.warning("Notion call failed: %s", exc)
        raise NotionUnavailableError("Notion request failed") from exc


def _client():
    return Client(auth=os.environ["NOTION_API_KEY"])


def get_upcoming_projects(today: date) -> list:
    with translate_notion_errors():
        client = _client()
        pages = _query_all_pages(
            client,
            database_id=PROJECTS_DB,
            filter={
                "and": [
                    {
                        "property": "Status/Aufgaben",
                        "status": {"does_not_equal": "abgeschlossen"},
                    },
                    {
                        "property": "Status/Aufgaben",
                        "status": {"does_not_equal": "kein Status erforderlich"},
                    },
                ]
            },
            sorts=[{"property": "Termin", "direction": "ascending"}],
        )
        tasks_by_project = _tasks_by_project(client, [page["id"] for page in pages])

        projects = []
        for page in pages:
            props = page["properties"]
            status_prop = props.get("Status/Aufgaben", {}).get("status")
            project = {
                "id": page["id"],
                "name": _text(props["Name der Veranstaltung"]["title"]),
                "event_date": _date(props["Termin"]),
                "event_date_uncertain": props.get("Termin unsicher", {}).get(
                    "checkbox", False
                ),
                "performers": _text(props["Musiker / Mitwirkende"]["rich_text"]),
                "status": status_prop["name"] if status_prop else None,
                "status_color": status_prop["color"] if status_prop else "gray",
                "tasks": tasks_by_project[page["id"]],
            }
            projects.append(project)

        return projects


def _get_tasks(project_page_id: str) -> list:
    """One project's tasks.

    #196 replaced the read paths' per-project fan-out with
    _tasks_by_project, but this single-project read stays: create_tasks
    needs it for its idempotency check, where there is exactly one project
    and the answer is needed before any write. It pages, or that check
    silently stops working for a project past 100 tasks.
    """
    return [
        _parse_task_page(page)
        for page in _query_all_pages(
            _client(),
            database_id=TASKS_DB,
            filter={
                "property": "Related to Projekte",
                "relation": {"contains": project_page_id},
            },
        )
    ]


def _tasks_by_project(client, project_ids: list) -> dict:
    """Every listed project's tasks, in one query instead of one per project.

    #196: the read paths used to call _get_tasks per project, so a cold
    planner start cost ~100 sequential requests. Notion's filter reference
    permits a compound filter nested two levels deep, and a flat `or` over
    concrete relation.contains conditions is one level — the whole fan-out
    collapses into a single (paged) read.

    The `or` array's maximum length is not documented, only the 500KB
    payload ceiling. What keeps it permanently small is HISTORY_PROJECT_LIMIT
    (#225) on the larger of the two callers.

    Returns {project_id: [task, ...]}, with an entry for every id passed in.
    """
    if not project_ids:
        # An empty `or` array is a Notion error — and there is nothing to
        # ask about anyway.
        return {}

    pages = _query_all_pages(
        client,
        database_id=TASKS_DB,
        filter={
            "or": [
                {
                    "property": "Related to Projekte",
                    "relation": {"contains": project_id},
                }
                for project_id in project_ids
            ]
        },
    )

    by_project = {project_id: [] for project_id in project_ids}
    # Notion returns page ids both with and without hyphens depending on
    # context, so the grouping compares one normalised form.
    by_normalised_id = {_normalise_id(pid): pid for pid in project_ids}
    for page in pages:
        task = _parse_task_page(page)
        relation = page["properties"].get("Related to Projekte", {}).get("relation", [])
        for related in relation:
            project_id = by_normalised_id.get(_normalise_id(related["id"]))
            # A task can relate to several projects and appears under each;
            # a relation to a project outside this call is simply not ours.
            if project_id is not None:
                by_project[project_id].append(task)
    return by_project


def _normalise_id(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def get_unassigned_tasks(today: date) -> list:
    """#53: get_upcoming_projects/_get_tasks only ever query TASKS_DB per
    project via relation.contains — a task with an empty "Related to
    Projekte" relation ("Kleinkram" with no project) is never picked up by
    that path. This is its own top-level read, wrapped like
    get_upcoming_projects/get_historical_projects rather than nested inside
    one of their translate_notion_errors() blocks."""
    with translate_notion_errors():
        pages = _query_all_pages(
            _client(),
            database_id=TASKS_DB,
            filter={
                "property": "Related to Projekte",
                "relation": {"is_empty": True},
            },
        )
        return [_parse_task_page(page) for page in pages]


def _query_all_pages(client, **query) -> list:
    """Every row of a databases.query, not just the first page.

    Notion returns at most 100 rows per call, and signals the cut with
    has_more — a first-page-only read is a silent undercount, the class of
    bug #215 exists to remove. Every read in this module goes through here
    (#196, #225) except get_historical_projects, which is bounded on
    purpose; its reason lives at HISTORY_PROJECT_LIMIT.

    has_more with no next_cursor would be Notion breaking its own contract,
    and the loop cannot honour it: without a cursor the next request is
    byte-for-byte the first one, so paging on repeats that first page
    forever inside a web request. It stops and logs instead — an undercount
    is recoverable, a hung dashboard request is not.
    """
    results = []
    cursor = None
    while True:
        page = client.databases.query(
            **query, **({"start_cursor": cursor} if cursor else {})
        )
        results.extend(page["results"])
        cursor = page["next_cursor"] if page["has_more"] else None
        if not cursor:
            if page["has_more"]:
                logger.warning(
                    "Notion reported has_more without a next_cursor; "
                    "stopping after %d rows.",
                    len(results),
                )
            return results


def get_tasks_completed_in_range(start: date, end: date) -> list:
    """#215: every task whose "Erledigt am" falls in [start, end].

    Read straight from TASKS_DB rather than through get_upcoming_projects,
    which filters at the *project* level ("Status/Aufgaben" is neither
    "abgeschlossen" nor "kein Status erforderlich") and only ever reaches
    tasks that carry a project relation at all (#53). Both filters cost real
    tasks: measured against the live database for KW 36/2026, 19 tasks were
    completed and the project-keyed path could reach 10 of them.

    Notion's on_or_after/on_or_before cover the whole day — verified against
    the live database, where a single-day range returns rows created at
    11:36 UTC — so both bounds go in as bare dates.

    Known floor, not a bug: a task checked off directly in Notion, or before
    "Erledigt am" existed in the schema, has "Done" without a date and
    cannot be placed in any week. It is missing from this read by
    construction. See the count in views.close_week_confirm.
    """
    with translate_notion_errors():
        pages = _query_all_pages(
            _client(),
            database_id=TASKS_DB,
            filter={
                "and": [
                    {
                        "property": "Erledigt am",
                        "date": {"on_or_after": start.isoformat()},
                    },
                    {
                        "property": "Erledigt am",
                        "date": {"on_or_before": end.isoformat()},
                    },
                ]
            },
        )
        return [_parse_task_page(page) for page in pages]


def get_tasks_created_in_range(start: date, end: date) -> list:
    """#215: every task created in [start, end].

    The same independent TASKS_DB read as get_tasks_completed_in_range, so
    the close-out's two counts measure one population instead of two.

    created_time is a timestamp rather than a property, so its filter
    carries a "timestamp" key and no "property" key. The bounds are whole
    days here too (same verification as above).
    """
    with translate_notion_errors():
        pages = _query_all_pages(
            _client(),
            database_id=TASKS_DB,
            filter={
                "and": [
                    {
                        "timestamp": "created_time",
                        "created_time": {"on_or_after": start.isoformat()},
                    },
                    {
                        "timestamp": "created_time",
                        "created_time": {"on_or_before": end.isoformat()},
                    },
                ]
            },
        )
        return [_parse_task_page(page) for page in pages]


def _parse_task_page(page: dict) -> dict:
    props = page["properties"]
    return {
        "id": page["id"],
        "name": _text(props["Aufgabe"]["title"]),
        "due": _date(props["Wann?"]),
        "done": props["Done"]["checkbox"],
        "kontext": [
            k["name"] for k in props.get("Kontext", {}).get("multi_select", [])
        ],
        # #171: read fresh on every fetch, or a task's count would reset to 0
        # on display even though the stored value is correct — Notion has no
        # atomic increment, see increment_postpone_count below.
        "postpone_count": props.get("Verschoben", {}).get("number") or 0,
        # #169: only used by the close-out flow's "added this week" stat
        # (production only) — every Notion page carries it.
        "created_time": _date_from_iso_datetime(page.get("created_time")),
        # #19: written by toggle_task alongside Done — .get() rather than a
        # direct index, like Kontext/Verschoben above, so a page fetched
        # before the property existed in the schema doesn't KeyError.
        "completed_date": _date(props.get("Erledigt am", {})),
    }


def toggle_task(task_id: str, done: bool, completed_date: str | None = None) -> None:
    """#19: one call writes both Done and Erledigt am — they change together
    (a task is done or it isn't, and the completion date follows) and
    there's no read-then-write race here to guard against, unlike
    increment_postpone_count below."""
    with translate_notion_errors():
        _client().pages.update(
            page_id=task_id,
            properties={
                "Done": {"checkbox": done},
                "Erledigt am": {
                    "date": {"start": completed_date} if completed_date else None
                },
            },
        )


def update_task_date(task_id: str, new_date: str) -> None:
    with translate_notion_errors():
        _client().pages.update(
            page_id=task_id, properties={"Wann?": {"date": {"start": new_date}}}
        )


def increment_postpone_count(task_id: str) -> int:
    """Read-then-write, since Notion has no atomic increment. Deliberately
    not folded into update_task_date (#171): two calls instead of one costs
    an extra Notion request per reschedule, but leaves update_task_date and
    its own tests untouched. Acceptable for a single-user app."""
    with translate_notion_errors():
        client = _client()
        page = client.pages.retrieve(page_id=task_id)
        current = page["properties"].get("Verschoben", {}).get("number") or 0
        new_value = current + 1
        client.pages.update(
            page_id=task_id, properties={"Verschoben": {"number": new_value}}
        )
        return new_value


def _text(rich_text_list: list) -> str:
    return "".join(t["plain_text"] for t in rich_text_list)


def _date(date_prop: dict) -> date | None:
    value = date_prop.get("date")
    if value and value.get("start"):
        return date.fromisoformat(value["start"])
    return None


def _date_from_iso_datetime(value: str | None) -> date | None:
    """Notion's created_time is an ISO 8601 UTC timestamp
    ("2026-08-25T10:00:00.000Z") — only the calendar date matters here."""
    if not value:
        return None
    return date.fromisoformat(value[:10])


def get_historical_projects() -> list:
    """The HISTORY_PROJECT_LIMIT most recent closed projects, newest first.

    Capped rather than paginated, and the reason lives at the constant: this
    is prompt input, not a listing. The Marktzeit exclusion is part of the
    Notion filter rather than a Python skip, or the cap would mean "40 minus
    however many Marktzeit rows happen to fall inside it".
    """
    with translate_notion_errors():
        client = _client()
        response = client.databases.query(
            database_id=PROJECTS_DB,
            filter={
                "and": [
                    {
                        "property": "Status/Aufgaben",
                        "status": {"equals": "abgeschlossen"},
                    },
                    {
                        "property": "Name der Veranstaltung",
                        "title": {"does_not_contain": "Marktzeit"},
                    },
                ]
            },
            sorts=[{"property": "Termin", "direction": "descending"}],
            page_size=HISTORY_PROJECT_LIMIT,
        )
        pages = response["results"]
        tasks_by_project = _tasks_by_project(client, [page["id"] for page in pages])

        projects = []
        for page in pages:
            props = page["properties"]
            projects.append(
                {
                    "name": _text(props["Name der Veranstaltung"]["title"]),
                    "event_date": _date(props["Termin"]),
                    "event_date_uncertain": props.get("Termin unsicher", {}).get(
                        "checkbox", False
                    ),
                    "performers": _text(props["Musiker / Mitwirkende"]["rich_text"]),
                    "tasks": tasks_by_project[page["id"]],
                }
            )

        return projects


def find_project(name: str, event_date: date) -> str | None:
    """Returns the page id of the project with exactly this name and date,
    or None. planner_create checks this before create_project so that
    retrying a save that died halfway reuses the page the first attempt
    already created instead of creating a twin.
    """
    with translate_notion_errors():
        response = _client().databases.query(
            database_id=PROJECTS_DB,
            filter={
                "and": [
                    {"property": "Name der Veranstaltung", "title": {"equals": name}},
                    {"property": "Termin", "date": {"equals": event_date.isoformat()}},
                ]
            },
        )
        results = response["results"]
        return results[0]["id"] if results else None


def create_project(name: str, event_date: date, date_uncertain: bool = False) -> str:
    with translate_notion_errors():
        response = _client().pages.create(
            parent={"database_id": PROJECTS_DB},
            properties={
                "Name der Veranstaltung": {"title": [{"text": {"content": name}}]},
                "Termin": {"date": {"start": event_date.isoformat()}},
                "Termin unsicher": {"checkbox": date_uncertain},
                "Status/Aufgaben": {"status": {"name": "geplant / mit Zeitplan"}},
            },
        )
        return response["id"]


def create_tasks(project_id: str, tasks: list) -> None:
    client = _client()
    with translate_notion_errors():
        # A failed attempt may have written part of this list already (the
        # loop below is one API call per task) — skip what already exists so
        # a retry from planner_create is idempotent instead of duplicating.
        existing = {
            (t["name"], t["due"].isoformat() if t["due"] else None)
            for t in _get_tasks(project_id)
        }
        for task in tasks:
            if (task["name"], task["date"]) in existing:
                continue
            client.pages.create(
                parent={"database_id": TASKS_DB},
                properties={
                    "Aufgabe": {"title": [{"text": {"content": task["name"]}}]},
                    "Wann?": {"date": {"start": task["date"]}},
                    "Done": {"checkbox": False},
                    "Kontext": {
                        "multi_select": [{"name": k} for k in task.get("kontext", [])]
                    },
                    "Related to Projekte": {"relation": [{"id": project_id}]},
                },
            )
