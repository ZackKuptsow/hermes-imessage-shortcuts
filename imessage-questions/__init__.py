"""iMessage questions: a Hermes session's unanswered question, texted to its owner.

For the profile whose sessions ask (a coding profile), beside `imessage-shortcuts` in the
texting profile, which does the sending. Import-light on purpose: discovery imports this
package in every process, and the hooks load only when Hermes registers the plugin.
"""

from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:
    """Plugin entry point: the pair of hooks around every wait on a person."""
    from .registration import register_hooks

    register_hooks(ctx)
