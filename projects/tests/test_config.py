"""Deployment, settings and environment: what has to be true of the
configuration itself, plus the error and health endpoints."""

import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from planning_hub.env import apply_credentials

from ..models import DemoEvent
from ..startup import (
    MissingAPIKeyError,
    require_api_keys,
)
from .base import DemoModeTestCase


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


class DemoSqliteConcurrencyConfTest(SimpleTestCase):
    """#255 follow-up: gunicorn serves the demo from eight threads instead of
    two processes, and the demo keeps its sessions and its DatabaseCache in
    the one SQLite file. SQLite admits a single writer whichever way the
    concurrency was bought, and on the stock options a transaction that reads
    before it writes does not queue for the lock at all — it fails the moment
    the lock is held, which is a 500 rather than a slow request.

    Measured on that shape (8 threads, 200 read-then-write transactions, the
    order DatabaseCache._base_set and a session save both have): 43 failed on
    the defaults, 0 with these three set. Pinned so they are not later tidied
    away as noise on a database "only the demo uses"."""

    def demo_databases(self):
        # django.conf.settings copied its values at process startup —
        # reloading the module object touches nothing the running suite reads
        # (see StaticStorageConfigTest). The production leg of CI runs this
        # test too, which is why the setting is re-derived under an explicit
        # DEMO_MODE rather than read off the live settings.
        import planning_hub.settings as settings_module

        env = {"SECRET_KEY": "test-key", "DEMO_MODE": "true"}
        with patch.dict(os.environ, env, clear=True):
            databases = importlib.reload(settings_module).DATABASES
        importlib.reload(settings_module)  # re-derive the test-run state
        return databases

    def test_the_demo_database_lets_readers_past_a_write(self):
        """Equality rather than a substring, because a *longer* value is the
        silent failure: SQLite ignores an unrecognised journal mode without
        error, so `WALL` would leave the file in rollback mode while still
        containing `WAL`. Nothing downstream would catch it — the test
        database is in-memory, where the pragma is a documented no-op."""
        options = self.demo_databases()["default"]["OPTIONS"]
        self.assertEqual(options["init_command"], "PRAGMA journal_mode=WAL;")

    def test_a_write_transaction_takes_its_lock_at_the_start(self):
        """The half that removes the failure rather than shortening it: under
        DEFERRED the lock is asked for halfway through, and that request does
        not honour `timeout`."""
        options = self.demo_databases()["default"]["OPTIONS"]
        self.assertEqual(options["transaction_mode"], "IMMEDIATE")

    def test_a_queued_writer_waits_longer_than_sqlite_would(self):
        options = self.demo_databases()["default"]["OPTIONS"]
        self.assertGreaterEqual(options["timeout"], 20)

    def test_production_gets_none_of_it(self):
        """Postgres has its own concurrency and none of these keys mean
        anything there — the block must not grow a copy of them."""
        import planning_hub.settings as settings_module

        env = {"SECRET_KEY": "test-key", "DEMO_MODE": "false"}
        with patch.dict(os.environ, env, clear=True):
            databases = importlib.reload(settings_module).DATABASES
        importlib.reload(settings_module)  # re-derive the test-run state
        self.assertEqual(
            databases["default"]["ENGINE"], "django.db.backends.postgresql"
        )
        self.assertNotIn("OPTIONS", databases["default"])


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
    which application code happens to raise. It renders with an empty Context
    (no request, no context processors), which base_public.html survives
    because the template engine resolves missing variables to the empty
    string — not because it is request-free. Its og:image and og:url tags do
    read request, and on this page they come out as
    ":///static/projects/og-image.png" and "://". The includes are genuinely
    request-free. Malformed og tags on a page no crawler indexes are not
    worth a fix; this note exists so the test is not mistaken for proof that
    base_public.html needs no context."""

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


class DotenvCredentialRuleTest(TestCase):
    """#220: `.env`'s credentials beat the ambient environment, its switches
    do not. A blanket override fixes the shadowed API key and silently takes
    away `DEMO_MODE=false manage.py test`, the local production test leg."""

    def test_a_credential_in_dotenv_beats_the_environment(self):
        # The bug this exists for: a stale key in the login session shadowed
        # the working one and every Claude call came back 401.
        environ = {"ANTHROPIC_API_KEY": "sk-ant-stale"}
        apply_credentials(environ, {"ANTHROPIC_API_KEY": "sk-ant-good"})
        self.assertEqual(environ["ANTHROPIC_API_KEY"], "sk-ant-good")

    def test_every_credential_is_covered_not_just_the_one_that_broke(self):
        environ = {"NOTION_API_KEY": "ntn_stale"}
        apply_credentials(environ, {"NOTION_API_KEY": "ntn_good"})
        self.assertEqual(environ["NOTION_API_KEY"], "ntn_good")

    def test_a_switch_from_the_environment_survives(self):
        # `DEMO_MODE=false manage.py test` selects the PostgreSQL leg. Losing
        # it would be silent: both legs pass, so the run still looks right.
        environ = {"DEMO_MODE": "false"}
        apply_credentials(environ, {"DEMO_MODE": "TRUE"})
        self.assertEqual(environ["DEMO_MODE"], "false")

    def test_a_secret_that_is_not_an_api_key_is_left_alone(self):
        # SECRET_KEY and DB_PASSWORD are per-deployment configuration, not
        # ambient pollution — CI sets SECRET_KEY for exactly one run.
        environ = {"SECRET_KEY": "ci-test-key", "DB_PASSWORD": "postgres"}
        apply_credentials(
            environ, {"SECRET_KEY": "local-key", "DB_PASSWORD": "local-pw"}
        )
        self.assertEqual(environ["SECRET_KEY"], "ci-test-key")
        self.assertEqual(environ["DB_PASSWORD"], "postgres")

    def test_a_credential_missing_from_the_environment_is_still_filled_in(self):
        environ = {}
        apply_credentials(environ, {"ANTHROPIC_API_KEY": "sk-ant-good"})
        self.assertEqual(environ["ANTHROPIC_API_KEY"], "sk-ant-good")

    def test_an_empty_value_in_dotenv_does_not_blank_a_working_key(self):
        # python-dotenv yields "" for a bare `ANTHROPIC_API_KEY=` line, and
        # None for a key with no `=` at all.
        environ = {"ANTHROPIC_API_KEY": "sk-ant-good"}
        apply_credentials(environ, {"ANTHROPIC_API_KEY": ""})
        apply_credentials(environ, {"ANTHROPIC_API_KEY": None})
        self.assertEqual(environ["ANTHROPIC_API_KEY"], "sk-ant-good")

    def test_dot_env_is_kept_out_of_the_docker_image(self):
        # The single line that makes this rule harmless in the containers.
        # manage.py runs there three times (entrypoint.sh: migrate,
        # seed_rules, collectstatic), so the rule executes — it is a no-op
        # only because there is no .env inside the image to read. Loosen
        # .dockerignore and a developer's key would outrank the deployment's
        # own env_file, silently.
        ignored = (settings.BASE_DIR / ".dockerignore").read_text().split()
        self.assertIn(".env", ignored)

    def test_no_host_path_is_mounted_into_the_app_directory(self):
        # The other half: .dockerignore keeps .env out of the *image*, and a
        # bind mount could still put it into the running container. Named
        # volumes under /app are fine (static_files, demo_db) — a host path
        # is not, because the project directory is where .env lives. The
        # nginx service's ./nginx.conf mounts land outside /app entirely.
        for name in ("docker-compose.yml", "docker-compose.demo.yml"):
            compose = (settings.BASE_DIR / name).read_text()
            for line in compose.splitlines():
                entry = line.strip()
                if not entry.startswith("- ") or ":" not in entry:
                    continue
                source, target = entry[2:].split(":")[:2]
                is_host_path = source.startswith((".", "/", "~", "$"))
                reaches_app = target == "/app" or target.startswith("/app/")
                self.assertFalse(
                    is_host_path and reaches_app,
                    f"{name} mounts the host path {source} at {target}; that "
                    f"would carry .env into the container and let it outrank "
                    f"the deployment's own env_file",
                )

    def test_manage_py_applies_the_rule_after_load_dotenv(self):
        # Order matters: load_dotenv() first fills the gaps, then this
        # overrides the credentials. Reversed, load_dotenv would be a no-op
        # over the values just written and the stale key would stand.
        source = (settings.BASE_DIR / "manage.py").read_text()
        self.assertLess(
            source.index("load_dotenv()"),
            source.index("apply_credentials(os.environ, dotenv_values())"),
        )


class HttpsOnlySettingsConfTest(SimpleTestCase):
    """#157: `check --deploy` reported four warnings — W012 and W016 (the
    session and CSRF cookies may travel over plain HTTP), W004 (no HSTS) and
    W008 (no SSL redirect). Four settings lines, on a public portfolio repo
    whose code otherwise shows deliberate care.

    Gating them on plain `not DEBUG`, as the issue suggested, would break
    production: `nginx.conf` listens on port 80 only, there is no TLS on the
    Mac Mini, and access is `http://192.168.178.121` over VPN. A browser
    discards a `Secure`-flagged cookie on such a connection, so sessions and
    every CSRF-protected POST would die. Hence one env switch, `HTTPS_ONLY`
    (default true, the same shape as DEBUG and DEMO_MODE), which production
    sets to false in its own `.env`. `check --deploy` keeps warning *there*,
    which is simply the truth about that deployment.

    Reloading the module object touches nothing the running suite reads —
    django.conf.settings copied its values at startup (see
    StaticStorageConfigTest)."""

    SERVER_ARGV = ["gunicorn", "planning_hub.wsgi"]

    def reload_with(self, env, argv=None):
        """The settings module re-derived under a given environment. SECRET_KEY
        is always supplied because a missing one fails closed (#47)."""
        import planning_hub.settings as settings_module

        with (
            patch.dict(os.environ, {"SECRET_KEY": "test-key", **env}, clear=True),
            patch.object(sys, "argv", argv or sys.argv),
        ):
            reloaded = importlib.reload(settings_module)
            values = {
                name: getattr(reloaded, name, None)
                for name in (
                    "HTTPS_ONLY",
                    "SESSION_COOKIE_SECURE",
                    "CSRF_COOKIE_SECURE",
                    "SECURE_HSTS_SECONDS",
                    "SECURE_PROXY_SSL_HEADER",
                    "SECURE_SSL_REDIRECT",
                    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
                )
            }
        importlib.reload(settings_module)  # re-derive the test-run state
        return values

    def test_a_fresh_checkout_with_debug_off_turns_everything_on(self):
        """Acceptance 1: no HTTPS_ONLY in the environment means the secure
        defaults apply, so `check --deploy` finds silence rather than the four
        warnings."""
        values = self.reload_with({"DEBUG": "false"}, argv=self.SERVER_ARGV)
        self.assertTrue(values["HTTPS_ONLY"])
        self.assertTrue(values["SESSION_COOKIE_SECURE"])
        self.assertTrue(values["CSRF_COOKIE_SECURE"])
        self.assertTrue(values["SECURE_SSL_REDIRECT"])
        self.assertGreater(values["SECURE_HSTS_SECONDS"], 0)
        self.assertEqual(
            values["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_local_runserver_over_plain_http_keeps_working(self):
        """Acceptance 2: with DEBUG on, every flag is off — runserver speaks
        http:// and a Secure cookie would never come back."""
        values = self.reload_with({"DEBUG": "true"}, argv=self.SERVER_ARGV)
        self.assertFalse(values["HTTPS_ONLY"])
        self.assertFalse(values["SESSION_COOKIE_SECURE"])
        self.assertFalse(values["CSRF_COOKIE_SECURE"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])

    def test_debug_wins_even_if_https_only_is_asked_for(self):
        # HTTPS_ONLY is an opt-*out* for a TLS-less deployment, never an
        # opt-in that overrides DEBUG: a local runserver must not start
        # handing out Secure cookies because a stale .env says true.
        values = self.reload_with(
            {"DEBUG": "true", "HTTPS_ONLY": "true"}, argv=self.SERVER_ARGV
        )
        # assertIs, not assertFalse: a setting that does not exist at all
        # reads as None, which is falsy — the weaker assertion would pass
        # before the block is written.
        self.assertIs(values["HTTPS_ONLY"], False)

    def test_production_opts_out_and_keeps_working(self):
        """Acceptance 3: the Mac Mini has no TLS. HTTPS_ONLY=false in its
        .env keeps sessions and CSRF-protected POSTs alive."""
        values = self.reload_with(
            {"DEBUG": "false", "HTTPS_ONLY": "false"}, argv=self.SERVER_ARGV
        )
        self.assertFalse(values["HTTPS_ONLY"])
        self.assertFalse(values["SESSION_COOKIE_SECURE"])
        self.assertFalse(values["CSRF_COOKIE_SECURE"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])

    def test_the_suite_never_redirects_itself_to_https(self):
        """The test client speaks http://, and CI runs without a .env so DEBUG
        defaults to false — without the _TESTING guard every request in the
        suite would answer 301 instead of reaching a view."""
        self.assertFalse(settings.SECURE_SSL_REDIRECT)
        values = self.reload_with({"DEBUG": "false"})  # test-runner argv
        self.assertTrue(values["HTTPS_ONLY"], "the other flags still apply")
        self.assertFalse(values["SECURE_SSL_REDIRECT"])

    def test_hsts_starts_short_and_says_when_to_raise_it(self):
        """Acceptance 4. HSTS is remembered by browsers for its whole max-age,
        so a misconfiguration cannot be taken back by fixing the server — an
        hour is the recoverable starting point, and the raise-later plan
        belongs where the number is."""
        values = self.reload_with({"DEBUG": "false"}, argv=self.SERVER_ARGV)
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 3600)
        # The plan belongs in the comment the number carries, not three
        # screens away — asserted against the block that precedes the
        # assignment, so moving the number without its reasoning fails here.
        source = (settings.BASE_DIR / "planning_hub/settings.py").read_text()
        block = source[: source.index("SECURE_HSTS_SECONDS = ")]
        block = block[block.rindex("\n\n") :]
        self.assertRegex(block, r"(?i)rais\w+ it")
        self.assertRegex(block, r"(?i)year")
        self.assertIn("#157", source)

    def test_subdomains_are_not_this_apps_to_promise_for(self):
        # ligaauguste.de carries more than this app — the demo, and whatever
        # else the domain serves — so an includeSubDomains would commit every
        # one of them to HTTPS on the strength of this deployment alone.
        #
        # Django's own default is already False, so asserting the value proves
        # nothing. What is worth pinning is that settings.py never turns it
        # on, and that the reason is written down beside the silenced check.
        source = (settings.BASE_DIR / "planning_hub/settings.py").read_text()
        self.assertNotIn("SECURE_HSTS_INCLUDE_SUBDOMAINS = True", source)
        self.assertIs(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS, False)
        block = source[source.index("SILENCED_SYSTEM_CHECKS") - 900 :]
        self.assertRegex(block, r"(?i)subdomain")

    def test_the_two_warnings_hsts_itself_raises_are_silenced_with_a_reason(self):
        # Enabling HSTS trades four warnings for two: W005 wants
        # includeSubDomains, W021 wants preload. Both are deliberate
        # non-choices, so they are silenced rather than left to erode the
        # point of the issue.
        self.assertEqual(
            sorted(settings.SILENCED_SYSTEM_CHECKS), ["security.W005", "security.W021"]
        )
        source = (settings.BASE_DIR / "planning_hub/settings.py").read_text()
        block = source[source.index("SILENCED_SYSTEM_CHECKS") - 900 :]
        self.assertIn("W005", block)
        self.assertIn("W021", block)

    def test_the_trusted_header_is_the_one_the_demo_nginx_actually_sets(self):
        # SECURE_PROXY_SSL_HEADER is a promise about the proxy in front: if
        # nginx did not overwrite the header, a client could forge it and
        # Django would believe the request arrived over TLS. Pinned as a pair
        # so neither half can drift alone.
        conf = (settings.BASE_DIR / "nginx-demo.conf").read_text()
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", conf)
        values = self.reload_with({"DEBUG": "false"}, argv=self.SERVER_ARGV)
        self.assertEqual(
            values["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_the_switch_is_documented_for_the_deployment_that_needs_it(self):
        # The rollout order matters on the Mac Mini (HTTPS_ONLY=false before
        # the pull), so the switch has to be discoverable from the files a
        # deploy reads, not only from the issue.
        self.assertIn("HTTPS_ONLY", (settings.BASE_DIR / ".env.example").read_text())
        self.assertIn("HTTPS_ONLY", (settings.BASE_DIR / "README.md").read_text())
