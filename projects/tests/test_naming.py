"""Display names and date formatting — what a project or a task is called
on screen, as opposed to what it is called in Notion."""

from datetime import (
    date,
    timedelta,
)
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
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
    SummaryFlowMixin,
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
            response,
            '<button type="button" class="sidebar-item active" id="nav-overview">',
        )
        self.assertContains(response, "Dashboard")
        self.assertNotContains(response, "Übersicht")

    def test_ai_card_heading_says_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<div class="page-heading">Dashboard</div>')


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

    # #282: the habit also writes a weekday before the date, which used to
    # survive the strip and read as part of the name ("… am Do").

    def test_strips_a_short_weekday_before_the_date(self):
        self.assertEqual(
            _strip_trailing_date("Adventssingen am Do, 17. Dezember 2026"),
            "Adventssingen",
        )

    def test_strips_a_long_weekday_before_the_date(self):
        self.assertEqual(
            _strip_trailing_date("Adventssingen am Donnerstag, 17. Dezember 2026"),
            "Adventssingen",
        )

    def test_strips_an_abbreviated_weekday_before_a_numeric_date(self):
        self.assertEqual(_strip_trailing_date("Konzert am Do. 17.12.2026"), "Konzert")

    def test_strips_a_weekday_without_am(self):
        self.assertEqual(_strip_trailing_date("Konzert, Sa 5. Dezember"), "Konzert")

    def test_a_weekday_inside_a_word_is_not_a_weekday(self):
        # The separator before it is what makes it one — without it
        # "Sonntagskonzert" would lose its first half to the "So" branch.
        self.assertEqual(
            _strip_trailing_date("Sonntagskonzert 2026"), "Sonntagskonzert"
        )

    def test_a_weekday_with_no_date_after_it_is_kept(self):
        self.assertEqual(
            _strip_trailing_date("Matinee am Sonntag"), "Matinee am Sonntag"
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


class ReplaceNoticeDisplayNameTest(DemoModeTestCase):
    """#129: the notice names the plan a new run would replace — through
    _strip_trailing_date like every other place a plan name reaches a screen,
    not the raw session value."""

    def test_the_notice_shows_the_cleaned_name(self):
        self.given_session_plan(name="Adventskonzert 12.09.2026")
        response = self.client.get(reverse("planner_start"))
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

    def test_row_role_abbreviates_the_month(self):
        # #238: "row" rather than "short" — "short" is taken by the numeric
        # calendar form, and roles name the surface, not the format.
        self.assertEqual(format_date(date(2026, 9, 3), role="row"), "Do, 3. Sep")

    def test_the_row_abbreviation_carries_no_period(self):
        # MONTHS_SHORT has none, and format_week_range has read fine
        # without one since it was written.
        self.assertEqual(format_date(date(2026, 5, 4), role="row"), "Mo, 4. Mai")

    def test_note_role_drops_the_weekday(self):
        # #214: the empty-summary note names the date inside a sentence
        # ("Die nächste Aufgabe ist am 23. Dezember."), where the weekday
        # "long" carries would read as a second clause.
        self.assertEqual(format_date(date(2026, 12, 23), role="note"), "23. Dezember")

    def test_none_is_empty_in_every_role(self):
        self.assertEqual(format_date(None), "")
        self.assertEqual(format_date(None, role="short"), "")
        self.assertEqual(format_date(None, role="note"), "")

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

    def test_role_argument_selects_the_row_form(self):
        self.assertEqual(
            self.render(
                '{% load planner_tags %}{{ v|plan_date:"row" }}', date(2026, 9, 3)
            ),
            "Do, 3. Sep",
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


class ShortRowDateReachesOnlyTheRowTest(SimpleTestCase):
    """#238: the abbreviated month is the task row's alone. Every other date
    surface keeps the spelled-out "long" form, and the only way that can
    drift is a template picking up the new role by copy-paste — so the
    surfaces are counted against the templates themselves.

    #279's add row does not change that count. It carries no role of its own:
    the including surface passes one, because "a row asking for the same kind
    of value in a shape nothing else on the page uses" was the defect that
    issue was about, and the dashboard's list and /mein-plan/'s do not spell a
    date the same way."""

    TEMPLATES = Path(settings.BASE_DIR) / "projects/templates/projects"

    def read(self, name):
        return (self.TEMPLATES / name).read_text()

    def read_with_summary(self, name, summary_partial):
        """A page together with the summary body it includes (#156). Both
        runs of tasks are still this page's surfaces; one of them simply
        lives in its own file now so the fragment endpoint can render it."""
        return self.read(name) + self.read(summary_partial)

    def test_the_task_row_is_the_only_template_on_the_row_role(self):
        on_row_role = sorted(
            path.name
            for path in self.TEMPLATES.glob("*.html")
            if 'plan_date:"row"' in path.read_text()
        )
        self.assertEqual(on_row_role, ["_task_row.html"])

    def test_the_add_row_takes_the_role_of_the_list_it_closes(self):
        # The pair this rests on, asserted rather than assumed: the
        # dashboard's rows abbreviate the month, /mein-plan/'s list writes it
        # out. The add row is included under each with that surface's role, so
        # #279's fix is one include argument per surface and no third spelling.
        self.assertIn('plan_date:"row"', self.read("_task_row.html"))
        self.assertIn('plan_date:"long"', self.read("my_plan.html"))
        for template, role in (("dashboard.html", "row"), ("my_plan.html", "long")):
            with self.subTest(template=template):
                self.assertIn(
                    '{% include "projects/_task_add_row.html" '
                    f'with project_id=project.id date_role="{role}" %}}',
                    self.read(template),
                )

    def test_the_add_row_pins_no_role_of_its_own(self):
        # The assertion the count above would otherwise let through: a
        # default here would be one surface's form imposed on the other, which
        # is the state #279 shipped first.
        partial = self.read("_task_add_row.html")
        self.assertIn("plan_date:date_role", partial)
        self.assertNotIn('plan_date:"', partial)

    def test_the_kanban_board_still_spells_the_month_out(self):
        # It has the width, and #238 stage 4 is about the row only.
        self.assertIn(
            '<span class="kanban-card-due">{{ task.due|plan_date:"long" }}</span>',
            self.read("dashboard.html"),
        )

    def test_the_ai_summary_still_spells_the_month_out(self):
        # Since #195 the markup comes from _task_due.html and only the form
        # is stated here, which is the half that is about this surface. What
        # the assertion protects is unchanged: the summary has the width for
        # the month and spells it out where the row abbreviates. #266 dropped
        # the readonly flag — the form is all this surface still states.
        self.assertIn(
            '{% include "projects/_task_due.html" '
            'with due_display=task.due|plan_date:"long" %}',
            self.read("_ai_summary_body.html"),
        )

    def test_my_plan_and_the_close_out_triage_are_untouched(self):
        self.assertEqual(
            self.read_with_summary("my_plan.html", "_my_plan_summary_body.html").count(
                'plan_date:"long"'
            ),
            2,
        )
        self.assertIn('plan_date:"long"', self.read("close_week_start.html"))


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
class DashboardSummaryShowsTaskDatesTest(SummaryFlowMixin, TestCase):
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
                            "heading": "Programm offen",
                            "assessment": "Programm ist der Engpass",
                            "task_refs": list(task_refs),
                        }
                    ],
                    "naechste_woche": [],
                },
            ),
        ):
            # #156: the card renders its summary inline once the fragment has
            # filled the cache, which is the render this slices.
            return self.dashboard_with_summary()

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
        response = self.my_plan_with_summary()
        self.assertIn(
            format_date(date.today() + timedelta(days=7)),
            self._summary_box_html(response),
        )
