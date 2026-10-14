"""Production backups: the backup script, its schedule and the runbook (#300).

The script runs as a subprocess under `sh`, with a stub `docker` first on
PATH. The stub records every call and answers with canned output, so no test
touches Docker or a database. CI's `sh` is dash, so the suite also proves the
script is POSIX: it runs under macOS's `/bin/sh` in production.
"""

import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO = Path(settings.BASE_DIR)
SCRIPT = REPO / "backup" / "backup.sh"
PLIST = REPO / "backup" / "de.ligaauguste.planninghub.backup.plist"

RESTORE_COMMAND = "pg_restore --clean --if-exists --create --exit-on-error"

LIVE_SUMS = "auth_user|aaa\nprojects_plannerrule|bbb\nprojects_weekcloseout|ccc\n"

# Answers by what is being asked, not by call order, so a reordered script
# still gets the right reply. Throwaway-container calls start with `exec`/`run`,
# calls into the live stack with `compose`.
STUB_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  *"printenv POSTGRES_USER"*) echo liga ;;
  *"printenv POSTGRES_DB"*) echo planning_hub ;;
  *pg_dump*)
    printf '%s' "$(cat "$STUB_DIR/dump-bytes")"
    exit "$(cat "$STUB_DIR/dump-exit")" ;;
  run\ *) echo 0123456789ab ;;
  *pg_isready*) ;;
  *pg_restore*)
    cat > "$STUB_DIR/restore-input"
    exit "$(cat "$STUB_DIR/restore-exit")" ;;
  compose\ *psql*) cat > /dev/null; cat "$STUB_DIR/live-sums" ;;
  exec\ *psql*) cat > /dev/null; cat "$STUB_DIR/restored-sums" ;;
  rm\ *) ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 99 ;;
esac
"""


class BackupScriptCase(SimpleTestCase):
    """Runs backup/backup.sh against a stub docker in a scratch directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        docker = self.stub / "docker"
        docker.write_text(STUB_DOCKER)
        docker.chmod(0o755)
        self.backup_dir = self.tmp / "backups"
        self.stub_state(
            dump_bytes="PGDMP-fake-archive",
            dump_exit="0",
            restore_exit="0",
            live_sums=LIVE_SUMS,
            restored_sums=LIVE_SUMS,
        )

    def stub_state(self, **files):
        for name, content in files.items():
            (self.stub / name.replace("_", "-")).write_text(content)

    def run_backup(self, *args, keep=None):
        env = {
            "PATH": f"{self.stub}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "STUB_DIR": str(self.stub),
            "BACKUP_DIR": str(self.backup_dir),
            # Never the host's real settings, whatever machine runs the suite.
            "BACKUP_ENV": str(self.tmp / "no-such-backup.env"),
        }
        if keep is not None:
            env["BACKUP_KEEP"] = str(keep)
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            # Each test reads the exit code itself; a failing run is often the point.
            check=False,
        )

    def calls(self):
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def calls_with(self, needle):
        return [c for c in self.calls() if needle in c]

    def dumps(self):
        if not self.backup_dir.exists():
            return []
        return sorted(p.name for p in self.backup_dir.glob("planning_hub-*.dump"))

    def last_success(self):
        path = self.backup_dir / "last-success"
        return path.read_text() if path.exists() else None

    def assert_failed_cleanly(self, result, last_success_before=None):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.last_success(), last_success_before)
        leftovers = (
            list(self.backup_dir.glob("*.partial")) if self.backup_dir.exists() else []
        )
        self.assertEqual(leftovers, [])


class ARunLeavesADumpTest(BackupScriptCase):
    def test_a_run_leaves_a_timestamped_dump_and_records_it(self):
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [name] = self.dumps()
        self.assertRegex(name, r"^planning_hub-\d{4}-\d{2}-\d{2}_\d{6}\.dump$")
        self.assertEqual((self.backup_dir / name).read_text(), "PGDMP-fake-archive")
        self.assertIn(name, self.last_success())

    def test_the_dump_call_is_binary_safe_and_skips_rebuildable_rows(self):
        self.run_backup()

        [dump] = self.calls_with("pg_dump")
        # Without -T, compose allocates a TTY, and a TTY corrupts a binary stream.
        self.assertTrue(dump.startswith("compose exec -T db "), dump)
        self.assertIn("-Fc", dump)
        self.assertIn("--exclude-table-data=django_session", dump)
        self.assertIn("--exclude-table-data=planning_hub_cache", dump)

    def test_the_run_logs_one_timestamped_line_per_step(self):
        result = self.run_backup()

        lines = result.stdout.splitlines()
        self.assertGreater(len(lines), 2)
        for line in lines:
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} ")


class AFailedDumpLeavesNothingTest(BackupScriptCase):
    def test_a_failed_dump_leaves_nothing_behind(self):
        self.backup_dir.mkdir()
        (self.backup_dir / "last-success").write_text("earlier\n")
        self.stub_state(dump_exit="1")

        result = self.run_backup()

        self.assert_failed_cleanly(result, last_success_before="earlier\n")
        self.assertEqual(self.dumps(), [])


class TheVerificationRestoresTest(BackupScriptCase):
    def test_the_throwaway_gets_no_network_trust_and_the_live_user(self):
        self.run_backup()

        [run] = self.calls_with("run ")
        self.assertIn("--network none", run)
        self.assertIn("POSTGRES_HOST_AUTH_METHOD=trust", run)
        self.assertIn("POSTGRES_USER=liga", run)
        self.assertIn("postgres:16", run)

    def test_the_restore_is_the_runbooks_command(self):
        self.run_backup()

        [restore] = self.calls_with("pg_restore")
        self.assertIn(f"{RESTORE_COMMAND} -U liga -d postgres", restore)
        self.assertEqual(
            (self.stub / "restore-input").read_text(), "PGDMP-fake-archive"
        )

    def test_readiness_is_checked_over_tcp(self):
        self.run_backup()

        # The image's init phase serves only the Unix socket, so a socket
        # check can say "ready" before the real server is up.
        for call in self.calls_with("pg_isready"):
            self.assertIn("-h 127.0.0.1", call)

    def test_both_sides_are_checksummed_in_utc(self):
        self.run_backup()

        psql = self.calls_with("psql")
        self.assertEqual(len(psql), 2)
        for call in psql:
            self.assertIn("PGTZ=UTC", call)

    def test_a_dump_that_does_not_restore_is_not_a_backup(self):
        self.stub_state(restore_exit="1")

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertEqual(self.dumps(), [])

    def test_a_mismatch_names_its_table(self):
        self.stub_state(
            restored_sums=LIVE_SUMS.replace(
                "projects_plannerrule|bbb", "projects_plannerrule|xxx"
            )
        )

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertIn("projects_plannerrule", result.stderr)
        self.assertNotIn("projects_weekcloseout", result.stderr)

    def test_a_table_missing_after_the_restore_is_a_mismatch(self):
        self.stub_state(restored_sums="auth_user|aaa\nprojects_weekcloseout|ccc\n")

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertIn("projects_plannerrule", result.stderr)

    def test_no_tables_compared_is_not_a_match(self):
        self.stub_state(live_sums="", restored_sums="")

        result = self.run_backup()

        self.assert_failed_cleanly(result)


class TheThrowawayIsAlwaysRemovedTest(BackupScriptCase):
    def assert_removed(self):
        [run] = self.calls_with("run ")
        name = re.search(r"--name (\S+)", run).group(1)
        # -v as well: the image's anonymous data volume outlives `rm -f`.
        self.assertIn(f"rm -fv {name}", self.calls())

    def test_after_success(self):
        self.run_backup()
        self.assert_removed()

    def test_after_a_failed_restore(self):
        self.stub_state(restore_exit="1")
        self.run_backup()
        self.assert_removed()

    def test_after_a_mismatch(self):
        self.stub_state(restored_sums="auth_user|zzz\n")
        self.run_backup()
        self.assert_removed()


class VerifyAGivenDumpTest(BackupScriptCase):
    def test_verify_restores_and_compares_without_writing(self):
        dump = self.tmp / "fetched.dump"
        dump.write_text("PGDMP-from-elsewhere")

        result = self.run_backup("--verify", str(dump))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.stub / "restore-input").read_text(), "PGDMP-from-elsewhere"
        )
        self.assertEqual(self.calls_with("pg_dump"), [])
        self.assertFalse(self.backup_dir.exists())

    def test_verify_reports_a_mismatch(self):
        dump = self.tmp / "fetched.dump"
        dump.write_text("PGDMP-from-elsewhere")
        self.stub_state(restored_sums="auth_user|zzz\n")

        result = self.run_backup("--verify", str(dump))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.backup_dir.exists())

    def test_verify_without_a_readable_file_fails(self):
        result = self.run_backup("--verify", str(self.tmp / "missing.dump"))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])


class OptionsAreCheckedTest(BackupScriptCase):
    def test_local_only_is_accepted(self):
        result = self.run_backup("--local-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.dumps()), 1)

    def test_an_unknown_option_fails_and_writes_nothing(self):
        result = self.run_backup("--lokal-only")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--lokal-only", result.stderr)
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(self.calls(), [])


class RetentionTest(BackupScriptCase):
    OLD = [f"planning_hub-2026-01-0{d}_033000.dump" for d in range(1, 6)]

    def seed_old_dumps(self):
        self.backup_dir.mkdir()
        for name in self.OLD:
            (self.backup_dir / name).write_text("old")

    def test_retention_keeps_the_newest_by_name(self):
        self.seed_old_dumps()

        result = self.run_backup(keep=3)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dumps = self.dumps()
        self.assertEqual(len(dumps), 3)
        self.assertEqual(dumps[:2], self.OLD[3:])

    def test_a_failed_run_deletes_nothing(self):
        self.seed_old_dumps()
        self.stub_state(restore_exit="1")

        self.run_backup(keep=3)

        self.assertEqual(self.dumps(), self.OLD)

    def test_partial_files_never_count(self):
        self.seed_old_dumps()
        stray = self.backup_dir / "planning_hub-2099-01-01_000000.dump.partial"
        stray.write_text("aborted")

        self.run_backup(keep=3)

        self.assertEqual(len(self.dumps()), 3)


class TheScheduleTest(SimpleTestCase):
    def setUp(self):
        with PLIST.open("rb") as f:
            self.plist = plistlib.load(f)

    def test_label_and_program(self):
        self.assertEqual(self.plist["Label"], "de.ligaauguste.planninghub.backup")
        self.assertTrue(self.plist["ProgramArguments"][-1].endswith("backup/backup.sh"))

    def test_daily_at_0330(self):
        self.assertEqual(self.plist["StartCalendarInterval"], {"Hour": 3, "Minute": 30})

    def test_output_is_logged(self):
        self.assertIn("StandardOutPath", self.plist)
        self.assertIn("StandardErrorPath", self.plist)

    def test_it_does_not_run_at_load(self):
        # A run at every login or bootstrap would be a backup nobody asked for,
        # and on an install it would race the first manual kickstart.
        self.assertNotIn("RunAtLoad", self.plist)


class HostConfigurationStaysOnTheHostTest(SimpleTestCase):
    def test_gitignore_covers_the_host_settings(self):
        lines = (REPO / ".gitignore").read_text().splitlines()
        self.assertIn("backup/backup.env", lines)

    def test_the_web_image_gets_none_of_it(self):
        lines = (REPO / ".dockerignore").read_text().splitlines()
        self.assertIn("backup/", lines)


class TheRunbookTest(SimpleTestCase):
    def test_the_runbook_holds_the_restore_command(self):
        runbook = (REPO / "docs" / "backup.md").read_text()
        self.assertIn(RESTORE_COMMAND, runbook)

    def test_the_readme_production_section_links_it(self):
        readme = (REPO / "README.md").read_text()
        start = readme.index("### Docker (production)")
        end = readme.index("## Project structure")
        self.assertIn("docs/backup.md", readme[start:end])


class ADeployBacksUpFirstTest(SimpleTestCase):
    def setUp(self):
        self.skill = (REPO / ".claude" / "skills" / "deploy" / "SKILL.md").read_text()

    def test_the_backup_runs_before_the_pull(self):
        backup = self.skill.index("backup/backup.sh --local-only")
        pull = self.skill.index("git pull && docker compose")
        self.assertLess(backup, pull)

    def test_restore_is_no_longer_out_of_scope(self):
        self.assertNotIn("out of scope", self.skill)
        self.assertIn("docs/backup.md", self.skill)
