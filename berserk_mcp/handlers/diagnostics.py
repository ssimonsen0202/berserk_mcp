"""Handlers for diagnostics, model drift and the parser tools.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import learned as bm_learned
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp.handlers import learning as bm_learning
import _kql_boundary
import investigation
import json
import kql_validation
import parser_factory
import time


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
