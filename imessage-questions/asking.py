"""The pair of hooks around a session's wait on a person.

`on_human_input_request` fires when a session stops to ask and `on_human_input_resolved`
when the wait ends. A question still unanswered after the saved delay is posted to the
texting plugin's listener. Only a question is: an approval's command or a password prompt
never leaves the Mac this way.

Nothing here logs a question, a session's label or the secret.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import listener_client
from .settings import AskingSettings, SettingsRefusedError
from .waiting import WaitingQuestions

KIND_QUESTION = "clarify"  # Hermes's name for a question the agent stops to ask
LOG_PREFIX = "[imessage_questions]"

logger = logging.getLogger(__name__)


class QuestionHooks:
    """Starts a question's wait when it is asked, and ends it when the wait is over."""

    def __init__(self, *, read_settings: Callable[[], AskingSettings], session_label: Callable[[], str],
                 waiting: WaitingQuestions,
                 post_question: Callable[[AskingSettings, str, str], None] = listener_client.post_question):
        """Take the collaborators Hermes supplies at registration.

        Args:
            read_settings: the settings as saved now; raises SettingsRefusedError.
            session_label: a label for the session whose hook is running, "" when it has none.
            waiting: the table of questions nobody has answered yet.
            post_question: posts one question to the listener; raises QuestionNotSentError.
        """
        self._post_question = post_question
        self._read_settings = read_settings
        self._session_label = session_label
        self._waiting = waiting

    def on_human_input_request(self, kind: str = "", request_id: str = "", prompt: str = "", platform: str = "",
                               **_: object) -> None:
        """A session is about to wait on a person: start the wait for a question.

        The settings and the label are read here, on the thread that carries the session's
        profile, and handed to the timer, which runs outside it.
        """
        if kind != KIND_QUESTION or not request_id or not prompt.strip():
            return
        try:
            settings = self._read_settings()
        except SettingsRefusedError as error:
            logger.error(f"{LOG_PREFIX} a question will not be texted: {error}")
            return
        label = self._session_label() or platform
        self._waiting.asked(request_id, settings.delay_seconds, lambda: self._text(settings, prompt, label))

    def on_human_input_resolved(self, request_id: str = "", **_: object) -> None:
        """The wait ended, however it ended: the question is no longer texted."""
        self._waiting.answered(request_id)

    def _text(self, settings: AskingSettings, question: str, label: str) -> None:
        """Post one unanswered question; a failure is logged, since nothing awaits a timer."""
        try:
            self._post_question(settings, question, label)
        except listener_client.QuestionNotSentError as error:
            logger.error(f"{LOG_PREFIX} a question was not texted: {error}")
            return
        logger.info(f"{LOG_PREFIX} texted a question nobody answered in {settings.delay_seconds:g} seconds")
