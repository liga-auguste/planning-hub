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
        response = _client().databases.query(
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

        projects = []
        for page in response["results"]:
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
                "tasks": _get_tasks(page["id"]),
            }
            projects.append(project)

        return projects


def _get_tasks(project_page_id: str) -> list:
    response = _client().databases.query(
        database_id=TASKS_DB,
        filter={
            "property": "Related to Projekte",
            "relation": {"contains": project_page_id},
        },
    )
    return [_parse_task_page(page) for page in response["results"]]


def get_unassigned_tasks(today: date) -> list:
    """#53: get_upcoming_projects/_get_tasks only ever query TASKS_DB per
    project via relation.contains — a task with an empty "Related to
    Projekte" relation ("Kleinkram" with no project) is never picked up by
    that path. This is its own top-level read, wrapped like
    get_upcoming_projects/get_historical_projects rather than nested inside
    one of their translate_notion_errors() blocks."""
    with translate_notion_errors():
        response = _client().databases.query(
            database_id=TASKS_DB,
            filter={
                "property": "Related to Projekte",
                "relation": {"is_empty": True},
            },
        )
        return [_parse_task_page(page) for page in response["results"]]


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
    }


def toggle_task(task_id: str, done: bool) -> None:
    with translate_notion_errors():
        _client().pages.update(page_id=task_id, properties={"Done": {"checkbox": done}})


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
    with translate_notion_errors():
        response = _client().databases.query(
            database_id=PROJECTS_DB,
            filter={
                "property": "Status/Aufgaben",
                "status": {"equals": "abgeschlossen"},
            },
            sorts=[{"property": "Termin", "direction": "descending"}],
        )

        projects = []
        for page in response["results"]:
            props = page["properties"]
            name = _text(props["Name der Veranstaltung"]["title"])
            if "Marktzeit" in name:
                continue
            projects.append(
                {
                    "name": name,
                    "event_date": _date(props["Termin"]),
                    "event_date_uncertain": props.get("Termin unsicher", {}).get(
                        "checkbox", False
                    ),
                    "performers": _text(props["Musiker / Mitwirkende"]["rich_text"]),
                    "tasks": _get_tasks(page["id"]),
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
