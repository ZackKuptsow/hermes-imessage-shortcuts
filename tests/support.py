"""Test support: load the plugin package from its directory, and the fakes the suites share."""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PACKAGE_NAME = "imessage_shortcuts"
PLUGIN_DIRECTORY = Path(__file__).resolve().parent.parent / "imessage-shortcuts"
# Set by setup.sh: the adapter's suite must run, never skip, so a run under the wrong
# interpreter (or a Hermes re-exec into one) fails instead of passing on skips.
REQUIRE_HERMES_VARIABLE = "IMESSAGE_SHORTCUTS_TESTS_REQUIRE_HERMES"
ZACK_EMAIL = "zack@example.com"
ZACK_PHONE = "+15550100199"
PARTNER_PHONE = "+15550100142"
SECRET = "s" * 40


def plugin_package() -> ModuleType:
    """The plugin package itself, loaded from its directory and registered once."""
    if PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME, PLUGIN_DIRECTORY / "__init__.py", submodule_search_locations=[str(PLUGIN_DIRECTORY)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the plugin package from {PLUGIN_DIRECTORY}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = package
        spec.loader.exec_module(package)
    return sys.modules[PACKAGE_NAME]


def plugin_module(name: str) -> ModuleType:
    """Import `<package>.<name>` from the plugin directory."""
    plugin_package()
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


def hermes_is_importable() -> bool:
    """True under Hermes's own interpreter, where `gateway` and `aiohttp` import."""
    return all(importlib.util.find_spec(name) is not None for name in ("gateway", "aiohttp", "hermes_constants"))


def hermes_is_required() -> bool:
    """True when the run must use Hermes's interpreter: suites that need it run, never skip."""
    return os.environ.get(REQUIRE_HERMES_VARIABLE) == "1"


class RecordingRun:
    """Stands in for `subprocess.run`: records each argument vector and answers as told."""

    def __init__(self, stdout: str = '{"status":"sent"}', returncode: int = 0, raises: BaseException | None = None):
        self.calls: list[list[str]] = []
        self.raises = raises
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(arguments))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(arguments, self.returncode, stdout=self.stdout, stderr="")

    def option(self, call_index: int, prefix: str) -> str | None:
        """The value of `--name=value` in one recorded call, or None."""
        for argument in self.calls[call_index]:
            if argument.startswith(prefix):
                return argument[len(prefix):]
        return None


def write_allow_list(directory: Path, *lines: str) -> Path:
    """An allow-list file holding *lines*."""
    path = directory / "zack-handles.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
