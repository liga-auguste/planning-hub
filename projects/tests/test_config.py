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
