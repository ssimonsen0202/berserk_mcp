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


if __name__ == "__main__":
    unittest.main()
