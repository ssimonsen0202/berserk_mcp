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
    (passed in rather than imported, to avoid the cycle). q_errors: the
    exact KQL constant berserk_mcp.py already defines for errors_by_service
    (fixed string -- the start node has no service to scope by yet; it's
    the one finding it). q_soc_log_spike, q_trace_find_errors: callables
    service -> kql, scoped to the one service this hop cares about (Codex
    review finding, 2026-08-28: the fixed tools' own unscoped queries cap
    at a global `take`/`tail` before this module's Python-side service
    filter ever runs, so a service outside that global top-N silently
    reads as "no data" even when it has real matching rows -- scoping the
    query itself, not just post-filtering its already-truncated result,
    is the actual fix)."""
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
    if not str(out or "").strip()[:1] in "[{":
        # Codex review finding, 2026-08-28: bzrk_search_json deliberately
        # falls back to plain aligned-table text on older bzrk builds that
        # reject --json (see its own docstring in berserk_mcp.py). That
        # fallback is not a corrupted response -- it's a real, documented
        # compatibility path -- but this module can't safely reconstruct
        # array-valued columns (soc_log_spike's make-series `hits` series)
        # from flat table text, so it still can't proceed. Say so plainly
        # instead of the generic "unexpected non-JSON response", which
        # reads like backend corruption rather than a version mismatch.
        return None, (
            "backend returned a non-JSON response, consistent with an "
            "older bzrk build that doesn't support --json (this "
            "investigation tool requires --json for structured branching "
            f"and cannot parse legacy table output): {str(out)[:200]}"
        )
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return None, f"unexpected non-JSON response: {str(out)[:200]}"
    records = agent_analytics._json_records(parsed)
    if records is None:
        return None, f"unrecognized response shape: {str(out)[:200]}"
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
            True, None, None,
        )
    if not rows:
        return (
            f"Checked: errors_by_service (since={since})\n"
            f"Result: no errors\n"
            f"Investigation complete.\n"
            f"Verdict: no errors in window, nothing to investigate.",
            False, None, None,
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
            False, None, None,
        )
    return (
        f"Checked: errors_by_service (since={since})\n"
        f"Result: {count} errors for service {service!r} (~{rate:.1f}/min)\n"
        f"Threshold: >{ERROR_RATE_INVESTIGATE_PER_MIN}/min investigate\n"
        f"Branch: investigate (elevated)",
        False, "check_log_spike", service,
    )


def _node_check_log_spike(since, service):
    if not service:
        return (
            "check_log_spike requires service (pass the value the start "
            "node's response gave you).",
            True, None, None,
        )
    kql = _q_soc_log_spike(service)
    rows, err = _run_json(kql, since)
    if err is not None:
        return (
            f"Checked: soc_log_spike (since={since}) — FAILED\n"
            f"Error: {err}\n"
            f"Investigation halted at check_log_spike. The error-rate "
            f"elevation from the previous step is still valid; this "
            f"step's result is unknown, not \"no spike.\"",
            True, None, None,
        )
    # The query is already scoped to `service`; this lookup is defense in
    # depth (a backend that ignored the filter shouldn't silently pass).
    row = next((r for r in rows if str(r.get("service")) == service), None)
    if row is None:
        return (
            f"Checked: soc_log_spike (since={since})\n"
            f"No log-volume data for service={service!r} in this window "
            f"— FAILED\n"
            f"Investigation halted at check_log_spike.",
            True, None, None,
        )
    hits = row.get("hits")
    if not isinstance(hits, list) or len(hits) < _MIN_SPIKE_BUCKETS:
        return (
            f"Checked: soc_log_spike (since={since})\n"
            f"Result: insufficient buckets for service={service!r} to "
            f"assess a spike (need >= {_MIN_SPIKE_BUCKETS}) — FAILED\n"
            f"Investigation halted at check_log_spike.",
            True, None, None,
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
            False, None, None,
        )
    return (
        f"Checked: soc_log_spike (since={since})\n"
        f"Result: service={service!r} recent volume {recent_mean:.1f}/min "
        f"vs baseline {baseline_mean:.1f}/min — spike confirmed\n"
        f"Branch: correlated spike",
        False, "check_traces", service,
    )


def _node_check_traces(since, service):
    if not service:
        return (
            "check_traces requires service (pass the value the previous "
            "step's response gave you).",
            True, None, None,
        )
    kql = _q_trace_find_errors(service)
    rows, err = _run_json(kql, since)
    if err is not None:
        return (
            f"Checked: trace_find_errors (since={since}) — FAILED\n"
            f"Error: {err}\n"
            f"Investigation halted at check_traces.",
            True, None, None,
        )
    # The query is already scoped to `service`; this filter is defense in
    # depth. Dedup by trace_id -- Q_TRACE_FIND_ERRORS returns one row per
    # error *span*, and multiple spans (and their status changes) can
    # share one trace_id, so counting rows overcounts and can repeat the
    # same trace in the example list (Codex review finding, 2026-08-28).
    matching = [r for r in rows if str(r.get("service")) == service]
    distinct = {}
    for r in matching:
        distinct.setdefault(r.get("trace_id"), r)
    distinct_traces = list(distinct.values())
    if not distinct_traces:
        return (
            f"Checked: trace_find_errors (since={since})\n"
            f"Result: no failing traces found for service={service!r}\n"
            f"Investigation complete.\n"
            f"Verdict: error rate elevated, correlated log-volume spike "
            f"confirmed for {service!r}, but no failing traces found — "
            f"investigate ingestion lag or a non-trace-instrumented "
            f"failure path.",
            False, None, None,
        )
    examples = "; ".join(
        f"{r.get('span_name')} ({r.get('trace_id')})"
        for r in distinct_traces[:_MAX_EXAMPLE_TRACES]
    )
    return (
        f"Checked: trace_find_errors (since={since})\n"
        f"Result: {len(distinct_traces)} failing traces found for "
        f"service={service!r}: {examples}\n"
        f"Investigation complete.\n"
        f"Verdict: error rate elevated, correlated log-volume spike "
        f"confirmed, {len(distinct_traces)} failing traces found for "
        f"{service!r} — root cause is likely in {service!r}'s own "
        f"request path, not a downstream dependency.",
        False, None, None,
    )


def run_error_rate_node(node, since, service):
    """Execute exactly one hop of the elevated-error-rate tree. Returns
    (text, is_error, next_node, next_service).

    text carries only telemetry-derived findings for this hop -- never the
    fixed continuation directive. The caller (berserk_mcp.py's dispatch)
    is responsible for presenting `next_node`/`next_service` as the "call
    this next" instruction *outside* whatever untrusted-data fence it
    applies to `text` (Codex review finding, 2026-08-28: this module used
    to bake "Next: call investigate_error_rate(...)" into the same text
    that gets fenced as untrusted at the dispatch boundary -- since the
    server's own instructions tell every client to never follow anything
    inside that fence, a compliant model could never actually advance past
    the first hop. next_node/next_service are structured values this
    module fully controls, letting the caller build that instruction from
    trusted server code instead of the fenced text). Both are None at
    every terminal state (concluded or halted)."""
    if node == "start":
        return _node_start(since)
    if node == "check_log_spike":
        return _node_check_log_spike(since, service)
    if node == "check_traces":
        return _node_check_traces(since, service)
    return (
        f"Unknown node {node!r}. Call investigate_error_rate with no "
        f"node argument (or node=\"start\") to begin a new investigation.",
        True, None, None,
    )
