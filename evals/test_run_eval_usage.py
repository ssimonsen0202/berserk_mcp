#!/usr/bin/env python3
"""Tests for evals/run_eval.py's usage/cost extraction (issue #37). Only the
pure per-call and aggregate logic gets unit tests, matching this repo's
existing convention (test_ci_gate.py) of not unit-testing the
subprocess/network glue -- that's covered by mcp_protocol_smoke.py-style
integration checks instead."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval  # noqa: E402


class UsageFieldsTest(unittest.TestCase):
    def test_empty_usage_gives_zero_tokens_and_null_cost(self):
        f = run_eval.usage_fields({})
        self.assertEqual(f["prompt_tokens"], 0)
        self.assertEqual(f["completion_tokens"], 0)
        self.assertIsNone(f["cost_usd"])
        self.assertIsNone(f["cached_tokens"])

    def test_none_usage_behaves_like_empty(self):
        f = run_eval.usage_fields(None)
        self.assertEqual(f["prompt_tokens"], 0)
        self.assertEqual(f["completion_tokens"], 0)
        self.assertIsNone(f["cost_usd"])
        self.assertIsNone(f["cached_tokens"])

    def test_openai_style_token_keys(self):
        f = run_eval.usage_fields({"prompt_tokens": 27330, "completion_tokens": 12})
        self.assertEqual(f["prompt_tokens"], 27330)
        self.assertEqual(f["completion_tokens"], 12)

    def test_anthropic_style_token_keys(self):
        f = run_eval.usage_fields({"input_tokens": 500, "output_tokens": 30})
        self.assertEqual(f["prompt_tokens"], 500)
        self.assertEqual(f["completion_tokens"], 30)

    def test_openai_keys_take_priority_over_anthropic_keys_when_both_present(self):
        # A backend should never send both, but if it did, prefer the
        # explicit key already used elsewhere in this file's fallback chain.
        f = run_eval.usage_fields({"prompt_tokens": 10, "input_tokens": 999})
        self.assertEqual(f["prompt_tokens"], 10)

    def test_real_cost_field_is_extracted(self):
        f = run_eval.usage_fields({"prompt_tokens": 100, "cost": 0.00056052})
        self.assertEqual(f["cost_usd"], 0.00056052)

    def test_cached_tokens_extracted_from_nested_prompt_tokens_details(self):
        f = run_eval.usage_fields({
            "prompt_tokens": 27327,
            "prompt_tokens_details": {"cached_tokens": 27296},
        })
        self.assertEqual(f["cached_tokens"], 27296)

    def test_missing_prompt_tokens_details_does_not_crash(self):
        f = run_eval.usage_fields({"prompt_tokens": 100})
        self.assertIsNone(f["cached_tokens"])

    def test_partial_usage_defaults_missing_token_field_to_zero(self):
        f = run_eval.usage_fields({"prompt_tokens": 50})
        self.assertEqual(f["completion_tokens"], 0)


class AggregateUsageTest(unittest.TestCase):
    def test_empty_rows_gives_zero_tokens_and_null_cost(self):
        agg = run_eval.aggregate_usage([])
        self.assertEqual(agg["total_input_tokens"], 0)
        self.assertEqual(agg["total_output_tokens"], 0)
        self.assertIsNone(agg["total_cost_usd"])

    def test_sums_tokens_and_cost_across_rows(self):
        rows = [
            {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.001},
            {"prompt_tokens": 200, "completion_tokens": 20, "cost_usd": 0.002},
        ]
        agg = run_eval.aggregate_usage(rows)
        self.assertEqual(agg["total_input_tokens"], 300)
        self.assertEqual(agg["total_output_tokens"], 30)
        self.assertAlmostEqual(agg["total_cost_usd"], 0.003)

    def test_no_row_reporting_cost_gives_null_not_zero(self):
        # A backend that never reports cost (local/Anthropic-direct) must not
        # be misread as "this run was free" -- null means "unknown", not $0.
        rows = [
            {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": None},
            {"prompt_tokens": 200, "completion_tokens": 20, "cost_usd": None},
        ]
        agg = run_eval.aggregate_usage(rows)
        self.assertIsNone(agg["total_cost_usd"])

    def test_mixed_cost_reporting_sums_only_the_rows_that_have_it(self):
        rows = [
            {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.005},
            {"prompt_tokens": 200, "completion_tokens": 20, "cost_usd": None},
        ]
        agg = run_eval.aggregate_usage(rows)
        self.assertAlmostEqual(agg["total_cost_usd"], 0.005)
        self.assertEqual(agg["total_input_tokens"], 300)


if __name__ == "__main__":
    unittest.main()
