"""What the Shortcut posts, checked before anything else sees it.

The Message automation sends one request per text: `POST` with the shared secret in
`X-Hermes-Secret` and a JSON object of three strings, `sender`, `sent_at` and `text` (the
household assistant's contract, so one Shortcut shape serves both automations). The
secret is compared first, in constant time; the body is read only after it matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

FIELD_NAMES = ("sender", "sent_at", "text")
MAXIMUM_BODY_BYTES = 65_536
SECRET_HEADER = "X-Hermes-Secret"  # noqa: S105 -- a header name, not a secret
SECRET_MINIMUM_LENGTH = 32


class MalformedMessageError(ValueError):
    """A body that is not a JSON object of the three strings."""


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


def parse_posted_message(raw: bytes) -> PostedMessage:
    """Read the three strings out of a request body.

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
    for name in FIELD_NAMES:
        value = document.get(name)
        if not isinstance(value, str):
            raise MalformedMessageError(f"the field {name!r} is missing or not a string")
        values[name] = value
    return PostedMessage(**values)
