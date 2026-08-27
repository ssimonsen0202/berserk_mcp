import unittest

import kql_validation as kv


SCHEMA_FIELDS = {
    "timestamp", "metric_name", "value", "severity_text", "body",
    "resource['service.name']", "resource.service.name", "service.name",
    "resource['host.name']", "resource.host.name", "host.name",
}


class KqlValidationTest(unittest.TestCase):
    def report(self, kql, **kw):
        return kv.validate_kql_static(
            kql,
            table="default",
            since=kw.pop("since", "15m ago"),
            schema_fields=kw.pop("schema_fields", SCHEMA_FIELDS),
            suggest=kw.pop("suggest", lambda name: ["resource['service.name']"] if name == "service_name" else []),
            **kw,
        )

    def codes(self, report):
        return [f["code"] for f in report["findings"]]

    def test_valid_bounded_query_low_risk(self):
        r = self.report("default | where resource['service.name'] == 'api' | project timestamp, severity_text | take 20")
        self.assertTrue(r["valid"])
        self.assertEqual(r["risk"], "low")

    def test_wrong_table_and_control_commands_rejected(self):
        self.assertIn("WRONG_TABLE", self.codes(self.report("other | take 1")))
        r = self.report(".show tables")
        self.assertFalse(r["valid"])
        self.assertIn("CONTROL_COMMAND", self.codes(r))

    def test_semicolon_is_rejected_even_inside_a_string_literal(self):
        for query in (
            "default | take 1; default | take 1",
            "default | where body contains 'a;b' | take 1",
        ):
            with self.subTest(query=query):
                report = self.report(query)
                self.assertFalse(report["valid"])
                self.assertIn("MULTI_STATEMENT_USER_QUERY", self.codes(report))

    def test_source_introducing_operators_are_blocked_outside_literals(self):
        queries = {
            "union": "default | union default | take 1",
            "externaldata": "default | where value in (externaldata(x:string)['https://example.invalid']) | take 1",
            "evaluate": "default | evaluate plugin() | take 1",
            "find": "default | find withsource=s in (*) where body has 'x' | take 1",
            "search": "default | search 'needle' | take 1",
            "join": "default | join kind=inner (default | take 1) on trace_id | take 10",
            "cluster": (
                "default | where value in (toscalar(cluster('https://evil.example')."
                "database('OtherDb').table('Secret') | take 1)) | take 5"
            ),
            "database": "default | where value in (toscalar(database('OtherDb').table('Secret') | take 1)) | take 5",
            "table": "default | extend x = toscalar(table('Secret') | take 1) | take 5",
            # Codex re-review finding: a bare table-name reference inside
            # toscalar(...), with no cluster()/database()/table() call
            # wrapping it, was not caught by the cluster/database/table
            # function-name check above -- toscalar(Secret | count)
            # references another table directly by name and previously
            # validated cleanly. No legitimate shipped query in this
            # codebase uses toscalar, so block it outright rather than try
            # to distinguish "table name" from "other identifier" by regex.
            "toscalar": "default | extend x = toscalar(Secret | count) | take 5",
            # Second Codex re-review finding: real Kusto's `in` operator
            # accepts a tabular subquery directly -- `col in (TableName |
            # project col)` -- with no join/toscalar/cluster keyword at
            # all. A bare identifier immediately followed by a pipe inside
            # `in (...)` is that shape; a scalar literal list never has a
            # bare identifier directly followed by a pipe there.
            "in-subquery": "default | where trace_id in (Secret | project trace_id) | take 5",
            "not-in-subquery": "default | where trace_id !in (Secret | project trace_id) | take 5",
            # Third Codex re-review round: the bare-identifier-before-pipe
            # heuristic above only recognized ONE disguise for a tabular
            # subquery inside in(...). Real Kusto has several more shapes
            # that are just as valid: a bracket-quoted table name, a
            # function-backed tabular source, and `union` as the first
            # token instead of a bare table name. All of these -- and any
            # future disguise -- share one structural fact a scalar literal
            # list can never have: a `|` character somewhere inside the
            # in(...) group. The fix below checks for that directly
            # (paren-depth-aware pipe scan) instead of pattern-matching
            # specific disguises, closing the whole class at once.
            "in-subquery-bracketed-name": 'default | where trace_id in (["Secret"] | project trace_id) | take 5',
            "in-subquery-function-backed": "default | where trace_id in (SecretFn() | project trace_id) | take 5",
            "in-subquery-union-first-token": "default | where trace_id in (union Secret | project trace_id) | take 5",
            # `lookup` reads a second (right-hand) table by name, the same
            # shape as `join`.
            "lookup": "default | lookup kind=leftouter (Secret | project trace_id) on trace_id | take 5",
        }
        for operator, query in queries.items():
            with self.subTest(operator=operator):
                report = self.report(query, schema_fields=None)
                self.assertFalse(report["valid"])
                self.assertIn("SOURCE_INTRODUCING_OPERATOR", self.codes(report))

    def test_in_with_scalar_literal_list_is_not_blocked(self):
        # Negative control: `in` with an ordinary scalar list (not a
        # tabular subquery) must keep working -- this is the KQL_WORDS-
        # documented, everyday shape, not the exploit shape above.
        for query in (
            "default | where severity_text in ('ERROR', 'WARN') | take 5",
            "default | where metric_name in (dynamic(['a', 'b'])) | take 5",
        ):
            with self.subTest(query=query):
                report = self.report(query, schema_fields=None)
                self.assertNotIn("SOURCE_INTRODUCING_OPERATOR", self.codes(report))

    def test_source_operator_words_inside_literals_do_not_trigger(self):
        for query in (
            "default | where body contains 'union externaldata evaluate find search' | take 1",
            "default | where metric_name == 'x' // | union externaldata evaluate find search\n| take 1",
        ):
            with self.subTest(query=query):
                report = self.report(query, schema_fields=None)
                self.assertNotIn("SOURCE_INTRODUCING_OPERATOR", self.codes(report))

    def test_missing_and_oversized_bounds(self):
        self.assertIn("UNBOUNDED_RESULT", self.codes(self.report("default | where metric_name == 'x'")))
        self.assertIn(
            "RESULT_BOUND_TOO_LARGE",
            self.codes(self.report("default | where metric_name == 'x' | take 5000", max_rows=100)),
        )

    def test_wide_projection_sort_and_expensive_operators(self):
        r = self.report("default | project timestamp, body, resource | sort by timestamp desc")
        self.assertIn("WIDE_PROJECTION", self.codes(r))
        self.assertIn("SORT_BEFORE_FILTER", self.codes(r))
        self.assertIn("SORT_WITHOUT_BOUND", self.codes(r))
        r = self.report("default | where body matches regex 'timeout.*' | mv-expand resource | take 10")
        self.assertIn("EXPENSIVE_OPERATOR", self.codes(r))
        self.assertIn("RAW_CONTAINS_SCAN", self.codes(r))

    def test_selective_predicates_lower_risk_but_do_not_erase_expensive_operator(self):
        broad = self.report("default | mv-expand resource | take 10")
        narrow = self.report("default | where metric_name == 'x' | mv-expand resource | take 10")
        self.assertLess(narrow["score"], broad["score"])
        self.assertIn("EXPENSIVE_OPERATOR", self.codes(narrow))

    def test_unknown_field_suggestion_when_schema_supplied(self):
        r = self.report("default | where service_name == 'api' | take 5")
        self.assertIn("UNKNOWN_FIELD", self.codes(r))
        self.assertIn("resource['service.name']", r["findings"][0]["message"])
        r = self.report("default | where resource['service.nam'] == 'api' | take 5")
        self.assertIn("UNKNOWN_FIELD", self.codes(r))
        no_schema = self.report("default | where service_name == 'api' | take 5", schema_fields=None)
        self.assertNotIn("UNKNOWN_FIELD", self.codes(no_schema))

    def test_deterministic_ordering_and_malformed_inputs(self):
        a = self.report("default | sort by timestamp desc | project body")
        b = self.report("default | sort by timestamp desc | project body")
        self.assertEqual(a["score"], b["score"])
        self.assertEqual(self.codes(a), self.codes(b))
        r = self.report(None)
        self.assertFalse(r["valid"])
        self.assertIn("EMPTY_QUERY", self.codes(r))

    def test_stats_parser_valid_partial_malformed(self):
        parsed = kv.parse_cli_stats('{"rows_returned": 3, "rowsProcessed": 9, "bytesScanned": 12, "plan": "x"}')
        self.assertTrue(parsed["stats_available"])
        self.assertEqual(parsed["rows_returned"], 3)
        self.assertEqual(parsed["rows_processed"], 9)
        self.assertEqual(parsed["bytes_scanned"], 12)
        partial = kv.parse_cli_stats("rows returned: 4")
        self.assertTrue(partial["stats_available"])
        self.assertEqual(partial["rows_returned"], 4)
        malformed = kv.parse_cli_stats("not stats")
        self.assertFalse(malformed["stats_available"])
        self.assertIsNone(malformed["rows_returned"])


if __name__ == "__main__":
    unittest.main()
