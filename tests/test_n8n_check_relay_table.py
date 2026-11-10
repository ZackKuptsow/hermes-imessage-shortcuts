"""The check's own Data Table must have the run record's five columns, typed as n8n types them.

On 2026-11-09 `run_record_check` was made with `duration_ms` as a String column. n8n then stored
each duration as text, and every row the relay wrote failed the check as if the relay were wrong.
So the check reads the table's columns from n8n's `data_table_column` before it imports anything,
and refuses a table that differs, naming each difference. The database is built from n8n's
migrations, as in test_n8n_check_relay.py, and really read by sqlite3.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_n8n_check_relay import RUN_RECORD_TABLE_ID, SCHEMA, LocalN8n, check_relay


class CheckTableColumnsTest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.database = self.scratch / "database.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(SCHEMA)
        self.run = LocalN8n(self.database)

    def change_columns(self, sql: str, *parameters: object) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(sql, parameters)

    def find(self) -> str:
        return check_relay.find_check_table(run=self.run, home=str(self.scratch), database=str(self.database))

    def refusal(self) -> str:
        with self.assertRaises(check_relay.CheckError) as raised:
            self.find()
        return str(raised.exception)

    def test_a_table_with_the_run_records_five_columns_is_found(self):
        self.assertEqual(self.find(), RUN_RECORD_TABLE_ID)

    def test_a_duration_made_as_a_string_column_is_refused_by_name(self):
        self.change_columns("UPDATE data_table_column SET type = 'string' WHERE dataTableId = ? AND name = 'duration_ms'",
                            RUN_RECORD_TABLE_ID)
        message = self.refusal()
        self.assertIn("duration_ms is a string column", message)
        self.assertIn("number", message)
        self.assertNotIn("plugin_status", message)

    def test_a_route_made_as_a_number_column_is_refused(self):
        self.change_columns("UPDATE data_table_column SET type = 'number' WHERE dataTableId = ? AND name = 'route'",
                            RUN_RECORD_TABLE_ID)
        self.assertIn("route is a number column", self.refusal())

    def test_a_missing_column_is_refused(self):
        self.change_columns("DELETE FROM data_table_column WHERE dataTableId = ? AND name = 'outcome'", RUN_RECORD_TABLE_ID)
        self.assertIn("outcome is missing", self.refusal())

    def test_a_sixth_column_is_refused(self):
        self.change_columns("INSERT INTO data_table_column VALUES ('extra', 'text', 'string', 5, ?)", RUN_RECORD_TABLE_ID)
        self.assertIn("text is not one of the run record's columns", self.refusal())

    def test_the_refusal_says_where_the_columns_are_listed(self):
        self.change_columns("DELETE FROM data_table_column WHERE dataTableId = ?", RUN_RECORD_TABLE_ID)
        self.assertIn("n8n/README.md", self.refusal())

    def test_only_the_check_tables_columns_count(self):
        # The real run_record's columns are the relay's business, not the check's.
        self.change_columns("UPDATE data_table_column SET type = 'string' WHERE dataTableId <> ?", RUN_RECORD_TABLE_ID)
        self.assertEqual(self.find(), RUN_RECORD_TABLE_ID)

    def test_the_columns_are_read_without_writing(self):
        self.find()
        self.assertTrue(all("-readonly" in call for call in self.run.calls), self.run.calls)


if __name__ == "__main__":
    unittest.main()
