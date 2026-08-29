#!/usr/bin/env python3
"""Tests for evals/run_eval.py's multi-turn eval mode (issue #75). Only the
pure fixture/message-building logic gets unit tests, matching this repo's
existing convention (test_run_eval_usage.py) of not unit-testing the
subprocess/network glue."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_eval  # noqa: E402


class BuildInvestigateHop1FixtureTest(unittest.TestCase):
    def test_returns_real_fenced_text_with_continuation_directive(self):
        text = run_eval.build_investigate_hop1_fixture(
            top_service="checkout", top_errors=700, since="1h ago")
        self.assertIn("<untrusted_log_data>", text)
        self.assertIn("</untrusted_log_data>", text)
        self.assertIn("checkout", text)
        # The round-3 Codex fix (PR #72): the continuation directive must
        # appear after the fence closes, and must not repeat the raw
        # service value -- see berserk_mcp.py's investigate_error_rate
        # dispatch. This fixture is only useful for issue #75's multi-turn
        # test if it reflects that real, current behavior.
        close_idx = text.index("</untrusted_log_data>")
        next_idx = text.index("Next: call investigate_error_rate")
        self.assertGreater(next_idx, close_idx)
        self.assertIn(
            "service=<the service value from the Result line above>", text)

    def test_different_service_and_error_count_are_reflected(self):
        text = run_eval.build_investigate_hop1_fixture(
            top_service="auth-service", top_errors=450, since="6h ago")
        self.assertIn("auth-service", text)
        self.assertIn("450 errors", text)
        self.assertIn("since=6h ago", text)

    def test_normal_rate_produces_no_continuation_directive(self):
        # A low error count should conclude "normal", not advance --
        # confirms the fixture builder isn't hardcoding the elevated path.
        text = run_eval.build_investigate_hop1_fixture(
            top_service="checkout", top_errors=1, since="1h ago")
        self.assertNotIn("Next: call investigate_error_rate", text)
        self.assertIn("normal", text.lower())


class BuildMultiTurnMessagesTest(unittest.TestCase):
    def test_openai_shape_has_tool_call_and_tool_result(self):
        messages = run_eval.build_multi_turn_messages(
            False, "why is checkout's error rate up?", "HOP1_TEXT")
        self.assertEqual(messages[0], {
            "role": "user", "content": "why is checkout's error rate up?"})
        self.assertEqual(messages[1]["role"], "assistant")
        call = messages[1]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "investigate_error_rate")
        self.assertEqual(call["function"]["arguments"], "{}")
        self.assertEqual(messages[2], {
            "role": "tool", "tool_call_id": call["id"], "content": "HOP1_TEXT"})

    def test_anthropic_shape_has_tool_use_and_tool_result_block(self):
        messages = run_eval.build_multi_turn_messages(
            True, "why is checkout's error rate up?", "HOP1_TEXT")
        self.assertEqual(messages[0], {
            "role": "user", "content": "why is checkout's error rate up?"})
        tool_use = messages[1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["name"], "investigate_error_rate")
        self.assertEqual(tool_use["input"], {})
        result_block = messages[2]["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertEqual(result_block["tool_use_id"], tool_use["id"])
        self.assertEqual(result_block["content"], "HOP1_TEXT")


class ScoreCaseMultiTurnShapeTest(unittest.TestCase):
    """score_case() is unchanged for multi-turn cases -- it already scores
    any expect_tool/expect_args generically. This locks that in."""

    def test_correct_continuation_call_scores_both_tool_and_args_ok(self):
        case = {
            "expect_tool": "investigate_error_rate",
            "expect_args": {"node": "check_log_spike", "service": "checkout"},
        }
        tool_ok, arg_ok = run_eval.score_case(
            case, "investigate_error_rate",
            {"node": "check_log_spike", "service": "checkout"})
        self.assertTrue(tool_ok)
        self.assertTrue(arg_ok)

    def test_wrong_service_value_fails_arg_scoring(self):
        case = {
            "expect_tool": "investigate_error_rate",
            "expect_args": {"node": "check_log_spike", "service": "checkout"},
        }
        tool_ok, arg_ok = run_eval.score_case(
            case, "investigate_error_rate",
            {"node": "check_log_spike", "service": "auth-service"})
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

    def test_no_tool_call_fails_tool_scoring(self):
        case = {
            "expect_tool": "investigate_error_rate",
            "expect_args": {"node": "check_log_spike", "service": "checkout"},
        }
        tool_ok, arg_ok = run_eval.score_case(case, None, {})
        self.assertFalse(tool_ok)


if __name__ == "__main__":
    unittest.main()
