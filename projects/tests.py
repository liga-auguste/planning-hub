import importlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import anthropic
import httpx
import markdown
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse
from notion_client.errors import HTTPResponseError, RequestTimeoutError

from .ai import (
    AIUnavailableError,
    _valid_moments,
    build_prompt,
    generate_timelapse_moments,
    generate_weekly_summary,
    log_claude_call,
)
from .models import DemoEvent, PlannerRule
from .notion import (
    NotionUnavailableError,
    create_project,
    create_tasks,
    find_project,
    get_historical_projects,
    get_upcoming_projects,
    toggle_task,
    update_task_date,
)
from .planner import generate_plan, get_clarifying_questions
from .planner_views import _get_history, _parse_event_date
from .rules import DEMO_RULES_KEY, INITIAL_RULES, get_active_rule_texts
from .startup import MissingAPIKeyError, require_api_keys
from .views import (
    CACHE_KEY,
    DEMO_MULTI_SUMMARY_KEY,
    STALE_CACHE_KEY,
    SUMMARY_KEY,
    _annotate_tasks,
    _fix_ai_markdown,
    _format_date,
)

# The view modules import the AI functions with `from .ai import ...`, so the
# name to patch is the one bound in the view module, not the one in projects.ai.
AI_STUBS = {
    "projects.views.generate_weekly_summary": "**Test summary**",
    "projects.planner_views.get_clarifying_questions": "**Wie viele Mitwirkende?**",
    # generate_plan now parses its own response and returns a dict (see #29 /
    # GeneratePlanRetryTest) — this stub has to match that shape, not the raw
    # JSON string Claude used to hand back.
    "projects.planner_views.generate_plan": {
        "project_name": "Testkonzert",
        "tasks": [],
    },
    "projects.planner_views.generate_timelapse_moments": [],
}


@override_settings(DEMO_MODE=True)
class DemoModeTestCase(TestCase):
    """Stubs the Claude API — no test may make a real call."""

    def setUp(self):
        # The demo caches live in the test database, shared across the whole
        # run, so an entry a previous test left behind would make a later
        # test skip the Claude call it asserts on — see AiStubTest.
        cache.clear()
        self.addCleanup(cache.clear)
        self.ai_mocks = {}
        for target, return_value in AI_STUBS.items():
            patcher = patch(target, return_value=return_value)
            self.ai_mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    def given_session_plan(self, **overrides):
        """Creates a session plan the way planner_create produces it."""
        plan = {
            "name": "Testkonzert",
            "event_date": (date.today() + timedelta(days=30)).isoformat(),
            "tasks": [
                {
                    "id": "demo-session-0",
                    "name": "Programm festlegen",
                    "date": (date.today() + timedelta(days=7)).isoformat(),
                    "done": False,
                },
            ],
        }
        plan.update(overrides)
        session = self.client.session
        session["demo_plan"] = plan
        session.save()
        return plan

    def given_timelapse_moments(self, *dates):
        """Stores moments the way planner_create does. Only these dates are postable."""
        session = self.client.session
        session["demo_timelapse_moments"] = [
            {"date": d, "label": "Moment", "description": "Beschreibung"} for d in dates
        ]
        session.save()


class BootstrapVendoredVersionTest(SimpleTestCase):
    """#64 unit 1: what sits on disk under projects/static/. The globs match
    `bootstrap*` rather than the whole directory listing so that a .DS_Store
    dropped by Finder can't turn the trim assertions red."""

    css_dir = settings.BASE_DIR / "projects" / "static" / "projects" / "css"
    js_dir = settings.BASE_DIR / "projects" / "static" / "projects" / "js"

    def test_vendored_css_is_538(self):
        header = (self.css_dir / "bootstrap.min.css").read_text()[:200]
        self.assertIn("Bootstrap", header)
        self.assertIn("v5.3.8", header)
        self.assertNotIn("v5.0.2", header)

    def test_vendored_css_ships_color_modes(self):
        # data-bs-theme is the 5.3 feature #12 rides on; 5.0.2 has none.
        css = (self.css_dir / "bootstrap.min.css").read_text()
        self.assertIn("data-bs-theme", css)

    def test_only_the_linked_stylesheet_is_vendored(self):
        # No RTL builds, no .map files, no grid/utilities/reboot variants.
        self.assertEqual(
            sorted(p.name for p in self.css_dir.glob("bootstrap*")),
            ["bootstrap.min.css"],
        )

    def test_no_bootstrap_javascript_is_vendored(self):
        self.assertEqual(sorted(p.name for p in self.js_dir.glob("bootstrap*")), [])


class VendoredCssSourceMapTest(SimpleTestCase):
    """#74 groundwork: bootstrap.min.css ended with a sourceMappingURL comment
    although the .map file is deliberately not vendored (#64). Harmless while
    nothing resolved the name — but ManifestStaticFilesStorage rewrites
    source-map references, so collectstatic would fail loudly on the dangling
    file the moment the hashed storage goes live."""

    def test_the_stylesheet_names_no_source_map(self):
        css_path = settings.BASE_DIR / "projects/static/projects/css/bootstrap.min.css"
        self.assertNotIn("sourceMappingURL", css_path.read_text())


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


class StaticCacheHeadersConfTest(SimpleTestCase):
    """#74: with hashed filenames the far-future cache header is safe — and
    only with them, which is why the two land together and are pinned
    together, following BootstrapVendoredVersionTest's read-the-disk
    precedent."""

    def test_both_deployments_cache_static_files_immutably(self):
        for name in ("nginx.conf", "nginx-demo.conf"):
            with self.subTest(conf=name):
                conf = (settings.BASE_DIR / name).read_text()
                self.assertIn('add_header Cache-Control "public, immutable";', conf)
                self.assertIn("expires 1y;", conf)


class StaticStorageConfigTest(SimpleTestCase):
    """#74: settings.py picks the staticfiles backend by process type —
    ManifestStaticFilesStorage for the server, the plain storage under the
    test runner, which forces DEBUG = False without ever running
    collectstatic and would otherwise trip over the missing manifest (the
    arrangement the Django docs recommend for testing)."""

    def test_the_suite_itself_runs_on_the_plain_storage(self):
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            "django.contrib.staticfiles.storage.StaticFilesStorage",
        )

    def test_the_server_process_gets_the_manifest_storage(self):
        # The suite runs on the plain storage (see above), so the server
        # branch is pinned by re-executing the module the way gunicorn sees
        # it. django.conf.settings copied its values at startup — reloading
        # the module object touches nothing the running suite reads.
        import planning_hub.settings as settings_module

        with patch.object(sys, "argv", ["gunicorn", "planning_hub.wsgi"]):
            backend = importlib.reload(settings_module).STORAGES["staticfiles"][
                "BACKEND"
            ]
        importlib.reload(settings_module)  # re-derive the test-run state
        self.assertEqual(
            backend,
            "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        )

    def test_the_default_file_storage_survives_the_override(self):
        # Defining STORAGES replaces Django's whole default dict — dropping
        # the "default" key would leave the app without a file storage.
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )


class SecretKeyConfTest(SimpleTestCase):
    """Same principle #47 established for DEBUG, applied to SECRET_KEY: the
    previous code fell back to a hardcoded string committed to this repo,
    which is not a secret in any deployment that skips the documented `.env`
    step — indistinguishable from running session signing and CSRF
    protection on a publicly known key. Unlike DEBUG or ALLOWED_HOSTS, there
    is no value that is both valid and safe to default to, so a missing
    SECRET_KEY must fail closed by raising, not by falling back."""

    def test_secret_key_raises_when_unset(self):
        # django.conf.settings copied its values at process startup —
        # reloading the module object touches nothing the running suite
        # reads (see StaticStorageConfigTest).
        import planning_hub.settings as settings_module

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ImproperlyConfigured),
        ):
            importlib.reload(settings_module)
        importlib.reload(settings_module)  # re-derive the test-run state

    def test_secret_key_respected_when_set(self):
        import planning_hub.settings as settings_module

        with patch.dict(os.environ, {"SECRET_KEY": "test-key"}, clear=True):
            secret_key = importlib.reload(settings_module).SECRET_KEY
        importlib.reload(settings_module)
        self.assertEqual(secret_key, "test-key")


class DebugDefaultConfTest(SimpleTestCase):
    """#47: DEBUG must fail closed. An unset DEBUG env var previously
    defaulted to True, rendering full tracebacks (settings values, installed
    apps, stack trace) on any unhandled exception — a fresh clone without
    .env, an incomplete deploy step, or a misconfigured container all hit
    this silently. .env.example already documents DEBUG=false for both
    deployment paths; this pins the code to match when that step is
    skipped."""

    def test_debug_defaults_to_false_when_env_var_unset(self):
        # django.conf.settings copied its values at process startup —
        # reloading the module object touches nothing the running suite
        # reads (see StaticStorageConfigTest). SECRET_KEY has to ride along
        # here too, now that it fails closed in the same reload (see
        # SecretKeyConfTest) — this test isolates DEBUG, not SECRET_KEY.
        import planning_hub.settings as settings_module

        with patch.dict(os.environ, {"SECRET_KEY": "test-key"}, clear=True):
            debug = importlib.reload(settings_module).DEBUG
        importlib.reload(settings_module)  # re-derive the test-run state
        self.assertFalse(debug)

    def test_debug_true_still_respected_when_set(self):
        import planning_hub.settings as settings_module

        env = {"DEBUG": "true", "SECRET_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            debug = importlib.reload(settings_module).DEBUG
        importlib.reload(settings_module)
        self.assertTrue(debug)


class LocaleAndTimeZoneConfTest(SimpleTestCase):
    """#14: LANGUAGE_CODE and TIME_ZONE contradicted the German UI — both
    base templates declare <html lang="de"> and the audience is in Germany,
    but settings.py shipped Django's en-us/UTC defaults."""

    def test_language_code_is_german(self):
        self.assertEqual(settings.LANGUAGE_CODE, "de")

    def test_time_zone_is_berlin(self):
        self.assertEqual(settings.TIME_ZONE, "Europe/Berlin")

    def test_use_i18n_stays_on_for_the_admin_chrome(self):
        self.assertTrue(settings.USE_I18N)

    def test_the_time_zone_name_resolves(self):
        # python:3.12-slim ships without the IANA database, so this catches
        # a missing tzdata dependency before it reaches production.
        ZoneInfo(settings.TIME_ZONE)


class AdminLoginRendersGermanTest(TestCase):
    """#14: USE_I18N stays on specifically so Django translates its own
    admin chrome, since the app itself has no {% trans %} of its own."""

    def test_admin_login_page_is_german(self):
        response = self.client.get(reverse("admin:login"))
        self.assertContains(response, "Anmelden")
        self.assertContains(response, "Benutzername")
        self.assertContains(response, "Passwort")


class NoDateTodayCallsTest(SimpleTestCase):
    """#85: date.today() reads the container's system clock, not
    settings.TIME_ZONE — near midnight, that can land "today" on the wrong
    calendar day relative to Europe/Berlin. Pinned to timezone.localdate()
    at all 4 call-site files so this can't quietly regress."""

    FILES = ["views.py", "ai.py", "demo_data.py", "planner_views.py"]

    def test_no_bare_date_today_remains(self):
        for name in self.FILES:
            with self.subTest(file=name):
                source = (settings.BASE_DIR / "projects" / name).read_text()
                self.assertNotIn("date.today()", source)


class DemoEventLocalTimeTest(SimpleTestCase):
    """#14: __str__ formatted created_at via a raw f-string, which bypasses
    Django's timezone conversion — that only runs through template filters
    or an explicit timezone.localtime() call."""

    def test_str_renders_berlin_local_time_not_utc(self):
        event = DemoEvent(
            event_type="plan_started",
            project_type="konzert",
            created_at=datetime(2026, 1, 15, 23, 30, tzinfo=UTC),
        )
        self.assertIn("16.01.2026 00:30", str(event))
        self.assertNotIn("15.01.2026 23:30", str(event))


class HashedStaticFilesTest(DemoModeTestCase):
    """#74 end to end: under ManifestStaticFilesStorage, collectstatic writes
    a content-hashed twin of every file plus the manifest that maps plain
    names to hashed ones, and {% static %} resolves through that manifest —
    so a changed file is a new URL and no browser cache can pin a stale copy,
    the failure mode the #64 bootstrap swap exposed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.static_root = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.static_root, ignore_errors=True)
        storage_override = override_settings(
            STATIC_ROOT=cls.static_root,
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                },
                "staticfiles": {
                    "BACKEND": (
                        "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
                    ),
                },
            },
        )
        storage_override.enable()
        cls.addClassCleanup(storage_override.disable)
        # Would raise on any dangling reference inside the collected files —
        # the loud failure VendoredCssSourceMapTest clears the way for.
        call_command("collectstatic", interactive=False, verbosity=0)
        manifest_path = Path(cls.static_root) / "staticfiles.json"
        cls.manifest = json.loads(manifest_path.read_text())["paths"]

    def test_collectstatic_writes_a_hashed_twin_and_the_manifest(self):
        hashed = self.manifest["projects/css/bootstrap.min.css"]
        self.assertRegex(hashed, r"^projects/css/bootstrap\.min\.[0-9a-f]{12}\.css$")
        self.assertTrue((Path(self.static_root) / hashed).exists())

    def test_a_rendered_page_links_the_hashed_stylesheet(self):
        # Tests run with DEBUG = False, so {% static %} resolves through the
        # manifest just as it does behind gunicorn.
        html = self.client.get(reverse("index")).content.decode()
        self.assertNotIn("bootstrap.min.css", html)
        self.assertRegex(
            html, r"/static/projects/css/bootstrap\.min\.[0-9a-f]{12}\.css"
        )


class BootstrapJsBundleDroppedTest(DemoModeTestCase):
    """#64 unit 1: nothing initialises a Bootstrap JS component (zero
    data-bs-* attributes), so the bundle was dropped rather than upgraded."""

    def test_dashboard_does_not_load_the_bundle(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, "bootstrap.bundle.min.js")

    def test_dashboard_keeps_its_own_sidebar_script(self):
        response = self.client.get("/dashboard/")
        self.assertContains(response, "sidebarCollapsed")

    def test_both_bases_still_link_the_stylesheet(self):
        self.assertContains(self.client.get("/dashboard/"), "bootstrap.min.css")
        self.assertContains(self.client.get("/impressum/"), "bootstrap.min.css")


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

    def test_mobile_launcher_shares_the_wordmark_rows_center_axis(self):
        # dashboard.html's wordmark row: 24px content-body padding-top +
        # half its 40px logo image = a 44px center from the viewport top.
        # The 36px launcher button needs top: 26px to share that center.
        content_body_top_padding = 24
        logo_height = 40
        button_height = 36
        wordmark_center = content_body_top_padding + logo_height / 2
        button_top = wordmark_center - button_height / 2
        self.assertIn(f"top: {int(button_top)}px; right: 20px;", self.css)

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
            patch("projects.views.generate_weekly_summary", return_value="**Test**"),
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

    def test_urgent_project_gets_the_urgent_ring_class(self):
        self.given_session_plan(
            tasks=[
                {
                    "id": "t1",
                    "name": "Bald fällig",
                    "date": date.today().isoformat(),
                    "kontext": "",
                    "done": False,
                }
            ]
        )
        response = self.client.get("/dashboard/")
        self.assertContains(response, "progress-ring-fill urgent")

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
        response = self.client.get("/dashboard/")
        self.assertContains(
            response, ".progress-ring-fill.overdue { stroke: var(--color-overdue)"
        )
        self.assertContains(
            response, ".progress-ring-fill.urgent { stroke: var(--color-urgent)"
        )

    def test_the_old_sidebar_item_urgency_css_is_gone(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, ".sidebar-item.overdue")
        self.assertNotContains(response, ".sidebar-item.urgent")

    def test_the_dead_multi_colour_dot_block_is_gone(self):
        response = self.client.get("/dashboard/")
        self.assertNotContains(response, ".dot.gray, .dot.blue")


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
        "--color-urgent",
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


class PlannerStepsMixin:
    """Fetches the three planner steps that carry a submit button."""

    def steps(self):
        return {
            "start": self.client.get(reverse("planner_start") + "?type=eigenes"),
            "questions": self.client.post(
                reverse("planner_start"),
                data={"description": "Konzert am 15. September 2026"},
            ),
            "review": self.client.post(
                reverse("planner_review"),
                data={
                    "description": "Konzert am 5. September 2026",
                    "answers": "keine weiteren Angaben",
                },
            ),
        }


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

    def test_create_get_redirects_to_start(self):
        response = self.client.get(reverse("planner_create"))
        self.assertRedirects(response, reverse("planner_start"))

    def test_questions_route_is_gone(self):
        # Literal path: the URL name no longer exists, so reverse() cannot be used.
        self.assertEqual(self.client.get("/planner/questions/").status_code, 404)


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
            "tr.sofort .col-name { box-shadow: inset 3px 0 0 var(--color-urgent); }",
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
            response, "tr.sofort { box-shadow: inset 3px 0 0 var(--color-urgent); }"
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


class SummarySessionCacheTest(DemoModeTestCase):
    """Proves the views actually write the current versioned key — without
    this, a key bump could leave every view writing a dead key and the
    assertNotIn tests above would pass vacuously."""

    def test_a_successful_summary_is_cached_under_the_current_key(self):
        self.given_session_plan()
        self.client.get(reverse("dashboard"))
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)

    def test_my_plan_reads_and_writes_the_same_key(self):
        self.given_session_plan()
        self.client.get(reverse("my_plan"))
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)


class MyPlanMultiViewCtaTest(DemoModeTestCase):
    """#7: my_plan only ever renders with a session plan present, so its
    "Mehrprojekt-Dashboard ansehen" CTA landing on plain {% url 'dashboard' %}
    always bounced back into the single-project view instead of the example
    projects it promises."""

    def test_the_cta_links_to_the_multi_project_view(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, f'href="{reverse("dashboard")}?mode=multi"')


class MyPlanAiFailureTest(DemoModeTestCase):
    def test_my_plan_degrades_without_a_summary(self):
        self.given_session_plan()
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.get(reverse("my_plan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nicht verfügbar")


class MyPlanEventDateDisplayTest(DemoModeTestCase):
    """my_plan() built its own task dict inline instead of reusing
    _build_session_project (#9) — this pins the one field that builder
    doesn't set, so the refactor to reuse it can't quietly drop it."""

    def test_my_plan_still_renders_the_formatted_event_date(self):
        self.given_session_plan()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, _format_date(date.today() + timedelta(days=30)))


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
        self.assertContains(response, 'class="dot urgent"')
        self.assertNotContains(response, 'class="dot ok"')


class PreloadAiFailureTest(DemoModeTestCase):
    def test_preload_reports_ok_false_and_writes_nothing_to_the_session(self):
        self.given_session_plan()
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(moment)
        self.ai_mocks[
            "projects.views.generate_weekly_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data=f'{{"date": "{moment}"}}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": False})
        self.assertNotIn(f"{SUMMARY_KEY}_{moment}", self.client.session)


class TimelapseValidationTest(DemoModeTestCase):
    """An unvalidated string in demo_sim_date used to break every later request."""

    def post_date(self, body):
        return self.client.post(
            reverse("set_timelapse_date"), data=body, content_type="application/json"
        )

    def test_invalid_date_is_rejected(self):
        response = self.post_date('{"date": "kaputt"}')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_malformed_json_is_rejected(self):
        response = self.post_date("{")
        self.assertEqual(response.status_code, 400)

    def test_valid_date_is_stored(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(sim_date)
        response = self.post_date(f'{{"date": "{sim_date}"}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["demo_sim_date"], sim_date)

    def test_empty_date_clears_the_session(self):
        today = date.today().isoformat()
        self.given_timelapse_moments(today)
        self.post_date(f'{{"date": "{today}"}}')
        response = self.post_date('{"date": null}')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("demo_sim_date", self.client.session)

    @override_settings(DEMO_MODE=False)
    def test_unavailable_outside_demo_mode(self):
        self.assertEqual(self.post_date('{"date": null}').status_code, 404)

    def test_preload_rejects_invalid_date(self):
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data='{"date": "kaputt"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_preload_rejects_malformed_json(self):
        response = self.client.post(
            reverse("preload_timelapse_summary"),
            data="{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class SimDateIsRestrictedToGeneratedMomentsTest(DemoModeTestCase):
    """A parseable date is not enough. Every accepted date costs a Claude call, so
    only the moments planner_create generated for this session may be posted."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name),
            data=f'{{"date": "{raw}"}}',
            content_type="application/json",
        )

    def test_well_formed_date_outside_the_moments_is_rejected(self):
        self.given_timelapse_moments((date.today() + timedelta(days=5)).isoformat())
        other = (date.today() + timedelta(days=6)).isoformat()
        self.assertEqual(self.post_date("set_timelapse_date", other).status_code, 400)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_far_future_date_is_rejected(self):
        self.given_timelapse_moments(date.today().isoformat())
        self.assertEqual(
            self.post_date("set_timelapse_date", "9999-12-31").status_code, 400
        )

    def test_no_moments_means_no_date_is_accepted(self):
        sim_date = (date.today() + timedelta(days=5)).isoformat()
        self.assertEqual(
            self.post_date("set_timelapse_date", sim_date).status_code, 400
        )

    def test_preload_spends_no_api_call_on_an_unlisted_date(self):
        self.given_session_plan()
        self.given_timelapse_moments(date.today().isoformat())
        unlisted = (date.today() + timedelta(days=99)).isoformat()
        response = self.post_date("preload_timelapse_summary", unlisted)
        self.assertEqual(response.status_code, 400)
        self.ai_mocks["projects.views.generate_weekly_summary"].assert_not_called()

    def test_preload_accepts_a_listed_date(self):
        moment = (date.today() + timedelta(days=5)).isoformat()
        self.given_session_plan()
        self.given_timelapse_moments(moment)
        response = self.post_date("preload_timelapse_summary", moment)
        self.assertEqual(response.status_code, 200)
        self.ai_mocks["projects.views.generate_weekly_summary"].assert_called()

    def test_replanning_invalidates_the_old_moments(self):
        old = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(old)
        self.given_timelapse_moments((date.today() + timedelta(days=9)).isoformat())
        self.assertEqual(self.post_date("set_timelapse_date", old).status_code, 400)


class UnparseableMomentDateTest(DemoModeTestCase):
    """Being on the allowlist is not enough — the moments come from Claude, so a
    session written before they were validated can list a date nothing can parse."""

    def post_date(self, url_name, raw):
        return self.client.post(
            reverse(url_name),
            data=f'{{"date": "{raw}"}}',
            content_type="application/json",
        )

    def test_wrong_format_is_not_written_to_the_session(self):
        self.given_timelapse_moments("05.09.2026")
        self.assertEqual(
            self.post_date("set_timelapse_date", "05.09.2026").status_code, 400
        )
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_impossible_day_does_not_reach_fromisoformat(self):
        self.given_session_plan()
        self.given_timelapse_moments("2026-02-30")
        self.assertEqual(
            self.post_date("preload_timelapse_summary", "2026-02-30").status_code, 400
        )


class MalformedPayloadTest(DemoModeTestCase):
    """Valid JSON of the wrong shape must be a 400, not an unhandled exception.
    Both endpoints are unauthenticated on the public demo."""

    URL_NAMES = ("set_timelapse_date", "preload_timelapse_summary")

    def post_body(self, url_name, body):
        return self.client.post(
            reverse(url_name), data=body, content_type="application/json"
        )

    def assert_rejects(self, body):
        for url_name in self.URL_NAMES:
            with self.subTest(url=url_name, body=body):
                self.assertEqual(self.post_body(url_name, body).status_code, 400)

    def test_unhashable_date_is_rejected(self):
        # `raw not in <set>` hashes the value first, so a list used to raise TypeError.
        self.assert_rejects('{"date": ["2026-09-05"]}')
        self.assert_rejects('{"date": {"a": 1}}')

    def test_non_string_date_is_rejected(self):
        self.assert_rejects('{"date": 20260905}')

    def test_body_that_is_not_an_object_is_rejected(self):
        for body in ("null", "[]", '"2026-09-05"', "5"):
            self.assert_rejects(body)

    def test_unhashable_moment_in_the_session_does_not_break_the_allowlist(self):
        session = self.client.session
        session["demo_timelapse_moments"] = [{"date": ["2026-09-05"]}, {"date": None}]
        session.save()
        self.assert_rejects('{"date": "2026-09-05"}')


class PoisonedSessionHealingTest(DemoModeTestCase):
    """Sessions poisoned before the validation landed must recover on their own."""

    def poison(self):
        session = self.client.session
        session["demo_sim_date"] = "kaputt"
        session.save()

    def test_dashboard_renders_and_clears_the_bad_value(self):
        self.given_session_plan()
        self.poison()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("demo_sim_date", self.client.session)

    def test_dashboard_renders_without_a_session_plan(self):
        self.poison()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)


class MultiViewSidebarLinkTest(DemoModeTestCase):
    """Part C of #39: has_session_plan is unconditionally False under
    ?mode=multi, so the sidebar used to show a dead-end "Mein Plan" link
    even when no plan had ever been generated in this session."""

    def test_shows_create_link_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Projekt selbst planen")
        self.assertNotContains(response, "Mein Plan")

    def test_shows_mein_plan_link_with_a_session_plan(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, "Mein Plan")
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


class MultiViewSimDateTest(DemoModeTestCase):
    """#50: the simulated date used to be read before the mode was known, so a
    Zeitreise set on the visitor's own plan classified and narrated the example
    projects too — while every control that reveals or resets it stayed hidden."""

    def given_sim_date(self, sim_date):
        """Writes the value straight into the session, the way set_timelapse_date does."""
        session = self.client.session
        session["demo_sim_date"] = sim_date.isoformat()
        session.save()

    def given_plan_in_the_future(self):
        sim_date = date.today() + timedelta(days=120)
        self.given_session_plan()
        self.given_timelapse_moments(sim_date.isoformat())
        self.given_sim_date(sim_date)
        return sim_date

    def multi_tasks(self, response):
        return [
            task
            for group in response.context["month_groups"]
            for project in group["projects"]
            for task in project["tasks"]
        ]

    def test_a_future_task_is_not_overdue(self):
        self.given_plan_in_the_future()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        future = [
            t
            for t in self.multi_tasks(response)
            if t["due"] and t["due"] > date.today()
        ]
        self.assertTrue(future, "the fixtures should carry tasks that are still due")
        self.assertEqual([t for t in future if t["urgency"] == "overdue"], [])

    def test_the_summary_is_generated_for_the_real_today(self):
        """A fix that only corrects the classification would leave the AI card
        narrating the simulated date — the contradiction the issue observed."""
        self.given_plan_in_the_future()
        self.client.get(reverse("dashboard") + "?mode=multi")
        call = self.ai_mocks["projects.views.generate_weekly_summary"].call_args
        self.assertEqual(call[0][1], date.today())

    def test_no_simulation_banner_and_no_simulation_label(self):
        self.given_plan_in_the_future()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertNotContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, "KI-Simulation")
        self.assertContains(response, "KI-Wochenübersicht")

    def test_the_simulated_date_survives_the_detour(self):
        """The simulation belongs to the visitor's plan, so a look at the example
        projects scopes it out rather than resetting it."""
        self.given_plan_in_the_future()
        self.client.get(reverse("dashboard") + "?mode=multi")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt")


class MultiViewSummaryCacheTest(DemoModeTestCase):
    """#51: the multi-project view called Claude on every single GET — the path
    the landing-page CTA sends every first-time visitor to. Its input is a pure
    function of date.today() with no per-visitor data, so one call per day
    serves everyone."""

    @property
    def summary_mock(self):
        return self.ai_mocks["projects.views.generate_weekly_summary"]

    def test_claude_is_called_once_for_repeated_visits(self):
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertEqual(self.summary_mock.call_count, 1)

    def test_the_second_visit_still_renders_the_summary(self):
        """ "Called once" must not be bought with a blank AI card."""
        self.client.get(reverse("dashboard") + "?mode=multi")
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Test summary")

    def test_the_summary_is_cached_under_the_current_key(self):
        """Without this, a key bump would leave the tests above vacuously green."""
        self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertIsNotNone(
            cache.get(f"{DEMO_MULTI_SUMMARY_KEY}_{date.today().isoformat()}")
        )

    def test_a_failure_is_not_cached(self):
        self.summary_mock.side_effect = AIUnavailableError("boom")
        first = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(first, "nicht verfügbar")
        self.summary_mock.side_effect = None
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Test summary")

    def test_the_single_plan_view_does_not_use_the_multi_cache(self):
        """The visitor's own plan is per-session data and stays in the session."""
        self.given_session_plan()
        self.client.get(reverse("dashboard"))
        self.assertIsNone(
            cache.get(f"{DEMO_MULTI_SUMMARY_KEY}_{date.today().isoformat()}")
        )
        self.assertIn(f"{SUMMARY_KEY}_today", self.client.session)


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
            patch("projects.views.generate_weekly_summary", return_value="**Test**"),
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
            patch("projects.views.generate_weekly_summary", return_value="**Test**"),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="date-uncertain-badge"')


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


# --- Unit tests for the logic that is not a view ---


class BuildPromptKontextUebersichtTest(SimpleTestCase):
    """kontext is production-only (#18): a demo prompt carries no per-project
    kontext, so the cross-project overview it feeds has nothing to group —
    the heading itself must then not appear, rather than render empty."""

    def test_single_project_demo_with_no_kontext_omits_the_heading(self):
        project = {
            "name": "Sommerkonzert",
            "event_date": date.today() + timedelta(days=10),
            "performers": "",
            "tasks": [
                {
                    "name": "Programm festlegen",
                    "done": False,
                    "due": None,
                    "kontext": [],
                }
            ],
        }
        prompt = build_prompt([project], date.today(), single_project_demo=True)
        self.assertNotIn("Kontext-Übersicht", prompt)

    def test_production_prompt_with_kontext_keeps_the_heading(self):
        project = {
            "name": "Sommerkonzert",
            "event_date": date.today() + timedelta(days=10),
            "performers": "",
            "tasks": [
                {
                    "name": "GEMA-Meldung",
                    "done": False,
                    "due": None,
                    "kontext": ["Büro"],
                }
            ],
        }
        prompt = build_prompt([project], date.today())
        self.assertIn("Kontext-Übersicht", prompt)
        self.assertIn("**Büro:** GEMA-Meldung", prompt)


class ValidMomentsTest(SimpleTestCase):
    """generate_timelapse_moments returns raw model JSON. These dates become an
    allowlist and are parsed back later, so they cannot be taken on trust."""

    def test_well_formed_moments_survive_untouched(self):
        moments = [{"date": "2026-09-05", "label": "Probe", "description": "Text"}]
        self.assertEqual(_valid_moments(moments), moments)

    def test_unparseable_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "05.09.2026", "label": "Probe"}]), [])

    def test_impossible_day_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "2026-02-30"}]), [])

    def test_non_string_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": None}, {"date": ["2026-09-05"]}]), [])

    def test_moment_without_a_date_is_dropped(self):
        self.assertEqual(_valid_moments([{"label": "Probe"}, "nonsense"]), [])

    def test_parseable_date_is_normalised(self):
        """date.fromisoformat also takes the basic and week forms, which the dashboard
        JS cannot — it builds `date + 'T12:00:00'`. Rewrite them rather than drop them."""
        self.assertEqual(
            _valid_moments([{"date": "20260905", "label": "Probe"}]),
            [{"date": "2026-09-05", "label": "Probe"}],
        )
        self.assertEqual(
            _valid_moments([{"date": "2026-W36-6"}]), [{"date": "2026-09-05"}]
        )

    def test_datetime_string_is_dropped(self):
        self.assertEqual(_valid_moments([{"date": "2026-09-05T10:00:00"}]), [])

    def test_good_moments_are_kept_when_a_sibling_is_dropped(self):
        result = _valid_moments([{"date": "kaputt"}, {"date": "2026-09-05"}])
        self.assertEqual(result, [{"date": "2026-09-05"}])

    def test_a_non_list_response_yields_no_moments(self):
        self.assertEqual(_valid_moments({"date": "2026-09-05"}), [])
        self.assertEqual(_valid_moments(None), [])

    def test_out_of_order_moments_are_sorted_chronologically(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "2026-12-14", "label": "Visuelles Gesicht"},
            {"date": "2027-01-08", "label": "Verkauf öffnet"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
                {"date": "2027-01-08", "label": "Verkauf öffnet"},
            ],
        )

    def test_sort_uses_the_normalised_date_not_the_original_spelling(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "20261214", "label": "Visuelles Gesicht"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
            ],
        )

    def test_invalid_moments_are_dropped_before_sorting(self):
        moments = [
            {"date": "2026-12-23", "label": "Verträge besiegelt"},
            {"date": "kaputt"},
            {"date": "2026-12-14", "label": "Visuelles Gesicht"},
        ]
        self.assertEqual(
            _valid_moments(moments),
            [
                {"date": "2026-12-14", "label": "Visuelles Gesicht"},
                {"date": "2026-12-23", "label": "Verträge besiegelt"},
            ],
        )


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

    def test_task_without_due_date_is_done(self):
        self.assertEqual(self.urgency_for(due=None), "done")

    def test_past_due_is_overdue(self):
        self.assertEqual(
            self.urgency_for(due=self.TODAY - timedelta(days=1)), "overdue"
        )

    def test_today_is_urgent(self):
        self.assertEqual(self.urgency_for(due=self.TODAY), "urgent")

    def test_seven_days_out_is_still_urgent(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=7)), "urgent")

    def test_eight_days_out_is_ok(self):
        self.assertEqual(self.urgency_for(due=self.TODAY + timedelta(days=8)), "ok")

    def test_overdue_beats_urgent_on_the_project(self):
        project = self.annotate(
            {"due": self.TODAY + timedelta(days=2)},
            {"due": self.TODAY - timedelta(days=2)},
        )
        self.assertEqual(project["urgency"], "overdue")

    def test_project_without_open_work_stays_ok(self):
        project = self.annotate({"due": self.TODAY + timedelta(days=30)})
        self.assertEqual(project["urgency"], "ok")

    def test_due_display_is_formatted_german(self):
        task = self.annotate({"due": date(2026, 6, 15)})["tasks"][0]
        self.assertEqual(task["due_display"], "Mo, 15. Juni")

    def test_done_count_and_total_count_for_a_mixed_set(self):
        project = self.annotate(
            {"done": True, "due": None},
            {"done": False, "due": self.TODAY + timedelta(days=1)},
            {"done": False, "due": self.TODAY - timedelta(days=1)},
        )
        self.assertEqual(project["done_count"], 1)
        self.assertEqual(project["total_count"], 3)

    def test_a_dateless_undone_task_does_not_count_as_done(self):
        # It's annotated urgency="done" above (no due date), but done_count
        # has to come from task["done"] directly or this would miscount it.
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


class FixAiMarkdownTest(SimpleTestCase):
    """Claude returns task lines under a project bullet without list markers."""

    def test_continuation_lines_become_sub_bullets(self):
        result = _fix_ai_markdown("- **Konzert**\nPlakate aushängen")
        self.assertEqual(result, "- **Konzert**\n    - Plakate aushängen")

    def test_blank_lines_inside_a_block_are_dropped(self):
        result = _fix_ai_markdown("- **Konzert**\n\nPlakate aushängen")
        self.assertEqual(result, "- **Konzert**\n    - Plakate aushängen")

    def test_existing_list_markers_are_left_alone(self):
        result = _fix_ai_markdown("- **Konzert**\n- Plakate aushängen")
        self.assertEqual(result, "- **Konzert**\n- Plakate aushängen")

    def test_horizontal_rule_ends_the_block(self):
        """A boundary glued to the last task line would be lazily continued
        into the list by Markdown — a blank line is restored before it."""
        result = _fix_ai_markdown("- **Konzert**\n---\nFreier Text")
        self.assertEqual(result, "- **Konzert**\n\n---\nFreier Text")

    def test_bold_line_ends_the_block(self):
        result = _fix_ai_markdown("- **Konzert**\n**Hinweis**\nFreier Text")
        self.assertEqual(result, "- **Konzert**\n\n**Hinweis**\nFreier Text")

    def test_text_outside_a_block_is_untouched(self):
        self.assertEqual(_fix_ai_markdown("Nur ein Satz."), "Nur ein Satz.")

    def test_section_header_keeps_its_blank_line(self):
        """A '##' header after a project block used to lose the blank line that
        makes it a header, so Markdown rendered it as list content and the
        summary showed an unexplained gap. See #20.
        """
        text = "- **Konzert**\n\n## Jetzt fällig\n\nPlakate aushängen"
        self.assertEqual(_fix_ai_markdown(text), text)

    def test_section_header_ends_the_block(self):
        """'#' must reset in_project — text after the header is not a sub-task."""
        result = _fix_ai_markdown(
            "- **Konzert**\nPlakate aushängen\n## Nächste Woche\nFreier Text"
        )
        self.assertEqual(
            result,
            "- **Konzert**\n    - Plakate aushängen\n\n## Nächste Woche\nFreier Text",
        )

    def test_shallow_sub_task_indent_is_deepened_to_nest(self):
        """python-markdown nests a sub-list at four spaces of indent; the two
        the model tends to emit leave every sub-task a flat sibling li."""
        result = _fix_ai_markdown("- **Konzert**\n  - Plakate aushängen")
        self.assertEqual(result, "- **Konzert**\n    - Plakate aushängen")

    def test_four_space_indent_is_left_alone(self):
        result = _fix_ai_markdown("- **Konzert**\n    - Plakate aushängen")
        self.assertEqual(result, "- **Konzert**\n    - Plakate aushängen")

    def test_shallow_indent_outside_a_block_is_untouched(self):
        self.assertEqual(_fix_ai_markdown("  - Notiz"), "  - Notiz")

    def test_new_format_reply_renders_headers_and_nested_lists(self):
        """The whole pipeline: a reply in the ## format (see build_prompt)
        through markdown() ends up with real h2 headers and nested sub-task
        lists — not a <p><strong> next to an invisible <hr>. See #20."""
        # Suppressed rather than turned into the f-string the rule wants: this
        # fixture is Markdown, and one line per line is what keeps it readable.
        reply = "\n".join(  # noqa: FLY002
            [
                "## Jetzt fällig",
                "",
                "- **Sommerkonzert, 5. Aug** — Plakate müssen heute raus:",
                "  - Plakate aushängen",
                "  - GEMA-Meldung",
                "",
                "## Nächste Woche",
                "",
                "- **Herbstkonzert** — noch gut im Zeitplan:",
                "  - Programm festlegen",
            ]
        )
        html = markdown.markdown(_fix_ai_markdown(reply))
        self.assertIn("<h2>Jetzt fällig</h2>", html)
        self.assertIn("<h2>Nächste Woche</h2>", html)
        self.assertNotIn("<hr", html)
        self.assertNotIn("<p><strong>", html)
        # Two blocks, each an outer project list with a nested sub-task list.
        self.assertEqual(html.count("<ul>"), 4)
        self.assertIn("<li>Plakate aushängen</li>", html)


# --- #29: fail at startup, not at first request ---


class RequiredApiKeysTest(SimpleTestCase):
    """wsgi.py calls require_api_keys() before serving a single request. This is
    deliberately not a Django system check: `manage.py test` runs the full check
    registry with no tag filtering (DiscoverRunner.run_checks -> call_command
    ("check", ...)), which would break the offline proof in README.md ("env -u
    ANTHROPIC_API_KEY python manage.py test projects"). wsgi.py is only imported
    by runserver and gunicorn — the processes that actually serve traffic.
    """

    def test_missing_anthropic_key_raises(self):
        with (
            patch.dict(os.environ, {"NOTION_API_KEY": "x"}, clear=True),
            self.assertRaises(MissingAPIKeyError) as ctx,
        ):
            require_api_keys()
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    @override_settings(DEMO_MODE=False)
    def test_missing_notion_key_raises_outside_demo_mode(self):
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=True),
            self.assertRaises(MissingAPIKeyError) as ctx,
        ):
            require_api_keys()
        self.assertIn("NOTION_API_KEY", str(ctx.exception))

    @override_settings(DEMO_MODE=True)
    def test_missing_notion_key_is_fine_in_demo_mode(self):
        # Demo mode never calls notion.py — get_upcoming_projects etc. are only
        # reached from the non-demo branch of every view that touches Notion.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=True):
            require_api_keys()

    @override_settings(DEMO_MODE=False)
    def test_all_keys_present_is_fine(self):
        env = {"ANTHROPIC_API_KEY": "x", "NOTION_API_KEY": "y"}
        with patch.dict(os.environ, env, clear=True):
            require_api_keys()

    @override_settings(DEMO_MODE=False)
    def test_both_missing_names_both_variables(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(MissingAPIKeyError) as ctx,
        ):
            require_api_keys()
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))
        self.assertIn("NOTION_API_KEY", str(ctx.exception))


def _anthropic_timeout_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


def _anthropic_rate_limit_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


class AnthropicFailureTranslationTest(SimpleTestCase):
    """ai.py's two Claude calls translate SDK failures into one app-level
    exception, after the SDK's own retries (max_retries=2 by default) are
    exhausted. Views only need to catch AIUnavailableError, never an
    anthropic.* type directly."""

    def test_weekly_summary_translates_a_timeout(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = (
                _anthropic_timeout_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_weekly_summary_translates_a_rate_limit(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.side_effect = (
                _anthropic_rate_limit_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_weekly_summary([], date.today())

    def test_timelapse_moments_translates_a_failure(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.create.side_effect = (
                _anthropic_timeout_error()
            )
            with self.assertRaises(AIUnavailableError):
                generate_timelapse_moments("Test", date.today(), [])

    def test_original_exception_is_preserved_as_the_cause(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            timeout = _anthropic_timeout_error()
            MockAnthropic.return_value.messages.stream.side_effect = timeout
            with self.assertRaises(AIUnavailableError) as ctx:
                generate_weekly_summary([], date.today())
        self.assertIs(ctx.exception.__cause__, timeout)


def _fake_response(text, model="claude-sonnet-4-6", input_tokens=100, output_tokens=50):
    return Mock(
        content=[Mock(text=text)],
        model=model,
        usage=Mock(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class LogClaudeCallTest(SimpleTestCase):
    """log_claude_call (#31) wraps a Claude call site: exception translation
    (#29) plus structured duration/usage logging on success."""

    def test_logs_model_duration_and_tokens_on_success(self):
        with (
            self.assertLogs("projects.ai", level="INFO") as cm,
            log_claude_call("some_call") as result,
        ):
            result["message"] = _fake_response(
                "hi", model="claude-sonnet-4-6", input_tokens=123, output_tokens=45
            )
        [record] = cm.output
        self.assertIn("call=some_call", record)
        self.assertIn("model=claude-sonnet-4-6", record)
        self.assertIn("input_tokens=123", record)
        self.assertIn("output_tokens=45", record)
        self.assertIn("outcome=success", record)

    def test_logs_outcome_error_on_anthropic_failure(self):
        with (
            self.assertLogs("projects.ai", level="WARNING") as cm,
            self.assertRaises(AIUnavailableError),
            log_claude_call("some_call"),
        ):
            raise _anthropic_timeout_error()
        [record] = cm.output
        self.assertIn("call=some_call", record)
        self.assertIn("outcome=error", record)

    def test_success_path_does_not_also_log_a_warning(self):
        with (
            self.assertLogs("projects.ai", level="INFO") as cm,
            log_claude_call("some_call") as result,
        ):
            result["message"] = _fake_response("hi")
        self.assertEqual(len(cm.output), 1)


class TimelapseBaselineUsesLocalDateTest(SimpleTestCase):
    """#85: generate_timelapse_moments used date.today() as the "Zeitraum"
    start date fed into the Claude prompt — same day-boundary bug as task
    urgency, just baked into a prompt instead of an urgency bucket."""

    @patch("django.utils.timezone.now")
    def test_prompt_zeitraum_start_is_the_berlin_date(self, mock_now):
        mock_now.return_value = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response("[]")
            generate_timelapse_moments("Test", date(2026, 2, 1), [])
        prompt = create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Zeitraum: 2026-01-16 bis", prompt)


class TimelapseMomentsLoggingTest(SimpleTestCase):
    def test_logs_usage_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            create = MockAnthropic.return_value.messages.create
            create.return_value = _fake_response(
                "[]", input_tokens=77, output_tokens=12
            )
            with self.assertLogs("projects.ai", level="INFO") as cm:
                generate_timelapse_moments("Test", date.today(), [])
        [record] = cm.output
        self.assertIn("call=generate_timelapse_moments", record)
        self.assertIn("input_tokens=77", record)
        self.assertIn("output_tokens=12", record)
        self.assertIn("outcome=success", record)


class WeeklySummaryLoggingTest(SimpleTestCase):
    def test_logs_usage_on_success(self):
        fake_stream = MagicMock()
        fake_stream.__enter__.return_value = fake_stream
        fake_stream.get_final_text.return_value = "Zusammenfassung"
        fake_stream.get_final_message.return_value = _fake_response(
            "Zusammenfassung", input_tokens=200, output_tokens=80
        )
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.return_value = fake_stream
            with self.assertLogs("projects.ai", level="INFO") as cm:
                text = generate_weekly_summary([], date.today())
        self.assertEqual(text, "Zusammenfassung")
        [record] = cm.output
        self.assertIn("call=generate_weekly_summary", record)
        self.assertIn("input_tokens=200", record)
        self.assertIn("output_tokens=80", record)
        self.assertIn("outcome=success", record)


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


class NotionFailureTranslationTest(SimpleTestCase):
    """notion.py's six public functions each build a fresh client per call
    (_client()) and were entirely unguarded. translate_notion_errors() covers
    notion_client's own exception types (HTTPResponseError, RequestTimeoutError)
    plus httpx.HTTPError as a safety net — notion_client only wraps a timeout,
    not a raw connection failure like httpx.ConnectError."""

    def setUp(self):
        # _client() reads os.environ["NOTION_API_KEY"] directly (a KeyError,
        # not a graceful failure, if unset) — irrelevant to what's under test
        # here, so pin it rather than depend on the ambient environment.
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stub_every_call(self, MockClient, exc):
        instance = MockClient.return_value
        instance.databases.query.side_effect = exc
        instance.pages.update.side_effect = exc
        instance.pages.create.side_effect = exc
        return instance

    def test_get_upcoming_projects_translates_a_timeout(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                get_upcoming_projects(date.today())

    def test_get_historical_projects_translates_an_http_error(self):
        request = httpx.Request("POST", "https://api.notion.com/v1/databases/x/query")
        response = httpx.Response(500, request=request)
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, HTTPResponseError(response))
            with self.assertRaises(NotionUnavailableError):
                get_historical_projects()

    def test_toggle_task_translates_a_raw_connection_error(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, httpx.ConnectError("boom"))
            with self.assertRaises(NotionUnavailableError):
                toggle_task("task-id", True)

    def test_update_task_date_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                update_task_date("task-id", "2026-09-05")

    def test_create_project_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_project("Test", date.today())

    def test_create_tasks_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                create_tasks("project-id", [{"name": "x", "date": "2026-09-05"}])


class CreateProjectDateUncertainTest(SimpleTestCase):
    """create_project's new date_uncertain param writes a "Termin unsicher"
    checkbox, the read-path (get_upcoming_projects/get_historical_projects)
    counterpart to the fallback date planner_review/planner_create apply
    when the description carried no recognizable date."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_to_false(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.create.return_value = {"id": "page-1"}
            create_project("Sommerkonzert", date(2026, 9, 5))
        properties = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(properties["Termin unsicher"], {"checkbox": False})

    def test_true_when_the_date_was_a_guess(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.create.return_value = {"id": "page-1"}
            create_project("Herbstkonzert", date(2026, 10, 3), True)
        properties = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(properties["Termin unsicher"], {"checkbox": True})


class FindProjectTest(SimpleTestCase):
    """find_project makes retrying planner_create idempotent at the project
    level: an attempt that died in create_tasks left a project page behind,
    and the retry must find and reuse it instead of creating a twin."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_id_of_an_exact_match(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [{"id": "page-1"}]
            }
            self.assertEqual(find_project("Sommerkonzert", date(2026, 9, 5)), "page-1")

    def test_queries_by_exact_name_and_date(self):
        with patch("projects.notion.Client") as MockClient:
            query = MockClient.return_value.databases.query
            query.return_value = {"results": []}
            find_project("Sommerkonzert", date(2026, 9, 5))
        conditions = query.call_args.kwargs["filter"]["and"]
        self.assertIn(
            {
                "property": "Name der Veranstaltung",
                "title": {"equals": "Sommerkonzert"},
            },
            conditions,
        )
        self.assertIn(
            {"property": "Termin", "date": {"equals": "2026-09-05"}}, conditions
        )

    def test_returns_none_when_nothing_matches(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {"results": []}
            self.assertIsNone(find_project("Sommerkonzert", date(2026, 9, 5)))

    def test_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                find_project("Sommerkonzert", date(2026, 9, 5))


def _fake_task_page(name, iso_date):
    # Shaped the way _get_tasks parses a Notion task page.
    return {
        "id": "task-1",
        "properties": {
            "Aufgabe": {"title": [{"plain_text": name}]},
            "Wann?": {"date": {"start": iso_date}},
            "Done": {"checkbox": False},
            "Kontext": {"multi_select": []},
        },
    }


class CreateTasksIdempotencyTest(SimpleTestCase):
    """A retried save reaches create_tasks with the same list a failed
    attempt may have partially written (one API call per task) — what
    already made it to Notion must be skipped, not created again."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_already_written_tasks_are_skipped(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                "results": [_fake_task_page("Programm festlegen", "2026-08-20")]
            }
            create_tasks(
                "project-id",
                [
                    {"name": "Programm festlegen", "date": "2026-08-20"},
                    {"name": "Plakate aushängen", "date": "2026-08-27"},
                ],
            )
        self.assertEqual(instance.pages.create.call_count, 1)
        created = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(
            created["Aufgabe"]["title"][0]["text"]["content"], "Plakate aushängen"
        )

    def test_a_fresh_project_writes_the_whole_list(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            create_tasks(
                "project-id",
                [
                    {"name": "Programm festlegen", "date": "2026-08-20"},
                    {"name": "Plakate aushängen", "date": "2026-08-27"},
                ],
            )
        self.assertEqual(instance.pages.create.call_count, 2)

    def test_same_name_on_a_different_date_is_not_skipped(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {
                "results": [_fake_task_page("Programm festlegen", "2026-08-20")]
            }
            create_tasks(
                "project-id", [{"name": "Programm festlegen", "date": "2026-08-27"}]
            )
        self.assertEqual(instance.pages.create.call_count, 1)

    def test_writes_kontext_as_a_multi_select_property(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            create_tasks(
                "project-id",
                [{"name": "GEMA-Meldung", "date": "2026-08-20", "kontext": ["Büro"]}],
            )
        created = instance.pages.create.call_args.kwargs["properties"]
        self.assertEqual(created["Kontext"], {"multi_select": [{"name": "Büro"}]})


def _fake_upcoming_project(name="Testkonzert"):
    return {
        "id": "p1",
        "name": name,
        "event_date": date.today() + timedelta(days=10),
        "performers": "",
        "status": None,
        "status_color": "gray",
        "tasks": [],
    }


def _fake_upcoming_project_with_task():
    """A project whose tasks actually render — the empty task list above never
    reaches the per-task markup."""
    project = _fake_upcoming_project()
    project["tasks"] = [
        {
            "id": "task-1",
            "name": "Programm festlegen",
            "due": date.today() + timedelta(days=3),
            "done": False,
            "kontext": [],
        }
    ]
    return project


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
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Sommerkonzert**",
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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Sommerkonzert**",
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "evtl. nicht")


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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Sommerkonzert**",
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'stroke-dashoffset="43.98"')


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
            patch(
                "projects.views.generate_weekly_summary", return_value="**Wieder da**"
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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Letzte gute Übersicht**",
            ),
        ):
            self.client.get(reverse("dashboard"))

        cache.delete(CACHE_KEY)  # the 8h primary cache expiring; the stale copy stays
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=[_fake_upcoming_project()],
            ),
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
        mock_toggle.assert_called_once_with("task-1", True)

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
        with patch("projects.views.update_task_date") as mock_update:
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
        with patch("projects.views.update_task_date"):
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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Sommerkonzert**",
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Aktualisieren")
        self.assertNotContains(response, "Sync mit Notion")


class NginxDemoRateLimitTest(SimpleTestCase):
    """#36: guards the demo rate-limit sizing against config drift. rate=10r/m
    with burst=5 was fine for one request per page view, but the timelapse
    preloader alone fires 4 POSTs per dashboard load."""

    def setUp(self):
        self.conf = (settings.BASE_DIR / "nginx-demo.conf").read_text()

    def test_rate_and_burst_cover_a_page_load_plus_preloads(self):
        self.assertIn("rate=30r/m", self.conf)
        self.assertIn("burst=10 nodelay", self.conf)

    def test_rejections_are_a_429_not_a_bare_503(self):
        self.assertIn("limit_req_status 429;", self.conf)


class TimelapsePrecachedMomentsTest(DemoModeTestCase):
    """#36: preloadAll() re-fires all 4 preload POSTs on every reload because
    the in-memory `preloaded` Set is empty again. Moments the session already
    cached a summary for are now passed to the template so the JS can seed
    `preloaded` from them instead of re-requesting."""

    def test_a_moment_with_a_cached_summary_is_precached(self):
        self.given_timelapse_moments("2026-09-05")
        session = self.client.session
        session[f"{SUMMARY_KEY}_2026-09-05"] = "<p>cached</p>"
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'const PRECACHED_MOMENTS = ["2026-09-05"]')

    def test_a_moment_without_a_cached_summary_is_not_precached(self):
        self.given_timelapse_moments("2026-09-05")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const PRECACHED_MOMENTS = []")

    def test_malformed_session_moments_do_not_break_the_dashboard(self):
        session = self.client.session
        session["demo_timelapse_moments"] = [
            {"date": ["2026-09-05"]},
            {"date": None},
            "not-a-dict",
        ]
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)


class TimelapsePreloadMarkupTest(DemoModeTestCase):
    """#36: markup-contract tests for the preloader fix — runtime behaviour
    (a rate-limited fetch really gets skipped) isn't provable by a Django
    TestCase and gets a manual browser pass, same boundary documented on
    PlannerLoadingStateTest."""

    def test_preload_one_checks_response_ok_before_marking_preloaded(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "if (!response.ok) return;")

    def test_template_seeds_preloaded_from_precached_moments(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const PRECACHED_MOMENTS = ")
        self.assertContains(response, "const preloaded = new Set(PRECACHED_MOMENTS);")


class MyPlanProgressBarTest(DemoModeTestCase):
    """§4 of #10: the bar rendered `width: {{ done_count }}00%` — the done count
    times 100 rather than a percentage — so it was full from the first completed
    task on. updateProgress() computes it correctly but only runs after a toggle,
    so on page load the bar was never right."""

    def given_plan_with(self, total, done):
        return self.given_session_plan(
            tasks=[
                {
                    "id": f"demo-session-{i}",
                    "name": f"Aufgabe {i}",
                    "date": (date.today() + timedelta(days=i + 1)).isoformat(),
                    "kontext": "Planung",
                    "done": i < done,
                }
                for i in range(total)
            ]
        )

    def test_shows_the_correct_percentage_on_load(self):
        self.given_plan_with(total=4, done=2)
        self.assertContains(self.client.get(reverse("my_plan")), "width: 50%")

    def test_does_not_multiply_the_done_count_by_hundred(self):
        self.given_plan_with(total=3, done=3)
        response = self.client.get(reverse("my_plan"))
        self.assertNotContains(response, "width: 300%")
        self.assertContains(response, "width: 100%")

    def test_nothing_done_is_zero_percent(self):
        self.given_plan_with(total=4, done=0)
        self.assertContains(self.client.get(reverse("my_plan")), "width: 0%")


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
        self.assertContains(reloaded, _format_date(date.fromisoformat(self.NEW_DATE)))

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
        self.assertNotContains(response, 'class="today-btn"')

    def test_not_offered_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'title="Datum ändern"')
        self.assertNotContains(response, 'class="today-btn"')

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
            patch(
                "projects.views.generate_weekly_summary",
                return_value="**Sommerkonzert**",
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="Datum ändern"')
        self.assertContains(response, 'data-task-id="task-1"')


class PlannerRulesBackLinkTest(DemoModeTestCase):
    """#7 (inherited from #22): the rules page's "← Planer" link always
    returned to the empty tile step, discarding whatever project type the
    visitor had already chosen — even though planner_start's ?type= handler
    already writes it to the session unconditionally. Only the step is
    restored, not unsaved free text (see PlannerTileLinksTest for why)."""

    def test_back_link_is_bare_without_a_chosen_type(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, f'href="{reverse("planner_start")}"')
        self.assertNotContains(response, "?type=")

    def test_back_link_carries_the_previously_chosen_type(self):
        self.client.get(reverse("planner_start") + "?type=konzert")
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, f'href="{reverse("planner_start")}?type=konzert"')


class PlannerRulesDemoModeTest(DemoModeTestCase):
    """#22: PlannerRule was the one demo-editable object that was not
    session-scoped, so any anonymous visitor could rewrite or delete the rules
    every other visitor's plan is generated with. In demo mode the rules now
    live in request.session, seeded from INITIAL_RULES."""

    def request_with_session(self):
        """A request carrying this client's session, as the planner views see it."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    def rule_ids(self):
        """Reading persists nothing, so before the first write fall back to the
        seed's ids — _seed() hands out 1..n every time."""
        rules = self.client.session.get(DEMO_RULES_KEY)
        if rules is None:
            return list(range(1, len(INITIAL_RULES) + 1))
        return [r["id"] for r in rules]

    def test_a_fresh_session_is_seeded_with_the_initial_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        for text in INITIAL_RULES:
            self.assertContains(response, text)
        self.assertNotContains(response, "Noch keine Regeln")

    def test_reading_the_rules_page_persists_no_session(self):
        """The demo is public and not yet behind a robots.txt (#27), so a GET
        must not leave a session row behind for every visitor and crawler."""
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, INITIAL_RULES[0])
        self.assertEqual(Session.objects.count(), 0)

    def test_the_first_write_persists_the_seeded_rules(self):
        self.client.post(reverse("rule_toggle", args=[self.rule_ids()[0]]))
        self.assertEqual(Session.objects.count(), 1)
        stored = self.client.session[DEMO_RULES_KEY]
        self.assertEqual([r["text"] for r in stored], INITIAL_RULES)
        self.assertFalse(stored[0]["active"])

    def test_adding_a_rule_writes_nothing_to_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        self.assertEqual(PlannerRule.objects.count(), 0)
        self.assertContains(self.client.get(reverse("rules_list")), "Neue Regel")

    def test_toggle_update_delete_and_reorder_write_nothing_to_the_database(self):
        self.client.get(reverse("rules_list"))
        ids = self.rule_ids()
        self.client.post(reverse("rule_toggle", args=[ids[0]]))
        self.client.post(
            reverse("rule_update", args=[ids[1]]),
            data=json.dumps({"text": "Geänderte Regel"}),
            content_type="application/json",
        )
        self.client.post(reverse("rule_delete", args=[ids[2]]))
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(i) for i in reversed(ids[:2])]}),
            content_type="application/json",
        )
        self.assertEqual(PlannerRule.objects.count(), 0)

    def test_one_visitor_cannot_change_what_another_one_sees(self):
        other = Client()
        self.client.post(reverse("rule_add"), data={"text": "Nur für mich"})
        first_id = self.rule_ids()[0]
        self.client.post(reverse("rule_delete", args=[first_id]))

        response = other.get(reverse("rules_list"))
        self.assertNotContains(response, "Nur für mich")
        for text in INITIAL_RULES:
            self.assertContains(response, text)

    def test_a_deactivated_rule_stays_listed_but_leaves_the_prompt(self):
        self.client.get(reverse("rules_list"))
        ids = self.rule_ids()
        response = self.client.post(reverse("rule_toggle", args=[ids[0]]))
        self.assertEqual(response.json()["active"], False)

        request = self.request_with_session()
        self.assertNotIn(INITIAL_RULES[0], get_active_rule_texts(request))
        self.assertEqual(get_active_rule_texts(request), INITIAL_RULES[1:])
        self.assertContains(self.client.get(reverse("rules_list")), INITIAL_RULES[0])

    def test_reordering_reaches_the_prompt_in_the_new_order(self):
        self.client.get(reverse("rules_list"))
        ids = self.rule_ids()
        reordered = [ids[-1]] + ids[:-1]
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(i) for i in reordered]}),
            content_type="application/json",
        )
        expected = [INITIAL_RULES[-1]] + INITIAL_RULES[:-1]
        self.assertEqual(get_active_rule_texts(self.request_with_session()), expected)

    def test_toggling_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_toggle", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_deleting_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_a_poisoned_session_re_seeds_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = "kaputt"
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, INITIAL_RULES[0])

    def test_entries_of_the_wrong_shape_re_seed_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = [{"id": 1}, "kaputt"]
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, INITIAL_RULES[0])

    def test_the_page_explains_the_demo_scope_and_inactive_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "diesem Besuch")
        self.assertContains(response, "nicht in den Plan")


@override_settings(DEMO_MODE=False)
class PlannerRulesDatabaseModeTest(TestCase):
    """The production path is untouched by #22: rules stay in the database and
    the session stays empty."""

    def setUp(self):
        for i, text in enumerate(INITIAL_RULES):
            PlannerRule.objects.create(text=text, active=True, order=i)

    def test_add_creates_a_rule_in_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        rule = PlannerRule.objects.get(text="Neue Regel")
        self.assertTrue(rule.active)
        self.assertEqual(rule.order, len(INITIAL_RULES))
        self.assertNotIn(DEMO_RULES_KEY, self.client.session)

    def test_toggle_flips_the_database_row(self):
        rule = PlannerRule.objects.first()
        response = self.client.post(reverse("rule_toggle", args=[rule.pk]))
        self.assertEqual(response.json()["active"], False)
        rule.refresh_from_db()
        self.assertFalse(rule.active)

    def test_update_changes_the_database_row(self):
        rule = PlannerRule.objects.first()
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"text": "Geänderte Regel"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.text, "Geänderte Regel")

    def test_delete_removes_the_database_row(self):
        rule = PlannerRule.objects.first()
        self.client.post(reverse("rule_delete", args=[rule.pk]))
        self.assertFalse(PlannerRule.objects.filter(pk=rule.pk).exists())
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES) - 1)

    def test_deleting_an_unknown_rule_is_a_404(self):
        response = self.client.post(reverse("rule_delete", args=[9999]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))

    def test_reorder_writes_the_new_order_column(self):
        ids = list(PlannerRule.objects.values_list("pk", flat=True))
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(i) for i in reversed(ids)]}),
            content_type="application/json",
        )
        self.assertEqual(
            list(PlannerRule.objects.values_list("pk", flat=True)),
            list(reversed(ids)),
        )

    def test_the_prompt_gets_the_active_rules_from_the_database(self):
        PlannerRule.objects.filter(text=INITIAL_RULES[0]).update(active=False)
        request = RequestFactory().get("/")
        request.session = self.client.session
        self.assertEqual(get_active_rule_texts(request), INITIAL_RULES[1:])

    def test_the_demo_notice_is_absent(self):
        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, "diesem Besuch")
