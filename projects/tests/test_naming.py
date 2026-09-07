"""Display names and date formatting — what a project or a task is called
on screen, as opposed to what it is called in Notion."""

from datetime import (
    date,
    timedelta,
)
from unittest.mock import patch

from django.core.cache import cache
from django.template import (
    Context,
    Template,
)
from django.test import (
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..date_format import (
    format_date,
    format_week_range,
)
from ..views import (
    CACHE_KEY,
    UNASSIGNED_CACHE_KEY,
    _strip_trailing_date,
)
from .base import (
    DemoModeTestCase,
    _fake_upcoming_project_with_task,
    _summary_data,
)


class OverviewPageNamingTest(DemoModeTestCase):
    """#48: "Übersicht" used to label both the single-project overview and
    the AI-card heading, so a visitor saw the same word for two different
    things. Renamed to "Dashboard", matching the dashboard/ URL path."""

    def test_sidebar_nav_overview_says_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, '<a class="sidebar-item active" id="nav-overview"'
        )
        self.assertContains(response, "Dashboard")
        self.assertNotContains(response, "Übersicht")

    def test_ai_card_heading_says_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, '<div style="font-size: 22px; font-weight: 700;">Dashboard</div>'
        )


class MultiProjectViewNamingTest(DemoModeTestCase):
    """#48: the multi-project view was "Mehrprojekt-Ansicht" in the sidebar
    but "Mehrprojekt-Dashboard"/"Beispiel-Dashboard" elsewhere — three words
    for one destination (dashboard?mode=multi). Unified on
    "Mehrprojekt-Dashboard" everywhere a visitor can reach it from.

    The sidebar dropped out of that set once #183 gave it two
    .sidebar-title groups: there the heading names the data and the entry
    names the view, so the compound would state the heading's half twice.
    Everywhere without a heading to lean on still carries it."""

    def test_the_sidebar_never_says_mehrprojekt_ansicht(self):
        # The word #48 removed is still gone. What the sidebar says instead
        # is SidebarModeGroupingTest's subject, since it is only readable
        # per group.
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Mehrprojekt-Ansicht")

    def test_landing_page_links_say_mehrprojekt_dashboard(self):
        response = self.client.get("/")
        self.assertContains(response, "Mehrprojekt-Dashboard ansehen")
        self.assertNotContains(response, "Beispiel-Dashboard")

    def test_my_plan_link_already_said_mehrprojekt_dashboard(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Mehrprojekt-Dashboard ansehen")


class StripTrailingDateTest(SimpleTestCase):
    """#134: the maintainer's Notion naming habit appends the event date to
    the name ("Adventskonzert 12.09.2026"), which collides with the UI's own
    date display. _strip_trailing_date removes a trailing German date or bare
    year for display only — the Notion property is never touched."""

    def test_strips_full_date(self):
        self.assertEqual(
            _strip_trailing_date("Adventskonzert 12.09.2026"), "Adventskonzert"
        )

    def test_strips_date_without_year(self):
        self.assertEqual(
            _strip_trailing_date("Adventskonzert 12.09."), "Adventskonzert"
        )

    def test_strips_date_after_comma(self):
        self.assertEqual(
            _strip_trailing_date("Adventskonzert, 12.09.2026"), "Adventskonzert"
        )

    def test_strips_date_after_dash(self):
        self.assertEqual(
            _strip_trailing_date("Adventskonzert – 12.09.2026"), "Adventskonzert"
        )
        self.assertEqual(
            _strip_trailing_date("Adventskonzert - 12.09.2026"), "Adventskonzert"
        )

    def test_strips_single_digit_day_and_month(self):
        self.assertEqual(_strip_trailing_date("Konzert 1.9.2026"), "Konzert")

    def test_still_strips_bare_year(self):
        self.assertEqual(_strip_trailing_date("Konzert 2026"), "Konzert")

    def test_name_without_date_is_unchanged(self):
        self.assertEqual(_strip_trailing_date("Sommerfest"), "Sommerfest")

    def test_trailing_non_year_number_is_kept(self):
        self.assertEqual(_strip_trailing_date("Jubiläum 175"), "Jubiläum 175")

    def test_name_that_is_only_a_date_is_never_emptied(self):
        self.assertEqual(_strip_trailing_date("12.09.2026"), "12.09.2026")

    # The real Notion names spell the date out ("am 5. September") instead of
    # the numeric form the first round covered — seen live after PR #135.

    def test_strips_textual_date_with_am(self):
        self.assertEqual(
            _strip_trailing_date("Musik zur Marktzeit am 5. September"),
            "Musik zur Marktzeit",
        )

    def test_strips_textual_date_without_am(self):
        self.assertEqual(
            _strip_trailing_date("Trio romantique 13. September"),
            "Trio romantique",
        )

    def test_strips_textual_date_with_year(self):
        self.assertEqual(
            _strip_trailing_date("Jahreskonzert der Kantorei am 15. November 2026"),
            "Jahreskonzert der Kantorei",
        )

    def test_month_word_without_day_number_is_kept(self):
        self.assertEqual(
            _strip_trailing_date("Klänge im September"), "Klänge im September"
        )

    def test_non_month_ordinal_is_kept(self):
        self.assertEqual(
            _strip_trailing_date("Konzert am 3. Advent"), "Konzert am 3. Advent"
        )


class MyPlanDisplayNameTest(DemoModeTestCase):
    """#134: my_plan.html rendered the raw project.name in the page title and
    the project header, bypassing display_name entirely — a trailing date in
    the name showed up next to the app's own date display."""

    def test_my_plan_shows_cleaned_name(self):
        self.given_session_plan(name="Adventskonzert 12.09.2026")
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Adventskonzert")
        self.assertNotContains(response, "Adventskonzert 12.09.2026")


class DashboardDisplayNameStripsFullDateTest(TestCase):
    """#134: _strip_year only caught a bare trailing year, so a full date
    ("12.09.2026") survived into display_name on the production dashboard."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(DEMO_MODE=False)
    def test_dashboard_shows_cleaned_name(self):
        project = _fake_upcoming_project_with_task()
        project["name"] = "Adventskonzert 12.09.2026"
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Adventskonzert")
        self.assertNotContains(response, "Adventskonzert 12.09.2026")


class DownloadPlanDisplayNameTest(DemoModeTestCase):
    """#134 follow-up (PR #135 review): the markdown export printed the raw
    plan name as the heading, directly above its own Zieldatum line — the
    same date doubling the dashboard and Mein Plan fixes addressed."""

    def test_export_heading_shows_cleaned_name(self):
        self.given_session_plan(name="Adventskonzert 12.09.2026")
        response = self.client.get(reverse("download_plan"))
        content = response.content.decode()
        self.assertIn("# Adventskonzert\n", content)
        self.assertNotIn("Adventskonzert 12.09.2026", content)

    def test_filename_keeps_the_raw_name(self):
        self.given_session_plan(name="Adventskonzert 12.09.2026")
        response = self.client.get(reverse("download_plan"))
        self.assertIn(
            'filename="Adventskonzert_12.09.2026.md"',
            response["Content-Disposition"],
        )


class DateFormatModuleTest(SimpleTestCase):
    """#189: display formatting lives in its own module so both the views
    and the template filter can reach it. The tables and the "long" output
    are unchanged from views._format_date — this pins that, so the move
    stays behaviour-neutral."""

    def test_long_role_matches_the_previous_format(self):
        self.assertEqual(format_date(date(2026, 6, 15), role="long"), "Mo, 15. Juni")

    def test_long_is_the_default_role(self):
        self.assertEqual(format_date(date(2026, 6, 15)), "Mo, 15. Juni")

    def test_short_role_is_the_numeric_calendar_form(self):
        self.assertEqual(format_date(date(2026, 3, 3), role="short"), "03.03.")

    def test_none_is_empty_in_every_role(self):
        self.assertEqual(format_date(None), "")
        self.assertEqual(format_date(None, role="short"), "")

    def test_an_unknown_role_is_an_error_not_a_fallback(self):
        # Falling back to "long" would render the wrong format silently,
        # and #192 is about changing the role set — the point where a
        # typo stops being theoretical.
        with self.assertRaises(ValueError) as caught:
            format_date(date(2026, 6, 15), role="shrot")
        self.assertIn("shrot", str(caught.exception))

    def test_an_unknown_role_raises_even_without_a_date(self):
        # Checked before the date, so a typo cannot hide behind whichever
        # rows happen to be undated.
        with self.assertRaises(ValueError):
            format_date(None, role="shrot")

    def test_week_range_collapses_a_shared_month(self):
        self.assertEqual(
            format_week_range(date(2026, 3, 2), date(2026, 3, 8)), "2.–8. März"
        )

    def test_week_range_spells_both_months_when_they_differ(self):
        self.assertEqual(
            format_week_range(date(2026, 3, 30), date(2026, 4, 5)),
            "30. Mär – 5. April",
        )


class PlanDateFilterTest(SimpleTestCase):
    """#189: the filter is what lets templates format at render time, so a
    date the dashboard cached before a format change still renders in the
    new format."""

    def render(self, template, value):
        return Template(template).render(Context({"v": value}))

    def test_renders_the_long_form_by_default(self):
        self.assertEqual(
            self.render("{% load planner_tags %}{{ v|plan_date }}", date(2026, 6, 15)),
            "Mo, 15. Juni",
        )

    def test_role_argument_selects_the_short_form(self):
        self.assertEqual(
            self.render(
                '{% load planner_tags %}{{ v|plan_date:"short" }}', date(2026, 3, 3)
            ),
            "03.03.",
        )

    def test_a_missing_date_renders_as_nothing(self):
        self.assertEqual(
            self.render('{% load planner_tags %}{{ v|plan_date:"long" }}', None), ""
        )

    def test_a_misspelled_role_fails_the_render(self):
        # Django does not swallow a filter's ValueError, so the typo
        # surfaces as a failure instead of a wrong date on the page.
        with self.assertRaises(ValueError):
            self.render(
                '{% load planner_tags %}{{ v|plan_date:"shrot" }}', date(2026, 6, 15)
            )


@override_settings(DEMO_MODE=False)
class DashboardCacheHoldsNoFormattedDatesTest(TestCase):
    """#189: the whole point of the move. Both cached payloads are task
    dicts, CACHE_KEY for 8 hours and STALE_CACHE_KEY forever — a formatted
    date in there means a format change does not reach the screen until the
    entry expires, and the stale copy never does."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_neither_cached_payload_carries_a_formatted_date(self):
        unassigned = [
            {
                "id": "task-2",
                "name": "Noten bestellen",
                "due": date.today() + timedelta(days=4),
                "done": False,
                "kontext": [],
            }
        ]
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=unassigned),
            # cache.set only runs when the summary came back, so a real
            # dict here is what makes the assertions below reachable.
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

        cached_projects, _ = cache.get(CACHE_KEY)
        cached_tasks = [t for p in cached_projects for t in p["tasks"]]
        self.assertTrue(cached_tasks)
        for task in cached_tasks:
            self.assertNotIn("due_display", task)

        cached_unassigned = cache.get(UNASSIGNED_CACHE_KEY)
        self.assertTrue(cached_unassigned)
        for task in cached_unassigned:
            self.assertNotIn("due_display", task)

    def test_the_date_still_reaches_the_page(self):
        # The counterpart to the assertions above: dropping the key must
        # not mean dropping the date.
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
        self.assertContains(response, format_date(date.today() + timedelta(days=3)))


@override_settings(DEMO_MODE=False)
class DashboardSummaryShowsTaskDatesTest(TestCase):
    """#190: the projection is only half the fix — the KI-Wochenübersicht
    writes out its own task list, so the date has to be rendered there too.
    A page-wide assertContains would pass on the task rows further down,
    hence the slice."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _ai_card_html(self, response):
        content = response.content.decode()
        start = content.index('<div class="ai-card">')
        end = content.index('<div class="overview-progress"', start)
        return content[start:end]

    def _render(self, task_refs=(1,)):
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project_with_task()],
            ),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={
                    "jetzt_faellig": [
                        {
                            "project_ref": 1,
                            "assessment": "Programm ist der Engpass",
                            "task_refs": list(task_refs),
                        }
                    ],
                    "naechste_woche": [],
                },
            ),
        ):
            return self.client.get(reverse("dashboard"))

    def test_the_summary_lists_each_tasks_due_date(self):
        response = self._render()
        self.assertIn(
            format_date(date.today() + timedelta(days=3)), self._ai_card_html(response)
        )

    def test_the_date_carries_the_tasks_urgency_class(self):
        # Same class the task rows use, so the two surfaces cannot end up
        # colouring the same date differently.
        html = self._ai_card_html(self._render())
        self.assertIn('class="task-due ', html)


class MyPlanSummaryShowsTaskDatesTest(DemoModeTestCase):
    """#190: /mein-plan/ resolves the same projection but renders its own
    copy of the block list, so it needs the date added separately."""

    def _summary_box_html(self, response):
        content = response.content.decode()
        start = content.index('<div class="summary-box">')
        end = content.index('<div class="task-list">', start)
        return content[start:end]

    def test_the_summary_lists_each_tasks_due_date(self):
        self.given_session_plan()
        self.ai_mocks["projects.views.generate_weekly_summary"].return_value = {
            "jetzt_faellig": [
                {"heading": "Testkonzert", "assessment": "x", "task_refs": [1]}
            ],
            "naechste_woche": [],
        }
        response = self.client.get(reverse("my_plan"))
        self.assertIn(
            format_date(date.today() + timedelta(days=7)),
            self._summary_box_html(response),
        )
