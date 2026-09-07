"""notion.py directly: the read and write paths against the API, mocked."""

import os
from datetime import date
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase
from notion_client.errors import (
    HTTPResponseError,
    RequestTimeoutError,
)

from ..notion import (
    TASKS_DB,
    NotionUnavailableError,
    _get_tasks,
    create_project,
    create_tasks,
    find_project,
    get_historical_projects,
    get_unassigned_tasks,
    get_upcoming_projects,
    increment_postpone_count,
    toggle_task,
    update_task_date,
)


class NotionFailureTranslationTest(SimpleTestCase):
    """notion.py's six public functions each build a fresh client per call
    (_client()) and were entirely unguarded. translate_notion_errors() covers
    notion_client's own exception types (HTTPResponseError, RequestTimeoutError)
    plus httpx.HTTPError as a safety net — notion_client only wraps a timeout,
    not a raw connection failure like httpx.ConnectError."""

    def setUp(self):
        # _client() reads os.environ["NOTION_API_KEY"] directly (a KeyError,
        # not a graceful failure, if unset) — irrelevant to what's under test
        # here, so pin it rather than depend on the ambient environment.
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub_every_call(self, MockClient, exc):
        instance = MockClient.return_value
        instance.databases.query.side_effect = exc
        instance.pages.update.side_effect = exc
        instance.pages.create.side_effect = exc
        instance.pages.retrieve.side_effect = exc
        return instance

    def test_get_upcoming_projects_translates_a_timeout(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                get_upcoming_projects(date.today())

    def test_get_historical_projects_translates_an_http_error(self):
        request = httpx.Request("POST", "https://api.notion.com/v1/databases/x/query")
        response = httpx.Response(500, request=request)
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, HTTPResponseError(response))
            with self.assertRaises(NotionUnavailableError):
                get_historical_projects()

    def test_toggle_task_translates_a_raw_connection_error(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, httpx.ConnectError("boom"))
            with self.assertRaises(NotionUnavailableError):
                toggle_task("task-id", True)

    def test_update_task_date_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                update_task_date("task-id", "2026-09-05")

    def test_create_project_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_project("Test", date.today())

    def test_create_tasks_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_tasks("project-id", [{"name": "x", "date": "2026-09-05"}])

    def test_increment_postpone_count_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                increment_postpone_count("task-id")


class PostponeCountReadFromNotionTest(SimpleTestCase):
    """#171: _get_tasks reads the "Verschoben" number property fresh on
    every fetch, or a task's count would reset to 0 on display even though
    the stored value is correct."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_verschoben_number_property(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [
                    {
                        "id": "task-1",
                        "created_time": "2026-08-01T10:00:00.000Z",
                        "properties": {
                            "Aufgabe": {"title": [{"plain_text": "Test"}]},
                            "Wann?": {"date": {"start": "2026-08-20"}},
                            "Done": {"checkbox": False},
                            "Kontext": {"multi_select": []},
                            "Verschoben": {"number": 3},
                        },
                    }
                ]
            }
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["postpone_count"], 3)
        self.assertEqual(tasks[0]["created_time"], date(2026, 8, 1))

    def test_missing_property_defaults_to_zero(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Test", "2026-08-20")]
            }
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["postpone_count"], 0)
        self.assertIsNone(tasks[0]["created_time"])


class CompletedDateReadFromNotionTest(SimpleTestCase):
    """#19: _get_tasks reads the "Erledigt am" date property Notion gets
    alongside Done — a manually-added property, so a page fetched before it
    existed in the schema must not KeyError."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_erledigt_am_date(self):
        with patch("projects.notion.Client") as MockClient:
            page = _fake_task_page("Test", "2026-08-20")
            page["properties"]["Erledigt am"] = {"date": {"start": "2026-08-22"}}
            page["properties"]["Done"] = {"checkbox": True}
            MockClient.return_value.databases.query.return_value = {"results": [page]}
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["completed_date"], date(2026, 8, 22))

    def test_missing_property_is_none(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Test", "2026-08-20")]
            }
            tasks = _get_tasks("project-id")
        self.assertIsNone(tasks[0]["completed_date"])


class ToggleTaskWritesCompletedDateTest(SimpleTestCase):
    """#19: toggle_task writes Done and Erledigt am in one pages.update call —
    they change together, and there's no read-then-write race to guard
    against here (unlike increment_postpone_count below)."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marking_done_writes_both_properties_together(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            toggle_task("task-1", True, "2026-08-22")
        instance.pages.update.assert_called_once_with(
            page_id="task-1",
            properties={
                "Done": {"checkbox": True},
                "Erledigt am": {"date": {"start": "2026-08-22"}},
            },
        )

    def test_unmarking_clears_the_completed_date(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            toggle_task("task-1", False, None)
        instance.pages.update.assert_called_once_with(
            page_id="task-1",
            properties={"Done": {"checkbox": False}, "Erledigt am": {"date": None}},
        )


class GetUnassignedTasksTest(SimpleTestCase):
    """#53: get_upcoming_projects only ever queries TASKS_DB per project via
    a relation.contains filter — a task with an empty "Related to Projekte"
    relation is never picked up by any existing read path. get_unassigned_tasks
    is the deliberate second read path for that "Kleinkram" residue."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_queries_tasks_db_with_an_is_empty_relation_filter(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            get_unassigned_tasks(date(2026, 8, 31))
        instance.databases.query.assert_called_once_with(
            database_id=TASKS_DB,
            filter={
                "property": "Related to Projekte",
                "relation": {"is_empty": True},
            },
        )

    def test_returns_tasks_with_no_project_relation(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Blumen besorgen", "2026-09-01")]
            }
            tasks = get_unassigned_tasks(date(2026, 8, 31))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Blumen besorgen")
        self.assertEqual(tasks[0]["due"], date(2026, 9, 1))

    def test_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                get_unassigned_tasks(date(2026, 8, 31))


class IncrementPostponeCountTest(SimpleTestCase):
    """#171: read-then-write, since Notion has no atomic increment."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_then_writes_the_incremented_value(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.retrieve.return_value = {
                "properties": {"Verschoben": {"number": 2}}
            }
            result = increment_postpone_count("task-1")
        self.assertEqual(result, 3)
        instance.pages.update.assert_called_once_with(
            page_id="task-1", properties={"Verschoben": {"number": 3}}
        )

    def test_missing_property_starts_at_one(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.retrieve.return_value = {"properties": {}}
            result = increment_postpone_count("task-1")
        self.assertEqual(result, 1)


class CreateProjectDateUncertainTest(SimpleTestCase):
    """create_project's new date_uncertain param writes a "Termin unsicher"
    checkbox, the read-path (get_upcoming_projects/get_historical_projects)
    counterpart to the fallback date planner_review/planner_create apply
    when the description carried no recognizable date."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_to_false(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.create.return_value = {"id": "page-1"}
            create_project("Sommerkonzert", date(2026, 9, 5))
        properties = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(properties["Termin unsicher"], {"checkbox": False})

    def test_true_when_the_date_was_a_guess(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.create.return_value = {"id": "page-1"}
            create_project("Herbstkonzert", date(2026, 10, 3), True)
        properties = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(properties["Termin unsicher"], {"checkbox": True})


class FindProjectTest(SimpleTestCase):
    """find_project makes retrying planner_create idempotent at the project
    level: an attempt that died in create_tasks left a project page behind,
    and the retry must find and reuse it instead of creating a twin."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_id_of_an_exact_match(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [{"id": "page-1"}]
            }
            self.assertEqual(find_project("Sommerkonzert", date(2026, 9, 5)), "page-1")

    def test_queries_by_exact_name_and_date(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = {"results": []}
            find_project("Sommerkonzert", date(2026, 9, 5))
        conditions = query.call_args.kwargs["filter"]["and"]
        self.assertIn(
            {
                "property": "Name der Veranstaltung",
                "title": {"equals": "Sommerkonzert"},
            },
            conditions,
        )
        self.assertIn(
            {"property": "Termin", "date": {"equals": "2026-09-05"}}, conditions
        )

    def test_returns_none_when_nothing_matches(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {"results": []}
            self.assertIsNone(find_project("Sommerkonzert", date(2026, 9, 5)))

    def test_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                find_project("Sommerkonzert", date(2026, 9, 5))


def _fake_task_page(name, iso_date):
    # Shaped the way _get_tasks parses a Notion task page.
    return {
        "id": "task-1",
        "properties": {
            "Aufgabe": {"title": [{"plain_text": name}]},
            "Wann?": {"date": {"start": iso_date}},
            "Done": {"checkbox": False},
            "Kontext": {"multi_select": []},
        },
    }


class CreateTasksIdempotencyTest(SimpleTestCase):
    """A retried save reaches create_tasks with the same list a failed
    attempt may have partially written (one API call per task) — what
    already made it to Notion must be skipped, not created again."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_already_written_tasks_are_skipped(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                "results": [_fake_task_page("Programm festlegen", "2026-08-20")]
            }
            create_tasks(
                "project-id",
                [
                    {"name": "Programm festlegen", "date": "2026-08-20"},
                    {"name": "Plakate aushängen", "date": "2026-08-27"},
                ],
            )
        self.assertEqual(instance.pages.create.call_count, 1)
        created = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(
            created["Aufgabe"]["title"][0]["text"]["content"], "Plakate aushängen"
        )

    def test_a_fresh_project_writes_the_whole_list(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            create_tasks(
                "project-id",
                [
                    {"name": "Programm festlegen", "date": "2026-08-20"},
                    {"name": "Plakate aushängen", "date": "2026-08-27"},
                ],
            )
        self.assertEqual(instance.pages.create.call_count, 2)

    def test_same_name_on_a_different_date_is_not_skipped(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                "results": [_fake_task_page("Programm festlegen", "2026-08-20")]
            }
            create_tasks(
                "project-id", [{"name": "Programm festlegen", "date": "2026-08-27"}]
            )
        self.assertEqual(instance.pages.create.call_count, 1)

    def test_writes_kontext_as_a_multi_select_property(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            create_tasks(
                "project-id",
                [{"name": "GEMA-Meldung", "date": "2026-08-20", "kontext": ["Büro"]}],
            )
        created = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(created["Kontext"], {"multi_select": [{"name": "Büro"}]})
