"""The household service's fixed phrases: the texts that reach it around the model (smart-home ADR 0037).

The plugin's copy of what the service reads as a whole message, with nothing after it to
interpret (household-assistant's `core/rule_parser.py` and `channels/imessage_mac/keywords.py`),
pinned by `tests/test_fixed_phrases.py` to the ADR's set. A phrase added there reaches the
model until it is added here. A command that ends in free text stays with the model, since a
conversation can open with the same word.
"""

from __future__ import annotations

import re

# What a phone may add after the words, as the service's rule parser reads it.
CLOSING_PUNCTUATION = r"[.!?]*"
PHRASES = (
    r"(?:cancel|remove)\s+reminder\s+\d+",
    r"digest",
    r"done",
    r"done\s+\d+",
    r"done\s+shopping",
    r"help",
    r"help\s+(?:calendar|digest|reminders?|shopping|todos?)",
    r"list",
    r"my\s+todos",
    r"otis",
    r"reminders",
    r"stop",
    r"todos",
)
FIXED_PHRASE = re.compile(rf"(?:{'|'.join(PHRASES)}){CLOSING_PUNCTUATION}", re.IGNORECASE)


def is_fixed_phrase(text: str) -> bool:
    """True when *text*, trimmed, is exactly one of the phrases, in any case."""
    return FIXED_PHRASE.fullmatch(text.strip()) is not None
