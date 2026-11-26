"""What the plugin serves with: the listener's address, its secret, and how long to wait.

The secret comes from the profile's `.env`; the rest from the plugin's settings in the
profile's `config.yaml` (`plugins.entries.imessage-questions.settings`), each with a default.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_DELAY_SECONDS = 120  # long enough to read a question at the Mac before the phone buzzes
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8645  # imessage-shortcuts's default
ENV_SECRET = "IMESSAGE_QUESTIONS_SECRET"  # noqa: S105 -- an env name, not a secret
HIGHEST_PORT = 65_535
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})
SECRET_MINIMUM_LENGTH = 32  # the listener's own minimum
SETTING_DELAY_SECONDS = "delay_seconds"
SETTING_HOST = "host"
SETTING_PORT = "port"


class SettingsRefusedError(ValueError):
    """A setting this plugin will not serve with; the message names it, never a secret's value."""


@dataclass(frozen=True)
class AskingSettings:
    """Where a question is posted, and after how long unanswered."""

    delay_seconds: float
    host: str
    port: int
    secret: str = field(repr=False)


def read_settings(read_setting: Callable[[str, object], object], secret: str) -> AskingSettings:
    """Check the saved settings and the secret, and return what the plugin serves with.

    Args:
        read_setting: Hermes's `ctx.get_config`: a saved plugin setting, else the default given.
        secret: the profile's value of `IMESSAGE_QUESTIONS_SECRET`, "" when unset.

    Raises:
        SettingsRefusedError: the secret is shorter than the listener takes, the host is not
            a loopback address (the secret would leave the machine), the port is not a port
            number, or the delay is not a number of seconds from zero up.
    """
    stripped_secret = secret.strip()
    if len(stripped_secret) < SECRET_MINIMUM_LENGTH:
        raise SettingsRefusedError(f"{ENV_SECRET} is unset or shorter than {SECRET_MINIMUM_LENGTH} characters")
    host = read_setting(SETTING_HOST, DEFAULT_HOST)
    if host not in LOOPBACK_ADDRESSES:
        raise SettingsRefusedError(f"the setting {SETTING_HOST!r} must be a loopback address, 127.0.0.1 or ::1")
    port = read_setting(SETTING_PORT, DEFAULT_PORT)
    if type(port) is not int or not 0 < port <= HIGHEST_PORT:
        raise SettingsRefusedError(f"the setting {SETTING_PORT!r} is not a port number")
    delay_seconds = read_setting(SETTING_DELAY_SECONDS, DEFAULT_DELAY_SECONDS)
    if type(delay_seconds) not in (int, float) or not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise SettingsRefusedError(f"the setting {SETTING_DELAY_SECONDS!r} is not a number of seconds from 0 up")
    return AskingSettings(delay_seconds=delay_seconds, host=host, port=port, secret=stripped_secret)
