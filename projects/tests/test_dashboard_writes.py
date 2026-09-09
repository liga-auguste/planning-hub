"""The dashboard write paths: toggle, reschedule and rename, what they
persist, what they answer and what they leave in the cache."""

import json
import re
from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import (
    Client,
    TestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone

from ..ai import AIUnavailableError
from ..date_format import format_date
from ..dates import iso_week_bounds
from ..notion import NotionUnavailableError
from ..views import (
    _URGENCY_RANK,
    CACHE_DEADLINE_KEY,
    CACHE_KEY,
    CACHE_TTL,
    STALE_CACHE_KEY,
    STALE_UNASSIGNED_CACHE_KEY,
    SUMMARY_KEY,
    UNASSIGNED_CACHE_DEADLINE_KEY,
    UNASSIGNED_CACHE_KEY,
    _annotate_tasks,
    _bust_dashboard_cache,
    _cache_fresh_read,
    _derive_dashboard_figures,
    _remap_summary_refs,
)
from .base import (
    DemoModeTestCase,
    _fake_upcoming_project,
    _fake_upcoming_project_with_task,
    _summary_data,
)


@override_settings(DEMO_MODE=False)
class ToggleTaskNotionFailureTest(TestCase):
    """toggle_task_view always returned {"ok": True} regardless of what
    toggle_task() actually did — a Notion failure here used to either 500 or
    (before #29) go unnoticed entirely, leaving the checkbox and Notion
    silently disagreeing. A non-200 is what lets the frontend refuse to
    apply the change it was hoping for."""

    def test_notion_failure_is_a_502_not_a_500(self):
        with patch(
            "projects.views.toggle_task", side_effect=NotionUnavailableError("boom")
        ):
            response = self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": true}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "notion unavailable"})

    def test_success_still_reports_ok(self):
        with patch("projects.views.toggle_task") as mock_toggle:
            response = self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": true}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        mock_toggle.assert_called_once_with("task-1", True, date.today().isoformat())

    def test_unmarking_a_task_clears_the_completed_date(self):
        with patch("projects.views.toggle_task") as mock_toggle:
            self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": false}',
                content_type="application/json",
            )
        mock_toggle.assert_called_once_with("task-1", False, None)

    def test_success_busts_the_dashboard_cache(self):
        """#52: with the cache shared across workers, a write that doesn't
        invalidate it can hide a completed task as open for up to CACHE_TTL."""
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with patch("projects.views.toggle_task"):
            self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": true}',
                content_type="application/json",
            )
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))

    def test_notion_failure_leaves_the_cache_untouched(self):
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with patch(
            "projects.views.toggle_task", side_effect=NotionUnavailableError("boom")
        ):
            self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": true}',
                content_type="application/json",
            )
        self.assertIsNotNone(cache.get(CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_CACHE_KEY))


@override_settings(DEMO_MODE=False)
class RescheduleTaskNotionFailureTest(TestCase):
    def test_notion_failure_is_a_502_not_a_500(self):
        with patch(
            "projects.views.update_task_date",
            side_effect=NotionUnavailableError("boom"),
        ):
            response = self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "notion unavailable"})

    def test_success_still_reports_ok(self):
        with (
            patch("projects.views.update_task_date") as mock_update,
            patch("projects.views.increment_postpone_count", return_value=1),
        ):
            response = self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        mock_update.assert_called_once_with("task-1", "2026-09-05")

    def test_success_busts_the_dashboard_cache(self):
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with (
            patch("projects.views.update_task_date"),
            patch("projects.views.increment_postpone_count", return_value=1),
        ):
            self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))

    def test_notion_failure_leaves_the_cache_untouched(self):
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with patch(
            "projects.views.update_task_date",
            side_effect=NotionUnavailableError("boom"),
        ):
            self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertIsNotNone(cache.get(CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_CACHE_KEY))


@override_settings(DEMO_MODE=False)
class RescheduleIncrementsCounterProductionTest(TestCase):
    """#171: reschedule_task_view increments the postpone counter after a
    successful date update and reports the new value."""

    # Relative rather than fixed: the answer now carries the stage the new
    # date implies, and a hard-coded date would slide from "ok" through
    # "urgent" into "overdue" as the real clock passed it. Two months out is
    # never in this ISO week.
    NEW_DATE = date.today() + timedelta(days=60)

    def test_increments_and_returns_the_new_count(self):
        with (
            patch("projects.views.update_task_date"),
            patch(
                "projects.views.increment_postpone_count", return_value=4
            ) as mock_increment,
        ):
            response = self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data=f'{{"date": "{self.NEW_DATE.isoformat()}"}}',
                content_type="application/json",
            )
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "postpone_count": 4,
                "due_display": format_date(self.NEW_DATE),
                "due_display_row": format_date(self.NEW_DATE, role="row"),
                "urgency": "ok",
            },
        )
        mock_increment.assert_called_once_with("task-1")

    def test_a_failing_increment_after_a_successful_date_update_is_still_a_502(self):
        # #171 accepted gap: the date has already moved but the counter
        # hasn't — self-healing on the next reschedule, not special-cased.
        with (
            patch("projects.views.update_task_date"),
            patch(
                "projects.views.increment_postpone_count",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            response = self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)

    def test_a_failing_increment_still_busts_the_cache(self):
        # The date update itself already succeeded by this point, so the
        # cache must not keep serving the pre-move date for the rest of its
        # TTL just because the counter call afterwards failed (wf-review on
        # PR #175 — _bust_dashboard_cache's own contract is "every confirmed
        # Notion write").
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with (
            patch("projects.views.update_task_date"),
            patch(
                "projects.views.increment_postpone_count",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "2026-09-05"}',
                content_type="application/json",
            )
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))


class FetchRejectionHandlingTest(DemoModeTestCase):
    """#159: a *rejected* fetch (transport failure — as opposed to an error
    response, which the !response.ok branches handle) threw out of three
    handlers: my_plan's toggleTask kept an unsaved optimistic state, the
    dashboard toggle failed with no feedback, and reschedule() left the
    date input wedged in the row. Each fetch now routes the rejection into
    the same path its error-response branch already takes."""

    GUARD = "if (!response || !response.ok)"

    def test_my_plan_toggle_catches_and_reverts(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, self.GUARD)
        # The revert path survives behind the widened guard.
        self.assertContains(response, "applyDone(taskId, currentDone);")
        self.assertContains(response, "flashActionFailed(btn);")

    def test_dashboard_toggle_and_reschedule_catch(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        # All five handlers — the toggle listener, reschedule(), #180's
        # day-column drag handler and #239's rename and trash — carry the
        # widened guard; their error paths (flash / return false / revert
        # the drag) stay.
        self.assertContains(response, self.GUARD, count=5)
        self.assertContains(response, "flashActionFailed(dueSpan);")
        self.assertContains(response, "flashActionFailed(nameSpan);")


class ToggleTaskDemoModeTest(DemoModeTestCase):
    """#61: toggle_task_view's demo branch fell out of its lookup loop
    silently on a miss and always answered {"ok": True} — indistinguishable
    from a real toggle. It now uses the same next(...) lookup + 404 on a
    miss that reschedule_task_view already established (#10 §5)."""

    def post_toggle(self, task_id, done=True):
        return self.client.post(
            reverse("toggle_task", args=[task_id]),
            data=json.dumps({"done": done}),
            content_type="application/json",
        )

    def test_a_toggle_survives_a_reload(self):
        self.given_session_plan()
        response = self.post_toggle("demo-session-0", done=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.session["demo_plan"]["tasks"][0]["done"])

    def test_marking_done_stamps_a_completed_date(self):
        """#19: mirrors toggle_task's own Done/Erledigt am pairing in Notion."""
        self.given_session_plan()
        self.post_toggle("demo-session-0", done=True)
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["completed_date"],
            timezone.localdate().isoformat(),
        )

    def test_unmarking_clears_the_completed_date(self):
        self.given_session_plan()
        self.post_toggle("demo-session-0", done=True)
        self.post_toggle("demo-session-0", done=False)
        self.assertIsNone(
            self.client.session["demo_plan"]["tasks"][0]["completed_date"]
        )

    def test_an_unknown_task_is_a_404(self):
        self.given_session_plan()
        response = self.post_toggle("demo-1-7", done=True)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.client.session["demo_plan"]["tasks"][0]["done"])

    def test_no_session_plan_at_all_is_a_404(self):
        response = self.post_toggle("demo-session-0", done=True)
        self.assertEqual(response.status_code, 404)


class ToggleSessionTaskDemoModeTest(DemoModeTestCase):
    """#61: toggle_session_task (my_plan.html's own toggle endpoint) had the
    same silent-miss shape as toggle_task_view's demo branch."""

    def post_toggle(self, task_id, done=True):
        return self.client.post(
            reverse("toggle_session_task", args=[task_id]),
            data=json.dumps({"done": done}),
            content_type="application/json",
        )

    def test_a_toggle_survives_a_reload(self):
        self.given_session_plan()
        response = self.post_toggle("demo-session-0", done=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.session["demo_plan"]["tasks"][0]["done"])

    def test_an_unknown_task_is_a_404(self):
        self.given_session_plan()
        response = self.post_toggle("demo-1-7", done=True)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.client.session["demo_plan"]["tasks"][0]["done"])

    def test_no_session_plan_at_all_is_a_404(self):
        response = self.post_toggle("demo-session-0", done=True)
        self.assertEqual(response.status_code, 404)


class MalformedJsonBodyTest(DemoModeTestCase):
    """#154: invalid client input is a 400, never a 500. Four endpoints
    parsed their JSON body unguarded — they now answer the way
    reschedule_task_view and _parse_posted_date always did."""

    def post_raw(self, url, body):
        return self.client.post(url, data=body, content_type="application/json")

    def all_four(self):
        return [
            ("toggle_task", reverse("toggle_task", args=["demo-session-0"])),
            (
                "toggle_session_task",
                reverse("toggle_session_task", args=["demo-session-0"]),
            ),
            ("rule_update", reverse("rule_update", args=[1])),
            ("rule_reorder", reverse("rule_reorder")),
        ]

    def toggles_only(self):
        return self.all_four()[:2]

    def assert_json_400(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_malformed_json_is_a_400(self):
        for name, url in self.all_four():
            with self.subTest(endpoint=name):
                self.assert_json_400(self.post_raw(url, b"{"))

    def test_invalid_utf8_bytes_are_a_400(self):
        # json.loads raises UnicodeDecodeError — not JSONDecodeError — for
        # bytes that are not valid UTF-8, so it needs its own catch.
        for name, url in self.all_four():
            with self.subTest(endpoint=name):
                self.assert_json_400(self.post_raw(url, b"\x80"))

    def test_a_non_dict_body_is_a_400(self):
        for name, url in self.all_four():
            with self.subTest(endpoint=name):
                self.assert_json_400(self.post_raw(url, json.dumps([1, 2])))

    def test_a_missing_done_key_is_a_400(self):
        for name, url in self.toggles_only():
            with self.subTest(endpoint=name):
                self.assert_json_400(self.post_raw(url, json.dumps({})))

    def test_a_non_bool_done_is_a_400(self):
        for name, url in self.toggles_only():
            with self.subTest(endpoint=name):
                self.assert_json_400(self.post_raw(url, json.dumps({"done": "yes"})))


class RescheduleTaskDemoModeTest(DemoModeTestCase):
    """§5 of #10: reschedule_task_view answered {"ok": True} in demo mode
    without writing anything, so the date the JS had already moved optimistically
    was gone after a reload. It now writes to session['demo_plan'] the way
    toggle_task_view does — and, unlike toggle_task_view, refuses to claim
    success for a task it did not find."""

    NEW_DATE = (date.today() + timedelta(days=21)).isoformat()

    def post_date(self, task_id, body):
        return self.client.post(
            reverse("reschedule_task", args=[task_id]),
            data=body,
            content_type="application/json",
        )

    def stored_dates(self):
        return [t["date"] for t in self.client.session["demo_plan"]["tasks"]]

    def test_a_new_date_survives_a_reload(self):
        self.given_session_plan()
        response = self.post_date("demo-session-0", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_dates(), [self.NEW_DATE])
        reloaded = self.client.get(reverse("my_plan"))
        self.assertContains(reloaded, format_date(date.fromisoformat(self.NEW_DATE)))

    def test_an_invalid_date_is_rejected(self):
        plan = self.given_session_plan()
        response = self.post_date("demo-session-0", '{"date": "kein-datum"}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_dates(), [plan["tasks"][0]["date"]])

    def test_a_missing_date_is_rejected(self):
        plan = self.given_session_plan()
        response = self.post_date("demo-session-0", "{}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_dates(), [plan["tasks"][0]["date"]])

    def test_a_malformed_body_is_rejected(self):
        plan = self.given_session_plan()
        response = self.post_date("demo-session-0", "kein json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_dates(), [plan["tasks"][0]["date"]])

    def test_an_unknown_task_is_a_404(self):
        # A demo-fixture id: it renders on the multi-project dashboard but is
        # not in the session plan, so there is nothing to write it to.
        plan = self.given_session_plan()
        response = self.post_date("demo-1-7", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored_dates(), [plan["tasks"][0]["date"]])

    def test_no_session_plan_at_all_is_a_404(self):
        response = self.post_date("demo-session-0", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 404)

    # --- #140: the task order is chronological, so a new date moves the
    # task; a cached summary's task_refs are positions in that order. The
    # session summaries are renumbered to follow the move rather than swept,
    # because the sweep made the visitor wait out a fresh Claude call for a
    # summary whose text was still true. ---

    def given_two_task_plan(self):
        """A plan whose two tasks a move can reorder — the one-task plan
        given_session_plan builds numbers the same either way."""
        return self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (date.today() + timedelta(days=7)).isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Plakate drucken",
                    "date": (date.today() + timedelta(days=14)).isoformat(),
                    "done": False,
                },
            ]
        )

    def given_cached_summaries(self, task_refs=(1,)):
        """A current-version summary, a preloaded sim-date one, and an
        old-version leftover. The first two are renumbered; the third was
        numbered against an order this code no longer produces, so it goes."""
        session = self.client.session
        summary = {
            "jetzt_faellig": [
                {
                    "heading": "Jetzt fällig",
                    "assessment": "Programm zuerst",
                    "task_refs": list(task_refs),
                }
            ],
            "naechste_woche": [],
        }
        session[f"{SUMMARY_KEY}_today"] = summary
        session[f"{SUMMARY_KEY}_2026-09-01"] = summary
        session["demo_plan_summary_v1_today"] = {"summary": "uralt"}
        session.save()

    def stored_task_refs(self, key):
        return self.client.session[key]["jetzt_faellig"][0]["task_refs"]

    def test_a_reschedule_renumbers_every_current_summary(self):
        # The first task moves past the second, so what the summary points
        # at with position 1 is position 2 afterwards.
        self.given_two_task_plan()
        self.given_cached_summaries()
        response = self.post_date("demo-session-0", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 200)
        for key in (f"{SUMMARY_KEY}_today", f"{SUMMARY_KEY}_2026-09-01"):
            with self.subTest(key=key):
                self.assertEqual(self.stored_task_refs(key), [2])

    def test_a_move_that_reorders_nothing_leaves_the_refs_alone(self):
        self.given_two_task_plan()
        self.given_cached_summaries(task_refs=(1, 2))
        self.post_date(
            "demo-session-0",
            f'{{"date": "{(date.today() + timedelta(days=8)).isoformat()}"}}',
        )
        self.assertEqual(self.stored_task_refs(f"{SUMMARY_KEY}_today"), [1, 2])

    def test_an_older_format_summary_is_still_dropped(self):
        # Its refs were numbered against an order this code no longer
        # produces — the unversioned-prefix sweep planner_create does.
        self.given_two_task_plan()
        self.given_cached_summaries()
        self.post_date("demo-session-0", f'{{"date": "{self.NEW_DATE}"}}')
        # list(...keys()): SessionBase is not a dict and not iterable itself,
        # so SIM118's bare-iteration fix does not apply (cf. planner_views).
        leftovers = [
            k
            for k in list(self.client.session.keys())
            if k.startswith("demo_plan_summary") and not k.startswith(SUMMARY_KEY)
        ]
        self.assertEqual(leftovers, [])

    def test_a_rejected_reschedule_keeps_the_cached_summaries(self):
        # Nothing moved, so nothing may be thrown away — the summary is a
        # Claude call the visitor would otherwise pay for again.
        self.given_session_plan()
        self.given_cached_summaries()
        response = self.post_date("demo-session-0", '{"date": "kein-datum"}')
        self.assertEqual(response.status_code, 400)
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)

    def test_an_unknown_task_keeps_the_cached_summaries(self):
        self.given_session_plan()
        self.given_cached_summaries()
        response = self.post_date("demo-1-7", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 404)
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)


class RescheduleAnswersTheNewStageTest(DemoModeTestCase):
    """A task row wears its urgency class twice — on the dot and on the date
    label — and reschedule() only ever cleared the label's, so moving a task
    due today left an amber "act today" dot beside a neutral date until the
    next page load. The view now answers with the stage the new date implies,
    computed by the same _classify_due_urgency the dashboard renders with
    rather than by a second copy of the rule living in JS."""

    # A fixed Monday, so that "urgent" (same ISO week, later than today) is
    # constructible at all — on a real Sunday no such date exists. Time
    # travel is a demo-session feature, which makes it the natural way to pin
    # the arithmetic without freezing the clock.
    MONDAY = "2026-09-07"

    def given_simulated_today(self, day):
        session = self.client.session
        session["demo_sim_date"] = day
        session.save()

    def moved_to(self, new_date):
        response = self.client.post(
            reverse("reschedule_task", args=["demo-session-0"]),
            data=f'{{"date": "{new_date}"}}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["urgency"]

    def test_every_stage_is_measured_against_the_simulated_today(self):
        self.given_session_plan()
        self.given_simulated_today(self.MONDAY)
        for new_date, stage in (
            ("2026-09-06", "overdue"),  # the Sunday before, last ISO week
            (self.MONDAY, "today"),
            ("2026-09-09", "urgent"),  # Wednesday, still this ISO week
            ("2026-09-14", "ok"),  # the Monday after, a week out
        ):
            with self.subTest(date=new_date):
                self.assertEqual(self.moved_to(new_date), stage)

    def test_without_time_travel_the_real_today_decides(self):
        self.given_session_plan()
        self.assertEqual(self.moved_to(date.today().isoformat()), "today")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(self.moved_to(yesterday), "overdue")


class RescheduleReclassifiesTheWholeRowTest(DemoModeTestCase):
    """The client half of the same fix: both halves of the row move together,
    and the class list the client clears stays in step with the stages the
    server can actually answer with."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_client_clears_every_stage_the_server_can_answer_with(self):
        # `done` is deliberately absent: it is the toggle's own class, and
        # reclassifying a checked-off task must not strip its green.
        declared = re.search(
            r"const URGENCY_CLASSES = \[(.*?)\];", self.dashboard_html()
        )
        self.assertIsNotNone(declared)
        self.assertEqual(
            set(re.findall(r"'([a-z]+)'", declared.group(1))),
            set(_URGENCY_RANK) - {"done"},
        )

    def test_both_the_label_and_the_dot_are_reclassified(self):
        html = self.dashboard_html()
        self.assertIn("reclassify(dueSpan, data.urgency);", html)
        self.assertIn("if (dot) reclassify(dot, data.urgency);", html)

    def test_the_row_is_handed_in_rather_than_walked_up_to(self):
        # The picker does span.replaceWith(input) while it is open, so the
        # span has no parent for the duration of the request and closest()
        # called on it inside reschedule() would find nothing — the dot would
        # silently keep its pre-move stage. Both call sites therefore read
        # the row while the span is still attached and pass it in.
        html = self.dashboard_html()
        self.assertIn(
            "async function reschedule(taskId, newDate, dueSpan, row) {", html
        )
        self.assertIn("const row = span.closest('.task-row');", html)
        self.assertIn(
            "await reschedule(span.dataset.taskId, input.value, span, row);", html
        )
        # #239 moved the second call site into the actions menu, which reads
        # the row off the clicked item rather than off a button in the row.
        self.assertIn(
            "await reschedule(item.dataset.taskId, TODAY, dueSpan, row);", html
        )
        self.assertIn("const row = item.closest('.task-row');", html)


class RescheduleIncrementsCounterDemoModeTest(DemoModeTestCase):
    """#171: awareness, not punishment — the counter increments on every
    reschedule, the badge's >=2 threshold is a display concern (see
    PostponeBadgeRenderingTest)."""

    def post_date(self, task_id, new_date):
        return self.client.post(
            reverse("reschedule_task", args=[task_id]),
            data=json.dumps({"date": new_date}),
            content_type="application/json",
        )

    def test_first_reschedule_sets_the_count_to_one(self):
        self.given_session_plan()
        new_date = (date.today() + timedelta(days=14)).isoformat()
        response = self.post_date("demo-session-0", new_date)
        # The figures beside these are #216's business, asserted in
        # RescheduleAnswersTheRecomputedFiguresTest — this test is about the
        # counter, so it reads only the fields it is here for.
        answer = response.json()
        self.assertEqual(
            {
                k: answer[k]
                for k in (
                    "ok",
                    "postpone_count",
                    "due_display",
                    "due_display_row",
                    "urgency",
                )
            },
            {
                "ok": True,
                "postpone_count": 1,
                "due_display": format_date(date.fromisoformat(new_date)),
                "due_display_row": format_date(
                    date.fromisoformat(new_date), role="row"
                ),
                "urgency": "ok",
            },
        )
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["postpone_count"], 1
        )

    def test_counter_increments_across_multiple_reschedules(self):
        self.given_session_plan()
        response = None
        for _ in range(3):
            response = self.post_date(
                "demo-session-0", (date.today() + timedelta(days=14)).isoformat()
            )
        self.assertEqual(response.json()["postpone_count"], 3)

    def test_an_unknown_task_is_a_404_and_increments_nothing(self):
        self.given_session_plan()
        response = self.post_date(
            "demo-1-7", (date.today() + timedelta(days=14)).isoformat()
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("postpone_count", self.client.session["demo_plan"]["tasks"][0])


class TaskActionsMenuDrivesTheExistingControlsTest(DemoModeTestCase):
    """#239 stage 1 adds no endpoint. Each item drives a control the row
    already had, so there is one implementation of every write and the
    interaction can be settled without anything changing server-side.

    What that means concretely is asserted here against the rendered JS:
    the toggle item submits the row's own .toggle-form, "Datum ändern"
    clicks the date span the mouse would, and "→ heute" calls the same
    reschedule() the date picker does."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_toggle_item_submits_the_rows_own_form(self):
        self.assertIn(
            "row.querySelector('.toggle-form')?.requestSubmit();", self.dashboard_html()
        )

    def test_the_date_item_clicks_the_date_span(self):
        self.assertIn("dueSpan.click();", self.dashboard_html())

    def test_the_today_item_calls_reschedule_with_todays_date(self):
        self.assertIn(
            "await reschedule(item.dataset.taskId, TODAY, dueSpan, row);",
            self.dashboard_html(),
        )

    def test_the_project_item_opens_the_project_view(self):
        self.assertIn("showProject(item.dataset.projectId);", self.dashboard_html())

    def test_stage_one_added_no_endpoint_of_its_own(self):
        # The four items above all drive controls the row already had.
        # /rename/ arrived with stage 2 and is asserted there.
        # The four items all drive controls the row already had —
        # "Projekt öffnen" renders only where a task carries a project_id,
        # asserted in TaskActionsMenuMirrorsItsControlsTest. The two items
        # with endpoints of their own arrived with stages 2 and 3 and are
        # asserted there.
        html = self.dashboard_html()
        for action in ("toggle", "reschedule", "today"):
            self.assertIn(f'data-action="{action}"', html)
        self.assertNotIn("/delete/", html)

    def test_the_date_click_survives_as_a_desktop_shortcut(self):
        # The one-click reschedule used daily is not lost to the menu.
        self.assertIn(
            "document.querySelectorAll('.task-due[data-task-id]')",
            self.dashboard_html(),
        )


class TaskActionsMenuMirrorsItsControlsTest(DemoModeTestCase):
    """The menu can never offer what the row itself does not. Each item
    carries the same condition as the control it drives."""

    def test_no_toggle_item_while_a_zeitreise_moment_is_active(self):
        # #217: a moment is a rendering of a date, not a place to check
        # things off — _task_dot.html drops the button for the same reason.
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'data-action="toggle"')
        # The trigger stays: "Projekt öffnen" and the date are unaffected.
        self.assertContains(response, 'class="task-menu-trigger"')

    def test_the_toggle_item_is_offered_outside_a_moment(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'data-action="toggle"')

    def test_the_today_item_only_renders_on_an_overdue_row(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Längst fällig",
                    "date": (date.today() - timedelta(days=3)).isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Noch Zeit",
                    "date": (date.today() + timedelta(days=20)).isoformat(),
                    "done": False,
                },
            ]
        )
        html = self.client.get(reverse("dashboard")).content.decode()
        # One per rendered overdue row — the same task renders in more than
        # one list, the Heute bucket and the project detail. Counted on the
        # rendered button, not on the bare attribute: the JS below carries
        # the same selector to remove the item when a move lifts the row out
        # of overdue.
        self.assertEqual(
            html.count(">→ heute</button>"), html.count('class="dot overdue "')
        )
        self.assertEqual(html.count(">→ heute</button>"), 2)

    def test_the_project_item_renders_beside_every_project_label(self):
        # Only _build_week_view tags a task with its project (views.py), so
        # the item belongs to the Heute view and to no other list.
        # Against the clickable label, not every label: a task with no
        # project of its own is tagged "Ohne Projekt" (#53) and renders a
        # plain span with nothing to open.
        html = self.client.get(reverse("dashboard") + "?mode=multi").content.decode()
        self.assertGreater(html.count('class="task-project ai-project-link"'), 0)
        self.assertEqual(
            html.count('data-action="project"'),
            html.count('class="task-project ai-project-link"'),
        )

    def test_no_project_item_where_a_task_carries_no_project(self):
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('class="task-project', html)
        self.assertNotIn('data-action="project"', html)


class RenameTaskDemoModeTest(DemoModeTestCase):
    """#239 stage 2 in a demo session: the write lands in
    session['demo_plan'], the same place the toggle and the reschedule
    write to."""

    def post_name(self, task_id, name):
        return self.client.post(
            reverse("rename_task", args=[task_id]),
            data=json.dumps({"name": name}),
            content_type="application/json",
        )

    def test_a_rename_reaches_the_session_plan(self):
        self.given_session_plan()
        response = self.post_name("demo-session-0", "Programm endlich festlegen")
        self.assertEqual(
            response.json(), {"ok": True, "name": "Programm endlich festlegen"}
        )
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["name"],
            "Programm endlich festlegen",
        )

    def test_the_new_name_survives_a_reload(self):
        self.given_session_plan()
        self.post_name("demo-session-0", "Programm endlich festlegen")
        self.assertContains(
            self.client.get(reverse("dashboard")), "Programm endlich festlegen"
        )

    def test_a_rename_during_a_moment_is_refused(self):
        # #217: a moment is a rendering of a date, not a place to change
        # things. Same refusal as the toggle.
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        response = self.post_name("demo-session-0", "Anders")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["name"], "Programm festlegen"
        )

    def test_an_unknown_task_is_a_404(self):
        self.given_session_plan()
        self.assertEqual(self.post_name("demo-1-7", "Anders").status_code, 404)

    def test_a_get_is_a_405(self):
        self.given_session_plan()
        response = self.client.get(reverse("rename_task", args=["demo-session-0"]))
        self.assertEqual(response.status_code, 405)

    def test_a_malformed_body_is_a_400(self):
        self.given_session_plan()
        response = self.client.post(
            reverse("rename_task", args=["demo-session-0"]),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_empty_name_is_a_400(self):
        # A row with nothing to identify it by is worse than the old name.
        self.given_session_plan()
        for value in ("", "   ", None, 42, ["Neu"]):
            with self.subTest(value=value):
                self.assertEqual(
                    self.post_name("demo-session-0", value).status_code, 400
                )
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["name"], "Programm festlegen"
        )

    def test_the_name_is_stripped(self):
        self.given_session_plan()
        self.assertEqual(
            self.post_name("demo-session-0", "  Neu  ").json()["name"], "Neu"
        )


class RenameHappensInTheRowTest(DemoModeTestCase):
    """The name is edited where it stands — the same swap-the-element-out
    shape the date has used since #10, rather than a browser prompt() that
    would sit outside the page's own visual language."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_menu_offers_the_item(self):
        self.assertIn('data-action="rename"', self.dashboard_html())

    def test_the_name_span_is_swapped_for_an_input(self):
        html = self.dashboard_html()
        self.assertIn("nameSpan.replaceWith(input);", html)
        self.assertIn("input.className = 'task-name-input';", html)

    def test_enter_commits_and_escape_cancels(self):
        html = self.dashboard_html()
        self.assertIn("if (e.key === 'Escape') {", html)
        self.assertIn("} else if (e.key === 'Enter') {", html)

    def test_the_input_leaves_the_dom_exactly_once(self):
        # Enter swaps the span back and the blur that follows must not swap
        # a second time.
        html = self.dashboard_html()
        self.assertIn("let settled = false;", html)
        self.assertIn("if (settled) return;", html)

    def test_the_write_goes_to_the_rename_endpoint(self):
        self.assertIn("`/task/${taskId}/rename/`", self.dashboard_html())

    def test_every_rendered_copy_of_the_name_is_updated(self):
        # The same task renders in the task rows, the AI summary, a day card
        # and on the board — one selector list, the same rule applyTaskDone
        # follows.
        html = self.dashboard_html()
        self.assertIn("function applyTaskName(taskId, name) {", html)
        self.assertIn("card.querySelector('.task-name, .day-task-name')", html)
        self.assertIn('.kanban-card[data-task-id="${taskId}"] .kanban-card-name', html)


@override_settings(DEMO_MODE=False)
class RenameTaskProductionTest(TestCase):
    """The production half: the write goes to Notion, and the cached lists
    carry it rather than being thrown away (#199) — a rename moves nothing
    in the chronological order, so it fits _patch_cached_tasks directly."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_name(self, task_id="task-1", name="Neuer Name"):
        return self.client.post(
            reverse("rename_task", args=[task_id]),
            data=json.dumps({"name": name}),
            content_type="application/json",
        )

    def test_the_write_reaches_notion(self):
        with patch("projects.views.rename_task") as mock_rename:
            response = self.post_name()
        mock_rename.assert_called_once_with("task-1", "Neuer Name")
        self.assertEqual(response.json(), {"ok": True, "name": "Neuer Name"})

    def test_a_notion_failure_is_a_502(self):
        with patch(
            "projects.views.rename_task", side_effect=NotionUnavailableError("boom")
        ):
            self.assertEqual(self.post_name().status_code, 502)

    def test_a_failing_write_is_not_reported_as_done(self):
        with patch(
            "projects.views.rename_task", side_effect=NotionUnavailableError("boom")
        ):
            response = self.post_name()
        self.assertNotIn("ok", response.json())

    def test_the_cached_lists_carry_the_rename(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ) as mock_summary,
        ):
            self.client.get(reverse("dashboard"))
            with patch("projects.views.rename_task"):
                self.post_name("task-1", "Programm endlich festlegen")
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Programm endlich festlegen")
        # The point of patching rather than busting: neither the Notion read
        # nor the Claude call is paid for again.
        self.assertEqual(mock_summary.call_count, 1)

    def test_a_cold_cache_falls_back_to_a_bust(self):
        with patch("projects.views.rename_task"):
            response = self.post_name()
        self.assertEqual(response.json(), {"ok": True, "name": "Neuer Name"})
        self.assertIsNone(cache.get(CACHE_KEY))


class TrashTaskDemoModeTest(DemoModeTestCase):
    """#239 stage 3 in a demo session: the task leaves
    session['demo_plan'], the same place the other writes land."""

    def post_trash(self, task_id):
        return self.client.post(
            reverse("trash_task", args=[task_id]),
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_the_task_leaves_the_session_plan(self):
        self.given_session_plan()
        self.assertEqual(self.post_trash("demo-session-0").json(), {"ok": True})
        self.assertEqual(self.client.session["demo_plan"]["tasks"], [])

    def test_it_is_gone_from_every_list_on_the_next_render(self):
        self.given_session_plan()
        self.post_trash("demo-session-0")
        self.assertNotContains(
            self.client.get(reverse("dashboard")), "Programm festlegen"
        )

    def test_the_cached_summaries_are_swept(self):
        # Their task_refs were numbered against an order this task was part
        # of, so they cannot be rewritten — a ref no longer points at the
        # task it was written for.
        self.given_session_plan()
        self.client.get(reverse("dashboard"))
        session = self.client.session
        session[f"{SUMMARY_KEY}_today"] = _summary_data()
        session.save()
        self.post_trash("demo-session-0")
        self.assertNotIn(f"{SUMMARY_KEY}_today", self.client.session)

    def test_a_trash_during_a_moment_is_refused(self):
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        self.assertEqual(self.post_trash("demo-session-0").status_code, 404)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_an_unknown_task_is_a_404(self):
        self.given_session_plan()
        self.assertEqual(self.post_trash("demo-1-7").status_code, 404)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_a_get_is_a_405(self):
        self.given_session_plan()
        response = self.client.get(reverse("trash_task", args=["demo-session-0"]))
        self.assertEqual(response.status_code, 405)

    def test_a_malformed_body_is_a_400(self):
        self.given_session_plan()
        response = self.client.post(
            reverse("trash_task", args=["demo-session-0"]),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)


@override_settings(DEMO_MODE=False)
class TrashTaskProductionTest(TestCase):
    """The production half. The cache is busted rather than patched —
    _patch_cached_tasks mutates in place and has no removal path, and a
    removal shifts every count and every cached task_ref. The cost is one
    Notion read and one Claude call on the next render, paid for the least
    frequent write in the app rather than reshaping the path every other
    write hangs off."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_trash(self, task_id="task-1"):
        return self.client.post(
            reverse("trash_task", args=[task_id]),
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_the_write_reaches_notion(self):
        with patch("projects.views.trash_task") as mock_trash:
            response = self.post_trash()
        mock_trash.assert_called_once_with("task-1")
        self.assertEqual(response.json(), {"ok": True})

    def test_a_notion_failure_is_a_502_and_leaves_the_cache_alone(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
        with patch(
            "projects.views.trash_task", side_effect=NotionUnavailableError("boom")
        ):
            self.assertEqual(self.post_trash().status_code, 502)
        self.assertIsNotNone(cache.get(CACHE_KEY))

    def test_a_confirmed_removal_busts_every_cached_copy(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
        with patch("projects.views.trash_task"):
            self.post_trash()
        for key in (
            CACHE_KEY,
            STALE_CACHE_KEY,
            UNASSIGNED_CACHE_KEY,
            STALE_UNASSIGNED_CACHE_KEY,
        ):
            with self.subTest(key=key):
                self.assertIsNone(cache.get(key))

    def test_the_task_is_gone_from_the_next_render(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
            with patch("projects.views.trash_task"):
                self.post_trash()
            project["tasks"] = []
            response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Programm festlegen")


class TrashHappensBehindASecondClickTest(DemoModeTestCase):
    """The only action that reads as irreversible, so it asks — a two-step
    inside the menu rather than a modal, which would sit outside the page's
    own language and block everything behind it."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_item_says_papierkorb_not_loeschen(self):
        # The Notion API cannot permanently delete: the page stays
        # restorable in the trash, and "Löschen" would promise otherwise.
        html = self.dashboard_html()
        self.assertIn(">In den Papierkorb</button>", html)
        self.assertNotIn(">Löschen</button>", html)

    def test_the_first_click_only_arms_it(self):
        html = self.dashboard_html()
        self.assertIn(
            "if (action === 'trash' && !item.classList.contains('armed')) {", html
        )
        self.assertIn("item.textContent = TRASH_ARMED_LABEL;", html)

    def test_closing_the_menu_disarms_it(self):
        # Reopening never starts one click away from a removal.
        html = self.dashboard_html()
        self.assertIn(
            "items.querySelectorAll('.task-menu-item[data-action=\"trash\"]')"
            ".forEach(disarmTrash);",
            html,
        )

    def test_a_confirmed_removal_reloads_the_page(self):
        # Every count, progress bar and board badge is re-rendered by the
        # server rather than reconciled by hand (#210) — there is no warm
        # cache left to derive figures from anyway.
        html = self.dashboard_html()
        self.assertIn("`/task/${taskId}/trash/`", html)
        self.assertIn("window.location.reload();", html)


class RescheduleOfferedOnlyWherePersistedTest(DemoModeTestCase):
    """§5 of #10: rescheduling is offered exactly where it persists — via Notion
    in production, via session['demo_plan'] for a demo session plan. The five demo
    example projects come from get_demo_projects() and are in no session, so the
    interaction is not offered for them at all."""

    def test_offered_for_a_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="Datum ändern"')

    def test_not_offered_in_the_multi_project_view(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertNotContains(response, 'title="Datum ändern"')
        # #239: the menu mirrors the controls it drives, so it cannot offer
        # a write the row itself does not.
        self.assertNotContains(response, ">Datum ändern</button>")
        self.assertNotContains(response, ">→ heute</button>")

    def test_not_offered_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'title="Datum ändern"')
        self.assertNotContains(response, ">Datum ändern</button>")
        self.assertNotContains(response, ">→ heute</button>")

    def test_the_date_itself_still_renders_when_it_is_not_clickable(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, 'class="task-due')

    @override_settings(DEMO_MODE=False)
    def test_still_offered_in_production(self):
        # has_session_plan is only ever set in the DEMO_MODE branch, so gating
        # on it alone would have removed rescheduling from production — where
        # it does persist, to Notion.
        cache.clear()
        self.addCleanup(cache.clear)
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="Datum ändern"')
        self.assertContains(response, 'data-task-id="task-1"')


class ToggleSyncCoversEveryCardShapeTest(DemoModeTestCase):
    """#210: the toggle's DOM sync was written for the two card shapes that
    existed when #122 added it — the task row and the AI summary's list item.
    The day-column card, added later, carries a matching toggle-form and so
    got its dot flipped, but its name span is `.day-task-name` and the card
    itself is no `.task-row` or `<li>`, so neither the strike-through nor the
    dimming ever arrived. Half an update reads as "it worked", which is why
    this is asserted per shape rather than on the selector as a whole."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_sync_is_one_named_function(self):
        # Inline in the submit handler it could only ever serve the toggle;
        # named, it is the one place that knows what "this task is done"
        # looks like across the document.
        self.assertIn("function applyTaskDone(taskId, done) {", self.dashboard_html())

    def test_the_handler_calls_it_instead_of_carrying_its_own_copy(self):
        html = self.dashboard_html()
        self.assertIn("applyTaskDone(taskId, done);", html)
        self.assertNotIn(
            "const nameSpan = f.closest('.task-row, li')?.querySelector('.task-name');",
            html,
        )

    def test_all_three_card_shapes_are_reachable(self):
        self.assertIn(
            "f.closest('.task-row, li, .day-task-card')", self.dashboard_html()
        )

    def test_the_day_columns_own_name_span_is_in_the_selector(self):
        self.assertIn("'.task-name, .day-task-name'", self.dashboard_html())

    def test_the_day_card_itself_is_dimmed_not_only_its_name(self):
        # .day-task-card.done { opacity: 0.55 } sits on the card, so the
        # class has to land there too — the name span alone leaves a card
        # at full strength with a struck-through label inside it.
        html = self.dashboard_html()
        self.assertIn(".day-task-card.done { opacity: 0.55; }", html)
        self.assertIn("card.classList.toggle('done', done);", html)


def _cached_task(task_id, due, done=False, completed_date=None):
    """A task dict in the shape notion.py hands back and the cache stores."""
    return {
        "id": task_id,
        "name": f"Aufgabe {task_id}",
        "due": due,
        "done": done,
        "kontext": [],
        "postpone_count": 0,
        "completed_date": completed_date,
    }


def _warm_dashboard_cache(tasks, unassigned=(), summary="<p>alt</p>", today=None):
    """Fills all four dashboard cache keys the way a successful dashboard()
    read leaves them. Every Django cache backend serializes on set and get,
    so the four entries are independent object graphs — which is exactly why
    a patch has to reach each of them.

    The two live entries go in through _cache_fresh_read, the same seam
    dashboard() uses, so they carry the deadline stamp a patch needs (#216)
    instead of the helper having to remember to add one."""
    today = today or date.today()
    project = _fake_upcoming_project()
    project["tasks"] = [dict(t) for t in tasks]
    projects = _annotate_tasks([project], today)
    unassigned_tasks = _annotate_tasks(
        [{"id": "_unassigned", "tasks": [dict(t) for t in unassigned]}], today
    )[0]["tasks"]
    _cache_fresh_read(CACHE_KEY, (projects, summary), CACHE_DEADLINE_KEY, 60)
    cache.set(STALE_CACHE_KEY, (projects, summary), None)
    _cache_fresh_read(
        UNASSIGNED_CACHE_KEY, unassigned_tasks, UNASSIGNED_CACHE_DEADLINE_KEY, 60
    )
    cache.set(STALE_UNASSIGNED_CACHE_KEY, unassigned_tasks, None)


def _summary_with_refs(task_refs):
    """A raw reference dict (#122) whose one block points at task positions
    — the numbering a reschedule moves."""
    return {
        "jetzt_faellig": [
            {
                "project_ref": 1,
                "assessment": "Zusammenfassung läuft",
                "task_refs": list(task_refs),
            }
        ],
        "naechste_woche": [],
    }


def _cached_task_refs(cache_key):
    return cache.get(cache_key)[1]["jetzt_faellig"][0]["task_refs"]


def _cached_task_by_id(cache_key, task_id):
    entry = cache.get(cache_key)
    if entry is None:
        return None
    tasks = (
        [t for p in entry[0] for t in p["tasks"]]
        if isinstance(entry, tuple)
        else list(entry)
    )
    return next((t for t in tasks if t["id"] == task_id), None)


@override_settings(DEMO_MODE=False)
class ToggleKeepsTheDashboardCacheWarmTest(TestCase):
    """#199: a toggle used to throw the whole dashboard cache away, so the
    next read paid a full Notion round trip plus a Claude call. The task
    order deliberately excludes `done` from its sort key (_annotate_tasks),
    so a toggle moves nothing — positions stay valid, the summary's
    task_refs stay valid, and setting the two fields in the cached dicts and
    re-running the cheap derivations is enough."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_toggle(self, task_id, done=True):
        with patch("projects.views.toggle_task"):
            return self.client.post(
                reverse("toggle_task", args=[task_id]),
                data=json.dumps({"done": done}),
                content_type="application/json",
            )

    def test_the_cache_is_patched_not_deleted(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        self.post_toggle("task-1")
        self.assertIsNotNone(cache.get(CACHE_KEY))
        task = _cached_task_by_id(CACHE_KEY, "task-1")
        self.assertTrue(task["done"])
        self.assertEqual(task["completed_date"], date.today())

    def test_the_stale_copy_is_patched_too(self):
        # It never expires, so leaving it behind would let the Notion-down
        # fallback serve a state predating a confirmed write — the one thing
        # _bust_dashboard_cache's docstring promises against.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        self.post_toggle("task-1")
        self.assertTrue(_cached_task_by_id(STALE_CACHE_KEY, "task-1")["done"])

    def test_a_task_without_a_project_is_patched_in_its_own_key_pair(self):
        _warm_dashboard_cache(
            [_cached_task("task-1", date.today())],
            unassigned=[_cached_task("loose-1", date.today())],
        )
        self.post_toggle("loose-1")
        self.assertTrue(_cached_task_by_id(UNASSIGNED_CACHE_KEY, "loose-1")["done"])
        self.assertTrue(
            _cached_task_by_id(STALE_UNASSIGNED_CACHE_KEY, "loose-1")["done"]
        )

    def test_a_project_task_leaves_the_project_less_stale_copy_alone(self):
        # The two stale entries are written independently — dashboard() only
        # writes STALE_CACHE_KEY when the summary is not None — so one
        # routinely exists without the other. A project task is never in the
        # project-less copy, so failing to find it there says nothing about
        # that copy and must not cost it (#216).
        _warm_dashboard_cache(
            [_cached_task("task-1", date.today())],
            unassigned=[_cached_task("loose-1", date.today())],
        )
        cache.delete(STALE_CACHE_KEY)
        self.post_toggle("task-1")
        self.assertIsNotNone(cache.get(STALE_UNASSIGNED_CACHE_KEY))

    def test_a_project_less_task_leaves_the_projects_stale_copy_alone(self):
        # The costlier direction of the same mistake: this copy carries the
        # projects and the summary the last Claude call paid for.
        _warm_dashboard_cache(
            [_cached_task("task-1", date.today())],
            unassigned=[_cached_task("loose-1", date.today())],
            summary="<p>alt</p>",
        )
        cache.delete(STALE_UNASSIGNED_CACHE_KEY)
        self.post_toggle("loose-1")
        self.assertEqual(cache.get(STALE_CACHE_KEY)[1], "<p>alt</p>")

    def test_a_stale_copy_predating_the_task_is_still_dropped(self):
        # The rule the split above must not weaken: a snapshot that cannot
        # carry the write cannot be corrected, so it goes rather than serve a
        # state older than a confirmed write.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        stale_projects, stale_summary = cache.get(STALE_CACHE_KEY)
        stale_projects[0]["tasks"] = []
        cache.set(STALE_CACHE_KEY, (stale_projects, stale_summary), None)
        self.post_toggle("task-1")
        self.assertIsNone(cache.get(STALE_CACHE_KEY))

    def test_the_derived_fields_are_recomputed_not_only_the_raw_ones(self):
        # A patch that writes `done` and stops leaves the dot, the board and
        # the sidebar ring rendering the pre-toggle state on the next load.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        self.post_toggle("task-1")
        task = _cached_task_by_id(CACHE_KEY, "task-1")
        self.assertEqual(task["urgency"], "done")
        self.assertEqual(task["kanban_column"], "done")
        project = cache.get(CACHE_KEY)[0][0]
        self.assertEqual(project["done_count"], 1)
        self.assertEqual(project["urgency"], "ok")
        self.assertEqual(project["ring_dashoffset"], "0.00")

    def test_the_summary_survives(self):
        # The whole point: the toggle moves no task, so every task_ref the
        # cached summary holds still points where it did.
        _warm_dashboard_cache(
            [_cached_task("task-1", date.today())], summary="<p>alt</p>"
        )
        self.post_toggle("task-1")
        self.assertEqual(cache.get(CACHE_KEY)[1], "<p>alt</p>")

    def test_a_task_in_no_cached_list_falls_back_to_a_full_bust(self):
        # The cached lists are then not the state Notion now holds, and
        # serving them would be a lie. The fallback is the normal path.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        self.post_toggle("task-99")
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))
        self.assertIsNone(cache.get(UNASSIGNED_CACHE_KEY))
        self.assertIsNone(cache.get(STALE_UNASSIGNED_CACHE_KEY))

    def test_a_cold_cache_stays_cold(self):
        response = self.post_toggle("task-1")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(cache.get(CACHE_KEY))

    def test_a_notion_failure_patches_nothing(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        with patch(
            "projects.views.toggle_task", side_effect=NotionUnavailableError("boom")
        ):
            self.client.post(
                reverse("toggle_task", args=["task-1"]),
                data='{"done": true}',
                content_type="application/json",
            )
        self.assertFalse(_cached_task_by_id(CACHE_KEY, "task-1")["done"])


@override_settings(DEMO_MODE=False)
class PatchingDoesNotRenewTheReadWindowTest(TestCase):
    """#216: #199 turned a write from a cache delete into a cache re-write,
    and a re-write has to name a timeout. Naming CACHE_TTL renewed the eight
    hours on every checkbox — check one task off per working day and the
    dashboard never performs an unforced Notion read again, so a task edited
    in Notion's own UI (which _count_done_in_range explicitly expects) stays
    invisible for as long as the patching continues. The TTL is a freshness
    policy about the *read*: the read stamps a deadline, every later write
    keeps it, and a write that cannot keep it falls back to the bust.

    Every assertion below is on the timeout a write actually named, not on
    the deadline stamp beside it — a patch never touches that stamp either
    way, so asserting on it would pass with the bug still in place.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_toggle(self, task_id="task-1", done=True):
        with patch("projects.views.toggle_task"):
            return self.client.post(
                reverse("toggle_task", args=[task_id]),
                data=json.dumps({"done": done}),
                content_type="application/json",
            )

    def post_reschedule(self, task_id="task-1", days=3):
        with (
            patch("projects.views.update_task_date"),
            patch("projects.views.increment_postpone_count", return_value=1),
        ):
            return self.client.post(
                reverse("reschedule_task", args=[task_id]),
                data=json.dumps(
                    {"date": (date.today() + timedelta(days=days)).isoformat()}
                ),
                content_type="application/json",
            )

    def timeouts_named_by(self, action, *keys):
        """The timeout every write to `keys` named while `action` ran.

        Django's cache API cannot report how long an entry has left, so
        watching the writes from inside views is the only way to see the
        difference between "put back" and "renewed"."""
        with patch("projects.views.cache", wraps=cache) as views_cache:
            action()
        return [
            call.args[2]
            for call in views_cache.set.call_args_list
            if call.args[0] in keys and len(call.args) > 2
        ]

    # _warm_dashboard_cache seeds the live pair with 60 seconds, so a
    # timeout above that is the entry outliving the read that filled it.
    SEEDED_TTL = 60

    def test_a_toggle_puts_the_entry_back_with_the_time_it_had_left(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        timeouts = self.timeouts_named_by(
            self.post_toggle, CACHE_KEY, UNASSIGNED_CACHE_KEY
        )
        # Both live entries, patched as #199 wants — and neither renewed.
        self.assertEqual(len(timeouts), 2)
        for timeout in timeouts:
            self.assertLessEqual(timeout, self.SEEDED_TTL)
        self.assertTrue(_cached_task_by_id(CACHE_KEY, "task-1")["done"])

    def test_a_reschedule_puts_them_back_with_the_time_they_had_left(self):
        # Two patches in one request — the confirmed date, then the
        # confirmed postpone counter — so a renewal here would compound.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        timeouts = self.timeouts_named_by(self.post_reschedule, CACHE_KEY)
        self.assertEqual(len(timeouts), 2)
        for timeout in timeouts:
            self.assertLessEqual(timeout, self.SEEDED_TTL)

    def test_regenerating_a_dropped_summary_does_not_renew_it_either(self):
        # Those projects came out of the cache, not out of Notion — only the
        # summary is new, so a Claude call must not restart the window a
        # Notion read opened.
        _warm_dashboard_cache([_cached_task("task-1", date.today())], summary=None)

        def load():
            with (
                patch(
                    "projects.views.generate_weekly_summary",
                    return_value=_summary_data(),
                ),
                patch("projects.views.get_upcoming_projects") as fetch,
            ):
                self.client.get(reverse("dashboard"))
            fetch.assert_not_called()

        timeouts = self.timeouts_named_by(load, CACHE_KEY)
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], self.SEEDED_TTL)
        self.assertEqual(cache.get(CACHE_KEY)[1], _summary_data())

    def test_an_elapsed_deadline_busts_instead_of_patching(self):
        # Past its window the entry may not go back at all: writing it would
        # extend it. The fallback is the delete #199 replaced, so the next
        # load pays one Notion read and the freshness policy holds.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        cache.set(CACHE_DEADLINE_KEY, timezone.now() - timedelta(seconds=1), 60)
        response = self.post_toggle()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(UNASSIGNED_CACHE_KEY))
        # No figures either, so the client reloads rather than writing
        # numbers the server had nothing to derive from.
        self.assertEqual(response.json(), {"ok": True})

    def test_an_entry_from_before_the_stamp_existed_is_busted_not_renewed(self):
        # The first request after this deploy meets entries no read stamped.
        # An unknown deadline is treated as none, never as a fresh one.
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        cache.delete(CACHE_DEADLINE_KEY)
        cache.delete(UNASSIGNED_CACHE_DEADLINE_KEY)
        self.post_toggle()
        self.assertIsNone(cache.get(CACHE_KEY))

    def test_a_fresh_notion_read_is_what_starts_the_window(self):
        # The one event allowed to move the deadline, because it is the one
        # the eight hours are actually about.
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
        for key in (CACHE_DEADLINE_KEY, UNASSIGNED_CACHE_DEADLINE_KEY):
            with self.subTest(key=key):
                self.assertGreater(
                    cache.get(key), timezone.now() + timedelta(seconds=CACHE_TTL - 60)
                )


@override_settings(DEMO_MODE=False)
class ToggleAnswersTheRecomputedFiguresTest(TestCase):
    """#210: every count on the dashboard is server-derived, and a toggle
    can change a denominator, not just a numerator — an overdue task from an
    earlier week, cleared today, enters this week's total. So the server,
    which already knows the answer, hands it back instead of leaving the
    client to reimplement _count_done_in_range in JavaScript."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_toggle(self, task_id, done=True, **body):
        with patch("projects.views.toggle_task"):
            return self.client.post(
                reverse("toggle_task", args=[task_id]),
                data=json.dumps({"done": done, **body}),
                content_type="application/json",
            )

    def test_the_week_bar_counts_come_back(self):
        today = date.today()
        _warm_dashboard_cache(
            [_cached_task("task-1", today), _cached_task("task-2", today)]
        )
        data = self.post_toggle("task-1").json()
        self.assertEqual(data["week"], {"done": 1, "total": 2, "pct": 50})

    def test_all_seven_day_counts_come_back_keyed_by_iso_date(self):
        today = date.today()
        monday = iso_week_bounds(today)[0]
        _warm_dashboard_cache([_cached_task("task-1", monday)])
        data = self.post_toggle("task-1", week_start=monday.isoformat()).json()
        self.assertEqual(len(data["days"]), 7)
        self.assertEqual(data["days"][monday.isoformat()], {"done": 1, "total": 1})

    def test_the_browsed_week_is_the_one_the_client_is_showing(self):
        # ?week= navigates the day columns to any week; the server cannot
        # guess which one is on screen, so the client sends its Monday.
        today = date.today()
        next_monday = iso_week_bounds(today)[0] + timedelta(days=7)
        _warm_dashboard_cache([_cached_task("task-1", next_monday)])
        data = self.post_toggle("task-1", week_start=next_monday.isoformat()).json()
        self.assertIn(next_monday.isoformat(), data["days"])
        self.assertEqual(data["days"][next_monday.isoformat()], {"done": 1, "total": 1})

    def test_an_unparseable_week_start_falls_back_to_the_current_week(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        data = self.post_toggle("task-1", week_start="übermorgen").json()
        self.assertIn(iso_week_bounds(today)[0].isoformat(), data["days"])

    def test_a_week_start_against_the_calendars_edge_answers_normally(self):
        # #216: it parses, so the guard above never saw it, and
        # _bucket_by_day walking six days on from date.max raised
        # OverflowError — a 500 for a Notion write that had already been
        # confirmed, which the client reads as 'it failed' and leaves the
        # checkbox alone. Falls back to the current week like any other
        # week_start this view cannot use.
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        for edge in (date.max.isoformat(), date.min.isoformat()):
            with self.subTest(edge=edge):
                response = self.post_toggle("task-1", week_start=edge)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    iso_week_bounds(today)[0].isoformat(), response.json()["days"]
                )

    def test_the_kanban_column_counts_come_back(self):
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("task-1", today),
                _cached_task("task-2", today + timedelta(days=90)),
            ]
        )
        data = self.post_toggle("task-1").json()
        self.assertEqual(data["kanban"], {"open": 1, "urgent": 0, "done": 1})

    def test_the_toggled_task_names_the_column_it_belongs_in_now(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        data = self.post_toggle("task-1").json()
        self.assertEqual(data["urgency"], "done")
        self.assertEqual(data["kanban_column"], "done")
        self.assertEqual(
            self.post_toggle("task-1", done=False).json()["urgency"], "today"
        )

    def test_the_affected_projects_ring_comes_back(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        data = self.post_toggle("task-1").json()
        self.assertEqual(data["project"]["id"], "p1")
        self.assertEqual(data["project"]["ring_dashoffset"], "0.00")
        self.assertEqual(data["project"]["urgency"], "ok")

    def test_a_task_without_a_project_carries_no_ring(self):
        today = date.today()
        _warm_dashboard_cache(
            [_cached_task("task-1", today)],
            unassigned=[_cached_task("loose-1", today)],
        )
        data = self.post_toggle("loose-1").json()
        self.assertNotIn("project", data)
        # It still moves the week-independent figures it does belong to.
        self.assertEqual(data["days"][today.isoformat()]["done"], 1)

    def test_clearing_an_older_overdue_task_changes_the_weeks_denominator(self):
        # The effect that rules out recomputing in JavaScript: the task is
        # due outside this week, so it counts toward nothing here — until it
        # is completed inside it, at which point it joins both halves of the
        # fraction. A card that never moved on screen changed the total.
        today = date.today()
        long_overdue = iso_week_bounds(today)[0] - timedelta(days=14)
        _warm_dashboard_cache(
            [
                _cached_task("task-1", today),
                _cached_task("task-2", today),
                _cached_task("old", long_overdue),
            ]
        )
        # Two tasks in range, one outside it counting toward neither half.
        self.assertEqual(
            self.post_toggle("task-1").json()["week"],
            {"done": 1, "total": 2, "pct": 50},
        )
        # Completing the third pulls it into both halves at once — the
        # denominator moved without a single card moving on screen.
        self.assertEqual(
            self.post_toggle("old").json()["week"], {"done": 2, "total": 3, "pct": 67}
        )

    def test_a_cold_cache_answers_without_figures(self):
        # Nothing to derive from — the client reloads rather than being
        # handed numbers the server had to guess at.
        data = self.post_toggle("task-1").json()
        self.assertEqual(data, {"ok": True})

    def test_the_load_path_and_the_toggle_path_share_one_helper(self):
        # A second implementation is what #210 is; asserting the call is
        # what keeps the two from drifting apart again.
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
            patch(
                "projects.views._derive_dashboard_figures",
                side_effect=_derive_dashboard_figures,
            ) as derive,
        ):
            self.client.get(reverse("dashboard"))
        derive.assert_called_once()


@override_settings(DEMO_MODE=False)
class KanbanCountsComeFromTheServerTest(TestCase):
    """They used to be counted in the browser from .kanban-card classes, so
    the toggle path and the load path could show different numbers for the
    same board."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_badges_are_rendered_not_counted_in_the_browser(self):
        project = _fake_upcoming_project()
        project["tasks"] = [
            _cached_task("t-open", date.today() + timedelta(days=90)),
            _cached_task("t-urgent", date.today()),
            _cached_task(
                "t-done", date.today(), done=True, completed_date=date.today()
            ),
        ]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            html = self.client.get(reverse("dashboard")).content.decode()
        for badge_id, count in (("open", 1), ("urgent", 1), ("done", 1)):
            self.assertIn(
                f'<span class="kanban-col-count" id="count-{badge_id}">{count}</span>',
                html,
            )
        self.assertNotIn("document.querySelectorAll('.kanban-card.ok", html)


class ToggleFiguresFromTheSessionPlanTest(DemoModeTestCase):
    """#183's exception has to survive the extraction: in a demo session the
    bar counts the whole plan, not the week — a week-scoped count barely
    moved between Zeitreise moments."""

    def test_the_bar_counts_the_whole_plan_not_the_week(self):
        far = (date.today() + timedelta(days=60)).isoformat()
        self.given_session_plan(
            tasks=[
                {"id": "demo-session-0", "name": "Nah", "date": far, "done": False},
                {"id": "demo-session-1", "name": "Fern", "date": far, "done": False},
            ]
        )
        response = self.client.post(
            reverse("toggle_task", args=["demo-session-0"]),
            data='{"done": true}',
            content_type="application/json",
        )
        # Week-scoped both tasks would be out of range entirely (0 / 0).
        self.assertEqual(response.json()["week"], {"done": 1, "total": 2, "pct": 50})

    def test_the_session_plan_is_its_own_project_for_the_ring(self):
        self.given_session_plan()
        response = self.client.post(
            reverse("toggle_task", args=["demo-session-0"]),
            data='{"done": true}',
            content_type="application/json",
        )
        self.assertEqual(response.json()["project"]["id"], "session-plan")
        self.assertEqual(response.json()["project"]["ring_dashoffset"], "0.00")

    def test_an_unknown_task_still_answers_404_without_figures(self):
        self.given_session_plan()
        response = self.client.post(
            reverse("toggle_task", args=["nope"]),
            data='{"done": true}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class ToggleUpdatesEverySurfaceTest(DemoModeTestCase):
    """The client half of #210. Each surface is asserted on its own: the
    failure mode here was additive — every surface was correct on load and
    nobody carried the toggle path forward — so a single "the handler exists"
    assertion is exactly the check that would have passed all along."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_figures_are_written_by_one_named_function(self):
        html = self.dashboard_html()
        self.assertIn("function applyToggleFigures(taskId, data) {", html)
        self.assertIn("applyToggleFigures(taskId, data)", html)

    def test_the_week_bar_and_its_label_are_written(self):
        html = self.dashboard_html()
        self.assertIn("fill.style.width = data.week.pct + '%';", html)
        self.assertIn(
            "label.textContent = data.week.total ? "
            "`${data.week.done} / ${data.week.total} erledigt` : '';",
            html,
        )

    def test_the_day_column_counters_are_written(self):
        html = self.dashboard_html()
        self.assertIn(
            'document.querySelector(`.day-column-body[data-date="${iso}"]`)', html
        )
        self.assertIn("badge.textContent = `${counts.done}/${counts.total}`;", html)

    def test_a_day_that_empties_loses_its_badge(self):
        # The template renders the badge only when total_count is truthy, so
        # leaving a "0/0" behind would be a shape the server never renders.
        self.assertIn(
            "if (!counts.total) { if (badge) badge.remove(); return; }",
            self.dashboard_html(),
        )

    def test_the_kanban_counts_are_written(self):
        self.assertIn(
            "document.getElementById('count-' + column)", self.dashboard_html()
        )

    def test_the_card_moves_to_the_column_the_server_named(self):
        html = self.dashboard_html()
        self.assertIn(
            "document.querySelector('.kanban-col.col-' + data.kanban_column)", html
        )
        self.assertIn("column.appendChild(card);", html)
        self.assertIn("reclassify(card, data.urgency);", html)
        self.assertIn("card.classList.toggle('done', data.urgency === 'done');", html)

    def test_the_sidebar_ring_is_written(self):
        html = self.dashboard_html()
        self.assertIn(
            "ring.setAttribute('stroke-dashoffset', data.project.ring_dashoffset);",
            html,
        )
        self.assertIn("reclassify(ring, data.project.urgency);", html)

    def test_the_ring_is_addressable_by_project_id(self):
        # id="nav-…" only exists on the dashboard's own branch of the
        # sidebar partial, so it is no reliable anchor for this.
        html = self.dashboard_html()
        self.assertIn('data-project-id="session-plan"', html)
        partial = (
            settings.BASE_DIR / "projects/templates/projects/_sidebar_project_list.html"
        ).read_text()
        self.assertEqual(partial.count('data-project-id="{{ project.id }}"'), 2)

    def test_a_response_without_figures_reloads(self):
        self.assertIn(
            "if (!applyToggleFigures(taskId, data)) window.location.reload();",
            self.dashboard_html(),
        )

    def test_the_client_tells_the_server_which_week_it_is_showing(self):
        # ?week= navigates the day columns to any week and the server cannot
        # guess which one is on screen.
        html = self.dashboard_html()
        self.assertIn("document.querySelector('.day-column-body[data-date]')", html)
        # One helper, sent by both writes — the reschedule answer is scoped
        # to the same week (#216).
        self.assertEqual(html.count("week_start: browsedWeekStart()"), 2)

    def test_no_count_is_derived_in_javascript(self):
        # The whole point of the server answering with figures. A length
        # count over rendered cards is week-blind and completion-date-blind.
        html = self.dashboard_html()
        toggle_block = html[
            html.index("function applyToggleFigures") : html.index(
                "function flashActionFailed"
            )
        ]
        self.assertNotIn(".length", toggle_block)


@override_settings(DEMO_MODE=False)
class RescheduleKeepsTheCachedProjectsTest(TestCase):
    """#199, second half. The projects survive a new date: re-sort,
    re-annotate, write back, so the Notion read goes. So does the summary —
    a new date renumbers the task_refs it holds
    (_number_projects_and_tasks, ai.py), and the positions are rewritten to
    follow the move instead of the summary being dropped. Dropping it made
    every single move pay for a fresh Claude call on the next render."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_date(self, task_id, new_date, count=1):
        with (
            patch("projects.views.update_task_date"),
            patch("projects.views.increment_postpone_count", return_value=count),
        ):
            return self.client.post(
                reverse("reschedule_task", args=[task_id]),
                data=json.dumps({"date": new_date.isoformat()}),
                content_type="application/json",
            )

    def test_the_projects_survive_with_the_new_date(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today + timedelta(days=1))])
        self.post_date("task-1", today + timedelta(days=10))
        self.assertIsNotNone(cache.get(CACHE_KEY))
        self.assertEqual(
            _cached_task_by_id(CACHE_KEY, "task-1")["due"], today + timedelta(days=10)
        )

    def test_the_cached_tasks_are_re_sorted(self):
        # The order is what the summary's task_refs are numbered against and
        # what every task list renders in — a moved task that keeps its old
        # position is a list that is no longer chronological.
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("first", today + timedelta(days=1)),
                _cached_task("second", today + timedelta(days=5)),
            ]
        )
        self.post_date("first", today + timedelta(days=10))
        self.assertEqual(
            [t["id"] for t in cache.get(CACHE_KEY)[0][0]["tasks"]], ["second", "first"]
        )

    def test_the_derived_fields_are_re_derived(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today - timedelta(days=1))])
        self.assertEqual(_cached_task_by_id(CACHE_KEY, "task-1")["urgency"], "overdue")
        self.post_date("task-1", today + timedelta(days=90))
        task = _cached_task_by_id(CACHE_KEY, "task-1")
        self.assertEqual(task["urgency"], "ok")
        self.assertEqual(task["kanban_column"], "open")

    def test_the_summary_survives_the_move(self):
        today = date.today()
        _warm_dashboard_cache(
            [_cached_task("task-1", today + timedelta(days=1))],
            summary=_summary_data(),
        )
        self.post_date("task-1", today + timedelta(days=10))
        projects, summary_data = cache.get(CACHE_KEY)
        self.assertTrue(projects)
        self.assertEqual(summary_data, _summary_data())

    def test_the_task_refs_follow_the_task_that_moved(self):
        # "first" moves past "second", so the position that meant "first"
        # is 2 afterwards — the summary text is unchanged and still points
        # at the task it was written about.
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("first", today + timedelta(days=1)),
                _cached_task("second", today + timedelta(days=5)),
            ],
            summary=_summary_with_refs([1]),
        )
        self.post_date("first", today + timedelta(days=10))
        self.assertEqual(_cached_task_refs(CACHE_KEY), [2])

    def test_a_move_that_reorders_nothing_leaves_the_refs_alone(self):
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("first", today + timedelta(days=1)),
                _cached_task("second", today + timedelta(days=5)),
            ],
            summary=_summary_with_refs([1, 2]),
        )
        self.post_date("first", today + timedelta(days=2))
        self.assertEqual(_cached_task_refs(CACHE_KEY), [1, 2])

    def test_the_next_dashboard_load_makes_no_claude_call(self):
        # The point of the whole exercise: a move used to leave the cache
        # without a summary, and the next render blocked on regenerating it.
        today = date.today()
        _warm_dashboard_cache(
            [_cached_task("task-1", today + timedelta(days=1))],
            summary=_summary_data(),
        )
        self.post_date("task-1", today + timedelta(days=10))
        with (
            patch("projects.views.generate_weekly_summary") as claude,
            patch("projects.views.get_upcoming_projects") as fetch,
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        claude.assert_not_called()
        fetch.assert_not_called()

    def test_the_stale_copy_is_patched_too(self):
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("first", today + timedelta(days=1)),
                _cached_task("second", today + timedelta(days=5)),
            ],
            summary=_summary_with_refs([1]),
        )
        self.post_date("first", today + timedelta(days=10))
        self.assertEqual(
            _cached_task_by_id(STALE_CACHE_KEY, "first")["due"],
            today + timedelta(days=10),
        )
        # The last-known-good entry is what a Notion outage renders from, so
        # its summary is renumbered in step rather than dropped.
        self.assertEqual(_cached_task_refs(STALE_CACHE_KEY), [2])

    def test_the_new_postpone_count_reaches_the_cache(self):
        # Written after the counter call confirms it, never optimistically:
        # a failure there returns a 502 and must not leave the cache
        # claiming a move Notion never counted.
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today + timedelta(days=1))])
        self.post_date("task-1", today + timedelta(days=10), count=3)
        self.assertEqual(_cached_task_by_id(CACHE_KEY, "task-1")["postpone_count"], 3)

    def test_a_failed_counter_call_leaves_the_confirmed_date_in_place(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today + timedelta(days=1))])
        with (
            patch("projects.views.update_task_date"),
            patch(
                "projects.views.increment_postpone_count",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            response = self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data=json.dumps({"date": (today + timedelta(days=10)).isoformat()}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            _cached_task_by_id(CACHE_KEY, "task-1")["due"], today + timedelta(days=10)
        )
        self.assertEqual(_cached_task_by_id(CACHE_KEY, "task-1")["postpone_count"], 0)

    def test_a_task_in_no_cached_list_falls_back_to_a_full_bust(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())])
        self.post_date("task-99", date.today() + timedelta(days=10))
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))


class RemapSummaryRefsTest(TestCase):
    """_remap_summary_refs on its own: what it does with the raw dict Claude
    returned, which the cache stores unvalidated (#122). Judging the shape of
    a ref belongs to resolve_weekly_summary (ai.py) and lives there alone, so
    anything this cannot translate is passed through for the resolver to
    drop."""

    BEFORE = ["a", "b", "c"]
    AFTER = ["c", "a", "b"]

    def remapped(self, refs, before=None, after=None):
        data = _remap_summary_refs(
            _summary_with_refs(refs), before or self.BEFORE, after or self.AFTER
        )
        return data["jetzt_faellig"][0]["task_refs"]

    def test_a_position_follows_its_task(self):
        self.assertEqual(self.remapped([1, 2, 3]), [2, 3, 1])

    def test_an_unchanged_order_returns_the_summary_unchanged(self):
        summary = _summary_with_refs([1, 2])
        self.assertIs(_remap_summary_refs(summary, self.BEFORE, self.BEFORE), summary)

    def test_a_ref_whose_task_is_gone_is_dropped(self):
        # Not something a reschedule produces — it moves a task, it does not
        # remove one — but the resolver drops an unresolvable ref rather than
        # rendering it, and this agrees with it instead of keeping a position
        # that now means a different task.
        self.assertEqual(self.remapped([1, 2], after=["b"]), [1])

    def test_an_out_of_range_ref_is_left_for_the_resolver(self):
        self.assertEqual(self.remapped([99]), [99])

    def test_a_ref_that_is_not_a_number_is_left_for_the_resolver(self):
        self.assertEqual(self.remapped(["1"]), ["1"])

    def test_true_is_not_treated_as_position_one(self):
        # bool is an int subclass; _resolve_ref (ai.py) excludes it for the
        # same reason.
        self.assertEqual(self.remapped([True]), [True])

    def test_a_summary_that_is_not_a_dict_is_returned_as_it_is(self):
        for summary in (None, "<p>alt</p>", []):
            with self.subTest(summary=summary):
                self.assertEqual(
                    _remap_summary_refs(summary, self.BEFORE, self.AFTER), summary
                )

    def test_a_block_without_task_refs_survives_untouched(self):
        data = _remap_summary_refs(
            {"jetzt_faellig": [{"project_ref": 1, "assessment": "ohne refs"}]},
            self.BEFORE,
            self.AFTER,
        )
        self.assertEqual(
            data["jetzt_faellig"], [{"project_ref": 1, "assessment": "ohne refs"}]
        )

    def test_the_project_ref_is_left_alone(self):
        # Only the tasks are renumbered: _annotate_tasks re-sorts inside each
        # project and never reorders the project list itself.
        data = _remap_summary_refs(_summary_with_refs([1]), self.BEFORE, self.AFTER)
        self.assertEqual(data["jetzt_faellig"][0]["project_ref"], 1)


@override_settings(DEMO_MODE=False)
class RescheduleAnswersTheRecomputedFiguresTest(TestCase):
    """#216: the day columns bucket by date, so every reschedule changes
    them — including one that leaves the stage alone, which is the only path
    that does not reload. The answer carries the same figures a toggle's
    does, from the same helper, so the two writes cannot disagree."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_date(self, task_id, new_date, week_start=None, count=1):
        body = {"date": new_date.isoformat()}
        if week_start is not None:
            body["week_start"] = week_start.isoformat()
        with (
            patch("projects.views.update_task_date"),
            patch("projects.views.increment_postpone_count", return_value=count),
        ):
            return self.client.post(
                reverse("reschedule_task", args=[task_id]),
                data=json.dumps(body),
                content_type="application/json",
            )

    def test_the_day_counters_follow_the_move(self):
        # The move a stage change never catches: within one week, so the row
        # keeps its stage while the card belongs under a different day.
        monday = iso_week_bounds(date.today())[0]
        _warm_dashboard_cache([_cached_task("task-1", monday + timedelta(days=1))])
        data = self.post_date(
            "task-1", monday + timedelta(days=3), week_start=monday
        ).json()
        self.assertEqual(
            data["days"][(monday + timedelta(days=1)).isoformat()],
            {"done": 0, "total": 0},
        )
        self.assertEqual(
            data["days"][(monday + timedelta(days=3)).isoformat()],
            {"done": 0, "total": 1},
        )

    def test_the_week_bar_and_the_board_counts_come_back_too(self):
        monday = iso_week_bounds(date.today())[0]
        _warm_dashboard_cache([_cached_task("task-1", monday + timedelta(days=1))])
        data = self.post_date("task-1", monday + timedelta(days=2)).json()
        self.assertEqual(data["week"], {"done": 0, "total": 1, "pct": 0})
        self.assertEqual(sum(data["kanban"].values()), 1)
        self.assertEqual(data["project"]["id"], "p1")

    def test_a_move_out_of_the_current_week_empties_the_bar(self):
        # The denominator, not the numerator: nothing was completed, the
        # task simply left the range the bar counts.
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        data = self.post_date("task-1", today + timedelta(days=90)).json()
        self.assertEqual(data["week"], {"done": 0, "total": 0, "pct": 0})

    def test_the_browsed_week_is_the_one_the_client_is_showing(self):
        today = date.today()
        next_monday = iso_week_bounds(today)[0] + timedelta(days=7)
        _warm_dashboard_cache([_cached_task("task-1", today)])
        data = self.post_date("task-1", next_monday, week_start=next_monday).json()
        self.assertEqual(data["days"][next_monday.isoformat()], {"done": 0, "total": 1})

    def test_a_cold_cache_answers_without_figures(self):
        # Same contract as the toggle: no figures means the server had
        # nothing to derive from, and the client reloads.
        data = self.post_date("task-1", date.today() + timedelta(days=1)).json()
        self.assertEqual(data["ok"], True)
        self.assertNotIn("week", data)
        self.assertNotIn("days", data)

    def test_the_load_path_and_the_write_path_call_the_same_helper(self):
        # Two implementations of these counts is what #210 was.
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today)])
        with patch(
            "projects.views._derive_dashboard_figures",
            side_effect=_derive_dashboard_figures,
        ) as derive:
            self.post_date("task-1", today + timedelta(days=1))
        derive.assert_called_once()


class RescheduleFiguresFromTheSessionPlanTest(DemoModeTestCase):
    """The demo half of the same answer — a visitor's own plan renders the
    same day columns and the same bar."""

    def test_the_figures_come_back_for_a_session_plan_too(self):
        monday = iso_week_bounds(date.today())[0]
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Nah",
                    "date": (monday + timedelta(days=1)).isoformat(),
                    "done": False,
                }
            ]
        )
        response = self.client.post(
            reverse("reschedule_task", args=["demo-session-0"]),
            data=json.dumps(
                {
                    "date": (monday + timedelta(days=3)).isoformat(),
                    "week_start": monday.isoformat(),
                }
            ),
            content_type="application/json",
        )
        data = response.json()
        self.assertEqual(
            data["days"][(monday + timedelta(days=1)).isoformat()],
            {"done": 0, "total": 0},
        )
        self.assertEqual(
            data["days"][(monday + timedelta(days=3)).isoformat()],
            {"done": 0, "total": 1},
        )
        # #183: a demo session's bar counts the whole plan, not the week.
        self.assertEqual(data["week"], {"done": 0, "total": 1, "pct": 0})


@override_settings(DEMO_MODE=False)
class DashboardRegeneratesADroppedSummaryTest(TestCase):
    """CACHE_KEY holds (projects, summary_data) as one tuple, so
    "invalidate only the summary" means writing (patched_projects, None). A
    hit in that shape now means "projects are good, regenerate the summary"
    — otherwise the card would read "KI nicht verfügbar" until the TTL ran
    out, which is not what a reschedule should cost."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_hit_without_a_summary_regenerates_it_and_writes_it_back(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())], summary=None)
        with (
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ) as generate,
            patch("projects.views.get_upcoming_projects") as fetch,
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        generate.assert_called_once()
        # The point of the whole exercise: no Notion round trip.
        fetch.assert_not_called()
        self.assertEqual(cache.get(CACHE_KEY)[1], _summary_data())
        self.assertEqual(cache.get(STALE_CACHE_KEY)[1], _summary_data())

    def test_an_unavailable_claude_leaves_the_entry_summaryless_for_a_retry(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())], summary=None)
        with (
            patch(
                "projects.views.generate_weekly_summary",
                side_effect=AIUnavailableError("boom"),
            ),
            patch("projects.views.get_upcoming_projects"),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Die KI-Wochenübersicht ist gerade nicht")
        self.assertIsNone(cache.get(CACHE_KEY)[1])


@override_settings(DEMO_MODE=False)
class RegeneratingASummaryDoesNotUndoAConcurrentWriteTest(TestCase):
    """#216: generate_weekly_summary takes seconds, and the branch above used
    to write back the projects it had read *before* that call. A write
    confirmed in Notion inside that window was discarded by the write-back,
    leaving the cache serving a task as open that Notion has as done — for
    the rest of the entry's life, which #199 no longer bounds tightly.

    The window is simulated rather than threaded: the second request runs
    inside the stubbed Claude call, which is exactly where it would land."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def load_dashboard_while(self, concurrent_write):
        def generate(*args, **kwargs):
            concurrent_write()
            return _summary_data()

        with (
            patch("projects.views.generate_weekly_summary", side_effect=generate),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch("projects.views.get_upcoming_projects") as fetch,
        ):
            response = self.client.get(reverse("dashboard"))
        # Still the point of the branch: no Notion round trip for the projects.
        fetch.assert_not_called()
        return response

    def toggle(self, task_id="task-1"):
        with patch("projects.views.toggle_task"):
            Client().post(
                reverse("toggle_task", args=[task_id]),
                data=json.dumps({"done": True}),
                content_type="application/json",
            )

    def reschedule(self, task_id, new_date):
        with (
            patch("projects.views.update_task_date"),
            patch("projects.views.increment_postpone_count", return_value=1),
        ):
            Client().post(
                reverse("reschedule_task", args=[task_id]),
                data=json.dumps({"date": new_date.isoformat()}),
                content_type="application/json",
            )

    def test_a_toggle_during_the_claude_call_survives_the_write_back(self):
        _warm_dashboard_cache([_cached_task("task-1", date.today())], summary=None)
        self.load_dashboard_while(self.toggle)
        # Both halves land: the confirmed write is still in the cache, and
        # the summary the call paid for was attached to it rather than to
        # the snapshot the load opened with.
        self.assertTrue(_cached_task_by_id(CACHE_KEY, "task-1")["done"])
        self.assertEqual(cache.get(CACHE_KEY)[1], _summary_data())
        self.assertTrue(_cached_task_by_id(STALE_CACHE_KEY, "task-1")["done"])

    def test_a_reschedule_during_the_call_drops_the_summary_it_renumbered(self):
        # task_refs are positions in the chronological order and a new date
        # moves the task, so attaching this summary would point them at the
        # wrong tasks — in range, and therefore rendered rather than dropped
        # by resolve_weekly_summary. Dropping it costs one more Claude call
        # on the next load; keeping it renders the wrong checkbox.
        today = date.today()
        _warm_dashboard_cache(
            [
                _cached_task("task-1", today),
                _cached_task("task-2", today + timedelta(days=2)),
            ],
            summary=None,
        )
        self.load_dashboard_while(
            lambda: self.reschedule("task-1", today + timedelta(days=5))
        )
        projects, summary = cache.get(CACHE_KEY)
        self.assertEqual([t["id"] for t in projects[0]["tasks"]], ["task-2", "task-1"])
        self.assertIsNone(summary)

    def test_a_bust_during_the_call_is_not_undone(self):
        # Writing the entry back would restore exactly the state the bust
        # discarded — a resurrection, not a cache fill.
        _warm_dashboard_cache([_cached_task("task-1", date.today())], summary=None)
        self.load_dashboard_while(_bust_dashboard_cache)
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))


class RescheduleResortsTheRowTest(DemoModeTestCase):
    """#194, client half: the server sorts every task list chronologically
    (#140), so a moved row that keeps its old position leaves the client's
    copy disagreeing with what a render would produce."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def reschedule_block(self, html):
        return html[
            html.index("async function reschedule(") : html.index(
                "document.querySelectorAll('.task-due[data-task-id]')"
            )
        ]

    def test_a_sort_function_exists_and_reads_the_raw_date(self):
        html = self.dashboard_html()
        self.assertIn("function sortRows(row, movedDate) {", html)
        self.assertIn("el.querySelector('.task-due')?.dataset.rawDate", html)

    def test_it_runs_after_a_successful_reschedule(self):
        self.assertIn("sortRows(row, newDate);", self.dashboard_html())

    def test_undated_rows_sort_last(self):
        self.assertIn("if (!da) return da === db ? 0 : 1;", self.dashboard_html())

    def test_a_stage_change_reloads_instead_of_re_sorting(self):
        html = self.dashboard_html()
        self.assertIn("if (stageBefore && stageBefore !== data.urgency) {", html)
        self.assertIn("window.location.reload();", self.reschedule_block(html))

    def test_the_stage_is_read_before_the_row_is_reclassified(self):
        # reclassify() overwrites the very class this compares against.
        html = self.reschedule_block(self.dashboard_html())
        self.assertLess(
            html.index("const stageBefore ="),
            html.index("reclassify(dot, data.urgency)"),
        )

    def test_the_moved_rows_own_date_is_passed_in_not_read_back(self):
        # The date picker holds the span out of the DOM while the request
        # runs, so reading the new date off it would find nothing and sort
        # the moved row last.
        self.assertIn(
            "const dateOf = el => el === row ? (movedDate || '') :",
            self.dashboard_html(),
        )

    def test_the_reload_rule_is_written_down_in_the_source(self):
        html = self.dashboard_html()
        self.assertIn("Same stage → re-sort in place. Stage changed → reload.", html)

    def test_no_date_arithmetic_is_reimplemented_here(self):
        # The stage comes from the server (#169: calendar-week based, and in
        # a demo session measured against the simulated date). Anything
        # parsing dates in this block would be a second implementation of it.
        block = self.reschedule_block(self.dashboard_html())
        self.assertNotIn("new Date(", block)
        self.assertNotIn("getDay(", block)


class RescheduleAnswersBothDateFormsTest(DemoModeTestCase):
    """#238: the row's date was shortened, the Kanban card's was not — and
    the client writes this one answer into both elements. A single
    due_display would have put the long month back into every rescheduled
    row until the next reload."""

    def test_the_answer_carries_the_row_form_beside_the_long_one(self):
        self.given_session_plan()
        new_date = date.today() + timedelta(days=14)
        response = self.client.post(
            reverse("reschedule_task", args=["demo-session-0"]),
            data=json.dumps({"date": new_date.isoformat()}),
            content_type="application/json",
        )
        answer = response.json()
        self.assertEqual(answer["due_display"], format_date(new_date, role="long"))
        self.assertEqual(answer["due_display_row"], format_date(new_date, role="row"))

    def test_the_row_takes_the_short_form_and_the_board_the_long_one(self):
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("dueSpan.textContent = data.due_display_row;", html)
        self.assertIn("if (due) due.textContent = data.due_display;", html)


class RescheduleUpdatesTheDayColumnsTest(DemoModeTestCase):
    """#216: a day column's membership is its date, so every reschedule
    changes it — including the same-stage move, the only path that does not
    reload. The row read Thursday while its card stayed under Wednesday and
    both counters kept their old numbers."""

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def figures_block(self, html):
        return html[
            html.index("function applyRescheduleFigures") : html.index(
                "function flashActionFailed"
            )
        ]

    def test_the_same_stage_path_applies_the_figures(self):
        self.assertIn(
            "} else if (applyRescheduleFigures(taskId, newDate, data)) {",
            self.dashboard_html(),
        )

    def test_the_card_moves_into_the_column_of_its_new_date(self):
        block = self.figures_block(self.dashboard_html())
        self.assertIn(
            'document.querySelector(`.day-column-body[data-date="${newDate}"]`)',
            block,
        )
        self.assertIn(
            "if (dayCard && targetColumn) targetColumn.appendChild(dayCard);", block
        )

    def test_a_move_out_of_the_browsed_week_removes_the_card(self):
        self.assertIn(
            "else if (dayCard) dayCard.remove();",
            self.figures_block(self.dashboard_html()),
        )

    def test_a_card_that_would_have_to_be_created_reloads_instead(self):
        # Markup belongs in the template. A task moved into the week the
        # columns show has no card here to move, so the server renders it.
        block = self.figures_block(self.dashboard_html())
        self.assertIn("if (!dayCard && targetColumn) return false;", block)
        self.assertIn("window.location.reload();", self.dashboard_html())

    def test_the_kanban_cards_date_label_is_rewritten(self):
        # The board spells the date out, unlike the day card, whose column
        # *is* its date.
        block = self.figures_block(self.dashboard_html())
        self.assertIn("if (due) due.textContent = data.due_display;", block)

    def test_every_board_column_renders_a_hook_for_that_label(self):
        # Without its own element the badge beside it would be wiped along
        # with the date. Asserted against the template, not one render: all
        # three columns spell the date out, and only one of them holds the
        # moved card.
        template = (
            settings.BASE_DIR / "projects/templates/projects/dashboard.html"
        ).read_text()
        self.assertEqual(template.count('<span class="kanban-card-due">'), 3)
        self.assertIn('<span class="kanban-card-due">', self.dashboard_html())

    def test_no_count_is_derived_in_this_block_either(self):
        block = self.figures_block(self.dashboard_html())
        self.assertNotIn(".length", block)
        self.assertNotIn("new Date(", block)

    def test_both_writes_share_one_figure_writer(self):
        # A second copy of "write the numbers you were handed" is the drift
        # #210 exists to prevent.
        html = self.dashboard_html()
        self.assertEqual(html.count("function applyFigures(data) {"), 1)
        self.assertIn("if (!applyFigures(data)) return false;", html)
