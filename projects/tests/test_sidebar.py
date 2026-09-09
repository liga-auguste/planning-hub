"""The sidebar: nav, project list, progress rings and its behaviour across
viewports and views."""

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

from ..models import WeekCloseout
from ..notion import NotionUnavailableError
from ..views import CACHE_KEY
from .base import (
    CLOSEOUT_TODAY,
    DemoModeTestCase,
    _closeout_tasks,
    _fake_upcoming_project,
    _fake_upcoming_project_with_task,
    _summary_data,
)


class SidebarInfoIconTest(DemoModeTestCase):
    """The 'Über dieses Projekt' link used a plain 'ℹ' character. iOS gives
    it emoji presentation by default (a coloured icon, not a text glyph),
    which this project's design language reserves for typographic symbols
    only — a pictographic character needs a Lucide SVG instead, the way
    every other icon in this codebase already does it."""

    def test_the_emoji_character_is_gone(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, "ℹ")

    def test_an_inline_svg_takes_its_place(self):
        response = self.client.get("/dashboard/")
        self.assertContains(
            response, '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/>'
        )


class SidebarMobileDefaultCollapseTest(DemoModeTestCase):
    """#25: the sidebar's fixed 320px width used to render at every
    viewport. Below the tablet breakpoint the rest of the app already uses,
    it now always starts (and stays) collapsed — including over a stored
    "open" preference from a prior desktop session, since arriving on a
    phone with someone else's desktop choice claiming 320px of the screen
    defeats the point of the breakpoint. A stored "closed" preference still
    applies on desktop, both on load and when a MediaQueryList change
    listener keeps this in sync live as the window crosses the breakpoint
    mid-session."""

    def test_load_script_checks_the_breakpoint(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "window.matchMedia('(max-width: 768px)')")

    def test_mobile_wins_over_a_stored_preference_on_load(self):
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            "const startCollapsed = tabletBreakpoint.matches || storedCollapsed === 'true';",
        )

    def test_the_breakpoint_listener_also_lets_mobile_win(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "tabletBreakpoint.addEventListener('change'")
        self.assertContains(
            response,
            "const collapsed = e.matches || localStorage.getItem('sidebarCollapsed') === 'true';",
        )


class SidebarMobileOverlayTest(DemoModeTestCase):
    """Below the tablet breakpoint, collapsing the sidebar used to shrink it
    to a 48px rail — the same desktop push behaviour, just squeezed onto a
    phone screen. It's now a full-screen overlay instead: a backdrop, a
    launcher button reachable even while the panel itself is off-screen,
    and the main content never reflows. The scrim/launcher visibility is
    pure CSS (sibling selectors keyed off .sidebar.collapsed), so the JS
    only needs to wire both extra controls to the same toggle."""

    def test_backdrop_and_launcher_button_are_rendered(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, 'id="sidebar-backdrop"')
        self.assertContains(response, 'id="sidebar-toggle-mobile"')

    def test_sidebar_becomes_an_overlay_on_mobile(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn("transform: translateX(-100%);", css)
        self.assertIn(".sidebar:not(.collapsed) { transform: translateX(0); }", css)

    def test_content_never_reflows_on_mobile(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(".main, .main.sidebar-collapsed { margin-left: 0; }", css)

    def test_both_extra_controls_share_the_existing_toggle(self):
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            "if (toggleBtnMobile) toggleBtnMobile.addEventListener('click', toggleSidebar);",
        )
        self.assertContains(
            response, "if (backdrop) backdrop.addEventListener('click', toggleSidebar);"
        )


class SidebarMobileWidthAndScrollClearanceTest(DemoModeTestCase):
    """A phone-width sidebar overlay surfaced two problems a fixed 260px
    panel never showed on desktop: a flat 260px is either cramped on a
    small phone or leaves an odd sliver of backdrop on a large one, and the
    collapse arrow / theme toggle — both position: absolute within the
    scrolling .sidebar, so they stay pinned over the visible box instead of
    scrolling away — had no reserved space and sat directly on top of the
    first and last nav items."""

    def setUp(self):
        super().setUp()
        self.css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()

    def test_mobile_sidebar_width_is_proportional_not_fixed(self):
        self.assertIn(
            "@media (max-width: 768px) {\n    .sidebar, .sidebar.collapsed {",
            self.css,
        )
        self.assertIn("width: min(85vw, 320px);", self.css)

    def test_sidebar_content_reserves_room_for_the_pinned_controls(self):
        self.assertIn(".sidebar-content { padding: 24px 0 56px; }", self.css)

    def test_theme_toggle_gets_an_opaque_full_width_bar_on_mobile(self):
        # Real (longer, wrapping) event names mean any item can scroll
        # through the toggle's screen position, not just the last one —
        # its own pill background only covered itself, so text either side
        # showed through instead of being cleanly hidden underneath.
        self.assertIn(
            ".sidebar .theme-toggle {\n        left: 0; right: 0; width: 100%;\n"
            "        transform: none;\n        justify-content: flex-end;\n"
            "        background: var(--color-bg-primary);",
            self.css,
        )


class SidebarCollapseClearsInlineWidthTest(DemoModeTestCase):
    """#137: the resize handle stores its result as inline styles on
    .sidebar and .main. An inline style always beats a stylesheet rule, so
    after any drag the collapse paths — which only toggled classes — left
    the panel at its dragged width instead of the 48px rail. Every collapse
    path now clears the inline styles, and every expand path restores the
    stored width through one shared helper that also refuses to put a
    desktop drag width onto the mobile overlay."""

    def setUp(self):
        super().setUp()
        self.response = self.client.get("/dashboard/")

    def test_collapsing_clears_both_inline_styles(self):
        self.assertContains(
            self.response,
            "function clearInlineWidth() {\n"
            "        sidebar.style.width = '';\n"
            "        main.style.marginLeft = '';\n"
            "    }",
        )

    def test_toggle_and_breakpoint_listener_clear_or_restore(self):
        # The same clear-or-restore pair must sit in both dynamic collapse
        # paths: toggleSidebar() and the tabletBreakpoint change listener.
        self.assertContains(
            self.response,
            "if (collapsed) clearInlineWidth();\n        else applySavedWidth();",
            count=2,
        )

    def test_the_initial_collapsed_load_also_clears(self):
        self.assertContains(
            self.response,
            "toggleBtn.textContent = '›';\n        clearInlineWidth();",
        )

    def test_saved_width_is_never_restored_below_the_tablet_breakpoint(self):
        self.assertContains(self.response, "if (tabletBreakpoint.matches) return;")


class SidebarDefaultWidthTest(DemoModeTestCase):
    """The 320px default left a lot of empty space next to the nav labels
    and project names it actually holds — 260px still comfortably fits the
    demo data's longest entry without wrapping, with room to spare for
    real, somewhat longer event names. The drag-resize range (180-500px in
    base_dashboard.html) is unrelated and untouched. #96's floating-tile
    change offsets .main's margin-left by the fixed 24px sidebar gap, so it
    no longer matches .sidebar's width 1:1 — see SidebarFloatingTileTest."""

    def test_sidebar_and_main_agree_on_the_narrower_width(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(".sidebar {\n    width: 260px;", css)
        self.assertIn(".main {\n    margin-left: 284px;", css)


class SidebarFloatingTileTest(DemoModeTestCase):
    """#96: the sidebar becomes a floating tile — inset from the viewport
    edge with rounded corners and a shadow instead of a flush panel with a
    hard border. .main's margin-left grows by the fixed 24px gap (12px inset
    + 12px space to the content) so the content doesn't creep back under the
    now-floating sidebar."""

    def setUp(self):
        super().setUp()
        self.css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()

    def test_sidebar_has_no_flush_border(self):
        self.assertNotIn(
            "border-right: 1px solid var(--color-border-primary);", self.css
        )

    def test_sidebar_is_rounded_and_elevated(self):
        self.assertIn("border-radius: 12px;", self.css)
        self.assertIn("box-shadow: var(--shadow-medium);", self.css)

    def test_sidebar_is_inset_from_the_viewport_edge(self):
        self.assertIn("top: 12px; left: 12px; bottom: 12px;", self.css)

    def test_main_margin_left_accounts_for_the_gap(self):
        self.assertIn(".main {\n    margin-left: 284px;", self.css)
        self.assertIn(".main.sidebar-collapsed { margin-left: 72px; }", self.css)

    def test_mobile_overlay_keeps_its_own_shadow_and_full_width(self):
        # Explicitly untouched by the floating-tile change (see plan on #96).
        self.assertIn("transform: translateX(-100%);", self.css)
        self.assertIn("box-shadow: var(--shadow-medium);", self.css)

    def test_drag_resize_keeps_the_gap_offset(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, "main.style.marginLeft = (width + SIDEBAR_GAP) + 'px';"
        )

    def test_mobile_launcher_shares_the_content_bodys_right_inset(self):
        # Production mode's "Aktualisieren" button (dashboard.html, inside
        # .ai-card's flex row, margin-left: auto) sits at .content-body's
        # own right padding. The launcher needs the same value, or the two
        # buttons land 8px apart instead of sharing one edge.
        content_body_padding = 20
        self.assertIn(f"right: {content_body_padding}px; z-index: 1002;", self.css)

    def test_mobile_launcher_keeps_its_fixed_top_inset(self):
        # The 26px was originally derived from the content-area wordmark
        # row's center axis; #95 moved the logo into the sidebar, so the
        # button now simply keeps its established top-right spot as a
        # plain fixed inset.
        self.assertIn("top: 26px; right: 20px;", self.css)

    def test_content_body_padding_shrinks_below_the_tablet_breakpoint(self):
        # The desktop 40px/48px padding left barely 3/4 of a phone's width
        # for content once .main went full-bleed there.
        self.assertIn(".content-body { padding: 24px 20px; }", self.css)
        self.assertIn(".page-footer { padding: 20px 20px 32px; }", self.css)
        self.assertLess(
            self.css.index(".content-body { padding: 40px 48px; flex: 1; }"),
            self.css.index(".content-body { padding: 24px 20px; }"),
        )

    def test_main_rule_precedes_the_mobile_override_in_the_cascade(self):
        # #96 follow-up: .main's base margin-left and the @media override
        # that zeroes it on mobile carry equal specificity, so whichever
        # one is later in the stylesheet wins regardless of the media
        # condition. The base rule has to come first, or the override is
        # silently dead on every width, mobile included.
        self.assertLess(
            self.css.index(".main {\n    margin-left: 284px;"),
            self.css.index(".main, .main.sidebar-collapsed { margin-left: 0; }"),
        )


def _sidebar_group(html, heading, count):
    """The first `count` entry labels under one .sidebar-title heading.

    A bare assertContains cannot tell the groups apart any more: both list
    "Dashboard" and "Heute", which is exactly what the headings exist to
    disambiguate. The leading icon is stripped off each label — it is still
    a dot in one state and the grid glyph in another, deliberately, so an
    assertion about wording must not trip over it.
    """
    _, after = html.split(f">{heading}</div>", 1)
    entries = re.findall(
        r'<a class="sidebar-item[^"]*"[^>]*>(.*?)</a>', after, re.DOTALL
    )
    labels = []
    for entry in entries[:count]:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", entry)).strip()
        labels.append(re.sub(r"^[^\wÄÖÜäöüß]+", "", text))
    return labels


class SidebarModeGroupingTest(DemoModeTestCase):
    """#183 follow-up, second round: grouping the sidebar by which data was
    currently on screen still meant the *set* of visible links reshuffled
    between states — one group had four links, the other one, and which was
    which kept swapping. That reshuffling itself read as "too much back and
    forth". Both "Dein Projekt" and "Demo" headers, and each one's full link
    set, are now always present regardless of state — only the project list
    further down still switches by context. Whichever group matches the
    currently-loaded page keeps the fast client-side toggle
    (id="nav-overview"/"nav-today"); the other group's Dashboard/Heute are
    plain links to the other mode, since that data isn't loaded here."""

    def test_session_plan_shows_full_links_in_both_groups(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response,
            '<div class="sidebar-title" style="margin-top: 16px;">Demo-Projekte</div>',
        )
        self.assertContains(response, "Plan als Liste")
        self.assertContains(response, "Woche abschließen")
        html = response.content.decode()
        self.assertEqual(
            _sidebar_group(html, "Demo-Projekte", 2), ["Dashboard", "Heute"]
        )

    def test_no_plan_yet_still_shows_both_group_headers(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response,
            '<div class="sidebar-title" style="margin-top: 16px;">Demo-Projekte</div>',
        )
        self.assertContains(response, "Projekt selbst planen")

    def test_multi_project_view_with_a_plan_shows_full_links_in_both_groups(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response,
            '<div class="sidebar-title" style="margin-top: 16px;">Demo-Projekte</div>',
        )
        self.assertContains(response, "Plan als Liste")
        self.assertContains(response, "Woche abschließen")
        # Both groups carry the same two entries; the headings say which
        # data each one is over. Asserted as a pair rather than by substring,
        # because "Dashboard" now appears in both by design.
        html = response.content.decode()
        self.assertEqual(
            _sidebar_group(html, "Dein Projekt", 2), ["Dashboard", "Heute"]
        )
        self.assertEqual(
            _sidebar_group(html, "Demo-Projekte", 2), ["Dashboard", "Heute"]
        )

    def test_the_demo_overview_reads_the_same_in_every_state(self):
        """One view, one name. The demo overview is reachable in three
        states, and it used to read "Mehrprojekt-Dashboard" in the first —
        where it is a jump-in link — and "Dashboard" in the other two, where
        it is the fast toggle for the view already on screen. Same view,
        different data, so the entry is "Dashboard" throughout and the group
        heading carries the difference.

        The three are asserted together on purpose: each state already had a
        test of its own, and the wording drifted apart anyway, because
        nothing compared them.
        """
        self.given_session_plan()
        states = {
            "own plan on screen": self.client.get(reverse("dashboard")),
            "demo on screen, plan saved": self.client.get(
                reverse("dashboard") + "?mode=multi"
            ),
            # A second client, so this one has no plan in its session.
            "demo on screen, no plan": Client().get(reverse("dashboard")),
        }
        for state, response in states.items():
            with self.subTest(state=state):
                labels = _sidebar_group(response.content.decode(), "Demo-Projekte", 2)
                self.assertEqual(labels, ["Dashboard", "Heute"])


@override_settings(DEMO_MODE=False)
class SidebarModeGroupingProductionTest(TestCase):
    def test_production_shows_no_mode_grouping(self):
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value={"jetzt_faellig": [], "naechste_woche": []},
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertNotContains(
            response, '<div class="sidebar-title">Dein Projekt</div>'
        )


class SidebarLogoHeaderTest(DemoModeTestCase):
    """#95: the logo moves from the content area into a sidebar header row —
    a real link to /dashboard/ above the nav. Collapsed, only the icon mark
    stays visible in the 48px rail; the wordmark span is hidden via CSS."""

    templates = Path(settings.BASE_DIR) / "projects/templates/projects"

    def setUp(self):
        super().setUp()
        self.base_html = (self.templates / "base_dashboard.html").read_text()
        self.dashboard_html = (self.templates / "dashboard.html").read_text()
        self.css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()

    def test_header_links_logo_and_wordmark_to_the_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertIn("{% url 'dashboard' %}", self.base_html)
        self.assertIn("logo_schwarz.png", self.base_html)
        self.assertIn("logo_weiss.png", self.base_html)
        self.assertIn(
            '<span class="sidebar-logo-text">Planning Hub</span>', self.base_html
        )

    def test_header_is_a_sibling_of_sidebar_content_not_a_child(self):
        # .sidebar.collapsed .sidebar-content { display: none } would take a
        # nested header down with it — same constraint the theme toggle
        # already documents (#12). Source order in the template is enough to
        # prove the header sits before (and thus outside) .sidebar-content.
        self.assertLess(
            self.base_html.index('class="sidebar-header"'),
            self.base_html.index('<div class="sidebar-content">'),
        )

    def test_collapse_arrow_shares_the_header_center_axis(self):
        # The 28px logo starts at the sidebar's 24px padding-top, putting
        # its center at 38px; the arrow's 20px box (16px glyph + 2px
        # padding each side) needs top: 28px to share that axis.
        sidebar_padding_top = 24
        logo_height = 28
        arrow_box_height = 16 + 2 * 2
        arrow_top = sidebar_padding_top + logo_height / 2 - arrow_box_height / 2
        self.assertIn(f"top: {int(arrow_top)}px; right: 12px;", self.css)

    def test_collapsed_rail_hides_only_the_wordmark(self):
        self.assertIn(
            ".sidebar.collapsed .sidebar-logo-text { display: none; }", self.css
        )

    def test_link_has_an_accessible_name_independent_of_collapse(self):
        # Both <img> alts are empty and the wordmark span disappears when
        # collapsed — without a static aria-label the link would have no
        # accessible name in the collapsed rail.
        self.assertIn('aria-label="Planning Hub', self.base_html)

    def test_old_logo_row_left_the_content_area(self):
        # The 40px sizing was unique to the removed #view-overview row; the
        # about overlay's own 36px copy must survive (next test).
        self.assertNotIn("height:40px;width:auto;", self.dashboard_html)

    def test_about_overlay_keeps_its_own_logo_copy(self):
        # #183 follow-up: moved from dashboard.html into _about_overlay.html
        # so "Über dieses Projekt" (and the overlay it opens) is shared with
        # my_plan.html/close_week_start.html/week_review.html too.
        about_overlay_html = (self.templates / "_about_overlay.html").read_text()
        self.assertIn('height:36px;" class="logo-light"', about_overlay_html)
        self.assertIn('height:36px;" class="logo-dark"', about_overlay_html)

    def test_first_content_row_clears_the_mobile_launcher(self):
        # The removed wordmark row used to keep the top-of-page band free
        # of the fixed hamburger launcher on mobile; every element that can
        # now render first reserves the button's footprint on the right,
        # mirroring .project-header's existing reservation. .timelapse-bar
        # joined the list once a demo session showed the launcher sitting on
        # its top-right moment tile — it renders before the banners in both
        # views, so it is first more often than any of them.
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".demo-banner, .sim-banner, .stale-notice, .ai-card-header, "
            ".timelapse-bar { padding-right: 44px; }",
        )

    def test_pages_without_a_sidebar_render_no_header(self):
        # stats.html overrides {% block body %} entirely and ships no
        # sidebar — it's a maintainer-only page, linked from nowhere in the
        # UI, so it sits outside the "sidebar guides you through the app"
        # principle #183's follow-up applied to my_plan/close_week/
        # week_review (see SidebarNavOnStandalonePagesTest).
        response = self.client.get(reverse("stats"))
        self.assertNotContains(response, "sidebar-header")


class SidebarNavOnStandalonePagesTest(DemoModeTestCase):
    """#183 follow-up: my_plan.html, close_week_start.html and
    week_review.html used to override {% block body %} entirely, same as
    stats.html — but unlike stats.html, these three ARE reachable by
    clicking through the sidebar (Plan als Liste, Woche abschließen), so
    leaving without a sidebar meant navigating away from the very thing
    meant to guide the visitor through the app. Dashboard/Heute can't use
    the fast client-side toggle here (view-overview/view-today don't exist
    on these pages), so they render as real links into dashboard.html
    instead — and whichever nav item matches the current page gets marked
    active via active_nav, since there's no JS toggle to do it dynamically."""

    def test_my_plan_has_the_sidebar_with_plan_als_liste_active(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response, f'class="sidebar-item active" href="{reverse("my_plan")}"'
        )
        self.assertContains(response, f'href="{reverse("dashboard")}"')
        self.assertNotContains(response, 'onclick="showOverview()"')

    def test_my_plan_has_the_about_link_and_overlay(self):
        # #183 follow-up: "Über dieses Projekt" lived only in dashboard.html's
        # own sidebar_content, not the shared _sidebar_nav.html partial, so
        # the link (and the #about-overlay it opens) went missing on every
        # other page using it.
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Über dieses Projekt")
        self.assertContains(response, 'id="about-overlay"')

    @patch("django.utils.timezone.localdate")
    def test_close_week_start_has_the_sidebar_with_woche_abschliessen_active(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertContains(
            response,
            f'class="sidebar-item active" href="{reverse("close_week_start")}"',
        )
        self.assertContains(response, "Über dieses Projekt")
        self.assertContains(response, 'id="about-overlay"')

    def test_week_review_has_the_sidebar(self):
        self.given_session_plan()
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 1,
            "rescheduled_count": 0,
            "added_count": 0,
            "summary_text": "Text.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("week_review"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertContains(response, "Über dieses Projekt")
        self.assertContains(response, 'id="about-overlay"')


class SidebarProjectListOnStandalonePagesTest(DemoModeTestCase):
    """#185: the "Projekte" sidebar block (grouped by month, progress ring)
    used to render only inside dashboard.html's own sidebar_content. These
    three standalone pages fall back to a real link into the dashboard's
    ?project= deep link (dashboard.html's own JS, views.py:552-558, already
    opens the right project detail from that param) instead of the
    showProject() JS toggle that only exists inside dashboard.html's DOM."""

    def test_my_plan_shows_the_project_list_sidebar(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )
        self.assertContains(response, "Testkonzert")
        self.assertContains(response, 'class="progress-ring"')
        self.assertContains(
            response, f'href="{reverse("dashboard")}?project=session-plan"'
        )
        self.assertNotContains(response, 'onclick="showProject(')

    @patch("django.utils.timezone.localdate")
    def test_close_week_start_shows_the_project_list_sidebar(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )
        self.assertContains(response, "Testkonzert")
        self.assertContains(
            response, f'href="{reverse("dashboard")}?project=session-plan"'
        )
        self.assertNotContains(response, 'onclick="showProject(')

    def test_week_review_shows_the_project_list_sidebar(self):
        self.given_session_plan()
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 1,
            "rescheduled_count": 0,
            "added_count": 0,
            "summary_text": "Text.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("week_review"))
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )
        self.assertContains(response, "Testkonzert")
        self.assertContains(
            response, f'href="{reverse("dashboard")}?project=session-plan"'
        )
        self.assertNotContains(response, 'onclick="showProject(')


@override_settings(DEMO_MODE=False)
class SidebarNavOnStandalonePagesProductionTest(TestCase):
    def setUp(self):
        # These tests exercise the production project-fetch path (#185's
        # sidebar list) — a stale CACHE_KEY entry left by an earlier test in
        # the run would make the cold-cache assertions below flaky.
        cache.clear()
        self.addCleanup(cache.clear)

    @patch("django.utils.timezone.localdate")
    def test_close_week_start_has_the_sidebar_with_woche_abschliessen_active(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertContains(
            response,
            f'class="sidebar-item active" href="{reverse("close_week_start")}"',
        )
        # No view-overview/view-today or showOverview()/showToday() JS exist
        # on this standalone page, so Dashboard/Heute must be real links back
        # into dashboard.html, same as in demo mode — not the client-side
        # toggle (id="nav-overview"/"nav-today"), which would be dead here.
        self.assertContains(response, f'href="{reverse("dashboard")}"')
        self.assertContains(response, f'href="{reverse("dashboard")}?view=today"')
        self.assertNotContains(response, 'id="nav-overview"')
        # "Über dieses Projekt" explains the demo instance — not relevant,
        # so not offered, in production.
        self.assertNotContains(response, "Über dieses Projekt")

    def test_week_review_has_the_sidebar(self):
        WeekCloseout.objects.create(
            iso_year=2026,
            iso_week=25,
            completed_count=1,
            rescheduled_count=0,
            added_count=0,
            summary_text="Text.",
        )
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            response = self.client.get(reverse("week_review"))
        self.assertContains(response, 'class="sidebar-header"')
        self.assertNotContains(response, "Über dieses Projekt")

    @patch("django.utils.timezone.localdate")
    def test_close_week_start_shows_the_project_list_sidebar(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        project = _fake_upcoming_project_with_task()
        with patch("projects.views.get_upcoming_projects", return_value=[project]):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )
        self.assertContains(response, project["name"])
        self.assertContains(
            response, f'href="{reverse("dashboard")}?project={project["id"]}"'
        )

    def test_week_review_shows_the_project_list_sidebar(self):
        WeekCloseout.objects.create(
            iso_year=2026,
            iso_week=25,
            completed_count=1,
            rescheduled_count=0,
            added_count=0,
            summary_text="Text.",
        )
        project = _fake_upcoming_project_with_task()
        with patch("projects.views.get_upcoming_projects", return_value=[project]):
            response = self.client.get(reverse("week_review"))
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )
        self.assertContains(response, project["name"])
        self.assertContains(
            response, f'href="{reverse("dashboard")}?project={project["id"]}"'
        )


@override_settings(DEMO_MODE=False)
class SidebarProjectsCacheTest(TestCase):
    """#185: the sidebar's project list on week_review() prefers
    dashboard()'s own warm CACHE_KEY entry over a fresh Notion fetch.
    Exercised via week_review() because it is the view with no other
    project fetch of its own, so a cache hit here means zero Notion calls
    for the whole request — close_week_start() never consults this cache
    at all since the #185 follow-up, it hands _sidebar_projects() the
    triage fetch it already made (see SidebarProjectsSingleFetchTest)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        WeekCloseout.objects.create(
            iso_year=2026,
            iso_week=25,
            completed_count=1,
            rescheduled_count=0,
            added_count=0,
            summary_text="Text.",
        )

    def test_reuses_the_warm_dashboard_cache(self):
        project = _fake_upcoming_project_with_task()
        cache.set(CACHE_KEY, ([project], _summary_data()), 60)
        with patch("projects.views.get_upcoming_projects") as mock_fetch:
            response = self.client.get(reverse("week_review"))
        mock_fetch.assert_not_called()
        self.assertContains(response, project["name"])

    def test_falls_back_to_a_direct_fetch_on_a_cold_cache(self):
        project = _fake_upcoming_project_with_task()
        with patch(
            "projects.views.get_upcoming_projects", return_value=[project]
        ) as mock_fetch:
            response = self.client.get(reverse("week_review"))
        mock_fetch.assert_called_once()
        self.assertContains(response, project["name"])

    def test_the_cache_entry_survives_the_in_place_annotation(self):
        # #185 follow-up: _sidebar_projects() annotates the cached
        # projects in place instead of deep-copying them first. That is
        # safe only because every Django cache backend serializes on both
        # set and get, so cache.get() hands back an object graph no other
        # request shares — dashboard() already depends on it when it
        # writes display_name onto its own cached projects. This test
        # guards that assumption, not the removal of the copy: a backend
        # handing out shared objects would let one request's annotation
        # leak into the next request's data, and only this assertion
        # would notice.
        project = _fake_upcoming_project_with_task()
        cache.set(CACHE_KEY, ([project], _summary_data()), 60)
        self.client.get(reverse("week_review"))
        still_cached, _ = cache.get(CACHE_KEY)
        self.assertNotIn("display_name", still_cached[0])
        self.assertNotIn("urgency", still_cached[0])

    def test_degrades_to_an_empty_project_list_on_notion_failure(self):
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            response = self.client.get(reverse("week_review"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Projekte'
        )


@override_settings(DEMO_MODE=False)
class SidebarProjectsSingleFetchTest(TestCase):
    """#185 follow-up: close_week_start() reads Notion exactly once per
    request. Its triage list is a deliberately uncached fetch and #185's
    sidebar list was a second one, so on a cold cache the view issued two
    identical reads — and get_upcoming_projects is 1 + N requests (one per
    project for its tasks, notion.py _get_tasks), so that doubled the
    whole thing.
    Cold is the normal state in this very flow: every task toggle and
    every "→ nächste Woche" move calls _bust_dashboard_cache().

    Counting calls, not asserting on the markup: what the sidebar renders
    from that one fetch is covered by
    SidebarNavOnStandalonePagesProductionTest."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_close_week_start_fetches_once_on_a_cold_cache(self):
        with patch(
            "projects.views.get_upcoming_projects",
            return_value=[_fake_upcoming_project_with_task()],
        ) as mock_fetch:
            response = self.client.get(reverse("close_week_start"))
        self.assertEqual(response.status_code, 200)
        mock_fetch.assert_called_once()

    def test_close_week_start_still_ignores_the_dashboard_cache(self):
        # The triage list must not come from a cache a stale week could
        # have filled (#169) — feeding the sidebar from the same fetch
        # must not have quietly turned this view into a cache reader.
        cached = _fake_upcoming_project_with_task()
        cached["name"] = "Aus dem Cache"
        cache.set(CACHE_KEY, ([cached], _summary_data()), 60)
        fresh = _fake_upcoming_project_with_task()
        fresh["name"] = "Frisch aus Notion"
        with patch(
            "projects.views.get_upcoming_projects", return_value=[fresh]
        ) as mock_fetch:
            response = self.client.get(reverse("close_week_start"))
        mock_fetch.assert_called_once()
        self.assertContains(response, "Frisch aus Notion")
        self.assertNotContains(response, "Aus dem Cache")


class SidebarProgressRingTest(DemoModeTestCase):
    """#76: the sidebar's per-project status dot becomes a progress ring —
    fill from done/total, stroke colour from the project's urgency."""

    def test_ring_dashoffset_reflects_a_known_ratio(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Erledigt",
                    "date": None,
                    "kontext": "",
                    "done": True,
                },
                {
                    "id": "t2",
                    "name": "Offen",
                    "date": (date.today() + timedelta(days=1)).isoformat(),
                    "kontext": "",
                    "done": False,
                },
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, 'stroke-dashoffset="21.99"')

    def test_fully_done_project_renders_a_fully_filled_ring(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Erledigt",
                    "date": None,
                    "kontext": "",
                    "done": True,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, 'stroke-dashoffset="0.00"')

    def test_overdue_project_gets_the_overdue_ring_class(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Überfällig",
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, "progress-ring-fill overdue")

    @patch("django.utils.timezone.localdate")
    def test_urgent_project_gets_the_urgent_ring_class(self, mock_localdate):
        # #169: urgent is calendar-week based now — a real date.today() + 2
        # would only land in the same ISO week on some weekdays, so "today"
        # is pinned to a known Monday rather than left to whichever day the
        # suite happens to run on.
        fixed_today = date(2026, 6, 15)
        mock_localdate.return_value = fixed_today
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Bald fällig",
                    "date": (fixed_today + timedelta(days=2)).isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, "progress-ring-fill urgent")

    def test_due_today_project_gets_the_today_ring_class(self):
        # #160: due today outranks urgent on the project level.
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Heute fällig",
                    "date": date.today().isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, "progress-ring-fill today")

    def test_on_track_project_gets_the_ok_ring_class(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Weit weg",
                    "date": (date.today() + timedelta(days=30)).isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, "progress-ring-fill ok")

    def test_the_old_sidebar_item_urgency_classes_are_gone(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Überfällig",
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, 'class="sidebar-item overdue"')
        self.assertNotContains(response, 'class="sidebar-item urgent"')

    def test_the_old_status_dot_no_longer_renders_for_projects(self):
        self.given_session_plan()
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, '<span class="dot default">')


class SidebarProgressRingCssTest(DemoModeTestCase):
    def test_ring_css_references_the_status_tokens(self):
        # #173: overdue is the only stroke override left — every other open
        # stage rides the neutral base default.
        # #185 follow-up: .progress-ring* moved from dashboard.html's own
        # extra_css into the shared dashboard.css, alongside .sidebar-icon
        # (see SidebarIconSlotWidthTest) — _sidebar_project_list.html (which
        # renders the ring) is now included from my_plan.html/
        # close_week_start.html/week_review.html too, and without this the
        # ring rendered as a plain filled black circle on those pages.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(".progress-ring-fill.overdue { stroke: var(--color-overdue)", css)
        self.assertIn(
            ".progress-ring-fill { stroke: var(--color-text-quaternary); "
            "stroke-linecap: round; }",
            css,
        )

    def test_the_old_sidebar_item_urgency_css_is_gone(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, ".sidebar-item.overdue")
        self.assertNotContains(response, ".sidebar-item.urgent")

    def test_the_dead_multi_colour_dot_block_is_gone(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, ".dot.gray, .dot.blue")


class SidebarIconSlotWidthTest(DemoModeTestCase):
    """#97: sidebar icons (dot, progress ring, bare glyphs) each carried their
    own intrinsic width plus a per-icon margin-right, so the text after them
    landed at a different x-position depending on which icon preceded it.
    .sidebar-icon gives every icon the same fixed, centered 16px slot."""

    def test_sidebar_icon_css_defines_a_fixed_centered_slot(self):
        # #183 follow-up: .sidebar-icon moved from dashboard.html's own
        # extra_css into the shared dashboard.css, since _sidebar_nav.html
        # (which uses it) is now included from my_plan.html/
        # close_week_start.html/week_review.html too — each of those
        # replaces extra_css with its own page-specific styles rather than
        # extending dashboard.html's.
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(
            ".sidebar-icon { display: inline-flex; align-items: center; "
            "justify-content: center; width: 16px; flex-shrink: 0; "
            "margin-right: 8px; }",
            css,
        )

    def test_progress_ring_no_longer_carries_its_own_margin(self):
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(".progress-ring { flex-shrink: 0; }", css)

    def test_sidebar_icon_neutralizes_the_dot_margin(self):
        # Renamed .sidebar-icon > .dot to .sidebar-icon > .sidebar-dot in the
        # same move: a shared .dot rule would have collided with my_plan.html's
        # own differently-sized .dot for its task list (see dashboard.css).
        css = (
            Path(settings.BASE_DIR) / "projects/static/projects/css/dashboard.css"
        ).read_text()
        self.assertIn(".sidebar-icon > .sidebar-dot { margin-right: 0; }", css)

    def test_the_base_dot_rule_still_carries_its_own_margin(self):
        """The task-completion checkbox reuses .dot outside the sidebar and
        relies on this rule for its spacing before the task name."""
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            ".dot { display: inline-block; width: 7px; height: 7px; "
            "border-radius: 50%; margin-right: 8px;",
        )

    @patch("django.utils.timezone.localdate")
    def test_the_checkbox_dot_still_renders_outside_the_sidebar_icon_wrapper(
        self, mock_localdate
    ):
        # #169: the default fixture's task (due today + 7) is never in the
        # same ISO week as today under the calendar-week rule, so this needs
        # its own explicitly urgent task rather than the shared default.
        fixed_today = date(2026, 6, 15)
        mock_localdate.return_value = fixed_today
        self.given_session_plan(
            tasks=[
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (fixed_today + timedelta(days=2)).isoformat(),
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, 'class="dot urgent " title="Abhaken"')

    def test_the_overview_dot_is_wrapped(self):
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            '<span class="sidebar-icon"><span class="sidebar-dot" '
            'style="background: var(--color-solid-bg);"></span></span>',
        )

    def test_the_progress_ring_is_wrapped(self):
        self.given_session_plan()
        response = self.client.get("/dashboard/")
        self.assertContains(
            response,
            '<span class="sidebar-icon">\n            <svg class="progress-ring"',
        )

    def test_own_plan_view_icons_are_wrapped(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<span class="sidebar-icon">⊞</span>')
        self.assertContains(response, '<span class="sidebar-icon">☰</span>')
        self.assertContains(response, '<span class="sidebar-icon">←</span>')

    def test_force_multi_plan_exists_icons_are_wrapped(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, '<span class="sidebar-icon">←</span>')
        self.assertContains(response, '<span class="sidebar-icon">☰</span>')

    def test_force_multi_no_plan_icons_are_wrapped(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(
            response,
            '<span class="sidebar-icon" style="font-size: 15px; line-height: 1;">+</span>',
        )
        self.assertContains(response, '<span class="sidebar-icon">←</span>')

    def test_demo_default_icons_are_wrapped(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            '<span class="sidebar-icon" style="font-size: 15px; line-height: 1;">+</span>',
        )
        self.assertContains(response, '<span class="sidebar-icon">←</span>')

    def test_about_info_svg_is_wrapped(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, '<span class="sidebar-icon"><svg width="14" height="14"'
        )

    def test_old_inline_margin_style_on_a_glyph_span_is_gone(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'style="margin-right: 8px;">')


@override_settings(DEMO_MODE=False)
class ProductionSidebarIconSlotWidthTest(TestCase):
    """#97 in production: "Neue Veranstaltung" (+), "Planungsregeln" (⚙) and
    the per-project progress ring only render outside demo mode."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_production_icons_are_wrapped(self):
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
            response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            '<span class="sidebar-icon" style="font-size: 15px; line-height: 1;">+</span>',
        )
        self.assertContains(response, '<span class="sidebar-icon">⚙</span>')
        self.assertContains(
            response,
            '<span class="sidebar-icon">\n            <svg class="progress-ring"',
        )


class MultiViewSidebarLinkTest(DemoModeTestCase):
    """Part C of #39: has_session_plan is unconditionally False under
    ?mode=multi, so the sidebar used to show a dead-end back-to-plan link
    even when no plan had ever been generated in this session."""

    def test_shows_create_link_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Projekt selbst planen")
        self.assertNotContains(response, "Plan als Liste")

    def test_shows_plan_links_with_a_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Plan als Liste")
        self.assertNotContains(response, "Projekt selbst planen")


class MyPlanSidebarLinkTest(DemoModeTestCase):
    """#7: /mein-plan/ was fully built but linked from nowhere, reachable only
    by typing the URL. The link belongs wherever a session plan exists —
    both while looking at it (has_session_plan) and while looking at the
    example projects instead (plan_exists, force_multi)."""

    def test_no_link_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, f'href="{reverse("my_plan")}"')

    def test_no_link_without_a_session_plan_under_multi_view(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertNotContains(response, f'href="{reverse("my_plan")}"')

    def test_shows_link_while_viewing_the_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("my_plan")}"')

    def test_shows_link_while_viewing_the_example_projects(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, f'href="{reverse("my_plan")}"')


@override_settings(DEMO_MODE=False)
class ProductionRulesSidebarLinkTest(TestCase):
    """#105: the maintainer manages the whole rule set from the dashboard, not
    just mid-way through planning a new event — the demo deliberately omits
    this link to avoid inviting repeated (costly) plan generation."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_dashboard_links_to_the_rules_page(self):
        with (
            patch("projects.views.get_upcoming_projects", return_value=[]),
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("rules_list")}"')


class DemoRulesSidebarLinkTest(DemoModeTestCase):
    def test_dashboard_does_not_link_to_the_rules_page(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, f'href="{reverse("rules_list")}"')


@override_settings(DEMO_MODE=False)
class SidebarProgressRingZeroTasksTest(TestCase):
    """#76: a project with no tasks yet must render an empty ring, not
    crash with a ZeroDivisionError."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_project_with_no_tasks_renders_an_empty_ring(self):
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
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'stroke-dashoffset="43.98"')
