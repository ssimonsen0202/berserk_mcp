"""Tool definitions, metadata and the text the model reads about them.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import queries as bm_queries
import _http
import agent_analytics
import hashlib
import json
import kql_validation
import os
import re
import secret_scan
import tool_catalog
import tool_discovery


# ---------- tool definitions ----------
# The advertised `since` pattern. JSON Schema `pattern` has no portable
# case-insensitive flag, and _SINCE_RE matches with re.IGNORECASE, so the
# schema must accept any letter case, or a client doing grammar-constrained
# decoding rejects values the server accepts ('NOW', '2 HOURS AGO').
#
# It accepts any unit of up to the longest real unit's length rather than
# spelling each unit out letter by letter: the spelled-out form was ~370
# bytes repeated in every tool, about 30% of each lane's tools/list (review
# 2026-09-26, P2). An unknown unit ('5 xyz') passes the schema and is then
# rejected by valid_since with a message naming the accepted forms.
_SINCE_MAX_UNIT_CHARS = max(len(unit) for unit in bm_config._SINCE_HOURS_FACTORS)


_SINCE_SCHEMA_PATTERN = rf"^([Nn][Oo][Ww]|\d+\s*[A-Za-z]{{1,{_SINCE_MAX_UNIT_CHARS}}}(\s+[Aa][Gg][Oo])?)$"


def _since():
    return {
        "since": {
            "type": "string",
            "description": "Time window e.g. '15m ago', '1h ago', '2d ago'.",
            "pattern": _SINCE_SCHEMA_PATTERN,
            "examples": ["15m ago", "1h ago", "6h ago", "1d ago", "7d ago", "now"],
        }
    }


def _agent_prop():
    return {
        "agent": {
            "type": "string",
            "description": "Which ingesting agent's activity to query. Defaults to Claude Code.",
            "enum": sorted(bm_queries._AGENT_SERVICE_NAMES.keys()),
            "default": "claude-code",
        }
    }


_REPORT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "generated_at": {"type": "string"},
        "source_window": {"type": "string"},
    },
    "required": ["schema_version"],
    "additionalProperties": True,
}


_STRUCTURED_OUTPUT_TOOLS = frozenset(
    {
        "claude_spend_overview",
        "claude_feature_cost",
        "claude_project_economics",
        "claude_efficiency_insights",
        "claude_harness_recommendations",
        "claude_optimization_impact",
        "claude_management_report",
        "claude_generate_dashboard",
    }
)


_TASK_ELIGIBLE_TOOLS = frozenset(
    {
        "generate_parser",
        "run_discovery_worker",
        "claude_generate_dashboard",
    }
)


def _with_output_schema(tool):
    enriched = dict(tool)
    if tool.get("name") in _STRUCTURED_OUTPUT_TOOLS:
        enriched["outputSchema"] = _REPORT_OUTPUT_SCHEMA
    if tool.get("name") in _TASK_ELIGIBLE_TOOLS:
        schema = dict(enriched["inputSchema"])
        properties = dict(schema.get("properties", {}))
        properties["as_task"] = {
            "type": "boolean",
            "description": "Modern MCP only: return a task immediately and run the tool asynchronously.",
        }
        schema["properties"] = properties
        enriched["inputSchema"] = schema
    return enriched


def _canonloom_call(path: str, method: str = "GET", body=None):
    """Call the canonloom HTTP API. Returns (result_text, is_error)."""
    server_url = os.environ.get("CANONLOOM_SERVER_URL", "").rstrip("/")
    if not server_url:
        return (
            "CANONLOOM_SERVER_URL is not set. Start canonloom-server and set the URL. "
            "Example: export CANONLOOM_SERVER_URL=http://localhost:8080"
        ), True
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("CANONLOOM_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        url = server_url + path
        import json as _json

        if method == "GET":
            data, err = _http.http_get_json(url, headers, timeout=120, allow_plaintext_remote=False)
        else:
            data, err = _http.http_post_json(url, headers, body or {}, timeout=300, allow_plaintext_remote=False)
        if err:
            return f"CanonLoom API error: {err}", True
        return _json.dumps(data, separators=(",", ":")), False
    except Exception as exc:
        return f"CanonLoom API error: {exc}", True


# Each entry: name -> (kql, default_since). Tools requiring user input or extra
# calls (logs, search, cc_search, schema) are handled explicitly in handle_call.
SIMPLE = {
    "list_containers": (bm_queries.Q_CONTAINERS, "15m ago"),
    "top_cpu": (bm_queries.Q_CPU, "15m ago"),
    "top_memory": (bm_queries.Q_MEM, "15m ago"),
    "errors_by_service": (bm_queries.Q_ERRORS, "1h ago"),
    "list_services": (bm_queries.Q_SERVICES, "1h ago"),
    "list_hosts": (bm_queries.Q_HOSTS, "1h ago"),
    "host_cpu": (bm_queries.Q_HOST_CPU, "30m ago"),
    "host_memory": (bm_queries.Q_HOST_MEM, "30m ago"),
    "container_hosts": (bm_queries.Q_CONTAINER_HOSTS, "1h ago"),
    "list_metrics": (bm_queries.Q_METRICS, "1h ago"),
    "bzrk_query_perf": (bm_queries.Q_QUERY_PERF, "1h ago"),
    "sre_error_rate": (bm_queries.Q_SRE_ERROR_RATE, "1h ago"),
    "sre_host_headroom": (bm_queries.Q_SRE_HOST_HEADROOM, "30m ago"),
    "sre_ingest_health": (bm_queries.Q_SRE_INGEST_HEALTH, "1h ago"),
    "sre_top_error_messages": (bm_queries.Q_SRE_TOP_ERRORS, "1h ago"),
    "soc_high_severity_logs": (bm_queries.Q_SOC_HIGH_SEV, "1h ago"),
    "soc_log_spike": (bm_queries.Q_SOC_LOG_SPIKE, "1h ago"),
    "soc_repeated_errors": (bm_queries.Q_SOC_REPEATED_ERRORS, "6h ago"),
    "trace_find_slow": (bm_queries.Q_TRACE_FIND_SLOW, "1h ago"),
    "trace_find_errors": (bm_queries.Q_TRACE_FIND_ERRORS, "1h ago"),
}


# SIMPLE tools whose fixed query derives output from `body`, even though
# it's already substring-capped at the KQL level (200-500 chars). Confirmed
# empirically that the runtime table renderer can still clip those capped
# values further depending on the calling process's terminal-width detection
# -- unrelated to the KQL-level cap. sre_top_error_messages and
# soc_repeated_errors derive their `example` column from
# substring(tostring(body), ...) rather than projecting a literal `body`
# column, which is easy to miss on a quick scan. The rest of SIMPLE is
# aggregation-only and never carries body-derived content, so it stays on
# the more compact table mode.
_SIMPLE_JSON_TOOLS = {
    "claude_errors",
    "soc_high_severity_logs",
    "sre_top_error_messages",
    "soc_repeated_errors",
}


# Issue #42: these four take an optional `agent` argument (default
# "claude-code"), so their query is resolved lazily via a callable rather
# than the fixed string every other SIMPLE tool uses.
_AGENT_AWARE_SIMPLE = {
    "claude_recent": (bm_queries.q_cc_recent, "1h ago"),
    "claude_sessions": (bm_queries.q_cc_sessions, "6h ago"),
    "claude_tools": (bm_queries.q_cc_tools, "6h ago"),
    "claude_errors": (bm_queries.q_cc_errors, "6h ago"),
}


_DEFAULT_EMPTY_NEXT_STEP = "Try a wider window with since='24h ago'."


_EMPTY_NEXT_STEP = {
    "list_containers": "Widen with since='1h ago', or check list_hosts for hosts without containers.",
    "top_cpu": "For whole-machine CPU use host_cpu; top_cpu is per-container. If both are empty, widen with since='1h ago'.",
    "top_memory": "For whole-machine memory use host_memory; top_memory is per-container. If both are empty, widen with since='1h ago'.",
    "errors_by_service": "Widen with since='24h ago', or confirm the source is reporting with list_services.",
    "list_services": "Widen with since='6h ago', or check list_hosts if services are attached to hosts.",
    "list_hosts": "Widen with since='6h ago', or verify ingest health with sre_ingest_health.",
    "host_cpu": "For per-container CPU use top_cpu; host_cpu is per-host. If both are empty, widen with since='1h ago'.",
    "host_memory": "For per-container memory use top_memory; host_memory is per-host. If both are empty, widen with since='1h ago'.",
    "container_hosts": "Widen with since='6h ago', or check list_containers for active containers.",
    "list_metrics": "Widen with since='6h ago', or verify the source is ingesting with sre_ingest_health.",
    "bzrk_query_perf": "Widen with since='6h ago'; no query traffic means no perf data to show.",
    "sre_error_rate": "Widen with since='6h ago', or check errors_by_service for breakdown by service.",
    "sre_host_headroom": "Widen with since='2h ago'; no headroom data means no host metrics arrived.",
    "sre_ingest_health": "Widen with since='6h ago'; check list_hosts to see if any hosts are reporting.",
    "sre_top_error_messages": "Widen with since='6h ago', or check errors_by_service for aggregate counts.",
    "soc_high_severity_logs": "Widen with since='6h ago', or check soc_log_spike for volume anomalies.",
    "soc_log_spike": "Widen with since='6h ago'; no spike means no volume anomaly in this window.",
    "soc_repeated_errors": "Widen with since='24h ago', or check errors_by_service for recent counts.",
    "claude_recent": "Widen with since='6h ago', or check claude_sessions for session-level activity.",
    "claude_sessions": "Widen with since='24h ago', or check claude_recent for recent events.",
    "claude_tools": "Widen with since='24h ago'; no tool data means no Claude Code sessions in this window.",
    "claude_errors": "Widen with since='24h ago', or check claude_recent to see if sessions are active.",
    "trace_find_slow": "Widen with since='6h ago'; no slow traces means all spans finished within threshold.",
    "trace_find_errors": "Widen with since='6h ago', or check errors_by_service for error counts.",
}


def _envelope(tool, since, out, fence_body=False):
    """Wrap SIMPLE tool output with a window/rows header. Never raises.

    fence_body=True (set for _SIMPLE_JSON_TOOLS, issue #11): wrap the raw
    rows in an untrusted-data marker. The header stays outside the fence
    (server-generated metadata); the "No rows" sentence is also unfenced
    (interpretive text, not real telemetry)."""
    try:
        if out.strip() == "(no rows)":
            next_step = _EMPTY_NEXT_STEP.get(tool, _DEFAULT_EMPTY_NEXT_STEP)
            if _names_hidden_tool(next_step, _hidden_tool_names()):
                next_step = _DEFAULT_EMPTY_NEXT_STEP
            return f"No rows in window {since}. {next_step}"
        stripped = out.strip()
        if stripped and stripped[0] in "[{":
            try:
                records = agent_analytics._json_records(json.loads(stripped))
                rows = len(records) if records is not None else None
            except Exception:
                rows = None
        else:
            data_lines = [ln for ln in stripped.splitlines() if ln.strip()]
            rows = max(0, len(data_lines) - 1)
        header = f"window={since}"
        if rows is not None:
            header = f"{header}  rows={rows}"
        # Evidence fields (review 2026-09-26): where the rows came from, the
        # redaction policy the MCP boundary (dispatch) applies to this result,
        # when the rows were queried (a cached result keeps its original
        # time), and a stable reference: identical tool, window and delivered
        # rows always give the same ref. The ref hashes the rows as the client
        # receives them (after the same output filter dispatch applies), never
        # the raw rows, so it cannot confirm a guess at a redacted value.
        delivered = secret_scan.apply_output_filter(
            out,
            mode=bm_config.REDACT_MODE,
            include_entropy=bm_config.REDACT_ENTROPY,
            pii_types=bm_config.REDACT_PII_TYPES,
        )
        digest = hashlib.sha256(f"{tool}\n{since}\n{delivered}".encode("utf-8", "replace")).hexdigest()[:12]
        header = f"{header}  source=fixed:{tool}  redaction={bm_config.REDACT_MODE}  at={bm_config.now_iso()}  ref={tool}#{digest}"
        body = bm_fencing._fence_untrusted(out) if fence_body else out
        return f"{header}\n\n{body}"
    except Exception:
        return out


_QUERY_RISK_BUDGET_MULTIPLIERS = {
    "low": 1.0,
    "medium": 1.5,
    "high": 2.0,
}


def _derive_tool_budget_multipliers(simple_queries=None, include_discovery=True):
    """Derive per-tool budgets from the same static validator used by the gate."""
    queries = dict(SIMPLE if simple_queries is None else simple_queries)
    if include_discovery:
        queries["discover_schema"] = (bm_queries.q_discover_fieldstats(), "1h ago")
    multipliers = {}
    for tool_name, (kql, since) in queries.items():
        report = kql_validation.validate_kql_static(
            kql,
            table=bm_config.TABLE,
            since=since,
        )
        multipliers[str(tool_name)] = _QUERY_RISK_BUDGET_MULTIPLIERS.get(
            report.get("risk"),
            1.0,
        )
    return multipliers


TOOL_BUDGET_MULTIPLIERS = _derive_tool_budget_multipliers()


def _tool_budget_multiplier(tool_name):
    """Return the derived multiplier; non-shipped/non-SIMPLE tools stay at 1x."""
    return TOOL_BUDGET_MULTIPLIERS.get(str(tool_name), 1.0)


TOOLS = tool_catalog.build_tools(
    TABLE=bm_config.TABLE,
    MAX_INTERPOLATED_NAME_CHARS=bm_queries.MAX_INTERPOLATED_NAME_CHARS,
    MAX_SEARCH_TERM_CHARS=bm_queries.MAX_SEARCH_TERM_CHARS,
    MAX_TRACE_ID_CHARS=bm_queries.MAX_TRACE_ID_CHARS,
    _FORECAST_METRICS=bm_queries._FORECAST_METRICS,
    _agent_prop=_agent_prop,
    _since=_since,
)


MGMT_TOOLS = tool_catalog.build_mgmt_tools(
    TABLE=bm_config.TABLE,
    MAX_INTERPOLATED_NAME_CHARS=bm_queries.MAX_INTERPOLATED_NAME_CHARS,
    MAX_SEARCH_TERM_CHARS=bm_queries.MAX_SEARCH_TERM_CHARS,
    _since=_since,
)


# ---------- text the model reads must not name tools it cannot call ----------
# A hidden tool answers "unknown tool" (deliberately non-leaking), so a model
# sent to one by a description, next step or instruction has no way to
# recover. Descriptions and next steps are shared across lanes and tiers;
# these helpers drop the parts that name a tool hidden in this process.
_TOOL_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_CODE_SPAN_TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)")


_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+")


_ABBREVIATION_END_RE = re.compile(r"\b(?:e\.g|i\.e|etc|vs)\.$")


def _hidden_tool_names():
    """Names of built-in tools hidden by role or tier in this process."""
    return {t["name"] for t in TOOLS + MGMT_TOOLS if not bm_config.tool_visible(t)}


def _tool_references(text):
    """Tool names `text` refers to. A snake_case name counts anywhere; a
    one-word name (`search`, `schema`) only at the start of a code span, since
    as a bare word it is ordinary English ("full-text search")."""
    text = str(text)
    bare = {token for token in _TOOL_TOKEN_RE.findall(text) if "_" in token}
    return bare | set(_CODE_SPAN_TOKEN_RE.findall(text))


def _names_hidden_tool(text, hidden):
    return bool(hidden) and not hidden.isdisjoint(_tool_references(text))


def _without_hidden_tool_sentences(text, hidden):
    """Drop each sentence of `text` that names a hidden tool. Text naming
    none is returned unchanged, so the deep tier and `all` are unaffected."""
    if not _names_hidden_tool(text, hidden):
        return text
    sentences = []
    for part in _SENTENCE_BREAK_RE.split(text):
        if sentences and _ABBREVIATION_END_RE.search(sentences[-1]):
            sentences[-1] += " " + part
        else:
            sentences.append(part)
    return " ".join(s for s in sentences if not _names_hidden_tool(s, hidden))


# An operator primer (BERSERK_MCP_PRIMERS_DIR) has no deep-tier markers unless
# its author added them; say so loudly rather than ship guidance to hidden tools.
# Stricter than the description filter: a bare "search" counts too, since a
# false warning costs nothing and a missed one ships guidance to a hidden tool.
_instruction_hidden_refs = sorted(
    (_tool_references(bm_config.INSTRUCTIONS) | set(_TOOL_TOKEN_RE.findall(bm_config.INSTRUCTIONS)))
    & _hidden_tool_names()
)


if _instruction_hidden_refs:
    bm_config.log(
        f"warning: instructions for role={bm_config.ACTIVE_ROLE} tier={bm_config.ACTIVE_TIER_RESOLVED} name hidden tools: "
        f"{', '.join(_instruction_hidden_refs)}. End those primer lines with '{bm_config._DEEP_ONLY_MARKER.strip()}'."
    )


# ---------- tool metadata: titles + behavioral annotations (MCP 2025-06-18) ----------
# Annotations are advisory hints that let clients reason about a tool's behavior.
# Every tool here is read-only against Berserk (KQL cannot mutate) EXCEPT save_query
# and request_discovery, which write to local stores (learned-query store / discovery
# queue) rather than any external system, so both carry openWorldHint=false.
_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


_READ_LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


_WRITE_LOCAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


# Parser-factory tools query Berserk AND (generate_parser/run_discovery_worker)
# call external LLM APIs, and are not idempotent (an LLM may generate different
# queries across runs) -- openWorldHint=true distinguishes them from the
# local-store-only _WRITE_LOCAL tools above.
_WRITE_EXTERNAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}


_ANNOTATIONS = {
    "find_tool": _READ_LOCAL,
    "save_query": _WRITE_LOCAL,
    "list_saved": _READ_LOCAL,
    "request_discovery": _WRITE_LOCAL,
    "discovery_status": _READ_LOCAL,
    "detect_new_sources": _WRITE_EXTERNAL,
    "generate_parser": _WRITE_EXTERNAL,
    "run_discovery_worker": _WRITE_EXTERNAL,
    "review_generated": _READ_LOCAL,
    "claude_record_recommendation_decision": _WRITE_EXTERNAL,
    "claude_generate_dashboard": _WRITE_LOCAL,
}


# Issue #14: just-in-time tool discovery. Off by default for one release
# (kill switch) -- when on, tools/list returns only the anchor set below
# plus find_tool, and callers reach everything else via find_tool's search
# instead of the full ~3,500-token-per-lane schema being resident always.
BERSERK_MCP_DISCOVERY = os.environ.get("BERSERK_MCP_DISCOVERY", "").strip().lower() in {"1", "true", "yes", "on"}


# Stated rule, not a hand-picked list (see issue #14): every tool with no
# role restriction, no required parameters, and orientation/discovery
# intent -- these answer "what exists" rather than "what is the value of
# X", which is what a caller needs before find_tool is even useful.
# Provisional: there's no real call-frequency data to base this on yet
# (that needs the audit ledger, issue #17) -- revisit once it exists.
_ANCHOR_TOOL_NAMES = frozenset(
    {
        "find_tool",
        "schema",
        "discover_schema",
        "list_saved",
        "list_services",
        "list_hosts",
        "discovery_status",
        "self_check",
    }
)


_DISCOVERY_INDEX = tool_discovery.build_index(TOOLS + MGMT_TOOLS)


TITLES = tool_catalog.TITLES


def annotations_for(name):
    """Read-only by default; only the two store-management tools differ."""
    if isinstance(name, str) and name.startswith("saved__"):
        # A projected saved query is a deterministic replay of a verified
        # local query, same as list_saved/run_saved -- not an open-world
        # call, which the bare _READ default (openWorldHint=True) would
        # otherwise imply.
        return _READ_LOCAL
    return _ANNOTATIONS.get(name, _READ)


def _tool_candidate_view(t):
    """A find_tool search result: name + description + full inputSchema
    inline, so a caller can go straight to calling it without a second
    round trip to look the schema up."""
    return {
        "name": t["name"],
        "description": _without_hidden_tool_sentences(t["description"], _hidden_tool_names()),
        "inputSchema": t["inputSchema"],
    }
