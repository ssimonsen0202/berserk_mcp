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
from pathlib import Path

__version__ = "1.3.0"

# ---------- configuration (env-overridable) ----------
BZRK_BIN = os.environ.get("BZRK_BIN", "bzrk")
PROFILE = os.environ.get("BZRK_PROFILE", "local")
TABLE = os.environ.get("BERSERK_TABLE", "default")
DEFAULT_TIMEOUT = int(os.environ.get("BZRK_TIMEOUT", "120"))


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
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "berserk-q", "title": "Berserk Query", "version": __version__}

# Surfaced to clients in the initialize response. Primes models (especially small
# / local ones) to answer by *calling a specific tool* rather than authoring KQL.
INSTRUCTIONS = (
    "Answer observability questions by calling these tools — do not write KQL by hand. "
    "Prefer the most specific tool (e.g. top_cpu, errors_by_service, logs_for_service, "
    "host_cpu) over the generic `search`. Per-host metrics (host_cpu, host_memory) and "
    "per-container metrics (top_cpu, top_memory) are different — pick by what's asked. "
    "Every query tool takes an optional `since` like '15m ago' or '2h ago'. For a "
    "recurring custom question, get it working with `search`, then `save_query` so it "
    "can be re-run deterministically with `run_saved`."
)


def log(msg):
    print("[berserk-mcp] " + str(msg), file=sys.stderr, flush=True)


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
    "claude_recent": (Q_CC_RECENT, "1h ago"),
    "claude_sessions": (Q_CC_SESSIONS, "6h ago"),
    "claude_tools": (Q_CC_TOOLS, "6h ago"),
    "claude_errors": (Q_CC_ERRORS, "6h ago"),
}

TOOLS = [
    {"name": "list_containers", "description": "List all containers currently sending metrics to Berserk (with sample counts).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "top_cpu", "description": "Containers ranked by CPU percent, highest first (per-CONTAINER; for per-host CPU use host_cpu).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "top_memory", "description": "Containers ranked by memory usage in MB, highest first (per-CONTAINER; for per-host memory use host_memory).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "errors_by_service", "description": "Count of ERROR-level log lines grouped by service.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "list_services", "description": "All services/sources sending data, with log vs metric breakdown.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "list_hosts", "description": "All hosts reporting telemetry, by record count.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "host_cpu", "description": "Average CPU load (1-minute load average) per host. Use this for per-host CPU questions (top_cpu is per-CONTAINER).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "host_memory", "description": "Used memory in GB per host. Use this for per-host memory questions (top_memory is per-CONTAINER).", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "logs_for_service", "description": "Recent log lines for a specific service e.g. 'nginx', 'postgres'.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "description": "service.name value"}}, **_since()), "required": ["service"]}},
    {"name": "schema", "description": "Show Berserk tables + column schema (live introspection).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search", "description": "Run an arbitrary Kusto/KQL query against the Berserk table. Use when the other tools do not fit; once it works, persist it with save_query.", "inputSchema": {"type": "object", "properties": dict({"kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}}, **_since()), "required": ["kql"]}},
    # --- Claude Code activity (service.name == 'claude-code'); low-volume, keep windows bounded ---
    {"name": "claude_recent", "description": "Recent Claude Code activity (timestamp, type, role, model, tool names, error flag), newest first. Default window 1h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_sessions", "description": "Claude Code sessions rollup: events, first/last seen, assistant turns, tool turns, and error count per session. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_tools", "description": "Claude Code tool-use histogram — how many times each tool (Bash, Edit, Read, ...) was used. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_errors", "description": "Claude Code tool errors — failed tool results (is_error=true) with a body snippet. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_search", "description": "Full-text search across Claude Code message and tool bodies for a substring. Default 6h.", "inputSchema": {"type": "object", "properties": dict({"term": {"type": "string", "description": "substring to find; may not contain quotes, pipe, backslash, or backtick"}}, **_since()), "required": ["term"]}},
]

MGMT_TOOLS = [
    {"name": "list_saved", "description": "List previously-saved custom queries (name + description). For a non-standard question, CHECK HERE FIRST before writing new KQL.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_saved", "description": "Run a previously-saved query by name (see list_saved). Deterministic - no KQL authoring.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "saved query name"}}, **_since()), "required": ["name"]}},
    {"name": "save_query", "description": "Persist a WORKING KQL query as a reusable named query so it never has to be figured out again. Call this after you answer a non-standard question with a custom search query. The query is run once to verify it works; if it errors it is NOT saved.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "short snake_case name"}, "description": {"type": "string", "description": "what the query answers"}, "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}}, **_since()), "required": ["name", "description", "kql"]}},
]


# ---------- tool metadata: titles + behavioral annotations (MCP 2025-06-18) ----------
# Annotations are advisory hints that let clients reason about a tool's behavior.
# Every tool here is read-only against Berserk (KQL cannot mutate) EXCEPT save_query,
# which writes to the local learned-query store. list_saved only reads that local
# store, so it carries openWorldHint=false.
_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
_READ_LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_WRITE_LOCAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

_ANNOTATIONS = {"save_query": _WRITE_LOCAL, "list_saved": _READ_LOCAL}

TITLES = {
    "list_containers": "List Containers",
    "top_cpu": "Top Containers by CPU",
    "top_memory": "Top Containers by Memory",
    "errors_by_service": "Errors by Service",
    "list_services": "List Services",
    "list_hosts": "List Hosts",
    "host_cpu": "Per-Host CPU Load",
    "host_memory": "Per-Host Memory",
    "logs_for_service": "Service Logs",
    "schema": "Schema Introspection",
    "search": "Run KQL",
    "claude_recent": "Claude Code: Recent Activity",
    "claude_sessions": "Claude Code: Sessions",
    "claude_tools": "Claude Code: Tool Histogram",
    "claude_errors": "Claude Code: Tool Errors",
    "claude_search": "Claude Code: Full-Text Search",
    "list_saved": "List Saved Queries",
    "run_saved": "Run Saved Query",
    "save_query": "Save Query",
}


def annotations_for(name):
    """Read-only by default; only the two store-management tools differ."""
    return _ANNOTATIONS.get(name, _READ)


def handle_call(name, arguments):
    """Dispatch a tools/call. Returns (text, is_error)."""
    # --- learning-loop management tools ---
    if name == "list_saved":
        items = load_learned()
        if not items:
            return "No saved queries yet.", False
        return "Saved queries:\n" + "\n".join(
            "- " + it["name"] + ": " + it.get("description", "") for it in items
        ), False
    if name == "run_saved":
        qn = sanitize_name(arguments.get("name", ""))
        items = load_learned()
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
        items = [it for it in load_learned() if it["name"] != nm]
        items.append({"name": nm, "description": desc, "kql": kql, "since": since})
        items = items[-500:]  # cap learned store to prevent unbounded growth
        save_learned(items)
        return "Saved '" + nm + "'. Reusable now via run_saved name=" + nm + " (verified, returned data).", False

    # --- simple fixed-query tools ---
    if name in SIMPLE:
        kql, default_since = SIMPLE[name]
        since = arguments.get("since") or default_since
        return bzrk_search(kql, since)

    # --- tools needing input validation or extra calls ---
    if name == "schema":
        return do_schema()
    if name == "logs_for_service":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not re.match(r"^[A-Za-z0-9._-]+$", str(svc)):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        return bzrk_search(q_logs(str(svc)), since)
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
        allt = TOOLS + MGMT_TOOLS
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
