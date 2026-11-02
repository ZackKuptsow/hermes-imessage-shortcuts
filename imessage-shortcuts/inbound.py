"""What the listener is posted, checked before anything else sees it.

The Message automation sends one request per text: `POST` with the shared secret in
`X-Hermes-Secret` and a JSON object of three strings, `sender`, `sent_at` and `text` (the
household assistant's contract, so one Shortcut shape serves both automations). A send
request, from a loopback caller such as n8n's alarms, is the same secret and a JSON object
of two strings, `to` and `text`. The secret is compared first, in constant time; the body
is read only after it matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

FIELD_NAMES = ("sender", "sent_at", "text")
MAXIMUM_BODY_BYTES = 65_536
SECRET_HEADER = "X-Hermes-Secret"  # noqa: S105 -- a header name, not a secret
SECRET_MINIMUM_LENGTH = 32
SEND_FIELD_NAMES = ("text", "to")


class MalformedMessageError(ValueError):
    """A body that is not a JSON object of the strings its route takes."""


@dataclass(frozen=True)
class PostedMessage:
    """One text as the Shortcut handed it over; the sender is not yet canonical."""

    sender: str
    sent_at: str
    text: str

    def key(self) -> str:
        """An identifier for duplicate detection: the same sender, moment and text give the same key."""
        joined = "\x1f".join((self.sender, self.sent_at, self.text))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def secret_matches(expected: str, presented: str | None) -> bool:
    """True when *presented* is exactly *expected*, compared in constant time.

    An *expected* shorter than the minimum never matches, so a blank or short secret in the
    configuration fails closed instead of admitting a blank header.
    """
    if presented is None or len(expected) < SECRET_MINIMUM_LENGTH:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def is_a_moment(value: str) -> bool:
    """True when *value* is an ISO 8601 moment with its time and its offset.

    The household function refuses the same (its `request.py`). A date alone reads as a
    naive midnight, so the offset is what refuses it. A Format Date action given no date
    writes "", which would give every repeat of a text one key: Hermes then refuses the
    repeat as a changed copy of the first (`admission_conflict`) and texts back an error.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment.tzinfo is not None


@dataclass(frozen=True)
class SendRequest:
    """One text a loopback caller asks to have sent; the recipient is not yet canonical."""

    text: str
    to: str


def read_string_fields(raw: bytes, names: tuple[str, ...]) -> dict[str, str]:
    """The fields *names* out of a request body that must be a JSON object of strings.

    Raises:
        MalformedMessageError: the body is too long, not UTF-8 JSON, not an object, or a
            field is missing or not a string. The message never quotes the body.
    """
    if len(raw) > MAXIMUM_BODY_BYTES:
        raise MalformedMessageError("the body is too long")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedMessageError("the body is not UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise MalformedMessageError("the body is not a JSON object")
    values = {}
    for name in names:
        value = document.get(name)
        if not isinstance(value, str):
            raise MalformedMessageError(f"the field {name!r} is missing or not a string")
        values[name] = value
    return values


def parse_posted_message(raw: bytes) -> PostedMessage:
    """Read the three strings out of a posted text's body.

    Raises:
        MalformedMessageError: the body is not an object of the three strings
            (`read_string_fields`), or `sent_at` is not a moment with its time and offset.
    """
    values = read_string_fields(raw, FIELD_NAMES)
    if not is_a_moment(values["sent_at"]):
        raise MalformedMessageError("the field 'sent_at' is not a moment with its time and offset")
    return PostedMessage(**values)


def parse_send_request(raw: bytes) -> SendRequest:
    """Read the two strings out of a send request's body.

    An empty text is not malformed: `imsg.send_message` refuses it, and the listener says so.

    Raises:
        MalformedMessageError: the body is not an object of the two strings (`read_string_fields`).
    """
    return SendRequest(**read_string_fields(raw, SEND_FIELD_NAMES))
