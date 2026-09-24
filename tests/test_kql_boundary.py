"""The final KQL execution boundary (_kql_boundary.check), tested on its own."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _kql_boundary  # noqa: E402

TABLE = "default"


class KqlBoundaryTest(unittest.TestCase):
    # Covers SECURITY.md#query-and-process-execution
    def test_semicolons_are_rejected_anywhere(self):
        for query in (f"{TABLE} | take 1;", f"{TABLE} | where a == ';'", ";", f"{TABLE};.drop table x"):
            with self.subTest(query=query):
                self.assertIn("semicolons", _kql_boundary.check(query, TABLE))

    def test_control_commands_are_rejected(self):
        for query in (".show tables", "  .show tables", "\t.drop table x", f".{TABLE}"):
            with self.subTest(query=query):
                self.assertIn("control commands", _kql_boundary.check(query, TABLE))

    def test_query_must_start_with_the_configured_table(self):
        for query in ("", "--profile x", "-P other search", "union *", f"{TABLE}x | take 1", f"x{TABLE} | take 1"):
            with self.subTest(query=query):
                self.assertIn(f"query must start with '{TABLE} | ...'", _kql_boundary.check(query, TABLE))

    def test_valid_queries_pass(self):
        for query in (TABLE, f"{TABLE} | take 1", f"  {TABLE}|take 1", f"\n{TABLE} | where x == 1"):
            with self.subTest(query=query):
                self.assertIsNone(_kql_boundary.check(query, TABLE))

    def test_table_name_is_matched_literally(self):
        self.assertIsNone(_kql_boundary.check("a.b | take 1", "a.b"))
        self.assertIsNotNone(_kql_boundary.check("aXb | take 1", "a.b"))

    def test_error_echo_is_bounded(self):
        message = _kql_boundary.check("x" * 500, TABLE)
        self.assertLess(len(message), 120)


if __name__ == "__main__":
    unittest.main()
