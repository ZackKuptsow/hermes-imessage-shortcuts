"""n8n/install.sh, step by step, sourced under /bin/bash with root's commands stood in for.

install.sh runs as root on the one Mac it is for, so no test runs it whole. Sourcing it runs
nothing, since main runs only when the file is executed, and a shell function named for a
command stands in for that command: launchctl, curl and lsof answer as a test tells them and
record what they were asked, and sudo and the directory tools refuse, so nothing a test does
can reach the Mac's users or its LaunchDaemons. Every path the script writes points into a
scratch directory, and every user and group it names is whoever runs the test.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL_SCRIPT = Path(__file__).resolve().parent.parent / "n8n" / "install.sh"
# The shell in the script's shebang as sudo finds it, with no Homebrew on its PATH.
BASH = "/bin/bash"
DAEMON_LABEL = "com.zack.hermes-imessage.n8n"
DAEMON_SERVICE = f"system/{DAEMON_LABEL}"
EXIT_REFUSED = 3
EXIT_UNHEALTHY = 5
LOG_FILE_MODE = 0o640
STARTING_MODE = 0o600
TIMEOUT_SECONDS = 60
INSTALLED_VERSION = "2.45.6"
# Each step main takes before it checks the running daemon, in its order.
STEPS_BEFORE_THE_DAEMON_CHECK = (
    "check_machine", "check_ports", "ensure_group", "ensure_user", "check_isolation", "ensure_directory",
    "ensure_n8n", "ensure_log_files", "ensure_daemon_plist", "start_daemon",
)
DAEMON_CHECK = "check_daemon"
# Sourced after install.sh: its commands stood in for, its paths moved into $SCRATCH.
STAND_INS = r"""
calls="$SCRATCH/calls"
record() { printf '%s\n' "$*" >>"$calls"; }
launchctl() { record launchctl "$@"; }
sudo() { record sudo "$@"; return 1; }
dscl() { record dscl "$@"; return 1; }
dseditgroup() { record dseditgroup "$@"; return 1; }
dsmemberutil() { record dsmemberutil "$@"; return 1; }
curl() { printf '%s' "${HEALTH_STATUS:-200}"; }
lsof() { if [[ -f "$SCRATCH/listeners" ]]; then cat "$SCRATCH/listeners"; fi; }
sleep() { :; }
N8N_USER="$(id -un)"
N8N_GROUP="$(id -gn)"
ADMIN_GROUP="$(id -gn)"
ROOT_USER="$(id -un)"
WHEEL_GROUP="$(id -gn)"
N8N_HOME="$SCRATCH/home"
N8N_PACKAGE_JSON="$SCRATCH/home/npm/lib/node_modules/n8n/package.json"
LOG_FILES=("$SCRATCH/log/n8n.log")
DAEMON_PATH="$SCRATCH/LaunchDaemons/com.zack.hermes-imessage.n8n.plist"
EGRESS_WATCH_PIDFILE="$SCRATCH/egress-watch.pid"
RELOAD_PENDING_MARKER="$SCRATCH/reload-pending"
RESTART_PENDING_MARKER="$SCRATCH/restart-pending"
"""


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def run_install(self, body: str, *, stubbed: tuple[str, ...] = (), recorded: tuple[str, ...] = ()
                    ) -> subprocess.CompletedProcess[str]:
        """Source install.sh and run *body*; *stubbed* steps do nothing, *recorded* ones say they ran."""
        steps = [*(f"{step}() {{ :; }}" for step in stubbed), *(f"{step}() {{ record {step}; }}" for step in recorded)]
        script = "\n".join((f'source "{INSTALL_SCRIPT}"', STAND_INS, *steps, body))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                              check=False, env={**os.environ, "SCRATCH": str(self.scratch)})

    def calls(self) -> list[str]:
        path = self.scratch / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def listen_on(self, *addresses: str) -> None:
        """What lsof's -F output names as _n8n's listening sockets."""
        lines = [line for address in addresses for line in ("p4242", f"n{address}")]
        (self.scratch / "listeners").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def mode_of(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


class InstalledVersionTest(InstallScriptTest):
    def setUp(self):
        super().setUp()
        self.package_json = self.scratch / "home/npm/lib/node_modules/n8n/package.json"
        self.package_json.parent.mkdir(parents=True)

    def test_the_version_is_read_from_package_json(self):
        self.package_json.write_text(f'{{"name": "n8n", "version": "{INSTALLED_VERSION}"}}\n', encoding="utf-8")
        result = self.run_install("installed_n8n_version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, INSTALLED_VERSION), result.stderr)

    def test_reading_the_version_runs_nothing_n8n_left_in_its_place(self):
        # _n8n owns the tree, so it can make package.json a link to code; root must not run it.
        planted = self.scratch / "planted.js"
        planted.write_text('require("fs").writeFileSync(process.env.SCRATCH + "/ran", "");\n'
                           'module.exports = {version: "planted"};\n', encoding="utf-8")
        self.package_json.symlink_to(planted)
        result = self.run_install("installed_n8n_version")
        self.assertFalse((self.scratch / "ran").exists(), "root ran the file package.json links to")
        self.assertNotEqual(result.returncode, 0, result.stdout)


class LogFileTest(InstallScriptTest):
    """Root changes these files' group and mode inside a directory _n8n owns."""

    def setUp(self):
        super().setUp()
        (self.scratch / "log").mkdir()
        self.log_file = self.scratch / "log/n8n.log"
        self.elsewhere = self.scratch / "elsewhere"
        self.elsewhere.write_text("", encoding="utf-8")
        self.elsewhere.chmod(STARTING_MODE)

    def test_a_log_file_of_another_mode_is_set_to_640(self):
        self.log_file.write_text("", encoding="utf-8")
        self.log_file.chmod(STARTING_MODE)
        result = self.run_install("ensure_log_files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mode_of(self.log_file), LOG_FILE_MODE)

    def test_a_missing_log_file_is_made(self):
        result = self.run_install("ensure_log_files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mode_of(self.log_file), LOG_FILE_MODE)

    def test_a_log_file_that_links_elsewhere_is_refused_and_its_target_left_alone(self):
        self.log_file.symlink_to(self.elsewhere)
        result = self.run_install("ensure_log_files")
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertEqual(self.mode_of(self.elsewhere), STARTING_MODE)

    def test_a_log_file_linking_to_nothing_is_refused(self):
        self.log_file.symlink_to(self.scratch / "nothing")
        result = self.run_install("ensure_log_files")
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertFalse((self.scratch / "nothing").exists())

    def test_a_log_file_with_a_second_name_is_refused_and_left_alone(self):
        os.link(self.elsewhere, self.log_file)
        result = self.run_install("ensure_log_files")
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertEqual(self.mode_of(self.elsewhere), STARTING_MODE)

    def test_a_log_file_another_user_owns_is_refused_and_left_alone(self):
        self.log_file.write_text("", encoding="utf-8")
        self.log_file.chmod(STARTING_MODE)
        result = self.run_install('N8N_USER="nobody"\nensure_log_files')
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertEqual(self.mode_of(self.log_file), STARTING_MODE)


class DaemonCheckTest(InstallScriptTest):
    """main's last step: n8n answers on 127.0.0.1 and listens nowhere else."""

    def run_daemon_check(self, health_status: str = "200", launchctl: str = "") -> subprocess.CompletedProcess[str]:
        return self.run_install(f"HEALTH_STATUS={health_status}\nHEALTH_WAIT_SECONDS=4\n{launchctl}\nmain",
                                stubbed=STEPS_BEFORE_THE_DAEMON_CHECK)

    def assert_disabled_then_stopped(self) -> None:
        self.assertEqual(self.calls(), [f"launchctl disable {DAEMON_SERVICE}", f"launchctl bootout {DAEMON_SERVICE}"])

    def test_n8n_on_the_loopback_address_alone_passes(self):
        self.listen_on("127.0.0.1:5678", "127.0.0.1:5679")
        result = self.run_daemon_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_listener_beyond_the_loopback_address_is_disabled_and_stopped(self):
        # Disabled, so a restart of the Mac cannot start it again before a run checks it.
        self.listen_on("127.0.0.1:5678", "*:5679")
        result = self.run_daemon_check()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("*:5679", result.stderr)
        self.assert_disabled_then_stopped()

    def test_an_n8n_that_never_answers_is_stopped_when_it_listens_beyond_the_loopback_address(self):
        self.listen_on("192.168.1.20:5678")
        result = self.run_daemon_check(health_status="000")
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assert_disabled_then_stopped()

    def test_an_n8n_that_never_answers_on_the_loopback_address_alone_is_unhealthy(self):
        self.listen_on("127.0.0.1:5679")
        result = self.run_daemon_check(health_status="000")
        self.assertEqual(result.returncode, EXIT_UNHEALTHY, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_an_n8n_holding_no_listener_is_refused(self):
        result = self.run_daemon_check()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)

    def test_a_bootout_that_fails_says_n8n_may_still_listen(self):
        self.listen_on("*:5678")
        result = self.run_daemon_check(launchctl='launchctl() { record launchctl "$@"; [[ "$1" != bootout ]]; }')
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("could not stop", result.stderr)


class PendingWorkTest(InstallScriptTest):
    """A run that stops after changing a file leaves the reload or restart to the next."""

    def setUp(self):
        super().setUp()
        (self.scratch / "LaunchDaemons").mkdir()
        self.package_json = self.scratch / "home/npm/lib/node_modules/n8n/package.json"
        self.package_json.parent.mkdir(parents=True)
        self.listen_on("127.0.0.1:5678")
        self.bootstrap = f"launchctl bootstrap system {self.scratch}/LaunchDaemons/{DAEMON_LABEL}.plist"

    def steps_except(self, *taken: str) -> tuple[str, ...]:
        return tuple(step for step in (*STEPS_BEFORE_THE_DAEMON_CHECK, DAEMON_CHECK) if step not in taken)

    def forget_calls(self) -> None:
        (self.scratch / "calls").unlink()

    def test_a_plist_written_by_a_run_that_stopped_is_loaded_by_the_next(self):
        steps = self.steps_except("ensure_daemon_plist", "start_daemon")
        stopped = self.run_install('start_daemon() { refuse "stopped before launchd was told"; }\nmain', stubbed=steps)
        self.assertEqual(stopped.returncode, EXIT_REFUSED, stopped.stdout)
        # Loaded until it is booted out.
        loaded = "daemon_loaded() { ! grep -q '^launchctl bootout' \"$calls\" 2>/dev/null; }\nmain"
        result = self.run_install(loaded, stubbed=steps)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"launchctl bootout {DAEMON_SERVICE}", f"launchctl enable {DAEMON_SERVICE}",
                                        self.bootstrap])
        self.forget_calls()
        again = self.run_install(loaded, stubbed=steps)
        self.assertEqual((again.returncode, self.calls()), (0, []), again.stderr)

    def test_an_n8n_installed_by_a_run_that_stopped_is_restarted_by_the_next(self):
        self.package_json.write_text(f'{{"version": "{INSTALLED_VERSION}"}}\n', encoding="utf-8")
        npm = 'sudo() { record sudo; printf \'{"version": "%s"}\\n\' "$N8N_VERSION" >"$N8N_PACKAGE_JSON"; }'
        stopped = self.run_install(f'{npm}\nensure_log_files() {{ refuse "stopped after npm"; }}\nmain',
                                   stubbed=self.steps_except("ensure_n8n", "ensure_log_files"))
        self.assertEqual(stopped.returncode, EXIT_REFUSED, stopped.stdout)
        self.forget_calls()
        steps = self.steps_except("ensure_n8n", "start_daemon")
        result = self.run_install("daemon_loaded() { return 0; }\nmain", stubbed=steps)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"launchctl kickstart -k {DAEMON_SERVICE}"])
        self.forget_calls()
        again = self.run_install("daemon_loaded() { return 0; }\nmain", stubbed=steps)
        self.assertEqual((again.returncode, self.calls()), (0, []), again.stderr)

    def test_a_start_enables_the_daemon_before_it_loads_it(self):
        # A run that stopped n8n for listening beyond the loopback address disabled it.
        result = self.run_install("daemon_loaded() { return 1; }\negress_watch_running() { return 0; }\nmain",
                                  stubbed=self.steps_except("start_daemon"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[:2], [f"launchctl enable {DAEMON_SERVICE}", self.bootstrap])


class IsolationTest(InstallScriptTest):
    """`_n8n` must not be able to list or enter the home of whoever runs the script."""

    # Drops `-u _n8n` and runs the probe as whoever runs the test.
    PASSING_SUDO = 'sudo() { shift 2; "$@"; }'

    def setUp(self):
        super().setUp()
        self.home = self.scratch / "zack"
        self.home.mkdir()
        self.addCleanup(self.home.chmod, stat.S_IRWXU)

    def check_isolation(self, home_mode: int, sudo: str = PASSING_SUDO) -> subprocess.CompletedProcess[str]:
        # The test runs as the home's owner, so the owner's bits stand for what _n8n may do.
        self.home.chmod(home_mode)
        return self.run_install(f'SUDO_USER=zack\ndirectory_record() {{ printf "%s\\n" "$SCRATCH/zack"; }}\n'
                                f"{sudo}\ncheck_isolation")

    def test_a_home_n8n_can_neither_list_nor_enter_passes(self):
        result = self.check_isolation(0)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_home_n8n_can_list_is_refused(self):
        result = self.check_isolation(stat.S_IRWXU)
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)

    def test_a_home_n8n_can_enter_but_not_list_is_refused(self):
        # Entering is enough to read any file inside whose own mode lets others read it.
        result = self.check_isolation(stat.S_IWUSR | stat.S_IXUSR)
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)

    def test_a_probe_that_cannot_run_as_n8n_is_refused(self):
        # Otherwise every failure of sudo itself would read as isolation.
        result = self.check_isolation(0, sudo='sudo() { return 1; }')
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)


class OrderTest(InstallScriptTest):
    def test_a_port_another_user_holds_is_refused_before_anything_is_made(self):
        making = tuple(step for step in STEPS_BEFORE_THE_DAEMON_CHECK if step.startswith("ensure_"))
        result = self.run_install('check_ports() { refuse "UID 501 already listens on port 5678"; }\nmain',
                                  stubbed=("check_machine", "check_isolation"), recorded=making)
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
