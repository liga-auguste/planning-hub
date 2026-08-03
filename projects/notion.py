import os
from datetime import date
from notion_client import Client

PROJECTS_DB = "87ad1e35b3344ed49c1ba977664bb087"
TASKS_DB = "f22abd16a92d48598c04be76f35c6b1d"

def _client():
    return Client(auth=os.environ["NOTION_API_KEY"])

def get_upcoming_projects(today: date) -> list:
    response = _client().databases.query(
        database_id=PROJECTS_DB,
        filter={
            "property": "Termin",
            "date": {"on_or_after": today.isoformat()}
        },
        sorts=[{"property": "Termin", "direction": "ascending"}]
    )

    projects = []
    for page in response["results"]:
        props = page["properties"]
        project = {
            "id": page["id"],
            "name": _text(props["Name der Veranstaltung"]["title"]),
            "event_date": _date(props["Termin"]),
            "performers": _text(props["Musiker / Mitwirkende"]["rich_text"]),
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
        })

    return tasks


def toggle_task(task_id: str, done: bool) -> None:
    _client().pages.update(
        page_id=task_id,
        properties={"Done": {"checkbox": done}}
    )


def _text(rich_text_list: list) -> str:
    return "".join(t["plain_text"] for t in rich_text_list)


def _date(date_prop: dict) -> date | None:
    value = date_prop.get("date")
    if value and value.get("start"):
        return date.fromisoformat(value["start"])
    return None