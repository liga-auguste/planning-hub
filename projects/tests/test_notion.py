"""notion.py directly: the read and write paths against the API, mocked."""

import os
from datetime import date
from unittest.mock import Mock, patch

import httpx
from django.test import SimpleTestCase
from notion_client.errors import (
    HTTPResponseError,
    RequestTimeoutError,
)

from ..notion import (
    HISTORY_PROJECT_LIMIT,
    PROJECTS_DB,
    TASKS_DB,
    NotionUnavailableError,
    _get_tasks,
    _query_all_pages,
    create_project,
    create_tasks,
    find_project,
    get_historical_projects,
    get_unassigned_tasks,
    get_upcoming_projects,
    increment_postpone_count,
    rename_task,
    toggle_task,
    trash_task,
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

    def test_trash_task_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                trash_task("task-id")

    def test_rename_task_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                rename_task("task-id", "Neuer Name")

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
            MockClient.return_value.databases.query.return_value = _query_response(
                [
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
            )
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["postpone_count"], 3)
        self.assertEqual(tasks[0]["created_time"], date(2026, 8, 1))

    def test_missing_property_defaults_to_zero(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = _query_response(
                [_fake_task_page("Test", "2026-08-20")]
            )
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
            MockClient.return_value.databases.query.return_value = _query_response(
                [page]
            )
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["completed_date"], date(2026, 8, 22))

    def test_missing_property_is_none(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = _query_response(
                [_fake_task_page("Test", "2026-08-20")]
            )
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


class RenameTaskTest(SimpleTestCase):
    """#239 stage 2: the first genuinely new capability. It writes the
    Aufgabe title property _parse_task_page already reads, so the rename is
    visible everywhere the task is without any other read path changing."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_the_title_property(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            rename_task("task-1", "GEMA-Meldung einreichen")
        instance.pages.update.assert_called_once_with(
            page_id="task-1",
            properties={
                "Aufgabe": {"title": [{"text": {"content": "GEMA-Meldung einreichen"}}]}
            },
        )

    def test_it_writes_nothing_else(self):
        # A title-only update: Wann?, Done and Kontext are other writes'
        # business, and sending them along would overwrite whatever Notion's
        # own UI put there since the page was last read.
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            rename_task("task-1", "Neuer Name")
        self.assertEqual(
            list(instance.pages.update.call_args.kwargs["properties"]), ["Aufgabe"]
        )


class TrashTaskTest(SimpleTestCase):
    """#239 stage 3. The Notion API cannot permanently delete: the page
    moves to the trash through the Update page endpoint and stays
    restorable, which is why the menu says "In den Papierkorb"."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_archives_the_page(self):
        # `archived`, not `in_trash`: the pinned notion-client==2.2.1 sends
        # Notion-Version: 2022-06-28, where that is the field. A version
        # bump has to come past the call site, which says so.
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            trash_task("task-1")
        instance.pages.update.assert_called_once_with(page_id="task-1", archived=True)

    def test_the_pinned_client_still_sends_the_version_that_field_belongs_to(self):
        from notion_client.client import ClientOptions

        self.assertEqual(ClientOptions.notion_version, "2022-06-28")


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
            instance.databases.query.return_value = _query_response([])
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
            MockClient.return_value.databases.query.return_value = _query_response(
                [_fake_task_page("Blumen besorgen", "2026-09-01")]
            )
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
            MockClient.return_value.databases.query.return_value = _query_response(
                [{"id": "page-1"}]
            )
            self.assertEqual(find_project("Sommerkonzert", date(2026, 9, 5)), "page-1")

    def test_queries_by_exact_name_and_date(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = _query_response([])
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
            MockClient.return_value.databases.query.return_value = _query_response([])
            self.assertIsNone(find_project("Sommerkonzert", date(2026, 9, 5)))

    def test_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                find_project("Sommerkonzert", date(2026, 9, 5))


def _query_response(results, has_more=False, next_cursor=None):
    """A databases.query response the way Notion actually returns one.

    has_more/next_cursor are always present on a real response, and
    _query_all_pages indexes has_more directly — a stub should reproduce the
    format rather than have the code route around its absence.
    """
    return {"results": results, "has_more": has_more, "next_cursor": next_cursor}


def _fake_task_page(name, iso_date, project_ids=()):
    # Shaped the way _get_tasks parses a Notion task page.
    return {
        "id": "task-1",
        "properties": {
            "Aufgabe": {"title": [{"plain_text": name}]},
            "Wann?": {"date": {"start": iso_date}},
            "Done": {"checkbox": False},
            "Kontext": {"multi_select": []},
            "Related to Projekte": {"relation": [{"id": pid} for pid in project_ids]},
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
            instance.databases.query.return_value = _query_response(
                [_fake_task_page("Programm festlegen", "2026-08-20")]
            )
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
            instance.databases.query.return_value = _query_response([])
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
            instance.databases.query.return_value = _query_response(
                [_fake_task_page("Programm festlegen", "2026-08-20")]
            )
            create_tasks(
                "project-id", [{"name": "Programm festlegen", "date": "2026-08-27"}]
            )
        self.assertEqual(instance.pages.create.call_count, 1)

    def test_writes_kontext_as_a_multi_select_property(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = _query_response([])
            create_tasks(
                "project-id",
                [{"name": "GEMA-Meldung", "date": "2026-08-20", "kontext": ["Büro"]}],
            )
        created = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(created["Kontext"], {"multi_select": [{"name": "Büro"}]})


def _fake_project_page(page_id, name, iso_date):
    # Shaped the way get_historical_projects/get_upcoming_projects parse a
    # Notion project page.
    return {
        "id": page_id,
        "properties": {
            "Name der Veranstaltung": {"title": [{"plain_text": name}]},
            "Termin": {"date": {"start": iso_date}},
            "Musiker / Mitwirkende": {"rich_text": []},
            "Status/Aufgaben": {"status": {"name": "geplant", "color": "blue"}},
        },
    }


class HistoricalProjectsCapTest(SimpleTestCase):
    """#225: get_historical_projects is the one read that loses rows today —
    102 matching projects, 100 returned. It gets a deliberate cap rather than
    pagination, because everything it returns goes whole into every planner
    prompt."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _project_queries(self, query_mock):
        return [
            call
            for call in query_mock.call_args_list
            if call.kwargs.get("database_id") == PROJECTS_DB
        ]

    def test_asks_notion_for_exactly_the_capped_number(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = _query_response([])
            get_historical_projects()
        self.assertEqual(
            self._project_queries(query)[0].kwargs["page_size"],
            HISTORY_PROJECT_LIMIT,
        )

    def test_the_marktzeit_exclusion_is_part_of_the_notion_filter(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = _query_response([])
            get_historical_projects()
        conditions = self._project_queries(query)[0].kwargs["filter"]["and"]
        self.assertIn(
            {
                "property": "Name der Veranstaltung",
                "title": {"does_not_contain": "Marktzeit"},
            },
            conditions,
        )
        self.assertIn(
            {"property": "Status/Aufgaben", "status": {"equals": "abgeschlossen"}},
            conditions,
        )

    def test_a_marktzeit_project_notion_still_returns_is_kept(self):
        """The Python `continue` is gone: filtering twice would make the cap
        mean "40 minus however many Marktzeit rows fall inside it"."""
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [_fake_project_page("p1", "Marktzeit Mai", "2026-05-01")]
                ),
                _query_response([]),
            ]
            projects = get_historical_projects()
        self.assertEqual([p["name"] for p in projects], ["Marktzeit Mai"])

    def test_does_not_page_past_the_cap(self):
        """has_more is expected here — the bound is a decision, not a
        leftover first page."""
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [_fake_project_page("p1", "Sommerkonzert", "2026-05-01")],
                    has_more=True,
                    next_cursor="cursor-1",
                ),
                _query_response([]),
            ]
            get_historical_projects()
        self.assertEqual(len(self._project_queries(query)), 1)


class QueryAllPagesTest(SimpleTestCase):
    """#196: _query_all_pages arrived with #215 but was only ever stubbed at
    view level, so its paging loop had never run in the suite. Three reads
    route through it now — covering it comes first."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_single_page_is_returned_as_is(self):
        client = Mock()
        client.databases.query.return_value = _query_response([{"id": "a"}])
        self.assertEqual(_query_all_pages(client, database_id=TASKS_DB), [{"id": "a"}])
        self.assertEqual(client.databases.query.call_count, 1)

    def test_a_second_page_is_fetched_with_the_cursor_and_concatenated(self):
        client = Mock()
        client.databases.query.side_effect = [
            _query_response([{"id": "a"}], has_more=True, next_cursor="cursor-1"),
            _query_response([{"id": "b"}]),
        ]
        results = _query_all_pages(client, database_id=TASKS_DB)
        self.assertEqual(results, [{"id": "a"}, {"id": "b"}])
        self.assertNotIn(
            "start_cursor", client.databases.query.call_args_list[0].kwargs
        )
        self.assertEqual(
            client.databases.query.call_args_list[1].kwargs["start_cursor"], "cursor-1"
        )

    def test_has_more_without_a_cursor_stops_instead_of_looping(self):
        """Notion breaking its own contract. Paging on would repeat the
        cursor-less first request forever, inside a web request — the
        side_effect list is what turns that into a failure instead of a
        hang."""
        client = Mock()
        client.databases.query.side_effect = [
            _query_response([{"id": "a"}], has_more=True, next_cursor=None)
        ] * 3
        with self.assertLogs("projects.notion", level="WARNING") as cm:
            results = _query_all_pages(client, database_id=TASKS_DB)
        self.assertEqual(results, [{"id": "a"}])
        self.assertEqual(client.databases.query.call_count, 1)
        self.assertIn("has_more without a next_cursor", cm.output[0])

    def test_the_query_is_repeated_unchanged_on_every_page(self):
        client = Mock()
        client.databases.query.side_effect = [
            _query_response([], has_more=True, next_cursor="cursor-1"),
            _query_response([]),
        ]
        _query_all_pages(client, database_id=TASKS_DB, filter={"x": 1})
        for call in client.databases.query.call_args_list:
            self.assertEqual(call.kwargs["database_id"], TASKS_DB)
            self.assertEqual(call.kwargs["filter"], {"x": 1})


class TasksInOneQueryTest(SimpleTestCase):
    """#196: get_upcoming_projects/get_historical_projects used to query
    TASKS_DB once per project — a cold planner start was ~100 sequential
    requests. One `or` filter over the concrete project ids replaces the
    fan-out with a single read."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task_query(self, query_mock):
        calls = [
            call
            for call in query_mock.call_args_list
            if call.kwargs.get("database_id") == TASKS_DB
        ]
        return calls

    def test_three_projects_cost_two_queries_not_four(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [
                        _fake_project_page("p1", "Konzert", "2026-09-01"),
                        _fake_project_page("p2", "Vesper", "2026-09-08"),
                        _fake_project_page("p3", "Andacht", "2026-09-15"),
                    ]
                ),
                _query_response([]),
            ]
            get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual(query.call_count, 2)

    def test_the_task_query_filters_on_every_project_id(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [
                        _fake_project_page("p1", "Konzert", "2026-09-01"),
                        _fake_project_page("p2", "Vesper", "2026-09-08"),
                    ]
                ),
                _query_response([]),
            ]
            get_upcoming_projects(date(2026, 9, 1))
        conditions = self._task_query(query)[0].kwargs["filter"]["or"]
        self.assertEqual(
            conditions,
            [
                {"property": "Related to Projekte", "relation": {"contains": "p1"}},
                {"property": "Related to Projekte", "relation": {"contains": "p2"}},
            ],
        )

    def test_tasks_are_grouped_onto_the_project_they_relate_to(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [
                        _fake_project_page("p1", "Konzert", "2026-09-01"),
                        _fake_project_page("p2", "Vesper", "2026-09-08"),
                    ]
                ),
                _query_response(
                    [
                        _fake_task_page("Programm", "2026-08-20", ["p1"]),
                        _fake_task_page("Liedzettel", "2026-08-25", ["p2"]),
                    ]
                ),
            ]
            projects = get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual([t["name"] for t in projects[0]["tasks"]], ["Programm"])
        self.assertEqual([t["name"] for t in projects[1]["tasks"]], ["Liedzettel"])

    def test_a_task_related_to_two_projects_appears_under_both(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [
                        _fake_project_page("p1", "Konzert", "2026-09-01"),
                        _fake_project_page("p2", "Vesper", "2026-09-08"),
                    ]
                ),
                _query_response(
                    [_fake_task_page("Noten kopieren", "2026-08-20", ["p1", "p2"])]
                ),
            ]
            projects = get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual([t["name"] for t in projects[0]["tasks"]], ["Noten kopieren"])
        self.assertEqual([t["name"] for t in projects[1]["tasks"]], ["Noten kopieren"])

    def test_hyphenated_and_bare_relation_ids_match(self):
        """Notion returns page ids both with and without hyphens depending on
        context, so the grouping compares one normalised form."""
        page_id = "1a2b3c4d-0000-4000-8000-000000000001"
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response([_fake_project_page(page_id, "Konzert", "2026-09-01")]),
                _query_response(
                    [
                        _fake_task_page(
                            "Programm", "2026-08-20", [page_id.replace("-", "")]
                        )
                    ]
                ),
            ]
            projects = get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual([t["name"] for t in projects[0]["tasks"]], ["Programm"])

    def test_no_open_projects_means_no_task_query_at_all(self):
        """An empty `or` array is a Notion error, and there is nothing to
        ask about anyway."""
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = _query_response([])
            self.assertEqual(get_upcoming_projects(date(2026, 9, 1)), [])
        self.assertEqual(self._task_query(query), [])

    def test_a_multi_page_task_response_is_paged_through(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response([_fake_project_page("p1", "Konzert", "2026-09-01")]),
                _query_response(
                    [_fake_task_page("Programm", "2026-08-20", ["p1"])],
                    has_more=True,
                    next_cursor="cursor-1",
                ),
                _query_response([_fake_task_page("Plakate", "2026-08-27", ["p1"])]),
            ]
            projects = get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual(
            [t["name"] for t in projects[0]["tasks"]], ["Programm", "Plakate"]
        )

    def test_get_historical_projects_uses_the_same_single_task_query(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [
                        _fake_project_page("p1", "Konzert 2025", "2025-09-01"),
                        _fake_project_page("p2", "Vesper 2025", "2025-09-08"),
                    ]
                ),
                _query_response([_fake_task_page("Programm", "2025-08-20", ["p2"])]),
            ]
            projects = get_historical_projects()
        self.assertEqual(query.call_count, 2)
        self.assertEqual(projects[0]["tasks"], [])
        self.assertEqual([t["name"] for t in projects[1]["tasks"]], ["Programm"])


class ProjectlessAndProjectReadsPaginateTest(SimpleTestCase):
    """#196/#225: Notion caps a query at 100 rows and signals the cut with
    has_more. get_unassigned_tasks is the one to watch — it is the #53
    "Kleinkram" bucket, nothing ever removes rows from it, and at 70 rows it
    is 70% of the way to tasks silently disappearing from the dashboard."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_unassigned_tasks_pages_through_every_result(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [_fake_task_page("Blumen besorgen", "2026-09-01")],
                    has_more=True,
                    next_cursor="cursor-1",
                ),
                _query_response(
                    [_fake_task_page("Kerzen nachbestellen", "2026-09-02")]
                ),
            ]
            tasks = get_unassigned_tasks(date(2026, 8, 31))
        self.assertEqual(
            [t["name"] for t in tasks], ["Blumen besorgen", "Kerzen nachbestellen"]
        )

    def test_get_upcoming_projects_pages_through_every_project(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.side_effect = [
                _query_response(
                    [_fake_project_page("p1", "Konzert", "2026-09-01")],
                    has_more=True,
                    next_cursor="cursor-1",
                ),
                _query_response([_fake_project_page("p2", "Vesper", "2026-09-08")]),
                _query_response([]),
            ]
            projects = get_upcoming_projects(date(2026, 9, 1))
        self.assertEqual([p["name"] for p in projects], ["Konzert", "Vesper"])

    def test_the_idempotency_read_in_create_tasks_pages_too(self):
        """create_tasks keeps the single-project read; unpaged, its skip-what-
        exists check would silently stop working past 100 tasks."""
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.side_effect = [
                _query_response(
                    [_fake_task_page("Programm festlegen", "2026-08-20")],
                    has_more=True,
                    next_cursor="cursor-1",
                ),
                _query_response([_fake_task_page("Plakate aushängen", "2026-08-27")]),
            ]
            create_tasks(
                "project-id",
                [
                    {"name": "Programm festlegen", "date": "2026-08-20"},
                    {"name": "Plakate aushängen", "date": "2026-08-27"},
                ],
            )
        self.assertEqual(instance.pages.create.call_count, 0)
