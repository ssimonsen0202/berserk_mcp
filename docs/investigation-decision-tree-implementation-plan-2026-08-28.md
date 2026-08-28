# Investigation Decision Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `investigate_error_rate`, a fixed decision-tree MCP tool that
composes three existing SRE signals (elevated error rate → correlated
log-volume spike → failing traces) without agent-authored code, addressing
issue #24's "composition ceiling" gap.

**Architecture:** New module `investigation.py`, one hop per call, each hop
executing the underlying data-fetch directly (never parsing another tool's
display text). No server-side session state — the caller's `node` and
`service` parameters carry the only state between calls.

**Tech Stack:** stdlib-only Python, same `configure()`-wiring convention as
`agent_analytics.py`/`parser_factory.py`, same DI-for-tests seam pattern
used throughout this codebase.

**Spec:** `docs/investigation-decision-tree-implementation-spec.md`

## Global Constraints

- Stdlib only — no new pip dependencies (project-wide rule).
- `investigation.py` must not import `berserk_mcp` (avoids the import
  cycle every other capability module already avoids the same way).
- Branch logic always operates on structured JSON row values from
  `bzrk_search_json`, never on another tool's formatted display text.
- A failed backend call halts the investigation and reports the failure
  explicitly — it never silently proceeds down a default branch.
- No server-side session state. All state travels in the caller-visible
  `node`/`service`/`since` parameters.
- Response text follows this project's convention everywhere else: plain
  text, not a JSON envelope.
- Thresholds sourced from `primers/sre.md`'s existing escalation table
  (error rate: >10/min investigate). Known drift risk (spec's own
  follow-up note) — not fixed in this plan, single source of truth is
  the constant defined in Task 1.

## Interface refinement discovered during planning (not a spec change)

The spec's tool-interface section didn't specify how the "offending
service" found at `start` reaches later hops. Two options: re-derive it at
each hop (extra query, no coupling) or thread it forward explicitly. This
plan adds an explicit `service` parameter, pre-filled in each hop's "next
call" instruction text — consistent with the spec's own citation of
Datadog's `recommended_tool_call` pattern (parameters pre-filled, not
re-derived). `service` is ignored at `node="start"` and required (with a
clear validation error if missing) at `check_log_spike` and `check_traces`.

---

### Task 1: `investigation.py` module + `start` node

**Files:**
- Create: `investigation.py`
- Test: `tests/test_investigation.py`

**Interfaces:**
- Produces: `investigation.configure(bzrk_search, since_hours, q_errors, q_soc_log_spike, q_trace_find_errors)`
- Produces: `investigation.ERROR_RATE_INVESTIGATE_PER_MIN = 10` (module constant, sourced from `primers/sre.md`)
- Produces: `investigation.run_error_rate_node(node, since, service)` — returns `(text: str, is_error: bool, next_node: str | None)`. `next_node` is `None` when the investigation concluded or halted; the MCP-layer wrapper (Task 5) uses it only for the docstring/description, not for control flow — the response `text` itself is what the calling agent reads.

- [ ] **Step 1: Write the failing test for `_run_json` (the shared query-and-parse helper)**

```python
# tests/test_investigation.py
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import investigation as inv  # noqa: E402


def bzrk_json_table(columns, rows):
    """Build the exact bzrk --json shape agent_analytics._json_records
    parses: {"Tables": [{"schema": {"columns": [...]}, "rows": [[...]]}]}."""
    return json.dumps({"Tables": [{
        "schema": {"columns": [{"name": c} for c in columns]},
        "rows": rows,
    }]})


class RunJsonTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_returns_parsed_rows_on_success(self):
        def fake_search(kql, since):
            self.calls.append((kql, since))
            return bzrk_json_table(["service", "errors"], [["checkout", 23]]), False
        inv.configure(
            bzrk_search=fake_search, since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [{"service": "checkout", "errors": 23}])
        self.assertEqual(self.calls, [("Q1", "1h ago")])

    def test_backend_error_returns_error_text_not_rows(self):
        inv.configure(
            bzrk_search=lambda kql, since: ("bzrk timed out", True),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertEqual(err, "bzrk timed out")

    def test_non_json_response_is_reported_not_silently_empty(self):
        inv.configure(
            bzrk_search=lambda kql, since: ("not json at all", False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(rows)
        self.assertIn("unexpected non-JSON response", err)

    def test_no_rows_sentinel_returns_empty_list_not_an_error(self):
        # bzrk's --json mode still returns the plain-text "(no rows)"
        # sentinel for a genuinely empty result, not an empty JSON array.
        # This must be treated as success-with-zero-rows, not a parse
        # failure -- caught during implementation verification, not
        # written speculatively (a real bug in an earlier draft of this
        # code halted every empty-result investigation with "unexpected
        # non-JSON response" instead of concluding cleanly).
        inv.configure(
            bzrk_search=lambda kql, since: ("(no rows)", False),
            since_hours=lambda s: 1.0,
            q_errors="Q1", q_soc_log_spike="Q2", q_trace_find_errors="Q3",
        )
        rows, err = inv._run_json("Q1", "1h ago")
        self.assertIsNone(err)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'investigation'`

- [ ] **Step 3: Write the module skeleton and `_run_json`**

```python
# investigation.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for the `start` node**

```python
class StartNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_normal_rate_concludes(self):
        # 5 errors over a 1h window = 5/60 per-minute, below the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 5]]), False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("normal", text.lower())

    def test_elevated_rate_advances_to_check_log_spike(self):
        # 700 errors over 1h = ~11.7/min, above the 10/min gate.
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700], ["auth", 3]]), False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_log_spike")
        self.assertIn("checkout", text)
        self.assertIn("check_log_spike", text)
        self.assertIn("service=\"checkout\"", text)

    def test_no_rows_concludes_nothing_to_investigate(self):
        self.responses["Q_ERRORS"] = ("(no rows)", False)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no errors", text.lower())

    def test_backend_failure_halts_and_reports(self):
        self.responses["Q_ERRORS"] = ("bzrk timed out after 120s", True)
        text, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("FAILED", text)
        self.assertIn("bzrk timed out after 120s", text)
```

- [ ] **Step 6: Run test to verify it fails**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: FAIL with `AttributeError: module 'investigation' has no attribute 'run_error_rate_node'`

- [ ] **Step 7: Implement `_node_start` and the `run_error_rate_node` dispatcher (start-only for now)**

```python
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


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def run_error_rate_node(node, since, service):
    """Execute exactly one hop of the elevated-error-rate tree. Returns
    (text, is_error, next_node) -- next_node is None at every terminal
    state (concluded or halted)."""
    if node == "start":
        return _node_start(since)
    return (
        f"Unknown node {node!r}. Call investigate_error_rate with no "
        f"node argument (or node=\"start\") to begin a new investigation.",
        True, None,
    )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: PASS (all tests so far)

- [ ] **Step 9: Commit**

```bash
git add investigation.py tests/test_investigation.py
git commit -m "feat: investigation.py module + start node (issue #24)"
```

---

### Task 2: `check_log_spike` node

**Files:**
- Modify: `investigation.py`
- Test: `tests/test_investigation.py`

**Interfaces:**
- Consumes: `_run_json(kql, since)` from Task 1, `ERROR_RATE_INVESTIGATE_PER_MIN`-style module constants pattern
- Produces: `_node_check_log_spike(since, service)` wired into `run_error_rate_node`

- [ ] **Step 1: Write the failing tests**

```python
class CheckLogSpikeNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def _spike_response(self, service, hits):
        return bzrk_json_table(["service", "hits"], [[service, hits]]), False

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node = inv.run_error_rate_node("check_log_spike", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("service", text.lower())

    def test_spike_advances_to_check_traces(self):
        # 55 baseline buckets averaging ~2, last 5 buckets averaging 20 --
        # 20 > 2 * SPIKE_MULTIPLIER (3), so this is a spike.
        hits = [2] * 55 + [20] * 5
        self.responses["Q_SPIKE"] = self._spike_response("checkout", hits)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertEqual(next_node, "check_traces")
        self.assertIn("check_traces", text)
        self.assertIn("service=\"checkout\"", text)

    def test_no_spike_concludes_with_manual_review_recommendation(self):
        hits = [2] * 60  # flat, no spike
        self.responses["Q_SPIKE"] = self._spike_response("checkout", hits)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("manual review", text.lower())

    def test_service_not_present_in_series_halts(self):
        self.responses["Q_SPIKE"] = self._spike_response("auth", [1] * 60)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no log-volume data", text.lower())

    def test_insufficient_buckets_halts(self):
        self.responses["Q_SPIKE"] = self._spike_response("checkout", [1, 2, 3])
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("insufficient", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_SPIKE"] = ("bzrk timed out", True)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_log_spike", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
        self.assertIn("FAILED", text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_investigation.CheckLogSpikeNodeTest -v`
Expected: FAIL — `check_log_spike` currently falls through to the "Unknown node" branch.

- [ ] **Step 3: Implement `_node_check_log_spike`**

```python
_MIN_SPIKE_BUCKETS = _RECENT_BUCKET_COUNT * 2  # need real baseline, not just recent


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
```

Wire it into the dispatcher from Task 1:

```python
def run_error_rate_node(node, since, service):
    if node == "start":
        return _node_start(since)
    if node == "check_log_spike":
        return _node_check_log_spike(since, service)
    return (
        f"Unknown node {node!r}. Call investigate_error_rate with no "
        f"node argument (or node=\"start\") to begin a new investigation.",
        True, None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add investigation.py tests/test_investigation.py
git commit -m "feat: investigation.py check_log_spike node (issue #24)"
```

---

### Task 3: `check_traces` node (terminal)

**Files:**
- Modify: `investigation.py`
- Test: `tests/test_investigation.py`

**Interfaces:**
- Consumes: `_run_json`, `_q_trace_find_errors`
- Produces: `_node_check_traces(since, service)` wired into `run_error_rate_node`

- [ ] **Step 1: Write the failing tests**

```python
class CheckTracesNodeTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_no_service_param_is_a_validation_error(self):
        text, is_error, next_node = inv.run_error_rate_node("check_traces", "1h ago", None)
        self.assertTrue(is_error)
        self.assertIsNone(next_node)

    def test_failing_traces_found_concludes_with_root_cause_verdict(self):
        rows = [
            ["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"],
            ["t2", "GET /cart", "2026-08-28T00:01:00Z", "checkout"],
            ["t3", "POST /pay", "2026-08-28T00:02:00Z", "auth"],
        ]
        self.responses["Q_TRACE"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("2 failing", text)
        self.assertIn("checkout", text)
        self.assertNotIn("auth", text)  # filtered to the offending service

    def test_no_matching_traces_concludes_with_ingestion_gap_hypothesis(self):
        rows = [["t1", "GET /health", "2026-08-28T00:00:00Z", "other-service"]]
        self.responses["Q_TRACE"] = bzrk_json_table(
            ["trace_id", "span_name", "timestamp", "service"], rows), False
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertFalse(is_error)
        self.assertIsNone(next_node)
        self.assertIn("no failing traces", text.lower())
        self.assertIn("ingestion lag", text.lower())

    def test_backend_failure_halts(self):
        self.responses["Q_TRACE"] = ("bzrk timed out", True)
        text, is_error, next_node = inv.run_error_rate_node(
            "check_traces", "1h ago", "checkout")
        self.assertTrue(is_error)
        self.assertIsNone(next_node)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_investigation.CheckTracesNodeTest -v`
Expected: FAIL — falls through to "Unknown node."

- [ ] **Step 3: Implement `_node_check_traces`**

```python
_MAX_EXAMPLE_TRACES = 3


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
```

Wire it into the dispatcher:

```python
def run_error_rate_node(node, since, service):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add investigation.py tests/test_investigation.py
git commit -m "feat: investigation.py check_traces node, tree complete (issue #24)"
```

---

### Task 4: End-to-end walk tests + pre-formatting-data-path regression test

**Files:**
- Modify: `tests/test_investigation.py`

**Interfaces:**
- Consumes: `run_error_rate_node` (Tasks 1-3), nothing new produced — this task is test-only, validating the full spec's "Tests plan" section end to end.

- [ ] **Step 1: Write the full-walk and regression tests**

```python
class FullWalkTest(unittest.TestCase):
    def setUp(self):
        inv.configure(
            bzrk_search=self._search, since_hours=lambda s: 1.0,
            q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE", q_trace_find_errors="Q_TRACE",
        )
        self.responses = {}

    def _search(self, kql, since):
        return self.responses[kql]

    def test_full_walk_elevated_spike_traces_found(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [2] * 55 + [20] * 5]]), False)
        self.responses["Q_TRACE"] = (
            bzrk_json_table(
                ["trace_id", "span_name", "timestamp", "service"],
                [["t1", "POST /checkout", "2026-08-28T00:00:00Z", "checkout"]],
            ), False)

        text1, err1, next1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")

        text2, err2, next2 = inv.run_error_rate_node("check_log_spike", "1h ago", "checkout")
        self.assertFalse(err2)
        self.assertEqual(next2, "check_traces")

        text3, err3, next3 = inv.run_error_rate_node("check_traces", "1h ago", "checkout")
        self.assertFalse(err3)
        self.assertIsNone(next3)
        self.assertIn("root cause is likely", text3)

    def test_full_walk_elevated_but_no_spike_early_exit(self):
        self.responses["Q_ERRORS"] = (
            bzrk_json_table(["service", "errors"], [["checkout", 700]]), False)
        self.responses["Q_SPIKE"] = (
            bzrk_json_table(["service", "hits"], [["checkout", [5] * 60]]), False)

        _, err1, next1 = inv.run_error_rate_node("start", "1h ago", None)
        self.assertFalse(err1)
        self.assertEqual(next1, "check_log_spike")

        text2, err2, next2 = inv.run_error_rate_node("check_log_spike", "1h ago", "checkout")
        self.assertFalse(err2)
        self.assertIsNone(next2)
        self.assertIn("manual review", text2.lower())


class DisplayFormatIndependenceTest(unittest.TestCase):
    """Proves the architecture decision (branch logic never depends on a
    fixed tool's display-formatted text) actually holds, not just that
    it's stated as intent (spec's own required regression test)."""

    def test_branch_logic_unaffected_by_reformatted_but_same_structured_value(self):
        # Two structurally-identical responses that would render as very
        # different display text if a fixed tool changed its formatting
        # (different key order, extra whitespace) but carry the same
        # decision-relevant value. Branch outcome must be identical.
        variant_a = json.dumps({"Tables": [{
            "schema": {"columns": [{"name": "service"}, {"name": "errors"}]},
            "rows": [["checkout", 700]],
        }]})
        variant_b = json.dumps({"Tables": [{
            "schema": {"columns": [{"name": "errors"}, {"name": "service"}]},
            "rows": [[700, "checkout"]],
        }]}, indent=4)  # different formatting, different column order

        for variant in (variant_a, variant_b):
            with self.subTest(variant=variant[:30]):
                inv.configure(
                    bzrk_search=lambda kql, since, v=variant: (v, False),
                    since_hours=lambda s: 1.0,
                    q_errors="Q_ERRORS", q_soc_log_spike="Q_SPIKE",
                    q_trace_find_errors="Q_TRACE",
                )
                _, is_error, next_node = inv.run_error_rate_node("start", "1h ago", None)
                self.assertFalse(is_error)
                self.assertEqual(next_node, "check_log_spike")
```

- [ ] **Step 2: Run all tests to verify they pass**

Run: `python3 -m unittest tests.test_investigation -v`
Expected: PASS (all tests across all classes)

- [ ] **Step 3: Commit**

```bash
git add tests/test_investigation.py
git commit -m "test: end-to-end walk + display-format-independence regression (issue #24)"
```

---

### Task 5: Wire into `berserk_mcp.py`

**Files:**
- Modify: `berserk_mcp.py`
- Test: `tests/test_berserk_mcp.py`

**Interfaces:**
- Consumes: `investigation.configure`, `investigation.run_error_rate_node` (Tasks 1-4)
- Produces: MCP tool `investigate_error_rate`, dispatched through the normal `handle_call` path

- [ ] **Step 1: Write the failing dispatch test**

Add near the other SRE-tool tests in `tests/test_berserk_mcp.py` (find an
existing `class` covering `sre_error_rate` or similar and add a sibling
test class, or append near end of file):

```python
class InvestigateErrorRateTest(unittest.TestCase):
    def setUp(self):
        self._orig = bm.run_bzrk

    def tearDown(self):
        bm.run_bzrk = self._orig

    def _mock_bzrk(self, out, err=False):
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (out, err)

    def test_start_node_dispatches_and_returns_text(self):
        doc = {"Tables": [{
            "schema": {"columns": [{"name": "service"}, {"name": "errors"}]},
            "rows": [["checkout", 5]],
        }]}
        self._mock_bzrk(json.dumps(doc))
        text, err = bm.handle_call("investigate_error_rate", {})
        self.assertFalse(err, text)
        self.assertIn("normal", text.lower())

    def test_unknown_node_is_reported_as_error(self):
        self._mock_bzrk("(no rows)")
        text, err = bm.handle_call(
            "investigate_error_rate", {"node": "not_a_real_node"})
        self.assertTrue(err)
        self.assertIn("unknown node", text.lower())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_berserk_mcp.InvestigateErrorRateTest -v`
Expected: FAIL — `investigate_error_rate` is not a known tool name yet.

- [ ] **Step 3: Import and configure `investigation` in `berserk_mcp.py`**

Find the existing `import agent_analytics` near the top of `berserk_mcp.py`
and add the new import alongside it:

```python
import investigation
```

Find the `agent_analytics.configure(...)` call (the one wiring
`bzrk_search=bzrk_search_json, table=TABLE, ...`) and add a matching call
immediately after it:

```python
investigation.configure(
    bzrk_search=bzrk_search_json,
    since_hours=_since_hours,
    q_errors=Q_ERRORS,
    q_soc_log_spike=Q_SOC_LOG_SPIKE,
    q_trace_find_errors=Q_TRACE_FIND_ERRORS,
)
```

- [ ] **Step 4: Add the TOOLS entry, near the other SRE tools (find the
  `sre_error_rate` entry in the tools list and add this one right after it)**

```python
{"name": "investigate_error_rate", "roles": ["sre"], "description": "Fixed decision-tree investigation for an elevated error rate: checks errors_by_service, and if elevated, walks correlated log-volume spike and failing-trace checks — one hop per call, reproducible, no agent-authored composition. Start with no arguments (or node='start'); each response tells you the next call to make.", "inputSchema": {"type": "object", "properties": dict({"node": {"type": "string", "description": "which hop to run; omit or 'start' to begin a new investigation"}, "service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "required for node='check_log_spike'/'check_traces' — the service name the previous step's response gave you"}}, **_since())}},
```

- [ ] **Step 5: Add the TITLES entry**

Find the `TITLES` dict (near `"sre_error_rate": "..."`) and add:

```python
"investigate_error_rate": "Investigate: Error Rate",
```

- [ ] **Step 6: Add the dispatch branch**

Find the dispatch block handling `sre_error_rate` (or another SRE SIMPLE
tool) in `_handle_call_uncached` and add, in the SRE-tools region:

```python
if name == "investigate_error_rate":
    node = str(arguments.get("node") or "start").strip()
    service = arguments.get("service")
    service = str(service).strip() if service else None
    since = arguments.get("since") or "1h ago"
    text, is_err, _next_node = investigation.run_error_rate_node(node, since, service)
    return _fence_untrusted(text), is_err
```

**Note the fencing:** every value the response text might echo back
(`service`, span names, trace IDs) originates from live telemetry and is
attacker-influenceable, same as every other SRE/SOC tool's output — fence
unconditionally, matching the established pattern (issue #11).

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_berserk_mcp.InvestigateErrorRateTest -v`
Expected: PASS

Run the full suite to confirm no regressions:

Run: `python3 -m unittest discover -s tests`
Expected: PASS, test count increased from 881 by this task's new tests
(dispatch tests here + all of Tasks 1-4's `test_investigation.py` tests,
once that file is picked up by `discover`)

- [ ] **Step 8: Commit**

```bash
git add berserk_mcp.py tests/test_berserk_mcp.py
git commit -m "feat: wire investigate_error_rate into berserk_mcp dispatch (issue #24)"
```

---

### Task 6: Discoverability, primer, eval case, and docs

**Files:**
- Modify: `primers/sre.md`
- Modify: `evals/router_cases.jsonl`
- Modify: `README.md`
- Modify: `docs/berserk-dev-brief-2026-08-20.md`

**Interfaces:** none — documentation and eval-fixture only, no code.

- [ ] **Step 1: Add a row to `primers/sre.md`'s tool routing table**

Find the `## Tool routing guide` table and add a row (the table is small
enough that alphabetical/logical placement doesn't matter much — place it
near the error-rate row):

```markdown
| Why is error rate elevated — full root-cause walk, not just the number? | `investigate_error_rate` |
```

- [ ] **Step 2: Add a first-hop selection eval case to `evals/router_cases.jsonl`**

Append (matching the existing one-line-per-case format, using the same
placeholder service-name convention already used elsewhere in this file
per the eval-harness's synthetic-fixtures-only guardrail):

```json
{"id": "investigate_error_spike", "prompt": "why is checkout-service's error rate elevated? I want the root cause, not just the count.", "expect_tool": "investigate_error_rate"}
```

Sanity-check the case file parses and the harness runs against it with no
model credentials required, using the mock backend:

Run: `python3 evals/run_eval.py evals/router_cases.jsonl --backend mock --limit 1`
Expected: runs without error (confirms the new JSONL line is valid and
`expect_tool` names a real, currently-registered tool — the mock backend
doesn't score routing accuracy, it only proves the harness can load and
iterate the case file).

A real accuracy check against a live model is a separate, optional step —
`python3 evals/run_eval.py evals/router_cases.jsonl --backend <openai|anthropic|ollama|lmstudio> --model <name>` — and is not required to merge this PR if no model credentials are configured in this environment; note that explicitly in the PR description if skipped (matches how other eval-only additions in this project's history have shipped).

- [ ] **Step 3: Add the SRE tools table row in `README.md`**

Find `### SRE tools (\`sre\` lane only)` (around line 420) and add a row
to the table:

```markdown
| `investigate_error_rate` | Fixed decision-tree root-cause walk for an elevated error rate — errors_by_service → correlated log-spike → failing traces, one hop per call. |
```

- [ ] **Step 4: Add a "Release history" bullet placeholder note — do NOT bump `__version__` in this task**

Per this project's established convention (confirmed via git history:
`__version__` in `berserk_mcp.py` and the `## Release history` table are
updated together, in their own dedicated step, not folded into a feature
PR silently), leave the version bump and release-notes entry as a
deliberate, separate final step — either its own tiny follow-up PR
immediately after this one merges, or the last commit in this same PR if
the person executing this plan and the user shipping it agree that's
appropriate at the time. Do not skip it silently; flag it explicitly in
the PR description if left for a follow-up.

- [ ] **Step 5: Update `docs/investigation-decision-tree-implementation-spec.md`'s status line**

Change:

```markdown
Status: DRAFT — brainstormed and approved 2026-08-27, not yet implemented.
```

to:

```markdown
Status: IMPLEMENTED — brainstormed and approved 2026-08-27, shipped 2026-08-28.
```

(Use the actual ship date at implementation time, not the date this plan
was written, if they differ.)

- [ ] **Step 6: Run the full suite one more time**

Run: `python3 -m unittest discover -s tests` — Expected: PASS
Run: `python3 evals/mcp_protocol_smoke.py --include-http` — Expected: all PASS (confirms the new tool appears correctly in `tools/list` and dispatches through both stdio and HTTP transports)

- [ ] **Step 7: Commit**

```bash
git add primers/sre.md evals/router_cases.jsonl README.md docs/investigation-decision-tree-implementation-spec.md
git commit -m "docs: investigate_error_rate discoverability, primer, eval case (issue #24)"
```

---

## Post-plan: shipping pipeline

This plan's tasks produce commits on a feature branch in an isolated
worktree, matching this project's established pipeline for every prior
change: push the branch, open a PR, wait for CI, merge once green, run
the full test suite once more against a fresh `origin/main` checkout in a
new worktree (never trust `gh pr merge`'s own success alone), then mirror
to Gitea (`git push gitea main:github-main` from the dedicated checkout,
confirmed via `git rev-list --count gitea/github-main..origin/main` == 0)
before cleaning up worktrees. Close issue #24 with a comment linking the
merged PR once shipped.
