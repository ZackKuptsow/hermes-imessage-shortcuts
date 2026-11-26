"""`imessage-questions`: when a session's question is texted, and when it is not.

Runs under any Python: the settings, the wait and the hook pair take their collaborators as
arguments, so nothing here needs Hermes, a listener or a clock.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable

from support import questions_plugin_module

asking = questions_plugin_module("asking")
listener_client = questions_plugin_module("listener_client")
settings_module = questions_plugin_module("settings")
waiting_module = questions_plugin_module("waiting")

QUESTION = "Which branch should the fix land on?"
SECRET = "q" * 40


def saved(**values: object) -> Callable[[str, object], object]:
    """Stands in for Hermes's `ctx.get_config`: the saved value, else the default."""
    return lambda key, default=None: values.get(key, default)


class FakeTimer:
    """Stands in for `threading.Timer`: it records, and the test decides when it fires."""

    def __init__(self, interval: float, function: Callable[[], None]):
        self.cancelled = False
        self.daemon = False
        self.function = function
        self.interval = interval
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class FakeTimers:
    """A timer factory that keeps every timer it made."""

    def __init__(self):
        self.made: list[FakeTimer] = []

    def __call__(self, interval: float, function: Callable[[], None]) -> FakeTimer:
        self.made.append(FakeTimer(interval, function))
        return self.made[-1]


class ReadSettingsTest(unittest.TestCase):
    def test_with_nothing_saved_the_defaults_wait_two_minutes_and_post_to_the_plugins_port(self):
        read = settings_module.read_settings(saved(), SECRET)
        self.assertEqual((read.delay_seconds, read.host, read.port, read.secret), (120, "127.0.0.1", 8645, SECRET))

    def test_saved_settings_are_taken(self):
        read = settings_module.read_settings(saved(delay_seconds=0, host="::1", port=9000), f"  {SECRET}\n")
        self.assertEqual((read.delay_seconds, read.host, read.port, read.secret), (0, "::1", 9000, SECRET))
        self.assertEqual(settings_module.read_settings(saved(delay_seconds=45.5), SECRET).delay_seconds, 45.5)

    def test_refuses_each_setting_it_cannot_serve_with(self):
        cases = {
            "delay_seconds": (-1, "soon", True, float("inf"), float("nan"), None),
            "host": ("0.0.0.0", "localhost", "192.168.1.20", 5, ""),  # noqa: S104 -- refused on purpose
            "port": (0, 70000, "8645", True, 8645.0),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(settings_module.SettingsRefusedError):
                    settings_module.read_settings(saved(**{name: value}), SECRET)

    def test_refuses_a_missing_or_short_secret_without_quoting_it(self):
        for secret in ("", "   ", "short-secret"):
            with self.subTest(secret=secret), self.assertRaises(settings_module.SettingsRefusedError) as refused:
                settings_module.read_settings(saved(), secret)
            self.assertIn("IMESSAGE_QUESTIONS_SECRET", str(refused.exception))
            self.assertNotIn("short-secret", str(refused.exception))

    def test_the_settings_never_show_the_secret(self):
        self.assertNotIn(SECRET, repr(settings_module.read_settings(saved(), SECRET)))


class WaitingQuestionsTest(unittest.TestCase):
    def setUp(self):
        self.timers = FakeTimers()
        self.waiting = waiting_module.WaitingQuestions(timer_factory=self.timers)
        self.texted: list[str] = []

    def ask(self, request_id: str = "request-1", delay_seconds: float = 120) -> FakeTimer:
        self.waiting.asked(request_id, delay_seconds, lambda: self.texted.append(request_id))
        return self.timers.made[-1]

    def test_a_question_starts_a_background_timer_for_its_delay(self):
        timer = self.ask(delay_seconds=45)
        self.assertEqual((timer.interval, timer.started, timer.daemon), (45, True, True))
        self.assertEqual(self.texted, [])

    def test_a_question_nobody_answers_is_texted_once_when_its_timer_fires(self):
        timer = self.ask()
        timer.function()
        timer.function()  # a timer cannot fire twice, but a second call must not text twice
        self.assertEqual(self.texted, ["request-1"])

    def test_a_question_answered_in_time_is_never_texted(self):
        timer = self.ask()
        self.waiting.answered("request-1")
        self.assertTrue(timer.cancelled)
        timer.function()  # the timer had already fired when the answer cancelled it
        self.assertEqual(self.texted, [])

    def test_an_answer_to_a_question_that_is_not_waiting_changes_nothing(self):
        timer = self.ask()
        self.waiting.answered("another-request")
        self.assertFalse(timer.cancelled)
        timer.function()
        self.assertEqual(self.texted, ["request-1"])

    def test_each_question_waits_on_its_own(self):
        first, second = self.ask("request-1"), self.ask("request-2")
        self.waiting.answered("request-1")
        second.function()
        self.assertEqual((first.cancelled, self.texted), (True, ["request-2"]))

    def test_a_repeat_of_a_waiting_question_keeps_the_first_timer(self):
        self.ask()
        self.ask()
        self.assertEqual(len(self.timers.made), 1)

    def test_forgetting_everything_cancels_every_timer(self):
        first, second = self.ask("request-1"), self.ask("request-2")
        self.waiting.forget_all()
        first.function()
        second.function()
        self.assertEqual((first.cancelled, second.cancelled, self.texted), (True, True, []))


class QuestionHooksTest(unittest.TestCase):
    def setUp(self):
        self.timers = FakeTimers()
        self.posted: list[tuple[object, str, str]] = []
        self.post_raises: Exception | None = None
        self.saved_settings: dict = {"delay_seconds": 30}
        self.secret = SECRET
        self.session_folder = "dotfiles"
        self.hooks = asking.QuestionHooks(
            post_question=self.post_question,
            read_settings=lambda: settings_module.read_settings(saved(**self.saved_settings), self.secret),
            session_label=lambda: self.session_folder,
            waiting=waiting_module.WaitingQuestions(timer_factory=self.timers))

    def post_question(self, settings: object, question: str, session: str) -> None:
        self.posted.append((settings, question, session))
        if self.post_raises is not None:
            raise self.post_raises

    def request(self, **overrides: object) -> None:
        self.hooks.on_human_input_request(**{
            "kind": "clarify", "platform": "cli", "prompt": QUESTION, "request_id": "request-1",
            "session_id": "20261125_101500_a1b2c3", "session_key": "agent:main:cli", **overrides})

    def test_a_question_waits_the_saved_delay_then_is_posted_with_its_sessions_label(self):
        self.request()
        self.assertEqual((len(self.timers.made), self.timers.made[0].interval, self.posted), (1, 30, []))
        self.timers.made[0].function()
        ((settings, question, session),) = self.posted
        self.assertEqual((settings.port, settings.secret, question, session), (8645, SECRET, QUESTION, "dotfiles"))

    def test_a_question_answered_before_the_delay_is_never_posted(self):
        self.request()
        self.hooks.on_human_input_resolved(kind="clarify", outcome="submitted", request_id="request-1")
        self.timers.made[0].function()
        self.assertEqual(self.posted, [])

    def test_only_a_question_is_texted_never_an_approval_or_a_password_prompt(self):
        for kind in ("approval", "sudo", "", "secret"):
            with self.subTest(kind=kind):
                self.request(kind=kind, prompt="rm -rf build")
        self.assertEqual(self.timers.made, [])

    def test_a_request_with_no_words_or_no_identifier_is_not_texted(self):
        self.request(prompt="  \n")
        self.request(request_id="")
        self.assertEqual(self.timers.made, [])

    def test_a_session_with_no_folder_is_labelled_by_where_it_runs(self):
        self.session_folder = ""
        self.request(platform="desktop")
        self.timers.made[0].function()
        self.assertEqual(self.posted[0][2], "desktop")

    def test_the_settings_are_read_when_the_question_is_asked_so_a_change_needs_no_restart(self):
        self.saved_settings = {"delay_seconds": 5, "port": 9000}
        self.request()
        self.timers.made[0].function()
        self.assertEqual((self.timers.made[0].interval, self.posted[0][0].port), (5, 9000))

    def test_settings_it_cannot_serve_with_are_logged_and_nothing_waits(self):
        self.secret = "short-secret"
        with self.assertLogs("imessage_questions", level="ERROR") as logged:
            self.request()
        self.assertEqual(self.timers.made, [])
        self.assertIn("IMESSAGE_QUESTIONS_SECRET", logged.output[0])
        self.assertNotIn("short-secret", logged.output[0])

    def test_a_question_the_listener_did_not_send_is_logged_and_nothing_raises(self):
        self.post_raises = listener_client.QuestionNotSentError("the listener answered 502")
        self.request()
        with self.assertLogs("imessage_questions", level="ERROR") as logged:
            self.timers.made[0].function()
        self.assertIn("the listener answered 502", logged.output[0])

    def test_the_log_never_holds_the_question_the_label_or_the_secret(self):
        with self.assertLogs("imessage_questions", level="DEBUG") as logged:
            self.request()
            self.timers.made[0].function()
            self.post_raises = listener_client.QuestionNotSentError("the listener answered 401")
            self.request(request_id="request-2")
            self.timers.made[1].function()
        self.assertEqual(len(self.posted), 2)  # both paths that log were taken
        for line in logged.output:
            for kept_out in (QUESTION, "dotfiles", SECRET):
                with self.subTest(line=line):
                    self.assertNotIn(kept_out, line)

    def test_the_hooks_take_fields_a_later_hermes_adds(self):
        self.request(execution_generation=3, added_later="value")
        self.hooks.on_human_input_resolved(request_id="request-1", outcome="submitted", added_later="value")
        self.assertTrue(self.timers.made[0].cancelled)


if __name__ == "__main__":
    unittest.main()
