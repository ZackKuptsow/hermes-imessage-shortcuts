"""`imessage-questions` posting a question to the texting plugin's listener, over real HTTP.

Runs under any Python: the listener is a stand-in on the loopback address that records what
it was sent and answers as told.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import unittest
from unittest import mock

from support import questions_plugin_module

listener_client = questions_plugin_module("listener_client")
settings_module = questions_plugin_module("settings")

QUESTION = "Which branch should the fix land on?"
SECRET = "q" * 40
SESSION = "dotfiles"


class StandInListener:
    """A loopback HTTP server that records each request and answers with one status."""

    def __init__(self, status: int = 202, body: bytes = b"", location: str | None = None):
        self.requests: list[dict] = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                stand_in.requests.append({
                    "body": self.rfile.read(length), "headers": dict(self.headers.items()),
                    "method": self.command, "path": self.path})
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = answer  # noqa: N815 -- http.server's naming
            do_POST = answer  # noqa: N815 -- http.server's naming

            def log_message(self, *_: object) -> None:
                """Keep the stand-in quiet: the test reads `requests`, not stderr."""

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class PostQuestionTest(unittest.TestCase):
    def listener(self, **answer: object) -> StandInListener:
        stand_in = StandInListener(**answer)
        self.addCleanup(stand_in.close)
        return stand_in

    @staticmethod
    def settings(port: int) -> object:
        return settings_module.AskingSettings(delay_seconds=0, host="127.0.0.1", port=port, secret=SECRET)

    def test_a_question_is_posted_as_json_with_the_secret_to_the_question_route(self):
        stand_in = self.listener()
        listener_client.post_question(self.settings(stand_in.port), QUESTION, SESSION)
        (request,) = stand_in.requests
        self.assertEqual((request["method"], request["path"]), ("POST", "/imessage/question"))
        self.assertEqual(json.loads(request["body"]), {"question": QUESTION, "session": SESSION})
        self.assertEqual(request["headers"]["X-Hermes-Secret"], SECRET)

    def test_any_answer_but_sent_is_an_error_that_names_the_status_and_never_the_question(self):
        for status, body in ((401, b""), (404, b""), (400, b""), (502, b'{"disposition": "not_started"}'), (200, b"")):
            stand_in = self.listener(status=status, body=body)
            with self.subTest(status=status), self.assertRaises(listener_client.QuestionNotSentError) as not_sent:
                listener_client.post_question(self.settings(stand_in.port), QUESTION, SESSION)
            self.assertIn(str(status), str(not_sent.exception))
            for kept_out in (QUESTION, SESSION, SECRET):
                self.assertNotIn(kept_out, str(not_sent.exception))

    def test_with_nothing_listening_the_error_says_the_listener_could_not_be_reached(self):
        with self.assertRaises(listener_client.QuestionNotSentError) as not_sent:
            listener_client.post_question(self.settings(unused_port()), QUESTION, SESSION)
        self.assertIn("could not be reached", str(not_sent.exception))
        self.assertNotIn(SECRET, str(not_sent.exception))

    def test_a_proxy_in_the_environment_is_never_used_so_the_secret_stays_on_this_machine(self):
        stand_in = self.listener()
        proxy = self.listener(status=502)
        proxied = {name: f"http://127.0.0.1:{proxy.port}" for name in ("http_proxy", "HTTP_PROXY", "all_proxy")}
        with mock.patch.dict(os.environ, {**proxied, "no_proxy": "", "NO_PROXY": ""}):
            listener_client.post_question(self.settings(stand_in.port), QUESTION, SESSION)
        self.assertEqual((len(stand_in.requests), proxy.requests), (1, []))

    def test_a_redirect_is_not_followed_so_the_secret_goes_nowhere_else(self):
        elsewhere = self.listener()
        stand_in = self.listener(status=302, location=f"http://127.0.0.1:{elsewhere.port}/imessage/question")
        with self.assertRaises(listener_client.QuestionNotSentError) as not_sent:
            listener_client.post_question(self.settings(stand_in.port), QUESTION, SESSION)
        self.assertIn("302", str(not_sent.exception))
        self.assertEqual(elsewhere.requests, [])

    def test_the_address_brackets_an_ipv6_loopback_host(self):
        self.assertEqual(listener_client.question_url("::1", 8645), "http://[::1]:8645/imessage/question")
        self.assertEqual(listener_client.question_url("127.0.0.1", 9000), "http://127.0.0.1:9000/imessage/question")


if __name__ == "__main__":
    unittest.main()
