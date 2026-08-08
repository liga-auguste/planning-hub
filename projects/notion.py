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
                    {"property": "Status/Aufgaben", "status": {"does_not_equal": "abgeschlossen"}},
                    {"property": "Status/Aufgaben", "status": {"does_not_equal": "kein Status erforderlich"}},
                ]
            },
            sorts=[{"property": "Termin", "direction": "ascending"}]
        )

        projects = []
        for page in response["results"]:
            props = page["properties"]
            status_prop = props.get("Status/Aufgaben", {}).get("status")
            project = {
                "id": page["id"],
                "name": _text(props["Name der Veranstaltung"]["title"]),
                "event_date": _date(props["Termin"]),
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
            "relation": {"contains": project_page_id}
        }
    )

    tasks = []
    for page in response["results"]:
        props = page["properties"]
        tasks.append({
            "id": page["id"],
            "name": _text(props["Aufgabe"]["title"]),
            "due": _date(props["Wann?"]),
            "done": props["Done"]["checkbox"],
            "kontext": [k["name"] for k in props.get("Kontext", {}).get("multi_select", [])],
        })

    return tasks


def toggle_task(task_id: str, done: bool) -> None:
    with translate_notion_errors():
        _client().pages.update(
            page_id=task_id,
            properties={"Done": {"checkbox": done}}
        )


def update_task_date(task_id: str, new_date: str) -> None:
    with translate_notion_errors():
        _client().pages.update(
            page_id=task_id,
            properties={"Wann?": {"date": {"start": new_date}}}
        )


def _text(rich_text_list: list) -> str:
    return "".join(t["plain_text"] for t in rich_text_list)


def _date(date_prop: dict) -> date | None:
    value = date_prop.get("date")
    if value and value.get("start"):
        return date.fromisoformat(value["start"])
    return None

def get_historical_projects() -> list:
    with translate_notion_errors():
        response = _client().databases.query(
            database_id=PROJECTS_DB,
            filter={
                "property": "Status/Aufgaben",
                "status": {"equals": "abgeschlossen"}
            },
            sorts=[{"property": "Termin", "direction": "descending"}]
        )

        projects = []
        for page in response["results"]:
            props = page["properties"]
            name = _text(props["Name der Veranstaltung"]["title"])
            if "Marktzeit" in name:
                continue
            projects.append({
                "name": name,
                "event_date": _date(props["Termin"]),
                "performers": _text(props["Musiker / Mitwirkende"]["rich_text"]),
                "tasks": _get_tasks(page["id"]),
            })

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


def create_project(name: str, event_date: date) -> str:
    with translate_notion_errors():
        response = _client().pages.create(
            parent={"database_id": PROJECTS_DB},
            properties={
                "Name der Veranstaltung": {
                    "title": [{"text": {"content": name}}]
                },
                "Termin": {
                    "date": {"start": event_date.isoformat()}
                },
                "Status/Aufgaben": {
                    "status": {"name": "geplant / mit Zeitplan"}
                },
            }
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
                    "Aufgabe": {
                        "title": [{"text": {"content": task["name"]}}]
                    },
                    "Wann?": {
                        "date": {"start": task["date"]}
                    },
                    "Done": {
                        "checkbox": False
                    },
                    "Related to Projekte": {
                        "relation": [{"id": project_id}]
                    },
                }
            )