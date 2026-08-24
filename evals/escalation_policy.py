"""Two-tier escalation policy — pure logic, no I/O.

Determines whether a router interaction should be handled by the small
(4-9B) model tier or escalated to the deep (largest) model tier.

Escalation triggers (any one is sufficient):
  - tool_call is None (model refused / didn't fire a tool)
  - tool_call is in DEEP_ONLY_TOOLS (always requires deep synthesis)
  - arg_confidence below threshold (small model picked a tool but args look wrong)
  - explicit escalation flags in the result

Policy constants are module-level so callers can override in tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tools that always require deep-tier handling regardless of confidence
DEEP_ONLY_TOOLS: frozenset[str] = frozenset()

# Confidence threshold below which small-tier results are escalated
LOW_CONFIDENCE_THRESHOLD: float = 0.5

# Maximum retries before hard-failing (used by the harness, not this module)
MAX_SMALL_RETRIES: int = 1


@dataclass(frozen=True)
class RoutingDecision:
    handled_by: str           # "small" | "deep"
    escalated: bool
    reason: str               # human-readable explanation


def should_escalate(
    tool_call: str | None,
    args: dict[str, Any],
    confidence: float = 1.0,
    *,
    force_escalate: bool = False,
) -> RoutingDecision:
    """Decide whether a small-tier result should be escalated to the deep tier.

    Args:
        tool_call: Tool name the small model chose, or None if it didn't call one.
        args: Arguments the small model provided (may be empty).
        confidence: Caller-supplied confidence [0.0, 1.0]. Use 1.0 when the
            backend doesn't report logprobs (conservative: assume confident).
        force_escalate: Hard-override — always escalate regardless of result.

    Returns:
        RoutingDecision with handled_by, escalated, and reason.
    """
    if force_escalate:
        return RoutingDecision(
            handled_by="deep",
            escalated=True,
            reason="force_escalate flag set",
        )

    if tool_call is None:
        return RoutingDecision(
            handled_by="deep",
            escalated=True,
            reason="small model produced no tool call",
        )

    if tool_call in DEEP_ONLY_TOOLS:
        return RoutingDecision(
            handled_by="deep",
            escalated=True,
            reason=f"tool '{tool_call}' is deep-only",
        )

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return RoutingDecision(
            handled_by="deep",
            escalated=True,
            reason=f"confidence {confidence:.2f} below threshold {LOW_CONFIDENCE_THRESHOLD}",
        )

    return RoutingDecision(
        handled_by="small",
        escalated=False,
        reason="small model routed with sufficient confidence",
    )


def tier_for_case(case: dict[str, Any]) -> str:
    """Return the expected handling tier for a labelled eval case.

    Cases with "tier": "small" are expected to be handled by the small model.
    All other cases (no tier field, or "tier": "deep") default to "deep".
    """
    return case.get("tier", "deep")
