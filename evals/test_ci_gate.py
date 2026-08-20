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


if __name__ == "__main__":
    unittest.main(verbosity=2)
