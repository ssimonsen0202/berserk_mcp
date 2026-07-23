import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals" / "fleet"))

import cache_sim  # noqa: E402
import cooldown_sim  # noqa: E402
import jitter_sim  # noqa: E402
import latency_eval  # noqa: E402


class FleetEvalPureTest(unittest.TestCase):
    def test_jitter_is_deterministic_and_recommends_bounded_value(self):
        a = jitter_sim.run_sweep(trials=1000)
        b = jitter_sim.run_sweep(trials=1000)
        self.assertEqual(a, b)
        self.assertIn(a["recommendation_seconds"], (0, 60, 300, 600, 1800, 3600, 7200))

    def test_cache_ttl_only_hits_identical_tool_and_args(self):
        trace = [
            {"ts": 0, "tool": "a", "args_hash": "x"},
            {"ts": 5, "tool": "a", "args_hash": "x"},
            {"ts": 5, "tool": "a", "args_hash": "y"},
        ]
        result = cache_sim.replay(trace, 10)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["cluster_calls_avoided"], 1)

    def test_cooldown_does_not_delay_changed_args(self):
        trace = cooldown_sim.synthetic_trace()
        result = cooldown_sim.replay(trace, 30)
        self.assertGreaterEqual(result["storm_calls_absorbed"], 4)
        self.assertEqual(result["legitimate_retry_delay"], 0)

    def test_latency_recommendation_rule(self):
        self.assertEqual(latency_eval.percentile([1, 2, 3, 4], .95), 4)


if __name__ == "__main__":
    unittest.main()
