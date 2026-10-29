"""The adapter over real HTTP on the loopback address, under Hermes's own interpreter.

Skipped under any other Python (the dotfiles test runner's python3 has no `gateway`);
`setup.sh` runs this suite with Hermes's interpreter before it links the plugin.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import (
    PARTNER_PHONE,
    SECRET,
    ZACK_EMAIL,
    ZACK_PHONE,
    hermes_is_importable,
    hermes_is_required,
    plugin_module,
    plugin_package,
)

TEXT = "what is on my calendar"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RegistryContext:
    """Stands in for Hermes's PluginContext: platforms go to the real registry, tools are recorded."""

    def __init__(self):
        self.platform_kwargs: dict = {}
        self.tools: dict = {}

    def register_platform(self, **kwargs) -> None:
        from gateway.platform_registry import PlatformEntry, platform_registry

        self.platform_kwargs = kwargs
        platform_registry.register(PlatformEntry(source="plugin", **kwargs))

    def register_tool(self, **kwargs) -> None:
        self.tools[kwargs["name"]] = kwargs


@unittest.skipUnless(hermes_is_importable() or hermes_is_required(), "needs Hermes's interpreter (gateway, aiohttp)")
class AdapterTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = RegistryContext()
        plugin_package().register(cls.context)

    def setUp(self):
        # The plugin serves the texting profile alone (smart-home ADR 0034), so the tests'
        # Hermes home is <root>/profiles/imessage, as in the gateway.
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "profiles/imessage"
        self.write_allow_list(self.home)
        self.record = self.root / "imsg-calls.jsonl"
        fake_imsg = self.root / "imsg"
        fake_imsg.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "open(os.environ['FAKE_IMSG_RECORD'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(json.dumps({'status': 'sent'}))\n")
        fake_imsg.chmod(fake_imsg.stat().st_mode | stat.S_IXUSR)
        self.port = free_port()
        self.environment = mock.patch.dict(os.environ, {
            "FAKE_IMSG_RECORD": str(self.record),
            "HERMES_HOME": str(self.home),
            "IMESSAGE_SHORTCUTS_ALLOWED_USERS": f"(555) 010-0199,{ZACK_EMAIL.upper()}",
            "IMESSAGE_SHORTCUTS_IMSG": str(fake_imsg),
            "IMESSAGE_SHORTCUTS_PORT": str(self.port),
            "IMESSAGE_SHORTCUTS_SECRET": SECRET,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.adapter_module = plugin_module("adapter")

    @staticmethod
    def write_allow_list(home: Path) -> None:
        (home / "data/imessage").mkdir(parents=True, exist_ok=True)
        (home / "data/imessage/zack-handles.txt").write_text(f"{ZACK_PHONE}\n{ZACK_EMAIL}\n")

    def imsg_calls(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    async def start_adapter(self):
        from gateway.config import PlatformConfig

        adapter = self.adapter_module.ImessageShortcutsAdapter(PlatformConfig(enabled=True))
        adapter.handle_message = mock.AsyncMock()
        self.assertTrue(await adapter.connect())
        self.addAsyncCleanup(adapter.disconnect)
        return adapter

    async def post(self, payload: object, secret: str | None = SECRET, raw: bytes | None = None) -> int:
        import aiohttp

        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-Hermes-Secret"] = secret
        data = raw if raw is not None else json.dumps(payload).encode()
        async with aiohttp.ClientSession() as session, session.post(
                f"http://127.0.0.1:{self.port}/imessage", data=data, headers=headers) as response:
            return response.status

    @staticmethod
    def message(sender: str = "+1 (555) 010-0199", text: str = TEXT) -> dict:
        return {"sender": sender, "sent_at": "2026-10-27T13:14:05-04:00", "text": text}

    # --- what the plugin registers -------------------------------------------------------

    def test_the_entry_point_registers_the_tool_and_a_platform_that_never_pairs(self):
        self.assertIn("household_forward", self.context.tools)
        kwargs = self.context.platform_kwargs
        self.assertEqual(kwargs["name"], "imessage_shortcuts")
        self.assertIs(kwargs["allowlist_suppresses_pairing"], True)
        self.assertIs(kwargs["allow_update_command"], False)
        self.assertEqual(kwargs["allowed_users_env"], "IMESSAGE_SHORTCUTS_ALLOWED_USERS")

    # --- what it refuses to start with ---------------------------------------------------

    def test_start_is_refused_for_each_unsafe_setting(self):
        cases = {
            "IMESSAGE_SHORTCUTS_HOST": ("0.0.0.0", "localhost", "192.168.1.20"),  # noqa: S104 -- refused on purpose
            "IMESSAGE_SHORTCUTS_SECRET": ("short", ""),
            "IMESSAGE_SHORTCUTS_ALLOW_ALL_USERS": ("true", "1"),
            "IMESSAGE_SHORTCUTS_ALLOWED_USERS": (ZACK_PHONE, f"{ZACK_PHONE},{ZACK_EMAIL},{PARTNER_PHONE}", ""),
            "IMESSAGE_SHORTCUTS_IMSG": (str(self.home / "absent"),),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}), \
                        self.assertRaises(self.adapter_module.StartupRefusedError):
                    self.adapter_module.read_startup_settings()

    def test_start_accepts_the_allow_list_in_another_written_form(self):
        host, port, _, allowed, _ = self.adapter_module.read_startup_settings()
        self.assertEqual((host, port, allowed), ("127.0.0.1", self.port, (ZACK_PHONE, ZACK_EMAIL)))

    # --- which profile it serves (smart-home ADR 0034) ------------------------------------

    def test_start_is_refused_in_any_profile_but_the_texting_one(self):
        for home in (self.root, self.root / "profiles/coder", self.root / "imessage"):
            self.write_allow_list(home)
            with self.subTest(home=str(home.relative_to(self.root))), \
                    mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                    self.assertRaisesRegex(self.adapter_module.StartupRefusedError, "imessage profile alone"):
                self.adapter_module.read_startup_settings()

    async def test_in_the_default_profile_connect_fails_and_nothing_listens(self):
        self.write_allow_list(self.root)
        adapter = self.adapter_module.ImessageShortcutsAdapter(self.platform_config())
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            self.assertFalse(await adapter.connect())
        self.assertIsNone(adapter._runner)
        refusal = (adapter._fatal_error_code, adapter._fatal_error_retryable)
        self.assertEqual(refusal, ("imessage_shortcuts_refused", False))
        self.assertIn("imessage profile alone", adapter._fatal_error_message)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))  # free: the adapter never bound it

    async def test_under_the_gateways_profile_override_the_serving_profile_decides(self):
        # The gateway serves a profile by setting this override (gateway.run's
        # _profile_runtime_scope), not by changing the environment. gateway.run is not imported
        # here: with HERMES_HOME on a temporary root it builds a runtime for that root and
        # re-executes the test run, which then skips every test.
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        coder_home = self.root / "profiles/coder"
        self.write_allow_list(coder_home)
        # The launch environment names the root, as when the default profile's gateway serves others.
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            token = set_hermes_home_override(coder_home)
            try:
                with self.assertRaises(self.adapter_module.StartupRefusedError):
                    self.adapter_module.read_startup_settings()
            finally:
                reset_hermes_home_override(token)
            token = set_hermes_home_override(self.home)
            try:
                self.assertEqual(self.adapter_module.read_startup_settings()[1], self.port)
            finally:
                reset_hermes_home_override(token)

    def platform_config(self):
        from gateway.config import PlatformConfig

        return PlatformConfig(enabled=True)

    async def test_it_listens_on_the_loopback_address_alone(self):
        adapter = await self.start_adapter()
        self.assertEqual({address[0] for address in adapter._runner.addresses}, {"127.0.0.1"})

    # --- what comes in -------------------------------------------------------------------

    async def test_zacks_text_is_taken_and_handed_to_the_gateway_as_him(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message()), 202)
        await asyncio.sleep(0.05)
        (call,) = adapter.handle_message.await_args_list
        event = call.args[0]
        self.assertEqual((event.text, event.source.chat_id, event.source.user_id), (TEXT, ZACK_PHONE, ZACK_PHONE))

    async def test_the_partners_text_never_reaches_the_gateway(self):
        adapter = await self.start_adapter()
        for sender in (PARTNER_PHONE, "Partner Name", ""):
            with self.subTest(sender=sender):
                self.assertEqual(await self.post(self.message(sender=sender)), 204)
        adapter.handle_message.assert_not_awaited()

    async def test_a_request_without_the_secret_is_refused_before_its_body_is_read(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message(), secret=None), 401)
        self.assertEqual(await self.post(self.message(), secret=SECRET[:-1] + "x"), 401)
        adapter.handle_message.assert_not_awaited()

    async def test_malformed_and_oversized_bodies_are_refused(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(None, raw=b"{not json"), 400)
        self.assertEqual(await self.post(None, raw=b'{"sender": "' + b"x" * 70_000 + b'"}'), 413)
        adapter.handle_message.assert_not_awaited()

    async def test_a_repeated_text_and_an_empty_one_are_dropped(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message()), 202)
        self.assertEqual(await self.post(self.message()), 204)
        self.assertEqual(await self.post(self.message(text="  ")), 204)
        await asyncio.sleep(0.05)
        self.assertEqual(adapter.handle_message.await_count, 1)

    # --- what goes out -------------------------------------------------------------------

    async def test_a_reply_to_zack_goes_through_imsg_as_plain_text(self):
        adapter = await self.start_adapter()
        result = await adapter.send(ZACK_PHONE, "**Done.** Two things left.")
        self.assertTrue(result.success)
        (call,) = self.imsg_calls()
        self.assertEqual(call[:2], ["send", f"--to={ZACK_PHONE}"])
        self.assertIn("--text=Done. Two things left.", call)

    async def test_a_send_to_anyone_else_is_refused_and_imsg_never_runs(self):
        adapter = await self.start_adapter()
        for handle in (PARTNER_PHONE, "Partner Name"):
            with self.subTest(handle=handle):
                self.assertFalse((await adapter.send(handle, "your pairing code is 123456")).success)
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_long_reply_arrives_as_several_texts(self):
        adapter = await self.start_adapter()
        self.assertTrue((await adapter.send(ZACK_PHONE, "word " * 1500)).success)
        self.assertGreater(len(self.imsg_calls()), 1)


if __name__ == "__main__":
    unittest.main()
