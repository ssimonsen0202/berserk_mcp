"""Enterprise AI spend, attribution, BI, and harness-efficiency analytics.

The module is deliberately standard-library only.  ``berserk_mcp`` injects a
bounded Berserk search callable and filesystem locations at startup; pure
functions remain independently testable without a live cluster.
"""
from collections import defaultdict
from datetime import datetime, timezone
import argparse
import csv
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import threading

import _http
import _store


SCHEMA_VERSION = "1.0"
MAX_USAGE_ROWS = 2000
MIN_RECOMMENDATION_EVENTS = 5
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_search = None
_table = "default"
_redact = lambda value: str(value)
_redact_aggressive = lambda value: str(value)
_catalog_path = None
_business_store_path = None
_decision_store_path = None
_report_dir = None
_otlp_endpoint = ""
_otlp_headers = ""
_store_lock = threading.RLock()


def configure(search, table="default", redact=None, redact_aggressive=None, catalog_path=None,
              business_store_path=None, decision_store_path=None,
              report_dir=None, otlp_endpoint="", otlp_headers=""):
    """Inject runtime dependencies without importing ``berserk_mcp``."""
    global _search, _table, _redact, _redact_aggressive, _catalog_path, _business_store_path
    global _decision_store_path, _report_dir, _otlp_endpoint, _otlp_headers
    _search = search
    _table = str(table or "default")
    _redact = redact or (lambda value: str(value))
    _redact_aggressive = redact_aggressive or _redact
    _catalog_path = Path(catalog_path) if catalog_path else None
    _business_store_path = Path(business_store_path) if business_store_path else None
    _decision_store_path = Path(decision_store_path) if decision_store_path else None
    _report_dir = Path(report_dir) if report_dir else None
    _otlp_endpoint = str(otlp_endpoint or "").strip()
    _otlp_headers = str(otlp_headers or "").strip()


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _nonnegative_int(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _nonnegative_float(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "ok", "success"}


def _timestamp_text(value):
    """Normalize Berserk JSON epoch timestamps while preserving ISO input."""
    if isinstance(value, (int, float)) or re.fullmatch(r"-?\d+(?:\.\d+)?", str(value or "")):
        try:
            numeric = float(value)
            magnitude = abs(numeric)
            if magnitude >= 1e17:  # nanoseconds
                numeric /= 1_000_000_000.0
            elif magnitude >= 1e14:  # microseconds
                numeric /= 1_000_000.0
            elif magnitude >= 1e11:  # milliseconds
                numeric /= 1_000.0
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (OverflowError, OSError, TypeError, ValueError):
            pass
    return str(value or "")


def _first(*values):
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return ""


def _nested(obj, bag, key):
    value = obj.get(bag)
    return value.get(key) if isinstance(value, dict) else None


def _json_records(value):
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    tables = value.get("Tables")
    if isinstance(tables, list) and tables and isinstance(tables[0], dict):
        table = tables[0]
        columns = [
            col.get("name") for col in (table.get("schema") or {}).get("columns", [])
            if isinstance(col, dict) and col.get("name")
        ]
        rows = table.get("rows")
        if columns and isinstance(rows, list):
            return [dict(zip(columns, row)) for row in rows if isinstance(row, list)]
    for key in ("rows", "data", "results", "records"):
        if isinstance(value.get(key), list):
            return [row for row in value[key] if isinstance(row, dict)]
    return []


def parse_records(text):
    """Parse bzrk JSON, JSON arrays/wrappers, or JSONL defensively."""
    raw = str(text or "").strip()
    if not raw or raw == "(no rows)":
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(value, dict):
                rows.append(value)
        return rows
    return _json_records(parsed)


def normalize_usage_row(obj):
    """Map native Claude OTel and legacy forwarder rows to one schema."""
    attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
    resource = obj.get("resource") if isinstance(obj.get("resource"), dict) else {}

    input_raw = _first(
        obj.get("tokens_in_sum"), obj.get("input_tokens"), obj.get("tokens_in"),
        attrs.get("input_tokens"), attrs.get("gen_ai.usage.input_tokens"),
        attrs.get("claude.tokens_input"),
    )
    output_raw = _first(
        obj.get("tokens_out_sum"), obj.get("output_tokens"), obj.get("tokens_out"),
        attrs.get("output_tokens"), attrs.get("gen_ai.usage.output_tokens"),
        attrs.get("claude.tokens_output"),
    )
    cache_read_raw = _first(
        obj.get("cache_read_tokens_sum"), obj.get("cache_read_tokens"),
        obj.get("cache_read"), attrs.get("cache_read_tokens"),
        attrs.get("cache_read_input_tokens"), attrs.get("claude.cache_read_tokens"),
    )
    cache_create_raw = _first(
        obj.get("cache_creation_tokens_sum"), obj.get("cache_creation_tokens"),
        obj.get("cache_create"), attrs.get("cache_creation_tokens"),
        attrs.get("cache_creation_input_tokens"),
        attrs.get("claude.cache_creation_tokens"),
    )
    cache_create_1h_raw = _first(
        obj.get("cache_creation_1h_tokens_sum"),
        obj.get("cache_creation_1h_tokens"),
        attrs.get("cache_creation_1h_tokens"),
    )
    long_input_raw = _first(obj.get("long_input_tokens_sum"), obj.get("long_input_tokens"))
    long_output_raw = _first(obj.get("long_output_tokens_sum"), obj.get("long_output_tokens"))
    long_cache_read_raw = _first(
        obj.get("long_cache_read_tokens_sum"), obj.get("long_cache_read_tokens")
    )
    long_cache_create_raw = _first(
        obj.get("long_cache_creation_tokens_sum"), obj.get("long_cache_creation_tokens")
    )
    long_split_known = any(
        value is not None and str(value).strip() != ""
        for value in (long_input_raw, long_output_raw, long_cache_read_raw,
                      long_cache_create_raw)
    )
    raw_token_fields_present = any(
        value is not None and str(value).strip() != ""
        for value in (input_raw, output_raw, cache_read_raw, cache_create_raw,
                      cache_create_1h_raw)
    )
    aggregate_token_marker = (
        "exact_usage_events" in obj or "estimated_usage_events" in obj
    )
    exact_usage_events = _nonnegative_int(obj.get("exact_usage_events"))
    estimated_usage_events = _nonnegative_int(obj.get("estimated_usage_events"))
    token_fields_present = (
        exact_usage_events > 0 if aggregate_token_marker else raw_token_fields_present
    )
    body_chars = _nonnegative_int(_first(
        obj.get("body_chars_sum"), obj.get("body_chars"),
        len(str(obj.get("body") or "")),
    ))
    tokens_in = _nonnegative_int(input_raw)
    tokens_out = _nonnegative_int(output_raw)
    cache_read = _nonnegative_int(cache_read_raw)
    cache_create = _nonnegative_int(cache_create_raw)
    cache_create_1h = _nonnegative_int(cache_create_1h_raw)
    estimated_tokens = 0
    if aggregate_token_marker and estimated_usage_events and body_chars:
        estimated_tokens = int(math.ceil(body_chars / 4.0))
        tokens_in += estimated_tokens
    elif not token_fields_present and body_chars:
        estimated_tokens = int(math.ceil(body_chars / 4.0))
        tokens_in = estimated_tokens

    timestamp = _timestamp_text(_first(
        obj.get("day"), obj.get("ts"), obj.get("timestamp"), attrs.get("event.timestamp")
    ))
    day = timestamp[:10] if len(timestamp) >= 10 else ""
    event_name = str(_first(obj.get("event_name"), attrs.get("event.name"),
                            obj.get("typ"), attrs.get("claude.type")))
    session = str(_first(obj.get("session"), obj.get("session_id"),
                         attrs.get("session.id"), attrs.get("claude.session_id")))

    native_source = event_name in {
        "api_request", "api_error", "api_retries_exhausted", "tool_result",
        "compaction", "claude_code.api_request",
    } or any(key in attrs for key in ("input_tokens", "output_tokens", "cost_usd"))
    legacy_source = bool(attrs.get("claude.type") or attrs.get("claude.session_id"))
    if aggregate_token_marker:
        telemetry_source = "aggregate"
    elif native_source:
        telemetry_source = "native"
    elif legacy_source:
        telemetry_source = "legacy"
    else:
        telemetry_source = "unknown"
    event_count = _nonnegative_int(_first(obj.get("events"), obj.get("usage_events"), 1))
    result = {
        "day": day,
        "timestamp": timestamp,
        "event_name": event_name,
        "session_id": session,
        "prompt_id": str(_first(obj.get("prompt_id"), attrs.get("prompt.id"))),
        "interaction_id": str(_first(obj.get("interaction_id"), attrs.get("interaction.id"))),
        "request_id": str(_first(obj.get("request_id"), attrs.get("request_id"),
                                  attrs.get("gen_ai.response.id"))),
        "message_id": str(_first(obj.get("message_id"), attrs.get("claude.message_id"))),
        "organization_id": str(_first(obj.get("organization"), obj.get("organization_id"),
                                       attrs.get("organization.id"), resource.get("organization.id"))),
        "team_id": str(_first(obj.get("team"), obj.get("team_id"),
                               resource.get("business.team.id"), attrs.get("business.team.id"))),
        "portfolio_id": str(_first(obj.get("portfolio"), obj.get("portfolio_id"),
                                    resource.get("business.portfolio.id"),
                                    attrs.get("business.portfolio.id"))),
        "project_id": str(_first(obj.get("project"), obj.get("project_id"),
                                  resource.get("business.project.id"),
                                  attrs.get("business.project.id"))),
        "feature_id": str(_first(obj.get("feature"), obj.get("feature_id"),
                                  resource.get("business.feature.id"),
                                  attrs.get("business.feature.id"))),
        "work_item_id": str(_first(obj.get("work_item"), obj.get("work_item_id"),
                                    resource.get("business.work_item.id"),
                                    attrs.get("business.work_item.id"))),
        "cost_center": str(_first(obj.get("cost_center"),
                                   resource.get("business.cost_center"),
                                   attrs.get("business.cost_center"))),
        "repository_id": str(_first(obj.get("repository"), obj.get("repository_id"),
                                     resource.get("code.repository.id"),
                                     attrs.get("code.repository.id"))),
        "branch_id": str(_first(obj.get("branch"), obj.get("branch_id"),
                                 resource.get("code.branch.id"), attrs.get("code.branch.id"))),
        "pull_request_id": str(_first(obj.get("pull_request"), obj.get("pull_request_id"),
                                       attrs.get("vcs.pull_request.id"))),
        "agent_profile": str(_first(obj.get("agent"), obj.get("agent_profile"),
                                     resource.get("berserk.agent.profile"),
                                     attrs.get("agent.name"), attrs.get("agent_id"))),
        "parent_agent_id": str(_first(obj.get("parent_agent"), obj.get("parent_agent_id"),
                                       attrs.get("parent_agent_id"))),
        "harness_version": str(_first(obj.get("harness"), obj.get("harness_version"),
                                       resource.get("berserk.harness.version"),
                                       attrs.get("berserk.harness.version"))),
        "recommendation_id": str(_first(obj.get("recommendation_id"),
                                         resource.get("berserk.recommendation.id"),
                                         attrs.get("berserk.recommendation.id"))),
        "model": str(_first(obj.get("model"), attrs.get("model"),
                             attrs.get("gen_ai.request.model"),
                             attrs.get("claude.message_model"))),
        "speed": str(_first(obj.get("speed"), attrs.get("speed"), "normal")).lower(),
        "query_source": str(_first(obj.get("query_source"), attrs.get("query_source"))),
        "tool_name": str(_first(obj.get("tool"), obj.get("tool_name"),
                                 attrs.get("mcp_tool.name"), attrs.get("tool_name"),
                                 attrs.get("claude.tool_names"))),
        "events": event_count,
        "tool_calls": _nonnegative_int(_first(obj.get("tool_calls"),
                                               1 if event_name == "tool_result" else 0)),
        "compactions": _nonnegative_int(_first(
            obj.get("compactions"), 1 if event_name == "compaction" else 0
        )),
        "errors": _nonnegative_int(_first(obj.get("errors"),
                                           1 if _bool(_first(obj.get("error"), obj.get("err"),
                                                            attrs.get("error"), attrs.get("claude.error"))) else 0)),
        "successes": _nonnegative_int(_first(
            obj.get("successes"),
            1 if _bool(_first(obj.get("success"), attrs.get("success"))) else 0,
        )),
        "attempts": _nonnegative_int(_first(obj.get("attempts"), obj.get("attempt"),
                                           attrs.get("attempt"), 1)),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": max(0, cache_create - cache_create_1h),
        "cache_creation_1h_tokens": cache_create_1h,
        "long_input_tokens": _nonnegative_int(long_input_raw),
        "long_output_tokens": _nonnegative_int(long_output_raw),
        "long_cache_read_tokens": _nonnegative_int(long_cache_read_raw),
        "long_cache_creation_tokens": _nonnegative_int(long_cache_create_raw),
        "long_context_split_known": long_split_known,
        "estimated_tokens": estimated_tokens,
        "token_source": (
            "mixed" if token_fields_present and estimated_tokens else
            "exact" if token_fields_present else
            "estimated" if estimated_tokens else "missing"
        ),
        "exact_usage_events": (
            exact_usage_events if aggregate_token_marker else
            event_count if token_fields_present else 0
        ),
        "estimated_usage_events": (
            estimated_usage_events if aggregate_token_marker else
            event_count if estimated_tokens else 0
        ),
        "native_events": _nonnegative_int(_first(
            obj.get("native_events"), event_count if native_source else 0,
        )),
        "legacy_events": _nonnegative_int(_first(
            obj.get("legacy_events"), event_count if legacy_source and not native_source else 0,
        )),
        "telemetry_source": telemetry_source,
        "body_chars": body_chars,
        "reported_cost_usd": _nonnegative_float(_first(
            obj.get("reported_cost_usd"), obj.get("cost_usd"),
            attrs.get("cost_usd"),
            (_nonnegative_float(attrs.get("cost_usd_micros")) / 1_000_000.0
             if attrs.get("cost_usd_micros") not in (None, "") else None),
            attrs.get("claude.cost_usd"),
        )),
        "active_seconds": _nonnegative_float(_first(
            obj.get("active_seconds"), obj.get("active_seconds_sum"),
            attrs.get("active_seconds"),
        )),
        "duration_seconds": _nonnegative_float(_first(
            obj.get("duration_seconds"), obj.get("duration_seconds_sum"),
            (_nonnegative_float(attrs.get("duration_ms")) / 1000.0
             if attrs.get("duration_ms") not in (None, "") else None),
        )),
        "result_tokens": _nonnegative_int(_first(obj.get("result_tokens"),
                                                  obj.get("result_tokens_sum"),
                                                  attrs.get("result_tokens"),
                                                  _nonnegative_int(attrs.get("tool_result_size_bytes")) / 4)),
        "lines_added": _nonnegative_int(_first(
            obj.get("lines_added"), obj.get("lines_added_sum"), attrs.get("lines_added")
        )),
        "lines_removed": _nonnegative_int(_first(
            obj.get("lines_removed"), obj.get("lines_removed_sum"), attrs.get("lines_removed")
        )),
        "commits": _nonnegative_int(_first(
            obj.get("commits"), obj.get("commits_sum"), attrs.get("commits")
        )),
        "pull_requests": _nonnegative_int(_first(
            obj.get("pull_requests"), obj.get("pull_requests_sum"), attrs.get("pull_requests")
        )),
        "web_search_requests": _nonnegative_int(_first(
            obj.get("web_search_requests"), obj.get("web_search_requests_sum"),
            attrs.get("server_tool_use.web_search_requests"),
        )),
        "code_execution_seconds": _nonnegative_float(_first(
            obj.get("code_execution_seconds"), obj.get("code_execution_seconds_sum"),
            attrs.get("code_execution_seconds"),
        )),
    }
    return result


def deduplicate_usage_rows(raw_rows):
    """Prefer native records when native and legacy sources describe one request."""
    prepared = []
    request_positions = {}
    for raw in raw_rows:
        normalized = normalize_usage_row(raw)
        aggregate = _nonnegative_int(raw.get("events")) > 1 or (
            "exact_usage_events" in raw or "estimated_usage_events" in raw
        )
        source = normalized.get("telemetry_source")
        request_id = normalized.get("request_id")
        priority = 2 if source == "native" else 1
        if not aggregate and request_id:
            key = str(request_id)
            if key in request_positions:
                position = request_positions[key]
                if priority > prepared[position][2]:
                    prepared[position] = (raw, normalized, priority, aggregate)
                continue
            request_positions[key] = len(prepared)
        prepared.append((raw, normalized, priority, aggregate))

    native_fingerprints = set()
    for _, row, _, aggregate in prepared:
        timestamp = str(row.get("timestamp") or "")
        if aggregate or row.get("telemetry_source") != "native" or len(timestamp) <= 10:
            continue
        native_fingerprints.add((
            row.get("session_id"), timestamp, row.get("model"),
            row.get("input_tokens"), row.get("output_tokens"),
            row.get("cache_read_tokens"), row.get("cache_creation_tokens"),
        ))

    after_fingerprint = []
    for raw, row, priority, aggregate in prepared:
        timestamp = str(row.get("timestamp") or "")
        fingerprint = (
            row.get("session_id"), timestamp, row.get("model"),
            row.get("input_tokens"), row.get("output_tokens"),
            row.get("cache_read_tokens"), row.get("cache_creation_tokens"),
        )
        if (not aggregate and row.get("telemetry_source") == "legacy"
                and len(timestamp) > 10 and fingerprint in native_fingerprints):
            continue
        after_fingerprint.append((raw, row, priority, aggregate))

    # Second, distinct dedup pass: collapse content-block rows that share one
    # claude.message_id (one real, billable API call) down to a single row.
    # Unrelated to the native_fingerprints pass above (which resolves two
    # telemetry *sources* describing the same request) -- do not merge the
    # two mechanisms. Rows with no message_id (aggregate rows, already
    # collapsed upstream in usage_aggregate_query, or data predating the
    # forwarder capturing it) are never grouped with each other on any other
    # key, so they pass through untouched.
    seen_messages = set()
    result = []
    for raw, row, _, aggregate in after_fingerprint:
        message_id = str(row.get("message_id") or "").strip()
        if not aggregate and message_id:
            key = (row.get("session_id"), message_id)
            if key in seen_messages:
                continue
            seen_messages.add(key)
        result.append(raw)
    return result


# Columns carried through both post-row-level dedup stages of
# usage_aggregate_query (request_id/timestamp-fingerprint dedup, then the
# separate message_id collapse for per-content-block over-counting). Built
# as a list and joined into "col=max(col)" clauses so the two summarize
# stages can't drift out of sync with each other by hand-edit.
_USAGE_ROW_FIELDS = (
    "timestamp", "event_name", "legacy_type", "model", "speed", "tool_name",
    "tokens_in", "tokens_out", "cache_read", "cache_create", "cache_create_1h",
    "context_tokens", "organization", "team", "portfolio", "project", "feature",
    "work_item", "cost_center", "repository", "branch", "pull_request", "agent",
    "harness", "recommendation_id", "query_source", "source_priority",
    "result_tokens", "active_seconds", "lines_added", "lines_removed", "commits",
    "pull_requests", "token_exact", "estimated_body_chars", "error_flag",
    "success_flag", "request_attempts", "reported_cost", "web_search_requests",
    "code_execution_seconds", "duration_seconds",
)


def _max_agg_clause(fields):
    return ", ".join(f"{f}=max({f})" for f in fields)


def usage_aggregate_query():
    """Bounded, aggregate-first query for enterprise reporting."""
    return (
        f"{_table} | where resource['service.name'] == 'claude-code' "
        "| extend raw_attributes=$raw['attributes'], raw_resource=$raw['resource'] "
        "| extend event_name=tostring(raw_attributes['event.name']), "
        "legacy_type=tostring(raw_attributes['claude.type']) "
        "| where event_name in ('api_request','api_error','api_retries_exhausted',"
        "'tool_result','compaction','claude_code.api_request') "
        "or legacy_type == 'assistant' "
        "or metric_name in ('claude_code.lines_of_code.count','claude_code.pull_request.count',"
        "'claude_code.commit.count','claude_code.active_time.total') "
        "| extend model=iff(isnotempty(tostring(raw_attributes['model'])), "
        "tostring(raw_attributes['model']), tostring(raw_attributes['claude.message_model'])), "
        "tokens_in=iff(isnotnull(raw_attributes['input_tokens']), "
        "toint(tostring(raw_attributes['input_tokens'])), "
        "toint(tostring(raw_attributes['claude.tokens_input']))), "
        "tokens_out=iff(isnotnull(raw_attributes['output_tokens']), "
        "toint(tostring(raw_attributes['output_tokens'])), "
        "toint(tostring(raw_attributes['claude.tokens_output']))), "
        "cache_read=toint(tostring(raw_attributes['cache_read_tokens'])), "
        "cache_create=toint(tostring(raw_attributes['cache_creation_tokens'])), "
        "cache_create_1h=toint(tostring(raw_attributes['cache_creation_1h_tokens'])), "
        "request_id=tostring(raw_attributes['request_id']), "
        "message_id=tostring(raw_attributes['claude.message_id']), "
        "session_id=iff(isnotempty(tostring(raw_attributes['session.id'])), "
        "tostring(raw_attributes['session.id']), "
        "tostring(raw_attributes['claude.session_id'])), "
        "tool_name=iff(isnotempty(tostring(raw_attributes['mcp_tool.name'])), "
        "tostring(raw_attributes['mcp_tool.name']), tostring(raw_attributes['tool_name'])), "
        "speed=iff(isempty(tostring(raw_attributes['speed'])), 'normal', "
        "tostring(raw_attributes['speed'])) "
        "| extend context_tokens=iff(isnull(tokens_in), 0, tokens_in) + "
        "iff(isnull(cache_read), 0, cache_read) + "
        "iff(isnull(cache_create), 0, cache_create) "
        "| extend organization=iff(isnotempty(tostring(raw_attributes['organization.id'])), "
        "tostring(raw_attributes['organization.id']), tostring(raw_resource['organization.id'])), "
        "team=iff(isnotempty(tostring(raw_attributes['business.team.id'])), "
        "tostring(raw_attributes['business.team.id']), tostring(raw_resource['business.team.id'])), "
        "portfolio=iff(isnotempty(tostring(raw_attributes['business.portfolio.id'])), "
        "tostring(raw_attributes['business.portfolio.id']), "
        "tostring(raw_resource['business.portfolio.id'])), "
        "project=iff(isnotempty(tostring(raw_attributes['business.project.id'])), "
        "tostring(raw_attributes['business.project.id']), "
        "tostring(raw_resource['business.project.id'])), "
        "feature=iff(isnotempty(tostring(raw_attributes['business.feature.id'])), "
        "tostring(raw_attributes['business.feature.id']), "
        "tostring(raw_resource['business.feature.id'])), "
        "work_item=iff(isnotempty(tostring(raw_attributes['business.work_item.id'])), "
        "tostring(raw_attributes['business.work_item.id']), "
        "tostring(raw_resource['business.work_item.id'])), "
        "cost_center=iff(isnotempty(tostring(raw_attributes['business.cost_center'])), "
        "tostring(raw_attributes['business.cost_center']), "
        "tostring(raw_resource['business.cost_center'])), "
        "repository=iff(isnotempty(tostring(raw_attributes['code.repository.id'])), "
        "tostring(raw_attributes['code.repository.id']), "
        "tostring(raw_resource['code.repository.id'])), "
        "branch=iff(isnotempty(tostring(raw_attributes['code.branch.id'])), "
        "tostring(raw_attributes['code.branch.id']), tostring(raw_resource['code.branch.id'])), "
        "pull_request=tostring(raw_attributes['vcs.pull_request.id']), "
        "agent=iff(isnotempty(tostring(raw_attributes['agent.name'])), "
        "tostring(raw_attributes['agent.name']), "
        "tostring(raw_resource['berserk.agent.profile'])), "
        "harness=iff(isnotempty(tostring(raw_attributes['berserk.harness.version'])), "
        "tostring(raw_attributes['berserk.harness.version']), "
        "tostring(raw_resource['berserk.harness.version'])), "
        "recommendation_id=iff(isnotempty(tostring(raw_attributes['berserk.recommendation.id'])), "
        "tostring(raw_attributes['berserk.recommendation.id']), "
        "tostring(raw_resource['berserk.recommendation.id'])), "
        "query_source=tostring(raw_attributes['query_source']), "
        "source_priority=iff(event_name in ('api_request','api_error','api_retries_exhausted',"
        "'tool_result','compaction','claude_code.api_request'), 2, 1), "
        "result_tokens=iff(isnotnull(raw_attributes['result_tokens']), "
        "toint(tostring(raw_attributes['result_tokens'])), "
        "toint(tostring(raw_attributes['tool_result_size_bytes'])) / 4), "
        "active_seconds=iff(metric_name == 'claude_code.active_time.total', "
        "todouble(tostring($raw['value'])), 0.0), "
        "lines_added=iff(metric_name == 'claude_code.lines_of_code.count' "
        "and tostring(raw_attributes['type']) == 'added', "
        "toint(tostring($raw['value'])), 0), "
        "lines_removed=iff(metric_name == 'claude_code.lines_of_code.count' "
        "and tostring(raw_attributes['type']) == 'removed', "
        "toint(tostring($raw['value'])), 0), "
        "commits=iff(metric_name == 'claude_code.commit.count', "
        "toint(tostring($raw['value'])), 0), "
        "pull_requests=iff(metric_name == 'claude_code.pull_request.count', "
        "toint(tostring($raw['value'])), 0), "
        "error_flag=iff(event_name in ('api_error','api_retries_exhausted') "
        "or (event_name == 'tool_result' and tostring(raw_attributes['success']) == 'false') "
        "or tostring(raw_attributes['error']) == 'true' "
        "or tostring(raw_attributes['claude.error']) == 'true', 1, 0), "
        "success_flag=iff(event_name in ('api_request','claude_code.api_request') "
        "or (legacy_type == 'assistant' "
        "and tostring(raw_attributes['claude.error']) != 'true'), 1, 0), "
        "request_attempts=iff(event_name in ('api_request','claude_code.api_request','api_error'), "
        "iff(isnull(raw_attributes['attempt']), 1, "
        "toint(tostring(raw_attributes['attempt']))), 0), "
        "reported_cost=iff(isnotnull(raw_attributes['cost_usd']), "
        "todouble(tostring(raw_attributes['cost_usd'])), "
        "todouble(tostring(raw_attributes['cost_usd_micros'])) / 1000000.0), "
        "web_search_requests=toint(tostring(raw_attributes['server_tool_use.web_search_requests'])), "
        "code_execution_seconds=todouble(tostring(raw_attributes['code_execution_seconds'])) "
        "| extend duration_seconds=iff(isnotnull(raw_attributes['duration_ms']), "
        "todouble(tostring(raw_attributes['duration_ms'])) / 1000.0, 0.0) "
        "| extend token_exact=iff((event_name in ('api_request','claude_code.api_request') "
        "or legacy_type == 'assistant') and (isnotnull(raw_attributes['input_tokens']) "
        "or isnotnull(raw_attributes['output_tokens']) "
        "or isnotnull(raw_attributes['claude.tokens_input']) "
        "or isnotnull(raw_attributes['claude.tokens_output'])), 1, 0), "
        "estimated_body_chars=iff((event_name in ('api_request','claude_code.api_request') "
        "or legacy_type == 'assistant') and isnull(raw_attributes['input_tokens']) "
        "and isnull(raw_attributes['output_tokens']) "
        "and isnull(raw_attributes['claude.tokens_input']) "
        "and isnull(raw_attributes['claude.tokens_output']), "
        "strlen(tostring($raw['body'])), 0) "
        "| extend dedupe_key=iff(isnotempty(request_id), strcat('request:', request_id), "
        "strcat('event:', session_id, ':', tostring(timestamp), ':', event_name, ':', "
        "tostring(metric_name), ':', model, ':', tool_name, ':', tostring(tokens_in), ':', "
        "tostring(tokens_out))) "
        "| project timestamp, event_name, legacy_type, model, speed, tool_name, tokens_in, tokens_out, "
        "cache_read, cache_create, cache_create_1h, context_tokens, organization, team, "
        "portfolio, project, feature, work_item, cost_center, repository, branch, "
        "pull_request, agent, harness, recommendation_id, query_source, source_priority, "
        "result_tokens, active_seconds, lines_added, lines_removed, commits, pull_requests, "
        "token_exact, estimated_body_chars, dedupe_key, message_id, error_flag, success_flag, "
        "request_attempts, reported_cost, web_search_requests, code_execution_seconds, "
        "duration_seconds "
        f"| summarize {_max_agg_clause(_USAGE_ROW_FIELDS)}, "
        "message_id=max(message_id) by dedupe_key "
        # Second, distinct dedup pass: collapse content-block rows that share
        # one claude.message_id (one real, billable API call) down to a
        # single row. This is unrelated to dedupe_key above (which resolves
        # native-vs-legacy telemetry describing the *same request*) — do not
        # merge the two mechanisms. Rows with no message_id (data predating
        # the forwarder capturing it, or non-Claude-Code native events) fall
        # back to dedupe_key itself as the grouping key, which is already
        # unique per distinct event at this point, so they pass through
        # ungrouped rather than risking a content/timestamp-based collapse.
        "| extend msg_gkey=iff(isnotempty(message_id), strcat('m|', message_id), dedupe_key) "
        f"| summarize {_max_agg_clause(_USAGE_ROW_FIELDS)} by msg_gkey "
        "| summarize events=countif(event_name in ('api_request','claude_code.api_request') "
        "or event_name == 'api_error' or legacy_type == 'assistant'), "
        "tool_calls=countif(event_name == 'tool_result'), "
        "compactions=countif(event_name == 'compaction'), "
        "errors=sum(error_flag), successes=sum(success_flag), attempts=sum(request_attempts), "
        "exact_usage_events=sum(token_exact), "
        "estimated_usage_events=countif(estimated_body_chars > 0), "
        "native_events=countif(source_priority == 2 and event_name in "
        "('api_request','claude_code.api_request','api_error')), "
        "legacy_events=countif(source_priority == 1 and legacy_type == 'assistant'), "
        "tokens_in_sum=sum(tokens_in), tokens_out_sum=sum(tokens_out), "
        "cache_read_tokens_sum=sum(cache_read), "
        "cache_creation_tokens_sum=sum(cache_create), "
        "cache_creation_1h_tokens_sum=sum(cache_create_1h), "
        "long_input_tokens_sum=sum(iff(context_tokens > 200000, tokens_in, 0)), "
        "long_output_tokens_sum=sum(iff(context_tokens > 200000, tokens_out, 0)), "
        "long_cache_read_tokens_sum=sum(iff(context_tokens > 200000, cache_read, 0)), "
        "long_cache_creation_tokens_sum=sum(iff(context_tokens > 200000, cache_create, 0)), "
        "reported_cost_usd=sum(reported_cost), "
        "result_tokens_sum=sum(result_tokens), "
        "web_search_requests_sum=sum(web_search_requests), "
        "code_execution_seconds_sum=sum(code_execution_seconds), "
        "duration_seconds_sum=sum(duration_seconds), "
        "active_seconds_sum=sum(active_seconds), lines_added_sum=sum(lines_added), "
        "lines_removed_sum=sum(lines_removed), commits_sum=sum(commits), "
        "pull_requests_sum=sum(pull_requests), "
        "body_chars_sum=sum(estimated_body_chars) "
        "by day=bin(timestamp, 1d), organization, team, portfolio, project, feature, "
        "work_item, cost_center, repository, branch, pull_request, agent, harness, "
        "recommendation_id, query_source, tool_name, model, speed "
        f"| sort by day asc | take {MAX_USAGE_ROWS}"
    )


def _catalog_candidates():
    if _catalog_path:
        yield _catalog_path
    yield Path(__file__).resolve().parent / "pricing_catalog.json"
    yield Path(sys.prefix) / "share" / "berserk-mcp" / "pricing_catalog.json"


def load_pricing_catalog(path=None):
    candidates = [Path(path)] if path else list(_catalog_candidates())
    last_error = None
    for candidate in candidates:
        try:
            safe_candidate = _safe_absolute(candidate, "pricing catalog")
            with open(safe_candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("pricing catalog must contain a models list")
        if not data.get("catalog_version"):
            raise ValueError("pricing catalog must contain catalog_version")
        return data
    raise ValueError("unable to load pricing catalog: %s" % (
        type(last_error).__name__ if last_error else "not found"
    ))


def resolve_model_price(catalog, model, at=None):
    model_text = str(model or "").strip().lower()
    if not model_text:
        return None
    at_day = str(at or _now_iso())[:10]
    candidates = []
    for entry in catalog.get("models", []):
        if not isinstance(entry, dict):
            continue
        effective_from = str(entry.get("effective_from") or catalog.get("effective_from") or "")[:10]
        effective_to = str(entry.get("effective_to") or "")[:10]
        if at_day and effective_from and at_day < effective_from:
            continue
        if at_day and effective_to and at_day > effective_to:
            continue
        aliases = [str(entry.get("id") or "").lower()] + [
            str(alias).lower() for alias in entry.get("aliases", [])
        ]
        matched = [
            alias for alias in aliases
            if alias and (
                alias == model_text
                or (any(char.isdigit() for char in alias) and alias in model_text)
            )
        ]
        if matched:
            candidates.append((max(len(alias) for alias in matched), effective_from, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def calculate_public_cost(row, catalog):
    normalized = normalize_usage_row(row) if "input_tokens" not in row else dict(row)
    price = resolve_model_price(catalog, normalized.get("model"), normalized.get("day"))
    token_total = sum(_nonnegative_int(normalized.get(key)) for key in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens", "cache_creation_1h_tokens",
    ))
    server_tools = catalog.get("server_tools", {}) if isinstance(catalog, dict) else {}
    web_rate = _nonnegative_float(
        (server_tools.get("web_search") or {}).get("usd_per_1000_requests")
    )
    execution_rate = _nonnegative_float(
        (server_tools.get("code_execution") or {}).get("usd_per_session_hour")
    )
    tool_cost = (
        _nonnegative_int(normalized.get("web_search_requests")) * web_rate / 1000.0
        + _nonnegative_float(normalized.get("code_execution_seconds"))
        * execution_rate / 3600.0
    )
    if price is None:
        return {
            "public_api_equivalent_usd": round(tool_cost, 8),
            "pricing_status": "partially_priced" if tool_cost else "unknown",
            "pricing_model": "",
            "pricing_variant": "unknown",
            "priced_tokens": 0,
            "unpriced_tokens": token_total,
            "long_context": False,
            "long_context_unknown": False,
        }

    rates = dict(price)
    pricing_variant = "standard"
    if str(normalized.get("speed") or "").lower() == "fast" and isinstance(
            price.get("fast_mode"), dict):
        rates.update(price["fast_mode"])
        pricing_variant = "fast"
    total_input = sum(_nonnegative_int(normalized.get(key)) for key in (
        "input_tokens", "cache_read_tokens", "cache_creation_tokens",
        "cache_creation_1h_tokens",
    ))
    long_context = False
    long_context_unknown = False
    long_rates = rates.get("long_context")
    components = {
        "input": (_nonnegative_int(normalized.get("input_tokens")),
                  _nonnegative_float(rates.get("input_usd_per_mtok"))),
        "output": (_nonnegative_int(normalized.get("output_tokens")),
                   _nonnegative_float(rates.get("output_usd_per_mtok"))),
        "cache_read": (_nonnegative_int(normalized.get("cache_read_tokens")),
                       _nonnegative_float(rates.get("cache_read_usd_per_mtok"))),
        "cache_write_5m": (_nonnegative_int(normalized.get("cache_creation_tokens")),
                           _nonnegative_float(rates.get("cache_write_5m_usd_per_mtok"))),
        "cache_write_1h": (_nonnegative_int(normalized.get("cache_creation_1h_tokens")),
                           _nonnegative_float(rates.get("cache_write_1h_usd_per_mtok"))),
    }
    cost = tool_cost + sum(tokens * rate / 1_000_000.0
                           for tokens, rate in components.values())
    if isinstance(long_rates, dict):
        long_tokens = {
            "input": _nonnegative_int(normalized.get("long_input_tokens")),
            "output": _nonnegative_int(normalized.get("long_output_tokens")),
            "cache_read": _nonnegative_int(normalized.get("long_cache_read_tokens")),
            "cache_write_5m": _nonnegative_int(normalized.get("long_cache_creation_tokens")),
            "cache_write_1h": 0,
        }
        if normalized.get("long_context_split_known"):
            long_context = any(long_tokens.values())
        elif _nonnegative_int(normalized.get("events") or 1) <= 1 and total_input > _nonnegative_int(
            long_rates.get("threshold_input_tokens")
        ):
            long_context = True
            long_tokens = {name: tokens for name, (tokens, _) in components.items()}
        elif total_input > _nonnegative_int(long_rates.get("threshold_input_tokens")):
            # Aggregated rows without a per-request long-context split cannot
            # safely apply the premium to every token. Keep the base estimate
            # and expose partial coverage rather than overcharging silently.
            long_context_unknown = True
        rate_names = {
            "input": "input_usd_per_mtok",
            "output": "output_usd_per_mtok",
            "cache_read": "cache_read_usd_per_mtok",
            "cache_write_5m": "cache_write_5m_usd_per_mtok",
            "cache_write_1h": "cache_write_1h_usd_per_mtok",
        }
        long_missing_tokens = 0
        for name, tokens in long_tokens.items():
            base_rate = components[name][1]
            premium_rate = _nonnegative_float(long_rates.get(rate_names[name]))
            if tokens and not premium_rate:
                long_missing_tokens += tokens
                continue
            cost += tokens * (premium_rate - base_rate) / 1_000_000.0
    else:
        long_missing_tokens = 0
    missing_rate_tokens = (
        sum(tokens for tokens, rate in components.values() if tokens and not rate)
        + long_missing_tokens
    )
    status = "partially_priced" if missing_rate_tokens or long_context_unknown else "priced"
    return {
        "public_api_equivalent_usd": round(cost, 8),
        "pricing_status": status,
        "pricing_model": str(price.get("id") or ""),
        "pricing_variant": pricing_variant,
        "priced_tokens": max(0, token_total - missing_rate_tokens),
        "unpriced_tokens": min(token_total, missing_rate_tokens),
        "long_context": long_context,
        "long_context_unknown": long_context_unknown,
    }


def _empty_business_store():
    return {"schema_version": 1, "features": [], "effort": [], "updated_at": ""}


def _safe_absolute(path, purpose):
    return _store.validate_store_path(path, purpose)


def _atomic_write_text(path, text, *, private=True):
    return _store.atomic_write_text(path, text, private=private)


def _atomic_write_json(path, value, *, private=True):
    return _store.atomic_write_json(path, value, private=private, sort_keys=True)


def load_business_store(path=None):
    target = Path(path) if path else _business_store_path
    if target is None:
        return _empty_business_store()
    try:
        safe = _safe_absolute(target, "business store")
        with open(safe, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return _empty_business_store()
    if not isinstance(data, dict):
        return _empty_business_store()
    data.setdefault("features", [])
    data.setdefault("effort", [])
    return data


def _business_data_stale(store, max_age_days=7):
    updated_at = str(store.get("updated_at") or "")
    if not updated_at:
        return True
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - updated).total_seconds() > max_age_days * 86400


def _load_import_file(path, fmt=None):
    source = _safe_absolute(path, "business import")
    chosen = (fmt or source.suffix.lstrip(".")).lower()
    if chosen == "csv":
        with open(source, "r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    with open(source, "r", encoding="utf-8") as handle:
        raw = handle.read().strip()
    if not raw:
        return []
    if chosen in {"json", "ndjson", "jsonl"}:
        if raw.startswith("[") or (raw.startswith("{") and "\n" not in raw):
            value = json.loads(raw)
            if isinstance(value, dict):
                value = value.get("records", [value])
            if not isinstance(value, list):
                raise ValueError("JSON import must be an array or records object")
            return [row for row in value if isinstance(row, dict)]
        rows = []
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid NDJSON at line {number}: {exc.msg}")
            if not isinstance(row, dict):
                raise ValueError(f"NDJSON line {number} must be an object")
            rows.append(row)
        return rows
    raise ValueError("format must be csv, json, ndjson, or jsonl")


def _identifier(value, field, required=False):
    text = str(value or "").strip()
    if not text and not required:
        return ""
    if not _IDENT_RE.match(text):
        raise ValueError(f"{field} must use letters, digits, '.', '_', ':', '/', or '-'")
    return text


def _list_value(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def _validated_number(value, field, maximum=None):
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a finite nonnegative number")
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        upper = f" no greater than {maximum}" if maximum is not None else ""
        raise ValueError(f"{field} must be a finite nonnegative number{upper}")
    return number


def _source_timestamp(value):
    text = str(value or _now_iso()).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("source_updated_at must be an ISO 8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError("source_updated_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _identifier_list(value, field):
    return [_identifier(item, field, True) for item in _list_value(value)]


def normalize_business_record(kind, record):
    if kind == "feature":
        result = {
            "feature_id": _identifier(record.get("feature_id"), "feature_id", True),
            "work_item_id": _identifier(record.get("work_item_id"), "work_item_id"),
            "project_id": _identifier(record.get("project_id"), "project_id", True),
            "portfolio_id": _identifier(record.get("portfolio_id"), "portfolio_id"),
            "team_id": _identifier(record.get("team_id"), "team_id"),
            "owner_id": _identifier(record.get("owner_id"), "owner_id"),
            "cost_center": _identifier(record.get("cost_center"), "cost_center"),
            "name": str(record.get("name") or record.get("feature_id") or "").strip()[:240],
            "status": str(record.get("status") or "planned").strip().lower()[:40],
            "planned_start": str(record.get("planned_start") or "")[:32],
            "planned_end": str(record.get("planned_end") or "")[:32],
            "planned_hours": _validated_number(record.get("planned_hours"), "planned_hours"),
            "planned_ai_budget_usd": _validated_number(
                record.get("planned_ai_budget_usd"), "planned_ai_budget_usd"
            ),
            "completion_pct": _validated_number(
                record.get("completion_pct"), "completion_pct", maximum=100
            ),
            "repositories": _identifier_list(record.get("repositories"), "repository"),
            "branches": _identifier_list(record.get("branches"), "branch"),
            "pull_requests": _identifier_list(record.get("pull_requests"), "pull_request"),
            "source_system": _identifier(record.get("source_system") or "import", "source_system", True),
            "source_record_id": _identifier(record.get("source_record_id") or record.get("feature_id"),
                                             "source_record_id", True),
            "source_updated_at": _source_timestamp(record.get("source_updated_at")),
        }
        return result
    if kind == "effort":
        hours = _validated_number(
            _first(record.get("actual_hours"), record.get("hours")),
            "actual_hours", maximum=24,
        )
        date = str(record.get("work_date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ValueError("work_date must be YYYY-MM-DD")
        return {
            "worklog_id": _identifier(record.get("worklog_id"), "worklog_id", True),
            "feature_id": _identifier(record.get("feature_id"), "feature_id", True),
            "work_item_id": _identifier(record.get("work_item_id"), "work_item_id"),
            "team_id": _identifier(record.get("team_id"), "team_id"),
            "work_date": date,
            "hours": hours,
            "actual_hours": hours,
            "source_system": _identifier(record.get("source_system") or "import", "source_system", True),
            "source_updated_at": _source_timestamp(record.get("source_updated_at")),
        }
    raise ValueError("kind must be feature or effort")


def _merge_latest(existing, incoming, key_fields):
    merged = {}
    for row in list(existing) + list(incoming):
        key = tuple(str(row.get(field) or "") for field in key_fields)
        current = merged.get(key)
        incoming_ts = str(row.get("source_updated_at") or "")
        current_ts = str(current.get("source_updated_at") or "") if current else ""
        if current is not None and incoming_ts == current_ts and row != current:
            raise ValueError(
                "conflicting records have the same source key and source_updated_at"
            )
        if current is None or incoming_ts > current_ts:
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


def _otlp_attributes(record):
    allowed = {
        "feature_id", "work_item_id", "project_id", "portfolio_id", "team_id",
        "cost_center", "status", "planned_start", "planned_end",
        "planned_hours", "planned_ai_budget_usd", "completion_pct", "repositories",
        "branches", "pull_requests", "source_system", "source_record_id",
        "source_updated_at", "worklog_id", "work_date", "hours", "actual_hours",
        "recommendation_id", "decision", "owner_hash", "rationale_hash", "ts",
    }
    attrs = []
    for key in sorted(record):
        if key not in allowed:
            continue
        value = record[key]
        if isinstance(value, bool):
            otel_value = {"boolValue": value}
        elif isinstance(value, (int, float)):
            otel_value = {"doubleValue": float(value)}
        elif isinstance(value, list):
            otel_value = {"arrayValue": {"values": [
                {"stringValue": str(item)[:240]} for item in value[:50]
            ]}}
        else:
            otel_value = {"stringValue": str(value)[:1000]}
        attrs.append({"key": key, "value": otel_value})
    return attrs


def _parse_headers(raw):
    return _http.parse_header_items(raw, force_json=True)


def emit_otlp_records(records, service_name):
    if not _otlp_endpoint or not records:
        return False
    try:
        _http.validate_http_url(
            _otlp_endpoint, label="OTLP endpoint", allow_plaintext_remote=False,
        )
    except _http.UrlPolicyError as exc:
        raise ValueError(str(exc)) from None
    logs = []
    now_ns = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))
    for record in records:
        logs.append({
            "timeUnixNano": now_ns,
            "body": {"stringValue": service_name},
            "attributes": _otlp_attributes(record),
        })
    payload = {
        "resourceLogs": [{
            "resource": {"attributes": [{
                "key": "service.name", "value": {"stringValue": service_name}
            }]},
            "scopeLogs": [{"scope": {"name": "berserk-mcp.ai-finops"},
                           "logRecords": logs}],
        }]
    }
    status = _http.post_bytes_status(
        _otlp_endpoint,
        _parse_headers(_otlp_headers),
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        timeout=15,
        label="OTLP endpoint",
        allow_plaintext_remote=False,
        cap=_http.MAX_RESPONSE_BYTES,
    )
    return 200 <= status < 300


def import_business_data(kind, input_path, fmt=None, store_path=None, emit_otlp=True):
    target = Path(store_path) if store_path else _business_store_path
    if target is None:
        raise ValueError("business store path is not configured")
    raw_records = _load_import_file(input_path, fmt)
    normalized = [normalize_business_record(kind, row) for row in raw_records]
    with _store_lock:
        store = load_business_store(target)
        if kind == "feature":
            store["features"] = _merge_latest(
                store.get("features", []), normalized, ("source_system", "source_record_id")
            )
        else:
            store["effort"] = _merge_latest(
                store.get("effort", []), normalized, ("source_system", "worklog_id")
            )
        store["updated_at"] = _now_iso()
        _atomic_write_json(target, store)
    emitted = emit_otlp_records(normalized, "engineering-work") if emit_otlp else False
    return {"kind": kind, "imported": len(normalized), "emitted_otlp": emitted,
            "store": str(target)}


def _feature_indexes(store):
    by_id = {}
    work_item = defaultdict(set)
    repo = defaultdict(set)
    branch = defaultdict(set)
    pr = defaultdict(set)
    for feature in store.get("features", []):
        fid = str(feature.get("feature_id") or "")
        if not fid:
            continue
        by_id[fid] = feature
        if feature.get("work_item_id"):
            work_item[str(feature["work_item_id"])].add(fid)
        for value in feature.get("repositories", []):
            repo[str(value)].add(fid)
        for value in feature.get("branches", []):
            branch[str(value)].add(fid)
        for value in feature.get("pull_requests", []):
            pr[str(value)].add(fid)
    return by_id, work_item, repo, branch, pr


def _unique_index_match(index, value):
    matches = index.get(str(value or ""), set())
    return next(iter(matches)) if len(matches) == 1 else ""


def attribute_usage(row, store):
    normalized = dict(row)
    if normalized.get("feature_id"):
        normalized["attribution_source"] = "explicit"
        return normalized
    by_id, work_items, repos, branches, prs = _feature_indexes(store)
    fid = ""
    source = "unattributed"
    if normalized.get("work_item_id"):
        fid = _unique_index_match(work_items, normalized["work_item_id"])
        source = "work_item" if fid else source
    if not fid and normalized.get("pull_request_id"):
        fid = _unique_index_match(prs, normalized["pull_request_id"])
        source = "pull_request" if fid else source
    if not fid and normalized.get("branch_id"):
        fid = _unique_index_match(branches, normalized["branch_id"])
        source = "branch" if fid else source
    if not fid and normalized.get("repository_id"):
        repo_matches = repos.get(normalized["repository_id"], set())
        fid = next(iter(repo_matches)) if len(repo_matches) == 1 else ""
        source = "repository" if fid else source
        if not fid and repo_matches:
            projects = {str(by_id[item].get("project_id") or "") for item in repo_matches}
            projects.discard("")
            if len(projects) == 1:
                normalized["project_id"] = normalized.get("project_id") or next(iter(projects))
                source = "repository_project"
    if fid:
        feature = by_id.get(fid, {})
        normalized["feature_id"] = fid
        normalized["project_id"] = normalized.get("project_id") or str(feature.get("project_id") or "")
        normalized["portfolio_id"] = normalized.get("portfolio_id") or str(feature.get("portfolio_id") or "")
        normalized["team_id"] = normalized.get("team_id") or str(feature.get("team_id") or "")
    normalized["attribution_source"] = source
    return normalized


_GROUP_FIELD = {
    "day": "day", "team": "team_id", "portfolio": "portfolio_id",
    "project": "project_id", "repository": "repository_id",
    "feature": "feature_id", "work_item": "work_item_id",
    "agent": "agent_profile", "harness": "harness_version", "model": "model",
}


def _apply_filters(rows, filters):
    result = []
    mapping = {
        "team": "team_id", "project": "project_id", "repository": "repository_id",
        "feature": "feature_id", "agent": "agent_profile", "harness": "harness_version",
        "model": "model",
    }
    for row in rows:
        if all(not filters.get(name) or str(row.get(field) or "") == str(filters[name])
               for name, field in mapping.items()):
            result.append(row)
    return result


def _normalized_usage_rows(raw_rows, store):
    return [
        attribute_usage(normalize_usage_row(row), store)
        for row in deduplicate_usage_rows(raw_rows)
    ]


def _aggregate(rows, fields, catalog):
    groups = {}
    for row in rows:
        key = tuple(str(row.get(field) or "") or "unattributed" for field in fields)
        slot = groups.setdefault(key, {
            field: key[index] for index, field in enumerate(fields)
        })
        if "events" not in slot:
            slot.update({
                "events": 0, "tool_calls": 0, "compactions": 0,
                "errors": 0, "successes": 0,
                "attempts": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "cache_creation_1h_tokens": 0, "estimated_tokens": 0,
                "reported_cost_usd": 0.0, "public_api_equivalent_usd": 0.0,
                "priced_tokens": 0, "unpriced_tokens": 0, "active_seconds": 0.0,
                "duration_seconds": 0.0,
                "result_tokens": 0, "lines_added": 0, "lines_removed": 0,
                "commits": 0, "pull_requests": 0, "exact_rows": 0,
                "estimated_rows": 0, "native_events": 0, "legacy_events": 0,
                "attributed_events": 0,
            })
        pricing = calculate_public_cost(row, catalog)
        for name in ("events", "tool_calls", "compactions", "errors", "successes", "attempts",
                     "input_tokens", "output_tokens", "cache_read_tokens",
                     "cache_creation_tokens", "cache_creation_1h_tokens",
                     "estimated_tokens", "result_tokens", "lines_added", "lines_removed",
                     "commits", "pull_requests"):
            slot[name] += _nonnegative_int(row.get(name))
        slot["native_events"] += _nonnegative_int(row.get("native_events"))
        slot["legacy_events"] += _nonnegative_int(row.get("legacy_events"))
        slot["reported_cost_usd"] += _nonnegative_float(row.get("reported_cost_usd"))
        slot["public_api_equivalent_usd"] += pricing["public_api_equivalent_usd"]
        slot["priced_tokens"] += pricing["priced_tokens"]
        slot["unpriced_tokens"] += pricing["unpriced_tokens"]
        slot["active_seconds"] += _nonnegative_float(row.get("active_seconds"))
        slot["duration_seconds"] += _nonnegative_float(row.get("duration_seconds"))
        slot["exact_rows"] += _nonnegative_int(row.get("exact_usage_events"))
        slot["estimated_rows"] += _nonnegative_int(row.get("estimated_usage_events"))
        if row.get("feature_id"):
            slot["attributed_events"] += _nonnegative_int(row.get("events"))
    result = []
    for slot in groups.values():
        total_prompt = slot["input_tokens"] + slot["cache_read_tokens"]
        slot["cache_hit_ratio"] = round(
            slot["cache_read_tokens"] / float(total_prompt), 4
        ) if total_prompt else 0.0
        slot["error_rate"] = round(slot["errors"] / float(max(1, slot["events"])), 4)
        slot["success_rate"] = round(slot["successes"] / float(max(1, slot["events"])), 4)
        slot["cost_per_active_hour_usd"] = round(
            slot["public_api_equivalent_usd"] / (slot["active_seconds"] / 3600.0), 6
        ) if slot["active_seconds"] else None
        slot["cost_per_success_usd"] = round(
            slot["public_api_equivalent_usd"] / slot["successes"], 6
        ) if slot["successes"] else None
        slot["cost_per_commit_usd"] = round(
            slot["public_api_equivalent_usd"] / slot["commits"], 6
        ) if slot["commits"] else None
        slot["cost_per_pull_request_usd"] = round(
            slot["public_api_equivalent_usd"] / slot["pull_requests"], 6
        ) if slot["pull_requests"] else None
        slot["average_duration_seconds"] = round(
            slot["duration_seconds"] / max(1, slot["events"] + slot["tool_calls"]), 4
        )
        slot["public_api_equivalent_usd"] = round(slot["public_api_equivalent_usd"], 6)
        slot["reported_cost_usd"] = round(slot["reported_cost_usd"], 6)
        total_tokens = slot["priced_tokens"] + slot["unpriced_tokens"]
        slot["pricing_coverage"] = round(slot["priced_tokens"] / float(total_tokens), 4) if total_tokens else 0.0
        slot["attribution_coverage"] = round(
            slot["attributed_events"] / float(max(1, slot["events"])), 4
        )
        telemetry_total = slot["native_events"] + slot["legacy_events"]
        slot["native_telemetry_coverage"] = round(
            slot["native_events"] / float(telemetry_total), 4
        ) if telemetry_total else 0.0
        exact_total = slot["exact_rows"] + slot["estimated_rows"]
        slot["exact_token_coverage"] = round(
            slot["exact_rows"] / float(exact_total), 4
        ) if exact_total else 0.0
        result.append(slot)
    return result


def build_spend_overview(raw_rows, catalog, store=None, group_by="day", filters=None, limit=20):
    if group_by not in _GROUP_FIELD:
        raise ValueError("invalid group_by")
    store = store or _empty_business_store()
    normalized = _normalized_usage_rows(raw_rows, store)
    normalized = _apply_filters(normalized, filters or {})
    groups = _aggregate(normalized, [_GROUP_FIELD[group_by]], catalog)
    if group_by == "day":
        groups.sort(key=lambda row: row.get("day", ""))
    else:
        groups.sort(key=lambda row: (-row["public_api_equivalent_usd"],
                                     str(row.get(_GROUP_FIELD[group_by], ""))))
    groups = groups[:max(1, min(100, int(limit or 20)))]
    total = _aggregate(normalized, [], catalog)
    overall = total[0] if total else {
        "events": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "public_api_equivalent_usd": 0.0, "reported_cost_usd": 0.0,
        "pricing_coverage": 0.0, "attribution_coverage": 0.0,
        "exact_rows": 0, "estimated_rows": 0, "unpriced_tokens": 0,
        "native_events": 0, "legacy_events": 0,
        "native_telemetry_coverage": 0.0, "exact_token_coverage": 0.0,
    }
    daily = sorted(_aggregate(normalized, ["day"], catalog), key=lambda row: row["day"])
    trend = {"direction": "insufficient-data", "change_pct": None, "points": len(daily)}
    if len(daily) >= 2:
        first = daily[0]["public_api_equivalent_usd"]
        last = daily[-1]["public_api_equivalent_usd"]
        change = ((last - first) / first * 100.0) if first else None
        direction = "flat"
        if change is not None and change > 5:
            direction = "growing"
        elif change is not None and change < -5:
            direction = "declining"
        trend = {"direction": direction,
                 "change_pct": round(change, 2) if change is not None else None,
                 "points": len(daily)}
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": catalog.get("catalog_version"),
        "group_by": group_by,
        "filters": filters or {},
        "overall": overall,
        "groups": groups,
        "trend": trend,
    }


def _envelope(title, payload, lines=None):
    readable = [title]
    readable.extend(lines or [])
    readable.append("\nStructured data:")
    readable.append("```json")
    readable.append(json.dumps(payload, indent=2, sort_keys=True))
    readable.append("```")
    return "\n".join(readable)


_STRUCTURAL_ID_PATTERNS = {
    "recommendation_id": re.compile(r"rec_[a-f0-9]{16}"),
    "request_id": re.compile(r"req_[A-Za-z0-9_-]{8,128}"),
    "dedupe_key": re.compile(r"(?:request|message|event):[A-Za-z0-9_.:/-]{1,220}"),
    "session_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"),
    "schema_hash": re.compile(r"[A-Fa-f0-9]{32,128}"),
    "sha256": re.compile(r"[A-Fa-f0-9]{64}"),
    "feature_id": _IDENT_RE,
    "project_id": _IDENT_RE,
    "work_item_id": _IDENT_RE,
    "harness_version": _IDENT_RE,
    "agent_profile": _IDENT_RE,
}


def _sanitize_payload(value, field_name=""):
    if isinstance(value, dict):
        return {str(key): _sanitize_payload(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item, field_name) for item in value]
    if isinstance(value, str):
        # Always run deterministic secret/PII patterns first. Structural IDs
        # skip only optional entropy matching, and only after format validation.
        base = _redact(value)
        if base != value:
            return base
        pattern = _STRUCTURAL_ID_PATTERNS.get(field_name)
        if pattern is not None and pattern.fullmatch(value):
            return value
        return _redact_aggressive(value)
    return value


def _fetch_usage(since):
    if _search is None:
        return "AI FinOps search backend is not configured.", True
    text, error = _search(usage_aggregate_query(), since)
    if error:
        return text, True
    return parse_records(text), False


def spend_overview(since="7d ago", group_by="day", filters=None, limit=20):
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    try:
        catalog = load_pricing_catalog()
        report = build_spend_overview(rows, catalog, load_business_store(), group_by,
                                      filters, limit)
        report["generated_at"] = _now_iso()
        report["source_window"] = since
    except ValueError as exc:
        return str(exc), True
    overall = report["overall"]
    lines = [
        f"Window: {since}; grouped by {group_by}.",
        f"API-equivalent cost: ${overall.get('public_api_equivalent_usd', 0):.4f}; "
        f"input={overall.get('input_tokens', 0)}, output={overall.get('output_tokens', 0)}, "
        f"cache-read={overall.get('cache_read_tokens', 0)}.",
        f"Feature attribution coverage: {overall.get('attribution_coverage', 0) * 100:.1f}%; "
        f"pricing coverage: {overall.get('pricing_coverage', 0) * 100:.1f}%.",
    ]
    return _envelope("Claude enterprise spend overview", report, lines), False


def _feature_snapshot(feature_id, rows, catalog, store):
    features, _, _, _, _ = _feature_indexes(store)
    feature = dict(features.get(feature_id, {"feature_id": feature_id, "name": feature_id}))
    attributed = _normalized_usage_rows(rows, store)
    selected = [row for row in attributed if row.get("feature_id") == feature_id]
    totals = _aggregate(selected, ["feature_id"], catalog)
    usage = totals[0] if totals else {
        "events": 0, "public_api_equivalent_usd": 0.0, "input_tokens": 0,
        "output_tokens": 0, "cache_read_tokens": 0, "errors": 0,
        "successes": 0, "attribution_coverage": 0.0,
    }
    actual_hours = round(sum(
        _nonnegative_float(_first(row.get("actual_hours"), row.get("hours")))
        for row in store.get("effort", [])
        if str(row.get("feature_id") or "") == feature_id
    ), 2)
    planned_hours = _nonnegative_float(feature.get("planned_hours"))
    budget = _nonnegative_float(feature.get("planned_ai_budget_usd"))
    completion_pct = min(100.0, _nonnegative_float(feature.get("completion_pct")))
    actual_cost = usage.get("public_api_equivalent_usd", 0.0)
    forecast = None
    if 10.0 <= completion_pct <= 100.0:
        forecast = round(actual_cost / (completion_pct / 100.0), 4)
    agents = _aggregate(selected, ["agent_profile"], catalog)
    harnesses = _aggregate(selected, ["harness_version"], catalog)
    models = _aggregate(selected, ["model", "speed"], catalog)
    operations = _aggregate(selected, ["query_source", "tool_name"], catalog)
    for values in (agents, harnesses, models, operations):
        values.sort(key=lambda item: -item["public_api_equivalent_usd"])
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": catalog.get("catalog_version"),
        "feature": feature,
        "planned_hours": planned_hours,
        "actual_hours": actual_hours,
        "hours_variance": round(actual_hours - planned_hours, 2) if planned_hours else None,
        "planned_ai_budget_usd": budget,
        "actual_ai_cost_usd": round(actual_cost, 6),
        "ai_budget_variance_usd": round(actual_cost - budget, 6) if budget else None,
        "forecast_ai_cost_at_completion_usd": forecast,
        "completion_pct": completion_pct,
        "ai_cost_per_developer_hour_usd": round(actual_cost / actual_hours, 6) if actual_hours else None,
        "usage": usage,
        "agents": agents[:20],
        "harness_versions": harnesses[:20],
        "models": models[:20],
        "operations": operations[:20],
        "delivery_outcomes": {
            "lines_added": usage.get("lines_added", 0),
            "lines_removed": usage.get("lines_removed", 0),
            "commits": usage.get("commits", 0),
            "pull_requests": usage.get("pull_requests", 0),
            "successes": usage.get("successes", 0),
            "errors": usage.get("errors", 0),
        },
    }


def feature_cost(feature_id, since="90d ago"):
    try:
        feature_id = _identifier(feature_id, "feature_id", True)
    except ValueError as exc:
        return str(exc), True
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    snapshot = _feature_snapshot(feature_id, rows, load_pricing_catalog(), load_business_store())
    snapshot["generated_at"] = _now_iso()
    snapshot["source_window"] = since
    lines = [
        f"Feature: {snapshot['feature'].get('name', feature_id)} ({feature_id}).",
        f"Developer hours: {snapshot['actual_hours']:.2f} actual / "
        f"{snapshot['planned_hours']:.2f} planned.",
        f"AI API-equivalent cost: ${snapshot['actual_ai_cost_usd']:.4f} / "
        f"${snapshot['planned_ai_budget_usd']:.4f} planned.",
    ]
    if snapshot["forecast_ai_cost_at_completion_usd"] is not None:
        lines.append(f"Forecast at completion: ${snapshot['forecast_ai_cost_at_completion_usd']:.4f}.")
    return _envelope("Claude feature delivery economics", snapshot, lines), False


def _project_snapshot(project_id, rows, catalog, store):
    normalized = _normalized_usage_rows(rows, store)
    selected = [row for row in normalized if row.get("project_id") == project_id]
    totals = _aggregate(selected, ["project_id"], catalog)
    usage = totals[0] if totals else {"events": 0, "public_api_equivalent_usd": 0.0}
    feature_ids = sorted({
        str(row.get("feature_id")) for row in store.get("features", [])
        if str(row.get("project_id") or "") == project_id
    })
    features = [_feature_snapshot(fid, rows, catalog, store) for fid in feature_ids]
    repositories = _aggregate(selected, ["repository_id"], catalog)
    repositories.sort(key=lambda item: -item["public_api_equivalent_usd"])
    unattributed = [row for row in selected if not row.get("feature_id")]
    unattributed_totals = _aggregate(unattributed, [], catalog)
    actual_cost = usage.get("public_api_equivalent_usd", 0.0)
    budget = round(sum(item["planned_ai_budget_usd"] for item in features), 4)
    completed = sum(1 for item in features if item.get("completion_pct", 0) >= 100)
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": catalog.get("catalog_version"),
        "project_id": project_id,
        "usage": usage,
        "planned_hours": round(sum(item["planned_hours"] for item in features), 2),
        "actual_hours": round(sum(item["actual_hours"] for item in features), 2),
        "planned_ai_budget_usd": budget,
        "actual_ai_cost_usd": round(actual_cost, 6),
        "ai_budget_variance_usd": round(actual_cost - budget, 6) if budget else None,
        "unattributed_ai_cost_usd": round(
            unattributed_totals[0]["public_api_equivalent_usd"]
            if unattributed_totals else 0.0, 6
        ),
        "completed_features": completed,
        "ai_cost_per_completed_feature_usd": round(actual_cost / completed, 6)
        if completed else None,
        "repositories": repositories[:50],
        "features": features,
    }


def project_economics(project_id, since="90d ago"):
    try:
        project_id = _identifier(project_id, "project_id", True)
    except ValueError as exc:
        return str(exc), True
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    snapshot = _project_snapshot(project_id, rows, load_pricing_catalog(), load_business_store())
    snapshot["generated_at"] = _now_iso()
    snapshot["source_window"] = since
    lines = [
        f"Project: {project_id}; {len(snapshot['features'])} governed features.",
        f"Developer hours: {snapshot['actual_hours']:.2f} actual / "
        f"{snapshot['planned_hours']:.2f} planned.",
        f"AI API-equivalent cost: ${snapshot['usage'].get('public_api_equivalent_usd', 0):.4f}.",
    ]
    return _envelope("Claude project economics", snapshot, lines), False


_AMENDMENTS = {
    "low_cache_reuse": "Stabilize system and tool prefixes, remove volatile prefix content, and move changing context later.",
    "large_default_context": "Split the default harness into a small baseline and load task-specific skills on demand.",
    "large_tool_results": "Add server-side filtering, pagination, output caps, and summary-first tool responses.",
    "retry_churn": "Add preflight validation, bounded retries, and explicit stop or escalation conditions.",
    "high_error_rate": "Clarify tool schemas and error handling, then add a deterministic recovery path.",
    "frontier_on_simple_work": "Route this profile to a cheaper default model with explicit escalation triggers.",
    "high_input_per_outcome": "Split work into phases, persist concise checkpoints, and reduce unrelated context injection.",
    "repeated_file_reads": "Add a context ledger keyed by target and revision, and do not reread unchanged files.",
    "excessive_compaction": "Split work into bounded phases and persist concise handoff checkpoints before context pressure builds.",
    "subagent_fanout": "Cap subagent depth and concurrency, and route narrow workers to a cheaper model profile.",
    "expensive_kql": "Route common intents through fixed MCP tools; narrow time windows and add query and result budgets.",
}

_EXPECTED_RESULTS = {
    "low_cache_reuse": "Higher cache-read ratio and lower repeated-input cost.",
    "large_default_context": "Lower input tokens per successful outcome without lower success.",
    "large_tool_results": "Lower result-token ingestion and shorter follow-up context.",
    "retry_churn": "Fewer attempts and lower error-retry cost.",
    "high_error_rate": "Lower tool error rate with unchanged completion quality.",
    "frontier_on_simple_work": "Lower model cost for the same successful outcomes.",
    "high_input_per_outcome": "Lower cost per successful outcome with stable latency and errors.",
    "repeated_file_reads": "Fewer redundant Read operations with no stale-context failures.",
    "excessive_compaction": "Fewer compactions and less context reconstruction.",
    "subagent_fanout": "Lower fan-out cost with unchanged task completion and latency.",
    "expensive_kql": "Lower cluster scan and returned-result load with equivalent answers.",
}

_RISKS = {
    "frontier_on_simple_work": "A cheaper default may miss complexity; retain explicit escalation triggers.",
    "subagent_fanout": "Lower concurrency may increase elapsed time on genuinely parallel tasks.",
    "expensive_kql": "Tighter bounds can omit relevant history; allow deliberate authorized expansion.",
}


def analyze_efficiency_rows(raw_rows, catalog, store=None, filters=None):
    store = store or _empty_business_store()
    rows = _normalized_usage_rows(raw_rows, store)
    rows = _apply_filters(rows, filters or {})
    groups = _aggregate(
        rows,
        ["agent_profile", "harness_version", "project_id", "model",
         "speed", "query_source", "tool_name"],
        catalog,
    )
    findings = []
    for group in groups:
        events = group["events"]
        observations = max(events, group["tool_calls"], group["compactions"])
        evidence = {key: group.get(key) for key in (
            "agent_profile", "harness_version", "project_id", "model",
            "speed", "query_source", "tool_name", "events", "tool_calls", "compactions",
            "input_tokens", "output_tokens", "cache_read_tokens", "result_tokens",
            "errors", "attempts", "successes", "average_duration_seconds",
            "public_api_equivalent_usd", "cost_per_success_usd",
        )}
        candidates = []
        prompt_tokens = group["input_tokens"] + group["cache_read_tokens"]
        if prompt_tokens >= 1000 and group["cache_hit_ratio"] < 0.20:
            candidates.append(("low_cache_reuse", min(0.95, 0.55 + events / 100.0)))
        if group["input_tokens"] >= 50000 and group["cache_read_tokens"] == 0:
            candidates.append(("large_default_context", min(0.95, 0.6 + events / 100.0)))
        if group["result_tokens"] >= max(5000, group["output_tokens"]):
            candidates.append(("large_tool_results", min(0.95, 0.6 + events / 100.0)))
        if group["attempts"] > max(events, 1) * 1.2:
            candidates.append(("retry_churn", min(0.95, 0.6 + events / 100.0)))
        if events >= MIN_RECOMMENDATION_EVENTS and group["error_rate"] >= 0.10:
            candidates.append(("high_error_rate", min(0.99, 0.65 + group["error_rate"])))
        model = str(group.get("model") or "").lower()
        if "opus" in model and events <= 10 and group["errors"] == 0 and group["tool_calls"] <= 3:
            candidates.append(("frontier_on_simple_work", 0.70))
        if group["successes"] and group["input_tokens"] / float(group["successes"]) >= 100000:
            candidates.append(("high_input_per_outcome", min(0.95, 0.6 + events / 100.0)))
        tool_name = str(group.get("tool_name") or "").lower()
        query_source = str(group.get("query_source") or "").lower()
        if tool_name == "read" and group["tool_calls"] >= 20:
            candidates.append(("repeated_file_reads", min(0.95, 0.6 + group["tool_calls"] / 100.0)))
        if group["compactions"] >= 3:
            candidates.append(("excessive_compaction", min(0.95, 0.65 + group["compactions"] / 50.0)))
        if "subagent" in query_source and events >= 20:
            candidates.append(("subagent_fanout", min(0.95, 0.6 + events / 200.0)))
        if ("kql" in tool_name or "search" in tool_name) and (
                group["result_tokens"] >= 10000 or group["attempts"] > max(1, events) * 1.2):
            candidates.append(("expensive_kql", min(0.95, 0.65 + events / 100.0)))
        for code, confidence in candidates:
            stable_scope = {key: evidence.get(key) for key in (
                "agent_profile", "harness_version", "project_id", "model",
                "speed", "query_source", "tool_name",
            )}
            material = json.dumps({"code": code, "scope": stable_scope}, sort_keys=True,
                                  separators=(",", ":"))
            rec_id = "rec_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
            findings.append({
                "recommendation_id": rec_id,
                "code": code,
                "confidence": round(confidence, 2),
                "sample_size": observations,
                "eligible_for_approval": observations >= MIN_RECOMMENDATION_EVENTS and confidence >= 0.65,
                "amendment": _AMENDMENTS[code],
                "expected_result": _EXPECTED_RESULTS[code],
                "risks": _RISKS.get(
                    code,
                    "Over-constraining the harness may reduce completion quality; validate against matched outcomes.",
                ),
                "evidence": evidence,
                "validation_window": "14d",
                "rollback_condition": "Rollback if error rate rises by >5 percentage points or success rate falls by >10%.",
            })
    findings.sort(key=lambda item: (-item["confidence"], item["recommendation_id"]))
    return {"schema_version": SCHEMA_VERSION,
            "catalog_version": catalog.get("catalog_version"),
            "findings": findings, "cohorts": groups,
            "groups_analyzed": len(groups)}


def efficiency_insights(since="7d ago", filters=None):
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    report = analyze_efficiency_rows(rows, load_pricing_catalog(), load_business_store(), filters)
    report["generated_at"] = _now_iso()
    report["source_window"] = since
    eligible = sum(1 for item in report["findings"] if item["eligible_for_approval"])
    lines = [f"Window: {since}; {report['groups_analyzed']} matched cohorts analyzed.",
             f"Findings: {len(report['findings'])}; approval-eligible: {eligible}."]
    return _envelope("Claude agent and harness efficiency", report, lines), False


def harness_recommendations(since="14d ago", filters=None):
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    report = analyze_efficiency_rows(rows, load_pricing_catalog(), load_business_store(), filters)
    report["generated_at"] = _now_iso()
    report["source_window"] = since
    report["findings"] = [item for item in report["findings"] if item["eligible_for_approval"]]
    lines = [f"Generated {len(report['findings'])} evidence-backed recommendations.",
             "Every amendment requires owner approval; no harness was modified."]
    return _envelope("Claude harness recommendations", report, lines), False


def _load_decisions(path=None):
    target = Path(path) if path else _decision_store_path
    if target is None:
        return []
    try:
        safe = _safe_absolute(target, "decision store")
        with open(safe, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def record_recommendation_decision(recommendation_id, decision, owner, rationale):
    recommendation_id = str(recommendation_id or "").strip()
    if not re.match(r"^rec_[a-f0-9]{16}$", recommendation_id):
        return "invalid recommendation_id", True
    decision = str(decision or "").strip().lower()
    if decision not in {"approved", "rejected", "deferred"}:
        return "decision must be approved, rejected, or deferred", True
    owner = str(owner or "").strip()
    rationale = str(rationale or "").strip()
    if not owner or not rationale:
        return "owner and rationale are required", True
    if len(rationale) > 1000:
        return "rationale is too long (maximum 1000 characters)", True
    if _decision_store_path is None:
        return "recommendation decision store is not configured", True
    entry = {
        "recommendation_id": recommendation_id,
        "decision": decision,
        "owner_hash": hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16],
        "rationale_hash": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
        "ts": _now_iso(),
    }
    with _store_lock:
        decisions = _load_decisions()
        duplicate = next((item for item in decisions
                          if item.get("recommendation_id") == recommendation_id
                          and item.get("decision") == decision
                          and item.get("owner_hash") == entry["owner_hash"]
                          and item.get("rationale_hash") == entry["rationale_hash"]), None)
        if duplicate:
            entry = duplicate
            idempotent = True
        else:
            decisions.append(entry)
            _atomic_write_json(_decision_store_path, decisions)
            idempotent = False
    emitted = emit_otlp_records([entry], "berserk-mcp-recommendation") if not idempotent else False
    payload = {"schema_version": SCHEMA_VERSION, "record": entry,
               "idempotent": idempotent, "emitted_otlp": emitted}
    return _envelope("Claude recommendation decision recorded", payload,
                     [f"{recommendation_id}: {decision}."]), False


def optimization_impact(agent_profile, before_harness, after_harness,
                        since="30d ago", project=""):
    for value, name in ((agent_profile, "agent_profile"),
                        (before_harness, "before_harness"),
                        (after_harness, "after_harness")):
        try:
            _identifier(value, name, True)
        except ValueError as exc:
            return str(exc), True
    rows, error = _fetch_usage(since)
    if error:
        return rows, True
    catalog = load_pricing_catalog()
    store = load_business_store()
    normalized = _normalized_usage_rows(rows, store)
    normalized = [row for row in normalized if row.get("agent_profile") == agent_profile
                  and (not project or row.get("project_id") == project)]
    before_cohorts = {
        (row.get("model"), row.get("speed") or "normal",
         row.get("query_source") or "unspecified")
        for row in normalized
        if row.get("harness_version") == before_harness and row.get("model")
    }
    after_cohorts = {
        (row.get("model"), row.get("speed") or "normal",
         row.get("query_source") or "unspecified")
        for row in normalized
        if row.get("harness_version") == after_harness and row.get("model")
    }
    matched_cohorts = sorted(before_cohorts & after_cohorts)
    matched = [
        row for row in normalized
        if (row.get("model"), row.get("speed") or "normal",
            row.get("query_source") or "unspecified") in matched_cohorts
    ]
    cohorts = _aggregate(matched, ["harness_version"], catalog)
    by_harness = {row["harness_version"]: row for row in cohorts}
    before = by_harness.get(before_harness)
    after = by_harness.get(after_harness)
    verdict = "insufficient-data"
    metrics = {}
    if before and after and before["events"] >= MIN_RECOMMENDATION_EVENTS and after["events"] >= MIN_RECOMMENDATION_EVENTS:
        before_cost = before["public_api_equivalent_usd"] / max(1, before["events"])
        after_cost = after["public_api_equivalent_usd"] / max(1, after["events"])
        cost_change = (after_cost - before_cost) / before_cost if before_cost else 0.0
        error_delta = after["error_rate"] - before["error_rate"]
        success_delta = after["success_rate"] - before["success_rate"]
        before_latency = before.get("average_duration_seconds", 0)
        after_latency = after.get("average_duration_seconds", 0)
        latency_change = (
            (after_latency - before_latency) / before_latency if before_latency else 0.0
        )
        before_delivery = (
            before.get("commits", 0) + before.get("pull_requests", 0)
        ) / float(max(1, before["events"]))
        after_delivery = (
            after.get("commits", 0) + after.get("pull_requests", 0)
        ) / float(max(1, after["events"]))
        delivery_change = (
            (after_delivery - before_delivery) / before_delivery if before_delivery else 0.0
        )
        delivery_regressed = before_delivery > 0 and delivery_change < -0.10
        if (error_delta > 0.05 or success_delta < -0.10 or cost_change > 0.10
                or (before_latency and latency_change > 0.20) or delivery_regressed):
            verdict = "recommend-rollback"
        elif cost_change <= -0.05:
            verdict = "keep"
        else:
            verdict = "no-material-change"
        metrics = {"cost_per_event_change_pct": round(cost_change * 100.0, 2),
                   "error_rate_delta": round(error_delta, 4),
                   "success_rate_delta": round(success_delta, 4),
                   "average_duration_change_pct": round(latency_change * 100.0, 2),
                   "delivery_outcomes_per_event_change_pct": round(
                       delivery_change * 100.0, 2
                   ) if before_delivery else None}
    payload = {"schema_version": SCHEMA_VERSION, "agent_profile": agent_profile,
               "project_id": project, "before": before, "after": after,
               "matched_cohorts": [
                   {"model": model, "speed": speed, "query_source": query_source}
                   for model, speed, query_source in matched_cohorts
               ], "metrics": metrics,
               "verdict": verdict, "catalog_version": catalog.get("catalog_version"),
               "generated_at": _now_iso(), "source_window": since}
    return _envelope("Claude harness optimization impact", payload,
                     [f"Verdict: {verdict}."]), False


def management_report(scope="portfolio", identifier="", since="90d ago"):
    if scope == "feature":
        return feature_cost(identifier, since)
    if scope == "project":
        return project_economics(identifier, since)
    group = "portfolio" if scope == "portfolio" else "team"
    return spend_overview(since, group_by=group, limit=50)


def _dashboard_payload(dashboard, identifier, since):
    rows, error = _fetch_usage(since)
    if error:
        raise RuntimeError(str(rows))
    catalog = load_pricing_catalog()
    store = load_business_store()
    if dashboard == "feature":
        payload = _feature_snapshot(_identifier(identifier, "feature_id", True), rows, catalog, store)
    elif dashboard == "project":
        payload = _project_snapshot(_identifier(identifier, "project_id", True), rows, catalog, store)
    elif dashboard == "agent_efficiency":
        payload = analyze_efficiency_rows(rows, catalog, store)
    elif dashboard == "data_quality":
        report = build_spend_overview(rows, catalog, store, "project", {}, 100)
        overall = report["overall"]
        normalized = _normalized_usage_rows(rows, store)
        unpriced_models = sorted({
            row.get("model") or "unknown" for row in normalized
            if calculate_public_cost(row, catalog).get("pricing_status") != "priced"
            and (row.get("input_tokens") or row.get("output_tokens"))
        })
        decision_counts = defaultdict(int)
        for decision in _load_decisions():
            decision_counts[str(decision.get("decision") or "unknown")] += 1
        payload = {"schema_version": SCHEMA_VERSION,
                   "pricing_coverage": overall.get("pricing_coverage", 0),
                   "attribution_coverage": overall.get("attribution_coverage", 0),
                   "native_telemetry_coverage": overall.get("native_telemetry_coverage", 0),
                   "exact_token_coverage": overall.get("exact_token_coverage", 0),
                   "exact_rows": overall.get("exact_rows", 0),
                   "estimated_rows": overall.get("estimated_rows", 0),
                   "unpriced_tokens": overall.get("unpriced_tokens", 0),
                   "unpriced_models": unpriced_models,
                   "business_data_stale": _business_data_stale(store),
                   "recommendation_decisions": dict(sorted(decision_counts.items()))}
    else:
        payload = build_spend_overview(rows, catalog, store, "project", {}, 30)
    payload["generated_at"] = _now_iso()
    payload["source_window"] = since
    payload["pricing_catalog_version"] = catalog.get("catalog_version")
    payload["business_data_updated_at"] = store.get("updated_at", "")
    return payload


def _markdown_dashboard(title, payload, since):
    lines = [f"# {title}", "", f"Generated: {_now_iso()}", f"Window: {since}", ""]
    if "overall" in payload:
        overall = payload["overall"]
        lines.extend([
            "## Summary", "",
            f"- API-equivalent cost: ${overall.get('public_api_equivalent_usd', 0):.4f}",
            f"- Input tokens: {overall.get('input_tokens', 0)}",
            f"- Output tokens: {overall.get('output_tokens', 0)}",
            f"- Attribution coverage: {overall.get('attribution_coverage', 0) * 100:.1f}%",
            f"- Pricing coverage: {overall.get('pricing_coverage', 0) * 100:.1f}%", "",
            "## Breakdown", "",
            "| Group | API-equivalent USD | Events | Error rate |",
            "|---|---:|---:|---:|",
        ])
        field = _GROUP_FIELD.get(payload.get("group_by"), "project_id")
        for row in payload.get("groups", []):
            lines.append(f"| {_redact(row.get(field, 'unattributed'))} | "
                         f"{row.get('public_api_equivalent_usd', 0):.4f} | "
                         f"{row.get('events', 0)} | {row.get('error_rate', 0) * 100:.1f}% |")
    else:
        lines.extend(["## Report", "", "```json",
                      json.dumps(payload, indent=2, sort_keys=True), "```"])
    lines.extend(["", "---", "Costs are public API equivalents, not invoices. "])
    return "\n".join(lines)


def _html_dashboard(title, payload, since):
    groups = payload.get("groups", []) if isinstance(payload, dict) else []
    max_cost = max([row.get("public_api_equivalent_usd", 0) for row in groups] or [1])
    bars = []
    field = _GROUP_FIELD.get(payload.get("group_by"), "project_id") if isinstance(payload, dict) else "project_id"
    for index, row in enumerate(groups[:20]):
        width = 0 if not max_cost else 700 * row.get("public_api_equivalent_usd", 0) / max_cost
        bars.append(
            f'<text x="10" y="{25 + index * 30}" class="label">{html.escape(str(_redact(row.get(field, "unattributed"))))}</text>'
            f'<rect x="230" y="{8 + index * 30}" width="{width:.1f}" height="20" rx="3" />'
            f'<text x="{240 + width:.1f}" y="{24 + index * 30}" class="value">${row.get("public_api_equivalent_usd", 0):.4f}</text>'
        )
    overall = payload.get("overall", {}) if isinstance(payload, dict) else {}
    raw_json = html.escape(json.dumps(payload, indent=2, sort_keys=True))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:15px system-ui;margin:2rem;color:#17212b;background:#f7f9fb}}
.cards{{display:flex;gap:1rem;flex-wrap:wrap}}.card{{background:white;padding:1rem 1.4rem;border-radius:9px;box-shadow:0 1px 4px #ccd}}
svg{{background:white;border-radius:9px;margin-top:1rem}}rect{{fill:#6e56cf}}.label{{font-size:12px}}.value{{font-size:12px;fill:#333}}
details{{margin-top:1rem}}pre{{white-space:pre-wrap;background:white;padding:1rem;border-radius:9px}}</style></head>
<body><h1>{html.escape(title)}</h1><p>Generated {_now_iso()} · window {html.escape(since)}</p>
<div class="cards"><div class="card"><b>API-equivalent cost</b><br>${overall.get('public_api_equivalent_usd', 0):.4f}</div>
<div class="card"><b>Attribution coverage</b><br>{overall.get('attribution_coverage', 0) * 100:.1f}%</div>
<div class="card"><b>Pricing coverage</b><br>{overall.get('pricing_coverage', 0) * 100:.1f}%</div></div>
<svg width="1000" height="{max(100, 30 * len(bars) + 20)}" role="img" aria-label="AI cost breakdown">{''.join(bars)}</svg>
<details><summary>Structured data</summary><pre>{raw_json}</pre></details>
<p>Costs are public API equivalents, not invoices.</p></body></html>"""


def generate_dashboard(dashboard="portfolio", identifier="", since="90d ago",
                       fmt="markdown", filename=""):
    if dashboard not in {"portfolio", "project", "feature", "agent_efficiency", "data_quality"}:
        return "invalid dashboard type", True
    if fmt not in {"markdown", "html"}:
        return "format must be markdown or html", True
    if dashboard in {"project", "feature"} and not identifier:
        return f"{dashboard} dashboard requires an identifier", True
    if _report_dir is None:
        return "report directory is not configured", True
    try:
        payload = _dashboard_payload(dashboard, identifier, since)
    except (ValueError, RuntimeError) as exc:
        return str(exc), True
    payload = _sanitize_payload(payload)
    suffix = ".md" if fmt == "markdown" else ".html"
    if filename:
        if not _SAFE_FILENAME_RE.match(filename) or Path(filename).name != filename:
            return "filename must be a simple basename", True
        if not filename.endswith(suffix):
            filename += suffix
    else:
        name_part = f"-{identifier}" if identifier else ""
        filename = f"claude-{dashboard}{name_part}{suffix}"
    target = _safe_absolute(Path(_report_dir).resolve() / filename, "report")
    root = _safe_absolute(Path(_report_dir), "report directory")
    if target.parent != root:
        return "report output must remain inside BERSERK_MCP_REPORT_DIR", True
    title = "Claude " + dashboard.replace("_", " ").title()
    content = _markdown_dashboard(title, payload, since) if fmt == "markdown" else _html_dashboard(title, payload, since)
    _atomic_write_text(target, content, private=False)
    result = {"schema_version": SCHEMA_VERSION, "dashboard": dashboard,
              "format": fmt, "path": str(target), "generated_at": _now_iso()}
    return _envelope("Claude dashboard generated", result, [f"Report: {target}"]), False


def build_bi_datasets(raw_rows, catalog, store=None, decisions=None):
    store = store or _empty_business_store()
    decisions = decisions if decisions is not None else []
    rows = _normalized_usage_rows(raw_rows, store)
    daily = _aggregate(rows, ["day", "team_id", "project_id", "repository_id",
                              "feature_id", "agent_profile", "harness_version", "model"], catalog)
    features = [_feature_snapshot(str(feature.get("feature_id")), raw_rows, catalog, store)
                for feature in store.get("features", []) if feature.get("feature_id")]
    project_ids = sorted({str(feature.get("project_id")) for feature in store.get("features", [])
                          if feature.get("project_id")} | {str(row.get("project_id")) for row in rows
                                                          if row.get("project_id")})
    projects = [_project_snapshot(project_id, raw_rows, catalog, store) for project_id in project_ids]
    analysis = analyze_efficiency_rows(raw_rows, catalog, store)
    efficiency = analysis["cohorts"]
    latest_decisions = {}
    for decision in decisions:
        recommendation_id = str(decision.get("recommendation_id") or "")
        current = latest_decisions.get(recommendation_id)
        if recommendation_id and (
                current is None or str(decision.get("ts") or "") >= str(current.get("ts") or "")):
            latest_decisions[recommendation_id] = decision
    recommendation_status = []
    for finding in analysis["findings"]:
        decision = latest_decisions.get(finding["recommendation_id"], {})
        recommendation_status.append({
            "schema_version": SCHEMA_VERSION,
            "recommendation_id": finding["recommendation_id"],
            "code": finding["code"],
            "confidence": finding["confidence"],
            "sample_size": finding["sample_size"],
            "eligible_for_approval": finding["eligible_for_approval"],
            "status": decision.get("decision", "proposed"),
            "decision_timestamp": decision.get("ts", ""),
            "amendment": finding["amendment"],
            "expected_result": finding["expected_result"],
            "risks": finding["risks"],
        })
    known_recommendations = {row["recommendation_id"] for row in recommendation_status}
    recommendation_status.extend(
        dict({"schema_version": SCHEMA_VERSION, "status": item.get("decision", "")}, **item)
        for rec_id, item in latest_decisions.items() if rec_id not in known_recommendations
    )
    effort_daily_map = {}
    for effort in store.get("effort", []):
        key = tuple(str(effort.get(field) or "") for field in (
            "work_date", "feature_id", "work_item_id", "team_id"
        ))
        slot = effort_daily_map.setdefault(key, {
            "schema_version": SCHEMA_VERSION,
            "work_date": key[0], "feature_id": key[1],
            "work_item_id": key[2], "team_id": key[3],
            "actual_hours": 0.0, "worklog_count": 0,
        })
        slot["actual_hours"] += _nonnegative_float(
            _first(effort.get("actual_hours"), effort.get("hours"))
        )
        slot["worklog_count"] += 1
    effort_daily = [effort_daily_map[key] for key in sorted(effort_daily_map)]
    for effort in effort_daily:
        effort["actual_hours"] = round(effort["actual_hours"], 2)
    overview = build_spend_overview(raw_rows, catalog, store, "project", {}, 100)
    unpriced_models = sorted({
        row.get("model") or "unknown"
        for row in rows
        if calculate_public_cost(row, catalog).get("pricing_status") != "priced"
        and (row.get("input_tokens") or row.get("output_tokens")
             or row.get("cache_read_tokens") or row.get("cache_creation_tokens"))
    })
    updated_at = str(store.get("updated_at") or "")
    stale_business_data = _business_data_stale(store)
    quality = [{
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "pricing_coverage": overview["overall"].get("pricing_coverage", 0),
        "attribution_coverage": overview["overall"].get("attribution_coverage", 0),
        "native_telemetry_coverage": overview["overall"].get("native_telemetry_coverage", 0),
        "exact_token_coverage": overview["overall"].get("exact_token_coverage", 0),
        "exact_rows": overview["overall"].get("exact_rows", 0),
        "estimated_rows": overview["overall"].get("estimated_rows", 0),
        "unpriced_tokens": overview["overall"].get("unpriced_tokens", 0),
        "unpriced_models": unpriced_models,
        "business_data_updated_at": updated_at,
        "business_data_stale": stale_business_data,
    }]
    return {
        "ai_usage_daily": daily,
        "feature_cost_snapshot": features,
        "project_cost_snapshot": projects,
        "human_effort_daily": effort_daily,
        "agent_harness_efficiency": efficiency,
        "harness_recommendation_status": recommendation_status,
        "attribution_quality": quality,
    }


def _csv_text(rows):
    flat_rows = []
    fields = set()
    for row in rows:
        flat = {}
        for key, value in row.items():
            flat[key] = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            fields.add(key)
        flat_rows.append(flat)
    fieldnames = sorted(fields)
    if not fieldnames:
        return ""
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(flat_rows)
    return buffer.getvalue()


def export_bi(since, output_dir, fmt="csv"):
    if fmt not in {"csv", "ndjson"}:
        raise ValueError("BI format must be csv or ndjson")
    target_dir = _safe_absolute(output_dir, "BI output")
    rows, error = _fetch_usage(since)
    if error:
        raise RuntimeError(str(rows))
    catalog = load_pricing_catalog()
    datasets = _sanitize_payload(
        build_bi_datasets(rows, catalog, load_business_store(), _load_decisions())
    )
    serialized = {}
    for name, values in datasets.items():
        if fmt == "csv":
            serialized[name + ".csv"] = _csv_text(values)
        else:
            serialized[name + ".ndjson"] = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in values
            )
    generated_at = _now_iso()
    generation_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + f"-{os.getpid()}-{threading.get_ident()}"
    )
    snapshot_dir = target_dir / ".snapshots" / generation_id
    quality = (datasets.get("attribution_quality") or [{}])[0]
    warnings = []
    if quality.get("pricing_coverage", 1) < 1:
        warnings.append("Some token usage is unpriced.")
    if quality.get("attribution_coverage", 1) < 0.8:
        warnings.append("Feature attribution coverage is below 80%.")
    if quality.get("exact_token_coverage", 1) < 0.8:
        warnings.append("Exact token coverage is below 80%.")
    if quality.get("business_data_stale", True):
        warnings.append("Feature or developer-effort data is missing or older than seven days.")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "since": since,
        "format": fmt,
        "pricing_catalog_version": catalog.get("catalog_version"),
        "coverage": {
            "pricing": quality.get("pricing_coverage", 0),
            "attribution": quality.get("attribution_coverage", 0),
            "exact_tokens": quality.get("exact_token_coverage", 0),
            "native_telemetry": quality.get("native_telemetry_coverage", 0),
        },
        "data_quality_warnings": warnings,
        "datasets": {},
    }
    for filename, content in serialized.items():
        dataset_name = filename.rsplit(".", 1)[0]
        manifest["datasets"][dataset_name] = {
            "filename": str(Path(".snapshots") / generation_id / filename),
            "latest_filename": filename,
            "rows": len(datasets[dataset_name]),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    # Build an immutable generation first. The manifest is committed last and
    # points at this snapshot, so a failed export leaves the previous manifest
    # and all files it references intact.
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    for filename, content in serialized.items():
        _atomic_write_text(snapshot_dir / filename, content, private=False)
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in serialized.items():
        _atomic_write_text(target_dir / filename, content, private=False)
    _atomic_write_json(target_dir / "manifest.json", manifest, private=False)
    return manifest


_WORK_CONTEXT_FIELDS = {
    "team": "business.team.id", "portfolio": "business.portfolio.id",
    "project": "business.project.id", "feature": "business.feature.id",
    "work_item": "business.work_item.id", "cost_center": "business.cost_center",
    "repository": "code.repository.id", "branch": "code.branch.id",
    "agent_profile": "berserk.agent.profile", "harness_version": "berserk.harness.version",
    "recommendation_id": "berserk.recommendation.id",
}


def build_work_context_attributes(existing="", **values):
    attrs = {}
    for item in str(existing or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if key.strip() and value.strip():
                attrs[key.strip()] = value.strip()
    for short, otel_name in _WORK_CONTEXT_FIELDS.items():
        value = values.get(short)
        if value:
            attrs[otel_name] = _identifier(value, short, True)
    return ",".join(f"{key}={attrs[key]}" for key in sorted(attrs))


def launcher_main():
    parser = argparse.ArgumentParser(prog="berserk-claude",
                                     description="Launch Claude Code with governed AI-cost attribution tags")
    for name in _WORK_CONTEXT_FIELDS:
        parser.add_argument("--" + name.replace("_", "-"))
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Claude command and arguments; defaults to 'claude'")
    args = parser.parse_args()
    values = {name: getattr(args, name) for name in _WORK_CONTEXT_FIELDS}
    env = dict(os.environ)
    env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    env["OTEL_RESOURCE_ATTRIBUTES"] = build_work_context_attributes(
        env.get("OTEL_RESOURCE_ATTRIBUTES", ""), **values
    )
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    command = command or ["claude"]
    os.execvpe(command[0], command, env)
