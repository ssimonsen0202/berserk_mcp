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


class RunJsonTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_returns_parsed_rows_on_success(self):
        def fake_search(kql, since):
            self.calls.append((kql, since))
            return bzrk_json_table(["service", "errors"], [["checkout", 23]]), False
        inv.configure(
            bzrk_search=fake_search, since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [{"service": "checkout", "errors": 23}])
        self.assertEqual(self.calls, [("Q1", "1h ago")])

    def test_backend_error_returns_error_text_not_rows(self):
        inv.configure(
            bzrk_search=lambda kql, since: ("bzrk timed out", True),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertEqual(err, "bzrk timed out")

    def test_non_json_response_is_reported_not_silently_empty(self):
        inv.configure(
            bzrk_search=lambda kql, since: ("not json at all", False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertIn("unexpected non-JSON response", err)

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
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [])


class StartNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_normal_rate_concludes(self):
        # 5 errors over a 1h window = 5/60 per-minute, below the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 5]]), False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("normal", text.lower())

    def test_elevated_rate_advances_to_check_log_spike(self):
        # 700 errors over 1h = ~11.7/min, above the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700], ["auth", 3]]), False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_log_spike")
        self.assertIn("checkout", text)
        self.assertIn("check_log_spike", text)
        self.assertIn("service=\"checkout\"", text)

    def test_no_rows_concludes_nothing_to_investigate(self):
        self.responses["Q_ERRORS"] = ("(no rows)", False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no errors", text.lower())

    def test_backend_failure_halts_and_reports(self):
        self.responses["Q_ERRORS"] = ("bzrk timed out after 120s", True)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("FAILED", text)
        self.assertIn("bzrk timed out after 120s", text)


class CheckLogSpikeNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def _spike_response(self, service, hits):
        return bzrk_json_table(["service", "hits"], [[service, hits]]), False

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node = inv.run_error_rate_node("check_log_spike", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("service", text.lower())

    def test_spike_advances_to_check_traces(self):
        # 55 baseline buckets averaging ~2, last 5 buckets averaging 20 --
        # 20 > 2 * SPIKE_MULTIPLIER (3), so this is a spike.
        hits = [2] * 55 + [20] * 5
        self.responses["Q_SPIKE"] = self._spike_response("checkout", hits)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_traces")
        self.assertIn("check_traces", text)
        self.assertIn("service=\"checkout\"", text)

    def test_no_spike_concludes_with_manual_review_recommendation(self):
        hits = [2] * 60  # flat, no spike
        self.responses["Q_SPIKE"] = self._spike_response("checkout", hits)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("manual review", text.lower())

    def test_service_not_present_in_series_halts(self):
        self.responses["Q_SPIKE"] = self._spike_response("auth", [1] * 60)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no log-volume data", text.lower())

    def test_insufficient_buckets_halts(self):
        self.responses["Q_SPIKE"] = self._spike_response("checkout", [1, 2, 3])
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("insufficient", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_SPIKE"] = ("bzrk timed out", True)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("FAILED", text)


class CheckTracesNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node = inv.run_error_rate_node("check_traces", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)

    def test_failing_traces_found_concludes_with_root_cause_verdict(self):
        rows = [
            ["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"],
            ["t2", "GET /cart", "2026-08-28T00:01:00Z", "checkout"],
            ["t3", "POST /pay", "2026-08-28T00:02:00Z", "auth"],
        ]
        self.responses["Q_TRACE"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("2 failing", text)
        self.assertIn("checkout", text)
        self.assertNotIn("auth", text)  # filtered to the offending service

    def test_no_matching_traces_concludes_with_ingestion_gap_hypothesis(self):
        rows = [["t1", "GET /health", "2026-08-28T00:00:00Z", "other-service"]]
        self.responses["Q_TRACE"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no failing traces", text.lower())
        self.assertIn("ingestion lag", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_TRACE"] = ("bzrk timed out", True)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)


class FullWalkTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_full_walk_elevated_spike_traces_found(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [2] * 55 + [20] * 5]]), False)
        self.responses["Q_TRACE"] = (
            bzrk_json_table(
                ["trace_id", "span_name", "timestamp", "service"],
                [["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"]],
            ), False)

        text1, err1, next1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")

        text2, err2, next2 = inv.run_error_rate_node("check_log_spike", "1h ago", "checkout")
        self.assertFalse(err2)
        self.assertEqual(next2, "check_traces")

        text3, err3, next3 = inv.run_error_rate_node("check_traces", "1h ago", "checkout")
        self.assertFalse(err3)
        self.assertIsNone(next3)
        self.assertIn("root cause is likely", text3)

    def test_full_walk_elevated_but_no_spike_early_exit(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [5] * 60]]), False)

        _, err1, next1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")

        text2, err2, next2 = inv.run_error_rate_node("check_log_spike", "1h ago", "checkout")
        self.assertFalse(err2)
        self.assertIsNone(next2)
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
                    q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE",
                    q_trace_find_errors="Q_TRACE",
                )
                _, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
                self.assertFalse(is_error)
                self.assertEqual(next_node, "check_log_spike")


if __name__ == "__main__":
    unittest.main()
