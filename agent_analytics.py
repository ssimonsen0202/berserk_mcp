"""Agent-log analytics for Claude Code telemetry in Berserk.

This module is stdlib-only and configured by berserk_mcp.py at import time.
It does not import berserk_mcp directly, which keeps tests simple and avoids
cycles. The public functions return text suitable for MCP tool output.
"""
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
import os
import re

_bzrk_search = None
_table = None

# Confirmed live 2026-07-17 -- these ARE the real ingest attribute names.
# Kept env-overridable in case a different forwarder config ever uses
# something else (e.g. 'claude.usage.input_tokens'); when the names don't
# match, every session falls back to the labeled body-length estimate
# rather than reporting exact tokens.
_TOKENS_IN_ATTR = os.environ.get("BERSERK_MCP_TOKENS_IN_ATTR", "claude.tokens_input")
_TOKENS_OUT_ATTR = os.environ.get("BERSERK_MCP_TOKENS_OUT_ATTR", "claude.tokens_output")

# Path segments that mark "inside a project" for cost attribution; the
# directory immediately before the first marker is taken as the project name.
_PROJECT_MARKERS = frozenset(
    p.strip() for p in os.environ.get(
        "BERSERK_MCP_PROJECT_MARKERS", "src,tests,lib,pkg"
    ).split(",") if p.strip()
)

MODEL_TIERS = {
    "opus": "frontier",
    "fable": "frontier",
    "sonnet": "mid",
    "haiku": "cheap",
    "mini": "cheap",
}


def _identity(text):
    return text


_redact = _identity


def configure(bzrk_search, table, redact=None):
    """redact: optional callable(str)->str used to scrub secret-bearing body
    snippets before they appear in tool output (roadmap A1 requires it, and the
    global dispatch filter only *removes* secrets in redact mode — so relying
    on that alone would still echo secrets in the default flag/off modes)."""
    global _bzrk_search, _table, _redact
    _bzrk_search = bzrk_search
    _table = table
    _redact = redact or _identity


def _events_query():
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| project session=tostring(attributes['claude.session_id']), "
        f"ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"err=tostring(attributes['claude.error']), "
        f"body=substring(tostring(body), 0, 80) "
        f"| take 2000"
    )


def _burn_events_query():
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| project session=tostring(attributes['claude.session_id']), "
        f"ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"file_targets=tostring(attributes['claude.file_targets']), "
        f"err=tostring(attributes['claude.error']), "
        f"body=substring(tostring(body), 0, 240), "
        f"body_chars=strlen(tostring(body)) "
        f", tokens_in=tostring(attributes['{_TOKENS_IN_ATTR}']) "
        f", tokens_out=tostring(attributes['{_TOKENS_OUT_ATTR}']) "
        f"| take 2000"
    )


def _json_records(parsed):
    """Extract a list of row-dicts from a whole-document JSON value: a bare
    array, a wrapper object keying the rows under a common name, or bzrk's
    real `--json` shape (verified live 2026-07-17):
    {"Tables": [{"schema": {"columns": [{"name": ...}, ...]}, "rows": [[...]]}], ...}
    -- rows there are positional arrays matching column order, not dicts, so
    they're zipped against the column names first. Returns None when the
    value isn't a recognizable record container (so the caller falls through
    to jsonl / table parsing)."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        tables = parsed.get("Tables")
        if isinstance(tables, list) and tables and isinstance(tables[0], dict):
            table = tables[0]
            columns = [
                c.get("name") for c in (table.get("schema") or {}).get("columns", [])
                if isinstance(c, dict)
            ]
            rows = table.get("rows")
            if columns and isinstance(rows, list):
                return [
                    dict(zip(columns, row)) for row in rows if isinstance(row, list)
                ]
        for key in ("rows", "data", "results", "records"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
    return None


def _parse_rows(text):
    rows = []
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)":
        return rows

    # Whole-document JSON (array or {rows:[...]}) — what `--json` yields on
    # bzrk builds that support it. jsonl and single objects fall through below.
    if whole[0] in "[{":
        try:
            records = _json_records(json.loads(whole))
        except json.JSONDecodeError:
            records = None
        if records is not None:
            return [_normalize_row(o) for o in records if isinstance(o, dict)]

    lines = [ln.strip() for ln in whole.splitlines() if ln.strip()]
    for ln in lines:
        if not ln.startswith("{"):
            break
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            break
        rows.append(_normalize_row(obj))
    if rows:
        return rows

    header = re.split(r"\s{2,}|\t+", lines[0].strip())
    if len(header) < 2:
        header = lines[0].split()
    wanted = {"session", "ts", "typ", "model", "tools", "file_targets", "err", "body"}
    if not set(header) & wanted:
        return rows
    for ln in lines[1:]:
        parts = re.split(r"\s{2,}|\t+", ln.strip(), maxsplit=len(header) - 1)
        if len(parts) < len(header):
            parts = ln.split(maxsplit=len(header) - 1)
        obj = {header[i]: parts[i] if i < len(parts) else "" for i in range(len(header))}
        rows.append(_normalize_row(obj))
    return rows


def _normalize_row(obj):
    tokens_in = obj.get("tokens_in", obj.get("input_tokens"))
    tokens_out = obj.get("tokens_out", obj.get("output_tokens"))
    return {
        "session": str(obj.get("session") or obj.get("session_id") or "(none)"),
        "ts": str(obj.get("ts") or obj.get("timestamp") or ""),
        "typ": str(obj.get("typ") or obj.get("type") or ""),
        "model": str(obj.get("model") or ""),
        "tools": str(obj.get("tools") or obj.get("tool_names") or ""),
        "file_targets": str(obj.get("file_targets") or ""),
        "err": str(obj.get("err") or obj.get("error") or "").lower(),
        "body": str(obj.get("body") or ""),
        "body_chars": _nonnegative_int(obj.get("body_chars")) or len(str(obj.get("body") or "")),
        "tokens_in": _nonnegative_int(tokens_in),
        "tokens_out": _nonnegative_int(tokens_out),
        "has_token_usage": _valid_token_value(tokens_in) or _valid_token_value(tokens_out),
    }


def _valid_token_value(value):
    if value in (None, ""):
        return False
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def _nonnegative_int(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _tool_names(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _target(body):
    compact = re.sub(r"\s+", " ", str(body or "")).strip()
    return compact[:60] if compact else "-"


def _calls_for_events(events):
    calls = []
    for ev in events:
        # Compute per-event values once; _target runs a regex sub, so calling
        # it twice per tool (as before) doubled that work for no benefit.
        target = _target(ev.get("body"))
        err = ev.get("err") == "true"
        ts = ev.get("ts", "")
        for tool in _tool_names(ev.get("tools")):
            calls.append({
                "tool": tool,
                "target": target,
                "key": f"{tool}({target})",
                "err": err,
                "ts": ts,
            })
    return calls


def _parse_ts(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(events):
    stamps = [_parse_ts(ev.get("ts")) for ev in events]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return 0
    return max(0, int((max(stamps) - min(stamps)).total_seconds()))


def _oscillation_count(seq):
    count = 0
    for size in (2, 3):
        for i in range(0, max(0, len(seq) - (size * 2) + 1)):
            if seq[i:i + size] == seq[i + size:i + (size * 2)]:
                count += 1
    return count


def analyze_loop_events(events):
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)

    reports = []
    for session, session_events in sorted(by_session.items()):
        session_events.sort(key=lambda ev: ev.get("ts", ""))
        calls = _calls_for_events(session_events)
        total = len(calls)
        distinct = len({c["key"] for c in calls})
        repetition_ratio = 1.0 - (float(distinct) / float(total)) if total else 0.0
        error_retries = 0
        for prev, nxt in zip(calls, calls[1:]):
            if prev["err"] and prev["tool"] == nxt["tool"]:
                error_retries += 1
        osc = _oscillation_count([c["key"] for c in calls])
        top_call, top_count = ("-", 0)
        if calls:
            top_call, top_count = Counter(c["key"] for c in calls).most_common(1)[0]

        if repetition_ratio >= 0.70 or error_retries >= 3 or osc >= 2:
            verdict = "likely-looping"
        elif repetition_ratio >= 0.40 or error_retries >= 1 or osc >= 1:
            verdict = "some-repetition"
        else:
            verdict = "healthy"

        reports.append({
            "session_id": session,
            "total_tool_calls": total,
            "distinct_tool_calls": distinct,
            "repetition_ratio": repetition_ratio,
            "top_repeated_call": top_call,
            "top_repeated_count": top_count,
            "error_retry_count": error_retries,
            "oscillation_count": osc,
            "verdict": verdict,
            "duration_seconds": _duration_seconds(session_events),
            "models": sorted({ev.get("model", "") for ev in session_events if ev.get("model")}),
        })
    return reports


def _fetch_events(since):
    return _bzrk_search(_events_query(), since)


def claude_loop_check(since="6h ago"):
    text, is_err = _fetch_events(since)
    if is_err:
        return text, True
    reports = analyze_loop_events(_parse_rows(text))
    if not reports:
        return "No Claude Code tool-call events found in this window.", False
    lines = ["Claude Code loop check:"]
    for r in reports:
        # top_repeated_call embeds up to 60 chars of a message body; scrub any
        # secrets before echoing so this holds regardless of the global filter
        # mode (roadmap A1: "no raw secret-bearing body is echoed").
        top = _redact(r["top_repeated_call"])
        lines.append(
            "- {session_id}: verdict={verdict}, calls={total_tool_calls}, "
            "repetition={repetition_ratio:.2f}, error_retries={error_retry_count}, "
            "top={top} x{top_repeated_count}".format(top=top, **r)
        )
    return "\n".join(lines), False


def _model_tier(model):
    low = str(model or "").lower()
    for needle, tier in MODEL_TIERS.items():
        if needle in low:
            return tier
    return "unknown"


def _complexity_bucket(loop_report, events):
    total = loop_report["total_tool_calls"]
    errors = sum(1 for ev in events if ev.get("err") == "true")
    duration = loop_report["duration_seconds"]
    distinct_tools = len({c["tool"] for c in _calls_for_events(events)})
    if total <= 3 and errors == 0 and duration <= 120:
        return "trivial"
    if total >= 25 or errors >= 5 or duration >= 3600 or loop_report["verdict"] == "likely-looping":
        return "complex"
    if distinct_tools >= 4 or total >= 8 or errors:
        return "moderate"
    return "trivial"


def analyze_model_fit_events(events):
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)
    loop_by_session = {r["session_id"]: r for r in analyze_loop_events(events)}

    reports = []
    for session, session_events in sorted(by_session.items()):
        loop_report = loop_by_session.get(session)
        if not loop_report:
            continue
        # _events_query uses an unordered bounded take; preserve the prior
        # "latest observed model" behavior explicitly at the consumer.
        ordered_events = sorted(
            session_events, key=lambda ev: str(ev.get("ts", ""))
        )
        models = [ev.get("model", "") for ev in ordered_events if ev.get("model")]
        model = models[-1] if models else ""
        tier = _model_tier(model)
        bucket = _complexity_bucket(loop_report, session_events)
        if tier == "frontier" and bucket == "trivial":
            verdict = "overpowered (consider a cheaper model)"
            rationale = "frontier-tier model on a short, low-error session"
        elif tier == "cheap" and bucket == "complex" and loop_report["verdict"] != "healthy":
            verdict = "underpowered (consider escalating)"
            rationale = "cheap-tier model on a complex/repetitive session"
        else:
            verdict = "ok"
            rationale = "model tier roughly matches observed complexity"
        reports.append({
            "session_id": session,
            "model": model or "unknown",
            "model_tier": tier,
            "complexity": bucket,
            "verdict": verdict,
            "rationale": rationale,
            "loop_verdict": loop_report["verdict"],
            "tool_calls": loop_report["total_tool_calls"],
        })
    return reports


def claude_model_fit(since="6h ago"):
    text, is_err = _fetch_events(since)
    if is_err:
        return text, True
    reports = analyze_model_fit_events(_parse_rows(text))
    if not reports:
        return "No Claude Code model events found in this window.", False
    over = sum(1 for r in reports if r["verdict"].startswith("overpowered"))
    under = sum(1 for r in reports if r["verdict"].startswith("underpowered"))
    lines = [
        "Claude Code model fit (heuristic, not a billing statement):",
        f"Summary: {over} sessions overpowered, {under} underpowered.",
    ]
    for r in reports:
        lines.append(
            "- {session_id}: tier={model_tier}, complexity={complexity}, "
            "verdict={verdict}; {rationale}".format(**r)
        )
    return "\n".join(lines), False


_FILE_TOOLS = {"edit", "glob", "grep", "read", "write"}


def _file_targets(events):
    targets = set()
    for ev in events:
        file_targets = str(ev.get("file_targets") or "")
        if file_targets:
            for target in file_targets.split(","):
                target = target.strip()
                if target:
                    targets.add(target)
            continue
        body = _target(ev.get("body"))
        for tool in _tool_names(ev.get("tools")):
            if tool.lower() in _FILE_TOOLS and body != "-":
                targets.add(body)
    return targets


def _infer_project(path_text):
    """Deterministic project name from a file path: the directory segment
    immediately before the first marker segment (src/tests/lib/pkg by
    default; BERSERK_MCP_PROJECT_MARKERS overrides). '(unattributed)'
    when no marker with a parent exists. Windows separators normalized."""
    parts = [p for p in str(path_text or "").replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        if part in _PROJECT_MARKERS and i > 0:
            return parts[i - 1]
    return "(unattributed)"


def analyze_token_burn_events(events):
    """Calculate token burn, falling back to body size when usage is absent."""
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)
    loop_by_session = {r["session_id"]: r for r in analyze_loop_events(events)}

    reports = []
    for session, session_events in sorted(by_session.items()):
        body_chars = sum(
            _nonnegative_int(ev.get("body_chars")) or len(str(ev.get("body") or ""))
            for ev in session_events
        )
        has_exact_usage = any(ev.get("has_token_usage") for ev in session_events)
        exact_tokens = sum(
            _nonnegative_int(ev.get("tokens_in")) + _nonnegative_int(ev.get("tokens_out"))
            for ev in session_events
        )
        token_count = exact_tokens if has_exact_usage else int(math.ceil(body_chars / 4.0))
        calls = _calls_for_events(session_events)
        distinct_tools = len({c["tool"] for c in calls})
        files_touched = len(_file_targets(session_events))
        progress_units = distinct_tools + files_touched
        burn_per_progress = token_count / float(max(1, progress_units))
        loop_verdict = loop_by_session.get(session, {}).get("verdict", "healthy")
        reports.append({
            "session_id": session,
            "tokens": token_count,
            "token_source": "exact" if has_exact_usage else "estimated",
            "body_chars": body_chars,
            "tool_calls": len(calls),
            "distinct_tools": distinct_tools,
            "files_touched": files_touched,
            "progress_units": progress_units,
            "burn_per_progress": burn_per_progress,
            "loop_verdict": loop_verdict,
            "verdict": "normal-burn",
        })

    if reports:
        high_burn_count = max(1, int(math.ceil(len(reports) * 0.1)))
        ranked = sorted(
            reports,
            key=lambda r: (-r["burn_per_progress"], r["session_id"]),
        )
        high_burn_sessions = {
            r["session_id"] for r in ranked[:high_burn_count] if r["tokens"] > 0
        }
        for report in reports:
            if report["session_id"] in high_burn_sessions:
                report["verdict"] = (
                    "high-burn + likely-looping"
                    if report["loop_verdict"] == "likely-looping"
                    else "high-burn"
                )
    return reports


def _cost_daily_query():
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| summarize events=count(), "
        f"errors=countif(tostring(attributes['claude.error']) == 'true'), "
        f"tokens_in_sum=sum(toint(attributes['{_TOKENS_IN_ATTR}'])), "
        f"tokens_out_sum=sum(toint(attributes['{_TOKENS_OUT_ATTR}'])), "
        f"body_chars_sum=sum(strlen(tostring(body))) "
        f"by day=bin(timestamp, 1d), model=tostring(attributes['claude.message_model']) "
        f"| sort by day asc"
    )


def _cost_trend_query():
    """Build the native daily burn series and least-squares fit.

    The daily rollup remains a separate query because it preserves the
    exact/estimated labels and per-model split. This companion query moves
    only the trend calculation into Berserk's series engine.
    """
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| extend burn=iff(isnotnull(attributes['{_TOKENS_IN_ATTR}']) "
        f"or isnotnull(attributes['{_TOKENS_OUT_ATTR}']), "
        f"toint(attributes['{_TOKENS_IN_ATTR}']) + toint(attributes['{_TOKENS_OUT_ATTR}']), "
        f"strlen(tostring(body)) / 4.0) "
        f"| make-series burn=sum(burn) default=0 on timestamp step 1d "
        f"| extend fit=series_fit_line(burn) | take 1"
    )


def _trend_fit(text):
    """Read ``series_fit_line``'s [R², slope, ..., line] result."""
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)":
        return None
    # JSON mode is preferred by production wiring; retain a small TSV fallback
    # for test doubles and older bzrk renderers.
    records = None
    if whole[0] in "[{":
        try:
            records = _json_records(json.loads(whole))
        except json.JSONDecodeError:
            records = None
    if records:
        value = records[0].get("fit") if isinstance(records[0], dict) else None
        if isinstance(value, list) and len(value) >= 2:
            try:
                return {"r2": float(value[0]), "slope": float(value[1])}
            except (TypeError, ValueError):
                return None
    return None


_SLOPE_FLAT_PCT = 10.0  # |slope| below this %/day of mean burn counts as flat


def analyze_cost_daily(rows):
    """Aggregate per-day+model cost rows into a report dict. Pure function.

    Per-day tokens prefer the exact sums; a day with no exact usage falls
    back to body_chars_sum / 4 and is labeled "estimated" (doctrine from
    claude_token_burn). Verdict is a least-squares slope over daily totals,
    expressed as %/day of the mean; needs >= 3 days else insufficient-data.
    """
    by_day = {}
    models = {}
    for r in rows:
        day = str(r.get("day") or "")[:10]
        if not day:
            continue
        exact = _nonnegative_int(r.get("tokens_in_sum")) + _nonnegative_int(r.get("tokens_out_sum"))
        est = _nonnegative_int(r.get("body_chars_sum")) // 4
        tokens = exact if exact > 0 else est
        source = "exact" if exact > 0 else "estimated"
        slot = by_day.setdefault(day, {"day": day, "tokens": 0, "source": source,
                                       "events": 0, "errors": 0})
        slot["tokens"] += tokens
        if source == "estimated" and slot["tokens"] == tokens:
            slot["source"] = "estimated"
        slot["events"] += _nonnegative_int(r.get("events"))
        slot["errors"] += _nonnegative_int(r.get("errors"))
        model = str(r.get("model") or "").strip()
        if model:
            models[model] = models.get(model, 0) + tokens

    days = sorted(by_day.values(), key=lambda d: d["day"])
    if len(days) < 3:
        return {"days": days, "models": models,
                "verdict": "insufficient-data", "slope_pct_per_day": 0.0,
                "r2": None}

    ys = [d["tokens"] for d in days]
    n = len(ys)
    mean_x = (n - 1) / 2.0
    mean_y = sum(ys) / float(n)
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    den = sum((i - mean_x) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    slope_pct = (slope / mean_y * 100.0) if mean_y else 0.0

    if slope_pct > _SLOPE_FLAT_PCT:
        verdict = "burn-growing"
    elif slope_pct < -_SLOPE_FLAT_PCT:
        verdict = "burn-declining"
    else:
        verdict = "burn-flat"
    return {"days": days, "models": models,
            "verdict": verdict, "slope_pct_per_day": round(slope_pct, 1),
            "r2": None}


def claude_token_burn(since="6h ago"):
    text, is_err = _bzrk_search(_burn_events_query(), since)
    if is_err:
        return text, True
    reports = analyze_token_burn_events(_parse_rows(text))
    if not reports:
        return "No Claude Code events found for token-burn analysis.", False
    exact = sum(1 for r in reports if r["token_source"] == "exact")
    estimated = len(reports) - exact
    lines = [
        "Claude Code token burn (exact usage when present; body-length fallback otherwise):",
        f"Coverage: {exact} exact sessions, {estimated} estimated sessions. Estimates use body characters / 4.",
    ]
    for r in reports:
        lines.append(
            "- {session_id}: tokens={tokens} ({token_source}), "
            "burn_per_progress={burn_per_progress:.1f}, distinct_tools={distinct_tools}, "
            "files_touched={files_touched}, loop={loop_verdict}, verdict={verdict}".format(**r)
        )
    return "\n".join(lines), False


def _daily_rows(text):
    """Parse --json output rows for the daily cost query (generic dicts,
    NOT _normalize_row'd — that helper is for event rows)."""
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)":
        return []
    if whole[0] in "[{":
        try:
            records = _json_records(json.loads(whole))
        except json.JSONDecodeError:
            records = None
        if records is not None:
            return [r for r in records if isinstance(r, dict)]
    rows = []
    for ln in whole.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            return []
    return rows


def claude_cost_report(since="7d ago", group_by="day"):
    """Multi-day Claude Code cost report. NOT yet live-verified (J0 pending)."""
    group_by = str(group_by or "day").strip().lower()
    if group_by not in ("day", "model", "project"):
        return "invalid group_by: use 'day', 'model', or 'project'", True

    if group_by == "project":
        text, is_err = _bzrk_search(_burn_events_query(), since)
        if is_err:
            return text, True
        events = _parse_rows(text)
        if not events:
            return "No Claude Code events found in this window.", False
        by_project = {}
        for ev in events:
            tokens = (
                _nonnegative_int(ev.get("tokens_in")) + _nonnegative_int(ev.get("tokens_out"))
            ) if ev.get("has_token_usage") else len(ev.get("body", "")) // 4
            project = "(unattributed)"
            for tgt in _file_targets([ev]):
                inferred = _infer_project(tgt)
                if inferred != "(unattributed)":
                    project = inferred
                    break
            slot = by_project.setdefault(project, {"tokens": 0, "events": 0})
            slot["tokens"] += tokens
            slot["events"] += 1
        lines = [f"Claude Code cost by project (most recent {len(events)} events; "
                 f"window {since}):"]
        for name in sorted(by_project, key=lambda k: -by_project[k]["tokens"]):
            s = by_project[name]
            lines.append(f"- {_redact(name)}: ~{s['tokens']} tokens across {s['events']} events")
        return "\n".join(lines), False

    text, is_err = _bzrk_search(_cost_daily_query(), since)
    if is_err:
        return text, True
    rows = _daily_rows(text)
    if not rows:
        return "No Claude Code activity found in this window.", False
    rep = analyze_cost_daily(rows)
    # The daily rollup preserves exact/estimated labels. Use the native series
    # fit for the verdict when the backend returns it, with the Python fit as a
    # compatibility fallback for older deployments and test doubles.
    trend_text, trend_err = _bzrk_search(_cost_trend_query(), since)
    trend = _trend_fit(trend_text) if not trend_err else None
    if trend is not None and len(rep["days"]) >= 3:
        mean_tokens = sum(d["tokens"] for d in rep["days"]) / len(rep["days"])
        slope_pct = (trend["slope"] / mean_tokens * 100.0) if mean_tokens else 0.0
        if slope_pct > _SLOPE_FLAT_PCT:
            rep["verdict"] = "burn-growing"
        elif slope_pct < -_SLOPE_FLAT_PCT:
            rep["verdict"] = "burn-declining"
        else:
            rep["verdict"] = "burn-flat"
        rep["slope_pct_per_day"] = round(slope_pct, 1)
        rep["r2"] = trend["r2"]
    trend_marker = f", R²={rep['r2']:.2f}" if rep.get("r2") is not None else ""
    lines = [f"Claude Code cost report ({since}): verdict={rep['verdict']} "
             f"(slope {rep['slope_pct_per_day']:+.1f}%/day{trend_marker})"]
    if group_by == "model":
        for model in sorted(rep["models"], key=lambda k: -rep["models"][k]):
            lines.append(f"- {model}: ~{rep['models'][model]} tokens")
    else:
        for d in rep["days"]:
            lines.append(f"- {d['day']}: ~{d['tokens']} tokens ({d['source']}), "
                         f"{d['events']} events, {d['errors']} errors")
    return "\n".join(lines), False


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GAP_SECONDS = 300


def _session_events_query(session_id):
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| where attributes['claude.session_id'] == '{session_id}' "
        f"| project session=tostring(attributes['claude.session_id']), "
        f"ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"err=tostring(attributes['claude.error']), "
        f"body=substring(tostring(body), 0, 240), "
        f"body_chars=strlen(tostring(body)) "
        f", tokens_in=tostring(attributes['{_TOKENS_IN_ATTR}']) "
        f", tokens_out=tostring(attributes['{_TOKENS_OUT_ATTR}']) "
        f"| sort by ts asc | take 2000"
    )


def analyze_session_events(events):
    """Timeline analysis for one session's events. Pure function."""
    phases = []
    gaps = []
    prev_ts = None
    for ev in events:
        names = _tool_names(ev.get("tools")) or ["(no-tool)"]
        tool = names[0]
        err = 1 if ev.get("err") == "true" else 0
        ts = ev.get("ts", "")
        cur = _parse_ts(ts)
        if prev_ts is not None and cur is not None:
            delta = (cur - prev_ts).total_seconds()
            if delta > _GAP_SECONDS:
                gaps.append({"after_ts": ts, "seconds": int(delta)})
        if cur is not None:
            prev_ts = cur
        if phases and phases[-1]["tool"] == tool:
            phases[-1]["count"] += 1
            phases[-1]["errors"] += err
            phases[-1]["last_ts"] = ts
        else:
            phases.append({"tool": tool, "count": 1, "errors": err,
                           "first_ts": ts, "last_ts": ts})
    exact = sum(_nonnegative_int(ev.get("tokens_in")) + _nonnegative_int(ev.get("tokens_out"))
                for ev in events if ev.get("has_token_usage"))
    if exact > 0:
        burn = {"tokens": exact, "source": "exact"}
    else:
        burn = {"tokens": sum(
                    _nonnegative_int(ev.get("body_chars")) or len(ev.get("body", ""))
                    for ev in events
                ) // 4,
                "source": "estimated"}
    loops = analyze_loop_events(events)
    loop_verdict = loops[0]["verdict"] if loops else "no-tool-calls"
    return {"phases": phases, "gaps": gaps, "burn": burn, "loop": loop_verdict}


def claude_session_deep_dive(session_id, since="24h ago"):
    """Timeline + burn + loop drilldown for one session. NOT yet live-verified."""
    sid = str(session_id or "").strip()
    if not _SESSION_ID_RE.match(sid):
        return "invalid session_id (allowed: letters, digits, '.', '_', '-')", True
    text, is_err = _bzrk_search(_session_events_query(sid), since)
    if is_err:
        return text, True
    events = _parse_rows(text)
    if not events:
        return f"No data for session {sid} in this window.", False
    rep = analyze_session_events(events)
    lines = [f"Session {sid} deep dive ({since}): loop={rep['loop']}, "
             f"~{rep['burn']['tokens']} tokens ({rep['burn']['source']})"]
    for p in rep["phases"]:
        marker = f", {p['errors']} errors" if p["errors"] else ""
        lines.append(f"- {p['first_ts']} {p['tool']} x{p['count']}{marker}")
    for g in rep["gaps"]:
        lines.append(f"- gap of {g['seconds']}s before {g['after_ts']}")
    return "\n".join(lines), False


def analyze_workflow_events(events):
    """Aggregate workflow patterns across sessions. Pure function."""
    by_session = {}
    for ev in events:
        by_session.setdefault(ev.get("session", "(none)"), []).append(ev)

    seq_counts = {}
    for sess_events in by_session.values():
        # The query intentionally uses an unordered `take` so Berserk can
        # stop early. Restore chronological order before adjacency analysis.
        stamps = [str(ev.get("ts", "")) for ev in sess_events]
        # Equal timestamps have no meaningful ordering; preserve the source
        # order for ties (some forwarders batch several events at one stamp).
        if len(set(stamps)) == len(stamps) and any(
            left > right for left, right in zip(stamps, stamps[1:])
        ):
            sess_events = sorted(sess_events, key=lambda ev: str(ev.get("ts", "")))
        tools = []
        for ev in sess_events:
            tools.extend(_tool_names(ev.get("tools")))
        for size in (2, 3):
            for i in range(len(tools) - size + 1):
                pattern = "→".join(tools[i:i + size])
                seq_counts[pattern] = seq_counts.get(pattern, 0) + 1
    sequences = [{"pattern": p, "count": c}
                 for p, c in sorted(seq_counts.items(), key=lambda kv: -kv[1])[:10]
                 if c >= 2]

    call_stats = {}
    for call in _calls_for_events(events):
        slot = call_stats.setdefault(call["key"], {"errors": 0, "calls": 0})
        slot["calls"] += 1
        if call["err"]:
            slot["errors"] += 1
    hotspots = [{"key": k, "errors": v["errors"], "calls": v["calls"]}
                for k, v in sorted(call_stats.items(), key=lambda kv: -kv[1]["errors"])
                if v["errors"] >= 2][:10]

    burn_rank = []
    for session, sess_events in by_session.items():
        exact = sum(_nonnegative_int(ev.get("tokens_in")) + _nonnegative_int(ev.get("tokens_out"))
                    for ev in sess_events if ev.get("has_token_usage"))
        tokens = exact if exact > 0 else sum(
            _nonnegative_int(ev.get("body_chars")) or len(ev.get("body", ""))
            for ev in sess_events
        ) // 4
        targets = max(1, len(_file_targets(sess_events)))
        burn_rank.append({"session": session,
                          "tokens_per_target": tokens // targets})
    burn_rank.sort(key=lambda r: -r["tokens_per_target"])
    decile = max(1, len(burn_rank) // 10)
    inefficient = burn_rank[:decile] if len(burn_rank) >= 3 else []

    return {"sequences": sequences, "hotspots": hotspots, "inefficient": inefficient}


def claude_workflow_insights(since="7d ago"):
    """Cross-session workflow patterns. NOT yet live-verified (J0 pending)."""
    text, is_err = _bzrk_search(_burn_events_query(), since)
    if is_err:
        return text, True
    events = _parse_rows(text)
    if not events:
        return "No Claude Code events found in this window.", False
    rep = analyze_workflow_events(events)
    lines = [f"Claude Code workflow insights ({since}, {len(events)} events):"]
    if rep["sequences"]:
        lines.append("Top tool sequences:")
        for s in rep["sequences"][:5]:
            lines.append(f"- {s['pattern']} x{s['count']}")
    else:
        lines.append("Top tool sequences: (not enough repeated activity)")
    if rep["hotspots"]:
        lines.append("Error hotspots (>=2 errors):")
        for h in rep["hotspots"][:5]:
            lines.append(f"- {_redact(h['key'])}: {h['errors']}/{h['calls']} failed")
    else:
        lines.append("Error hotspots: none")
    if rep["inefficient"]:
        lines.append("Top-decile burn per distinct target:")
        for r in rep["inefficient"][:5]:
            lines.append(f"- {r['session']}: ~{r['tokens_per_target']} tokens/target")
    return "\n".join(lines), False


def agent_report(since="6h ago"):
    loop_text, loop_err = claude_loop_check(since)
    fit_text, fit_err = claude_model_fit(since)
    burn_text, burn_err = claude_token_burn(since)
    text = loop_text + "\n\n" + fit_text + "\n\n" + burn_text
    # Alert only on absolute-threshold verdicts. "high-burn" is a *relative*
    # top-decile ranking — analyze_token_burn_events always marks at least one
    # session high-burn whenever any session has tokens>0 (max(1, ...)), so
    # including it here would make the cron exit code fire on essentially every
    # run and carry no signal. High-burn still appears in the report body.
    should_alert = any(marker in text for marker in ("likely-looping", "underpowered"))
    return text, (loop_err or fit_err or burn_err or should_alert)
