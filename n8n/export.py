#!/usr/bin/env python3
"""export.py: turn what n8n's CLI exported back into the value-free template git holds.

Usage: export.py HANDLE_FILE EXPORTED TEMPLATE_OUT --run-record-table-id ID

The inverse of render.py, file to file; neither runs n8n. EXPORTED is the array
`n8n export:workflow --id=<id> --output=<file>` writes. Keeps the workflow's four portable keys
and drops the rest by default, so a key a later n8n adds cannot reach git; drops each node's
webhook id, each credential's id and each resource locator's cached name and URL; puts the tokens
back; and writes canonically, so render then export leaves the template byte-identical. Refuses,
writing nothing, while anything in the result still looks like a handle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import workflow_template

# Kept by name, not dropped by name: an instance-specific key n8n adds later fails closed.
PORTABLE_KEYS = ("connections", "name", "nodes", "settings")
INSTANCE_NODE_KEYS = ("webhookId",)
INSTANCE_LOCATOR_KEYS = ("cachedResultName", "cachedResultUrl")
# A node's id is a UUID, whose digits can read as a phone number.
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
PHONE_PATTERN = re.compile(r"\+?\d(?:[\s().\- ]*\d){6,}")
EMAIL_PATTERN = re.compile(r"[^\s@'\"(),]+@[^\s@'\"(),]+\.[^\s@'\"(),]+")


def the_one_workflow(exported: object) -> dict:
    """The single workflow in what n8n's CLI wrote, which is an array even for one id."""
    if not isinstance(exported, list) or len(exported) != 1 or not isinstance(exported[0], dict):
        raise workflow_template.TemplateError("the export must hold exactly one workflow; export it by --id")
    return exported[0]


def without_locator_caches(node: object) -> object:
    """A copy of *node* with every resource locator's cached name and URL gone."""
    if isinstance(node, dict):
        return {key: without_locator_caches(value) for key, value in node.items() if key not in INSTANCE_LOCATOR_KEYS}
    if isinstance(node, list):
        return [without_locator_caches(value) for value in node]
    return node


def portable_workflow(workflow: dict) -> dict:
    """*workflow* with everything specific to this instance taken out."""
    missing = [key for key in PORTABLE_KEYS if key not in workflow]
    if missing:
        raise workflow_template.TemplateError(f"the export does not hold {', '.join(missing)}")
    portable = without_locator_caches({key: workflow[key] for key in PORTABLE_KEYS})
    for node in portable["nodes"]:
        for key in INSTANCE_NODE_KEYS:
            node.pop(key, None)
        for reference in node.get("credentials", {}).values():
            reference.pop("id", None)
    return portable


def is_handles_array(string: str, allowed: tuple[str, ...]) -> bool:
    """Whether *string* is a rendered routing table: a JSON array of handles from the file."""
    handles = workflow_template.plugin_module("handles")
    try:
        parsed = json.loads(string)
    except ValueError:
        return False
    if not isinstance(parsed, list) or not parsed or not all(isinstance(entry, str) for entry in parsed):
        return False
    try:
        return all(handles.canonical_handle(entry) in allowed for entry in parsed)
    except handles.InvalidHandleError:
        return False


def tokenised(workflow: dict, allowed: tuple[str, ...], table_id: str) -> dict:
    """*workflow* with the routing table's array and the table's id back to their tokens."""
    def take_values_out(string: str) -> str:
        if is_handles_array(string, allowed):
            return workflow_template.HANDLES_TOKEN
        return string.replace(table_id, workflow_template.TABLE_TOKEN)

    return workflow_template.map_string_values(workflow, take_values_out)


def looks_like_a_handle(string: str) -> bool:
    """Whether *string* holds what could be a phone number or an email address."""
    without_ids = UUID_PATTERN.sub("", string)
    return bool(PHONE_PATTERN.search(without_ids) or EMAIL_PATTERN.search(without_ids))


def stray_handle_locations(node: object, location: str = "") -> list[str]:
    """Where in *node* a string value still looks like a handle. Names the place, never the value,
    and a key that looks like one is not named either: it would print the very thing refused."""
    if isinstance(node, str):
        return [location] if looks_like_a_handle(node) else []
    if isinstance(node, dict):
        return [found for key, value in node.items()
                for found in stray_handle_locations(value, f"{location}/{'<a name>' if looks_like_a_handle(key) else key}")]
    if isinstance(node, list):
        return [found for index, value in enumerate(node) for found in stray_handle_locations(value, f"{location}/{index}")]
    return []


def export_to_template(exported: object, allowed: tuple[str, ...], table_id: str) -> dict:
    """The template for *exported*.

    Raises:
        workflow_template.TemplateError: it is not one workflow, a token could not be restored,
            or something in it still looks like a handle.
    """
    template = tokenised(portable_workflow(the_one_workflow(exported)), allowed, table_id)
    stray = stray_handle_locations(template)
    if stray:
        raise workflow_template.TemplateError(f"these still look like a handle: {', '.join(stray)}")
    missing = workflow_template.missing_tokens(template)
    if missing:
        raise workflow_template.TemplateError(f"could not restore {', '.join(missing)}: is the id the one this workflow uses?")
    return template


def write_replacing(path: Path, text: str) -> None:
    """Replace *path* whole, so a failure cannot leave half a template."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    """Parse the arguments, turn the export into the template, and report."""
    parser = argparse.ArgumentParser(description="Turn what n8n's CLI exported back into the value-free template.")
    parser.add_argument("handle_file", type=Path, help="the plugin's allow list")
    parser.add_argument("exported", type=Path, help="what n8n export:workflow wrote")
    parser.add_argument("template_out", type=Path, help="the template to write, in n8n/workflows/")
    parser.add_argument("--run-record-table-id", required=True, help="the run_record Data Table's id, from its URL")
    arguments = parser.parse_args()
    try:
        table_id = workflow_template.checked_table_id(arguments.run_record_table_id)
        allowed = workflow_template.read_handles(arguments.handle_file)
        template = export_to_template(workflow_template.read_json(arguments.exported), allowed, table_id)
        write_replacing(arguments.template_out, workflow_template.canonical_json(template))
    except workflow_template.TemplateError as error:
        print(f"not exported: {error}", file=sys.stderr)
        return workflow_template.EXIT_REFUSED
    print(f"wrote {arguments.template_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
