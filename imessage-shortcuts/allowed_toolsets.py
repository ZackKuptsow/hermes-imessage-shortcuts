"""The toolsets a text may reach: the plugin's household_forward and the profile's memory.

Ghostty holds Full Disk Access on the Mac Studio, and macOS runs a launched app with that
app's own grants, so any tool that can run a command, click, or write a file another
process runs reaches the partner's texts in Messages' database. A text therefore reaches
two toolsets and no others (smart-home ADR 0035).

This module checks the three settings a text's reach is built from: the platform's saved
list (with none, Hermes falls back to a composite of every core toolset), the MCP servers
(Hermes merges each into every platform's toolsets), and the memory provider (an external
one brings its own tools in on the `memory` toolset). It imports nothing from Hermes, so it
runs under any python3. The adapter adds what only Hermes can answer: the toolsets Hermes's
own resolver turns on for the platform, which can include ones no setting names.
"""

from __future__ import annotations

ALLOWED_TOOLSETS = frozenset({"imessage_shortcuts", "memory"})
# memory.provider values meaning Hermes's built-in store (Hermes agent/memory_provider.py,
# CORE_MEMORY_PROVIDER_SENTINELS; a test under Hermes keeps the two equal).
CORE_MEMORY_PROVIDERS = frozenset({"", "built-in", "builtin", "default", "none"})
MCP_SERVERS_KEY = "mcp_servers"
MEMORY_KEY = "memory"
MEMORY_PROVIDER_KEY = "provider"
PLATFORM_TOOLSETS_KEY = "platform_toolsets"
REFUSAL_HINT = "(smart-home ADR 0035; README, \"What it refuses\")"


class ToolsetsRefusedError(ValueError):
    """The profile's configuration gives a text more than the two allowed toolsets."""


def check_platform_toolsets(config: object, platform: str) -> None:
    """Refuse a configuration that lets a text reach more than ALLOWED_TOOLSETS.

    Args:
        config: the profile's configuration, as Hermes reads it from its config.yaml.
        platform: the platform's name, the key under platform_toolsets.

    Raises:
        ToolsetsRefusedError: the configuration is not a mapping, configures an MCP server
            or an external memory provider, has no saved list for the platform, saves
            something other than a list there, or the list is not exactly ALLOWED_TOOLSETS.
            The message names toolsets and settings, never a value.
    """
    if not isinstance(config, dict):
        raise ToolsetsRefusedError("the profile's config.yaml is not a mapping of settings")
    if config.get(MCP_SERVERS_KEY):
        raise ToolsetsRefusedError(
            f"the profile configures {MCP_SERVERS_KEY}, which Hermes merges into every platform's toolsets: "
            f"remove them {REFUSAL_HINT}")
    _check_memory_provider(config.get(MEMORY_KEY))
    _check_saved_list(config.get(PLATFORM_TOOLSETS_KEY), platform)


def _check_memory_provider(memory: object) -> None:
    """Refuse an external memory provider, whose tools ride in on the `memory` toolset."""
    provider = memory.get(MEMORY_PROVIDER_KEY) if isinstance(memory, dict) else None
    if str(provider or "").strip().lower() not in CORE_MEMORY_PROVIDERS:
        raise ToolsetsRefusedError(
            f"the profile's {MEMORY_KEY}.{MEMORY_PROVIDER_KEY} is an external provider, whose tools come in "
            f"with the {MEMORY_KEY} toolset: use the built-in store {REFUSAL_HINT}")


def _check_saved_list(saved_lists: object, platform: str) -> None:
    """Refuse unless the platform's saved list names exactly ALLOWED_TOOLSETS."""
    key = f"{PLATFORM_TOOLSETS_KEY}.{platform}"
    saved = saved_lists.get(platform) if isinstance(saved_lists, dict) else None
    if saved is None:
        raise ToolsetsRefusedError(
            f"{key} has no saved list, so Hermes would give a text every core toolset: save the list {REFUSAL_HINT}")
    if not isinstance(saved, list):
        raise ToolsetsRefusedError(
            f"{key} is a {type(saved).__name__}, not a list: save it as a YAML list {REFUSAL_HINT}")
    not_names = sorted({type(entry).__name__ for entry in saved if not isinstance(entry, str)})
    if not_names:
        raise ToolsetsRefusedError(
            f"{key} holds a {', a '.join(not_names)} where a toolset's name belongs {REFUSAL_HINT}")
    named = set(saved)
    extra = sorted(named - ALLOWED_TOOLSETS)
    if extra:
        raise ToolsetsRefusedError(
            f"{key} names {', '.join(extra)}; a text may reach {' and '.join(sorted(ALLOWED_TOOLSETS))} alone: "
            f"fix the saved list {REFUSAL_HINT}")
    missing = sorted(ALLOWED_TOOLSETS - named)
    if missing:
        raise ToolsetsRefusedError(
            f"{key} leaves out {', '.join(missing)}: save exactly {' and '.join(sorted(ALLOWED_TOOLSETS))} "
            f"{REFUSAL_HINT}")
