"""The two toolsets a text may reach (smart-home ADR 0035), checked on the profile's settings."""

from __future__ import annotations

import unittest

from support import plugin_module

allowed_toolsets = plugin_module("allowed_toolsets")
PLATFORM = "imessage_shortcuts"


def settings(toolsets: object, **other: object) -> dict:
    """A profile configuration whose saved list for the platform is *toolsets*."""
    return {"platform_toolsets": {PLATFORM: toolsets, "cli": ["terminal"]}, **other}


class CheckPlatformToolsetsTest(unittest.TestCase):
    def test_the_two_toolsets_or_fewer_are_accepted(self):
        for toolsets in (["imessage_shortcuts", "memory"], ["memory", "imessage_shortcuts"], ["memory"], []):
            with self.subTest(toolsets=toolsets):
                allowed_toolsets.check_platform_toolsets(settings(toolsets), PLATFORM)

    def test_other_platforms_lists_and_an_empty_mcp_section_are_accepted(self):
        allowed_toolsets.check_platform_toolsets(settings(["memory"], mcp_servers={}), PLATFORM)

    def test_each_toolset_that_reaches_an_app_or_the_network_is_refused_by_name(self):
        for widening in ("terminal", "file", "code_execution", "computer_use", "browser", "delegation", "cronjob",
                         "skills", "connections", "web", "hermes-imessage_shortcuts"):
            with self.subTest(widening=widening), self.assertRaises(allowed_toolsets.ToolsetsRefusedError) as raised:
                allowed_toolsets.check_platform_toolsets(settings(["imessage_shortcuts", "memory", widening]), PLATFORM)
            self.assertIn(widening, str(raised.exception))

    def test_no_saved_list_is_refused_since_hermes_would_give_every_core_toolset(self):
        for config in ({}, {"platform_toolsets": None}, {"platform_toolsets": {"cli": ["memory"]}},
                       settings("memory"), settings(None)):
            with self.subTest(config=config), self.assertRaises(allowed_toolsets.ToolsetsRefusedError):
                allowed_toolsets.check_platform_toolsets(config, PLATFORM)

    def test_a_configured_mcp_server_is_refused_since_hermes_merges_it_in(self):
        config = settings(["memory"], mcp_servers={"notes": {"command": "notes-server"}})
        with self.assertRaises(allowed_toolsets.ToolsetsRefusedError) as raised:
            allowed_toolsets.check_platform_toolsets(config, PLATFORM)
        self.assertIn("mcp_servers", str(raised.exception))
        self.assertNotIn("notes-server", str(raised.exception))

    def test_a_configuration_that_is_not_a_mapping_is_refused(self):
        for config in (None, [], "platform_toolsets"):
            with self.subTest(config=config), self.assertRaises(allowed_toolsets.ToolsetsRefusedError):
                allowed_toolsets.check_platform_toolsets(config, PLATFORM)


if __name__ == "__main__":
    unittest.main()
