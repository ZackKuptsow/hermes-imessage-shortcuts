"""The plugin's copy of the household service's fixed phrases, pinned to smart-home ADR 0037's set.

The service reads a command only as the whole message, so a near miss here must stay with the
model: a conversation can open like a command, and only an exact phrase goes around it.
"""

from __future__ import annotations

import unittest

from support import plugin_module

fixed_phrases = plugin_module("fixed_phrases")

PHRASES = (
    "OTIS",
    "STOP",
    "cancel reminder 2",
    "digest",
    "done",
    "done 12",
    "done 3",
    "done shopping",
    "help",
    "help calendar",
    "help digest",
    "help reminder",
    "help reminders",
    "help shopping",
    "help todo",
    "help todos",
    "list",
    "my todos",
    "reminders",
    "remove reminder 10",
    "remove reminder 2",
    "todos",
)
# What ADR 0037 leaves with the model: commands that end in free text, the new-list answers,
# the near misses a conversation could open with, and a slash command, which is the gateway's.
NOT_PHRASES = (
    "",
    "   ",
    "/stop",
    "add",
    "add milk",
    "digest at 8am",
    "digest off",
    "digest sections todos",
    "done 1 2",
    "done 1x",
    "done shopping now",
    "done with that",
    "done, thanks",
    "every day 5pm pills",
    "help me",
    "help todos please",
    "list the groceries",
    "my list",
    "my todo call the vet",
    "n",
    "no",
    "otis please",
    "please stop",
    "remind me in 2 minutes test",
    "reminders please",
    "remove reminder",
    "remove reminder one",
    "remove reminders 2",
    "snooze 5m",
    "stop it",
    "todo call the vet",
    "what's on monday",
    "y",
    "yes",
)


class FixedPhraseTest(unittest.TestCase):
    def test_each_phrase_of_the_set_is_one(self):
        for phrase in PHRASES:
            with self.subTest(phrase):
                self.assertTrue(fixed_phrases.is_fixed_phrase(phrase))

    def test_case_does_not_matter(self):
        for phrase in PHRASES:
            for written in (phrase.lower(), phrase.upper()):
                with self.subTest(written):
                    self.assertTrue(fixed_phrases.is_fixed_phrase(written))

    def test_the_ending_a_phone_adds_does_not_matter(self):
        for phrase in PHRASES:
            for ending in (".", "!", "?", "..."):
                with self.subTest(phrase + ending):
                    self.assertTrue(fixed_phrases.is_fixed_phrase(phrase + ending))

    def test_space_around_and_between_the_words_does_not_matter(self):
        for written in ("  done  ", "\ndone\n", "done   shopping", "my \t todos", "remove  reminder  2"):
            with self.subTest(written):
                self.assertTrue(fixed_phrases.is_fixed_phrase(written))

    def test_anything_else_stays_with_the_model(self):
        for text in NOT_PHRASES:
            with self.subTest(text):
                self.assertFalse(fixed_phrases.is_fixed_phrase(text))


if __name__ == "__main__":
    unittest.main()
