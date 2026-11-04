#!/usr/bin/env python3
"""render.py: fill a workflow template's tokens, for n8n to import.

Usage: render.py HANDLE_FILE TEMPLATE OUT --run-record-table-id ID

Reads HANDLE_FILE by the plugin's own rules, so the relay's routing table and the plugin's allow
list come from one file and cannot disagree. Writes OUT mode 600 and refuses to overwrite it,
since OUT holds handles. Prints how many handles it wrote, and never one of them.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

import workflow_template

OWNER_ONLY_MODE = 0o600


def with_null_credential_ids(workflow: dict) -> dict:
    """A copy in which every credential reference has `id: null`.

    The template carries a credential's name and no id. n8n's import matches a reference by name
    and type only when its id is exactly null (replaceInvalidCredentials in workflow-helpers.js);
    a reference with no id key at all takes the path that looks it up by id.
    """
    rendered = copy.deepcopy(workflow)
    for node in rendered.get("nodes", []):
        for reference in node.get("credentials", {}).values():
            reference["id"] = None
    return rendered


def render_workflow(template: dict, handles: tuple[str, ...], table_id: str) -> dict:
    """*template* with its tokens filled.

    Raises:
        workflow_template.TemplateError: a token is missing, so the file is not a template.
    """
    missing = workflow_template.missing_tokens(template)
    if missing:
        raise workflow_template.TemplateError(f"the template does not hold {', '.join(missing)}")
    handles_array = json.dumps(list(handles))

    def fill(string: str) -> str:
        return string.replace(workflow_template.HANDLES_TOKEN, handles_array).replace(workflow_template.TABLE_TOKEN, table_id)

    return with_null_credential_ids(workflow_template.map_string_values(template, fill))


def write_owner_only(path: Path, text: str) -> None:
    """Write a new file only its owner can read. Raises FileExistsError rather than overwrite."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)


def main() -> int:
    """Parse the arguments, render, and report."""
    parser = argparse.ArgumentParser(description="Fill a workflow template's tokens, for n8n to import.")
    parser.add_argument("handle_file", type=Path, help="the plugin's allow list")
    parser.add_argument("template", type=Path, help="the workflow template, from n8n/workflows/")
    parser.add_argument("out", type=Path, help="the file to write, mode 600; it must not exist")
    parser.add_argument("--run-record-table-id", required=True, help="the run_record Data Table's id, from its URL")
    arguments = parser.parse_args()
    try:
        table_id = workflow_template.checked_table_id(arguments.run_record_table_id)
        handles = workflow_template.read_handles(arguments.handle_file)
        rendered = render_workflow(workflow_template.read_json(arguments.template), handles, table_id)
        write_owner_only(arguments.out, workflow_template.canonical_json(rendered))
    except workflow_template.TemplateError as error:
        print(f"not rendered: {error}", file=sys.stderr)
        return workflow_template.EXIT_REFUSED
    except FileExistsError:
        print(f"not rendered: {arguments.out} exists, and may hold handles; move it first", file=sys.stderr)
        return workflow_template.EXIT_REFUSED
    print(f"rendered {arguments.out} with {len(handles)} handles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
