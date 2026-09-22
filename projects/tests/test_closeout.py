"""Wochenabschluss: the close-out ritual, its backends and its summary."""

import json
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
    get_closeout,
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

    @override_settings(DEMO_MODE=False)
    def test_production_get_closeout_answers_the_named_week(self):
        """#263: the named week, not the latest one. Once a past week can be
        closed, closing KW 25 while KW 26 is already closed would otherwise
        show the visitor KW 26's numbers as the result of their own action."""
        request = self.request()
        save_closeout(request, 2026, 25, _closeout_stats(), "Fünfundzwanzig.")
        save_closeout(request, 2026, 26, _closeout_stats(), "Sechsundzwanzig.")
        self.assertEqual(
            get_closeout(request, 2026, 25)["summary_text"], "Fünfundzwanzig."
        )
        self.assertEqual(
            get_latest_closeout(request)["summary_text"], "Sechsundzwanzig."
        )

    @override_settings(DEMO_MODE=True)
    def test_demo_get_closeout_answers_the_named_week(self):
        """The session holds exactly one close-out, which is why this defect
        never showed in demo mode — asserted so the two backends keep
        answering the same question."""
        request = self.request()
        save_closeout(request, 2026, 25, _closeout_stats(), "Text.")
        self.assertEqual(get_closeout(request, 2026, 25)["summary_text"], "Text.")
        self.assertIsNone(get_closeout(request, 2026, 26))

    @override_settings(DEMO_MODE=False)
    def test_production_get_closeout_misses_a_week_never_closed(self):
        self.assertIsNone(get_closeout(self.request(), 2026, 25))

    @override_settings(DEMO_MODE=True)
    def test_demo_get_closeout_misses_a_week_never_closed(self):
        self.assertIsNone(get_closeout(self.request(), 2026, 25))


class CloseWeekStartDemoModeTest(DemoModeTestCase):
    """#169: the triage list is only open tasks due in the ISO week being
    closed — tasks already done or due a different week stay out.

    #263: "a different week" is the whole rule. It used to be "a different
    week, or a day that has already passed", which cut half the week's own
    open tasks out of the one page whose job is deciding what happens to
    them. A task from a *past* week still stays out — that one does already
    have its own overdue signal."""

    @patch("django.utils.timezone.localdate")
    def test_lists_only_open_tasks_due_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche")
        self.assertNotContains(response, "Nächste Woche")
        # CLOSEOUT_TODAY is a Monday, so "Überfällig" (-1 day) sits in the
        # previous ISO week — still out, and for the reason that survives
        # #263.
        self.assertNotContains(response, "Überfällig")
        self.assertNotContains(response, "Schon erledigt")

    @patch("django.utils.timezone.localdate")
    def test_a_task_due_earlier_the_same_week_is_still_triageable(self, mock_localdate):
        """#263: due Tuesday, still open on Thursday. The old `>= today`
        bound left it out of the list, so it could not be moved from the one
        surface that moves tasks, and it rolled into the next week
        untouched."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=3)
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-passed",
                    "name": "Dienstag faellig",
                    "date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Dienstag faellig")
        # Still due + 7 rather than a date measured from today — the move
        # button means the same thing for every row on the list.
        self.assertContains(
            response, f"→ {format_date(CLOSEOUT_TODAY + timedelta(days=8))}"
        )

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


class CloseWeekBrowsedWeekTest(DemoModeTestCase):
    """#263: the triage page names the week it is showing and ?week= points
    it at a past one — the Monday-morning review is about the week that just
    ended, and before this there was no way to reach it. Same wire format as
    the dashboard's own ?week= (#180), same fallback on anything unusable."""

    def _plan(self):
        """One task in the current ISO week (W25) and one in the week before
        it (W24) — CLOSEOUT_TODAY is the Monday of W25."""
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-this-week",
                    "name": "Diese Woche",
                    "date": (CLOSEOUT_TODAY + timedelta(days=2)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-last-week",
                    "name": "Vorwoche",
                    "date": (CLOSEOUT_TODAY - timedelta(days=3)).isoformat(),
                    "done": False,
                },
            ]
        )

    @patch("django.utils.timezone.localdate")
    def test_a_past_week_lists_the_tasks_due_in_it(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        response = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertContains(response, 'class="triage-task-name">Vorwoche<')
        self.assertNotContains(response, 'class="triage-task-name">Diese Woche<')

    @patch("django.utils.timezone.localdate")
    def test_the_page_names_the_week_it_is_triaging(self, mock_localdate):
        """The subtitle used to carry today's date, which said nothing about
        which week the list below it belongs to once the two can differ."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        response = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertContains(response, "KW 24, 8.–14. Juni")
        current = self.client.get(reverse("close_week_start"))
        self.assertContains(current, "KW 25, 15.–21. Juni")

    @patch("django.utils.timezone.localdate")
    def test_the_form_carries_the_browsed_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        response = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertContains(
            response, '<input type="hidden" name="week" value="2026-W24">', html=False
        )

    @patch("django.utils.timezone.localdate")
    def test_an_unusable_week_falls_back_to_the_current_one(self, mock_localdate):
        """Malformed, or a week number ISO does not have — the same tolerance
        _week_monday applies everywhere, because the value is one a visitor
        is free to hand-edit."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        for raw in ("nonsense", "2026-W99", "9999-W52"):
            with self.subTest(week=raw):
                response = self.client.get(f"{reverse('close_week_start')}?week={raw}")
                self.assertContains(response, "KW 25, 15.–21. Juni")
                self.assertContains(response, 'value="2026-W25"')

    @patch("django.utils.timezone.localdate")
    def test_a_future_week_is_clamped_to_the_current_one(self, mock_localdate):
        """The nav offers no way forward on purpose — the ritual has no
        meaning for a week that has not happened. ?week= is hand-editable all
        the same, so the bound lives in the parser rather than in the
        template, and it snaps back the way every other unusable value does
        instead of erroring the page."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        response = self.client.get(f"{reverse('close_week_start')}?week=2026-W26")
        self.assertContains(response, "KW 25, 15.–21. Juni")
        self.assertContains(response, 'value="2026-W25"')
        self.assertNotContains(response, ">Diese Woche</a>")

    @patch("django.utils.timezone.localdate")
    def test_already_closed_asks_about_the_browsed_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=[])
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 24,
            "completed_count": 3,
            "rescheduled_count": 1,
            "added_count": 0,
            "summary_text": "Text.",
            "closed_at": "2026-06-14T12:00:00",
        }
        session.save()
        browsed = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertContains(browsed, "Diese Woche hast du schon abgeschlossen.")
        current = self.client.get(reverse("close_week_start"))
        self.assertNotContains(current, "Diese Woche hast du schon abgeschlossen.")

    @patch("django.utils.timezone.localdate")
    def test_the_weekend_empty_state_only_greets_the_current_week(self, mock_localdate):
        """The weekend greeting is about the week that is ending now. On a
        Saturday spent looking back at a week that is already over it is the
        wrong sentence, so the flag asks the browsed week too."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=5)  # Saturday
        self.given_session_plan(tasks=[])
        browsed = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertNotContains(browsed, "Genieße dein Wochenende")
        self.assertContains(browsed, "Für diese Woche ist alles erledigt")
        current = self.client.get(reverse("close_week_start"))
        self.assertContains(current, "Genieße dein Wochenende")

    @patch("django.utils.timezone.localdate")
    def test_browsing_offers_the_previous_week_and_the_way_back(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self._plan()
        current = self.client.get(reverse("close_week_start"))
        self.assertContains(current, 'href="?week=2026-W24"')
        self.assertNotContains(current, ">Diese Woche</a>")
        browsed = self.client.get(f"{reverse('close_week_start')}?week=2026-W24")
        self.assertContains(browsed, 'href="?week=2026-W23"')
        self.assertContains(browsed, ">Diese Woche</a>")

    @patch("django.utils.timezone.localdate")
    def test_the_move_button_can_offer_a_date_that_has_passed(self, mock_localdate):
        """The accepted consequence of one list with one move semantics
        (#263): browsing two weeks back, "due + 7" lands in a week that is
        itself over. It is a real date the visitor reads before clicking and
        can correct afterwards — the rejected alternative was a second group
        with its own meaning of "→ nächste Woche"."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        due = CLOSEOUT_TODAY - timedelta(days=10)  # Friday of KW 23
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-old",
                    "name": "Lange offen",
                    "date": due.isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.get(f"{reverse('close_week_start')}?week=2026-W23")
        self.assertContains(response, "Lange offen")
        self.assertContains(response, f"→ {format_date(due + timedelta(days=7))}")


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
        # #263: the review is asked for the week that was just closed, not
        # for whichever one happens to be the latest.
        self.assertRedirects(response, f"{reverse('week_review')}?week=2026-W25")
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

    def test_a_hand_toggle_does_not_take_a_task_out_of_the_simulated_week(self):
        """#246: toggle_session_task records the real date, because
        /mein-plan/ renders the real date and a write there lands where it
        shows. That date must not displace the moment's own placement: the
        task is struck through on the dashboard because it is due by `sim`,
        and it belongs to the simulated week either way. While the due date
        only answered where no completion date stood, checking a task off by
        hand *removed* it from this count."""
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
            ]
        )
        self.given_timelapse_moments(sim.isoformat())
        session = self.client.session
        session["demo_sim_date"] = sim.isoformat()
        session.save()
        # Through the route the visitor actually has, not by seeding the
        # field: the real date it writes is the whole point (today is months
        # away from the simulated June week).
        self.client.post(
            reverse("toggle_session_task", args=["t-a"]),
            data=json.dumps({"done": True}),
            content_type="application/json",
        )
        self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(
            self.client.session["demo_week_closeout"]["completed_count"], 1
        )

    def test_a_task_completed_both_ways_counts_once(self):
        """The other half of asking both placements: one task due by `sim`
        *and* carrying a completion date inside the same week is one task, not
        two. `any` over the placements rather than a sum."""
        monday = date(2026, 6, 15)
        sim = monday + timedelta(days=3)
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-a",
                    "name": "Frueh faellig",
                    "date": (monday + timedelta(days=1)).isoformat(),
                    "done": True,
                    "completed_date": (monday + timedelta(days=2)).isoformat(),
                },
            ]
        )
        self.given_timelapse_moments(sim.isoformat())
        session = self.client.session
        session["demo_sim_date"] = sim.isoformat()
        session.save()
        self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(
            self.client.session["demo_week_closeout"]["completed_count"], 1
        )

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
        self.assertRedirects(response, f"{reverse('week_review')}?week=2026-W25")
        self.assertEqual(self.client.session["demo_week_closeout"]["summary_text"], "")


class CloseWeekConfirmCarriesItsWeekDemoModeTest(DemoModeTestCase):
    """#263: the close-out closes the week its form was showing, not the week
    the POST happens to land in. The form is filled on Friday evening and
    submitted on Monday often enough that this is the intended use of the
    page, and the week turning underneath it used to rewrite every number."""

    def _plan(self):
        """Two tasks in KW 25: one still due there, one moved into KW 26 —
        the second is the only reschedule this close-out made."""
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-stayed",
                    "name": "Geblieben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=2)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-moved",
                    "name": "Verschoben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=9)).isoformat(),
                    "done": False,
                },
            ]
        )

    @patch("django.utils.timezone.localdate")
    def test_the_posted_week_is_the_one_that_gets_closed(self, mock_localdate):
        # The request falls in KW 26; the page was showing KW 25.
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=7)
        self._plan()
        response = self.client.post(
            reverse("close_week_confirm"),
            data={"week": "2026-W25", "task_id": ["t-stayed", "t-moved"]},
        )
        closeout = self.client.session["demo_week_closeout"]
        self.assertEqual((closeout["iso_year"], closeout["iso_week"]), (2026, 25))
        self.assertRedirects(response, f"{reverse('week_review')}?week=2026-W25")

    @patch("django.utils.timezone.localdate")
    def test_rescheduled_is_measured_against_the_posted_week(self, mock_localdate):
        """The count asks "is this task still due the week I am closing?".
        Asked against the request's own week instead, every task on the list
        answered no the moment the week turned, and the review claimed a
        move for each of them."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=7)
        self._plan()
        self.client.post(
            reverse("close_week_confirm"),
            data={"week": "2026-W25", "task_id": ["t-stayed", "t-moved"]},
        )
        self.assertEqual(
            self.client.session["demo_week_closeout"]["rescheduled_count"], 1
        )

    @patch("django.utils.timezone.localdate")
    def test_the_counts_follow_the_posted_week(self, mock_localdate):
        """Completed is read from the week being closed. A task finished in
        KW 25 must still count when the form is submitted in KW 26."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=7)
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-done",
                    "name": "Erledigt",
                    "date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                    "done": True,
                    "completed_date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                },
            ]
        )
        self.client.post(
            reverse("close_week_confirm"), data={"week": "2026-W25", "task_id": []}
        )
        self.assertEqual(
            self.client.session["demo_week_closeout"]["completed_count"], 1
        )

    @patch("django.utils.timezone.localdate")
    def test_a_missing_or_unusable_week_falls_back_to_the_request(self, mock_localdate):
        """The behaviour this flow had while the week was never carried at
        all — a hand-edited hidden field is not worth an error page."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        for data in ({}, {"week": ""}, {"week": "nonsense"}, {"week": "2026-W99"}):
            with self.subTest(data=data):
                self.given_session_plan(tasks=[])
                self.client.post(
                    reverse("close_week_confirm"), data={**data, "task_id": []}
                )
                closeout = self.client.session["demo_week_closeout"]
                self.assertEqual(
                    (closeout["iso_year"], closeout["iso_week"]), (2026, 25)
                )


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
            response,
            f"{reverse('week_review')}?week=2026-W25",
            fetch_redirect_response=False,
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
    def test_the_week_reads_follow_the_posted_week(self, mock_localdate):
        """#263: KW 25's Monday and Sunday, from a request that falls in KW
        26. Both counts describe the week being closed, so both reads have to
        be pointed at it rather than at the calendar."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=7)
        completed_read, created_read = self._week_reads()
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read as completed_mock,
            created_read as created_mock,
        ):
            self.client.post(
                reverse("close_week_confirm"),
                data={"week": "2026-W25", "task_id": []},
            )
        monday, sunday = iso_week_bounds(CLOSEOUT_TODAY)
        completed_mock.assert_called_once_with(monday, sunday)
        created_mock.assert_called_once_with(monday, sunday)
        closeout = WeekCloseout.objects.get()
        self.assertEqual((closeout.iso_year, closeout.iso_week), (2026, 25))

    @patch("django.utils.timezone.localdate")
    def test_closing_a_past_week_lands_on_that_weeks_review(self, mock_localdate):
        """#263: week_review renders the *latest* close-out when no week is
        named, so closing KW 25 while KW 26 is already closed used to answer
        with KW 26's numbers — someone else's week as the result of this
        visitor's own action."""
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=7)
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=26, summary_text="Die spätere Woche."
        )
        completed_read, created_read = self._week_reads()
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read,
            created_read,
            patch(
                "projects.views.generate_closeout_summary", return_value="KW 25 also."
            ),
        ):
            response = self.client.post(
                reverse("close_week_confirm"),
                data={"week": "2026-W25", "task_id": []},
            )
            review = self.client.get(response["Location"])
        self.assertContains(review, "KW 25/2026")
        self.assertContains(review, "KW 25 also.")
        self.assertNotContains(review, "Die spätere Woche.")

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

    def _failing_close_out(
        self, failing_read="get_tasks_completed_in_range", data=None
    ):
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
                reverse("close_week_confirm"), data={"task_id": [], **(data or {})}
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
    def test_the_failure_lands_back_on_the_week_it_was_closing(self, mock_localdate):
        """#263: the failure redirect is the only path back to the triage
        page, and it used to drop the week. A KW 24 review whose Notion read
        died landed on the KW 25 list under a notice saying "try again" — so
        the retry closed the week the visitor never triaged."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out(data={"week": "2026-W24"})
        self.assertIn("week=2026-W24", landing)
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            landed = self.client.get(landing)
        self.assertContains(landed, "Notion war gerade nicht erreichbar")
        # The notice and the week travel together: the page that carries the
        # retry button is the one the retry should act on.
        self.assertContains(landed, "KW 24, 8.–14. Juni")
        self.assertContains(
            landed, '<input type="hidden" name="week" value="2026-W24">'
        )

    @patch("django.utils.timezone.localdate")
    def test_a_failure_on_the_current_week_names_it_too(self, mock_localdate):
        """No special case for "the week is today's" — one rule, so the
        landing page is addressed the same way whichever week failed."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        landing = self._failing_close_out()
        self.assertIn("week=2026-W25", landing)

    @patch("django.utils.timezone.localdate")
    def test_a_future_week_cannot_be_closed(self, mock_localdate):
        """#263 opened the week to a parameter in order to reach *back*. A
        close-out stored under a week that has not happened outranks every
        real one in get_latest_closeout's ordering, so /wochenrueckblick/
        without a parameter would answer with it until that week arrives —
        the value is clamped to the current week rather than trusted."""
        mock_localdate.return_value = CLOSEOUT_TODAY
        completed_read, created_read = self._week_reads()
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            completed_read,
            created_read,
        ):
            response = self.client.post(
                reverse("close_week_confirm"),
                data={"week": "2099-W01", "task_id": []},
            )
        closeout = WeekCloseout.objects.get()
        self.assertEqual((closeout.iso_year, closeout.iso_week), (2026, 25))
        self.assertRedirects(
            response,
            f"{reverse('week_review')}?week=2026-W25",
            fetch_redirect_response=False,
        )

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

    def test_a_named_week_is_shown_instead_of_the_latest(self):
        """#263: ?week= addresses one close-out. Nothing about the no-param
        route changes — that is still the latest one, which is every way into
        this page that existed before a past week could be closed."""
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=24, summary_text="Die frühere Woche."
        )
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=25, summary_text="Die spätere Woche."
        )
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            named = self.client.get(f"{reverse('week_review')}?week=2026-W24")
            latest = self.client.get(reverse("week_review"))
        self.assertContains(named, "Die frühere Woche.")
        self.assertContains(named, "KW 24/2026")
        self.assertContains(latest, "Die spätere Woche.")

    def test_an_unknown_or_unusable_week_falls_back_to_the_latest(self):
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=25, summary_text="Die einzige Woche."
        )
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            for raw in ("2026-W24", "nonsense", "2026-W99"):
                with self.subTest(week=raw):
                    response = self.client.get(f"{reverse('week_review')}?week={raw}")
                    self.assertContains(response, "Die einzige Woche.")


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
