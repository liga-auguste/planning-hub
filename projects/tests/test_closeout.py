"""Wochenabschluss: the close-out ritual, its backends and its summary."""

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
from ..models import WeekCloseout
from ..notion import NotionUnavailableError
from .base import (
    CLOSEOUT_TODAY,
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
        self.assertIn("Erledigt: 3 Aufgaben", prompt)
        self.assertIn("Verschoben in die nächste Woche: 1 Aufgaben", prompt)
        self.assertIn("Neu dazugekommen: 2 Aufgaben", prompt)
        self.assertIn("summary_text", prompt)


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
    def test_already_closed_and_nothing_open_hides_the_button(self, mock_localdate):
        # Re-confirming an already-closed week with an empty triage list
        # would post an empty task_id list and overwrite the real
        # completed/rescheduled counts with zeros — the button has to go,
        # not just the copy above it.
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
        self.assertContains(response, "Diese Woche hast du bereits abgeschlossen.")
        self.assertContains(response, "Rückblick ansehen")
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
    def test_already_closed_and_nothing_open_hides_the_button(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=25, completed_count=3, rescheduled_count=1
        )
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche hast du bereits abgeschlossen.")
        self.assertNotContains(response, "Woche abschließen</button>")
        self.assertContains(response, "bereits abgeschlossen</div>")
        self.assertNotContains(response, "noch offene Aufgaben dieser Woche")


class CloseWeekConfirmDemoModeTest(DemoModeTestCase):
    """#169: stats are computed by diffing the posted task_id list against
    live state — completed if now done, rescheduled if no longer due this
    same ISO week."""

    @patch("django.utils.timezone.localdate")
    def test_completed_and_rescheduled_and_unchanged(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-done",
                    "name": "Erledigt",
                    "date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                    "done": True,
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
            data={"task_id": ["t-done", "t-moved", "t-stayed"]},
        )
        self.assertRedirects(response, reverse("week_review"))
        closeout = self.client.session["demo_week_closeout"]
        self.assertEqual(closeout["completed_count"], 1)
        self.assertEqual(closeout["rescheduled_count"], 1)
        self.assertEqual(closeout["added_count"], 0)
        self.assertEqual(closeout["summary_text"], "Gute Woche gewesen.")

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
class CloseWeekConfirmProductionTest(TestCase):
    def _task(self, task_id, name, due, done=False, created_time=None):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": created_time,
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

    @patch("django.utils.timezone.localdate")
    def test_added_count_comes_from_created_time_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        tasks = [
            self._task(
                "t-done",
                "Erledigt",
                CLOSEOUT_TODAY,
                done=True,
                created_time=date(2026, 5, 1),
            ),
            self._task(
                "t-new",
                "Neu",
                CLOSEOUT_TODAY + timedelta(days=1),
                created_time=CLOSEOUT_TODAY,
            ),
        ]
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=self._project(tasks),
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Rückschau."
            ),
        ):
            response = self.client.post(
                reverse("close_week_confirm"), data={"task_id": ["t-done"]}
            )
        # fetch_redirect_response=False: week_review() builds the sidebar
        # project list (#185) and would fetch Notion on the follow-up GET,
        # outside the patch above. What it renders has its own test.
        self.assertRedirects(
            response, reverse("week_review"), fetch_redirect_response=False
        )
        closeout = WeekCloseout.objects.get()
        self.assertEqual(closeout.completed_count, 1)
        self.assertEqual(closeout.added_count, 1)
        self.assertEqual(closeout.summary_text, "Rückschau.")

    @patch("django.utils.timezone.localdate")
    def test_reclosing_the_same_week_updates_not_duplicates(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Erster Text."
            ),
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Zweiter Text."
            ),
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(WeekCloseout.objects.count(), 1)
        self.assertEqual(WeekCloseout.objects.get().summary_text, "Zweiter Text.")


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
