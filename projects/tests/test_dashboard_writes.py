"""The dashboard write paths: toggle, reschedule and rename, what they
persist, what they answer and what they leave in the cache."""

import json
import re
from datetime import (
    date,
    timedelta,
)
from pathlib import Path
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
from ..templatetags.planner_tags import date_names
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
    _build_session_project,
    _bust_dashboard_cache,
    _cache_fresh_read,
    _demo_completed_in_range,
    _derive_dashboard_figures,
    _remap_summary_refs,
)
from .base import (
    DemoModeTestCase,
    _fake_upcoming_project,
    _fake_upcoming_project_with_task,
    _summary_data,
)


def _iso_in(days):
    return (date.today() + timedelta(days=days)).isoformat()


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

    def test_a_non_canonical_iso_date_reaches_notion_canonically(self):
        # Notion's date property takes YYYY-MM-DD, and "20260905" passes
        # date.fromisoformat — so validating is not the same as normalising.
        with (
            patch("projects.views.update_task_date") as mock_update,
            patch("projects.views.increment_postpone_count", return_value=1),
        ):
            self.client.post(
                reverse("reschedule_task", args=["task-1"]),
                data='{"date": "20260905"}',
                content_type="application/json",
            )
        mock_update.assert_called_once_with("task-1", "2026-09-05")

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
                # #266: the close-out triage list's move button is relabelled
                # from this, so the German date stays server-side.
                "next_week_display": format_date(
                    self.NEW_DATE + timedelta(days=7), role="long"
                ),
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
        # The revert path survives behind the widened guard. #211 part 2
        # added its second argument: the revert restores the green the dot
        # had before the click, not the green the click would have implied.
        self.assertContains(response, "applyDone(taskId, currentDone, wasThisWeek);")
        self.assertContains(response, "flashActionFailed(btn);")

    def test_dashboard_toggle_and_reschedule_catch(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        # All nine handlers — the toggle listener, reschedule(), #180's
        # day-column drag handler, #239's rename and trash, #284's project
        # trash, #283's project reschedule, #233's setSimDate and #156's
        # loadSummary — carry the widened guard; their error paths (flash /
        # return false / revert the drag / leave the confirmation bar
        # standing / take the Zeitreise paint back / show the summary's own
        # unavailable state) stay. The count is what makes a new handler say
        # so.
        self.assertContains(response, self.GUARD, count=9)
        self.assertContains(response, "flashActionFailed(dueSpan);")
        self.assertContains(response, "flashActionFailed(nameSpan);")

    PARSE_GUARD = "    try {\n        return await response.json();\n    } catch {"

    def test_a_body_that_is_not_json_is_a_failed_write_too(self):
        """Found reviewing #233: the guard above covers a rejected fetch and
        an error response, and then both reschedule helpers handed
        `response.json()` on as a promise. A 200 whose body is not JSON makes
        it reject one step later, and two callers await it with no catch of
        their own — #239's "Heute" menu item on the dashboard and the +7
        button in the triage list. The rejection threw out of the handler:
        nothing flashed, and the triage button kept the `disabled` it had set
        itself."""
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(self.PARSE_GUARD, html)
        # The bare form is what threw. Asserted as its own line, so a helper
        # that goes back to returning the promise fails here. The triage
        # list's own copy is checked where its fixtures live
        # (TheTriageListReportsAFailedMoveTest, test_closeout.py).
        self.assertNotIn("\n    return response.json();\n", html)


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

    def test_a_toggle_records_when_it_happened(self):
        """#246: _count_done_in_range promises in its own docstring that
        "any task toggled through this app has `done` and `completed_date`
        set together". This route wrote only `done`, so the sentence was
        false for every toggle made on /mein-plan/."""
        self.given_session_plan()
        self.post_toggle("demo-session-0", done=True)
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][0]["completed_date"],
            timezone.localdate().isoformat(),
        )

    def test_unchecking_clears_the_completion_date(self):
        self.given_session_plan()
        self.post_toggle("demo-session-0", done=True)
        self.post_toggle("demo-session-0", done=False)
        self.assertIsNone(
            self.client.session["demo_plan"]["tasks"][0]["completed_date"]
        )

    def test_a_task_cleared_here_counts_in_the_week_closeout(self):
        """Where the missing field actually cost something.
        _demo_completed_in_range places a task in a week by its completion
        date, and falls back to the due date only under a moment. A task due
        outside this week, cleared on /mein-plan/ today, therefore never
        appeared in the close-out's "completed this week" at all."""
        week_start, week_end = iso_week_bounds(timezone.localdate())
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (week_end + timedelta(days=14)).isoformat(),
                    "done": False,
                }
            ]
        )
        self.post_toggle("demo-session-0", done=True)
        tasks = _build_session_project(self.client.session["demo_plan"])["tasks"]
        self.assertEqual(_demo_completed_in_range(tasks, week_start, week_end, None), 1)


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

    def test_a_non_canonical_iso_date_is_stored_canonically(self):
        # The same rule the add path follows (_parse_posted_task_date): a
        # date that passes date.fromisoformat is not yet a YYYY-MM-DD string,
        # and the stored one is what download_plan sorts on.
        self.given_session_plan()
        self.post_date("demo-session-0", '{"date": "20260905"}')
        self.assertEqual(self.stored_dates(), ["2026-09-05"])

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
        # The picker does dueEl.replaceWith(input) while it is open, so the
        # date element has no parent for the duration of the request and
        # closest() called on it inside reschedule() would find nothing — the
        # dot would silently keep its pre-move stage. Both call sites
        # therefore read the row while the element is still attached and pass
        # it in. (The local name is dueEl rather than span since #200 made it
        # a button; what this asserts is the reading, not the name.)
        #
        # #266: the reading moved into the shared module with the picker, and
        # it is what the row selector is a parameter for — the triage list's
        # row is not a .task-row, and the summary has none at all.
        self.assertIn(
            "async function reschedule(taskId, newDate, dueSpan, row) {",
            self.dashboard_html(),
        )
        picker = (
            Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"
        ).read_text()
        self.assertIn("const row = dueEl.closest(rowSelector);", picker)
        self.assertIn("rowSelector = '.task-row'", picker)
        # The module hands both to the callback, which is what makes the
        # reading survive the swap regardless of which surface reacts to it.
        # #279: the swap itself moved into openTaskDatePicker(), so it is the
        # binding that closes over the row and the id. The reading is
        # unchanged and still happens before the swap.
        self.assertIn(
            "openTaskDatePicker(dueEl, iso => "
            "onPick(dueEl.dataset.taskId, iso, dueEl, row));",
            picker,
        )
        # #239 moved the second call site into the actions menu, which reads
        # the row off the clicked item rather than off a button in the row.
        html = self.dashboard_html()
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
        # The one-click reschedule used daily is not lost to the menu. #266
        # moved the listener into the shared module, so the page's half of
        # that is the binding call.
        self.assertIn(
            "bindTaskDatePickers(reschedule, {exclude: '.ai-card'});",
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
        # One per rendered overdue row, wherever the row renders. Counted on
        # the rendered button, not on the bare attribute: the JS below
        # carries the same selector to remove the item when a move lifts the
        # row out of overdue.
        #
        # The equality is the rule; how many rows carry it is a question for
        # whichever lists the page happens to render, and #240 changed that
        # number by hiding the Heute view for a session plan — it will change
        # again when that view returns. The lower bound is what keeps the
        # equality from passing at nothing at all.
        self.assertGreater(html.count(">→ heute</button>"), 0)
        self.assertEqual(
            html.count(">→ heute</button>"), html.count('class="dot overdue "')
        )

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

    def test_leaving_the_field_commits_rather_than_discards(self):
        """The date input this is shaped after commits on `change`, which
        fires before the blur — so tapping away from a date keeps it. The
        name discarded instead, silently, and two controls that swap into
        the same row must not answer the same gesture differently. On a
        touch screen tapping outside the field is a normal way to finish,
        and a discard with no feedback is the one outcome the row cannot
        show. Escape stays the way to cancel: it settles the input, so the
        blur behind it is a no-op."""
        html = self.dashboard_html()
        self.assertIn("input.addEventListener('blur', commit);", html)
        self.assertNotIn("input.addEventListener('blur', restore);", html)

    def test_the_input_leaves_the_dom_exactly_once(self):
        # Both Enter and blur go through commit(), so whichever runs second
        # — including the blur that Enter's own swap fires — finds the input
        # already settled and neither swaps twice nor posts twice.
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
            self.client.post(reverse("summary_fragment"))
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


@override_settings(DEMO_MODE=False)
class TrashProjectProductionTest(TestCase):
    """#284: the project level's first write beyond create_project.

    The tasks go with it, and that is not a convenience — a task whose
    project page is in the trash still points at that page, so
    get_unassigned_tasks (relation.is_empty, #53) does not find it either.
    Left behind it would be invisible in the app and alive in Notion."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def warm_cache(self, project=None, with_fallback=False):
        project = project or _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
            if with_fallback:
                # STALE_CACHE_KEY is written when the summary arrives, not by
                # the read itself (#156) — so a test that cares about the
                # last-known-good copy has to let the summary land first.
                self.client.post(reverse("summary_fragment"))
        return project

    def post_trash(self, project_id="p1"):
        return self.client.post(
            reverse("trash_project", args=[project_id]),
            data=json.dumps({}),
            content_type="application/json",
        )

    def test_the_project_and_its_tasks_reach_notion(self):
        self.warm_cache()
        with (
            patch(
                "projects.views.get_exclusive_task_ids", return_value=["task-1"]
            ) as mock_read,
            patch("projects.views.trash_task") as mock_task,
            patch("projects.views.trash_project") as mock_project,
        ):
            response = self.post_trash()
        mock_read.assert_called_once_with("p1")
        mock_task.assert_called_once_with("task-1")
        mock_project.assert_called_once_with("p1")
        self.assertEqual(response.json(), {"ok": True})

    def test_the_task_list_comes_from_notion_rather_than_the_cache(self):
        # The cached entry lives up to CACHE_TTL (eight hours), so a task
        # created in Notion's own UI since it was read is not in it. Archiving
        # the project around such a task would leave exactly the orphan this
        # write exists to prevent: pointing at a trashed page, so invisible to
        # get_upcoming_projects, and not relation.is_empty, so invisible to
        # get_unassigned_tasks (#53) either.
        self.warm_cache()  # one task, "task-1"
        with (
            patch(
                "projects.views.get_exclusive_task_ids",
                return_value=["task-1", "task-added-in-notion"],
            ),
            patch("projects.views.trash_task") as mock_task,
            patch("projects.views.trash_project"),
        ):
            self.post_trash()
        self.assertEqual(
            [call.args[0] for call in mock_task.call_args_list],
            ["task-1", "task-added-in-notion"],
        )

    def test_the_tasks_are_trashed_before_the_project(self):
        # The order is the whole failure contract: a project archived first
        # would leave its tasks reachable by no read at all, while tasks
        # first leaves the project on the dashboard with fewer rows under
        # it — visible, and repeatable.
        self.warm_cache()
        calls = []
        with (
            patch("projects.views.get_exclusive_task_ids", return_value=["task-1"]),
            patch(
                "projects.views.trash_task",
                side_effect=lambda task_id: calls.append(("task", task_id)),
            ),
            patch(
                "projects.views.trash_project",
                side_effect=lambda project_id: calls.append(("project", project_id)),
            ),
        ):
            self.post_trash()
        self.assertEqual(calls, [("task", "task-1"), ("project", "p1")])

    def test_an_unknown_project_is_a_404_and_reads_nothing(self):
        self.warm_cache()
        with (
            patch("projects.views.get_exclusive_task_ids") as mock_read,
            patch("projects.views.trash_task") as mock_task,
            patch("projects.views.trash_project") as mock_project,
        ):
            response = self.post_trash("does-not-exist")
        self.assertEqual(response.status_code, 404)
        mock_read.assert_not_called()
        mock_task.assert_not_called()
        mock_project.assert_not_called()

    def test_a_cold_cache_is_a_404_rather_than_a_guess(self):
        # Archiving a page the app cannot currently see is the one thing
        # this endpoint must not do on a guess — the next load is right.
        with (
            patch("projects.views.get_exclusive_task_ids") as mock_read,
            patch("projects.views.trash_task") as mock_task,
            patch("projects.views.trash_project") as mock_project,
        ):
            response = self.post_trash()
        self.assertEqual(response.status_code, 404)
        mock_read.assert_not_called()
        mock_task.assert_not_called()
        mock_project.assert_not_called()

    def test_a_failed_task_read_writes_nothing_and_keeps_the_cache(self):
        # The one failure on this path that costs the cache nothing: no page
        # was touched, so the entry this request read is still true — and
        # dropping it would force the next load into a read that fails too.
        self.warm_cache()
        with (
            patch(
                "projects.views.get_exclusive_task_ids",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.trash_task") as mock_task,
            patch("projects.views.trash_project") as mock_project,
        ):
            self.assertEqual(self.post_trash().status_code, 502)
        mock_task.assert_not_called()
        mock_project.assert_not_called()
        self.assertIsNotNone(cache.get(CACHE_KEY))

    def test_a_partial_failure_drops_the_fresh_entry_and_keeps_the_fallback(self):
        # The fresh entry has to go: tasks before the failure are already
        # trashed and it still lists them. The stale pair must not, and that
        # is the difference — this path is reached *because* Notion is
        # unreachable, which is the one situation dashboard() renders from the
        # last-known-good copy. Dropping it would answer a half-finished
        # removal with an empty page (data_unavailable) that cannot even be
        # refreshed into a true one.
        self.warm_cache(with_fallback=True)
        with (
            patch("projects.views.get_exclusive_task_ids", return_value=["task-1"]),
            patch(
                "projects.views.trash_task",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.trash_project"),
        ):
            self.assertEqual(self.post_trash().status_code, 502)
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(UNASSIGNED_CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_UNASSIGNED_CACHE_KEY))

    def test_a_confirmed_removal_busts_every_cached_copy(self):
        self.warm_cache(with_fallback=True)
        with (
            patch("projects.views.get_exclusive_task_ids", return_value=["task-1"]),
            patch("projects.views.trash_task"),
            patch("projects.views.trash_project"),
        ):
            self.post_trash()
        for key in (
            CACHE_KEY,
            STALE_CACHE_KEY,
            UNASSIGNED_CACHE_KEY,
            STALE_UNASSIGNED_CACHE_KEY,
        ):
            with self.subTest(key=key):
                self.assertIsNone(cache.get(key))

    def test_get_is_not_a_removal(self):
        self.assertEqual(
            self.client.get(reverse("trash_project", args=["p1"])).status_code, 405
        )


class TrashProjectIsProductionOnlyTest(DemoModeTestCase):
    """A demo visitor's two kinds of project have no Notion page to archive:
    the example projects are in no session (#10 §5), and a session plan is
    the sitting itself rather than one project among several. The endpoint
    refuses and the menu is not rendered — the same rule on both sides, so a
    click never has to be interpreted."""

    def test_the_endpoint_refuses(self):
        response = self.client.post(
            reverse("trash_project", args=["demo-1"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_the_menu_is_not_offered(self):
        self.assertNotContains(
            self.client.get(reverse("dashboard")), 'data-action="trash-project"'
        )


@override_settings(DEMO_MODE=False)
class TrashProjectMenuTest(TestCase):
    """The control, in the menu shape #239 gave the task row — same classes,
    so the open/close, the keyboard handling and the two-click arming are
    inherited rather than written a second time."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def dashboard(self, with_fallback=False):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            page = self.client.get(reverse("dashboard"))
            if with_fallback:
                # STALE_CACHE_KEY is written when the summary arrives, not by
                # the read itself (#156) — and inside this block, or the Claude
                # call would be a real one.
                self.client.post(reverse("summary_fragment"))
            return page

    def test_the_header_offers_the_action(self):
        self.assertContains(
            self.dashboard(),
            'data-action="trash-project" data-project-id="p1" data-task-count="1"',
        )

    def test_it_reuses_the_row_menu_markup(self):
        page = self.dashboard()
        self.assertContains(page, '<span class="task-menu project-menu">')
        self.assertContains(page, 'class="task-menu-item task-menu-item-danger"')

    def test_the_armed_label_is_built_from_the_task_count(self):
        # The count sits in the label before the click rather than in a
        # warning after it: this is the app's first write touching more than
        # one page, and how many is the part worth knowing.
        page = self.dashboard()
        self.assertContains(page, "function armedTrashLabel(item)")
        self.assertContains(page, "Projekt und ${tasks} in den Papierkorb?")

    def test_disarming_covers_both_trash_actions(self):
        # The selector has to reach data-action="trash-project" too, or a
        # reopened project menu starts one click from a removal.
        # Inside a <script>, so Django escapes nothing — the selector is in
        # the page exactly as it is written.
        self.assertContains(
            self.dashboard(), '.task-menu-item[data-action^="trash"]', html=False
        )

    def test_it_is_not_offered_on_a_stale_render(self):
        # A stale page is the page whose last Notion read failed, so the write
        # behind this control cannot land — and the endpoint reads CACHE_KEY,
        # which that page did not render from. The same rule as demo_mode,
        # from the other side: a control that is offered has to be able to
        # work.
        self.dashboard(with_fallback=True)  # the stale copy comes with it
        cache.delete(CACHE_KEY)
        with (
            patch(
                "projects.views.get_upcoming_projects",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
        ):
            page = self.client.get(reverse("dashboard"))
        self.assertTrue(page.context["stale"])
        self.assertContains(page, "Programm festlegen")  # the project is there
        self.assertNotContains(page, 'data-action="trash-project"')


class AddTaskDemoModeTest(DemoModeTestCase):
    """#148 in a demo session: the task lands in session['demo_plan'], the
    same place every other demo write does."""

    def post_add(self, project_id="session-plan", **body):
        payload = {"name": "Programmheft prüfen", "date": _iso_in(3)}
        payload.update(body)
        return self.client.post(
            reverse("add_task"),
            data=json.dumps({"project_id": project_id, **payload}),
            content_type="application/json",
        )

    def test_the_task_lands_in_the_session_plan(self):
        self.given_session_plan()
        self.assertEqual(self.post_add().json(), {"ok": True})
        names = [t["name"] for t in self.client.session["demo_plan"]["tasks"]]
        self.assertIn("Programmheft prüfen", names)

    def test_it_survives_the_next_request(self):
        self.given_session_plan()
        self.post_add()
        self.assertContains(self.client.get(reverse("my_plan")), "Programmheft prüfen")

    def test_it_is_stored_the_way_planner_create_stores_one(self):
        # Every consumer of demo_plan["tasks"] reads these keys —
        # _build_session_project the first three, the toggle "done".
        self.given_session_plan()
        self.post_add(date="2026-09-05")
        added = self.client.session["demo_plan"]["tasks"][-1]
        self.assertEqual(added["name"], "Programmheft prüfen")
        self.assertEqual(added["date"], "2026-09-05")
        self.assertIs(added["done"], False)
        self.assertTrue(added["id"])

    def test_the_new_id_does_not_reuse_a_trashed_one(self):
        # planner_create numbers by enumerate, so an id derived from
        # len(tasks) would collide with a live task after a trash — and
        # every write addresses a task by id.
        plan = self.given_session_plan()
        plan["tasks"].append(
            {
                "id": "demo-session-1",
                "name": "Plakate",
                "date": _iso_in(5),
                "done": False,
            }
        )
        session = self.client.session
        session["demo_plan"] = plan
        session.save()
        self.client.post(
            reverse("trash_task", args=["demo-session-0"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.post_add()
        ids = [t["id"] for t in self.client.session["demo_plan"]["tasks"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("demo-session-1", ids[1:])

    def test_the_cached_summaries_are_swept(self):
        # An added task is content the summary should have mentioned and
        # could not — unlike a reschedule, which only moves a task the
        # summary already knew about and is therefore remappable.
        self.given_session_plan()
        session = self.client.session
        session[f"{SUMMARY_KEY}_today"] = _summary_data()
        session["demo_plan_summary_v1_today"] = _summary_data()
        session.save()
        self.post_add()
        self.assertNotIn(f"{SUMMARY_KEY}_today", self.client.session)
        self.assertNotIn("demo_plan_summary_v1_today", self.client.session)

    def test_a_project_that_is_not_the_session_plan_is_a_404(self):
        # The demo example projects are in no session, so nothing there can
        # be written to (#10 §5).
        self.given_session_plan()
        self.assertEqual(self.post_add(project_id="demo-1").status_code, 404)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_an_add_during_a_moment_is_refused(self):
        # #217: a task dated before the moment would be forced done by the
        # simulated render the instant it appeared.
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        self.assertEqual(self.post_add().status_code, 404)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_a_get_is_a_405(self):
        self.given_session_plan()
        self.assertEqual(self.client.get(reverse("add_task")).status_code, 405)

    def test_a_malformed_body_is_a_400(self):
        self.given_session_plan()
        response = self.client.post(
            reverse("add_task"), data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_an_empty_name_is_a_400(self):
        self.given_session_plan()
        self.assertEqual(self.post_add(name="   ").status_code, 400)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_an_unparseable_date_is_a_400(self):
        self.given_session_plan()
        self.assertEqual(self.post_add(date="übermorgen").status_code, 400)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_a_missing_date_is_a_400(self):
        # Unlike the timelapse date, an absent one here clears nothing — a
        # dateless task would drop out of every list this app sorts by date.
        self.given_session_plan()
        self.assertEqual(self.post_add(date=None).status_code, 400)
        self.assertEqual(len(self.client.session["demo_plan"]["tasks"]), 1)

    def test_the_name_is_stripped(self):
        self.given_session_plan()
        self.post_add(name="  Programmheft prüfen  ")
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][-1]["name"], "Programmheft prüfen"
        )

    def test_a_non_canonical_iso_date_is_stored_canonically(self):
        # date.fromisoformat takes every ISO 8601 date form since 3.11, so
        # validating with it is not the same as having YYYY-MM-DD. The stored
        # string is what download_plan sorts on, and "-" < "0", so "20260905"
        # kept as it arrived would sort after every hyphenated date.
        self.given_session_plan()
        self.assertEqual(self.post_add(date="20260905").json(), {"ok": True})
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][-1]["date"], "2026-09-05"
        )

    def test_an_iso_week_date_is_stored_as_the_day_it_names(self):
        self.given_session_plan()
        self.post_add(date="2026-W36-5")
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][-1]["date"], "2026-09-04"
        )

    def test_an_id_outside_the_scheme_does_not_raise_the_counter(self):
        # _next_demo_task_id reads the number off "demo-session-N". An id that
        # never carried that prefix carries no number of this scheme either —
        # rpartition used to hand the whole string back as the suffix, so a
        # bare "7" was read as number 7 and the next add jumped to 8.
        plan = self.given_session_plan()
        plan["tasks"].append({"id": "7", "name": "Alt", "date": "", "done": False})
        session = self.client.session
        session["demo_plan"] = plan
        session.save()
        self.post_add()
        self.assertEqual(
            self.client.session["demo_plan"]["tasks"][-1]["id"], "demo-session-1"
        )


@override_settings(DEMO_MODE=False)
class AddTaskProductionTest(TestCase):
    """The production half. The cache is busted rather than patched, for the
    reason trash_task_view already wrote down and one more: the new Notion
    page id does not exist until the write returns, and _patch_cached_tasks
    has no insertion path at all."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_add(self, **body):
        payload = {
            "project_id": "p1",
            "name": "Programmheft prüfen",
            "date": _iso_in(3),
        }
        payload.update(body)
        return self.client.post(
            reverse("add_task"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def warm_the_cache(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            self.client.get(reverse("dashboard"))
        return project

    def test_the_write_reaches_notion(self):
        with patch("projects.views.create_task") as mock_create:
            response = self.post_add(date="2026-09-05")
        mock_create.assert_called_once_with("p1", "Programmheft prüfen", "2026-09-05")
        self.assertEqual(response.json(), {"ok": True})

    def test_a_confirmed_write_busts_every_cached_copy(self):
        self.warm_the_cache()
        with patch("projects.views.create_task"):
            self.post_add()
        for key in (
            CACHE_KEY,
            STALE_CACHE_KEY,
            UNASSIGNED_CACHE_KEY,
            STALE_UNASSIGNED_CACHE_KEY,
            CACHE_DEADLINE_KEY,
            UNASSIGNED_CACHE_DEADLINE_KEY,
        ):
            with self.subTest(key=key):
                self.assertIsNone(cache.get(key))

    def test_a_notion_failure_is_a_502_and_leaves_the_cache_alone(self):
        self.warm_the_cache()
        with patch(
            "projects.views.create_task", side_effect=NotionUnavailableError("boom")
        ):
            self.assertEqual(self.post_add().status_code, 502)
        self.assertIsNotNone(cache.get(CACHE_KEY))

    def test_the_task_is_on_the_next_render(self):
        project = self.warm_the_cache()
        with patch("projects.views.create_task"):
            self.post_add()
        project["tasks"].append(
            {
                "id": "task-2",
                "name": "Programmheft prüfen",
                "due": date.today() + timedelta(days=3),
                "done": False,
                "kontext": [],
            }
        )
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Programmheft prüfen")

    def test_a_get_is_a_405(self):
        self.assertEqual(self.client.get(reverse("add_task")).status_code, 405)

    def test_a_malformed_body_is_a_400_before_any_write(self):
        with patch("projects.views.create_task") as mock_create:
            response = self.client.post(
                reverse("add_task"), data="not json", content_type="application/json"
            )
        self.assertEqual(response.status_code, 400)
        mock_create.assert_not_called()

    def test_a_missing_project_is_a_400_before_any_write(self):
        with patch("projects.views.create_task") as mock_create:
            response = self.post_add(project_id="")
        self.assertEqual(response.status_code, 400)
        mock_create.assert_not_called()

    def test_an_empty_name_is_a_400_before_any_write(self):
        with patch("projects.views.create_task") as mock_create:
            response = self.post_add(name="   ")
        self.assertEqual(response.status_code, 400)
        mock_create.assert_not_called()

    def test_an_unparseable_date_is_a_400_before_any_write(self):
        with patch("projects.views.create_task") as mock_create:
            response = self.post_add(date="übermorgen")
        self.assertEqual(response.status_code, 400)
        mock_create.assert_not_called()

    def test_a_non_canonical_iso_date_reaches_notion_canonically(self):
        # Notion's date property takes YYYY-MM-DD; "20260905" passes
        # date.fromisoformat and would have been forwarded as it arrived.
        with patch("projects.views.create_task") as mock_create:
            self.post_add(date="20260905")
        mock_create.assert_called_once_with("p1", "Programmheft prüfen", "2026-09-05")


class AddRowIsOfferedOnlyWhereItPersistsTest(DemoModeTestCase):
    """#148's two refusals, in the markup rather than only in the endpoint.
    A row that answers 404 on every submit is an affordance that is not
    there — the same reasoning _task_due.html and _task_dot.html carry."""

    def dashboard_html(self, **params):
        return self.client.get(reverse("dashboard"), params).content.decode()

    def test_a_session_plan_gets_the_row_on_the_dashboard(self):
        self.given_session_plan()
        self.assertIn('class="task-add-row"', self.dashboard_html())

    def test_a_session_plan_gets_the_row_on_my_plan(self):
        self.given_session_plan()
        html = self.client.get(reverse("my_plan")).content.decode()
        self.assertIn('class="task-add-row"', html)

    def test_the_demo_example_projects_do_not_get_it(self):
        # viewing_demo_data: those projects are in no session (#10 §5).
        self.assertNotIn('class="task-add-row"', self.dashboard_html())

    def test_a_moment_takes_it_away(self):
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        self.assertNotIn('class="task-add-row"', self.dashboard_html())

    def test_my_plan_loses_it_under_a_moment_too(self):
        # #246: /mein-plan/ renders the real date, so the write the
        # dashboard refuses is not the one this page would make. The
        # endpoint decides per world, and this page is not the simulated
        # one — but the moment lives in the same session, so the refusal
        # would reach it. Pinned so the pair cannot drift apart silently.
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        self.client.post(
            reverse("set_timelapse_date"),
            data=json.dumps({"date": "2026-09-01"}),
            content_type="application/json",
        )
        html = self.client.get(reverse("my_plan")).content.decode()
        self.assertNotIn('class="task-add-row"', html)


class TheAddRowIsOneComponentTest(DemoModeTestCase):
    """#148 follows #266's and #233's cut, one write further on: the markup
    is one partial and the write is one module, so a surface is an include
    plus one call.

    The whole write, not only the asking — which is where this differs from
    the date picker deliberately. Each surface's *reschedule* means something
    different; an add means the same thing everywhere, because the endpoint
    busts the caches and answers no figures. Two identical fetches in two
    templates would be the duplication those two issues were about."""

    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"
    MODULE = Path(settings.BASE_DIR) / "projects/static/projects/js/task_add_row.js"

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_module_holds_the_write(self):
        source = self.MODULE.read_text()
        self.assertIn(
            "function bindTaskAddRows(csrfToken, {serialize = fn => fn()} = {})",
            source,
        )
        self.assertIn("fetch('/task/add/'", source)

    def test_the_write_goes_through_the_surface_s_own_serialisation(self):
        """#156: an add writes the session, and so does the background request
        that fetches the AI summary — Django saves the whole dict per
        response, so two in flight mean the later save drops the earlier
        one's. The two surfaces serialise differently, so the module takes the
        wrapper rather than one of their implementations. The default runs the
        fetch straight, for a surface with no background writer."""
        source = self.MODULE.read_text()
        self.assertIn("response = await serialize(() => fetch('/task/add/'", source)

    def test_no_template_holds_a_copy_of_it(self):
        # The uniqueness criterion as a test rather than as a review note:
        # the endpoint is the one thing a second copy could not do without.
        holders = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if "/task/add/" in path.read_text()
        )
        self.assertEqual(holders, [])

    def test_every_surface_inherits_it_from_the_base_template(self):
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertIn(
            "<script src=\"{% static 'projects/js/task_add_row.js' %}\"></script>",
            base,
        )

    def test_it_is_loaded_before_the_inline_scripts_that_call_it(self):
        # Not `defer`, for task_date_picker.js's reason: both surfaces call
        # bindTaskAddRows() from an inline script in extra_js, and an inline
        # script runs while the document is still parsing.
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertNotIn("task_add_row.js' %}\" defer", base)
        self.assertLess(
            base.index("task_add_row.js"), base.index("{% block extra_js %}")
        )

    def test_both_surfaces_bind_it_with_their_own_token(self):
        # The token is the one thing the two get differently — the dashboard
        # from the hidden input the page owns, /mein-plan/ from the template
        # variable — which is why it is the argument.
        self.assertIn("bindTaskAddRows(CSRF, {serialize:", self.dashboard_html())
        self.assertIn(
            "bindTaskAddRows(CSRF, {serialize:",
            self.client.get(reverse("my_plan")).content.decode(),
        )

    def test_each_surface_passes_the_serialisation_it_has(self):
        """The dashboard has the #235 queue; /mein-plan/ has the one promise
        its single background request needs. Same guarantee, two sizes — the
        argument is what keeps the module out of that choice (#156)."""
        self.assertIn(
            "bindTaskAddRows(CSRF, {serialize: withSessionWriteLock});",
            self.dashboard_html(),
        )
        self.assertIn(
            "bindTaskAddRows(CSRF, {serialize: async fn => "
            "{ await summarySettled(); return fn(); }});",
            self.client.get(reverse("my_plan")).content.decode(),
        )

    def test_a_successful_add_reloads(self):
        # The answer carries no figures — the cache was busted, so there is
        # nothing warm left to derive them from, and the server re-renders
        # every count, bar and badge instead (#210).
        self.assertIn("window.location.reload();", self.MODULE.read_text())

    def test_a_rejected_fetch_is_a_failed_add_too(self):
        # #159: a transport failure never reaches the .ok check.
        source = self.MODULE.read_text()
        self.assertIn("if (!response || !response.ok)", source)
        self.assertIn("flashActionFailed(submitEl);", source)

    def test_what_was_typed_survives_a_failure(self):
        # Cleared by the reload on success and by nothing else, so a failed
        # add is retried rather than retyped.
        source = self.MODULE.read_text()
        self.assertNotIn("nameEl.value = ''", source)

    def test_the_write_is_visible_while_it_runs(self):
        # #198, the same promise the date picker makes — and the guard that
        # keeps a second click from writing a second Notion page, which
        # create_task deliberately does not deduplicate.
        source = self.MODULE.read_text()
        self.assertIn("row.classList.add('pending');", source)
        self.assertIn("if (row.classList.contains('pending')) return;", source)


class TheAddRowOpensOnTodayTest(DemoModeTestCase):
    """#279: the row's date field was a bare <input type="date">. Empty, it
    paints the browser's own format hint in the field's own grey, which beside
    a placeholder-grey name field reads as a value that is already there — and
    is not: the module refused the empty date before the fetch, so typing a
    name and pressing Enter flashed a failure on the one field the eye had
    ticked off.

    Today rather than nothing, because that is the common case: a task typed
    into a plan that is open right now is due today or within a few days.
    Safe here only because the partial renders nothing under a Zeitreise
    moment — so "today" can never be read against a simulated date and
    #217's rule is inherited rather than restated."""

    # The role each surface includes the row under, which is the role its own
    # list spells dates in — asserted against the templates themselves in
    # test_naming.ShortRowDateReachesOnlyTheRowTest.
    ROLES = {"dashboard": "row", "my_plan": "long"}

    def surfaces(self):
        self.given_session_plan()
        return {
            "dashboard": self.client.get(reverse("dashboard")).content.decode(),
            "my_plan": self.client.get(reverse("my_plan")).content.decode(),
        }

    def add_row(self, html):
        start = html.index('class="task-add-row"')
        return html[start : html.index("</div>", start)]

    def test_both_surfaces_render_today_as_the_value(self):
        today = timezone.localdate()
        for surface, html in self.surfaces().items():
            with self.subTest(surface=surface):
                self.assertIn(
                    f'data-raw-date="{today.isoformat()}"', self.add_row(html)
                )

    def test_the_label_is_the_same_form_the_rows_above_it_use(self):
        # Not the browser's 10.12.2026 — the defect was a row asking for the
        # same kind of value in a shape the app shows nowhere else. Which
        # shape that is, is the surface's own: #238 settled the display form
        # per surface, so the dashboard's list abbreviates the month and
        # /mein-plan/'s writes it out, and the add row follows whichever list
        # it closes rather than pinning one and being the odd date on the
        # other page.
        today = timezone.localdate()
        for surface, html in self.surfaces().items():
            with self.subTest(surface=surface):
                label = format_date(today, role=self.ROLES[surface])
                row = self.add_row(html)
                self.assertIn(f">{label}</button>", row)
                self.assertIn(f'aria-label="Fällig am, aktuell {label}"', row)

    def test_each_surface_hands_over_its_own_month_table(self):
        # The assertion that keeps the one above honest. Not a comparison of
        # the two rendered labels: in May the abbreviation and the full name
        # are both "Mai", so a label test alone would pass for a month a year
        # with both surfaces pinned to one role — the state this fixed. The
        # tables differ in every month, so they are what gets compared.
        rows = {
            surface: self.add_row(html) for surface, html in self.surfaces().items()
        }
        self.assertIn('data-months="Jan,Feb,Mär,', rows["dashboard"])
        self.assertIn('data-months="Januar,Februar,März,', rows["my_plan"])

    def test_the_date_carries_no_urgency_stage(self):
        # Urgency is a property of a task, and there is none yet. Painting one
        # would also mean re-deriving _classify_due_urgency in the client on
        # every pick, which #198 settled the other way.
        for surface, html in self.surfaces().items():
            with self.subTest(surface=surface):
                self.assertIn(
                    '<button type="button" class="task-due task-add-date"',
                    self.add_row(html),
                )

    def test_the_date_carries_no_task_id(self):
        # Asserted rather than assumed: data-task-id is the contract
        # bindTaskDatePickers() binds on, and both surfaces call it. A row
        # that grew one would be claimed by those calls and hand an undefined
        # id to a reschedule.
        for surface, html in self.surfaces().items():
            with self.subTest(surface=surface):
                row = self.add_row(html)
                self.assertIn("task-add-date", row)
                self.assertNotIn("data-task-id", row)

    def test_an_untouched_row_creates_a_task_due_today(self):
        # The whole point of the preselection, end to end: what the server
        # rendered is what the endpoint accepts.
        html = self.surfaces()["dashboard"]
        raw = re.search(
            r'class="task-due task-add-date" data-raw-date="([^"]+)"',
            self.add_row(html),
        )
        self.assertIsNotNone(raw)
        self.client.post(
            reverse("add_task"),
            data=json.dumps(
                {
                    "project_id": "session-plan",
                    "name": "Noten kopieren",
                    "date": raw.group(1),
                }
            ),
            content_type="application/json",
        )
        added = [
            task
            for task in self.client.session["demo_plan"]["tasks"]
            if task["name"] == "Noten kopieren"
        ]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["date"], timezone.localdate().isoformat())


class TheAddRowReusesThePickerTest(DemoModeTestCase):
    """#279: the add row asks for a date the way every other surface does, by
    calling task_date_picker.js rather than by growing a second swap.

    It cannot be *bound* by bindTaskDatePickers() — that function's contract
    is a task id and there is no task yet — so the swap became an entry point
    of its own, openTaskDatePicker(), and the binding above it delegates. The
    alternative, a smaller copy of the swap inside this module, is what every
    assertion here exists to keep out: the keyboard handling, the focus
    restoration and the modality tracking are the three things #200 and #257
    took two attempts each to get right."""

    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"
    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"
    MODULE = Path(settings.BASE_DIR) / "projects/static/projects/js/task_add_row.js"

    def test_the_swap_is_an_entry_point_of_its_own(self):
        self.assertIn(
            "function openTaskDatePicker(displayEl, onPick) {", self.PICKER.read_text()
        )

    def test_the_task_id_binding_delegates_to_it(self):
        # One swap, two ways in — not two swaps. The binding's own job is
        # reduced to reading the row and closing over the id.
        picker = self.PICKER.read_text()
        self.assertIn(
            "openTaskDatePicker(dueEl, iso => "
            "onPick(dueEl.dataset.taskId, iso, dueEl, row));",
            picker,
        )
        self.assertEqual(picker.count("input.showPicker();"), 1)
        self.assertEqual(picker.count("document.createElement('input')"), 1)

    def test_the_add_row_calls_it_rather_than_repeating_it(self):
        source = self.MODULE.read_text()
        self.assertIn("openTaskDatePicker(dateEl, picked => {", source)
        for copied in (
            "createElement('input')",
            "showPicker()",
            "replaceWith(",
            "lastInputWasKeyboard",
        ):
            with self.subTest(copied=copied):
                self.assertNotIn(copied, source)

    def test_the_picker_is_loaded_before_the_module_that_calls_it(self):
        # True before #279 too, by the order the two scripts happened to be
        # listed in. It is load-bearing now, so it gets asserted: both are
        # plain scripts, so task_add_row.js's call site is resolved at call
        # time, but a reversed order would still be a reader's trap.
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertLess(
            base.index("task_date_picker.js"), base.index("task_add_row.js")
        )


class TheAddRowsLabelMirrorsItsSurfacesRoleTest(DemoModeTestCase):
    """#279: the one place a date format is composed twice, and the reason it
    is allowed to be.

    Every other surface gets its new label from the write's own response —
    the dashboard's reschedule() reads due_display_row off it. A pick in the
    add row writes nothing, so there is no response to read and the label has
    to be composed in the client. What #198 declined was the larger half of
    that: painting a rescheduled row's *stage*, which meant re-deriving
    #169's calendar-week urgency rule in JavaScript. There is no urgency
    here, and the names still come from date_format.py — handed to the
    partial by planner_tags.date_names, so #192 finds both halves.

    One literal, two roles. "long" and "row" differ in the month table and in
    nothing else, so the surface's role decides which table travels and the
    composition in the client is the same either way — which is what lets the
    add row follow the list it closes instead of being the odd date on one of
    the two pages."""

    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"
    MODULE = Path(settings.BASE_DIR) / "projects/static/projects/js/task_add_row.js"
    LITERAL = re.compile(r"return `([^`]+)`;")

    def test_the_names_are_rendered_from_the_server(self):
        self.given_session_plan()
        for surface, months in (
            ("dashboard", "Jan,Feb,Mär,Apr,Mai,Jun,Jul,Aug,Sep,Okt,Nov,Dez"),
            (
                "my_plan",
                (
                    "Januar,Februar,März,April,Mai,Juni,"
                    "Juli,August,September,Oktober,November,Dezember"
                ),
            ),
        ):
            with self.subTest(surface=surface):
                html = self.client.get(reverse(surface)).content.decode()
                # Shared by both roles — the two differ in the month table
                # alone, which is the whole reason one literal serves both.
                self.assertIn('data-weekdays="Mo,Di,Mi,Do,Fr,Sa,So"', html)
                self.assertIn(f'data-months="{months}"', html)

    def test_the_partial_reads_them_through_the_tag(self):
        partial = (self.TEMPLATES / "_task_add_row.html").read_text()
        self.assertIn("{% date_names 'weekdays' date_role %}", partial)
        self.assertIn("{% date_names 'months' date_role %}", partial)

    def test_the_module_carries_no_name_list_of_its_own(self):
        # The duplication that would actually cost something: a second table
        # drifts silently when #192 or a typo changes the first. Both months
        # tables, since either role's can be the one handed over.
        source = self.MODULE.read_text()
        for name in ("'Jan'", "'Mo'", "'Dez'", "'So'", "'Januar'", "'Dezember'"):
            with self.subTest(name=name):
                self.assertNotIn(name, source)

    def test_the_composed_label_is_the_role_the_surface_passed(self):
        # The template literal is read out of the module and composed in
        # Python against the tables the tag hands over, so the client's
        # format, date_names and format_date cannot drift apart without this
        # failing — for either role the add row can be included under.
        d = date(2026, 12, 15)
        match = self.LITERAL.search(self.MODULE.read_text())
        self.assertIsNotNone(match, "formatRowDate's template literal moved")
        for role in ("row", "long"):
            with self.subTest(role=role):
                weekdays = date_names("weekdays", role).split(",")
                months = date_names("months", role).split(",")
                composed = match.group(1)
                for placeholder, value in (
                    ("${WEEKDAYS[(d.getDay() + 6) % 7]}", weekdays[d.weekday()]),
                    ("${d.getDate()}", str(d.day)),
                    ("${MONTHS[d.getMonth()]}", months[d.month - 1]),
                ):
                    self.assertIn(placeholder, composed)
                    composed = composed.replace(placeholder, value)
                self.assertNotIn("${", composed)
                self.assertEqual(composed, format_date(d, role=role))

    def test_an_unknown_name_table_or_role_raises(self):
        # format_date's reason: both are named as bare strings from a
        # template, so a typo has no other way of announcing itself — it
        # would render an empty attribute and the client would compose
        # `undefined` into a date.
        for table, role in (("monate", "row"), ("months", "zeile")):
            with self.subTest(table=table, role=role), self.assertRaises(ValueError):
                date_names(table, role)

    def test_a_role_format_date_knows_is_not_automatically_offered(self):
        # "short" and "note" are real roles (date_format._ROLE_FORMATTERS)
        # that this tag deliberately does not serve: neither is a shape the
        # client's one literal composes, so handing over tables for them
        # would promise a format task_add_row.js cannot produce.
        for role in ("short", "note"):
            with self.subTest(role=role):
                format_date(date(2026, 12, 15), role=role)
            with self.subTest(role=role), self.assertRaises(ValueError):
                date_names("months", role)


class TheAddRowSendsIsoTest(DemoModeTestCase):
    """#279 is a display change only: what travels to /task/add/ is still the
    ISO date the server rendered, or the one the native picker answered with.

    The value is kept in a local variable rather than read back off the
    button, because the button now holds a formatted German label — parsing
    "Mi, 10. Dez" back into a date is exactly the round trip this keeps out."""

    MODULE = Path(settings.BASE_DIR) / "projects/static/projects/js/task_add_row.js"

    def test_the_iso_value_starts_from_the_rendered_attribute(self):
        self.assertIn("let iso = dateEl.dataset.rawDate;", self.MODULE.read_text())

    def test_nothing_reads_a_date_off_the_display_element(self):
        source = self.MODULE.read_text()
        self.assertNotIn("dateEl.value", source)
        self.assertNotIn("dateEl.textContent.", source)

    def test_a_pick_writes_the_iso_and_the_label_together(self):
        # An aria-label overrides the element's own text as the accessible
        # name, so writing only textContent would leave the button reading
        # the new date and announcing the old one — reschedule() documents
        # the same thing on the dashboard's rows.
        source = self.MODULE.read_text()
        self.assertIn("iso = picked;", source)
        self.assertIn("dateEl.dataset.rawDate = picked;", source)
        self.assertIn("dateEl.textContent = label;", source)
        self.assertIn(
            "dateEl.setAttribute('aria-label', `Fällig am, aktuell ${label}`);", source
        )

    def test_a_cleared_pick_is_dropped_rather_than_stored(self):
        # An empty label would leave a button with nothing to click on, and
        # the value the row needs is the one it already has.
        self.assertIn("if (!picked) return false;", self.MODULE.read_text())

    def test_the_post_still_carries_the_iso_date(self):
        self.assertIn("name: name, date: iso}", self.MODULE.read_text())


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
        # #284 widened the branch to every trash action rather than adding a
        # second one beside it: the project's removal asks the same way, and
        # a prefix test is what keeps the two from drifting apart.
        html = self.dashboard_html()
        self.assertIn(
            "if (action.startsWith('trash') && !item.classList.contains('armed')) {",
            html,
        )
        self.assertIn("item.textContent = armedTrashLabel(item);", html)

    def test_closing_the_menu_disarms_it(self):
        # Reopening never starts one click away from a removal — for the
        # project's item too, which is what the ^= reaches (#284).
        html = self.dashboard_html()
        self.assertIn(
            "items.querySelectorAll('.task-menu-item[data-action^=\"trash\"]')"
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


class TheDateIsOneComponentTest(DemoModeTestCase):
    """#195 and #200: the reschedulable date is defined once, in
    _task_due.html, and it is a real control rather than a styled span.

    Before this, four surfaces re-typed the contract — a `.task-due` element
    carrying `data-task-id` and `data-raw-date`, picked up by selector — and
    two of them had already drifted apart. The point of the partial is that
    a new surface offering the date (#186, #193) is an include, and that
    #200's question "what element is this" has exactly one answer.

    #266 moved the behaviour after the markup: the picker is one module, so
    the assertions about how the date is asked for read that file rather
    than whichever template used to hold the block."""

    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"
    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"

    def picker_source(self):
        return self.PICKER.read_text()

    def test_only_the_two_partials_write_the_date_markup(self):
        # The assertion that keeps the next surface from re-typing it: any
        # other template spelling the class itself is the drift coming back.
        #
        # #279 added the second one, and it is the only kind of date that can
        # earn a template of its own: the add row's date belongs to no task
        # yet, so it can carry neither the task id nor the urgency stage
        # _task_due.html renders from. What it does share is the class, the
        # stylesheet and the picker — the three halves that drifted.
        writers = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if 'class="task-due' in path.read_text()
        )
        self.assertEqual(writers, ["_task_add_row.html", "_task_due.html"])

    def test_the_reschedulable_date_is_a_focusable_button(self):
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('<button type="button" class="task-due', html)
        # Named for a screen reader, which reads a bare date as a date and
        # gives no hint that activating it does anything.
        self.assertIn('aria-label="Datum ändern, aktuell ', html)

    def test_the_button_carries_the_same_contract_the_selector_binds_to(self):
        # The JS binds by selector, not by template, so the attributes are
        # the whole contract — an element with them is reschedulable.
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(
            "document.querySelectorAll('.task-due[data-task-id]')",
            self.picker_source(),
        )
        self.assertRegex(
            html,
            r'<button type="button" class="task-due[^"]*" data-task-id="[^"]+" '
            r'data-raw-date="\d{4}-\d{2}-\d{2}"',
        )

    def test_the_keyboard_gets_its_focus_back_after_the_picker_closes(self):
        # A button answers Enter by itself; what it cannot do by itself is
        # survive being swapped out for the input — focus would be left on a
        # detached element and the next Tab would start from the top.
        #
        # The device is read off the events, not off the element afterwards.
        # Asking the button was tried twice and failed twice: :focus-visible is
        # false on the actions menu's route, where the button is clicked by
        # script and never focused (#239), and :focus:not(:focus-visible) is
        # false in Safari on macOS for an ordinary mouse click, because Safari
        # does not focus a <button> when you click it. An element cannot say
        # how the click that reached it was produced; the events can.
        source = self.picker_source()
        self.assertIn("const cameFromKeyboard = lastInputWasKeyboard;", source)
        self.assertIn(
            "const restore = () => { if (cameFromKeyboard) displayEl.focus(); };",
            source,
        )

    def test_the_device_is_tracked_from_the_events_themselves(self):
        # capture:true so a handler that stops propagation cannot leave the
        # modality stale, and pointerdown/keydown rather than click/keyup so it
        # is already current when the click handler above reads it.
        source = self.picker_source()
        self.assertIn(
            "document.addEventListener('keydown', "
            "() => { lastInputWasKeyboard = true; }, true);",
            source,
        )
        self.assertIn(
            "document.addEventListener('pointerdown', "
            "() => { lastInputWasKeyboard = false; }, true);",
            source,
        )

    def test_the_menu_route_reaches_the_button_without_focusing_it(self):
        # What makes the case above real rather than theoretical: the menu
        # item does not re-implement the picker, it clicks the button — so the
        # handler runs on an element the user never focused.
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("dueSpan.click();", html)

    def test_the_announced_date_is_rewritten_with_the_visible_one(self):
        # An aria-label overrides the element's own text as the accessible
        # name, so updating only textContent leaves the button reading the new
        # date and announcing the old one — on every path that does not reload,
        # which is any move that keeps the task's stage. Both come from the
        # same response field so they cannot drift.
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("dueSpan.textContent = data.due_display_row;", html)
        self.assertIn(
            "dueSpan.setAttribute('aria-label', "
            "`Datum ändern, aktuell ${data.due_display_row}`);",
            html,
        )


class ProjectLinksAreButtonsTest(DemoModeTestCase):
    """#200: opening a project was an onclick on a <span> in the task row
    and on a <strong> in the AI summary. Neither takes focus, so the second
    of the two things this app is for could be reached with a mouse only.

    The same shape _task_due.html settled for the date: keep the class, use
    a real <button>, and give it an accessible name that carries the
    context rather than leaning on a title attribute.

    #49 left one in-content shape instead of two: the summary's heading is
    a theme now and carries no link, while its task rows render the same
    button.task-project.ai-project-link the task rows do. That markup is
    asserted in test_summary, where the generator is stubbed and the block
    renders at all."""

    CSS = Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"

    def test_the_row_label_is_a_button_naming_its_project(self):
        html = self.client.get(reverse("dashboard") + "?mode=multi").content.decode()
        self.assertRegex(
            html,
            r'<button type="button" class="task-project ai-project-link" '
            r'data-project-id="[^"]+" aria-label="Projekt [^"]+ öffnen">',
        )

    def test_no_rule_decorates_a_summary_heading_as_a_link(self):
        # #49: the heading is a theme, so the › affordance it carried would
        # promise a click-through that is no longer there.
        self.assertNotContains(
            self.client.get(reverse("dashboard")),
            "strong > button.ai-project-link",
        )

    def test_no_rendered_page_opens_a_project_from_an_onclick(self):
        for query in ("", "?mode=multi"):
            with self.subTest(query=query):
                self.assertNotIn(
                    'onclick="showProject(',
                    self.client.get(reverse("dashboard") + query).content.decode(),
                )

    def test_the_opened_section_takes_focus_from_the_hidden_row(self):
        # Activating the label hides the row it sits in, so focus would fall
        # to <body> and the next Tab would start at the top of the document.
        # The header of the section that just opened takes it instead —
        # tabindex="-1" makes it focusable without adding it to the tab
        # order, the same trick the picker's restore() has no need of
        # because its button stays in the page.
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('<div class="project-header" tabindex="-1">', html)
        self.assertIn(
            "if (lastInputWasKeyboard) section.querySelector('.project-header')?.focus();",
            html,
        )

    def test_the_move_is_gated_on_the_same_modality_flag(self):
        # Read off the events rather than off the element, which is the
        # answer #257 settled on because it is the browser-independent one.
        # It also keeps the initial deep-link sync out: no keystroke has
        # happened yet at load, so a ?project= URL leaves focus where the
        # page put it.
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("lastInputWasKeyboard", html)
        self.assertIn(
            "let lastInputWasKeyboard = false;",
            (
                Path(settings.BASE_DIR)
                / "projects/static/projects/js/task_date_picker.js"
            ).read_text(),
        )

    def test_one_listener_covers_every_project_label(self):
        # .task-project.ai-project-link is a subset of this selector, so the
        # task rows and the summary's rows need one binding between them.
        # #156 gave the binding a root so the summary can be rebound after
        # the fragment endpoint swaps it in; the selector is unchanged.
        page = self.client.get(reverse("dashboard"))
        self.assertContains(
            page,
            "root.querySelectorAll('button.ai-project-link[data-project-id]')",
        )
        self.assertContains(page, "bindProjectLinks(document);")

    def test_the_reset_and_the_ring_live_in_the_stylesheet(self):
        css = self.CSS.read_text()
        for rule in (
            "button.ai-project-link {",
            "button.ai-project-link:focus-visible {",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)


class ThePickerIsOneModuleTest(DemoModeTestCase):
    """#266: the behaviour half of #195. The markup was already one partial;
    the handler that turns it into a control was still a block inside
    dashboard.html, bound by selector — which only ever reached markup
    rendered by that one template. Any further surface had to copy it, and
    the block carries #200's modality reasoning, which is the part most
    likely to be re-derived wrongly.

    What moved is the *asking*: swap the button for an input, open it, track
    the device, swap back. What stayed per surface is the consequence — each
    passes its own callback."""

    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"
    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"

    def test_the_module_holds_the_picker(self):
        source = self.PICKER.read_text()
        self.assertIn("function bindTaskDatePickers(onPick", source)
        self.assertIn("input.showPicker();", source)

    def test_no_template_holds_a_copy_of_it(self):
        # The uniqueness criterion as a test rather than as a review note:
        # showPicker() is the one call a second copy could not do without.
        # The call, not the word — _task_due.html names it in prose, which
        # is the pointer working rather than a copy.
        holders = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if "input.showPicker()" in path.read_text()
        )
        self.assertEqual(holders, [])

    def test_every_surface_inherits_it_from_the_base_template(self):
        # In base_dashboard.html, so the dashboard, /mein-plan/, the triage
        # list and any further surface get it without a script tag of their
        # own — and cannot get a different one.
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertIn(
            "<script src=\"{% static 'projects/js/task_date_picker.js' %}\"></script>",
            base,
        )

    def test_it_is_loaded_before_the_inline_scripts_that_call_it(self):
        # Not `defer`: every surface calls bindTaskDatePickers() from an
        # inline script in extra_js, and an inline script runs while the
        # document is still parsing — a deferred module would not be defined
        # yet. Order is what makes the plain tag correct, so it is asserted.
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertNotIn("task_date_picker.js' %}\" defer", base)
        self.assertLess(
            base.index("task_date_picker.js"), base.index("{% block extra_js %}")
        )

    def test_the_consequence_did_not_move_with_it(self):
        # The wrong reading of "extract the handler" is to share
        # reschedule(): the dashboard's depends on browsedWeekStart(),
        # reclassify(), applyRescheduleFigures() and sortRows(), none of
        # which mean anything on the triage list.
        source = self.PICKER.read_text()
        for helper in ("sortRows", "reclassify", "applyRescheduleFigures"):
            with self.subTest(helper=helper):
                self.assertNotIn(helper, source)

    def test_the_two_dashboard_behaviours_are_bound_by_region(self):
        # Read at bind time, not at pick time: the picker detaches the
        # button while the request runs, so closest() inside the callback
        # would find nothing to decide with.
        html = self.dashboard_html()
        self.assertIn("bindTaskDatePickers(reschedule, {exclude: '.ai-card'});", html)
        # #156 named the summary's callback, because the fragment makes it a
        # binding that has to happen twice — once on load and once against
        # the markup that was swapped in.
        self.assertEqual(
            html.count(
                "bindTaskDatePickers(rescheduleFromSummary, {within: '.ai-card'});"
            ),
            2,
        )

    def dashboard_html(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()


class TheFailureFlashIsOneModuleTest(DemoModeTestCase):
    """#233: the second half of #266's cut, one level up. The *asking* for a
    date was shared; the *reporting* of a write that did not land still lived
    in two copies of the same four lines plus two copies of the same CSS —
    and in neither of the surfaces #266 added. The close-out triage list was
    the proof: it had the handler's shape and none of the animation, so it
    could not have reported a failure even if it had wanted to, and both of
    its paths returned silently.

    Both halves move together, into base_dashboard.html's script tag and into
    dashboard.css, which that base already loads. A surface inherits the whole
    feedback or none of it — it cannot inherit half."""

    MODULE = Path(settings.BASE_DIR) / "projects/static/projects/js/action_feedback.js"
    CSS = Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"

    def test_the_module_holds_the_flash(self):
        source = self.MODULE.read_text()
        self.assertIn("function flashActionFailed(el)", source)
        self.assertIn("el.classList.add('action-failed');", source)
        self.assertIn(
            "setTimeout(() => el.classList.remove('action-failed'), ACTION_FAILED_MS);",
            source,
        )
        # Named rather than written twice: #283's project date reloads the
        # page once the flash has been seen, and the reload has to outlast
        # the feedback or there would be nothing to see.
        self.assertIn("const ACTION_FAILED_MS = 1500;", source)

    def test_no_template_holds_a_copy_of_it(self):
        holders = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if "function flashActionFailed" in path.read_text()
        )
        self.assertEqual(holders, [])

    def test_every_surface_inherits_it_from_the_base_template(self):
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertIn(
            "<script src=\"{% static 'projects/js/action_feedback.js' %}\"></script>",
            base,
        )

    def test_it_is_loaded_before_the_inline_scripts_that_call_it(self):
        # Same reason as the picker's tag: every caller is an inline script in
        # extra_js, which runs while the document is still parsing, so a
        # deferred module would not be defined yet.
        base = (self.TEMPLATES / "base_dashboard.html").read_text()
        self.assertNotIn("action_feedback.js' %}\" defer", base)
        self.assertLess(
            base.index("action_feedback.js"), base.index("{% block extra_js %}")
        )

    def test_the_animation_lives_once_in_the_shared_sheet(self):
        css = self.CSS.read_text()
        self.assertEqual(css.count(".action-failed { animation:"), 1)
        self.assertEqual(css.count("@keyframes flash-failed"), 1)

    def test_no_surface_carries_its_own_copy_of_the_animation(self):
        # The CSS is the half that decided this: a template keeping its own
        # keyframes would go on working while the triage list still could not
        # flash, which is exactly the state this replaces.
        holders = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if "@keyframes flash-failed" in path.read_text()
        )
        self.assertEqual(holders, [])

    def test_the_guard_is_only_for_the_caller_that_needs_it(self):
        # setSimDate's clicked control is an optional argument, so its failure
        # branch flashes unconditionally rather than repeating the check its
        # optimistic paint already makes.
        self.assertIn("if (!el) return;", self.MODULE.read_text())


class ThePickerSaysItIsSavingTest(DemoModeTestCase):
    """#198: the row sat unchanged while a reschedule ran, and a reschedule is
    two Notion round trips — update_task_date plus increment_postpone_count,
    which is read-then-write because Notion has no atomic increment. Nothing
    on screen said the pick had been taken.

    The literal optimistic write the issue asks for is declined deliberately
    (see the comment on #198): it means a second copy of German date
    formatting and of #169's calendar-week urgency rule in JavaScript, both
    settled the other way. What is shared instead is the *wait*, because every
    surface's wait is the same one."""

    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"
    CSS = Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"

    def test_the_input_is_marked_while_the_write_runs(self):
        source = self.PICKER.read_text()
        self.assertIn("input.classList.add('pending');", source)
        self.assertIn("input.setAttribute('aria-busy', 'true');", source)
        self.assertLess(
            source.index("input.classList.add('pending');"),
            source.index("await onPick("),
        )

    def test_the_mark_is_cleared_in_the_finally_that_swaps_the_date_back(self):
        # The same finally, not a second one: a callback that throws rather
        # than answering falsy would otherwise leave the input marked, and
        # the module exists so no surface has to know that.
        self.assertIn(
            "} finally {\n"
            "            input.classList.remove('pending');\n"
            "            input.removeAttribute('aria-busy');\n"
            "            swapBack();",
            self.PICKER.read_text(),
        )

    def test_it_marks_rather_than_disables(self):
        # Disabling blurs the input, and blur is what swaps the display
        # element back — mid-request.
        self.assertNotIn("input.disabled", self.PICKER.read_text())

    def test_the_mark_has_a_rule_in_the_shared_sheet(self):
        self.assertIn(".task-due-input.pending", self.CSS.read_text())


class TheDateAlwaysComesBackTest(DemoModeTestCase):
    """The one promise the shared module makes to all five surfaces: a click
    on a date can cost the picker, never the date — the add row's included
    since #279, which is the whole reason the swap is a function of its own
    rather than something a second surface could reimplement. Swapping in an
    <input type="date"> puts the row in a state only this module knows how to
    leave, and a surface's onPick cannot be trusted to unwind it — each one is
    a different page's code, and the module exists so none of them has to know.

    Two paths used to skip the way back. An onPick that *threw* rather than
    answering falsy — response.json() on a 200 that is not JSON, or any of the
    dashboard's own patching helpers — never reached the swap; and showPicker()
    throwing ran before the listeners existed at all. Either left a bare date
    input standing where the date was until the next page load."""

    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"

    def source(self):
        return self.PICKER.read_text()

    def test_a_throwing_callback_still_gets_the_date_put_back(self):
        # try/finally rather than a plain await: resolved, falsy and thrown
        # all have to end in the same swap.
        source = self.source()
        self.assertIn(
            "} finally {\n"
            "            input.classList.remove('pending');\n"
            "            input.removeAttribute('aria-busy');\n"
            "            swapBack();",
            source,
        )
        # Asserted before the index() below, so a module without the try at all
        # fails with the reason rather than with a ValueError.
        self.assertIn("try {", source)
        self.assertLess(source.index("try {"), source.index("await onPick("))

    def test_both_listeners_exist_before_the_picker_is_opened(self):
        # showPicker() is the line that can throw, so it is the line nothing
        # this handler still owes may sit behind. Registered first, a throw
        # costs the picker and nothing else: blur still brings the date back.
        source = self.source()
        opened = source.index("input.showPicker();")
        self.assertLess(source.index("input.addEventListener('change'"), opened)
        self.assertLess(source.index("input.addEventListener('blur'"), opened)

    def test_one_place_puts_it_back_and_it_runs_once(self):
        # A pick followed by a click elsewhere fires change and blur both, so
        # the way back needs a guard — replaceWith() on a detached input is
        # already a no-op, but a second restore() would drag a keyboard user's
        # focus off whatever they had just moved to. One call site, one guard.
        source = self.source()
        self.assertEqual(source.count("input.replaceWith(displayEl);"), 1)
        self.assertIn("if (swappedBack) return;", source)
        self.assertIn("input.addEventListener('blur', swapBack);", source)


class TheAiSummaryOffersTheDateTest(DemoModeTestCase):
    """#193: the summary rendered a date it did not offer to change. It
    reloads rather than patching — its prose makes claims about urgency that
    a new date invalidates, and its task_refs are positions in a
    chronological order the move has just changed."""

    def summary_html(self):
        self.given_session_plan()
        self.ai_mocks["projects.views.generate_weekly_summary"].return_value = {
            "jetzt_faellig": [
                {"heading": "Testkonzert", "assessment": "x", "task_refs": [1]}
            ],
            "naechste_woche": [],
        }
        html = self.dashboard_with_summary().content.decode()
        return html[html.index('class="ai-card"') : html.index('<div class="kanban">')]

    def test_the_summary_date_is_a_button_for_a_session_plan(self):
        self.assertIn('<button type="button" class="task-due', self.summary_html())

    def test_it_stays_a_span_for_the_demo_example_projects(self):
        # The gate #193 asks for, and it needed no flag of its own: those
        # projects are in no session, so reschedule_task_view answers 404 by
        # design (#10 §5) — which is exactly what viewing_demo_data means.
        self.ai_mocks["projects.views.generate_weekly_summary"].return_value = {
            "jetzt_faellig": [
                {"heading": "Testkonzert", "assessment": "x", "task_refs": [1]}
            ],
            "naechste_woche": [],
        }
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('<button type="button" class="task-due', html)
        self.assertIn('<span class="task-due', html)

    def test_a_successful_move_from_the_summary_reloads(self):
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        binding = html[html.index("async function rescheduleFromSummary(") :]
        self.assertIn("const ok = await reschedulePersist(taskId, newDate);", binding)
        self.assertIn("window.location.reload();", binding)


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

    def test_the_date_that_is_not_clickable_is_not_a_button_either(self):
        # #200: the element carries the affordance. A <button> that opens no
        # picker would announce one to a screen reader and take a tab stop
        # for it, so a date that cannot be changed stays the span it was —
        # the same split _task_dot.html makes under a Zeitreise moment.
        html = self.client.get(reverse("dashboard") + "?mode=multi").content.decode()
        self.assertIn('<span class="task-due', html)
        self.assertNotIn('<button type="button" class="task-due', html)

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
                "heading": "Diese Woche",
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
                self.client.post(reverse("summary_fragment"))
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
                "function applyRescheduleFigures"
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
            {"jetzt_faellig": [{"heading": "Thema", "assessment": "ohne refs"}]},
            self.BEFORE,
            self.AFTER,
        )
        self.assertEqual(
            data["jetzt_faellig"], [{"heading": "Thema", "assessment": "ohne refs"}]
        )

    def test_everything_but_the_task_refs_is_left_alone(self):
        # task_refs are the only positions this rewrites. Since #49 the
        # block's heading is free text and carries no numbering at all.
        data = _remap_summary_refs(_summary_with_refs([1]), self.BEFORE, self.AFTER)
        self.assertEqual(data["jetzt_faellig"][0]["heading"], "Diese Woche")


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
    hit in that shape means "projects are good, generate the summary" —
    otherwise the card would read "KI nicht verfügbar" until the TTL ran
    out, which is not what a reschedule should cost.

    #156 moved that generation into summary_fragment() and made the same
    shape the one a *first* load leaves behind, so this is now the ordinary
    path rather than the post-reschedule one. What is asserted is unchanged:
    the summary comes back, it is written back, and no Notion read is paid
    for."""

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
            response = self.client.post(reverse("summary_fragment"))
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
            response = self.client.post(reverse("summary_fragment"))
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
        """#156: the Claude call runs inside summary_fragment() now, so the
        window this test simulates is that request's, not the page load's."""

        def generate(*args, **kwargs):
            concurrent_write()
            return _summary_data()

        with (
            patch("projects.views.generate_weekly_summary", side_effect=generate),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch("projects.views.get_upcoming_projects") as fetch,
        ):
            response = self.client.post(reverse("summary_fragment"))
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
        # #266: the block ends where the shared picker is bound to it — the
        # listener that used to close it off lives in its own file now.
        return html[
            html.index("async function reschedule(") : html.index(
                "bindTaskDatePickers(reschedule,"
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

    def test_the_answer_also_carries_the_next_week_label(self):
        # #266: the close-out triage list's move button offers due + 7 and
        # has to follow a hand-picked date. Derived here, so format_date
        # stays the one place that knows German date formatting (#189/#192)
        # and the client never grows a second copy of the month names.
        self.given_session_plan()
        new_date = date.today() + timedelta(days=14)
        response = self.client.post(
            reverse("reschedule_task", args=["demo-session-0"]),
            data=json.dumps({"date": new_date.isoformat()}),
            content_type="application/json",
        )
        self.assertEqual(
            response.json()["next_week_display"],
            format_date(new_date + timedelta(days=7), role="long"),
        )

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
                "const SIM_LOCK_NOTICE_MS"
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


@override_settings(DEMO_MODE=False)
class ProjectDateReachesNotionTest(TestCase):
    """#283: a project's event date, and its tasks' dates with it.

    Shifting a concert by a week shifts its posters, its press text and its
    GEMA filing by a week too; correcting a date that was a day off must
    touch nothing. Both are real, so `move_tasks` carries the answer the
    visitor gave rather than the server guessing one."""

    EVENT_DATE = date.today() + timedelta(days=10)
    NEW_DATE = (EVENT_DATE + timedelta(days=7)).isoformat()

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def warm(self, tasks=(), summary="<p>alt</p>"):
        _warm_dashboard_cache(tasks, summary=summary)

    def post(self, body, project_id="p1"):
        return self.client.post(
            reverse("reschedule_project", args=[project_id]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_the_project_date_reaches_notion(self):
        self.warm()
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch("projects.views.get_exclusive_tasks", return_value=[]),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post({"date": self.NEW_DATE, "move_tasks": False})
        mock_project.assert_called_once_with("p1", self.NEW_DATE)
        mock_task.assert_not_called()
        self.assertEqual(response.json(), {"ok": True, "moved": 0, "partial": False})

    def test_a_non_canonical_date_is_normalised_first(self):
        # _parse_posted_task_date's rule, inherited rather than restated:
        # date.fromisoformat accepts "20261217" and the string is what both
        # Notion and the shift arithmetic then work from.
        self.warm()
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch("projects.views.get_exclusive_tasks", return_value=[]),
        ):
            self.post({"date": "20261217", "move_tasks": False})
        mock_project.assert_called_once_with("p1", "2026-12-17")

    def test_the_delta_is_measured_against_the_cached_date(self):
        # The visitor confirmed "+7 Tage" against the date the page showed
        # them, which came out of CACHE_KEY. A server that re-read Termin
        # from Notion and recomputed the difference would shift by an amount
        # nobody agreed to.
        self.warm([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date"),
            patch(
                "projects.views.get_exclusive_tasks",
                return_value=[_cached_task("t-1", date.today() + timedelta(days=3))],
            ),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        mock_task.assert_called_once_with(
            "t-1", (date.today() + timedelta(days=10)).isoformat()
        )
        self.assertEqual(response.json()["moved"], 1)

    def test_a_task_shifts_from_its_own_date_rather_than_from_the_cached_one(self):
        # The tasks come from Notion for the opposite reason the delta does
        # not: they are shifted *relative* to their own dates, so a task
        # somebody rescheduled in Notion's own UI since the entry was read
        # moves from its real date.
        self.warm([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date"),
            patch(
                "projects.views.get_exclusive_tasks",
                # Notion's truth: moved to +5 since the cache was filled.
                return_value=[_cached_task("t-1", date.today() + timedelta(days=5))],
            ),
            patch("projects.views.update_task_date") as mock_task,
        ):
            self.post({"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1})
        mock_task.assert_called_once_with(
            "t-1", (date.today() + timedelta(days=12)).isoformat()
        )

    def test_only_open_dated_tasks_move(self):
        # A completed task's date records when the work was due and met;
        # moving it rewrites the plan's own history. A dateless task has
        # nothing to shift from. Which is also why the count the bar names
        # is neither tasks|length nor total - done: it is this set, and the
        # request below consents to exactly its size.
        self.warm()
        notion_tasks = [
            _cached_task("t-open", date.today() + timedelta(days=3)),
            _cached_task(
                "t-done",
                date.today() + timedelta(days=4),
                done=True,
                completed_date=date.today(),
            ),
            _cached_task("t-undated", None),
        ]
        with (
            patch("projects.views.update_project_date"),
            patch("projects.views.get_exclusive_tasks", return_value=notion_tasks),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(
            [call.args[0] for call in mock_task.call_args_list], ["t-open"]
        )
        self.assertEqual(response.json()["moved"], 1)

    def test_the_project_is_written_before_the_tasks(self):
        # No order makes a retry safe — project first and a retry computes a
        # zero delta, tasks first and a retry shifts twice what already
        # moved. The project goes first because it is the write that was
        # asked for and the task shift is the consequence consented to.
        self.warm()
        calls = []
        with (
            patch(
                "projects.views.update_project_date",
                side_effect=lambda pid, d: calls.append(("project", pid)),
            ),
            patch(
                "projects.views.get_exclusive_tasks",
                return_value=[_cached_task("t-1", date.today() + timedelta(days=3))],
            ),
            patch(
                "projects.views.update_task_date",
                side_effect=lambda tid, d: calls.append(("task", tid)),
            ),
        ):
            self.post({"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1})
        self.assertEqual(calls, [("project", "p1"), ("task", "t-1")])

    def test_the_project_only_answer_reads_no_tasks_at_all(self):
        self.warm([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch("projects.views.get_exclusive_tasks") as mock_read,
            patch("projects.views.update_task_date") as mock_task,
        ):
            self.post({"date": self.NEW_DATE, "move_tasks": False})
        mock_project.assert_called_once()
        mock_read.assert_not_called()
        mock_task.assert_not_called()

    def test_a_confirmed_write_busts_every_cached_copy(self):
        # A new event date can move the project between month groups and
        # re-orders the project list itself, which no other write does — and
        # the order comes from Notion's own sort, not from anything this app
        # knows how to reproduce. So the entry goes and the client reloads.
        self.warm([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date"),
            patch("projects.views.get_exclusive_tasks", return_value=[]),
        ):
            self.post({"date": self.NEW_DATE, "move_tasks": True, "shift_count": 0})
        for key in (
            CACHE_KEY,
            STALE_CACHE_KEY,
            UNASSIGNED_CACHE_KEY,
            STALE_UNASSIGNED_CACHE_KEY,
        ):
            with self.subTest(key=key):
                self.assertIsNone(cache.get(key))


@override_settings(DEMO_MODE=False)
class ProjectDateFailureTest(TestCase):
    """Every refusal on its own, and each one asserted to land before any
    write. The partial failure is the one this write cannot make repeatable:
    Notion has no batch update, so it is n+1 sequential writes, and nothing
    is idempotent against a shift by a difference."""

    EVENT_DATE = date.today() + timedelta(days=10)
    NEW_DATE = (EVENT_DATE + timedelta(days=7)).isoformat()

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post(self, body, project_id="p1"):
        return self.client.post(
            reverse("reschedule_project", args=[project_id]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_a_failed_project_write_leaves_the_cache_alone(self):
        # Nothing landed, so the entry this request read is still true.
        _warm_dashboard_cache([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch(
                "projects.views.update_project_date",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.get_exclusive_tasks", return_value=[]),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 0}
            )
        self.assertEqual(response.status_code, 502)
        self.assertIs(response.json()["partial"], False)
        mock_task.assert_not_called()
        self.assertIsNotNone(cache.get(CACHE_KEY))

    def test_a_failed_task_read_writes_nothing_and_keeps_the_cache(self):
        _warm_dashboard_cache([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch(
                "projects.views.get_exclusive_tasks",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.update_project_date") as mock_project,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(response.status_code, 502)
        mock_project.assert_not_called()
        self.assertIsNotNone(cache.get(CACHE_KEY))

    def test_a_partial_failure_drops_the_fresh_entry_and_keeps_the_fallback(self):
        # The project date has already moved, so the fresh entry still
        # names the old one. The stale pair must not go, and that is the
        # difference: this path is reached *because* Notion is unreachable,
        # which is the one situation dashboard() renders that copy for.
        _warm_dashboard_cache([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date"),
            patch(
                "projects.views.get_exclusive_tasks",
                return_value=[
                    _cached_task("t-1", date.today() + timedelta(days=3)),
                    _cached_task("t-2", date.today() + timedelta(days=4)),
                ],
            ),
            patch(
                "projects.views.update_task_date",
                side_effect=[None, NotionUnavailableError("boom")],
            ),
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 2}
            )
        self.assertEqual(response.status_code, 502)
        body = response.json()
        # The answer names how many landed: every task left behind is
        # individually fixable from its own row's date control.
        self.assertIs(body["partial"], True)
        self.assertEqual(body["moved"], 1)
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(UNASSIGNED_CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_CACHE_KEY))
        self.assertIsNotNone(cache.get(STALE_UNASSIGNED_CACHE_KEY))

    def test_a_cold_cache_is_a_404_rather_than_a_guess(self):
        # The delta cannot be reconstructed from anything the visitor saw,
        # which is trash_project_view's own reason for a 404 here.
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch("projects.views.get_exclusive_tasks") as mock_read,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(response.status_code, 404)
        mock_project.assert_not_called()
        mock_read.assert_not_called()

    def test_an_unknown_project_is_a_404(self):
        _warm_dashboard_cache([])
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1},
                project_id="nope",
            )
        self.assertEqual(response.status_code, 404)
        mock_project.assert_not_called()

    def test_a_missing_move_tasks_is_a_400_before_any_write(self):
        # Not defaulted either way: defaulting it to true would move tasks
        # nobody consented to, and to false would leave behind tasks
        # somebody did.
        _warm_dashboard_cache([])
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post({"date": self.NEW_DATE})
        self.assertEqual(response.status_code, 400)
        mock_project.assert_not_called()

    def test_a_move_tasks_that_is_not_a_boolean_is_a_400(self):
        _warm_dashboard_cache([])
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post({"date": self.NEW_DATE, "move_tasks": "ja"})
        self.assertEqual(response.status_code, 400)
        mock_project.assert_not_called()

    def test_an_invalid_date_is_a_400_before_any_write(self):
        _warm_dashboard_cache([])
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post({"date": "kein-datum", "move_tasks": False})
        self.assertEqual(response.status_code, 400)
        mock_project.assert_not_called()

    def test_a_malformed_body_is_a_400(self):
        _warm_dashboard_cache([])
        response = self.client.post(
            reverse("reschedule_project", args=["p1"]),
            data="kein json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_project_with_no_date_cannot_shift_its_tasks(self):
        # There is no difference to shift by. Unreachable from the page —
        # a project with no event_date renders the span rather than the
        # control (dashboard.html) — so this is the direct POST.
        project = _fake_upcoming_project()
        project["event_date"] = None
        project["tasks"] = [_cached_task("t-1", date.today() + timedelta(days=3))]
        _cache_fresh_read(
            CACHE_KEY,
            (_annotate_tasks([project], date.today()), None),
            CACHE_DEADLINE_KEY,
            60,
        )
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(response.status_code, 400)
        mock_project.assert_not_called()

    def test_get_is_not_a_write(self):
        self.assertEqual(
            self.client.get(reverse("reschedule_project", args=["p1"])).status_code,
            405,
        )


@override_settings(DEMO_MODE=False)
class ProjectDateCountIsCheckedTest(TestCase):
    """The figure the bar named, held against what the write is about to do.

    The bar reads `shiftable_count` off a CACHE_KEY entry up to eight hours
    old; the tasks come from a read taken now. So the two can genuinely
    disagree — a task created in Notion's own UI inside that window would be
    shifted without ever having been counted, and a visitor who agreed to
    three would have had five moved. The check turns the number from a guess
    into a promise."""

    EVENT_DATE = date.today() + timedelta(days=10)
    NEW_DATE = (EVENT_DATE + timedelta(days=7)).isoformat()

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post(self, body, project_id="p1"):
        return self.client.post(
            reverse("reschedule_project", args=[project_id]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_a_count_notion_no_longer_matches_is_a_409_before_any_write(self):
        _warm_dashboard_cache([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch(
                "projects.views.get_exclusive_tasks",
                # One more than the page counted: added in Notion's own UI
                # since the entry was read.
                return_value=[
                    _cached_task("t-1", date.today() + timedelta(days=3)),
                    _cached_task("t-new", date.today() + timedelta(days=4)),
                ],
            ),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(response.status_code, 409)
        # The true figure travels back, so the question can be asked again
        # with a number that is right rather than repeated with the wrong one.
        self.assertEqual(response.json()["shift_count"], 2)
        mock_project.assert_not_called()
        mock_task.assert_not_called()

    def test_the_count_is_of_the_tasks_that_would_move_and_no_others(self):
        # Same rule _annotate_tasks counts by: open and dated. A done task
        # and an undated one are in neither figure, so a plan holding them
        # does not 409 on its own shape.
        _warm_dashboard_cache(
            [_cached_task("t-open", date.today() + timedelta(days=3))]
        )
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch(
                "projects.views.get_exclusive_tasks",
                return_value=[
                    _cached_task("t-open", date.today() + timedelta(days=3)),
                    _cached_task(
                        "t-done",
                        date.today() + timedelta(days=4),
                        done=True,
                        completed_date=date.today(),
                    ),
                    _cached_task("t-undated", None),
                ],
            ),
            patch("projects.views.update_task_date") as mock_task,
        ):
            response = self.post(
                {"date": self.NEW_DATE, "move_tasks": True, "shift_count": 1}
            )
        self.assertEqual(response.status_code, 200)
        mock_project.assert_called_once()
        self.assertEqual(
            [call.args[0] for call in mock_task.call_args_list], ["t-open"]
        )

    def test_a_missing_shift_count_is_a_400_when_tasks_would_move(self):
        # move_tasks' own rule: the figure is not defaulted, because a
        # default would be a consent nobody gave.
        _warm_dashboard_cache([])
        with patch("projects.views.update_project_date") as mock_project:
            response = self.post({"date": self.NEW_DATE, "move_tasks": True})
        self.assertEqual(response.status_code, 400)
        mock_project.assert_not_called()

    def test_a_shift_count_that_is_not_a_whole_number_is_a_400(self):
        _warm_dashboard_cache([])
        for value in ("1", 1.5, -1, None, True):
            with self.subTest(value=value):
                with patch("projects.views.update_project_date") as mock_project:
                    response = self.post(
                        {
                            "date": self.NEW_DATE,
                            "move_tasks": True,
                            "shift_count": value,
                        }
                    )
                self.assertEqual(response.status_code, 400)
                mock_project.assert_not_called()

    def test_the_project_only_answer_needs_no_count(self):
        # Nothing moves, so there is no count to consent to — and no read
        # to check one against either.
        _warm_dashboard_cache([_cached_task("t-1", date.today() + timedelta(days=3))])
        with (
            patch("projects.views.update_project_date") as mock_project,
            patch("projects.views.get_exclusive_tasks") as mock_read,
        ):
            response = self.post({"date": self.NEW_DATE, "move_tasks": False})
        self.assertEqual(response.status_code, 200)
        mock_project.assert_called_once()
        mock_read.assert_not_called()


class ProjectDateDemoModeTest(DemoModeTestCase):
    """Both stacks, unlike the trash beside it. A session plan carries an
    event_date and an event_date_uncertain of its own, and the planner's
    fallback lead time is reachable there whenever the description held no
    date — so the public demo rendered "Termin unsicher" with no way to
    answer it."""

    def post(self, body, project_id="session-plan"):
        return self.client.post(
            reverse("reschedule_project", args=[project_id]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def given_plan(self, **overrides):
        return self.given_session_plan(
            event_date=(date.today() + timedelta(days=30)).isoformat(),
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
                    "done": True,
                },
                {
                    "id": "demo-session-2",
                    "name": "Noch ohne Termin",
                    "date": None,
                    "done": False,
                },
            ],
            **overrides,
        )

    def stored(self):
        return self.client.session["demo_plan"]

    def test_the_new_date_survives_a_reload(self):
        self.given_plan()
        new_date = (date.today() + timedelta(days=37)).isoformat()
        response = self.post({"date": new_date, "move_tasks": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored()["event_date"], new_date)
        self.assertContains(
            self.client.get(reverse("dashboard")),
            format_date(date.fromisoformat(new_date)),
        )

    def test_a_picked_date_is_no_longer_uncertain(self):
        # A date somebody has just picked is a date somebody has looked at,
        # which is what the flag asks about — the same answer
        # clearDateUncertain() gives a manual edit in the planner review.
        self.given_plan(event_date_uncertain=True)
        self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": False,
            }
        )
        self.assertIs(self.stored()["event_date_uncertain"], False)
        self.assertNotContains(self.client.get(reverse("dashboard")), "Termin unsicher")

    def test_the_open_tasks_shift_and_the_rest_stay(self):
        # One of the three is open *and* dated, which is the figure the bar
        # names and the one the request consents to — the done task and the
        # undated one are in neither.
        self.given_plan()
        self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            }
        )
        self.assertEqual(
            [t["date"] for t in self.stored()["tasks"]],
            [
                (date.today() + timedelta(days=14)).isoformat(),
                (date.today() + timedelta(days=14)).isoformat(),
                None,
            ],
        )

    def test_the_project_only_answer_moves_nothing(self):
        plan = self.given_plan()
        before = [t["date"] for t in plan["tasks"]]
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": False,
            }
        )
        self.assertEqual(response.json()["moved"], 0)
        self.assertEqual([t["date"] for t in self.stored()["tasks"]], before)

    def test_a_backwards_shift_is_not_clamped_to_today(self):
        # The planner review clamps (`if (d <= today) dateInput.value =
        # todayISO`); this deliberately does not. A task shifted into the
        # past is overdue, and _classify_due_urgency has an honest state for
        # that, while clamping would silently collapse several tasks onto
        # one date.
        self.given_plan()
        self.post(
            {
                "date": (date.today() + timedelta(days=10)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            }
        )
        self.assertEqual(
            self.stored()["tasks"][0]["date"],
            (date.today() - timedelta(days=13)).isoformat(),
        )

    def test_the_cached_summaries_follow_the_move(self):
        # The shift moves only the open tasks, so it can re-order a plan
        # whose done tasks stay put — the refs are rewritten rather than
        # swept, the way a task reschedule rewrites them.
        self.given_plan()
        session = self.client.session
        session[f"{SUMMARY_KEY}_today"] = {
            "jetzt_faellig": [
                {
                    "heading": "Jetzt fällig",
                    "assessment": "Programm zuerst",
                    "task_refs": [1],
                }
            ],
            "naechste_woche": [],
        }
        session.save()
        self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            }
        )
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)
        self.assertEqual(
            self.client.session[f"{SUMMARY_KEY}_today"]["jetzt_faellig"][0][
                "task_refs"
            ],
            [1],
        )

    def test_it_is_refused_under_a_zeitreise_moment(self):
        # add_task_view's answer rather than reschedule_task_view's, and the
        # count in the question is what decides it. A moment renders every
        # task due by sim_date as done (_simulated_project), so the page's
        # shiftable_count is the count at that moment — while the write
        # moves every open dated task the plan holds, because a moment is a
        # view and must not change what a write does (#217). Asking with one
        # number and moving by the other is the bug; the question is not
        # asked there instead. A single task's reschedule carries no such
        # number, which is why it stays available.
        plan = self.given_plan()
        self.given_timelapse_moments("2026-09-01")
        session = self.client.session
        session["demo_sim_date"] = "2026-09-01"
        session.save()
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            }
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored()["event_date"], plan["event_date"])

    def test_the_moment_refuses_the_date_only_answer_too(self):
        # The refusal is about the write, not about the task shift: the
        # event date is what a moment is a rendering of, so changing it
        # under one would re-date the picture from inside it.
        plan = self.given_plan()
        self.given_timelapse_moments("2026-09-01")
        session = self.client.session
        session["demo_sim_date"] = "2026-09-01"
        session.save()
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": False,
            }
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored()["event_date"], plan["event_date"])

    def test_a_count_the_plan_no_longer_matches_is_a_409(self):
        # Production's check, in both worlds for one write: the page this
        # answer was given on can have been rendered before another tab
        # added or checked off a task.
        plan = self.given_plan()
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 3,
            }
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["shift_count"], 1)
        self.assertEqual(self.stored()["event_date"], plan["event_date"])
        self.assertEqual(
            [t["date"] for t in self.stored()["tasks"]],
            [t["date"] for t in plan["tasks"]],
        )

    def test_an_example_project_is_a_404(self):
        # The demo example projects come from get_demo_projects() and are in
        # no session (#10 §5) — a 404 rather than a cheerful ok for
        # something that was never saved.
        plan = self.given_plan()
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            },
            project_id="demo-1",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored()["event_date"], plan["event_date"])

    def test_no_session_plan_at_all_is_a_404(self):
        response = self.post(
            {
                "date": (date.today() + timedelta(days=37)).isoformat(),
                "move_tasks": True,
                "shift_count": 1,
            }
        )
        self.assertEqual(response.status_code, 404)

    def test_a_rejected_write_moves_nothing(self):
        plan = self.given_plan()
        response = self.post({"date": "kein-datum", "move_tasks": True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored()["event_date"], plan["event_date"])


class ProjectDateIsOfferedWhereItPersistsTest(DemoModeTestCase):
    """A write is offered where it takes effect. This one takes effect in
    both worlds, which is where it parts company with the trash it shares a
    menu with: a session plan has no Notion page to archive but does have an
    event_date to change."""

    def production_dashboard(self, stale=False):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            page = self.client.get(reverse("dashboard"))
            if not stale:
                return page
            # STALE_CACHE_KEY is written when the summary arrives, not by
            # the read itself (#156) — and inside this block, or the Claude
            # call would be a real one.
            self.client.post(reverse("summary_fragment"))
        cache.delete(CACHE_KEY)
        with (
            patch(
                "projects.views.get_upcoming_projects",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
        ):
            return self.client.get(reverse("dashboard"))

    def test_the_date_is_a_button_for_a_demo_visitors_own_plan(self):
        self.given_session_plan()
        self.assertContains(
            self.client.get(reverse("dashboard")),
            '<button type="button" class="project-date"',
        )

    def test_a_demo_example_project_keeps_the_span_and_no_menu(self):
        # _task_due.html's own rule for the same catalogue: the example
        # projects come from get_demo_projects() and are in no session (#10
        # §5), so nothing there can be written to and the endpoint answers
        # 404. A control that answers 404 on every click is an affordance
        # that is not there.
        page = self.client.get(reverse("dashboard"))
        self.assertTrue(page.context["viewing_demo_data"])
        self.assertContains(page, '<span class="project-date">')
        self.assertNotContains(page, '<button type="button" class="project-date"')
        self.assertNotContains(page, 'data-action="reschedule-project"')
        self.assertNotContains(page, '<span class="task-menu project-menu">')

    @override_settings(DEMO_MODE=False)
    def test_the_date_is_a_button_in_production_too(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.assertContains(
            self.production_dashboard(),
            '<button type="button" class="project-date"',
        )

    @override_settings(DEMO_MODE=False)
    def test_a_stale_render_keeps_the_span(self):
        # A stale page is the page whose last Notion read failed, so the
        # write behind this control cannot land — and the endpoint reads
        # CACHE_KEY, which that page did not render from. A control that is
        # offered has to be able to work.
        cache.clear()
        self.addCleanup(cache.clear)
        page = self.production_dashboard(stale=True)
        self.assertTrue(page.context["stale"])
        self.assertContains(page, '<span class="project-date">')
        self.assertNotContains(page, '<button type="button" class="project-date"')

    def test_the_menu_offers_the_date_in_demo_mode_and_the_trash_does_not(self):
        # The two project writes genuinely differ here, which is why the
        # menu wrapper's own condition is `not stale` and the trash carries
        # the demo-mode one itself.
        self.given_session_plan()
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'data-action="reschedule-project"')
        self.assertNotContains(page, 'data-action="trash-project"')
        # So the menu is rendered for a session plan, which it was not
        # before — #284's wrapper was `not demo_mode and not stale`.
        self.assertContains(page, '<span class="task-menu project-menu">')

    @override_settings(DEMO_MODE=False)
    def test_production_offers_both(self):
        cache.clear()
        self.addCleanup(cache.clear)
        page = self.production_dashboard()
        self.assertContains(page, 'data-action="reschedule-project"')
        self.assertContains(page, 'data-action="trash-project"')

    @override_settings(DEMO_MODE=False)
    def test_a_stale_render_offers_neither(self):
        cache.clear()
        self.addCleanup(cache.clear)
        page = self.production_dashboard(stale=True)
        self.assertNotContains(page, 'data-action="reschedule-project"')
        self.assertNotContains(page, 'data-action="trash-project"')

    def test_the_endpoint_and_the_markup_agree_about_an_example_project(self):
        # The same rule from both sides, so a click never has to be
        # interpreted: no control, and a refusal if a POST arrives anyway.
        response = self.client.post(
            reverse("reschedule_project", args=["demo-1"]),
            data=json.dumps(
                {
                    "date": (date.today() + timedelta(days=30)).isoformat(),
                    "move_tasks": False,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_the_menu_item_drives_the_control_rather_than_a_second_picker(self):
        # The same route "Datum ändern" takes to a task's own date: it
        # clicks the button, so the picker and the confirmation bar behind
        # it are not written a second time.
        self.given_session_plan()
        self.assertContains(
            self.client.get(reverse("dashboard")),
            "item.closest('.project-section').querySelector('button.project-date')?.click();",
        )

    def test_the_item_does_not_take_focus_back_off_the_picker(self):
        # "Datum ändern" and "Umbenennen" both hand focus to what they open;
        # the project's date is the third of that kind.
        self.given_session_plan()
        self.assertContains(
            self.client.get(reverse("dashboard")),
            "!['reschedule', 'reschedule-project', 'rename'].includes(action)",
        )

    def test_a_moment_keeps_the_span_and_names_what_it_removed(self):
        # The add row's answer rather than the reschedule's, because the
        # question carries a count: a moment renders every task due by then
        # as done, so the number the page could name is not the number the
        # write would move (reschedule_project_view). Named rather than
        # silently dropped, which is #244's rule in _task_actions_menu.html.
        self.given_session_plan()
        self.given_timelapse_moments("2026-09-01")
        session = self.client.session
        session["demo_sim_date"] = "2026-09-01"
        session.save()
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, '<span class="project-date">')
        self.assertNotContains(page, '<button type="button" class="project-date"')
        self.assertNotContains(page, 'data-action="reschedule-project"')
        self.assertContains(
            page, "Im simulierten Zeitpunkt nicht verfügbar: Termin ändern."
        )

    @override_settings(DEMO_MODE=False)
    def test_a_project_with_no_date_keeps_the_span(self):
        # get_upcoming_projects filters on Status/Aufgaben alone, so a
        # project Notion holds with no Termin reaches the page — and its
        # display date is the empty string. A button there would be an
        # invisible but tab-reachable control with no difference to measure
        # a shift against, so every pick could only be dropped.
        cache.clear()
        self.addCleanup(cache.clear)
        project = _fake_upcoming_project_with_task()
        project["event_date"] = None
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            page = self.client.get(reverse("dashboard"))
        self.assertContains(page, '<span class="project-date">')
        self.assertNotContains(page, '<button type="button" class="project-date"')


class ProjectDateConfirmsTheShiftTest(DemoModeTestCase):
    """The confirmation is a bar under the header, not a modal — #239
    rejected modals for this page outright, and the ⋮ menu's two-click
    arming cannot express a three-way answer."""

    def test_the_count_comes_from_the_server(self):
        # KanbanCountsComeFromTheServerTest's rule, one level up: the bar
        # names how many tasks would move, and the server is the one place
        # that gets to say what a count means (#210).
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Offen",
                    "date": (date.today() + timedelta(days=7)).isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Erledigt",
                    "date": (date.today() + timedelta(days=8)).isoformat(),
                    "done": True,
                },
            ]
        )
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'data-shift-count="1"')
        # Not counted off the rendered rows in JavaScript — the bar reads
        # the attribute the server wrote.
        self.assertNotContains(page, ".task-row:not(.done)")

    def test_an_undated_open_task_is_not_in_the_count(self):
        # It is open and it will not move, so naming it would make the bar
        # promise a shift the write cannot perform — which is why the count
        # is not total_count - done_count (_annotate_tasks).
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Mit Termin",
                    "date": (date.today() + timedelta(days=7)).isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Ohne Termin",
                    "date": None,
                    "done": False,
                },
            ]
        )
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, 'data-shift-count="1"')

    def test_the_bar_offers_both_answers_and_a_way_out(self):
        self.given_session_plan()
        page = self.client.get(reverse("dashboard"))
        for marker in (
            '<div class="project-date-confirm" role="status" hidden>',
            'class="project-date-confirm-move">Mit Aufgaben<',
            'class="project-date-confirm-only">Nur Termin<',
            'class="project-date-confirm-cancel"',
        ):
            with self.subTest(marker=marker):
                self.assertContains(page, marker)

    def test_the_label_names_the_difference_rather_than_the_new_date(self):
        # A date would mean a second German date formatter in JavaScript,
        # and task_add_row.js already carries the one date_format.py exists
        # to keep single.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("function projectShiftLabel(shiftCount, days) {", page)
        self.assertIn("${tasks} um ${span} ${verb}?", page)
        self.assertIn("'mitverschieben' : 'vorziehen'", page)

    def test_the_label_pluralises_both_halves(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("'1 Tag' : `${Math.abs(days)} Tage`", page)
        # No "alle": the count is the open tasks that carry a date, and a
        # plan holding an undated one alongside them has open tasks this
        # move does not reach.
        self.assertIn("'1 offene Aufgabe' : `${shiftCount} offene Aufgaben`", page)
        self.assertNotIn("Alle ${shiftCount}", page)

    def test_a_plan_with_nothing_open_asks_nothing(self):
        # The question would have no second answer: "Nur Termin" is what
        # every answer would mean.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("if (shiftCount === 0) {", page)
        self.assertIn("commitProjectDate(dateEl, iso, false, dateEl);", page)

    def test_the_answer_carries_the_count_it_was_given_for(self):
        # So the server can refuse a write the figure no longer describes
        # rather than moving more tasks than were agreed to.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("shift_count: Number(dateEl.dataset.shiftCount) || 0", page)

    def test_a_stale_count_re_asks_instead_of_writing(self):
        # 409: nothing was written, and the question goes back up with the
        # figure the server found. The flash points at the text that
        # changed, which is where action_feedback.js says an answer belongs.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("response.status === 409", page)
        self.assertIn("dateEl.dataset.shiftCount = String(body.shift_count);", page)
        self.assertIn("if (bar) openProjectDateConfirm(dateEl, bar, iso);", page)

    def test_a_partial_failure_reloads_rather_than_offering_the_answer_again(self):
        # The project date is already in Notion and the server dropped the
        # entry the delta is read off, so a second answer could only 404.
        # Reloaded once the flash has been seen instead, so the page and
        # Notion agree again.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("if (body?.partial) {", page)
        self.assertIn(
            "setTimeout(() => window.location.reload(), ACTION_FAILED_MS);", page
        )

    def test_escape_answers_from_where_the_focus_is(self):
        # The pick is made on the date button in the header and the picker
        # hands focus straight back to it, so a handler on the bar itself
        # would only answer after a Tab into it.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(
            "bar.closest('.project-section').addEventListener('keydown'", page
        )

    def test_the_bar_announces_itself(self):
        # .sim-lock-notice's own shape: a notice that appears, so un-hiding
        # it is the change a screen reader has to hear.
        self.assertContains(
            self.client.get(reverse("dashboard")),
            '<div class="project-date-confirm" role="status" hidden>',
        )

    def test_the_bar_says_it_is_saving(self):
        # #198: one project write plus one per task, in sequence — the
        # slowest write on the page by some distance, and the answer must
        # not be given twice while it runs.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("bar.classList.add('pending');", page)
        self.assertIn("bar.setAttribute('aria-busy', 'true');", page)
        self.assertIn(".project-date-confirm.pending { opacity: 0.6; }", page)


class ProjectDateReusesThePickerTest(DemoModeTestCase):
    """#283: the project's date is asked for with the same picker every task
    date goes through — the second caller of openTaskDatePicker() with no
    task id, after the add row (#279). Nothing in the module changes for it,
    and nothing about the swap is written here a second time."""

    TEMPLATE = Path(settings.BASE_DIR) / "projects/templates/projects/dashboard.html"
    PICKER = Path(settings.BASE_DIR) / "projects/static/projects/js/task_date_picker.js"

    def test_the_page_calls_the_shared_swap(self):
        self.assertIn(
            "openTaskDatePicker(dateEl, iso => "
            "openProjectDateConfirm(dateEl, bar, iso));",
            self.TEMPLATE.read_text(),
        )

    def test_the_bar_is_looked_up_before_the_swap_detaches_the_button(self):
        # The same trap bindTaskDatePickers() hands its `row` in to avoid:
        # the picker replaces the display element with the input *before*
        # onPick runs, so closest() called on it in there walks up from a
        # detached node and finds nothing. Found in the browser — the bar
        # never opened and the exception was swallowed by the picker's own
        # try/finally.
        source = self.TEMPLATE.read_text()
        binding = source[
            source.index("function bindProjectDatePickers()") : source.index(
                "function projectShiftLabel("
            )
        ]
        self.assertIn(
            "const bar = dateEl.closest('.project-section')"
            ".querySelector('.project-date-confirm');",
            binding,
        )
        # And not inside the callback, where it is too late.
        confirm = source[
            source.index("function openProjectDateConfirm(") : source.index(
                "function closeProjectDateConfirm("
            )
        ]
        self.assertNotIn("closest(", confirm)

    def test_the_page_defines_no_second_swap(self):
        # The three things the swap is made of, none of which may be
        # retyped here: the date input itself, the call that opens it, and
        # the modality flag #200 and #257 took two attempts each to get
        # right. (The page does build a text input — startRename's, which
        # is a different control entirely — so the date input is named by
        # its type rather than by createElement.)
        source = self.TEMPLATE.read_text()
        for copied in ("input.type = 'date'", "showPicker()", "lastInputWasKeyboard ="):
            with self.subTest(copied=copied):
                self.assertNotIn(copied, source)

    def test_the_module_still_has_exactly_one_swap(self):
        picker = self.PICKER.read_text()
        self.assertEqual(picker.count("input.showPicker();"), 1)
        self.assertEqual(picker.count("document.createElement('input')"), 1)

    def test_the_project_date_is_outside_the_task_id_contract(self):
        # bindTaskDatePickers() binds on .task-due[data-task-id]; a project
        # date carrying either would be claimed by it and handed to a task
        # reschedule with no id.
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('class="project-date" data-task-id', page)
        self.assertIn(
            "document.querySelectorAll('button.project-date[data-project-id]')", page
        )
