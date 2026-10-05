"""Tool-call dispatch with fleet budget, cache and cooldown.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import learned as bm_learned
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
from berserk_mcp.handlers import diagnostics as bm_diagnostics
from berserk_mcp.handlers import learning as bm_learning
from berserk_mcp.handlers import search as bm_search
from berserk_mcp.handlers import tail as bm_tail
import json
import parser_factory
import time


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
    result = bm_diagnostics._handle_parser_factory(name, arguments)
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

    result = bm_search._handle_validated(name, arguments)
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

    fleet_token = bm_config._set_fleet_context(
        {
            "tool": str(name),
            "budget": bm_config.TOOL_BUDGET_SECONDS if bm_config.TOOL_BUDGET_SECONDS > 0 else None,
            "budget_multiplier": bm_tools._tool_budget_multiplier(name),
        }
    )
    try:
        text, is_err = _handle_call_uncached(name, args)
    finally:
        bm_config._restore_fleet_context(fleet_token)

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
