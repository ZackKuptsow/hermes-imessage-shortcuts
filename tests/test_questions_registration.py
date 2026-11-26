"""`imessage-questions` as Hermes registers it, and one question through both plugins to the stand-in imsg.

Under Hermes's own interpreter: registration reads the secret through Hermes's scoped reader,
and the question travels over real HTTP to the real adapter.
"""

from __future__ import annotations

import asyncio
import os
import threading
import unittest
from collections.abc import Callable
from unittest import mock

from support import (
    QUESTIONS_PLUGIN_DIRECTORY,
    ZACK_PHONE,
    hermes_is_importable,
    hermes_is_required,
    plugin_module,
    questions_plugin_package,
)
from test_adapter import AdapterHarness

ARRIVAL_LIMIT_SECONDS = 10
POLL_SECONDS = 0.05
QUESTION = "Which branch should the fix land on?"
QUESTION_SECRET = "q" * 40
SECOND_TEXT_WAIT_SECONDS = 1.5
TEXT_ARGUMENT_PREFIX = "--text="
TO_ARGUMENT_PREFIX = "--to="
WORKSPACE = "/Users/someone/Projects/dotfiles"


class HookContext:
    """Stands in for Hermes's PluginContext: it records what the plugin registers and answers what it reads."""

    def __init__(self, saved: dict | None = None, workspace: str = WORKSPACE):
        self.hooks: dict[str, Callable[..., None]] = {}
        self.saved = saved or {}
        self.unload_callbacks: list[Callable[[], None]] = []
        self.workspace = workspace

    def get_config(self, key: str, default: object = None) -> object:
        return self.saved.get(key, default)

    def on_unload(self, callback: Callable[[], None]) -> None:
        self.unload_callbacks.append(callback)

    def register_hook(self, hook_name: str, callback: Callable[..., None], **_: object) -> None:
        self.hooks[hook_name] = callback

    def session_cwd(self) -> str:
        return self.workspace


def question_request(request_id: str = "request-1") -> dict:
    return {"kind": "clarify", "platform": "cli", "prompt": QUESTION, "request_id": request_id,
            "session_id": "20261125_101500_a1b2c3", "session_key": "agent:main:cli"}


@unittest.skipUnless(hermes_is_importable() or hermes_is_required(), "needs Hermes's interpreter (gateway)")
class RegistrationTest(unittest.TestCase):
    def test_the_entry_point_registers_exactly_the_hooks_the_manifest_names(self):
        import hermes_yaml

        context = HookContext()
        questions_plugin_package().register(context)
        manifest = hermes_yaml.safe_load((QUESTIONS_PLUGIN_DIRECTORY / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(context.hooks), ["on_human_input_request", "on_human_input_resolved"])
        self.assertEqual(sorted(manifest["provides_hooks"]), sorted(context.hooks))

    def test_unloading_the_plugin_cancels_the_questions_still_waiting(self):
        context = HookContext(saved={"delay_seconds": 600})
        questions_plugin_package().register(context)
        before = set(threading.enumerate())
        with mock.patch.dict(os.environ, {"IMESSAGE_QUESTIONS_SECRET": QUESTION_SECRET}):
            context.hooks["on_human_input_request"](**question_request())
        (timer,) = set(threading.enumerate()) - before
        for unload in context.unload_callbacks:
            unload()
        timer.join(timeout=ARRIVAL_LIMIT_SECONDS)
        self.assertFalse(timer.is_alive())

    def test_without_the_secret_in_this_profile_nothing_waits(self):
        context = HookContext()
        questions_plugin_package().register(context)
        before = set(threading.enumerate())
        with mock.patch.dict(os.environ, {"IMESSAGE_QUESTIONS_SECRET": ""}), \
                self.assertLogs("imessage_questions", level="ERROR"):
            context.hooks["on_human_input_request"](**question_request())
        self.assertEqual(set(threading.enumerate()) - before, set())


class ThroughBothPluginsTest(AdapterHarness):
    def setUp(self):
        super().setUp()
        secrets = mock.patch.dict(os.environ, {
            "IMESSAGE_QUESTIONS_SECRET": QUESTION_SECRET, "IMESSAGE_SHORTCUTS_QUESTION_SECRET": QUESTION_SECRET})
        secrets.start()
        self.addCleanup(secrets.stop)

    async def texts_sent(self, count: int) -> list[list[str]]:
        """The stand-in imsg's calls once there are *count* of them, or as many as arrived in time."""
        for _ in range(int(ARRIVAL_LIMIT_SECONDS / POLL_SECONDS)):
            if len(self.imsg_calls()) >= count:
                break
            await asyncio.sleep(POLL_SECONDS)
        return self.imsg_calls()

    async def test_a_question_nobody_answers_arrives_as_one_text_naming_the_workspace_folder(self):
        await self.start_adapter()
        context = HookContext(saved={"delay_seconds": 0, "port": self.port})
        questions_plugin_package().register(context)
        context.hooks["on_human_input_request"](**question_request())
        (call,) = await self.texts_sent(1)
        self.assertIn(f"{TO_ARGUMENT_PREFIX}{ZACK_PHONE}", call)
        self.assertIn(
            TEXT_ARGUMENT_PREFIX + plugin_module("question_text").compose_question_text(QUESTION, "dotfiles"), call)

    async def test_a_question_answered_at_the_mac_first_sends_no_text(self):
        await self.start_adapter()
        context = HookContext(saved={"delay_seconds": 0.2, "port": self.port})
        questions_plugin_package().register(context)
        context.hooks["on_human_input_request"](**question_request("request-answered"))
        context.hooks["on_human_input_resolved"](**question_request("request-answered"), outcome="submitted")
        context.hooks["on_human_input_request"](**question_request("request-unanswered"))
        self.assertEqual(len(await self.texts_sent(1)), 1)  # the unanswered one, sent after the shared delay
        await asyncio.sleep(SECOND_TEXT_WAIT_SECONDS)  # a text for the answered one would have arrived by now
        self.assertEqual(len(self.imsg_calls()), 1)


if __name__ == "__main__":
    unittest.main()
