"""`POST /imessage/question`: a session's unanswered question, texted to the first handle on the allow list.

Over real HTTP on the loopback address, under Hermes's own interpreter, through the stand-in imsg.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import SECRET, ZACK_EMAIL, ZACK_PHONE, plugin_module
from test_adapter import IMSG_NOT_STARTED, INGRESS, SEND, AdapterHarness

QUESTION = "Which branch should the fix land on?"
QUESTION_ROUTE = "/imessage/question"
QUESTION_SECRET = "q" * 40
SESSION = "dotfiles"
TEXT_ARGUMENT_PREFIX = "--text="
TO_ARGUMENT_PREFIX = "--to="


def asked(question: str = QUESTION, session: str = SESSION) -> dict:
    return {"question": question, "session": session}


class QuestionRouteTest(AdapterHarness):
    def setUp(self):
        super().setUp()
        self.question_environment = mock.patch.dict(os.environ, {"IMESSAGE_SHORTCUTS_QUESTION_SECRET": QUESTION_SECRET})
        self.question_environment.start()
        self.addCleanup(self.question_environment.stop)

    async def ask(self, payload: object = None, secret: str | None = QUESTION_SECRET,
                  raw: bytes | None = None) -> tuple[int, bytes]:
        """POST to the question route: the status and the body."""
        status, _, answer = await self.request(QUESTION_ROUTE, asked() if payload is None else payload, secret, raw)
        return status, answer

    def argument(self, call_index: int, prefix: str) -> str:
        """The value of one `--name=value` argument the stand-in imsg was given."""
        return next(value[len(prefix):] for value in self.imsg_calls()[call_index] if value.startswith(prefix))

    # --- what it sends, and to whom -------------------------------------------------------

    async def test_a_question_is_texted_to_the_first_handle_on_the_allow_list(self):
        await self.start_adapter()
        status, _ = await self.ask()
        self.assertEqual(status, 202)
        self.assertEqual(len(self.imsg_calls()), 1)
        self.assertEqual(self.argument(0, TO_ARGUMENT_PREFIX), ZACK_PHONE)
        self.assertEqual(self.argument(0, TEXT_ARGUMENT_PREFIX),
                         plugin_module("question_text").compose_question_text(QUESTION, SESSION))

    async def test_the_first_handle_decides_even_when_it_is_an_email_address(self):
        (self.home / "data/imessage/zack-handles.txt").write_text(f"{ZACK_EMAIL}\n{ZACK_PHONE}\n")
        await self.start_adapter()
        await self.ask()
        self.assertEqual(self.argument(0, TO_ARGUMENT_PREFIX), ZACK_EMAIL)

    async def test_a_question_reaches_no_session_so_the_model_never_sees_it(self):
        adapter = await self.start_adapter()
        await self.ask()
        adapter.handle_message.assert_not_awaited()

    async def test_markdown_in_a_question_goes_out_as_plain_text(self):
        await self.start_adapter()
        await self.ask(asked(question="Use **main** or `staging`?"))
        self.assertIn("Use main or staging?", self.argument(0, TEXT_ARGUMENT_PREFIX))

    # --- who may ask ----------------------------------------------------------------------

    async def test_with_no_question_secret_the_route_does_not_exist(self):
        with mock.patch.dict(os.environ, {"IMESSAGE_SHORTCUTS_QUESTION_SECRET": ""}):
            await self.start_adapter()
        for secret in (QUESTION_SECRET, SECRET, None):
            with self.subTest(secret=secret):
                self.assertEqual((await self.ask(secret=secret))[0], 404)
        self.assertEqual(self.imsg_calls(), [])

    async def test_the_shortcuts_secret_does_not_open_the_question_route(self):
        await self.start_adapter()
        for secret in (SECRET, QUESTION_SECRET[:-1] + "x", "", None):
            with self.subTest(secret=secret):
                self.assertEqual((await self.ask(secret=secret))[0], 401)
        self.assertEqual(self.imsg_calls(), [])

    async def test_the_question_secret_opens_neither_the_ingress_nor_the_send_route(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message(), secret=QUESTION_SECRET), 401)
        self.assertEqual((await self.send_request(self.alarm(), secret=QUESTION_SECRET))[0], 401)
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(self.imsg_calls(), [])

    def test_start_is_refused_for_a_short_question_secret(self):
        with mock.patch.dict(os.environ, {"IMESSAGE_SHORTCUTS_QUESTION_SECRET": "short"}), \
                self.assertRaises(self.adapter_module.StartupRefusedError):
            self.adapter_module.read_startup_settings()

    def test_start_is_refused_when_the_question_secret_is_the_shortcuts_secret(self):
        with mock.patch.dict(os.environ, {"IMESSAGE_SHORTCUTS_QUESTION_SECRET": SECRET}), \
                self.assertRaises(self.adapter_module.StartupRefusedError) as refused:
            self.adapter_module.read_startup_settings()
        self.assertNotIn(SECRET, str(refused.exception))

    def test_the_settings_never_show_the_question_secret(self):
        self.assertNotIn(QUESTION_SECRET, repr(self.adapter_module.read_startup_settings()))

    # --- what it refuses ------------------------------------------------------------------

    async def test_a_malformed_question_is_refused_and_imsg_never_runs(self):
        await self.start_adapter()
        for raw in (b"{not json", json.dumps({"question": QUESTION}).encode(),
                    json.dumps(asked(question=" ")).encode()):
            with self.subTest(raw=raw[:20]):
                self.assertEqual((await self.ask(raw=raw))[0], 400)
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_question_imsg_cannot_be_handed_is_a_bad_request_and_imsg_never_runs(self):
        await self.start_adapter()
        self.assertEqual((await self.ask(asked(question=f"{QUESTION}\x00")))[0], 400)
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_question_while_the_platform_is_not_serving_is_refused(self):
        adapter = await self.start_adapter()
        adapter._settings = None
        self.assertEqual((await self.ask())[0], 503)
        self.assertEqual(self.imsg_calls(), [])

    async def test_imsg_not_sending_is_a_502_with_its_disposition_and_never_the_question(self):
        await self.start_adapter()
        self.fake_imsg_answers(IMSG_NOT_STARTED, exit_code=1)
        status, answer = await self.ask()
        self.assertEqual((status, json.loads(answer)), (502, {"disposition": "not_started"}))
        self.assertNotIn(QUESTION.encode(), answer)

    async def test_the_question_route_never_logs_the_question_a_handle_or_a_secret(self):
        await self.start_adapter()
        with self.assertLogs("imessage_shortcuts", level="DEBUG") as logged:
            statuses = [(await self.ask())[0],
                        (await self.ask(asked(question=f"{QUESTION}\x00")))[0],
                        (await self.ask(raw=json.dumps({"question": QUESTION}).encode()))[0],
                        (await self.ask(secret=SECRET))[0]]
            self.fake_imsg_answers(IMSG_NOT_STARTED, exit_code=1)
            statuses.append((await self.ask())[0])
        self.assertEqual(statuses, [202, 400, 400, 401, 502])  # every path that logs was taken
        for record in logged.records:
            for kept_out in (QUESTION, SESSION, "5550100199", "010-0199", ZACK_EMAIL, QUESTION_SECRET, SECRET):
                with self.subTest(message=record.getMessage()):
                    self.assertNotIn(kept_out, record.getMessage())

    async def test_the_other_routes_still_answer_with_the_question_route_on(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message()), 202)
        self.assertEqual((await self.send_request(self.alarm()))[0], 202)
        adapter.handle_message.assert_awaited_once()
        self.assertEqual((INGRESS, SEND), (self.adapter_module.INGRESS_PATH, self.adapter_module.SEND_PATH))


if __name__ == "__main__":
    unittest.main()
