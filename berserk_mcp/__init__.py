#!/usr/bin/env python3
"""berserk-mcp — a Model Context Protocol server for Berserk observability.

Lets an LLM answer observability questions by *calling tools* instead of
hand-authoring KQL. Each tool wraps a verified Kusto/KQL query, so the model
cannot mangle field names or table references — the determinism is the point.

Transport: newline-delimited JSON-RPC 2.0 over stdio (the MCP stdio transport).
Dependencies: none. Pure Python standard library, so it runs anywhere `bzrk`
(the Berserk CLI) is installed, including Windows.

It shells out to the `bzrk` CLI for every query. The Berserk bearer token lives
only in `bzrk`'s own config (typically 0600) and is never read, stored, or
logged by this server.

Configuration (all optional, via environment):
  BZRK_BIN                 trusted path/name of the bzrk binary   (default: "bzrk")
  BZRK_PROFILE             bzrk profile to query                  (default: "local")
  BZRK_TIMEOUT             per-query timeout in seconds           (default: "120")
  BERSERK_WORKER_JITTER_SECONDS  max random startup delay for --worker (default: "7200")
  BERSERK_MCP_TOOL_BUDGET_SECONDS interactive tools/call budget (default: "10")
  BERSERK_MCP_FAIL_COOLDOWN_SECONDS identical timeout suppression (default: "30")
  BERSERK_MCP_CACHE_TTL_SECONDS read-only result cache TTL (default: "120")
  BERSERK_MCP_CACHE_MAX_ENTRIES entries kept in the result cache and the fail-cooldown table (default: "256")
  BERSERK_MCP_KQL_VALIDATION validation policy: off/warn/strict (default: "warn")
  BERSERK_MCP_KQL_LIVE_VALIDATION enable validate_kql mode=live (default: "0")
  BERSERK_MCP_MAX_CONCURRENT_QUERIES in-process query concurrency (default: "2")
  BERSERK_MCP_KQL_MAX_CHARS maximum user KQL length (default: "50000")
  BERSERK_MCP_KQL_MAX_ROWS recommended arbitrary-query row bound (default: "2000")
  BERSERK_MCP_KQL_STATS stats handling: off/auto/required (default: "auto")
  BERSERK_MCP_MAX_RESULT_BYTES hard cap for bzrk stdout (default: 10485760)
  BERSERK_MCP_MAX_OUTPUT_CHARS characters of search/saved-query result sent to the model; 0 = no cap (default: "40000")
  BERSERK_MCP_FINOPS_REDACT_ENTROPY enable entropy redaction in FinOps free text (default: "0")
  BERSERK_TABLE            the Berserk table to query             (default: "default")
  BERSERK_MCP_LEARNED_PATH where saved queries persist  (default: per-user config dir)

Parser factory (LLM-driven parser generation, see parser_factory.py) adds
outbound HTTP to LLM providers -- all optional, a provider with no key
configured is skipped:
  BERSERK_LLM_LADDER          provider order for generation    (default: "hermes,openai,anthropic")
  HERMES_API_KEY               bearer token for the Hermes endpoint
  BERSERK_LLM_HERMES_URL       Hermes chat-completions endpoint (else local
                               llm_config.json, else http://localhost:3000/...;
                               set via: berserk-mcp --set-hermes-url <URL>)
  BERSERK_LLM_HERMES_MODEL     Hermes model id            (default: auto-discovered via /api/models)
  OPENAI_API_KEY                OpenAI API key
  BERSERK_LLM_OPENAI_MODEL     OpenAI model                     (default: "gpt-4o")
  ANTHROPIC_API_KEY             Anthropic API key
  BERSERK_LLM_ANTHROPIC_MODEL  Anthropic model                  (default: "claude-opus-4-8")
  BERSERK_LLM_TIMEOUT          per-LLM-call timeout in seconds  (default: "120")

This is an unofficial, community-maintained integration. It is not affiliated
with or endorsed by the Berserk project.
"""

import sys
import json
import re
import os
import threading
import time
import uuid
import urllib.error
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import suppress
from pathlib import Path

import _http
import _kql_boundary
import agent_analytics
import investigation
import ai_finops
import kql_validation
import parser_factory
import quota_status
import secret_scan
import tool_discovery

from berserk_mcp._version import __version__ as __version__
from berserk_mcp import _facade
from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
from berserk_mcp import learned as bm_learned
from berserk_mcp import httpconfig as bm_httpconfig
from berserk_mcp import doctor as bm_doctor
from berserk_mcp.handlers import tail as bm_tail
from berserk_mcp.handlers import learning as bm_learning


# ---------- configuration (env-overridable) ----------


# ---------- learned-query store ----------


# ── CanonLoom knowledge-pipeline bridge ──────────────────────────────────────


def _handle_parser_core(name, arguments):
    """Parser-factory tools: detect, generate, review. Returns (text, is_error) or None."""
    if name == "detect_new_sources":
        since = arguments.get("since") or "24h ago"
        auto_queue = arguments.get("auto_queue") is True
        check_drift = arguments.get("check_drift") is True
        text = parser_factory.detect_new_sources(
            since=since,
            auto_queue=auto_queue,
            check_drift=check_drift,
            load_json_list=bm_config.load_json_list,
            save_json_list=bm_config.save_json_list,
            discovery_queue_path=bm_config.DISCOVERY_QUEUE_PATH,
            active_role=bm_config.ACTIVE_ROLE,
        )
        return text, False
    if name == "generate_parser":
        service = str(arguments.get("service") or "").strip()
        metric = str(arguments.get("metric") or "").strip()
        if bool(service) == bool(metric):
            return "generate_parser needs exactly one of 'service' or 'metric'.", True
        target = service or metric
        if not bm_queries._valid_interpolated_name(target):
            return "invalid source name (allowed: letters, digits, '.', '_', '-')", True
        kind = "service" if service else "metric"
        role_hint = bm_config.normalize_roles(arguments.get("role_hint"))
        job = {
            "source": target,
            "kind": kind,
            "role_hint": role_hint[0] if role_hint else "",
        }
        report, ok = parser_factory.generate_parser_for(job)
        return json.dumps(report, separators=(",", ":")), not ok
    if name == "run_discovery_worker":
        raw_max = arguments.get("max_jobs")
        try:
            max_jobs = int(raw_max) if raw_max is not None else 1
        except (TypeError, ValueError):
            max_jobs = 1
        max_jobs = max(1, min(max_jobs, 5))
        outcomes, any_needs_human = bm_learning._drain_pending_jobs(max_jobs)
        if outcomes is None:
            return "No pending discovery jobs.", False
        return "\n".join(outcomes), any_needs_human
    if name == "review_generated":
        items = bm_learned.load_learned()
        generated = [it for it in items if "generated_by" in it]
        nm = arguments.get("name")
        if nm:
            nm = bm_learned.sanitize_name(nm)
            match = next((it for it in generated if it["name"] == nm), None)
            if not match:
                return f"No generated query named '{nm}'.", True
            return json.dumps(match, separators=(",", ":")), False
        if not generated:
            return "No generated queries yet.", False
        lines = []
        for it in generated:
            gb = it.get("generated_by", {})
            status = bm_config.GENERATED_PENDING if bm_config.awaiting_approval(it) else bm_config.GENERATED_APPROVED
            lines.append(
                f"- {it['name']}: {it.get('description', '')} "
                f"[{gb.get('provider', '?')}/{gb.get('model', '?')} @ {gb.get('ts', '?')}] "
                f"status={status}"
            )
        return (
            "Generated queries:\n"
            + "\n".join(lines)
            + "\nA pending query is hidden from the small tier until an operator runs "
            "berserk-mcp --approve-generated <name>."
        ), False
    return None


def _handle_validate_kql(arguments):
    """Handle the validate_kql tool. Returns (text, is_error)."""
    kql = arguments.get("kql")
    if not kql:
        return "missing required 'kql'", True
    since = arguments.get("since") or "15m ago"
    mode = str(arguments.get("mode") or "static").strip().lower()
    if mode not in {"static", "live"}:
        return "mode must be 'static' or 'live'", True
    use_schema = arguments.get("use_schema", True) is not False
    report = bm_runner._validate_user_kql(
        str(kql),
        since,
        use_schema=use_schema,
        allow_refresh_schema=(mode == "live"),
    )
    if mode == "live":
        if not bm_config.KQL_LIVE_VALIDATION:
            return (
                "live validation is disabled; set BERSERK_MCP_KQL_LIVE_VALIDATION=1 to allow validate_kql mode=live.",
                True,
            )
        if any(f.get("severity") == "error" for f in report.get("findings", [])):
            return json.dumps(report, separators=(",", ":")), True
        # The report above is advisory; this is the same mandatory check
        # bzrk_search applies, since this path calls run_bzrk directly.
        boundary_error = _kql_boundary.check(str(kql), bm_config.TABLE)
        if boundary_error:
            return boundary_error, True
        budget = bm_config._window_budget(
            bm_config.TOOL_BUDGET_SECONDS if bm_config.TOOL_BUDGET_SECONDS > 0 else bm_config.DEFAULT_TIMEOUT, since
        )
        argv = ["-P", bm_config.PROFILE, "search", str(kql), "--since", since]
        if bm_config.KQL_STATS_MODE != "off":
            argv.append("--stats")
        start = time.monotonic()
        with bm_config._query_semaphore_slot(budget) as acquired:
            if not acquired:
                return "Local MCP query queue is full; retry later or narrow the time window.", True
            out, err = bm_runner.run_bzrk(argv, timeout=budget)
        duration_ms = int((time.monotonic() - start) * 1000)
        stats = kql_validation.parse_cli_stats(out if not err else "")
        runtime = {
            "duration_ms": duration_ms,
            "timed_out": bool(err and str(out).lower().startswith("bzrk timed out")),
            "rows_returned": stats.get("rows_returned"),
            "rows_processed": stats.get("rows_processed"),
            "bytes_scanned": stats.get("bytes_scanned"),
            "engine_stats": stats.get("engine_stats", {}),
            "stats_available": stats.get("stats_available", False),
            "budget_seconds": budget,
            "budget_compatible": not err,
        }
        report["runtime"] = runtime
        if not stats.get("stats_available"):
            report.setdefault("findings", []).append(
                {
                    "code": "STATS_UNAVAILABLE",
                    "severity": "info",
                    "message": "Engine statistics were unavailable or unrecognized; duration was measured locally.",
                    "location": "runtime",
                    "recommendation": "",
                }
            )
        if err:
            report["runtime_error"] = bm_fencing._fence_untrusted(out)
        # `out` is already fenced above before being assigned into
        # report; the taint tracker can't follow it through the
        # dict-field write and json.dumps, but the fencing genuinely
        # happened.
        return json.dumps(report, separators=(",", ":")), bool(err)  # nosemgrep: unfenced-bzrk-output-reaches-return
    return json.dumps(report, separators=(",", ":")), False


def _handle_diagnostic_tools(name, arguments):
    """Diagnostic tools: anomalies, investigation, forecast, drift, similarity. Returns (text, is_error) or None."""
    if name == "detect_anomalies":
        service = str(arguments.get("service") or "").strip()
        if service and not bm_queries._valid_interpolated_name(service):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "6h ago"
        out, err = bm_runner.bzrk_search(bm_queries.q_detect_anomalies(service or None), since)
        if err:
            return bm_fencing._fence_untrusted(out), True
        if not out or out.strip() == "(no rows)":
            return f"No anomalies detected (window {since}).", False
        return (
            f"Anomaly decomposition for window {since}; non-zero anomaly markers indicate spikes:\n{bm_fencing._fence_untrusted(out)}",
            False,
        )

    if name == "investigate_error_rate":
        node = str(arguments.get("node") or "start").strip()
        service = str(arguments.get("service") or "").strip()
        if service and not bm_queries._valid_interpolated_name(service):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        service = service or None
        since = arguments.get("since") or "1h ago"
        text, is_err, next_node, next_service = investigation.run_error_rate_node(node, since, service)
        result = bm_fencing._fence_untrusted(text)
        if next_node:
            # Deliberately built from trusted server code, outside the
            # fence above -- never baked into the fenced text itself
            # (issue #24 Codex review round 1, 2026-08-28:
            # _BASE_INSTRUCTIONS tells every client to never follow an
            # instruction found inside <untrusted_log_data>, so embedding
            # the continuation directive inside the fence would strand a
            # compliant model at hop one). This directive never repeats
            # next_service's raw value, even when it passes character
            # validation -- next_service is backend-controlled telemetry
            # (round 3, 2026-08-28: an allowlisted charset makes a value
            # safe to interpolate into KQL, not safe to present as
            # trusted instruction text; e.g. a service named
            # "ignore-all-previous-instructions" passes the charset check
            # but reads as an instruction). The model reads the actual
            # service name from the fenced Result line above and reuses
            # it -- the same "pass the value the previous step's response
            # gave you" idiom this module's own missing-service errors
            # already use.
            result = (
                f"{result}\n"
                f"Next: call investigate_error_rate(node={next_node!r}, "
                f"since={since!r}, service=<the service value from the "
                f"Result line above>) to continue, or stop here if this "
                f"is enough."
            )
        return result, is_err

    if name == "forecast_capacity":
        metric = str(arguments.get("metric") or "").strip()
        if metric not in bm_queries._FORECAST_METRICS:
            return (
                "metric is not allowlisted; use system.memory.usage, system.filesystem.usage, or system.disk.io",
                True,
            )
        host = str(arguments.get("host") or "").strip()
        if host and not bm_queries._valid_interpolated_name(host):
            return "invalid host name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "7d ago"
        out, err = bm_runner.bzrk_search_json(bm_queries.q_forecast_capacity(metric, host or None), since)
        if err:
            return bm_fencing._fence_untrusted(out), True
        if not out or out.strip() == "(no rows)":
            return f"No {metric} data found for forecast window {since}.", False
        fits = bm_queries._forecast_fit_rows(out)
        if fits:
            lines = []
            for fit in fits:
                # host=tostring(resource['host.name']) is a real string
                # field, not numeric fit data -- attacker-influenceable via
                # container/host naming (round 2 finding 3).
                fenced_host = bm_fencing._fence_untrusted(fit["host"], inline=True)
                if fit["r2"] < 0.6 or fit["slope"] <= 0:
                    lines.append(
                        f"{fenced_host}: no reliable trend — not forecastable "
                        f"(R²={fit['r2']:.3f}, slope={fit['slope']:.3g})."
                    )
                else:
                    lines.append(
                        f"{fenced_host}: reliable upward trend "
                        f"(R²={fit['r2']:.3f}, slope={fit['slope']:.3g}); "
                        "native fit array returned, but no ceiling/date is inferred."
                    )
            return (f"Capacity trend for {metric} (window {since}):\n" + "\n".join(lines)), False
        return (
            f"Capacity trend for {metric} (window {since}). Native fit arrays include "
            "R² and slope; unable to parse coefficients from this renderer, so no "
            "forecast date is inferred:\n" + bm_fencing._fence_untrusted(out)
        ), False

    return _handle_drift_and_similarity(name, arguments)


def _handle_model_drift(name, arguments):
    """model_drift_check and model_drift_history. Returns (text, is_error) or None."""
    import model_drift

    if name == "model_drift_check":
        model = str(arguments.get("model") or "").strip()
        if model and not bm_queries._valid_model_id(model):
            return "invalid model id (allowed: letters, digits, '.', '_', '-', '/')", True
        since = arguments.get("since") or "30d ago"
        out, err = bm_runner.bzrk_search_json(model_drift.series_kql(model or None), since)
        if err:
            return bm_fencing._fence_untrusted(out), True
        if not out or out.strip() == "(no rows)":
            return (
                f"No canary results in {since}. Is --canary-run scheduled and BERSERK_MCP_CANARY_MODELS set?"
            ), False
        try:
            grouped = model_drift.group_by_model(out)
        except model_drift.BzrkResultParseError as exc:
            return bm_fencing._fence_untrusted(f"could not read canary results: {exc}"), True
        lines = []
        for model_name, series in grouped.items():
            verdict = model_drift.classify(series)
            fenced = bm_fencing._fence_untrusted(model_name, inline=True)
            line = f"{fenced}: {verdict['verdict']} ({verdict['confidence']}) — {verdict['reason']}"
            if verdict["fingerprint_values"]:
                fp_text = ", ".join(
                    f"{k}={bm_fencing._fence_untrusted(v[-1], inline=True)}"
                    for k, v in sorted(verdict["fingerprint_values"].items())
                )
                line += f" [fingerprint: {fp_text}]"
            lines.append(line)
        return f"Model drift (window {since}):\n" + "\n".join(lines), False

    if name == "model_drift_history":
        model = str(arguments.get("model") or "").strip()
        if not model or not bm_queries._valid_model_id(model):
            return "model is required (allowed: letters, digits, '.', '_', '-', '/')", True
        since = arguments.get("since") or "30d ago"
        out, err = bm_runner.bzrk_search_json(model_drift.series_kql(model), since)
        if err:
            return bm_fencing._fence_untrusted(out), True
        if not out or out.strip() == "(no rows)":
            return f"No canary results for {model} in {since}.", False
        try:
            grouped = model_drift.group_by_model(out)
        except model_drift.BzrkResultParseError as exc:
            return bm_fencing._fence_untrusted(f"could not read canary results: {exc}"), True
        series_data = grouped.get(model, [])
        if not series_data:
            return f"No data for model {model}.", False
        lines = [
            f"Model quality history for {bm_fencing._fence_untrusted(model, inline=True)} (tool-routing only, window {since}):"
        ]
        for row in series_data:
            ts = row.get("timestamp", "?")
            acc = row.get("tool_accuracy", "?")
            status = bm_fencing._fence_untrusted(row.get("status", "?"), inline=True)
            line = f"  {ts}: accuracy={acc}, status={status}"
            for key in ("behavioral_fingerprint", "provider_metadata_fingerprint"):
                val = row.get(key)
                if val:
                    line += f", {key}={bm_fencing._fence_untrusted(val, inline=True)}"
            lines.append(line)
        return "\n".join(lines), False

    return None


def _handle_drift_and_similarity(name, arguments):
    """Model drift and semantic similarity tools. Returns (text, is_error) or None."""
    result = _handle_model_drift(name, arguments)
    if result is not None:
        return result
    if name == "find_similar":
        description = str(arguments.get("description") or "").strip()
        if not description:
            return "missing required 'description'", True
        if len(description) > 500:
            return "description is too long (maximum 500 characters)", True
        if bm_queries._TEXT_GUARD_RE.search(description):
            return "description may not contain quotes, pipe, backslash, or backtick", True
        service = str(arguments.get("service") or "").strip()
        if service and not bm_queries._valid_interpolated_name(service):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        try:
            k = max(1, min(50, int(arguments.get("k", 10))))
        except (TypeError, ValueError):
            return "k must be an integer between 1 and 50", True
        since = arguments.get("since") or "6h ago"
        out, err = bm_runner.bzrk_search_json(bm_queries.q_find_similar(description, service or None, k), since)
        if err:
            if "similarto" in str(out).lower() or "semantic" in str(out).lower():
                return (
                    "Semantic indexing is not enabled on this Berserk cluster — "
                    "falling back is not possible for meaning-based search; use "
                    "search with has '<term>' for exact terms.",
                    False,
                )
            return bm_fencing._fence_untrusted(out), True
        if "_score" in out and not bm_queries._find_similar_has_real_score(out):
            return (
                "Semantic indexing is not enabled on this Berserk cluster — falling "
                "back is not possible for meaning-based search; use search with "
                "has '<term>' for exact terms.",
                False,
            )
        return bm_fencing._fence_untrusted(out), False
    return None


def _handle_parser_factory(name, arguments):
    """Parser-factory / diagnostic tools. Returns (text, is_error) or None."""
    result = _handle_parser_core(name, arguments)
    if result is not None:
        return result
    if name == "validate_kql":
        return _handle_validate_kql(arguments)
    return _handle_diagnostic_tools(name, arguments)


def _handle_query_tools(name, arguments):
    """Schema, service query, trace, and search tools. Returns (text, is_error) or None."""
    if name == "self_check":
        results = bm_doctor._run_doctor_checks()
        code = bm_doctor._doctor_exit_code(results)
        return json.dumps({"checks": results, "exit_code": code}, separators=(",", ":"), sort_keys=True), code == 2
    if name == "schema":
        return bm_runner.do_schema()
    if name == "discover_schema":
        svc = arguments.get("service")
        if svc and not bm_queries._valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        svc_str = str(svc) if svc else None
        out1, e1 = bm_runner.bzrk_search(bm_queries.q_discover_fieldstats(svc_str), since)
        out2, e2 = bm_runner.bzrk_search(bm_queries.q_discover_sample(svc_str), since)
        fenced1 = bm_fencing._fence_untrusted(out1)
        fenced2 = bm_fencing._fence_untrusted(out2)
        return f"== resource fieldstats ==\n{fenced1}\n\n== sample rows ==\n{fenced2}", (e1 and e2)
    if name == "logs_for_service":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not bm_queries._valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        out, err = bm_runner.bzrk_search_json(bm_queries.q_logs(str(svc)), since)
        return bm_fencing._fence_untrusted(out), err
    if name == "sre_service_health":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not bm_queries._valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        out, err = bm_runner.bzrk_search(bm_queries.q_sre_service_health(str(svc)), since)
        return bm_fencing._fence_untrusted(out), err
    if name == "soc_timeline":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not bm_queries._valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "6h ago"
        out, err = bm_runner.bzrk_search_json(bm_queries.q_soc_timeline(str(svc)), since)
        return bm_fencing._fence_untrusted(out), err
    return _handle_search_tools(name, arguments)


def _handle_search_tools(name, arguments):
    """Trace, search, find_tool, and claude_search. Returns (text, is_error) or None."""
    if name == "trace_analyze":
        trace_id = arguments.get("trace_id")
        if not trace_id:
            return "missing required 'trace_id'", True
        if len(str(trace_id)) > bm_queries.MAX_TRACE_ID_CHARS or not bm_queries._TRACE_ID_RE.fullmatch(str(trace_id)):
            return "invalid trace_id (allowed: letters and digits only)", True
        out1, e1 = bm_runner.bzrk_search(bm_queries.q_trace_analyze(str(trace_id)), "30d ago")
        out2, e2 = bm_runner.bzrk_search_json(bm_queries.q_trace_logs(str(trace_id)), "30d ago")
        out1 = bm_fencing._fence_untrusted(out1)
        out2 = bm_fencing._fence_untrusted(out2)
        return f"== spans ==\n{out1}\n\n== correlated logs ==\n{out2}", (e1 and e2)
    if name == "search":
        kql = arguments.get("kql")
        if not kql:
            return "missing required 'kql'", True
        since = arguments.get("since") or "15m ago"
        warning = ""
        if bm_config.KQL_VALIDATION_MODE != "off":
            report = bm_runner._validate_user_kql(str(kql), since)
            if bm_runner._blocking_validation(report):
                return bm_runner._format_validation_rejection(report), True
            if bm_config.KQL_VALIDATION_MODE == "warn":
                warning = bm_runner._format_validation_warnings(report)
        out, err = bm_runner.bzrk_search_json(str(kql), since)
        if err:
            return bm_fencing._fence_untrusted(out), err
        out = bm_fencing._fence_limited(out)
        if warning:
            return warning + "\n\n" + out, False
        return out, False
    if name == "find_tool":
        intent = arguments.get("intent")
        if not intent:
            return "missing required 'intent'", True
        if len(str(intent)) > bm_queries.MAX_SEARCH_TERM_CHARS:
            return f"intent is too long (maximum {bm_queries.MAX_SEARCH_TERM_CHARS} characters)", True
        visible = {t["name"]: t for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS if bm_config.tool_visible(t)}
        ranked = tool_discovery.search(bm_tools._DISCOVERY_INDEX, str(intent), top_k=5)
        candidates = [visible[n] for n, _ in ranked if n in visible]
        low_confidence = not candidates
        if low_confidence:
            candidates = [visible[n] for n in sorted(bm_tools._ANCHOR_TOOL_NAMES) if n in visible]
        payload = {
            "resultType": "low_confidence" if low_confidence else "complete",
            "candidates": [bm_tools._tool_candidate_view(t) for t in candidates],
        }
        if low_confidence:
            payload["message"] = (
                "No confident match for that intent -- these are the always-available anchor tools instead."
            )
        return json.dumps(payload, separators=(",", ":")), False
    if name == "claude_search":
        term = arguments.get("term")
        if not term:
            return "missing required 'term'", True
        if len(str(term)) > bm_queries.MAX_SEARCH_TERM_CHARS:
            return f"term is too long (maximum {bm_queries.MAX_SEARCH_TERM_CHARS} characters)", True
        if bm_queries._TEXT_GUARD_RE.search(str(term)):
            return "term may not contain quotes, pipe, backslash, or backtick", True
        since = arguments.get("since") or "6h ago"
        agent = arguments.get("agent") or "claude-code"
        out, err = bm_runner.bzrk_search_json(bm_queries.q_cc_search(str(term), agent), since)
        return bm_fencing._fence_untrusted(out), err
    return None


def _handle_analytics_tools(name, arguments):
    """Claude analytics tools (loop, model-fit, burn, quota, cost, session, workflow). Returns (text, is_error) or None."""
    if name == "claude_loop_check":
        since = arguments.get("since") or "6h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(agent_analytics.claude_loop_check(since))
    if name == "claude_model_fit":
        since = arguments.get("since") or "6h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(agent_analytics.claude_model_fit(since))
    if name == "claude_token_burn":
        since = arguments.get("since") or "6h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(agent_analytics.claude_token_burn(since))
    if name == "claude_quota_status":
        since = arguments.get("since") or "5h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        result = quota_status.get_quota_status(since=since)
        return quota_status.format_quota_status(result), False
    if name == "claude_cost_report":
        since = arguments.get("since") or "7d ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(
            agent_analytics.claude_cost_report(since, group_by=arguments.get("group_by") or "day")
        )
    if name == "claude_session_deep_dive":
        since = arguments.get("since") or "24h ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(
            agent_analytics.claude_session_deep_dive(str(arguments.get("session_id") or ""), since)
        )
    if name == "claude_workflow_insights":
        since = arguments.get("since") or "7d ago"
        if not bm_runner.valid_since(since):
            return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
        return bm_fencing._wrap_analytics(agent_analytics.claude_workflow_insights(since))
    return None


def _handle_finops_tools(name, arguments):
    """FinOps tools (spend, feature cost, economics, efficiency, recommendations, dashboard). Returns (text, is_error) or None."""
    if name not in {
        "claude_spend_overview",
        "claude_feature_cost",
        "claude_project_economics",
        "claude_efficiency_insights",
        "claude_harness_recommendations",
        "claude_optimization_impact",
        "claude_management_report",
        "claude_generate_dashboard",
    }:
        return None
    default_since = (
        "7d ago"
        if name
        in {
            "claude_spend_overview",
            "claude_efficiency_insights",
        }
        else "90d ago"
    )
    if name == "claude_harness_recommendations":
        default_since = "14d ago"
    if name == "claude_optimization_impact":
        default_since = "30d ago"
    since = arguments.get("since") or default_since
    if not bm_runner.valid_since(since):
        return (f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', '2d ago', or 'now'."), True
    filters = {
        key: str(arguments.get(key) or "").strip()
        for key in ("team", "project", "repository", "feature", "agent", "harness", "model")
        if arguments.get(key)
    }
    if name == "claude_spend_overview":
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            return "limit must be an integer between 1 and 100", True
        if not 1 <= limit <= 100:
            return "limit must be an integer between 1 and 100", True
        return ai_finops.spend_overview(
            since,
            group_by=arguments.get("group_by") or "day",
            filters=filters,
            limit=limit,
        )
    if name == "claude_feature_cost":
        return ai_finops.feature_cost(arguments.get("feature_id"), since)
    if name == "claude_project_economics":
        return ai_finops.project_economics(arguments.get("project_id"), since)
    if name == "claude_efficiency_insights":
        return ai_finops.efficiency_insights(since, filters=filters)
    if name == "claude_harness_recommendations":
        return ai_finops.harness_recommendations(since, filters=filters)
    if name == "claude_optimization_impact":
        return ai_finops.optimization_impact(
            str(arguments.get("agent_profile") or ""),
            str(arguments.get("before_harness") or ""),
            str(arguments.get("after_harness") or ""),
            since=since,
            project=str(arguments.get("project") or ""),
        )
    if name == "claude_management_report":
        scope = str(arguments.get("scope") or "portfolio")
        identifier = str(arguments.get("identifier") or "")
        if scope in {"feature", "project"} and not identifier:
            return f"{scope} scope requires 'identifier'", True
        return ai_finops.management_report(scope, identifier, since)
    return ai_finops.generate_dashboard(
        dashboard=str(arguments.get("dashboard") or "portfolio"),
        identifier=str(arguments.get("identifier") or ""),
        since=since,
        fmt=str(arguments.get("format") or "markdown"),
        filename=str(arguments.get("filename") or ""),
    )


def _handle_validated(name, arguments):
    """Tools needing input validation or extra calls. Returns (text, is_error) or None."""
    result = _handle_query_tools(name, arguments)
    if result is not None:
        return result
    result = _handle_analytics_tools(name, arguments)
    if result is not None:
        return result
    return _handle_finops_tools(name, arguments)


def _handle_call_uncached(name, arguments):
    """Dispatch a tools/call. Returns (text, is_error)."""
    if name.startswith("saved__"):
        # Dispatch target for a projected saved-query tool (see
        # _saved_query_tools / issue #5). Resolves against the same
        # role-filtered list run_saved uses, so a role-hidden entry is
        # indistinguishable from a name that was never saved -- this is the
        # enforcement F-008 relies on for callers that reach
        # _handle_call_uncached directly, bypassing dispatch()'s matched_tool
        # lookup (e.g. a direct handle_call() call, as most tests make).
        target = name[len("saved__") :]
        items = [it for it in bm_learned.load_learned() if bm_config.item_visible(it)]
        match = next((it for it in items if bm_learned.sanitize_name(it["name"]) == target), None)
        if not match:
            return "unknown tool: " + name, True
        return bm_learning._run_saved_entry(match, arguments.get("since"))

    result = bm_learning._handle_learning_loop(name, arguments)
    if result is not None:
        return result
    result = bm_learning._handle_discovery(name, arguments)
    if result is not None:
        return result
    result = _handle_parser_factory(name, arguments)
    if result is not None:
        return result

    # --- simple fixed-query tools ---
    if name in bm_tools.SIMPLE or name in bm_tools._AGENT_AWARE_SIMPLE:
        if name in bm_tools._AGENT_AWARE_SIMPLE:
            kql_fn, default_since = bm_tools._AGENT_AWARE_SIMPLE[name]
            kql = kql_fn(arguments.get("agent") or "claude-code")
        else:
            kql, default_since = bm_tools.SIMPLE[name]
        since = arguments.get("since") or default_since
        if name in bm_tools._SIMPLE_JSON_TOOLS:
            out, err = bm_runner.bzrk_search_json(kql, since)
        else:
            out, err = bm_runner.bzrk_search(kql, since)
        # Fencing (issue #11) is independent of ENVELOPE_ENABLED -- an
        # operator disabling the envelope for byte-identical prior output
        # must not also silently disable untrusted-data marking on the
        # body-bearing subset. Also applies on the error path (round 2
        # finding 4): the overflow rewrite below replaces `out` with a
        # clean server message, safe as-is, but any other error diagnostic
        # can still embed partial real rows (run_bzrk concatenates raw
        # stdout with stderr on a failed query) and must be fenced too.
        if err and out.startswith("bzrk result exceeded"):
            out = (
                f"Result exceeded BERSERK_MCP_MAX_RESULT_BYTES={bm_config.MAX_BZRK_RESULT_BYTES}."
                f" This tool's query is fixed — narrow the window, e.g. since='15m ago'."
            )
        elif bm_config.ENVELOPE_ENABLED and not err:
            # fence_body=True for all SIMPLE tools: host names, container
            # names, service names, and metric names are all attacker-
            # influenceable even when they're not log body content.
            out = bm_tools._envelope(name, since, out, fence_body=True)
        elif not err:
            # Success output for all SIMPLE tools is fenced -- same reason.
            out = bm_fencing._fence_untrusted(out)
        else:
            # Error output for every SIMPLE tool -- JSON or not -- can carry
            # partial real rows (run_bzrk concatenates raw stdout with
            # stderr on a failed query), so it's fenced unconditionally.
            # This branch used to only fence _SIMPLE_JSON_TOOLS' errors,
            # leaving every non-JSON tool's generic (non-overflow) query
            # failure completely unfenced -- caught by manual review after
            # 4 Codex rounds missed it, confirmed by direct reproduction
            # against list_hosts. The one caller that depends on this
            # text's shape (handle_call's timeout/fail-cooldown check,
            # ~line 3148) was changed to look for its marker as a substring
            # rather than requiring an unfenced exact prefix, so fencing
            # here doesn't break it.
            out = bm_fencing._fence_untrusted(out)
        return out, err

    if name == "soc_new_services":
        since = arguments.get("since") or "24h ago"
        out, err = bm_runner.bzrk_search(bm_queries.Q_SOC_NEW_SERVICES, since)
        if err:
            return bm_fencing._fence_untrusted(out), True
        baseline = parser_factory.load_json_dict(parser_factory._known_sources_path())
        known = set(baseline.get("services", {}).keys())
        if not known:
            return (
                "(no baseline — run detect_new_sources first to establish "
                "known services; showing all active services)\n" + bm_fencing._fence_untrusted(out)
            ), False
        lines = out.strip().splitlines()
        header = lines[0] if lines else ""
        filtered = [header] if header else []
        for line in lines[1:]:
            svc_name = line.split()[0] if line.split() else ""
            if svc_name and svc_name not in known:
                filtered.append(line)
        if len(filtered) <= 1:
            return "No genuinely new services (all active services are in the baseline).", False
        return bm_fencing._fence_untrusted("\n".join(filtered)), False

    result = _handle_validated(name, arguments)
    if result is not None:
        return result
    result = bm_tail._handle_tail(name, arguments)
    if result is not None:
        return result

    return "unknown tool: " + str(name), True


_CACHEABLE_TOOLS = frozenset(
    set(bm_tools.SIMPLE)
    | {
        "sre_service_health",
        "soc_timeline",
        "claude_loop_check",
        "claude_model_fit",
        "claude_token_burn",
        "claude_cost_report",
        "claude_session_deep_dive",
        "claude_workflow_insights",
        "claude_spend_overview",
        "claude_feature_cost",
        "claude_project_economics",
        "claude_efficiency_insights",
        "claude_harness_recommendations",
        "claude_optimization_impact",
        "claude_management_report",
    }
)


def _fleet_args_key(name, arguments):
    try:
        encoded = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(arguments)
    # The function identity prevents test doubles (and a reconfigured process)
    # from inheriting another backend's cached response.
    # Keep the callable itself in the key.  Using only ``id()`` can collide
    # when short-lived test doubles (or a hot-reloaded backend) are collected
    # and Python reuses their address.
    try:
        hash(bm_runner.run_bzrk)
        backend = bm_runner.run_bzrk
    except TypeError:
        backend = (type(bm_runner.run_bzrk), id(bm_runner.run_bzrk))
    return (backend, str(name), encoded)


def _fleet_backend_fingerprint():
    try:
        hash(bm_runner.run_bzrk)
        return bm_runner.run_bzrk
    except TypeError:
        return (type(bm_runner.run_bzrk), id(bm_runner.run_bzrk))


def _cache_marker(text, age):
    return f"{text}\n(cached, {age:.1f}s old)"


def handle_call(name, arguments):
    """Dispatch one tool call with fleet-friendly budget/cache controls."""
    args = arguments if isinstance(arguments, dict) else {}
    bm_runner._normalize_since_arg(args)
    backend_id = _fleet_backend_fingerprint()
    with bm_config._FLEET_LOCK:
        bm_config._note_fleet_backend(backend_id)
    key = _fleet_args_key(name, args)
    now = time.monotonic()

    with bm_config._FLEET_LOCK:
        if bm_config.FAIL_COOLDOWN_SECONDS > 0:
            failed = bm_config._FAIL_COOLDOWN.get(key)
            if failed and now - failed[2] < bm_config.FAIL_COOLDOWN_SECONDS:
                return (
                    f"{failed[0]}\n(fail-cooldown, {now - failed[2]:.1f}s old; identical retry suppressed)",
                    True,
                )
            if failed:
                bm_config._FAIL_COOLDOWN.pop(key, None)
        if name in _CACHEABLE_TOOLS and bm_config.CACHE_TTL_SECONDS > 0:
            cached = bm_config._RESULT_CACHE.get(key)
            if cached and now - cached[2] < bm_config.CACHE_TTL_SECONDS:
                return _cache_marker(cached[0], now - cached[2]), cached[1]
            if cached:
                bm_config._RESULT_CACHE.pop(key, None)

    previous_context = bm_config._set_fleet_context(
        {
            "tool": str(name),
            "budget": bm_config.TOOL_BUDGET_SECONDS if bm_config.TOOL_BUDGET_SECONDS > 0 else None,
            "budget_multiplier": bm_tools._tool_budget_multiplier(name),
        }
    )
    try:
        text, is_err = _handle_call_uncached(name, args)
    finally:
        bm_config._set_fleet_context(previous_context)

    text = str(text)
    # `in`, not startswith: the SIMPLE-dispatch error path now fences every
    # error (issue #11 manual-review fix), which wraps this message in
    # <untrusted_log_data> tags for the 4 _SIMPLE_JSON_TOOLS -- a prefix
    # check would silently stop matching and fail-cooldown would never
    # trigger for those tools' timeouts. The exact phrase (tool name +
    # "exceeded its") is specific enough that a substring check doesn't
    # introduce a false-positive risk.
    timed_out = is_err and f"{name} exceeded its " in text
    with bm_config._FLEET_LOCK:
        stamp = time.monotonic()
        if timed_out and bm_config.FAIL_COOLDOWN_SECONDS > 0:
            bm_config._bounded_put(
                bm_config._FAIL_COOLDOWN, key, (text, True, stamp), ttl=bm_config.FAIL_COOLDOWN_SECONDS, now=stamp
            )
        elif name in _CACHEABLE_TOOLS and not is_err and bm_config.CACHE_TTL_SECONDS > 0:
            bm_config._bounded_put(
                bm_config._RESULT_CACHE, key, (text, False, stamp), ttl=bm_config.CACHE_TTL_SECONDS, now=stamp
            )
    return text, is_err


# ---------- JSON-RPC plumbing ----------
# BUG-005 (2026-07-18 security review): three real defects fixed together
# here, since they're all about dispatch() trusting shapes it must not:
#   1. dispatch([]) (or any non-dict top-level value) raised an uncaught
#      AttributeError from req.get(...) -- confirmed live -- which propagated
#      out of main()'s loop with no handler and killed the whole server
#      process. A single malformed line from a connected stdio client was a
#      full process-level denial of service.
#   2. Every request branch (tools/call, initialize, tools/list, ping)
#      unconditionally returned a response dict, even when the incoming
#      message had no "id" -- i.e. was itself a notification. Only the
#      unknown-method fallback checked for that. Notifications are one-way
#      by JSON-RPC/MCP definition; a client sending e.g. a tools/call
#      notification got a response anyway.
#   3. initialize echoed back whatever protocolVersion the client sent,
#      instead of negotiating: this server implements exactly one version
#      (PROTOCOL_VERSION), so it must report that version regardless of
#      what the client claims to speak.
def _jsonrpc_error(code, message, id_=None):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_unsupported_protocol(id_, requested):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {
                "supported": list(bm_config.SUPPORTED_PROTOCOL_VERSIONS),
                "requested": requested,
            },
        },
    }


def _valid_mcp_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _modern_mcp_enabled():
    return bool(bm_config.ENABLE_MCP_2026_07_28)


def _request_meta(params):
    """Return a validated modern-MCP metadata object, or None if malformed.

    MCP 2026-07-28 moves protocol information into per-request ``_meta``.
    Phase 1 only adds internal mode selection; actual modern methods are added
    in later phases.
    """
    meta = params.get("_meta") if isinstance(params, dict) else None
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        return None
    return meta


def _requested_protocol_version(params):
    meta = _request_meta(params)
    if meta is None:
        return None
    version = meta.get(bm_config.MCP_META_PROTOCOL_VERSION)
    if version is None:
        version = meta.get("protocolVersion")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _protocol_mode_for_request(method, params):
    """Select the internal MCP compatibility mode for a validated request.

    Default behavior stays legacy. Modern mode is selected only when the
    explicit feature flag is on and the request advertises the modern protocol
    through per-request metadata.
    """
    del method  # reserved for method-specific routing in Phase 2+
    if _modern_mcp_enabled() and _requested_protocol_version(params) == bm_config.MCP_PROTOCOL_MODERN:
        return bm_config.PROTOCOL_MODE_MODERN
    return bm_config.PROTOCOL_MODE_LEGACY


def _valid_modern_meta(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    requested = _requested_protocol_version(params)
    caps = meta.get(bm_config.MCP_META_CLIENT_CAPABILITIES)
    client_info = meta.get(bm_config.MCP_META_CLIENT_INFO)
    return requested == bm_config.MCP_PROTOCOL_MODERN and isinstance(caps, dict) and isinstance(client_info, dict)


def _list_changed_supported():
    """True only when a save could plausibly reach the client: stdio can
    push notifications/tools/list_changed at any time (see _TRANSPORT),
    and there must be something for it to announce -- a saved__* projection
    disabled via SAVED_TOOL_PROJECTION_CAP=0 never changes tools/list at
    all, so advertising the capability would just be noise."""
    return _TRANSPORT == "stdio" and bm_learned.SAVED_TOOL_PROJECTION_CAP > 0


# 2026-07-28 delivers list-changed notifications only on a subscriptions/listen
# stream the client opened, and the server MUST NOT send a type the client did
# not request. A stdio process serves one client, so this is per-process state.
_LISTEN_FILTER_BOOL_KEYS = frozenset({"toolsListChanged", "promptsListChanged", "resourcesListChanged"})
_LISTEN_SUBSCRIPTIONS = {}
# Guards _LISTEN_SUBSCRIPTIONS and delivery, so nothing is sent after a cancel
# (task threads notify while the main loop handles listen/cancel).
_LISTEN_LOCK = threading.Lock()
_MAX_LISTEN_SUBSCRIPTIONS = 1024  # the SDK reference server's default
_MODERN_STDIO_CLIENT = False


def _note_request_mode(mode):
    global _MODERN_STDIO_CLIENT
    if mode == bm_config.PROTOCOL_MODE_MODERN and _TRANSPORT == "stdio":
        _MODERN_STDIO_CLIENT = True


def _valid_listen_filter(requested):
    # Unknown keys are ignored, not rejected: the SDK's SubscriptionFilter schema strips them.
    if not isinstance(requested, dict):
        return False
    if any(not isinstance(requested[k], bool) for k in _LISTEN_FILTER_BOOL_KEYS & set(requested)):
        return False
    uris = requested.get("resourceSubscriptions", [])
    return isinstance(uris, list) and all(isinstance(u, str) for u in uris)


def _dispatch_listen(params, id_, is_notification, mode):
    """subscriptions/listen: acknowledge on stdio, then hold the request open.

    The listen request gets no response while the subscription lives; the spec
    answers it only on graceful teardown. Tools are the only list this server
    changes, so only toolsListChanged is ever agreed to.
    """
    if is_notification:
        return None
    if mode != bm_config.PROTOCOL_MODE_MODERN or _TRANSPORT != "stdio":
        return _jsonrpc_error(-32601, "Method not found", id_)
    requested = params.get("notifications")
    if (
        set(params) - {"_meta", "notifications"}
        or not _valid_modern_meta(params)
        or not _valid_listen_filter(requested)
    ):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    agreed = (
        {"toolsListChanged": True} if requested.get("toolsListChanged") is True and _list_changed_supported() else {}
    )
    with _LISTEN_LOCK:
        if id_ in _LISTEN_SUBSCRIPTIONS:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        if len(_LISTEN_SUBSCRIPTIONS) >= _MAX_LISTEN_SUBSCRIPTIONS:
            return _jsonrpc_error(-32603, "Subscription limit reached", id_)
        _LISTEN_SUBSCRIPTIONS[id_] = agreed
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": {bm_config.MCP_META_SUBSCRIPTION_ID: id_}, "notifications": agreed},
            }
        )
    return None


def _notify_tools_list_changed():
    """Legacy clients get the unsolicited notification; a modern client gets it
    only on each listen stream that opted in to toolsListChanged."""
    if not _list_changed_supported():
        return
    if not _MODERN_STDIO_CLIENT:
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return
    with _LISTEN_LOCK:
        for sub_id, agreed in _LISTEN_SUBSCRIPTIONS.items():
            if agreed.get("toolsListChanged"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed",
                        "params": {"_meta": {bm_config.MCP_META_SUBSCRIPTION_ID: sub_id}},
                    }
                )


def _notify_tools_list_changed_hook():
    # Looks the sender up at call time, so a test that replaces
    # _notify_tools_list_changed is still the one that runs.
    _notify_tools_list_changed()


bm_learned.set_tools_changed_notifier(_notify_tools_list_changed_hook)


def _discover_result():
    capabilities = {
        "tools": {"listChanged": _list_changed_supported()},
        "extensions": {
            "tasks": {
                "uri": bm_config.MCP_TASK_EXTENSION_URI,
                "methods": ["tasks/get", "tasks/cancel"],
                "createHint": "Set arguments.as_task=true on eligible long-running tools.",
            }
        },
    }
    return {
        "resultType": "complete",
        "supportedVersions": [bm_config.MCP_PROTOCOL_MODERN, bm_config.MCP_PROTOCOL_LEGACY],
        "capabilities": capabilities,
        "_meta": {
            bm_config.MCP_META_SERVER_INFO: bm_config.SERVER_INFO,
        },
        "instructions": bm_config.INSTRUCTIONS,
        # Role and environment can change tool visibility/instructions, so this
        # is cacheable only for the current caller/deployment context.
        "ttlMs": bm_config.MCP_PRIVATE_CACHE_TTL_MS,
        "cacheScope": "private",
    }


_TASKS = {}
_TASK_LOCK = threading.RLock()


def _task_now():
    return time.time()


def _task_public(record):
    return {
        "id": record["id"],
        "status": record["status"],
        "tool": record["tool"],
        "createdAt": record["created_at"],
        "updatedAt": record["updated_at"],
        "expiresAt": record["expires_at"],
    }


def _task_result(record):
    payload = {"resultType": "complete", "task": _task_public(record)}
    if record.get("result") is not None:
        payload["result"] = record["result"]
    if record.get("error"):
        payload["error"] = record["error"]
    return payload


def _task_prune_locked(now=None):
    now = _task_now() if now is None else now
    expired = [task_id for task_id, record in _TASKS.items() if record.get("expires_ts", 0) <= now]
    for task_id in expired:
        _TASKS.pop(task_id, None)
    if len(_TASKS) > bm_config.MCP_MAX_TASKS:
        removable = sorted(
            (
                (record.get("updated_ts", 0), task_id)
                for task_id, record in _TASKS.items()
                if record.get("status") in {"complete", "failed", "cancelled"}
            )
        )
        for _, task_id in removable[: len(_TASKS) - bm_config.MCP_MAX_TASKS]:
            _TASKS.pop(task_id, None)


def _launch_task_worker(target):
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    return worker


def _execute_task_tool(name, arguments, mode):
    text, is_err = handle_call(name, arguments)
    text = secret_scan.apply_output_filter(
        text,
        mode=bm_config.REDACT_MODE,
        include_entropy=bm_config.REDACT_ENTROPY,
        pii_types=bm_config.REDACT_PII_TYPES,
    )
    return _tool_call_result(name, text, is_err, mode)


def _run_task(task_id, name, arguments, mode):
    with _TASK_LOCK:
        record = _TASKS.get(task_id)
        if record is None or record.get("status") == "cancelled":
            return
        record["status"] = "running"
        record["updated_ts"] = _task_now()
        record["updated_at"] = bm_config.now_iso()
    try:
        result = _execute_task_tool(name, arguments, mode)
        status = "complete"
        error = ""
    except Exception as exc:  # pragma: no cover - defensive boundary
        result = None
        status = "failed"
        error = type(exc).__name__
    with _TASK_LOCK:
        record = _TASKS.get(task_id)
        if record is None or record.get("status") == "cancelled":
            return
        record["status"] = status
        record["result"] = result
        record["error"] = error
        record["updated_ts"] = _task_now()
        record["updated_at"] = bm_config.now_iso()


def _create_task(name, arguments, mode):
    now = _task_now()
    task_id = "task_" + uuid.uuid4().hex
    record = {
        "id": task_id,
        "status": "pending",
        "tool": name,
        "role": bm_config.ACTIVE_ROLE,
        "created_ts": now,
        "updated_ts": now,
        "expires_ts": now + bm_config.MCP_TASK_TTL_SECONDS,
        "created_at": bm_config.now_iso(),
        "updated_at": bm_config.now_iso(),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + bm_config.MCP_TASK_TTL_SECONDS)),
        "result": None,
        "error": "",
    }
    with _TASK_LOCK:
        _task_prune_locked(now)
        if len(_TASKS) >= bm_config.MCP_MAX_TASKS:
            return None
        _TASKS[task_id] = record
    _launch_task_worker(lambda: _run_task(task_id, name, dict(arguments), mode))
    return {"resultType": "task", "task": _task_public(record)}


def _task_lookup(task_id):
    with _TASK_LOCK:
        _task_prune_locked()
        record = _TASKS.get(task_id)
        if record is None or record.get("role") != bm_config.ACTIVE_ROLE:
            return None
        return dict(record)


def _client_supports_tasks(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    caps = meta.get(bm_config.MCP_META_CLIENT_CAPABILITIES)
    if not isinstance(caps, dict):
        return False
    extensions = caps.get("extensions")
    return isinstance(caps.get("tasks"), dict) or (
        isinstance(extensions, dict) and ("tasks" in extensions or bm_config.MCP_TASK_EXTENSION_URI in extensions)
    )


def _task_id_from_params(params):
    task_id = params.get("taskId") or params.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
        return None
    return task_id


def _tool_list_result(mode):
    allt = [
        t for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS + bm_learned._saved_query_tools() if bm_config.tool_visible(t)
    ]
    if bm_tools.BERSERK_MCP_DISCOVERY:
        allt = [t for t in allt if t["name"] in bm_tools._ANCHOR_TOOL_NAMES]
    tl = []
    hidden = bm_tools._hidden_tool_names()
    builtin = {t["name"] for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS}
    for t in allt:
        visible_tool = bm_tools._with_output_schema(t) if mode == bm_config.PROTOCOL_MODE_MODERN else t
        description = visible_tool["description"]
        if visible_tool["name"] in builtin:
            # saved__* descriptions are fenced user/model text; never edit them.
            description = bm_tools._without_hidden_tool_sentences(description, hidden)
        item = {
            "name": visible_tool["name"],
            "title": bm_tools.TITLES.get(visible_tool["name"], visible_tool["name"]),
            "description": description,
            "inputSchema": visible_tool["inputSchema"],
            "annotations": bm_tools.annotations_for(visible_tool["name"]),
        }
        if "outputSchema" in visible_tool:
            item["outputSchema"] = visible_tool["outputSchema"]
        tl.append(item)
    result = {"tools": tl}
    if mode == bm_config.PROTOCOL_MODE_MODERN:
        result["resultType"] = "complete"
        result["ttlMs"] = bm_config.MCP_PRIVATE_CACHE_TTL_MS
        result["cacheScope"] = "private"
    return result


def _extract_structured_content(name, text, is_error):
    if is_error or name not in bm_tools._STRUCTURED_OUTPUT_TOOLS:
        return None
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("schema_version"), str):
        return payload
    return None


def _input_required_result(reason, message, request_state, input_requests=None):
    result = {
        "resultType": "input_required",
        "reason": reason,
        "content": [{"type": "text", "text": message}],
        "requestState": json.dumps(request_state, sort_keys=True, separators=(",", ":")),
    }
    if input_requests:
        result["inputRequests"] = input_requests
    return result


def _looks_bounded_kql(kql):
    lowered = str(kql or "").lower()
    return any(
        re.search(r"\b" + re.escape(operator) + r"\b", lowered)
        for operator in ("take", "limit", "count", "summarize", "top")
    )


def _modern_preflight_input_required(name, arguments):
    if name == "search":
        kql = str(arguments.get("kql") or "")
        since = arguments.get("since") or "15m ago"
        if (
            bm_runner.valid_since(since)
            and bm_config._since_hours(since) > bm_config.MCP_EXPENSIVE_SEARCH_WINDOW_HOURS
            and not _looks_bounded_kql(kql)
            and arguments.get("allow_expensive") is not True
        ):
            message = (
                "This custom KQL spans more than 24 hours and does not appear "
                "to include a bounding operator such as take, limit, count, "
                "summarize, or top. Narrow the window, add a bounding operator, "
                "or retry with arguments.allow_expensive=true if this cost is "
                "intentional."
            )
            return _input_required_result(
                "expensive_query_guard",
                message,
                {
                    "tool": name,
                    "since": since,
                    "window_hours": bm_config._since_hours(since),
                    "suggested_actions": [
                        "narrow since to 24h ago or less",
                        "add take/limit/count/summarize/top",
                        "retry with allow_expensive=true after explicit approval",
                    ],
                },
            )
    if name == "claude_feature_cost" and not str(arguments.get("feature_id") or "").strip():
        return _input_required_result(
            "missing_finops_attribution",
            "Feature economics requires a feature_id so spend can be attributed without guessing.",
            {
                "tool": name,
                "missing": ["feature_id"],
                "suggested_actions": ["provide feature_id"],
            },
        )
    if name == "claude_project_economics" and not str(arguments.get("project_id") or "").strip():
        return _input_required_result(
            "missing_finops_attribution",
            "Project economics requires a project_id so spend can be attributed without guessing.",
            {
                "tool": name,
                "missing": ["project_id"],
                "suggested_actions": ["provide project_id"],
            },
        )
    return None


def _tool_call_result(name, text, is_error, mode):
    result = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if mode == bm_config.PROTOCOL_MODE_MODERN:
        result["resultType"] = "complete"
        structured = _extract_structured_content(name, text, is_error)
        if structured is not None:
            result["structuredContent"] = structured
    return result


def dispatch(req):
    """Handle one JSON-RPC request per JSON-RPC 2.0 and MCP 2025-06-18.

    Returns a response dict, or None for valid notifications.
    """
    if not isinstance(req, dict):
        return _jsonrpc_error(-32600, "Invalid Request")

    if req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        return _jsonrpc_error(-32600, "Invalid Request")

    has_id = "id" in req
    id_ = req.get("id")
    if has_id and not _valid_mcp_id(id_):
        return _jsonrpc_error(-32600, "Invalid Request")

    is_notification = not has_id
    method = req["method"]

    if "params" in req and not isinstance(req["params"], dict):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)

    params = req.get("params") or {}

    try:
        mode = _protocol_mode_for_request(method, params)
        _note_request_mode(mode)
        return _dispatch_validated(method, params, id_, is_notification, mode=mode)
    except Exception as exc:
        bm_config.log(f"dispatch failed: {type(exc).__name__}")
        if is_notification:
            return None
        return _jsonrpc_error(-32603, "Internal error", id_)


def _dispatch_discover(params, id_, mode):
    """Handle server/discover. Returns response."""
    if mode != bm_config.PROTOCOL_MODE_MODERN:
        if _modern_mcp_enabled() and _request_meta(params) is None:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        requested = _requested_protocol_version(params)
        if _modern_mcp_enabled() and requested and requested not in bm_config.SUPPORTED_PROTOCOL_VERSIONS:
            return _jsonrpc_unsupported_protocol(id_, requested)
        if _modern_mcp_enabled() and requested != bm_config.MCP_PROTOCOL_MODERN:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _jsonrpc_error(-32601, "Method not found", id_)
    if set(params) - {"_meta"}:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    return _jsonrpc_result(id_, _discover_result())


def _dispatch_tasks(method, params, id_, mode):
    """Handle tasks/get and tasks/cancel. Returns response."""
    if mode != bm_config.PROTOCOL_MODE_MODERN:
        return _jsonrpc_error(-32601, "Method not found", id_)
    if set(params) - {"_meta", "taskId", "id"}:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    task_id = _task_id_from_params(params)
    if task_id is None:
        return _jsonrpc_error(-32602, "Invalid params", id_)
    record = _task_lookup(task_id)
    if record is None:
        return _jsonrpc_error(-32602, "Unknown task", id_)
    if method == "tasks/cancel":
        with _TASK_LOCK:
            current = _TASKS.get(task_id)
            if current is None or current.get("role") != bm_config.ACTIVE_ROLE:
                return _jsonrpc_error(-32602, "Unknown task", id_)
            if current.get("status") in {"pending", "running"}:
                current["status"] = "cancelled"
                current["updated_ts"] = _task_now()
                current["updated_at"] = bm_config.now_iso()
            record = dict(current)
    return _jsonrpc_result(id_, _task_result(record))


def _dispatch_protocol(method, params, id_, is_notification, mode):
    """Handle MCP protocol methods (discover, tasks, initialize, ping). Returns response or NOT_MATCHED."""
    if method == "server/discover":
        if is_notification:
            return None
        return _dispatch_discover(params, id_, mode)
    if method in {"tasks/get", "tasks/cancel"}:
        if is_notification:
            return None
        return _dispatch_tasks(method, params, id_, mode)
    if method == "initialize":
        if is_notification:
            return None
        pv = params.get("protocolVersion")
        if not isinstance(pv, str) or not pv.strip():
            return _jsonrpc_error(-32602, "Invalid params", id_)
        caps = params.get("capabilities")
        if caps is not None and not isinstance(caps, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        client_info = params.get("clientInfo")
        if client_info is not None and not isinstance(client_info, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _jsonrpc_result(
            id_,
            {
                "protocolVersion": bm_config.PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": _list_changed_supported()}},
                "serverInfo": bm_config.SERVER_INFO,
                "instructions": bm_config.INSTRUCTIONS,
            },
        )
    if method == "notifications/initialized":
        if not is_notification:
            return _jsonrpc_error(-32600, "Invalid Request", id_)
        return None
    if method == "ping":
        if params:
            if is_notification:
                return None
            return _jsonrpc_error(-32602, "Invalid params", id_)
        if is_notification:
            return None
        return _jsonrpc_result(id_, {})

    return "NOT_MATCHED"


def _dispatch_tools_list(params, id_, is_notification, mode):
    """Handle tools/list. Returns response or None."""
    if set(params) - {"_meta", "cursor"}:
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if "_meta" in params and not isinstance(params["_meta"], dict):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if params.get("cursor") is not None:
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if mode == bm_config.PROTOCOL_MODE_MODERN and not _valid_modern_meta(params):
        if is_notification:
            return None
        return _jsonrpc_error(-32602, "Invalid params", id_)
    if is_notification:
        return None
    return _jsonrpc_result(id_, _tool_list_result(mode))


# Argument names every tool accepts although no inputSchema declares them:
# protocol-level keys read before handle_call (modern task creation, and the
# cost-approval retry after an input_required preflight).
_PROTOCOL_ARGUMENTS = frozenset({"as_task", "allow_expensive"})
_MAX_REPORTED_ARGUMENT_NAMES = 5
_MAX_REPORTED_ARGUMENT_CHARS = 64


def _unknown_argument_error(tool, arguments):
    """Error text when `arguments` holds a name the tool's schema does not
    declare, else None.

    Review 2026-09-26 P2: a misspelled optional filter (`svc` for `service`)
    used to be ignored, so the call ran unfiltered and the answer looked
    filtered. Rejecting the name and listing the valid ones lets the model
    correct itself. Caller-supplied names are bounded before being echoed.
    """
    declared = set(tool.get("inputSchema", {}).get("properties", {}))
    unknown = sorted(name for name in arguments if name not in declared and name not in _PROTOCOL_ARGUMENTS)
    if not unknown:
        return None
    shown = [repr(name[:_MAX_REPORTED_ARGUMENT_CHARS]) for name in unknown[:_MAX_REPORTED_ARGUMENT_NAMES]]
    if len(unknown) > _MAX_REPORTED_ARGUMENT_NAMES:
        shown.append(f"and {len(unknown) - _MAX_REPORTED_ARGUMENT_NAMES} more")
    valid = ", ".join(sorted(declared)) or "(none)"
    label = "argument" if len(unknown) == 1 else "arguments"
    return f"unknown {label} {', '.join(shown)} for {tool['name']}; valid: {valid}"


def _dispatch_tools_call(params, id_, mode):
    """Handle tools/call (never a notification). Returns response."""
    if mode == bm_config.PROTOCOL_MODE_MODERN and not _valid_modern_meta(params):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    name = params.get("name")
    if not name or not isinstance(name, str):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _jsonrpc_error(-32602, "Invalid params", id_)
    arguments = arguments or {}
    bm_runner._normalize_since_arg(arguments)
    matched_tool = next((t for t in bm_tools.TOOLS + bm_tools.MGMT_TOOLS if t["name"] == name), None)
    if matched_tool is None and name.startswith("saved__"):
        matched_tool = next((t for t in bm_learned._saved_query_tools() if t["name"] == name), None)
    # Visibility first: a hidden tool must answer "unknown tool" whatever its
    # arguments, or the argument error below would reveal its schema.
    argument_error = None
    if matched_tool is not None and bm_config.tool_visible(matched_tool):
        argument_error = _unknown_argument_error(matched_tool, arguments)
    if matched_tool is not None and not bm_config.tool_visible(matched_tool):
        text, is_err = "unknown tool: " + name, True
    elif argument_error is not None:
        text, is_err = argument_error, True
    else:
        if mode == bm_config.PROTOCOL_MODE_MODERN:
            input_required = _modern_preflight_input_required(name, arguments)
            if input_required is not None:
                return _jsonrpc_result(id_, input_required)
            if (
                name in bm_tools._TASK_ELIGIBLE_TOOLS
                and arguments.get("as_task") is True
                and _client_supports_tasks(params)
            ):
                task_args = dict(arguments)
                task_args.pop("as_task", None)
                task_result = _create_task(name, task_args, mode)
                if task_result is None:
                    return _jsonrpc_error(-32000, "Task limit reached", id_)
                return _jsonrpc_result(id_, task_result)
        text, is_err = handle_call(name, arguments)
    text = secret_scan.apply_output_filter(
        text,
        mode=bm_config.REDACT_MODE,
        include_entropy=bm_config.REDACT_ENTROPY,
        pii_types=bm_config.REDACT_PII_TYPES,
    )
    return _jsonrpc_result(id_, _tool_call_result(name, text, is_err, mode))


def _dispatch_validated(method, params, id_, is_notification, mode=bm_config.PROTOCOL_MODE_LEGACY):
    """Dispatch a validated request envelope to the appropriate handler."""
    result = _dispatch_protocol(method, params, id_, is_notification, mode)
    if result != "NOT_MATCHED":
        return result
    if method == "subscriptions/listen":
        return _dispatch_listen(params, id_, is_notification, mode)
    if method == "notifications/cancelled" and is_notification:
        request_id = params.get("requestId")
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            with _LISTEN_LOCK:
                _LISTEN_SUBSCRIPTIONS.pop(request_id, None)
        return None
    if method == "tools/list":
        return _dispatch_tools_list(params, id_, is_notification, mode)
    if method == "tools/call":
        if is_notification:
            return None
        return _dispatch_tools_call(params, id_, mode)
    if is_notification:
        return None
    return _jsonrpc_error(-32601, "Method not found", id_)


_SEND_LOCK = threading.Lock()


def send(msg):
    # Task threads send notifications while the main loop sends responses; one
    # lock keeps each JSON line whole on the shared stdout.
    line = json.dumps(msg) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


# Which transport is serving this process, set by _serve_mcp()/_serve_http()
# -- None until one of them starts (e.g. under test, or during import).
# Governs whether tools.listChanged is advertised truthfully: stdio can push
# a notification at any time via send(); HTTP's do_POST is one request, one
# response, with no server-initiated channel -- advertising listChanged
# there would promise a notification that can never arrive.
_TRANSPORT = None


def _serve_mcp():
    global _TRANSPORT
    _TRANSPORT = "stdio"
    bm_config.log(
        f"starting v{__version__} (profile={bm_config.PROFILE}, table={bm_config.TABLE}, bzrk={bm_config.BZRK_BIN})"
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            bm_config.log(f"bad json from client ({type(e).__name__})")
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = dispatch(req)
        except Exception as e:  # pragma: no cover - defense in depth
            bm_config.log(f"dispatch crashed: {type(e).__name__}")
            if isinstance(req, dict) and "id" in req and _valid_mcp_id(req["id"]):
                resp = _jsonrpc_error(-32603, "Internal error", req["id"])
            else:
                continue
        if resp is not None:
            send(resp)
    bm_config.log("stdin closed")


def _http_error(handler, status, message):
    body = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


_REFUSED_BODY_DRAIN_TIMEOUT = 2.0


def _discard_request_body(handler, cap):
    """Read and drop a refused request's body, up to `cap` bytes, before
    replying. Closing a socket with unread data makes Windows send a TCP reset,
    so the client could see "connection aborted" instead of the error status.
    The request is not yet authorised, so the drain stops at an absolute
    deadline: a client that trickles bytes cannot hold the handler thread."""
    try:
        remaining = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return
    if not 0 < remaining <= cap:
        return
    sock = handler.connection
    previous = sock.gettimeout()
    deadline = time.monotonic() + _REFUSED_BODY_DRAIN_TIMEOUT
    try:
        while remaining > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            chunk = handler.rfile.read1(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            sock.settimeout(previous)


def _http_refuse(handler, config, status, message):
    """Send an error for a request whose body was never read."""
    _discard_request_body(handler, config["max_request_bytes"])
    _http_error(handler, status, message)


def _make_http_handler(config):
    class BerserkMcpHttpHandler(BaseHTTPRequestHandler):
        server_version = "berserk-mcp"
        sys_version = ""

        def log_message(self, fmt, *args):
            bm_config.log("http: " + bm_config._sanitize_log_line(fmt % args))

        def do_GET(self):
            if self.path != "/healthz":
                _http_error(self, 404, "not found")
                return
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            _http_error(self, 405, "method not allowed")

        def do_POST(self):
            if self.path != "/mcp":
                _http_refuse(self, config, 404, "not found")
                return
            ok, status, message = bm_httpconfig._http_request_allowed(self, config)
            if not ok:
                _http_refuse(self, config, status, message)
                return
            ctype = self.headers.get("Content-Type", "")
            if "application/json" not in ctype.lower():
                _http_refuse(self, config, 415, "content-type must be application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                _http_error(self, 411, "content-length required")
                return
            if length < 0 or length > config["max_request_bytes"]:
                _http_error(self, 413, "request too large")
                return
            if not config["semaphore"].acquire(blocking=False):
                _http_refuse(self, config, 429, "too many concurrent requests")
                return
            try:
                raw = self.rfile.read(length)
                try:
                    req = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                else:
                    resp = dispatch(req)
                    if resp is None:
                        self.send_response(204)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                body = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            finally:
                config["semaphore"].release()

    return BerserkMcpHttpHandler


def _serve_http():
    global _TRANSPORT
    _TRANSPORT = "http"
    config = bm_httpconfig._build_http_config()
    if not config["enabled"]:
        raise bm_httpconfig.HttpConfigError("HTTP transport is disabled; set BERSERK_MCP_HTTP_ENABLE=1")
    handler = _make_http_handler(config)
    bm_config.log(f"http starting v{__version__} on {config['host']}:{config['port']}")
    server = ThreadingHTTPServer((config["host"], config["port"]), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


# ---------- --doctor / self_check preflight ----------
# Fleet/air-gapped deployments can't iterate interactively (see F-008's
# fail-fast comment above, extended here from one env var to the whole
# config surface): everything from a missing `bzrk` binary to a wrong
# BZRK_PROFILE to an empty database fails late and opaquely, addressed to
# an agent rather than an operator. These checks give one ordered,
# pass/fail/skip readiness report usable as a fleet probe, a container
# healthcheck, or something an agent hitting repeated errors can call
# itself to tell "wired wrong" apart from "genuinely nothing to report".


def main():
    import argparse

    cli = argparse.ArgumentParser(
        prog="berserk-mcp",
        description="Berserk MCP observability server",
        add_help=True,
    )
    cli.add_argument("--worker", action="store_true", help="run one headless discovery pass (for cron)")
    cli.add_argument("--agent-report", action="store_true", help="run Claude Code agent analytics report")
    cli.add_argument(
        "--agent-report-mode",
        choices=("operational", "daily", "weekly"),
        default="operational",
        help="agent report depth",
    )
    cli.add_argument("--agent-report-json", action="store_true", help="emit a machine-readable agent report envelope")
    cli.add_argument(
        "--canary-run",
        action="store_true",
        help="run the model canary for BERSERK_MCP_CANARY_MODELS and ingest results",
    )
    cli.add_argument(
        "--drift-report",
        action="store_true",
        help="evaluate stored canary history; exit non-zero if any model is degrading",
    )
    cli.add_argument("--auto-queue", action="store_true", help="(worker) queue newly detected sources")
    cli.add_argument("--max-jobs", type=int, default=3, help="(worker) max discovery jobs to drain")
    cli.add_argument("--check-drift", action="store_true", help="(worker) check known services for schema drift")
    cli.add_argument("--since", default="6h ago", help="(agent-report) time window")
    cli.add_argument(
        "--import-business-data",
        choices=("feature", "effort"),
        help="import governed feature catalog or developer-effort records",
    )
    cli.add_argument("--input", help="input CSV/JSON/NDJSON file for business-data import")
    cli.add_argument(
        "--input-format", choices=("csv", "json", "ndjson", "jsonl"), help="override business-data input format"
    )
    cli.add_argument("--export-bi", action="store_true", help="export management-ready AI FinOps datasets")
    cli.add_argument("--output", help="absolute BI export directory")
    cli.add_argument("--export-format", choices=("csv", "ndjson"), default="csv", help="BI export format")
    cli.add_argument(
        "--generate-dashboard",
        choices=("portfolio", "project", "feature", "agent_efficiency", "data_quality"),
        help="generate a Claude Code dashboard snapshot",
    )
    cli.add_argument("--identifier", help="project/feature identifier for dashboard generation")
    cli.add_argument("--dashboard-format", choices=("markdown", "html"), default="markdown")
    cli.add_argument("--set-hermes-url", metavar="URL", help="persist the Hermes LLM endpoint and exit")
    cli.add_argument(
        "--http", action="store_true", help="serve HTTP transport instead of stdio; requires BERSERK_MCP_HTTP_ENABLE=1"
    )
    cli.add_argument(
        "--doctor", action="store_true", help="run preflight readiness checks and exit (0 pass / 1 degraded / 2 broken)"
    )
    cli.add_argument("--json", action="store_true", help="(--doctor) emit the report as JSON instead of a table")
    cli.add_argument(
        "--approve-generated",
        metavar="NAME",
        help="approve a generated saved query so the small tier can use it; prints what was approved",
    )
    ns = cli.parse_args()
    admin_exit = bm_doctor._run_admin_command(ns)
    if admin_exit is not None:
        sys.exit(admin_exit)
    if ns.import_business_data:
        if not ns.input:
            cli.error("--import-business-data requires --input")
        try:
            result = ai_finops.import_business_data(
                ns.import_business_data,
                ns.input,
                fmt=ns.input_format,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"business-data import failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.export_bi:
        if not ns.output:
            cli.error("--export-bi requires --output")
        if not bm_runner.valid_since(ns.since):
            cli.error("--export-bi received an invalid --since value")
        try:
            manifest = ai_finops.export_bi(ns.since, ns.output, fmt=ns.export_format)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"BI export failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.generate_dashboard:
        if not bm_runner.valid_since(ns.since):
            cli.error("--generate-dashboard received an invalid --since value")
        text, is_error = ai_finops.generate_dashboard(
            dashboard=ns.generate_dashboard,
            identifier=ns.identifier or "",
            since=ns.since,
            fmt=ns.dashboard_format,
        )
        print(text, file=sys.stderr if is_error else sys.stdout)
        sys.exit(2 if is_error else 0)
    if ns.set_hermes_url:
        try:
            path = parser_factory.save_hermes_url(ns.set_hermes_url)
            print(
                f"Saved Hermes URL to {path} (0600). It overrides the "
                f"localhost default; BERSERK_LLM_HERMES_URL still takes priority."
            )
            sys.exit(0)
        except Exception as e:
            print(f"failed to save Hermes URL: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.worker:
        sys.exit(
            run_worker_pass(
                auto_queue=ns.auto_queue,
                max_jobs=max(1, min(ns.max_jobs, 5)),
                check_drift=ns.check_drift,
                apply_jitter=True,
            )
        )
    if ns.agent_report:
        sys.exit(
            run_agent_report(
                since=ns.since,
                mode=ns.agent_report_mode,
                output_json=ns.agent_report_json,
            )
        )
    if ns.canary_run:
        models = [m.strip() for m in os.environ.get("BERSERK_MCP_CANARY_MODELS", "").split(",") if m.strip()]
        if not models:
            print("BERSERK_MCP_CANARY_MODELS is unset; nothing to do.")
            sys.exit(0)
        repeats = int(os.environ.get("BERSERK_MCP_CANARY_REPEATS", "3"))
        cases_path = os.environ.get(
            "BERSERK_MCP_CANARY_CASES", str(bm_config.REPO_ROOT / "evals" / "canary_cases.jsonl")
        )
        sys.exit(run_canary_pass(models, cases_path, repeats))
    if ns.drift_report:
        sys.exit(run_drift_report())
    if ns.http or bm_config.HTTP_ENABLE:
        try:
            _serve_http()
        except bm_httpconfig.HttpConfigError as e:
            print(f"HTTP configuration error: {e}", file=sys.stderr)
            sys.exit(2)
    _serve_mcp()


def _post_discord_alert(text):
    """POST a text alert to the local Discord bridge (see
    DISCORD_ALERT_URL/_SECRET above). No-ops silently if the secret isn't
    configured -- this is an opt-in feature for the --worker cron path,
    never a requirement. Never raises: a failed or unconfigured alert must
    never affect the worker pass's own exit code or job outcomes.

    Returns True on a confirmed post, False otherwise (unconfigured,
    validation failure, network error, or a non-2xx bridge response).
    """
    if not bm_config.DISCORD_ALERT_SECRET:
        return False
    text = str(text or "").strip()
    if not text:
        return False
    # Alerts are an egress boundary, never a raw debugging surface. Force the
    # strongest deterministic secret/PII policy even when MCP output is in an
    # explicitly weaker flag/off mode, and do so before the transport cap.
    text = secret_scan.apply_output_filter(
        text,
        mode="redact",
        include_entropy=False,
        pii_types=secret_scan.ALL_PII_TYPES,
    )
    try:
        _http.validate_http_url(bm_config.DISCORD_ALERT_URL, label="discord alert endpoint")
    except _http.UrlPolicyError as e:
        bm_config.log(f"discord alert: endpoint rejected: {e}")
        return False
    payload = json.dumps({"text": text[: bm_config.DISCORD_ALERT_MAX_CHARS]}).encode("utf-8")
    try:
        status = _http.post_bytes_status(
            bm_config.DISCORD_ALERT_URL,
            {
                "Content-Type": "application/json",
                "X-Auth-Token": bm_config.DISCORD_ALERT_SECRET,
            },
            payload,
            timeout=10,
            label="discord alert endpoint",
        )
        return 200 <= status < 300
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        bm_config.log(f"discord alert: bridge returned HTTP {code}")
        return False
    except Exception as e:
        bm_config.log(f"discord alert failed: {type(e).__name__}")
        return False


_AMENDMENT_EMOJI = {"generated": "\U0001f916", "updated": "✏️", "created": "✨"}


def _drain_amendments_changelog():
    """Read amendments_log.json, format a Discord changelog line per entry
    (emoji keyed by action -- generated/updated/created), and clear the
    log ONLY if the alert bridge confirms the post. If Discord isn't
    configured, or the post fails, the log is left intact so the next
    drain run picks up the same entries rather than losing the audit
    trail (the log is already capped at 1000 entries elsewhere, so
    leaving it undrained indefinitely is bounded, not unbounded growth).

    Returns the formatted changelog text, or "" if there was nothing to
    report.
    """
    amendments_path = Path(bm_config.LEARNED_PATH).parent / "amendments_log.json"
    with bm_config._FileLock(amendments_path):
        amendments = bm_config.load_json_list(amendments_path)
        if not amendments:
            return ""
        lines = ["**Query changelog:**"]
        for entry in amendments:
            emoji = _AMENDMENT_EMOJI.get(entry.get("action"), "•")
            name = entry.get("name", "?")
            desc = entry.get("description", "")
            lines.append(f"{emoji} `{name}` — {desc}")
        text = "\n".join(lines)
        if _post_discord_alert(text):
            bm_config.save_json_list(amendments_path, [])
        return text


def run_worker_pass(auto_queue=False, max_jobs=3, check_drift=False, apply_jitter=False):
    """One headless pass for cron/systemd: detect new sources, optionally
    queue them, then drain up to max_jobs pending discovery jobs. Prints a
    summary to stdout, and -- if BERSERK_DISCORD_ALERT_SECRET is
    configured -- posts a job summary and a query changelog to the
    Discord alert bridge. Returns an exit code: 1 if any drained job ended
    needs_human, else 0. No loop, no daemon -- the caller (cron) owns the
    schedule.
    """
    if apply_jitter and bm_config.WORKER_JITTER_SECONDS > 0:
        delay = random.uniform(0, bm_config.WORKER_JITTER_SECONDS)
        bm_config.log(f"worker startup jitter: sleeping {delay:.1f}s (max {bm_config.WORKER_JITTER_SECONDS:g}s)")
        time.sleep(delay)

    detect_summary = parser_factory.detect_new_sources(
        since="24h ago",
        auto_queue=auto_queue,
        check_drift=check_drift,
        load_json_list=bm_config.load_json_list,
        save_json_list=bm_config.save_json_list,
        discovery_queue_path=bm_config.DISCOVERY_QUEUE_PATH,
        active_role=bm_config.ACTIVE_ROLE,
    )
    print(detect_summary)

    outcomes, any_needs_human = bm_learning._drain_pending_jobs(max_jobs)
    summary_lines = [detect_summary]
    if outcomes is None:
        print("No pending discovery jobs.")
    else:
        for line in outcomes:
            print(line)
        summary_lines.extend(outcomes)

    # Only alert when there's something noteworthy -- a bare "No new
    # sources." with nothing drained would be daily noise for an operator
    # who wired up the Discord bridge.
    if outcomes or not detect_summary.startswith("No new sources"):
        _post_discord_alert("\n".join(summary_lines))

    _drain_amendments_changelog()

    return 1 if any_needs_human else 0


def run_agent_report(since="6h ago", mode="operational", output_json=False):
    """One headless pass for cron/systemd: run Claude Code loop and
    model-fit checks, print the report, and return non-zero when an alertable
    condition is present.
    """
    if not bm_runner.valid_since(since):
        print(f"invalid --since value: {since!r}", file=sys.stderr)
        return 2
    if mode not in {"operational", "daily", "weekly"}:
        print(f"invalid agent report mode: {mode!r}", file=sys.stderr)
        return 2
    effective_since = since
    if since == "6h ago" and mode == "daily":
        effective_since = "24h ago"
    elif since == "6h ago" and mode == "weekly":
        effective_since = "7d ago"
    text, should_alert = agent_analytics.agent_report(effective_since)
    spend_text = ""
    spend_error = False
    if mode in {"daily", "weekly"}:
        spend_text, spend_error = ai_finops.spend_overview(
            effective_since,
            group_by="project",
            limit=20,
        )
    if output_json:
        print(
            json.dumps(
                {
                    "schema_version": ai_finops.SCHEMA_VERSION,
                    "mode": mode,
                    "since": effective_since,
                    "operational_report": text,
                    "spend_report": spend_text,
                    "alert": bool(should_alert),
                    "spend_error": bool(spend_error),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(text)
        if spend_text:
            print("\n" + spend_text)
    return 1 if should_alert or spend_error else 0


def run_drift_report(since="30d ago"):
    """Evaluate stored canary history and return an exit code: 2 if the
    query itself failed, 1 if any model is degrading/step-change, else 0.
    A standalone function, not inline in main(), for the same reason as
    run_canary_pass -- testable in isolation, and every path already
    returns explicitly so there is nothing to accidentally fall through.

    group_by_model() raising BzrkResultParseError (an older bzrk build's
    non-JSON output) must be caught and reported as a failure here, not
    left to propagate as an uncaught exception or -- the actual bug this
    fixes -- silently treated as "no data", which made this function
    print "All models stable" and return 0 on a totally broken read path
    (found by Codex backtest, 2026-09-02)."""
    import model_drift

    out, err = bm_runner.bzrk_search_json(model_drift.series_kql(None), since)
    if err:
        print(f"Drift report failed: {out}", file=sys.stderr)
        return 2
    if not out or out.strip() == "(no rows)":
        print("No canary results found.")
        return 0

    try:
        grouped = model_drift.group_by_model(out)
    except model_drift.BzrkResultParseError as exc:
        print(f"Drift report failed: could not read canary results: {exc}", file=sys.stderr)
        return 2

    degraded_models = []
    for model_name, series in grouped.items():
        verdict = model_drift.classify(series)
        if verdict["verdict"] in ("degrading", "step-change"):
            degraded_models.append((model_name, verdict))

    if degraded_models:
        lines = ["Models with degraded or changed behavior:"]
        for model_name, verdict in degraded_models:
            fenced = bm_fencing._fence_untrusted(model_name, inline=True)
            lines.append(f"  {fenced}: {verdict['verdict']} ({verdict['confidence']}) — {verdict['reason']}")
        text = "\n".join(lines)
        print(text, file=sys.stderr)
        _post_discord_alert(text)
        return 1
    print("All models stable (tool-routing quality).")
    return 0


def run_canary_pass(models, cases_path, repeats):
    """One headless canary pass for cron: score each configured model
    against the frozen case set, attach fingerprints for successful runs,
    and persist every result via OTLP. Returns an exit code: 1 if any
    result failed to persist (even a successfully-scored one -- see
    canary.emit()'s return value), else 0.

    A standalone function returning an exit code, not inline code in
    main(), matching run_worker_pass/run_agent_report's own convention --
    deliberately, not incidentally. An earlier inline version had no
    return/exit after its loop, so execution fell through into whatever
    CLI branch happened to follow it (the HTTP/MCP server start) on every
    real invocation, turning a one-shot cron command into a hang (found by
    Codex review, 2026-09-02). Structuring this as sys.exit(run_canary_pass(...))
    at the call site makes that class of bug structurally impossible here:
    there is no code path through this function that does not return.
    """
    sys.path.insert(0, str(bm_config.REPO_ROOT / "evals"))
    import canary

    any_emit_failed = False
    for model in models:
        record = canary.run_canary(model, cases_path=cases_path, repeats=repeats)
        if record.get("eval.status") == "ok":
            bm_doctor._attach_fingerprints(record, model)
        # canary.emit() -> ai_finops.emit_otlp_records() returns False when
        # BERSERK_MCP_OTLP_LOGS_ENDPOINT is unset or the POST fails --
        # previously discarded, so a run could spend money, score a model,
        # print "ok", and store nothing, with cron seeing exit 0 throughout
        # (found by Codex review, 2026-09-02).
        emitted = canary.emit([record], int(time.time() * 1_000_000_000))
        if not emitted:
            any_emit_failed = True
        print(f"{model}: {record.get('eval.status')} tool_accuracy={record.get('eval.tool_accuracy')} stored={emitted}")
    return 1 if any_emit_failed else 0


_facade.install(sys.modules[__name__])
