"""The two toolsets a text may reach (smart-home ADR 0035), checked on the profile's settings."""

from __future__ import annotations

import unittest

from support import plugin_module

allowed_toolsets = plugin_module("allowed_toolsets")
PLATFORM = "imessage_shortcuts"
THE_TWO = ["imessage_shortcuts", "memory"]


def settings(toolsets: object, **other: object) -> dict:
    """A profile configuration whose saved list for the platform is *toolsets*."""
    return {"platform_toolsets": {PLATFORM: toolsets, "cli": ["terminal"]}, **other}


def refusal(config: object) -> str:
    """The refusal's message for *config*; fails the test when it is accepted."""
    try:
        allowed_toolsets.check_platform_toolsets(config, PLATFORM)
    except allowed_toolsets.ToolsetsRefusedError as error:
        return str(error)
    raise AssertionError(f"accepted: {config!r}")


class CheckPlatformToolsetsTest(unittest.TestCase):
    def test_exactly_the_two_toolsets_are_accepted_in_either_order(self):
        for toolsets in (THE_TWO, list(reversed(THE_TWO))):
            with self.subTest(toolsets=toolsets):
                allowed_toolsets.check_platform_toolsets(settings(toolsets), PLATFORM)

    def test_a_list_without_both_is_refused_since_an_empty_one_takes_hermess_composite_path(self):
        for toolsets in ([], ["memory"], ["imessage_shortcuts"]):
            with self.subTest(toolsets=toolsets):
                self.assertIn("leaves out", refusal(settings(toolsets)))

    def test_other_platforms_lists_and_an_empty_mcp_section_are_accepted(self):
        allowed_toolsets.check_platform_toolsets(settings(THE_TWO, mcp_servers={}), PLATFORM)

    def test_each_toolset_that_reaches_an_app_or_the_network_is_refused_by_name(self):
        for widening in ("terminal", "file", "code_execution", "computer_use", "browser", "delegation", "cronjob",
                         "skills", "connections", "web", "hermes-imessage_shortcuts"):
            with self.subTest(widening=widening):
                self.assertIn(widening, refusal(settings([*THE_TWO, widening])))

    def test_no_saved_list_is_refused_since_hermes_would_give_every_core_toolset(self):
        for config in ({}, {"platform_toolsets": None}, {"platform_toolsets": {"cli": ["memory"]}}, settings(None)):
            with self.subTest(config=config):
                self.assertIn("has no saved list", refusal(config))

    def test_a_saved_value_that_is_not_a_list_is_refused_as_one(self):
        for value in ("", {}, {"memory": True}, "memory", '["imessage_shortcuts", "memory"]'):
            with self.subTest(value=value):
                self.assertIn(f"is a {type(value).__name__}, not a list", refusal(settings(value)))

    def test_an_entry_that_is_not_a_name_is_refused_by_its_type_never_its_value(self):
        message = refusal(settings([*THE_TWO, {"token": "do-not-print"}]))
        self.assertIn("a dict", message)
        self.assertNotIn("do-not-print", message)

    def test_a_configured_mcp_server_is_refused_since_hermes_merges_it_in(self):
        message = refusal(settings(THE_TWO, mcp_servers={"notes": {"command": "notes-server"}}))
        self.assertIn("mcp_servers", message)
        self.assertNotIn("notes-server", message)

    def test_an_external_memory_provider_is_refused_since_its_tools_ride_in_on_memory(self):
        message = refusal(settings(THE_TWO, memory={"provider": "retaindb"}))
        self.assertIn("memory.provider", message)
        self.assertNotIn("retaindb", message)

    def test_the_built_in_memory_store_is_accepted_however_it_is_named(self):
        for memory in ({}, {"provider": ""}, {"provider": None}, {"provider": "builtin"}, {"provider": " Built-In "},
                       {"provider": "default"}, {"provider": "none"}, "not a mapping"):
            with self.subTest(memory=memory):
                allowed_toolsets.check_platform_toolsets(settings(THE_TWO, memory=memory), PLATFORM)

    def test_a_configuration_that_is_not_a_mapping_is_refused(self):
        for config in (None, [], "platform_toolsets"):
            with self.subTest(config=config):
                self.assertIn("not a mapping", refusal(config))


if __name__ == "__main__":
    unittest.main()
