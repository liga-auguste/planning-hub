"""The production host's macOS settings and their check (#309).

host/check.sh runs as a subprocess under `sh` with every command it reads
from stubbed first on PATH. The stubs record their arguments and answer from
a scratch directory, in the shape the real macOS tools print, so no test
reads the machine that runs the suite. CI's `sh` is dash, so the suite also
proves the script is POSIX: it runs under macOS's `/bin/sh` on the host.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO = Path(settings.BASE_DIR)
SCRIPT = REPO / "host" / "check.sh"
DOC = REPO / "docs" / "production-host.md"

DOCKER_SETTINGS = "Library/Group Containers/group.com.docker/settings-store.json"

# `pmset -g` as macOS prints it. "Sleep On Power Button" must not be read as
# `sleep`, nor `disksleep`/`displaysleep`.
PMSET = """System-wide power settings:
Currently in use:
 standby              0
 Sleep On Power Button 1
 autorestart          {autorestart}
 hibernatefile        /var/vm/sleepimage
 powernap             1
 networkoversleep     0
 disksleep            10
 sleep                {sleep}
 ttyskeepawake        1
 displaysleep         10
 tcpkeepalive         1
 womp                 1
"""

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.0.2.1
  interface: {interface}
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""

IFCONFIG = """{interface}: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tether {mac}
\tinet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
\tstatus: active
"""

HARDWARE_MAC = "3c:22:fb:00:00:01"
# 0x3e: the locally administered bit (0x02) is set, as on a private address.
PRIVATE_MAC = "3e:5a:91:00:00:01"

# One stub per command. Each appends "<name> <args>" to `calls` and answers
# from `<name>-out` / `<name>-exit`; the few that answer by argument say so.
STUB = r"""#!/bin/sh
printf '%s %s\n' {name} "$*" >> "$STUB_DIR/calls"
[ -f "$STUB_DIR/{name}-out" ] && cat "$STUB_DIR/{name}-out"
exit "$(cat "$STUB_DIR/{name}-exit" 2>/dev/null || echo 0)"
"""

STUB_ID = r"""#!/bin/sh
printf 'id %s\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  -un) echo stackuser ;;
  -u) echo 501 ;;
  *) echo "stub id: unexpected call: $*" >&2; exit 99 ;;
esac
"""

STUB_IFCONFIG = r"""#!/bin/sh
printf 'ifconfig %s\n' "$*" >> "$STUB_DIR/calls"
if [ -f "$STUB_DIR/ifconfig-$1" ]; then
  cat "$STUB_DIR/ifconfig-$1"
else
  echo "ifconfig: interface $1 does not exist" >&2
  exit 1
fi
"""

STUB_DOCKER = r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "$STUB_DIR/calls"
case "$*" in
  info) exit "$(cat "$STUB_DIR/docker-info-exit")" ;;
  "compose ps -q web") cat "$STUB_DIR/web-id" ;;
  "inspect --format {{.State.Health.Status}} "*) cat "$STUB_DIR/web-health" ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 99 ;;
esac
"""

COMMANDS = (
    "pmset",
    "readlink",
    "fdesetup",
    "defaults",
    "plutil",
    "route",
    "curl",
    "launchctl",
    "sudo",
)

LABELS = (
    "restart after power loss",
    "no idle sleep",
    "time zone Europe/Berlin",
    "FileVault off",
    "automatic login",
    "Docker Desktop at sign-in",
    "hardware MAC on the network",
    "Docker answers",
    "web healthy",
    "nginx answers 401",
    "backup agent loaded",
)


STUB_BIN = None


def setUpModule():
    # Written once: macOS checks every new executable on its first run, which
    # costs seconds per stub. Each test gets its own answers in STUB_DIR.
    global STUB_BIN
    STUB_BIN = Path(tempfile.mkdtemp())
    bodies = {name: STUB.replace("{name}", name) for name in COMMANDS}
    bodies.update(id=STUB_ID, ifconfig=STUB_IFCONFIG, docker=STUB_DOCKER)
    for name, body in bodies.items():
        path = STUB_BIN / name
        path.write_text(body)
        path.chmod(0o755)


def tearDownModule():
    shutil.rmtree(STUB_BIN)


class HostCheckCase(SimpleTestCase):
    """Runs host/check.sh against stubs set up for a host where all is right."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.stub_state(
            pmset_out=PMSET.format(autorestart=1, sleep=0),
            readlink_out="/var/db/timezone/zoneinfo/Europe/Berlin\n",
            fdesetup_out="FileVault is Off.\n",
            defaults_out="stackuser\n",
            plutil_out="true\n",
            route_out=ROUTE.format(interface="en1"),
            ifconfig_en1=IFCONFIG.format(interface="en1", mac=HARDWARE_MAC),
            docker_info_exit="0",
            web_id="0123456789ab\n",
            web_health="healthy\n",
            curl_out="401",
            sudo_exit="1",
        )

    def stub_state(self, **files):
        for name, content in files.items():
            (self.stub / name.replace("_", "-")).write_text(content)

    def drop(self, *names):
        for name in names:
            (self.stub / name).unlink()

    def run_check(self):
        return subprocess.run(
            ["sh", str(SCRIPT)],
            env={
                "PATH": f"{STUB_BIN}:{os.environ['PATH']}",
                "HOME": str(self.tmp),
                "STUB_DIR": str(self.stub),
            },
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=30,
            # Each test reads the exit code itself; a failing run is often the point.
            check=False,
        )

    def calls(self, name):
        path = self.stub / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line[len(name) + 1 :] for line in lines if line.startswith(name + " ")]

    def line_for(self, result, label):
        for line in result.stdout.splitlines():
            if line.startswith((f"ok   {label}", f"FAIL {label}:")):
                return line
        self.fail(f"no line for {label!r} in:\n{result.stdout}{result.stderr}")

    def assert_fails(self, label, fix, found=None):
        result = self.run_check()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        line = self.line_for(result, label)
        self.assertTrue(line.startswith(f"FAIL {label}: "), line)
        self.assertIn(f". Fix: {fix}", line)
        if found is not None:
            self.assertIn(found, line)
        return result


class EverySettingRightTest(HostCheckCase):
    def test_exit_0_and_one_ok_line_per_check(self):
        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), [f"ok   {label}" for label in LABELS]
        )

    def test_dockers_own_settings_file_is_read_without_writing_it(self):
        self.run_check()

        [plutil] = self.calls("plutil")
        # Without `-o -`, plutil writes the extracted value back into the file.
        self.assertIn("-extract AutoStart raw -o - ", plutil)
        self.assertTrue(plutil.endswith(f"{self.tmp}/{DOCKER_SETTINGS}"), plutil)

    def test_the_backup_agent_is_looked_up_in_the_users_gui_domain(self):
        self.run_check()

        self.assertEqual(
            self.calls("launchctl"),
            ["print gui/501/de.ligaauguste.planninghub.backup"],
        )

    def test_the_mac_is_read_from_the_default_routes_interface(self):
        # Moved to Ethernet: en0 carries the route, Wi-Fi's private address
        # on en1 no longer matters.
        self.stub_state(
            route_out=ROUTE.format(interface="en0"),
            ifconfig_en0=IFCONFIG.format(interface="en0", mac=HARDWARE_MAC),
            ifconfig_en1=IFCONFIG.format(interface="en1", mac=PRIVATE_MAC),
        )

        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls("ifconfig"), ["en0"])


class RestartAfterPowerLossTest(HostCheckCase):
    LABEL = "restart after power loss"
    FIX = "sudo pmset -a autorestart 1"

    def test_autorestart_off(self):
        self.stub_state(pmset_out=PMSET.format(autorestart=0, sleep=0))
        self.assert_fails(self.LABEL, self.FIX, found="autorestart 0")

    def test_no_autorestart_line(self):
        # The MacBook's `pmset -g` has none: unreadable counts as wrong.
        self.stub_state(
            pmset_out=PMSET.format(autorestart=1, sleep=0).replace(
                " autorestart          1\n", ""
            )
        )
        self.assert_fails(self.LABEL, self.FIX)

    def test_pmset_failing(self):
        self.stub_state(pmset_out="", pmset_exit="1")
        self.assert_fails(self.LABEL, self.FIX)


class NoIdleSleepTest(HostCheckCase):
    LABEL = "no idle sleep"
    FIX = "sudo pmset -a sleep 0"

    def test_a_sleep_timer(self):
        self.stub_state(pmset_out=PMSET.format(autorestart=1, sleep=10))
        self.assert_fails(self.LABEL, self.FIX, found="sleep 10")

    def test_kept_awake_only_by_caffeinate(self):
        # pmset reports the stored value, annotated by whatever holds it off.
        # The annotation lasts only as long as that process.
        self.stub_state(
            pmset_out=PMSET.format(
                autorestart=1, sleep="1 (sleep prevented by caffeinate, powerd)"
            )
        )
        self.assert_fails(self.LABEL, self.FIX, found="caffeinate")


class TimeZoneTest(HostCheckCase):
    def test_another_zone(self):
        self.stub_state(readlink_out="/var/db/timezone/zoneinfo/America/Los_Angeles\n")
        self.assert_fails(
            "time zone Europe/Berlin",
            "sudo systemsetup -settimezone Europe/Berlin, then restart",
            found="America/Los_Angeles",
        )

    def test_unreadable(self):
        self.stub_state(readlink_out="", readlink_exit="1")
        self.assert_fails(
            "time zone Europe/Berlin",
            "sudo systemsetup -settimezone Europe/Berlin, then restart",
        )

    def test_the_zone_is_read_from_the_symlink(self):
        # `systemsetup -gettimezone` exits 0 on a refusal without admin rights.
        self.run_check()

        self.assertEqual(self.calls("readlink"), ["/etc/localtime"])


class FileVaultTest(HostCheckCase):
    def test_filevault_on(self):
        self.stub_state(fdesetup_out="FileVault is On.\n")
        self.assert_fails(
            "FileVault off",
            "docs/production-host.md, FileVault",
            found="FileVault is On.",
        )

    def test_fdesetup_failing(self):
        self.stub_state(fdesetup_out="", fdesetup_exit="1")
        self.assert_fails("FileVault off", "docs/production-host.md, FileVault")


class AutomaticLoginTest(HostCheckCase):
    LABEL = "automatic login"
    FIX = "System Settings > Users & Groups > Automatically log in as"

    def test_no_automatic_login(self):
        self.stub_state(defaults_out="", defaults_exit="1")
        self.assert_fails(self.LABEL, self.FIX)

    def test_another_user_logs_in(self):
        self.stub_state(defaults_out="someoneelse\n")
        self.assert_fails(self.LABEL, self.FIX, found="someoneelse")


class DockerDesktopAtSignInTest(HostCheckCase):
    LABEL = "Docker Desktop at sign-in"
    FIX = "Docker Desktop > Settings > General > Start Docker Desktop when you sign in"

    def test_switched_off(self):
        self.stub_state(plutil_out="false\n")
        self.assert_fails(self.LABEL, self.FIX, found="false")

    def test_settings_file_unreadable(self):
        self.stub_state(plutil_out="", plutil_exit="1")
        self.assert_fails(self.LABEL, self.FIX)


class HardwareMacTest(HostCheckCase):
    LABEL = "hardware MAC on the network"
    FIX = "System Settings > Wi-Fi > Details > Private Wi-Fi address > Off"

    def test_a_private_address(self):
        self.stub_state(ifconfig_en1=IFCONFIG.format(interface="en1", mac=PRIVATE_MAC))
        self.assert_fails(self.LABEL, self.FIX, found=PRIVATE_MAC)

    def test_no_default_route(self):
        self.stub_state(route_out="", route_exit="1")
        self.assert_fails(self.LABEL, self.FIX)

    def test_no_mac_on_the_interface(self):
        self.drop("ifconfig-en1")
        self.assert_fails(self.LABEL, self.FIX)


class RunningStateTest(HostCheckCase):
    def test_docker_not_answering(self):
        self.stub_state(docker_info_exit="1")
        self.assert_fails("Docker answers", "start Docker Desktop")

    def test_web_unhealthy(self):
        self.stub_state(web_health="unhealthy\n")
        self.assert_fails(
            "web healthy",
            "docker compose up -d, then docker compose logs web",
            found="unhealthy",
        )

    def test_no_web_container(self):
        self.stub_state(web_id="")
        self.assert_fails(
            "web healthy", "docker compose up -d, then docker compose logs web"
        )

    def test_nginx_not_answering(self):
        self.stub_state(curl_out="000", curl_exit="7")
        self.assert_fails(
            "nginx answers 401",
            "docker compose up -d, then docker compose logs nginx",
            found="000",
        )

    def test_nginx_answering_without_basic_auth(self):
        self.stub_state(curl_out="200")
        self.assert_fails(
            "nginx answers 401",
            "docker compose up -d, then docker compose logs nginx",
            found="200",
        )

    def test_backup_agent_not_loaded(self):
        self.stub_state(launchctl_exit="113")
        self.assert_fails("backup agent loaded", "docs/backup.md, Install")


class EveryCheckRunsTest(HostCheckCase):
    def test_a_failed_first_check_does_not_stop_the_rest(self):
        self.stub_state(pmset_out=PMSET.format(autorestart=0, sleep=0))

        result = self.run_check()

        self.assertEqual(result.returncode, 1)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), len(LABELS))
        self.assertTrue(lines[0].startswith("FAIL "), lines[0])
        for line in lines[1:]:
            self.assertTrue(line.startswith("ok   "), line)


class TheCheckIsReadOnlyTest(HostCheckCase):
    def test_nothing_is_set_and_nothing_is_elevated(self):
        self.run_check()

        for args in self.calls("pmset"):
            self.assertTrue(args.startswith("-g"), args)
        for args in self.calls("defaults"):
            self.assertTrue(args.startswith("read "), args)
        for args in self.calls("launchctl"):
            self.assertTrue(args.startswith("print "), args)
        self.assertEqual(self.calls("sudo"), [])


class TheDocListsWhatIsCheckedTest(HostCheckCase):
    def test_every_label_the_script_prints_is_in_the_doc(self):
        result = self.run_check()

        doc = DOC.read_text()
        printed = [line[5:] for line in result.stdout.splitlines()]
        self.assertEqual(len(printed), len(LABELS), result.stdout + result.stderr)
        for label in printed:
            with self.subTest(label=label):
                self.assertIn(label, doc)


class TheDocIsFoundTest(SimpleTestCase):
    def test_the_readme_production_section_links_it(self):
        readme = (REPO / "README.md").read_text()
        start = readme.index("### Docker (production)")
        end = readme.index("## Project structure")
        self.assertIn("docs/production-host.md", readme[start:end])

    def test_the_web_image_gets_none_of_it(self):
        lines = (REPO / ".dockerignore").read_text().splitlines()
        self.assertIn("host/", lines)
