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
  BZRK_BIN                 path/name of the bzrk binary           (default: "bzrk")
  BZRK_PROFILE             bzrk profile to query                  (default: "local")
  BZRK_TIMEOUT             per-query timeout in seconds           (default: "120")
  BERSERK_TABLE            the Berserk table to query             (default: "default")
  BERSERK_MCP_LEARNED_PATH where saved queries persist  (default: per-user config dir)

This is an unofficial, community-maintained integration. It is not affiliated
with or endorsed by the Berserk project.
"""
import sys
import json
import subprocess
import re
import os
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.6.0"

# ---------- configuration (env-overridable) ----------
BZRK_BIN = os.environ.get("BZRK_BIN", "bzrk")
PROFILE = os.environ.get("BZRK_PROFILE", "local")
TABLE = os.environ.get("BERSERK_TABLE", "default")
DEFAULT_TIMEOUT = int(os.environ.get("BZRK_TIMEOUT", "120"))
ACTIVE_ROLE = os.environ.get("BERSERK_MCP_ROLE", "all").strip().lower() or "all"


def _default_learned_path() -> Path:
    """Where to persist learned queries, following platform conventions."""
    env = os.environ.get("BERSERK_MCP_LEARNED_PATH")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "berserk-mcp" / "learned.json"


LEARNED_PATH = _default_learned_path()
DISCOVERY_QUEUE_PATH = _default_learned_path().parent / "discovery_queue.json"
KNOWN_SOURCES_PATH = _default_learned_path().parent / "known_sources.json"
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "berserk-q", "title": "Berserk Query", "version": __version__}

_BASE_INSTRUCTIONS = (
    "Answer observability questions by calling these tools — do not write KQL by hand. "
    "Prefer the most specific tool (e.g. top_cpu, errors_by_service, logs_for_service, "
    "host_cpu) over the generic `search`. Per-host metrics (host_cpu, host_memory) and "
    "per-container metrics (top_cpu, top_memory) are different — pick by what's asked. "
    "Every query tool takes an optional `since` like '15m ago' or '2h ago'. For a "
    "recurring custom question, get it working with `search`, then `save_query` so it "
    "can be re-run deterministically with `run_saved`."
)
_ROLE_PREFIX = {
    "sre": "You are in the SRE lane; focus on reliability, headroom, saturation, error rates, and rollback signals. ",
    "soc": "You are in the SOC lane; focus on anomalies, spikes, first-seen behavior, repeated failures, and incident timelines. ",
    "claude": "You are in the Claude Code lane; focus on Claude session activity, tool errors, and developer workflow traces. ",
    "ops": "You are in the operations lane; focus on service health, hosts, containers, and actionable operator checks. ",
}


def _load_primer(role: str) -> str:
    """Load primers/<role>.md adjacent to this script, or from BERSERK_MCP_PRIMERS_DIR."""
    env_dir = os.environ.get("BERSERK_MCP_PRIMERS_DIR", "")
    primer_dir = Path(env_dir) if env_dir else Path(__file__).parent / "primers"
    if role in {"sre", "soc", "claude", "ops"}:
        f = primer_dir / f"{role}.md"
        try:
            return f.read_text(encoding="utf-8").strip() + "\n\n"
        except OSError:
            pass
    return ""


INSTRUCTIONS = _load_primer(ACTIVE_ROLE) + _ROLE_PREFIX.get(ACTIVE_ROLE, "") + _BASE_INSTRUCTIONS


def log(msg):
    print("[berserk-mcp] " + str(msg), file=sys.stderr, flush=True)


def tool_visible(tool):
    roles = tool.get("roles")
    return not roles or ACTIVE_ROLE == "all" or ACTIVE_ROLE in roles


def item_visible(item):
    roles = item.get("roles")
    return not roles or ACTIVE_ROLE == "all" or ACTIVE_ROLE in roles


def normalize_roles(value):
    if value is None:
        return [ACTIVE_ROLE] if ACTIVE_ROLE not in {"all", ""} else None
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    elif isinstance(value, list):
        parts = [str(p).strip().lower() for p in value if str(p).strip()]
    else:
        parts = [str(value).strip().lower()]
    valid = [r for r in parts if r in {"sre", "soc", "claude", "ops"}]
    return valid or None


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_json_list(path, items):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, path)


# ---------- verified queries (do not edit field names; they are confirmed
# against the live `default` schema — see docs/claude-code.md) ----------
T = TABLE
CC = f"{T} | where resource['service.name'] == 'claude-code'"

Q_CONTAINERS = (
    f"{T} | where isnotnull(metric_name) | where isnotempty(resource['container.name']) "
    f"| summarize samples=count() by container=tostring(resource['container.name']) "
    f"| sort by container asc"
)
Q_CPU = (
    f"{T} | where metric_name == 'container.cpu.utilization' "
    f"| summarize cpu_pct=avg(value) by container=tostring(resource['container.name']) "
    f"| sort by cpu_pct desc"
)
Q_MEM = (
    f"{T} | where metric_name == 'container.memory.usage.total' "
    f"| summarize mb=avg(value)/1048576 by container=tostring(resource['container.name']) "
    f"| sort by mb desc"
)
Q_ERRORS = (
    f"{T} | where isnotnull(body) | where severity_text == 'ERROR' "
    f"| summarize errors=count() by service=tostring(resource['service.name']) "
    f"| sort by errors desc"
)
Q_SERVICES = (
    f"{T} | summarize total=count(), logs=countif(isnotnull(body)), "
    f"metrics=countif(isnotnull(metric_name)) by service=tostring(resource['service.name']) "
    f"| sort by total desc"
)
Q_HOSTS = (
    f"{T} | summarize total=count() by host=tostring(resource['host.name']) "
    f"| sort by total desc"
)
Q_HOST_CPU = (
    f"{T} | where metric_name == 'system.cpu.load_average.1m' "
    f"| summarize load_1m=avg(value) by host=tostring(resource['host.name']) "
    f"| sort by load_1m desc"
)
Q_HOST_MEM = (
    f"{T} | where metric_name == 'system.memory.usage' "
    f"| where tostring(attributes['state']) == 'used' "
    f"| summarize used_gb=avg(value)/1073741824 by host=tostring(resource['host.name']) "
    f"| sort by used_gb desc"
)
Q_CONTAINER_HOSTS = (
    f"{T} | where isnotempty(resource['container.name']) "
    f"| summarize last_seen=max(timestamp) by "
    f"container=tostring(resource['container.name']), host=tostring(resource['host.name']) "
    f"| sort by host asc, container asc"
)
Q_METRICS = (
    f"{T} | where isnotnull(metric_name) "
    f"| summarize samples=count(), last_seen=max(timestamp) by metric_name "
    f"| sort by samples desc"
)
# bzrk.query.execution_duration is a cumulative OTel histogram — value is null.
# otel_histogram_percentile($raw, N) is a native Berserk aggregate that reads the
# internal histogram representation directly; subscript access ($raw['count'] etc.)
# still works for count/sum/max if needed.
Q_QUERY_PERF = (
    f"{T} | where metric_name == 'bzrk.query.execution_duration' "
    f"| summarize p50=otel_histogram_percentile($raw, 50), "
    f"p95=otel_histogram_percentile($raw, 95), "
    f"p99=otel_histogram_percentile($raw, 99)"
)

# --- SRE Tier-A queries (verified aggregates: countif/avg/max/min all confirmed in Berserk) ---
Q_SRE_ERROR_RATE = (
    f"{T} | where isnotnull(body) | where severity_text == 'ERROR' "
    f"| summarize errors=count() by service=tostring(resource['service.name']), minute=bin(timestamp, 1m) "
    f"| sort by minute desc, errors desc | take 120"
)
Q_SRE_HOST_HEADROOM = (
    f"{T} | where metric_name in ('system.cpu.load_average.1m', 'system.memory.usage') "
    f"| summarize samples=count(), avg_value=avg(value) "
    f"by host=tostring(resource['host.name']), metric=tostring(metric_name), state=tostring(attributes['state']) "
    f"| where metric == 'system.cpu.load_average.1m' or (metric == 'system.memory.usage' and state == 'used') "
    f"| sort by host asc, metric asc"
)
Q_SRE_INGEST_HEALTH = (
    f"{T} | where metric_name in ('bzrk.nursery.ingest_lag_seconds', 'bzrk.ingest.data_dropped') "
    f"| summarize samples=count(), avg_value=avg(value), max_value=max(value), last_seen=max(timestamp) "
    f"by host=tostring(resource['host.name']), metric=tostring(metric_name) "
    f"| sort by host asc, metric asc"
)
Q_SRE_TOP_ERRORS = (
    f"{T} | where isnotnull(body) | where severity_text == 'ERROR' "
    f"| summarize hits=count(), last_seen=max(timestamp) "
    f"by service=tostring(resource['service.name']), msg=substring(tostring(body), 0, 160) "
    f"| sort by hits desc | take 40"
)

# --- SOC Tier-A queries ---
Q_SOC_HIGH_SEV = (
    f"{T} | where isnotnull(body) | where severity_text in ('CRITICAL', 'FATAL', 'ERROR') "
    f"| project timestamp, severity_text, service=tostring(resource['service.name']), "
    f"body=substring(tostring(body), 0, 240) "
    f"| sort by timestamp desc | take 60"
)
Q_SOC_LOG_SPIKE = (
    f"{T} | where isnotnull(body) "
    f"| summarize hits=count() by service=tostring(resource['service.name']), minute=bin(timestamp, 1m) "
    f"| sort by hits desc, minute desc | take 60"
)
Q_SOC_NEW_SERVICES = (
    f"{T} | summarize first_seen=min(timestamp), last_seen=max(timestamp), events=count() "
    f"by service=tostring(resource['service.name']) "
    f"| sort by first_seen desc | take 40"
)
Q_SOC_REPEATED_ERRORS = (
    f"{T} | where isnotnull(body) | where severity_text == 'ERROR' "
    f"| summarize hits=count(), last_seen=max(timestamp) by msg=substring(tostring(body), 0, 160) "
    f"| where hits > 5 | sort by hits desc | take 40"
)


def q_sre_service_health(svc: str) -> str:
    return (
        f"{T} | where resource['service.name'] == '{svc}' "
        f"| summarize total=count(), logs=countif(isnotnull(body)), "
        f"metrics=countif(isnotnull(metric_name)), errors=countif(severity_text == 'ERROR'), "
        f"last_seen=max(timestamp)"
    )


def q_soc_timeline(svc: str) -> str:
    return (
        f"{T} | where resource['service.name'] == '{svc}' "
        f"| project timestamp, severity_text, metric_name, body=substring(tostring(body), 0, 200) "
        f"| sort by timestamp desc | take 100"
    )


def q_discover_keys(service=None):
    """Enumerate the keys present in `resource` (optionally for one service) with
    counts — verified-working fallback for buildschema() which bzrk doesn't ship."""
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    return (
        f"{T} | where isnotnull(resource) {filt}"
        f"| project k=bag_keys(resource) | mv-expand k "
        f"| summarize n=count() by key=tostring(k) | sort by n desc"
    )


def q_discover_sample(service=None):
    """Sample real rows (optionally for one service) so a model can read the
    nested resource + attributes values, not just key names."""
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    return (
        f"{T} {filt}| take 6 "
        f"| project resource, attributes, metric_name, severity_text, body"
    )
Q_CC_RECENT = (
    f"{CC} | project ts=timestamp, typ=tostring(attributes['claude.type']), "
    f"role=tostring(attributes['claude.message_role']), "
    f"model=tostring(attributes['claude.message_model']), "
    f"tools=tostring(attributes['claude.tool_names']), "
    f"err=tostring(attributes['claude.error']) | sort by ts desc | take 60"
)
Q_CC_SESSIONS = (
    f"{CC} | summarize events=count(), first=min(timestamp), last=max(timestamp), "
    f"assistant_turns=countif(tostring(attributes['claude.type'])=='assistant'), "
    f"tool_turns=countif(isnotempty(tostring(attributes['claude.tool_names']))), "
    f"errors=countif(tostring(attributes['claude.error'])=='true') "
    f"by session=tostring(attributes['claude.session_id']) | sort by last desc | take 40"
)
Q_CC_TOOLS = (
    f"{CC} | where isnotempty(tostring(attributes['claude.tool_names'])) "
    f"| mv-expand t=split(tostring(attributes['claude.tool_names']), ',') "
    f"| summarize uses=count() by tool=tostring(t) | sort by uses desc | take 40"
)
Q_CC_ERRORS = (
    f"{CC} | where tostring(attributes['claude.error'])=='true' "
    f"| project ts=timestamp, typ=tostring(attributes['claude.type']), "
    f"tools=tostring(attributes['claude.tool_names']), "
    f"body=substring(tostring(body),0,220) | sort by ts desc | take 40"
)


def q_logs(svc: str) -> str:
    return (
        f"{T} | where isnotnull(body) | where resource['service.name'] == '{svc}' "
        f"| project timestamp, severity_text, body | sort by timestamp desc | take 50"
    )


def q_cc_search(term: str) -> str:
    return (
        f"{CC} | where tostring(body) contains '{term}' "
        f"| project ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"body=substring(tostring(body),0,240) | sort by ts desc | take 40"
    )


# ---------- bzrk invocation ----------
def run_bzrk(args, timeout=DEFAULT_TIMEOUT):
    """Run the bzrk CLI with the given argument list. Returns (text, is_error)."""
    try:
        p = subprocess.run(
            [BZRK_BIN] + args, capture_output=True, text=True, timeout=timeout
        )
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            err = (p.stderr or "").strip()
            return ((out + "\n" + err).strip() or f"bzrk exited {p.returncode}"), True
        return (out or "(no rows)"), False
    except FileNotFoundError:
        return (
            f"error: '{BZRK_BIN}' not found on PATH. Install the Berserk CLI or set "
            f"BZRK_BIN to its full path."
        ), True
    except subprocess.TimeoutExpired:
        return f"bzrk timed out after {timeout}s", True
    except Exception as e:  # pragma: no cover - defensive
        return ("error running bzrk: " + str(e)), True


# Accepts "now" or "<n> <unit> [ago]" — e.g. "15m ago", "2 hours ago", "1d".
_SINCE_RE = re.compile(
    r"^(now|\d+\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks)(\s+ago)?)$",
    re.IGNORECASE,
)


def valid_since(s):
    """Lightweight validation of a time window. Not a security control (the value
    is passed as an argv element, never a shell string) — purely a better error."""
    return bool(_SINCE_RE.match(str(s).strip())) and len(str(s)) <= 32


def bzrk_search(kql, since):
    """Run a KQL search on the configured profile and time window."""
    if not valid_since(since):
        return (
            f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
            f"'2d ago', or 'now'."
        ), True
    return run_bzrk(["-P", PROFILE, "search", kql, "--since", since])


def do_schema():
    out1, e1 = run_bzrk(["-P", PROFILE, "search", ".show tables"])
    out2, e2 = run_bzrk(["-P", PROFILE, "search", f"{T} | getschema", "--since", "1h ago"])
    text = f"== tables ==\n{out1}\n== columns ==\n{out2}"
    return text, (e1 or e2)


# ---------- learned-query store ----------
def load_learned():
    try:
        with open(LEARNED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_learned(items):
    LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(LEARNED_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, LEARNED_PATH)


def sanitize_name(n):
    n = re.sub(r"[^a-zA-Z0-9_]+", "_", str(n).strip().lower()).strip("_")
    return n or "query"


# ---------- tool definitions ----------
def _since():
    return {"since": {"type": "string", "description": "Time window e.g. '15m ago', '1h ago', '2d ago'."}}


# Each entry: name -> (kql, default_since). Tools requiring user input or extra
# calls (logs, search, cc_search, schema) are handled explicitly in handle_call.
SIMPLE = {
    "list_containers": (Q_CONTAINERS, "15m ago"),
    "top_cpu": (Q_CPU, "15m ago"),
    "top_memory": (Q_MEM, "15m ago"),
    "errors_by_service": (Q_ERRORS, "1h ago"),
    "list_services": (Q_SERVICES, "1h ago"),
    "list_hosts": (Q_HOSTS, "1h ago"),
    "host_cpu": (Q_HOST_CPU, "30m ago"),
    "host_memory": (Q_HOST_MEM, "30m ago"),
    "container_hosts": (Q_CONTAINER_HOSTS, "1h ago"),
    "list_metrics": (Q_METRICS, "1h ago"),
    "bzrk_query_perf": (Q_QUERY_PERF, "1h ago"),
    "sre_error_rate": (Q_SRE_ERROR_RATE, "1h ago"),
    "sre_host_headroom": (Q_SRE_HOST_HEADROOM, "30m ago"),
    "sre_ingest_health": (Q_SRE_INGEST_HEALTH, "1h ago"),
    "sre_top_error_messages": (Q_SRE_TOP_ERRORS, "1h ago"),
    "soc_high_severity_logs": (Q_SOC_HIGH_SEV, "1h ago"),
    "soc_log_spike": (Q_SOC_LOG_SPIKE, "1h ago"),
    "soc_new_services": (Q_SOC_NEW_SERVICES, "7d ago"),
    "soc_repeated_errors": (Q_SOC_REPEATED_ERRORS, "6h ago"),
    "claude_recent": (Q_CC_RECENT, "1h ago"),
    "claude_sessions": (Q_CC_SESSIONS, "6h ago"),
    "claude_tools": (Q_CC_TOOLS, "6h ago"),
    "claude_errors": (Q_CC_ERRORS, "6h ago"),
}

TOOLS = [
    {"name": "list_containers", "description": "List all containers currently sending metrics to Berserk (with sample counts).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "top_cpu", "description": "Containers ranked by CPU percent, highest first. PER-CONTAINER — use ONLY when the user names a container, says 'docker'/'container', or asks for 'top containers'. For ambiguous whole-machine questions ('the box', 'the system', 'the server', 'the machine', 'what’s hammering/running hot') use host_cpu instead.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "top_memory", "description": "Containers ranked by memory usage in MB, highest first. PER-CONTAINER — use ONLY when the user names a container or says 'docker'/'container'. For ambiguous whole-machine memory questions ('the box', 'the system', 'the server') use host_memory instead.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "errors_by_service", "description": "Count of ERROR-level log lines grouped by service. Use for 'how many errors', 'which services have errors', or 'any errors?' — gives counts, not log text. For the actual error messages, use logs_for_service with the service name from this result.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "list_services", "description": "All services/sources sending data, with log vs metric breakdown. Best default for 'what's running?', 'what's reporting?', or 'what services are there?' — shows everything. For just hosts use list_hosts; for just containers use list_containers.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "list_hosts", "description": "All hosts reporting telemetry, by record count.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "host_cpu", "description": "Average CPU load (1-minute load average) per host. Use for per-host CPU AND as the DEFAULT for ambiguous whole-machine questions — 'the box', 'the system', 'the server', 'the machine', 'what's hammering/running hot' are about the hosts, not containers (top_cpu is per-CONTAINER).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "host_memory", "description": "Used memory in GB per host. Use for per-host memory AND as the DEFAULT for ambiguous whole-machine memory questions ('the box', 'the system', 'the server') — these are about the hosts, not containers (top_memory is per-CONTAINER).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "container_hosts", "description": "Map each container to the host/VM it runs on. Use to answer 'which host runs container X' or to JOIN per-container metrics (top_cpu/top_memory) with per-host metrics (host_cpu/host_memory) — don't infer the host from the container's name.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "logs_for_service", "description": "Recent log lines for a specific service e.g. 'nginx', 'postgres'. Use for 'show me the errors/logs from X' — returns actual log text. For error COUNTS across all services, use errors_by_service first, then drill into a specific service here.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "description": "service.name value"}}, **_since()), "required": ["service"]}},
    {"name": "schema", "description": "Show Berserk tables + column schema (live introspection).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_metrics", "description": "List every metric name currently being ingested, with sample counts + last-seen. Use to DISCOVER what telemetry exists before writing a `search` query.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "bzrk_query_perf", "description": "Berserk query engine latency percentiles: p50, p95, p99 in µs. Use for 'how fast is Berserk?', 'query latency', or 'p50/p95/p99 execution time'. Uses otel_histogram_percentile($raw, N) — the native Berserk histogram aggregate.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "discover_schema", "description": "Discover the shape of a data source: returns (1) every key present under `resource` with row counts, AND (2) a small sample of real rows so you can read the actual values. Use to learn an unknown or newly-ingested source before querying it. Optional `service` filter. Pair with list_services / list_metrics. Once you work out a query with `search`, persist it with save_query so it becomes reusable.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "description": "optional: limit to one service.name"}}, **_since())}},
    {"name": "search", "description": "Run an arbitrary Kusto/KQL query against the Berserk table. Use when the other tools do not fit; once it works, persist it with save_query.", "inputSchema": {"type": "object", "properties": dict({"kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}}, **_since()), "required": ["kql"]}},
    # --- SRE role tools (reliability, headroom, saturation, error rates, rollback signals) ---
    {"name": "sre_error_rate", "roles": ["sre"], "description": "SRE view of ERROR log events grouped by service and minute. Use for 'is the error rate climbing', 'which service is burning error budget', or 'what should we rollback first'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "sre_host_headroom", "roles": ["sre"], "description": "SRE view of host CPU load and memory used side-by-side. Use for 'which host is hottest', 'where is headroom lowest', or 'which VM is nearest saturation'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "sre_ingest_health", "roles": ["sre"], "description": "SRE view of Berserk ingest lag and dropped-data signals per host. Use for 'is ingest healthy', 'are we dropping telemetry', or 'is observability lagging'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "sre_service_health", "roles": ["sre"], "description": "SRE health rollup for one service: total events, error count, logs, metrics, last seen. Use for 'is service X healthy' or 'rollback signal for X'.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "description": "service.name value"}}, **_since()), "required": ["service"]}},
    {"name": "sre_top_error_messages", "roles": ["sre"], "description": "SRE summary of the most repeated error messages by service. Use for 'what error is dominating', 'top error signatures', or 'which message to investigate first'.", "inputSchema": {"type": "object", "properties": _since()}},
    # --- SOC role tools (anomalies, spikes, first-seen, repeated failures, incident timelines) ---
    {"name": "soc_high_severity_logs", "roles": ["soc"], "description": "SOC view of recent CRITICAL/FATAL/ERROR logs with service and message text. Use for 'show critical events', 'recent incident logs', or 'what looks severe right now'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_log_spike", "roles": ["soc"], "description": "SOC view of services with the largest log volume per minute. Use for 'anything anomalous', 'which source is spiking', or 'suspicious burst of logs'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_new_services", "roles": ["soc"], "description": "SOC view of services ordered by first-seen time. Use for 'what is new', 'anything first-seen', or 'did a new source appear'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_repeated_errors", "roles": ["soc"], "description": "SOC view of error messages that appear more than 5 times — potential probes, loops, or persistent incidents. Use for 'what keeps repeating' or 'show recurring failures'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_timeline", "roles": ["soc"], "description": "SOC incident timeline for one service: timestamps, severity, metric names, and message snippets. Use for 'timeline for service X' or 'reconstruct incident for X'.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "description": "service.name value"}}, **_since()), "required": ["service"]}},
    # --- Claude Code activity (service.name == 'claude-code'); low-volume, keep windows bounded ---
    {"name": "claude_recent", "roles": ["claude"], "description": "Recent Claude Code activity (timestamp, type, role, model, tool names, error flag), newest first. Default window 1h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_sessions", "roles": ["claude"], "description": "Claude Code sessions rollup: events, first/last seen, assistant turns, tool turns, and error count per session. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_tools", "roles": ["claude"], "description": "Claude Code tool-use histogram — how many times each tool (Bash, Edit, Read, ...) was used. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_errors", "roles": ["claude"], "description": "Claude Code tool errors — failed tool results (is_error=true) with a body snippet. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_search", "roles": ["claude"], "description": "Full-text search across Claude Code message and tool bodies for a substring. Default 6h.", "inputSchema": {"type": "object", "properties": dict({"term": {"type": "string", "description": "substring to find; may not contain quotes, pipe, backslash, or backtick"}}, **_since()), "required": ["term"]}},
]

MGMT_TOOLS = [
    {"name": "list_saved", "description": "List previously-saved custom queries (name + description). For a non-standard question, CHECK HERE FIRST before writing new KQL.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_saved", "description": "Run a previously-saved query by name (see list_saved). Deterministic - no KQL authoring.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "saved query name"}}, **_since()), "required": ["name"]}},
    {"name": "save_query", "description": "Persist a WORKING KQL query as a reusable named query so it never has to be figured out again. Call this after you answer a non-standard question with a custom search query. The query is run once to verify it works; if it errors it is NOT saved.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "short snake_case name"}, "description": {"type": "string", "description": "what the query answers"}, "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}, "roles": {"type": ["array", "string"], "description": "optional role(s) this query serves: sre, soc, claude, ops"}}, **_since()), "required": ["name", "description", "kql"]}},
    {"name": "request_discovery", "description": "Queue a newly-added service or metric for author-lane integration. Validates the source is currently visible in Berserk, then records a job for the discovery worker to drain. Use when a user says 'I added / connected / started shipping SOURCE'.", "inputSchema": {"type": "object", "properties": {"service": {"type": "string", "description": "service.name to integrate"}, "metric": {"type": "string", "description": "metric name to integrate"}, "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"}, "requested_by": {"type": "string", "description": "optional requester label"}, **_since()}}},
    {"name": "discovery_status", "description": "List pending and completed discovery jobs for new services or metrics.", "inputSchema": {"type": "object", "properties": {}}},
]


# ---------- tool metadata: titles + behavioral annotations (MCP 2025-06-18) ----------
# Annotations are advisory hints that let clients reason about a tool's behavior.
# Every tool here is read-only against Berserk (KQL cannot mutate) EXCEPT save_query,
# which writes to the local learned-query store. list_saved only reads that local
# store, so it carries openWorldHint=false.
_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
_READ_LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_WRITE_LOCAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

_ANNOTATIONS = {
    "save_query": _WRITE_LOCAL,
    "list_saved": _READ_LOCAL,
    "request_discovery": _WRITE_LOCAL,
    "discovery_status": _READ_LOCAL,
}

TITLES = {
    "list_containers": "List Containers",
    "top_cpu": "Top Containers by CPU",
    "top_memory": "Top Containers by Memory",
    "errors_by_service": "Errors by Service",
    "list_services": "List Services",
    "list_hosts": "List Hosts",
    "host_cpu": "Per-Host CPU Load",
    "host_memory": "Per-Host Memory",
    "container_hosts": "Container → Host Map",
    "logs_for_service": "Service Logs",
    "schema": "Schema Introspection",
    "list_metrics": "List Metrics",
    "bzrk_query_perf": "Berserk Query Performance",
    "sre_error_rate": "SRE: Error Rate",
    "sre_host_headroom": "SRE: Host Headroom",
    "sre_ingest_health": "SRE: Ingest Health",
    "sre_service_health": "SRE: Service Health",
    "sre_top_error_messages": "SRE: Top Error Messages",
    "soc_high_severity_logs": "SOC: High Severity Logs",
    "soc_log_spike": "SOC: Log Spike",
    "soc_new_services": "SOC: New Services",
    "soc_repeated_errors": "SOC: Repeated Errors",
    "soc_timeline": "SOC: Incident Timeline",
    "discover_schema": "Discover Schema",
    "search": "Run KQL",
    "claude_recent": "Claude Code: Recent Activity",
    "claude_sessions": "Claude Code: Sessions",
    "claude_tools": "Claude Code: Tool Histogram",
    "claude_errors": "Claude Code: Tool Errors",
    "claude_search": "Claude Code: Full-Text Search",
    "list_saved": "List Saved Queries",
    "run_saved": "Run Saved Query",
    "save_query": "Save Query",
    "request_discovery": "Request Discovery",
    "discovery_status": "Discovery Status",
}


def annotations_for(name):
    """Read-only by default; only the two store-management tools differ."""
    return _ANNOTATIONS.get(name, _READ)


def handle_call(name, arguments):
    """Dispatch a tools/call. Returns (text, is_error)."""
    # --- learning-loop management tools ---
    if name == "list_saved":
        items = [it for it in load_learned() if item_visible(it)]
        if not items:
            return "No saved queries yet.", False
        return "Saved queries:\n" + "\n".join(
            "- " + it["name"] + ": " + it.get("description", "") for it in items
        ), False
    if name == "run_saved":
        qn = sanitize_name(arguments.get("name", ""))
        items = [it for it in load_learned() if item_visible(it)]
        match = next((it for it in items if it["name"] == qn), None)
        if not match:
            avail = ", ".join(it["name"] for it in items) or "(none)"
            return "No saved query named '" + qn + "'. Available: " + avail, True
        since = arguments.get("since") or match.get("since") or "1h ago"
        return bzrk_search(match["kql"], since)
    if name == "save_query":
        nm = sanitize_name(arguments.get("name", ""))
        desc = str(arguments.get("description", "")).strip()
        kql = str(arguments.get("kql", "")).strip()
        since = arguments.get("since") or "1h ago"
        if not kql or not desc:
            return "save_query needs name, description, and kql.", True
        out, is_err = bzrk_search(kql, since)
        if is_err:
            return "NOT saved - the query failed when verified:\n" + out, True
        all_items = load_learned()
        is_amendment = any(it["name"] == nm for it in all_items)
        items = [it for it in all_items if it["name"] != nm]
        entry = {"name": nm, "description": desc, "kql": kql, "since": since}
        roles = normalize_roles(arguments.get("roles"))
        if roles:
            entry["roles"] = roles
        items.append(entry)
        items = items[-500:]  # cap learned store to prevent unbounded growth
        save_learned(items)
        log_entry = {
            "ts": now_iso(),
            "name": nm,
            "description": desc,
            "kql_preview": kql[:120],
            "action": "updated" if is_amendment else "created",
            "role": ACTIVE_ROLE,
        }
        amendments_path = Path(LEARNED_PATH).parent / "amendments_log.json"
        amendments = load_json_list(amendments_path)
        amendments.append(log_entry)
        save_json_list(amendments_path, amendments)
        return "Saved '" + nm + "'. Reusable now via run_saved name=" + nm + " (verified, returned data).", False

    # --- discovery queue tools ---
    if name == "request_discovery":
        service = str(arguments.get("service") or "").strip()
        metric = str(arguments.get("metric") or "").strip()
        if bool(service) == bool(metric):
            return "request_discovery needs exactly one of 'service' or 'metric'.", True
        target = service or metric
        if not re.match(r"^[A-Za-z0-9._-]+$", target):
            return "invalid source name (allowed: letters, digits, '.', '_', '-')", True
        kind = "service" if service else "metric"
        since = arguments.get("since") or "1h ago"
        check_kql = Q_SERVICES if kind == "service" else Q_METRICS
        visible, is_err = bzrk_search(check_kql, since)
        if is_err:
            return "Could not verify source visibility:\n" + visible, True
        if target not in visible:
            return f"{target} is not currently visible in Berserk; verify it is ingesting before queueing.", True
        role_hint = normalize_roles(arguments.get("role_hint"))
        queue = load_json_list(DISCOVERY_QUEUE_PATH)
        job = {
            "source": target, "kind": kind,
            "role_hint": role_hint[0] if role_hint else (ACTIVE_ROLE if ACTIVE_ROLE != "all" else ""),
            "requested_by": str(arguments.get("requested_by") or "").strip() or "manual",
            "status": "pending", "ts": now_iso(),
        }
        queue = [it for it in queue if not (it.get("source") == target and it.get("kind") == kind and it.get("status") == "pending")]
        queue.append(job)
        save_json_list(DISCOVERY_QUEUE_PATH, queue)
        return f"{target} queued for integration ({kind}). The author lane will author, verify, and save a query for it.", False
    if name == "discovery_status":
        items = load_json_list(DISCOVERY_QUEUE_PATH)
        if not items:
            return "No discovery jobs queued.", False
        lines = [
            f"- {it.get('source','?')} [{it.get('kind','?')}] status={it.get('status','?')} "
            f"role={it.get('role_hint','') or 'none'} requested_by={it.get('requested_by','?')} ts={it.get('ts','?')}"
            for it in items
        ]
        return "Discovery jobs:\n" + "\n".join(lines), False

    # --- simple fixed-query tools ---
    if name in SIMPLE:
        kql, default_since = SIMPLE[name]
        since = arguments.get("since") or default_since
        return bzrk_search(kql, since)

    # --- tools needing input validation or extra calls ---
    if name == "schema":
        return do_schema()
    if name == "discover_schema":
        svc = arguments.get("service")
        if svc and not re.match(r"^[A-Za-z0-9._-]+$", str(svc)):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        svc_str = str(svc) if svc else None
        # Two perspectives: keys+counts (compact, sortable) and a row sample (real values).
        # NOTE: bag_keys() is listed as a "missing function" in Berserk's docs but works
        # in practice. The row sample uses only documented features (where/take/project),
        # so we treat the call as a failure ONLY if BOTH halves fail — if bag_keys ever
        # gets removed, the sample still answers.
        out1, e1 = bzrk_search(q_discover_keys(svc_str), since)
        out2, e2 = bzrk_search(q_discover_sample(svc_str), since)
        return f"== resource keys (count) ==\n{out1}\n\n== sample rows ==\n{out2}", (e1 and e2)
    if name == "logs_for_service":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not re.match(r"^[A-Za-z0-9._-]+$", str(svc)):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        return bzrk_search(q_logs(str(svc)), since)
    if name == "sre_service_health":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not re.match(r"^[A-Za-z0-9._-]+$", str(svc)):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        return bzrk_search(q_sre_service_health(str(svc)), since)
    if name == "soc_timeline":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not re.match(r"^[A-Za-z0-9._-]+$", str(svc)):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "6h ago"
        return bzrk_search(q_soc_timeline(str(svc)), since)
    if name == "search":
        kql = arguments.get("kql")
        if not kql:
            return "missing required 'kql'", True
        since = arguments.get("since") or "15m ago"
        return bzrk_search(str(kql), since)
    if name == "claude_search":
        term = arguments.get("term")
        if not term:
            return "missing required 'term'", True
        if re.search(r"['\"|\\`]", str(term)):
            return "term may not contain quotes, pipe, backslash, or backtick", True
        since = arguments.get("since") or "6h ago"
        return bzrk_search(q_cc_search(str(term)), since)

    return "unknown tool: " + str(name), True


# ---------- JSON-RPC plumbing ----------
def dispatch(req):
    """Handle one JSON-RPC request. Returns a response dict, or None for notifications."""
    method = req.get("method")
    id_ = req.get("id")
    if method == "initialize":
        pv = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        allt = [t for t in TOOLS + MGMT_TOOLS if tool_visible(t)]
        tl = []
        for t in allt:
            tl.append({
                "name": t["name"],
                "title": TITLES.get(t["name"], t["name"]),
                "description": t["description"],
                "inputSchema": t["inputSchema"],
                "annotations": annotations_for(t["name"]),
            })
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tl}}
    if method == "tools/call":
        params = req.get("params") or {}
        text, is_err = handle_call(params.get("name"), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_err}}
    if "id" not in req:
        return None  # unknown notification
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "method not found: " + str(method)}}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    log(f"starting v{__version__} (profile={PROFILE}, table={TABLE}, bzrk={BZRK_BIN})")
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            log("bad json: " + str(e))
            continue
        resp = dispatch(req)
        if resp is not None:
            send(resp)
    log("stdin closed")


if __name__ == "__main__":
    main()
