"""Production monitoring: the watcher, the logs it keeps, and its alerts (#303).

host/watch.sh runs as a subprocess under `sh`, the way test_production_host.py
runs check.sh. `docker` and `curl` are stubs first on PATH that record their
arguments and answer from a scratch directory, and check.sh is replaced by a
stub next to the copied watch.sh. The `web` log the stub docker serves is built
from the app's own log records, so a changed log message fails here and not in
production.
"""

import logging
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import include, path

from projects.ai import AIUnavailableError, log_claude_call
from projects.notion import NotionUnavailableError, translate_notion_errors

REPO = Path(settings.BASE_DIR)
SCRIPT = REPO / "host" / "watch.sh"
PLIST = REPO / "host" / "de.ligaauguste.planninghub.watch.plist"
DOC = REPO / "docs" / "monitoring.md"

PING = "https://hc-ping.com/production-uuid"
ERRORS_PING = "https://hc-ping.com/errors-uuid"

# Every line the watcher can put into a ping body, by its label. The doc has
# to explain each one.
ALERT_LABELS = (
    "FAIL web healthy",
    "FAIL backup fresh",
    "unhandled errors (500)",
    "Notion failures",
    "Claude failures",
)

STUB_DOCKER = r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "$STUB_DIR/calls"
[ -f "$STUB_DIR/docker-exit" ] && exit "$(cat "$STUB_DIR/docker-exit")"
for service; do :; done
[ -f "$STUB_DIR/logs-$service" ] && cat "$STUB_DIR/logs-$service"
exit 0
"""

# Records each call on one line, the body with its newlines made visible.
STUB_CURL = r"""#!/bin/sh
printf 'curl %s\n' "$*" | tr '\n' '|' >> "$STUB_DIR/calls"
printf '\n' >> "$STUB_DIR/calls"
exit "$(cat "$STUB_DIR/curl-exit" 2>/dev/null || echo 0)"
"""

STUB_SUDO = r"""#!/bin/sh
printf 'sudo %s\n' "$*" >> "$STUB_DIR/calls"
exit 1
"""

# Stands in for host/check.sh next to the copied watch.sh.
STUB_CHECK = r"""#!/bin/sh
cat "$STUB_DIR/check-out"
exit "$(cat "$STUB_DIR/check-exit")"
"""

CHECK_OK = "ok   Docker answers\nok   web healthy\n"
CHECK_FAIL = (
    "ok   Docker answers\n"
    "FAIL web healthy: unhealthy. Fix: docker compose up -d, then docker compose logs web\n"
)

STUB_BIN = None


def setUpModule():
    # Written once: macOS checks every new executable on its first run.
    global STUB_BIN
    STUB_BIN = Path(tempfile.mkdtemp())
    for name, body in (
        ("docker", STUB_DOCKER),
        ("curl", STUB_CURL),
        ("sudo", STUB_SUDO),
    ):
        stub = STUB_BIN / name
        stub.write_text(body)
        stub.chmod(0o755)


def tearDownModule():
    shutil.rmtree(STUB_BIN)


def docker_line(moment, text):
    """One line as `docker compose logs --timestamps --no-log-prefix` prints it:
    a fixed-width UTC timestamp, a space, the container's own output."""
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond:06d}000Z {text}"


BASE = datetime(2026, 10, 29, 9, 0, 0, 120000, tzinfo=UTC)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


class WatchCase(SimpleTestCase):
    """A checkout with host/watch.env, a fresh backup and stubs for a healthy host."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.checkout = self.tmp / "checkout"
        (self.checkout / "host").mkdir(parents=True)
        (self.checkout / "backup").mkdir()
        (self.checkout / "host" / "watch.env").write_text(
            f"WATCH_PING_URL={PING}\nWATCH_ERRORS_PING_URL={ERRORS_PING}\n"
        )
        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        shutil.copy(SCRIPT, self.scripts / "watch.sh")
        check = self.scripts / "check.sh"
        check.write_text(STUB_CHECK)
        check.chmod(0o755)
        self.home = self.tmp / "home"
        self.backups = self.home / "Backups" / "planning-hub"
        self.backups.mkdir(parents=True)
        self.logs = self.home / "Library" / "Logs" / "planning-hub"
        self.set_backup_age(hours=1)
        self.check(CHECK_OK, 0)
        self.serve("web", [])
        self.serve("nginx", [])

    def check(self, out, code):
        (self.stub / "check-out").write_text(out)
        (self.stub / "check-exit").write_text(str(code))

    def serve(self, service, lines):
        (self.stub / f"logs-{service}").write_text(
            "".join(f"{line}\n" for line in lines)
        )

    def set_backup_age(self, hours, directory=None):
        last = (directory or self.backups) / "last-success"
        last.write_text("2026-10-29T03:30:12 planning_hub-20261029-033000.dump\n")
        moment = time.time() - hours * 3600
        os.utime(last, (moment, moment))

    def run_watch(self):
        (self.stub / "calls").unlink(missing_ok=True)
        return subprocess.run(
            ["sh", str(self.scripts / "watch.sh")],
            env={
                "PATH": f"{STUB_BIN}:{os.environ['PATH']}",
                "HOME": str(self.home),
                "STUB_DIR": str(self.stub),
            },
            cwd=self.checkout,
            capture_output=True,
            text=True,
            timeout=30,
            # Each test reads the exit code itself.
            check=False,
        )

    def calls(self, name):
        path = self.stub / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line[len(name) + 1 :] for line in lines if line.startswith(name + " ")]

    def pings(self):
        """{url: body or None} for every curl call of the last run."""
        sent = {}
        for args in self.calls("curl"):
            parts = args.rstrip("|").split(" ")
            url = parts[-1]
            body = None
            if "--data-raw" in args:
                start = args.index("--data-raw ") + len("--data-raw ")
                end = args.rindex(" " + url)
                body = args[start:end].replace("|", "\n")
            sent[url] = body
        return sent

    def assert_all_well(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pings(), {PING: None, ERRORS_PING: None})


class AllWellTest(WatchCase):
    def test_a_success_ping_to_both_checks(self):
        self.assert_all_well(self.run_watch())

    def test_the_pings_have_a_timeout_and_retries(self):
        self.run_watch()

        for args in self.calls("curl"):
            self.assertIn("-m 10", args)
            self.assertIn("--retry 3", args)


class CheckFailureTest(WatchCase):
    def test_one_failing_run_does_not_alert(self):
        # A deploy rebuilds web within about a minute.
        self.check(CHECK_FAIL, 1)

        self.assert_all_well(self.run_watch())

    def test_two_failing_runs_in_a_row_alert_production(self):
        self.check(CHECK_FAIL, 1)
        self.run_watch()

        result = self.run_watch()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.pings(), {f"{PING}/fail": "FAIL web healthy", ERRORS_PING: None}
        )

    def test_a_recovery_clears_the_marker(self):
        self.check(CHECK_FAIL, 1)
        self.run_watch()
        self.check(CHECK_OK, 0)
        self.run_watch()
        self.check(CHECK_FAIL, 1)

        self.assert_all_well(self.run_watch())

    def test_a_failing_check_without_a_fail_line_still_alerts(self):
        # check.sh missing or broken: no label to report, but still a failure.
        self.check("", 2)
        self.run_watch()

        self.run_watch()

        self.assertIn(f"{PING}/fail", self.pings())


class BackupFreshnessTest(WatchCase):
    def test_25_hours_is_fine(self):
        self.set_backup_age(hours=25)
        self.assert_all_well(self.run_watch())

    def test_27_hours_alerts_on_the_first_run(self):
        self.set_backup_age(hours=27)

        self.run_watch()

        self.assertEqual(self.pings()[f"{PING}/fail"], "FAIL backup fresh")

    def test_a_missing_last_success_alerts(self):
        (self.backups / "last-success").unlink()

        self.run_watch()

        self.assertEqual(self.pings()[f"{PING}/fail"], "FAIL backup fresh")

    def test_backup_dir_from_the_backup_settings_is_honoured(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.set_backup_age(hours=1, directory=elsewhere)
        (self.backups / "last-success").unlink()
        (self.checkout / "backup" / "backup.env").write_text(
            f"BACKUP_DIR={elsewhere}\n"
        )

        self.assert_all_well(self.run_watch())

    def test_both_failures_share_one_body(self):
        self.set_backup_age(hours=27)
        self.check(CHECK_FAIL, 1)
        self.run_watch()

        self.run_watch()

        self.assertEqual(
            self.pings()[f"{PING}/fail"], "FAIL web healthy\nFAIL backup fresh"
        )


def _boom(request):
    raise RuntimeError("SECRET-IN-TRACEBACK")


# The app's own routes as well: the 500 page links back into the app.
urlpatterns = [path("boom/", _boom), path("", include(settings.ROOT_URLCONF))]


class AppRecordsMixin:
    """`web` log lines built from the records the app itself logs."""

    def format(self, record, seconds):
        fmt = settings.LOGGING["formatters"]["plain"]
        formatter = logging.Formatter(fmt["format"], style=fmt["style"])
        return [
            docker_line(at(seconds), line)
            for line in formatter.format(record).splitlines()
        ]

    @override_settings(ROOT_URLCONF=__name__)
    def unhandled_error(self, seconds):
        self.client.raise_request_exception = False
        with self.assertLogs("django.request", "ERROR") as logs:
            response = self.client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        return self.format(logs.records[0], seconds)

    def notion_failure(self, seconds):
        with (
            self.assertLogs("projects.notion", "WARNING") as logs,
            self.assertRaises(NotionUnavailableError),
            translate_notion_errors(),
        ):
            raise httpx.ConnectError("SECRET-NOTION")
        return self.format(logs.records[0], seconds)

    def claude_failure(self, seconds):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        with (
            self.assertLogs("projects.ai", "WARNING") as logs,
            self.assertRaises(AIUnavailableError),
            log_claude_call("generate_plan"),
        ):
            raise anthropic.APIConnectionError(message="SECRET-CLAUDE", request=request)
        return self.format(logs.records[0], seconds)

    def every_error(self):
        """One of each, enough of each to alert, all within one run."""
        return [
            *self.unhandled_error(1),
            *self.notion_failure(2),
            *self.notion_failure(3),
            *self.notion_failure(4),
            *self.claude_failure(5),
            *self.claude_failure(6),
            *self.claude_failure(7),
        ]

    def bad_gateway(self, seconds):
        record = logging.LogRecord(
            "django.request", logging.WARNING, "", 0, "Bad Gateway: %s", ("/x/",), None
        )
        return self.format(record, seconds)


class AppErrorTest(AppRecordsMixin, WatchCase):
    def test_one_unhandled_error_alerts(self):
        self.serve("web", self.unhandled_error(1))

        result = self.run_watch()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.pings(),
            {PING: None, f"{ERRORS_PING}/fail": "1 unhandled errors (500)"},
        )

    def test_two_notion_failures_do_not_alert(self):
        self.serve("web", self.notion_failure(1) + self.notion_failure(2))
        self.assert_all_well(self.run_watch())

    def test_three_notion_failures_alert(self):
        self.serve(
            "web",
            [*self.notion_failure(1), *self.notion_failure(2), *self.notion_failure(3)],
        )

        self.run_watch()

        self.assertEqual(self.pings()[f"{ERRORS_PING}/fail"], "3 Notion failures")

    def test_two_claude_failures_do_not_alert(self):
        self.serve("web", self.claude_failure(1) + self.claude_failure(2))
        self.assert_all_well(self.run_watch())

    def test_three_claude_failures_alert(self):
        self.serve(
            "web",
            [*self.claude_failure(1), *self.claude_failure(2), *self.claude_failure(3)],
        )

        self.run_watch()

        self.assertEqual(self.pings()[f"{ERRORS_PING}/fail"], "3 Claude failures")

    def test_bad_gateway_lines_alone_do_not_alert(self):
        # The views' answer to the Notion and Claude failures counted above.
        self.serve(
            "web", [*self.bad_gateway(1), *self.bad_gateway(2), *self.bad_gateway(3)]
        )
        self.assert_all_well(self.run_watch())

    def test_lines_counted_once_are_not_counted_again(self):
        self.serve("web", self.unhandled_error(1))
        self.run_watch()

        self.assert_all_well(self.run_watch())


class TheBodyLeaksNothingTest(AppRecordsMixin, WatchCase):
    def test_no_log_line_and_no_found_value_reaches_a_ping(self):
        self.check(CHECK_FAIL.replace("unhealthy", "SECRET-FOUND"), 1)
        self.run_watch()
        self.serve("web", self.every_error())

        self.run_watch()

        calls = (self.stub / "calls").read_text()
        for marker in ("SECRET", "Traceback", "/boom/", "Fix:"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, calls)


class CollectionTest(AppRecordsMixin, WatchCase):
    def stored(self, service):
        return (self.logs / f"{service}.log").read_text().splitlines()

    def logs_calls(self, service):
        return [args for args in self.calls("docker") if args.endswith(f" {service}")]

    def test_the_first_run_stores_everything(self):
        lines = [docker_line(at(1), "GET / 200"), docker_line(at(2), "GET /x/ 200")]
        self.serve("nginx", lines)

        self.run_watch()

        self.assertEqual(self.stored("nginx"), lines)
        [args] = self.logs_calls("nginx")
        self.assertEqual(
            args, "compose logs --no-color --no-log-prefix --timestamps nginx"
        )

    def test_the_next_run_stores_only_newer_lines_and_passes_since(self):
        first = [docker_line(at(1), "one"), docker_line(at(1.5), "two")]
        self.serve("nginx", first)
        self.run_watch()
        # Docker answers --since to the second, so "two" comes back again.
        later = [first[1], docker_line(at(1.7), "three"), docker_line(at(9), "four")]
        self.serve("nginx", later)

        self.run_watch()

        self.assertEqual(self.stored("nginx"), [*first, *later[1:]])
        [args] = self.logs_calls("nginx")
        self.assertIn("--since 2026-10-29T09:00:01Z ", args)

    def test_a_recreated_containers_lines_are_stored(self):
        self.serve("web", [docker_line(at(1), "old container")])
        self.run_watch()
        # A deploy recreates web: the old container's lines are gone.
        self.serve("web", [docker_line(at(60), "new container")])

        self.run_watch()

        self.assertEqual(
            self.stored("web"),
            [docker_line(at(1), "old container"), docker_line(at(60), "new container")],
        )

    def test_when_docker_fails_the_cursor_stays(self):
        self.serve("nginx", [docker_line(at(1), "one")])
        self.run_watch()
        (self.stub / "docker-exit").write_text("1")

        result = self.run_watch()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        (self.stub / "docker-exit").unlink()
        self.serve("nginx", [docker_line(at(1), "one"), docker_line(at(5), "two")])
        self.run_watch()
        self.assertEqual(
            self.stored("nginx"), [docker_line(at(1), "one"), docker_line(at(5), "two")]
        )
        [args] = self.logs_calls("nginx")
        self.assertIn("--since 2026-10-29T09:00:01Z ", args)

    def test_a_file_rotates_at_10_mb_and_keeps_five(self):
        self.logs.mkdir(parents=True)
        (self.logs / "nginx.log").write_bytes(b"x" * (10 * 1024 * 1024))
        for n in range(1, 6):
            (self.logs / f"nginx.log.{n}").write_text(f"generation {n}\n")
        self.serve("nginx", [docker_line(at(1), "fresh")])

        self.run_watch()

        self.assertEqual(self.stored("nginx"), [docker_line(at(1), "fresh")])
        self.assertEqual((self.logs / "nginx.log.1").stat().st_size, 10 * 1024 * 1024)
        self.assertEqual((self.logs / "nginx.log.5").read_text(), "generation 4\n")
        self.assertFalse((self.logs / "nginx.log.6").exists())

    def test_the_watchers_own_log_rotates_the_same_way(self):
        self.logs.mkdir(parents=True)
        (self.logs / "watch.log").write_bytes(b"x" * (10 * 1024 * 1024))

        self.run_watch()

        self.assertTrue((self.logs / "watch.log.1").exists())
        self.assertFalse((self.logs / "watch.log").exists())

    def test_a_log_dir_from_the_watch_settings_is_honoured(self):
        elsewhere = self.tmp / "logs"
        with (self.checkout / "host" / "watch.env").open("a") as env:
            env.write(f"WATCH_LOG_DIR={elsewhere}\n")
        self.serve("nginx", [docker_line(at(1), "one")])

        self.run_watch()

        self.assertEqual(
            (elsewhere / "nginx.log").read_text(), docker_line(at(1), "one") + "\n"
        )


class FailClosedTest(WatchCase):
    def test_without_the_settings_file(self):
        (self.checkout / "host" / "watch.env").unlink()

        result = self.run_watch()

        self.assertEqual(result.returncode, 1)
        self.assertIn("host/watch.env", result.stderr)
        self.assertEqual(self.calls("curl"), [])

    def test_without_one_of_the_urls(self):
        env = self.checkout / "host" / "watch.env"
        for setting, other in (
            ("WATCH_PING_URL", f"WATCH_ERRORS_PING_URL={ERRORS_PING}"),
            ("WATCH_ERRORS_PING_URL", f"WATCH_PING_URL={PING}"),
        ):
            with self.subTest(setting=setting):
                env.write_text(f"{other}\n")

                result = self.run_watch()

                self.assertEqual(result.returncode, 1)
                self.assertIn(setting, result.stderr)
                self.assertEqual(self.calls("curl"), [])

    def test_a_failed_ping_exits_non_zero_after_storing(self):
        (self.stub / "curl-exit").write_text("22")
        self.serve("nginx", [docker_line(at(1), "one")])

        result = self.run_watch()

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.logs / "nginx.log").exists())
        (self.stub / "curl-exit").unlink()
        self.run_watch()
        self.assertEqual(len((self.logs / "nginx.log").read_text().splitlines()), 1)


class TheWatcherIsReadOnlyTest(WatchCase):
    def test_docker_only_reads_logs_and_nothing_is_elevated(self):
        self.check(CHECK_FAIL, 1)
        self.run_watch()
        self.run_watch()

        docker = self.calls("docker")
        self.assertTrue(docker)
        for args in docker:
            self.assertTrue(args.startswith("compose logs "), args)
        self.assertEqual(self.calls("sudo"), [])


class LoggingSettingsTest(SimpleTestCase):
    def test_request_errors_go_to_the_console(self):
        # Without this, a 500 under DEBUG=False leaves no line at all:
        # Django's own console handler requires DEBUG=True.
        logger = settings.LOGGING["loggers"]["django.request"]
        self.assertEqual(logger["handlers"], ["console"])
        # Propagating would print each error twice under DEBUG=True.
        self.assertFalse(logger["propagate"])

    def test_the_suite_stays_quiet(self):
        self.assertEqual(
            settings.LOGGING["loggers"]["django.request"]["level"], "CRITICAL"
        )


class ComposeLogLimitsTest(SimpleTestCase):
    # Read as text, like test_config.py: there is no YAML parser in the
    # requirements.
    ANCHOR = (
        "x-logging: &default-logging\n"
        "  driver: local\n"
        "  options:\n"
        '    max-size: "10m"\n'
        '    max-file: "5"\n'
    )

    def services(self, compose):
        block = compose[compose.index("\nservices:\n") :]
        block = block[: block.index("\nvolumes:\n")]
        names, bodies = [], {}
        for line in block.splitlines():
            if (
                line.startswith("  ")
                and not line.startswith("   ")
                and line.endswith(":")
            ):
                names.append(line.strip()[:-1])
                bodies[names[-1]] = []
            elif names:
                bodies[names[-1]].append(line)
        return {name: "\n".join(lines) for name, lines in bodies.items()}

    def test_every_service_in_both_files_is_bounded(self):
        for name in ("docker-compose.yml", "docker-compose.demo.yml"):
            compose = (REPO / name).read_text()
            with self.subTest(compose=name):
                self.assertIn(self.ANCHOR, compose)
                services = self.services(compose)
                self.assertTrue(services)
                for service, body in services.items():
                    with self.subTest(service=service):
                        self.assertIn("    logging: *default-logging", body)


class TheScheduleTest(SimpleTestCase):
    def setUp(self):
        with PLIST.open("rb") as f:
            self.plist = plistlib.load(f)

    def test_label_and_program(self):
        self.assertEqual(self.plist["Label"], "de.ligaauguste.planninghub.watch")
        self.assertEqual(
            self.plist["ProgramArguments"], ["/bin/sh", "__REPO__/host/watch.sh"]
        )

    def test_every_five_minutes_from_the_checkout(self):
        self.assertEqual(self.plist["StartInterval"], 300)
        self.assertEqual(self.plist["WorkingDirectory"], "__REPO__")

    def test_output_is_logged(self):
        self.assertEqual(self.plist["StandardOutPath"], "__LOG__")
        self.assertEqual(self.plist["StandardErrorPath"], "__LOG__")

    def test_it_does_not_run_at_load(self):
        # man launchd.plist: the key "should be avoided". After a reboot the
        # first run comes one interval after login, inside the production
        # check's grace time.
        self.assertNotIn("RunAtLoad", self.plist)


class HostConfigurationStaysOnTheHostTest(SimpleTestCase):
    def test_gitignore_covers_the_ping_urls(self):
        lines = (REPO / ".gitignore").read_text().splitlines()
        self.assertIn("host/watch.env", lines)

    def test_the_example_names_every_setting(self):
        example = (REPO / "host" / "watch.env.example").read_text()
        for setting in ("WATCH_PING_URL", "WATCH_ERRORS_PING_URL", "WATCH_LOG_DIR"):
            with self.subTest(setting=setting):
                self.assertIn(setting, example)


class TheDocTest(AppRecordsMixin, WatchCase):
    def test_every_alert_label_is_explained(self):
        self.set_backup_age(hours=27)
        self.check(CHECK_FAIL, 1)
        self.run_watch()
        self.serve("web", self.every_error())
        self.run_watch()

        bodies = "\n".join(body for body in self.pings().values() if body)
        doc = DOC.read_text()
        for label in ALERT_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, bodies)
                self.assertIn(label, doc)

    def test_the_readme_production_section_links_it(self):
        readme = (REPO / "README.md").read_text()
        start = readme.index("### Docker (production)")
        end = readme.index("## Project structure")
        self.assertIn("docs/monitoring.md", readme[start:end])


class ADeployCollectsTheLogsFirstTest(SimpleTestCase):
    def test_the_watcher_runs_before_the_pull(self):
        skill = (REPO / ".claude" / "skills" / "deploy" / "SKILL.md").read_text()
        watch = skill.index("sh host/watch.sh")
        pull = skill.index("git pull && docker compose")
        self.assertLess(watch, pull)
