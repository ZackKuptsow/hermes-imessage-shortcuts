#!/usr/bin/env python3
"""check_relay.py: proves the switchboard end to end, at the Mac, against a stand-in plugin.

Usage: check_relay.py [--break {wrong-header,ip-allowlist,routing-table}]

Renders the relay template under its own name, webhook path and credentials, aimed at a stand-in
plugin on a free port, and writing to a second Data Table, `run_record_check`, so the real relay,
its table, its secrets and the plugin on 8645 are never touched. It posts each case and reads the
row each leaves, then removes everything it made. The senders are made up and the secrets random
for each run, so it never reads your handle file or your secrets, and it prints no secret, handle
or body. With --break it breaks one gate on purpose: that run should go red.

It publishes through n8n's CLI and lets the running daemon pick that up, which its reconciler does
within about half a minute, so n8n is never restarted and the real relay keeps serving through a
run. Exits 0 only when every case held.
"""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import render
import workflow_template

EXIT_FAILED = 1
EXIT_BREAK_UNNOTICED = 3
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
RELAY_TEMPLATE = SCRIPT_DIRECTORY / "workflows" / "imessage-relay.json"
CLI_SCRIPT = SCRIPT_DIRECTORY / "n8n-cli.sh"
N8N_USER = "_n8n"
N8N_HOME = "/var/n8n"
DATABASE_PATH = "/var/n8n/.n8n/database.sqlite"
SQLITE_PATH = "/usr/bin/sqlite3"
SQLITE_BUSY_WAIT = ".timeout 5000"
HAND_OVER_FOLDER_PREFIX = "check."
# n8n's reconciler looks every 10 seconds and its outbox every 15, on the daemon's own clock.
UNPUBLISH_WAIT_SECONDS = 60.0
UNPUBLISH_POLL_SECONDS = 1.0

CHECK_TABLE_NAME = "run_record_check"
# The run record's columns as n8n types them. A duration made as a String column stores each value as
# text, and every row then fails the check as if the relay were at fault (seen 2026-11-09).
CHECK_TABLE_COLUMNS = {"duration_ms": "number", "outcome": "string", "plugin_status": "number", "received_at": "string",
                       "route": "string"}
CHECK_WORKFLOW_NAME = "iMessage relay check"
CHECK_WORKFLOW_ID = "relayCheckWrkflw"
CHECK_WEBHOOK_PATH = "imessage-relay-check"
CHECK_WEBHOOK_URL = f"http://127.0.0.1:5678/webhook/{CHECK_WEBHOOK_PATH}"
CHECK_RELAY_CREDENTIAL = "Relay check relay secret"
CHECK_RELAY_CREDENTIAL_ID = "relayCheckRelayC"
CHECK_PLUGIN_CREDENTIAL = "Relay check plugin secret"
CHECK_PLUGIN_CREDENTIAL_ID = "relayCheckPlugin"
CREDENTIAL_TYPE = "httpHeaderAuth"
WEBHOOK_TYPE = "n8n-nodes-base.webhook"
HTTP_REQUEST_TYPE = "n8n-nodes-base.httpRequest"
RELAY_SECRET_HEADER = "X-Relay-Secret"
PLUGIN_SECRET_HEADER = "X-Hermes-Secret"
REAL_PLUGIN_ADDRESS = "127.0.0.1:8645"
# 555-01XX numbers are set aside for fiction, so neither of these can be anyone's.
KNOWN_SENDER = "+15550100199"
UNKNOWN_SENDER = "+15550100142"
# Holds the word the relay's failure outcome looks for, so a text deciding an outcome shows up, and
# characters that would not survive a careless re-encoding on the way through.
CHECK_TEXT = "relay check: timeout \u2603 \U0001f600"
LONG_TEXT_LENGTH = 70_000

BREAK_WRONG_HEADER = "wrong-header"
BREAK_IP_ALLOWLIST = "ip-allowlist"
BREAK_ROUTING_TABLE = "routing-table"
GATES_TO_BREAK = (BREAK_WRONG_HEADER, BREAK_IP_ALLOWLIST, BREAK_ROUTING_TABLE)
WRONG_HEADER = "X-Not-The-Relay-Secret"
# TEST-NET-1, set aside for documentation, so it can never be this Mac.
NOT_THIS_MAC = "192.0.2.1"

ACCEPTED_STATUS = 202
DROPPED_STATUS = 204
UNAUTHORIZED_STATUS = 401
MALFORMED_STATUS = 400
NOT_SERVING_STATUS = 503
FORBIDDEN_STATUS = 403
RECEIVED_STATUS = 200
NO_ANSWER_STATUS = 0
PLUGIN_BODY_LIMIT_BYTES = 65_536
POST_TIMEOUT_SECONDS = 15
# How often the stand-in looks up from serving: how long stopping it can take.
STAND_IN_POLL_SECONDS = 0.02

SECRET_NONE = "none"
SECRET_WRONG = "wrong"
SECRET_RIGHT = "right"
ROUTE_ZACK = "zack"
ROUTE_DROPPED = "dropped"

Report = Callable[[str], None]


class CheckError(RuntimeError):
    """Something the check needs could not be done. The message holds no value."""


@dataclass(frozen=True)
class Timing:
    """How long the check waits for what, so a test need not wait as long as n8n does."""

    row_wait_seconds: float = 10.0
    settle_seconds: float = 2.0
    poll_seconds: float = 0.25
    # Past the relay's own 5-second timeout on the plugin.
    slow_answer_seconds: float = 7.0
    webhook_wait_seconds: float = 60.0


@dataclass(frozen=True)
class Case:
    """One post, and everything it should leave behind. No expected route means no row at all."""

    name: str
    secret: str
    sender: str
    expected_status: int
    expected_route: str | None = None
    expected_outcome: str | None = None
    expected_plugin_status: int | None = None
    reaches_plugin: bool = False
    stand_in_up: bool = True
    answer_status: int = ACCEPTED_STATUS
    answer_delay_seconds: float = 0.0


def relay_cases(timing: Timing) -> tuple[Case, ...]:
    """Every case the design names, and the rest of the outcome mapping."""
    return (
        Case("no secret", SECRET_NONE, KNOWN_SENDER, FORBIDDEN_STATUS),
        Case("wrong secret", SECRET_WRONG, KNOWN_SENDER, FORBIDDEN_STATUS),
        Case("unknown sender", SECRET_RIGHT, UNKNOWN_SENDER, RECEIVED_STATUS, ROUTE_DROPPED, "dropped"),
        Case("plugin unreachable", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "plugin_unreachable", stand_in_up=False),
        Case("accepted", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "accepted", ACCEPTED_STATUS, reaches_plugin=True),
        Case("dropped by the plugin", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "dropped_by_plugin", DROPPED_STATUS,
             reaches_plugin=True, answer_status=DROPPED_STATUS),
        Case("refused secret", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "secret_refused", UNAUTHORIZED_STATUS,
             reaches_plugin=True, answer_status=UNAUTHORIZED_STATUS),
        Case("plugin refusing", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "plugin_refusing", NOT_SERVING_STATUS,
             reaches_plugin=True, answer_status=NOT_SERVING_STATUS),
        Case("timeout", SECRET_RIGHT, KNOWN_SENDER, RECEIVED_STATUS, ROUTE_ZACK, "timeout", reaches_plugin=True,
             answer_delay_seconds=timing.slow_answer_seconds),
    )


class StandInPlugin:
    """A few lines of http.server answering as the plugin would, and keeping what it was sent."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.answer_status = ACCEPTED_STATUS
        self.answer_delay_seconds = 0.0
        self.presented_secrets: list[str | None] = []
        self.port = 0
        self._server: http.server.ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/imessage"

    @property
    def listening_on(self) -> str | None:
        """The address it is bound to while it runs, which is what decides who can reach it."""
        return None if self._server is None else self._server.server_address[0]

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 -- http.server's name
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stand_in.received.append(body)
                stand_in.presented_secrets.append(self.headers.get(PLUGIN_SECRET_HEADER))
                time.sleep(stand_in.answer_delay_seconds)
                status = MALFORMED_STATUS if len(body) > PLUGIN_BODY_LIMIT_BYTES else stand_in.answer_status
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the relay gave up waiting, which is the timeout case working

            def log_message(self, *_: object) -> None:
                """Silent, since a request line is all it could add and a body must never be printed."""

        return Handler

    def start(self) -> None:
        """Listen on the loopback address; after a stop, on the same port as before."""
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), self._handler())
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, args=(STAND_IN_POLL_SECONDS,), daemon=True).start()

    def set_running(self, running: bool) -> None:
        """Up or down as a case wants it, changing only if it has to."""
        if running and self._server is None:
            self.start()
        elif not running:
            self.stop()

    def stop(self) -> None:
        """Stop listening, so a connection to it is refused."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


def the_one_node(workflow: dict, node_type: str) -> dict:
    """The single node of *node_type*, or CheckError: more than one could not be redirected safely."""
    nodes = [node for node in workflow["nodes"] if node["type"] == node_type]
    if len(nodes) != 1:
        raise CheckError(f"the template has {len(nodes)} nodes of type {node_type}, and the check expects one")
    return nodes[0]


def build_check_workflow(template: dict, stand_in_url: str, table_id: str, gate_broken: str | None) -> dict:
    """The relay under its own name, path and credentials, aimed at the stand-in.

    Raises:
        CheckError: *gate_broken* is not one of the gates, or anything still names the real plugin.
    """
    if gate_broken is not None and gate_broken not in GATES_TO_BREAK:
        raise CheckError(f"the gates that can be broken are {', '.join(GATES_TO_BREAK)}")
    handles = (UNKNOWN_SENDER,) if gate_broken == BREAK_ROUTING_TABLE else (KNOWN_SENDER,)
    workflow = render.render_workflow(copy.deepcopy(template), handles, table_id)
    workflow["name"] = CHECK_WORKFLOW_NAME
    workflow["id"] = CHECK_WORKFLOW_ID
    webhook = the_one_node(workflow, WEBHOOK_TYPE)
    webhook["parameters"]["path"] = CHECK_WEBHOOK_PATH
    webhook["credentials"][CREDENTIAL_TYPE] = {"id": CHECK_RELAY_CREDENTIAL_ID, "name": CHECK_RELAY_CREDENTIAL}
    if gate_broken == BREAK_IP_ALLOWLIST:
        webhook["parameters"]["options"]["ipWhitelist"] = NOT_THIS_MAC
    request = the_one_node(workflow, HTTP_REQUEST_TYPE)
    request["parameters"]["url"] = stand_in_url
    request["credentials"][CREDENTIAL_TYPE] = {"id": CHECK_PLUGIN_CREDENTIAL_ID, "name": CHECK_PLUGIN_CREDENTIAL}
    if REAL_PLUGIN_ADDRESS in json.dumps(workflow):
        raise CheckError("the check's workflow would still post to the real plugin")
    return workflow


def check_credentials(relay_secret: str, plugin_secret: str, gate_broken: str | None) -> list[dict]:
    """The check's own two credentials, as n8n's CLI imports them: it encrypts plain data itself."""
    relay_header = WRONG_HEADER if gate_broken == BREAK_WRONG_HEADER else RELAY_SECRET_HEADER
    return [
        {"data": {"name": relay_header, "value": relay_secret}, "id": CHECK_RELAY_CREDENTIAL_ID,
         "name": CHECK_RELAY_CREDENTIAL, "type": CREDENTIAL_TYPE},
        {"data": {"name": PLUGIN_SECRET_HEADER, "value": plugin_secret}, "id": CHECK_PLUGIN_CREDENTIAL_ID,
         "name": CHECK_PLUGIN_CREDENTIAL, "type": CREDENTIAL_TYPE},
    ]


def post_to_webhook(url: str, secret: str | None, body: bytes) -> int:
    """POST *body*; the status it got, or NO_ANSWER_STATUS when nothing answered."""
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[RELAY_SECRET_HEADER] = secret
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    # No proxy from the environment: this is the loopback address, and a proxy would see the secret.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=POST_TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as error:
        # A refusal arrives holding its response open, and the check is refused dozens of times.
        with error:
            return error.code
    except (urllib.error.URLError, TimeoutError):
        return NO_ANSWER_STATUS


def wait_for_webhook(url: str, timing: Timing) -> None:
    """Until the check's webhook answers a post with no secret as its gate should, with a 403.

    Only that counts: a 404 means it is not registered yet, and an n8n still starting answers 200
    to anything. Raises CheckError if it never does, saying what it last answered.
    """
    deadline = time.monotonic() + timing.webhook_wait_seconds
    last_status = NO_ANSWER_STATUS
    while time.monotonic() < deadline:
        last_status = post_to_webhook(url, None, b"{}")
        if last_status == FORBIDDEN_STATUS:
            return
        time.sleep(timing.poll_seconds)
    raise CheckError(f"the check's webhook did not register within {timing.webhook_wait_seconds:g}s; it last answered {last_status}")


def wait_for_new_rows(instance: object, rows_before: int, timing: Timing) -> list[dict]:
    """Whatever rows have appeared since *rows_before*, once one has or the wait runs out."""
    deadline = time.monotonic() + timing.row_wait_seconds
    while time.monotonic() < deadline:
        rows = instance.rows()
        if len(rows) > rows_before:
            # Long enough for a second row to show up, which would be its own failure.
            time.sleep(timing.settle_seconds)
            return instance.rows()[rows_before:]
        time.sleep(timing.poll_seconds)
    return []


def row_problems(case: Case, new_rows: list[dict]) -> list[str]:
    """What is wrong with the rows *case* left. Names fields, and never prints a value of a body."""
    if case.expected_route is None:
        return [f"expected no row, found {len(new_rows)}"] if new_rows else []
    if len(new_rows) != 1:
        return [f"expected one row, found {len(new_rows)}"]
    row = new_rows[0]
    problems = []
    for name, expected in (("route", case.expected_route), ("outcome", case.expected_outcome),
                           ("plugin_status", case.expected_plugin_status)):
        if row.get(name) != expected:
            problems.append(f"{name} was not {expected}")
    if not row.get("received_at"):
        problems.append("received_at is empty")
    if not isinstance(row.get("duration_ms"), int | float):
        problems.append("duration_ms is not a number")
    if CHECK_TEXT in json.dumps(row, ensure_ascii=False):
        problems.append("the text reached the run record")
    return problems


def describe_rows(new_rows: list[dict]) -> str:
    """The row's metadata for the report. These five fields are all a row can hold."""
    if not new_rows:
        return "no row"
    row = new_rows[-1]
    return f"route {row.get('route')}, outcome {row.get('outcome')}, plugin_status {row.get('plugin_status')}, {row.get('duration_ms')} ms"


def run_one_case(case: Case, instance: object, stand_in: StandInPlugin, webhook_url: str, relay_secret: str, timing: Timing) -> tuple[bool, str]:
    """Post *case* and judge what it left. Returns whether it held, and a line with no value in it."""
    stand_in.answer_status = case.answer_status
    stand_in.answer_delay_seconds = case.answer_delay_seconds
    stand_in.set_running(case.stand_in_up)
    received_before = len(stand_in.received)
    rows_before = len(instance.rows())
    document = {"sender": case.sender, "sent_at": datetime.now(timezone.utc).isoformat(), "text": CHECK_TEXT}
    secret = {SECRET_NONE: None, SECRET_WRONG: secrets.token_hex(32), SECRET_RIGHT: relay_secret}[case.secret]
    status = post_to_webhook(webhook_url, secret, json.dumps(document, ensure_ascii=False).encode("utf-8"))
    if case.expected_route is None:
        time.sleep(timing.settle_seconds)
        new_rows = instance.rows()[rows_before:]
    else:
        new_rows = wait_for_new_rows(instance, rows_before, timing)
    problems = [f"status was not {case.expected_status}"] if status != case.expected_status else []
    problems += row_problems(case, new_rows)
    arrived = stand_in.received[received_before:]
    if len(arrived) != (1 if case.reaches_plugin else 0):
        problems.append(f"the stand-in received {len(arrived)} posts")
    elif arrived and json.loads(arrived[0]) != document:
        problems.append("the body the stand-in received was not the body posted")
    verdict = "pass" if not problems else f"FAIL: {'; '.join(problems)}"
    return not problems, f"{case.name}: {verdict} (status {status}; {describe_rows(new_rows)})"


def record_the_over_long_body(instance: object, stand_in: StandInPlugin, webhook_url: str, relay_secret: str, timing: Timing, report: Report) -> None:
    """The sixth case: not judged, since what it answers is the thing to find out."""
    stand_in.answer_status = ACCEPTED_STATUS
    stand_in.answer_delay_seconds = 0.0
    stand_in.set_running(True)
    received_before = len(stand_in.received)
    rows_before = len(instance.rows())
    document = {"sender": KNOWN_SENDER, "sent_at": datetime.now(timezone.utc).isoformat(), "text": "x" * LONG_TEXT_LENGTH}
    body = json.dumps(document).encode("utf-8")
    status = post_to_webhook(webhook_url, relay_secret, body)
    new_rows = wait_for_new_rows(instance, rows_before, timing)
    sizes = ", ".join(f"{len(received)} bytes" for received in stand_in.received[received_before:]) or "nothing"
    report(f"over 65,536 bytes ({len(body)} posted): webhook answered {status}, stand-in received {sizes}; {describe_rows(new_rows)}")


def exercise(instance: object, stand_in: StandInPlugin, template: dict, table_id: str, gate_broken: str | None,
             timing: Timing, report: Report, webhook_url: str) -> int:
    """Everything between the clean-ups: install, every case. Returns the exit code."""
    relay_secret, plugin_secret = secrets.token_hex(32), secrets.token_hex(32)
    report(f"publishing the check's workflow, which takes about half a minute{'' if gate_broken is None else f'; {gate_broken} is broken on purpose'}")
    if instance.leftovers():
        report("removing what an interrupted run left")
        instance.remove_leftovers()
    workflow = build_check_workflow(template, stand_in.url, table_id, gate_broken)
    instance.install(workflow, check_credentials(relay_secret, plugin_secret, gate_broken))
    wait_for_webhook(webhook_url, timing)
    cases = relay_cases(timing)
    passed = 0
    for case in cases:
        held, line = run_one_case(case, instance, stand_in, webhook_url, relay_secret, timing)
        passed += held
        report(line)
    record_the_over_long_body(instance, stand_in, webhook_url, relay_secret, timing, report)
    if any(secret != plugin_secret for secret in stand_in.presented_secrets):
        report("FAIL: a post reached the stand-in without the plugin credential's secret")
        passed -= 1
    report(f"{passed} of {len(cases)} cases held")
    return 0 if passed == len(cases) else EXIT_FAILED


def run_check(instance: object, template: dict, table_id: str, gate_broken: str | None, timing: Timing, report: Report,
              webhook_url: str = CHECK_WEBHOOK_URL) -> int:
    """The whole run. Whatever happens, the clean-up runs before anything else is said."""
    stand_in = StandInPlugin()
    stand_in.start()
    code = EXIT_FAILED
    clean_up_problem = None
    try:
        code = exercise(instance, stand_in, template, table_id, gate_broken, timing, report, webhook_url)
    except CheckError as error:
        report(f"FAIL: {error}")
    finally:
        stand_in.stop()
        try:
            instance.remove_leftovers()
        except CheckError as error:
            clean_up_problem = str(error)
    if clean_up_problem:
        report(f"FAIL: the clean-up did not finish, so the next run must remove what is left behind: {clean_up_problem}")
        return EXIT_FAILED
    if instance.leftovers():
        report("FAIL: something was left behind; the next run removes it first")
        return EXIT_FAILED
    if gate_broken is None:
        return code
    if code == 0:
        report(f"FAIL: {gate_broken} was broken on purpose and nothing noticed")
        return EXIT_BREAK_UNNOTICED
    report(f"{gate_broken} was broken on purpose and seen to fail, as it should be")
    return code


class N8nInstance:
    """The real n8n: every command as `_n8n` or through n8n-cli.sh, and no secret on a command line."""

    def __init__(self, table_id: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                 home: str = N8N_HOME, database: str = DATABASE_PATH, unpublish_wait_seconds: float = UNPUBLISH_WAIT_SECONDS,
                 poll_seconds: float = UNPUBLISH_POLL_SECONDS):
        if not workflow_template.TABLE_ID_PATTERN.match(table_id):
            raise CheckError("the check table's id holds characters n8n's ids do not")
        self.table_name = f"data_table_user_{table_id}"
        self.run = run
        self.home = home
        self.database = database
        self.unpublish_wait_seconds = unpublish_wait_seconds
        self.poll_seconds = poll_seconds

    def _as_n8n(self, *command: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.run(["sudo", "-u", N8N_USER, *command], cwd=self.home, input=input_text, capture_output=True, text=True, check=False)

    def _read(self, sql: str) -> list[dict]:
        result = self._as_n8n(SQLITE_PATH, "-readonly", "-json", "-cmd", SQLITE_BUSY_WAIT, self.database, sql)
        if result.returncode != 0:
            raise CheckError(f"sqlite3 could not read n8n's database, exit {result.returncode}")
        return json.loads(result.stdout) if result.stdout.strip() else []

    def _count(self, sql: str) -> int:
        rows = self._read(f"SELECT ({sql}) AS found")
        return rows[0]["found"] if rows else 0

    def _cli(self, *arguments: str) -> None:
        result = self.run([str(CLI_SCRIPT), *arguments], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CheckError(f"n8n {arguments[0]} failed, exit {result.returncode}; try it by hand through n8n-cli.sh to see why")

    def rows(self) -> list[dict]:
        """The check table's rows, oldest first."""
        return self._read(f"SELECT received_at, route, outcome, plugin_status, duration_ms FROM {self.table_name} ORDER BY id")

    def _publication_rows_left(self) -> int:
        """The row that says the check's workflow is published, the last thing the daemon removes."""
        return self._count(f"SELECT COUNT(*) FROM workflow_published_version WHERE workflowId = '{CHECK_WORKFLOW_ID}'")

    def leftovers(self) -> int:
        """How many things of the check's are in n8n now, the rows nothing cascades to included."""
        return self._count(
            f"SELECT (SELECT COUNT(*) FROM workflow_entity WHERE id = '{CHECK_WORKFLOW_ID}')"
            f" + (SELECT COUNT(*) FROM credentials_entity WHERE id IN ('{CHECK_RELAY_CREDENTIAL_ID}', '{CHECK_PLUGIN_CREDENTIAL_ID}'))"
            f" + (SELECT COUNT(*) FROM workflow_published_version WHERE workflowId = '{CHECK_WORKFLOW_ID}')"
            f" + (SELECT COUNT(*) FROM webhook_entity WHERE workflowId = '{CHECK_WORKFLOW_ID}')"
            f" + (SELECT COUNT(*) FROM workflow_publication_outbox WHERE workflowId = '{CHECK_WORKFLOW_ID}')"
            f" + (SELECT COUNT(*) FROM workflow_statistics WHERE workflowId = '{CHECK_WORKFLOW_ID}')"
            f" + (SELECT COUNT(*) FROM {self.table_name})")

    def _unpublish(self) -> None:
        """Unpublish the check's workflow and wait for the daemon to finish taking it down.

        n8n's foreign key refuses to delete a published workflow, and only the running daemon
        removes the row that says it is one, so a delete before that fails, and fails again.
        """
        self._cli("unpublish:workflow", f"--id={CHECK_WORKFLOW_ID}")
        deadline = time.monotonic() + self.unpublish_wait_seconds
        while self._publication_rows_left():
            if time.monotonic() >= deadline:
                raise CheckError(f"n8n had not finished unpublishing the check's workflow after {self.unpublish_wait_seconds:g}s")
            time.sleep(self.poll_seconds)

    def _sweep_hand_over_folders(self) -> None:
        """A run killed outright leaves its folder, which holds that run's random secrets."""
        result = self._as_n8n("/usr/bin/find", self.home, "-maxdepth", "1", "-type", "d", "-name", f"{HAND_OVER_FOLDER_PREFIX}*",
                              "-exec", "/bin/rm", "-rf", "{}", "+")
        if result.returncode != 0:
            raise CheckError(f"could not remove the check's folders from {self.home}, exit {result.returncode}")

    def remove_leftovers(self) -> None:
        """Everything of the check's, each by its own id, all of it or none of it.

        Three tables have no foreign key to the workflow, so nothing would clear them for it. Only
        the check's own table is emptied, and that one is the check's alone.
        """
        self._sweep_hand_over_folders()
        if self._count(f"SELECT COUNT(*) FROM workflow_entity WHERE id = '{CHECK_WORKFLOW_ID}'"):
            self._unpublish()
        sql = ("PRAGMA foreign_keys=ON; BEGIN;"
               f" DELETE FROM webhook_entity WHERE workflowId = '{CHECK_WORKFLOW_ID}';"
               f" DELETE FROM workflow_publication_outbox WHERE workflowId = '{CHECK_WORKFLOW_ID}';"
               f" DELETE FROM workflow_statistics WHERE workflowId = '{CHECK_WORKFLOW_ID}';"
               f" DELETE FROM workflow_entity WHERE id = '{CHECK_WORKFLOW_ID}';"
               f" DELETE FROM credentials_entity WHERE id IN ('{CHECK_RELAY_CREDENTIAL_ID}', '{CHECK_PLUGIN_CREDENTIAL_ID}');"
               f" DELETE FROM {self.table_name};"
               " COMMIT;")
        result = self._as_n8n(SQLITE_PATH, "-cmd", SQLITE_BUSY_WAIT, self.database, sql)
        if result.returncode != 0:
            raise CheckError(f"sqlite3 could not remove the check's leftovers, exit {result.returncode}")

    def install(self, workflow: dict, credentials: list[dict]) -> None:
        """Import the credentials and the workflow and publish it. The copies go whatever happens."""
        made = self._as_n8n("/usr/bin/mktemp", "-d", f"{self.home}/{HAND_OVER_FOLDER_PREFIX}XXXXXX")
        directory = made.stdout.strip()
        if made.returncode != 0 or not directory:
            raise CheckError(f"could not make a folder {N8N_USER} can read")
        try:
            for name, content, command in (("credentials.json", credentials, "import:credentials"), ("workflow.json", workflow, "import:workflow")):
                path = f"{directory}/{name}"
                written = self._as_n8n("/bin/sh", "-c", 'umask 077; cat >"$1"', "sh", path, input_text=json.dumps(content))
                if written.returncode != 0:
                    raise CheckError(f"could not hand {name} to {N8N_USER}")
                self._cli(command, f"--input={path}")
            self._cli("publish:workflow", f"--id={CHECK_WORKFLOW_ID}")
        finally:
            self._as_n8n("/bin/rm", "-rf", directory)


def check_table_column_problems(columns: dict[str, str]) -> list[str]:
    """How *columns*, each name with n8n's type for it, differ from the run record's five."""
    problems = [f"{name} is missing" for name in sorted(CHECK_TABLE_COLUMNS) if name not in columns]
    problems += [f"{name} is a {columns[name]} column, and must be a {wanted} one"
                 for name, wanted in sorted(CHECK_TABLE_COLUMNS.items()) if name in columns and columns[name] != wanted]
    problems += [f"{name} is not one of the run record's columns" for name in sorted(set(columns) - set(CHECK_TABLE_COLUMNS))]
    return problems


def find_check_table(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, home: str = N8N_HOME,
                     database: str = DATABASE_PATH) -> str:
    """The id of the `run_record_check` Data Table, once its columns are the run record's.

    Raises:
        CheckError: there is not exactly one such table, or its columns differ, saying how to put it right.
    """
    def read(sql: str) -> str:
        result = run(["sudo", "-u", N8N_USER, SQLITE_PATH, "-readonly", "-json", "-cmd", SQLITE_BUSY_WAIT, database, sql],
                     cwd=home, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CheckError(f"sqlite3 could not read n8n's database, exit {result.returncode}")
        return result.stdout

    found = read(f"SELECT id FROM data_table WHERE name = '{CHECK_TABLE_NAME}'")
    identifiers = [row["id"] for row in json.loads(found)] if found.strip() else []
    if len(identifiers) != 1:
        raise CheckError(f"found {len(identifiers)} Data Tables named {CHECK_TABLE_NAME}, and the check needs one; n8n/README.md says how to make it")
    identifier = identifiers[0]
    if not workflow_template.TABLE_ID_PATTERN.match(identifier):
        raise CheckError(f"the {CHECK_TABLE_NAME} Data Table's id is not one n8n makes")
    listed = read(f"SELECT name, type FROM data_table_column WHERE dataTableId = '{identifier}'")
    columns = {row["name"]: row["type"] for row in json.loads(listed)} if listed.strip() else {}
    problems = check_table_column_problems(columns)
    if problems:
        raise CheckError(f"the {CHECK_TABLE_NAME} Data Table's columns are not the run record's: {'; '.join(problems)}. "
                         "Fix them in the editor; n8n/README.md lists the five")
    return identifier


def interrupt_on_termination() -> None:
    """Treat a closed terminal or a `kill` as Ctrl-C, so the clean-up runs for those too."""
    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    for signal_number in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signal_number, interrupted)


def main() -> int:
    """Parse the arguments and run the check."""
    parser = argparse.ArgumentParser(description="Prove the relay end to end against a stand-in plugin.")
    parser.add_argument("--break", dest="gate_broken", choices=GATES_TO_BREAK, help="break this gate on purpose; the run should go red")
    arguments = parser.parse_args()
    interrupt_on_termination()
    try:
        template = workflow_template.read_json(RELAY_TEMPLATE)
        table_id = find_check_table()
        instance = N8nInstance(table_id)
    except (CheckError, workflow_template.TemplateError) as error:
        print(f"not run: {error}", file=sys.stderr)
        return EXIT_FAILED
    return run_check(instance, template, table_id, arguments.gate_broken, Timing(), lambda line: print(line, flush=True))


if __name__ == "__main__":
    sys.exit(main())
