"""Registers the iMessage (Shortcuts) platform with the gateway, through public seams only."""

from __future__ import annotations

import importlib.util

from gateway.platforms._shared import get_scoped_secret, seed_extra_from_env

from . import adapter

# Cached-prompt bytes: keep stable.
PLATFORM_HINT = (
    "You are talking with Zack over iMessage, through the household assistant's Apple Account on "
    "the Mac Studio. Messages shows Markdown as literal characters, so write plain text. Keep replies "
    "short; a long one arrives as several texts. When he sends a household command (the shared "
    "lists, reminders, chores, OTIS, STOP, help), pass his words unchanged to the household_forward "
    "tool: the household assistant's answer goes straight to his phone, and you learn only whether "
    "it was delivered."
)


def check_requirements() -> bool:
    """Passive probe: aiohttp importable and the secret set. Never installs anything."""
    secret = get_scoped_secret(adapter.ENV_SECRET, "").strip()
    return importlib.util.find_spec("aiohttp") is not None and bool(secret)


def env_enablement() -> dict | None:
    """Enable the platform when its secret is set; the adapter checks everything else at start."""
    if not get_scoped_secret(adapter.ENV_SECRET, "").strip():
        return None
    seeded = seed_extra_from_env(((adapter.ENV_HOST, "host", None), (adapter.ENV_PORT, "port", None)))
    return {"host": adapter.DEFAULT_HOST, "port": str(adapter.DEFAULT_PORT), **seeded}


def register_platform(ctx) -> None:
    """Register the adapter and its authorization names.

    An unknown sender is ignored, never sent a pairing code: the code would be a text to
    someone who is not Zack.
    """
    ctx.register_platform(
        adapter_factory=adapter.ImessageShortcutsAdapter,
        allow_all_env=adapter.ENV_ALLOW_ALL,
        allow_update_command=False,
        allowed_users_env=adapter.ENV_ALLOWED_USERS,
        allowlist_suppresses_pairing=True,
        check_fn=check_requirements,
        emoji="\U0001f4ac",  # speech balloon
        env_enablement_fn=env_enablement,
        install_hint="See https://github.com/ZackKuptsow/hermes-imessage-shortcuts#install",
        label="iMessage (Shortcuts)",
        max_message_length=adapter.MAX_MESSAGE_LENGTH,
        name=adapter.PLATFORM_NAME,
        pii_safe=True,
        platform_hint=PLATFORM_HINT,
        required_env=[adapter.ENV_SECRET, adapter.ENV_ALLOWED_USERS],
    )
