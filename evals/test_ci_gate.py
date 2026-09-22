#!/usr/bin/env python3
"""Tests for evals/ci_gate.py (issue #13): the threshold decision logic,
plus the results-file discovery glue, which once failed spuriously when a
concurrent mock run wrote a same-second filename into evals/results/."""

import json
import subprocess
import sys
import tempfile
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


def _out_path(cmd):
    return Path(cmd[cmd.index("--out") + 1])


class RunEvalAndLoadResultsTest(unittest.TestCase):
    def test_reads_its_own_output_despite_a_concurrent_same_second_run(self):
        # Regression: a developer's mock run landing in the same second wrote
        # mock_mock-<stamp>.json into evals/results/ first, so the gate's own
        # file "already existed" in its before-snapshot and the gate failed
        # with "did not produce a new results file". The gate must now read
        # exactly the file it asked for, never a neighbour's report.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shared_dir = Path(tmp.name)
        decoy = shared_dir / "mock_mock-20260922-120000.json"

        def fake_run(cmd, **kwargs):
            out = _out_path(cmd)
            self.assertFalse(out.exists(), "gate must pass a fresh, unused --out path")
            decoy.write_text(json.dumps({"tool_accuracy": 0.10}))
            out.write_text(json.dumps({"tool_accuracy": 0.90}))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        results = ci_gate._run_eval_and_load_results(run=fake_run)
        self.assertEqual(results, {"tool_accuracy": 0.90})

    def test_fails_closed_when_no_results_file_is_written(self):
        # run_eval.py exiting 0 without writing --out (e.g. it wrote to the
        # shared dir instead) must fail the gate, not pass it.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self.assertRaises(SystemExit) as ctx:
            ci_gate._run_eval_and_load_results(run=fake_run)
        self.assertIn("did not produce a results file", str(ctx.exception.code))

    def test_fails_closed_on_nonzero_exit_even_if_file_written(self):
        def fake_run(cmd, **kwargs):
            _out_path(cmd).write_text(json.dumps({"tool_accuracy": 0.90}))
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        with self.assertRaises(SystemExit) as ctx:
            ci_gate._run_eval_and_load_results(run=fake_run)
        self.assertIn("exited 1", str(ctx.exception.code))

    def test_run_eval_writes_exactly_the_out_path(self):
        # End-to-end wiring: the real run_eval.py honours --out, so the fake
        # runners above model its actual behaviour.
        results = ci_gate._run_eval_and_load_results()
        self.assertIn("tool_accuracy", results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
