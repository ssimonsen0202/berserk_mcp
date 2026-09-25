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

    # Covers SECURITY.md#query-and-process-execution
    def test_other_sources_are_rejected(self):
        # Codex Security scan 77004e6e finding 1, plus the forms found while fixing it.
        for query in (
            f"{TABLE} | where false | union SecretTable | take 100",
            f"{TABLE} | take 1 | extend probe='candidate' | where probe in (SecretTable) | take 1",
            f"{TABLE} | where x in (union Secret)",
            f"{TABLE} | where x in ((Secret))",
            f"{TABLE} | where x in(Secret)",
            f"{TABLE} | where x IN (Secret)",
            f"{TABLE} | where x in\n(Secret)",
            f"{TABLE} | where x in (['Secret'])",
            f'{TABLE} | where x in (["Secret"] | project y)',
            f"{TABLE} | where x in (Secret, 'a')",
            f"{TABLE} | where x in (dynamic(['a']), Secret)",
            f"{TABLE} | where x !in~ (SecretFn())",
            f"{TABLE} | where x has (Secret)",
            f"{TABLE} | where x has Secret",
            f"{TABLE} | where x has SecretFn()",
            f"{TABLE} | where x has_any SecretFn()",
            f"{TABLE} | where x has ['Secret']",
            f"{TABLE} | where x has dynamic([Secret])",
            f"{TABLE} | where x has_any (Secret | project y)",
            f"{TABLE} | join (Secret) on k",
            f"{TABLE} | lookup Secret on k",
            f"{TABLE} | invoke SecretFn()",
            f"{TABLE} | extend y = toscalar(Secret | count)",
            f"{TABLE} | extend v = table('Secret')",
            f"{TABLE} | evaluate bag_unpack(attributes)",
        ):
            with self.subTest(query=query):
                self.assertIn("only the configured table", _kql_boundary.check(query, TABLE))

    def test_string_forms_cannot_hide_a_source(self):
        # A verbatim string ends at the quote after a backslash; a lexer that
        # treated the backslash as an escape would hide the union.
        for query in (
            f"{TABLE} | where a == @'\\' | union SecretTable | where b == ''",
            f'{TABLE} | where a == @"\\" | union SecretTable | where b == ""',
            f"{TABLE} | where a == h@'x' | join (Secret) on k",
            f"{TABLE} | where a == ```x``` | lookup Secret on k",
            f"{TABLE} | where a == 'x",
            f"{TABLE} | where a == ```x",
        ):
            with self.subTest(query=query):
                self.assertIsNotNone(_kql_boundary.check(query, TABLE))

    def test_single_source_queries_pass(self):
        for query in (
            f"{TABLE} | where metric_name in ('a', 'b') | take 1",
            f"{TABLE} | where x !in (dynamic(['a', 'b'])) | take 1",
            f'{TABLE} | where x has_any (dynamic({{"k": 1}})) | take 1',
            f"{TABLE} | where ts > datetime(2024-01-01T10:00:00) | take 1",
            f"{TABLE} | where x in (1, 2, -3.5, 1h, true, null) | take 1",
            f"{TABLE} | where body contains 'union Secret' | take 1",
            f"{TABLE} | where x == @'C:\\a' | take 1",
            f"{TABLE} | where x has h'secret' | take 1",
            f"{TABLE} // union Secret\n| take 1",
            f"{TABLE} | where x has_any dynamic(['a']) | take 1",
            f"{TABLE} | where isnotempty(resource['service.name']) | summarize count() by bin(timestamp, 1h)",
        ):
            with self.subTest(query=query):
                self.assertIsNone(_kql_boundary.check(query, TABLE))

    def test_strip_literals(self):
        self.assertEqual(_kql_boundary.strip_literals("a 'x;y' b"), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals('a "q\\"x" b'), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals("a @'c:\\' b"), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals("a @'it''s' b"), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals("a H'k' b"), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals("a ```x\ny``` b"), "a '' b")
        self.assertEqual(_kql_boundary.strip_literals("a // c\nb"), "a  \nb")
        self.assertIsNone(_kql_boundary.strip_literals("a 'x"))

    def test_long_query_is_checked_in_linear_time(self):
        import time

        query = f"{TABLE} | where x in (" + "'a', " * 50_000 + "'b') | take 1"
        start = time.monotonic()
        self.assertIsNone(_kql_boundary.check(query, TABLE))
        self.assertLess(time.monotonic() - start, 2.0)


if __name__ == "__main__":
    unittest.main()
