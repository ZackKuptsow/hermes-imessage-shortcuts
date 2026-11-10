"""n8n/import.sh and n8n/n8n-cli.sh, sourced under /bin/bash with sudo and n8n stood in for.

import.sh's one promise beyond importing is that the rendered file, which holds handles, is gone
whether or not the import worked. n8n-cli.sh's is that a command run by hand sees what the daemon
sees: the same user, the same data folder, and every switch that keeps n8n from calling home.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ZACK_EMAIL, ZACK_PHONE, write_allow_list

REPOSITORY = Path(__file__).resolve().parent.parent
IMPORT_SCRIPT = REPOSITORY / "n8n" / "import.sh"
CLI_SCRIPT = REPOSITORY / "n8n" / "n8n-cli.sh"
FIXTURE_TEMPLATE = REPOSITORY / "tests" / "fixtures" / "n8n-relay-template.json"
BASH = "/bin/bash"
EXIT_REFUSED = 3
TIMEOUT_SECONDS = 60
RUN_RECORD_TABLE_ID = "kQ3xV9mT2bLw7ZpA"
EXISTING_WORKFLOW = "oLdWoRkFlOw|iMessage relay"
OTHER_WORKFLOW = "oThErWoRkFlOw|Inbound heartbeat"
NEW_WORKFLOW_ID = "nEwWoRkFlOwId123"
DAEMON_ENVIRONMENT = {"N8N_DIAGNOSTICS_ENABLED": "false", "N8N_LOG_OUTPUT": "file", "N8N_USER_FOLDER": "/var/n8n",
                      "EXTERNAL_FRONTEND_HOOKS_URLS": ""}

# Sudo drops `-u _n8n` and runs the command as whoever runs the test, in the directory it was given.
IMPORT_STAND_INS = rf"""
calls="$SCRATCH/calls"
record() {{ printf '%s\n' "$*" >>"$calls"; }}
sudo() {{ record sudo "$@"; shift 2; "$@"; }}
run_n8n() {{
  record run_n8n "$@"
  case "$1" in
    list:workflow) if [[ -f "$SCRATCH/workflows" ]]; then cat "$SCRATCH/workflows"; fi ;;
    import:workflow)
      cp "${{2#--input=}}" "$SCRATCH/seen-by-n8n.json"
      [[ ! -f "$SCRATCH/import-fails" ]] || return 1
      [[ -f "$SCRATCH/import-adds-nothing" ]] || printf '%s\n' '{NEW_WORKFLOW_ID}|iMessage relay' >>"$SCRATCH/workflows"
      ;;
  esac
}}
N8N_HOME="$SCRATCH/n8n-home"
HANDLE_FILE="$SCRATCH/zack-handles.txt"
TEMPLATE="{FIXTURE_TEMPLATE}"
"""
CLI_STAND_INS = r"""
sudo() {
  { printf '%s\n' "$PWD"; printf '%s\n' "$@"; } >"$SCRATCH/sudo-call"
  return "${SUDO_EXIT:-0}"
}
N8N_HOME="$SCRATCH"
DAEMON_PLIST="$SCRATCH/daemon.plist"
"""


class ShellTest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def run_shell(self, source: Path, stand_ins: str, body: str, **environment: str) -> subprocess.CompletedProcess[str]:
        script = "\n".join((f'source "{source}"', stand_ins, body))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
                              env={**os.environ, "SCRATCH": str(self.scratch), **environment})


class ImportTest(ShellTest):
    def setUp(self):
        super().setUp()
        (self.scratch / "n8n-home").mkdir()
        (self.scratch / "private-temporary").mkdir()
        write_allow_list(self.scratch, ZACK_PHONE, ZACK_EMAIL)

    def run_import(self, arguments: str = RUN_RECORD_TABLE_ID) -> subprocess.CompletedProcess[str]:
        return self.run_shell(IMPORT_SCRIPT, IMPORT_STAND_INS, f"main {arguments}",
                              TMPDIR=str(self.scratch / "private-temporary"))

    def flag(self, name: str) -> None:
        (self.scratch / name).touch()

    def calls(self) -> list[str]:
        path = self.scratch / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def leftovers(self) -> list[Path]:
        """Anything left where a rendered copy was: n8n's home, and the private temporary folder."""
        return [*(self.scratch / "n8n-home").iterdir(), *(self.scratch / "private-temporary").iterdir()]

    def imported_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("run_n8n import:workflow")]

    def test_n8n_is_handed_the_rendered_workflow(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = (self.scratch / "seen-by-n8n.json").read_text(encoding="utf-8")
        self.assertIn(ZACK_PHONE, seen)
        self.assertIn(RUN_RECORD_TABLE_ID, seen)
        self.assertEqual(json.loads(seen)["name"], "iMessage relay")

    def test_the_rendered_copies_are_gone_after_a_good_import(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_the_rendered_copies_are_gone_after_a_failed_import(self):
        self.flag("import-fails")
        result = self.run_import()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.scratch / "seen-by-n8n.json").exists(), "the import must have been attempted")
        self.assertIn("could not import", result.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_the_copy_n8n_reads_is_written_by_n8n_and_removed_by_n8n(self):
        # /var/n8n is _n8n's, so root writing or deleting there could be pointed elsewhere by a link.
        self.run_import()
        sudo_calls = [call for call in self.calls() if call.startswith("sudo ")]
        self.assertTrue(sudo_calls, "the hand-over must go through sudo")
        for call in sudo_calls:
            self.assertTrue(call.startswith("sudo -u _n8n "), f"not run as _n8n: {call}")

    def test_an_import_that_adds_nothing_is_a_failure(self):
        # A policy skip logs and exits 0, so the list is what says whether anything came in.
        self.flag("import-adds-nothing")
        result = self.run_import()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.leftovers(), [])

    def test_a_relay_already_there_is_refused_before_anything_is_rendered(self):
        # Two copies would hold one webhook path between them, and only one could be published.
        (self.scratch / "workflows").write_text(f"{EXISTING_WORKFLOW}\n", encoding="utf-8")
        result = self.run_import()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertEqual(self.imported_calls(), [])
        self.assertFalse((self.scratch / "seen-by-n8n.json").exists())
        self.assertEqual(self.leftovers(), [])

    def test_another_workflow_being_there_is_no_reason_to_refuse(self):
        (self.scratch / "workflows").write_text(f"{OTHER_WORKFLOW}\n", encoding="utf-8")
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NEW_WORKFLOW_ID, result.stdout)
        self.assertNotIn("oThErWoRkFlOw", result.stdout)

    def test_it_says_the_new_workflow_needs_publishing(self):
        # An imported workflow always arrives unpublished, and its webhook answers nothing until then.
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NEW_WORKFLOW_ID, result.stdout)
        self.assertIn("Publish", result.stdout)

    def test_a_handle_file_render_refuses_imports_nothing(self):
        write_allow_list(self.scratch, "# nobody yet")
        result = self.run_import()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("render.py refused", result.stderr)
        self.assertEqual(self.imported_calls(), [])
        self.assertEqual(self.leftovers(), [])

    def test_no_table_id_is_refused_with_its_usage(self):
        result = self.run_import("")
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("usage", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_list_n8n_cannot_answer_stops_the_run(self):
        # Otherwise a failed read would look like an instance with no relay in it.
        result = self.run_shell(IMPORT_SCRIPT, IMPORT_STAND_INS, f"run_n8n() {{ return 1; }}\nmain {RUN_RECORD_TABLE_ID}",
                                TMPDIR=str(self.scratch / "private-temporary"))
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("could not list", result.stderr)
        self.assertEqual(self.calls(), [], "nothing may be handed to n8n after a list that failed")
        self.assertEqual(self.leftovers(), [])

    def test_nothing_it_prints_holds_a_handle(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        for value in (ZACK_PHONE, ZACK_EMAIL):
            self.assertNotIn(value, result.stdout + result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root is what this test is not")
    def test_run_with_no_arguments_as_a_script_it_stops_at_its_usage(self):
        result = subprocess.run([BASH, str(IMPORT_SCRIPT)], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("usage", result.stderr)


class CliTest(ShellTest):
    def setUp(self):
        super().setUp()
        with (self.scratch / "daemon.plist").open("wb") as plist_file:
            plistlib.dump({"EnvironmentVariables": DAEMON_ENVIRONMENT, "Label": "stand-in"}, plist_file)

    def run_cli(self, body: str = "run_n8n list:workflow", **environment: str) -> subprocess.CompletedProcess[str]:
        return self.run_shell(CLI_SCRIPT, CLI_STAND_INS, body, **environment)

    def sudo_call(self) -> tuple[str, list[str]]:
        directory, *arguments = (self.scratch / "sudo-call").read_text(encoding="utf-8").splitlines()
        return directory, arguments

    def test_it_runs_as_n8n_with_the_daemons_node_and_n8n(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        _, arguments = self.sudo_call()
        self.assertEqual(arguments[:2], ["-u", "_n8n"])
        self.assertEqual(arguments[-3:], ["/opt/homebrew/bin/node", "/var/n8n/npm/lib/node_modules/n8n/bin/n8n", "list:workflow"])

    def test_it_runs_in_n8ns_home_since_n8n_cannot_enter_yours(self):
        self.run_cli()
        directory, _ = self.sudo_call()
        self.assertEqual(Path(directory).resolve(), self.scratch.resolve())

    def test_it_sees_every_variable_the_daemon_does_and_nothing_inherited(self):
        result = self.run_cli(LEAKED_VARIABLE="should-not-pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        _, arguments = self.sudo_call()
        self.assertEqual(arguments[2:4], ["/usr/bin/env", "-i"])
        for name, value in DAEMON_ENVIRONMENT.items():
            if name != "N8N_LOG_OUTPUT":
                self.assertIn(f"{name}={value}", arguments)
        self.assertFalse([argument for argument in arguments if "LEAKED_VARIABLE" in argument])

    def test_its_log_goes_to_the_terminal_rather_than_the_daemons_file(self):
        # env takes the last value it is given, so the override has to come after the daemon's.
        self.run_cli()
        _, arguments = self.sudo_call()
        settings = [argument for argument in arguments if argument.startswith("N8N_LOG_OUTPUT=")]
        self.assertEqual(settings[-1], "N8N_LOG_OUTPUT=console")

    def test_a_failure_of_the_command_is_passed_on(self):
        result = self.run_cli(SUDO_EXIT="7")
        self.assertEqual(result.returncode, 7, result.stdout)

    def test_no_daemon_plist_is_refused_before_sudo(self):
        (self.scratch / "daemon.plist").unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("is missing", result.stderr)
        self.assertFalse((self.scratch / "sudo-call").exists())

    def test_a_plist_with_no_environment_is_refused_before_sudo(self):
        with (self.scratch / "daemon.plist").open("wb") as plist_file:
            plistlib.dump({"Label": "stand-in"}, plist_file)
        result = self.run_cli()
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertFalse((self.scratch / "sudo-call").exists())

    def test_run_with_no_arguments_as_a_script_it_stops_at_its_usage(self):
        result = subprocess.run([BASH, str(CLI_SCRIPT)], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
