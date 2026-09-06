import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import anthropic
import httpx
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone
from notion_client.errors import HTTPResponseError, RequestTimeoutError

from .ai import (
    AIUnavailableError,
    _number_projects_and_tasks,
    _valid_moments,
    build_closeout_prompt,
    build_prompt,
    generate_closeout_summary,
    generate_timelapse_moments,
    generate_weekly_summary,
    log_claude_call,
    resolve_weekly_summary,
)
from .closeout import get_latest_closeout, is_week_closed, save_closeout
from .date_format import format_date, format_week_range
from .dates import is_same_iso_week, iso_week_bounds
from .models import DemoEvent, PlannerRule, RulesSeeded, WeekCloseout
from .notion import (
    TASKS_DB,
    NotionUnavailableError,
    _get_tasks,
    create_project,
    create_tasks,
    find_project,
    get_historical_projects,
    get_unassigned_tasks,
    get_upcoming_projects,
    increment_postpone_count,
    toggle_task,
    update_task_date,
)
from .planner import generate_plan, get_clarifying_questions
from .planner_views import _get_history, _parse_event_date
from .rules import DEMO_RULES_KEY, INITIAL_RULES, add_rule, get_active_rule_texts
from .startup import MissingAPIKeyError, require_api_keys
from .views import (
    _KANBAN_COLUMN,
    _URGENCY_RANK,
    CACHE_DEADLINE_KEY,
    CACHE_KEY,
    CACHE_TTL,
    DEMO_MULTI_SUMMARY_KEY,
    STALE_CACHE_KEY,
    STALE_UNASSIGNED_CACHE_KEY,
    SUMMARY_KEY,
    UNASSIGNED_CACHE_DEADLINE_KEY,
    UNASSIGNED_CACHE_KEY,
    _annotate_tasks,
    _bucket_by_day,
    _build_week_view,
    _bust_dashboard_cache,
    _cache_fresh_read,
    _count_done_in_range,
    _derive_dashboard_figures,
    _kanban_column,
    _parse_week_param,
    _strip_trailing_date,
)

# The view modules import the AI functions with `from .ai import ...`, so the
# name to patch is the one bound in the view module, not the one in projects.ai.
AI_STUBS = {
    # generate_weekly_summary returns the raw reference dict since #122 —
    # the stub has to match that shape, not the markdown string Claude used
    # to hand back (same evolution as the generate_plan stub below).
    "projects.views.generate_weekly_summary": {
        "jetzt_faellig": [],
        "naechste_woche": [],
    },
    "projects.planner_views.get_clarifying_questions": "**Wie viele Mitwirkende?**",
    # generate_plan now parses its own response and returns a dict (see #29 /
    # GeneratePlanRetryTest) — this stub has to match that shape, not the raw
    # JSON string Claude used to hand back.
    "projects.planner_views.generate_plan": {
        "project_name": "Testkonzert",
        "tasks": [],
    },
    "projects.planner_views.generate_timelapse_moments": [],
    "projects.views.generate_closeout_summary": "Gute Woche gewesen.",
}


def _summary_data(marker="Zusammenfassung läuft"):
    """A minimal raw reference dict (#122) whose assessment carries a
    recognisable marker; project_ref 1 resolves against whatever project the
    test's get_upcoming_projects stub returns first (and the block is simply
    dropped when there is none)."""
    return {
        "jetzt_faellig": [{"project_ref": 1, "assessment": marker, "task_refs": []}],
        "naechste_woche": [],
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


class EntrypointConfTest(SimpleTestCase):
    """#24: entrypoint.sh ran migrate and collectstatic but never seed_rules,
    so a stack built from scratch starts with an empty PlannerRule table —
    get_active_rule_texts() returns [] and the planner prompt is silently
    missing every maintainer rule. seed_rules is idempotent (see
    SeedRulesCommandTest for how), so it is safe to run on every container
    start, including the demo container where it runs but has no effect
    (demo reads the session backend, not this table)."""

    def test_seed_rules_runs_between_migrate_and_gunicorn(self):
        entrypoint = (settings.BASE_DIR / "entrypoint.sh").read_text()
        self.assertIn("manage.py migrate", entrypoint)
        self.assertIn("manage.py seed_rules", entrypoint)
        self.assertLess(
            entrypoint.index("manage.py migrate"),
            entrypoint.index("manage.py seed_rules"),
            "seed_rules must run after migrate",
        )
        self.assertLess(
            entrypoint.index("manage.py seed_rules"),
            entrypoint.index("gunicorn"),
            "seed_rules must run before gunicorn starts serving",
        )


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


class EnvExampleConfTest(SimpleTestCase):
    """#24: settings.py reads CSRF_TRUSTED_ORIGINS from the environment, but
    .env.example never documented it, so a fresh deploy that only copies
    .env.example starts with an empty value and nothing errors — the failure
    surfaces later as a rejected POST behind the reverse proxy. This guards
    every settings.py env var against the same drift, not just this one
    key."""

    def test_env_example_documents_every_settings_env_var(self):
        settings_source = (
            settings.BASE_DIR / "planning_hub" / "settings.py"
        ).read_text()
        read_keys = set(
            re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z_]+)"', settings_source)
        )

        env_example_source = (settings.BASE_DIR / ".env.example").read_text()
        documented_keys = set(
            re.findall(r"^([A-Z_]+)=", env_example_source, re.MULTILINE)
        )

        undocumented = read_keys - documented_keys
        self.assertEqual(
            undocumented,
            set(),
            f".env.example is missing: {sorted(undocumented)}",
        )


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
            response, '<div class="sidebar-title" style="margin-top: 16px;">Demo</div>'
        )
        self.assertContains(response, "Plan als Liste")
        self.assertContains(response, "Woche abschließen")
        self.assertContains(response, "Mehrprojekt-Dashboard")

    def test_no_plan_yet_still_shows_both_group_headers(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Demo</div>'
        )
        self.assertContains(response, "Projekt selbst planen")

    def test_multi_project_view_with_a_plan_shows_full_links_in_both_groups(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(response, '<div class="sidebar-title">Dein Projekt</div>')
        self.assertContains(
            response, '<div class="sidebar-title" style="margin-top: 16px;">Demo</div>'
        )
        self.assertContains(response, "Plan als Liste")
        self.assertContains(response, "Woche abschließen")
        # Currently on the demo view — "Demo" group uses the fast toggle,
        # not the "Mehrprojekt-Dashboard" jump-in link (that's for reaching
        # this view from elsewhere, not for a view you're already on).
        self.assertNotContains(response, "Mehrprojekt-Dashboard")


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
        # mirroring .project-header's existing reservation.
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            ".demo-banner, .sim-banner, .stale-notice, .ai-card-header "
            "{ padding-right: 44px; }",
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
        self.assertContains(response, format_date(date.today() + timedelta(days=30)))


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
        self.summary_mock.return_value = _summary_data("Alles im Plan")
        self.client.get(reverse("dashboard") + "?mode=multi")
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Alles im Plan")

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
        self.summary_mock.return_value = _summary_data("Alles im Plan")
        second = self.client.get(reverse("dashboard") + "?mode=multi")
        self.assertContains(second, "Alles im Plan")

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
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary", return_value=_summary_data()
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="task-kontext">Büro<')
        self.assertNotContains(response, "[&#x27;")


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


class TimelapseSingleDateAuthorityTest(DemoModeTestCase):
    """#153: with a simulated moment active the dashboard showed two
    "today"s at once — the sim banner's simulated date and the real
    {{ today_display }} under the header. The header date now hides during
    a simulation, leaving the banner as the single date authority."""

    def given_active_simulation(self):
        sim = (date.today() + timedelta(days=5)).isoformat()
        self.given_timelapse_moments(sim)
        session = self.client.session
        session["demo_sim_date"] = sim
        session.save()

    def test_the_header_date_hides_during_a_simulation(self):
        self.given_session_plan()
        self.given_active_simulation()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Simulierter Zeitpunkt")
        self.assertNotContains(response, format_date(date.today()))

    def test_the_header_date_is_back_without_a_simulation(self):
        # "Zurück zu heute" clears demo_sim_date, so the no-sim render is
        # exactly the state that button restores.
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, format_date(date.today()))
        self.assertNotContains(response, "Simulierter Zeitpunkt")

    def _view_today_html(self, response):
        """Isolates the "Heute" panel's own markup — the same banner text
        also lives in view-overview, so a page-wide assertContains would
        pass even if this panel specifically were missing it."""
        content = response.content.decode()
        start = content.index('id="view-today"')
        end = content.index('class="project-section" id=', start)
        return content[start:end]

    def test_the_today_view_carries_the_same_sim_banner(self):
        # #53 added a second "today" surface after #153 fixed the first one
        # — without its own banner, switching to "Heute" during a
        # simulation silently drops back into the two-todays confusion
        # #153 was meant to have settled for good.
        self.given_session_plan()
        self.given_active_simulation()
        response = self.client.get(reverse("dashboard"))
        self.assertIn("Simulierter Zeitpunkt", self._view_today_html(response))

    def test_no_sim_banner_in_the_today_view_without_a_simulation(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertNotIn("Simulierter Zeitpunkt", self._view_today_html(response))


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
        # UI directly — only the server's human-readable due_display may.
        self.given_mixed_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const data = await response.json();")
        self.assertContains(response, "dueSpan.textContent = data.due_display;")
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


class PromptUndatedAndTodayTest(SimpleTestCase):
    """#160: the weekly-summary prompt describes undated open tasks as
    "ohne Termin" (previously the nonsensical "fällig in ? Tagen") and flags
    due-today tasks as "HEUTE fällig"."""

    TODAY = date(2026, 9, 1)

    def project_with_task(self, **task):
        return {
            "id": "p-solo",
            "name": "Konzert Solo",
            "event_date": self.TODAY + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-x",
                    "name": "Aufgabe X",
                    "due": None,
                    "done": False,
                    "kontext": [],
                    **task,
                }
            ],
        }

    def test_an_undated_task_reads_ohne_termin(self):
        prompt = build_prompt([self.project_with_task(due=None)], self.TODAY)
        self.assertIn("Aufgabe X — ohne Termin", prompt)
        self.assertNotIn("fällig in ? Tagen", prompt)

    def test_a_task_due_today_reads_heute_faellig(self):
        prompt = build_prompt([self.project_with_task(due=self.TODAY)], self.TODAY)
        self.assertIn("Aufgabe X — HEUTE fällig", prompt)

    def test_a_task_due_this_week_keeps_diese_woche(self):
        prompt = build_prompt(
            [self.project_with_task(due=self.TODAY + timedelta(days=3))], self.TODAY
        )
        self.assertIn("Aufgabe X — DIESE WOCHE", prompt)


class BuildPromptCalendarWeekLabelTest(SimpleTestCase):
    """#169: build_prompt's "DIESE WOCHE" label follows the same calendar-week
    rule as _annotate_tasks now, not a rolling 7-day window — but an overdue
    task's label must stay exactly as it was (see the edge case called out
    in the issue's implementation plan)."""

    # A Tuesday — ISO week 36 runs through the following Sunday.
    TODAY = date(2026, 9, 1)

    def project_with_task(self, **task):
        return {
            "id": "p-solo",
            "name": "Konzert Solo",
            "event_date": self.TODAY + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-x",
                    "name": "Aufgabe X",
                    "due": None,
                    "done": False,
                    "kontext": [],
                    **task,
                }
            ],
        }

    def label_for(self, due):
        prompt = build_prompt([self.project_with_task(due=due)], self.TODAY)
        line = next(line for line in prompt.splitlines() if "Aufgabe X" in line)
        return line

    def test_due_the_last_day_of_this_iso_week_is_diese_woche(self):
        self.assertIn("DIESE WOCHE", self.label_for(self.TODAY + timedelta(days=5)))

    def test_due_the_first_day_of_next_iso_week_is_days_remaining(self):
        line = self.label_for(self.TODAY + timedelta(days=6))
        self.assertIn("(fällig in 6 Tagen)", line)
        self.assertNotIn("DIESE WOCHE", line)

    def test_overdue_from_a_past_calendar_week_still_reads_diese_woche(self):
        # Naively swapping in is_same_iso_week here would send an overdue
        # task from a past week into the days-remaining branch instead —
        # the exact regression the issue's plan calls out.
        line = self.label_for(self.TODAY - timedelta(days=5))
        self.assertIn("DIESE WOCHE", line)
        self.assertNotIn("fällig in -5 Tagen", line)


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


def _fake_stream(text, input_tokens=100, output_tokens=50):
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.get_final_text.return_value = text
    stream.get_final_message.return_value = _fake_response(
        text, input_tokens=input_tokens, output_tokens=output_tokens
    )
    return stream


VALID_SUMMARY_JSON = '{"jetzt_faellig": [], "naechste_woche": []}'


class WeeklySummaryLoggingTest(SimpleTestCase):
    def test_logs_usage_on_success(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            MockAnthropic.return_value.messages.stream.return_value = _fake_stream(
                VALID_SUMMARY_JSON, input_tokens=200, output_tokens=80
            )
            with self.assertLogs("projects.ai", level="INFO") as cm:
                data = generate_weekly_summary([], date.today())
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        [record] = cm.output
        self.assertIn("call=generate_weekly_summary", record)
        self.assertIn("input_tokens=200", record)
        self.assertIn("output_tokens=80", record)
        self.assertIn("outcome=success", record)


def _summary_projects(today=None):
    """Two projects, four tasks; the second task is already done. The
    numbering the prompt and the resolver share counts every task, done ones
    included: 1=Programm, 2=Ensemble (done), 3=Plakate, 4=Technik."""
    today = today or date(2026, 9, 1)
    return [
        {
            "id": "p-alpha",
            "name": "Konzert Alpha",
            "event_date": today + timedelta(days=5),
            "performers": "",
            "tasks": [
                {
                    "id": "t-programm",
                    "name": "Programm festlegen",
                    "due": today + timedelta(days=1),
                    "done": False,
                    "kontext": [],
                    "urgency": "urgent",
                },
                {
                    "id": "t-ensemble",
                    "name": "Ensemble anfragen",
                    "due": today - timedelta(days=10),
                    "done": True,
                    "kontext": [],
                    "urgency": "done",
                },
            ],
        },
        {
            "id": "p-beta",
            "name": "Konzert Beta",
            "event_date": today + timedelta(days=30),
            "performers": "",
            "tasks": [
                {
                    "id": "t-plakate",
                    "name": "Plakate drucken",
                    "due": today + timedelta(days=10),
                    "done": False,
                    "kontext": [],
                    "urgency": "ok",
                },
                {
                    "id": "t-technik",
                    "name": "Technik prüfen",
                    "due": today + timedelta(days=12),
                    "done": False,
                    "kontext": [],
                    "urgency": "ok",
                },
            ],
        },
    ]


class NumberingAndPromptTest(SimpleTestCase):
    """#122: build_prompt and resolve_weekly_summary share one numbering,
    produced by _number_projects_and_tasks. Every task occupies a number,
    done ones included — numbering only open tasks would shift every later
    ref the moment a task is toggled between cache-write and render."""

    def test_numbering_counts_every_task_across_projects(self):
        numbered_projects, numbered_tasks = _number_projects_and_tasks(
            _summary_projects()
        )
        self.assertEqual([p["id"] for p in numbered_projects], ["p-alpha", "p-beta"])
        self.assertEqual(
            [t["id"] for t in numbered_tasks],
            ["t-programm", "t-ensemble", "t-plakate", "t-technik"],
        )

    def test_a_project_without_event_date_is_skipped_like_in_the_prompt(self):
        projects = _summary_projects()
        projects[0]["event_date"] = None
        numbered_projects, numbered_tasks = _number_projects_and_tasks(projects)
        self.assertEqual([p["id"] for p in numbered_projects], ["p-beta"])
        self.assertEqual([t["id"] for t in numbered_tasks], ["t-plakate", "t-technik"])

    def test_prompt_numbers_open_tasks_with_their_global_position(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("[1] Programm festlegen", prompt)
        # Position 2 belongs to the done task, which is never listed — the
        # gap is deliberate, it keeps the numbering stable across toggles.
        self.assertIn("[3] Plakate drucken", prompt)
        self.assertIn("[4] Technik prüfen", prompt)

    def test_done_tasks_are_not_listed_in_the_prompt(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertNotIn("Ensemble anfragen", prompt)

    def test_multi_mode_states_each_projects_ref(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("Projekt-Nr.: 1", prompt)
        self.assertIn("Projekt-Nr.: 2", prompt)
        self.assertIn('"project_ref"', prompt)

    def test_single_project_demo_mode_has_no_project_refs(self):
        prompt = build_prompt(
            _summary_projects()[:1], date(2026, 9, 1), single_project_demo=True
        )
        self.assertNotIn("Projekt-Nr.", prompt)
        self.assertNotIn('"project_ref"', prompt)
        self.assertIn('"heading"', prompt)

    def test_prompt_asks_for_json_only(self):
        prompt = build_prompt(_summary_projects(), date(2026, 9, 1))
        self.assertIn("NUR mit JSON", prompt)
        self.assertIn('"jetzt_faellig"', prompt)
        self.assertIn('"naechste_woche"', prompt)


class ResolveWeeklySummaryTest(SimpleTestCase):
    """#122: resolve_weekly_summary turns Claude's raw reference dict into
    render-ready sections against *live* projects — the raw dict is what
    every cache layer stores, so done-state must come from projects at
    render time, never from the cached artifact."""

    def resolve(self, data, projects=None, **kwargs):
        return resolve_weekly_summary(
            data, projects if projects is not None else _summary_projects(), **kwargs
        )

    def test_valid_refs_resolve_to_projects_and_tasks(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {
                        "project_ref": 1,
                        "assessment": "Programm ist der Engpass",
                        "task_refs": [1],
                    }
                ],
                "naechste_woche": [
                    {
                        "project_ref": 2,
                        "assessment": "noch gut im Zeitplan",
                        "task_refs": [3, 4],
                    }
                ],
            }
        )
        self.assertEqual(sections[0]["title"], "Jetzt fällig")
        self.assertEqual(sections[1]["title"], "Nächste Woche")
        [block] = sections[0]["blocks"]
        self.assertEqual(block["project_id"], "p-alpha")
        self.assertEqual(block["project_name"], "Konzert Alpha")
        self.assertEqual(block["assessment"], "Programm ist der Engpass")
        self.assertEqual([t["id"] for t in block["tasks"]], ["t-programm"])
        [block2] = sections[1]["blocks"]
        self.assertEqual([t["id"] for t in block2["tasks"]], ["t-plakate", "t-technik"])

    def test_the_projection_carries_the_tasks_due_date(self):
        # #190: the summary listed a name and a status dot but no date,
        # which is exactly the moment a date is worth most — a task the
        # summary calls urgent says nothing about when it is actually due.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["due"], date(2026, 9, 2))

    def test_the_projection_carries_the_raw_date_not_a_formatted_one(self):
        # #189: a formatted string here would reintroduce the same mistake
        # on a second projection — the templates format at render time.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertNotIn("due_display", task)

    def test_a_task_without_a_date_projects_none_rather_than_dropping_out(self):
        # An undated task still belongs in the summary; only its date is
        # missing, and the template hides the empty span for that case.
        projects = _summary_projects()
        projects[0]["tasks"][0]["due"] = None
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            },
            projects=projects,
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["id"], "t-programm")
        self.assertIsNone(task["due"])

    def test_an_invalid_task_ref_is_dropped_and_the_rest_survive(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [1, 99, 3]}
                ],
                "naechste_woche": [],
            }
        )
        [block] = sections[0]["blocks"]
        self.assertEqual([t["id"] for t in block["tasks"]], ["t-programm", "t-plakate"])

    def test_an_invalid_project_ref_drops_the_whole_block(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 99, "assessment": "x", "task_refs": [1]},
                    {"project_ref": 2, "assessment": "y", "task_refs": []},
                ],
                "naechste_woche": [],
            }
        )
        self.assertEqual(len(sections[0]["blocks"]), 1)
        self.assertEqual(sections[0]["blocks"][0]["project_id"], "p-beta")

    def test_a_bool_ref_does_not_resolve_as_an_integer(self):
        # True is an int subclass — without the explicit check it would
        # resolve as ref 1 and silently attach the wrong task.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [True]}
                ],
                "naechste_woche": [],
            }
        )
        self.assertEqual(sections[0]["blocks"][0]["tasks"], [])

    def test_single_project_demo_uses_the_free_text_heading(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"heading": "Jetzt kritisch", "assessment": "x", "task_refs": [1]}
                ],
                "naechste_woche": [],
            },
            single_project_demo=True,
        )
        [block] = sections[0]["blocks"]
        self.assertEqual(block["heading"], "Jetzt kritisch")
        self.assertNotIn("project_id", block)

    def test_single_project_demo_drops_a_block_without_heading(self):
        sections = self.resolve(
            {
                "jetzt_faellig": [{"assessment": "x", "task_refs": [1]}],
                "naechste_woche": [],
            },
            single_project_demo=True,
        )
        self.assertEqual(sections[0]["blocks"], [])

    def test_a_done_task_resolves_as_done_regardless_of_the_cached_refs(self):
        # The core regression case: the raw dict was cached while the task
        # was open; by render time it is done in projects — the checkbox
        # must render done.
        sections = self.resolve(
            {
                "jetzt_faellig": [
                    {"project_ref": 1, "assessment": "x", "task_refs": [2]}
                ],
                "naechste_woche": [],
            }
        )
        [task] = sections[0]["blocks"][0]["tasks"]
        self.assertEqual(task["id"], "t-ensemble")
        self.assertTrue(task["done"])

    def test_garbage_blocks_and_missing_keys_are_tolerated(self):
        sections = self.resolve(
            {
                "jetzt_faellig": ["kein dict", 42, {"project_ref": 1}],
                "naechste_woche": "gar keine Liste",
            }
        )
        [block] = sections[0]["blocks"]
        self.assertEqual(block["assessment"], "")
        self.assertEqual(block["tasks"], [])
        self.assertEqual(sections[1]["blocks"], [])


class GenerateWeeklySummaryRetryTest(SimpleTestCase):
    """#122: generate_weekly_summary parses Claude's response as JSON with
    the same retry contract as generate_plan (GeneratePlanRetryTest): one
    re-ask on unparseable or wrong-shape JSON, AIUnavailableError after the
    second bad response, SDK failures never spent as a JSON retry."""

    def generate(self):
        return generate_weekly_summary(_summary_projects(), date(2026, 9, 1))

    def test_returns_parsed_dict_on_first_valid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(VALID_SUMMARY_JSON)
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        self.assertEqual(stream.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream(VALID_SUMMARY_JSON),
            ]
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})
        self.assertEqual(stream.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream("immer noch kein json"),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_valid_json_in_the_wrong_shape_is_retried(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream('["nur", "eine", "liste"]'),
                _fake_stream('{"jetzt_faellig": []}'),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(f"```json\n{VALID_SUMMARY_JSON}\n```")
            data = self.generate()
        self.assertEqual(data, {"jetzt_faellig": [], "naechste_woche": []})


VALID_CLOSEOUT_JSON = '{"summary_text": "Gute Woche gewesen."}'


def _closeout_stats():
    return {"completed_count": 3, "rescheduled_count": 1, "added_count": 2}


class GenerateCloseOutSummaryTest(SimpleTestCase):
    """#169: generate_closeout_summary parses Claude's response with the
    same retry contract as generate_weekly_summary (GenerateWeeklySummaryRetryTest):
    one re-ask on unparseable or wrong-shape JSON, AIUnavailableError after
    the second bad response, SDK failures never spent as a JSON retry."""

    def generate(self):
        return generate_closeout_summary(_closeout_stats(), date(2026, 9, 1))

    def test_returns_the_summary_text_on_first_valid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(VALID_CLOSEOUT_JSON)
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")
        self.assertEqual(stream.call_count, 1)

    def test_retries_once_on_invalid_json_then_succeeds(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream(VALID_CLOSEOUT_JSON),
            ]
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")
        self.assertEqual(stream.call_count, 2)

    def test_raises_ai_unavailable_after_a_second_invalid_response(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream("kein json"),
                _fake_stream("immer noch kein json"),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_valid_json_in_the_wrong_shape_is_retried(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = [
                _fake_stream('{"other_key": "x"}'),
                _fake_stream('{"summary_text": 5}'),
            ]
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 2)

    def test_an_sdk_failure_is_not_retried_as_a_json_error(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.side_effect = _anthropic_timeout_error()
            with self.assertRaises(AIUnavailableError):
                self.generate()
        self.assertEqual(stream.call_count, 1)

    def test_fenced_response_is_still_parsed(self):
        with patch("anthropic.Anthropic") as MockAnthropic:
            stream = MockAnthropic.return_value.messages.stream
            stream.return_value = _fake_stream(f"```json\n{VALID_CLOSEOUT_JSON}\n```")
            text = self.generate()
        self.assertEqual(text, "Gute Woche gewesen.")

    def test_prompt_names_the_three_stats_and_asks_for_json(self):
        prompt = build_closeout_prompt(_closeout_stats(), date(2026, 9, 1))
        self.assertIn("Erledigt: 3 Aufgaben", prompt)
        self.assertIn("Verschoben in die nächste Woche: 1 Aufgaben", prompt)
        self.assertIn("Neu dazugekommen: 2 Aufgaben", prompt)
        self.assertIn("summary_text", prompt)


class AiSummaryCheckboxViewTest(DemoModeTestCase):
    """#122 end to end: the rendered summary carries real inline checkboxes
    wired to the existing toggle endpoints, and their done state is read
    from live data at render time, not from the cached Claude response."""

    def summary_stub(self):
        return self.ai_mocks["projects.views.generate_weekly_summary"]

    def single_project_summary(self, task_refs):
        return {
            "jetzt_faellig": [
                {
                    "heading": "Jetzt kritisch",
                    "assessment": "Programm zuerst",
                    "task_refs": task_refs,
                }
            ],
            "naechste_woche": [],
        }

    def test_dashboard_summary_renders_a_checkbox_for_a_referenced_task(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([1])
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Jetzt kritisch")
        self.assertContains(response, "Programm zuerst")
        # Once in the AI card, once in the project section's task list —
        # both are the same .toggle-form markup on the same endpoint.
        self.assertContains(
            response,
            'class="toggle-form" data-task-id="demo-session-0"',
            count=2,
        )

    def test_dashboard_checkbox_state_follows_a_toggle_not_the_cache(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([1])
        self.client.get(reverse("dashboard"))  # caches the raw refs in the session
        self.client.post(
            reverse("toggle_task", args=["demo-session-0"]),
            json.dumps({"done": True}),
            content_type="application/json",
        )
        response = self.client.get(reverse("dashboard"))
        # The cached refs were written while the task was open; the rendered
        # checkbox must still show the live done state — in the AI card and
        # the task list alike.
        self.assertContains(
            response,
            'data-task-id="demo-session-0" data-done="true"',
            count=2,
        )
        self.summary_stub().assert_called_once()

    def test_an_unresolvable_ref_does_not_break_the_page(self):
        self.given_session_plan()
        self.summary_stub().return_value = self.single_project_summary([99])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jetzt kritisch")

    def test_multi_project_summary_links_the_project_by_id(self):
        # Demo multi mode (no session plan): headings come from project_ref,
        # resolved server-side — no PROJECT_MAP substring matching anywhere.
        self.summary_stub().return_value = {
            "jetzt_faellig": [
                {"project_ref": 1, "assessment": "läuft", "task_refs": []}
            ],
            "naechste_woche": [],
        }
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "showProject('demo-1')")
        self.assertContains(response, 'class="ai-project-link"')
        self.assertNotContains(response, "PROJECT_MAP")

    def test_my_plan_summary_renders_the_same_live_checkbox(self):
        plan = self.given_session_plan()
        plan["tasks"][0]["done"] = True
        session = self.client.session
        session["demo_plan"] = plan
        # Raw refs cached while the task was still open — done must come
        # from the live session plan.
        session[f"{SUMMARY_KEY}_today"] = self.single_project_summary([1])
        session.save()
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "Jetzt kritisch")
        # The summary checkbox renders the live done state (the task list
        # row formats its attributes across lines, so this single-line
        # pattern matches the summary markup).
        self.assertContains(response, 'data-task-id="demo-session-0" data-done="true"')


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
        instance.pages.retrieve.side_effect = exc
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

    def test_increment_postpone_count_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            self._stub_every_call(MockClient, RequestTimeoutError())
            with self.assertRaises(NotionUnavailableError):
                increment_postpone_count("task-id")


class PostponeCountReadFromNotionTest(SimpleTestCase):
    """#171: _get_tasks reads the "Verschoben" number property fresh on
    every fetch, or a task's count would reset to 0 on display even though
    the stored value is correct."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_verschoben_number_property(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [
                    {
                        "id": "task-1",
                        "created_time": "2026-08-01T10:00:00.000Z",
                        "properties": {
                            "Aufgabe": {"title": [{"plain_text": "Test"}]},
                            "Wann?": {"date": {"start": "2026-08-20"}},
                            "Done": {"checkbox": False},
                            "Kontext": {"multi_select": []},
                            "Verschoben": {"number": 3},
                        },
                    }
                ]
            }
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["postpone_count"], 3)
        self.assertEqual(tasks[0]["created_time"], date(2026, 8, 1))

    def test_missing_property_defaults_to_zero(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Test", "2026-08-20")]
            }
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["postpone_count"], 0)
        self.assertIsNone(tasks[0]["created_time"])


class CompletedDateReadFromNotionTest(SimpleTestCase):
    """#19: _get_tasks reads the "Erledigt am" date property Notion gets
    alongside Done — a manually-added property, so a page fetched before it
    existed in the schema must not KeyError."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_erledigt_am_date(self):
        with patch("projects.notion.Client") as MockClient:
            page = _fake_task_page("Test", "2026-08-20")
            page["properties"]["Erledigt am"] = {"date": {"start": "2026-08-22"}}
            page["properties"]["Done"] = {"checkbox": True}
            MockClient.return_value.databases.query.return_value = {"results": [page]}
            tasks = _get_tasks("project-id")
        self.assertEqual(tasks[0]["completed_date"], date(2026, 8, 22))

    def test_missing_property_is_none(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Test", "2026-08-20")]
            }
            tasks = _get_tasks("project-id")
        self.assertIsNone(tasks[0]["completed_date"])


class ToggleTaskWritesCompletedDateTest(SimpleTestCase):
    """#19: toggle_task writes Done and Erledigt am in one pages.update call —
    they change together, and there's no read-then-write race to guard
    against here (unlike increment_postpone_count below)."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marking_done_writes_both_properties_together(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            toggle_task("task-1", True, "2026-08-22")
        instance.pages.update.assert_called_once_with(
            page_id="task-1",
            properties={
                "Done": {"checkbox": True},
                "Erledigt am": {"date": {"start": "2026-08-22"}},
            },
        )

    def test_unmarking_clears_the_completed_date(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            toggle_task("task-1", False, None)
        instance.pages.update.assert_called_once_with(
            page_id="task-1",
            properties={"Done": {"checkbox": False}, "Erledigt am": {"date": None}},
        )


class GetUnassignedTasksTest(SimpleTestCase):
    """#53: get_upcoming_projects only ever queries TASKS_DB per project via
    a relation.contains filter — a task with an empty "Related to Projekte"
    relation is never picked up by any existing read path. get_unassigned_tasks
    is the deliberate second read path for that "Kleinkram" residue."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_queries_tasks_db_with_an_is_empty_relation_filter(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.databases.query.return_value = {"results": []}
            get_unassigned_tasks(date(2026, 8, 31))
        instance.databases.query.assert_called_once_with(
            database_id=TASKS_DB,
            filter={
                "property": "Related to Projekte",
                "relation": {"is_empty": True},
            },
        )

    def test_returns_tasks_with_no_project_relation(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.return_value = {
                "results": [_fake_task_page("Blumen besorgen", "2026-09-01")]
            }
            tasks = get_unassigned_tasks(date(2026, 8, 31))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "Blumen besorgen")
        self.assertEqual(tasks[0]["due"], date(2026, 9, 1))

    def test_translates_a_failure(self):
        with patch("projects.notion.Client") as MockClient:
            MockClient.return_value.databases.query.side_effect = RequestTimeoutError()
            with self.assertRaises(NotionUnavailableError):
                get_unassigned_tasks(date(2026, 8, 31))


class IncrementPostponeCountTest(SimpleTestCase):
    """#171: read-then-write, since Notion has no atomic increment."""

    def setUp(self):
        patcher = patch.dict(os.environ, {"NOTION_API_KEY": "testkey"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_then_writes_the_incremented_value(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.retrieve.return_value = {
                "properties": {"Verschoben": {"number": 2}}
            }
            result = increment_postpone_count("task-1")
        self.assertEqual(result, 3)
        instance.pages.update.assert_called_once_with(
            page_id="task-1", properties={"Verschoben": {"number": 3}}
        )

    def test_missing_property_starts_at_one(self):
        with patch("projects.notion.Client") as MockClient:
            instance = MockClient.return_value
            instance.pages.retrieve.return_value = {"properties": {}}
            result = increment_postpone_count("task-1")
        self.assertEqual(result, 1)


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
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
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

    def test_sim_date_awaits_preload_before_reloading(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, "if (dateStr) {\n        await preloadOne(dateStr);\n    }"
        )

    def test_preload_one_dedupes_concurrent_calls_for_the_same_date(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "const preloadPromises = new Map();")
        self.assertContains(
            response,
            "if (preloadPromises.has(dateStr)) return preloadPromises.get(dateStr);",
        )

    def test_preload_one_checks_json_ok_not_just_http_status(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "if (!data.ok) return;")

    def test_session_writing_fetches_are_serialized_through_one_queue(self):
        """#93 follow-up: Django saves the whole session dict per response, so
        concurrent /timelapse/preload/ calls (fired together from
        preloadAll's Promise.all) raced and silently dropped each other's
        cached summary — reproduced by clearing all moment summaries and
        observing only some survive a fresh preloadAll(). withSessionLock
        forces every session-writing fetch onto one queue."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "let sessionWriteQueue = Promise.resolve();")
        self.assertContains(response, "withSessionLock(() => fetch('/timelapse/', {")
        self.assertContains(response, "const promise = withSessionLock(async () => {")

    def test_moment_tiles_stretch_to_fill_the_row(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, ".moment-btn { flex: 1;")
        self.assertContains(response, ".moment-btn-today { flex: 1;")

    def test_reload_fades_out_before_reloading(self):
        """A cross-document view transition would be the native fix, but
        Chrome doesn't grant one to a same-URL reload — verified by listening
        for `pagereveal`'s `viewTransition` around a real setSimDate() click,
        which came back null. This CSS fade is the fallback."""
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "document.body.style.opacity = '0';")

    def test_loading_spinner_lives_next_to_the_heading_not_inside_each_tile(self):
        """Toggling a spinner's display inside a moment-btn changed that
        tile's height, so every click reflowed the whole row — one shared
        spinner next to the "Zeitreise" label avoids that; the tiles
        themselves now only ever change color, never size."""
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            '<div class="timelapse-label">Zeitreise<span class="timelapse-spinner"></span></div>',
        )
        self.assertContains(
            response, ".timelapse-bar.loading .timelapse-spinner { display: block; }"
        )
        self.assertNotContains(response, '<div class="spinner"></div>')


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


class MyPlanDoneCounterTest(DemoModeTestCase):
    """#151: the "x / y erledigt" counter was server-rendered only, so after a
    toggle the progress bar moved while the counter kept its load-time value
    until the next reload. updateProgress() now rewrites the counter alongside
    the bar, which requires the count to sit in an addressable element."""

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

    def test_done_count_renders_inside_an_addressable_element(self):
        self.given_plan_with(total=4, done=2)
        self.assertContains(
            self.client.get(reverse("my_plan")),
            '<span id="done-count">2</span> / 4 erledigt',
        )

    def test_update_progress_rewrites_the_counter(self):
        self.given_plan_with(total=4, done=1)
        response = self.client.get(reverse("my_plan"))
        self.assertContains(response, "getElementById('done-count')")


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
        # All three handlers — the toggle listener, reschedule(), and #180's
        # day-column drag handler — carry the widened guard; their error
        # paths (flash / return false / revert the drag) stay.
        self.assertContains(response, self.GUARD, count=3)
        self.assertContains(response, "flashActionFailed(dueSpan);")


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


class RuleReorderNonListOrderTest(DemoModeTestCase):
    """#167: a non-list "order" value is a 400, never a 500 or a silent
    reorder. An int or null crashed reorder_rules with a TypeError; a
    string was iterated character by character."""

    def post_order(self, order):
        return self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": order}),
            content_type="application/json",
        )

    def test_a_non_list_order_is_a_400(self):
        for payload in (5, "12", None):
            with self.subTest(order=payload):
                response = self.post_order(payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())


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
    # task; a cached summary's task_refs would keep pointing at the old
    # positions. A reschedule therefore sweeps the session summaries the
    # way planner_create does. ---

    def given_cached_summaries(self):
        """A current-version summary, a preloaded sim-date one, and an
        old-version leftover — the unversioned-prefix sweep clears all three."""
        session = self.client.session
        session[f"{SUMMARY_KEY}_today"] = {"summary": "alt"}
        session[f"{SUMMARY_KEY}_2026-09-01"] = {"summary": "alt"}
        session["demo_plan_summary_v1_today"] = {"summary": "uralt"}
        session.save()

    def test_a_reschedule_clears_every_cached_summary(self):
        self.given_session_plan()
        self.given_cached_summaries()
        response = self.post_date("demo-session-0", f'{{"date": "{self.NEW_DATE}"}}')
        self.assertEqual(response.status_code, 200)
        # list(...keys()): SessionBase is not a dict and not iterable itself,
        # so SIM118's bare-iteration fix does not apply (cf. planner_views).
        leftovers = [
            k
            for k in list(self.client.session.keys())
            if k.startswith("demo_plan_summary")
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
        self.assertIn(
            "await reschedule(taskId, TODAY, dueSpan, btn.closest('.task-row'));", html
        )


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
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "postpone_count": 1,
                "due_display": format_date(date.fromisoformat(new_date)),
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


class WeekCloseoutModelTest(TestCase):
    def test_unique_constraint_on_iso_year_and_week(self):
        WeekCloseout.objects.create(iso_year=2026, iso_week=25)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WeekCloseout.objects.create(iso_year=2026, iso_week=25)

    def test_str_shows_the_iso_week(self):
        closeout = WeekCloseout.objects.create(iso_year=2026, iso_week=25)
        self.assertEqual(str(closeout), "KW25/2026")


class CloseoutBackendTest(TestCase):
    """closeout.py direct: two backends behind one interface, the same shape
    as rules.py — the views never learn which backend answered."""

    def request(self):
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    @override_settings(DEMO_MODE=False)
    def test_production_round_trip(self):
        request = self.request()
        self.assertFalse(is_week_closed(request, 2026, 25))
        save_closeout(request, 2026, 25, _closeout_stats(), "Text.")
        self.assertTrue(is_week_closed(request, 2026, 25))
        self.assertEqual(get_latest_closeout(request)["summary_text"], "Text.")

    @override_settings(DEMO_MODE=True)
    def test_demo_round_trip(self):
        request = self.request()
        self.assertFalse(is_week_closed(request, 2026, 25))
        save_closeout(request, 2026, 25, _closeout_stats(), "Text.")
        self.assertTrue(is_week_closed(request, 2026, 25))
        self.assertEqual(get_latest_closeout(request)["summary_text"], "Text.")

    @override_settings(DEMO_MODE=True)
    def test_no_closeout_yet_is_none(self):
        self.assertIsNone(get_latest_closeout(self.request()))

    @override_settings(DEMO_MODE=False)
    def test_production_no_closeout_yet_is_none(self):
        self.assertIsNone(get_latest_closeout(self.request()))


def _closeout_tasks(fixed_today):
    return [
        {
            "id": "t-this-week",
            "name": "Diese Woche",
            "date": (fixed_today + timedelta(days=2)).isoformat(),
            "done": False,
        },
        {
            "id": "t-next-week",
            "name": "Nächste Woche",
            "date": (fixed_today + timedelta(days=9)).isoformat(),
            "done": False,
        },
        {
            "id": "t-overdue",
            "name": "Überfällig",
            "date": (fixed_today - timedelta(days=1)).isoformat(),
            "done": False,
        },
        {
            "id": "t-done",
            "name": "Schon erledigt",
            "date": (fixed_today + timedelta(days=1)).isoformat(),
            "done": True,
        },
    ]


# A Monday — matches AnnotateTasksTest/IsSameIsoWeekTest's reference date.
CLOSEOUT_TODAY = date(2026, 6, 15)


class CloseWeekStartDemoModeTest(DemoModeTestCase):
    """#169: the triage list is only open tasks due in the current ISO
    week — overdue tasks stay out (their own signal already), and so do
    tasks already done or due a different week."""

    @patch("django.utils.timezone.localdate")
    def test_lists_only_open_tasks_due_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche")
        self.assertNotContains(response, "Nächste Woche")
        self.assertNotContains(response, "Überfällig")
        self.assertNotContains(response, "Schon erledigt")

    def test_no_session_plan_redirects_to_index(self):
        response = self.client.get(reverse("close_week_start"))
        self.assertRedirects(response, reverse("index"))

    @patch("django.utils.timezone.localdate")
    def test_nothing_open_this_week_is_an_empty_state(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY  # a Monday
        self.given_session_plan(tasks=[])
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(
            response, "Für diese Woche ist alles erledigt oder verschoben."
        )

    @patch("django.utils.timezone.localdate")
    def test_the_empty_state_greets_the_weekend_on_a_weekend(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY + timedelta(days=5)  # Saturday
        self.given_session_plan(tasks=[])
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Genieße dein Wochenende")

    @patch("django.utils.timezone.localdate")
    def test_the_move_button_shows_the_target_date_not_a_generic_label(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        # "Diese Woche" +2 days is due 2026-06-17, +7 = 2026-06-24, a Wednesday.
        self.assertContains(response, "→ Mi, 24. Juni")
        self.assertNotContains(response, "→ nächste Woche")

    @patch("django.utils.timezone.localdate")
    def test_the_triage_row_shows_the_tasks_own_due_date(self, mock_localdate):
        # #189: this date used to be a string the view precomputed; the
        # template formats task.due itself now, and nothing else in the
        # suite pins what the triage row renders.
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=_closeout_tasks(CLOSEOUT_TODAY))
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, format_date(CLOSEOUT_TODAY + timedelta(days=2)))

    @patch("django.utils.timezone.localdate")
    def test_already_closed_and_nothing_open_hides_the_button(self, mock_localdate):
        # Re-confirming an already-closed week with an empty triage list
        # would post an empty task_id list and overwrite the real
        # completed/rescheduled counts with zeros — the button has to go,
        # not just the copy above it.
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=[])
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 3,
            "rescheduled_count": 1,
            "added_count": 0,
            "summary_text": "Text.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche hast du bereits abgeschlossen.")
        self.assertContains(response, "Rückblick ansehen")
        self.assertNotContains(response, "Woche abschließen</button>")
        # The subtitle used to say "noch offene Aufgaben dieser Woche" even
        # here — the same contradiction as the empty-state text, one line up.
        self.assertContains(response, "bereits abgeschlossen</div>")
        self.assertNotContains(response, "noch offene Aufgaben dieser Woche")


@override_settings(DEMO_MODE=False)
class CloseWeekStartProductionTest(TestCase):
    def _project(self, tasks):
        return [
            {
                "id": "p1",
                "name": "Projekt",
                "event_date": CLOSEOUT_TODAY + timedelta(days=30),
                "event_date_uncertain": False,
                "performers": "",
                "status": None,
                "status_color": "gray",
                "tasks": tasks,
            }
        ]

    def _task(self, task_id, name, due, done=False):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": None,
        }

    @patch("django.utils.timezone.localdate")
    def test_lists_only_open_tasks_due_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        tasks = [
            self._task(
                "t-this-week", "Diese Woche", CLOSEOUT_TODAY + timedelta(days=2)
            ),
            self._task(
                "t-next-week", "Nächste Woche", CLOSEOUT_TODAY + timedelta(days=9)
            ),
        ]
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project(tasks)
        ):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche")
        self.assertNotContains(response, "Nächste Woche")

    def test_notion_failure_redirects_to_dashboard(self):
        with patch(
            "projects.views.get_upcoming_projects",
            side_effect=NotionUnavailableError("boom"),
        ):
            response = self.client.get(reverse("close_week_start"))
        # fetch_redirect_response=False: dashboard()'s own Notion-failure
        # behavior has its own tests (DashboardNotionFailureTest); the mock
        # above is out of scope by the time a live follow-up GET would run.
        self.assertRedirects(
            response, reverse("dashboard"), fetch_redirect_response=False
        )

    @patch("django.utils.timezone.localdate")
    def test_already_closed_and_nothing_open_hides_the_button(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        WeekCloseout.objects.create(
            iso_year=2026, iso_week=25, completed_count=3, rescheduled_count=1
        )
        with patch(
            "projects.views.get_upcoming_projects", return_value=self._project([])
        ):
            response = self.client.get(reverse("close_week_start"))
        self.assertContains(response, "Diese Woche hast du bereits abgeschlossen.")
        self.assertNotContains(response, "Woche abschließen</button>")
        self.assertContains(response, "bereits abgeschlossen</div>")
        self.assertNotContains(response, "noch offene Aufgaben dieser Woche")


class CloseWeekConfirmDemoModeTest(DemoModeTestCase):
    """#169: stats are computed by diffing the posted task_id list against
    live state — completed if now done, rescheduled if no longer due this
    same ISO week."""

    @patch("django.utils.timezone.localdate")
    def test_completed_and_rescheduled_and_unchanged(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(
            tasks=[
                {
                    "id": "t-done",
                    "name": "Erledigt",
                    "date": (CLOSEOUT_TODAY + timedelta(days=1)).isoformat(),
                    "done": True,
                },
                {
                    "id": "t-moved",
                    "name": "Verschoben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=9)).isoformat(),
                    "done": False,
                },
                {
                    "id": "t-stayed",
                    "name": "Geblieben",
                    "date": (CLOSEOUT_TODAY + timedelta(days=2)).isoformat(),
                    "done": False,
                },
            ]
        )
        response = self.client.post(
            reverse("close_week_confirm"),
            data={"task_id": ["t-done", "t-moved", "t-stayed"]},
        )
        self.assertRedirects(response, reverse("week_review"))
        closeout = self.client.session["demo_week_closeout"]
        self.assertEqual(closeout["completed_count"], 1)
        self.assertEqual(closeout["rescheduled_count"], 1)
        self.assertEqual(closeout["added_count"], 0)
        self.assertEqual(closeout["summary_text"], "Gute Woche gewesen.")

    def test_no_session_plan_redirects_to_index(self):
        response = self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertRedirects(response, reverse("index"))

    def test_get_redirects_to_start(self):
        self.given_session_plan()
        response = self.client.get(reverse("close_week_confirm"))
        self.assertRedirects(response, reverse("close_week_start"))

    @patch("django.utils.timezone.localdate")
    def test_ai_failure_still_saves_the_closeout_with_an_empty_summary(
        self, mock_localdate
    ):
        mock_localdate.return_value = CLOSEOUT_TODAY
        self.given_session_plan(tasks=[])
        self.ai_mocks[
            "projects.views.generate_closeout_summary"
        ].side_effect = AIUnavailableError("boom")
        response = self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertRedirects(response, reverse("week_review"))
        self.assertEqual(self.client.session["demo_week_closeout"]["summary_text"], "")


@override_settings(DEMO_MODE=False)
class CloseWeekConfirmProductionTest(TestCase):
    def _task(self, task_id, name, due, done=False, created_time=None):
        return {
            "id": task_id,
            "name": name,
            "due": due,
            "done": done,
            "kontext": [],
            "postpone_count": 0,
            "created_time": created_time,
        }

    def _project(self, tasks):
        return [
            {
                "id": "p1",
                "name": "Projekt",
                "event_date": CLOSEOUT_TODAY + timedelta(days=30),
                "event_date_uncertain": False,
                "performers": "",
                "status": None,
                "status_color": "gray",
                "tasks": tasks,
            }
        ]

    @patch("django.utils.timezone.localdate")
    def test_added_count_comes_from_created_time_this_week(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        tasks = [
            self._task(
                "t-done",
                "Erledigt",
                CLOSEOUT_TODAY,
                done=True,
                created_time=date(2026, 5, 1),
            ),
            self._task(
                "t-new",
                "Neu",
                CLOSEOUT_TODAY + timedelta(days=1),
                created_time=CLOSEOUT_TODAY,
            ),
        ]
        with (
            patch(
                "projects.views.get_upcoming_projects",
                return_value=self._project(tasks),
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Rückschau."
            ),
        ):
            response = self.client.post(
                reverse("close_week_confirm"), data={"task_id": ["t-done"]}
            )
        # fetch_redirect_response=False: week_review() builds the sidebar
        # project list (#185) and would fetch Notion on the follow-up GET,
        # outside the patch above. What it renders has its own test.
        self.assertRedirects(
            response, reverse("week_review"), fetch_redirect_response=False
        )
        closeout = WeekCloseout.objects.get()
        self.assertEqual(closeout.completed_count, 1)
        self.assertEqual(closeout.added_count, 1)
        self.assertEqual(closeout.summary_text, "Rückschau.")

    @patch("django.utils.timezone.localdate")
    def test_reclosing_the_same_week_updates_not_duplicates(self, mock_localdate):
        mock_localdate.return_value = CLOSEOUT_TODAY
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Erster Text."
            ),
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        with (
            patch(
                "projects.views.get_upcoming_projects", return_value=self._project([])
            ),
            patch(
                "projects.views.generate_closeout_summary", return_value="Zweiter Text."
            ),
        ):
            self.client.post(reverse("close_week_confirm"), data={"task_id": []})
        self.assertEqual(WeekCloseout.objects.count(), 1)
        self.assertEqual(WeekCloseout.objects.get().summary_text, "Zweiter Text.")


@override_settings(DEMO_MODE=False)
class WeekReviewProductionTest(TestCase):
    def test_no_closeout_redirects_to_start(self):
        response = self.client.get(reverse("week_review"))
        # fetch_redirect_response=False: close_week_start's own Notion call
        # has its own tests (CloseWeekStartProductionTest); nothing here
        # mocks it, so a live follow-up GET would hit the network.
        self.assertRedirects(
            response, reverse("close_week_start"), fetch_redirect_response=False
        )

    def test_renders_the_latest_closeout(self):
        WeekCloseout.objects.create(
            iso_year=2026,
            iso_week=25,
            completed_count=3,
            rescheduled_count=1,
            added_count=2,
            summary_text="Gute Woche.",
        )
        # The sidebar project list (#185) makes this view fetch Notion on a
        # cold cache — stubbed here so the assertions below stay about the
        # closeout, not about project data this test never sets up.
        with patch("projects.views.get_upcoming_projects", return_value=[]):
            response = self.client.get(reverse("week_review"))
        self.assertContains(response, "Gute Woche.")
        self.assertContains(response, "KW 25/2026")


class WeekReviewDemoModeTest(DemoModeTestCase):
    def test_no_session_plan_redirects_to_index(self):
        response = self.client.get(reverse("week_review"))
        self.assertRedirects(response, reverse("index"))

    def test_no_closeout_yet_redirects_to_start(self):
        self.given_session_plan()
        response = self.client.get(reverse("week_review"))
        self.assertRedirects(response, reverse("close_week_start"))

    def test_renders_the_latest_closeout_from_the_session(self):
        self.given_session_plan()
        session = self.client.session
        session["demo_week_closeout"] = {
            "iso_year": 2026,
            "iso_week": 25,
            "completed_count": 2,
            "rescheduled_count": 1,
            "added_count": 0,
            "summary_text": "Solide Woche.",
            "closed_at": "2026-06-15T12:00:00",
        }
        session.save()
        response = self.client.get(reverse("week_review"))
        self.assertContains(response, "Solide Woche.")


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
            patch("projects.views.get_unassigned_tasks", return_value=[]),
            patch(
                "projects.views.generate_weekly_summary",
                return_value=_summary_data(),
            ),
        ):
            response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'title="Datum ändern"')
        self.assertContains(response, 'data-task-id="task-1"')


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


class TimelapseBarSharedAcrossViewsTest(DemoModeTestCase):
    """Same reasoning as StatusBannersSharedAcrossViewsTest, for the
    Zeitreise bar: it only ever rendered in view-overview, so switching to
    "Heute" lost the ability to jump between simulated moments — the visitor
    had to flip back to "Dashboard" just to change the simulated date."""

    def test_timelapse_bar_appears_for_both_views(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'class="timelapse-bar"', count=2)
        self.assertContains(response, 'class="timelapse-moments"', count=2)

    def test_absent_without_a_session_plan(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="timelapse-bar"')


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
    live in request.session — and, per #105, start empty rather than seeded
    from INITIAL_RULES, so a visitor's example plan isn't built from the
    maintainer's concert-specific production rules."""

    def request_with_session(self):
        """A request carrying this client's session, as the planner views see it."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    def add_rule_id(self, text, project_types=None):
        """Adds a demo rule via the view (so CSRF/session wiring matches a real
        request) and returns the id the session assigned it."""
        self.client.post(
            reverse("rule_add"),
            data={"text": text, "project_types": project_types or []},
        )
        stored = self.client.session[DEMO_RULES_KEY]
        return next(r["id"] for r in stored if r["text"] == text)

    def test_a_fresh_session_starts_with_no_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")
        for rule in INITIAL_RULES:
            self.assertNotContains(response, rule["text"])

    def test_reading_the_rules_page_persists_no_session(self):
        """The demo is public and not yet behind a robots.txt (#27), so a GET
        must not leave a session row behind for every visitor and crawler."""
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "Noch keine Regeln")
        self.assertEqual(Session.objects.count(), 0)

    def test_the_first_write_persists_an_added_rule(self):
        self.client.post(reverse("rule_add"), data={"text": "Erste Regel"})
        self.assertEqual(Session.objects.count(), 1)
        stored = self.client.session[DEMO_RULES_KEY]
        self.assertEqual([r["text"] for r in stored], ["Erste Regel"])
        self.assertEqual(stored[0]["project_types"], [])
        self.assertTrue(stored[0]["active"])

    def test_adding_a_rule_writes_nothing_to_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        self.assertEqual(PlannerRule.objects.count(), 0)
        self.assertContains(self.client.get(reverse("rules_list")), "Neue Regel")

    def test_adding_a_rule_persists_its_project_types(self):
        self.client.post(
            reverse("rule_add"),
            data={"text": "Neue Regel", "project_types": ["hochzeit", "konzert"]},
        )
        stored = self.client.session[DEMO_RULES_KEY]
        added = next(r for r in stored if r["text"] == "Neue Regel")
        self.assertEqual(added["project_types"], ["hochzeit", "konzert"])

    def test_add_rejects_malformed_project_types(self):
        request = self.request_with_session()
        add_rule(request, "Direkt aufgerufen", project_types="konzert")
        stored = request.session[DEMO_RULES_KEY]
        added = next(r for r in stored if r["text"] == "Direkt aufgerufen")
        self.assertEqual(added["project_types"], [])

    def test_toggle_update_delete_and_reorder_write_nothing_to_the_database(self):
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        id_c = self.add_rule_id("Regel C")
        self.client.post(reverse("rule_toggle", args=[id_a]))
        self.client.post(
            reverse("rule_update", args=[id_b]),
            data=json.dumps({"text": "Geänderte Regel"}),
            content_type="application/json",
        )
        self.client.post(reverse("rule_delete", args=[id_c]))
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(id_b), str(id_a)]}),
            content_type="application/json",
        )
        self.assertEqual(PlannerRule.objects.count(), 0)

    def test_one_visitor_cannot_change_what_another_one_sees(self):
        other = Client()
        self.client.post(reverse("rule_add"), data={"text": "Nur für mich"})

        response = other.get(reverse("rules_list"))
        self.assertNotContains(response, "Nur für mich")
        self.assertContains(response, "Noch keine Regeln")

    def test_a_deactivated_rule_stays_listed_but_leaves_the_prompt(self):
        rule_id = self.add_rule_id("GEMA-Meldung einplanen", ["konzert"])
        response = self.client.post(reverse("rule_toggle", args=[rule_id]))
        self.assertEqual(response.json()["active"], False)

        request = self.request_with_session()
        self.assertNotIn(
            "GEMA-Meldung einplanen", get_active_rule_texts(request, "konzert")
        )
        self.assertContains(
            self.client.get(reverse("rules_list")), "GEMA-Meldung einplanen"
        )

    def test_reordering_reaches_the_prompt_in_the_new_order(self):
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": [str(id_b), str(id_a)]}),
            content_type="application/json",
        )
        self.assertEqual(
            get_active_rule_texts(self.request_with_session(), "konzert"),
            ["Regel B", "Regel A"],
        )

    def test_a_string_order_does_not_silently_reorder_the_rules(self):
        """#167: a string like "21" used to be iterated character by
        character, applying a swap the client never validly asked for.
        It is rejected and the stored order stays untouched."""
        id_a = self.add_rule_id("Regel A")
        id_b = self.add_rule_id("Regel B")
        response = self.client.post(
            reverse("rule_reorder"),
            data=json.dumps({"order": f"{id_b}{id_a}"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            get_active_rule_texts(self.request_with_session(), "konzert"),
            ["Regel A", "Regel B"],
        )

    def test_rules_are_filtered_by_project_type(self):
        """#105: a rule tagged with specific project_types only reaches the
        prompt for those types; a rule with no project_types applies to all
        of them."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Immer", [])
        request = self.request_with_session()

        self.assertIn("Nur Konzert", get_active_rule_texts(request, "konzert"))
        self.assertNotIn("Nur Konzert", get_active_rule_texts(request, "hochzeit"))
        self.assertIn("Immer", get_active_rule_texts(request, "konzert"))
        self.assertIn("Immer", get_active_rule_texts(request, "hochzeit"))

    def test_rules_page_only_shows_rules_for_the_current_project_type(self):
        """A visitor planning a Workshop should not see a Konzert-only rule
        left over from an earlier tile in the same session (#105)."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Nur Workshop", ["workshop"])
        self.add_rule_id("Immer", [])

        session = self.client.session
        session["demo_project_type"] = "workshop"
        session.save()

        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, "Nur Konzert")
        self.assertContains(response, "Nur Workshop")
        self.assertContains(response, "Immer")

    def test_rules_page_shows_everything_without_a_current_project_type(self):
        """Reached from outside the planner flow (no tile picked yet), there is
        nothing to scope by, so every rule stays visible."""
        self.add_rule_id("Nur Konzert", ["konzert"])
        self.add_rule_id("Nur Workshop", ["workshop"])

        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "Nur Konzert")
        self.assertContains(response, "Nur Workshop")

    def test_updating_a_rules_project_types_is_reflected_in_the_filter(self):
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"project_types": []}),
            content_type="application/json",
        )
        request = self.request_with_session()
        self.assertIn("Nur Konzert", get_active_rule_texts(request, "hochzeit"))

    def test_omitting_project_types_on_update_leaves_it_unchanged(self):
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"text": "Neuer Text"}),
            content_type="application/json",
        )
        stored = self.client.session[DEMO_RULES_KEY]
        updated = next(r for r in stored if r["id"] == rule_id)
        self.assertEqual(updated["text"], "Neuer Text")
        self.assertEqual(updated["project_types"], ["konzert"])

    def test_malformed_project_types_on_update_leaves_it_unchanged(self):
        """A crafted request straight to the JSON endpoint (the UI never sends
        a non-list) must not be able to write a value into the session that
        would later force a re-seed — same intent as _is_valid()'s self-healing
        on read, applied at the point the bad value would otherwise land."""
        rule_id = self.add_rule_id("Nur Konzert", ["konzert"])
        self.client.post(
            reverse("rule_update", args=[rule_id]),
            data=json.dumps({"project_types": "konzert"}),
            content_type="application/json",
        )
        stored = self.client.session[DEMO_RULES_KEY]
        updated = next(r for r in stored if r["id"] == rule_id)
        self.assertEqual(updated["project_types"], ["konzert"])

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
        self.assertContains(response, "Noch keine Regeln")

    def test_entries_of_the_wrong_shape_re_seed_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = [{"id": 1}, "kaputt"]
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")

    def test_malformed_project_types_re_seeds_instead_of_crashing(self):
        session = self.client.session
        session[DEMO_RULES_KEY] = [
            {"id": 1, "text": "x", "active": True, "project_types": "konzert"}
        ]
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Noch keine Regeln")

    def test_the_page_explains_the_demo_scope_and_inactive_rules(self):
        response = self.client.get(reverse("rules_list"))
        self.assertContains(response, "diesem Besuch")
        self.assertContains(response, "nicht in den Plan")


@override_settings(DEMO_MODE=False)
class PlannerRulesDatabaseModeTest(TestCase):
    """The production path is untouched by #22: rules stay in the database and
    the session stays empty."""

    def setUp(self):
        for i, rule in enumerate(INITIAL_RULES):
            PlannerRule.objects.create(
                text=rule["text"],
                active=True,
                order=i,
                project_types=rule["project_types"],
            )

    def test_add_creates_a_rule_in_the_database(self):
        self.client.post(reverse("rule_add"), data={"text": "Neue Regel"})
        rule = PlannerRule.objects.get(text="Neue Regel")
        self.assertTrue(rule.active)
        self.assertEqual(rule.order, len(INITIAL_RULES))
        self.assertEqual(rule.project_types, [])
        self.assertNotIn(DEMO_RULES_KEY, self.client.session)

    def test_add_persists_project_types_in_the_database(self):
        self.client.post(
            reverse("rule_add"),
            data={"text": "Neue Regel", "project_types": ["hochzeit"]},
        )
        rule = PlannerRule.objects.get(text="Neue Regel")
        self.assertEqual(rule.project_types, ["hochzeit"])

    def test_add_rejects_malformed_project_types(self):
        """add_rule's only caller (the view) always sends a list via
        request.POST.getlist(), but the function itself should not rely on
        that — same guard as update_rule, applied on the way in instead of
        on the way out."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        add_rule(request, "Direkt aufgerufen", project_types="konzert")
        rule = PlannerRule.objects.get(text="Direkt aufgerufen")
        self.assertEqual(rule.project_types, [])

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

    def test_update_persists_project_types(self):
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"project_types": ["hochzeit", "recruiting"]}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["hochzeit", "recruiting"])

    def test_omitting_project_types_on_update_leaves_it_unchanged(self):
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"text": "Nur Text geändert"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

    def test_malformed_project_types_on_update_leaves_it_unchanged(self):
        """The DB backend has no read-time self-healing like the session's
        _is_valid() — an int would make _applies() raise, a string would
        silently turn its membership check into a substring check. Rejecting
        the bad value here, before it reaches the JSONField, is the only
        guard this backend gets."""
        rule = PlannerRule.objects.first()  # seeded as ["konzert"]
        self.client.post(
            reverse("rule_update", args=[rule.pk]),
            data=json.dumps({"project_types": "konzert"}),
            content_type="application/json",
        )
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

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
        PlannerRule.objects.filter(text=INITIAL_RULES[0]["text"]).update(active=False)
        request = RequestFactory().get("/")
        request.session = self.client.session
        expected = [
            r["text"]
            for r in INITIAL_RULES[1:]
            if not r["project_types"] or "konzert" in r["project_types"]
        ]
        self.assertEqual(get_active_rule_texts(request, "konzert"), expected)

    def test_rules_are_filtered_by_project_type_in_the_database(self):
        """#105: same filter as the demo session backend."""
        request = RequestFactory().get("/")
        request.session = self.client.session
        konzert_only = INITIAL_RULES[0]["text"]  # tagged ["konzert"]
        always = INITIAL_RULES[1]["text"]  # tagged []

        self.assertIn(konzert_only, get_active_rule_texts(request, "konzert"))
        self.assertNotIn(konzert_only, get_active_rule_texts(request, "hochzeit"))
        self.assertIn(always, get_active_rule_texts(request, "konzert"))
        self.assertIn(always, get_active_rule_texts(request, "hochzeit"))

    def test_the_demo_notice_is_absent(self):
        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, "diesem Besuch")

    def test_the_rules_page_is_scoped_to_the_current_project_type(self):
        """The maintainer only ever plans Konzert-tile events (concerts and,
        within that same tile, church services) and does not want to manage
        an event-type distinction she has no use for — so, same as the demo,
        the page filters to whatever she is currently planning (#105)."""
        session = self.client.session
        session["demo_project_type"] = "hochzeit"
        session.save()
        response = self.client.get(reverse("rules_list"))
        self.assertNotContains(response, INITIAL_RULES[0]["text"])  # konzert-only
        self.assertContains(response, INITIAL_RULES[1]["text"])  # applies to all

    def test_the_rules_page_shows_everything_without_a_current_project_type(self):
        """Reached from the dashboard sidebar rather than mid-planning, there
        is no type to scope by, so the maintainer sees her whole rule set."""
        response = self.client.get(reverse("rules_list"))
        for rule in INITIAL_RULES:
            self.assertContains(response, rule["text"])


@override_settings(DEMO_MODE=False)
class SeedRulesCommandTest(TestCase):
    def test_seeds_all_initial_rules_with_their_project_types(self):
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))
        for i, rule in enumerate(INITIAL_RULES):
            stored = PlannerRule.objects.get(text=rule["text"])
            self.assertEqual(stored.project_types, rule["project_types"])
            self.assertEqual(stored.order, i)
            self.assertTrue(stored.active)

    def test_marks_itself_seeded(self):
        call_command("seed_rules")
        self.assertTrue(RulesSeeded.objects.exists())

    def test_is_a_no_op_on_a_second_run(self):
        call_command("seed_rules")
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), len(INITIAL_RULES))

    def test_does_not_reseed_after_every_rule_is_deleted(self):
        """A maintainer can clear PlannerRule down to zero via the rules UI
        (rules.py exposes full add/delete). entrypoint.sh now runs seed_rules
        on every container start, and a deploy reruns the whole stack, so an
        idempotency check based on PlannerRule's row count would silently
        resurrect the deleted defaults on the next deploy. Tracking "already
        seeded" via RulesSeeded instead avoids that."""
        call_command("seed_rules")
        PlannerRule.objects.all().delete()
        call_command("seed_rules")
        self.assertEqual(PlannerRule.objects.count(), 0)


@override_settings(DEMO_MODE=False)
class BackfillPlannerRuleProjectTypesMigrationTest(TestCase):
    """#105 review follow-up: rows a pre-#105 seed_rules run created sit at
    project_types' field default ([]) once the migration adds the column.
    Migration 0007 backfills the scoping those rows are missing by matching
    on rule text."""

    def setUp(self):
        from django.apps import apps

        self.backfill = importlib.import_module(
            "projects.migrations.0007_backfill_planner_rule_project_types"
        ).backfill_project_types
        self.apps = apps

    def test_backfills_a_known_rule_still_at_the_default(self):
        rule = PlannerRule.objects.create(
            text="Vorverkauf nur bei größeren Konzerten relevant",
            active=True,
            order=0,
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["konzert"])

    def test_leaves_a_manually_assigned_row_untouched(self):
        rule = PlannerRule.objects.create(
            text="Vorverkauf nur bei größeren Konzerten relevant",
            active=True,
            order=0,
            project_types=["hochzeit"],
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, ["hochzeit"])

    def test_leaves_a_custom_rule_untouched(self):
        rule = PlannerRule.objects.create(
            text="Eigene Regel der Maintainerin", active=True, order=0
        )
        self.backfill(self.apps, None)
        rule.refresh_from_db()
        self.assertEqual(rule.project_types, [])


@override_settings(DEMO_MODE=False)
class MarkSeededIfRulesAlreadyExistMigrationTest(TestCase):
    """Migration 0009 backfills a RulesSeeded marker for any environment that
    already ran the old, row-count-based seed_rules before this migration
    existed — e.g. a maintainer who invoked it by hand before entrypoint.sh
    called it automatically. Without this, deploying the fix would find no
    marker and duplicate the INITIAL_RULES on top of what is already there.
    """

    def setUp(self):
        from django.apps import apps

        self.mark_seeded = importlib.import_module(
            "projects.migrations.0009_rulesseeded"
        ).mark_seeded_if_rules_already_exist
        self.apps = apps

    def test_marks_seeded_when_rules_already_exist(self):
        PlannerRule.objects.create(text="Vorhandene Regel", active=True, order=0)
        self.mark_seeded(self.apps, None)
        self.assertTrue(RulesSeeded.objects.exists())

    def test_is_a_no_op_on_a_fresh_empty_table(self):
        self.mark_seeded(self.apps, None)
        self.assertFalse(RulesSeeded.objects.exists())


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
    "Mehrprojekt-Dashboard" everywhere a visitor can reach it from."""

    def test_sidebar_link_says_mehrprojekt_dashboard(self):
        self.given_session_plan()
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Mehrprojekt-Dashboard")
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


class FaviconTest(DemoModeTestCase):
    """#27: no base template linked a favicon, so every tab showed the
    browser default. Both logos are already 2000x2000px — no new assets."""

    def test_favicon_links_on_public_pages(self):
        response = self.client.get(reverse("index"))
        self.assertContains(
            response,
            '<link rel="icon" type="image/png" href="/static/projects/logo_schwarz.png"',
        )
        self.assertContains(
            response,
            '<link rel="icon" type="image/png" href="/static/projects/logo_weiss.png"',
        )

    def test_favicon_links_on_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response,
            '<link rel="icon" type="image/png" href="/static/projects/logo_schwarz.png"',
        )
        self.assertContains(
            response,
            '<link rel="icon" type="image/png" href="/static/projects/logo_weiss.png"',
        )

    def test_apple_touch_icon_present(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, '<link rel="apple-touch-icon"')


class SocialMetaTagsTest(DemoModeTestCase):
    """#27: no og:* tags anywhere, so the demo link previewed as a bare URL
    wherever it was shared."""

    def test_default_og_tags_present(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'property="og:title"')
        self.assertContains(response, 'property="og:type"')
        self.assertContains(response, 'property="og:description"')
        self.assertContains(response, 'property="og:locale"')
        self.assertContains(
            response,
            'property="og:image" content="http://testserver/static/projects/og-image.png"',
        )
        self.assertContains(response, 'property="og:url" content="http://testserver/"')


class Custom404Test(TestCase):
    """#27: with DEBUG=False, no 404.html meant Django's unstyled default
    page — and it leaked that this is Django."""

    def test_unknown_url_renders_the_custom_page(self):
        response = self.client.get("/this-page-does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Seite nicht gefunden", status_code=404)


class Custom500Test(SimpleTestCase):
    """#27: exercises django.views.defaults.server_error directly — the same
    function Django's own error handling calls in production, independent of
    which application code happens to raise. Renders with an empty Context
    (no request, no context processors), which base_public.html tolerates —
    neither it nor its includes reference request/user/messages."""

    def test_server_error_view_renders_the_custom_page(self):
        from django.views.defaults import server_error

        response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Etwas ist schiefgelaufen", response.content.decode())


class HealthCheckTest(TestCase):
    """#27: docker compose up -d reported success for a container that was
    up but not yet serving, since neither compose file defined a
    healthcheck. In production the check is DB-aware; the demo stack has no
    Postgres service, so it stays a pure liveness probe there."""

    @override_settings(DEMO_MODE=True)
    def test_returns_200_in_demo_mode(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)

    @override_settings(DEMO_MODE=False)
    def test_returns_200_when_database_reachable(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)

    @override_settings(DEMO_MODE=False)
    def test_returns_503_when_database_unreachable(self):
        from django.db import Error

        with patch(
            "django.db.connection.ensure_connection",
            side_effect=Error("down"),
        ):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)


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
        self.assertIn("week_start: weekStart", html)

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
    """#199, second half. A new date moves the task in the chronological
    order, which renumbers the summary's task_refs (_number_projects_and_tasks,
    ai.py) — that cannot survive. The projects can: re-sort, re-annotate,
    write back. So the Notion read goes and only the Claude call stays."""

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

    def test_only_the_summary_is_dropped(self):
        today = date.today()
        _warm_dashboard_cache(
            [_cached_task("task-1", today + timedelta(days=1))], summary="<p>alt</p>"
        )
        self.post_date("task-1", today + timedelta(days=10))
        projects, summary_data = cache.get(CACHE_KEY)
        self.assertTrue(projects)
        self.assertIsNone(summary_data)

    def test_the_stale_copy_is_patched_too(self):
        today = date.today()
        _warm_dashboard_cache([_cached_task("task-1", today + timedelta(days=1))])
        self.post_date("task-1", today + timedelta(days=10))
        self.assertEqual(
            _cached_task_by_id(STALE_CACHE_KEY, "task-1")["due"],
            today + timedelta(days=10),
        )
        self.assertIsNone(cache.get(STALE_CACHE_KEY)[1])

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
