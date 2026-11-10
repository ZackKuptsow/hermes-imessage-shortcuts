"""What render.py and export.py share: the template's tokens, its canonical form, and the
plugin's own rules for what a handle is.

The template is a workflow with its values taken out: the handles and the run_record table's id
are tokens in git, filled in on the Mac by render.py and taken out again by export.py. Stdlib
only, and it prints nothing that holds a value.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

EXIT_REFUSED = 2
HANDLES_TOKEN = "__ZACK_HANDLES_JSON__"
TABLE_TOKEN = "__RUN_RECORD_TABLE_ID__"
TOKENS = (HANDLES_TOKEN, TABLE_TOKEN)
PACKAGE_NAME = "imessage_shortcuts"
PLUGIN_DIRECTORY = Path(__file__).resolve().parent.parent / "imessage-shortcuts"
# n8n makes a Data Table's id as it makes a workflow's: 16 characters from its NANOID_ALPHABET of
# digits and letters (@n8n/utils generate-nano-id). Anything else, the table's name first, is a slip
# that would be written into the workflow and fail only when a run reaches the table.
TABLE_ID_PATTERN = re.compile(r"^[0-9A-Za-z]{16}$")
TABLE_ID_HELP = ("the run record table's id is the 16 letters and digits after /datatables/ in the table's "
                 "address in the editor, not its name")


class TemplateError(ValueError):
    """A template, an export or an argument that cannot be used. The message holds no value."""


def plugin_module(name: str) -> ModuleType:
    """Import one of the plugin's stdlib modules from the directory beside this one."""
    if PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME, PLUGIN_DIRECTORY / "__init__.py", submodule_search_locations=[str(PLUGIN_DIRECTORY)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {PLUGIN_DIRECTORY}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


def read_handles(handle_file: Path) -> tuple[str, ...]:
    """The canonical handles in *handle_file*, by the plugin's own rules.

    Raises:
        TemplateError: the file is missing, empty or malformed; the message names the line, never its value.
    """
    handles = plugin_module("handles")
    try:
        return handles.read_allowed_handles(handle_file)
    except handles.AllowListError as error:
        raise TemplateError(str(error)) from error


def checked_table_id(table_id: str) -> str:
    """*table_id*, or TemplateError when it could not be one of n8n's ids."""
    if not TABLE_ID_PATTERN.match(table_id):
        raise TemplateError(TABLE_ID_HELP)
    return table_id


def read_json(path: Path) -> object:
    """The JSON in *path*.

    Raises:
        TemplateError: it cannot be read or is not JSON.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise TemplateError(f"cannot read {path}: {error.strerror}") from error
    except ValueError as error:
        raise TemplateError(f"{path} is not JSON") from error


def canonical_json(workflow: object) -> str:
    """The one way the template is stored: keys sorted, two-space indent, a trailing newline."""
    return json.dumps(workflow, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def map_string_values(node: object, transform: Callable[[str], str]) -> object:
    """A copy of *node* with *transform* applied to every string value. Keys are left as they are."""
    if isinstance(node, str):
        return transform(node)
    if isinstance(node, dict):
        return {key: map_string_values(value, transform) for key, value in node.items()}
    if isinstance(node, list):
        return [map_string_values(value, transform) for value in node]
    return node


def missing_tokens(workflow: object) -> list[str]:
    """Which of the template's tokens *workflow* does not hold."""
    serialised = json.dumps(workflow)
    return [token for token in TOKENS if token not in serialised]
