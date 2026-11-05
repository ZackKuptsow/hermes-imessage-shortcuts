"""A text that is exactly a fixed phrase goes to the household service, around the model
(smart-home ADR 0037).

Over real HTTP on the loopback address, under Hermes's own interpreter. The real forward runs
and sends through the stand-in imsg; only the function's answer and the Keychain are stood in for.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import unittest
import urllib.error
from collections.abc import Callable
from http import HTTPStatus
from unittest import mock

from support import PARTNER_PHONE, ZACK_PHONE, plugin_module
from test_adapter import IMSG_NOT_STARTED, IMSG_SENT, INGRESS, WIDENED_TOOLSETS, AdapterHarness

ANSWER = "Reminder removed: pick up oat milk"
FUNCTION_URL = "https://function.example.invalid/"
HOUSEHOLD_SECRET = "h" * 64
HOUSEHOLD_SECRET_HEADER = "X-household-assistant-secret"  # as urllib.request.Request capitalizes a header
PHRASE = "remove reminder 7"
SLOW_FUNCTION_LIMIT_SECONDS = 3  # how long the stand-in function waits for the listener to have answered
TEXT_ARGUMENT_PREFIX = "--text="
UNREADABLE_BODY = b"oat milk\xff"


class FunctionResponse(io.BytesIO):
    """urlopen's response as post_command reads it: a readable body with a status."""

    def __init__(self, status: int, body: bytes = b""):
        super().__init__(body)
        self.status = status


def raising(error: Exception) -> Callable[[], FunctionResponse]:
    """A function answer that raises where urlopen would."""
    def answer() -> FunctionResponse:
        raise error
    return answer


class FixedPhraseRoutingTest(AdapterHarness):
    def setUp(self):
        super().setUp()
        self.url_file = self.home / "data/imessage/household-function-url"
        self.url_file.write_text(f"{FUNCTION_URL}\n")
        self.function_answer = lambda: FunctionResponse(HTTPStatus.OK, ANSWER.encode())
        self.function_requests = []
        self.household = plugin_module("household")
        self.read_household_secret = lambda: HOUSEHOLD_SECRET
        real_forward = self.household.forward_household_command

        def forward(*arguments, **keywords):
            return real_forward(*arguments, opener=self.open_function, secret_reader=self.read_household_secret,
                                **keywords)

        patcher = mock.patch.object(self.household, "forward_household_command", forward)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_function(self, request, timeout):
        self.function_requests.append(request)
        return self.function_answer()

    async def post_and_settle(self, adapter, payload: dict) -> int:
        """POST a text and wait for what the listener handed to the background; its status.

        Anything a background task raises fails the test here.
        """
        status = await self.post(payload)
        await asyncio.gather(*list(adapter._background_tasks))
        return status

    def sent_texts(self) -> list[str]:
        """The text of each imsg call, in order."""
        return [argument[len(TEXT_ARGUMENT_PREFIX):] for call in self.imsg_calls() for argument in call
                if argument.startswith(TEXT_ARGUMENT_PREFIX)]

    # --- a phrase reaches the service, and only the service --------------------------------

    async def test_a_fixed_phrase_gets_the_services_answer_and_the_model_never_sees_it(self):
        adapter = await self.start_adapter()
        status = await self.post_and_settle(adapter, self.message(text=PHRASE))
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(self.sent_texts(), [ANSWER])
        self.assertIn(f"--to={ZACK_PHONE}", self.imsg_calls()[0])

    async def test_the_post_is_zacks_own_words_at_the_messages_own_moment_with_the_household_secret(self):
        adapter = await self.start_adapter()
        as_written = "Remove Reminder 7."
        await self.post_and_settle(adapter, self.message(text=as_written))
        self.assertEqual(len(self.function_requests), 1)
        request = self.function_requests[0]
        self.assertEqual(json.loads(request.data),
                         {"sender": ZACK_PHONE, "sent_at": self.message()["sent_at"], "text": as_written})
        self.assertEqual(request.get_header(HOUSEHOLD_SECRET_HEADER), HOUSEHOLD_SECRET)

    async def test_the_shortcut_is_answered_while_the_service_is_still_being_asked(self):
        released = threading.Event()
        released_in_time = []

        def slow_answer() -> FunctionResponse:
            released_in_time.append(released.wait(timeout=SLOW_FUNCTION_LIMIT_SECONDS))
            return FunctionResponse(HTTPStatus.OK, ANSWER.encode())

        self.function_answer = slow_answer
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message(text=PHRASE)), HTTPStatus.ACCEPTED)
        released.set()
        await asyncio.gather(*list(adapter._background_tasks))
        self.assertEqual(released_in_time, [True])

    async def test_a_free_text_command_reaches_the_model_and_not_the_service(self):
        adapter = await self.start_adapter()
        for text in ("remind me in 2 minutes test", "add milk", "what is on my calendar", "digest at 8am"):
            with self.subTest(text):
                await self.post_and_settle(adapter, self.message(text=text))
                self.assertEqual(adapter.handle_message.await_args.args[0].text, text)
        self.assertEqual(self.function_requests, [])

    # --- the checks before it stay first ---------------------------------------------------

    async def test_a_phrase_refused_by_a_check_never_reaches_the_service(self):
        adapter = await self.start_adapter()
        phrase = self.message(text=PHRASE)
        cases = {
            "the wrong secret": ({"secret": "w" * 40}, phrase, HTTPStatus.UNAUTHORIZED),
            "a sent_at with no moment": ({}, {**phrase, "sent_at": ""}, HTTPStatus.BAD_REQUEST),
            "a sender off the allow list": ({}, self.message(sender=PARTNER_PHONE, text=PHRASE),
                                            HTTPStatus.NO_CONTENT),
        }
        for label, (keywords, payload, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(await self.post(payload, **keywords), expected)
        await asyncio.gather(*list(adapter._background_tasks))
        self.assertEqual(self.function_requests, [])
        adapter.handle_message.assert_not_awaited()

    async def test_while_the_profile_is_widened_a_phrase_is_refused_and_never_reaches_the_service(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, WIDENED_TOOLSETS)
        self.assertEqual(await self.post_and_settle(adapter, self.message(text=PHRASE)),
                         HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(self.function_requests, [])

    async def test_a_repeated_delivery_is_posted_once(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post_and_settle(adapter, self.message(text=PHRASE)), HTTPStatus.ACCEPTED)
        _, dropped, _ = await self.request(INGRESS, self.message(text=PHRASE))
        self.assertEqual(dropped, "duplicate")
        self.assertEqual(len(self.function_requests), 1)

    # --- when the forward cannot run, the text goes to the gateway as before ----------------

    async def test_with_no_function_url_file_the_phrase_goes_to_the_gateway(self):
        self.url_file.unlink()
        adapter = await self.start_adapter()
        with self.assertLogs(level="WARNING") as logs:
            await self.post_and_settle(adapter, self.message(text=PHRASE))
        self.assertEqual(adapter.handle_message.await_args.args[0].text, PHRASE)
        self.assertEqual(self.function_requests, [])
        self.assertEqual(self.sent_texts(), [])
        self.assertNotIn(PHRASE, "\n".join(logs.output))

    async def test_with_no_keychain_item_the_phrase_goes_to_the_gateway(self):
        def no_item() -> str:
            raise self.household.ForwardError("the login Keychain has no item")

        self.read_household_secret = no_item
        adapter = await self.start_adapter()
        await self.post_and_settle(adapter, self.message(text=PHRASE))
        self.assertEqual(adapter.handle_message.await_args.args[0].text, PHRASE)
        self.assertEqual(self.function_requests, [])

    # --- when it runs and fails, Zack is told, and the log holds no text ----------------------

    async def assert_told(self, adapter, expected_text: str, expected_in_log: str) -> None:
        """A phrase's failure: one text to Zack, one log line with no household words, and no model."""
        with self.assertLogs(level="ERROR") as logs:
            await self.post_and_settle(adapter, self.message(text=PHRASE))
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(self.sent_texts()[-1], expected_text)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(expected_in_log, logs.output[0])
        for words in (PHRASE, ANSWER, "oat milk"):
            self.assertNotIn(words, logs.output[0])

    async def test_a_refusal_texts_zack_the_fixed_summary(self):
        refusal = urllib.error.HTTPError(FUNCTION_URL, HTTPStatus.FORBIDDEN, "refused", hdrs=None, fp=None)
        self.addCleanup(refusal.close)
        self.function_answer = raising(refusal)
        adapter = await self.start_adapter()
        await self.assert_told(adapter, "The household assistant refused the message with HTTP 403.", "HTTP 403")

    async def test_an_unreachable_service_texts_zack_the_fixed_summary(self):
        self.function_answer = raising(urllib.error.URLError("connection refused"))
        adapter = await self.start_adapter()
        await self.assert_told(adapter, "Could not reach the household assistant (URLError).", "URLError")

    async def test_a_failed_send_tries_to_tell_zack_the_fixed_summary(self):
        self.fake_imsg_answers_in_turn((IMSG_NOT_STARTED, 1), (IMSG_SENT, 0))
        adapter = await self.start_adapter()
        await self.assert_told(
            adapter, "The household assistant answered, but sending its answer failed (not_started).", "not_started")
        self.assertEqual(self.sent_texts()[0], ANSWER)

    async def test_an_answer_of_nothing_sends_no_text_and_does_not_reach_the_model(self):
        answers = {"a 204": FunctionResponse(HTTPStatus.NO_CONTENT), "a blank 200": FunctionResponse(HTTPStatus.OK)}
        adapter = await self.start_adapter()
        for number, (label, response) in enumerate(answers.items()):
            with self.subTest(label):
                self.function_answer = lambda answer=response: answer
                await self.post_and_settle(adapter, self.message(text=f"done {number}"))
        self.assertEqual(self.sent_texts(), [])
        adapter.handle_message.assert_not_awaited()

    # --- nothing awaits the routed task, so nothing it raises may lose the text silently ------

    async def test_an_answer_that_cannot_be_read_tells_zack_without_quoting_it(self):
        self.function_answer = lambda: FunctionResponse(HTTPStatus.OK, UNREADABLE_BODY)
        adapter = await self.start_adapter()
        with self.assertLogs(level="ERROR") as logs:
            await self.post_and_settle(adapter, self.message(text=PHRASE))
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(len(self.sent_texts()), 1)
        self.assertNotIn("oat milk", self.sent_texts()[0])
        self.assertNotIn("oat milk", "\n".join(logs.output))

    async def test_an_unexpected_failure_texts_zack_and_logs_its_type_alone(self):
        self.function_answer = raising(ValueError("http://[bad"))
        adapter = await self.start_adapter()
        with self.assertLogs(level="ERROR") as logs:
            await self.post_and_settle(adapter, self.message(text=PHRASE))
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(self.sent_texts(),
                         ["Something went wrong reaching the household assistant, so that may not have been done."])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ValueError", logs.output[0])
        self.assertNotIn("[bad", logs.output[0])

    # --- what a routed text leaves in the log ----------------------------------------------

    async def test_a_routed_phrase_logs_a_line_with_neither_the_phrase_nor_the_answer(self):
        adapter = await self.start_adapter()
        with self.assertLogs(level="INFO") as logs:
            await self.post_and_settle(adapter, self.message(text=PHRASE))
        lines = "\n".join(logs.output)
        self.assertIn("routed a fixed phrase", lines)
        self.assertNotIn(PHRASE, lines)
        self.assertNotIn(ANSWER, lines)


if __name__ == "__main__":
    unittest.main()
