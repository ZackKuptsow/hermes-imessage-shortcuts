"""The two scripts, run as commands: what they send, to whom, and what they print."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import PARTNER_PHONE, ZACK_EMAIL, ZACK_PHONE

BIN = Path(__file__).resolve().parent.parent / "bin"


class ScriptTest(unittest.TestCase):
    def setUp(self):
        # HERMES_HOME is the Hermes root, as in the default profile's terminal; the scripts
        # read the texting profile's files beneath it (ADR 0034).
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "profiles/imessage"
        (self.home / "data/imessage").mkdir(parents=True)
        self.record = self.root / "imsg-calls.jsonl"
        fake_imsg = self.root / "imsg"
        fake_imsg.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "open(os.environ['FAKE_IMSG_RECORD'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(json.dumps({'status': 'sent'}))\n")
        fake_imsg.chmod(fake_imsg.stat().st_mode | stat.S_IXUSR)
        # The real imsg is never reachable from a test: the override names the fake.
        self.environment = {**os.environ, "FAKE_IMSG_RECORD": str(self.record), "HERMES_HOME": str(self.root),
                            "IMESSAGE_SHORTCUTS_IMSG": str(fake_imsg)}

    def allow(self, *handles: str) -> None:
        (self.home / "data/imessage/zack-handles.txt").write_text("\n".join(handles) + "\n")

    def run_script(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(BIN / arguments[0]), *arguments[1:]], input=stdin,
                              capture_output=True, text=True, env=self.environment, timeout=60, check=False)

    def imsg_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []


class ImessageReplyTest(ScriptTest):
    def test_sends_to_the_only_handle_and_prints_sent_not_the_text(self):
        self.allow(ZACK_PHONE)
        completed = self.run_script("imessage-reply", "the build is green")
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "sent"))
        self.assertEqual(self.imsg_calls()[0][:3], ["send", f"--to={ZACK_PHONE}", "--text=the build is green"])

    def test_reads_the_text_from_standard_input(self):
        self.allow(ZACK_PHONE)
        self.assertEqual(self.run_script("imessage-reply", "-", stdin="from stdin").returncode, 0)
        self.assertIn("--text=from stdin", self.imsg_calls()[0])

    def test_refuses_a_recipient_off_the_allow_list(self):
        self.allow(ZACK_PHONE)
        completed = self.run_script("imessage-reply", "--to", PARTNER_PHONE, "hi")
        self.assertEqual((completed.returncode, self.imsg_calls()), (2, []))

    def test_with_two_handles_the_recipient_must_be_named(self):
        self.allow(ZACK_PHONE, ZACK_EMAIL)
        self.assertEqual(self.run_script("imessage-reply", "hi").returncode, 2)
        self.assertEqual(self.run_script("imessage-reply", "--to", ZACK_EMAIL, "hi").returncode, 0)
        self.assertEqual(self.imsg_calls()[0][1], f"--to={ZACK_EMAIL}")

    def test_inside_another_profiles_session_reads_the_texting_profiles_list(self):
        self.allow(ZACK_PHONE)
        self.environment["HERMES_HOME"] = str(self.root / "profiles/coder")
        completed = self.run_script("imessage-reply", "from another profile")
        self.assertEqual((completed.returncode, self.imsg_calls()[0][1]), (0, f"--to={ZACK_PHONE}"))

    def test_the_roots_own_list_is_never_read(self):
        (self.root / "data/imessage").mkdir(parents=True)
        (self.root / "data/imessage/zack-handles.txt").write_text(f"{PARTNER_PHONE}\n")
        completed = self.run_script("imessage-reply", "hi")
        self.assertEqual((completed.returncode, self.imsg_calls()), (2, []))


class HouseholdForwardTest(ScriptTest):
    def test_without_the_functions_url_nothing_is_attempted(self):
        self.allow(ZACK_PHONE)
        completed = self.run_script("household-forward", "add oat milk")
        self.assertEqual((completed.returncode, self.imsg_calls()), (2, []))
        self.assertIn("Not forwarded", completed.stderr)
        self.assertNotIn("oat milk", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
