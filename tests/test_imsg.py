"""imsg: one send, to the allow list only, read back into an outcome."""

from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import PARTNER_PHONE, ZACK_EMAIL, ZACK_PHONE, RecordingRun, plugin_module

imsg = plugin_module("imsg")

ALLOWED = (ZACK_PHONE, ZACK_EMAIL)
IMSG_PATH = Path("/opt/homebrew/bin/imsg")


class BuildSendArgumentsTest(unittest.TestCase):
    def test_binds_each_value_to_its_option_and_forces_imessage(self):
        self.assertEqual(
            imsg.build_send_arguments(IMSG_PATH, ZACK_PHONE, "-5 degrees\nsecond line"),
            [str(IMSG_PATH), "send", f"--to={ZACK_PHONE}", "--text=-5 degrees\nsecond line",
             "--service", "imessage", "--json"])


class SendMessageRefusalTest(unittest.TestCase):
    def test_a_handle_off_the_allow_list_is_refused_and_imsg_never_runs(self):
        run = RecordingRun()
        with self.assertRaises(imsg.SendRefusedError):
            imsg.send_message("hi", PARTNER_PHONE, ALLOWED, IMSG_PATH, run=run)
        self.assertEqual(run.calls, [])

    def test_a_contact_name_is_refused_and_imsg_never_runs(self):
        run = RecordingRun()
        with self.assertRaises(imsg.SendRefusedError):
            imsg.send_message("hi", "Zack", ALLOWED, IMSG_PATH, run=run)
        self.assertEqual(run.calls, [])

    def test_an_empty_text_is_refused(self):
        run = RecordingRun()
        with self.assertRaises(imsg.SendRefusedError):
            imsg.send_message("  \n", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run)
        self.assertEqual(run.calls, [])


class SendMessageOutcomeTest(unittest.TestCase):
    def test_sends_to_the_canonical_form_of_an_allowed_handle(self):
        run = RecordingRun()
        outcome = imsg.send_message("hi", "(555) 010-0199", ALLOWED, IMSG_PATH, run=run)
        self.assertTrue(outcome.delivered)
        self.assertEqual(run.option(0, "--to="), ZACK_PHONE)

    def test_a_plain_sent_counts_as_delivered(self):
        outcome = imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=RecordingRun(stdout="sent"))
        self.assertTrue(outcome.delivered)

    def test_a_failure_carries_imsgs_disposition(self):
        run = RecordingRun(stdout='{"status":"error","disposition":"not_started"}', returncode=1)
        outcome = imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run)
        self.assertEqual((outcome.delivered, outcome.disposition), (False, "not_started"))

    def test_exit_zero_without_sent_is_not_delivered(self):
        run = RecordingRun(stdout='{"status":"queued"}')
        self.assertFalse(imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run).delivered)

    def test_sent_with_a_failing_exit_is_not_delivered(self):
        run = RecordingRun(stdout='{"status":"sent"}', returncode=1)
        self.assertFalse(imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run).delivered)

    def test_a_timeout_may_have_completed(self):
        run = RecordingRun(raises=subprocess.TimeoutExpired("imsg", 60))
        outcome = imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run)
        self.assertEqual((outcome.delivered, outcome.disposition), (False, "may_have_completed"))

    def test_an_imsg_that_cannot_start_never_started(self):
        run = RecordingRun(raises=FileNotFoundError(2, "No such file"))
        outcome = imsg.send_message("hi", ZACK_PHONE, ALLOWED, IMSG_PATH, run=run)
        self.assertEqual((outcome.delivered, outcome.disposition), (False, "not_started"))


class FindImsgTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def test_a_configured_executable_is_used(self):
        executable = self.directory / "imsg"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(imsg.find_imsg(str(executable)), executable)

    def test_a_configured_file_that_is_not_executable_is_missing(self):
        plain = self.directory / "imsg"
        plain.write_text("#!/bin/sh\n")
        with self.assertRaises(imsg.ImsgMissingError):
            imsg.find_imsg(str(plain))

    def test_a_configured_path_that_does_not_exist_is_missing(self):
        with self.assertRaises(imsg.ImsgMissingError):
            imsg.find_imsg(str(self.directory / "absent"))


if __name__ == "__main__":
    unittest.main()
