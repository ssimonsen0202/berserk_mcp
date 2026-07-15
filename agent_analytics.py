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

# The Claude Code OTel forwarder's token-usage attribute names are not
# guaranteed — roadmap Phase B calls for verifying them against live Berserk.
# These defaults are a best guess; override via env when the real ingest names
# differ (e.g. 'claude.usage.input_tokens') without a code change. When the
# names don't match, every session simply falls back to the labeled body-length
# estimate rather than reporting exact tokens.
_TOKENS_IN_ATTR = os.environ.get("BERSERK_MCP_TOKENS_IN_ATTR", "claude.tokens_input")
_TOKENS_OUT_ATTR = os.environ.get("BERSERK_MCP_TOKENS_OUT_ATTR", "claude.tokens_output")

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
        f"| sort by session asc, ts asc | take 2000"
    )


def _burn_events_query():
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        f"| project session=tostring(attributes['claude.session_id']), "
        f"ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"err=tostring(attributes['claude.error']), body=tostring(body) "
        f", tokens_in=tostring(attributes['{_TOKENS_IN_ATTR}']) "
        f", tokens_out=tostring(attributes['{_TOKENS_OUT_ATTR}']) "
        f"| sort by session asc, ts asc | take 2000"
    )


def _json_records(parsed):
    """Extract a list of row-dicts from a whole-document JSON value: a bare
    array, or a wrapper object keying the rows under a common name. Returns
    None when the value isn't a recognizable record container (so the caller
    falls through to jsonl / table parsing)."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
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
    wanted = {"session", "ts", "typ", "model", "tools", "err", "body"}
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
        "err": str(obj.get("err") or obj.get("error") or "").lower(),
        "body": str(obj.get("body") or ""),
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
        models = [ev.get("model", "") for ev in session_events if ev.get("model")]
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
        body = _target(ev.get("body"))
        for tool in _tool_names(ev.get("tools")):
            if tool.lower() in _FILE_TOOLS and body != "-":
                targets.add(body)
    return targets


def analyze_token_burn_events(events):
    """Calculate token burn, falling back to body size when usage is absent."""
    by_session = defaultdict(list)
    for ev in events:
        by_session[ev["session"]].append(ev)
    loop_by_session = {r["session_id"]: r for r in analyze_loop_events(events)}

    reports = []
    for session, session_events in sorted(by_session.items()):
        body_chars = sum(len(str(ev.get("body") or "")) for ev in session_events)
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
