"""The planner: the four-step flow from tile to created plan, and the
plan-generating calls behind it."""

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
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from ..ai import AIUnavailableError
from ..notion import NotionUnavailableError
from ..planner import (
    generate_plan,
    get_clarifying_questions,
)
from ..planner_views import (
    _get_history,
    _parse_event_date,
)
from ..views import (
    CACHE_KEY,
    STALE_CACHE_KEY,
    SUMMARY_KEY,
)
from .base import (
    DemoModeTestCase,
    _anthropic_timeout_error,
    _fake_response,
)


class PlannerLoadingStateTest(DemoModeTestCase):
    """#6: markup-contract tests for the shared loading-state CSS/JS in
    base_public.html, and the per-form data-loading-text attribute. Runtime
    behaviour (button really disables, double-submit is really swallowed)
    isn't provable by a Django TestCase and gets a manual browser pass."""

    def test_base_template_defines_the_loading_css(self):
        # #32 moved this rule from the inline <style> into public.css.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(".btn-primary.is-loading", css)

    def test_base_template_ships_the_double_submit_script(self):
        response = self.client.get(reverse("impressum"))
        self.assertContains(response, "loadingText")

    def test_base_template_resets_loading_state_on_bfcache_restore(self):
        response = self.client.get(reverse("impressum"))
        self.assertContains(response, "pageshow")
        self.assertContains(response, "e.persisted")

    def test_describe_form_button_has_loading_text(self):
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertContains(response, 'data-loading-text="Fragen werden erstellt..."')

    def test_questions_form_button_has_loading_text(self):
        response = self.client.post(
            reverse("planner_start"),
            data={
                "description": "Konzert am 15. September 2026",
            },
        )
        self.assertContains(response, 'data-loading-text="Plan wird erstellt..."')

    def test_review_form_button_has_loading_text(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertContains(response, 'data-loading-text="Wird gespeichert..."')

    def test_the_selector_this_script_hangs_on_still_matches(self):
        # #64 added `.btn` to the buttons; the script and the .is-loading /
        # .spinner rules key off `.btn-primary`, which has to survive that.
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertContains(response, 'button[type="submit"].btn-primary')
        self.assertContains(response, 'class="btn btn-primary"')


class AiStubTest(DemoModeTestCase):
    """Guards the guard: proves the stubs are actually in the request path."""

    def test_dashboard_does_not_call_the_real_api(self):
        self.client.get("/dashboard/")
        self.ai_mocks["projects.views.generate_weekly_summary"].assert_called()


class PlannerGetFallthroughTest(DemoModeTestCase):
    """Both views used to fall through to an implicit `return None` on GET."""

    def test_review_get_redirects_to_start(self):
        response = self.client.get(reverse("planner_review"))
        self.assertRedirects(response, reverse("planner_start"))

    def test_review_get_renders_the_stored_plan_after_a_post(self):
        # #116: the review step becomes GET-reachable so a refresh (or the
        # stepper eventually pointing at it) redisplays the generated plan
        # instead of bouncing the visitor back to step 1.
        self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        response = self.client.get(reverse("planner_review"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/planner_review.html")
        self.assertContains(response, "Testkonzert")

    def test_create_get_redirects_to_start(self):
        response = self.client.get(reverse("planner_create"))
        self.assertRedirects(response, reverse("planner_start"))

    def test_questions_get_redirects_to_start_when_session_is_empty(self):
        # #116: the route is back, but only ever useful once step 2 has
        # actually stored something to show.
        response = self.client.get(reverse("planner_questions"))
        self.assertRedirects(response, reverse("planner_start"))

    def test_questions_get_renders_from_session_after_a_post(self):
        self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        response = self.client.get(reverse("planner_questions"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/planner_questions.html")
        self.assertContains(response, "Konzert am 15. September 2026")
        self.assertContains(response, "Wie viele Mitwirkende?")


class StepperBackLinkTest(DemoModeTestCase):
    """#116: only the step immediately before the current one becomes a
    link in the tracker — every earlier 'done' step stays a plain marker,
    so a visitor can only ever go back exactly one step at a time."""

    def _ps_step_wrapper_tags(self, html):
        return re.findall(r'<(a|div) [^>]*class="ps-step[^"]*"', html)

    def test_step_one_has_no_back_link(self):
        response = self.client.get(reverse("planner_start"))
        self.assertEqual(
            self._ps_step_wrapper_tags(response.content.decode()),
            ["div", "div", "div", "div"],
        )

    def test_step_two_links_back_to_the_tile_grid_only(self):
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertEqual(
            self._ps_step_wrapper_tags(response.content.decode()),
            ["a", "div", "div", "div"],
        )
        self.assertContains(
            response, f'<a href="{reverse("planner_start")}" class="ps-step done">'
        )

    def test_step_three_links_back_to_beschreiben_only(self):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        response = self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        self.assertEqual(
            self._ps_step_wrapper_tags(response.content.decode()),
            ["div", "a", "div", "div"],
        )
        back_url = reverse("planner_start") + "?type=eigenes"
        self.assertContains(response, f'<a href="{back_url}" class="ps-step done">')

    def test_step_four_links_back_to_klaerung_only(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertEqual(
            self._ps_step_wrapper_tags(response.content.decode()),
            ["div", "div", "a", "div"],
        )
        self.assertContains(
            response,
            f'<a href="{reverse("planner_questions")}" class="ps-step done">',
        )


class PlannerBackNavigationPreservesDataTest(DemoModeTestCase):
    """#116: going back one step must not lose what the visitor already
    typed — the whole point of making the step reachable via GET."""

    def test_klaerung_back_to_beschreiben_keeps_the_description(self):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertContains(response, "Konzert am 15. September 2026</textarea>")

    def test_review_back_to_klaerung_keeps_questions_and_answers(self):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 15. September 2026",
                "answers": "20 Gäste, in der Kirche",
            },
        )
        response = self.client.get(reverse("planner_questions"))
        self.assertContains(response, "Wie viele Mitwirkende?")
        self.assertContains(response, "20 Gäste, in der Kirche</textarea>")


class PlannerFreshStartClearsStaleDraftTest(DemoModeTestCase):
    """#116: the tile grid is the explicit 'start over' entry point — an
    abandoned draft must not bleed into the next, unrelated attempt."""

    def test_visiting_the_tile_grid_clears_a_stale_description(self):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        self.client.post(
            reverse("planner_start"),
            data={"description": "Alter Entwurf, nie abgeschlossen"},
        )
        self.client.get(reverse("planner_start"))  # tile grid, no ?type=
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertNotContains(response, "Alter Entwurf")
        self.assertContains(response, "></textarea>")


class PlannerDescriptionChangeClearsStaleDownstreamTest(DemoModeTestCase):
    """#124: editing the description on step 2 must invalidate whatever an
    earlier round already produced downstream (answers, review state) — the
    same "this boundary starts a new draft" reasoning
    PlannerFreshStartClearsStaleDraftTest already covers for a tile-grid
    visit, now applied to the description edit itself."""

    def _reach_review_with(self, description, answers):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        self.client.post(reverse("planner_start"), data={"description": description})
        self.client.post(
            reverse("planner_review"),
            data={"description": description, "answers": answers},
        )

    def test_editing_the_description_drops_the_stale_answers(self):
        self._reach_review_with("Konzert A", "Antwort A")
        self.client.post(reverse("planner_start"), data={"description": "Konzert B"})
        response = self.client.get(reverse("planner_questions"))
        self.assertContains(response, "Konzert B")
        self.assertNotContains(response, "Antwort A")

    def test_editing_the_description_drops_the_stale_review_state(self):
        self._reach_review_with("Konzert A", "Antwort A")
        self.client.post(reverse("planner_start"), data={"description": "Konzert B"})
        response = self.client.get(reverse("planner_review"))
        self.assertRedirects(response, reverse("planner_start"))

    def test_resubmitting_the_same_description_keeps_the_answers(self):
        self._reach_review_with("Konzert A", "Antwort A")
        self.client.post(reverse("planner_start"), data={"description": "Konzert A"})
        response = self.client.get(reverse("planner_questions"))
        self.assertContains(response, "Antwort A")


class PlannerRedisplayNoticeTest(DemoModeTestCase):
    """#124's report noted that nothing in the UI told a redisplayed
    Klärung/Review page apart from a freshly generated one, even though the
    fix landing here already guarantees the two can no longer mismatch. A
    quiet .draft-notice now marks the GET-redisplay path (stepper back-link,
    refresh) so it doesn't read as brand new; the POST-rendered fresh page
    stays exactly as before."""

    def test_fresh_questions_carry_no_notice(self):
        response = self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        self.assertNotContains(response, '<span class="draft-notice">')

    def test_redisplayed_questions_carry_the_notice(self):
        self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )
        response = self.client.get(reverse("planner_questions"))
        self.assertContains(response, '<span class="draft-notice">')

    def test_fresh_review_carries_no_notice(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertNotContains(response, '<span class="draft-notice">')

    def test_redisplayed_review_carries_the_notice(self):
        self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        response = self.client.get(reverse("planner_review"))
        self.assertContains(response, '<span class="draft-notice">')


class PlannerTileLinksTest(DemoModeTestCase):
    """#5 / #72: the tile links used to carry a raw-text prefill query
    parameter ("?prefill=Konzert am [Datum], ..."), unencoded and written
    straight into the textarea — deleting the square brackets was the
    visitor's first job on step 2. The links now carry only the
    urlencode()-built type; the field starts empty and a type-specific
    placeholder shows the example instead. The prefill context key stays:
    it restores typed text on an AIUnavailableError re-render (see
    PlannerStartAiFailureTest)."""

    def test_the_nine_tile_links_carry_only_the_encoded_type(self):
        response = self.client.get(reverse("planner_start"))
        self.assertContains(response, "?type=", count=9)
        self.assertContains(response, 'href="/planner/?type=konzert"')
        self.assertContains(response, 'href="/planner/?type=eigenes"')
        self.assertNotContains(response, "prefill=")

    def test_a_chosen_type_shows_its_placeholder_in_an_empty_field(self):
        response = self.client.get(reverse("planner_start") + "?type=konzert")
        self.assertContains(response, 'placeholder="z.B. Konzert am 15. September')
        self.assertContains(response, "></textarea>")
        self.assertNotContains(response, "[Datum]")

    def test_an_unknown_type_falls_back_to_the_generic_placeholder(self):
        response = self.client.get(reverse("planner_start") + "?type=quatsch")
        self.assertContains(response, "oder: Kandidat einstellen bis 1. Oktober")

    def test_the_error_rerender_keeps_the_chosen_types_placeholder(self):
        self.client.get(reverse("planner_start") + "?type=hochzeit")
        self.ai_mocks[
            "projects.planner_views.get_clarifying_questions"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("planner_start"), data={"description": "Hochzeit am 20. Juni"}
        )
        self.assertContains(
            response, 'placeholder="z.B. Hochzeit am 20. Juni in Potsdam'
        )
        self.assertContains(response, "Hochzeit am 20. Juni</textarea>")


class ReviewLayoutTest(DemoModeTestCase):
    """#72 step 4: the review table's cells carry explicit column classes now.
    td:nth-child was unusable — in demo mode the row is name/date/delete, in
    production name/date/kontext/delete, so nth-child(3) named a different
    column per deployment; its fixed 150px date width was also the direct
    cause of #71. The name column takes every spare pixel, the rest shrink to
    their content, and the head becomes one labelled row."""

    def review_page(self):
        self.ai_mocks["projects.planner_views.generate_plan"].return_value = {
            "project_name": "Testkonzert",
            "tasks": [
                {"name": "Programm festlegen", "days_before": 30, "kontext": "Planung"}
            ],
        }
        return self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )

    def test_every_cell_carries_its_column_class(self):
        html = self.review_page().content.decode()
        for cls in ("col-name", "col-date", "col-actions"):
            with self.subTest(cls=cls):
                self.assertIn(f'<th class="{cls}"', html)
                self.assertIn(f'<td class="{cls}"', html)

    def test_the_name_column_takes_the_spare_width(self):
        response = self.review_page()
        self.assertContains(
            response, ".task-table .col-name { width: 100%; padding-left: 0; }"
        )
        self.assertContains(response, "width: 1%; white-space: nowrap;")

    def test_the_nth_child_widths_are_gone(self):
        response = self.review_page()
        self.assertNotContains(response, "td:nth-child(2)")
        self.assertNotContains(response, "td:last-child")

    def test_the_head_is_one_labelled_row(self):
        response = self.review_page()
        self.assertContains(response, 'class="plan-head"')
        self.assertContains(response, "Projektname")
        self.assertContains(response, "Zieldatum")
        self.assertNotContains(response, "date-header")
        self.assertNotContains(response, 'style="margin-bottom: 20px;"')

    def test_demo_mode_carries_no_kontext_field_at_all(self):
        # #18: kontext is production-only — the earlier hidden input that
        # discarded Claude's own suggestion on submit is gone, not disguised.
        html = self.review_page().content.decode()
        self.assertNotIn("task_kontext", html)

    def test_the_date_control_is_governed(self):
        response = self.review_page()
        self.assertContains(response, '.task-table input[type="date"]')
        self.assertContains(response, "::-webkit-calendar-picker-indicator")

    def test_the_sofort_marker_moved_to_the_classed_cell(self):
        response = self.review_page()
        self.assertContains(
            response,
            "tr.sofort .col-name { box-shadow: inset 3px 0 0 var(--color-text-quaternary); }",
        )
        self.assertNotContains(response, "td:first-child")


@override_settings(DEMO_MODE=False)
class ReviewKontextColumnTest(DemoModeTestCase):
    """The four-column layout never renders in a demo browser pass, so its
    markup contract is pinned here: the kontext select gets its own classed
    cell and wears the chip the design language gives a tag."""

    def review_page(self):
        self.ai_mocks["projects.planner_views.generate_plan"].return_value = {
            "project_name": "Testkonzert",
            "tasks": [
                {"name": "Programm festlegen", "days_before": 30, "kontext": "Planung"}
            ],
        }
        with patch("projects.planner_views.get_historical_projects", return_value=[]):
            return self.client.post(
                reverse("planner_review"),
                data={
                    "description": "Konzert am 5. September 2026",
                    "answers": "keine weiteren Angaben",
                },
            )

    def test_the_kontext_cell_is_classed(self):
        html = self.review_page().content.decode()
        self.assertIn('<th class="col-kontext">Kontext</th>', html)
        self.assertRegex(
            html, r'<td class="col-kontext">\s*<select name="task_kontext"'
        )

    def test_the_select_wears_the_chip(self):
        response = self.review_page()
        self.assertContains(
            response,
            ".task-table select { border: 1px solid transparent; border-radius: 99px;",
        )


class ReviewStacksOnMobileTest(DemoModeTestCase):
    """#71: on a 390px viewport the fixed column widths left the task name
    ~60px and cut every task after a few characters — on the one screen where
    the plan is checked before it is written anywhere. Below 768px each row
    becomes a block. These assert the rules are served; that they actually
    reflow is the browser pass at 390px."""

    def review_page(self):
        self.ai_mocks["projects.planner_views.generate_plan"].return_value = {
            "project_name": "Testkonzert",
            "tasks": [
                {"name": "Programm festlegen", "days_before": 30, "kontext": "Planung"}
            ],
        }
        return self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )

    def test_the_rows_become_blocks_below_the_breakpoint(self):
        response = self.review_page()
        self.assertContains(response, "@media (max-width: 768px)")
        self.assertContains(
            response,
            ".task-table, .task-table tbody, .task-table tr, .task-table td { display: block; }",
        )

    def test_the_header_row_is_dropped(self):
        self.assertContains(self.review_page(), ".task-table thead { display: none; }")

    def test_the_sofort_marker_moves_from_the_cell_to_the_row(self):
        response = self.review_page()
        self.assertContains(response, "tr.sofort .col-name { box-shadow: none; }")
        self.assertContains(
            response,
            "tr.sofort { box-shadow: inset 3px 0 0 var(--color-text-quaternary); }",
        )

    def test_the_delete_button_leaves_the_flow(self):
        self.assertContains(
            self.review_page(), ".task-table .col-actions { position: absolute;"
        )


class AddTaskRowMarkupTest(DemoModeTestCase):
    """#98: the review page only let a visitor edit or delete a generated
    task, never add one that Claude missed. These pin the markup the new
    client-side row-adder depends on."""

    def review_page(self):
        self.ai_mocks["projects.planner_views.generate_plan"].return_value = {
            "project_name": "Testkonzert",
            "tasks": [
                {"name": "Programm festlegen", "days_before": 30, "kontext": "Planung"}
            ],
        }
        return self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )

    def test_the_add_button_is_served(self):
        html = self.review_page().content.decode()
        self.assertIn('id="add-task-row"', html)
        self.assertIn("Aufgabe hinzufügen", html)

    def test_the_add_button_does_not_submit_the_form(self):
        html = self.review_page().content.decode()
        self.assertIn('<button type="button" id="add-task-row"', html)

    def test_demo_mode_still_carries_no_kontext_field_at_all(self):
        # Regression guard for the new JS specifically: addTaskRow() builds
        # its kontext cell inside a Django {% if not demo_mode %} block, not
        # a JS-level check, so a demo-mode response must stay entirely free
        # of task_kontext even inside the added <script>.
        html = self.review_page().content.decode()
        self.assertNotIn("task_kontext", html)

    def test_the_new_row_will_be_excluded_from_date_recompute(self):
        html = self.review_page().content.decode()
        self.assertIn("querySelectorAll('tbody tr[data-days]')", html)


class PlannerReviewHappyPathTest(DemoModeTestCase):
    """First test to exercise planner_review's POST path at all. generate_plan
    now returns a dict directly (see GeneratePlanRetryTest in planner.py) —
    this guards against a stub/view mismatch regressing that silently."""

    def test_post_renders_the_generated_plan(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Testkonzert")


class PlannerReviewDateFallbackTest(DemoModeTestCase):
    """A description with no recognizable date used to leave event_date_iso
    empty — the date input rendered blank, and submitting it unchanged
    crashed planner_create() on date.fromisoformat(""). event_date now
    always gets a placeholder four weeks out, flagged uncertain until the
    visitor confirms or changes it."""

    def test_no_date_in_the_description_still_prefills_a_date(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert irgendwann im Herbst",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertNotContains(response, 'id="event-date" value=""')
        self.assertContains(
            response,
            'name="event_date_uncertain" id="event-date-uncertain-hidden" value="true"',
        )
        self.assertContains(response, "Kein Termin im Text erkannt")
        self.assertNotContains(response, 'id="date-uncertain-notice" hidden')

    def test_a_recognized_date_is_not_marked_uncertain(self):
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertContains(response, 'id="event-date" value="2026-09-05"')
        self.assertContains(
            response,
            'name="event_date_uncertain" id="event-date-uncertain-hidden" value="false"',
        )
        self.assertContains(response, 'id="date-uncertain-notice" hidden')


class PlannerReviewSortsTasksTest(DemoModeTestCase):
    """#152: the review table lists tasks chronologically (days_before
    descending = ascending date), matching what every later view shows
    (#140) — it rendered Claude's raw emission order before. Post-event
    tasks (negative days_before, like a GEMA report) sort last."""

    OUT_OF_ORDER = [
        {"name": "Generalprobe", "days_before": 3, "kontext": ""},
        {"name": "Programm festlegen", "days_before": 30, "kontext": ""},
        {"name": "GEMA-Meldung einreichen", "days_before": -5, "kontext": ""},
        {"name": "Plakate drucken", "days_before": 10, "kontext": ""},
    ]
    CHRONOLOGICAL = [
        "Programm festlegen",
        "Plakate drucken",
        "Generalprobe",
        "GEMA-Meldung einreichen",
    ]

    def review_page(self, tasks):
        self.ai_mocks["projects.planner_views.generate_plan"].return_value = {
            "project_name": "Testkonzert",
            "tasks": tasks,
        }
        return self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )

    def test_the_review_table_lists_tasks_chronologically(self):
        html = self.review_page(self.OUT_OF_ORDER).content.decode()
        positions = [html.index(name) for name in self.CHRONOLOGICAL]
        self.assertEqual(positions, sorted(positions))

    def test_the_session_state_carries_the_sorted_order(self):
        # The GET/session-restore branch and every later consumer read
        # planner_review_state, so the stored order is the one that counts.
        self.review_page(self.OUT_OF_ORDER)
        names = [
            t["name"] for t in self.client.session["planner_review_state"]["tasks"]
        ]
        self.assertEqual(names, self.CHRONOLOGICAL)


class PlannerCreateClearsOldSummariesTest(DemoModeTestCase):
    """Replanning clears cached summaries by the unversioned prefix, so
    summaries written under any older key version go too — a session can
    outlive several format changes."""

    def test_all_summary_versions_are_cleared(self):
        session = self.client.session
        session["demo_plan_summary_v3_today"] = "<p>alt</p>"
        session[f"{SUMMARY_KEY}_today"] = "<p>aktuell</p>"
        session.save()
        self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert am 5. September",
                "project_name": "Sommerkonzert",
                "event_date": (date.today() + timedelta(days=30)).isoformat(),
                "task_name": ["Programm festlegen"],
                "task_date": [(date.today() + timedelta(days=7)).isoformat()],
                "task_kontext": ["Planung"],
            },
        )
        self.assertNotIn("demo_plan_summary_v3_today", self.client.session)
        self.assertNotIn(f"{SUMMARY_KEY}_today", self.client.session)


class PlannerCreateClearsDraftStateTest(DemoModeTestCase):
    """#116: a finished plan must not leave stale back-navigation state for
    the next, unrelated attempt."""

    DRAFT_KEYS = (
        "planner_description",
        "planner_questions_html",
        "planner_answers",
        "planner_review_state",
    )

    def test_successful_create_clears_the_draft_keys(self):
        self.client.get(reverse("planner_start") + "?type=eigenes")
        self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 5. September 2026"},
        )
        self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert am 5. September 2026",
                "project_name": "Sommerkonzert",
                "event_date": (date.today() + timedelta(days=30)).isoformat(),
                "task_name": ["Programm festlegen"],
                "task_date": [(date.today() + timedelta(days=7)).isoformat()],
                "task_kontext": ["Planung"],
            },
        )
        for key in self.DRAFT_KEYS:
            self.assertNotIn(key, self.client.session)


class PlannerCreateDatelessDescriptionTest(DemoModeTestCase):
    """planner_review always prefills event_date now (see
    PlannerReviewDateFallbackTest), but planner_create must not depend on
    that — a direct POST with the field omitted used to hit
    date.fromisoformat("") and 500. It now falls back the same way and
    still saves the plan."""

    def test_missing_event_date_falls_back_instead_of_crashing(self):
        response = self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert irgendwann im Herbst",
                "project_name": "Herbstkonzert",
                "task_name": ["Programm festlegen"],
                "task_date": [(date.today() + timedelta(days=7)).isoformat()],
                "task_kontext": ["Planung"],
            },
        )
        self.assertEqual(response.status_code, 302)
        plan = self.client.session["demo_plan"]
        self.assertTrue(plan["event_date"])
        self.assertTrue(plan["event_date_uncertain"])

    def test_a_supplied_event_date_is_not_marked_uncertain(self):
        self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert am 5. September 2026",
                "project_name": "Sommerkonzert",
                "event_date": "2026-09-05",
                "event_date_uncertain": "false",
                "task_name": ["Programm festlegen"],
                "task_date": [(date.today() + timedelta(days=7)).isoformat()],
                "task_kontext": ["Planung"],
            },
        )
        plan = self.client.session["demo_plan"]
        self.assertEqual(plan["event_date"], "2026-09-05")
        self.assertFalse(plan["event_date_uncertain"])


class PlannerCreateDropsIncompleteRowsTest(DemoModeTestCase):
    """#98: an added row left empty (never filled in, or added and then
    ignored) must vanish silently rather than save as a blank task —
    planner_create already drops any row with an empty name or date via the
    zip/if guard, but that behavior was only ever exercised with a single
    complete row. This pins it for a genuinely multi-row POST."""

    def test_the_empty_added_row_is_dropped_in_demo_mode(self):
        response = self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert am 5. September",
                "project_name": "Sommerkonzert",
                "event_date": (date.today() + timedelta(days=30)).isoformat(),
                "task_name": ["Programm festlegen", ""],
                "task_date": [(date.today() + timedelta(days=7)).isoformat(), ""],
                "task_kontext": ["Planung", ""],
            },
        )
        self.assertEqual(response.status_code, 302)
        tasks = self.client.session["demo_plan"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Programm festlegen")


@override_settings(DEMO_MODE=False)
class PlannerCreateDropsIncompleteRowsProductionTest(TestCase):
    """Same guard as PlannerCreateDropsIncompleteRowsTest, but through the
    production Notion path, which builds the task list independently."""

    def test_the_empty_added_row_is_dropped_before_reaching_notion(self):
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch("projects.planner_views.create_tasks") as mock_create_tasks,
        ):
            response = self.client.post(
                reverse("planner_create"),
                data={
                    "description": "Konzert am 5. September",
                    "project_name": "Sommerkonzert",
                    "event_date": (date.today() + timedelta(days=30)).isoformat(),
                    "task_name": ["Programm festlegen", ""],
                    "task_date": [
                        (date.today() + timedelta(days=7)).isoformat(),
                        "",
                    ],
                    "task_kontext": ["Planung", ""],
                },
            )
        self.assertEqual(response.status_code, 302)
        _, tasks = mock_create_tasks.call_args.args
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Programm festlegen")


class PlannerCreateClearsDraftStateProductionTest(TestCase):
    """Same cleanup as PlannerCreateClearsDraftStateTest, through the
    production Notion path, which has its own success branch."""

    def test_successful_create_clears_the_draft_keys(self):
        session = self.client.session
        session["planner_description"] = "Konzert am 5. September"
        session["planner_questions_html"] = "<p>Frage?</p>"
        session["planner_answers"] = "keine weiteren Angaben"
        session["planner_review_state"] = {
            "description": "Konzert am 5. September",
            "project_name": "Sommerkonzert",
            "tasks": [],
            "event_date_iso": "2026-09-05",
            "event_date_uncertain": False,
        }
        session.save()
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch("projects.planner_views.create_tasks"),
        ):
            self.client.post(
                reverse("planner_create"),
                data={
                    "description": "Konzert am 5. September",
                    "project_name": "Sommerkonzert",
                    "event_date": (date.today() + timedelta(days=30)).isoformat(),
                    "task_name": ["Programm festlegen"],
                    "task_date": [(date.today() + timedelta(days=7)).isoformat()],
                    "task_kontext": ["Planung"],
                },
            )
        for key in (
            "planner_description",
            "planner_questions_html",
            "planner_answers",
            "planner_review_state",
        ):
            self.assertNotIn(key, self.client.session)


class PlannerStartAiFailureTest(DemoModeTestCase):
    """get_clarifying_questions used to be entirely unguarded — a Claude
    failure here 500'd before the visitor ever saw the questions step."""

    def test_shows_a_german_error_and_keeps_the_description(self):
        self.ai_mocks[
            "projects.planner_views.get_clarifying_questions"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("planner_start"),
            data={
                "description": "Konzert am 5. September",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/planner_start.html")
        self.assertContains(response, "Konzert am 5. September")
        self.assertContains(response, "nicht erstellt werden")


class PlannerReviewAiFailureTest(DemoModeTestCase):
    """generate_plan's retry-once-then-raise (see GeneratePlanRetryTest in
    planner.py) still has to land somewhere other than a 500 — this is that
    landing, and it's the one case in the whole table where the visitor has
    already typed two rounds of input (description, then answers)."""

    def test_shows_a_german_error_and_keeps_description_and_answers(self):
        self.ai_mocks[
            "projects.planner_views.generate_plan"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 5. September",
                "answers": "20 Gäste, in der Kirche",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/planner_questions.html")
        self.assertContains(response, "Konzert am 5. September")
        self.assertContains(response, "20 Gäste, in der Kirche")
        self.assertContains(response, "nicht erstellt werden")


class QuestionsLayoutTest(DemoModeTestCase):
    """#72 step 3: four questions used to sit in a grey box above a single
    free-text field, so anyone answering the later questions typed against a
    list they had scrolled past. The form is now the grid — questions sticky
    on the left, answers on the right — and the answers stay one free-text
    field (decision 4: no parsing of Claude's markdown into per-question
    fields)."""

    def questions_page(self):
        return self.client.post(
            reverse("planner_start"),
            data={"description": "Konzert am 15. September 2026"},
        )

    def test_the_form_is_the_grid(self):
        response = self.questions_page()
        self.assertContains(response, 'class="qa-form"')
        self.assertContains(
            response, "grid-template-columns: repeat(2, minmax(0, 1fr))"
        )
        self.assertContains(response, "position: sticky")

    def test_the_questions_render_inside_the_form(self):
        html = self.questions_page().content.decode()
        self.assertLess(html.index("<form"), html.index('class="questions-box"'))

    def test_the_grey_panel_became_a_left_rule(self):
        response = self.questions_page()
        self.assertContains(
            response,
            ".questions-box { border-left: 2px solid var(--color-border-primary)",
        )
        self.assertNotContains(response, ".questions-box { background")

    def test_markdown_output_survives_the_global_reset(self):
        response = self.questions_page()
        self.assertContains(
            response,
            ".questions-box ul, .questions-box ol { margin: 0 0 8px; padding-left: 20px; }",
        )

    def test_the_error_moved_to_the_shared_notice(self):
        self.ai_mocks[
            "projects.planner_views.generate_plan"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("planner_review"),
            data={"description": "Konzert am 5. September", "answers": "20 Gäste"},
        )
        self.assertContains(response, 'class="error-notice"')
        self.assertContains(response, "nicht erstellt werden")
        self.assertNotContains(response, ".questions-box.error")

    def test_the_dead_field_rules_are_gone(self):
        response = self.questions_page()
        self.assertNotContains(response, ".field-label")
        self.assertNotContains(response, 'input[type="text"]')


class ParseEventDateTest(SimpleTestCase):
    def test_explicit_year_is_used(self):
        self.assertEqual(
            _parse_event_date("Konzert am 5. September 2026"), date(2026, 9, 5)
        )

    def test_without_year_returns_the_next_occurrence(self):
        result = _parse_event_date("Konzert am 5. September")
        self.assertEqual((result.month, result.day), (9, 5))
        self.assertGreater(result, date.today())
        self.assertLessEqual((result - date.today()).days, 366)

    def test_impossible_day_returns_none(self):
        self.assertIsNone(_parse_event_date("Konzert am 31. Februar 2026"))

    def test_unknown_month_returns_none(self):
        self.assertIsNone(_parse_event_date("Konzert am 5. Smarch 2026"))

    def test_no_date_returns_none(self):
        self.assertIsNone(_parse_event_date("Konzert irgendwann im Herbst"))


class GeneratePlanRetryTest(SimpleTestCase):
    """planner_review used to `raise` a json.JSONDecodeError straight at the
    visitor (planner_views.py, pre-#29) — the only place in the app that had
    even looked at a Claude failure, and it still produced a 500. generate_plan
    now retries a bad response once and never raises JSONDecodeError itself."""

    VALID = '{"project_name": "Testkonzert", "tasks": []}'

    def generate(self):
        return generate_plan("Konzert am 5. September", "keine weiteren Angaben", [])

    def test_returns_parsed_dict_on_first_valid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(self.VALID)
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response("not json"),
                _fake_response(self.VALID),
            ]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 2)

    def test_each_retry_attempt_logs_its_own_success_line(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response("not json"),
                _fake_response(self.VALID),
            ]
            with self.assertLogs("projects.ai", level="INFO") as cm:
                self.generate()
        self.assertEqual(len(cm.output), 2)
        for record in cm.output:
            self.assertIn("call=generate_plan", record)
            self.assertIn("outcome=success", record)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response("not json"),
                _fake_response("still not json"),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 2)

    def test_valid_json_that_is_not_an_object_is_retried(self):
        """A bare task array passes json.loads but would crash
        planner_review on plan.get() — it has to count as a bad response,
        not as a success (the third finding from PR #34's review)."""
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response('[{"name": "Programm festlegen", "days_before": 30}]'),
                _fake_response(self.VALID),
            ]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_non_object_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response("[]"),
                _fake_response('"nur ein String"'),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 2)

    def test_an_object_without_a_tasks_list_is_retried(self):
        """A valid object that lacks the "tasks" key passes the dict check
        but would crash planner_review on plan['tasks'] — it has to count
        as a bad response too (the finding from PR #34's review)."""
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = [
                _fake_response('{"project_name": "Testkonzert"}'),
                _fake_response(self.VALID),
            ]
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})
        self.assertEqual(create.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        """The SDK already retried transport failures internally (see
        AnthropicFailureTranslationTest); a second attempt here would silently
        double that budget instead of surfacing the failure."""
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(create.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(f"```json\n{self.VALID}\n```")
            result = self.generate()
        self.assertEqual(result, {"project_name": "Testkonzert", "tasks": []})


class GeneratePlanKontextVocabularyTest(SimpleTestCase):
    """planner.py hardcoded its own "Mögliche Kontexte" line with "Extern"
    where ai.KONTEXTE says "Graphiker" — the actual Notion multi-select
    option (#17). The prompt has to name the one canonical list."""

    def test_prompt_names_the_canonical_kontext_and_not_the_old_one(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(
                '{"project_name": "Testkonzert", "tasks": []}'
            )
            generate_plan("Konzert am 5. September", "keine weiteren Angaben", [])
        prompt = create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Graphiker", prompt)
        self.assertNotIn("Extern", prompt)


class GetClarifyingQuestionsTest(SimpleTestCase):
    def test_translates_an_sdk_failure(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.side_effect = (
                _anthropic_timeout_error()
            )
            with self.assertRaises(AIUnavailableError):
                get_clarifying_questions("Konzert am 5. September", [])

    def test_returns_the_response_text_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = _fake_response(
                "Wie viele Gäste?"
            )
            self.assertEqual(
                get_clarifying_questions("Konzert", []), "Wie viele Gäste?"
            )

    def test_logs_usage_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.return_value = _fake_response(
                "Wie viele Gäste?", input_tokens=64, output_tokens=20
            )
            with self.assertLogs("projects.ai", level="INFO") as cm:
                get_clarifying_questions("Konzert", [])
        [record] = cm.output
        self.assertIn("call=get_clarifying_questions", record)
        self.assertIn("input_tokens=64", record)
        self.assertIn("output_tokens=20", record)
        self.assertIn("outcome=success", record)


@override_settings(DEMO_MODE=False)
class HistoryFallbackTest(TestCase):
    """_get_history() feeds straight into the planner prompt — a Notion
    failure here used to 500 before the visitor's description even reached
    Claude. Falling back to no calibration data is a worse plan, not a
    broken one, so this degrades to [] rather than serving anything stale.
    """

    def test_notion_failure_falls_back_to_an_empty_history(self):
        cache.clear()
        with patch(
            "projects.planner_views.get_historical_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            self.assertEqual(_get_history(), [])


@override_settings(DEMO_MODE=False)
class PlannerCreateNotionFailureTest(TestCase):
    """create_project/create_tasks were unguarded — a Notion failure here
    used to 500 after the visitor had already reviewed and adjusted a full
    task list, losing all of it. It's now redisplayed with the same tasks
    and dates instead of vanishing."""

    def post_plan(self):
        event_date = date.today() + timedelta(days=30)
        task_date = date.today() + timedelta(days=7)
        return self.client.post(
            reverse("planner_create"),
            data={
                "description": "Konzert am 5. September",
                "project_name": "Sommerkonzert",
                "event_date": event_date.isoformat(),
                "task_name": ["Programm festlegen"],
                "task_date": [task_date.isoformat()],
                "task_kontext": ["Planung"],
            },
        )

    def test_notion_failure_redisplays_the_plan_instead_of_losing_it(self):
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch(
                "projects.planner_views.create_project",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/planner_review.html")
        self.assertContains(response, "Sommerkonzert")
        self.assertContains(response, "Programm festlegen")
        self.assertContains(response, "nicht gespeichert")

    def test_a_failure_in_create_tasks_also_redisplays_the_plan(self):
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch(
                "projects.planner_views.create_tasks",
                side_effect=NotionUnavailableError("boom"),
            ),
        ):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Programm festlegen")

    def test_a_failure_in_the_lookup_itself_also_redisplays_the_plan(self):
        with patch(
            "projects.planner_views.find_project",
            side_effect=NotionUnavailableError("boom"),
        ):
            response = self.post_plan()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Programm festlegen")
        self.assertContains(response, "nicht gespeichert")

    def test_the_selected_kontext_is_passed_to_create_tasks(self):
        """planner_views.py used to collect task_kontext via getlist() and
        then silently drop it — the tasks list handed to create_tasks never
        carried a "kontext" key, so the visitor's dropdown selection had no
        effect at all (#17)."""
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch("projects.planner_views.create_tasks") as mock_create_tasks,
        ):
            self.post_plan()
        mock_create_tasks.assert_called_once()
        _, called_tasks = mock_create_tasks.call_args.args
        self.assertEqual(called_tasks[0]["kontext"], ["Planung"])

    def test_a_retry_reuses_the_project_the_failed_attempt_created(self):
        """The error page invites the visitor to re-POST the same plan. If
        the first attempt died between create_project and create_tasks, the
        retry must attach the tasks to the existing page, not create a twin
        project — the duplicate-data finding from PR #34's review."""
        with (
            patch(
                "projects.planner_views.find_project", return_value="page-id"
            ) as mock_find,
            patch("projects.planner_views.create_project") as mock_create_project,
            patch("projects.planner_views.create_tasks") as mock_create_tasks,
        ):
            response = self.post_plan()
        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )
        mock_find.assert_called_once()
        mock_create_project.assert_not_called()
        mock_create_tasks.assert_called_once()
        called_project_id, called_tasks = mock_create_tasks.call_args.args
        self.assertEqual(called_project_id, "page-id")
        self.assertEqual([t["name"] for t in called_tasks], ["Programm festlegen"])

    def test_success_still_redirects_to_the_dashboard(self):
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch("projects.planner_views.create_tasks") as mock_create_tasks,
        ):
            response = self.post_plan()
        # fetch_redirect_response=False: dashboard()'s own behavior has its
        # own tests (DashboardNotionFailureTest); this only checks the
        # redirect target, not a live render of it.
        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )
        mock_create_tasks.assert_called_once()

    def test_a_saved_plan_busts_the_dashboard_cache(self):
        """planner_create used to delete the cache by a hardcoded string —
        a key bump in views.py would silently turn that into a no-op and a
        freshly saved project would hide behind the 8h TTL. #37: the
        never-expiring stale copy must go with it, or a Notion read that
        fails right after this save would serve the dashboard as it looked
        before the project existed."""
        self.addCleanup(cache.clear)
        cache.set(CACHE_KEY, ([], "<p>alt</p>"), 60)
        cache.set(STALE_CACHE_KEY, ([], "<p>alt</p>"), None)
        with (
            patch("projects.planner_views.find_project", return_value=None),
            patch("projects.planner_views.create_project", return_value="page-id"),
            patch("projects.planner_views.create_tasks"),
        ):
            self.post_plan()
        self.assertIsNone(cache.get(CACHE_KEY))
        self.assertIsNone(cache.get(STALE_CACHE_KEY))


class PlannerReplacesExistingPlanNoticeTest(DemoModeTestCase):
    """#129 finding 3: planner_create writes session["demo_plan"]
    unconditionally, so a second run silently replaced the first. The guard is
    a notice at both ends of the flow — entry and review — not a block: the
    visitor who wants exactly this must not pay an extra click for it."""

    def review_page(self):
        return self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 15. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )

    def test_the_tile_step_names_the_plan_it_would_replace(self):
        self.given_session_plan(name="Adventskonzert")
        response = self.client.get(reverse("planner_start"))
        self.assertContains(response, 'class="replace-notice"')
        self.assertContains(response, "Adventskonzert")
        self.assertContains(response, f'href="{reverse("my_plan")}"')

    def test_the_describe_step_carries_the_same_notice(self):
        self.given_session_plan(name="Adventskonzert")
        response = self.client.get(reverse("planner_start") + "?type=konzert")
        self.assertContains(response, 'class="replace-notice"')
        self.assertContains(response, "Adventskonzert")

    def test_the_tile_step_stays_quiet_without_a_plan(self):
        response = self.client.get(reverse("planner_start"))
        self.assertNotContains(response, 'class="replace-notice"')

    def test_the_describe_step_stays_quiet_without_a_plan(self):
        response = self.client.get(reverse("planner_start") + "?type=konzert")
        self.assertNotContains(response, 'class="replace-notice"')

    def test_the_review_step_names_the_plan_above_the_submit_button(self):
        self.given_session_plan(name="Adventskonzert")
        response = self.review_page()
        self.assertContains(response, 'class="replace-notice"')
        self.assertContains(response, "Adventskonzert")
        markup = response.content.decode()
        self.assertLess(
            markup.index('class="replace-notice"'),
            markup.index('class="review-actions"'),
        )

    def test_the_review_step_stays_quiet_without_a_plan(self):
        response = self.review_page()
        self.assertNotContains(response, 'class="replace-notice"')

    def test_creating_still_replaces_the_plan(self):
        """The guard is a notice, not a block — this locks in that the
        behaviour did *not* change."""
        self.given_session_plan(name="Adventskonzert")
        self.client.post(
            reverse("planner_create"),
            data={
                "description": "Sommerfest",
                "project_name": "Sommerfest",
                "event_date": (date.today() + timedelta(days=40)).isoformat(),
                "task_name": ["Bühne bestellen"],
                "task_date": [(date.today() + timedelta(days=10)).isoformat()],
            },
        )
        self.assertEqual(self.client.session["demo_plan"]["name"], "Sommerfest")


@override_settings(DEMO_MODE=False)
class PlannerReplaceNoticeIsDemoOnlyTest(DemoModeTestCase):
    """In production a second project is simply a second project — nothing is
    replaced, so nothing may claim it is."""

    def test_the_review_step_never_carries_the_notice(self):
        self.given_session_plan(name="Adventskonzert")
        response = self.client.post(
            reverse("planner_review"),
            data={
                "description": "Konzert am 15. September 2026",
                "answers": "keine weiteren Angaben",
            },
        )
        self.assertNotContains(response, 'class="replace-notice"')

    def test_the_tile_step_never_carries_the_notice(self):
        self.given_session_plan(name="Adventskonzert")
        response = self.client.get(reverse("planner_start"))
        self.assertNotContains(response, 'class="replace-notice"')
