"""Test support: load the plugin package from its directory, and the fakes the suites share."""

from __future__ import annotations

import http.client
import importlib
import importlib.util
import io
import os
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import NoReturn

PACKAGE_NAME = "imessage_shortcuts"
PLUGIN_DIRECTORY = Path(__file__).resolve().parent.parent / "imessage-shortcuts"
QUESTIONS_PACKAGE_NAME = "imessage_questions"
QUESTIONS_PLUGIN_DIRECTORY = Path(__file__).resolve().parent.parent / "imessage-questions"
# Set by setup.sh: the adapter's suite must run, never skip, so a run under the wrong
# interpreter (or a Hermes re-exec into one) fails instead of passing on skips.
REQUIRE_HERMES_VARIABLE = "IMESSAGE_SHORTCUTS_TESTS_REQUIRE_HERMES"
ZACK_EMAIL = "zack@example.com"
ZACK_PHONE = "+15550100199"
PARTNER_PHONE = "+15550100142"
SECRET = "s" * 40
# A household function's reply cut short, as real urllib reads one: 100 bytes declared, 5 sent.
CUT_SHORT_DECLARED_LENGTH = 100
CUT_SHORT_RECEIVED = b"Added"
NOT_HTTP_STATUS_LINE = "SPAM 200 nope"


def load_package(package_name: str, directory: Path) -> ModuleType:
    """A plugin package, loaded from its directory under *package_name* and registered once."""
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name, directory / "__init__.py", submodule_search_locations=[str(directory)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the plugin package from {directory}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        spec.loader.exec_module(package)
    return sys.modules[package_name]


def plugin_package() -> ModuleType:
    """The texting plugin's package itself."""
    return load_package(PACKAGE_NAME, PLUGIN_DIRECTORY)


def plugin_module(name: str) -> ModuleType:
    """Import `<package>.<name>` from the plugin directory."""
    plugin_package()
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


def questions_plugin_package() -> ModuleType:
    """The package of `imessage-questions`, the plugin for the profile whose sessions ask."""
    return load_package(QUESTIONS_PACKAGE_NAME, QUESTIONS_PLUGIN_DIRECTORY)


def questions_plugin_module(name: str) -> ModuleType:
    """Import `<package>.<name>` from the `imessage-questions` plugin directory."""
    questions_plugin_package()
    return importlib.import_module(f"{QUESTIONS_PACKAGE_NAME}.{name}")


def hermes_is_importable() -> bool:
    """True under Hermes's own interpreter, where `gateway` and `aiohttp` import."""
    return all(importlib.util.find_spec(name) is not None for name in ("gateway", "aiohttp", "hermes_constants"))


def hermes_is_required() -> bool:
    """True when the run must use Hermes's interpreter: suites that need it run, never skip."""
    return os.environ.get(REQUIRE_HERMES_VARIABLE) == "1"


class RecordingRun:
    """Stands in for `subprocess.run`: records each argument vector and answers as told.

    Like the real one, it raises ValueError, recording nothing, for an argument the operating
    system cannot be handed: one holding U+0000, or one `os.fsencode` cannot encode.
    """

    def __init__(self, stdout: str = '{"status":"sent"}', returncode: int = 0, raises: BaseException | None = None):
        self.calls: list[list[str]] = []
        self.raises = raises
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        for argument in arguments:
            if "\x00" in argument:
                raise ValueError("embedded null byte")
            os.fsencode(argument)  # UnicodeEncodeError, a ValueError, as subprocess raises it
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


class CutShortResponse(io.BytesIO):
    """urlopen's response to a 200 whose body ends before its Content-Length.

    The opener returns it as http.client returns the real one; only its read() raises, so the
    reply fails where post_command reads it.
    """

    status = HTTPStatus.OK

    def read(self, size: int | None = -1) -> bytes:
        """Raise IncompleteRead, as http.client does when the connection closes early."""
        raise http.client.IncompleteRead(CUT_SHORT_RECEIVED,
                                         expected=CUT_SHORT_DECLARED_LENGTH - len(CUT_SHORT_RECEIVED))


def function_cut_short(request: object, timeout: float) -> CutShortResponse:
    """An opener standing in for urlopen when the household function's reply is cut short."""
    return CutShortResponse()


def function_not_http(request: object, timeout: float) -> NoReturn:
    """An opener standing in for urlopen when the reply's status line is not HTTP: it raises, as urlopen does."""
    raise http.client.BadStatusLine(NOT_HTTP_STATUS_LINE)
