"""n8n/check_relay.py, whose only job is to go red when a gate is broken.

The check itself runs at the Mac against the real n8n, which no test here can reach. So its logic
is driven against a fake n8n in this file: a small HTTP server doing what the relay is designed to
do, which can be told to misbehave in each way the check exists to catch. A check that stayed
green against any of those would prove nothing when it stays green against the real one. What is
left for the Mac is whether the real relay behaves like the fake: that is what running it shows.
"""

from __future__ import annotations

import copy
import http.server
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from support import plugin_module

N8N_DIRECTORY = Path(__file__).resolve().parent.parent / "n8n"
sys.path.insert(0, str(N8N_DIRECTORY))

import check_relay  # noqa: E402 -- it lives beside the scripts it uses, not in a package

plugin_module("handles")

FIXTURE_TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "n8n-relay-template.json"
RUN_RECORD_TABLE_ID = "kQ3xV9mT2bLw7ZpA"
STAND_IN_URL = "http://127.0.0.1:49999/imessage"
PLUGIN_PORT = "8645"
WEBHOOK_TYPE = "n8n-nodes-base.webhook"
HTTP_REQUEST_TYPE = "n8n-nodes-base.httpRequest"
SET_TYPE = "n8n-nodes-base.set"
DATA_TABLE_TYPE = "n8n-nodes-base.dataTable"
CREDENTIAL_TYPE = "httpHeaderAuth"
FAST = check_relay.Timing(row_wait_seconds=1.0, settle_seconds=0.02, poll_seconds=0.01, slow_answer_seconds=0.3,
                          webhook_wait_seconds=0.3)
FAKE_FORWARD_TIMEOUT_SECONDS = 0.15
FORBIDDEN_STATUS = 403
UNAUTHORIZED_STATUS = 401
RECEIVED_STATUS = 200
WEBHOOK_NOT_REGISTERED_STATUS = 404


def template() -> dict:
    return json.loads(FIXTURE_TEMPLATE.read_text(encoding="utf-8"))


def nodes_of_type(workflow: dict, node_type: str) -> list[dict]:
    return [node for node in workflow["nodes"] if node["type"] == node_type]


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


class CheckWorkflowTest(unittest.TestCase):
    """What gets imported: the relay's own shape, aimed away from everything real."""

    def build(self, gate_broken: str | None = None, url: str = STAND_IN_URL) -> dict:
        return check_relay.build_check_workflow(template(), url, RUN_RECORD_TABLE_ID, gate_broken)

    def test_it_never_names_the_real_plugin(self):
        self.assertNotIn(PLUGIN_PORT, json.dumps(self.build()))

    def test_a_template_it_cannot_redirect_is_refused(self):
        changed = template()
        changed["nodes"].append(copy.deepcopy(nodes_of_type(changed, HTTP_REQUEST_TYPE)[0]))
        changed["nodes"][-1]["name"] = "A second forward"
        changed["nodes"][-1]["parameters"]["url"] = "http://127.0.0.1:8645/imessage"
        with self.assertRaises(check_relay.CheckError):
            check_relay.build_check_workflow(changed, STAND_IN_URL, RUN_RECORD_TABLE_ID, None)

    def test_a_second_forward_to_anywhere_at_all_is_refused(self):
        # Only one request can be aimed at the stand-in, so a second would go wherever it says.
        changed = template()
        changed["nodes"].append(copy.deepcopy(nodes_of_type(changed, HTTP_REQUEST_TYPE)[0]))
        changed["nodes"][-1]["name"] = "A second forward"
        changed["nodes"][-1]["parameters"]["url"] = "http://127.0.0.1:9/elsewhere"
        with self.assertRaises(check_relay.CheckError):
            check_relay.build_check_workflow(changed, STAND_IN_URL, RUN_RECORD_TABLE_ID, None)

    def test_a_stand_in_on_the_plugins_port_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            self.build(url=f"http://127.0.0.1:{PLUGIN_PORT}/imessage")

    def test_it_has_its_own_name_id_and_path(self):
        workflow = self.build()
        self.assertEqual(workflow["name"], check_relay.CHECK_WORKFLOW_NAME)
        self.assertEqual(workflow["id"], check_relay.CHECK_WORKFLOW_ID)
        self.assertEqual(nodes_of_type(workflow, WEBHOOK_TYPE)[0]["parameters"]["path"], check_relay.CHECK_WEBHOOK_PATH)

    def test_its_webhook_has_an_id_so_it_answers_on_its_path(self):
        self.assertTrue(nodes_of_type(self.build(), WEBHOOK_TYPE)[0].get("webhookId"))

    def test_it_posts_to_the_stand_in_and_writes_to_the_table_it_is_given(self):
        workflow = self.build()
        self.assertEqual(nodes_of_type(workflow, HTTP_REQUEST_TYPE)[0]["parameters"]["url"], STAND_IN_URL)
        self.assertEqual(nodes_of_type(workflow, DATA_TABLE_TYPE)[0]["parameters"]["dataTableId"]["value"], RUN_RECORD_TABLE_ID)

    def test_its_routing_table_holds_the_made_up_sender_alone(self):
        field_value = nodes_of_type(self.build(), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"]
        self.assertEqual(json.loads(field_value), [check_relay.KNOWN_SENDER])

    def test_it_uses_its_own_credentials_by_id_and_neither_real_one(self):
        workflow = self.build()
        for node_type, identifier, name in ((WEBHOOK_TYPE, check_relay.CHECK_RELAY_CREDENTIAL_ID, check_relay.CHECK_RELAY_CREDENTIAL),
                                            (HTTP_REQUEST_TYPE, check_relay.CHECK_PLUGIN_CREDENTIAL_ID, check_relay.CHECK_PLUGIN_CREDENTIAL)):
            with self.subTest(node_type):
                self.assertEqual(nodes_of_type(workflow, node_type)[0]["credentials"][CREDENTIAL_TYPE], {"id": identifier, "name": name})
        serialised = json.dumps(workflow)
        for real_name in ("Shortcuts relay secret", "Hermes iMessage plugin secret"):
            self.assertNotIn(real_name, serialised)

    def test_the_template_is_left_as_it_was(self):
        original = template()
        given = copy.deepcopy(original)
        check_relay.build_check_workflow(given, STAND_IN_URL, RUN_RECORD_TABLE_ID, "ip-allowlist")
        self.assertEqual(given, original)

    def test_the_ip_allowlist_break_names_an_address_that_is_not_this_mac(self):
        options = nodes_of_type(self.build("ip-allowlist"), WEBHOOK_TYPE)[0]["parameters"]["options"]
        self.assertEqual(options["ipWhitelist"], check_relay.NOT_THIS_MAC)

    def test_the_routing_table_break_takes_the_known_sender_out(self):
        field_value = nodes_of_type(self.build("routing-table"), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"]
        self.assertNotIn(check_relay.KNOWN_SENDER, field_value)

    def test_an_unknown_break_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            self.build("no-such-gate")


class CredentialsTest(unittest.TestCase):
    def test_the_header_names_are_the_real_ones_unless_that_gate_is_the_one_broken(self):
        for gate_broken, expected in ((None, check_relay.RELAY_SECRET_HEADER), ("wrong-header", check_relay.WRONG_HEADER)):
            with self.subTest(gate_broken):
                credentials = check_relay.check_credentials("relay-value", "plugin-value", gate_broken)
                relay = next(entry for entry in credentials if entry["name"] == check_relay.CHECK_RELAY_CREDENTIAL)
                self.assertEqual(relay["data"], {"name": expected, "value": "relay-value"})

    def test_each_has_its_own_id_and_the_header_auth_type(self):
        credentials = check_relay.check_credentials("relay-value", "plugin-value", None)
        self.assertEqual({entry["id"] for entry in credentials}, {check_relay.CHECK_RELAY_CREDENTIAL_ID, check_relay.CHECK_PLUGIN_CREDENTIAL_ID})
        self.assertEqual({entry["type"] for entry in credentials}, {CREDENTIAL_TYPE})


class BreakingAGateTest(CheckTest):
    def test_a_break_that_turns_it_red_says_so(self):
        result = run_check(self.fake(answers_without_a_secret=True), "ip-allowlist")
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertIn("seen to fail", result.output)

    def test_a_break_nothing_noticed_is_its_own_failure(self):
        result = run_check(self.fake(), "ip-allowlist")
        self.assertEqual(result.code, check_relay.EXIT_BREAK_UNNOTICED, result.output)
        self.assertIn("nothing noticed", result.output)


class StandInTest(unittest.TestCase):
    def setUp(self):
        self.stand_in = check_relay.StandInPlugin()
        self.stand_in.start()
        self.addCleanup(self.stand_in.stop)

    def post(self, body: bytes) -> int:
        request = urllib.request.Request(self.stand_in.url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_it_answers_202_and_keeps_the_body(self):
        self.assertEqual(self.post(b'{"a": 1}'), check_relay.ACCEPTED_STATUS)
        self.assertEqual(self.stand_in.received, [b'{"a": 1}'])

    def test_it_answers_whatever_it_is_told(self):
        self.stand_in.answer_status = 503
        self.assertEqual(self.post(b"{}"), 503)

    def test_it_refuses_a_body_past_the_plugins_limit_as_the_plugin_does(self):
        self.assertEqual(self.post(b"x" * (check_relay.PLUGIN_BODY_LIMIT_BYTES + 1)), check_relay.MALFORMED_STATUS)

    def test_it_listens_on_the_loopback_address_and_never_the_plugins_port(self):
        self.assertTrue(self.stand_in.url.startswith("http://127.0.0.1:"))
        self.assertEqual(self.stand_in.listening_on, "127.0.0.1")
        self.assertNotIn(PLUGIN_PORT, self.stand_in.url)

    def test_stopped_it_refuses_connections_and_started_it_comes_back_on_the_same_port(self):
        url = self.stand_in.url
        self.stand_in.stop()
        with self.assertRaises(urllib.error.URLError):
            self.post(b"{}")
        self.stand_in.start()
        self.assertEqual(self.stand_in.url, url)
        self.assertEqual(self.post(b"{}"), check_relay.ACCEPTED_STATUS)


REAL_RELAY_ID = "realRelayWorkflow"
REAL_CREDENTIAL_ID = "realCredentialId"
REAL_TABLE_ID = "realRunRecordTab"
# The tables as n8n's migrations make them, as far as this concerns them. The RESTRICT is
# 1772619247762-ChangeWorkflowPublishedVersionFKsToRestrict, which is what stops a delete; webhook
# rows, the outbox and the statistics have no foreign key, so nothing clears them for a workflow.
SCHEMA = f"""
CREATE TABLE workflow_entity (id TEXT PRIMARY KEY, name TEXT, activeVersionId TEXT);
CREATE TABLE workflow_history (versionId TEXT PRIMARY KEY, workflowId TEXT NOT NULL REFERENCES workflow_entity (id) ON DELETE CASCADE);
CREATE TABLE workflow_published_version (
  workflowId TEXT PRIMARY KEY REFERENCES workflow_entity (id) ON DELETE RESTRICT,
  publishedVersionId TEXT REFERENCES workflow_history (versionId) ON DELETE RESTRICT);
CREATE TABLE shared_workflow (workflowId TEXT NOT NULL REFERENCES workflow_entity (id) ON DELETE CASCADE, projectId TEXT);
CREATE TABLE webhook_entity (webhookPath TEXT, method TEXT, workflowId TEXT);
CREATE TABLE workflow_publication_outbox (id INTEGER PRIMARY KEY, workflowId TEXT, status TEXT);
CREATE TABLE workflow_statistics (id INTEGER PRIMARY KEY, workflowId TEXT, name TEXT);
CREATE TABLE credentials_entity (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE shared_credentials (credentialsId TEXT NOT NULL REFERENCES credentials_entity (id) ON DELETE CASCADE, projectId TEXT);
CREATE TABLE data_table (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE data_table_user_{RUN_RECORD_TABLE_ID} (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, route TEXT, outcome TEXT, plugin_status REAL, duration_ms REAL);
CREATE TABLE data_table_user_{REAL_TABLE_ID} (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, route TEXT, outcome TEXT, plugin_status REAL, duration_ms REAL);
INSERT INTO data_table VALUES ('{RUN_RECORD_TABLE_ID}', 'run_record_check'), ('{REAL_TABLE_ID}', 'run_record');
INSERT INTO workflow_entity VALUES ('{REAL_RELAY_ID}', 'iMessage relay', 'real-version');
INSERT INTO workflow_history VALUES ('real-version', '{REAL_RELAY_ID}');
INSERT INTO workflow_published_version VALUES ('{REAL_RELAY_ID}', 'real-version');
INSERT INTO shared_workflow VALUES ('{REAL_RELAY_ID}', 'project');
INSERT INTO webhook_entity VALUES ('imessage-relay', 'POST', '{REAL_RELAY_ID}');
INSERT INTO workflow_publication_outbox (workflowId, status) VALUES ('{REAL_RELAY_ID}', 'completed');
INSERT INTO workflow_statistics (workflowId, name) VALUES ('{REAL_RELAY_ID}', 'production_success');
INSERT INTO credentials_entity VALUES ('{REAL_CREDENTIAL_ID}', 'Shortcuts relay secret');
INSERT INTO shared_credentials VALUES ('{REAL_CREDENTIAL_ID}', 'project');
INSERT INTO data_table_user_{REAL_TABLE_ID} (received_at, route, outcome, plugin_status, duration_ms) VALUES ('then', 'zack', 'accepted', 202, 3);
"""
TABLES_THAT_HOLD_THE_REAL_RELAY = ("workflow_entity", "workflow_history", "workflow_published_version", "shared_workflow", "webhook_entity",
                                   "workflow_publication_outbox", "workflow_statistics", "credentials_entity", "shared_credentials",
                                   f"data_table_user_{REAL_TABLE_ID}")
CHECK_ID = check_relay.CHECK_WORKFLOW_ID


class LocalN8n:
    """Stands in for sudo and n8n's CLI alone: everything else really runs, on a scratch database."""

    def __init__(self, database: Path):
        self.database = database
        self.calls: list[list[str]] = []
        self.daemon_takes_workflows_down = True
        self.failing_command: str | None = None
        self.seen_by_cli: dict[str, str] = {}

    def __call__(self, arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(arguments))
        if arguments[0] == str(check_relay.CLI_SCRIPT):
            return self.cli(arguments[1:])
        if arguments[:3] != ["sudo", "-u", check_relay.N8N_USER]:
            raise AssertionError(f"not run as {check_relay.N8N_USER}: {arguments}")
        return subprocess.run(arguments[3:], **options)  # noqa: S603 -- the check's own commands, on scratch paths

    def cli(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = arguments[0]
        if command == self.failing_command:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")
        if command.startswith("import:"):
            self.seen_by_cli[command] = Path(arguments[1].removeprefix("--input=")).read_text(encoding="utf-8")
        with sqlite3.connect(self.database) as connection:
            if command == "import:credentials":
                for credential in json.loads(self.seen_by_cli[command]):
                    connection.execute("INSERT INTO credentials_entity VALUES (?, ?)", (credential["id"], credential["name"]))
                    connection.execute("INSERT INTO shared_credentials VALUES (?, 'project')", (credential["id"],))
            elif command == "import:workflow":
                connection.execute("INSERT INTO workflow_entity VALUES (?, ?, NULL)", (CHECK_ID, check_relay.CHECK_WORKFLOW_NAME))
                connection.execute("INSERT INTO workflow_history VALUES ('check-version', ?)", (CHECK_ID,))
                connection.execute("INSERT INTO shared_workflow VALUES (?, 'project')", (CHECK_ID,))
            elif command == "publish:workflow":
                connection.execute("UPDATE workflow_entity SET activeVersionId = 'check-version' WHERE id = ?", (CHECK_ID,))
                self.daemon_publishes(connection)
            elif command == "unpublish:workflow":
                connection.execute("UPDATE workflow_entity SET activeVersionId = NULL WHERE id = ?", (CHECK_ID,))
                if self.daemon_takes_workflows_down:
                    self.daemon_unpublishes(connection)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    def daemon_publishes(self, connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO workflow_published_version VALUES (?, 'check-version')", (CHECK_ID,))
        connection.execute("INSERT INTO webhook_entity VALUES ('imessage-relay-check', 'POST', ?)", (CHECK_ID,))
        connection.execute("INSERT INTO workflow_publication_outbox (workflowId, status) VALUES (?, 'completed')", (CHECK_ID,))
        connection.execute("INSERT INTO workflow_statistics (workflowId, name) VALUES (?, 'production_success')", (CHECK_ID,))

    def daemon_unpublishes(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM workflow_published_version WHERE workflowId = ?", (CHECK_ID,))
        connection.execute("DELETE FROM webhook_entity WHERE workflowId = ?", (CHECK_ID,))


class InstanceTest(unittest.TestCase):
    """The real instance's commands, really run. No test can reach n8n's own database, so this one is
    built from its migrations, with the real relay's rows beside the check's to show they survive."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.database = self.scratch / "database.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(SCHEMA)
        self.run = LocalN8n(self.database)
        self.instance = self.instance_of(RUN_RECORD_TABLE_ID)

    def instance_of(self, table_id: str, unpublish_wait_seconds: float = 0.2) -> check_relay.N8nInstance:
        return check_relay.N8nInstance(table_id, run=self.run, home=str(self.scratch), database=str(self.database),
                                       unpublish_wait_seconds=unpublish_wait_seconds, poll_seconds=0.01)

    def install(self, secret: str = "a-secret") -> None:
        credentials = check_relay.check_credentials(secret, secret, None)
        self.instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)

    def write_row(self, route: str = "zack") -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(f"INSERT INTO data_table_user_{RUN_RECORD_TABLE_ID} (received_at, route, outcome, plugin_status, duration_ms)"
                               " VALUES ('now', ?, 'accepted', 202, 4)", (route,))

    def count(self, table: str) -> int:
        with sqlite3.connect(self.database) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def real_rows(self) -> dict[str, list[tuple]]:
        with sqlite3.connect(self.database) as connection:
            return {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in TABLES_THAT_HOLD_THE_REAL_RELAY}

    def row_counts(self) -> dict[str, int]:
        """How many rows each table holds, for what has to be left in place rather than as it was:
        unpublishing clears a workflow's active version before anything can refuse."""
        return {table: self.count(table) for table in TABLES_THAT_HOLD_THE_REAL_RELAY}

    def hand_over_folders(self) -> list[Path]:
        return list(self.scratch.glob(f"{check_relay.HAND_OVER_FOLDER_PREFIX}*"))

    def cli_commands(self) -> list[str]:
        return [call[1] for call in self.run.calls if call[0] == str(check_relay.CLI_SCRIPT)]

    def test_reading_rows_is_read_only_and_as_n8n(self):
        self.write_row()
        self.assertEqual([row["route"] for row in self.instance.rows()], ["zack"])
        read = next(call for call in self.run.calls if check_relay.SQLITE_PATH in call)
        self.assertIn("-readonly", read)

    def test_no_rows_is_an_empty_list(self):
        self.assertEqual(self.instance.rows(), [])

    def test_a_status_reads_back_equal_though_it_is_stored_as_a_real(self):
        self.write_row()
        self.assertEqual(self.instance.rows()[0]["plugin_status"], check_relay.ACCEPTED_STATUS)

    def test_nothing_there_counts_as_no_leftovers(self):
        self.assertEqual(self.instance.leftovers(), 0)

    def test_each_kind_of_thing_it_can_leave_counts_as_a_leftover(self):
        # Three of these have no foreign key, so removing the workflow would not clear them.
        for table, values in (("workflow_entity", f"('{CHECK_ID}', 'x', NULL)"),
                              ("webhook_entity", f"('p', 'POST', '{CHECK_ID}')"),
                              ("workflow_publication_outbox", f"(1000, '{CHECK_ID}', 'completed')"),
                              ("workflow_statistics", f"(1000, '{CHECK_ID}', 'n')"),
                              ("credentials_entity", f"('{check_relay.CHECK_RELAY_CREDENTIAL_ID}', 'x')"),
                              (f"data_table_user_{RUN_RECORD_TABLE_ID}", "(1000, 'r', 'z', 'o', 1, 1)")):
            with self.subTest(table):
                with sqlite3.connect(self.database) as connection:
                    connection.execute(f"INSERT INTO {table} VALUES {values}")
                self.assertGreater(self.instance.leftovers(), 0)
                self.instance.remove_leftovers()
                self.assertEqual(self.instance.leftovers(), 0)

    def test_a_published_workflow_is_unpublished_before_it_is_deleted_and_all_of_it_goes(self):
        self.install()
        self.write_row()
        self.assertGreater(self.instance.leftovers(), 0)
        self.instance.remove_leftovers()
        self.assertEqual(self.instance.leftovers(), 0)
        self.assertIn("unpublish:workflow", self.cli_commands())

    def test_shared_rows_go_with_what_they_belong_to(self):
        self.install()
        before = self.real_rows()
        self.instance.remove_leftovers()
        with sqlite3.connect(self.database) as connection:
            for table in ("shared_workflow", "shared_credentials", "workflow_history"):
                with self.subTest(table):
                    self.assertEqual(connection.execute(f"SELECT * FROM {table}").fetchall(), [row for row in before[table] if CHECK_ID not in row
                                     and check_relay.CHECK_RELAY_CREDENTIAL_ID not in row and check_relay.CHECK_PLUGIN_CREDENTIAL_ID not in row])

    def test_the_real_relay_is_exactly_as_it_was(self):
        before = self.real_rows()
        self.install()
        self.write_row()
        self.instance.remove_leftovers()
        self.assertEqual(self.real_rows(), before)

    def test_deleting_a_published_workflow_is_refused_which_is_why_it_is_unpublished_first(self):
        # The control: without this the unpublishing above could be doing nothing.
        self.install()
        self.instance._unpublish = lambda: None
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()

    def test_a_removal_that_fails_changes_nothing(self):
        # All or none: half a clean-up would leave a workflow with its webhook row gone.
        self.install()
        self.write_row()
        self.instance._unpublish = lambda: None
        before = self.real_rows()
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()
        self.assertEqual(self.real_rows(), before)
        self.assertEqual(self.count(f"data_table_user_{RUN_RECORD_TABLE_ID}"), 1)

    def test_a_table_that_is_not_there_fails_the_removal_and_rolls_it_back(self):
        self.install()
        with sqlite3.connect(self.database) as connection:
            self.run.daemon_unpublishes(connection)
        before = self.row_counts()
        with self.assertRaises(check_relay.CheckError):
            self.instance_of("noSuchTableHere").remove_leftovers()
        self.assertEqual(self.row_counts(), before)

    def test_a_daemon_that_never_takes_it_down_is_refused_and_nothing_is_deleted(self):
        self.install()
        self.run.daemon_takes_workflows_down = False
        before = self.row_counts()
        with self.assertRaises(check_relay.CheckError) as raised:
            self.instance.remove_leftovers()
        self.assertIn("had not finished", str(raised.exception))
        self.assertEqual(self.row_counts(), before)

    def test_a_daemon_slow_to_take_it_down_is_waited_for(self):
        self.install()
        self.run.daemon_takes_workflows_down = False
        reads_until_down = {"left": 3}
        real_rows_left = self.instance._publication_rows_left

        def slowly() -> int:
            reads_until_down["left"] -= 1
            if reads_until_down["left"] == 0:
                with sqlite3.connect(self.database) as connection:
                    self.run.daemon_unpublishes(connection)
            return real_rows_left()

        self.instance._publication_rows_left = slowly
        self.instance.remove_leftovers()
        self.assertEqual(self.instance.leftovers(), 0)

    def test_nothing_of_the_checks_there_needs_no_unpublish(self):
        self.instance.remove_leftovers()
        self.assertNotIn("unpublish:workflow", self.cli_commands())

    def test_a_folder_a_killed_run_left_is_swept_and_nothing_else_is(self):
        stale = self.scratch / f"{check_relay.HAND_OVER_FOLDER_PREFIX}stale"
        stale.mkdir()
        (stale / "credentials.json").write_text("a secret from that run\n", encoding="utf-8")
        for kept in ("import.stale", ".n8n", "log"):
            (self.scratch / kept).mkdir()
        self.instance.remove_leftovers()
        self.assertEqual(self.hand_over_folders(), [])
        for kept in ("import.stale", ".n8n", "log"):
            self.assertTrue((self.scratch / kept).is_dir(), kept)

    def test_installing_imports_credentials_then_workflow_then_publishes(self):
        self.install()
        self.assertEqual(self.cli_commands(), ["import:credentials", "import:workflow", "publish:workflow"])

    def test_the_cli_is_handed_the_real_content_through_a_file(self):
        self.install("a-secret")
        self.assertEqual(json.loads(self.run.seen_by_cli["import:workflow"])["id"], CHECK_ID)
        self.assertIn("a-secret", self.run.seen_by_cli["import:credentials"])

    def test_no_secret_is_on_any_command_line(self):
        secret = "s3cret-value-that-must-not-show"
        self.install(secret)
        self.instance.remove_leftovers()
        self.assertTrue(self.run.calls)
        for call in self.run.calls:
            self.assertNotIn(secret, " ".join(call))

    def test_the_copies_are_gone_after_a_good_install(self):
        self.install()
        self.assertEqual(self.hand_over_folders(), [])

    def test_the_copies_are_gone_after_a_failed_import_and_it_is_refused(self):
        for command in ("import:credentials", "import:workflow", "publish:workflow"):
            with self.subTest(command):
                self.run.failing_command = command
                with self.assertRaises(check_relay.CheckError) as raised:
                    self.install()
                self.assertIn(command, str(raised.exception))
                self.assertEqual(self.hand_over_folders(), [])
                self.run.failing_command = None
                self.instance.remove_leftovers()

    def test_a_folder_it_could_not_make_stops_the_install(self):
        instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=self.run, home=str(self.scratch), database=str(self.database))
        instance.home = str(self.scratch / "no-such-home")
        (self.scratch / "no-such-home").mkdir()
        (self.scratch / "no-such-home").chmod(0o500)
        self.addCleanup((self.scratch / "no-such-home").chmod, 0o700)
        with self.assertRaises(check_relay.CheckError) as raised:
            instance.install({}, [])
        self.assertIn("could not make a folder", str(raised.exception))

    def test_a_failed_unpublish_is_refused(self):
        self.install()
        self.run.failing_command = "unpublish:workflow"
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()

    def test_a_database_that_cannot_be_read_is_refused_rather_than_read_as_empty(self):
        instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=self.run, home=str(self.scratch), database=str(self.scratch / "absent.sqlite"))
        for read in (instance.rows, instance.leftovers, instance.remove_leftovers):
            with self.subTest(read.__name__), self.assertRaises(check_relay.CheckError):
                read()

    def test_a_table_id_that_could_not_be_one_is_refused_before_it_reaches_sql(self):
        with self.assertRaises(check_relay.CheckError):
            check_relay.N8nInstance("x'; DROP TABLE workflow_entity; --", run=self.run)
        self.assertEqual(self.run.calls, [])

    def test_the_check_table_is_found_by_name(self):
        identifier = check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))
        self.assertEqual(identifier, RUN_RECORD_TABLE_ID)
        self.assertIn("-readonly", self.run.calls[0])

    def test_no_check_table_says_how_to_make_one(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM data_table WHERE name = 'run_record_check'")
        with self.assertRaises(check_relay.CheckError) as raised:
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))
        self.assertIn("n8n/README.md", str(raised.exception))

    def test_two_tables_of_that_name_are_refused_rather_than_guessed_between(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO data_table VALUES ('anotherTableId00', 'run_record_check')")
        with self.assertRaises(check_relay.CheckError):
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))

    def test_a_database_that_cannot_be_read_when_finding_the_table_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.scratch / "absent.sqlite"))


if __name__ == "__main__":
    unittest.main()
