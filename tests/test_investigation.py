import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import investigation as inv  # noqa: E402


def bzrk_json_table(columns, rows):
    """Build the exact bzrk --json shape agent_analytics._json_records
    parses: {"Tables": [{"schema": {"columns": [...]}, "rows": [[...]]}]}."""
    return json.dumps({"Tables": [{
        "schema": {"columns": [{"name": c} for c in columns]},
        "rows": rows,
    }]})


def _fixed(kql):
    """A q_soc_log_spike/q_trace_find_errors stand-in that ignores the
    service argument and always builds the same fixed query string --
    used by tests that don't care about query scoping."""
    return lambda service: kql


class RunJsonTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_returns_parsed_rows_on_success(self):
        def fake_search(kql, since):
            self.calls.append((kql, since))
            return bzrk_json_table(["service", "errors"], [["checkout", 23]]), False
        inv.configure(
            bzrk_search=fake_search, since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike=_fixed("Q2"), q_trace_find_errors=_fixed("Q3"),
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [{"service": "checkout", "errors": 23}])
        self.assertEqual(self.calls, [("Q1", "1h ago")])

    def test_backend_error_returns_error_text_not_rows(self):
        inv.configure(
            bzrk_search=lambda kql, since: ("bzrk timed out", True),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike=_fixed("Q2"), q_trace_find_errors=_fixed("Q3"),
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertEqual(err, "bzrk timed out")

    def test_non_json_response_is_reported_not_silently_empty(self):
        inv.configure(
            bzrk_search=lambda kql, since: ('{"not": "a recognizable shape"}', False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike=_fixed("Q2"), q_trace_find_errors=_fixed("Q3"),
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertIn("unrecognized response shape", err)

    def test_no_rows_sentinel_returns_empty_list_not_an_error(self):
        # bzrk's --json mode still returns the plain-text "(no rows)"
        # sentinel for a genuinely empty result, not an empty JSON array.
        # This must be treated as success-with-zero-rows, not a parse
        # failure -- caught during implementation verification, not
        # written speculatively (a real bug in an earlier draft of this
        # code halted every empty-result investigation with "unexpected
        # non-JSON response" instead of concluding cleanly).
        inv.configure(
            bzrk_search=lambda kql, since: ("(no rows)", False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike=_fixed("Q2"), q_trace_find_errors=_fixed("Q3"),
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [])

    def test_legacy_non_json_table_fallback_is_reported_distinctly(self):
        # Codex review finding, 2026-08-28: bzrk_search_json falls back to
        # plain aligned-table text on older bzrk builds that reject
        # --json. That's a real, documented compatibility path, not
        # corruption -- the halt message must say so plainly rather than
        # "unexpected non-JSON response", which reads like backend
        # corruption instead of a version mismatch.
        legacy_table = (
            "service    hits\n"
            "checkout   42\n"
        )
        inv.configure(
            bzrk_search=lambda kql, since: (legacy_table, False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike=_fixed("Q2"), q_trace_find_errors=_fixed("Q3"),
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertIn("non-JSON response", err)
        self.assertIn("--json", err)


class StartNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike=_fixed("Q_SPIKE"),
            q_trace_find_errors=_fixed("Q_TRACE"),
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_normal_rate_concludes(self):
        # 5 errors over a 1h window = 5/60 per-minute, below the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 5]]), False)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIsNone(next_service)
        self.assertIn("normal", text.lower())

    def test_elevated_rate_advances_to_check_log_spike(self):
        # 700 errors over 1h = ~11.7/min, above the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700], ["auth", 3]]), False)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_log_spike")
        self.assertEqual(next_service, "checkout")
        self.assertIn("checkout", text)

    def test_no_rows_concludes_nothing_to_investigate(self):
        self.responses["Q_ERRORS"] = ("(no rows)", False)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIsNone(next_service)
        self.assertIn("no errors", text.lower())

    def test_backend_failure_halts_and_reports(self):
        self.responses["Q_ERRORS"] = ("bzrk timed out after 120s", True)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "start", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIsNone(next_service)
        self.assertIn("FAILED", text)
        self.assertIn("bzrk timed out after 120s", text)


class CheckLogSpikeNodeTest(unittest.TestCase):
    def setUp(self):
        self.built_queries = []
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike=self._q_soc_log_spike,
            q_trace_find_errors=_fixed("Q_TRACE"),
        )
        self.responses = {}

    def _q_soc_log_spike(self, service):
        kql = f"Q_SPIKE[{service}]"
        self.built_queries.append(kql)
        return kql

    def _search(self, kql, since):
        return self.responses[kql]

    def _spike_response(self, kql, service, hits):
        self.responses[kql] = bzrk_json_table(["service", "hits"], [[service, hits]]), False

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("service", text.lower())

    def test_query_is_scoped_to_the_requested_service(self):
        # Codex review finding (P1), 2026-08-28: a service whose rows fall
        # outside an *unscoped* query's global cap silently reads as "no
        # data". The fix is to build a service-scoped query, not just
        # filter its result -- assert the query builder actually receives
        # the service.
        hits = [2] * 55 + [20] * 5
        self._spike_response("Q_SPIKE[checkout]", "checkout", hits)
        inv.run_error_rate_node("check_log_spike", "1h ago", "checkout")
        self.assertEqual(self.built_queries, ["Q_SPIKE[checkout]"])

    def test_spike_advances_to_check_traces(self):
        # 55 baseline buckets averaging ~2, last 5 buckets averaging 20 --
        # 20 > 2 * SPIKE_MULTIPLIER (3), so this is a spike.
        hits = [2] * 55 + [20] * 5
        self._spike_response("Q_SPIKE[checkout]", "checkout", hits)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_traces")
        self.assertEqual(next_service, "checkout")

    def test_no_spike_concludes_with_manual_review_recommendation(self):
        hits = [2] * 60  # flat, no spike
        self._spike_response("Q_SPIKE[checkout]", "checkout", hits)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIsNone(next_service)
        self.assertIn("manual review", text.lower())

    def test_service_not_present_in_series_halts(self):
        self._spike_response("Q_SPIKE[checkout]", "auth", [1] * 60)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no log-volume data", text.lower())

    def test_insufficient_buckets_halts(self):
        self._spike_response("Q_SPIKE[checkout]", "checkout", [1, 2, 3])
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("insufficient", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_SPIKE[checkout]"] = ("bzrk timed out", True)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("FAILED", text)


class CheckTracesNodeTest(unittest.TestCase):
    def setUp(self):
        self.built_queries = []
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike=_fixed("Q_SPIKE"),
            q_trace_find_errors=self._q_trace_find_errors,
        )
        self.responses = {}

    def _q_trace_find_errors(self, service):
        kql = f"Q_TRACE[{service}]"
        self.built_queries.append(kql)
        return kql

    def _search(self, kql, since):
        return self.responses[kql]

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_traces", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)

    def test_query_is_scoped_to_the_requested_service(self):
        # Codex review finding (P1), 2026-08-28: the unscoped query's
        # global `tail 20` can drop the target service's failing spans
        # before the Python-side filter ever runs. The fix scopes the
        # query itself.
        rows = [["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"]]
        self.responses["Q_TRACE[checkout]"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        inv.run_error_rate_node("check_traces", "1h ago", "checkout")
        self.assertEqual(self.built_queries, ["Q_TRACE[checkout]"])

    def test_failing_traces_found_concludes_with_root_cause_verdict(self):
        rows = [
            ["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"],
            ["t2", "GET /cart", "2026-08-28T00:01:00Z", "checkout"],
        ]
        self.responses["Q_TRACE[checkout]"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("2 failing", text)
        self.assertIn("checkout", text)

    def test_duplicate_trace_id_spans_counted_once(self):
        # Codex review finding (P2), 2026-08-28: Q_TRACE_FIND_ERRORS
        # returns one row per error *span*; multiple spans can share a
        # trace_id. Counting rows overcounts distinct traces and can
        # repeat the same trace_id in the example list.
        rows = [
            ["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"],
            ["t1", "POST /checkout/retry", "2026-08-28T00:00:01Z", "checkout"],
            ["t2", "GET /cart", "2026-08-28T00:01:00Z", "checkout"],
        ]
        self.responses["Q_TRACE[checkout]"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIn("2 failing", text)
        self.assertNotIn("3 failing", text)
        # The example list must not repeat trace_id "t1" twice.
        self.assertEqual(text.count("(t1)"), 1)

    def test_no_matching_traces_concludes_with_ingestion_gap_hypothesis(self):
        self.responses["Q_TRACE[checkout]"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], []), False
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no failing traces", text.lower())
        self.assertIn("ingestion lag", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_TRACE[checkout]"] = ("bzrk timed out", True)
        text, is_error, next_node, next_service = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)


class FullWalkTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike=lambda service: f"Q_SPIKE[{service}]",
            q_trace_find_errors=lambda service: f"Q_TRACE[{service}]",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_full_walk_elevated_spike_traces_found(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE[checkout]"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [2] * 55 + [20] * 5]]), False)
        self.responses["Q_TRACE[checkout]"] = (
            bzrk_json_table(
                ["trace_id", "span_name", "timestamp", "service"],
                [["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"]],
            ), False)

        text1, err1, next1, svc1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")
        self.assertEqual(svc1, "checkout")

        text2, err2, next2, svc2 = inv.run_error_rate_node(
            "check_log_spike", "1h ago", svc1)
        self.assertFalse(err2)
        self.assertEqual(next2, "check_traces")
        self.assertEqual(svc2, "checkout")

        text3, err3, next3, svc3 = inv.run_error_rate_node(
            "check_traces", "1h ago", svc2)
        self.assertFalse(err3)
        self.assertIsNone(next3)
        self.assertIsNone(svc3)
        self.assertIn("root cause is likely", text3)

    def test_full_walk_elevated_but_no_spike_early_exit(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE[checkout]"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [5] * 60]]), False)

        _, err1, next1, svc1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")

        text2, err2, next2, svc2 = inv.run_error_rate_node(
            "check_log_spike", "1h ago", svc1)
        self.assertFalse(err2)
        self.assertIsNone(next2)
        self.assertIsNone(svc2)
        self.assertIn("manual review", text2.lower())


class DisplayFormatIndependenceTest(unittest.TestCase):
    """Proves the architecture decision (branch logic never depends on a
    fixed tool's display-formatted text) actually holds, not just that
    it's stated as intent (spec's own required regression test)."""

    def test_branch_logic_unaffected_by_reformatted_but_same_structured_value(self):
        # Two structurally-identical responses that would render as very
        # different display text if a fixed tool changed its formatting
        # (different key order, extra whitespace) but carry the same
        # decision-relevant value. Branch outcome must be identical.
        variant_a = json.dumps({"Tables": [{
            "schema": {"columns": [{"name": "service"}, {"name": "errors"}]},
            "rows": [["checkout", 700]],
        }]})
        variant_b = json.dumps({"Tables": [{
            "schema": {"columns": [{"name": "errors"}, {"name": "service"}]},
            "rows": [[700, "checkout"]],
        }]}, indent=4)  # different formatting, different column order

        for variant in (variant_a, variant_b):
            with self.subTest(variant=variant[:30]):
                inv.configure(
                    bzrk_search=lambda kql, since, v=variant: (v, False),
                    since_hours=lambda s: 1.0,
                    q_errors="Q_ERRORS", q_soc_log_spike=_fixed("Q_SPIKE"),
                    q_trace_find_errors=_fixed("Q_TRACE"),
                )
                _, is_error, next_node, next_service = inv.run_error_rate_node(
                    "start", "1h ago", None)
                self.assertFalse(is_error)
                self.assertEqual(next_node, "check_log_spike")
                self.assertEqual(next_service, "checkout")


if __name__ == "__main__":
    unittest.main()
