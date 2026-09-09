"""The week surface: the date helpers behind it, the progress bar and the
per-day columns."""

from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

from django.core.cache import cache
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..dates import (
    is_same_iso_week,
    iso_week_bounds,
)
from ..notion import NotionUnavailableError
from ..views import (
    _annotate_tasks,
    _bucket_by_day,
    _build_week_view,
    _count_done_in_range,
    _parse_week_param,
)
from .base import (
    DemoModeTestCase,
    _fake_upcoming_project,
    _summary_data,
)


class IsSameIsoWeekTest(SimpleTestCase):
    """#169: is_same_iso_week compares the (ISO year, ISO week) tuple, not
    the bare week number — that's what has to hold across a year boundary."""

    def test_same_week_different_weekday(self):
        self.assertTrue(is_same_iso_week(date(2026, 6, 15), date(2026, 6, 21)))

    def test_adjacent_week_is_not_the_same(self):
        self.assertFalse(is_same_iso_week(date(2026, 6, 15), date(2026, 6, 22)))

    def test_a_date_is_always_in_its_own_week(self):
        self.assertTrue(is_same_iso_week(date(2026, 6, 15), date(2026, 6, 15)))

    def test_year_boundary_dec_31_and_jan_1_can_share_an_iso_week(self):
        # 2025-12-31 is a Wednesday, ISO week 1 of 2026 — bare week numbers
        # alone (both "1") would wrongly equate it with Jan 2027's week 1 too.
        self.assertTrue(is_same_iso_week(date(2025, 12, 31), date(2026, 1, 1)))

    def test_bare_week_number_collision_across_years_is_rejected(self):
        # Both fall in "week 1" of their respective ISO years, a year apart —
        # comparing only the week number would wrongly say True.
        self.assertFalse(is_same_iso_week(date(2025, 1, 1), date(2026, 1, 1)))


class IsoWeekBoundsTest(SimpleTestCase):
    """#19: the Monday-Sunday range a date range query (the week bar, later
    #180's per-day columns) needs — same week definition as is_same_iso_week."""

    def test_returns_monday_and_sunday_of_the_containing_week(self):
        # 2026-06-15 is itself a Monday (see AnnotateTasksTest).
        self.assertEqual(
            iso_week_bounds(date(2026, 6, 18)), (date(2026, 6, 15), date(2026, 6, 21))
        )

    def test_a_monday_is_its_own_start(self):
        self.assertEqual(
            iso_week_bounds(date(2026, 6, 15)), (date(2026, 6, 15), date(2026, 6, 21))
        )

    def test_a_sunday_is_its_own_end(self):
        self.assertEqual(
            iso_week_bounds(date(2026, 6, 21)), (date(2026, 6, 15), date(2026, 6, 21))
        )


class AnnotateTasksTest(SimpleTestCase):
    TODAY = date(2026, 6, 15)

    def annotate(self, *tasks):
        project = {
            "tasks": [
                {"name": "Aufgabe", "kontext": "Büro", "done": False, "due": None, **t}
                for t in tasks
            ]
        }
        return _annotate_tasks([project], self.TODAY)[0]

    def urgency_for(self, **task):
        return self.annotate(task)["tasks"][0]["urgency"]

    def test_done_task_is_done(self):
        self.assertEqual(
            self.urgency_for(done=True, due=self.TODAY - timedelta(days=1)), "done"
        )

    def test_open_task_without_due_date_is_undated(self):
        # #160: an open task with no due date must not render as done.
        self.assertEqual(self.urgency_for(due=None), "undated")

    def test_done_task_without_due_date_is_done(self):
        # done wins over undated — the checkbox state is what counts.
        self.assertEqual(self.urgency_for(done=True, due=None), "done")

    def test_past_due_is_overdue(self):
        self.assertEqual(
            self.urgency_for(due=self.TODAY - timedelta(days=1)), "overdue"
        )

    def test_due_today_is_today(self):
        # #160: due today is its own level, distinct from "urgent".
        self.assertEqual(self.urgency_for(due=self.TODAY), "today")

    def test_due_tomorrow_is_urgent(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=1)), "urgent")

    def test_seven_days_out_is_ok_not_urgent(self):
        # #169: calendar-week based, not a rolling 7-day window. TODAY is a
        # Monday (see is_same_iso_week helper tests below), so +7 days lands
        # on the same weekday next ISO week — always a different week.
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=7)), "ok")

    def test_eight_days_out_is_ok(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=8)), "ok")

    def test_due_later_this_same_calendar_week_is_urgent(self):
        # TODAY is a Monday — Sunday is the last day of its ISO week.
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=6)), "urgent")

    def test_overdue_beats_urgent_on_the_project(self):
        project = self.annotate(
            {"due": self.TODAY + timedelta(days=2)},
            {"due": self.TODAY - timedelta(days=2)},
        )
        self.assertEqual(project["urgency"], "overdue")

    def test_a_today_task_lifts_the_project_to_today(self):
        project = self.annotate(
            {"due": self.TODAY},
            {"due": self.TODAY + timedelta(days=30)},
        )
        self.assertEqual(project["urgency"], "today")

    def test_overdue_beats_today_on_the_project(self):
        project = self.annotate(
            {"due": self.TODAY},
            {"due": self.TODAY - timedelta(days=2)},
        )
        self.assertEqual(project["urgency"], "overdue")

    def test_today_beats_urgent_on_the_project(self):
        project = self.annotate(
            {"due": self.TODAY + timedelta(days=2)},
            {"due": self.TODAY},
        )
        self.assertEqual(project["urgency"], "today")

    def test_project_without_open_work_stays_ok(self):
        project = self.annotate({"due": self.TODAY + timedelta(days=30)})
        self.assertEqual(project["urgency"], "ok")

    def test_a_project_with_only_undated_tasks_stays_ok(self):
        # No date means no deadline pressure — undated never lifts the
        # project urgency.
        project = self.annotate({"due": None})
        self.assertEqual(project["urgency"], "ok")

    def test_no_formatted_date_is_written_onto_the_task(self):
        # #189: the formatted string used to be set here, which put it in
        # the dashboard cache and froze the format for up to CACHE_TTL.
        # Templates format task.due themselves now.
        task = self.annotate({"due": date(2026, 6, 15)})["tasks"][0]
        self.assertNotIn("due_display", task)

    def test_done_count_and_total_count_for_a_mixed_set(self):
        project = self.annotate(
            {"done": True, "due": None},
            {"done": False, "due": self.TODAY + timedelta(days=1)},
            {"done": False, "due": self.TODAY - timedelta(days=1)},
        )
        self.assertEqual(project["done_count"], 1)
        self.assertEqual(project["total_count"], 3)

    def test_a_dateless_undone_task_does_not_count_as_done(self):
        # done_count comes from task["done"] directly — since #160 the
        # urgency is "undated" anyway, but the count must not depend on it.
        project = self.annotate({"done": False, "due": None})
        self.assertEqual(project["done_count"], 0)
        self.assertEqual(project["total_count"], 1)

    def test_ring_dashoffset_at_half_done(self):
        project = self.annotate(
            {"done": True, "due": None},
            {"done": False, "due": self.TODAY + timedelta(days=1)},
        )
        self.assertEqual(project["ring_dashoffset"], "21.99")

    def test_ring_dashoffset_fully_done(self):
        project = self.annotate({"done": True, "due": None})
        self.assertEqual(project["ring_dashoffset"], "0.00")

    def test_ring_dashoffset_nothing_done(self):
        project = self.annotate({"done": False, "due": self.TODAY + timedelta(days=1)})
        self.assertEqual(project["ring_dashoffset"], "43.98")

    def test_ring_dashoffset_with_no_tasks_is_a_full_empty_ring(self):
        project = self.annotate()
        self.assertEqual(project["total_count"], 0)
        self.assertEqual(project["ring_dashoffset"], "43.98")

    # --- #140: tasks come out in chronological order ---

    def names(self, project):
        return [t["name"] for t in project["tasks"]]

    def test_tasks_are_sorted_by_due_date(self):
        project = self.annotate(
            {"name": "Spät", "due": self.TODAY + timedelta(days=9)},
            {"name": "Früh", "due": self.TODAY + timedelta(days=1)},
            {"name": "Mittel", "due": self.TODAY + timedelta(days=5)},
        )
        self.assertEqual(self.names(project), ["Früh", "Mittel", "Spät"])

    def test_dateless_tasks_go_to_the_end(self):
        project = self.annotate(
            {"name": "Ohne Datum", "due": None},
            {"name": "Mit Datum", "due": self.TODAY + timedelta(days=1)},
        )
        self.assertEqual(self.names(project), ["Mit Datum", "Ohne Datum"])

    def test_done_tasks_stay_at_their_date_position(self):
        # `done` must not be part of the sort key: task_refs in the cached
        # summary are positions in this order (_number_projects_and_tasks),
        # so a toggle must not move a task. See ai.py.
        project = self.annotate(
            {"name": "Später offen", "due": self.TODAY + timedelta(days=5)},
            {
                "name": "Früher erledigt",
                "done": True,
                "due": self.TODAY + timedelta(days=1),
            },
        )
        self.assertEqual(self.names(project), ["Früher erledigt", "Später offen"])

    def test_equal_dates_keep_their_relative_order(self):
        due = self.TODAY + timedelta(days=3)
        project = self.annotate(
            {"name": "Zuerst", "due": due},
            {"name": "Danach", "due": due},
        )
        self.assertEqual(self.names(project), ["Zuerst", "Danach"])


class BuildWeekViewTest(SimpleTestCase):
    """#53: the flat Heute/Diese-Woche work surface, built from tasks whose
    urgency _annotate_tasks already classified — not re-derived dates, so
    classification stays in exactly one place."""

    TODAY = date(2026, 6, 15)  # a Monday, see AnnotateTasksTest

    def _annotated(self, *tasks, name="P"):
        project = {
            "id": "p1",
            "display_name": name,
            "tasks": [
                {"name": "Aufgabe", "kontext": [], "done": False, "due": None, **t}
                for t in tasks
            ],
        }
        return _annotate_tasks([project], self.TODAY)[0]

    def test_overdue_and_today_are_separated(self):
        project = self._annotated(
            {"name": "Überfällig", "due": self.TODAY - timedelta(days=1)},
            {"name": "Heute fällig", "due": self.TODAY},
        )
        result = _build_week_view([project], [])
        self.assertEqual([t["name"] for t in result["overdue"]], ["Überfällig"])
        self.assertEqual([t["name"] for t in result["today"]], ["Heute fällig"])

    def test_rest_of_the_calendar_week_is_urgent(self):
        # TODAY is a Monday — Sunday is the last day of its ISO week.
        project = self._annotated(
            {"name": "Diese Woche", "due": self.TODAY + timedelta(days=6)}
        )
        result = _build_week_view([project], [])
        self.assertEqual([t["name"] for t in result["urgent"]], ["Diese Woche"])

    def test_next_week_and_done_and_undated_tasks_are_excluded(self):
        project = self._annotated(
            {"name": "Nächste Woche", "due": self.TODAY + timedelta(days=8)},
            {"name": "Erledigt", "due": self.TODAY, "done": True},
            {"name": "Ohne Datum", "due": None},
        )
        result = _build_week_view([project], [])
        self.assertEqual(result["overdue"] + result["today"] + result["urgent"], [])

    def test_unassigned_tasks_carry_no_project_and_are_labelled(self):
        unassigned = _annotate_tasks(
            [
                {
                    "id": "_unassigned",
                    "tasks": [
                        {
                            "name": "Blumen",
                            "kontext": [],
                            "done": False,
                            "due": self.TODAY,
                        }
                    ],
                }
            ],
            self.TODAY,
        )[0]["tasks"]
        result = _build_week_view([], unassigned)
        [task] = result["today"]
        self.assertIsNone(task["project_id"])
        self.assertEqual(task["project_name"], "Ohne Projekt")

    def test_tasks_across_projects_are_sorted_by_due_date(self):
        early = self._annotated(
            {"name": "Früh", "due": self.TODAY + timedelta(days=1)}, name="A"
        )
        late = self._annotated(
            {"name": "Spät", "due": self.TODAY + timedelta(days=5)}, name="B"
        )
        result = _build_week_view([late, early], [])
        self.assertEqual([t["name"] for t in result["urgent"]], ["Früh", "Spät"])


class CountDoneInRangeTest(SimpleTestCase):
    """#19: the counting helper behind the week progress bar, shared with
    #180's per-day indicator (same function, a narrower range). A task
    counts toward `total` if its due date falls in the range OR it was
    actually completed in the range — an overdue task from outside the
    range that finally gets cleared inside it still counts as done, the
    exact case #19's Notion addendum added completed_date to capture — while
    keeping done a subset of total (no task counts as done without also
    counting toward total, so the bar can never show more than 100%)."""

    def _task(self, due=None, completed_date=None, done=None, **overrides):
        # done defaults to "whatever completed_date implies" so most cases
        # below don't have to spell out both — the one test that needs them
        # to diverge (done=True, no completed_date) passes done explicitly.
        return {
            "due": due,
            "completed_date": completed_date,
            "done": bool(completed_date) if done is None else done,
            **overrides,
        }

    def test_a_task_due_in_range_and_done_counts_both(self):
        tasks = [self._task(due=date(2026, 6, 16), completed_date=date(2026, 6, 16))]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 15), date(2026, 6, 21)), (1, 1)
        )

    def test_a_task_due_in_range_but_not_done_counts_toward_total_only(self):
        tasks = [self._task(due=date(2026, 6, 16), completed_date=None)]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 15), date(2026, 6, 21)), (0, 1)
        )

    def test_an_overdue_task_completed_inside_the_range_counts_as_done(self):
        # Due last week, completed this week — the case the old Wann?-only
        # proxy couldn't see and the addendum's completed_date now can.
        tasks = [self._task(due=date(2026, 6, 8), completed_date=date(2026, 6, 16))]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 15), date(2026, 6, 21)), (1, 1)
        )

    def test_a_task_outside_the_range_entirely_is_not_counted(self):
        tasks = [self._task(due=date(2026, 6, 1), completed_date=date(2026, 6, 2))]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 15), date(2026, 6, 21)), (0, 0)
        )

    def test_a_task_due_in_range_but_completed_on_a_different_day_still_counts_as_done(
        self,
    ):
        # #180's per-day columns call this with start==end==one day. A task
        # due that day but checked off a day late (or early) is still done —
        # the card itself renders struck through — so the badge must not
        # undercount it just because completed_date lands outside that
        # single-day window.
        tasks = [self._task(due=date(2026, 6, 16), completed_date=date(2026, 6, 17))]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 16), date(2026, 6, 16)), (1, 1)
        )

    def test_a_task_done_with_no_completed_date_still_counts_as_done(self):
        # A task checked off before "Erledigt am" existed in the Notion
        # schema, or checked off directly in Notion instead of through this
        # app, has done=True but no completed_date. It's relevant here via
        # its due date, same as any open task — the card already renders it
        # struck through, so the badge must not silently disagree.
        tasks = [self._task(due=date(2026, 6, 16), completed_date=None, done=True)]
        self.assertEqual(
            _count_done_in_range(tasks, date(2026, 6, 15), date(2026, 6, 21)), (1, 1)
        )

    def test_no_tasks_is_a_clean_zero_not_a_division_by_zero(self):
        self.assertEqual(
            _count_done_in_range([], date(2026, 6, 15), date(2026, 6, 21)), (0, 0)
        )


class BucketByDayTest(SimpleTestCase):
    """#180: day columns build on top of #53's flat "Diese Woche" — tasks
    grouped by weekday within a given week, independent of urgency (a
    browsed week may not be the current one, where overdue/today/urgent
    don't apply)."""

    MONDAY = date(2026, 6, 15)  # see AnnotateTasksTest

    def _project(self, *tasks):
        return {
            "id": "p1",
            "display_name": "P",
            "tasks": [
                {
                    "name": "Aufgabe",
                    "kontext": [],
                    "done": False,
                    "due": None,
                    "completed_date": None,
                    **t,
                }
                for t in tasks
            ],
        }

    def test_tasks_land_on_their_own_weekday(self):
        project = self._project(
            {"name": "Montag", "due": self.MONDAY},
            {"name": "Sonntag", "due": self.MONDAY + timedelta(days=6)},
        )
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual(len(days), 7)
        self.assertEqual([t["name"] for t in days[0]["tasks"]], ["Montag"])
        self.assertEqual([t["name"] for t in days[6]["tasks"]], ["Sonntag"])

    def test_a_task_the_day_before_the_week_starts_is_excluded(self):
        project = self._project(
            {"name": "Vorwoche", "due": self.MONDAY - timedelta(days=1)}
        )
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual(sum(len(d["tasks"]) for d in days), 0)

    def test_undated_tasks_land_on_no_day(self):
        project = self._project({"name": "Ohne Datum", "due": None})
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual(sum(len(d["tasks"]) for d in days), 0)

    def test_unassigned_tasks_are_tagged_ohne_projekt(self):
        unassigned = [
            {
                "name": "Kleinkram",
                "kontext": [],
                "done": False,
                "due": self.MONDAY,
                "completed_date": None,
            }
        ]
        days = _bucket_by_day([], unassigned, self.MONDAY)
        self.assertEqual(days[0]["tasks"][0]["project_name"], "Ohne Projekt")
        self.assertIsNone(days[0]["tasks"][0]["project_id"])

    def test_a_day_with_zero_tasks_has_a_clean_zero_count(self):
        days = _bucket_by_day([], [], self.MONDAY)
        for day in days:
            self.assertEqual((day["done_count"], day["total_count"]), (0, 0))

    def test_per_day_done_total_counts_done_tasks_on_that_day(self):
        project = self._project(
            {
                "name": "Erledigt",
                "due": self.MONDAY,
                "done": True,
                "completed_date": self.MONDAY,
            },
            {"name": "Offen", "due": self.MONDAY},
        )
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual((days[0]["done_count"], days[0]["total_count"]), (1, 2))

    def test_a_task_checked_off_a_day_late_still_counts_as_done_on_its_due_day(self):
        # The card renders struck through under its due day regardless of
        # when it was actually completed (task["done"] is what the template
        # reads) — the done_count badge above it must agree.
        project = self._project(
            {
                "name": "Verspätet erledigt",
                "due": self.MONDAY,
                "done": True,
                "completed_date": self.MONDAY + timedelta(days=1),
            }
        )
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual((days[0]["done_count"], days[0]["total_count"]), (1, 1))

    def test_a_task_done_before_erledigt_am_existed_still_counts_as_done(self):
        # Notion's "Done" checkbox predates "Erledigt am" — a task checked
        # off before the property existed, or checked off directly in
        # Notion instead of through this app, has done=True but no
        # completed_date. Same card, same struck-through rendering — the
        # badge must not disagree just because the newer property is empty.
        project = self._project(
            {
                "name": "Alt erledigt",
                "due": self.MONDAY,
                "done": True,
                "completed_date": None,
            }
        )
        days = _bucket_by_day([project], [], self.MONDAY)
        self.assertEqual((days[0]["done_count"], days[0]["total_count"]), (1, 1))


class ParseWeekParamTest(SimpleTestCase):
    """#180: ?week=2026-W37 navigates to that week; anything unparseable
    falls back to the given default rather than erroring the page."""

    DEFAULT = date(2026, 6, 15)

    def _request(self, week=None):
        factory = RequestFactory()
        return factory.get("/dashboard/", {"week": week} if week else {})

    def test_valid_week_param_wins(self):
        result = _parse_week_param(self._request("2026-W25"), self.DEFAULT)
        self.assertEqual(result, date(2026, 6, 15))

    def test_absent_param_falls_back_to_default(self):
        result = _parse_week_param(self._request(), self.DEFAULT)
        self.assertEqual(result, self.DEFAULT)

    def test_malformed_param_falls_back_to_default(self):
        for bad in ["not-a-week", "2026-W99", "2026", "'; DROP TABLE", ""]:
            with self.subTest(bad=bad):
                result = _parse_week_param(self._request(bad), self.DEFAULT)
                self.assertEqual(result, self.DEFAULT)

    def test_a_week_against_the_calendars_edge_falls_back_to_default(self):
        # #216: parseable but unusable — _bucket_by_day walks six days
        # forward from this Monday and dashboard() reaches a week either
        # side, so both ends overflow date.min/date.max instead of
        # rendering. 9999-W52 starts on 9999-12-27, four days from the end.
        for edge in ["9999-W52", "0001-W01"]:
            with self.subTest(edge=edge):
                result = _parse_week_param(self._request(edge), self.DEFAULT)
                self.assertEqual(result, self.DEFAULT)


class TodayViewHiddenForASessionPlanTest(DemoModeTestCase):
    """#240: "Heute" is hidden for a demo session plan while the view is
    being worked on. In its current shape it is a cross-project surface —
    overdue, due today, then the week — and a session plan is one project,
    so it re-sorted the tasks the dashboard above had just shown under three
    more headings. The sidebar drops the entry with it (_sidebar_nav.html),
    since a toggle with nothing to toggle is a dead link that also throws.

    A hold rather than a verdict, and asserted so that it stays one: a view
    that quietly never comes back is indistinguishable from a view nobody
    decided about. Both states are pinned here — hidden for a session plan,
    rendered wherever the projects it spans exist — so returning it is a
    visible change to this class rather than a silent drift."""

    def test_a_session_plan_gets_no_today_view(self):
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('id="view-today"', html)
        self.assertIn('id="view-overview"', html)

    def test_the_sidebar_offers_no_entry_for_a_view_that_is_not_there(self):
        """Or the toggle it calls throws on a missing element, which is a
        dead link that also breaks the page it is on."""
        self.given_session_plan()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('id="nav-today"', html)

    def test_the_demo_example_projects_keep_theirs(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('id="view-today"', html)
        self.assertIn('id="nav-today"', html)

    def test_a_kept_today_url_falls_back_instead_of_throwing(self):
        """A visitor who bookmarked ?view=today before planning, or who
        follows the demo group's link and then plans. The deep-link sync
        checks for the panel rather than assuming it, the same answer an
        unknown ?project= id already gets: render the overview."""
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?view=today")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('id="view-today"', html)
        self.assertIn("params.get('view') === 'today' && todayView", html)


@override_settings(DEMO_MODE=False)
class TodayViewRendersInProductionTest(TestCase):
    """The other side of TodayViewHiddenForASessionPlanTest: production
    spans every project at once, which is the whole point of the view."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_production_keeps_the_today_view(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('id="view-today"', html)
        self.assertIn('id="nav-today"', html)


@override_settings(DEMO_MODE=False)
class TodayWeekViewProductionTest(TestCase):
    """#53: the Heute/Diese-Woche work surface in production — the
    unassigned-tasks read is independent of the project read, with its own
    cache and its own graceful degradation on a Notion failure."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_unassigned_task_renders_under_ohne_projekt(self):
        task = {
            "id": "u-1",
            "name": "Blumen besorgen",
            "due": date.today(),
            "done": False,
            "kontext": [],
        }
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[task]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Blumen besorgen")
        self.assertContains(response, "Ohne Projekt")

    def test_unassigned_read_failure_degrades_to_no_unassigned_tasks(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch(
                "projects.views.get_unassigned_tasks",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Ohne Projekt")

    def test_the_sidebar_offers_the_today_week_view(self):
        # A bare assertContains(response, "showToday()") or "Heute" also
        # matches the function's own definition and the hidden view-today
        # heading regardless of whether the sidebar link exists — it missed
        # the #183 regression where _sidebar_nav.html's production branch
        # dropped nav-overview/nav-today entirely. Assert on the actual
        # clickable link instead.
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, '<a class="sidebar-item active" id="nav-overview"'
        )
        self.assertContains(response, 'id="nav-today" onclick="showToday()"')


@override_settings(DEMO_MODE=False)
class WeekProgressBarProductionTest(TestCase):
    """#19: the "Diese Woche" bar is server-rendered from week_done_count /
    week_total_count — not recomputed client-side from kanban-card classes,
    which never were week-scoped."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_bar_shows_the_week_scoped_counts(self):
        today = date.today()
        project = _fake_upcoming_project()
        project["tasks"] = [
            {
                "id": "t-done",
                "name": "Erledigt",
                "due": today,
                "done": True,
                "kontext": [],
                "completed_date": today,
            },
            {
                "id": "t-open",
                "name": "Offen",
                "due": today,
                "done": False,
                "kontext": [],
                "completed_date": None,
            },
        ]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Diese Woche")
        self.assertContains(response, "1 / 2 erledigt")

    def test_a_task_done_with_no_completed_date_still_counts(self):
        # The real-world gap this covers: a task marked Done in Notion
        # before "Erledigt am" existed, or checked off directly in Notion's
        # own UI, carries done=True with no completed_date. It still renders
        # struck through in the Erledigt column, so the bar above it must
        # count it too instead of reporting it as open.
        today = date.today()
        project = _fake_upcoming_project()
        project["tasks"] = [
            {
                "id": "t-legacy-done",
                "name": "Alt erledigt",
                "due": today,
                "done": True,
                "kontext": [],
                "completed_date": None,
            },
        ]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "1 / 1 erledigt")

    def test_zero_tasks_this_week_is_a_clean_blank_not_a_crash(self):
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "erledigt</span>")

    def test_the_bar_excludes_unassigned_tasks_not_shown_on_the_kanban_board(self):
        # #182: the Kanban board only ever renders project.tasks — an
        # unassigned ("Ohne Projekt") task can never appear on it, so the
        # bar above it must not count one either, or the two numbers on the
        # same screen stop matching.
        today = date.today()
        project = _fake_upcoming_project()
        project["tasks"] = [
            {
                "id": "t-open",
                "name": "Offen",
                "due": today,
                "done": False,
                "kontext": [],
                "completed_date": None,
            },
        ]
        unassigned = [
            {
                "id": "t-unassigned",
                "name": "Kleinkram",
                "due": today,
                "done": False,
                "kontext": [],
                "completed_date": None,
            },
        ]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=unassigned),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "0 / 1 erledigt")


class WeekProgressBarDemoModeTest(DemoModeTestCase):
    """#182: reproduces the reported bug on the demo multi-project example
    view — get_demo_unassigned_tasks() feeds the same "Ohne Projekt" bucket
    the production get_unassigned_tasks() call does, through the same
    dashboard() code path and the same all_tasks expression."""

    def test_the_bar_excludes_demo_unassigned_tasks_not_shown_on_the_kanban_board(
        self,
    ):
        today = date.today()
        project = {
            "id": "demo-p1",
            "name": "Testkonzert",
            "event_date": today + timedelta(days=10),
            "tasks": [
                {
                    "id": "t-open",
                    "name": "Offen",
                    "due": today,
                    "done": False,
                    "kontext": [],
                    "completed_date": None,
                },
            ],
        }
        unassigned = [
            {
                "id": "demo-unassigned-1",
                "name": "Kleinkram",
                "due": today,
                "done": False,
                "kontext": [],
            },
        ]
        with (
            patch("projects.views.get_demo_projects", return_value=[project]),
            patch("projects.views.get_demo_unassigned_tasks", return_value=unassigned),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "0 / 1 erledigt")


class SessionPlanProgressBarTest(DemoModeTestCase):
    """#183 follow-up: for a session plan, the bar tracks the whole plan's
    completion instead of the current calendar week — a week-scoped count
    barely moved between Zeitreise moments and often sat at 0/0 several
    moments in a row, since a session plan's own tasks rarely all fall in
    one week. The whole-plan count does visibly progress: each Zeitreise
    moment marks every task due on/before it "done" (dashboard()'s deepcopy
    mutation), so scrubbing through moments now fills the bar moment to
    moment instead of resetting to an unrelated week's tiny subset."""

    def test_the_bar_counts_the_whole_plan_not_just_this_week(self):
        today = date.today()
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-long-past",
                    "name": "Lange her",
                    "date": (today - timedelta(days=60)).isoformat(),
                    "done": True,
                },
                {
                    "id": "t-this-week",
                    "name": "Diese Woche",
                    "date": today.isoformat(),
                    "done": False,
                },
                {
                    "id": "t-far-future",
                    "name": "Weit weg",
                    "date": (today + timedelta(days=90)).isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.get(reverse("dashboard"))
        # Week-scoped would have counted only "Diese Woche" (0/1) — the
        # whole plan is 1 done out of 3.
        self.assertContains(response, "1 / 3 erledigt")

    def test_the_label_says_projektfortschritt(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "<span>Projektfortschritt</span>")

    def test_the_bar_fills_further_at_a_later_zeitreise_moment(self):
        today = date.today()
        moment = (today + timedelta(days=30)).isoformat()
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-before-moment",
                    "name": "Vor dem Moment",
                    "date": (today + timedelta(days=10)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-after-moment",
                    "name": "Nach dem Moment",
                    "date": (today + timedelta(days=60)).isoformat(),
                    "done": False,
                },
            ]
        )
        self.given_timelapse_moments(moment)

        response_today = self.client.get(reverse("dashboard"))
        self.assertContains(response_today, "0 / 2 erledigt")

        session = self.client.session
        session["demo_sim_date"] = moment
        session.save()
        response_at_moment = self.client.get(reverse("dashboard"))
        # "Vor dem Moment" is due before the simulated date and counts as
        # done there (see dashboard()'s deepcopy mutation); "Nach dem
        # Moment" isn't due yet at that point in the story.
        self.assertContains(response_at_moment, "1 / 2 erledigt")


@override_settings(DEMO_MODE=False)
class DayColumnsProductionTest(TestCase):
    """#180: the day-column breakdown of "Diese Woche" — task placement and
    week navigation, end to end through the real dashboard() view."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_task_renders_in_its_own_day_column(self):
        monday = iso_week_bounds(date.today())[0]
        project = _fake_upcoming_project()
        project["tasks"] = [
            {
                "id": "t-1",
                "name": "Programm-Entwurf",
                "due": monday + timedelta(days=2),
                "done": False,
                "kontext": [],
                "completed_date": None,
            }
        ]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard") + "?view=today")
        self.assertContains(response, "Programm-Entwurf")
        self.assertContains(response, 'data-date="%s"' % (monday + timedelta(days=2)))

    def _day_columns_html(self, response):
        """The task also renders in the always-present kanban board and its
        own (hidden-by-default) project-section — this slices out just the
        day-columns markup so presence there specifically is what's checked.
        """
        content = response.content.decode()
        start = content.index('id="day-columns">')
        end = content.index('<div class="project-section"', start)
        return content[start:end]

    def test_week_param_navigates_to_a_different_week(self):
        monday = iso_week_bounds(date.today())[0]
        next_monday = monday + timedelta(days=7)
        project = _fake_upcoming_project()
        project["tasks"] = [
            {
                "id": "t-1",
                "name": "Nächste-Woche-Aufgabe",
                "due": next_monday,
                "done": False,
                "kontext": [],
                "completed_date": None,
            }
        ]
        iso_year, iso_week, _ = next_monday.isocalendar()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            this_week = self.client.get(reverse("dashboard") + "?view=today")
            next_week = self.client.get(
                reverse("dashboard") + f"?view=today&week={iso_year}-W{iso_week:02d}"
            )
        self.assertNotIn("Nächste-Woche-Aufgabe", self._day_columns_html(this_week))
        self.assertIn("Nächste-Woche-Aufgabe", self._day_columns_html(next_week))

    def test_malformed_week_param_does_not_500(self):
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(
                reverse("dashboard") + "?view=today&week=not-a-week"
            )
        self.assertEqual(response.status_code, 200)

    def test_a_week_param_against_the_calendars_edge_does_not_500(self):
        # #216: this one parses, so the malformed-param guard above never
        # saw it — ?week=9999-52 reached _bucket_by_day and took the whole
        # page down with an OverflowError.
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            for edge in ["9999-W52", "0001-W01"]:
                with self.subTest(edge=edge):
                    response = self.client.get(
                        reverse("dashboard") + f"?view=today&week={edge}"
                    )
                    self.assertEqual(response.status_code, 200)
