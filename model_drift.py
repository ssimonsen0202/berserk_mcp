#!/usr/bin/env python3
"""Verdict logic for model-behavior monitoring (issue #91).

Reads the score series the canary stored in Berserk and answers one
question: is this model still behaving the way it did?

The hard part is not detecting a drop -- it is NOT firing on noise. At 48
cases and repeats=1, one case flipping moves accuracy ~2.1 points. A canary
that alerts on that gets ignored within a week, which is worse than no
canary. Hence: a drop must exceed a measured noise band AND persist across
two consecutive runs before anything fires.

The noise band is measured, not guessed. Calibrated 2026-09-01 against 5
consecutive live canary runs of deepseek/deepseek-v4-flash (repeats=3,
case_set_version 61a845d75929, cost $0.3987 total):
  scores: [0.9514, 0.9583, 0.9514, 0.9514, 0.9444]
  mean 0.9514, stdev 0.0049, range 0.0139 (max single-run deviation from
  the mean was 0.0069, symmetric on both sides). DEFAULT_NOISE_BAND is set
  to ~3x that maximum observed deviation, rounded to a clean number --
  comfortably clears real run-to-run noise while still catching a
  regression that matters. See docs/model-routing-cost-validation-2026-08-23.md
  for the full run-by-run data. This one calibration is a starting point,
  not a permanent constant -- re-baseline if the case set changes size
  materially or after enough production history accumulates to compare.

The calibration was run at repeats=3 specifically. BERSERK_MCP_CANARY_REPEATS
is operator-configurable, and at repeats=1 a single case flipping (1/48 =
0.0208) already exceeds this band on its own -- a sampling regime the
calibration never covered. classify() tracks each row's own repeats value
and caps confidence at "low" when the window includes a run below
MIN_RELIABLE_REPEATS, rather than either silently trusting an uncalibrated
band or suppressing a verdict that might still be real.

Found by an independent Codex review (2026-09-02) after the branch merged:
the fingerprints Task 3 computes were never read back into a verdict (no
column in the KQL projection, no caller passed fingerprint_changed); a role
or discovery-mode change looks identical to a model regression under the
same case_set_version, since run_eval.py inherits BERSERK_MCP_ROLE /
BERSERK_MCP_DISCOVERY from the environment via its MCP subprocess handshake;
mixed-version history permanently blocked classification rather than just
requiring enough runs at the current version; the KQL hardcoded the
"default" table instead of the project's own BERSERK_TABLE; and
group_by_model() assumed JSON-only input despite bzrk_search_json's own
documented text fallback for older bzrk builds. All fixed below.
"""

import json
import os

MIN_HISTORY = 4           # runs at one case-set/role/discovery combination before any verdict
CONSECUTIVE_REQUIRED = 2  # degraded runs in a row before firing
MIN_TREND_R2 = 0.6        # matches berserk_mcp.py:2913's forecastability floor
DEFAULT_NOISE_BAND = 0.02  # measured 2026-09-01, see module docstring
MIN_RELIABLE_REPEATS = 3  # the calibration's own repeats value, see module docstring

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Matches berserk_mcp.py:142's TABLE constant. model_drift.py cannot import
# berserk_mcp (berserk_mcp imports model_drift -- that would be circular),
# so it reads the same environment variable directly instead of hardcoding
# "default", which broke on any deployment that configures a different
# table (found by Codex review, 2026-09-02).
_TABLE = os.environ.get("BERSERK_TABLE", "default")


def _usable(series):
    """Drop failed runs. A provider outage is not a score of zero."""
    return [r for r in series if r.get("status") == "ok"]


def _current_environment_rows(rows):
    """Only rows sharing the most recent case_set_version, role, and
    discovery_mode combination (rows are timestamp-ascending, so rows[-1]
    is the latest).

    This replaces an earlier version that required every row in the whole
    query window to share one case_set_version -- after any deliberate
    case-set edit, that blocked classification until every older-version
    row aged out of the 30-day window, even once enough new-version runs
    existed. Filtering to the latest combination lets classification
    resume as soon as MIN_HISTORY runs exist at it, without waiting on
    unrelated history to expire."""
    if not rows:
        return []
    latest = rows[-1]
    fields = ("case_set_version", "role", "discovery_mode", "tier")
    key = tuple(latest.get(f) for f in fields)
    return [r for r in rows if tuple(r.get(f) for f in fields) == key]


def _fingerprint_values(rows):
    """The set of non-empty fingerprint values present across a window of
    rows, per fingerprint kind. A window can legitimately span more than
    one value (e.g. two fetch attempts against a rotating value) -- the
    comparison in _fingerprint_changed is set-based for that reason."""
    out = {"behavioral_fingerprint": set(), "provider_metadata_fingerprint": set()}
    for row in rows:
        for key in out:
            val = row.get(key)
            if val:
                out[key].add(val)
    return out


def _fingerprint_changed(baseline_rows, recent_rows):
    """True if either fingerprint's value-set in the recent window
    disagrees with its value-set in the baseline window.

    Takes the baseline/recent SPLIT, not the raw series -- comparing only
    the two most recent rows (an earlier version) misses a transition
    that lands exactly at the baseline/recent boundary. Concretely:
    baseline=[(0.9,A),(0.9,A)], recent=[(0.6,B),(0.6,B)] -- the real A->B
    change is between baseline's last row and recent's first row; the two
    most recent rows are both already B, so a last-two-rows comparison
    reports no change at all. Found by Codex backtest, 2026-09-02.

    A fingerprint with no observed value on either side is not compared
    (an empty set never "disagrees") -- a run that skipped fingerprinting
    (fetch failure, see berserk_mcp._attach_fingerprints) must not create
    a false "the provider changed" signal."""
    baseline_vals = _fingerprint_values(baseline_rows)
    recent_vals = _fingerprint_values(recent_rows)
    for key in baseline_vals:
        b, r = baseline_vals[key], recent_vals[key]
        if b and r and b != r:
            return True
    return False


def classify(series, noise_band=DEFAULT_NOISE_BAND, fingerprint_changed=None):
    """fingerprint_changed defaults to None, not False: None means "derive
    it from the series", so a real caller that just passes classify(series)
    gets correct behavior automatically. Explicit True/False (as this
    module's own tests use) always overrides the derivation -- this keeps
    the function testable in isolation while making the common, real call
    path (berserk_mcp.py's two dispatcher branches, both of which called
    classify(series) with nothing else) correct without every caller
    having to remember to compute it. Found by Codex review (2026-09-02):
    neither caller passed it, so fingerprint_changed was always False in
    practice regardless of what the fingerprints actually showed.

    The return dict always includes "fingerprint_changed" and
    "fingerprint_values" (the recent window's own fingerprint set), even
    when the verdict is "stable" -- a fingerprint-only change with
    unchanged accuracy correctly stays "stable" (this function's job is
    accuracy-trend classification, not provider-change detection; that is
    mechanism 2 in the design spec, deliberately kept separate so a
    single verdict cannot conflate "the score moved" with "the provider
    changed"). But the two MCP tools' own descriptions promise reporting
    "provider fingerprint status" regardless of verdict, and nothing
    previously surfaced it when the verdict stayed stable -- found by
    Codex backtest, 2026-09-02. Callers render these fields directly so
    that promise holds even when no accuracy verdict fires."""
    rows = _current_environment_rows(_usable(series))
    if len(rows) < MIN_HISTORY:
        return {"verdict": "insufficient-data",
                "reason": "not enough runs at the current case-set version, "
                          "role, and discovery-mode combination",
                "confidence": "low", "fingerprint_changed": False,
                "fingerprint_values": {}}

    baseline_rows = rows[:-CONSECUTIVE_REQUIRED]
    recent_rows = rows[-CONSECUTIVE_REQUIRED:]
    if fingerprint_changed is None:
        fingerprint_changed = _fingerprint_changed(baseline_rows, recent_rows)
    fingerprint_values = {
        k: sorted(v) for k, v in _fingerprint_values(recent_rows).items() if v
    }

    scores = [float(r["tool_accuracy"]) for r in baseline_rows]
    baseline = sum(scores) / len(scores)
    recent = [float(r["tool_accuracy"]) for r in recent_rows]

    degraded = [s for s in recent if baseline - s > noise_band]
    if len(degraded) < CONSECUTIVE_REQUIRED:
        return {"verdict": "stable",
                "reason": "no sustained drop beyond the noise band",
                "confidence": "medium", "fingerprint_changed": fingerprint_changed,
                "fingerprint_values": fingerprint_values}

    drop = baseline - min(recent)
    sharp = drop > 2 * noise_band
    verdict = "step-change" if sharp else "degrading"

    repeats_values = [int(r["repeats"]) for r in rows if str(r.get("repeats") or "").strip()]
    under_calibrated_sampling = bool(repeats_values) and min(repeats_values) < MIN_RELIABLE_REPEATS

    if under_calibrated_sampling:
        confidence = "low"
    elif sharp and fingerprint_changed:
        confidence = "high"
    else:
        confidence = "medium"

    reason = f"{drop:.3f} below baseline {baseline:.3f} across {CONSECUTIVE_REQUIRED} runs"
    if fingerprint_changed:
        reason += "; provider fingerprint also changed"
    if under_calibrated_sampling:
        reason += (f"; repeats={min(repeats_values)} is below the noise band's "
                   f"own calibration ({MIN_RELIABLE_REPEATS}), so this verdict "
                   "is less reliable than the band alone suggests")
    return {"verdict": verdict, "reason": reason, "confidence": confidence,
            "fingerprint_changed": fingerprint_changed,
            "fingerprint_values": fingerprint_values}


def series_kql(model=None, since="30d ago"):
    """KQL for the canary's own OTLP records, column names pre-aliased to
    match what classify()/group_by_model() expect (no 'eval.' prefix in
    the output)."""
    filt = f" | where attributes['eval.model'] == '{model}'" if model else ""
    return (
        f"{_TABLE} | where resource['service.name'] == 'berserk-mcp-eval'"
        f"{filt}"
        " | project timestamp,"
        " model=tostring(attributes['eval.model']),"
        " status=tostring(attributes['eval.status']),"
        " case_set_version=tostring(attributes['eval.case_set_version']),"
        " role=tostring(attributes['eval.role']),"
        " discovery_mode=tostring(attributes['eval.discovery_mode']),"
        " tier=tostring(attributes['eval.tier']),"
        " tool_accuracy=toreal(attributes['eval.tool_accuracy']),"
        " arg_accuracy=toreal(attributes['eval.arg_accuracy']),"
        " repeats=toint(attributes['eval.repeats']),"
        " behavioral_fingerprint=tostring(attributes['eval.behavioral_fingerprint']),"
        " provider_metadata_fingerprint=tostring(attributes['eval.provider_metadata_fingerprint'])"
        " | order by timestamp asc"
    )


class BzrkResultParseError(Exception):
    """bzrk_search_json returned output group_by_model could not parse as
    JSON -- most likely an older bzrk build without --json support,
    returning bzrk_search_json's documented plain aligned-table text
    fallback instead (berserk_mcp.py:1536-1546).

    This must never be silently treated the same as a genuinely empty
    result. An earlier version of group_by_model caught the parse failure
    internally and returned {} to avoid an uncaught JSONDecodeError -- but
    that made a parse failure indistinguishable from "no canary results",
    so --drift-report printed "All models stable" and exited 0 on a
    totally broken read path (found by Codex backtest, 2026-09-02). This
    is the same class of bug as fault #4 in
    docs/claude-code-review-feedback-loop.md: a required check that
    cannot tell "healthy" from "no evidence" reports health for both.
    Callers must catch this distinctly and report it as a failure, not
    swallow it into an empty/stable result."""


def group_by_model(bzrk_json_text):
    """Bucket bzrk_search_json's rows by model, in the shape classify()
    consumes. model is required in series_kql's projection precisely so
    this grouping is possible from one query covering every model.

    Reuses agent_analytics._json_records -- NOT a function of this name in
    berserk_mcp.py, which has none; three other modules each carry their own
    copy (agent_analytics.py, ai_finops.py, secret_scan.py). Import from
    agent_analytics.py specifically, since model_drift.py already follows
    its precedent as a runtime analytics module. That function takes
    PARSED JSON (a dict/list), not raw text, and returns None -- never an
    exception -- for a shape it doesn't recognize; both must be handled.

    Raises BzrkResultParseError (not json.JSONDecodeError directly, and
    not swallowed into {}) when the input isn't valid JSON -- see that
    exception's docstring for why this must be loud, not silent. Callers
    are responsible for catching it and reporting a failure."""
    from agent_analytics import _json_records
    if not bzrk_json_text.strip():
        return {}
    try:
        parsed = json.loads(bzrk_json_text)
    except json.JSONDecodeError as exc:
        raise BzrkResultParseError(
            "bzrk output was not JSON -- an older bzrk build without "
            "--json support?"
        ) from exc
    records = _json_records(parsed) or []
    grouped = {}
    for row in records:
        grouped.setdefault(row.get("model", ""), []).append(row)
    return grouped
