"""The forward: Zack's command posted as him, the answer sent to his phone, and only the fact
of it reported back."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import ZACK_EMAIL, ZACK_PHONE, RecordingRun, plugin_module, write_allow_list

household = plugin_module("household")

ANSWER = "Added oat milk. The list: oat milk, her prescription refill."
COMMAND = "add oat milk"
HOUSEHOLD_SECRET = "h" * 64
SENT_AT = "2026-10-27T13:14:05-04:00"
FUNCTION_URL = "https://function.example.invalid/"


class StubFunction(BaseHTTPRequestHandler):
    """The inbound function: records each request and answers with the class's status and body."""

    answer_status = 200
    answer_body = ANSWER
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 -- the name http.server calls
        length = int(self.headers.get("Content-Length", "0"))
        StubFunction.requests.append({"body": json.loads(self.rfile.read(length)), "headers": dict(self.headers),
                                      "path": self.path})
        payload = StubFunction.answer_body.encode() if StubFunction.answer_status == 200 else b""
        self.send_response(StubFunction.answer_status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class names it
        """Silent: a test's output is its assertions."""


class ForwardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubFunction)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.stub_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubFunction.requests = []
        StubFunction.answer_status, StubFunction.answer_body = 200, ANSWER
        self.directory = Path(tempfile.mkdtemp())
        url_path = self.directory / "household-function-url"
        url_path.write_text(FUNCTION_URL + "\n")
        self.settings = household.ForwardSettings(
            allow_list_path=write_allow_list(self.directory, ZACK_EMAIL, ZACK_PHONE),
            function_url_path=url_path, imsg_path=Path("/opt/homebrew/bin/imsg"))
        self.imsg_run = RecordingRun()

    def to_stub(self, request: urllib.request.Request, timeout: float):
        """Opener: the same request, sent to the stub over plain HTTP instead of the https URL."""
        self.assertEqual(request.full_url, FUNCTION_URL)
        rerouted = urllib.request.Request(self.stub_url + "/", data=request.data, method=request.get_method(),
                                          headers=dict(request.header_items()))
        return urllib.request.urlopen(rerouted, timeout=timeout)

    def forward(self, command: str = COMMAND, secret_reader=lambda: HOUSEHOLD_SECRET):
        return household.forward_household_command(command, self.settings, secret_reader=secret_reader,
                                                   opener=self.to_stub, run=self.imsg_run, clock=lambda: SENT_AT)

    def test_posts_the_contract_as_zacks_phone_and_texts_him_the_answer(self):
        outcome = self.forward()
        self.assertTrue(outcome.delivered)
        (request,) = StubFunction.requests
        self.assertEqual(request["body"], {"sender": ZACK_PHONE, "sent_at": SENT_AT, "text": COMMAND})
        self.assertEqual(request["headers"]["X-Household-Assistant-Secret"], HOUSEHOLD_SECRET)
        self.assertEqual(request["path"], "/")
        self.assertEqual((self.imsg_run.option(0, "--to="), self.imsg_run.option(0, "--text=")), (ZACK_PHONE, ANSWER))

    def test_the_answer_never_reaches_the_caller(self):
        outcome = self.forward()
        for leaked in ("oat milk", "prescription", HOUSEHOLD_SECRET, "example.invalid"):
            self.assertNotIn(leaked, outcome.summary)

    def test_no_content_sends_nothing(self):
        StubFunction.answer_status = 204
        outcome = self.forward()
        self.assertEqual((outcome.delivered, outcome.status, self.imsg_run.calls), (False, 204, []))

    def test_a_refusal_sends_nothing_and_says_which(self):
        StubFunction.answer_status = 403
        outcome = self.forward()
        self.assertEqual((outcome.delivered, self.imsg_run.calls), (False, []))
        self.assertIn("403", outcome.summary)

    def test_a_failed_send_reports_imsgs_disposition_without_the_answer(self):
        self.imsg_run = RecordingRun(stdout='{"status":"error","disposition":"may_have_completed"}', returncode=1)
        outcome = self.forward()
        self.assertFalse(outcome.delivered)
        self.assertIn("may_have_completed", outcome.summary)
        self.assertNotIn("oat milk", outcome.summary)

    def test_an_unreachable_function_is_reported_without_its_url(self):
        def refuse(request, timeout):
            raise urllib.error.URLError("connection refused to function.example.invalid")

        outcome = household.forward_household_command(COMMAND, self.settings, secret_reader=lambda: HOUSEHOLD_SECRET,
                                                      opener=refuse, run=self.imsg_run, clock=lambda: SENT_AT)
        self.assertEqual((outcome.delivered, outcome.status, self.imsg_run.calls), (False, None, []))
        self.assertNotIn("example.invalid", outcome.summary)

    def test_an_empty_command_posts_nothing(self):
        with self.assertRaises(household.ForwardError):
            self.forward(command="   ")
        self.assertEqual(StubFunction.requests, [])

    def test_a_missing_secret_posts_nothing(self):
        def no_secret() -> str:
            raise household.ForwardError("the login Keychain has no item")

        with self.assertRaises(household.ForwardError):
            self.forward(secret_reader=no_secret)
        self.assertEqual(StubFunction.requests, [])


class ReadFunctionUrlTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "household-function-url"

    def test_an_https_url_gains_its_root_path(self):
        self.path.write_text("https://abc.lambda-url.us-east-1.on.aws\n")
        self.assertEqual(household.read_function_url(self.path), "https://abc.lambda-url.us-east-1.on.aws/")

    def test_anything_but_one_https_url_is_refused(self):
        for content in ("http://abc.example/", "https://a b/", ""):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(household.ForwardError):
                    household.read_function_url(self.path)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(household.ForwardError):
            household.read_function_url(self.path)


class ReadHouseholdSecretTest(unittest.TestCase):
    def test_reads_the_named_item_for_the_account(self):
        run = RecordingRun(stdout=HOUSEHOLD_SECRET + "\n")
        self.assertEqual(household.read_household_secret("the-service", "zack", run=run), HOUSEHOLD_SECRET)
        self.assertEqual(run.calls[0], ["/usr/bin/security", "find-generic-password", "-s", "the-service",
                                        "-a", "zack", "-w"])

    def test_a_missing_item_is_an_error(self):
        with self.assertRaises(household.ForwardError):
            household.read_household_secret("the-service", "zack", run=RecordingRun(stdout="", returncode=44))


if __name__ == "__main__":
    unittest.main()
