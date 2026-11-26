"""The text a waiting session's question becomes on its way to the owner's phone.

A Hermes session in another profile stops to ask something and nobody answers at the Mac;
that profile's `imessage-questions` plugin posts the question to this plugin's listener,
which texts it. The text says where to answer, because a reply by text goes to the texting
assistant like any other text and never to the session that asked.
"""

from __future__ import annotations

LEAD = "Hermes is waiting on your answer"
WHERE_TO_ANSWER = "Answer it at the Mac: a reply by text does not reach that session."


def compose_question_text(question: str, session: str) -> str:
    """The text for one question.

    Args:
        question: what the session asked, in its own lines.
        session: a short label for the session that asked (its workspace folder's name, or
            the surface it runs on); may be blank.

    Returns:
        A lead line naming the session, the question, then where to answer it.
    """
    label = " ".join(session.split())
    lead = f"{LEAD} in {label}:" if label else f"{LEAD}:"
    return f"{lead}\n{question.strip()}\n\n{WHERE_TO_ANSWER}"
