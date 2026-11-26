"""Registers the hook pair with Hermes, through the plugin context alone."""

from __future__ import annotations

from pathlib import Path

from gateway.platforms._shared import get_scoped_secret

from . import settings
from .asking import QuestionHooks
from .waiting import WaitingQuestions

HOOK_REQUEST = "on_human_input_request"
HOOK_RESOLVED = "on_human_input_resolved"


def register_hooks(ctx) -> None:
    """Register both hooks, reading the secret and the settings from the profile the plugin is enabled in.

    A session is labelled by its workspace folder's name, which says which of several
    sessions is asking without putting a path in a text.
    """
    waiting = WaitingQuestions()
    hooks = QuestionHooks(
        read_settings=lambda: settings.read_settings(ctx.get_config, get_scoped_secret(settings.ENV_SECRET, "")),
        session_label=lambda: Path(ctx.session_cwd()).name,
        waiting=waiting)
    ctx.register_hook(HOOK_REQUEST, hooks.on_human_input_request)
    ctx.register_hook(HOOK_RESOLVED, hooks.on_human_input_resolved)
    ctx.on_unload(waiting.forget_all)
