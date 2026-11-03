"""The n8n LaunchDaemon's plist: every switch the design names, nothing it forbids, no value.

The plist is readable by every user on the Mac and published on GitHub, so this suite is the
gate between a changed switch and an n8n that listens on the LAN or phones home. Each rule is
proven by a test that breaks a copy of the plist and expects that rule, and only it, to fire.
"""

from __future__ import annotations

import copy
import plistlib
import re
import unittest
from pathlib import Path

from support import ZACK_EMAIL, ZACK_PHONE, plugin_module

handles = plugin_module("handles")
inbound = plugin_module("inbound")

DAEMON_LABEL = "com.zack.hermes-imessage.n8n"
PLIST_PATH = Path(__file__).resolve().parent.parent / "n8n" / "launchd" / f"{DAEMON_LABEL}.plist"
ENVIRONMENT_KEY = "EnvironmentVariables"
# The environment table under "Install, service and hardening" in
# docs/superpowers/specs/2026-10-30-n8n-routing-design.md, row by row, as literals.
DESIGNED_ENVIRONMENT = {
    "EXECUTIONS_DATA_MAX_AGE": "168",
    "EXECUTIONS_DATA_PRUNE": "true",
    "EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS": "false",
    "EXECUTIONS_DATA_SAVE_ON_ERROR": "none",
    "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false",
    "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none",
    "EXTERNAL_FRONTEND_HOOKS_URLS": "",
    "HOME": "/var/n8n",
    "N8N_BLOCK_ENV_ACCESS_IN_NODE": "true",
    "N8N_COMMUNITY_PACKAGES_ENABLED": "false",
    "N8N_DIAGNOSTICS_CONFIG_BACKEND": "",
    "N8N_DIAGNOSTICS_CONFIG_FRONTEND": "",
    "N8N_DIAGNOSTICS_ENABLED": "false",
    "N8N_DISABLED_MODULES": "insights",
    "N8N_EDITOR_BASE_URL": "http://127.0.0.1:5678/",
    "N8N_HOST": "127.0.0.1",
    "N8N_LICENSE_AUTO_RENEW_ENABLED": "false",
    "N8N_LICENSE_SERVER_URL": "http://127.0.0.1:9/",
    "N8N_LISTEN_ADDRESS": "127.0.0.1",
    "N8N_LOG_FILE_LOCATION": "/var/n8n/log/n8n.log",
    "N8N_LOG_LEVEL": "info",
    "N8N_LOG_OUTPUT": "file",
    "N8N_PERSONALIZATION_ENABLED": "false",
    "N8N_PORT": "5678",
    "N8N_PROTOCOL": "http",
    "N8N_PUBLIC_API_DISABLED": "true",
    "N8N_PYTHON_ENABLED": "false",
    "N8N_RESTRICT_FILE_ACCESS_TO": "/var/n8n/.n8n",
    "N8N_RUNNERS_BROKER_LISTEN_ADDRESS": "127.0.0.1",
    "N8N_SECURE_COOKIE": "false",
    "N8N_SSRF_ALLOWED_IP_RANGES": "127.0.0.1/32",
    "N8N_SSRF_PROTECTION_ENABLED": "true",
    "N8N_TEMPLATES_ENABLED": "false",
    "N8N_USER_FOLDER": "/var/n8n",
    "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
    "N8N_WEBHOOK_URL": "http://127.0.0.1:5678/",
    "NODE_OPTIONS": "--max-old-space-size=512",
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
}
# The rest of the plist, from the paragraph under that table. Umask is the string "027":
# launchd reads a string through strtoul, so the leading zero makes it octal.
DESIGNED_KEYS = {
    "GroupName": "_n8n",
    "KeepAlive": True,
    "Label": DAEMON_LABEL,
    "ProcessType": "Background",
    "ProgramArguments": ["/opt/homebrew/bin/node", "/var/n8n/npm/lib/node_modules/n8n/bin/n8n", "start"],
    "RunAtLoad": True,
    "StandardErrorPath": "/var/n8n/log/launchd.err.log",
    "StandardOutPath": "/var/n8n/log/launchd.out.log",
    "ThrottleInterval": 30,
    "Umask": "027",
    "UserName": "_n8n",
    "WorkingDirectory": "/var/n8n",
}
# Absent by design: n8n's 2.x default already excludes Execute Command, and n8n writes its
# own key to /var/n8n/.n8n/config, readable by _n8n and root alone, never by every user.
FORBIDDEN_VARIABLES = ("N8N_ENCRYPTION_KEY", "NODES_EXCLUDE")
RELAY_SECRET_EXAMPLE = "0123456789abcdef" * 4  # the X-Relay-Secret's shape: 64 hex characters
# Hex and URL-safe base64 match whole. The slash is left out so a path never matches, which
# means a standard base64 secret is caught only when a run between its slashes is that long.
SECRET_PATTERN = re.compile(rf"[A-Za-z0-9+=_-]{{{inbound.SECRET_MINIMUM_LENGTH},}}")
# The design's own names are exempt from the rule on names: N8N_VERSION_NOTIFICATIONS_ENABLED
# alone is a run of 33 token characters.
DESIGNED_NAMES = frozenset({*DESIGNED_ENVIRONMENT, *DESIGNED_KEYS, ENVIRONMENT_KEY, *FORBIDDEN_VARIABLES})
# plistlib drops comments, so they are read from the file's text.
COMMENT_PATTERN = re.compile(r"<!--(.*?)-->", re.DOTALL)
WORD_PUNCTUATION = ".,;:()[]<>\"'"
UNDESIGNED_ROW = ("N8N_GRACEFUL_SHUTDOWN_TIMEOUT", "30")


def looks_like_secret_or_handle(value: str) -> bool:
    """Whether *value*, anywhere in the plist, reads as a credential or a person's handle.

    A credential is an unbroken run of token characters as long as the shortest secret the
    plugin accepts. A handle is whatever the plugin itself would read as one, so the two
    definitions cannot drift apart.
    """
    if SECRET_PATTERN.search(value):
        return True
    try:
        handles.canonical_handle(value)
    except handles.InvalidHandleError:
        return False
    return True


def same_value(actual: object, expected: object) -> bool:
    """Equal and of the same type: launchd ignores an environment value that is not a string,
    and Python's `True == 1` would let an integer stand in for a boolean key."""
    return type(actual) is type(expected) and actual == expected


def every_string(node: object, location: str = "") -> list[tuple[str, str]]:
    """Each string value in a parsed plist, with the path that reaches it."""
    if isinstance(node, str):
        return [(location, node)]
    if isinstance(node, dict):
        return [found for key, child in node.items() for found in every_string(child, f"{location}/{key}")]
    if isinstance(node, list):
        return [found for index, child in enumerate(node) for found in every_string(child, f"{location}[{index}]")]
    return []


def every_undesigned_name(node: object, location: str = "") -> list[tuple[str, str]]:
    """Each dict key in a parsed plist that the design does not give, with the path to its dict."""
    if isinstance(node, dict):
        return [*((location, key) for key in node if key not in DESIGNED_NAMES),
                *(found for key, child in node.items() for found in every_undesigned_name(child, f"{location}/{key}"))]
    if isinstance(node, list):
        return [found for index, child in enumerate(node)
                for found in every_undesigned_name(child, f"{location}[{index}]")]
    return []


def every_data(node: object, location: str = "") -> list[str]:
    """The path to each <data> value in a parsed plist, which plistlib reads as bytes."""
    if isinstance(node, bytes):
        return [location]
    if isinstance(node, dict):
        return [found for key, child in node.items() for found in every_data(child, f"{location}/{key}")]
    if isinstance(node, list):
        return [found for index, child in enumerate(node) for found in every_data(child, f"{location}[{index}]")]
    return []


def comment_problems(text: str) -> list[str]:
    """Each comment in a plist's text holding a word that reads as a secret or a handle.

    A word is a run between spaces, trimmed of punctuation, so a phone number written with
    spaces inside it is not seen; the plist's one comment holds no number at all.
    """
    return [f"comment {number} looks like it holds a secret or a handle"
            for number, comment in enumerate(COMMENT_PATTERN.findall(text), start=1)
            if any(looks_like_secret_or_handle(word.strip(WORD_PUNCTUATION)) for word in comment.split())]


def plist_problems(daemon: dict) -> list[str]:
    """Every way *daemon* departs from the design, one line each, empty when it matches.

    A message names a location and the design's own value, never the plist's: the value that
    broke a rule may be the secret the suite exists to keep out of a public CI log.
    """
    problems: list[str] = []
    environment = daemon.get(ENVIRONMENT_KEY, {})
    for name, expected in DESIGNED_ENVIRONMENT.items():
        if name not in environment:
            problems.append(f"{ENVIRONMENT_KEY}/{name} is missing")
        elif not same_value(environment[name], expected):
            problems.append(f"{ENVIRONMENT_KEY}/{name} differs from the design, which says {expected!r}")
    for key, expected in DESIGNED_KEYS.items():
        if key not in daemon:
            problems.append(f"{key} is missing")
        elif not same_value(daemon[key], expected):
            problems.append(f"{key} differs from the design, which says {expected!r}")
    problems.extend(f"{ENVIRONMENT_KEY}/{name} must be absent" for name in FORBIDDEN_VARIABLES if name in environment)
    problems.extend(f"{location} looks like a secret or a handle"
                    for location, value in every_string(daemon) if looks_like_secret_or_handle(value))
    problems.extend(f"{location or 'the top level'} has a name that looks like a secret or a handle"
                    for location, name in every_undesigned_name(daemon) if looks_like_secret_or_handle(name))
    problems.extend(f"{location} is data, which the design never holds" for location in every_data(daemon))
    return problems


def repository_plist() -> dict:
    with PLIST_PATH.open("rb") as plist_file:
        return plistlib.load(plist_file)


class RepositoryPlistTest(unittest.TestCase):
    def test_the_repository_plist_matches_the_design(self):
        self.assertEqual(plist_problems(repository_plist()), [])

    def test_the_repository_plists_comments_hold_no_secret_or_handle(self):
        text = PLIST_PATH.read_text(encoding="utf-8")
        self.assertTrue(COMMENT_PATTERN.findall(text), "the plist's comment, which this reads, is gone")
        self.assertEqual(comment_problems(text), [])

    def test_the_file_is_named_for_its_label(self):
        # launchd refuses a daemon whose file name and Label disagree.
        self.assertEqual(PLIST_PATH.stem, repository_plist()["Label"])


class PlistRuleTest(unittest.TestCase):
    """Each rule, broken on a copy of the plist, is named and nothing else is."""

    def setUp(self):
        self.daemon = repository_plist()

    def broken(self, change) -> list[str]:
        daemon = copy.deepcopy(self.daemon)
        change(daemon)
        return plist_problems(daemon)

    def assert_one_problem_naming(self, problems: list[str], name: str) -> None:
        self.assertEqual(len(problems), 1, problems)
        self.assertRegex(problems[0], rf"(^|/){re.escape(name)}\b")

    def test_a_missing_environment_row_is_named(self):
        for name in DESIGNED_ENVIRONMENT:
            with self.subTest(name=name):
                problems = self.broken(lambda daemon, name=name: daemon[ENVIRONMENT_KEY].pop(name))
                self.assert_one_problem_naming(problems, name)

    def test_a_changed_environment_row_is_named(self):
        for name in DESIGNED_ENVIRONMENT:
            with self.subTest(name=name):
                problems = self.broken(lambda daemon, name=name: daemon[ENVIRONMENT_KEY].update({name: "0.0.0.0"}))
                self.assert_one_problem_naming(problems, name)

    def test_an_environment_value_that_is_not_a_string_is_named(self):
        # launchd would silently drop it, and n8n would fall back to its own default.
        problems = self.broken(lambda daemon: daemon[ENVIRONMENT_KEY].update({"N8N_PORT": 5678}))
        self.assert_one_problem_naming(problems, "N8N_PORT")

    def test_a_missing_key_is_named(self):
        for key in DESIGNED_KEYS:
            with self.subTest(key=key):
                problems = self.broken(lambda daemon, key=key: daemon.pop(key))
                self.assert_one_problem_naming(problems, key)

    def test_a_changed_key_is_named(self):
        changed_values = {bool: False, int: 0, list: ["/usr/bin/false"], str: "root"}
        for key, expected in DESIGNED_KEYS.items():
            with self.subTest(key=key):
                change = {key: changed_values[type(expected)]}
                problems = self.broken(lambda daemon, change=change: daemon.update(change))
                self.assert_one_problem_naming(problems, key)

    def test_a_boolean_key_written_as_an_integer_is_named(self):
        problems = self.broken(lambda daemon: daemon.update({"KeepAlive": 1}))
        self.assert_one_problem_naming(problems, "KeepAlive")

    def test_a_forbidden_variable_is_named_even_when_empty(self):
        for name in FORBIDDEN_VARIABLES:
            with self.subTest(name=name):
                problems = self.broken(lambda daemon, name=name: daemon[ENVIRONMENT_KEY].update({name: ""}))
                self.assert_one_problem_naming(problems, name)

    def test_a_secret_or_a_handle_anywhere_is_named_without_its_value(self):
        placements = {
            "an environment row": lambda daemon, value: daemon[ENVIRONMENT_KEY].update({"RELAY_TOKEN": value}),
            "a program argument": lambda daemon, value: daemon.update({"ProgramArguments": [value]}),
        }
        for value in (RELAY_SECRET_EXAMPLE, ZACK_PHONE, ZACK_EMAIL):
            for placement, change in placements.items():
                with self.subTest(value=value, placement=placement):
                    problems = self.broken(lambda daemon, change=change, value=value: change(daemon, value))
                    hits = [problem for problem in problems if problem.endswith("looks like a secret or a handle")]
                    self.assertEqual(len(hits), 1, problems)
                    self.assertNotIn(value, " ".join(problems))

    def test_a_secret_or_a_handle_as_a_name_the_design_does_not_give_is_named_without_it(self):
        for value in (RELAY_SECRET_EXAMPLE, ZACK_PHONE, ZACK_EMAIL):
            with self.subTest(value=value):
                problems = self.broken(lambda daemon, value=value: daemon[ENVIRONMENT_KEY].update({value: "1"}))
                self.assert_one_problem_naming(problems, ENVIRONMENT_KEY)
                self.assertNotIn(value, " ".join(problems))

    def test_data_anywhere_is_named(self):
        # plistlib reads <data> as bytes, which no rule on strings can see into.
        problems = self.broken(lambda daemon: daemon.update({"Payload": RELAY_SECRET_EXAMPLE.encode()}))
        self.assert_one_problem_naming(problems, "Payload")

    def test_a_secret_or_a_handle_in_a_comment_is_named_without_its_value(self):
        text = PLIST_PATH.read_text(encoding="utf-8")
        for value in (RELAY_SECRET_EXAMPLE, ZACK_PHONE, ZACK_EMAIL):
            with self.subTest(value=value):
                # A comma: canonical_handle refuses a number with one, so the word must be trimmed.
                comment = f"<!-- the relay's is {value}, for now -->\n<dict>"
                problems = comment_problems(text.replace("<dict>", comment, 1))
                self.assertEqual(len(problems), 1, problems)
                self.assertNotIn(value, " ".join(problems))

    def test_a_row_the_design_does_not_name_is_allowed(self):
        name, value = UNDESIGNED_ROW
        self.assertEqual(self.broken(lambda daemon: daemon[ENVIRONMENT_KEY].update({name: value})), [])


if __name__ == "__main__":
    unittest.main()
