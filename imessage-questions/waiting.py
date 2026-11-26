"""The questions nobody has answered yet, each with the timer that will text it.

Hermes fires one hook when a session starts waiting on a person and one when the wait
ends. A question answered inside its delay is cancelled here and never leaves the Mac.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol


class Timer(Protocol):
    """What this module needs of `threading.Timer`."""

    daemon: bool

    def cancel(self) -> None: ...

    def start(self) -> None: ...


class WaitingQuestions:
    """One timer per unanswered question, by the request identifier Hermes gave it."""

    def __init__(self, timer_factory: Callable[[float, Callable[[], None]], Timer] = threading.Timer):
        self._lock = threading.Lock()
        self._timer_factory = timer_factory
        self._timers: dict[str, Timer] = {}

    def asked(self, request_id: str, delay_seconds: float, text_it: Callable[[], None]) -> None:
        """Start the wait for one question; *text_it* runs if nobody answers within *delay_seconds*.

        A repeat of a question already waiting keeps the first timer.
        """
        with self._lock:
            if request_id in self._timers:
                return
            timer = self._timer_factory(delay_seconds, lambda: self._fire(request_id, text_it))
            timer.daemon = True  # a waiting question never holds Hermes open at exit
            self._timers[request_id] = timer
        timer.start()

    def answered(self, request_id: str) -> None:
        """The wait for one question ended (answered, timed out or withdrawn): it is never texted."""
        with self._lock:
            timer = self._timers.pop(request_id, None)
        if timer is not None:
            timer.cancel()

    def forget_all(self) -> None:
        """Cancel every wait, as when the plugin is unloaded."""
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()

    def _fire(self, request_id: str, text_it: Callable[[], None]) -> None:
        """Text the question unless its wait ended first: cancelling cannot stop a timer already running."""
        with self._lock:
            still_waiting = self._timers.pop(request_id, None) is not None
        if still_waiting:
            text_it()
