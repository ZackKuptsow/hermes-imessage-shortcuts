"""The posted body and the secret, checked before anything else sees them."""

from __future__ import annotations

import json
import unittest

from support import SECRET, ZACK_PHONE, plugin_module

inbound = plugin_module("inbound")

TEXT = "add oat milk to the list"


def body(**fields: object) -> bytes:
    return json.dumps({"sender": ZACK_PHONE, "sent_at": "2026-10-27T13:14:05-04:00", "text": TEXT, **fields}).encode()


class SecretMatchesTest(unittest.TestCase):
    def test_the_exact_secret_matches(self):
        self.assertTrue(inbound.secret_matches(SECRET, SECRET))

    def test_a_wrong_or_missing_secret_does_not(self):
        for presented in (SECRET + "x", SECRET[:-1], "", None, "é" * 40):
            with self.subTest(presented=presented):
                self.assertFalse(inbound.secret_matches(SECRET, presented))

    def test_a_short_configured_secret_never_matches_even_itself(self):
        self.assertFalse(inbound.secret_matches("short", "short"))
        self.assertFalse(inbound.secret_matches("", ""))


class ParsePostedMessageTest(unittest.TestCase):
    def test_reads_the_three_strings_and_ignores_extra_fields(self):
        posted = inbound.parse_posted_message(body(extra="ignored"))
        self.assertEqual((posted.sender, posted.text), (ZACK_PHONE, TEXT))

    def test_refuses_what_is_not_an_object_of_the_three_strings(self):
        for raw in (body(text=5), body(sender=None), json.dumps(["a"]).encode(), b"{not json",
                    b"\xff\xfe", json.dumps({"sender": ZACK_PHONE}).encode(), b"x" * 70_000):
            with self.subTest(raw=raw[:20]), self.assertRaises(inbound.MalformedMessageError) as raised:
                inbound.parse_posted_message(raw)
            self.assertNotIn(TEXT, str(raised.exception))

    def test_the_key_tells_messages_apart_and_repeats_for_the_same_one(self):
        first = inbound.parse_posted_message(body())
        self.assertEqual(first.key(), inbound.parse_posted_message(body()).key())
        self.assertNotEqual(first.key(), inbound.parse_posted_message(body(text="another")).key())


if __name__ == "__main__":
    unittest.main()
