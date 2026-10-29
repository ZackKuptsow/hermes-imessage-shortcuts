"""The adapter over real HTTP on the loopback address, under Hermes's own interpreter.

Skipped under any other Python (a plain python3 has no `gateway`);
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
        self.write_settings(self.home, ["imessage_shortcuts", "memory"])
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

    @staticmethod
    def write_settings(home: Path, toolsets: list[str]) -> None:
        """The profile's config.yaml, saving *toolsets* as the platform's list (ADR 0035)."""
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(json.dumps({"platform_toolsets": {"imessage_shortcuts": toolsets}}))

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
        settings = self.adapter_module.read_startup_settings()
        self.assertEqual((settings.host, settings.port, settings.allowed),
                         ("127.0.0.1", self.port, (ZACK_PHONE, ZACK_EMAIL)))
        self.assertNotIn(SECRET, repr(settings))

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
                self.assertEqual(self.adapter_module.read_startup_settings().port, self.port)
            finally:
                reset_hermes_home_override(token)

    # --- which toolsets a text reaches (smart-home ADR 0035) --------------------------------

    def test_start_is_refused_while_a_text_could_reach_more_than_two_toolsets(self):
        the_two = '"imessage_shortcuts": ["imessage_shortcuts", "memory"]'
        cases = {
            "a shell beside the two": '{"platform_toolsets": {"imessage_shortcuts": '
                                      '["imessage_shortcuts", "memory", "terminal"]}}',
            "no saved list": '{"model": {"provider": "custom"}}',
            "an MCP server": f'{{"platform_toolsets": {{{the_two}}}, "mcp_servers": {{"notes": {{}}}}}}',
            "an external memory provider": f'{{"platform_toolsets": {{{the_two}}}, '
                                           '"memory": {"provider": "retaindb"}}',
        }
        for label, text in cases.items():
            (self.home / "config.yaml").write_text(text)
            with self.subTest(label), self.assertRaises(self.adapter_module.StartupRefusedError):
                self.adapter_module.read_startup_settings()

    def test_start_is_refused_while_a_managed_overlay_adds_what_the_profiles_file_does_not(self):
        managed = self.root / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text('{"mcp_servers": {"notes": {"command": "notes-server"}}}')
        with mock.patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(managed)}), \
                self.assertRaisesRegex(self.adapter_module.StartupRefusedError, "mcp_servers"):
            self.adapter_module.read_startup_settings()

    def test_start_is_refused_while_hermess_resolver_would_add_a_toolset_the_saved_list_never_names(self):
        from hermes_cli import tools_config

        with mock.patch.object(tools_config, "_RECENTLY_SHIPPED_TOOLSETS", frozenset({"web"})), \
                self.assertRaisesRegex(self.adapter_module.StartupRefusedError, "Hermes resolves web"):
            self.adapter_module.read_startup_settings()

    def test_settings_that_cannot_be_read_are_refused_without_echoing_them(self):
        cases = {
            "unclosed YAML": b"platform_toolsets: [unclosed",
            "a date that is no date": b"stamp: 2026-13-45\n",
            "a tag that does not parse": b"count: !!int abc\n",
            "bytes that are not UTF-8": b"name: \xff\xfe\n",
        }
        for label, data in cases.items():
            (self.home / "config.yaml").write_bytes(data)
            with self.subTest(label), \
                    self.assertRaisesRegex(self.adapter_module.StartupRefusedError, "cannot read") as raised:
                self.adapter_module.read_startup_settings()
            self.assertNotIn("unclosed", str(raised.exception))
        (self.home / "config.yaml").unlink()
        with self.assertRaisesRegex(self.adapter_module.StartupRefusedError, "cannot read"):
            self.adapter_module.read_startup_settings()

    async def test_with_settings_that_raise_a_value_error_connect_refuses_rather_than_retrying(self):
        (self.home / "config.yaml").write_bytes(b"stamp: 2026-13-45\n")
        adapter = self.adapter_module.ImessageShortcutsAdapter(self.platform_config())
        self.assertFalse(await adapter.connect())
        refusal = (adapter._fatal_error_code, adapter._fatal_error_retryable)
        self.assertEqual(refusal, ("imessage_shortcuts_refused", False))

    async def test_with_a_widened_list_connect_fails_and_nothing_listens(self):
        self.write_settings(self.home, ["imessage_shortcuts", "memory", "file"])
        adapter = self.adapter_module.ImessageShortcutsAdapter(self.platform_config())
        self.assertFalse(await adapter.connect())
        self.assertIsNone(adapter._runner)
        self.assertEqual(adapter._fatal_error_code, "imessage_shortcuts_refused")
        self.assertIn("names file", adapter._fatal_error_message)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", self.port))  # free: the adapter never bound it

    async def test_every_text_gets_the_two_toolsets_whatever_the_saved_list_says_now(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, ["imessage_shortcuts", "memory", "terminal"])
        source = adapter.build_source(chat_id=ZACK_PHONE, chat_type="dm", user_id=ZACK_PHONE)
        self.assertEqual(adapter.toolsets_for_source(source), ["imessage_shortcuts", "memory"])

    async def test_a_text_is_refused_once_the_profile_widens_after_start(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, ["imessage_shortcuts", "memory", "terminal"])
        self.assertEqual(await self.post(self.message()), 503)
        adapter.handle_message.assert_not_awaited()

    def test_the_allowed_toolsets_agree_with_the_tool_and_with_hermess_memory_store(self):
        from agent.memory_provider import CORE_MEMORY_PROVIDER_SENTINELS

        allowed_toolsets = plugin_module("allowed_toolsets")
        self.assertIn(plugin_module("tools").TOOLSET, allowed_toolsets.ALLOWED_TOOLSETS)
        self.assertEqual(allowed_toolsets.CORE_MEMORY_PROVIDERS, CORE_MEMORY_PROVIDER_SENTINELS)

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

    async def handed_events(self, adapter, texts: list[str]) -> list:
        """Post each of *texts* from Zack, then return the events the gateway was handed."""
        for text in texts:
            self.assertEqual(await self.post(self.message(text=text)), 202)
        await asyncio.sleep(0.05)
        return [call.args[0] for call in adapter.handle_message.await_args_list]

    async def test_a_slash_command_a_text_may_not_run_reaches_the_model_as_words(self):
        adapter = await self.start_adapter()
        texts = ["/kanban create probe --body-file /etc/hosts", "/model some-cloud-model", "/restart", "/debug",
                 "/always", "/etc/hosts"]
        for text, event in zip(texts, await self.handed_events(adapter, texts), strict=True):
            with self.subTest(text=text):
                self.assertEqual(event.text, text)
                self.assertFalse(event.allow_gateway_control)
                self.assertFalse(event.is_command())

    async def test_new_reset_stop_and_the_confirm_replies_stay_commands(self):
        adapter = await self.start_adapter()
        texts = ["/new", "/reset", "/stop", "/approve", "/cancel"]
        for text, event in zip(texts, await self.handed_events(adapter, texts), strict=True):
            with self.subTest(text=text):
                self.assertEqual(event.get_command(), text[1:])

    async def test_a_plain_text_keeps_gateway_control_for_a_confirm_reply(self):
        adapter = await self.start_adapter()
        (event,) = await self.handed_events(adapter, ["yes"])
        self.assertTrue(event.allow_gateway_control)

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


    async def test_after_a_refused_reconnect_nothing_is_sent(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, ["imessage_shortcuts", "memory", "terminal"])
        self.assertFalse(await adapter.connect(is_reconnect=True))
        self.assertFalse((await adapter.send(ZACK_PHONE, "still here")).success)
        self.assertEqual(self.imsg_calls(), [])

    async def test_after_disconnect_nothing_is_sent(self):
        adapter = await self.start_adapter()
        await adapter.disconnect()
        self.assertFalse((await adapter.send(ZACK_PHONE, "still here")).success)
        self.assertEqual(self.imsg_calls(), [])

if __name__ == "__main__":
    unittest.main()
