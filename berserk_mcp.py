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
import subprocess
import re
import os
from datetime import datetime, timezone
from pathlib import Path

import agent_analytics
import ingestion_advisor
import parser_factory
import secret_scan

__version__ = "1.15.0"

# ---------- configuration (env-overridable) ----------
BZRK_BIN = os.environ.get("BZRK_BIN", "bzrk")
PROFILE = os.environ.get("BZRK_PROFILE", "local")
TABLE = os.environ.get("BERSERK_TABLE", "default")
DEFAULT_TIMEOUT = int(os.environ.get("BZRK_TIMEOUT", "120"))
ACTIVE_ROLE = os.environ.get("BERSERK_MCP_ROLE", "all").strip().lower() or "all"
REDACT_MODE = os.environ.get("BERSERK_MCP_REDACT", "flag").strip().lower()
if REDACT_MODE not in {"off", "flag", "redact"}:
    REDACT_MODE = "flag"
REDACT_ENTROPY = os.environ.get("BERSERK_MCP_REDACT_ENTROPY", "").strip().lower() in {
    "1", "true", "yes", "on",
}
REDACT_PII_TYPES = frozenset(
    item.strip().lower()
    for item in os.environ.get("BERSERK_MCP_REDACT_PII", "").split(",")
    if item.strip().lower() in secret_scan.ALL_PII_TYPES
)


class StorePathError(ValueError):
    """Raised when a caller-supplied store path fails safety validation."""


def _validate_store_path(candidate, purpose):
    """Defense-in-depth guard for operator-supplied filesystem paths.

    Env vars (BERSERK_MCP_LEARNED_PATH, XDG_CONFIG_HOME, APPDATA) are set
    by the operator running this process, so a rogue value is self-inflicted
    rather than remote-attacker-controlled. This validator still rejects
    the two mistakes most likely to cause real damage:

    - a non-absolute path (rules out unpredictable CWD-relative writes)
    - traversal patterns (``..`` in any segment before or after resolve)

    Returns the resolved absolute ``Path`` on success; raises
    ``StorePathError`` otherwise.
    """
    if not candidate:
        raise StorePathError(f"{purpose} path is empty")
    if not isinstance(candidate, (str, Path)):
        raise StorePathError(f"{purpose} path must be a string or Path")
    text = str(candidate)
    if any(ord(c) < 32 for c in text):
        raise StorePathError(f"{purpose} path contains control characters")
    p = Path(text)
    if not p.is_absolute():
        raise StorePathError(f"{purpose} path must be absolute (got {text!r})")
    if ".." in p.parts:
        raise StorePathError(f"{purpose} path must not contain '..' segments")
    resolved = p.resolve(strict=False)
    if ".." in resolved.parts:
        raise StorePathError(f"{purpose} path resolves through '..'")
    return resolved


def _default_learned_path() -> Path:
    """Where to persist learned queries, following platform conventions.

    Any operator-supplied env-var override is validated through
    ``_validate_store_path``: absolute, no ``..`` segments, no control
    characters. Standard OS env vars (APPDATA, XDG_CONFIG_HOME) go through
    the same guard, so a poisoned XDG_CONFIG_HOME cannot direct writes
    outside a predictable absolute location either.
    """
    env = os.environ.get("BERSERK_MCP_LEARNED_PATH")
    if env:
        return _validate_store_path(env, "BERSERK_MCP_LEARNED_PATH")
    if os.name == "nt":
        raw = os.environ.get("APPDATA")
        base = _validate_store_path(raw, "APPDATA") if raw else (Path.home() / "AppData" / "Roaming")
    else:
        raw = os.environ.get("XDG_CONFIG_HOME")
        base = _validate_store_path(raw, "XDG_CONFIG_HOME") if raw else (Path.home() / ".config")
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
    "windows-forensics": (
        "You are in the Windows forensics lane; first verify that Windows event telemetry exists "
        "and inspect its real schema before authoring or saving any query. "
    ),
}


def _load_primer(role: str) -> str:
    """Load primers/<role>.md from BERSERK_MCP_PRIMERS_DIR, adjacent to this script,
    or the installed data-files location (share/berserk-mcp/primers/)."""
    env_dir = os.environ.get("BERSERK_MCP_PRIMERS_DIR", "")
    search_dirs = []
    if env_dir:
        search_dirs.append(Path(env_dir))
    search_dirs.append(Path(__file__).parent / "primers")
    search_dirs.append(Path(sys.prefix) / "share" / "berserk-mcp" / "primers")
    if role in _ROLE_PREFIX:
        for primer_dir in search_dirs:
            f = primer_dir / f"{role}.md"
            try:
                return f.read_text(encoding="utf-8").strip() + "\n\n"
            except OSError:
                continue
    return ""


def build_instructions(role: str) -> str:
    """Build initialize guidance for any role registered in ``_ROLE_PREFIX``."""
    return _load_primer(role) + _ROLE_PREFIX.get(role, "") + _BASE_INSTRUCTIONS


INSTRUCTIONS = build_instructions(ACTIVE_ROLE)


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
    valid = [r for r in parts if r in _ROLE_PREFIX]
    return valid or None


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_private_dir(path):
    """Create path's parent directory, chmod'd 0700, if it doesn't already exist.

    mkdir(mode=...) alone is masked by the process umask and doesn't fix a
    directory that already exists with looser permissions, so chmod
    explicitly every time rather than relying on the mkdir call.

    The path is passed through ``_validate_store_path`` at every entry so
    even a stale ``LEARNED_PATH`` predating the module-load validator, or a
    future caller that constructs a path from untrusted input, cannot mkdir
    or chmod outside a clean absolute location.
    """
    safe = _validate_store_path(path, "store")
    safe.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(safe.parent, 0o700)


def load_json_list(path):
    try:
        safe = _validate_store_path(path, "store")
    except StorePathError as e:
        log(f"load_json_list refused: {e}")
        return []
    try:
        with open(safe, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        log(f"load_json_list({safe}): {type(e).__name__}: {e}")
        return []


def save_json_list(path, items):
    safe = _validate_store_path(path, "store")
    _ensure_private_dir(safe)
    tmp = str(safe) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, safe)


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
    f"| extend val = iff(metric_name == 'system.memory.usage', value / 1073741824.0, value), "
    f"unit = iff(metric_name == 'system.memory.usage', 'GB', 'load_avg') "
    f"| where metric_name == 'system.cpu.load_average.1m' or tostring(attributes['state']) == 'used' "
    f"| summarize samples=count(), avg_value=avg(val) "
    f"by host=tostring(resource['host.name']), metric=tostring(metric_name), unit "
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

# --- Trace tools (span-level latency and error triage) ---
# Live-verified 2026-07-17 against a real Berserk deployment (see the "Trace
# tools" section in README.md). The field names guessed when this was first
# written -- trace_id/span_id/
# parent_span_id/span_name/duration/status_code -- were all confirmed correct
# by analogy with this table's `<signal>_name` convention. Two real bugs were
# caught by that live run and are fixed below:
#   1. `duration` is a *dynamic*-typed column -- Berserk's KQL rejects sorting
#      a dynamic value directly ("Cannot sort by a dynamic value"). Needs an
#      explicit toint(duration) cast first.
#   2. A trace_id's rows aren't all spans -- other correlated telemetry (seen
#      live: a log row) shares the same trace_id/span_id but has a null
#      span_name. Sorting by `timestamp` (an ingest-adjacent field) also gave
#      child-before-parent ordering on a real 2-span trace; `start_time` sorts
#      correctly. q_trace_analyze now filters to isnotnull(span_name) and
#      sorts by start_time.
#   3. (BUG-006, 2026-07-18 security review) Q_TRACE_FIND_SLOW had the same
#      correlated-non-span-row exposure as (2) above but never got the same
#      isnotnull(span_name) guard -- a log row sharing a trace_id can have an
#      empty parent_span_id too (isempty() matches null), so it could surface
#      as a fake "root span" candidate. Added the same guard here.
Q_TRACE_FIND_SLOW = (
    f"{T} | where isnotnull(trace_id) | where isnotnull(span_name) "
    f"| where isempty(parent_span_id) "
    f"| extend dur=toint(duration) "
    f"| where isnotnull(dur) and dur >= 0 "
    f"| project trace_id, span_name, dur, timestamp, "
    f"service=tostring(resource['service.name']) "
    f"| sort by dur desc | take 10"
)
Q_TRACE_FIND_ERRORS = (
    f"{T} | where isnotnull(trace_id) | where status_code == 'ERROR' "
    f"| project trace_id, span_name, timestamp, "
    f"service=tostring(resource['service.name']) "
    f"| sort by timestamp desc | take 20"
)


def q_trace_analyze(trace_id: str) -> str:
    return (
        f"{T} | where trace_id == '{trace_id}' | where isnotnull(span_name) "
        f"| project span_name, start_time, dur=toint(duration), span_id, parent_span_id, "
        f"service=tostring(resource['service.name']), status_code "
        f"| sort by start_time asc"
    )


def q_trace_logs(trace_id: str) -> str:
    return (
        f"{T} | where trace_id == '{trace_id}' | where isnotnull(body) "
        f"| project timestamp, severity_text, "
        f"service=tostring(resource['service.name']), "
        f"body=substring(tostring(body), 0, 200) "
        f"| sort by timestamp asc"
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
# bzrk has been observed to print an authentication failure (e.g. "Refresh
# token rejected...") to stderr while still exiting 0 -- a real 2026-07-10
# incident on the bzrk-q bash wrapper, which already carries this same guard
# (_bzrk_check_auth). This Python adapter never got the equivalent fix, so an
# exit-0 auth failure was silently returned as a successful empty result
# (confirmed by the 2026-07-18 security review, SEC-003). Match bzrk-q's
# pattern exactly for consistency between the two wrappers.
_AUTH_FAILURE_RE = re.compile(
    r"refresh token rejected|run .*bzrk login|unauthorized|unauthenticated|"
    r"login required",
    re.IGNORECASE,
)

AUTH_FAILURE_MESSAGE = "bzrk authentication failed; run `bzrk login` and retry"

# F-005: bound the DIAGNOSTIC text returned on a non-zero exit -- this text
# is always error/status output, never the actual data a caller asked for,
# so capping it has no effect on legitimate large query results. The
# success path (p.returncode == 0) is intentionally left unbounded here:
# real KQL result sets are wanted output, and tool queries already bound
# row counts via `take N`. This does not bound subprocess.run's own
# in-memory buffering while bzrk is running -- bzrk is an operator-
# installed, trusted local CLI, and the existing `timeout` already bounds
# worst-case duration; a full rewrite to streamed/spooled capture was
# judged disproportionate to that residual risk.
MAX_BZRK_DIAGNOSTIC_CHARS = 100_000


def run_bzrk(args, timeout=DEFAULT_TIMEOUT):
    """Run the bzrk CLI with the given argument list. Returns (text, is_error)."""
    try:
        p = subprocess.run(
            [BZRK_BIN] + args, capture_output=True, text=True, timeout=timeout
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if err and _AUTH_FAILURE_RE.search(err):
            return AUTH_FAILURE_MESSAGE, True
        if p.returncode != 0:
            diagnostic = (out + "\n" + err).strip() or f"bzrk exited {p.returncode}"
            if len(diagnostic) > MAX_BZRK_DIAGNOSTIC_CHARS:
                diagnostic = diagnostic[:MAX_BZRK_DIAGNOSTIC_CHARS] + "\n...[truncated]"
            return diagnostic, True
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


def count_result_is_zero(text):
    """True if a `summarize n=count()`-style single-row result reports zero.

    `summarize count()` always emits one row even when nothing matches (n=0),
    so it never hits run_bzrk's "(no rows)" empty-stdout sentinel. Read the
    last whitespace-separated token of the last non-empty line — the count —
    regardless of whether bzrk renders it as a table, CSV, or plain value.
    """
    if not text or text.strip() == "(no rows)":
        return True
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return True
    tokens = lines[-1].split()
    if tokens and tokens[-1].lstrip("-").isdigit():
        return int(tokens[-1]) == 0
    return False


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


# Free-text KQL is passed as a positional argv element to the bzrk CLI. If it
# began with '-', some CLI parsers would interpret it as an option rather than
# the query (e.g. a stray "--profile x"), silently changing what runs. Require
# every query to actually start with the configured table.
_KQL_PREFIX_RE = re.compile(r"^\s*" + re.escape(TABLE) + r"\b")


def bzrk_search(kql, since, extra=None):
    """Run a KQL search on the configured profile and time window. `extra` adds
    trailing CLI flags (e.g. ['--json']) without duplicating the guards."""
    if not _KQL_PREFIX_RE.match(str(kql)):
        return (
            f"invalid KQL: query must start with '{TABLE} | ...' "
            f"(got: {str(kql)[:40]!r})"
        ), True
    if not valid_since(since):
        return (
            f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
            f"'2d ago', or 'now'."
        ), True
    return run_bzrk(["-P", PROFILE, "search", kql, "--since", since] + list(extra or []))


# bzrk builds that don't support --json reject it with an argument-parse error;
# detect that so we can transparently fall back to the default table output.
_JSON_UNSUPPORTED_RE = re.compile(
    r"(?i)(unrecognized|unexpected|unknown|invalid)\b.*\b(argument|option|flag|--?json)|--json"
)


def bzrk_search_json(kql, since):
    """bzrk_search variant that requests --json for robust programmatic parsing
    (the analytics/secret modules parse rows in Python; aligned table output can
    truncate or ambiguously split wide `body` columns). Falls back to the
    default table output only when this bzrk build rejects the --json flag, so
    there is no regression on builds that lack it."""
    out, is_err = bzrk_search(kql, since, extra=["--json"])
    if is_err and _JSON_UNSUPPORTED_RE.search(out or ""):
        return bzrk_search(kql, since)
    return out, is_err


def do_schema():
    out1, e1 = run_bzrk(["-P", PROFILE, "search", ".show tables"])
    out2, e2 = run_bzrk(["-P", PROFILE, "search", f"{T} | getschema", "--since", "1h ago"])
    text = f"== tables ==\n{out1}\n== columns ==\n{out2}"
    return text, (e1 or e2)


# ---------- learned-query store ----------
def load_learned():
    try:
        safe = _validate_store_path(LEARNED_PATH, "LEARNED_PATH")
    except StorePathError as e:
        log(f"load_learned refused: {e}")
        return []
    try:
        with open(safe, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        log(f"load_learned({safe}): {type(e).__name__}: {e}")
        return []


def save_learned(items):
    safe = _validate_store_path(LEARNED_PATH, "LEARNED_PATH")
    _ensure_private_dir(safe)
    tmp = str(safe) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, safe)


def sanitize_name(n):
    n = re.sub(r"[^a-zA-Z0-9_]+", "_", str(n).strip().lower()).strip("_")
    return n or "query"


def persist_learned_query(entry, action_source):
    """Storage core shared by the save_query tool and the parser-factory
    pipeline: dedupe by name, append, cap at 500, and log the amendment.
    Returns the log_entry dict (whose 'name' reflects any rename below).

    action_source == "generated": pipeline-authored entries must never
    silently replace a human's saved query — on name collision, rename to
    '<name>_gen' rather than overwrite (a human save always outranks a
    generated one). Callers with a manual origin (save_query) are expected
    to have already resolved any overwrite confirmation before calling
    this helper, so a same-name entry here simply replaces, matching the
    pre-refactor behavior.
    """
    all_items = load_learned()
    nm = entry["name"]
    existing = next((it for it in all_items if it["name"] == nm), None)
    is_amendment = existing is not None
    if action_source == "generated":
        entry = {**entry, "origin": "generated"}
        by_name = {it["name"]: it for it in all_items}

        def _is_free_or_generated(candidate):
            found = by_name.get(candidate)
            return found is None or found.get("origin") == "generated"

        if not _is_free_or_generated(nm):
            base = nm
            gen_name = f"{base}_gen"
            chosen = None
            if _is_free_or_generated(gen_name):
                chosen = gen_name
            else:
                # Bound by store cap (500) rather than an arbitrary suffix cap
                for i in range(2, 502):
                    candidate = f"{base}_gen{i}"
                    if _is_free_or_generated(candidate):
                        chosen = candidate
                        break
            if chosen is None:
                raise ValueError(
                    "cannot persist generated query: no free name available "
                    "(base and all _gen/_genN suffixes are occupied by human entries)"
                )
            nm = chosen
            entry = {**entry, "name": nm}
        is_amendment = nm in by_name

    items = [it for it in all_items if it["name"] != nm]
    items.append(entry)
    items = items[-500:]  # cap learned store to prevent unbounded growth
    save_learned(items)

    log_entry = {
        "ts": now_iso(),
        "name": nm,
        "description": entry.get("description", ""),
        "kql_preview": entry.get("kql", "")[:120],
        "action": "generated" if action_source == "generated" else ("updated" if is_amendment else "created"),
        "role": ACTIVE_ROLE,
    }
    amendments_path = Path(LEARNED_PATH).parent / "amendments_log.json"
    amendments = load_json_list(amendments_path)
    amendments.append(log_entry)
    amendments = amendments[-1000:]  # cap to prevent unbounded growth
    save_json_list(amendments_path, amendments)
    return log_entry


parser_factory.configure(
    bzrk_search=bzrk_search,
    table=TABLE,
    # A callable, not a captured Path: tests monkeypatch bm.LEARNED_PATH
    # per-test to isolate stores into a tempdir, so this must resolve
    # LEARNED_PATH fresh on every call rather than freezing it here at
    # import time.
    get_store_dir=lambda: Path(LEARNED_PATH).parent,
    ensure_private_dir=_ensure_private_dir,
    now_iso=now_iso,
    log=log,
    persist_learned_query=persist_learned_query,
    sanitize_name=sanitize_name,
    redact=lambda text: secret_scan.redact(
        text, include_entropy=True, pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
)
agent_analytics.configure(
    bzrk_search=bzrk_search_json,
    table=TABLE,
    redact=lambda text: secret_scan.redact(
        text, include_entropy=True, pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
)
secret_scan.configure(
    bzrk_search=bzrk_search_json,
    table=TABLE,
)
ingestion_advisor.configure(
    list_services=lambda since: bzrk_search(Q_SERVICES, since),
    list_metrics=lambda since: bzrk_search(Q_METRICS, since),
)


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
    "soc_repeated_errors": (Q_SOC_REPEATED_ERRORS, "6h ago"),
    "claude_recent": (Q_CC_RECENT, "1h ago"),
    "claude_sessions": (Q_CC_SESSIONS, "6h ago"),
    "claude_tools": (Q_CC_TOOLS, "6h ago"),
    "claude_errors": (Q_CC_ERRORS, "6h ago"),
    "trace_find_slow": (Q_TRACE_FIND_SLOW, "1h ago"),
    "trace_find_errors": (Q_TRACE_FIND_ERRORS, "1h ago"),
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
    # --- Trace tools (span-level latency/error triage; UNVERIFIED field names — see the
    # comment above Q_TRACE_FIND_SLOW. Descriptions below flag this to the model too.) ---
    {"name": "trace_find_slow", "description": "Find the highest-duration root spans in the time window. Use for 'what's slow', 'find the slowest requests', or as the entry point before trace_analyze.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "trace_find_errors", "description": "Find spans whose status indicates an error. Use for 'which requests failed' or as the entry point before trace_analyze.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "trace_analyze", "description": "Full breakdown of one trace by trace_id — every span in time order plus correlated log lines from the same trace_id. Use after trace_find_slow/trace_find_errors surface a trace_id worth investigating.", "inputSchema": {"type": "object", "properties": {"trace_id": {"type": "string", "description": "trace_id from trace_find_slow/trace_find_errors/search"}}, "required": ["trace_id"]}},
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
    {"name": "claude_loop_check", "roles": ["claude"], "description": "Claude Code loop detector. Heuristically flags sessions that repeat the same tool/target, retry errors, or oscillate between the same calls. Bodies are truncated; output is diagnostic, not raw transcript replay. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_model_fit", "roles": ["claude"], "description": "Claude Code model-fit heuristic. Uses observed tool count, errors, duration, and loop signals to flag frontier models on trivial work or cheap models on complex/repetitive work. Not a billing statement. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_token_burn", "roles": ["claude"], "description": "Claude Code token-burn analysis. Uses exact claude.tokens_input/output usage when present, falls back to a labeled body-length estimate per session, computes burn per distinct tool/file target, and joins high burn with loop signals. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_cost_report", "roles": ["claude"], "description": "Claude Code multi-day cost report: per-day token burn with exact/estimated labeling, per-model split, optional per-project attribution from file paths, and a burn-growing/flat/declining trend verdict. Default 7d.", "inputSchema": {"type": "object", "properties": dict({"group_by": {"type": "string", "enum": ["day", "model", "project"], "description": "Aggregation: by day (default), model, or inferred project."}}, **_since())}},
    {"name": "claude_session_deep_dive", "roles": ["claude"], "description": "Timeline drilldown for one Claude Code session: contiguous tool phases with error counts, activity gaps over 5 minutes, cumulative token burn (exact/estimated), and a loop verdict. Requires session_id (find them via claude_sessions).", "inputSchema": {"type": "object", "properties": dict({"session_id": {"type": "string", "description": "claude.session_id value"}}, **_since()), "required": ["session_id"]}},
    {"name": "claude_workflow_insights", "roles": ["claude"], "description": "Cross-session Claude Code workflow patterns: most common tool sequences, error hotspots by tool+target, and top-decile burn-per-target sessions. Use for 'how is my agent working overall?'. Default 7d.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "scan_secrets", "roles": ["soc"], "description": "Audit recent log bodies for potential credentials and optionally selected PII categories. Returns only aggregate service/type counts and first-seen timestamps; secret values are never returned. Default 1h.", "inputSchema": {"type": "object", "properties": {"since": _since()["since"], "include_entropy": {"type": "boolean", "description": "Enable false-positive-prone high-entropy token detection."}, "include_pii": {"type": "array", "items": {"type": "string", "enum": ["email", "ipv4", "ipv6", "credit_card"]}, "description": "Optional PII categories to include."}}}},
    {"name": "suggest_ingestion", "description": "Recommend concrete telemetry sources for a role/use case. With check_gap=true, compares service and metric hints against live Berserk inventory and marks each source present or missing. Catalog-backed and read-only.", "inputSchema": {"type": "object", "properties": {"role_or_usecase": {"type": "string", "description": "Catalog key such as sre/onprem-ad-health, soc/endpoint-identity, change-management/ansible, or scom."}, "check_gap": {"type": "boolean", "description": "Compare recommendations with live service and metric inventory."}, "since": _since()["since"]}, "required": ["role_or_usecase"]}},
]

MGMT_TOOLS = [
    {"name": "list_saved", "description": "List previously-saved custom queries (name + description). For a non-standard question, CHECK HERE FIRST before writing new KQL.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_saved", "description": "Run a previously-saved query by name (see list_saved). Deterministic - no KQL authoring.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "saved query name"}}, **_since()), "required": ["name"]}},
    {"name": "save_query", "description": "Persist a WORKING KQL query as a reusable named query so it never has to be figured out again. Call this after you answer a non-standard question with a custom search query. The query is run once to verify it works; if it errors it is NOT saved. Replacing an existing saved query of the same name requires overwrite=true.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "short snake_case name"}, "description": {"type": "string", "description": "what the query answers"}, "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}, "roles": {"type": ["array", "string"], "description": "optional role(s) this query serves: sre, soc, claude, ops"}, "overwrite": {"type": "boolean", "description": "must be true to replace an existing saved query of the same name"}}, **_since()), "required": ["name", "description", "kql"]}},
    {"name": "request_discovery", "description": "Queue a newly-added service or metric for author-lane integration. Validates the source is currently visible in Berserk, then records a job for the discovery worker to drain. Use when a user says 'I added / connected / started shipping SOURCE'.", "inputSchema": {"type": "object", "properties": {"service": {"type": "string", "description": "service.name to integrate"}, "metric": {"type": "string", "description": "metric name to integrate"}, "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"}, "requested_by": {"type": "string", "description": "optional requester label"}, **_since()}}},
    {"name": "discovery_status", "description": "List pending and completed discovery jobs for new services or metrics.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "detect_new_sources", "description": "Scan Berserk for services/metrics never seen before (and optionally schema drift on known ones). Use for 'anything new reporting?', or run with auto_queue=true to queue newcomers for parser generation.", "inputSchema": {"type": "object", "properties": {"since": {"type": "string", "description": "Time window e.g. '24h ago'."}, "auto_queue": {"type": "boolean", "description": "queue newly-detected sources for parser generation"}, "check_drift": {"type": "boolean", "description": "also check known services for resource-key schema drift"}}}},
    {"name": "generate_parser", "description": "Generate and verify a query pack for one source right now (synchronous; may take minutes). An LLM authors 2-4 KQL queries from a live schema profile, validates each against Berserk, and saves the survivors. Requires at least one configured LLM provider (HERMES_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY).", "inputSchema": {"type": "object", "properties": {"service": {"type": "string", "description": "service.name to generate a parser for"}, "metric": {"type": "string", "description": "metric_name to generate a parser for"}, "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"}}}},
    {"name": "run_discovery_worker", "description": "Drain queued discovery jobs: for each one, an LLM authors a verified query pack for the new source. Requires at least one configured LLM provider; may take minutes per job.", "inputSchema": {"type": "object", "properties": {"max_jobs": {"type": "integer", "description": "max jobs to process this call, default 1, capped at 5"}}}},
    {"name": "review_generated", "description": "List or inspect LLM-generated saved queries for audit before trusting them. No arg: list all generated queries with their provider/model/timestamp. With name: full entry including the KQL.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "optional: a specific generated query name to inspect in full"}}}},
]


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
    "save_query": _WRITE_LOCAL,
    "list_saved": _READ_LOCAL,
    "request_discovery": _WRITE_LOCAL,
    "discovery_status": _READ_LOCAL,
    "detect_new_sources": _WRITE_EXTERNAL,
    "generate_parser": _WRITE_EXTERNAL,
    "run_discovery_worker": _WRITE_EXTERNAL,
    "review_generated": _READ_LOCAL,
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
    "trace_find_slow": "Trace: Find Slowest",
    "trace_find_errors": "Trace: Find Errors",
    "trace_analyze": "Trace: Analyze",
    "claude_recent": "Claude Code: Recent Activity",
    "claude_sessions": "Claude Code: Sessions",
    "claude_tools": "Claude Code: Tool Histogram",
    "claude_errors": "Claude Code: Tool Errors",
    "claude_search": "Claude Code: Full-Text Search",
    "claude_loop_check": "Claude Code: Loop Check",
    "claude_model_fit": "Claude Code: Model Fit",
    "claude_token_burn": "Claude Code: Token Burn",
    "claude_cost_report": "Claude Code: Cost Report",
    "claude_session_deep_dive": "Claude Code: Session Deep Dive",
    "claude_workflow_insights": "Claude Code: Workflow Insights",
    "scan_secrets": "SOC: Secret Scan",
    "suggest_ingestion": "Suggest Telemetry Ingestion",
    "list_saved": "List Saved Queries",
    "run_saved": "Run Saved Query",
    "save_query": "Save Query",
    "request_discovery": "Request Discovery",
    "discovery_status": "Discovery Status",
    "detect_new_sources": "Detect New Sources",
    "generate_parser": "Generate Parser",
    "run_discovery_worker": "Run Discovery Worker",
    "review_generated": "Review Generated Queries",
}


def annotations_for(name):
    """Read-only by default; only the two store-management tools differ."""
    return _ANNOTATIONS.get(name, _READ)


def _drain_pending_jobs(max_jobs):
    """Drain up to max_jobs pending discovery jobs through the parser
    factory pipeline. Mutates and persists the discovery queue. Shared by
    the run_discovery_worker MCP tool and the --worker CLI mode.

    Returns (outcome_lines, any_needs_human), or (None, False) if there was
    nothing pending -- callers render their own "no jobs" message so the
    MCP tool and the CLI can phrase it appropriately for their contexts.
    """
    queue = load_json_list(DISCOVERY_QUEUE_PATH)
    pending = [it for it in queue if it.get("status") == "pending"]
    if not pending:
        return None, False
    outcomes = []
    any_needs_human = False
    for job in pending[:max_jobs]:
        report, ok = parser_factory.generate_parser_for(job)
        if ok:
            job["status"] = "done"
            job["report"] = report.get("report", {})
            names = ", ".join(job["report"].get("queries_saved", []))
            outcomes.append(f"- {job['source']}: done ({names})")
        else:
            job["status"] = "needs_human"
            job["report"] = {
                "reason": report.get("reason"),
                "last_errors": report.get("last_errors", []),
            }
            outcomes.append(f"- {job['source']}: needs_human ({report.get('reason','')})")
            any_needs_human = True
    save_json_list(DISCOVERY_QUEUE_PATH, queue)
    return outcomes, any_needs_human


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
        # Require a real JSON boolean true — a string like "false" is truthy
        # in Python and must not authorize an overwrite.
        if is_amendment and arguments.get("overwrite") is not True:
            return (
                f"A saved query named '{nm}' already exists. Pass overwrite=true "
                f"to replace it (this will be logged)."
            ), True
        entry = {"name": nm, "description": desc, "kql": kql, "since": since}
        roles = normalize_roles(arguments.get("roles"))
        if roles:
            entry["roles"] = roles
        persist_learned_query(entry, action_source="manual")
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
        # Exact-match count, not a substring check against the raw output —
        # a short target would otherwise match as a substring of an unrelated
        # service name. `target` is allowlist-validated above, so it is safe
        # to interpolate into the single-quoted KQL literal.
        if kind == "service":
            check_kql = f"{T} | where resource['service.name'] == '{target}' | summarize n=count()"
        else:
            check_kql = f"{T} | where metric_name == '{target}' | summarize n=count()"
        visible, is_err = bzrk_search(check_kql, since)
        if is_err:
            return "Could not verify source visibility:\n" + visible, True
        if count_result_is_zero(visible):
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
        queue = queue[-500:]  # cap to prevent unbounded growth
        save_json_list(DISCOVERY_QUEUE_PATH, queue)
        return f"{target} queued for integration ({kind}). The author lane will author, verify, and save a query for it.", False
    if name == "discovery_status":
        items = load_json_list(DISCOVERY_QUEUE_PATH)
        if not items:
            return "No discovery jobs queued.", False
        lines = []
        for it in items:
            lines.append(
                f"- {it.get('source','?')} [{it.get('kind','?')}] status={it.get('status','?')} "
                f"role={it.get('role_hint','') or 'none'} requested_by={it.get('requested_by','?')} ts={it.get('ts','?')}"
            )
            report = it.get("report")
            if report:
                if "queries_saved" in report:
                    lines.append(f"  -> {report.get('provider','?')}: saved {', '.join(report.get('queries_saved', []))}")
                else:
                    lines.append(f"  -> {report.get('reason','')}")
        return "Discovery jobs:\n" + "\n".join(lines), False

    # --- parser-factory tools ---
    if name == "detect_new_sources":
        since = arguments.get("since") or "24h ago"
        auto_queue = arguments.get("auto_queue") is True
        check_drift = arguments.get("check_drift") is True
        text = parser_factory.detect_new_sources(
            since=since, auto_queue=auto_queue, check_drift=check_drift,
            load_json_list=load_json_list, save_json_list=save_json_list,
            discovery_queue_path=DISCOVERY_QUEUE_PATH, active_role=ACTIVE_ROLE,
        )
        return text, False
    if name == "generate_parser":
        service = str(arguments.get("service") or "").strip()
        metric = str(arguments.get("metric") or "").strip()
        if bool(service) == bool(metric):
            return "generate_parser needs exactly one of 'service' or 'metric'.", True
        target = service or metric
        if not re.match(r"^[A-Za-z0-9._-]+$", target):
            return "invalid source name (allowed: letters, digits, '.', '_', '-')", True
        kind = "service" if service else "metric"
        role_hint = normalize_roles(arguments.get("role_hint"))
        job = {
            "source": target, "kind": kind,
            "role_hint": role_hint[0] if role_hint else "",
        }
        report, ok = parser_factory.generate_parser_for(job)
        return json.dumps(report, indent=2), not ok
    if name == "run_discovery_worker":
        raw_max = arguments.get("max_jobs")
        try:
            max_jobs = int(raw_max) if raw_max is not None else 1
        except (TypeError, ValueError):
            max_jobs = 1
        max_jobs = max(1, min(max_jobs, 5))
        outcomes, any_needs_human = _drain_pending_jobs(max_jobs)
        if outcomes is None:
            return "No pending discovery jobs.", False
        return "\n".join(outcomes), any_needs_human
    if name == "review_generated":
        items = load_learned()
        generated = [it for it in items if "generated_by" in it]
        nm = arguments.get("name")
        if nm:
            nm = sanitize_name(nm)
            match = next((it for it in generated if it["name"] == nm), None)
            if not match:
                return f"No generated query named '{nm}'.", True
            return json.dumps(match, indent=2), False
        if not generated:
            return "No generated queries yet.", False
        lines = []
        for it in generated:
            gb = it.get("generated_by", {})
            lines.append(
                f"- {it['name']}: {it.get('description','')} "
                f"[{gb.get('provider','?')}/{gb.get('model','?')} @ {gb.get('ts','?')}]"
            )
        return "Generated queries:\n" + "\n".join(lines), False

    # --- simple fixed-query tools ---
    if name in SIMPLE:
        kql, default_since = SIMPLE[name]
        since = arguments.get("since") or default_since
        return bzrk_search(kql, since)

    if name == "soc_new_services":
        since = arguments.get("since") or "24h ago"
        out, err = bzrk_search(Q_SOC_NEW_SERVICES, since)
        if err:
            return out, True
        baseline = parser_factory.load_json_dict(parser_factory._known_sources_path())
        known = set(baseline.get("services", {}).keys())
        if not known:
            return (
                "(no baseline — run detect_new_sources first to establish "
                "known services; showing all active services)\n" + out
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
        return "\n".join(filtered), False

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
    if name == "trace_analyze":
        trace_id = arguments.get("trace_id")
        if not trace_id:
            return "missing required 'trace_id'", True
        if not re.match(r"^[A-Za-z0-9]+$", str(trace_id)):
            return "invalid trace_id (allowed: letters and digits only)", True
        # No time window on either half: a trace_id already scopes the query
        # tightly, and the trace could be older than any reasonable default
        # `since`. Two perspectives, like discover_schema: the span tree, then
        # any logs sharing the same trace_id — treated as a failure only if
        # BOTH halves fail, since a trace can legitimately have no logs.
        out1, e1 = bzrk_search(q_trace_analyze(str(trace_id)), "30d ago")
        out2, e2 = bzrk_search(q_trace_logs(str(trace_id)), "30d ago")
        return f"== spans ==\n{out1}\n\n== correlated logs ==\n{out2}", (e1 and e2)
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
    if name == "claude_loop_check":
        since = arguments.get("since") or "6h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_loop_check(since)
    if name == "claude_model_fit":
        since = arguments.get("since") or "6h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_model_fit(since)
    if name == "claude_token_burn":
        since = arguments.get("since") or "6h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_token_burn(since)
    if name == "claude_cost_report":
        since = arguments.get("since") or "7d ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_cost_report(
            since, group_by=arguments.get("group_by") or "day")
    if name == "claude_session_deep_dive":
        since = arguments.get("since") or "24h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_session_deep_dive(
            str(arguments.get("session_id") or ""), since)
    if name == "claude_workflow_insights":
        since = arguments.get("since") or "7d ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return agent_analytics.claude_workflow_insights(since)
    if name == "scan_secrets":
        since = arguments.get("since") or "1h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        include_entropy = arguments.get("include_entropy", False)
        if not isinstance(include_entropy, bool):
            return "'include_entropy' must be a boolean", True
        include_pii = arguments.get("include_pii") or []
        if not isinstance(include_pii, list) or any(
            item not in secret_scan.ALL_PII_TYPES for item in include_pii
        ):
            return (
                "'include_pii' must be a list containing only: "
                "email, ipv4, ipv6, credit_card"
            ), True
        return secret_scan.scan_secrets(
            since, include_entropy=include_entropy, pii_types=include_pii,
        )
    if name == "suggest_ingestion":
        role_or_usecase = arguments.get("role_or_usecase")
        if not isinstance(role_or_usecase, str) or not role_or_usecase.strip():
            return "missing required 'role_or_usecase'", True
        check_gap = arguments.get("check_gap", False)
        if not isinstance(check_gap, bool):
            return "'check_gap' must be a boolean", True
        since = arguments.get("since") or "24h ago"
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
        return ingestion_advisor.suggest_ingestion(
            role_or_usecase, check_gap=check_gap, since=since,
        )

    return "unknown tool: " + str(name), True


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
# A non-dict `params` (e.g. a list or string) hit the same AttributeError
# class as (1) the moment any branch called params.get(...); validated here
# too rather than per-branch.
def _is_object(value):
    return isinstance(value, dict)


def _jsonrpc_error(code, message, id_=None):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _valid_mcp_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


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
        return _dispatch_validated(method, params, id_, is_notification)
    except Exception as exc:
        log(f"dispatch failed: {type(exc).__name__}")
        if is_notification:
            return None
        return _jsonrpc_error(-32603, "Internal error", id_)


def _dispatch_validated(method, params, id_, is_notification):
    """Dispatch a validated request envelope to the appropriate handler."""
    def _reply(result):
        if is_notification:
            return None
        return _jsonrpc_result(id_, result)

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
        return _jsonrpc_result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "notifications/initialized":
        if not is_notification:
            return _jsonrpc_error(-32600, "Invalid Request", id_)
        if params:
            return None
        return None
    if method == "ping":
        if params:
            if is_notification:
                return None
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _reply({})
    if method == "tools/list":
        if params:
            if is_notification:
                return None
            return _jsonrpc_error(-32602, "Invalid params", id_)
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
        return _reply({"tools": tl})
    if method == "tools/call":
        if is_notification:
            return None
        name = params.get("name")
        if not name or not isinstance(name, str):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        arguments = arguments or {}
        text, is_err = handle_call(name, arguments)
        text = secret_scan.apply_output_filter(
            text,
            mode=REDACT_MODE,
            include_entropy=REDACT_ENTROPY,
            pii_types=REDACT_PII_TYPES,
        )
        return _jsonrpc_result(id_, {
            "content": [{"type": "text", "text": text}], "isError": is_err,
        })

    if is_notification:
        return None
    return _jsonrpc_error(-32601, "Method not found", id_)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _serve_mcp():
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
        except json.JSONDecodeError as e:
            log(f"bad json from client ({type(e).__name__})")
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            resp = dispatch(req)
        except Exception as e:  # pragma: no cover - defense in depth
            log(f"dispatch crashed: {type(e).__name__}")
            if isinstance(req, dict) and "id" in req and _valid_mcp_id(req["id"]):
                resp = _jsonrpc_error(-32603, "Internal error", req["id"])
            else:
                continue
        if resp is not None:
            send(resp)
    log("stdin closed")


def main():
    import argparse
    cli = argparse.ArgumentParser(
        prog="berserk-mcp",
        description="Berserk MCP observability server",
        add_help=True,
    )
    cli.add_argument("--worker", action="store_true",
                     help="run one headless discovery pass (for cron)")
    cli.add_argument("--agent-report", action="store_true",
                     help="run Claude Code agent analytics report")
    cli.add_argument("--auto-queue", action="store_true",
                     help="(worker) queue newly detected sources")
    cli.add_argument("--max-jobs", type=int, default=3,
                     help="(worker) max discovery jobs to drain")
    cli.add_argument("--check-drift", action="store_true",
                     help="(worker) check known services for schema drift")
    cli.add_argument("--since", default="6h ago",
                     help="(agent-report) time window")
    cli.add_argument("--set-hermes-url", metavar="URL",
                     help="persist the Hermes LLM endpoint and exit")
    ns = cli.parse_args()
    if ns.set_hermes_url:
        try:
            path = parser_factory.save_hermes_url(ns.set_hermes_url)
            print(f"Saved Hermes URL to {path} (0600). It overrides the "
                  f"localhost default; BERSERK_LLM_HERMES_URL still takes priority.")
            sys.exit(0)
        except Exception as e:
            print(f"failed to save Hermes URL: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.worker:
        sys.exit(run_worker_pass(
            auto_queue=ns.auto_queue,
            max_jobs=max(1, min(ns.max_jobs, 5)),
            check_drift=ns.check_drift,
        ))
    if ns.agent_report:
        sys.exit(run_agent_report(since=ns.since))
    _serve_mcp()


def run_worker_pass(auto_queue=False, max_jobs=3, check_drift=False):
    """One headless pass for cron/systemd: detect new sources, optionally
    queue them, then drain up to max_jobs pending discovery jobs. Prints a
    summary to stdout. Returns an exit code: 1 if any drained job ended
    needs_human, else 0. No loop, no daemon -- the caller (cron) owns the
    schedule.
    """
    detect_summary = parser_factory.detect_new_sources(
        since="24h ago", auto_queue=auto_queue, check_drift=check_drift,
        load_json_list=load_json_list, save_json_list=save_json_list,
        discovery_queue_path=DISCOVERY_QUEUE_PATH, active_role=ACTIVE_ROLE,
    )
    print(detect_summary)

    outcomes, any_needs_human = _drain_pending_jobs(max_jobs)
    if outcomes is None:
        print("No pending discovery jobs.")
        return 0
    for line in outcomes:
        print(line)
    return 1 if any_needs_human else 0


def run_agent_report(since="6h ago"):
    """One headless pass for cron/systemd: run Claude Code loop and
    model-fit checks, print the report, and return non-zero when an alertable
    condition is present.
    """
    if not valid_since(since):
        print(f"invalid --since value: {since!r}", file=sys.stderr)
        return 2
    text, should_alert = agent_analytics.agent_report(since)
    print(text)
    return 1 if should_alert else 0


if __name__ == "__main__":
    main()
