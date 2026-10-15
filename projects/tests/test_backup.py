"""Production backups: the scripts, the schedule and the runbook (#300).

Both scripts run as subprocesses under `sh`. The backup script gets stub
`docker`, `age` and `ssh` first on PATH; they record every call and answer
with canned output, so no test touches Docker, a database, a key or a network.
CI's `sh` is dash, so the suite also proves both scripts are POSIX: the backup
script runs under macOS's `/bin/sh` in production, the receiver under the
VPS's dash.
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
RECEIVER = REPO / "backup" / "receive.sh"
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
  exec\ *psql*) cat > "$STUB_DIR/checksum-sql"; cat "$STUB_DIR/restored-sums" ;;
  rm\ *) ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 99 ;;
esac
"""

# `age -r RECIPIENT -o OUT IN`: writes a recognisable "ciphertext" to OUT.
STUB_AGE = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/age-calls"
out=
while [ $# -gt 1 ]; do
  case $1 in
    -o) out=$2; shift ;;
  esac
  shift
done
[ "$(cat "$STUB_DIR/age-exit")" = 0 ] || exit 1
{ printf 'age-encrypted:'; cat "$1"; } > "$out"
"""

# Stores what arrives on stdin and answers the way the receiver does.
STUB_SSH = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_DIR/ssh-calls"
cat > "$STUB_DIR/ssh-input"
echo "stored planning_hub-2026-10-15_013000.dump.age (42 bytes)"
exit "$(cat "$STUB_DIR/ssh-exit")"
"""

RECIPIENT = "age1testrecipientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
REMOTE = "planninghub-backup@vps.example"


class BackupScriptCase(SimpleTestCase):
    """Runs backup/backup.sh against a stub docker in a scratch directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        for name, body in (
            ("docker", STUB_DOCKER),
            ("age", STUB_AGE),
            ("ssh", STUB_SSH),
        ):
            stub = self.stub / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.backup_dir = self.tmp / "backups"
        self.ssh_key = self.tmp / "backup_ed25519"
        self.ssh_key.write_text("private key")
        self.offsite = {
            "BACKUP_AGE_RECIPIENT": RECIPIENT,
            "BACKUP_REMOTE": REMOTE,
            "BACKUP_SSH_KEY": str(self.ssh_key),
        }
        self.stub_state(
            dump_bytes="PGDMP-fake-archive",
            dump_exit="0",
            restore_exit="0",
            live_sums=LIVE_SUMS,
            restored_sums=LIVE_SUMS,
            age_exit="0",
            ssh_exit="0",
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
            **self.offsite,
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

    def stub_calls(self, name):
        path = self.stub / f"{name}-calls"
        return path.read_text().splitlines() if path.exists() else []

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

    def test_the_run_records_the_restored_checksums_next_to_the_dump(self):
        self.run_backup()

        [name] = self.dumps()
        self.assertEqual((self.backup_dir / f"{name}.sums").read_text(), LIVE_SUMS)

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
        self.assertEqual(list(self.backup_dir.glob("*.sums")), [])


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

    def test_rows_are_sorted_by_byte_order(self):
        self.run_backup()

        # The database's collation comes from the image's libc, and a
        # re-pulled image could sort differently: the .sums would then fail
        # an intact dump on --verify.
        sql = (self.stub / "checksum-sql").read_text()
        self.assertIn('ORDER BY t::text COLLATE "C"', sql)

    def test_a_dump_that_does_not_restore_is_not_a_backup(self):
        self.stub_state(restore_exit="1")

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertEqual(self.dumps(), [])

    def test_a_run_that_fails_the_comparison_records_no_checksums(self):
        self.stub_state(restored_sums="auth_user|zzz\n")

        self.run_backup()

        self.assertEqual(list(self.backup_dir.glob("*.sums")), [])

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
    def fetched_dump(self, sums=LIVE_SUMS):
        dump = self.tmp / "fetched.dump"
        dump.write_text("PGDMP-from-elsewhere")
        if sums is not None:
            (self.tmp / "fetched.dump.sums").write_text(sums)
        return dump

    def test_verify_restores_and_compares_without_writing(self):
        dump = self.fetched_dump()

        result = self.run_backup("--verify", str(dump))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.stub / "restore-input").read_text(), "PGDMP-from-elsewhere"
        )
        self.assertEqual(self.calls_with("pg_dump"), [])
        self.assertFalse(self.backup_dir.exists())

    def test_verify_compares_with_the_recorded_sums_not_with_live(self):
        # The live data has moved on since the dump was taken: a rule edited,
        # a week closed. That says nothing about the dump.
        dump = self.fetched_dump()
        self.stub_state(live_sums="auth_user|aaa\nprojects_plannerrule|new\n")

        result = self.run_backup("--verify", str(dump))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [psql] = self.calls_with("psql")
        self.assertTrue(psql.startswith("exec "), psql)

    def test_verify_reports_a_mismatch_with_the_recorded_sums(self):
        dump = self.fetched_dump()
        self.stub_state(
            restored_sums=LIVE_SUMS.replace(
                "projects_plannerrule|bbb", "projects_plannerrule|xxx"
            )
        )

        result = self.run_backup("--verify", str(dump))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("projects_plannerrule", result.stderr)
        self.assertFalse(self.backup_dir.exists())

    def test_verify_without_recorded_sums_fails_before_any_docker_call(self):
        dump = self.fetched_dump(sums=None)

        result = self.run_backup("--verify", str(dump))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetched.dump.sums", result.stderr)
        self.assertEqual(self.calls(), [])

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


class TheOffsiteCopyTest(BackupScriptCase):
    def test_the_dump_is_encrypted_then_sent(self):
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [name] = self.dumps()
        [age] = self.stub_calls("age")
        self.assertIn(f"-r {RECIPIENT} -o ", age)
        self.assertTrue(age.endswith(f"/{name}"), age)
        [ssh] = self.stub_calls("ssh")
        self.assertIn(f"-i {self.ssh_key}", ssh)
        self.assertIn("-o BatchMode=yes", ssh)
        self.assertIn("-o IdentitiesOnly=yes", ssh)
        self.assertTrue(ssh.endswith(REMOTE), ssh)
        # The ciphertext goes over the wire, never the plain dump.
        self.assertEqual(
            (self.stub / "ssh-input").read_text(), "age-encrypted:PGDMP-fake-archive"
        )

    def test_a_stalled_connection_cannot_hang_the_run(self):
        # launchd starts no new run while one is alive, so a hung upload
        # would stop the local dumps too. TCP keepalive never fires while the
        # peer's kernel still answers; these bound the wait in seconds.
        self.run_backup()

        [ssh] = self.stub_calls("ssh")
        self.assertIn("-o ConnectTimeout=", ssh)
        self.assertIn("-o ServerAliveInterval=", ssh)
        self.assertIn("-o ServerAliveCountMax=", ssh)

    def test_no_pty_is_requested(self):
        # Without -T, ssh logs an untimestamped "Pseudo-terminal will not be
        # allocated" line to stderr, i.e. into backup.log, on every run.
        self.run_backup()

        [ssh] = self.stub_calls("ssh")
        self.assertTrue(ssh.startswith("-T "), ssh)

    def test_the_receivers_answer_is_logged(self):
        result = self.run_backup()

        self.assertIn("stored planning_hub-2026-10-15_013000.dump.age", result.stdout)

    def test_no_ciphertext_is_left_on_the_host(self):
        self.run_backup()

        self.assertEqual(list(self.backup_dir.glob("*.age")), [])

    def test_a_failed_upload_is_a_failed_run(self):
        self.backup_dir.mkdir()
        (self.backup_dir / "last-success").write_text("earlier\n")
        self.stub_state(ssh_exit="1")

        result = self.run_backup()

        self.assert_failed_cleanly(result, last_success_before="earlier\n")
        self.assertIn(REMOTE, result.stderr)
        # The local tier is still good: the dump stays, with its checksums.
        [name] = self.dumps()
        self.assertTrue((self.backup_dir / f"{name}.sums").exists())

    def test_a_failed_encryption_sends_nothing(self):
        self.stub_state(age_exit="1")

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertEqual(self.stub_calls("ssh"), [])

    def test_a_failed_upload_prunes_nothing(self):
        self.backup_dir.mkdir()
        for day in range(1, 6):
            (self.backup_dir / f"planning_hub-2026-01-0{day}_033000.dump").write_text(
                "old"
            )
        self.stub_state(ssh_exit="1")

        self.run_backup(keep=3)

        self.assertEqual(len(self.dumps()), 6)

    def test_a_missing_setting_fails_closed_and_names_it(self):
        # A silent local-only "success" is exactly the failure #303 would
        # never see.
        for setting in self.offsite:
            with self.subTest(setting=setting):
                shutil.rmtree(self.backup_dir, ignore_errors=True)
                saved = self.offsite.pop(setting)
                try:
                    result = self.run_backup()
                finally:
                    self.offsite[setting] = saved

                self.assert_failed_cleanly(result)
                self.assertIn(setting, result.stderr)
                self.assertEqual(self.stub_calls("ssh"), [])

    def test_an_unreadable_key_fails_closed(self):
        self.offsite["BACKUP_SSH_KEY"] = str(self.tmp / "no-such-key")

        result = self.run_backup()

        self.assert_failed_cleanly(result)
        self.assertIn("BACKUP_SSH_KEY", result.stderr)
        self.assertEqual(self.stub_calls("ssh"), [])

    def test_local_only_skips_the_offsite_leg(self):
        self.offsite.clear()

        result = self.run_backup("--local-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stub_calls("age"), [])
        self.assertEqual(self.stub_calls("ssh"), [])

    def test_local_only_does_not_count_as_a_complete_run(self):
        # last-success promises an off-site copy too, so a deploy's backup
        # must not refresh it and hide a nightly off-site leg that is failing.
        self.backup_dir.mkdir()
        (self.backup_dir / "last-success").write_text("earlier\n")

        self.run_backup("--local-only")

        self.assertEqual(self.last_success(), "earlier\n")


class RetentionTest(BackupScriptCase):
    OLD = [f"planning_hub-2026-01-0{d}_033000.dump" for d in range(1, 6)]

    def seed_old_dumps(self):
        self.backup_dir.mkdir()
        for name in self.OLD:
            (self.backup_dir / name).write_text("old")
            (self.backup_dir / f"{name}.sums").write_text("old sums")

    def test_retention_keeps_the_newest_by_name(self):
        self.seed_old_dumps()

        result = self.run_backup(keep=3)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dumps = self.dumps()
        self.assertEqual(len(dumps), 3)
        self.assertEqual(dumps[:2], self.OLD[3:])

    def test_a_pruned_dump_takes_its_checksums_along(self):
        self.seed_old_dumps()

        self.run_backup(keep=3)

        sums = sorted(p.name for p in self.backup_dir.glob("*.sums"))
        self.assertEqual(sums, [f"{name}.sums" for name in self.dumps()])

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


class ReceiverCase(SimpleTestCase):
    """Runs backup/receive.sh the way sshd's forced command would."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.store = self.tmp / "store"
        self.stub = self.tmp / "stub"
        self.stub.mkdir()

    def freeze_clock(self, stamp):
        date = self.stub / "date"
        date.write_text(f"#!/bin/sh\necho {stamp}\n")
        date.chmod(0o755)

    def receive(self, data, keep=None, command="rm -rf /"):
        env = {
            "PATH": f"{self.stub}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "RECEIVE_DIR": str(self.store),
            # What the client asked to run; a forced command must ignore it.
            "SSH_ORIGINAL_COMMAND": command,
        }
        if keep is not None:
            env["RECEIVE_KEEP"] = str(keep)
        return subprocess.run(
            ["sh", str(RECEIVER)],
            input=data,
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def stored(self):
        if not self.store.exists():
            return []
        return sorted(p.name for p in self.store.iterdir())


class TheReceiverTest(ReceiverCase):
    def test_it_names_the_file_by_its_own_clock(self):
        result = self.receive(b"ciphertext", command="planning_hub-1999.dump.age")

        self.assertEqual(result.returncode, 0, result.stderr)
        [name] = self.stored()
        self.assertRegex(name, r"^planning_hub-\d{4}-\d{2}-\d{2}_\d{6}\.dump\.age$")
        self.assertEqual((self.store / name).read_bytes(), b"ciphertext")
        self.assertIn(name, result.stdout.decode())

    def test_it_keeps_binary_data_intact(self):
        data = bytes(range(256)) * 64

        self.receive(data)

        [name] = self.stored()
        self.assertEqual((self.store / name).read_bytes(), data)

    def test_it_rejects_an_empty_upload(self):
        result = self.receive(b"")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stored(), [])

    def test_it_never_reuses_a_name(self):
        self.freeze_clock("2026-10-15_013000")
        self.receive(b"first")

        result = self.receive(b"second")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stored(), ["planning_hub-2026-10-15_013000.dump.age"])
        self.assertEqual(
            (self.store / "planning_hub-2026-10-15_013000.dump.age").read_bytes(),
            b"first",
        )

    def test_it_caps_the_size_at_50_mb(self):
        result = self.receive(b"x" * (50 * 1024 * 1024 + 1))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stored(), [])

    def test_50_mb_itself_is_accepted(self):
        result = self.receive(b"x" * (50 * 1024 * 1024))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.stored()), 1)

    def test_it_prunes_to_the_newest_after_storing(self):
        self.store.mkdir()
        old = [f"planning_hub-2026-01-0{d}_013000.dump.age" for d in range(1, 6)]
        for name in old:
            (self.store / name).write_bytes(b"old")
        self.freeze_clock("2026-10-15_013000")

        self.receive(b"new", keep=3)

        self.assertEqual(
            self.stored(), [*old[3:], "planning_hub-2026-10-15_013000.dump.age"]
        )

    def test_a_rejected_upload_prunes_nothing(self):
        self.store.mkdir()
        old = [f"planning_hub-2026-01-0{d}_013000.dump.age" for d in range(1, 6)]
        for name in old:
            (self.store / name).write_bytes(b"old")

        self.receive(b"", keep=3)

        self.assertEqual(self.stored(), old)


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
