"""iMessage through the Mac Studio's Shortcuts automation, as a Hermes platform.

Zack's Message automation posts each of his texts to a listener on the loopback address;
Hermes answers in a session per handle, and imsg sends the answer back to him from the
assistant's account (smart-home AHS-10, stage two; household-assistant ADR 0077).

Three walls keep anyone else out, any one of which is enough:

- the automation fires only for Zack's handle;
- this adapter drops every sender not on the allow-list file before the gateway sees the
  message, and refuses to start when the file and the gateway's allow list disagree;
- `send` refuses any handle not on the file, so not even a pairing code goes elsewhere.

It also refuses to start in any Hermes profile but `imessage`, the texting profile on the
Mac's Ollama (smart-home ADR 0034). Enabled in the default profile by hand, it stays off
rather than let that profile's cloud model answer Zack's texts.

Nothing here logs a text, a handle or the secret.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web
from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, send_chunks, strip_markdown
from hermes_constants import get_hermes_home

from . import handles, imsg, inbound

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8645
ENV_ALLOW_ALL = "IMESSAGE_SHORTCUTS_ALLOW_ALL_USERS"
ENV_ALLOWED_USERS = "IMESSAGE_SHORTCUTS_ALLOWED_USERS"
ENV_HOST = "IMESSAGE_SHORTCUTS_HOST"
ENV_IMSG = "IMESSAGE_SHORTCUTS_IMSG"
ENV_PORT = "IMESSAGE_SHORTCUTS_PORT"
ENV_SECRET = "IMESSAGE_SHORTCUTS_SECRET"  # noqa: S105 -- an env name, not a secret
INGRESS_PATH = "/imessage"
LOG_PREFIX = "[imessage_shortcuts]"
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})
MAX_MESSAGE_LENGTH = 4000
PLATFORM_NAME = "imessage_shortcuts"
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})

logger = logging.getLogger(__name__)


class StartupRefusedError(Exception):
    """A configuration this adapter will not serve; the message says which part."""


def allow_list_path() -> Path:
    """The allow-list file, shared with the stage-one webhook route's filter."""
    return get_hermes_home() / handles.ALLOW_LIST_RELATIVE_PATH


def check_texting_profile() -> None:
    """Refuse unless Hermes is serving the texting profile, `<root>/profiles/imessage`.

    In a profile the gateway serves, Hermes's home is that profile's own directory, set for
    `connect` and every reconnect; the default profile's home is the root itself.

    Raises:
        StartupRefusedError: the serving profile is not the texting profile.
    """
    home = get_hermes_home()
    if (home.parent.name, home.name) != (handles.PROFILES_DIRECTORY, handles.TEXTING_PROFILE):
        raise StartupRefusedError(
            f"this platform serves the {handles.TEXTING_PROFILE} profile alone (smart-home ADR 0034), "
            f"not the one at {home}: disable the plugin there and enable it in the texting profile (README, \"Install\")")


def read_startup_settings() -> tuple[str, int, str, tuple[str, ...], Path]:
    """Check the configuration and return the host, port, secret, allow list and imsg path.

    Raises:
        StartupRefusedError: the profile is not the texting profile, the host is not a
            loopback address, the secret is short, the allow-all switch is on, the allow
            lists disagree, or imsg is missing.
    """
    check_texting_profile()
    host = get_scoped_secret(ENV_HOST, DEFAULT_HOST).strip()
    if host not in LOOPBACK_ADDRESSES:
        raise StartupRefusedError(f"{ENV_HOST} must be a loopback address, 127.0.0.1 or ::1 (smart-home ADR 0002)")
    port_text = get_scoped_secret(ENV_PORT, str(DEFAULT_PORT)).strip()
    if not port_text.isdigit():
        raise StartupRefusedError(f"{ENV_PORT} is not a port number")
    secret = get_scoped_secret(ENV_SECRET, "").strip()
    if len(secret) < inbound.SECRET_MINIMUM_LENGTH:
        raise StartupRefusedError(f"{ENV_SECRET} is shorter than {inbound.SECRET_MINIMUM_LENGTH} characters")
    if get_scoped_secret(ENV_ALLOW_ALL, "").strip().lower() in TRUTHY_VALUES:
        raise StartupRefusedError(f"{ENV_ALLOW_ALL} is on; this platform serves the allow list alone")
    try:
        allowed = handles.read_allowed_handles(allow_list_path())
        gateway_allowed = {handles.canonical_handle(entry)
                           for entry in get_scoped_secret(ENV_ALLOWED_USERS, "").split(",") if entry.strip()}
    except (handles.AllowListError, handles.InvalidHandleError) as error:
        raise StartupRefusedError(f"the allow list cannot be read: {error}") from error
    if gateway_allowed != set(allowed):
        raise StartupRefusedError(f"{ENV_ALLOWED_USERS} and {allow_list_path()} must list the same handles")
    try:
        imsg_path = imsg.find_imsg(get_scoped_secret(ENV_IMSG, "").strip() or None)
    except imsg.ImsgMissingError as error:
        raise StartupRefusedError(str(error)) from error
    return host, int(port_text), secret, allowed, imsg_path


class ImessageShortcutsAdapter(BasePlatformAdapter):
    """Zack's texts in from the Message automation; Hermes's answers out through imsg."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        self._allowed: tuple[str, ...] = ()
        self._deduplicator = MessageDeduplicator()
        self._imsg_path: Path | None = None
        self._runner: web.AppRunner | None = None
        self._secret = ""

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Check the configuration, then listen on the loopback address."""
        try:
            host, port, self._secret, self._allowed, self._imsg_path = read_startup_settings()
        except StartupRefusedError as error:
            logger.error(f"{LOG_PREFIX} not starting: {error}")
            self._set_fatal_error("imessage_shortcuts_refused", str(error), retryable=False)
            return False
        application = web.Application(client_max_size=inbound.MAXIMUM_BODY_BYTES)
        application.router.add_post(INGRESS_PATH, self._handle_post)
        self._runner = web.AppRunner(application, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, host, port).start()
        except OSError as error:
            await self._runner.cleanup()
            self._runner = None
            logger.error(f"{LOG_PREFIX} cannot listen on {host}:{port}: {error.strerror}")
            self._set_fatal_error("imessage_shortcuts_bind", f"cannot listen on {host}:{port}", retryable=True)
            return False
        self._running = True
        self._mark_connected()
        logger.info(f"{LOG_PREFIX} listening on {host}:{port}{INGRESS_PATH} for {len(self._allowed)} handle(s)")
        return True

    async def disconnect(self) -> None:
        """Stop listening."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._running = False
        self._mark_disconnected()

    async def _handle_post(self, request: web.Request) -> web.Response:
        """Take one posted text: 401 without the secret, 400 when malformed, 204 when dropped, 202 when taken."""
        if request.remote not in LOOPBACK_ADDRESSES:
            return web.Response(status=403)
        if not inbound.secret_matches(self._secret, request.headers.get(inbound.SECRET_HEADER)):
            logger.warning(f"{LOG_PREFIX} refused a request without the right secret")
            return web.Response(status=401)
        try:
            posted = inbound.parse_posted_message(await request.read())
        except inbound.MalformedMessageError as error:
            logger.warning(f"{LOG_PREFIX} refused a request: {error}")
            return web.Response(status=400)
        try:
            sender = handles.canonical_handle(posted.sender)
        except handles.InvalidHandleError:
            sender = ""
        if sender not in self._allowed:
            logger.warning(f"{LOG_PREFIX} dropped a text from a sender not on the allow list")
            return web.Response(status=204)
        if not posted.text.strip() or self._deduplicator.is_duplicate(posted.key()):
            return web.Response(status=204)
        message_id = posted.key()[:16]
        source = self.build_source(chat_id=sender, chat_name="iMessage", chat_type="dm", user_id=sender,
                                   user_name="Zack", message_id=message_id)
        event = MessageEvent(text=posted.text, message_type=MessageType.TEXT, source=source, message_id=message_id)
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.Response(status=202)

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict | None = None) -> SendResult:
        """Send *content* to *chat_id* through imsg, refusing any handle off the allow list."""
        imsg_path = self._imsg_path
        if imsg_path is None:
            return SendResult(success=False, error="the platform is not connected")

        async def send_one(chunk: str) -> SendResult:
            try:
                outcome = await asyncio.to_thread(imsg.send_message, chunk, chat_id, self._allowed, imsg_path)
            except imsg.SendRefusedError as error:
                logger.error(f"{LOG_PREFIX} {error}")
                return SendResult(success=False, error=str(error))
            if outcome.delivered:
                return SendResult(success=True)
            logger.error(f"{LOG_PREFIX} imsg did not send: {outcome.disposition}")
            return SendResult(success=False, error=f"imsg did not send: {outcome.disposition}")

        chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
        return await send_chunks(chunks, send_one)

    async def get_chat_info(self, chat_id: str) -> dict:
        """iMessage chats here are one person's direct thread."""
        return {"name": "iMessage", "type": "dm", "chat_id": chat_id}

    def format_message(self, content: str) -> str:
        """Plain text: Messages shows Markdown as literal characters."""
        return strip_markdown(content)
