#!/usr/bin/env python3
"""Tests for evals/ci_gate.py's pure decision logic (issue #13). The
subprocess-invocation/file-discovery glue mirrors this repo's existing
convention (evals/mcp_protocol_smoke.py) of exercising integration scripts
directly rather than unit-testing them; only the threshold comparison --
the part a CI regression actually depends on being correct -- gets tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_gate  # noqa: E402


class CheckAccuracyTest(unittest.TestCase):
    def test_passes_at_exactly_the_threshold(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": 0.65}, min_accuracy=0.65)
        self.assertTrue(ok, msg)

    def test_passes_above_the_threshold(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": 0.90}, min_accuracy=0.65)
        self.assertTrue(ok, msg)

    def test_fails_below_the_threshold(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": 0.50}, min_accuracy=0.65)
        self.assertFalse(ok)
        self.assertIn("50", msg)
        self.assertIn("65", msg)

    def test_fails_closed_on_missing_field(self):
        # A malformed or empty results payload must not silently pass --
        # the whole point of a CI gate is to catch exactly this kind of
        # thing (e.g. run_eval.py changing its output schema).
        ok, msg = ci_gate.check_accuracy({}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_non_numeric_field(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": "not a number"}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_nan(self):
        # NaN compares false against everything, so a naive `accuracy <
        # min_accuracy` check silently passes a NaN score -- exactly the
        # kind of fail-open bug this gate exists to prevent.
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": float("nan")}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_positive_infinity(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": float("inf")}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_negative_infinity(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": float("-inf")}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_percentage_scaled_value(self):
        # A results payload that reports 65.85 (meaning 65.85%) instead of
        # 0.6585 must not slip through just because 65.85 < 0.65 is False.
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": 65.85}, min_accuracy=0.65)
        self.assertFalse(ok)

    def test_fails_closed_on_negative_value(self):
        ok, msg = ci_gate.check_accuracy({"tool_accuracy": -0.1}, min_accuracy=0.65)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
