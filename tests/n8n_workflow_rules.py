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
# Issue #10's two alarm workflows. Their closed list is the relay's with the two triggers in place of the
# Webhook and the HTTP Request: nothing posts to an alarm workflow, and none calls the plugin.
ERROR_TRIGGER_TYPE = "n8n-nodes-base.errorTrigger"
SCHEDULE_TRIGGER_TYPE = "n8n-nodes-base.scheduleTrigger"
IF_TYPE = "n8n-nodes-base.if"
ALARM_NODE_TYPES = (ALLOWED_NODE_TYPES - {WEBHOOK_TYPE, HTTP_REQUEST_TYPE}) | {ERROR_TRIGGER_TYPE, SCHEDULE_TRIGGER_TYPE}
# Row operations, insert being n8n's default. The table resource, which can delete or clear a whole table, has none here.
DATA_TABLE_OPERATIONS = frozenset({"insert", "get", "deleteRows"})
NON_WRITING_OPERATIONS = frozenset({"get", "deleteRows", "rowExists", "rowNotExists"})
ALARM_ROUTE = "alarm"
OUTCOME_NAME_PATTERN = re.compile(r"[a-z]+(?:_[a-z]+)*")
HEARTBEAT_CONSTANT_NAMES = frozenset({"quiet_hours", "keep_days"})
LAST_MORNING_HOUR = 11
# The Error Trigger hands over the failed run: its id, address, error and last node, and the workflow's name.
# The error's message can carry what a node was handed, so the run's id is the one thing that may be read.
FAILED_RUN_ID_READ = re.compile(r"\$json\.execution\.id\b")
FAILED_RUN_READ_PATTERN = re.compile(r"\b(?:execution|workflow|trigger)\b")


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
        if data_table_operation(node) in NON_WRITING_OPERATIONS:
            continue
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


def node_type_problems(template: dict, allowed: frozenset[str]) -> list[str]:
    """Each node whose type is not on *allowed*, the closed list."""
    return [f"{node.get('name')}: {node.get('type')} is off the closed list" for node in template.get("nodes", [])
            if node.get("type") not in allowed]


def settings_problems(template: dict) -> list[str]:
    """Each saving setting not written out as the one value the design allows."""
    settings = template.get("settings", {})
    return [f"settings: {key} is not {value!r}" for key, value in EXPLICIT_SETTINGS.items()
            if type(settings.get(key)) is not type(value) or settings.get(key) != value]


def relay_problems(template: dict) -> list[str]:
    """Why *template* may not be the relay, or nothing if it may."""
    problems = [*node_type_problems(template, ALLOWED_NODE_TYPES), *settings_problems(template)]
    serialised = json.dumps(template)
    problems += [f"the token {token} is missing" for token in (HANDLES_TOKEN, TABLE_TOKEN) if token not in serialised]
    return [*problems, *value_problems(template), *credential_problems(template), *text_problems(template),
            *gate_problems(template)]


def data_table_operation(node: dict) -> str:
    """What a Data table node does, as n8n reads it: a row operation, and insert where none is named."""
    parameters = node.get("parameters", {})
    operation = parameters.get("operation", "insert")
    return operation if parameters.get("resource", "row") == "row" else f"{parameters['resource']} {operation}"


def assigned_values(node: dict) -> dict[str, object]:
    """A Set node's fields, each name with its value."""
    return {entry.get("name"): entry.get("value") for entry in node.get("parameters", {}).get("assignments", {}).get("assignments", [])}


def alarm_problems(template: dict, trigger_type: str) -> list[str]:
    """Why *template* may not be one of the two alarm workflows, or nothing if it may.

    An alarm is a run record row with route `alarm` and a fixed outcome naming its cause, so it can
    carry neither a message nor a text.
    """
    nodes = template.get("nodes", [])
    serialised = json.dumps(template)
    problems = [*node_type_problems(template, ALARM_NODE_TYPES), *settings_problems(template)]
    if [node.get("type") for node in nodes if node.get("type") in (ERROR_TRIGGER_TYPE, SCHEDULE_TRIGGER_TYPE)] != [trigger_type]:
        problems.append(f"the one trigger is not a {trigger_type}")
    problems += [f"the token {TABLE_TOKEN} is missing"] if TABLE_TOKEN not in serialised else []
    problems += ["holds the routing table, which no alarm reads"] if HANDLES_TOKEN in serialised else []
    problems += [f"{node.get('name')}: holds a credential" for node in nodes if node.get("credentials")]
    problems += [f"{node.get('name')}: the Data table's {data_table_operation(node)} is off the closed list"
                 for node in nodes_of_type(template, DATA_TABLE_TYPE) if data_table_operation(node) not in DATA_TABLE_OPERATIONS]
    for node in nodes_of_type(template, SET_TYPE):
        fields = assigned_values(node)
        if "route" in fields and fields["route"] != ALARM_ROUTE:
            problems.append(f"{node['name']}: writes a route other than {ALARM_ROUTE}")
        if "outcome" in fields and not OUTCOME_NAME_PATTERN.fullmatch(str(fields["outcome"])):
            problems.append(f"{node['name']}: the outcome is not a fixed name")
    return [*problems, *value_problems(template), *literal_secret_problems(nodes), *text_problems(template)]


def failed_run_problems(template: dict) -> list[str]:
    """Each expression in the error workflow that reads more of the failed run than its id."""
    return [f"{location}: reads more of the failed run than its id" for location, string in every_string(template.get("nodes", []))
            if string.startswith("=") and FAILED_RUN_READ_PATTERN.search(FAILED_RUN_ID_READ.sub("", string))]


def filter_conditions(node: dict) -> list[tuple[str, str, str]]:
    """A Data table node's conditions as (column, condition, value), eq where none is named, as n8n reads them."""
    return [(entry.get("keyName"), entry.get("condition", "eq"), str(entry.get("keyValue", "")))
            for entry in node.get("parameters", {}).get("filters", {}).get("conditions", [])]


def fed_by(template: dict, node_name: object, output: int = 0) -> list[str]:
    """The names of the nodes one output of *node_name* feeds."""
    outputs = template.get("connections", {}).get(node_name, {}).get("main", [])
    return [link.get("node") for link in outputs[output]] if output < len(outputs) else []


def heartbeat_problems(template: dict) -> list[str]:
    """The heartbeat's constants at its top, and the three questions it asks, as Issue #10 sets them."""
    problems = []
    by_name = {node.get("name"): node for node in template.get("nodes", [])}
    trigger = next((node for node in template.get("nodes", []) if node.get("type") == SCHEDULE_TRIGGER_TYPE), {})
    intervals = trigger.get("parameters", {}).get("rule", {}).get("interval", [])
    if len(intervals) != 1 or intervals[0].get("field", "days") != "days" or intervals[0].get("triggerAtHour", 0) > LAST_MORNING_HOUR:
        problems.append("the trigger does not fire once every morning")
    first = [by_name.get(name, {}) for name in fed_by(template, trigger.get("name"))]
    constants = assigned_values(first[0]) if len(first) == 1 and first[0].get("type") == SET_TYPE else {}
    if set(constants) != HEARTBEAT_CONSTANT_NAMES or not all(isinstance(value, int) and value > 0 for value in constants.values()):
        problems.append("the trigger does not feed a Set holding the two constants, and nothing else")
    elif set(fed_by(template, first[0].get("name"))) != {"Find an accepted run", "Prune old rows"}:
        problems.append("the constants do not feed both questions of the table")
    prune, find = by_name.get("Prune old rows", {}), by_name.get("Find an accepted run", {})
    if data_table_operation(prune) != "deleteRows" or filter_conditions(prune) != [
            ("createdAt", "lt", "={{ $now.minus({ days: $json.keep_days }).toISO() }}")]:
        problems.append("the prune deletes something other than the rows older than the days kept")
    wanted = [("route", "eq", "zack"), ("outcome", "eq", "accepted"),
              ("createdAt", "gt", "={{ $now.minus({ hours: $json.quiet_hours }).toISO() }}")]
    if data_table_operation(find) != "get" or filter_conditions(find) != wanted or find.get("parameters", {}).get("matchType") != "allConditions":
        problems.append("the find does not ask for an accepted run from zack inside the quiet hours")
    if find.get("alwaysOutputData") is not True:
        problems.append("the find hands nothing on when it finds nothing, so it could never alarm")
    quiet = [node for node in nodes_of_type(template, IF_TYPE) if fed_by(template, find.get("name")) == [node.get("name")]]
    conditions = quiet[0].get("parameters", {}).get("conditions", {}).get("conditions", []) if quiet else []
    if len(conditions) != 1 or conditions[0].get("leftValue") != "={{ $json.id }}" or conditions[0].get("operator", {}).get("operation") != "notExists":
        problems.append("the find is not followed by an If asking whether it found nothing")
    elif [assigned_values(by_name.get(name, {})).get("route") for name in fed_by(template, quiet[0]["name"])] != [ALARM_ROUTE] \
            or fed_by(template, quiet[0]["name"], 1):
        problems.append("the alarm does not hang off the If's true output alone")
    return problems


def relay_failed_problems(template: dict) -> list[str]:
    """Why *template* may not be `Relay failed`, the relay's error workflow, or nothing if it may."""
    return [*alarm_problems(template, ERROR_TRIGGER_TYPE), *failed_run_problems(template)]


def inbound_heartbeat_problems(template: dict) -> list[str]:
    """Why *template* may not be `Inbound heartbeat`, or nothing if it may."""
    return [*alarm_problems(template, SCHEDULE_TRIGGER_TYPE), *heartbeat_problems(template)]

