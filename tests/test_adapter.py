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

ALARM = "The texting path failed: plugin answered 503 at 17:02"
# The header and the paths are the contract with n8n, so the tests spell them out.
DROPPED_HEADER = "X-Imessage-Dropped"
IMSG_NOT_STARTED = '{"status": "error", "disposition": "not_started"}'
IMSG_SENT = '{"status": "sent"}'
INGRESS = "/imessage"
SEND = "/imessage/send"
TEXT = "what is on my calendar"
WIDENED_TOOLSETS = ["imessage_shortcuts", "memory", "terminal"]


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
        self.fake_imsg = self.root / "imsg"
        self.fake_imsg_answers(IMSG_SENT)
        self.port = free_port()
        self.environment = mock.patch.dict(os.environ, {
            "FAKE_IMSG_RECORD": str(self.record),
            "HERMES_HOME": str(self.home),
            "IMESSAGE_SHORTCUTS_ALLOWED_USERS": f"(555) 010-0199,{ZACK_EMAIL.upper()}",
            "IMESSAGE_SHORTCUTS_IMSG": str(self.fake_imsg),
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

    def fake_imsg_answers(self, answer: str, exit_code: int = 0) -> None:
        """The stand-in imsg: records each call, then prints *answer* and exits *exit_code*."""
        self.fake_imsg.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "open(os.environ['FAKE_IMSG_RECORD'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"print({answer!r})\nsys.exit({exit_code})\n")
        self.fake_imsg.chmod(self.fake_imsg.stat().st_mode | stat.S_IXUSR)

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

    async def request(self, path: str, payload: object, secret: str | None = SECRET,
                      raw: bytes | None = None) -> tuple[int, str | None, bytes]:
        """POST to *path* on the listener: the status, the drop-reason header (None when absent), the body."""
        import aiohttp

        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-Hermes-Secret"] = secret
        data = raw if raw is not None else json.dumps(payload).encode()
        async with aiohttp.ClientSession() as session, session.post(
                f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers) as response:
            return response.status, response.headers.get(DROPPED_HEADER), await response.read()

    async def post(self, payload: object, secret: str | None = SECRET, raw: bytes | None = None) -> int:
        """POST a text to the ingress; its status."""
        status, _, _ = await self.request(INGRESS, payload, secret, raw)
        return status

    async def send_request(self, payload: object, secret: str | None = SECRET,
                           raw: bytes | None = None) -> tuple[int, str | None, bytes]:
        """POST to the send endpoint: the status, the drop-reason header, the body."""
        return await self.request(SEND, payload, secret, raw)

    @staticmethod
    def alarm(to: str = ZACK_PHONE, text: str = ALARM) -> dict:
        return {"text": text, "to": to}

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
        self.write_settings(self.home, WIDENED_TOOLSETS)
        source = adapter.build_source(chat_id=ZACK_PHONE, chat_type="dm", user_id=ZACK_PHONE)
        self.assertEqual(adapter.toolsets_for_source(source), ["imessage_shortcuts", "memory"])

    async def test_a_text_is_refused_once_the_profile_widens_after_start(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, WIDENED_TOOLSETS)
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

    async def test_a_text_whose_sent_at_is_blank_or_has_no_time_is_refused(self):
        adapter = await self.start_adapter()
        for sent_at in ("", "2026-11-01"):
            with self.subTest(sent_at=sent_at):
                self.assertEqual(await self.post({**self.message(), "sent_at": sent_at}), 400)
        await asyncio.sleep(0.05)
        adapter.handle_message.assert_not_awaited()

    async def test_a_repeated_text_and_an_empty_one_are_dropped(self):
        adapter = await self.start_adapter()
        self.assertEqual(await self.post(self.message()), 202)
        self.assertEqual(await self.post(self.message()), 204)
        self.assertEqual(await self.post(self.message(text="  ")), 204)
        await asyncio.sleep(0.05)
        self.assertEqual(adapter.handle_message.await_count, 1)

    async def test_each_drop_says_why_so_n8n_can_alarm_on_a_sender_drop_alone(self):
        await self.start_adapter()
        self.assertEqual((await self.request(INGRESS, self.message()))[:2], (202, None))
        cases = (
            ("a sender off the allow list", self.message(sender=PARTNER_PHONE), "sender"),
            ("a sender that is no handle", self.message(sender="Partner Name"), "sender"),
            ("an empty text", self.message(text="  "), "empty"),
            ("a repeat of the first text", self.message(), "duplicate"),
        )
        for case, payload, reason in cases:
            with self.subTest(case):
                self.assertEqual((await self.request(INGRESS, payload))[:2], (204, reason))

    async def test_no_other_answer_from_the_ingress_carries_the_drop_header(self):
        await self.start_adapter()
        answers = [await self.request(INGRESS, self.message()),
                   await self.request(INGRESS, self.message(), secret=None),
                   await self.request(INGRESS, None, raw=b"{not json")]
        self.write_settings(self.home, WIDENED_TOOLSETS)
        answers.append(await self.request(INGRESS, self.message(text="another")))
        self.assertEqual([answer[:2] for answer in answers], [(202, None), (401, None), (400, None), (503, None)])

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
        self.write_settings(self.home, WIDENED_TOOLSETS)
        self.assertFalse(await adapter.connect(is_reconnect=True))
        self.assertFalse((await adapter.send(ZACK_PHONE, "still here")).success)
        self.assertEqual(self.imsg_calls(), [])

    async def test_after_disconnect_nothing_is_sent(self):
        adapter = await self.start_adapter()
        await adapter.disconnect()
        self.assertFalse((await adapter.send(ZACK_PHONE, "still here")).success)
        self.assertEqual(self.imsg_calls(), [])

    # --- the send endpoint: a text to an allowed handle, never through the model -----------

    async def test_a_send_to_an_allowed_handle_goes_out_through_imsg_as_plain_text(self):
        adapter = await self.start_adapter()
        answer = await self.send_request(self.alarm(to="+1 (555) 010-0199", text=f"**Alarm.** {ALARM}"))
        self.assertEqual(answer[:2], (202, None))
        (call,) = self.imsg_calls()
        self.assertEqual(call[:3], ["send", f"--to={ZACK_PHONE}", f"--text=Alarm. {ALARM}"])
        adapter.handle_message.assert_not_awaited()

    async def test_a_long_send_arrives_as_several_texts(self):
        await self.start_adapter()
        self.assertEqual((await self.send_request(self.alarm(text="word " * 1500)))[:2], (202, None))
        self.assertGreater(len(self.imsg_calls()), 1)

    async def test_a_send_to_anyone_else_is_dropped_as_sender_and_imsg_never_runs(self):
        await self.start_adapter()
        for handle in (PARTNER_PHONE, "Partner Name", ""):
            with self.subTest(handle=handle):
                answer = await self.send_request(self.alarm(to=handle, text="your pairing code is 123456"))
                self.assertEqual(answer[:2], (204, "sender"))
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_send_with_no_text_is_refused_as_a_bad_request_and_imsg_never_runs(self):
        # The caller composes the text, so an empty one is its bug: a 204 would read as success.
        await self.start_adapter()
        for text in ("", "  \n"):
            with self.subTest(text=text):
                self.assertEqual((await self.send_request(self.alarm(text=text)))[:2], (400, None))
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_send_without_the_secret_is_refused(self):
        await self.start_adapter()
        for secret in (None, SECRET[:-1] + "x"):
            with self.subTest(secret=secret):
                self.assertEqual((await self.send_request(self.alarm(), secret=secret))[:2], (401, None))
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_malformed_send_is_refused(self):
        await self.start_adapter()
        for raw in (b"{not json", json.dumps({"to": ZACK_PHONE}).encode(), json.dumps({"text": ALARM}).encode(),
                    json.dumps({"text": ALARM, "to": 5}).encode(), json.dumps([ZACK_PHONE, ALARM]).encode()):
            with self.subTest(raw=raw[:24]):
                self.assertEqual((await self.send_request(None, raw=raw))[:2], (400, None))
        self.assertEqual(self.imsg_calls(), [])

    async def test_a_send_while_the_platform_is_not_serving_is_refused(self):
        adapter = await self.start_adapter()
        self.write_settings(self.home, WIDENED_TOOLSETS)
        self.assertFalse(await adapter.connect(is_reconnect=True))
        self.assertEqual((await self.send_request(self.alarm()))[:2], (503, None))
        self.assertEqual(self.imsg_calls(), [])

    async def test_the_alarm_about_a_widened_profile_still_goes_out(self):
        # A profile widened after start has every text answered 503, and n8n alarms on that
        # 503 through this endpoint; a text's toolset check here would silence the alarm.
        # The next connect refuses the profile outright (the not-serving test above).
        await self.start_adapter()
        self.write_settings(self.home, WIDENED_TOOLSETS)
        self.assertEqual(await self.post(self.message()), 503)
        self.assertEqual((await self.send_request(self.alarm()))[:2], (202, None))
        self.assertEqual(len(self.imsg_calls()), 1)

    async def test_a_repeated_send_goes_out_again_since_a_retry_is_no_duplicate(self):
        # n8n retries an alarm whose send failed with the same body; a duplicate check here
        # would drop the retry, and the alarm would never arrive.
        await self.start_adapter()
        answers = [await self.send_request(self.alarm()), await self.send_request(self.alarm())]
        self.assertEqual([answer[:2] for answer in answers], [(202, None), (202, None)])
        self.assertEqual(len(self.imsg_calls()), 2)

    async def test_the_send_endpoint_never_logs_the_text_a_handle_or_the_secret(self):
        await self.start_adapter()
        with self.assertLogs("imessage_shortcuts", level="DEBUG") as logged:
            statuses = [(await self.send_request(self.alarm(to="+1 (555) 010-0199")))[0],
                        (await self.send_request(self.alarm(to=PARTNER_PHONE)))[0],
                        (await self.send_request(self.alarm(text="  ")))[0],
                        (await self.send_request(None, raw=json.dumps({"to": ZACK_PHONE}).encode()))[0],
                        (await self.send_request(self.alarm(), secret=SECRET[:-1] + "x"))[0]]
            self.fake_imsg_answers(IMSG_NOT_STARTED, exit_code=1)
            statuses.append((await self.send_request(self.alarm()))[0])
        self.assertEqual(statuses, [202, 204, 400, 400, 401, 502])  # every path that logs was taken
        for record in logged.records:
            for kept_out in (ALARM, "5550100199", "010-0199", "5550100142", SECRET, SECRET[:-1]):
                with self.subTest(message=record.getMessage()):
                    self.assertNotIn(kept_out, record.getMessage())

    async def test_imsg_not_sending_is_a_502_with_its_disposition_and_never_the_text(self):
        self.fake_imsg_answers(IMSG_NOT_STARTED, exit_code=1)
        await self.start_adapter()
        status, dropped, body = await self.send_request(self.alarm())
        self.assertEqual((status, dropped, json.loads(body)), (502, None, {"disposition": "not_started"}))
        self.assertNotIn(b"texting path", body)

    async def test_a_peer_off_the_loopback_address_is_refused_by_both_routes(self):
        # The listener binds the loopback address alone, so no test can connect from elsewhere:
        # each route's handler is handed a request whose peer is a LAN address instead.
        from aiohttp.test_utils import make_mocked_request

        adapter = await self.start_adapter()
        transport = mock.Mock()
        transport.get_extra_info.return_value = ("192.168.1.20", 50_000)
        for path, handler in ((INGRESS, adapter._handle_post), (SEND, adapter._handle_send)):
            with self.subTest(path=path):
                request = make_mocked_request("POST", path, headers={"X-Hermes-Secret": SECRET}, transport=transport)
                self.assertEqual(request.remote, "192.168.1.20")
                response = await handler(request)
                self.assertEqual((response.status, response.headers.get(DROPPED_HEADER)), (403, None))
        self.assertEqual(self.imsg_calls(), [])
        adapter.handle_message.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
