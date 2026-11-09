"""The relay workflow as a template: what keeps it value-free, and its render and export scripts.

The template is the one workflow file in git, so this suite is the gate between an edit in n8n's
editor and a repository holding a handle or a secret, or a workflow that could store a text or
run a command. Every rule is proven by a test that breaks a copy of the template and expects
that rule to fire. The scripts are driven as the README runs them, file to file, with no n8n
present, under whichever python3 runs the suite.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import PARTNER_PHONE, ZACK_EMAIL, ZACK_PHONE, plugin_module, write_allow_list

handles = plugin_module("handles")

REPOSITORY = Path(__file__).resolve().parent.parent
RELAY_TEMPLATE = REPOSITORY / "n8n" / "workflows" / "imessage-relay.json"
FIXTURE_TEMPLATE = REPOSITORY / "tests" / "fixtures" / "n8n-relay-template.json"
RENDER_SCRIPT = REPOSITORY / "n8n" / "render.py"
EXPORT_SCRIPT = REPOSITORY / "n8n" / "export.py"
TIMEOUT_SECONDS = 60
EXIT_REFUSED = 2
OWNER_ONLY_MODE = 0o600
RUN_RECORD_TABLE_ID = "kQ3xV9mT2bLw7ZpA"
HANDLES_TOKEN = "__ZACK_HANDLES_JSON__"
TABLE_TOKEN = "__RUN_RECORD_TABLE_ID__"
RELAY_CREDENTIAL = "Shortcuts relay secret"
PLUGIN_CREDENTIAL = "Hermes iMessage plugin secret"
WEBHOOK_TYPE = "n8n-nodes-base.webhook"
HTTP_REQUEST_TYPE = "n8n-nodes-base.httpRequest"
DATA_TABLE_TYPE = "n8n-nodes-base.dataTable"
SET_TYPE = "n8n-nodes-base.set"
SWITCH_TYPE = "n8n-nodes-base.switch"
CREDENTIAL_TYPE = "httpHeaderAuth"
# The closed list from issue #8. Anything else, Code and Execute Command first, fails.
ALLOWED_NODE_TYPES = frozenset({WEBHOOK_TYPE, SET_TYPE, SWITCH_TYPE, "n8n-nodes-base.if", HTTP_REQUEST_TYPE,
                                DATA_TABLE_TYPE, "n8n-nodes-base.noOp"})
EXPLICIT_SETTINGS = {"saveDataErrorExecution": "none", "saveDataSuccessExecution": "none",
                     "saveExecutionProgress": False, "saveManualExecutions": False}
RUN_RECORD_COLUMNS = frozenset({"received_at", "route", "outcome", "plugin_status", "duration_ms"})
PLUGIN_URL = "http://127.0.0.1:8645/imessage"
WEBHOOK_PATH = "imessage-relay"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "[::1]", "localhost"})
SECRET_HEADERS = ("x-hermes-secret", "x-relay-secret")
SECRET_KEY_PATTERN = re.compile(r"secret|password|token|apikey", re.IGNORECASE)
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
PHONE_PATTERN = re.compile(r"\+?\d(?:[\s().\- ]*\d){6,}")
EMAIL_PATTERN = re.compile(r"[^\s@'\"(),]+@[^\s@'\"(),]+\.[^\s@'\"(),]+")
HEX_PATTERN = re.compile(r"[0-9a-fA-F]{32,}")
URL_PATTERN = re.compile(r"https?://([^/\s'\":]+|\[[^\]\s]+\])")
TEXT_READ_PATTERN = re.compile(r"""\.text\b|\[\s*['"]text['"]\s*\]""")
BODY_READ_PATTERN = re.compile(r"\$json\.body\b")
# An expression handed a whole item, which holds the text wherever a node keeps other fields, and
# on an error output always: n8n merges the input item in (handleNodeErrorOutput in n8n-core).
WHOLE_ITEM_READ_PATTERN = re.compile(r"(?:\$json|\.json)(?!\s*(?:\.|\?\.|\[))|\$input\b|\$items\b")
# The one read of the body the relay may make beyond the sender: passing it through unchanged.
PASS_THROUGH_BODY = "={{ $json.body }}"
SENDER_READ = "$json.body.sender"
SHAPES_OF_THE_ONE_NUMBER = ("+15550100199", "(555) 010-0199", "5550100199", "15550100199", "1 555 010 0199",
                            "555.010.0199", "  +15550100199  ", "555 010 0199")
SHAPES_OF_THE_ONE_EMAIL = ("zack@example.com", "ZACK@Example.COM", "  zack@example.com ")
NOT_ALLOWED_SENDERS = (PARTNER_PHONE, "someone@example.com", "Zack Kuptsow", "", "0199")
# Hex and letters that are long and plainly no one's: the shape of the relay's own secret.
RELAY_SECRET_EXAMPLE = "0123456789abcdef" * 4


def every_string(node: object, location: str = "") -> list[tuple[str, str]]:
    """Every string in *node*, keys and values, with where it was found."""
    if isinstance(node, str):
        return [(location, node)]
    if isinstance(node, dict):
        found: list[tuple[str, str]] = []
        for key, value in node.items():
            found += [*every_string(key, f"{location}/<key>"), *every_string(value, f"{location}/{key}")]
        return found
    if isinstance(node, list):
        return [pair for index, value in enumerate(node) for pair in every_string(value, f"{location}/{index}")]
    return []


def nodes_of_type(template: dict, node_type: str) -> list[dict]:
    return [node for node in template.get("nodes", []) if node.get("type") == node_type]


def value_problems(template: dict) -> list[str]:
    """A string that looks like a handle, a secret, or a URL off the Mac."""
    problems = []
    for location, string in every_string(template):
        without_ids = UUID_PATTERN.sub("", string)
        if PHONE_PATTERN.search(without_ids):
            problems.append(f"{location}: looks like a phone number")
        if EMAIL_PATTERN.search(without_ids):
            problems.append(f"{location}: looks like an email address")
        if HEX_PATTERN.search(without_ids):
            problems.append(f"{location}: looks like a secret")
        for host in URL_PATTERN.findall(without_ids):
            if host not in LOOPBACK_HOSTS:
                problems.append(f"{location}: names a URL beyond the loopback address")
    return problems


def literal_secret_problems(node: object, location: str = "") -> list[str]:
    """Any key that names a secret and holds a non-empty literal, anywhere under *node*."""
    if isinstance(node, list):
        return [problem for index, value in enumerate(node) for problem in literal_secret_problems(value, f"{location}/{index}")]
    if not isinstance(node, dict):
        return []
    problems = []
    for key, value in node.items():
        if SECRET_KEY_PATTERN.search(key) and isinstance(value, str) and value:
            problems.append(f"{location}/{key}: holds a literal")
        problems += literal_secret_problems(value, f"{location}/{key}")
    return problems


def credential_problems(template: dict) -> list[str]:
    """The Webhook and the HTTP Request authenticate by a credential named exactly, carrying no id."""
    problems = literal_secret_problems(template.get("nodes", []))
    for node_type, wanted_authentication, wanted_name in (
            (WEBHOOK_TYPE, {"authentication": "headerAuth"}, RELAY_CREDENTIAL),
            (HTTP_REQUEST_TYPE, {"authentication": "genericCredentialType", "genericAuthType": CREDENTIAL_TYPE}, PLUGIN_CREDENTIAL)):
        for node in nodes_of_type(template, node_type):
            parameters = node.get("parameters", {})
            for key, value in wanted_authentication.items():
                if parameters.get(key) != value:
                    problems.append(f"{node['name']}: {key} is not the credential form")
            reference = node.get("credentials", {}).get(CREDENTIAL_TYPE)
            if not isinstance(reference, dict) or reference.get("name") != wanted_name:
                problems.append(f"{node['name']}: the credential is not named {wanted_name}")
            elif "id" in reference:
                problems.append(f"{node['name']}: the credential reference carries an id")
            for header in parameters.get("headerParameters", {}).get("parameters", []):
                if str(header.get("name", "")).lower() in SECRET_HEADERS:
                    problems.append(f"{node['name']}: a header carries a secret inline")
    return problems


def text_problems(template: dict) -> list[str]:
    """The text is passed through whole and stored nowhere: nothing reads it, no column can hold it."""
    problems = []
    for location, string in every_string(template.get("nodes", [])):
        if TEXT_READ_PATTERN.search(string):
            problems.append(f"{location}: reads the text")
        if BODY_READ_PATTERN.search(string) and string != PASS_THROUGH_BODY and SENDER_READ not in string:
            problems.append(f"{location}: reads the body beyond passing it through")
        if WHOLE_ITEM_READ_PATTERN.search(string):
            problems.append(f"{location}: reads a whole item, and so the text with it")
    for node in nodes_of_type(template, DATA_TABLE_TYPE):
        columns = node.get("parameters", {}).get("columns", {}).get("value", {})
        if set(columns) != RUN_RECORD_COLUMNS:
            problems.append(f"{node['name']}: the columns are not the run record's five")
        for location, string in every_string(columns):
            if "body" in string:
                problems.append(f"{node['name']}{location}: writes the body to the table")
    return problems


def gate_problems(template: dict) -> list[str]:
    """The Webhook's gates and the HTTP Request's decided behaviour, each as issue #8 sets it."""
    problems = []
    webhooks = nodes_of_type(template, WEBHOOK_TYPE)
    requests = nodes_of_type(template, HTTP_REQUEST_TYPE)
    if len(webhooks) != 1 or len(requests) != 1:
        return [f"expected one Webhook and one HTTP Request, found {len(webhooks)} and {len(requests)}"]
    webhook = webhooks[0].get("parameters", {})
    if webhook.get("path") != WEBHOOK_PATH:
        problems.append("the Webhook's path is not the designed one")
    if webhook.get("responseMode", "onReceived") != "onReceived":
        problems.append("the Webhook does not respond immediately")
    allowed_addresses = {entry.strip() for entry in str(webhook.get("options", {}).get("ipWhitelist", "")).split(",")}
    if allowed_addresses != {"127.0.0.1", "::1"}:
        problems.append("the Webhook's IP allowlist is not the loopback addresses alone")
    request = requests[0]
    parameters = request.get("parameters", {})
    if parameters.get("url") != PLUGIN_URL:
        problems.append("the HTTP Request does not post to the plugin")
    if parameters.get("jsonBody") != PASS_THROUGH_BODY:
        problems.append("the HTTP Request does not pass the body through unchanged")
    options = parameters.get("options", {})
    if options.get("timeout") != 5000:
        problems.append("the HTTP Request's timeout is not 5 seconds")
    response = options.get("response", {}).get("response", {})
    if response.get("fullResponse") is not True or response.get("neverError") is not True:
        problems.append("the HTTP Request does not return every status as an item")
    if request.get("onError") != "continueErrorOutput":
        problems.append("a failed connection does not reach the run record")
    if request.get("retryOnFail"):
        problems.append("the HTTP Request retries")
    return problems


def relay_problems(template: dict) -> list[str]:
    """Why *template* may not be the relay, or nothing if it may."""
    problems = [f"{node.get('name')}: {node.get('type')} is off the closed list" for node in template.get("nodes", [])
                if node.get("type") not in ALLOWED_NODE_TYPES]
    settings = template.get("settings", {})
    problems += [f"settings: {key} is not {value!r}" for key, value in EXPLICIT_SETTINGS.items()
                 if type(settings.get(key)) is not type(value) or settings.get(key) != value]
    serialised = json.dumps(template)
    problems += [f"the token {token} is missing" for token in (HANDLES_TOKEN, TABLE_TOKEN) if token not in serialised]
    return [*problems, *value_problems(template), *credential_problems(template), *text_problems(template),
            *gate_problems(template)]


def template_under_test() -> dict:
    """The real relay once it exists; until the editor has made it, the fixture holds its shape."""
    path = RELAY_TEMPLATE if RELAY_TEMPLATE.exists() else FIXTURE_TEMPLATE
    return json.loads(path.read_text(encoding="utf-8"))


class RelayTemplateTest(unittest.TestCase):
    def test_the_relay_exists_and_passes_every_rule(self):
        self.assertTrue(RELAY_TEMPLATE.exists(), "the relay has not been exported from the editor yet")
        self.assertEqual(relay_problems(json.loads(RELAY_TEMPLATE.read_text(encoding="utf-8"))), [])

    def test_the_relay_is_stored_canonically(self):
        text = RELAY_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def test_the_fixture_passes_every_rule(self):
        self.assertEqual(relay_problems(json.loads(FIXTURE_TEMPLATE.read_text(encoding="utf-8"))), [])


class RelayRuleTest(unittest.TestCase):
    """Each rule, seen to fire on a copy broken in the one way it refuses."""

    def setUp(self):
        self.template = copy.deepcopy(template_under_test())
        self.assertEqual(relay_problems(self.template), [], "the copy must start clean, or nothing below shows a rule fired")

    def node(self, node_type: str) -> dict:
        return nodes_of_type(self.template, node_type)[0]

    def assert_refused(self, fragment: str) -> None:
        problems = relay_problems(self.template)
        self.assertTrue(any(fragment in problem for problem in problems), f"no problem names {fragment!r}: {problems}")

    def set_string(self, value: str) -> None:
        """Puts *value* where a builder would most plausibly leave it: a Set node's field."""
        self.node(SET_TYPE)["parameters"]["assignments"]["assignments"][0]["value"] = value

    def test_a_node_off_the_closed_list_is_refused(self):
        for node_type in ("n8n-nodes-base.code", "n8n-nodes-base.executeCommand", "n8n-nodes-base.ssh",
                          "n8n-nodes-base.readWriteFile", "n8n-nodes-base.function"):
            with self.subTest(node_type):
                changed = copy.deepcopy(self.template)
                changed["nodes"].append({"name": "Added", "type": node_type, "parameters": {}})
                self.assertTrue(any("off the closed list" in problem for problem in relay_problems(changed)))

    def test_a_settings_value_other_than_the_explicit_one_is_refused(self):
        for key, wrong_value in (("saveDataSuccessExecution", "all"), ("saveDataSuccessExecution", "DEFAULT"),
                                 ("saveDataErrorExecution", "all"), ("saveManualExecutions", True),
                                 ("saveManualExecutions", "DEFAULT"), ("saveManualExecutions", 0), ("saveExecutionProgress", True)):
            with self.subTest(key=key, wrong_value=wrong_value):
                changed = copy.deepcopy(self.template)
                changed["settings"][key] = wrong_value
                self.assertTrue(any(key in problem for problem in relay_problems(changed)))

    def test_a_settings_value_left_out_is_refused(self):
        for key in EXPLICIT_SETTINGS:
            with self.subTest(key):
                changed = copy.deepcopy(self.template)
                del changed["settings"][key]
                self.assertTrue(any(key in problem for problem in relay_problems(changed)))

    def test_a_string_like_a_phone_number_is_refused(self):
        for phone in (ZACK_PHONE, "(555) 010-0199", "555-010-0199"):
            with self.subTest(phone):
                self.set_string(phone)
                self.assert_refused("phone number")

    def test_a_string_like_an_email_is_refused(self):
        self.set_string(ZACK_EMAIL)
        self.assert_refused("email address")

    def test_a_long_hex_string_is_refused(self):
        self.set_string(RELAY_SECRET_EXAMPLE)
        self.assert_refused("secret")

    def test_a_url_beyond_the_loopback_address_is_refused(self):
        self.set_string("https://household.example.net/ingest")
        self.assert_refused("beyond the loopback address")

    def test_a_private_address_is_refused_as_beyond_the_loopback_address(self):
        self.set_string("http://192.168.1.20:8645/imessage")
        self.assert_refused("beyond the loopback address")

    def test_a_webhook_with_no_credential_form_is_refused(self):
        self.node(WEBHOOK_TYPE)["parameters"]["authentication"] = "none"
        self.assert_refused("credential form")

    def test_a_request_with_no_credential_form_is_refused(self):
        self.node(HTTP_REQUEST_TYPE)["parameters"]["authentication"] = "none"
        self.assert_refused("credential form")

    def test_a_secret_header_written_inline_is_refused(self):
        for header in ("X-Hermes-Secret", "x-relay-secret"):
            with self.subTest(header):
                self.node(HTTP_REQUEST_TYPE)["parameters"]["headerParameters"] = {
                    "parameters": [{"name": header, "value": "={{ $json.anything }}"}]}
                self.assert_refused("inline")

    def test_a_key_naming_a_secret_and_holding_a_literal_is_refused(self):
        for key in ("secret", "clientSecret", "password", "token", "apiKey"):
            with self.subTest(key):
                changed = copy.deepcopy(self.template)
                nodes_of_type(changed, HTTP_REQUEST_TYPE)[0]["parameters"]["options"][key] = "literal"
                self.assertTrue(any("holds a literal" in problem for problem in relay_problems(changed)))

    def test_a_credential_reference_carrying_an_id_is_refused(self):
        self.node(WEBHOOK_TYPE)["credentials"][CREDENTIAL_TYPE]["id"] = "sOmEcReDeNtIaLiD"
        self.assert_refused("carries an id")

    def test_a_credential_of_another_name_is_refused(self):
        for node_type in (WEBHOOK_TYPE, HTTP_REQUEST_TYPE):
            with self.subTest(node_type):
                changed = copy.deepcopy(self.template)
                nodes_of_type(changed, node_type)[0]["credentials"][CREDENTIAL_TYPE]["name"] = "Another credential"
                self.assertTrue(any("is not named" in problem for problem in relay_problems(changed)))

    def test_the_two_credentials_swapped_are_refused(self):
        # The plugin's secret on the webhook and the relay's on the request read as valid, and each fails only live.
        self.node(WEBHOOK_TYPE)["credentials"][CREDENTIAL_TYPE]["name"] = PLUGIN_CREDENTIAL
        self.node(HTTP_REQUEST_TYPE)["credentials"][CREDENTIAL_TYPE]["name"] = RELAY_CREDENTIAL
        self.assert_refused("is not named")

    def test_a_missing_template_token_is_refused(self):
        for token, replacement in ((HANDLES_TOKEN, "[]"), (TABLE_TOKEN, "literal")):
            with self.subTest(token):
                changed = json.loads(json.dumps(self.template).replace(token, replacement))
                self.assertTrue(any(token in problem for problem in relay_problems(changed)))

    def test_an_expression_reading_the_text_is_refused(self):
        for expression in ("={{ $json.body.text }}", "={{ $json.text }}", "={{ $json['text'] }}", '={{ $json["text"] }}'):
            with self.subTest(expression):
                self.set_string(expression)
                self.assert_refused("reads the text")

    def test_an_expression_reading_the_body_otherwise_is_refused(self):
        self.set_string("={{ $json.body.sent_at }}")
        self.assert_refused("beyond passing it through")

    def test_an_expression_handed_a_whole_item_is_refused(self):
        for expression in ("={{ JSON.stringify($json) }}", "={{ $json }}", "={{ String($json) }}", "={{ $input.first() }}",
                           "={{ $items() }}", "={{ JSON.stringify($('Routing table').first().json) }}"):
            with self.subTest(expression):
                self.set_string(expression)
                self.assert_refused("whole item")

    def test_a_named_field_of_an_item_is_not_a_whole_item(self):
        for expression in ("={{ $json.received_at }}", "={{ $json.error?.message }}", "={{ $json['received_at'] }}",
                           "={{ $('Routing table').first().json.received_at }}"):
            with self.subTest(expression):
                self.set_string(expression)
                self.assertFalse([problem for problem in relay_problems(self.template) if "whole item" in problem])

    def test_a_sixth_column_is_refused_since_it_could_store_the_text(self):
        self.node(DATA_TABLE_TYPE)["parameters"]["columns"]["value"]["note"] = "={{ $json.note }}"
        self.assert_refused("run record's five")

    def test_a_column_writing_the_body_is_refused(self):
        self.node(DATA_TABLE_TYPE)["parameters"]["columns"]["value"]["route"] = "={{ $json.body }}"
        self.assert_refused("writes the body")

    def test_each_gate_set_otherwise_is_refused(self):
        def set_webhook(name: str, value: object):
            return lambda: self.node(WEBHOOK_TYPE)["parameters"].__setitem__(name, value)

        def set_request(name: str, value: object):
            return lambda: self.node(HTTP_REQUEST_TYPE)["parameters"].__setitem__(name, value)

        def set_request_option(name: str, value: object):
            return lambda: self.node(HTTP_REQUEST_TYPE)["parameters"]["options"].__setitem__(name, value)

        breaks = {
            "the Webhook's path": set_webhook("path", "another-path"),
            "does not respond immediately": set_webhook("responseMode", "lastNode"),
            "IP allowlist": set_webhook("options", {"ipWhitelist": "127.0.0.1,::1,192.168.1.0/24"}),
            "does not post to the plugin": set_request("url", "http://127.0.0.1:8646/imessage"),
            "does not pass the body through": set_request("jsonBody", '={{ {"sender": $json.body.sender} }}'),
            "timeout is not 5 seconds": set_request_option("timeout", 300000),
            "every status as an item": set_request_option("response", {"response": {"fullResponse": True}}),
            "does not reach the run record": lambda: self.node(HTTP_REQUEST_TYPE).pop("onError"),
            "retries": lambda: self.node(HTTP_REQUEST_TYPE).__setitem__("retryOnFail", True),
        }
        for fragment, break_it in breaks.items():
            with self.subTest(fragment):
                original = copy.deepcopy(self.template)
                break_it()
                self.assert_refused(fragment)
                self.template = original


class SwitchExpressionTest(unittest.TestCase):
    """The Switch's expression and `handles.canonical_handle` must agree on who is who."""

    def setUp(self):
        self.node_path = shutil.which("node")
        self.assertIsNotNone(self.node_path, "no node on PATH, so the expression cannot be run")
        switch = nodes_of_type(template_under_test(), SWITCH_TYPE)[0]
        rule = switch["parameters"]["rules"]["values"][0]["conditions"]["conditions"][0]["leftValue"]
        self.expression = rule.removeprefix("={{").removesuffix("}}").strip()
        self.allowed = [ZACK_PHONE, ZACK_EMAIL]

    def routed_to_zack(self, sender: str) -> bool:
        program = (f"const $json = {json.dumps({'zack_handles': self.allowed, 'body': {'sender': sender}})};\n"
                   f"process.stdout.write(JSON.stringify({self.expression}));")
        result = subprocess.run([self.node_path, "-e", program], capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, check=True)
        return json.loads(result.stdout)

    def on_the_allow_list(self, sender: str) -> bool:
        try:
            return handles.canonical_handle(sender) in self.allowed
        except handles.InvalidHandleError:
            return False

    def test_every_written_form_of_a_known_handle_routes_to_zack_as_the_plugin_would_allow_it(self):
        for sender in (*SHAPES_OF_THE_ONE_NUMBER, *SHAPES_OF_THE_ONE_EMAIL):
            with self.subTest(sender):
                self.assertTrue(self.on_the_allow_list(sender), "the fixture's own premise")
                self.assertTrue(self.routed_to_zack(sender))

    def test_anyone_else_is_dropped_as_the_plugin_would_drop_them(self):
        for sender in NOT_ALLOWED_SENDERS:
            with self.subTest(sender):
                self.assertFalse(self.on_the_allow_list(sender), "the fixture's own premise")
                self.assertFalse(self.routed_to_zack(sender))


class OutcomeExpressionTest(unittest.TestCase):
    """The two outcome expressions, run in node against the item n8n really hands each."""

    def setUp(self):
        self.node_path = shutil.which("node")
        self.assertIsNotNone(self.node_path, "no node on PATH, so the expressions cannot be run")
        self.failure = self.field_of("Record failure", "outcome")
        self.forward = self.field_of("Record forward", "outcome")

    def field_of(self, node_name: str, field_name: str) -> str:
        node = next(node for node in template_under_test()["nodes"] if node["name"] == node_name)
        assignment = next(entry for entry in node["parameters"]["assignments"]["assignments"] if entry["name"] == field_name)
        return assignment["value"].removeprefix("={{").removesuffix("}}").strip()

    def evaluate(self, expression: str, item: dict) -> str:
        program = f"const $json = {json.dumps(item)};\nprocess.stdout.write(JSON.stringify({expression}));"
        result = subprocess.run([self.node_path, "-e", program], capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, check=True)
        return json.loads(result.stdout)

    def failure_item(self, error: object, text: str = "hello") -> dict:
        """As n8n's error output has it: the error, with the input item merged in beside it."""
        return {"body": {"sender": ZACK_PHONE, "sent_at": "2026-11-04T23:00:00Z", "text": text}, "error": error,
                "received_at": "2026-11-04T23:00:00.000Z", "started_ms": 1, "zack_handles": [ZACK_PHONE]}

    def test_a_refused_connection_is_unreachable(self):
        error = {"code": "ECONNREFUSED", "message": "connect ECONNREFUSED 127.0.0.1:8645"}
        self.assertEqual(self.evaluate(self.failure, self.failure_item(error)), "plugin_unreachable")

    def test_a_text_holding_the_word_timeout_does_not_change_the_outcome(self):
        # The text sits in the item beside the error, and must decide nothing.
        error = {"code": "ECONNREFUSED", "message": "connect ECONNREFUSED 127.0.0.1:8645"}
        for text in ("the timeout is at noon", "TIMEDOUT"):
            with self.subTest(text):
                self.assertEqual(self.evaluate(self.failure, self.failure_item(error, text)), "plugin_unreachable")

    def test_running_out_of_time_is_a_timeout_by_message_or_by_code(self):
        for error in ({"code": "ECONNABORTED", "message": "timeout of 5000ms exceeded"}, {"code": "ETIMEDOUT", "message": "socket hang up"}):
            with self.subTest(error):
                self.assertEqual(self.evaluate(self.failure, self.failure_item(error)), "timeout")

    def test_an_error_that_is_only_a_string_is_unreachable_rather_than_a_crash(self):
        self.assertEqual(self.evaluate(self.failure, self.failure_item("connect ECONNREFUSED")), "plugin_unreachable")

    def test_each_status_maps_to_its_outcome(self):
        for status, outcome in ((202, "accepted"), (204, "dropped_by_plugin"), (401, "secret_refused"), (400, "plugin_refusing"),
                                (403, "plugin_refusing"), (503, "plugin_refusing")):
            with self.subTest(status):
                self.assertEqual(self.evaluate(self.forward, {"statusCode": status}), outcome)


class ScriptTest(unittest.TestCase):
    """Runs render.py and export.py as the README does, file to file, in a scratch directory."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.handle_file = write_allow_list(self.scratch, "# Zack", ZACK_PHONE, "", ZACK_EMAIL)
        self.out = self.scratch / "rendered.json"

    def run_script(self, script: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *map(str, arguments)], capture_output=True, text=True,
                              timeout=TIMEOUT_SECONDS, check=False)

    def render(self, template: Path = FIXTURE_TEMPLATE, handle_file: Path | None = None, out: Path | None = None,
               table_id: str = RUN_RECORD_TABLE_ID) -> subprocess.CompletedProcess[str]:
        return self.run_script(RENDER_SCRIPT, handle_file or self.handle_file, template, out or self.out,
                               "--run-record-table-id", table_id)

    def export(self, exported: Path, template_out: Path, table_id: str = RUN_RECORD_TABLE_ID) -> subprocess.CompletedProcess[str]:
        return self.run_script(EXPORT_SCRIPT, self.handle_file, exported, template_out, "--run-record-table-id", table_id)

    def rendered(self) -> dict:
        return json.loads(self.out.read_text(encoding="utf-8"))

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        """Refused by the script's own check, rather than falling over on the way to something else."""
        self.assertEqual(result.returncode, EXIT_REFUSED, result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def assert_no_value_printed(self, result: subprocess.CompletedProcess[str]) -> None:
        for value in (ZACK_PHONE, ZACK_EMAIL, RUN_RECORD_TABLE_ID):
            self.assertNotIn(value, result.stdout + result.stderr)


class RenderTest(ScriptTest):
    def test_the_handles_token_becomes_the_handle_file_as_a_json_array(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        field = nodes_of_type(self.rendered(), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]
        self.assertEqual(json.loads(field["value"]), [ZACK_PHONE, ZACK_EMAIL])

    def test_the_handles_are_written_in_their_canonical_form(self):
        handle_file = write_allow_list(self.scratch, "(555) 010-0199", "ZACK@Example.COM")
        result = self.render(handle_file=handle_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        field = nodes_of_type(self.rendered(), SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]
        self.assertEqual(json.loads(field["value"]), [ZACK_PHONE, ZACK_EMAIL])

    def test_the_table_token_becomes_the_id(self):
        self.render()
        locator = nodes_of_type(self.rendered(), DATA_TABLE_TYPE)[0]["parameters"]["dataTableId"]
        self.assertEqual(locator["value"], RUN_RECORD_TABLE_ID)

    def test_no_token_is_left(self):
        self.render()
        self.assertNotIn("__", self.out.read_text(encoding="utf-8").replace("__rl", ""))

    def test_each_credential_reference_gets_a_null_id_so_n8n_matches_it_by_name(self):
        # n8n's replaceInvalidCredentials takes its by-name path on `id === null` and its by-id
        # path otherwise, so a reference with no id key at all would be looked up by id.
        self.render()
        for node_type, name in ((WEBHOOK_TYPE, RELAY_CREDENTIAL), (HTTP_REQUEST_TYPE, PLUGIN_CREDENTIAL)):
            with self.subTest(node_type):
                reference = nodes_of_type(self.rendered(), node_type)[0]["credentials"][CREDENTIAL_TYPE]
                self.assertEqual(reference, {"id": None, "name": name})

    def test_each_webhook_gets_a_webhook_id_so_it_answers_on_its_own_path(self):
        # Without one, n8n registers it at <workflow id>/<node name>/<path> (getNodeWebhookPath in
        # n8n-workflow), and the Shortcut's address answers 404. Import adds none, and export strips it.
        self.render()
        webhook = nodes_of_type(self.rendered(), WEBHOOK_TYPE)[0]
        self.assertTrue(UUID_PATTERN.fullmatch(webhook.get("webhookId", "")), "not a UUID")

    def test_only_webhooks_get_a_webhook_id(self):
        self.render()
        others = [node["name"] for node in self.rendered()["nodes"] if node["type"] != WEBHOOK_TYPE and "webhookId" in node]
        self.assertEqual(others, [])

    def test_the_file_is_written_for_its_owner_alone(self):
        self.render()
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), OWNER_ONLY_MODE)

    def test_an_existing_file_is_not_overwritten(self):
        self.out.write_text("kept\n", encoding="utf-8")
        result = self.render()
        self.assert_refused(result)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "kept\n")

    def test_an_empty_handle_file_is_refused_and_writes_nothing(self):
        result = self.render(handle_file=write_allow_list(self.scratch, "# nobody yet"))
        self.assert_refused(result)
        self.assertFalse(self.out.exists())

    def test_a_malformed_handle_file_is_refused_and_names_the_line_not_the_value(self):
        result = self.render(handle_file=write_allow_list(self.scratch, ZACK_PHONE, "Zack Kuptsow"))
        self.assert_refused(result)
        self.assertFalse(self.out.exists())
        self.assertIn("line 2", result.stderr)
        self.assertNotIn("Kuptsow", result.stderr)

    def test_a_missing_handle_file_is_refused(self):
        result = self.render(handle_file=self.scratch / "absent.txt")
        self.assert_refused(result)
        self.assertFalse(self.out.exists())

    def test_a_template_missing_a_token_is_refused(self):
        for token in (HANDLES_TOKEN, TABLE_TOKEN):
            with self.subTest(token):
                broken = self.scratch / "broken.json"
                broken.write_text(FIXTURE_TEMPLATE.read_text(encoding="utf-8").replace(token, "x"), encoding="utf-8")
                result = self.render(template=broken)
                self.assert_refused(result)
                self.assertIn(token, result.stderr)
                self.assertFalse(self.out.exists())

    def test_a_table_id_that_could_not_be_one_is_refused(self):
        # The table's name was given in place of its id on 2026-11-09, and imported unremarked.
        for table_id in ("", "has space", 'quo"te', "../path", "run_record", "kQ3xV9mT2bLw7Zp", "kQ3xV9mT2bLw7ZpAb",
                         "kQ3xV9mT2bLw7Z-A"):
            with self.subTest(table_id):
                result = self.render(table_id=table_id)
                self.assert_refused(result)
                self.assertFalse(self.out.exists())

    def test_a_refused_table_id_says_where_the_id_is(self):
        result = self.render(table_id="run_record")
        self.assert_refused(result)
        self.assertIn("/datatables/", result.stderr)

    def test_nothing_it_prints_holds_a_value(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_value_printed(result)


class ExportTest(ScriptTest):
    def setUp(self):
        super().setUp()
        self.exported = self.scratch / "exported.json"
        self.template_out = self.scratch / "template.json"

    def simulated_export(self, **overrides: object) -> dict:
        """The fixture rendered, then with everything an instance adds, as n8n's CLI writes it."""
        self.assertEqual(self.render().returncode, 0)
        workflow = self.rendered()
        workflow.update({
            "active": True, "activeVersion": {"nodes": []}, "activeVersionId": "version-one", "createdAt": "2026-11-04T22:00:00.000Z",
            "description": None, "id": "wOrKfLoWiD123456", "isArchived": False, "meta": {"templateCredsSetupCompleted": True},
            "nodeGroups": [], "parentFolder": None, "pinData": {}, "shared": [{"role": "workflow:owner", "project": {"id": "pRoJeCt"}}],
            "sourceWorkflowId": None, "staticData": None, "tags": [], "triggerCount": 1, "updatedAt": "2026-11-04T22:01:00.000Z",
            "versionCounter": 3, "versionId": "version-two", **overrides})
        for node in workflow["nodes"]:
            for reference in node.get("credentials", {}).values():
                reference["id"] = "cReDeNtIaLiD1234"
        nodes_of_type(workflow, WEBHOOK_TYPE)[0]["webhookId"] = "wEbHoOkId-1234"
        locator = nodes_of_type(workflow, DATA_TABLE_TYPE)[0]["parameters"]["dataTableId"]
        locator.update({"cachedResultName": "run_record", "cachedResultUrl": f"/projects/x/datatables/{RUN_RECORD_TABLE_ID}"})
        return workflow

    def write_exported(self, *workflows: dict) -> None:
        self.exported.write_text(json.dumps(list(workflows)), encoding="utf-8")

    def test_render_then_export_leaves_the_template_byte_identical(self):
        self.write_exported(self.simulated_export())
        result = self.export(self.exported, self.template_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.template_out.read_bytes(), FIXTURE_TEMPLATE.read_bytes())

    def test_everything_the_instance_added_is_gone(self):
        self.write_exported(self.simulated_export())
        self.export(self.exported, self.template_out)
        text = self.template_out.read_text(encoding="utf-8")
        for instance_value in ("wOrKfLoWiD123456", "cReDeNtIaLiD1234", "wEbHoOkId-1234", "cachedResult", "createdAt", "versionId",
                               "pRoJeCt", "triggerCount", "activeVersion", "versionCounter", "isArchived"):
            with self.subTest(instance_value):
                self.assertNotIn(instance_value, text)

    def test_a_key_n8n_adds_next_year_does_not_reach_the_template(self):
        # Kept by name rather than dropped by name, so an unknown key fails closed.
        self.write_exported(self.simulated_export(someFutureInstanceKey="its-value"))
        self.export(self.exported, self.template_out)
        self.assertNotIn("someFutureInstanceKey", self.template_out.read_text(encoding="utf-8"))

    def test_the_output_is_canonical(self):
        self.write_exported(self.simulated_export())
        self.export(self.exported, self.template_out)
        text = self.template_out.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def test_keys_in_any_order_come_out_sorted(self):
        # The fixture is sorted already, so this has to scramble what n8n hands over.
        def reversed_keys(node: object) -> object:
            if isinstance(node, dict):
                return {key: reversed_keys(node[key]) for key in sorted(node, reverse=True)}
            return [reversed_keys(value) for value in node] if isinstance(node, list) else node

        self.write_exported(reversed_keys(self.simulated_export()))
        result = self.export(self.exported, self.template_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.template_out.read_bytes(), FIXTURE_TEMPLATE.read_bytes())

    def test_the_handles_token_is_restored_whatever_the_array_looks_like(self):
        for array in (f'["{ZACK_PHONE}", "{ZACK_EMAIL}"]', f'["{ZACK_EMAIL}","{ZACK_PHONE}"]', f'[ "{ZACK_PHONE}" ]'):
            with self.subTest(array):
                workflow = self.simulated_export()
                self.out.unlink()
                nodes_of_type(workflow, SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"] = array
                self.write_exported(workflow)
                result = self.export(self.exported, self.template_out)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.template_out.read_bytes(), FIXTURE_TEMPLATE.read_bytes())

    def test_a_token_typed_into_the_editor_is_kept(self):
        # Authoring with the token means no handle is ever typed into the editor.
        workflow = self.simulated_export()
        nodes_of_type(workflow, SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"] = HANDLES_TOKEN
        self.write_exported(workflow)
        result = self.export(self.exported, self.template_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.template_out.read_bytes(), FIXTURE_TEMPLATE.read_bytes())

    def test_a_handle_left_anywhere_else_is_refused_and_nothing_written(self):
        for stray in (ZACK_PHONE, f"call {ZACK_PHONE} now", ZACK_EMAIL, "(555) 010-0199"):
            with self.subTest(stray):
                workflow = self.simulated_export()
                self.out.unlink()
                nodes_of_type(workflow, SET_TYPE)[0]["parameters"]["assignments"]["assignments"][1]["value"] = stray
                self.write_exported(workflow)
                result = self.export(self.exported, self.template_out)
                self.assert_refused(result)
                self.assertFalse(self.template_out.exists())
                self.assert_no_value_printed(result)

    def test_a_handle_used_as_a_name_is_refused_without_being_printed(self):
        # A refusal names where it found something, and that place can be the handle itself.
        workflow = self.simulated_export()
        workflow["connections"][ZACK_PHONE] = {"main": [[{"index": 0, "node": ZACK_PHONE, "type": "main"}]]}
        nodes_of_type(workflow, SET_TYPE)[0]["name"] = ZACK_PHONE
        self.write_exported(workflow)
        result = self.export(self.exported, self.template_out)
        self.assert_refused(result)
        self.assert_no_value_printed(result)

    def test_a_handle_not_in_the_file_is_refused_too(self):
        workflow = self.simulated_export()
        nodes_of_type(workflow, SET_TYPE)[0]["parameters"]["assignments"]["assignments"][0]["value"] = f'["{PARTNER_PHONE}"]'
        self.write_exported(workflow)
        result = self.export(self.exported, self.template_out)
        self.assert_refused(result)
        self.assertFalse(self.template_out.exists())

    def test_a_table_name_given_in_place_of_its_id_is_refused(self):
        self.write_exported(self.simulated_export())
        result = self.export(self.exported, self.template_out, table_id="run_record")
        self.assert_refused(result)
        self.assertFalse(self.template_out.exists())
        self.assertIn("/datatables/", result.stderr)

    def test_an_export_with_no_table_id_in_it_is_refused(self):
        self.write_exported(self.simulated_export())
        result = self.export(self.exported, self.template_out, table_id="aDifferentTable1")
        self.assert_refused(result)
        self.assertIn(TABLE_TOKEN, result.stderr)
        self.assertFalse(self.template_out.exists())

    def test_two_workflows_in_one_export_are_refused(self):
        workflow = self.simulated_export()
        self.write_exported(workflow, workflow)
        result = self.export(self.exported, self.template_out)
        self.assert_refused(result)
        self.assertFalse(self.template_out.exists())

    def test_an_export_that_is_not_json_is_refused(self):
        self.exported.write_text("not json\n", encoding="utf-8")
        result = self.export(self.exported, self.template_out)
        self.assert_refused(result)
        self.assertIn("not JSON", result.stderr)
        self.assertFalse(self.template_out.exists())

    def test_a_template_already_there_is_replaced(self):
        self.template_out.write_text("the earlier export\n", encoding="utf-8")
        self.write_exported(self.simulated_export())
        result = self.export(self.exported, self.template_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.template_out.read_bytes(), FIXTURE_TEMPLATE.read_bytes())

    def test_nothing_it_prints_holds_a_value(self):
        self.write_exported(self.simulated_export())
        result = self.export(self.exported, self.template_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_value_printed(result)


if __name__ == "__main__":
    unittest.main()
