"""The text a waiting session's question becomes, and the body it arrives in."""

from __future__ import annotations

import json
import unittest

from support import plugin_module

inbound = plugin_module("inbound")
question_text = plugin_module("question_text")

QUESTION = "Which branch should the fix land on?"
SESSION = "dotfiles"


def body(**fields: object) -> bytes:
    return json.dumps({"question": QUESTION, "session": SESSION, **fields}).encode()


class ParseQuestionRequestTest(unittest.TestCase):
    def test_reads_the_two_strings_and_ignores_extra_fields(self):
        asked = inbound.parse_question_request(body(extra="ignored"))
        self.assertEqual((asked.question, asked.session), (QUESTION, SESSION))

    def test_a_session_with_no_label_is_taken(self):
        self.assertEqual(inbound.parse_question_request(body(session="")).session, "")

    def test_refuses_what_is_not_an_object_of_the_two_strings(self):
        for raw in (body(question=5), body(session=None), json.dumps(["a"]).encode(), b"{not json", b"\xff\xfe",
                    json.dumps({"question": QUESTION}).encode(), b"x" * 70_000):
            with self.subTest(raw=raw[:20]), self.assertRaises(inbound.MalformedMessageError):
                inbound.parse_question_request(raw)

    def test_refuses_a_question_with_no_words_since_the_text_would_ask_nothing(self):
        for blank in ("", "   ", "\n\t"):
            with self.subTest(blank=blank), self.assertRaises(inbound.MalformedMessageError):
                inbound.parse_question_request(body(question=blank))

    def test_a_refusal_never_quotes_the_question(self):
        with self.assertRaises(inbound.MalformedMessageError) as refused:
            inbound.parse_question_request(json.dumps({"question": QUESTION, "session": 7}).encode())
        self.assertNotIn(QUESTION, str(refused.exception))


class ComposeQuestionTextTest(unittest.TestCase):
    def test_the_text_names_the_session_then_the_question_then_where_to_answer(self):
        self.assertEqual(
            question_text.compose_question_text(QUESTION, SESSION),
            f"Hermes is waiting on your answer in dotfiles:\n{QUESTION}\n\n{question_text.WHERE_TO_ANSWER}")

    def test_with_no_session_label_the_lead_names_none(self):
        self.assertEqual(question_text.compose_question_text(QUESTION, "  ").splitlines()[0],
                         "Hermes is waiting on your answer:")

    def test_a_label_is_one_line_however_it_arrived(self):
        lead = question_text.compose_question_text(QUESTION, " my\nproject\t folder ").splitlines()[0]
        self.assertEqual(lead, "Hermes is waiting on your answer in my project folder:")

    def test_the_question_keeps_its_own_lines_without_the_space_around_it(self):
        composed = question_text.compose_question_text("\n  First?\nSecond?  \n", SESSION)
        self.assertEqual(composed.splitlines()[1:3], ["First?", "Second?"])

    def test_the_text_says_a_reply_by_text_does_not_reach_the_session(self):
        self.assertIn("does not reach that session", question_text.WHERE_TO_ANSWER)


if __name__ == "__main__":
    unittest.main()
