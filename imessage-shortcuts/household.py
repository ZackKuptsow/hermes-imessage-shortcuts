"""Forward one of Zack's household commands to the household assistant's inbound function,
as him, and send its answer straight to his phone.

The answer can carry the partner's data (a shared list, her reminders), so it goes from the
function to imsg and never back to the caller: the caller, Hermes's model included, learns
only whether it was delivered (household-assistant ADR 0077). Nothing here logs or prints
the command, the answer, the secret or the function's URL.

The request is the contract in household-assistant's
`src/household_assistant/channels/imessage_mac/__init__.py`: `POST /`, JSON of three
strings `sender`, `sent_at` and `text`, the secret in `X-Household-Assistant-Secret`.
"""

from __future__ import annotations

import getpass
import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from . import handles, imsg

FUNCTION_URL_RELATIVE_PATH = Path("data/imessage/household-function-url")  # under the Hermes home
# The login-Keychain item, read where it is and never copied (smart-home ADR 0011's pattern;
# the name is settled by smart-home ADR 0036).
KEYCHAIN_SERVICE = "household-assistant-imessage-secret"
REQUEST_TIMEOUT_SECONDS = 15  # the function gives up at 10
SECRET_HEADER = "X-Household-Assistant-Secret"  # noqa: S105 -- a header name, not a secret
SECURITY_TOOL = "/usr/bin/security"
URL_SCHEME = "https://"


class ForwardError(Exception):
    """The forward could not be attempted: a missing secret, URL or allow list."""


@dataclass(frozen=True)
class ForwardOutcome:
    """The result of one forward, in words safe for a transcript: never the answer itself."""

    delivered: bool
    status: int | None
    summary: str


def read_household_secret(
    service: str = KEYCHAIN_SERVICE,
    account: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """The household secret from the login Keychain.

    Raises:
        ForwardError: the item is missing or empty. The message never holds the value.
    """
    completed = run([SECURITY_TOOL, "find-generic-password", "-s", service, "-a", account or getpass.getuser(), "-w"],
                    capture_output=True, text=True, check=False)
    secret = completed.stdout.strip()
    if completed.returncode != 0 or not secret:
        raise ForwardError(f"the login Keychain has no item {service!r} for this user (AHS-09 makes it)")
    return secret


def read_function_url(path: Path) -> str:
    """The inbound function's URL, one line in *path*.

    Raises:
        ForwardError: the file is missing, or its line is not an https URL.
    """
    try:
        url = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ForwardError(f"cannot read the function's URL from {path}: {error.strerror}") from error
    if not url.startswith(URL_SCHEME) or any(character.isspace() for character in url):
        raise ForwardError(f"{path} does not hold one https URL")
    return url if url.endswith("/") else f"{url}/"


def build_request(function_url: str, secret: str, sender: str, sent_at: str, text: str) -> urllib.request.Request:
    """The function's request for one message."""
    body = json.dumps({"sender": sender, "sent_at": sent_at, "text": text}).encode("utf-8")
    return urllib.request.Request(
        function_url, data=body, method="POST",
        headers={"Content-Type": "application/json", SECRET_HEADER: secret})


def post_command(
    request: urllib.request.Request, opener: Callable[..., object] = urllib.request.urlopen,
) -> tuple[int, str]:
    """Send *request*; return the status and the body. An HTTP error status is returned, not raised."""
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # type: ignore[attr-defined]
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, ""


def now_iso8601() -> str:
    """This moment as Format Date's "ISO 8601" with the time: `2026-10-27T13:14:05-04:00`."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ForwardSettings:
    """Where the forward reads what it needs."""

    allow_list_path: Path
    function_url_path: Path
    imsg_path: Path


def forward_household_command(
    text: str,
    settings: ForwardSettings,
    secret_reader: Callable[[], str] = read_household_secret,
    opener: Callable[..., object] = urllib.request.urlopen,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clock: Callable[[], str] = now_iso8601,
) -> ForwardOutcome:
    """Post *text* to the function as Zack, and send any answer to his phone.

    Raises:
        ForwardError: the allow list, the URL or the secret is missing. Nothing was sent.
    """
    if not text.strip():
        raise ForwardError("there is no command to forward")
    try:
        allowed = handles.read_allowed_handles(settings.allow_list_path)
        zack = handles.first_phone_number(allowed)
    except handles.AllowListError as error:
        raise ForwardError(str(error)) from error
    request = build_request(read_function_url(settings.function_url_path), secret_reader(), zack, clock(), text)
    try:
        status, answer = post_command(request, opener)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return ForwardOutcome(delivered=False, status=None,
                              summary=f"Could not reach the household assistant ({type(error).__name__}).")
    if status == HTTPStatus.NO_CONTENT or (status == HTTPStatus.OK and not answer.strip()):
        return ForwardOutcome(delivered=False, status=status,
                              summary="The household assistant took the message and had nothing to send back.")
    if status != HTTPStatus.OK:
        return ForwardOutcome(delivered=False, status=status,
                              summary=f"The household assistant refused the message with HTTP {status}.")
    sent = imsg.send_message(answer, zack, allowed, settings.imsg_path, run=run)
    if sent.delivered:
        return ForwardOutcome(delivered=True, status=status,
                              summary="The household assistant answered, and its answer was sent to Zack's phone.")
    return ForwardOutcome(
        delivered=False, status=status,
        summary=f"The household assistant answered, but sending its answer failed ({sent.disposition}).")
