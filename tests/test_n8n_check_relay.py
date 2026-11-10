"""n8n/check_relay.py, whose only job is to go red when a gate is broken.

The check itself runs at the Mac against the real n8n, which no test here can reach. So its logic
is driven against a fake n8n in this file: a small HTTP server doing what the relay is designed to
do, which can be told to misbehave in each way the check exists to catch. A check that stayed
green against any of those would prove nothing when it stays green against the real one. What is
left for the Mac is whether the real relay behaves like the fake: that is what running it shows.

The copy of the relay it imports, the stand-in plugin it aims that copy at, and its commands against
n8n's database are tested beside this file, in test_n8n_check_relay_*.py.
"""

from __future__ import annotations

import gc
import http.server
import json
import os
import signal
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field

from n8n_check_support import HTTP_REQUEST_TYPE, N8N_DIRECTORY, RUN_RECORD_TABLE_ID, check_relay, nodes_of_type, template

FAST = check_relay.Timing(row_wait_seconds=1.0, settle_seconds=0.02, poll_seconds=0.01, slow_answer_seconds=0.3,
                          webhook_wait_seconds=0.3)
FAKE_FORWARD_TIMEOUT_SECONDS = 0.15
FORBIDDEN_STATUS = 403
UNAUTHORIZED_STATUS = 401
RECEIVED_STATUS = 200
WEBHOOK_NOT_REGISTERED_STATUS = 404


@dataclass
class FakeRelay:
    """What each breakage does to the fake, one flag for each thing the check must notice."""

    answers_without_a_secret: bool = False
    answers_a_wrong_secret: bool = False
    forwards_everyone: bool = False
    drops_everyone: bool = False
    alters_the_body: bool = False
    writes_no_rows: bool = False
    writes_a_row_for_refused_posts: bool = False
    writes_the_wrong_outcome: bool = False
    writes_no_status: bool = False
    writes_text_into_a_row: bool = False
    omits_the_plugin_secret: bool = False
    refuses_with_the_wrong_status: bool = False
    writes_two_rows: bool = False
    writes_the_wrong_route: bool = False
    writes_no_received_at: bool = False
    writes_no_duration: bool = False
    forwards_twice: bool = False


class FakeN8n:
    """Stands in for n8n and the instance the check talks to: the webhook, its rows and its leftovers."""

    def __init__(self, breakage: FakeRelay | None = None, webhook_ready_after: int = 0, starting_answers: int = 0):
        self.breakage = breakage or FakeRelay()
        self.rows_written: list[dict] = []
        self.events: list[str] = []
        self.has_leftovers = False
        self.webhook_ready_after = webhook_ready_after
        self.starting_answers = starting_answers
        self.installed: tuple[dict, list[dict]] | None = None
        # As the real one: nothing answers at the path until the workflow is published.
        self.webhook_registered = False
        self.relay_secret = ""
        self.plugin_secret = ""
        self.forward_url = ""
        self.fails_to_clean = False
        self.fails_to_install = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 -- http.server's name
                fake.handle_post(self)

            def log_message(self, *_: object) -> None:
                """Silent: a test's output is its assertions."""

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(check_relay.STAND_IN_POLL_SECONDS,), daemon=True)
        self.thread.start()

    @property
    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/webhook/check"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def handle_post(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", 0))
        body = handler.rfile.read(length)
        if self.starting_answers > 0:
            # As n8n does before its migrations finish: 200 to anything, webhooks or not.
            self.starting_answers -= 1
            self.respond(handler, RECEIVED_STATUS)
            return
        if self.webhook_ready_after > 0:
            self.webhook_ready_after -= 1
            self.respond(handler, WEBHOOK_NOT_REGISTERED_STATUS)
            return
        if not self.webhook_registered:
            self.respond(handler, WEBHOOK_NOT_REGISTERED_STATUS)
            return
        presented = handler.headers.get(check_relay.RELAY_SECRET_HEADER)
        if presented is None and not self.breakage.answers_without_a_secret or (
                presented is not None and presented != self.relay_secret and not self.breakage.answers_a_wrong_secret):
            if self.breakage.writes_a_row_for_refused_posts:
                self.write_row("dropped", "dropped", None)
            # A post with no secret is what tells the check the webhook is up, so this one stays 403.
            wrong_status = self.breakage.refuses_with_the_wrong_status and presented is not None
            self.respond(handler, UNAUTHORIZED_STATUS if wrong_status else FORBIDDEN_STATUS)
            return
        self.respond(handler, RECEIVED_STATUS)
        self.route(json.loads(body))

    def respond(self, handler: http.server.BaseHTTPRequestHandler, status: int) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def route(self, document: dict) -> None:
        known = document.get("sender") == check_relay.KNOWN_SENDER
        if self.breakage.drops_everyone or (not known and not self.breakage.forwards_everyone):
            self.write_row("dropped", "dropped", None)
            return
        if self.breakage.alters_the_body:
            document = {**document, "text": "altered"}
        headers = {"Content-Type": "application/json"}
        if not self.breakage.omits_the_plugin_secret:
            headers[check_relay.PLUGIN_SECRET_HEADER] = self.plugin_secret
        request = urllib.request.Request(self.forward_url, data=json.dumps(document).encode(), method="POST", headers=headers)
        try:
            if self.breakage.forwards_twice:
                urllib.request.urlopen(request, timeout=FAKE_FORWARD_TIMEOUT_SECONDS).close()
            with urllib.request.urlopen(request, timeout=FAKE_FORWARD_TIMEOUT_SECONDS) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            with error:
                status = error.code
        except TimeoutError:
            self.write_row("zack", "timeout", None)
            return
        except urllib.error.URLError as error:
            outcome = "timeout" if isinstance(error.reason, TimeoutError) else "plugin_unreachable"
            self.write_row("zack", outcome, None)
            return
        outcomes = {202: "accepted", 204: "dropped_by_plugin", 401: "secret_refused"}
        self.write_row("zack", outcomes.get(status, "plugin_refusing"), status)

    def write_row(self, route: str, outcome: str, status: int | None) -> None:
        if self.breakage.writes_no_rows:
            return
        if self.breakage.writes_the_wrong_outcome:
            outcome = "accepted"
        if self.breakage.writes_no_status:
            status = None
        if self.breakage.writes_the_wrong_route:
            route = "zack" if route == "dropped" else "dropped"
        row = {"received_at": "2026-11-04T23:00:00.000Z", "route": route, "outcome": outcome, "plugin_status": status, "duration_ms": 3}
        if self.breakage.writes_text_into_a_row:
            row["received_at"] = check_relay.CHECK_TEXT
        if self.breakage.writes_no_received_at:
            row["received_at"] = None
        if self.breakage.writes_no_duration:
            row["duration_ms"] = None
        self.rows_written.append(row)
        if self.breakage.writes_two_rows:
            self.rows_written.append(dict(row))

    # The instance's side, as the check calls it.
    def rows(self) -> list[dict]:
        return list(self.rows_written)

    def leftovers(self) -> int:
        return 1 if self.has_leftovers else 0

    def remove_leftovers(self) -> None:
        self.events.append("remove_leftovers")
        if not self.fails_to_clean:
            self.has_leftovers = False
            self.rows_written.clear()

    def install(self, workflow: dict, credentials: list[dict]) -> None:
        self.events.append("install")
        if self.fails_to_install:
            raise check_relay.CheckError("the fake refused to install")
        self.has_leftovers = True
        self.installed = (workflow, credentials)
        self.webhook_registered = True
        self.relay_secret = next(entry["data"]["value"] for entry in credentials if entry["name"] == check_relay.CHECK_RELAY_CREDENTIAL)
        self.plugin_secret = next(entry["data"]["value"] for entry in credentials if entry["name"] == check_relay.CHECK_PLUGIN_CREDENTIAL)
        http_request = nodes_of_type(workflow, HTTP_REQUEST_TYPE)[0]
        self.forward_url = http_request["parameters"]["url"]


@dataclass
class Run:
    code: int
    lines: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


def run_check(fake: FakeN8n, gate_broken: str | None = None) -> Run:
    lines: list[str] = []
    code = check_relay.run_check(fake, template(), RUN_RECORD_TABLE_ID, gate_broken, FAST, lines.append, webhook_url=fake.webhook_url)
    return Run(code, lines)


class CheckTest(unittest.TestCase):
    def fake(self, **breakage: bool) -> FakeN8n:
        fake = FakeN8n(FakeRelay(**breakage))
        self.addCleanup(fake.stop)
        return fake


class HealthyRelayTest(CheckTest):
    def test_a_relay_doing_everything_it_should_passes(self):
        result = run_check(self.fake())
        self.assertEqual(result.code, 0, result.output)

    def test_every_case_is_reported_with_its_status_and_row(self):
        result = run_check(self.fake())
        for name in ("no secret", "wrong secret", "unknown sender", "plugin unreachable", "accepted"):
            with self.subTest(name):
                self.assertTrue(any(name in line and "pass" in line for line in result.lines), result.output)

    def test_it_says_how_many_cases_it_examined(self):
        # A check that examined nothing must not read like one that passed.
        result = run_check(self.fake())
        self.assertIn(f"{len(check_relay.relay_cases(FAST))} of {len(check_relay.relay_cases(FAST))}", result.output)

    def test_the_over_long_body_is_recorded_rather_than_judged(self):
        result = run_check(self.fake())
        line = next(line for line in result.lines if "over 65,536" in line)
        self.assertIn(f"webhook answered {RECEIVED_STATUS}", line)
        self.assertIn("stand-in received", line)

    def test_nothing_printed_holds_a_secret_a_handle_or_a_body(self):
        fake = self.fake()
        result = run_check(fake)
        for value in (fake.relay_secret, check_relay.KNOWN_SENDER, check_relay.UNKNOWN_SENDER, check_relay.CHECK_TEXT):
            self.assertTrue(value)
            self.assertNotIn(value, result.output)


class BrokenRelayTest(CheckTest):
    """The reason the check exists: each of these must turn it red."""

    def assert_red(self, **breakage: bool) -> None:
        result = run_check(self.fake(**breakage))
        self.assertEqual(result.code, check_relay.EXIT_FAILED, f"{breakage} went unnoticed:\n{result.output}")
        self.assertTrue(any("FAIL" in line for line in result.lines), result.output)

    def test_a_post_with_no_secret_getting_through_is_red(self):
        self.assert_red(answers_without_a_secret=True)

    def test_a_post_with_a_wrong_secret_getting_through_is_red(self):
        self.assert_red(answers_a_wrong_secret=True)

    def test_an_unknown_sender_being_forwarded_is_red(self):
        self.assert_red(forwards_everyone=True)

    def test_a_known_sender_being_dropped_is_red(self):
        self.assert_red(drops_everyone=True)

    def test_a_body_altered_on_the_way_is_red(self):
        self.assert_red(alters_the_body=True)

    def test_no_rows_being_written_is_red(self):
        self.assert_red(writes_no_rows=True)

    def test_a_row_for_a_refused_post_is_red(self):
        self.assert_red(writes_a_row_for_refused_posts=True)

    def test_a_wrong_outcome_in_a_row_is_red(self):
        self.assert_red(writes_the_wrong_outcome=True)

    def test_a_missing_status_in_a_row_is_red(self):
        self.assert_red(writes_no_status=True)

    def test_a_forward_missing_the_plugins_secret_is_red(self):
        self.assert_red(omits_the_plugin_secret=True)

    def test_a_refusal_with_the_wrong_status_is_red(self):
        self.assert_red(refuses_with_the_wrong_status=True)

    def test_two_rows_for_one_post_is_red(self):
        self.assert_red(writes_two_rows=True)

    def test_a_wrong_route_in_a_row_is_red(self):
        self.assert_red(writes_the_wrong_route=True)

    def test_a_row_with_no_received_at_is_red(self):
        self.assert_red(writes_no_received_at=True)

    def test_a_row_with_no_duration_is_red(self):
        self.assert_red(writes_no_duration=True)

    def test_a_text_forwarded_twice_is_red(self):
        self.assert_red(forwards_twice=True)

    def test_the_text_reaching_a_row_is_red(self):
        self.assert_red(writes_text_into_a_row=True)


class CleanupTest(CheckTest):
    def test_leftovers_are_removed_before_anything_is_installed(self):
        fake = self.fake()
        fake.has_leftovers = True
        run_check(fake)
        self.assertLess(fake.events.index("remove_leftovers"), fake.events.index("install"))

    def test_no_leftovers_needs_nothing_removed_before_the_install(self):
        fake = self.fake()
        run_check(fake)
        self.assertEqual(fake.events[0], "install")

    def test_n8n_is_never_restarted_so_the_real_relay_keeps_serving(self):
        # The fake has no restart at all, so a call to one fails there too; this names the boundary.
        source = (N8N_DIRECTORY / "check_relay.py").read_text(encoding="utf-8")
        for word in ("kickstart", "bootout", "launchctl"):
            self.assertNotIn(word, source)

    def test_everything_is_removed_at_the_end_of_a_good_run(self):
        fake = self.fake()
        result = run_check(fake)
        self.assertEqual(result.code, 0, result.output)
        self.assertEqual(fake.events[-1], "remove_leftovers")
        self.assertEqual(fake.leftovers(), 0)
        self.assertEqual(fake.rows(), [])

    def test_everything_is_removed_at_the_end_of_a_failed_run(self):
        fake = self.fake(answers_without_a_secret=True)
        run_check(fake)
        self.assertEqual(fake.events[-1], "remove_leftovers")
        self.assertEqual(fake.leftovers(), 0)

    def test_everything_is_removed_when_the_run_is_interrupted(self):
        fake = self.fake()

        def interrupted(_: str) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            check_relay.run_check(fake, template(), RUN_RECORD_TABLE_ID, None, FAST, interrupted, webhook_url=fake.webhook_url)
        self.assertEqual(fake.events[-1], "remove_leftovers")

    def test_a_failed_install_still_cleans_up_and_is_red(self):
        fake = self.fake()
        fake.fails_to_install = True
        result = run_check(fake)
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertEqual(fake.events[-1], "remove_leftovers")

    def test_leftovers_it_could_not_remove_are_red_though_every_case_passed(self):
        fake = self.fake()
        real_remove = fake.remove_leftovers

        def fails_the_last_time() -> None:
            fake.fails_to_clean = "install" in fake.events
            real_remove()

        fake.remove_leftovers = fails_the_last_time
        result = run_check(fake)
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertIn("left behind", result.output)

    def test_a_webhook_slow_to_register_is_waited_for(self):
        fake = FakeN8n(webhook_ready_after=3)
        self.addCleanup(fake.stop)
        result = run_check(fake)
        self.assertEqual(result.code, 0, result.output)

    def test_a_webhook_that_never_registers_is_red_rather_than_a_pass(self):
        fake = FakeN8n(webhook_ready_after=1_000_000)
        self.addCleanup(fake.stop)
        result = run_check(fake)
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertIn("did not register", result.output)
        self.assertIn(f"last answered {WEBHOOK_NOT_REGISTERED_STATUS}", result.output)

    def test_a_200_from_an_n8n_still_starting_is_not_taken_for_the_webhook(self):
        # Otherwise the first cases run before the webhook exists, and go red for no reason.
        fake = FakeN8n(starting_answers=3)
        self.addCleanup(fake.stop)
        result = run_check(fake)
        self.assertEqual(result.code, 0, result.output)


class CheckTextTest(unittest.TestCase):
    def test_the_text_holds_the_word_the_failure_outcome_looks_for(self):
        # So the plugin-unreachable case, at the Mac, shows a text deciding nothing.
        self.assertIn("timeout", check_relay.CHECK_TEXT)


class TerminationTest(unittest.TestCase):
    def test_a_kill_or_a_closed_terminal_runs_the_clean_up_as_ctrl_c_does(self):
        for signal_number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number):
                previous = signal.getsignal(signal_number)
                self.addCleanup(signal.signal, signal_number, previous)
                # Handled first, so one left unhandled fails this test rather than killing the run.
                signal.signal(signal_number, lambda *_: None)
                check_relay.interrupt_on_termination()
                with self.assertRaises(KeyboardInterrupt):
                    os.kill(os.getpid(), signal_number)
                    for _ in range(100):
                        time.sleep(0.001)


class BreakingAGateTest(CheckTest):
    def test_a_break_that_turns_it_red_says_so(self):
        result = run_check(self.fake(answers_without_a_secret=True), "ip-allowlist")
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertIn("seen to fail", result.output)

    def test_a_break_nothing_noticed_is_its_own_failure(self):
        result = run_check(self.fake(), "ip-allowlist")
        self.assertEqual(result.code, check_relay.EXIT_BREAK_UNNOTICED, result.output)
        self.assertIn("nothing noticed", result.output)


class PostToWebhookTest(unittest.TestCase):
    def test_a_refused_post_leaves_nothing_open(self):
        # urllib hands a refusal back as an HTTPError holding the response; the check makes dozens.
        fake = FakeN8n()
        self.addCleanup(fake.stop)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            status = check_relay.post_to_webhook(fake.webhook_url, None, b"{}")
            gc.collect()
        self.assertEqual(status, WEBHOOK_NOT_REGISTERED_STATUS)
        self.assertEqual([str(warning.message) for warning in caught if issubclass(warning.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
