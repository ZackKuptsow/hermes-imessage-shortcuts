"""Send one iMessage with imsg, and only to a handle on the allow list.

`imsg send --to` needs Automation for Messages alone. Without Full Disk Access, which the
gateway's process is never given on the Mac Studio (smart-home ADRs 0032 and 0035), imsg cannot
read the outgoing row back, so `sent` means Messages accepted the AppleScript call, not
that the message arrived.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import handles

DISPOSITION_NOT_STARTED = "not_started"
DISPOSITION_SENT = "sent"
DISPOSITION_TIMED_OUT = "may_have_completed"
DISPOSITION_UNKNOWN = "unknown"
IMSG_INSTALL_LOCATIONS = (Path("/opt/homebrew/bin/imsg"), Path("/usr/local/bin/imsg"))
NUL_CHARACTER = "\x00"
SEND_TIMEOUT_SECONDS = 60
SERVICE = "imessage"
STATUS_SENT = "sent"
TEXT_ENCODING = "utf-8"


class SendRefusedError(Exception):
    """A send this module will not attempt: a handle off the allow list, no text, or a text imsg cannot be handed."""


class RecipientRefusedError(SendRefusedError):
    """The recipient is not a handle, or not on the allow list."""


class EmptyTextRefusedError(SendRefusedError):
    """There is no text to send."""


class UnsendableTextRefusedError(SendRefusedError):
    """The text holds U+0000 or a lone surrogate, which imsg cannot be handed or Messages cannot show."""


class ImsgMissingError(Exception):
    """imsg is not installed where it is looked for."""


@dataclass(frozen=True)
class SendOutcome:
    """What imsg reported: `delivered` is True only for a `sent` status.

    `disposition` is imsg's own word for a failure (`not_started` is safe to retry;
    `may_have_completed` and `still_in_flight` are not). It never holds the text.
    """

    delivered: bool
    disposition: str


def find_imsg(configured: str | None = None) -> Path:
    """The imsg executable: *configured* when given, else the Homebrew locations, else PATH.

    Raises:
        ImsgMissingError: none of them is an executable file.
    """
    candidates = [Path(configured)] if configured else [*IMSG_INSTALL_LOCATIONS]
    if not configured and (found := shutil.which("imsg")):
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return candidate
    raise ImsgMissingError("imsg is not installed; smart-home AHS-10 installs it with Homebrew")


def build_send_arguments(imsg_path: Path, handle: str, text: str) -> list[str]:
    """The argument vector for one send.

    `--to=` and `--text=` bind the value to its option, so a text that starts with a dash
    is still the text. iMessage is forced: the assistant's account has no SMS relay, and an
    SMS fallback would leave the account the message was meant to come from.
    """
    return [str(imsg_path), "send", f"--to={handle}", f"--text={text}", "--service", SERVICE, "--json"]


def read_outcome(completed: subprocess.CompletedProcess[str]) -> SendOutcome:
    """Read imsg's JSON answer, or its plain `sent`, into an outcome."""
    output = completed.stdout.strip()
    try:
        answer = json.loads(output) if output else {}
    except json.JSONDecodeError:
        answer = {"status": output}
    if not isinstance(answer, dict):
        answer = {}
    status = str(answer.get("status", ""))
    if completed.returncode == 0 and status == STATUS_SENT:
        return SendOutcome(delivered=True, disposition=DISPOSITION_SENT)
    disposition = str(answer.get("disposition") or answer.get("delivery_disposition") or DISPOSITION_UNKNOWN)
    return SendOutcome(delivered=False, disposition=disposition)


def check_text_can_be_handed(text: str) -> None:
    """Refuse a text that cannot be passed to imsg as an argument.

    `subprocess.run` raises ValueError for an argument holding U+0000, and for one it cannot
    encode, such as a lone surrogate. Strict UTF-8 refuses slightly more than it would (it
    passes U+DC80 to U+DCFF through `surrogateescape`), on purpose: no such text is one
    Messages can show. The text is refused, never altered, so nothing goes out changed.

    Raises:
        UnsendableTextRefusedError: the text holds either. The message never holds the text.
    """
    if NUL_CHARACTER in text:
        raise UnsendableTextRefusedError("refusing to send a text holding a NUL character")
    try:
        text.encode(TEXT_ENCODING)
    except UnicodeEncodeError:
        # Not chained: the encoding error's `object` is the whole text.
        raise UnsendableTextRefusedError("refusing to send a text holding a lone surrogate") from None


def check_send(text: str, handle: str, allowed_handles: tuple[str, ...]) -> str:
    """Refuse a send of *text* to *handle* this module will not attempt; else the handle's canonical form.

    Checks the recipient, then the text. A caller that splits a text checks the whole text
    here first, so a refusal can never arrive after part of it went out.

    Raises:
        RecipientRefusedError: the handle is not a handle or not on the allow list.
        EmptyTextRefusedError: the text is empty.
        UnsendableTextRefusedError: the text cannot be passed to imsg as an argument.
    """
    try:
        recipient = handles.canonical_handle(handle)
    except handles.InvalidHandleError as error:
        raise RecipientRefusedError(f"refusing to send: the recipient is {error}") from error
    if recipient not in allowed_handles:
        raise RecipientRefusedError("refusing to send to a handle that is not on the allow list")
    if not text.strip():
        raise EmptyTextRefusedError("refusing to send an empty message")
    check_text_can_be_handed(text)
    return recipient


def send_message(
    text: str,
    handle: str,
    allowed_handles: tuple[str, ...],
    imsg_path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SendOutcome:
    """Send *text* to *handle* when the handle is on *allowed_handles*.

    Raises:
        SendRefusedError: one of `check_send`'s refusals. Nothing is sent.
    """
    recipient = check_send(text, handle, allowed_handles)
    try:
        completed = run(build_send_arguments(imsg_path, recipient, text), capture_output=True, text=True,
                        timeout=SEND_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return SendOutcome(delivered=False, disposition=DISPOSITION_TIMED_OUT)
    except OSError:
        return SendOutcome(delivered=False, disposition=DISPOSITION_NOT_STARTED)
    return read_outcome(completed)
