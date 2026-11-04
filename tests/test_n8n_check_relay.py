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
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from support import RecordingRun, plugin_module

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

    def __init__(self, breakage: FakeRelay | None = None, webhook_ready_after: int = 0):
        self.breakage = breakage or FakeRelay()
        self.rows_written: list[dict] = []
        self.events: list[str] = []
        self.has_leftovers = False
        self.webhook_ready_after = webhook_ready_after
        self.installed: tuple[dict, list[dict]] | None = None
        # As the real one: a workflow published from the CLI answers only after a restart.
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
        if self.webhook_ready_after > 0:
            self.webhook_ready_after -= 1
            self.respond(handler, check_relay.WEBHOOK_NOT_REGISTERED_STATUS)
            return
        if not self.webhook_registered:
            self.respond(handler, check_relay.WEBHOOK_NOT_REGISTERED_STATUS)
            return
        presented = handler.headers.get(check_relay.RELAY_SECRET_HEADER)
        if presented is None and not self.breakage.answers_without_a_secret or (
                presented is not None and presented != self.relay_secret and not self.breakage.answers_a_wrong_secret):
            if self.breakage.writes_a_row_for_refused_posts:
                self.write_row("dropped", "dropped", None)
            self.respond(handler, UNAUTHORIZED_STATUS if self.breakage.refuses_with_the_wrong_status else FORBIDDEN_STATUS)
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
        self.relay_secret = next(entry["data"]["value"] for entry in credentials if entry["name"] == check_relay.CHECK_RELAY_CREDENTIAL)
        self.plugin_secret = next(entry["data"]["value"] for entry in credentials if entry["name"] == check_relay.CHECK_PLUGIN_CREDENTIAL)
        http_request = nodes_of_type(workflow, HTTP_REQUEST_TYPE)[0]
        self.forward_url = http_request["parameters"]["url"]

    def restart(self) -> None:
        self.events.append("restart")
        self.webhook_registered = self.installed is not None


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

    def test_no_leftovers_needs_no_restart_before_the_install(self):
        fake = self.fake()
        run_check(fake)
        self.assertEqual(fake.events[0], "install")

    def test_leftovers_found_mean_a_restart_before_the_install(self):
        # n8n still has a leftover workflow's webhook registered until it restarts.
        fake = self.fake()
        fake.has_leftovers = True
        run_check(fake)
        self.assertEqual(fake.events[:3], ["remove_leftovers", "restart", "install"])

    def test_everything_is_removed_at_the_end_of_a_good_run(self):
        fake = self.fake()
        result = run_check(fake)
        self.assertEqual(result.code, 0, result.output)
        self.assertEqual(fake.events[-2:], ["remove_leftovers", "restart"])
        self.assertEqual(fake.leftovers(), 0)
        self.assertEqual(fake.rows(), [])

    def test_everything_is_removed_at_the_end_of_a_failed_run(self):
        fake = self.fake(answers_without_a_secret=True)
        run_check(fake)
        self.assertEqual(fake.events[-2:], ["remove_leftovers", "restart"])
        self.assertEqual(fake.leftovers(), 0)

    def test_everything_is_removed_when_the_run_is_interrupted(self):
        fake = self.fake()

        def interrupted(_: str) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            check_relay.run_check(fake, template(), RUN_RECORD_TABLE_ID, None, FAST, interrupted, webhook_url=fake.webhook_url)
        self.assertEqual(fake.events[-2:], ["remove_leftovers", "restart"])

    def test_a_failed_install_still_cleans_up_and_is_red(self):
        fake = self.fake()
        fake.fails_to_install = True
        result = run_check(fake)
        self.assertEqual(result.code, check_relay.EXIT_FAILED, result.output)
        self.assertEqual(fake.events[-2:], ["remove_leftovers", "restart"])

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

    def test_the_workflow_is_installed_before_a_restart_and_the_posts_come_after_it(self):
        # Published from the CLI, its webhook is not registered until n8n restarts.
        fake = self.fake()
        result = run_check(fake)
        self.assertEqual(result.code, 0, result.output)
        self.assertEqual(fake.events[:2], ["install", "restart"])


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


class InstanceTest(unittest.TestCase):
    """The real instance's commands: who they run as, and what must never be on a command line."""

    def setUp(self):
        self.run = RecordingRun(stdout="")
        self.instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=self.run)

    def sql_calls(self) -> list[list[str]]:
        return [call for call in self.run.calls if "/usr/bin/sqlite3" in call]

    def test_reading_rows_is_read_only_as_n8n_and_names_the_check_table(self):
        self.run.stdout = '[{"route": "zack"}]'
        self.assertEqual(self.instance.rows(), [{"route": "zack"}])
        call = self.sql_calls()[0]
        self.assertEqual(call[:3], ["sudo", "-u", "_n8n"])
        self.assertIn("-readonly", call)
        self.assertIn(f"data_table_user_{RUN_RECORD_TABLE_ID}", call[-1])

    def test_no_rows_is_an_empty_list(self):
        self.assertEqual(self.instance.rows(), [])

    def test_removing_turns_foreign_keys_on_and_touches_only_the_checks_own_ids(self):
        self.instance.remove_leftovers()
        sql = self.sql_calls()[0][-1]
        self.assertIn("PRAGMA foreign_keys=ON", sql)
        for identifier in (check_relay.CHECK_WORKFLOW_ID, check_relay.CHECK_RELAY_CREDENTIAL_ID, check_relay.CHECK_PLUGIN_CREDENTIAL_ID):
            self.assertIn(identifier, sql)
        self.assertIn(f"DELETE FROM data_table_user_{RUN_RECORD_TABLE_ID}", sql)
        self.assertNotIn("-readonly", self.sql_calls()[0])

    def test_removing_cannot_reach_a_workflow_by_anything_but_its_id(self):
        self.instance.remove_leftovers()
        sql = self.sql_calls()[0][-1]
        self.assertIn(f"DELETE FROM workflow_entity WHERE id = '{check_relay.CHECK_WORKFLOW_ID}'", sql)
        self.assertNotIn("WHERE name", sql)

    def test_installing_puts_no_secret_on_any_command_line(self):
        self.run.stdout = "/var/n8n/check.XXXX\n"
        secret = "s3cret-value-that-must-not-show"
        credentials = check_relay.check_credentials(secret, secret, None)
        self.instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)
        for call in self.run.calls:
            self.assertNotIn(secret, " ".join(call))

    def test_installing_imports_credentials_then_workflow_then_publishes(self):
        self.run.stdout = "/var/n8n/check.XXXX\n"
        credentials = check_relay.check_credentials("a", "b", None)
        self.instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)
        joined = [" ".join(call) for call in self.run.calls]
        steps = [next(index for index, call in enumerate(joined) if step in call)
                 for step in ("import:credentials", "import:workflow", f"publish:workflow --id={check_relay.CHECK_WORKFLOW_ID}")]
        self.assertEqual(steps, sorted(steps))

    def test_the_handed_over_copy_is_removed_even_when_an_import_fails(self):
        def failing_on_import(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.run.calls.append(list(arguments))
            failed = any("import:workflow" in argument for argument in arguments)
            return subprocess.CompletedProcess(arguments, 1 if failed else 0, stdout="/var/n8n/check.XXXX\n", stderr="")

        instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=failing_on_import)
        credentials = check_relay.check_credentials("a", "b", None)
        with self.assertRaises(check_relay.CheckError):
            instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)
        self.assertTrue(any("/bin/rm" in call for call in self.run.calls))

    def test_a_table_id_that_could_not_be_one_is_refused_before_it_reaches_sql(self):
        with self.assertRaises(check_relay.CheckError):
            check_relay.N8nInstance("x'; DROP TABLE workflow_entity; --", run=self.run)
        self.assertEqual(self.run.calls, [])

    def test_a_folder_it_could_not_make_stops_the_install(self):
        credentials = check_relay.check_credentials("a", "b", None)
        with self.assertRaises(check_relay.CheckError):
            self.instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)

    def test_the_check_table_is_found_by_name(self):
        self.run.stdout = f"{RUN_RECORD_TABLE_ID}\n"
        self.assertEqual(check_relay.find_check_table(run=self.run), RUN_RECORD_TABLE_ID)
        self.assertIn("-readonly", self.run.calls[0])

    def test_no_check_table_says_how_to_make_one(self):
        with self.assertRaises(check_relay.CheckError) as raised:
            check_relay.find_check_table(run=self.run)
        self.assertIn("n8n/README.md", str(raised.exception))

    def test_a_database_that_cannot_be_read_is_refused_rather_than_read_as_empty(self):
        self.run.returncode = 1
        with self.assertRaises(check_relay.CheckError):
            self.instance.rows()
        with self.assertRaises(check_relay.CheckError):
            self.instance.leftovers()
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()

    def test_restarting_uses_launchd_on_n8ns_own_daemon(self):
        self.run.stdout = ""
        self.instance.restart_daemon()
        self.assertEqual(self.run.calls[0], ["sudo", "launchctl", "kickstart", "-k", "system/com.zack.hermes-imessage.n8n"])


class HealthWaitTest(unittest.TestCase):
    def test_a_url_that_never_answers_is_refused_when_the_wait_runs_out(self):
        stand_in = check_relay.StandInPlugin()
        stand_in.start()
        stand_in.stop()
        with self.assertRaises(check_relay.CheckError):
            check_relay.wait_until_healthy(stand_in.url, 0.05, 0.01)

    def test_a_url_that_answers_200_ends_the_wait(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 -- http.server's name
                self.send_response(check_relay.HEALTHY_STATUS)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: object) -> None:
                """Silent."""

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        check_relay.wait_until_healthy(f"http://127.0.0.1:{server.server_address[1]}/healthz", 2.0, 0.01)


if __name__ == "__main__":
    unittest.main()
