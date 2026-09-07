"""Wochenabschluss: the close-out ritual, its backends and its summary."""

import re
from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

from django.db import (
    IntegrityError,
    transaction,
)
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..ai import (
    AIUnavailableError,
    build_closeout_prompt,
    generate_closeout_summary,
)
from ..closeout import (
    get_latest_closeout,
    is_week_closed,
    save_closeout,
)
from ..date_format import format_date
from ..dates import iso_week_bounds
from ..models import WeekCloseout
from ..notion import NotionUnavailableError
from .base import (
    CLOSEOUT_TODAY,
    AiStubMixin,
    DemoModeTestCase,
    _anthropic_timeout_error,
    _closeout_tasks,
    _fake_stream,
)

VALID_CLOSEOUT_JSON = '{"summary_text": "Gute Woche gewesen."}'


def _closeout_stats():
    return {"completed_count": 3, "rescheduled_count": 1, "added_count": 2}


class GenerateCloseOutSummaryTest(SimpleTestCase):
    """#169: generate_closeout_summary parses Claude's response with the
    same retry contract as generate_weekly_summary (GenerateWeeklySummaryRetryTest):
    one re-ask on unparseable or wrong-shape JSON, AIUnavailableError after
    the second bad response, SDK failures never spent as a JSON retry."""

    def generate(self):
        return generate_closeout_summary(_closeout_stats(), date(2026, 9, 1))

    def test_returns_the_summary_text_on_first_valid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(VALID_CLOSEOUT_JSON)
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")
        self.assertEqual(stream.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream(VALID_CLOSEOUT_JSON),
            ]
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")
        self.assertEqual(stream.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream("immer noch kein json"),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_valid_json_in_the_wrong_shape_is_retried(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream('{"other_key": "x"}'),
                _fake_stream('{"summary_text": 5}'),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(f"```json\n{VALID_CLOSEOUT_JSON}\n```")
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")

    def test_prompt_names_the_three_stats_and_asks_for_json(self):
        prompt = build_closeout_prompt(_closeout_stats(), date(2026, 9, 1))
        self.assertIn("In dieser Woche erledigt: 3 Aufgaben", prompt)
        self.assertIn("In dieser Woche neu dazugekommen: 2 Aufgaben", prompt)
        self.assertIn("summary_text", prompt)

    def test_the_reschedule_line_is_scoped_to_the_close_out_not_the_week(self):
        """#215: the number describes this one interaction, so the prompt has
        to say so — Claude echoes the framing it is given, and the old
        wording invited it to narrate a session-scoped count as a fact about
        the week."""
        prompt = build_closeout_prompt(_closeout_stats(), date(2026, 9, 1))
        self.assertIn(
            "Gerade beim Abschließen in die nächste Woche verschoben: 1 Aufgaben",
            prompt,
        )
        self.assertNotIn("Verschoben in die nächste Woche: 1", prompt)

    def test_an_unmeasured_added_count_leaves_the_line_out(self):
        """#215: a demo close-out passes None rather than 0 — stating a zero
        the number can never leave is what this issue removed elsewhere."""
        stats = {**_closeout_stats(), "added_count": None}
        prompt = build_closeout_prompt(stats, date(2026, 9, 1))
        self.assertNotIn("dazugekommen", prompt)
        self.assertIn("In dieser Woche erledigt: 3 Aufgaben", prompt)


class WeekCloseoutModelTest(TestCase):
    def test_unique_constraint_on_iso_year_and_week(self):
        WeekCloseout.objects.create(iso_year=2026, iso_week=25)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WeekCloseout.objects.create(iso_year=2026, iso_week=25)

    def test_str_shows_the_iso_week(self):
        closeout = WeekCloseout.objects.create(iso_year=2026, iso_week=25)
        self.assertEqual(str(closeout), "KW25/2026")


class CloseoutBackendTest(TestCase):
    """closeout.py direct: two backends behind one interface, the same shape
    as rules.py — the views never learn which backend answered."""

    def request(self):
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    @override_settings(DEMO_MODE=False)
    def test_production_round_trip(self):
        request = self.request()
        self.assertFalse(is_week_closed(request, 2026, 25))
        save_closeout(request, 2026, 25, _closeout_stats(), "Text.")
        self.assertTrue(is_week_closed(request, 2026, 25))
        self.assertEqual(get_latest_closeout(request)["summary_text"], "Text.")

    @override_settings(DEMO_MODE=True)
    def test_demo_round_trip(self):
        request = self.request()
        self.assertFalse(is_week_closed(request, 2026, 25))
        save_closeout(request, 2026, 25, _closeout_stats(), "Text.")
        self.assertTrue(is_week_closed(request, 2026, 25))
        self.assertEqual(get_latest_closeout(request)["summary_text"], "Text.")

    @override_settings(DEMO_MODE=True)
    def test_no_closeout_yet_is_none(self):
        self.assertIsNone(get_latest_closeout(self.request()))

    @override_settings(DEMO_MODE=False)
    def test_production_no_closeout_yet_is_none(self):
        self.assertIsNone(get_latest_closeout(self.request()))


class CloseWeekStartDemoModeTest(DemoModeTestCase):
    """#169: the triage list is only open tasks due in the current ISO
    week — overdue tasks stay out (their own signal already), and so do
    tasks already done or due a different week."""

    @patch("django.utils.timezone.localdate")
    def test_lists_only_open_tasks_due_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche")
        self.assertNotContains(response, "Nächste Woche")
        self.assertNotContains(response, "Überfällig")
        self.assertNotContains(response, "Schon erledigt")

    def test_no_session_plan_redirects_to_index(self):
        response = self.client.get(reverse("close_week_start"))
        self.assertRedirects(response, reverse("index"))

    @patch("django.utils.timezone.localdate")
    def test_nothing_open_this_week_is_an_empty_state(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY  # a Monday
        self.given_session_plan(tasks=[])
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(
            response, "Für diese Woche ist alles erledigt oder verschoben."
        )

    @patch("django.utils.timezone.localdate")
    def test_the_empty_state_greets_the_weekend_on_a_weekend(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=5)  # Saturday
        self.given_session_plan(tasks=[])
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Genieße dein Wochenende")

    @patch("django.utils.timezone.localdate")
    def test_the_move_button_shows_the_target_date_not_a_generic_label(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        # "Diese Woche" +2 days is due 2026-06-17, +7 = 2026-06-24, a Wednesday.
        self.assertContains(response, "→ Mi, 24. Juni")
        self.assertNotContains(response, "→ nächste Woche")

    @patch("django.utils.timezone.localdate")
    def test_the_triage_row_shows_the_tasks_own_due_date(self, mock_localdate):
        # #189: this date used to be a string the view precomputed; the
        # template formats task.due itself now, and nothing else in the
        # suite pins what the triage row renders.
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, format_date(CLOSEOUT_TODAY + timedelta(days=2)))

    @patch("django.utils.timezone.localdate")
    def test_already_closed_and_nothing_open_offers_a_refresh(self, mock_localdate):
        # #215: the button used to be hidden here, which froze the numbers in
        # the one case where they go stale fastest — a week finished off
        # after it was closed. It stays, relabelled for what it now does.
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=[])
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 3,
            "rescheduled_count": 1,
            "added_count": 0,
            "summary_text": "Text.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche hast du schon abgeschlossen.")
        self.assertContains(response, "→ Rückblick ansehen")
        self.assertContains(response, "Rückblick aktualisieren</button>")
        self.assertNotContains(response, "Woche abschließen</button>")
        # The subtitle used to say "noch offene Aufgaben dieser Woche" even
        # here — the same contradiction as the empty-state text, one line up.
        self.assertContains(response, "bereits abgeschlossen</div>")
        self.assertNotContains(response, "noch offene Aufgaben dieser Woche")


@override_settings(DEMO_MODE=False)
class CloseWeekStartProductionTest(TestCase):
    def _project(self, tasks):
        return [
            {
                "id": "p1",
                "name": "Projekt",
                "event_date": CLOSEOUT_TODAY + timedelta(days=30),
                "event_date_uncertain": False,
                "performers": "",
                "status": None,
                "status_color": "gray",
                "tasks": tasks,
            }
        ]

    def _task(self, task_id, name, due, done=False):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": None,
        }

    @patch("django.utils.timezone.localdate")
    def test_lists_only_open_tasks_due_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        tasks = [
            self._task(
                "t-this-week", "Diese Woche", CLOSEOUT_TODAY + timedelta(days=2)
            ),
            self._task(
                "t-next-week", "Nächste Woche", CLOSEOUT_TODAY + timedelta(days=9)
            ),
        ]
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project(tasks)
        ):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche")
        self.assertNotContains(response, "Nächste Woche")

    def test_notion_failure_redirects_to_dashboard(self):
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            response = self.client.get(reverse("close_week_start"))
        # fetch_redirect_response=False: dashboard()'s own Notion-failure
        # behavior has its own tests (DashboardNotionFailureTest); the mock
        # above is out of scope by the time a live follow-up GET would run.
        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )

    @patch("django.utils.timezone.localdate")
    def test_already_closed_and_nothing_open_offers_a_refresh(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=25, completed_count=3, rescheduled_count=1
        )
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche hast du schon abgeschlossen.")
        self.assertContains(response, "Rückblick aktualisieren</button>")
        self.assertNotContains(response, "Woche abschließen</button>")
        self.assertContains(response, "→ Rückblick ansehen")
        self.assertContains(response, "bereits abgeschlossen</div>")
        self.assertNotContains(response, "noch offene Aufgaben dieser Woche")


class CloseWeekConfirmDemoModeTest(DemoModeTestCase):
    """#215: "Erledigt" counts the ISO week from the session tasks' own
    completion dates, never the posted task_id list — that list can only
    ever hold tasks close_week_start left open. Only rescheduled_count still
    reads it, because moving a task on is what this one interaction does."""

    @patch("django.utils.timezone.localdate")
    def test_completed_counts_the_week_and_rescheduled_counts_the_interaction(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-done",
                    "name": "Erledigt",
                    "date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                    "done": True,
                    "completed_date": CLOSEOUT_TODAY.isoformat(),
                },
                {
                    "id": "t-done-last-week",
                    "name": "Letzte Woche erledigt",
                    "date": (CLOSEOUT_TODAY - timedelta(days=8)).isoformat(),
                    "done": True,
                    "completed_date": (CLOSEOUT_TODAY - timedelta(days=8)).isoformat(),
                },
                {
                    "id": "t-moved",
                    "name": "Verschoben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=9)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-stayed",
                    "name": "Geblieben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=2)).isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.post(
            reverse("close_week_confirm"),
            # t-done is deliberately absent: close_week_start filters out
            # anything already done, so the real page can never post it.
            data={"task_id": ["t-moved", "t-stayed"]},
        )
        self.assertRedirects(response, reverse("week_review"))
        closeout = self.client.session["demo_week_closeout"]
        self.assertEqual(closeout["completed_count"], 1)
        self.assertEqual(closeout["rescheduled_count"], 1)
        self.assertEqual(closeout["added_count"], 0)
        self.assertEqual(closeout["summary_text"], "Gute Woche gewesen.")

    @patch("django.utils.timezone.localdate")
    def test_a_task_done_without_a_completion_date_is_not_counted(self, mock_localdate):
        """#215: the documented floor. A session written before #19 has
        `done` and no date, and a count that cannot place a task in a week
        must not guess — asserted so nobody later "fixes" it into counting
        `done` and puts two scopes back in one row."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-undated",
                    "name": "Ohne Datum erledigt",
                    "date": CLOSEOUT_TODAY.isoformat(),
                    "done": True,
                },
            ]
        )
        self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(
            self.client.session["demo_week_closeout"]["completed_count"], 0
        )

    def test_the_timelapse_completions_count(self):
        """#215: the demo's headline feature completes tasks on a deepcopy
        that is never written back, so they carry no completion date. The due
        date is what made them done — without that branch a time-travelled
        demo reports the same 0 this issue removed."""
        monday = date(2026, 6, 15)
        sim = monday + timedelta(days=3)
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-a",
                    "name": "Frueh faellig",
                    "date": (monday + timedelta(days=1)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-b",
                    "name": "Am Simulationstag faellig",
                    "date": sim.isoformat(),
                    "done": False,
                },
                {
                    "id": "t-c",
                    "name": "Spaeter faellig",
                    "date": (monday + timedelta(days=6)).isoformat(),
                    "done": False,
                },
            ]
        )
        self.given_timelapse_moments(sim.isoformat())
        session = self.client.session
        session["demo_sim_date"] = sim.isoformat()
        session.save()
        self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        closeout = self.client.session["demo_week_closeout"]
        self.assertEqual(closeout["completed_count"], 2)
        # The week under the timelapse, not the real calendar week.
        self.assertEqual(closeout["iso_week"], sim.isocalendar()[1])

    def test_the_review_hides_the_added_tile_in_demo(self):
        """#215: a demo plan is created in one shot, so the count is
        structurally 0 — a tile that can never move is the shape of defect
        this issue removed, not a thing to keep."""
        self.given_session_plan(tasks=[])
        self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        response = self.client.get(reverse("week_review"))
        self.assertContains(response, "Erledigt</div>")
        self.assertNotContains(response, "Neu dazugekommen")

    def test_no_session_plan_redirects_to_index(self):
        response = self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertRedirects(response, reverse("index"))

    def test_get_redirects_to_start(self):
        self.given_session_plan()
        response = self.client.get(reverse("close_week_confirm"))
        self.assertRedirects(response, reverse("close_week_start"))

    @patch("django.utils.timezone.localdate")
    def test_ai_failure_still_saves_the_closeout_with_an_empty_summary(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=[])
        self.ai_mocks[
            "projects.views.generate_closeout_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertRedirects(response, reverse("week_review"))
        self.assertEqual(self.client.session["demo_week_closeout"]["summary_text"], "")


@override_settings(DEMO_MODE=False)
class CloseWeekConfirmProductionTest(AiStubMixin, TestCase):
    def _task(self, task_id, name, due, done=False, created_time=None):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": created_time,
            "completed_date": due if done else None,
        }

    def _project(self, tasks):
        return [
            {
                "id": "p1",
                "name": "Projekt",
                "event_date": CLOSEOUT_TODAY + timedelta(days=30),
                "event_date_uncertain": False,
                "performers": "",
                "status": None,
                "status_color": "gray",
                "tasks": tasks,
            }
        ]

    def _week_reads(self, completed=(), created=()):
        """#215: the two independent TASKS_DB reads the counts now come
        from. They are what makes the count reach a task the project-keyed
        path cannot see, so a test that stubs get_upcoming_projects alone no
        longer says anything about the numbers."""
        return (
            patch(
                "projects.views.get_tasks_completed_in_range",
                return_value=list(completed),
            ),
            patch(
                "projects.views.get_tasks_created_in_range", return_value=list(created)
            ),
        )

    @patch("django.utils.timezone.localdate")
    def test_added_count_comes_from_the_week_read_not_from_the_posted_list(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        completed_read, created_read = self._week_reads(
            completed=[self._task("t-done", "Erledigt", CLOSEOUT_TODAY, done=True)],
            created=[self._task("t-new", "Neu", CLOSEOUT_TODAY)],
        )
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read,
            created_read,
            patch(
                "projects.views.generate_closeout_summary", return_value="Rückschau."
            ),
        ):
            response = self.client.post(
                reverse("close_week_confirm"), data={"task_id": []}
            )
        # fetch_redirect_response=False: week_review() builds the sidebar
        # project list (#185) and would fetch Notion on the follow-up GET,
        # outside the patch above. What it renders has its own test.
        self.assertRedirects(
            response, reverse("week_review"), fetch_redirect_response=False
        )
        closeout = WeekCloseout.objects.get()
        # Neither number had a posted id behind it, and the triage list was
        # empty — the case that used to guarantee a zero.
        self.assertEqual(closeout.completed_count, 1)
        self.assertEqual(closeout.added_count, 1)
        self.assertEqual(closeout.summary_text, "Rückschau.")

    @patch("django.utils.timezone.localdate")
    def test_a_dated_task_that_is_not_done_is_not_counted(self, mock_localdate):
        """#215: "Erledigt" means the Done checkbox. A stray "Erledigt am"
        set by hand in Notion without ticking Done is not a completion."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        completed_read, created_read = self._week_reads(
            completed=[
                self._task("t-done", "Erledigt", CLOSEOUT_TODAY, done=True),
                self._task("t-dated-only", "Nur datiert", CLOSEOUT_TODAY, done=False),
            ]
        )
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read,
            created_read,
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(WeekCloseout.objects.get().completed_count, 1)

    @patch("django.utils.timezone.localdate")
    def test_the_week_reads_get_the_iso_week_bounds(self, mock_localdate):
        """#215: Monday to Sunday of the week the review is headlined with —
        the same bounds is_same_iso_week compares by."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        completed_read, created_read = self._week_reads()
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read as completed_mock,
            created_read as created_mock,
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        monday, sunday = iso_week_bounds(CLOSEOUT_TODAY)
        completed_mock.assert_called_once_with(monday, sunday)
        created_mock.assert_called_once_with(monday, sunday)

    @patch("django.utils.timezone.localdate")
    def test_a_failing_week_read_does_not_persist_a_zeroed_closeout(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            patch(
                "projects.views.get_tasks_completed_in_range",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            response = self.client.post(
                reverse("close_week_confirm"), data={"task_id": []}
            )
        self.assertEqual(
            response["Location"].split("?")[0], reverse("close_week_start")
        )
        self.assertEqual(WeekCloseout.objects.count(), 0)

    def _failing_close_out(self, failing_read="get_tasks_completed_in_range"):
        """POSTs a close-out whose Notion read dies, and returns the redirect
        target — ticket and all, which is what a browser would follow."""
        stubs = {
            "get_upcoming_projects": patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            "get_tasks_completed_in_range": patch(
                "projects.views.get_tasks_completed_in_range", return_value=[]
            ),
            "get_tasks_created_in_range": patch(
                "projects.views.get_tasks_created_in_range", return_value=[]
            ),
        }
        stubs[failing_read] = patch(
            f"projects.views.{failing_read}",
            side_effect=NotionUnavailableError("boom"),
        )
        with (
            stubs["get_upcoming_projects"],
            stubs["get_tasks_completed_in_range"],
            stubs["get_tasks_created_in_range"],
        ):
            response = self.client.post(
                reverse("close_week_confirm"), data={"task_id": []}
            )
        return response["Location"]

    @patch("django.utils.timezone.localdate")
    def test_a_failed_close_out_says_so_on_the_page_it_lands_on(self, mock_localdate):
        """#215: the redirect used to be wordless — the button looked like it
        had simply done nothing."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out()
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            landed = self.client.get(landing)
        self.assertContains(landed, "Notion war gerade nicht erreichbar")

    @patch("django.utils.timezone.localdate")
    def test_the_failure_notice_is_shown_once_and_then_gone(self, mock_localdate):
        """The ticket is consumed on the match, so reloading the failed tab —
        same URL, same ticket — stops warning about a failure that is over."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out()
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            first = self.client.get(landing)
            second = self.client.get(landing)
        self.assertContains(first, "Notion war gerade nicht erreichbar")
        self.assertNotContains(second, "Notion war gerade nicht erreichbar")

    @patch("django.utils.timezone.localdate")
    def test_a_second_tab_neither_sees_the_notice_nor_eats_it(self, mock_localdate):
        """#215: a bare session flag went to whichever request arrived first,
        which in a second open tab is a notice about a failure that tab never
        had — and the tab that earned it then got nothing. The ticket has to
        match, and a request without one must leave it untouched."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out()
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            other_tab = self.client.get(reverse("close_week_start"))
            failed_tab = self.client.get(landing)
        self.assertNotContains(other_tab, "Notion war gerade nicht erreichbar")
        self.assertContains(failed_tab, "Notion war gerade nicht erreichbar")

    @patch("django.utils.timezone.localdate")
    def test_a_forged_ticket_shows_nothing(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._failing_close_out()
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            guessed = self.client.get(
                reverse("close_week_start") + "?notice=nicht-das-ticket"
            )
        self.assertNotContains(guessed, "Notion war gerade nicht erreichbar")

    @patch("django.utils.timezone.localdate")
    def test_a_failing_project_read_also_says_so(self, mock_localdate):
        """The triage read is the third Notion call in this one POST — all
        three failure paths land on the same spoken notice."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out(failing_read="get_upcoming_projects")
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            landed = self.client.get(landing)
        self.assertContains(landed, "Notion war gerade nicht erreichbar")
        self.assertEqual(WeekCloseout.objects.count(), 0)

    @patch("django.utils.timezone.localdate")
    def test_reclosing_the_same_week_updates_not_duplicates(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        for text in ("Erster Text.", "Zweiter Text."):
            completed_read, created_read = self._week_reads()
            with (
                patch(
                    "projects.views.get_upcoming_projects",
                    return_value=self._project([]),
                ),
                completed_read,
                created_read,
                patch("projects.views.generate_closeout_summary", return_value=text),
            ):
                self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(WeekCloseout.objects.count(), 1)
        self.assertEqual(WeekCloseout.objects.get().summary_text, "Zweiter Text.")

    @patch("django.utils.timezone.localdate")
    def test_reclosing_refreshes_the_week_scoped_counts(self, mock_localdate):
        """#215: the point of dropping the submit-button guard. Finish a week,
        close it, finish two more things, close again — the review has to
        follow, and it can, because neither count reads the posted list."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        first = [self._task("t-1", "Eins", CLOSEOUT_TODAY, done=True)]
        later = first + [
            self._task("t-2", "Zwei", CLOSEOUT_TODAY, done=True),
            self._task("t-3", "Drei", CLOSEOUT_TODAY, done=True),
        ]
        for completed in (first, later):
            completed_read, created_read = self._week_reads(completed=completed)
            with (
                patch(
                    "projects.views.get_upcoming_projects",
                    return_value=self._project([]),
                ),
                completed_read,
                created_read,
                patch("projects.views.generate_closeout_summary", return_value="Text."),
            ):
                self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(WeekCloseout.objects.count(), 1)
        self.assertEqual(WeekCloseout.objects.get().completed_count, 3)


@override_settings(DEMO_MODE=False)
class CloseWeekWeekScopedCountTest(AiStubMixin, TestCase):
    """#215: the regression test that was missing.

    Both original confirm tests hand-built a task_id list containing an
    already-done task — an input close_week_start cannot produce, since it
    filters `not t["done"]`. So the diff behaved as designed and the bug
    lived underneath it. This class drives the real flow instead: render the
    page, take the ids it actually emitted, post exactly those.
    """

    def _task(self, task_id, name, due, done=False):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": None,
            "completed_date": due if done else None,
        }

    def _project(self, tasks, status=None):
        return {
            "id": "p1",
            "name": "Projekt",
            "event_date": CLOSEOUT_TODAY + timedelta(days=30),
            "event_date_uncertain": False,
            "performers": "",
            "status": status,
            "status_color": "gray",
            "tasks": tasks,
        }

    def _posted_ids(self, response):
        """The task_id values the triage page actually rendered."""
        return re.findall(r'name="task_id" value="([^"]+)"', response.content.decode())

    @patch("django.utils.timezone.localdate")
    def test_a_fully_worked_week_reports_what_it_completed(self, mock_localdate):
        """The case that used to guarantee a zero, and guaranteed it hardest:
        everything was already done when the page loaded, so the triage list
        is empty and there is no id to post."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        finished = [
            self._task("t-1", "Eins", CLOSEOUT_TODAY, done=True),
            self._task("t-2", "Zwei", CLOSEOUT_TODAY + timedelta(days=1), done=True),
            self._task("t-3", "Drei", CLOSEOUT_TODAY + timedelta(days=2), done=True),
        ]
        upcoming = patch(
            "projects.views.get_upcoming_projects",
            return_value=[self._project(finished)],
        )
        with (
            upcoming,
            patch("projects.views.get_tasks_completed_in_range", return_value=finished),
            patch("projects.views.get_tasks_created_in_range", return_value=[]),
            patch("projects.views.generate_closeout_summary", return_value="Text."),
        ):
            start = self.client.get(reverse("close_week_start"))
            posted = self._posted_ids(start)
            self.client.post(reverse("close_week_confirm"), data={"task_id": posted})
        self.assertEqual(posted, [])
        self.assertEqual(WeekCloseout.objects.get().completed_count, 3)

    @patch("django.utils.timezone.localdate")
    def test_tasks_the_project_keyed_read_cannot_see_are_counted(self, mock_localdate):
        """A task on a project already set to "abgeschlossen", and a
        project-less one (#53): neither reaches get_upcoming_projects, so
        neither appears in the fixture the triage list is built from. Both
        are in the week read, and both count. Measured on the live database
        for KW 36/2026: 19 completions, 10 of them reachable the old way."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        invisible = [
            self._task("t-closed-project", "Abgeschlossen", CLOSEOUT_TODAY, done=True),
            self._task("t-no-project", "Ohne Projekt", CLOSEOUT_TODAY, done=True),
        ]
        with (
            # An empty upcoming read is exactly what the two filters produce.
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch(
                "projects.views.get_tasks_completed_in_range", return_value=invisible
            ),
            patch("projects.views.get_tasks_created_in_range", return_value=invisible),
            patch("projects.views.generate_closeout_summary", return_value="Text."),
        ):
            start = self.client.get(reverse("close_week_start"))
            self.client.post(
                reverse("close_week_confirm"),
                data={"task_id": self._posted_ids(start)},
            )
        closeout = WeekCloseout.objects.get()
        self.assertEqual(closeout.completed_count, 2)
        self.assertEqual(closeout.added_count, 2)

    @patch("django.utils.timezone.localdate")
    def test_the_review_shows_two_week_scoped_tiles_and_a_sentence(
        self, mock_localdate
    ):
        """#215: three tiles under one heading read as three answers to the
        same question, and one of them answered a different one. Asserted on
        the tile labels, not on the bare numbers — those appear elsewhere on
        the page too."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        moved = self._task("t-move", "Verschieben", CLOSEOUT_TODAY + timedelta(days=2))
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[self._project([moved])],
            ),
            patch("projects.views.get_tasks_completed_in_range", return_value=[]),
            patch("projects.views.get_tasks_created_in_range", return_value=[]),
            patch("projects.views.generate_closeout_summary", return_value="Text."),
        ):
            start = self.client.get(reverse("close_week_start"))
            posted = self._posted_ids(start)
            # The task moved on to next week between render and submit —
            # what the "→ nächste Woche" button does.
            moved["due"] = CLOSEOUT_TODAY + timedelta(days=9)
            self.client.post(reverse("close_week_confirm"), data={"task_id": posted})
            response = self.client.get(reverse("week_review"))
        self.assertEqual(posted, ["t-move"])
        self.assertEqual(WeekCloseout.objects.get().rescheduled_count, 1)
        self.assertContains(response, "Erledigt</div>")
        self.assertContains(response, "Neu dazugekommen</div>")
        self.assertNotContains(response, "Verschoben</div>")
        self.assertContains(
            response, "Beim Abschließen hast du 1 Aufgabe in die nächste Woche"
        )


@override_settings(DEMO_MODE=False)
class WeekReviewProductionTest(TestCase):
    def test_no_closeout_redirects_to_start(self):
        response = self.client.get(reverse("week_review"))
        # fetch_redirect_response=False: close_week_start's own Notion call
        # has its own tests (CloseWeekStartProductionTest); nothing here
        # mocks it, so a live follow-up GET would hit the network.
        self.assertRedirects(
            response, reverse("close_week_start"), fetch_redirect_response=False
        )

    def test_renders_the_latest_closeout(self):
        WeekCloseout.objects.create(
            iso_year=2026,
            iso_week=25,
            completed_count=3,
            rescheduled_count=1,
            added_count=2,
            summary_text="Gute Woche.",
        )
        # The sidebar project list (#185) makes this view fetch Notion on a
        # cold cache — stubbed here so the assertions below stay about the
        # closeout, not about project data this test never sets up.
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            response = self.client.get(reverse("week_review"))
        self.assertContains(response, "Gute Woche.")
        self.assertContains(response, "KW 25/2026")


class WeekReviewDemoModeTest(DemoModeTestCase):
    def test_no_session_plan_redirects_to_index(self):
        response = self.client.get(reverse("week_review"))
        self.assertRedirects(response, reverse("index"))

    def test_no_closeout_yet_redirects_to_start(self):
        self.given_session_plan()
        response = self.client.get(reverse("week_review"))
        self.assertRedirects(response, reverse("close_week_start"))

    def test_renders_the_latest_closeout_from_the_session(self):
        self.given_session_plan()
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 2,
            "rescheduled_count": 1,
            "added_count": 0,
            "summary_text": "Solide Woche.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("week_review"))
        self.assertContains(response, "Solide Woche.")
