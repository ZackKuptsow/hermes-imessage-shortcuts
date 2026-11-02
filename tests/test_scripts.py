"""The two scripts, run as commands: what they send, to whom, and what they print."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import NoReturn
from unittest import mock

from support import PARTNER_PHONE, ZACK_EMAIL, ZACK_PHONE, RecordingRun

BIN = Path(__file__).resolve().parent.parent / "bin"
FUNCTION_URL = "https://function.example.invalid/"
HOUSEHOLD_ANSWER = "Added oat milk. The list: oat milk, her prescription refill."
HOUSEHOLD_SECRET = "h" * 64
IMSG_NOT_STARTED = '{"status":"error","disposition":"not_started"}'
SENT_AT = "2026-10-27T13:14:05-04:00"


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


def load_script(name: str) -> ModuleType:
    """A script from bin/ as a module, so its main() can run in this process."""
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class StubResponse(io.BytesIO):
    """urlopen's response as post_command reads it: a readable body with a status."""

    def __init__(self, status: int, body: str):
        super().__init__(body.encode("utf-8"))
        self.status = status


def function_answering(status: int, body: str = "") -> Callable[..., StubResponse]:
    """An opener standing in for urlopen: the function answers *status*, raising for an error as urlopen does."""
    def opener(request, timeout):
        if status >= HTTPStatus.BAD_REQUEST:
            raise urllib.error.HTTPError(request.full_url, status, "refused", hdrs=None, fp=None)
        return StubResponse(status, body)
    return opener


def function_unreachable(request, timeout) -> NoReturn:
    """An opener standing in for urlopen when the function cannot be reached at all."""
    raise urllib.error.URLError("connection refused")


class HouseholdForwardExitCodeTest(ScriptTest):
    """The exit code for each answer the function can give.

    Run in this process, not as a command: the forward posts only to an https URL and reads
    the secret from the login Keychain, neither of which a test can stand in for from outside.
    Only those edges and imsg are replaced; the script's main() and the forward run as shipped.
    """

    def setUp(self):
        super().setUp()
        self.allow(ZACK_PHONE)
        (self.home / "data/imessage/household-function-url").write_text(FUNCTION_URL + "\n")
        self.script = load_script("household-forward")

    def run_main(self, opener: Callable[..., StubResponse], imsg_run: RecordingRun) -> tuple[int, str]:
        """household-forward's main() for one command, against a function answering through *opener*.

        Returns the exit code and everything printed, standard output and standard error together.
        """
        household = self.script.plugin_module("household")
        shipped_forward = household.forward_household_command

        def forward_with_stubbed_edges(text, settings):
            return shipped_forward(text, settings, secret_reader=lambda: HOUSEHOLD_SECRET, opener=opener,
                                   run=imsg_run, clock=lambda: SENT_AT)

        printed = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(household, "forward_household_command", forward_with_stubbed_edges))
            stack.enter_context(mock.patch.dict(os.environ, self.environment))
            stack.enter_context(mock.patch.object(sys, "argv", ["household-forward", "add oat milk"]))
            stack.enter_context(contextlib.redirect_stdout(printed))
            stack.enter_context(contextlib.redirect_stderr(printed))
            exit_code = self.script.main()
        return exit_code, printed.getvalue()

    def test_exits_zero_only_when_delivered_or_there_was_nothing_to_send(self):
        cases = (
            ("an answer, sent", function_answering(200, HOUSEHOLD_ANSWER), RecordingRun(), 0),
            ("no content", function_answering(204), RecordingRun(), 0),
            ("a blank answer", function_answering(200, " \n"), RecordingRun(), 0),
            ("a refusal", function_answering(403), RecordingRun(), 1),
            ("an answer whose send failed", function_answering(200, HOUSEHOLD_ANSWER),
             RecordingRun(stdout=IMSG_NOT_STARTED, returncode=1), 1),
            ("no answer at all: the function is unreachable", function_unreachable, RecordingRun(), 1),
        )
        for case, opener, imsg_run, expected_exit_code in cases:
            with self.subTest(case=case):
                exit_code, printed = self.run_main(opener, imsg_run)
                self.assertEqual(exit_code, expected_exit_code)
                self.assertNotIn("oat milk", printed)


if __name__ == "__main__":
    unittest.main()
