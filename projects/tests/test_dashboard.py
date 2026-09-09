"""The dashboard read path: what renders, in which column, from a warm or
cold cache."""

import re
from datetime import (
    UTC,
    date,
    datetime,
    timedelta,
)
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import (
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..ai import AIUnavailableError
from ..notion import NotionUnavailableError
from ..views import (
    _KANBAN_COLUMN,
    _URGENCY_RANK,
    CACHE_KEY,
    STALE_CACHE_KEY,
    STALE_UNASSIGNED_CACHE_KEY,
    SUMMARY_KEY,
    UNASSIGNED_CACHE_KEY,
    _annotate_tasks,
    _kanban_column,
)
from .base import (
    DemoModeTestCase,
    _fake_upcoming_project,
    _fake_upcoming_project_with_task,
    _summary_data,
)


class DashboardKanbanCssTest(DemoModeTestCase):
    def test_kanban_meta_has_gap(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "gap: 6px")

    def test_kanban_meta_first_child_selector_exists(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, ".kanban-card-meta span:first-child")

    def test_kanban_meta_last_child_selector_exists(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, ".kanban-card-meta span:last-child")


class DashboardAiFailureTest(DemoModeTestCase):
    """generate_weekly_summary is called from four different places in
    views.py (dashboard x2, my_plan, preload) — none of them guarded before
    #29. The dashboard must still show projects/tasks even when the AI card
    can't."""

    def test_multi_project_dashboard_degrades_without_a_summary(self):
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nicht verfügbar")

    def test_session_plan_dashboard_degrades_without_a_summary(self):
        self.given_session_plan()
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nicht verfügbar")

    def test_a_failure_is_not_cached_as_a_summary(self):
        """A later, healthy request must retry rather than replay a blank."""
        self.given_session_plan()
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        self.client.get(reverse("dashboard"))
        self.assertNotIn(f"{SUMMARY_KEY}_today", self.client.session)


class MidnightBoundaryUsesLocalDateTest(DemoModeTestCase):
    """#85: task urgency used to be computed from date.today(), which reads
    the container's system clock rather than settings.TIME_ZONE."""

    @patch("django.utils.timezone.now")
    def test_my_plan_urgency_follows_the_berlin_date(self, mock_now):
        # 23:30 UTC on 2026-01-15 is already 00:30 CET on 2026-01-16 — in
        # winter Berlin runs UTC+1, so its "today" is one day ahead here.
        mock_now.return_value = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-berlin-yesterday",
                    "name": "Gestern in Berlin fällig",
                    "date": "2026-01-15",
                    "kontext": "",
                    "done": False,
                },
                {
                    "id": "t-berlin-today",
                    "name": "Heute in Berlin fällig",
                    "date": "2026-01-16",
                    "kontext": "",
                    "done": False,
                },
            ]
        )
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, 'class="dot overdue"')
        self.assertContains(response, 'class="dot today"')
        self.assertNotContains(response, 'class="dot ok"')


class DemoDataBannerTest(DemoModeTestCase):
    """#7: nothing on the dashboard told a visitor the 5 example projects are
    sample data rather than something real. viewing_demo_data is true
    whenever demo mode is showing the fixtures, i.e. whenever
    has_session_plan is false."""

    def test_banner_shows_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Beispieldaten")

    def test_banner_shows_without_a_session_plan_under_multi_view(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Beispieldaten")

    def test_banner_hidden_while_viewing_the_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Beispieldaten")

    def test_banner_shows_for_the_example_projects_with_a_session_plan_too(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Beispieldaten")

    def test_banner_cta_links_to_the_planner(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("planner_start")}"')


class KontextBadgeTest(DemoModeTestCase):
    """kontext is a production-only concept (#18): demo mode neither collects
    nor derives it anymore, on any of these three paths, so none of them may
    render a .task-kontext badge."""

    def assertRendersNoBadge(self, response):
        # Not a bare "task-kontext" substring check: both templates define
        # the .task-kontext CSS rule unconditionally in their <style> block,
        # so only the rendered span markup tells the two cases apart.
        self.assertNotContains(response, 'class="task-kontext">')

    def test_the_multi_view_renders_no_kontext_badge(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertRendersNoBadge(response)

    def test_the_single_plan_view_renders_no_kontext_badge(self):
        self.given_session_plan()
        self.assertRendersNoBadge(self.client.get(reverse("dashboard")))

    def test_my_plan_renders_no_kontext_badge(self):
        self.given_session_plan()
        self.assertRendersNoBadge(self.client.get(reverse("my_plan")))


@override_settings(DEMO_MODE=False)
class ProductionKontextBadgeTest(TestCase):
    """The one path kontext still reaches after #18: real Notion data in
    production. Pins that it renders as a word — the live [&#x27;Büro&#x27;]
    bug _build_session_project used to cause elsewhere (#9) — now anchored
    on the sole surviving path instead of on demo fixtures."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_dashboard_renders_kontext_as_a_word(self):
        project = _fake_upcoming_project_with_task()
        project["tasks"][0]["kontext"] = ["Büro"]
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="task-kontext">Büro<')
        self.assertNotContains(response, "[&#x27;")


class DateUncertainBadgeTest(DemoModeTestCase):
    """The "Termin unsicher" badge only appears for a project whose date was
    a fallback guess (see PlannerCreateDatelessDescriptionTest), on both
    demo-mode surfaces that show a project's date."""

    def test_dashboard_shows_the_badge_for_an_uncertain_date(self):
        self.given_session_plan(event_date_uncertain=True)
        response = self.client.get(reverse("dashboard"))
        # Renders twice: once in the visible AI-card header next to
        # demo_project_date (what a visitor actually sees after generating a
        # plan), once in the .project-section this same project also gets
        # (hidden by default, revealed by the multi-project/timelapse
        # toggles) — a single assertContains here previously passed even
        # when only the hidden copy carried the badge.
        self.assertContains(response, 'class="date-uncertain-badge"', count=2)

    def test_dashboard_shows_no_badge_for_a_confirmed_date(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="date-uncertain-badge"')

    def test_my_plan_shows_the_badge_for_an_uncertain_date(self):
        self.given_session_plan(event_date_uncertain=True)
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, 'class="date-uncertain-badge"')


@override_settings(DEMO_MODE=False)
class ProductionDateUncertainBadgeTest(TestCase):
    """The production counterpart: a real Notion project whose "Termin
    unsicher" checkbox is set renders the same badge on the dashboard."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_dashboard_shows_the_badge_when_notion_flags_the_date_uncertain(self):
        project = _fake_upcoming_project()
        project["event_date_uncertain"] = True
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="date-uncertain-badge"')


class TaskSortOrderInViewsTest(DemoModeTestCase):
    """#140: the per-project task lists render chronologically — my_plan and
    the dashboard project section both go through _annotate_tasks, which now
    sorts in place."""

    def given_unsorted_plan(self):
        base = date.today()
        self.given_session_plan(
            tasks=[
                {
                    "id": f"demo-session-{i}",
                    "name": name,
                    "date": (base + timedelta(days=days)).isoformat(),
                    "kontext": "",
                    "done": False,
                }
                for i, (name, days) in enumerate(
                    [("Spätaufgabe", 20), ("Frühaufgabe", 2), ("Mittelaufgabe", 10)]
                )
            ]
        )

    def assert_chronological(self, html):
        positions = [
            html.index(name) for name in ("Frühaufgabe", "Mittelaufgabe", "Spätaufgabe")
        ]
        self.assertEqual(positions, sorted(positions))

    def test_my_plan_lists_tasks_in_date_order(self):
        self.given_unsorted_plan()
        response = self.client.get(reverse("my_plan"))
        self.assert_chronological(response.content.decode())

    def test_dashboard_project_section_lists_tasks_in_date_order(self):
        self.given_unsorted_plan()
        response = self.client.get(reverse("dashboard"))
        # Only from the project section on — the kanban columns above it
        # split the same tasks by urgency, which reorders first occurrences.
        html = response.content.decode()
        self.assert_chronological(html[html.index('class="project-section"') :])


class UndatedAndTodayUrgencyRenderingTest(DemoModeTestCase):
    """#160: an open task without a due date renders as "undated" — Offen
    column, neutral dot — instead of borrowing the done styling, and a task
    due today renders as "today", distinct from "urgent"."""

    def given_mixed_plan(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Ohne-Termin-Aufgabe",
                    "date": None,
                    "done": False,
                },
                {
                    "id": "demo-session-1",
                    "name": "Heute-Aufgabe",
                    "date": date.today().isoformat(),
                    "done": False,
                },
                {
                    "id": "demo-session-2",
                    "name": "Erledigt-Aufgabe",
                    "date": None,
                    "done": True,
                },
            ]
        )

    def kanban_columns(self):
        """Splits the dashboard HTML into (open, urgent, done) column slices."""
        html = self.client.get(reverse("dashboard")).content.decode()
        open_start = html.index('id="count-open"')
        urgent_start = html.index('id="count-urgent"')
        done_start = html.index('id="count-done"')
        end = html.index('class="project-section"')
        return (
            html[open_start:urgent_start],
            html[urgent_start:done_start],
            html[done_start:end],
        )

    def test_an_open_undated_task_lands_in_the_open_column(self):
        self.given_mixed_plan()
        open_col, _urgent_col, done_col = self.kanban_columns()
        self.assertIn("Ohne-Termin-Aufgabe", open_col)
        self.assertIn("kanban-card undated", open_col)
        self.assertNotIn("Ohne-Termin-Aufgabe", done_col)

    def test_a_task_due_today_lands_in_the_urgent_column_as_today(self):
        self.given_mixed_plan()
        _open_col, urgent_col, _done_col = self.kanban_columns()
        self.assertIn("Heute-Aufgabe", urgent_col)
        self.assertIn("kanban-card today", urgent_col)

    def test_a_done_undated_task_still_lands_in_the_done_column(self):
        self.given_mixed_plan()
        open_col, _urgent_col, done_col = self.kanban_columns()
        self.assertIn("Erledigt-Aufgabe", done_col)
        self.assertNotIn("Erledigt-Aufgabe", open_col)

    def test_the_progress_counters_include_undated_and_today(self):
        # #210: the badges used to be counted in the browser from
        # .kanban-card classes, and a stage missing from that selector list
        # was a card on the board the badge above it did not count. They are
        # rendered from _KANBAN_COLUMN now, so the same guard asks the
        # rendered numbers instead of the selectors that used to produce them.
        self.given_mixed_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(
            response.context["kanban_counts"], {"open": 1, "urgent": 1, "done": 1}
        )

    def test_reschedule_js_clears_the_today_class_too(self):
        # Rescheduling a due-today task away must not leave the amber
        # styling behind until the next reload. The literal class list grew
        # a name once the dot started moving with the label — see
        # RescheduleReclassifiesTheWholeRowTest for the full contract.
        self.given_mixed_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "el.classList.remove(...URGENCY_CLASSES);")
        self.assertContains(response, "reclassify(dueSpan, data.urgency);")

    def test_reschedule_js_displays_the_servers_formatted_date(self):
        # #176: the raw ISO date (newDate/input.value) must never land in the
        # UI directly — only the server's human-readable form may. #238 split
        # that into two fields, the row's short one and the board's long one.
        self.given_mixed_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const data = await response.json();")
        self.assertContains(response, "dueSpan.textContent = data.due_display_row;")
        self.assertNotContains(response, "dueSpan.textContent = newDate;")
        self.assertNotContains(response, "span.textContent = input.value;")

    def test_the_overdue_dot_rule_precedes_the_done_rule(self):
        # Equal specificity — the later rule wins, and a checked-off task
        # must turn gray even while the JS leaves the overdue class in place.
        self.given_mixed_plan()
        for url in ("dashboard", "my_plan"):
            with self.subTest(url=url):
                html = self.client.get(reverse(url)).content.decode()
                self.assertLess(html.index(".dot.overdue"), html.index(".dot.done"))

    def test_my_plan_undated_dot_is_not_done(self):
        self.given_mixed_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "dot undated")

    def test_my_plan_today_date_label_carries_today(self):
        # Markup only since #173 — the class stays as classification, the
        # color rule is gone.
        self.given_mixed_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "task-date today")


@override_settings(DEMO_MODE=False)
class DashboardNotionFailureTest(TestCase):
    """dashboard()'s production branch used to have nothing between it and
    Notion — a single failed read 500'd the whole page. It now either serves
    the last successful read (flagged stale) or, the very first time ever,
    an honest empty state — never a stack trace."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_cold_cache_and_a_failure_is_an_honest_empty_state_not_a_500(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nicht verfügbar")

    def test_falls_back_to_the_last_successful_read_when_notion_then_fails(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            first = self.client.get(reverse("dashboard"))
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "Testkonzert")

        cache.delete(
            CACHE_KEY
        )  # the 8h primary cache expiring; the stale copy outlives it
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            second = self.client.get(reverse("dashboard"))
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Testkonzert")
        self.assertContains(second, "evtl. nicht")

    def test_no_stale_banner_on_a_normal_successful_request(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "evtl. nicht")


@override_settings(DEMO_MODE=False)
class DashboardAiFailureCacheTest(TestCase):
    """A Claude failure while Notion is fine must not be remembered as a
    success: (projects, None) used to land in CACHE_KEY (blanking the AI
    card for the whole 8h TTL) and in STALE_CACHE_KEY (clobbering the last
    good summary) — the second finding from PR #34's review."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_failed_summary_is_retried_on_the_next_request(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                side_effect=AIUnavailableError("boom"),
            ),
        ):
            first = self.client.get(reverse("dashboard"))
        self.assertContains(first, "nicht verfügbar")
        # Claude recovers. Without any cache-busting in between, the very
        # next request must pick the summary up again.
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data("Wieder da"),
            ),
        ):
            second = self.client.get(reverse("dashboard"))
        self.assertContains(second, "Wieder da")

    def test_a_failed_summary_does_not_clobber_the_last_good_one(self):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data("Letzte gute Übersicht"),
            ),
        ):
            self.client.get(reverse("dashboard"))

        cache.delete(CACHE_KEY)  # the 8h primary cache expiring; the stale copy stays
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                side_effect=AIUnavailableError("boom"),
            ),
        ):
            self.client.get(reverse("dashboard"))

        cache.delete(CACHE_KEY)  # must be a no-op — a failed fetch may not have cached
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            third = self.client.get(reverse("dashboard"))
        self.assertContains(third, "Letzte gute Übersicht")
        self.assertContains(third, "evtl. nicht")


@override_settings(DEMO_MODE=False)
class DashboardSyncButtonLabelTest(TestCase):
    """#52: the button never synced anything — it only busts one cache key
    and lets the following read re-fetch from Notion. Now that writes
    invalidate the cache themselves, the label should say what the button
    actually does."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_button_says_what_it_does(self):
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
        self.assertContains(response, "Aktualisieren")
        self.assertNotContains(response, "Sync mit Notion")


class DayTaskCardNoDragTest(DemoModeTestCase):
    """#183 Tier 2 regression net: .no-drag disables SortableJS's grab
    cursor on the read-only demo example cards specifically — a flipped
    condition here would silently make example data draggable (harmless,
    since the drag handler itself still checks server-side, but the cursor
    would promise an interaction that fails)."""

    def test_example_data_day_cards_are_no_drag(self):
        today = date.today()
        project = {
            "id": "demo-p1",
            "name": "Testkonzert",
            "event_date": today + timedelta(days=10),
            "tasks": [
                {
                    "id": "t-today",
                    "name": "Heute fällig",
                    "due": today,
                    "done": False,
                    "kontext": [],
                    "completed_date": None,
                },
            ],
        }
        with patch("projects.views.get_demo_projects", return_value=[project]):
            response = self.client.get(reverse("dashboard"))
        # "no-drag" alone would also match the always-present CSS rule
        # (.day-task-card.no-drag { ... }) — assert on the class actually
        # landing on this task's card instead.
        self.assertContains(response, 'no-drag" data-task-id="t-today"')

    def test_session_plan_day_cards_are_draggable(self):
        today = date.today()
        self.given_session_plan(
            tasks=[
                {
                    "id": "s-today",
                    "name": "Heute fällig",
                    "date": today.isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'no-drag" data-task-id="s-today"')


class SortableScriptInclusionTest(DemoModeTestCase):
    """#183 Tier 2 regression net: the SortableJS include itself must follow
    the same demo/session-plan split as the reschedule affordances it
    powers — omitted entirely for the read-only demo example data."""

    def test_included_for_a_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Sortable.min.js")

    def test_excluded_for_the_multi_project_example_data(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Sortable.min.js")


@override_settings(DEMO_MODE=False)
class SortableScriptInclusionProductionTest(TestCase):
    def test_included_in_production(self):
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Sortable.min.js")


class DayColumnIsADropTargetInFullTest(DemoModeTestCase):
    """#180's drop target is .day-column-body, and SortableJS accepts a drop
    only inside that element's own box — not inside the panel a visitor sees.

    The grid stretches every .day-column to the height of the fullest one,
    while the body was sized by its cards alone. A day holding few tasks, or
    none, therefore rendered a tall panel whose bottom was dead: measured
    against production data, a 567 px column offered 32 px of drop zone and
    503 px of panel that swallowed the drop. Dragging a card onto today, or
    onto an empty day, looked like it should work and did nothing.

    Making the column a flex column and letting the body take the free space
    keeps the two rectangles the same, so every pixel of a panel is a target.
    """

    def styles(self):
        self.given_session_plan()
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_column_lays_its_children_out_in_a_column(self):
        self.assertIn(
            ".day-column { background: var(--color-bg-secondary); "
            "border-radius: 8px; padding: 8px; min-height: 72px; "
            "display: flex; flex-direction: column; }",
            self.styles(),
        )

    def test_the_drop_target_takes_the_free_space(self):
        # min-height stays: it is what an empty column in a row of empty
        # columns falls back to, where there is no free space to take.
        self.assertIn(".day-column-body { min-height: 32px; flex: 1; }", self.styles())


class StatusBannersSharedAcrossViewsTest(DemoModeTestCase):
    """#183 Tier 2: view-overview and view-today render in the same
    response (JS just toggles which is visible) — the demo-banner and
    stale-notice must appear in both, not just view-overview, or switching
    to "Heute" during example data silently drops the only reminder that
    it's not the visitor's own plan."""

    def test_demo_banner_appears_for_both_views(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, "Das sind <strong>Beispieldaten</strong>", count=2
        )


@override_settings(DEMO_MODE=False)
class StaleNoticeSharedAcrossViewsTest(TestCase):
    """#183 Tier 2: same reasoning as StatusBannersSharedAcrossViewsTest,
    for the production-only stale-notice — data_unavailable/stale are only
    ever set on the production branch's NotionUnavailableError fallback."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_data_unavailable_notice_appears_for_both_views(self):
        with (
            patch(
                "projects.views._fetch_fresh_data",
                side_effect=NotionUnavailableError("boom"),
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            "Die Projektdaten sind gerade nicht verfügbar. Bitte versuche es in Kürze erneut.",
            count=2,
        )


class DashboardDeepLinkTest(DemoModeTestCase):
    """#177: the project view has to be reachable via a URL and survive
    browser back/forward — no headless browser here (same limit as the #176
    JS assertions), so this only checks the right JS source is present."""

    def test_dashboard_contains_the_history_sync_js(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "history.pushState")
        self.assertContains(response, "history.replaceState")
        self.assertContains(response, "URLSearchParams")
        self.assertContains(response, "addEventListener('popstate'")


class KanbanColumnTest(SimpleTestCase):
    """#210: the urgency -> column mapping was spelled out three times in
    dashboard.html as `{% if task.urgency == ... %}`. Moving a card on
    toggle would have made that a fourth copy in JavaScript, so the rule
    became a function and ships as a field on every task instead."""

    def test_done_has_its_own_column(self):
        self.assertEqual(_kanban_column("done"), "done")

    def test_everything_with_a_deadline_this_week_or_earlier_is_urgent(self):
        for urgency in ("overdue", "today", "urgent"):
            with self.subTest(urgency=urgency):
                self.assertEqual(_kanban_column(urgency), "urgent")

    def test_later_and_undated_work_is_open(self):
        for urgency in ("ok", "undated"):
            with self.subTest(urgency=urgency):
                self.assertEqual(_kanban_column(urgency), "open")

    def test_every_stage_a_task_can_carry_has_a_column(self):
        # A stage added to _annotate_tasks without a column here would
        # silently drop its cards off the board.
        self.assertEqual(set(_KANBAN_COLUMN), set(_URGENCY_RANK))

    def test_annotate_tasks_sets_the_field(self):
        today = date(2026, 9, 5)
        projects = _annotate_tasks(
            [
                {
                    "id": "p1",
                    "tasks": [
                        {"id": "t1", "due": today - timedelta(days=1), "done": False},
                        {"id": "t2", "due": today + timedelta(days=90), "done": False},
                        {"id": "t3", "due": today, "done": True},
                    ],
                }
            ],
            today,
        )
        self.assertEqual(
            [t["kanban_column"] for t in projects[0]["tasks"]],
            ["urgent", "done", "open"],
        )


class KanbanCardMarkupTest(TestCase):
    """The card has to be findable by task id before it can be moved."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_every_card_carries_its_task_id(self):
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
            html = self.client.get(reverse("dashboard")).content.decode()
        cards = re.findall(r'<div class="kanban-card [^"]*"[^>]*>', html)
        self.assertTrue(cards)
        for card in cards:
            self.assertIn("data-task-id=", card)

    def test_the_template_renders_from_the_column_field(self):
        # Not from a fourth copy of the urgency -> column mapping.
        template = (
            settings.BASE_DIR / "projects/templates/projects/dashboard.html"
        ).read_text()
        self.assertIn("{% if task.kanban_column == 'open' %}", template)
        self.assertIn("{% if task.kanban_column == 'urgent' %}", template)
        self.assertIn("{% if task.kanban_column == 'done' %}", template)
        self.assertNotIn(
            "{% if task.urgency == 'ok' or task.urgency == 'undated' %}", template
        )


class DashboardCacheVersionTest(SimpleTestCase):
    """#210 adds kanban_column to every cached task dict. The cache stores
    already-annotated projects and does not re-annotate on a hit, so a
    pre-deploy entry would render an empty board — and STALE_CACHE_KEY never
    expires, so it would serve that shape indefinitely."""

    def test_both_key_pairs_are_bumped_together(self):
        self.assertEqual(CACHE_KEY, "dashboard_data_v9")
        self.assertEqual(STALE_CACHE_KEY, "dashboard_data_stale_v9")
        self.assertEqual(UNASSIGNED_CACHE_KEY, "dashboard_unassigned_v4")
        self.assertEqual(STALE_UNASSIGNED_CACHE_KEY, "dashboard_unassigned_stale_v4")
