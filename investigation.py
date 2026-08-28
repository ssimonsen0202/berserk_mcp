"""SRE fault-isolation decision trees (issue #24).

Fixed, hardcoded per-intent trees -- no agent-authored composition, same
reproducibility posture as every other tool in this project. This module
is stdlib-only and configured by berserk_mcp.py at import time. It does
not import berserk_mcp directly, which keeps tests simple and avoids
cycles (same convention as agent_analytics.py and parser_factory.py).

Design: docs/investigation-decision-tree-implementation-spec.md
"""
import json

import agent_analytics

_bzrk_search = None
_since_hours = None
_q_errors = None
_q_soc_log_spike = None
_q_trace_find_errors = None

# From primers/sre.md's "Escalation thresholds" table. Single source of
# truth for this module; the primer's prose copy is not auto-synced
# (spec's documented follow-up, not fixed here).
ERROR_RATE_INVESTIGATE_PER_MIN = 10

# How many trailing buckets of soc_log_spike's per-minute series count as
# "recent" vs. baseline, and how many multiples of the baseline mean counts
# as a spike. Conservative starting values -- no real-traffic tuning data
# exists yet; revisit once this tree has run against live incidents.
_RECENT_BUCKET_COUNT = 5
_SPIKE_MULTIPLIER = 3
_MIN_SPIKE_BUCKETS = _RECENT_BUCKET_COUNT * 2  # need real baseline, not just recent
_MAX_EXAMPLE_TRACES = 3


def configure(bzrk_search, since_hours, q_errors, q_soc_log_spike, q_trace_find_errors):
    """bzrk_search: callable(kql, since) -> (json_text, is_error), the same
    bzrk_search_json berserk_mcp.py wires into agent_analytics. since_hours:
    callable(since_str) -> float hours, berserk_mcp.py's own _since_hours
    (passed in rather than imported, to avoid the cycle). q_*: the exact
    KQL constants berserk_mcp.py already defines for errors_by_service,
    soc_log_spike, and trace_find_errors -- reused verbatim so this tree's
    queries never drift from the fixed tools' own."""
    global _bzrk_search, _since_hours, _q_errors, _q_soc_log_spike, _q_trace_find_errors
    _bzrk_search = bzrk_search
    _since_hours = since_hours
    _q_errors = q_errors
    _q_soc_log_spike = q_soc_log_spike
    _q_trace_find_errors = q_trace_find_errors


def _run_json(kql, since):
    """Run one KQL query in JSON mode and return (rows, error_text).

    rows is a list of dicts on success (empty list for a genuinely empty
    result, not an error), None on any failure. Reuses
    agent_analytics._json_records for the same Tables[0].schema/rows
    parsing every other analytics module in this project already relies
    on -- never hand-rolls a second parser for the same wire shape.
    Branch logic downstream always operates on these structured values,
    never on a fixed tool's own formatted display text."""
    out, is_err = _bzrk_search(kql, since)
    if is_err:
        return None, out
    # bzrk's --json mode still returns the plain text sentinel "(no rows)"
    # for a genuinely empty result, not an empty JSON array -- the same
    # convention agent_analytics._parse_rows already handles explicitly.
    # Without this check, an empty result would be misreported as
    # "unexpected non-JSON response" and halt the investigation instead
    # of correctly concluding "no errors, nothing to investigate."
    if str(out or "").strip() == "(no rows)":
        return [], None
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return None, f"unexpected non-JSON response: {out[:200]}"
    records = agent_analytics._json_records(parsed)
    if records is None:
        return None, f"unrecognized response shape: {out[:200]}"
    return records, None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _node_start(since):
    rows, err = _run_json(_q_errors, since)
    if err is not None:
        return (
            f"Checked: errors_by_service (since={since}) — FAILED\n"
            f"Error: {err}\n"
            f"Investigation halted at start.",
            True, None,
        )
    if not rows:
        return (
            f"Checked: errors_by_service (since={since})\n"
            f"Result: no errors\n"
            f"Investigation complete.\n"
            f"Verdict: no errors in window, nothing to investigate.",
            False, None,
        )
    top = max(rows, key=lambda r: _as_int(r.get("errors")))
    service = str(top.get("service") or "(unknown)")
    count = _as_int(top.get("errors"))
    hours = _since_hours(since)
    rate = (count / (60.0 * hours)) if hours > 0 else 0.0
    if rate <= ERROR_RATE_INVESTIGATE_PER_MIN:
        return (
            f"Checked: errors_by_service (since={since})\n"
            f"Result: worst service is {service!r} at {count} errors "
            f"(~{rate:.1f}/min)\n"
            f"Threshold: >{ERROR_RATE_INVESTIGATE_PER_MIN}/min to investigate\n"
            f"Investigation complete.\n"
            f"Verdict: error rate normal, no further checks.",
            False, None,
        )
    return (
        f"Checked: errors_by_service (since={since})\n"
        f"Result: {count} errors for service {service!r} (~{rate:.1f}/min)\n"
        f"Threshold: >{ERROR_RATE_INVESTIGATE_PER_MIN}/min investigate\n"
        f"Branch: investigate (elevated)\n"
        f"Next: call investigate_error_rate(node=\"check_log_spike\", "
        f"since={since!r}, service=\"{service}\") to continue, or stop "
        f"here if this is enough.",
        False, "check_log_spike",
    )


def _node_check_log_spike(since, service):
    if not service:
        return (
            "check_log_spike requires service (pass the value the start "
            "node's response gave you).",
            True, None,
        )
    rows, err = _run_json(_q_soc_log_spike, since)
    if err is not None:
        return (
            f"Checked: soc_log_spike (since={since}) — FAILED\n"
            f"Error: {err}\n"
            f"Investigation halted at check_log_spike. The error-rate "
            f"elevation from the previous step is still valid; this "
            f"step's result is unknown, not \"no spike.\"",
            True, None,
        )
    row = next((r for r in rows if str(r.get("service")) == service), None)
    if row is None:
        return (
            f"Checked: soc_log_spike (since={since})\n"
            f"No log-volume data for service={service!r} in this window "
            f"— FAILED\n"
            f"Investigation halted at check_log_spike.",
            True, None,
        )
    hits = row.get("hits")
    if not isinstance(hits, list) or len(hits) < _MIN_SPIKE_BUCKETS:
        return (
            f"Checked: soc_log_spike (since={since})\n"
            f"Result: insufficient buckets for service={service!r} to "
            f"assess a spike (need >= {_MIN_SPIKE_BUCKETS}) — FAILED\n"
            f"Investigation halted at check_log_spike.",
            True, None,
        )
    recent = hits[-_RECENT_BUCKET_COUNT:]
    baseline = hits[:-_RECENT_BUCKET_COUNT]
    recent_mean = sum(recent) / len(recent)
    baseline_mean = sum(baseline) / len(baseline)
    is_spike = (
        recent_mean > 0 if baseline_mean == 0
        else recent_mean > baseline_mean * _SPIKE_MULTIPLIER
    )
    if not is_spike:
        return (
            f"Checked: soc_log_spike (since={since})\n"
            f"Result: service={service!r} recent volume {recent_mean:.1f}/min "
            f"vs baseline {baseline_mean:.1f}/min — not a spike "
            f"(threshold {_SPIKE_MULTIPLIER}x)\n"
            f"Investigation complete.\n"
            f"Verdict: error rate elevated for {service!r} but no "
            f"correlated log-volume spike; recommend manual review.",
            False, None,
        )
    return (
        f"Checked: soc_log_spike (since={since})\n"
        f"Result: service={service!r} recent volume {recent_mean:.1f}/min "
        f"vs baseline {baseline_mean:.1f}/min — spike confirmed\n"
        f"Branch: correlated spike\n"
        f"Next: call investigate_error_rate(node=\"check_traces\", "
        f"since={since!r}, service=\"{service}\") to continue.",
        False, "check_traces",
    )


def _node_check_traces(since, service):
    if not service:
        return (
            "check_traces requires service (pass the value the previous "
            "step's response gave you).",
            True, None,
        )
    rows, err = _run_json(_q_trace_find_errors, since)
    if err is not None:
        return (
            f"Checked: trace_find_errors (since={since}) — FAILED\n"
            f"Error: {err}\n"
            f"Investigation halted at check_traces.",
            True, None,
        )
    matching = [r for r in rows if str(r.get("service")) == service]
    if not matching:
        return (
            f"Checked: trace_find_errors (since={since})\n"
            f"Result: no failing traces found for service={service!r}\n"
            f"Investigation complete.\n"
            f"Verdict: error rate elevated, correlated log-volume spike "
            f"confirmed for {service!r}, but no failing traces found — "
            f"investigate ingestion lag or a non-trace-instrumented "
            f"failure path.",
            False, None,
        )
    examples = "; ".join(
        f"{r.get('span_name')} ({r.get('trace_id')})"
        for r in matching[:_MAX_EXAMPLE_TRACES]
    )
    return (
        f"Checked: trace_find_errors (since={since})\n"
        f"Result: {len(matching)} failing traces found for "
        f"service={service!r}: {examples}\n"
        f"Investigation complete.\n"
        f"Verdict: error rate elevated, correlated log-volume spike "
        f"confirmed, {len(matching)} failing traces found for "
        f"{service!r} — root cause is likely in {service!r}'s own "
        f"request path, not a downstream dependency.",
        False, None,
    )


def run_error_rate_node(node, since, service):
    """Execute exactly one hop of the elevated-error-rate tree. Returns
    (text, is_error, next_node) -- next_node is None at every terminal
    state (concluded or halted)."""
    if node == "start":
        return _node_start(since)
    if node == "check_log_spike":
        return _node_check_log_spike(since, service)
    if node == "check_traces":
        return _node_check_traces(since, service)
    return (
        f"Unknown node {node!r}. Call investigate_error_rate with no "
        f"node argument (or node=\"start\") to begin a new investigation.",
        True, None,
    )
