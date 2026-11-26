"""One question, posted to the texting plugin's listener on the loopback address.

The request carries the question secret, so it goes straight to the listener and nowhere
else: no proxy from the environment or the system settings, and no redirect followed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http import HTTPStatus

from .settings import AskingSettings

IPV6_SEPARATOR = ":"
QUESTION_PATH = "/imessage/question"
# The listener answers once imsg has: up to 60 seconds for each 4,000 characters it sends.
REQUEST_TIMEOUT_SECONDS = 150
SECRET_HEADER = "X-Hermes-Secret"  # noqa: S105 -- a header name, not a secret


class QuestionNotSentError(Exception):
    """The listener did not text the question; the message holds a status or an error's type, never the question."""


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Answer every redirect with its own status as an error: urllib would resend the secret header."""

    def redirect_request(self, *_: object, **__: object) -> None:
        """Follow nothing: given no new request, urllib raises the redirect's status as an HTTPError."""


def question_url(host: str, port: int) -> str:
    """The question route's address; an IPv6 host goes in brackets."""
    bracketed = f"[{host}]" if IPV6_SEPARATOR in host else host
    return f"http://{bracketed}:{port}{QUESTION_PATH}"


def post_question(settings: AskingSettings, question: str, session: str) -> None:
    """Post one question for the listener to text.

    Args:
        settings: the listener's loopback address and the question secret.
        question: what the session asked.
        session: a short label for the session that asked; may be blank.

    Raises:
        QuestionNotSentError: the listener could not be reached, or answered anything but 202.
    """
    # The scheme is fixed in `question_url`, and the settings refuse any host off the loopback address.
    request = urllib.request.Request(
        question_url(settings.host, settings.port),
        data=json.dumps({"question": question, "session": session}).encode("utf-8"),
        headers={"Content-Type": "application/json", SECRET_HEADER: settings.secret},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseRedirects)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        raise QuestionNotSentError(f"the listener answered {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise QuestionNotSentError(f"the listener could not be reached ({type(error).__name__})") from error
    if status != HTTPStatus.ACCEPTED:
        raise QuestionNotSentError(f"the listener answered {status}")
