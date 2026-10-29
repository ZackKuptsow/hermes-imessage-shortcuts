"""Zack's iMessage handles: the one written form each handle is compared in, and the allow
list read from its file.

A handle is compared only in its canonical form: a phone number as E.164 (`+` and digits;
ten digits are read as a US number) or an email address in lower case. Anything else, a
contact's name included, is refused rather than guessed at, since imsg resolves a name
through Contacts and would send to whoever it matched.
"""

from __future__ import annotations

import re
from pathlib import Path

ALLOW_LIST_RELATIVE_PATH = Path("data/imessage/zack-handles.txt")  # under the Hermes home
COMMENT_PREFIX = "#"
EMAIL_PATTERN = re.compile(r"^[^@\s-][^@\s]*@[^@\s]+\.[^@\s]+$")
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{6,14}$")
PHONE_PUNCTUATION_PATTERN = re.compile(r"[\s().\-\u00a0]")
PROFILES_DIRECTORY = "profiles"
# The Hermes profile that answers Zack's texts (smart-home ADR 0034).
TEXTING_PROFILE = "imessage"
US_COUNTRY_CODE = "1"
US_NATIONAL_NUMBER_LENGTH = 10


class InvalidHandleError(ValueError):
    """A handle that is neither a phone number nor an email address."""


class AllowListError(ValueError):
    """The allow-list file is missing, empty, or holds a line that is not a handle."""


def canonical_handle(handle: str) -> str:
    """Return *handle* in its canonical form.

    Raises:
        InvalidHandleError: *handle* is not a phone number or an email address.
    """
    stripped = handle.strip()
    if "@" in stripped:
        lowered = stripped.lower()
        if EMAIL_PATTERN.match(lowered):
            return lowered
        raise InvalidHandleError("not an email address")
    digits = PHONE_PUNCTUATION_PATTERN.sub("", stripped)
    if digits.isdigit() and len(digits) == US_NATIONAL_NUMBER_LENGTH:
        digits = f"+{US_COUNTRY_CODE}{digits}"
    elif digits.isdigit() and len(digits) == US_NATIONAL_NUMBER_LENGTH + 1 and digits.startswith(US_COUNTRY_CODE):
        digits = f"+{digits}"
    if E164_PATTERN.match(digits):
        return digits
    raise InvalidHandleError("not a phone number or an email address")


def is_phone_number(handle: str) -> bool:
    """True when a canonical *handle* is a phone number."""
    return bool(E164_PATTERN.match(handle))


def read_allowed_handles(path: Path) -> tuple[str, ...]:
    """Read the allow list: one handle a line, blank lines and `#` comments skipped.

    Returns the canonical handles in file order, without duplicates.

    Raises:
        AllowListError: the file is missing or unreadable, holds no handle, or a line is
            not a handle. The message names the line number, never the line.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AllowListError(f"cannot read the allow list {path}: {error.strerror}") from error
    handles: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        entry = line.strip()
        if not entry or entry.startswith(COMMENT_PREFIX):
            continue
        try:
            handle = canonical_handle(entry)
        except InvalidHandleError as error:
            raise AllowListError(f"{path}, line {line_number}: {error}") from error
        if handle not in handles:
            handles.append(handle)
    if not handles:
        raise AllowListError(f"the allow list {path} holds no handle")
    return tuple(handles)


def first_phone_number(handles: tuple[str, ...]) -> str:
    """The first phone number on the allow list: the handle the household assistant knows Zack by.

    Raises:
        AllowListError: the allow list holds no phone number.
    """
    for handle in handles:
        if is_phone_number(handle):
            return handle
    raise AllowListError("the allow list holds no phone number")


def texting_profile_home(hermes_home: str | None, user_home: Path) -> Path:
    """The texting profile's home, whichever Hermes home a script was started from.

    Inside a profile's session HERMES_HOME is that profile's own home, so the Hermes root is
    what precedes its first `profiles` directory, as in hermes/mac-studio/setup.sh.

    Args:
        hermes_home: $HERMES_HOME; unset or empty means the root is ~/.hermes.
        user_home: the user's home directory.

    Returns:
        <root>/profiles/imessage, where Zack's allow list and the function's URL live.
    """
    root = Path(hermes_home) if hermes_home else user_home / ".hermes"
    if PROFILES_DIRECTORY in root.parts:
        root = Path(*root.parts[: root.parts.index(PROFILES_DIRECTORY)])
    return root / PROFILES_DIRECTORY / TEXTING_PROFILE
