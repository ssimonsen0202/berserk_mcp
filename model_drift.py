#!/usr/bin/env python3
"""Verdict logic for model-behavior monitoring (issue #91).

Reads the score series the canary stored in Berserk and answers one
question: is this model still behaving the way it did?

The hard part is not detecting a drop -- it is NOT firing on noise. At 48
cases and repeats=1, one case flipping moves accuracy ~2.1 points. A canary
that alerts on that gets ignored within a week, which is worse than no
canary. Hence: a drop must exceed a measured noise band AND persist across
two consecutive runs before anything fires.

The noise band is measured, not guessed -- see the baselining step in the
implementation plan. DEFAULT_NOISE_BAND below is provisional until that
measurement lands.
"""

import json

MIN_HISTORY = 4          # runs at one case_set_version before any verdict
CONSECUTIVE_REQUIRED = 2  # degraded runs in a row before firing
MIN_TREND_R2 = 0.6        # matches berserk_mcp.py:2913's forecastability floor
DEFAULT_NOISE_BAND = 0.05  # PROVISIONAL -- replace with measured variance

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _usable(series):
    """Drop failed runs. A provider outage is not a score of zero."""
    return [r for r in series if r.get("status") == "ok"]


def _single_version(rows):
    versions = {r.get("case_set_version") for r in rows}
    return versions.pop() if len(versions) == 1 else None


def classify(series, noise_band=DEFAULT_NOISE_BAND, fingerprint_changed=False):
    rows = _usable(series)
    if len(rows) < MIN_HISTORY or _single_version(rows) is None:
        return {"verdict": "insufficient-data",
                "reason": "not enough runs at a single case-set version",
                "confidence": "low"}

    scores = [float(r["tool_accuracy"]) for r in rows]
    baseline = sum(scores[:-CONSECUTIVE_REQUIRED]) / len(scores[:-CONSECUTIVE_REQUIRED])
    recent = scores[-CONSECUTIVE_REQUIRED:]

    degraded = [s for s in recent if baseline - s > noise_band]
    if len(degraded) < CONSECUTIVE_REQUIRED:
        return {"verdict": "stable",
                "reason": "no sustained drop beyond the noise band",
                "confidence": "medium"}

    drop = baseline - min(recent)
    sharp = drop > 2 * noise_band
    verdict = "step-change" if sharp else "degrading"
    confidence = "high" if (sharp and fingerprint_changed) else "medium"
    reason = f"{drop:.3f} below baseline {baseline:.3f} across {CONSECUTIVE_REQUIRED} runs"
    if fingerprint_changed:
        reason += "; provider fingerprint also changed"
    return {"verdict": verdict, "reason": reason, "confidence": confidence}


def series_kql(model=None, since="30d ago"):
    """KQL for the canary's own OTLP records, column names pre-aliased to
    match what classify() expects (no 'eval.' prefix in the output)."""
    filt = f" | where attributes['eval.model'] == '{model}'" if model else ""
    return (
        "default | where resource['service.name'] == 'berserk-mcp-eval'"
        f"{filt}"
        " | project timestamp,"
        " model=tostring(attributes['eval.model']),"
        " status=tostring(attributes['eval.status']),"
        " case_set_version=tostring(attributes['eval.case_set_version']),"
        " tool_accuracy=toreal(attributes['eval.tool_accuracy']),"
        " arg_accuracy=toreal(attributes['eval.arg_accuracy'])"
        " | order by timestamp asc"
    )


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
    exception -- for a shape it doesn't recognize; both must be handled."""
    from agent_analytics import _json_records
    if not bzrk_json_text.strip():
        return {}
    records = _json_records(json.loads(bzrk_json_text)) or []
    grouped = {}
    for row in records:
        grouped.setdefault(row.get("model", ""), []).append(row)
    return grouped
