"""What every test of n8n/check_relay.py shares: the check itself, and the template it reads.

check_relay.py lives beside the scripts it uses rather than in a package, so its folder goes on the
path here, once, for each test file that imports it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from support import plugin_module

__all__ = [
    "CREDENTIAL_TYPE",
    "DATA_TABLE_TYPE",
    "HTTP_REQUEST_TYPE",
    "N8N_DIRECTORY",
    "PLUGIN_PORT",
    "RELAY_TEMPLATE",
    "RUN_RECORD_TABLE_ID",
    "SET_TYPE",
    "STAND_IN_URL",
    "WEBHOOK_TYPE",
    "check_relay",
    "nodes_of_type",
    "template",
]

N8N_DIRECTORY = Path(__file__).resolve().parent.parent / "n8n"
sys.path.insert(0, str(N8N_DIRECTORY))

import check_relay  # noqa: E402 -- it lives beside the scripts it uses, not in a package

plugin_module("handles")

# The template check_relay.py itself reads: the relay as n8n's editor exported it.
RELAY_TEMPLATE = Path(__file__).resolve().parent.parent / "n8n" / "workflows" / "imessage-relay.json"
RUN_RECORD_TABLE_ID = "kQ3xV9mT2bLw7ZpA"
STAND_IN_URL = "http://127.0.0.1:49999/imessage"
PLUGIN_PORT = "8645"
WEBHOOK_TYPE = "n8n-nodes-base.webhook"
HTTP_REQUEST_TYPE = "n8n-nodes-base.httpRequest"
SET_TYPE = "n8n-nodes-base.set"
DATA_TABLE_TYPE = "n8n-nodes-base.dataTable"
CREDENTIAL_TYPE = "httpHeaderAuth"


def template() -> dict:
    """The relay as n8n's editor exported it, read fresh so no test can change another's copy."""
    return json.loads(RELAY_TEMPLATE.read_text(encoding="utf-8"))


def nodes_of_type(workflow: dict, node_type: str) -> list[dict]:
    """Every node of one type, in the workflow's own order."""
    return [node for node in workflow["nodes"] if node["type"] == node_type]
