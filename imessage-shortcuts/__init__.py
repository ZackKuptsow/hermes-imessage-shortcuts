"""iMessage (Shortcuts): Zack's texts to Hermes through the Mac Studio's Message automation.

Import-light on purpose: discovery imports this package and `tools.py` in every process;
the adapter, and aiohttp with it, load only when the gateway asks for the platform.
"""

from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:
    """Plugin entry point: the household_forward tool, then the platform."""
    from .registration import register_platform
    from .tools import register_tools

    register_tools(ctx)
    register_platform(ctx)
