"""The commands n8n/check_relay.py runs against the instance, really run.

No test can reach n8n's own database, so a scratch one is built from its migrations, with the real
relay's rows beside the check's to show they survive. Only sudo and n8n's CLI are stood in for.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from n8n_check_support import RUN_RECORD_TABLE_ID, STAND_IN_URL, check_relay, template

REAL_RELAY_ID = "realRelayWorkflow"
REAL_CREDENTIAL_ID = "realCredentialId"
REAL_TABLE_ID = "realRunRecordTab"
# The tables as n8n's migrations make them, as far as this concerns them. The RESTRICT is
# 1772619247762-ChangeWorkflowPublishedVersionFKsToRestrict, which is what stops a delete; webhook
# rows, the outbox and the statistics have no foreign key, so nothing clears them for a workflow.
SCHEMA = f"""
CREATE TABLE workflow_entity (id TEXT PRIMARY KEY, name TEXT, activeVersionId TEXT);
CREATE TABLE workflow_history (versionId TEXT PRIMARY KEY, workflowId TEXT NOT NULL REFERENCES workflow_entity (id) ON DELETE CASCADE);
CREATE TABLE workflow_published_version (
  workflowId TEXT PRIMARY KEY REFERENCES workflow_entity (id) ON DELETE RESTRICT,
  publishedVersionId TEXT REFERENCES workflow_history (versionId) ON DELETE RESTRICT);
CREATE TABLE shared_workflow (workflowId TEXT NOT NULL REFERENCES workflow_entity (id) ON DELETE CASCADE, projectId TEXT);
CREATE TABLE webhook_entity (webhookPath TEXT, method TEXT, workflowId TEXT);
CREATE TABLE workflow_publication_outbox (id INTEGER PRIMARY KEY, workflowId TEXT, status TEXT);
CREATE TABLE workflow_statistics (id INTEGER PRIMARY KEY, workflowId TEXT, name TEXT);
CREATE TABLE credentials_entity (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE shared_credentials (credentialsId TEXT NOT NULL REFERENCES credentials_entity (id) ON DELETE CASCADE, projectId TEXT);
CREATE TABLE data_table (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE data_table_column (id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, "index" INTEGER NOT NULL,
  dataTableId TEXT NOT NULL REFERENCES data_table (id) ON DELETE CASCADE);
INSERT INTO data_table_column SELECT table_id || column_name, column_name, column_type, position, table_id FROM
  (SELECT '{RUN_RECORD_TABLE_ID}' AS table_id UNION SELECT '{REAL_TABLE_ID}') CROSS JOIN (SELECT 'received_at' AS column_name,
  'string' AS column_type, 0 AS position UNION SELECT 'route', 'string', 1 UNION SELECT 'outcome', 'string', 2
  UNION SELECT 'plugin_status', 'number', 3 UNION SELECT 'duration_ms', 'number', 4);
CREATE TABLE data_table_user_{RUN_RECORD_TABLE_ID} (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, route TEXT, outcome TEXT, plugin_status REAL, duration_ms REAL);
CREATE TABLE data_table_user_{REAL_TABLE_ID} (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, route TEXT, outcome TEXT, plugin_status REAL, duration_ms REAL);
INSERT INTO data_table VALUES ('{RUN_RECORD_TABLE_ID}', 'run_record_check'), ('{REAL_TABLE_ID}', 'run_record');
INSERT INTO workflow_entity VALUES ('{REAL_RELAY_ID}', 'iMessage relay', 'real-version');
INSERT INTO workflow_history VALUES ('real-version', '{REAL_RELAY_ID}');
INSERT INTO workflow_published_version VALUES ('{REAL_RELAY_ID}', 'real-version');
INSERT INTO shared_workflow VALUES ('{REAL_RELAY_ID}', 'project');
INSERT INTO webhook_entity VALUES ('imessage-relay', 'POST', '{REAL_RELAY_ID}');
INSERT INTO workflow_publication_outbox (workflowId, status) VALUES ('{REAL_RELAY_ID}', 'completed');
INSERT INTO workflow_statistics (workflowId, name) VALUES ('{REAL_RELAY_ID}', 'production_success');
INSERT INTO credentials_entity VALUES ('{REAL_CREDENTIAL_ID}', 'Shortcuts relay secret');
INSERT INTO shared_credentials VALUES ('{REAL_CREDENTIAL_ID}', 'project');
INSERT INTO data_table_user_{REAL_TABLE_ID} (received_at, route, outcome, plugin_status, duration_ms) VALUES ('then', 'zack', 'accepted', 202, 3);
"""
TABLES_THAT_HOLD_THE_REAL_RELAY = ("workflow_entity", "workflow_history", "workflow_published_version", "shared_workflow", "webhook_entity",
                                   "workflow_publication_outbox", "workflow_statistics", "credentials_entity", "shared_credentials",
                                   f"data_table_user_{REAL_TABLE_ID}")
CHECK_ID = check_relay.CHECK_WORKFLOW_ID


class LocalN8n:
    """Stands in for sudo and n8n's CLI alone: everything else really runs, on a scratch database."""

    def __init__(self, database: Path):
        self.database = database
        self.calls: list[list[str]] = []
        self.daemon_takes_workflows_down = True
        self.failing_command: str | None = None
        self.seen_by_cli: dict[str, str] = {}

    def __call__(self, arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(arguments))
        if arguments[0] == str(check_relay.CLI_SCRIPT):
            return self.cli(arguments[1:])
        if arguments[:3] != ["sudo", "-u", check_relay.N8N_USER]:
            raise AssertionError(f"not run as {check_relay.N8N_USER}: {arguments}")
        return subprocess.run(arguments[3:], **options)  # noqa: S603 -- the check's own commands, on scratch paths

    def cli(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = arguments[0]
        if command == self.failing_command:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")
        if command.startswith("import:"):
            self.seen_by_cli[command] = Path(arguments[1].removeprefix("--input=")).read_text(encoding="utf-8")
        with sqlite3.connect(self.database) as connection:
            if command == "import:credentials":
                for credential in json.loads(self.seen_by_cli[command]):
                    connection.execute("INSERT INTO credentials_entity VALUES (?, ?)", (credential["id"], credential["name"]))
                    connection.execute("INSERT INTO shared_credentials VALUES (?, 'project')", (credential["id"],))
            elif command == "import:workflow":
                connection.execute("INSERT INTO workflow_entity VALUES (?, ?, NULL)", (CHECK_ID, check_relay.CHECK_WORKFLOW_NAME))
                connection.execute("INSERT INTO workflow_history VALUES ('check-version', ?)", (CHECK_ID,))
                connection.execute("INSERT INTO shared_workflow VALUES (?, 'project')", (CHECK_ID,))
            elif command == "publish:workflow":
                connection.execute("UPDATE workflow_entity SET activeVersionId = 'check-version' WHERE id = ?", (CHECK_ID,))
                self.daemon_publishes(connection)
            elif command == "unpublish:workflow":
                connection.execute("UPDATE workflow_entity SET activeVersionId = NULL WHERE id = ?", (CHECK_ID,))
                if self.daemon_takes_workflows_down:
                    self.daemon_unpublishes(connection)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    def daemon_publishes(self, connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO workflow_published_version VALUES (?, 'check-version')", (CHECK_ID,))
        connection.execute("INSERT INTO webhook_entity VALUES ('imessage-relay-check', 'POST', ?)", (CHECK_ID,))
        connection.execute("INSERT INTO workflow_publication_outbox (workflowId, status) VALUES (?, 'completed')", (CHECK_ID,))
        connection.execute("INSERT INTO workflow_statistics (workflowId, name) VALUES (?, 'production_success')", (CHECK_ID,))

    def daemon_unpublishes(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM workflow_published_version WHERE workflowId = ?", (CHECK_ID,))
        connection.execute("DELETE FROM webhook_entity WHERE workflowId = ?", (CHECK_ID,))


class InstanceTest(unittest.TestCase):
    """The real instance's commands, really run. No test can reach n8n's own database, so this one is
    built from its migrations, with the real relay's rows beside the check's to show they survive."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.database = self.scratch / "database.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(SCHEMA)
        self.run = LocalN8n(self.database)
        self.instance = self.instance_of(RUN_RECORD_TABLE_ID)

    def instance_of(self, table_id: str, unpublish_wait_seconds: float = 0.2) -> check_relay.N8nInstance:
        return check_relay.N8nInstance(table_id, run=self.run, home=str(self.scratch), database=str(self.database),
                                       unpublish_wait_seconds=unpublish_wait_seconds, poll_seconds=0.01)

    def install(self, secret: str = "a-secret") -> None:
        credentials = check_relay.check_credentials(secret, secret, None)
        self.instance.install(check_relay.build_check_workflow(template(), STAND_IN_URL, RUN_RECORD_TABLE_ID, None), credentials)

    def write_row(self, route: str = "zack") -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(f"INSERT INTO data_table_user_{RUN_RECORD_TABLE_ID} (received_at, route, outcome, plugin_status, duration_ms)"
                               " VALUES ('now', ?, 'accepted', 202, 4)", (route,))

    def count(self, table: str) -> int:
        with sqlite3.connect(self.database) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def real_rows(self) -> dict[str, list[tuple]]:
        with sqlite3.connect(self.database) as connection:
            return {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in TABLES_THAT_HOLD_THE_REAL_RELAY}

    def row_counts(self) -> dict[str, int]:
        """How many rows each table holds, for what has to be left in place rather than as it was:
        unpublishing clears a workflow's active version before anything can refuse."""
        return {table: self.count(table) for table in TABLES_THAT_HOLD_THE_REAL_RELAY}

    def hand_over_folders(self) -> list[Path]:
        return list(self.scratch.glob(f"{check_relay.HAND_OVER_FOLDER_PREFIX}*"))

    def cli_commands(self) -> list[str]:
        return [call[1] for call in self.run.calls if call[0] == str(check_relay.CLI_SCRIPT)]

    def test_reading_rows_is_read_only_and_as_n8n(self):
        self.write_row()
        self.assertEqual([row["route"] for row in self.instance.rows()], ["zack"])
        read = next(call for call in self.run.calls if check_relay.SQLITE_PATH in call)
        self.assertIn("-readonly", read)

    def test_no_rows_is_an_empty_list(self):
        self.assertEqual(self.instance.rows(), [])

    def test_a_status_reads_back_equal_though_it_is_stored_as_a_real(self):
        self.write_row()
        self.assertEqual(self.instance.rows()[0]["plugin_status"], check_relay.ACCEPTED_STATUS)

    def test_nothing_there_counts_as_no_leftovers(self):
        self.assertEqual(self.instance.leftovers(), 0)

    def test_each_kind_of_thing_it_can_leave_counts_as_a_leftover(self):
        # Three of these have no foreign key, so removing the workflow would not clear them.
        for table, values in (("workflow_entity", f"('{CHECK_ID}', 'x', NULL)"),
                              ("webhook_entity", f"('p', 'POST', '{CHECK_ID}')"),
                              ("workflow_publication_outbox", f"(1000, '{CHECK_ID}', 'completed')"),
                              ("workflow_statistics", f"(1000, '{CHECK_ID}', 'n')"),
                              ("credentials_entity", f"('{check_relay.CHECK_RELAY_CREDENTIAL_ID}', 'x')"),
                              (f"data_table_user_{RUN_RECORD_TABLE_ID}", "(1000, 'r', 'z', 'o', 1, 1)")):
            with self.subTest(table):
                with sqlite3.connect(self.database) as connection:
                    connection.execute(f"INSERT INTO {table} VALUES {values}")
                self.assertGreater(self.instance.leftovers(), 0)
                self.instance.remove_leftovers()
                self.assertEqual(self.instance.leftovers(), 0)

    def test_a_published_workflow_is_unpublished_before_it_is_deleted_and_all_of_it_goes(self):
        self.install()
        self.write_row()
        self.assertGreater(self.instance.leftovers(), 0)
        self.instance.remove_leftovers()
        self.assertEqual(self.instance.leftovers(), 0)
        self.assertIn("unpublish:workflow", self.cli_commands())

    def test_shared_rows_go_with_what_they_belong_to(self):
        self.install()
        before = self.real_rows()
        self.instance.remove_leftovers()
        with sqlite3.connect(self.database) as connection:
            for table in ("shared_workflow", "shared_credentials", "workflow_history"):
                with self.subTest(table):
                    self.assertEqual(connection.execute(f"SELECT * FROM {table}").fetchall(), [row for row in before[table] if CHECK_ID not in row
                                     and check_relay.CHECK_RELAY_CREDENTIAL_ID not in row and check_relay.CHECK_PLUGIN_CREDENTIAL_ID not in row])

    def test_the_real_relay_is_exactly_as_it_was(self):
        before = self.real_rows()
        self.install()
        self.write_row()
        self.instance.remove_leftovers()
        self.assertEqual(self.real_rows(), before)

    def test_deleting_a_published_workflow_is_refused_which_is_why_it_is_unpublished_first(self):
        # The control: without this the unpublishing above could be doing nothing.
        self.install()
        self.instance._unpublish = lambda: None
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()

    def test_a_removal_that_fails_changes_nothing(self):
        # All or none: half a clean-up would leave a workflow with its webhook row gone.
        self.install()
        self.write_row()
        self.instance._unpublish = lambda: None
        before = self.real_rows()
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()
        self.assertEqual(self.real_rows(), before)
        self.assertEqual(self.count(f"data_table_user_{RUN_RECORD_TABLE_ID}"), 1)

    def test_a_table_that_is_not_there_fails_the_removal_and_rolls_it_back(self):
        self.install()
        with sqlite3.connect(self.database) as connection:
            self.run.daemon_unpublishes(connection)
        before = self.row_counts()
        with self.assertRaises(check_relay.CheckError):
            self.instance_of("noSuchTableHere").remove_leftovers()
        self.assertEqual(self.row_counts(), before)

    def test_a_daemon_that_never_takes_it_down_is_refused_and_nothing_is_deleted(self):
        self.install()
        self.run.daemon_takes_workflows_down = False
        before = self.row_counts()
        with self.assertRaises(check_relay.CheckError) as raised:
            self.instance.remove_leftovers()
        self.assertIn("had not finished", str(raised.exception))
        self.assertEqual(self.row_counts(), before)

    def test_a_daemon_slow_to_take_it_down_is_waited_for(self):
        self.install()
        self.run.daemon_takes_workflows_down = False
        reads_until_down = {"left": 3}
        real_rows_left = self.instance._publication_rows_left

        def slowly() -> int:
            reads_until_down["left"] -= 1
            if reads_until_down["left"] == 0:
                with sqlite3.connect(self.database) as connection:
                    self.run.daemon_unpublishes(connection)
            return real_rows_left()

        self.instance._publication_rows_left = slowly
        self.instance.remove_leftovers()
        self.assertEqual(self.instance.leftovers(), 0)

    def test_nothing_of_the_checks_there_needs_no_unpublish(self):
        self.instance.remove_leftovers()
        self.assertNotIn("unpublish:workflow", self.cli_commands())

    def test_a_folder_a_killed_run_left_is_swept_and_nothing_else_is(self):
        stale = self.scratch / f"{check_relay.HAND_OVER_FOLDER_PREFIX}stale"
        stale.mkdir()
        (stale / "credentials.json").write_text("a secret from that run\n", encoding="utf-8")
        for kept in ("import.stale", ".n8n", "log"):
            (self.scratch / kept).mkdir()
        self.instance.remove_leftovers()
        self.assertEqual(self.hand_over_folders(), [])
        for kept in ("import.stale", ".n8n", "log"):
            self.assertTrue((self.scratch / kept).is_dir(), kept)

    def test_installing_imports_credentials_then_workflow_then_publishes(self):
        self.install()
        self.assertEqual(self.cli_commands(), ["import:credentials", "import:workflow", "publish:workflow"])

    def test_the_cli_is_handed_the_real_content_through_a_file(self):
        self.install("a-secret")
        self.assertEqual(json.loads(self.run.seen_by_cli["import:workflow"])["id"], CHECK_ID)
        self.assertIn("a-secret", self.run.seen_by_cli["import:credentials"])

    def test_no_secret_is_on_any_command_line(self):
        secret = "s3cret-value-that-must-not-show"
        self.install(secret)
        self.instance.remove_leftovers()
        self.assertTrue(self.run.calls)
        for call in self.run.calls:
            self.assertNotIn(secret, " ".join(call))

    def test_the_copies_are_gone_after_a_good_install(self):
        self.install()
        self.assertEqual(self.hand_over_folders(), [])

    def test_the_copies_are_gone_after_a_failed_import_and_it_is_refused(self):
        for command in ("import:credentials", "import:workflow", "publish:workflow"):
            with self.subTest(command):
                self.run.failing_command = command
                with self.assertRaises(check_relay.CheckError) as raised:
                    self.install()
                self.assertIn(command, str(raised.exception))
                self.assertEqual(self.hand_over_folders(), [])
                self.run.failing_command = None
                self.instance.remove_leftovers()

    def test_a_folder_it_could_not_make_stops_the_install(self):
        instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=self.run, home=str(self.scratch), database=str(self.database))
        instance.home = str(self.scratch / "no-such-home")
        (self.scratch / "no-such-home").mkdir()
        (self.scratch / "no-such-home").chmod(0o500)
        self.addCleanup((self.scratch / "no-such-home").chmod, 0o700)
        with self.assertRaises(check_relay.CheckError) as raised:
            instance.install({}, [])
        self.assertIn("could not make a folder", str(raised.exception))

    def test_a_failed_unpublish_is_refused(self):
        self.install()
        self.run.failing_command = "unpublish:workflow"
        with self.assertRaises(check_relay.CheckError):
            self.instance.remove_leftovers()

    def test_a_database_that_cannot_be_read_is_refused_rather_than_read_as_empty(self):
        instance = check_relay.N8nInstance(RUN_RECORD_TABLE_ID, run=self.run, home=str(self.scratch), database=str(self.scratch / "absent.sqlite"))
        for read in (instance.rows, instance.leftovers, instance.remove_leftovers):
            with self.subTest(read.__name__), self.assertRaises(check_relay.CheckError):
                read()

    def test_a_table_id_that_could_not_be_one_is_refused_before_it_reaches_sql(self):
        with self.assertRaises(check_relay.CheckError):
            check_relay.N8nInstance("x'; DROP TABLE workflow_entity; --", run=self.run)
        self.assertEqual(self.run.calls, [])

    def test_the_check_table_is_found_by_name(self):
        identifier = check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))
        self.assertEqual(identifier, RUN_RECORD_TABLE_ID)
        self.assertIn("-readonly", self.run.calls[0])

    def test_no_check_table_says_how_to_make_one(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM data_table WHERE name = 'run_record_check'")
        with self.assertRaises(check_relay.CheckError) as raised:
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))
        self.assertIn("n8n/README.md", str(raised.exception))

    def test_two_tables_of_that_name_are_refused_rather_than_guessed_between(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO data_table VALUES ('anotherTableId00', 'run_record_check')")
        with self.assertRaises(check_relay.CheckError):
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))

    def test_a_database_that_cannot_be_read_when_finding_the_table_is_refused(self):
        with self.assertRaises(check_relay.CheckError):
            check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.scratch / "absent.sqlite"))


if __name__ == "__main__":
    unittest.main()
