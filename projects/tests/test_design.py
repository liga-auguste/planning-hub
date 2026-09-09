"""The visual language: tokens, palette, dark theme, layout and the rules
about what may appear on a page at all."""

import re
from datetime import (
    date,
    timedelta,
)
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import (
    SimpleTestCase,
    override_settings,
)
from django.urls import reverse

from ..ai import build_prompt
from .base import (
    DemoModeTestCase,
    PlannerStepsMixin,
    _fake_upcoming_project_with_task,
    _summary_data,
)


class BodyDynamicViewportHeightTest(SimpleTestCase):
    """#99: 100vh resolves to iOS Safari's large viewport (toolbar
    collapsed), so on load — toolbar visible, the default state — the
    sticky footer rendered partly behind the browser chrome. dvh tracks
    the actually-visible area; vh stays first as the fallback, since an
    unsupported declaration is ignored and leaves the earlier one standing."""

    def test_body_keeps_the_vh_fallback_before_the_dvh_upgrade(self):
        css = (settings.BASE_DIR / "projects/static/projects/css/base.css").read_text()
        self.assertLess(css.index("min-height: 100vh"), css.index("min-height: 100dvh"))

    def test_dashboard_sidebar_min_height_stays_untouched(self):
        # dashboard.css's .sidebar rule is a separate, unrelated 100vh use
        # (see #99) — guards against a future refactor collapsing the two.
        css = (
            settings.BASE_DIR / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertNotIn("100dvh", css)


class AiCardDeboxTest(DemoModeTestCase):
    """#96: .ai-card loses its background/border/radius entirely, at every
    width — no breakpoint-specific override, unlike the Kanban board
    (.kanban-card, unchanged — see DeboxingRegressionTest)."""

    def test_ai_card_has_no_background_border_or_radius(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".ai-card { padding: 0; margin-bottom: 40px; font-size: 13px; line-height: 1.6; }",
        )
        self.assertNotContains(response, "border-radius: 8px; padding: 20px 24px;")


class ProjectSectionDeboxTest(DemoModeTestCase):
    """#96 follow-up: .project-section loses its background/border/radius
    too, matching .ai-card — the per-project header (.project-header's own
    border-bottom) and per-task separators (.task-row's border-bottom) stay
    as internal dividers, the same pattern .ai-card already uses."""

    def test_project_section_has_no_background_border_or_radius(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".project-section { margin-bottom: 8px; padding: 0; }",
        )
        self.assertNotContains(response, "border-radius: 8px; padding: 16px 20px;")


class ProjectDateBadgeTest(DemoModeTestCase):
    """The project-header's separate date badge duplicates what's already in
    the project name for real Notion data — the maintainer's own habit is to
    write the event date into the name itself. Demo names carry no such
    date, example projects and a session plan alike (has_session_plan is
    only ever true inside DEMO_MODE — see views.dashboard), so demo_mode
    alone decides it; no has_session_plan check needed here."""

    def test_date_badge_shows_in_demo_mode(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="project-date"')

    @override_settings(DEMO_MODE=False)
    def test_date_badge_is_hidden_in_production(self):
        project = _fake_upcoming_project_with_task()
        with (
            patch("projects.views.get_upcoming_projects", return_value=[project]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="project-date"')
        self.assertContains(response, project["name"])


class ProjectHeaderMobileClearanceTest(DemoModeTestCase):
    """De-boxing .project-section (see ProjectSectionDeboxTest) took the
    20px inset that used to keep the header clear of the fixed mobile
    hamburger launcher (36px, right: 20px — dashboard.css) with it, so the
    title/date ran straight underneath it. padding-right reserves that
    space again; min-height is 48px, not 36 — box-sizing: border-box
    (base.css) counts the header's own padding-bottom: 8px against it, so a
    36px min-height only left 28px of actual band to center the text in,
    landing 6px above the button's center instead of matching it. 48px
    keeps that band a full 36px (48 − 8), matching the button's own height
    and, measured, its center exactly (both 44px down from the viewport
    top). The border-bottom separator is dropped at this width too — the
    button's own bottom edge lands right on it otherwise, reading as a
    stray line through the button rather than a divider under the
    heading."""

    def test_mobile_header_reserves_room_for_the_hamburger(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".project-header { padding-right: 44px; min-height: 48px; "
            "align-items: center; border-bottom: none; }",
        )


class MobileLauncherClearsTheTaskListsTest(DemoModeTestCase):
    """#238: .sidebar-toggle-mobile is position: fixed (top: 26px, right:
    20px — dashboard.css), so whatever scrolls under it is covered. The
    project header has reserved its 44px since #95's follow-up; the lists
    below it never did, and on a phone the launcher sat on top of a task
    row, hiding a date on one and part of a name on another.

    The reservation goes on the rows and the Heute headings rather than on
    their containers: #view-today already holds elements carrying their own
    44px (.ai-card-header, the banners) and a .day-columns grid that bleeds
    into the viewport edge with a negative margin, so a container inset
    would double up on the first and break the second. The headings are in
    because "Diese Woche" carries the week navigation on its right edge.

    The cost is stated rather than hidden: 44px of name width down the whole
    list, for a button occupying only the top ~62px of the viewport. Static
    CSS cannot do better — the alternative is a launcher that hides on
    scroll, which is JS with its own state. After the stacking above the
    name has its own line, which is where that loss hurts least.
    """

    def test_the_lists_reserve_the_same_44px_the_header_does(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, ".task-row, .today-week-heading { padding-right: 44px; }"
        )

    def test_the_header_still_reserves_its_own(self):
        """Left where it is: the two reservations sit on siblings, so
        neither doubles the other up."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, ".project-header { padding-right: 44px;")


class TaskRowMobileStackingTest(DemoModeTestCase):
    """#238: the row was a two-part flex container with nothing stopping its
    parts from colliding. `.task-name` carried no flex at all, so it shrank
    to its min-content width — the longest single word — and `.task-left`
    (min-width: 0) then ended up narrower than its own contents, which the
    unshrinkable `.task-project` overflowed straight across the date beside
    it. Neither box clipped, so the two runs of text simply painted over
    each other.

    The fix is structural rather than a clip: `.task-left` goes, so the row
    itself is the wrapping container, and the name is the one item that
    gives way — it grows, it may shrink past its longest word, and it
    breaks that word rather than overflowing. Nothing is truncated:
    production task names are long, and an ellipsis would hide the part
    that identifies the task.

    Below 768px the row stacks. The name's basis is the full width minus
    the dot's own 15px (7px wide, 8px margin-right), so line one is exactly
    full and everything after it wraps to a second line indented to match
    the name rather than the dot.
    """

    def test_the_name_is_the_item_that_gives_way(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".task-name { font-size: 13px; flex: 1 1 auto; min-width: 0; "
            "overflow-wrap: break-word; }",
        )

    def test_the_row_itself_wraps_and_no_longer_nests_a_left_half(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".task-row { display: flex; align-items: center; flex-wrap: wrap; "
            "padding: 5px 0; border-bottom: 1px solid var(--color-border-primary); }",
        )
        # The class itself, not the string: the rules above explain
        # themselves by naming the half that used to be there.
        self.assertNotContains(response, 'class="task-left"')
        self.assertNotContains(response, ".task-left {")

    def test_the_meta_half_is_styled_rather_than_carrying_inline_styles(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".task-right { display: flex; align-items: center; gap: 4px; "
            "margin-left: auto; }",
        )
        self.assertNotContains(
            response, 'style="display:flex;align-items:center;gap:4px;"'
        )

    def test_below_the_breakpoint_the_name_takes_the_whole_first_line(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, ".task-name { flex-basis: calc(100% - 15px); }")

    def test_below_the_breakpoint_the_meta_indents_under_the_name(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, ".task-project, .task-right { margin-left: 15px; }"
        )
        # .task-due's own desktop 16px would push the date a second indent
        # deep once .task-right starts a line of its own.
        self.assertContains(response, ".task-due { margin-left: 0; }")

    def test_both_task_lists_render_from_the_one_partial(self):
        """The project detail used to hold its own copy of the row markup,
        so every rule above would have had to be true of two templates."""
        contents = (
            settings.BASE_DIR / "projects/templates/projects/dashboard.html"
        ).read_text()
        self.assertIn(
            '{% for task in project.tasks %}{% include "projects/_task_row.html" %}{% endfor %}',
            contents,
        )


class DeboxingRegressionTest(DemoModeTestCase):
    """#96: the sidebar-tile/.ai-card de-boxing explicitly leaves these
    boxed areas untouched — locked in before any code change so a later step
    can't quietly widen the scope. (.project-section joined the de-boxed
    side in a #96 follow-up — see ProjectSectionDeboxTest.)"""

    def test_kanban_card_keeps_its_border(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".kanban-card { background: var(--color-bg-primary); "
            "border: 1px solid var(--color-border-primary); border-radius: 6px; "
            "padding: 8px 10px; margin-bottom: 6px; font-size: 12px; line-height: 1.4; }",
        )

    def test_my_plan_task_list_keeps_border_and_radius(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(
            response,
            ".task-list { background: var(--color-bg-primary); "
            "border: 1px solid var(--color-border-primary); border-radius: 10px; overflow: hidden; }",
        )

    def test_my_plan_summary_box_keeps_border_and_radius(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(
            response,
            ".summary-box { background: var(--color-bg-primary); "
            "border: 1px solid var(--color-border-primary); border-radius: 10px; "
            "padding: 24px 28px; margin-bottom: 24px; line-height: 1.7; font-size: 14px; }",
        )


class DemoBannerNarrowViewportWrapTest(DemoModeTestCase):
    """The demo banner's text and its CTA link sat in a non-wrapping flex
    row, so on a narrow main content area (sidebar expanded, or the sidebar
    collapse from #25 not yet triggered) the CTA squeezed the paragraph
    into a cramped, hard-to-read column instead of dropping to its own
    line. flex-wrap lets it reflow at whatever width actually runs out of
    room, without a hardcoded breakpoint."""

    def test_banner_wraps_instead_of_squeezing(self):
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            ".demo-banner { display: flex; align-items: center; flex-wrap: wrap;",
        )

    def test_cta_no_longer_forces_itself_right_with_a_fixed_margin(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, ".demo-banner-cta { margin-left: auto;")

    def test_text_gets_an_explicit_basis_so_wide_layouts_do_not_wrap_early(self):
        # Without this, flex-wrap breaks the row against the text's full
        # unbroken width even when there's plenty of room for it to shrink
        # and wrap internally instead — see the PR discussion.
        response = self.client.get("/dashboard/")
        self.assertContains(response, ".demo-banner span { flex: 1 1 200px; }")


class BaseResetParityTest(DemoModeTestCase):
    """#64 unit 4: base_public.html reset itself while base_dashboard.html
    inherited Reboot's. Two sources for the same thing is what produces
    unexplainable eight-pixel jumps later, so both now reset the same way.

    The other direction -- dropping our reset and letting Reboot serve both --
    would have added padding-left: 2rem to datenschutz.html's lists and shifted
    those bullets 32px right, on a page under the legal hard constraint."""

    RESET = "* { box-sizing: border-box; margin: 0; padding: 0; }"

    def test_both_bases_reset_the_same_way(self):
        # #32 moved this rule from an inline <style> block (rendered in every
        # response) into a linked stylesheet, so the assertion now reads the
        # served file's source directly rather than the rendered HTML.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()
        self.assertIn(self.RESET, css)

    def test_dashboard_summary_paragraphs_keep_their_spacing(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, ".ai-card p { margin: 0 0 8px; }")

    def test_my_plan_summary_keeps_its_list_indent_and_paragraph_spacing(self):
        self.given_session_plan()
        response = self.client.get("/mein-plan/")
        self.assertContains(
            response, ".summary-box ul, .summary-box ol { padding-left: 20px"
        )
        self.assertContains(response, ".summary-box p { margin: 0 0 8px; }")

    def test_ordered_lists_keep_room_for_their_numbers(self):
        # The reset took Reboot's ol padding with it, and .ai-card / .summary-box
        # hold markdown.markdown() output -- so the tag set they have to survive
        # is the model's, not the one the templates spell out.
        self.assertContains(
            self.client.get("/dashboard/"),
            ".ai-card ol { margin: 0 0 8px; padding-left: 20px; }",
        )
        self.given_session_plan()
        self.assertContains(
            self.client.get("/mein-plan/"),
            ".summary-box ul, .summary-box ol { padding-left: 20px",
        )

    def test_summary_headings_cover_every_level_markdown_can_emit(self):
        # h1-h3 were styled and h4-h6 were left on Reboot's margins, which the
        # reset then zeroed.
        self.given_session_plan()
        pages = {
            ".ai-card": self.client.get("/dashboard/"),
            ".summary-box": self.client.get("/mein-plan/"),
        }
        for prefix, response in pages.items():
            for level in range(1, 7):
                with self.subTest(container=prefix, level=level):
                    self.assertContains(response, f"{prefix} h{level}")

    def test_stats_empty_state_keeps_its_trailing_gap(self):
        response = self.client.get("/stats/")
        self.assertContains(
            response,
            ".empty { color: var(--color-text-quaternary); font-size: 13px; padding: 8px 0; margin-bottom: 16px; }",
        )

    def test_datenschutz_lists_are_untouched(self):
        response = self.client.get("/datenschutz/")
        self.assertContains(response, "ul { margin: 6px 0 8px 18px;")


class DesignTokenTest(DemoModeTestCase):
    """#11: a :root block of custom properties replaces hardcoded hex
    literals, so both base templates need to serve the same token names."""

    TOKENS = (
        "--color-bg-primary",
        "--color-accent",
        "--color-overdue",
        "--color-today",
    )
    RETIRED_LITERALS = ("#c0392b", "#e74c3c", "#e87200", "#e86600")

    def test_both_bases_serve_the_design_tokens(self):
        # #32 moved the :root block out of the inline <style> both base
        # templates used to render and into the linked base.css, so the
        # tokens are checked at their source rather than in every response.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()
        for token in self.TOKENS:
            self.assertIn(token, css)

    def test_the_retired_duplicate_literals_are_gone_from_the_dashboard(self):
        response = self.client.get("/dashboard/")
        for literal in self.RETIRED_LITERALS:
            self.assertNotContains(response, literal)


class MinimalTrafficLightColorTest(DemoModeTestCase):
    """#211: exactly three signal colors — red for overdue, amber for due
    today, green for done — and every other open stage keeps the neutral
    gray #173 collapsed it into. This is the additive reintroduction #173
    reserved: `urgent` stays gray, so only two warm tones exist and the
    narrow-band competition that sank #170 cannot come back."""

    # The badge adopts the app's existing neutral-chip pattern
    # (.task-kontext): red stays the only alarm color.
    NEUTRAL_BADGE = (
        ".date-uncertain-badge { font-size: 11px; font-weight: 600; "
        "color: var(--color-text-quaternary); background: var(--color-bg-tertiary); "
        "border-radius: 4px; padding: 1px 8px; white-space: nowrap; }"
    )

    def base_css(self):
        return (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()

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

    def test_the_urgent_stage_token_stays_retired(self):
        # Bare substring on purpose: it also catches the -tint variant and
        # any comment still leaning on the retired name.
        css = self.base_css()
        self.assertNotIn("--color-urgent", css)
        self.assertIn("--color-today", css)

    def test_the_surviving_status_tokens_stay_declared_in_both_themes(self):
        css = self.base_css()
        self.assertEqual(css.count("--color-overdue:"), 2)
        self.assertEqual(css.count("--color-overdue-tint:"), 2)
        self.assertEqual(css.count("--color-today:"), 2)
        self.assertEqual(css.count("--color-done:"), 2)

    def test_no_rendered_page_serves_the_retired_token(self):
        # The collapse's own drift guard, narrowed to the one stage that
        # stays retired. Safe against false positives: the kanban count
        # selectors and the reschedule JS strip class names, not token
        # names, so this sweep only bites color rules.
        self.given_session_plan()
        pages = {
            "index": self.client.get(reverse("index")),
            "dashboard": self.client.get(reverse("dashboard")),
            "my_plan": self.client.get(reverse("my_plan")),
            "planner_review": self.review_page(),
        }
        for name, response in pages.items():
            with self.subTest(page=name):
                self.assertNotContains(response, "--color-urgent")

    def test_the_date_uncertain_badge_wears_the_neutral_chip(self):
        self.given_session_plan()
        for url in ("dashboard", "my_plan"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(reverse(url)), self.NEUTRAL_BADGE)


def _wcag_contrast(hex_a, hex_b):
    """WCAG 2.1 contrast ratio between two sRGB hex colors.

    Twelve lines rather than a dependency: the suite needs exactly this one
    formula, and #211's acceptance criterion ("every signal color reaches at
    least 3:1 against its own surface") is only checkable if it is computed
    rather than asserted in a commit message.
    """

    def relative_luminance(value):
        value = value.lstrip("#")
        if len(value) == 3:  # --color-bg-primary is declared as #fff
            value = "".join(digit * 2 for digit in value)
        channels = []
        for start in (0, 2, 4):
            channel = int(value[start : start + 2], 16) / 255
            channels.append(
                channel / 12.92
                if channel <= 0.03928
                else ((channel + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    lighter, darker = sorted(
        (relative_luminance(hex_a), relative_luminance(hex_b)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


class SignalColorContrastTest(SimpleTestCase):
    """#211: the traffic light is only worth reintroducing if every signal
    is actually visible. WCAG 2.1 puts the floor for non-text UI at 3:1, and
    a 7px dot sits on two different surfaces — the card (--color-bg-primary)
    on my_plan and the landing mockup, the page itself
    (--color-bg-secondary) on the dashboard, whose .main declares no
    background of its own. Both surfaces have to clear the floor, which is
    why the light values run ~4% darker than the ones the issue computed
    against the card alone."""

    FLOOR = 3.0
    SIGNALS = {
        "--color-overdue": ("#ef4444", "#f87171"),
        "--color-today": ("#b88402", "#f4b00c"),
        "--color-done": ("#46a015", "#7fd85d"),
        "--color-text-quaternary": ("#86848d", "#83868d"),
    }
    SURFACES = {
        "light": {"--color-bg-primary": "#fff", "--color-bg-secondary": "#f9f8f9"},
        "dark": {"--color-bg-primary": "#2c2c2e", "--color-bg-secondary": "#1e1e1e"},
    }

    def base_css(self):
        return (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()

    def declared_value(self, css, token, theme):
        """The value a token resolves to in one theme.

        Light is declared in the first block and dark in the second, so the
        two declarations of a token appear in that order — the same ordering
        the sibling count assertions already rely on.
        """
        values = re.findall(rf"{token}:\s*(#[0-9a-fA-F]{{3,6}});", css)
        self.assertEqual(len(values), 2, f"{token} is not declared exactly twice")
        return values[0 if theme == "light" else 1]

    def test_the_signal_colors_carry_the_computed_values(self):
        css = self.base_css()
        for token, (light, dark) in self.SIGNALS.items():
            for theme, expected in (("light", light), ("dark", dark)):
                with self.subTest(token=token, theme=theme):
                    self.assertEqual(
                        self.declared_value(css, token, theme).lower(), expected
                    )

    def test_every_signal_clears_the_non_text_floor_on_both_surfaces(self):
        css = self.base_css()
        for token in self.SIGNALS:
            for theme, surfaces in self.SURFACES.items():
                signal = self.declared_value(css, token, theme)
                for surface, background in surfaces.items():
                    with self.subTest(token=token, theme=theme, surface=surface):
                        self.assertGreaterEqual(
                            _wcag_contrast(signal, background), self.FLOOR
                        )

    def test_the_helper_agrees_with_the_known_extremes(self):
        # Guards the helper itself: without this, a broken formula would
        # make the assertions above pass silently.
        self.assertAlmostEqual(_wcag_contrast("#000", "#fff"), 21.0, places=2)
        self.assertAlmostEqual(_wcag_contrast("#fff", "#fff"), 1.0, places=2)


class PostponeBadgeRenderingTest(DemoModeTestCase):
    """#171: a small badge with the count, starting at the second move —
    moving something once is normal planning and stays unmarked."""

    def given_task(self, postpone_count):
        # Deliberately not named anything containing "verschoben" — the
        # assertions below check for the badge, not the task's own name.
        return self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Programm festlegen",
                    "date": (date.today() + timedelta(days=3)).isoformat(),
                    "done": False,
                    "postpone_count": postpone_count,
                }
            ]
        )

    def test_a_single_reschedule_carries_no_badge(self):
        self.given_task(postpone_count=1)
        for url in ("dashboard", "my_plan"):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(reverse(url)), "verschoben")

    def test_the_badge_appears_exactly_at_the_threshold_of_two(self):
        self.given_task(postpone_count=2)
        for url in ("dashboard", "my_plan"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(reverse(url)), "2× verschoben")

    def test_a_session_written_before_the_counter_existed_renders_fine(self):
        # No postpone_count key at all — the shared demo default fixture.
        self.given_session_plan()
        for url in ("dashboard", "my_plan"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(reverse(url)).status_code, 200)

    def test_the_kanban_card_shows_the_compact_form(self):
        self.given_task(postpone_count=3)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="3× verschoben">3×</span>')


class DarkThemeTest(DemoModeTestCase):
    """#12: the dark palette lives in base.css as a [data-theme="dark"]
    block, and the preload script that switches the attribute runs before
    either stylesheet loads, on every page that extends a base template."""

    def test_base_css_defines_the_dark_theme_block(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()
        self.assertIn('[data-theme="dark"]', css)
        self.assertIn("--color-bg-secondary: #1e1e1e", css)

    def test_both_bases_run_the_preload_script_before_any_stylesheet(self):
        for url in ("/dashboard/", "/impressum/"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertLess(
                    html.index("setAttribute('data-theme'"),
                    html.index('rel="stylesheet"'),
                )

    def test_preload_script_sets_both_theme_attributes(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "setAttribute('data-theme', theme)")
        self.assertContains(response, "setAttribute('data-bs-theme', theme)")

    def test_both_bases_render_the_theme_toggle(self):
        for url in ("/dashboard/", "/impressum/"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), 'id="theme-toggle"')

    def test_the_toggle_offers_all_three_choices(self):
        response = self.client.get("/dashboard/")
        for choice in ("light", "dark", "system"):
            self.assertContains(response, f'data-theme-choice="{choice}"')

    def test_public_css_does_not_override_the_logo_swap_display_rule(self):
        """#103: `.wordmark img { display: block }` in public.css outranked
        base.css's `.logo-dark { display: none }` by specificity (a class
        plus a type selector beats a lone class), so both logo images
        rendered at once on every base_public.html page. `.wordmark img`
        must only size the logo, never touch `display`."""
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        start = css.index(".wordmark img")
        rule = css[start : css.index("}", start)]
        self.assertNotIn("display", rule)


class CardUsesBootstrapVariablesTest(DemoModeTestCase):
    """#64 unit 3: --bs-card-spacer-x/y are read by .card-body, not by .card,
    and our four cards were bare divs carrying their own padding. Without the
    inner element the spacer variables would have had nothing to act on."""

    def test_stats_tiles_feed_their_values_in(self):
        response = self.client.get("/stats/")
        self.assertContains(response, "--bs-card-bg: var(--color-bg-primary)")
        self.assertContains(
            response, "--bs-card-border-color: var(--color-border-primary)"
        )
        self.assertContains(response, "--bs-card-border-radius: 10px")
        self.assertContains(response, "--bs-card-spacer-x: 20px")
        self.assertContains(response, "--bs-card-spacer-y: 20px")

    def test_stats_renders_all_three_tiles_with_a_card_body(self):
        response = self.client.get("/stats/")
        self.assertContains(
            response, '<div class="card"><div class="card-body">', count=3
        )

    def test_stats_tile_contents_still_render(self):
        response = self.client.get("/stats/")
        self.assertContains(response, "card-value")
        self.assertContains(response, "card-label")

    def test_rules_card_feeds_its_values_in(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "--bs-card-bg: var(--color-bg-primary)")
        self.assertContains(
            response, "--bs-card-border-color: var(--color-border-primary)"
        )
        self.assertContains(response, "--bs-card-border-radius: 8px")
        self.assertContains(response, "--bs-card-spacer-x: 40px")
        self.assertContains(response, "--bs-card-spacer-y: 40px")

    def test_rules_renders_a_card_body(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(
            response, '<div class="card"><div class="card-body">', count=1
        )

    def test_neither_page_paints_over_bootstrap_any_more(self):
        for url in ("/stats/", reverse("rules_list")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotContains(
                    response, ".card { background: #fff; border: 1px solid"
                )

    def test_card_text_keeps_our_body_colour(self):
        # .card sets `color: var(--bs-body-color)` (#212529) rather than
        # inheriting ours, so .card-body has to be told the real value.
        for url in ("/stats/", reverse("rules_list")):
            with self.subTest(url=url):
                self.assertContains(
                    self.client.get(url), "--bs-card-color: var(--color-text-primary)"
                )


class PlannerButtonUsesBootstrapVariablesTest(PlannerStepsMixin, DemoModeTestCase):
    """#64 unit 2: the buttons were `class="btn-primary"` without `.btn`.
    In 5.3 `.btn-primary` only *sets* the --bs-btn-* variables and `.btn` is
    what reads them, so setting variables without adding `.btn` would have
    looked like adoption and changed nothing. Since #72 the variable block
    itself lives in _planner_css.html — one copy served to all three steps."""

    def test_every_submit_button_carries_both_classes(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, 'class="btn btn-primary"')

    def test_every_step_feeds_the_colours_in_as_variables(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, "--bs-btn-bg: var(--color-solid-bg)")
                self.assertContains(
                    response, "--bs-btn-hover-bg: var(--color-solid-bg)"
                )

    def test_every_step_keeps_the_borderless_box(self):
        # `border: none` before; .btn's 1px default would grow the button by
        # 2px in each direction, which a screenshot diff picks up.
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, "--bs-btn-border-width: 0")

    def test_every_step_keeps_our_colour_on_a_disabled_button(self):
        # .btn-primary ships --bs-btn-disabled-bg: #0d6efd, and .btn:disabled
        # reads it. Unreachable while the loading state is a class rather than
        # the disabled attribute -- this pins the colour before that changes.
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(
                    response, "--bs-btn-disabled-bg: var(--color-solid-bg)"
                )
                self.assertContains(
                    response, "--bs-btn-disabled-color: var(--color-solid-text)"
                )
                self.assertContains(
                    response,
                    "--bs-btn-disabled-border-color: var(--color-solid-bg)",
                )

    def test_no_step_paints_over_bootstrap_any_more(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertNotContains(response, ".btn-primary { background: #1a1a1a")
                self.assertNotContains(
                    response, ".btn-primary:hover { background: #333"
                )


class PlannerSharedCssTest(PlannerStepsMixin, DemoModeTestCase):
    """#72: the .btn-primary variable block, .planner-card, .back and
    .error-notice used to be copy-pasted into all three planner templates —
    28 lines, comments included, three times. They live in _planner_css.html
    now, included by the three steps and by nothing else."""

    def test_every_step_serves_the_shared_block_exactly_once(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, "--bs-btn-border-width: 0", count=1)
                self.assertContains(response, ".planner-card")
                self.assertContains(response, ".error-notice")

    def test_the_legal_pages_do_not_inherit_planner_css(self):
        # The guard against a later "just hoist it into the base": base CSS is
        # declared before extra_css at equal specificity, so hoisting would
        # silently override the legal pages' own .back flavour.
        for url in ("/impressum/", "/datenschutz/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotContains(response, ".planner-card")
                self.assertNotContains(response, "--bs-btn-border-width")


class PlannerVisualLanguageTest(PlannerStepsMixin, DemoModeTestCase):
    """#72: the planner steps wore a different product than the landing page
    one click earlier — a 6px near-square button against the landing pill,
    20px headlines with default leading against tracked display type. The
    shared partial now carries the landing figures at working-surface size."""

    def test_every_step_wears_the_landing_pill(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, "--bs-btn-border-radius: 99px")
                self.assertContains(response, "--bs-btn-padding-x: 22px")
                self.assertContains(response, "--bs-btn-padding-y: 11px")
                self.assertContains(response, "--bs-btn-font-weight: 600")
                self.assertNotContains(response, "--bs-btn-border-radius: 6px")

    def test_every_step_carries_the_landing_headline_treatment(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(response, "letter-spacing: -0.02em")
                self.assertContains(response, "text-wrap: balance")

    def test_the_subtitle_is_body_text_not_label_grey(self):
        for step, response in self.steps().items():
            with self.subTest(step=step):
                self.assertContains(
                    response, ".subtitle { color: var(--color-text-tertiary)"
                )


class StepperVisualLanguageTest(PlannerStepsMixin, DemoModeTestCase):
    """#72 decision 1: the four step labels stay as they are — the naming
    question against the landing page's three terms is deliberately deferred,
    and this pins the labels so a later pass cannot rename them silently."""

    LABELS = ["Projekttyp", "Beschreiben", "Klärung", "Review"]

    def test_all_four_labels_render_on_every_step(self):
        pages = {"tiles": self.client.get(reverse("planner_start")), **self.steps()}
        for step, response in pages.items():
            for label in self.LABELS:
                with self.subTest(step=step, label=label):
                    self.assertContains(
                        response, f'<span class="ps-label">{label}</span>'
                    )

    def test_the_active_dot_wears_the_landing_halo_held_still(self):
        # #32 moved this rule from the inline <style> into public.css.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn("box-shadow: 0 0 0 3px rgba(26,26,26,0.10)", css)

    def test_the_track_takes_its_own_row_on_a_phone(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(".top-bar:has(.ps-track) { flex-wrap: wrap;", css)


class TopBarRightStaysBesideTheWordmarkWithoutAStepperTest(DemoModeTestCase):
    """The <560px row-break for .top-bar-right exists so the planner
    stepper's .ps-track gets a full-width row on a phone. Pages with
    nothing but the theme toggle in that slot (landing, legal pages) have
    no such width need — the row-break rule is scoped with :has(.ps-track)
    so those pages keep the toggle on the same row as the wordmark,
    opposite it via .top-bar's own space-between, instead of pushed onto
    its own row underneath."""

    def test_the_row_break_is_scoped_to_pages_with_a_stepper(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(".top-bar-right:has(.ps-track) { display: contents; }", css)
        self.assertNotIn(
            ".top-bar-right:has(.ps-track) { flex-wrap: wrap; width: 100%;", css
        )


class ThemeToggleStaysOppositeWordmarkWithStepperTest(DemoModeTestCase):
    """#114: .top-bar-right used to wrap the theme toggle together with
    .ps-track below 560px, so the toggle got pushed onto its own row under
    the wordmark instead of staying opposite it. .top-bar-right now turns
    into display: contents at that width, making .ps-track and the toggle
    direct flex items of .top-bar itself — order keeps the toggle on row 1
    and only .ps-track (full width) wraps onto row 2, centered by its own
    auto margins."""

    def test_the_theme_toggle_is_ordered_first(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(".top-bar-right .theme-toggle { order: 1; }", css)

    def test_the_track_is_ordered_second_and_centered(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(
            ".ps-track { order: 2; width: 100%; max-width: 320px; margin: 0 auto; }",
            css,
        )


class LandingFlowStepOrderTest(DemoModeTestCase):
    """#86: nothing stopped a visitor from hovering step 2 or 3 before step 1,
    and reveal() still fast-forwarded data-reached to match. The handler now
    refuses to open a step until every step before it has been reached."""

    def test_reveal_refuses_to_open_ahead_of_reached(self):
        response = self.client.get("/")
        self.assertContains(
            response, "if (Number(flow.dataset.reached || 0) < i) return;"
        )

    def test_the_pulse_is_more_pronounced(self):
        response = self.client.get("/")
        self.assertContains(response, "opacity: 0.7; transform: scale(1);")
        self.assertContains(response, "transform: scale(3.4);")
        self.assertNotContains(response, "scale(2.8)")


class LandingMobileSeqTest(DemoModeTestCase):
    """#117: below 768px the flow curve's plain accordion is gone; a card
    sequence now walks a visitor through idea input, a simulated clarifying
    question and plan generation, ending on a static preview of the plan."""

    def test_seq_markup_present(self):
        response = self.client.get("/")
        self.assertContains(response, '<div class="seq" data-step="0">')

    def test_seq_deck_has_three_cards(self):
        response = self.client.get("/")
        self.assertContains(response, "Idee beschreiben")
        self.assertContains(response, "Rückfragen klären")
        self.assertContains(response, "Plan generieren")

    def test_headline_wrapped_without_duplicating_text(self):
        response = self.client.get("/")
        self.assertContains(response, '<div class="seq-headline">')
        self.assertEqual(
            response.content.decode().count("Aus einer Idee wird ein Plan"), 1
        )

    def test_static_task_list_has_no_interaction_hooks(self):
        response = self.client.get("/")
        self.assertNotContains(response, "data-task-id")
        self.assertNotContains(response, 'onclick="toggleTask')
        self.assertContains(response, '<span class="dot')

    def test_seq_card_deck_is_keyboard_accessible(self):
        response = self.client.get("/")
        self.assertContains(response, 'role="button"')
        self.assertContains(response, 'class="seq-deck"')

    def test_seq_hidden_on_desktop_by_default(self):
        response = self.client.get("/")
        self.assertContains(response, ".seq, .seq-cta { display: none; }")

    def test_reduced_motion_block_covers_new_classes(self):
        response = self.client.get("/")
        css = response.content.decode()
        rm_start = css.index("@media (prefers-reduced-motion: reduce)")
        rm_block = css[rm_start : rm_start + 900]
        self.assertIn(".seq-caret", rm_block)
        self.assertIn(".seq-skel-row", rm_block)
        self.assertIn(".seq-headline", rm_block)

    def test_static_plan_has_no_progress_indicator(self):
        # #117 follow-up: a freshly generated plan has nothing done yet, so
        # the progress bar and "x / 10 erledigt" count were dropped —
        # neither carries information at this point in the sequence.
        response = self.client.get("/")
        self.assertContains(response, "Freitag, 16.5.")
        self.assertNotContains(response, "erledigt")
        self.assertNotContains(response, "progress-bar-wrap")


class FooterPinningTest(DemoModeTestCase):
    """Part A of #21: the footer pinning rules used to live only in
    landing.html's extra_css override, so every other public page had the
    footer floating mid-viewport. They belong in base_public.html.

    #32 later moved these rules out of the base templates' inline <style>
    into base.css/public.css, so they're checked at the source rather than
    in the rendered response."""

    def test_base_template_makes_body_a_flex_column(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/base.css"
        ).read_text()
        self.assertIn("display: flex; flex-direction: column;", css)

    def test_base_template_pins_the_footer(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn("margin-top: auto; padding-top: 20px;", css)

    def test_landing_page_no_longer_duplicates_the_override(self):
        # The base rule lives in public.css now, not in any rendered <style>,
        # so the page's own inline extra_css should carry no copy of it.
        response = self.client.get("/")
        self.assertNotContains(response, "margin-top: auto")

    def test_wrapper_padding_for_the_floating_footer_is_gone(self):
        # The 80px bottom padding only existed to keep content clear of the
        # floating footer; once pinned it would double the spacing.
        response = self.client.get("/impressum/")
        self.assertNotContains(response, "padding: 32px 20px 80px")


class FooterOnDashboardPagesTest(DemoModeTestCase):
    """Part B of #21: dashboard pages rendered no footer at all, so the
    legally required Impressum/Datenschutz links were missing there. The
    cookie banner also links to Datenschutz, hence the full-markup asserts."""

    def assert_has_footer(self, response):
        self.assertContains(response, "page-footer")
        self.assertContains(response, '<a href="/impressum/">Impressum</a>')
        self.assertContains(response, '<a href="/datenschutz/">Datenschutz</a>')
        self.assertContains(response, "© 2026 Liga Auguste")

    def test_dashboard_has_the_footer(self):
        self.assert_has_footer(self.client.get("/dashboard/"))

    def test_my_plan_has_the_footer(self):
        self.given_session_plan()
        self.assert_has_footer(self.client.get("/mein-plan/"))

    def test_stats_has_the_footer(self):
        self.assert_has_footer(self.client.get("/stats/"))

    def test_planner_start_keeps_its_footer(self):
        self.assert_has_footer(self.client.get(reverse("planner_start")))


class PlannerCardFooterBorderTest(DemoModeTestCase):
    """The footer's border-top sat close under the stepper on a phone,
    reading as two parallel lines a few pixels apart on every planner step
    (the stepper wraps to its own full-width row there). On desktop the
    line has room to breathe and stays on every step, planner or not —
    :has(.ps-track) scopes the removal to the mobile breakpoint only."""

    def test_the_footer_border_is_dropped_on_mobile_with_a_stepper(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/public.css"
        ).read_text()
        self.assertIn(".wrapper:has(.ps-track) .page-footer { border-top: none; }", css)


class TileIconsTest(DemoModeTestCase):
    """#44: the eight emoji tile icons become Lucide line icons, delivered as
    one <symbol> sprite referenced by <use> — currentColor resolves through
    the <use> shadow tree, so the icons inherit the tile's colour. What no
    assertion can catch: a wrong viewBox or a leftover fill attribute shows
    up as a black blob only in a browser."""

    EMOJI = ["🎵", "💍", "👤", "🚀", "🎓", "📣", "🤝", "🏗"]

    def test_no_tile_emoji_renders_on_step_one(self):
        response = self.client.get(reverse("planner_start"))
        for emoji in self.EMOJI:
            with self.subTest(emoji=emoji):
                self.assertNotContains(response, emoji)

    def test_nine_references_and_nine_definitions(self):
        # 9 tile icons plus the 3 sun/moon/monitor icons _theme_toggle.html
        # (#12) now renders on every public page, this one included.
        response = self.client.get(reverse("planner_start"))
        self.assertContains(response, '<use href="#icon-', count=12)
        self.assertContains(response, '<symbol id="icon-', count=12)

    def test_the_icons_inherit_the_tiles_colour(self):
        response = self.client.get(reverse("planner_start"))
        self.assertContains(response, "stroke: currentColor")

    def test_the_ninth_tile_gets_the_pencil(self):
        # The one reference no other assertion would catch: "Eigenes Projekt"
        # had no icon at all before, so a count of nine could also mean a
        # duplicate on the eight.
        response = self.client.get(reverse("planner_start"))
        self.assertContains(response, '<symbol id="icon-pencil"')
        self.assertContains(response, '<use href="#icon-pencil"')

    def test_step_two_ships_no_tile_icon_sprite(self):
        # The theme toggle's own icon-theme-* sprite (#12) still renders here
        # — it's a base-template fixture on every public page — but none of
        # step one's nine tile icons should follow the user to step two.
        response = self.client.get(reverse("planner_start") + "?type=eigenes")
        self.assertContains(response, '<symbol id="icon-theme-')
        self.assertNotContains(response, '<symbol id="icon-pencil"')


class TemplateCommentTest(DemoModeTestCase):
    """Django's {# #} is single-line only, so one spanning several lines is not
    parsed as a comment and renders literally. It shipped twice: caught by
    accident in #57, and live in production from #56 until this test existed.
    Both dashboard modes are covered — the second leak sat in the per-task
    markup, which only the task rows render. my_plan is covered too, since #7
    added its own multi-line comment above {% block body %}."""

    def assertNoLeakedComment(self, response):
        for marker in ("{#", "#}"):
            self.assertNotContains(response, marker)

    def test_the_multi_view_renders_no_template_comment(self):
        self.assertNoLeakedComment(
            self.client.get(reverse("dashboard") + "?mode=multi")
        )

    def test_the_single_plan_view_renders_no_template_comment(self):
        self.given_session_plan()
        self.assertNoLeakedComment(self.client.get(reverse("dashboard")))

    def test_my_plan_renders_no_template_comment(self):
        self.given_session_plan()
        self.assertNoLeakedComment(self.client.get(reverse("my_plan")))


class PictographicEmojiTest(DemoModeTestCase):
    """#23: pictographic emoji are not part of the design language. The prompt
    symbols count as UI too — the language convention already treats prompts
    and their output as user-facing, and Claude echoes their register back
    into what renders on screen."""

    def test_the_summary_prompt_carries_no_pictographic_emoji(self):
        project = {
            "name": "Sommerkonzert",
            "event_date": date.today() + timedelta(days=10),
            "performers": "",
            "tasks": [
                {
                    "name": "Plakate aushängen",
                    "done": False,
                    "due": date.today(),
                    "kontext": ["Unterwegs"],
                },
                {
                    "name": "Programm festlegen",
                    "done": True,
                    "due": None,
                    "kontext": [],
                },
            ],
        }
        prompt = build_prompt([project], date.today())
        for glyph in ("✅", "⚠", "☐"):
            self.assertNotIn(glyph, prompt)

    def test_my_plan_decorates_no_date_with_an_emoji(self):
        self.given_session_plan()
        self.assertNotContains(self.client.get(reverse("my_plan")), "📅")


class SignalDotColorTest(DemoModeTestCase):
    """Descendant of the #161 drift guard, same purpose after #211: if the
    palettes split across surfaces again, the suite should say. It now
    covers all three signal dots — overdue red, today amber, done green —
    and still forbids a dot rule for the retired urgent stage."""

    OVERDUE_RULE = ".dot.overdue { background: var(--color-overdue); }"
    TODAY_RULE = ".dot.today { background: var(--color-today); }"
    DONE_RULE = ".dot.done { background: var(--color-done); }"

    def pages(self):
        self.given_session_plan()
        return {
            "dashboard": self.client.get(reverse("dashboard")),
            "my_plan": self.client.get(reverse("my_plan")),
            "index": self.client.get(reverse("index")),
        }

    def test_every_surface_serves_the_red_overdue_dot(self):
        for name, response in self.pages().items():
            with self.subTest(page=name):
                self.assertContains(response, self.OVERDUE_RULE)

    def test_every_surface_serves_the_amber_today_dot(self):
        for name, response in self.pages().items():
            with self.subTest(page=name):
                self.assertContains(response, self.TODAY_RULE)

    def test_the_done_dot_carries_the_completion_green(self):
        # Not on index: the landing mockup renders no done rows, so it
        # serves no done rule to drift.
        pages = self.pages()
        for name in ("dashboard", "my_plan"):
            with self.subTest(page=name):
                self.assertContains(pages[name], self.DONE_RULE)

    def test_the_done_rule_stays_after_the_today_rule(self):
        # applyDone() toggles the done class on without stripping the
        # urgency class, so a task due today becomes class="dot today done".
        # Equal specificity means source order decides: if .dot.today ever
        # drifted below .dot.done, checking off a today task would leave the
        # dot amber. The templates say this in a comment; this pins it.
        pages = self.pages()
        for name in ("dashboard", "my_plan"):
            with self.subTest(page=name):
                html = pages[name].content.decode()
                self.assertLess(html.index(self.TODAY_RULE), html.index(self.DONE_RULE))

    def test_no_surface_serves_an_urgent_dot_rule(self):
        for name, response in self.pages().items():
            with self.subTest(page=name):
                self.assertNotContains(response, ".dot.urgent { background")

    def test_every_surface_serves_the_amber_today_date_label(self):
        pages = self.pages()
        self.assertContains(
            pages["dashboard"],
            ".task-due.today { color: var(--color-today); font-weight: 500; }",
        )
        for name in ("my_plan", "index"):
            with self.subTest(page=name):
                self.assertContains(
                    pages[name], ".task-date.today { color: var(--color-today); }"
                )
