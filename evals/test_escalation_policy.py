"""Unit tests for escalation_policy — pure logic, no network/I/O."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import escalation_policy as ep


class TestShouldEscalate(unittest.TestCase):

    # ── no tool call → always escalate ────────────────────────────────────────
    def test_none_tool_escalates(self):
        d = ep.should_escalate(None, {})
        self.assertTrue(d.escalated)
        self.assertEqual(d.handled_by, "deep")
        self.assertIn("no tool call", d.reason)

    # ── normal confident routing → small ──────────────────────────────────────
    def test_good_routing_stays_small(self):
        d = ep.should_escalate("claude_cost_report", {"group_by": "project"})
        self.assertFalse(d.escalated)
        self.assertEqual(d.handled_by, "small")

    def test_good_routing_with_confidence_1(self):
        d = ep.should_escalate("list_containers", {}, confidence=1.0)
        self.assertFalse(d.escalated)

    # ── deep-only tools ────────────────────────────────────────────────────────
    def test_deep_only_tool_escalates(self):
        import escalation_policy as ep2
        orig = ep2.DEEP_ONLY_TOOLS
        ep2.DEEP_ONLY_TOOLS = frozenset({"my_synthesis_tool"})
        try:
            d = ep2.should_escalate("my_synthesis_tool", {})
            self.assertTrue(d.escalated)
            self.assertIn("deep-only", d.reason)
        finally:
            ep2.DEEP_ONLY_TOOLS = orig

    def test_non_deep_only_tool_not_escalated(self):
        d = ep.should_escalate("claude_workflow_insights", {})
        self.assertFalse(d.escalated)

    # ── confidence threshold ──────────────────────────────────────────────────
    def test_low_confidence_escalates(self):
        d = ep.should_escalate("errors_by_service", {}, confidence=0.3)
        self.assertTrue(d.escalated)
        self.assertIn("confidence", d.reason)

    def test_exact_threshold_not_escalated(self):
        # confidence == threshold is NOT escalated (strict less-than)
        d = ep.should_escalate("list_services", {}, confidence=ep.LOW_CONFIDENCE_THRESHOLD)
        self.assertFalse(d.escalated)

    def test_above_threshold_not_escalated(self):
        d = ep.should_escalate("top_cpu", {}, confidence=ep.LOW_CONFIDENCE_THRESHOLD + 0.01)
        self.assertFalse(d.escalated)

    # ── force_escalate flag ────────────────────────────────────────────────────
    def test_force_escalate_overrides_good_call(self):
        d = ep.should_escalate("top_memory", {}, confidence=1.0, force_escalate=True)
        self.assertTrue(d.escalated)
        self.assertIn("force_escalate", d.reason)

    def test_force_escalate_overrides_none(self):
        d = ep.should_escalate(None, {}, force_escalate=True)
        self.assertTrue(d.escalated)

    # ── result is immutable dataclass ─────────────────────────────────────────
    def test_decision_is_frozen(self):
        d = ep.should_escalate("list_hosts", {})
        with self.assertRaises((AttributeError, TypeError)):
            d.escalated = True  # type: ignore[misc]


class TestTierForCase(unittest.TestCase):

    def test_small_tier_label(self):
        self.assertEqual(ep.tier_for_case({"tier": "small"}), "small")

    def test_deep_tier_label(self):
        self.assertEqual(ep.tier_for_case({"tier": "deep"}), "deep")

    def test_missing_tier_defaults_to_deep(self):
        self.assertEqual(ep.tier_for_case({}), "deep")

    def test_unknown_tier_passes_through(self):
        self.assertEqual(ep.tier_for_case({"tier": "medium"}), "medium")


if __name__ == "__main__":
    unittest.main()
