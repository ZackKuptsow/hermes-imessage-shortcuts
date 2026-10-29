"""The toolsets a text may reach: the plugin's household_forward and the profile's memory.

Ghostty holds Full Disk Access on the Mac Studio, and macOS runs a launched app with that
app's own grants, so any tool that can run a command, click, or write a file another
process runs reaches the partner's texts in Messages' database. A text therefore reaches
two toolsets and no others (smart-home ADR 0035). Hermes resolves a platform's toolsets
from the profile's saved list for it, and merges in every MCP server the profile
configures; with no saved list it falls back to a composite of every core toolset. So
the adapter refuses to start unless the saved list exists and names nothing else, and
the profile configures no MCP server.
"""

from __future__ import annotations

ALLOWED_TOOLSETS = frozenset({"imessage_shortcuts", "memory"})
MCP_SERVERS_KEY = "mcp_servers"
PLATFORM_TOOLSETS_KEY = "platform_toolsets"


class ToolsetsRefusedError(ValueError):
    """The profile's configuration gives a text more than the two allowed toolsets."""


def check_platform_toolsets(config: object, platform: str) -> None:
    """Refuse a configuration that lets a text reach more than ALLOWED_TOOLSETS.

    Args:
        config: the profile's configuration, as parsed from its config.yaml.
        platform: the platform's name, the key under platform_toolsets.

    Raises:
        ToolsetsRefusedError: the configuration is not a mapping, configures an MCP
            server, has no saved list for the platform, or the list names a toolset
            outside ALLOWED_TOOLSETS. The message names the toolsets, never a value.
    """
    if not isinstance(config, dict):
        raise ToolsetsRefusedError("the profile's config.yaml is not a mapping of settings")
    if config.get(MCP_SERVERS_KEY):
        raise ToolsetsRefusedError(
            f"the profile configures {MCP_SERVERS_KEY}, which Hermes merges into every platform's toolsets "
            "(smart-home ADR 0035): remove them")
    saved_lists = config.get(PLATFORM_TOOLSETS_KEY)
    saved = saved_lists.get(platform) if isinstance(saved_lists, dict) else None
    if not isinstance(saved, list):
        raise ToolsetsRefusedError(
            f"{PLATFORM_TOOLSETS_KEY}.{platform} is not a saved list, so Hermes would give a text every core "
            "toolset (smart-home ADR 0035): save the list (README, \"What it refuses\")")
    extra = sorted({str(name) for name in saved} - ALLOWED_TOOLSETS)
    if extra:
        raise ToolsetsRefusedError(
            f"{PLATFORM_TOOLSETS_KEY}.{platform} names {', '.join(extra)}; a text may reach "
            f"{' and '.join(sorted(ALLOWED_TOOLSETS))} alone (smart-home ADR 0035): fix the saved list (README, \"What it refuses\")")
