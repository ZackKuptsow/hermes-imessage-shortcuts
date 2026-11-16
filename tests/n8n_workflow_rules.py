"""The rules a workflow template keeps before it may be in git, which tests/test_n8n_workflows.py applies.

Each returns a list of problems, empty when the template may be stored, and names where it found
something rather than the value it found. Kept beside the test so that file holds the tests alone.
"""

from __future__ import annotations

import json
import re

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
