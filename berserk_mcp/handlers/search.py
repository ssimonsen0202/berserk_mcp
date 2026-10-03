"""Handlers for query, search, analytics and FinOps tools.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import doctor as bm_doctor
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
import agent_analytics
import ai_finops
import json
import quota_status
import tool_discovery


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
