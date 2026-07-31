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
  BERSERK_MCP_KQL_VALIDATION validation policy: off/warn/strict (default: "warn")
  BERSERK_MCP_KQL_LIVE_VALIDATION enable validate_kql mode=live (default: "0")
  BERSERK_MCP_MAX_CONCURRENT_QUERIES in-process query concurrency (default: "2")
  BERSERK_MCP_KQL_MAX_CHARS maximum user KQL length (default: "50000")
  BERSERK_MCP_KQL_MAX_ROWS recommended arbitrary-query row bound (default: "2000")
  BERSERK_MCP_KQL_STATS stats handling: off/auto/required (default: "auto")
  BERSERK_MCP_MAX_RESULT_BYTES hard cap for bzrk stdout (default: 10485760)
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
import subprocess
import re
import os
import shutil
import threading
import time
import uuid
import urllib.error
import random
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import _http
import _store
import agent_analytics
import ai_finops
import ingestion_advisor
import kql_validation
import parser_factory
import schema_registry
import secret_scan

__version__ = "1.23.0"


def log(msg):
    print("[berserk-mcp] " + str(msg), file=sys.stderr, flush=True)


# ---------- configuration (env-overridable) ----------
_BZRK_BIN_CONFIG = os.environ.get("BZRK_BIN", "bzrk")


def _path_is_within(path, directory):
    try:
        Path(path).resolve(strict=False).relative_to(Path(directory).resolve(strict=False))
        return True
    except ValueError:
        return False


def _resolve_bzrk_binary(value, *, os_name=None, which=None, cwd=None):
    """Resolve the CLI once so subprocess never receives an unsafe bare name.

    Windows searches the current working directory before PATH for bare
    executable names.  Refuse that resolution unless the operator explicitly
    supplied an absolute path; an MCP client, not the operator, often controls
    the server's working directory.
    """
    configured = str(value or "bzrk").strip()
    if not configured:
        configured = "bzrk"
    platform_name = os.name if os_name is None else os_name
    resolver = shutil.which if which is None else which
    current_dir = Path.cwd() if cwd is None else Path(cwd)
    candidate = Path(configured)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        return str(resolved) if resolved.is_file() else None
    if "/" in configured or "\\" in configured:
        raise ValueError("BZRK_BIN must be an absolute path or a bare executable name")
    found = resolver(configured)
    if not found:
        return None
    resolved = Path(found).resolve(strict=False)
    if platform_name == "nt" and _path_is_within(resolved, current_dir):
        raise ValueError(
            "bare BZRK_BIN resolved inside the current working directory; "
            "set BZRK_BIN to the trusted executable's absolute path"
        )
    return str(resolved)


try:
    _RESOLVED_BZRK_BIN = _resolve_bzrk_binary(_BZRK_BIN_CONFIG)
except ValueError as _bzrk_resolution_error:
    sys.exit(f"berserk-mcp: invalid BZRK_BIN: {_bzrk_resolution_error}")
BZRK_BIN = _RESOLVED_BZRK_BIN or _BZRK_BIN_CONFIG
PROFILE = os.environ.get("BZRK_PROFILE", "local")
TABLE = os.environ.get("BERSERK_TABLE", "default")
DEFAULT_TIMEOUT = int(os.environ.get("BZRK_TIMEOUT", "120"))
ACTIVE_ROLE = os.environ.get("BERSERK_MCP_ROLE", "all").strip().lower() or "all"


def _nonnegative_float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        log(f"{name}={os.environ.get(name)!r} is invalid; using {default!r}.")
        return float(default)


def _nonnegative_int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        log(f"{name}={os.environ.get(name)!r} is invalid; using {default!r}.")
        return int(default)


def _choice_env(name, default, choices):
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        log(f"{name}={value!r} is invalid; using {default!r}.")
        return default
    return value


WORKER_JITTER_SECONDS = _nonnegative_float_env("BERSERK_WORKER_JITTER_SECONDS", 7200)
TOOL_BUDGET_SECONDS = min(
    _nonnegative_float_env("BERSERK_MCP_TOOL_BUDGET_SECONDS", 10),
    max(0.0, float(DEFAULT_TIMEOUT)),
)
# The base budget was calibrated on short windows (the fleet eval's latency
# sweep used 15-minute windows), but query cost on this engine grows with the
# scanned time range — a 72h aggregate that legitimately needs ~13s is not a
# runaway query, while 13s for a 15m window is. Scale the budget with the
# requested window instead of applying the short-window number to every call:
# effective = base + per_hour * window_hours, capped at BZRK_TIMEOUT. The
# default 0.5 s/h keeps a 15m window at the tight calibrated budget while a
# 72h window earns ~46s and a 7d cost report ~94s. Set to 0 to restore the
# flat budget.
BUDGET_PER_HOUR_SECONDS = _nonnegative_float_env(
    "BERSERK_MCP_BUDGET_PER_HOUR_SECONDS", 0.5
)

_SINCE_HOURS_FACTORS = {
    "s": 1 / 3600, "sec": 1 / 3600, "secs": 1 / 3600,
    "second": 1 / 3600, "seconds": 1 / 3600,
    "m": 1 / 60, "min": 1 / 60, "mins": 1 / 60,
    "minute": 1 / 60, "minutes": 1 / 60,
    "h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1,
    "d": 24, "day": 24, "days": 24,
    "w": 168, "wk": 168, "week": 168, "weeks": 168,
}


def _since_hours(since):
    """Window length in hours for a valid `since` string; 0.0 for 'now' or
    anything unparseable (unparseable values fail valid_since anyway)."""
    m = re.match(r"^(\d+)\s*([a-z]+?)(?:\s+ago)?$", str(since).strip(), re.IGNORECASE)
    if not m:
        return 0.0
    return float(m.group(1)) * _SINCE_HOURS_FACTORS.get(m.group(2).lower(), 0.0)


def _window_budget(base, since):
    """Effective per-query budget for this window, capped at BZRK_TIMEOUT."""
    if base is None or base <= 0:
        return base
    scaled = base + BUDGET_PER_HOUR_SECONDS * _since_hours(since)
    return min(scaled, max(base, float(DEFAULT_TIMEOUT)))
FAIL_COOLDOWN_SECONDS = _nonnegative_float_env("BERSERK_MCP_FAIL_COOLDOWN_SECONDS", 30)
CACHE_TTL_SECONDS = _nonnegative_float_env("BERSERK_MCP_CACHE_TTL_SECONDS", 120)
KQL_VALIDATION_MODE = _choice_env("BERSERK_MCP_KQL_VALIDATION", "warn", {"off", "warn", "strict"})
KQL_LIVE_VALIDATION = os.environ.get("BERSERK_MCP_KQL_LIVE_VALIDATION", "0").strip().lower() in {"1", "true", "yes", "on"}
MAX_CONCURRENT_QUERIES = _nonnegative_int_env("BERSERK_MCP_MAX_CONCURRENT_QUERIES", 2)
KQL_MAX_CHARS = _nonnegative_int_env("BERSERK_MCP_KQL_MAX_CHARS", 50000) or 50000
KQL_MAX_ROWS = _nonnegative_int_env("BERSERK_MCP_KQL_MAX_ROWS", 2000) or 2000
KQL_STATS_MODE = _choice_env("BERSERK_MCP_KQL_STATS", "auto", {"off", "auto", "required"})
MAX_BZRK_RESULT_BYTES = (
    _nonnegative_int_env("BERSERK_MCP_MAX_RESULT_BYTES", 10 * 1024 * 1024)
    or 10 * 1024 * 1024
)
FINOPS_REDACT_ENTROPY = os.environ.get(
    "BERSERK_MCP_FINOPS_REDACT_ENTROPY", "0"
).strip().lower() in {"1", "true", "yes", "on"}
_QUERY_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES) if MAX_CONCURRENT_QUERIES > 0 else None

# Fleet controls are deliberately in-process. An MCP stdio server is one
# agent session, so suppressing repeated work here addresses the retry storm
# without pretending that separate tenants share state.
_FLEET_LOCK = threading.RLock()
_RESULT_CACHE = {}
_FAIL_COOLDOWN = {}
_FLEET_CONTEXT = None
_FLEET_BACKEND_ID = None


def _reset_fleet_state():
    """Clear in-process fleet state (used by tests and controlled reloads)."""
    global _FLEET_BACKEND_ID
    with _FLEET_LOCK:
        _RESULT_CACHE.clear()
        _FAIL_COOLDOWN.clear()
        _FLEET_BACKEND_ID = None

# F-009: default to the safest output mode. An invalid mode string fails
# CLOSED to 'redact' (the strictest setting), not to the weaker 'flag'
# default this used to silently fall back to. Choosing 'off' or 'flag' is
# still fully supported -- it's just now an explicit, visible opt-in
# rather than the default, with a startup warning so an operator who
# didn't mean to weaken it notices immediately.
_redact_mode_env = os.environ.get("BERSERK_MCP_REDACT", "redact").strip().lower()
if _redact_mode_env not in {"off", "flag", "redact"}:
    log(
        f"BERSERK_MCP_REDACT={_redact_mode_env!r} is not a recognized mode "
        f"(off/flag/redact) -- defaulting to the safest mode, 'redact'."
    )
    REDACT_MODE = "redact"
else:
    REDACT_MODE = _redact_mode_env
    if REDACT_MODE in {"off", "flag"}:
        log(
            f"BERSERK_MCP_REDACT={REDACT_MODE!r}: secret/PII values in tool "
            f"output will NOT be fully redacted. This is an explicit "
            f"opt-in away from the safer default ('redact')."
        )

REDACT_ENTROPY = os.environ.get("BERSERK_MCP_REDACT_ENTROPY", "").strip().lower() in {
    "1", "true", "yes", "on",
}
REDACT_PII_TYPES = frozenset(
    item.strip().lower()
    for item in os.environ.get("BERSERK_MCP_REDACT_PII", "").split(",")
    if item.strip().lower() in secret_scan.ALL_PII_TYPES
)

# Discord alert bridge (--worker cron mode only; see run_worker_pass). Off by
# default -- only active if BERSERK_DISCORD_ALERT_SECRET is set. Posts to a
# local HTTP bridge (loopback by default, matching the same
# BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE opt-in convention as the LLM endpoint)
# rather than talking to Discord's API directly, so no Discord token or
# webhook secret needs to live in this process.
DISCORD_ALERT_URL = os.environ.get("BERSERK_DISCORD_ALERT_URL", "http://127.0.0.1:8765/alert")
DISCORD_ALERT_SECRET = os.environ.get("BERSERK_DISCORD_ALERT_SECRET", "")
DISCORD_ALERT_MAX_CHARS = 3800  # two bridge-side 1900-char chunks' worth


StorePathError = _store.StorePathError
_validate_store_path = _store.validate_store_path


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


def _optional_absolute_env_path(name, default):
    value = os.environ.get(name)
    return _validate_store_path(value, name) if value else Path(default)


FINOPS_BUSINESS_STORE_PATH = _optional_absolute_env_path(
    "BERSERK_MCP_BUSINESS_STORE_PATH",
    _default_learned_path().parent / "ai_finops_business.json",
)
FINOPS_DECISION_STORE_PATH = _optional_absolute_env_path(
    "BERSERK_MCP_RECOMMENDATION_STORE_PATH",
    _default_learned_path().parent / "ai_finops_recommendations.json",
)
FINOPS_PSEUDONYM_KEY_PATH = _default_learned_path().parent / "pseudonym.key"
FINOPS_REPORT_DIR = _optional_absolute_env_path(
    "BERSERK_MCP_REPORT_DIR",
    _default_learned_path().parent / "reports",
)
FINOPS_PRICING_CATALOG_PATH = _optional_absolute_env_path(
    "BERSERK_MCP_PRICING_CATALOG_PATH",
    Path(__file__).resolve().parent / "pricing_catalog.json",
)
FINOPS_OTLP_ENDPOINT = os.environ.get(
    "BERSERK_MCP_OTLP_LOGS_ENDPOINT",
    os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", ""),
).strip()
FINOPS_OTLP_HEADERS = os.environ.get(
    "BERSERK_MCP_OTLP_HEADERS",
    os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
).strip()
MCP_PROTOCOL_LEGACY = "2025-06-18"
MCP_PROTOCOL_MODERN = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_LEGACY, MCP_PROTOCOL_MODERN)
PROTOCOL_MODE_LEGACY = "legacy"
PROTOCOL_MODE_MODERN = "modern"
PROTOCOL_VERSION = MCP_PROTOCOL_LEGACY
MCP_PRIVATE_CACHE_TTL_MS = 300000
MCP_EXPENSIVE_SEARCH_WINDOW_HOURS = 24
MCP_TASK_TTL_SECONDS = 3600
MCP_MAX_TASKS = 64
MCP_TASK_EXTENSION_URI = "https://tasks.extensions.modelcontextprotocol.io"
MCP_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
MCP_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
MCP_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MCP_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
ENABLE_MCP_2026_07_28 = os.environ.get(
    "BERSERK_MCP_ENABLE_2026_07_28", ""
).strip().lower() in {"1", "true", "yes", "on"}
SERVER_INFO = {"name": "berserk-q", "title": "Berserk Query", "version": __version__}

_BASE_INSTRUCTIONS = (
    "Answer observability questions by calling these tools — do not write KQL by hand. "
    "Prefer the most specific tool (e.g. top_cpu, errors_by_service, logs_for_service, "
    "host_cpu) over the generic `search`. Per-host metrics (host_cpu, host_memory) and "
    "per-container metrics (top_cpu, top_memory) are different — pick by what's asked. "
    "Every query tool takes an optional `since` like '15m ago' or '2h ago'. For a "
    "recurring custom question, get it working with `search`, then `save_query` so it "
    "can be re-run deterministically with `run_saved`. If you do use `search`: fields "
    "are nested resource/log attributes, not flat columns — resource['service.name'], "
    "resource['host.name'], attributes['systemd.unit'], etc. A bare column name like "
    "service_name is not an error, it just silently matches zero rows — if a query you "
    "expect to match returns nothing, suspect the field access before assuming no data "
    "exists, and call discover_schema to check the real shape rather than guessing again."
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
    configured_dir = None
    if env_dir:
        try:
            configured_dir = _validate_store_path(env_dir, "BERSERK_MCP_PRIMERS_DIR")
        except StorePathError as exc:
            sys.exit(f"berserk-mcp: invalid BERSERK_MCP_PRIMERS_DIR: {exc}")
    if role not in _ROLE_PREFIX:
        return ""
    if configured_dir is not None:
        primer_path = configured_dir / f"{role}.md"
        try:
            if not primer_path.is_file():
                raise FileNotFoundError(primer_path)
            text = primer_path.read_text(encoding="utf-8")
        except OSError as exc:
            sys.exit(
                f"berserk-mcp: BERSERK_MCP_PRIMERS_DIR is configured but "
                f"{primer_path} is not readable: {type(exc).__name__}"
            )
        log(f"loaded {role} primer from {primer_path.resolve(strict=False)}")
        return text.strip() + "\n\n"
    search_dirs = [
        Path(__file__).parent / "primers",
        Path(sys.prefix) / "share" / "berserk-mcp" / "primers",
    ]
    for primer_dir in search_dirs:
        primer_path = primer_dir / f"{role}.md"
        try:
            text = primer_path.read_text(encoding="utf-8")
            log(f"loaded {role} primer from {primer_path.resolve(strict=False)}")
            return text.strip() + "\n\n"
        except OSError:
            continue
    return ""


def build_instructions(role: str) -> str:
    """Build initialize guidance for any role registered in ``_ROLE_PREFIX``."""
    return _load_primer(role) + _ROLE_PREFIX.get(role, "") + _BASE_INSTRUCTIONS


# F-008: fail fast on an unrecognized role rather than silently hiding
# every role-scoped tool. Without this, a typo in BERSERK_MCP_ROLE (e.g.
# "sre1") would make ACTIVE_ROLE match no entry in _ROLE_PREFIX, so
# tool_visible() would return True only for tools with no role tag at
# all -- an operator would see an almost-empty tool list with no
# indication why, rather than a clear startup error.
if ACTIVE_ROLE != "all" and ACTIVE_ROLE not in _ROLE_PREFIX:
    _valid_roles = ", ".join(sorted(list(_ROLE_PREFIX.keys()) + ["all"]))
    sys.exit(
        f"berserk-mcp: unknown BERSERK_MCP_ROLE={ACTIVE_ROLE!r}. "
        f"Valid roles: {_valid_roles}."
    )

INSTRUCTIONS = build_instructions(ACTIVE_ROLE)


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


_LOCK_STALE_SECONDS = _store.LOCK_STALE_SECONDS
_LOCK_TIMEOUT_SECONDS = _store.LOCK_TIMEOUT_SECONDS
_LOCK_RETRY_INTERVAL = _store.LOCK_RETRY_INTERVAL


def _FileLock(target_path):
    """Compatibility constructor for the shared store lock."""
    return _store.FileLock(
        target_path,
        stale_seconds=_LOCK_STALE_SECONDS,
        timeout_seconds=_LOCK_TIMEOUT_SECONDS,
        retry_interval=_LOCK_RETRY_INTERVAL,
    )


def _ensure_private_dir(path):
    return _store.ensure_private_dir(path, logger=log)


def load_json_list(path):
    return _store.load_json_list(path, logger=log)


_unique_tmp_path = _store.unique_tmp_path
_atomic_replace = _store.atomic_replace


def save_json_list(path, items):
    return _store.save_json_list(path, items, logger=log)


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
    f"| where attributes['state'] == 'used' "
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
    f"| make-series errors=count() default=0 on timestamp step 1m "
    f"by service=tostring(resource['service.name']) | take 120"
)
Q_SRE_HOST_HEADROOM = (
    f"{T} | where metric_name in ('system.cpu.load_average.1m', 'system.memory.usage') "
    f"| extend val = iff(metric_name == 'system.memory.usage', value / 1073741824.0, value), "
    f"unit = iff(metric_name == 'system.memory.usage', 'GB', 'load_avg') "
    f"| where metric_name == 'system.cpu.load_average.1m' or attributes['state'] == 'used' "
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
    f"| summarize hits=count(), last_seen=max(timestamp), "
    f"example=substring(min(tostring(body)), 0, 240) "
    f"by service=tostring(resource['service.name']), template=extract_log_template(tostring(body)) "
    f"| sort by hits desc | take 40"
)

# --- SOC Tier-A queries ---
Q_SOC_HIGH_SEV = (
    f"{T} | where isnotnull(body) | where severity_text in ('CRITICAL', 'FATAL', 'ERROR') "
    f"| project timestamp, severity_text, service=tostring(resource['service.name']), "
    f"body=substring(tostring(body), 0, 240) "
    f"| tail 60"
)
Q_SOC_LOG_SPIKE = (
    f"{T} | where isnotnull(body) "
    f"| make-series hits=count() default=0 on timestamp step 1m "
    f"by service=tostring(resource['service.name']) | take 60"
)
Q_SOC_NEW_SERVICES = (
    f"{T} | summarize first_seen=min(timestamp), last_seen=max(timestamp), events=count() "
    f"by service=tostring(resource['service.name']) "
    f"| sort by first_seen desc | take 40"
)
Q_SOC_REPEATED_ERRORS = (
    f"{T} | where isnotnull(body) | where severity_text == 'ERROR' "
    f"| summarize hits=count(), last_seen=max(timestamp), "
    f"example=substring(min(tostring(body)), 0, 240) "
    f"by template=extract_log_template(tostring(body)) "
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
    f"| tail 20"
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
        f"| tail 100"
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
    """Sample structural fields without exporting raw telemetry values."""
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    return (
        f"{T} {filt}| take 3 "
        f"| project resource_keys=bag_keys(resource), "
        f"attribute_keys=bag_keys(attributes), metric_name, "
        f"has_body=isnotempty(tostring(body)), "
        f"has_metric=isnotnull(metric_name), has_severity=isnotnull(severity_text)"
    )


def q_discover_fieldstats(service=None):
    """Bounded dynamic-field inventory for schema discovery.

    ``fieldstats`` reports field type, cardinality, and representative values
    without exporting the raw resource bag. Keep the row sample separate so
    callers can inspect value shape without widening the inventory result.
    """
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    return f"{T} {filt}| fieldstats resource with limit=50 depth=2"
Q_CC_RECENT = (
    f"{CC} | tail 60 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
    f"role=tostring(attributes['claude.message_role']), "
    f"model=tostring(attributes['claude.message_model']), "
    f"tools=tostring(attributes['claude.tool_names']), "
    f"err=tostring(attributes['claude.error'])"
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
    f"| tail 40 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
    f"tools=tostring(attributes['claude.tool_names']), "
    f"body=substring(tostring(body),0,220)"
)


def q_logs(svc: str) -> str:
    return (
        f"{T} | where isnotnull(body) | where resource['service.name'] == '{svc}' "
        f"| project timestamp, severity_text, body=substring(tostring(body), 0, 500) "
        f"| tail 50"
    )


def q_cc_search(term: str) -> str:
    return (
        f"{CC} | where tostring(body) contains '{term}' "
        f"| tail 40 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"body=substring(tostring(body),0,240)"
    )


MAX_INTERPOLATED_NAME_CHARS = 128
MAX_TRACE_ID_CHARS = 64
MAX_SEARCH_TERM_CHARS = 500
_SERVICE_RE = re.compile(r"[A-Za-z0-9._-]+")
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9]+")
_TEXT_GUARD_RE = re.compile(r"['\"|\\`\x00-\x1f\x7f]")
_FORECAST_METRICS = frozenset({
    "system.memory.usage", "system.filesystem.usage", "system.disk.io",
})


def _valid_interpolated_name(value, max_chars=MAX_INTERPOLATED_NAME_CHARS):
    text = str(value or "")
    return len(text) <= max_chars and bool(_SERVICE_RE.fullmatch(text))


def q_detect_anomalies(service=None):
    filt = ""
    if service:
        filt = f"| where resource['service.name'] == '{service}' "
    return (
        f"{T} {filt}| make-series events=count() default=0 on timestamp step 5m "
        f"by service=tostring(resource['service.name']) "
        f"| extend (anomalies, score, baseline)=series_decompose_anomalies(events) "
        f"| take 20"
    )


def q_forecast_capacity(metric, host=None):
    filt = f"| where resource['host.name'] == '{host}' " if host else ""
    state = "| where attributes['state'] == 'used' " if metric == "system.memory.usage" else ""
    return (
        f"{T} | where metric_name == '{metric}' {state}{filt}"
        f"| make-series value=avg(value) default=0 on timestamp step 1h "
        f"by host=tostring(resource['host.name']) "
        f"| extend fit=series_fit_line(value) | take 20"
    )


def q_find_similar(description, service=None, k=10):
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    return (
        f"{T} {filt}| where isnotnull(body) "
        f"| top {k} by body similarto \"{description}\""
    )


def _forecast_fit_rows(text):
    """Extract native ``series_fit_line`` coefficients from bzrk JSON.

    Berserk returns the fit as a dynamic array whose first two values are
    R² and slope (the same shape consumed by :mod:`agent_analytics`).  Keep
    this parser deliberately conservative: an unrecognised renderer is not
    treated as a reliable forecast.
    """
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)" or whole[0] not in "[{":
        return []
    try:
        records = agent_analytics._json_records(json.loads(whole))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not records:
        return []
    parsed = []
    for row in records:
        if not isinstance(row, dict):
            continue
        fit = row.get("fit")
        if not isinstance(fit, list) or len(fit) < 2:
            continue
        try:
            r2, slope = float(fit[0]), float(fit[1])
        except (TypeError, ValueError):
            continue
        parsed.append({"host": str(row.get("host") or "(all hosts)"), "r2": r2, "slope": slope})
    return parsed


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

# F-005/SR-17: bound both diagnostics and successful output while the child
# is still running. Row limits do not bound wide rows, and capture_output
# buffers an entire stream before this process can inspect it.
MAX_BZRK_DIAGNOSTIC_CHARS = 100_000
_PROCESS_READ_CHUNK = 64 * 1024


def _run_argv_bounded(argv, timeout, stdout_cap=MAX_BZRK_RESULT_BYTES,
                      stderr_cap=MAX_BZRK_DIAGNOSTIC_CHARS):
    """Run argv without a shell, bounding captured bytes before decoding.

    Two readers drain stdout and stderr concurrently to avoid pipe deadlocks.
    stdout overflow terminates and reaps the child; stderr is retained only up
    to its diagnostic cap while the remainder is discarded until completion.
    """
    process = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=False,
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    caps = {"stdout": max(1, int(stdout_cap)), "stderr": max(1, int(stderr_cap))}
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    reader_errors = []

    def drain(name, stream):
        try:
            with stream:
                while True:
                    chunk = stream.read(_PROCESS_READ_CHUNK)
                    if not chunk:
                        break
                    remaining = caps[name] - len(buffers[name])
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                    if len(chunk) > max(0, remaining):
                        if name == "stdout":
                            stdout_overflow.set()
                        else:
                            stderr_overflow.set()
        except Exception as exc:  # pragma: no cover - defensive OS pipe failure
            reader_errors.append(exc)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + max(0.0, float(timeout))
    timed_out = False
    while process.poll() is None:
        if stdout_overflow.is_set():
            try:
                process.kill()
            except OSError:  # pragma: no cover - child exited between poll and kill
                pass
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                process.kill()
            except OSError:  # pragma: no cover - child exited between poll and kill
                pass
            break
        stdout_overflow.wait(min(0.05, remaining))
    process.wait()
    for thread in threads:
        thread.join(timeout=2)
    if reader_errors:
        raise reader_errors[0]
    if timed_out:
        raise subprocess.TimeoutExpired(list(argv), timeout)
    return {
        "returncode": process.returncode,
        "stdout": bytes(buffers["stdout"]),
        "stderr": bytes(buffers["stderr"]),
        "stdout_overflow": stdout_overflow.is_set(),
        "stderr_overflow": stderr_overflow.is_set(),
    }


def run_bzrk(args, timeout=DEFAULT_TIMEOUT):
    """Run the bzrk CLI with the given argument list. Returns (text, is_error)."""
    if _RESOLVED_BZRK_BIN is None:
        return (
            f"error: '{_BZRK_BIN_CONFIG}' not found on PATH. Install the Berserk CLI or set "
            "BZRK_BIN to its full path."
        ), True
    try:
        result = _run_argv_bounded([_RESOLVED_BZRK_BIN] + list(args), timeout)
        out = result["stdout"].decode("utf-8", errors="replace").strip()
        err = result["stderr"].decode("utf-8", errors="replace").strip()
        if err and _AUTH_FAILURE_RE.search(err):
            return AUTH_FAILURE_MESSAGE, True
        if result["stdout_overflow"]:
            return (
                f"bzrk result exceeded BERSERK_MCP_MAX_RESULT_BYTES="
                f"{MAX_BZRK_RESULT_BYTES}; narrow the time window, project fewer "
                "columns, or add a smaller take/top/tail bound."
            ), True
        if result["returncode"] != 0:
            diagnostic = (out + "\n" + err).strip() or f"bzrk exited {result['returncode']}"
            if len(diagnostic) > MAX_BZRK_DIAGNOSTIC_CHARS or result.get("stderr_overflow"):
                diagnostic = diagnostic[:MAX_BZRK_DIAGNOSTIC_CHARS] + "\n...[truncated]"
            return diagnostic, True
        return (out or "(no rows)"), False
    except FileNotFoundError:
        return (
            f"error: '{_BZRK_BIN_CONFIG}' not found on PATH. Install the Berserk CLI or set "
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
_KQL_CONTROL_RE = re.compile(r"^\s*\.")


_BZRK_TIMEOUT_TEXT_RE = re.compile(r"^bzrk timed out after ", re.IGNORECASE)


def bzrk_search(kql, since, extra=None):
    """Run a KQL search on the configured profile and time window. `extra` adds
    trailing CLI flags (e.g. ['--json']) without duplicating the guards."""
    query = str(kql)
    if ";" in query:
        return "invalid KQL: semicolons are not allowed in user queries", True
    if _KQL_CONTROL_RE.match(query):
        return "invalid KQL: control commands are not allowed in user queries", True
    if not _KQL_PREFIX_RE.match(query):
        return (
            f"invalid KQL: query must start with '{TABLE} | ...' "
            f"(got: {query[:40]!r})"
        ), True
    if not valid_since(since):
        return (
            f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
            f"'2d ago', or 'now'."
        ), True
    timeout = None
    tool_name = None
    if _FLEET_CONTEXT is not None:
        timeout = _window_budget(_FLEET_CONTEXT.get("budget"), since)
        tool_name = _FLEET_CONTEXT.get("tool")
    effective_timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    with _query_semaphore_slot(effective_timeout) as acquired:
        if not acquired:
            return (
                "Local MCP query queue is full. Retry later, use a narrower 'since' "
                "window, or raise BERSERK_MCP_MAX_CONCURRENT_QUERIES if this process "
                "is intentionally serving more parallel callers.",
                True,
            )
        if timeout is None:
            out, is_err = run_bzrk(
                ["-P", PROFILE, "search", query, "--since", since] + list(extra or [])
            )
        else:
            out, is_err = run_bzrk(
                ["-P", PROFILE, "search", query, "--since", since] + list(extra or []),
                timeout=timeout,
            )
    if is_err and _BZRK_TIMEOUT_TEXT_RE.match(str(out or "")) and tool_name:
        return (
            f"{tool_name} exceeded its {timeout:g}s query budget for window {since!r}. "
            "Retry with a narrower 'since' window, or raise "
            "BERSERK_MCP_TOOL_BUDGET_SECONDS / BERSERK_MCP_BUDGET_PER_HOUR_SECONDS "
            "if this cluster is legitimately slower.",
            True,
        )
    return out, is_err


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


def _schema_fetcher():
    out_tables, _ = run_bzrk(["-P", PROFILE, "search", ".show tables"])
    out_schema, _ = run_bzrk(["-P", PROFILE, "search", f"{T} | getschema", "--since", "1h ago"])
    out_fields, _ = run_bzrk(["-P", PROFILE, "search", q_discover_fieldstats(None), "--since", "1h ago"])
    out_sample, _ = run_bzrk(["-P", PROFILE, "search", q_discover_sample(None), "--since", "1h ago"])
    return {
        "tables": out_tables,
        "getschema": out_schema,
        "fieldstats": out_fields,
        "sample": out_sample,
        "supported_idioms": [
            "tail", "take", "top", "summarize", "make-series", "fieldstats",
            "series_decompose_anomalies", "series_fit_line", "similarto",
        ],
    }


def _schema_snapshot(force=False, allow_refresh=True):
    return schema_registry.get_schema_snapshot(
        force=force,
        table=TABLE,
        config_dir=Path(LEARNED_PATH).parent,
        fetcher=_schema_fetcher if allow_refresh else None,
    )


def _validation_schema(use_schema=True, allow_refresh=True):
    if not use_schema:
        return None, None, {"schema_status": "disabled"}
    try:
        snapshot = _schema_snapshot(force=False, allow_refresh=allow_refresh)
        fields = schema_registry.schema_fields(snapshot)
        info = {
            "schema_hash": snapshot.get("schema_hash"),
            "schema_status": snapshot.get("source_status", "unavailable"),
            "table": snapshot.get("table", TABLE),
        }
        return snapshot, fields, info
    except Exception as e:
        log(f"schema validation unavailable: {type(e).__name__}: {e}")
        return None, None, {"schema_status": "unavailable"}


def _validate_user_kql(kql, since, *, use_schema=True, allow_refresh_schema=True):
    base_report = kql_validation.validate_kql_static(
        str(kql or ""),
        table=TABLE,
        since=str(since or ""),
        schema_fields=None,
        max_chars=KQL_MAX_CHARS,
        max_rows=KQL_MAX_ROWS,
        schema_info={"schema_status": "not_checked"},
    )
    if any(f.get("severity") == "error" for f in base_report.get("findings", [])) or not use_schema:
        return base_report
    snapshot, fields, info = _validation_schema(use_schema=use_schema, allow_refresh=allow_refresh_schema)
    report = kql_validation.validate_kql_static(
        str(kql or ""),
        table=TABLE,
        since=str(since or ""),
        schema_fields=fields,
        max_chars=KQL_MAX_CHARS,
        max_rows=KQL_MAX_ROWS,
        schema_info=info,
        suggest=(lambda field: schema_registry.suggest_field(field, snapshot)) if snapshot else None,
    )
    return report


def _blocking_validation(report, *, persistence=False):
    if any(f.get("severity") == "error" for f in report.get("findings", [])):
        return True
    if KQL_VALIDATION_MODE == "strict" and report.get("risk") == "high":
        return True
    if persistence and report.get("risk") == "high":
        return True
    return False


def _format_validation_rejection(report):
    finding = next((f for f in report.get("findings", []) if f.get("severity") == "error"), None)
    if finding is None:
        finding = (report.get("findings") or [{"code": "HIGH_RISK", "message": "high-risk query"}])[0]
    prefix = "invalid KQL: " if finding.get("code") == "WRONG_TABLE" else ""
    return (
        f"{prefix}KQL rejected ({finding.get('code')}): {finding.get('message')} "
        f"Estimated risk: {report.get('risk')}."
    )


def _format_validation_warnings(report):
    warnings = [f for f in report.get("findings", []) if f.get("severity") != "error"]
    if not warnings:
        return ""
    return "KQL validation warnings (risk=%s):\n" % report.get("risk") + "\n".join(
        f"- {f.get('code')}: {f.get('message')}" for f in warnings[:8]
    )


def _query_semaphore_acquire(timeout):
    if _QUERY_SEMAPHORE is None:
        return True
    try:
        wait = max(0.0, float(timeout if timeout is not None else DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        wait = float(DEFAULT_TIMEOUT)
    return _QUERY_SEMAPHORE.acquire(timeout=wait)


def _query_semaphore_release(acquired):
    if acquired and _QUERY_SEMAPHORE is not None:
        _QUERY_SEMAPHORE.release()


@contextmanager
def _query_semaphore_slot(timeout):
    acquired = _query_semaphore_acquire(timeout)
    try:
        yield acquired
    finally:
        _query_semaphore_release(acquired)


def _parser_static_validation(kql, since):
    return _validate_user_kql(kql, since, use_schema=True)


def _parser_schema_context():
    snapshot = _schema_snapshot(force=False)
    return (
        schema_registry.schema_context(snapshot, max_chars=12000),
        snapshot.get("schema_hash", ""),
        snapshot.get("source_status", "unavailable"),
    )


# ---------- learned-query store ----------
def load_learned():
    return _store.load_json_list(LEARNED_PATH, logger=log)


def save_learned(items):
    _validate_store_path(LEARNED_PATH, "LEARNED_PATH")
    return _store.save_json_list(LEARNED_PATH, items, logger=log)


def sanitize_name(n):
    n = re.sub(r"[^a-zA-Z0-9_]+", "_", str(n).strip().lower()).strip("_")
    return n or "query"


def _make_room(existing_items, room_needed, protect_human):
    """Evict `room_needed` entries from `existing_items` (oldest first) to
    make room for a new entry that will be appended separately by the
    caller -- this list never includes that new entry, so it can never be
    the one evicted (F-006).

    protect_human=True (a generated write): only origin=='generated'
    entries are eligible for eviction -- a generated write must never
    knock a human entry out of the store just because the store happens
    to be at capacity. Raises ValueError if room_needed still can't be
    met, i.e. the store is saturated with human entries and there is
    nothing a generated write is allowed to remove; the caller must not
    have persisted anything at that point.

    protect_human=False (a manual/human write): unchanged prior behavior
    -- simple oldest-first eviction regardless of origin. A human write is
    always allowed to make room for itself.
    """
    if room_needed <= 0:
        return existing_items
    kept = list(existing_items)
    i = 0
    evicted = 0
    while evicted < room_needed and i < len(kept):
        if not protect_human or kept[i].get("origin") == "generated":
            del kept[i]
            evicted += 1
        else:
            i += 1
    if evicted < room_needed:
        raise ValueError(
            "cannot persist generated query: learned-query store is at "
            "capacity with human entries; a generated write must not "
            "evict a human entry to make room"
        )
    return kept


LEARNED_STORE_CAP = 500


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
    # F-007: the whole load-modify-save cycle is one critical section --
    # locking only around save_learned() would still let two concurrent
    # callers both read the same stale all_items, compute independently,
    # and have the second one's atomic replace silently discard the
    # first's update.
    with _FileLock(LEARNED_PATH):
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
        room_needed = (len(items) + 1) - LEARNED_STORE_CAP
        if room_needed > 0:
            items = _make_room(items, room_needed, protect_human=(action_source == "generated"))
        items.append(entry)
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
    with _FileLock(amendments_path):
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
    validate_static=_parser_static_validation,
    schema_context_provider=_parser_schema_context,
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
ai_finops.configure(
    search=bzrk_search_json,
    table=TABLE,
    redact=lambda text: secret_scan.redact(
        text, include_entropy=False, pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
    redact_aggressive=lambda text: secret_scan.redact(
        text, include_entropy=FINOPS_REDACT_ENTROPY,
        pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
    catalog_path=FINOPS_PRICING_CATALOG_PATH,
    business_store_path=FINOPS_BUSINESS_STORE_PATH,
    decision_store_path=FINOPS_DECISION_STORE_PATH,
    pseudonym_key_path=FINOPS_PSEUDONYM_KEY_PATH,
    report_dir=FINOPS_REPORT_DIR,
    otlp_endpoint=FINOPS_OTLP_ENDPOINT,
    otlp_headers=FINOPS_OTLP_HEADERS,
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


_STRUCTURED_OUTPUT_TOOLS = frozenset({
    "claude_spend_overview",
    "claude_feature_cost",
    "claude_project_economics",
    "claude_efficiency_insights",
    "claude_harness_recommendations",
    "claude_optimization_impact",
    "claude_management_report",
    "claude_generate_dashboard",
})


_TASK_ELIGIBLE_TOOLS = frozenset({
    "generate_parser",
    "run_discovery_worker",
    "claude_generate_dashboard",
})


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
    {"name": "logs_for_service", "description": "Recent log lines for a specific service e.g. 'nginx', 'postgres'. Use for 'show me the errors/logs from X' — returns actual log text. For error COUNTS across all services, use errors_by_service first, then drill into a specific service here.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "service.name value"}}, **_since()), "required": ["service"]}},
    {"name": "schema", "description": "Show Berserk tables + column schema (live introspection).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_metrics", "description": "List every metric name currently being ingested, with sample counts + last-seen. Use to DISCOVER what telemetry exists before writing a `search` query.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "bzrk_query_perf", "description": "Berserk query engine latency percentiles: p50, p95, p99 in µs. Use for 'how fast is Berserk?', 'query latency', or 'p50/p95/p99 execution time'. Uses otel_histogram_percentile($raw, N) — the native Berserk histogram aggregate.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "discover_schema", "description": "Discover the shape of a data source: returns (1) every key present under `resource` with row counts, AND (2) a small structural sample with resource/attribute keys and body/metric presence flags. It never exports raw resource, attributes, or body values. Use to learn an unknown or newly-ingested source before querying it. Optional `service` filter. Pair with list_services / list_metrics. Once you work out a query with `search`, persist it with save_query so it becomes reusable.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "optional: limit to one service.name"}}, **_since())}},
    {"name": "validate_kql", "roles": ["sre", "soc", "claude", "ops"], "description": "Validate custom Berserk KQL before saving or running it. Static mode does not contact Berserk except for cached schema context; live mode is opt-in, executes a bounded read-only query, and may consume query budget.", "inputSchema": {"type": "object", "properties": dict({"kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'."}, "mode": {"type": "string", "enum": ["static", "live"], "default": "static"}, "use_schema": {"type": "boolean", "default": True, "description": "Use cached/discovered schema for unknown-field checks."}}, **_since()), "required": ["kql"]}},
    {"name": "search", "description": "Run an arbitrary Kusto/KQL query against the Berserk table. Use when the other tools do not fit; once it works, persist it with save_query. Fields are nested OTLP resource/log attributes, NOT flat columns — access as resource['service.name'], resource['host.name'], attributes['systemd.unit'], etc. (bare service_name/host_name do not exist and silently match zero rows instead of erroring). If you don't already know the exact field names for this source, call discover_schema first instead of guessing.", "inputSchema": {"type": "object", "properties": dict({"kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'. Field access is resource['key'] / attributes['key'], never a bare column name."}}, **_since()), "required": ["kql"]}},
    {"name": "detect_anomalies", "roles": ["sre", "soc"], "description": "Statistical anomaly detection for service event volume over time. Uses zero-filled make-series and series_decompose_anomalies; use for 'is anything behaving abnormally?' rather than guessing a threshold. Optional service filter.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "optional service.name filter"}}, **_since())}},
    {"name": "find_similar", "roles": ["sre", "soc"], "description": "Find log messages by meaning rather than exact text, for example 'database timeouts' or 'authentication failures'. Semantic indexing must be enabled on the Berserk cluster; use search with has for exact terms. Optional service filter and k (1-50).", "inputSchema": {"type": "object", "properties": dict({"description": {"type": "string", "maxLength": 500, "description": "natural-language description; quotes, pipes, backslashes, backticks, and controls are rejected"}, "service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "optional service.name filter"}, "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, **_since()), "required": ["description"]}},
    # --- Trace tools (span-level latency/error triage; UNVERIFIED field names — see the
    # comment above Q_TRACE_FIND_SLOW. Descriptions below flag this to the model too.) ---
    {"name": "trace_find_slow", "description": "Find the highest-duration root spans in the time window. Use for 'what's slow', 'find the slowest requests', or as the entry point before trace_analyze.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "trace_find_errors", "description": "Find spans whose status indicates an error. Use for 'which requests failed' or as the entry point before trace_analyze.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "trace_analyze", "description": "Full breakdown of one trace by trace_id — every span in time order plus correlated log lines from the same trace_id. Use after trace_find_slow/trace_find_errors surface a trace_id worth investigating.", "inputSchema": {"type": "object", "properties": {"trace_id": {"type": "string", "maxLength": MAX_TRACE_ID_CHARS, "description": "trace_id from trace_find_slow/trace_find_errors/search"}}, "required": ["trace_id"]}},
    # --- SRE role tools (reliability, headroom, saturation, error rates, rollback signals) ---
    {"name": "sre_error_rate", "roles": ["sre"], "description": "SRE view of ERROR log events grouped by service and minute. Use for 'is the error rate climbing', 'which service is burning error budget', or 'what should we rollback first'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "forecast_capacity", "roles": ["sre"], "description": "Forecast when an allowlisted host gauge may reach its ceiling using a native series fit. Use for 'when will memory fill?' or 'at this trend when does capacity run out?'. Refuses unreliable trends instead of inventing a date.", "inputSchema": {"type": "object", "properties": dict({"metric": {"type": "string", "enum": sorted(_FORECAST_METRICS)}, "host": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "optional host.name filter"}}, **_since()), "required": ["metric"]}},
    {"name": "sre_host_headroom", "roles": ["sre"], "description": "SRE view of host CPU load and memory used side-by-side. Use for 'which host is hottest', 'where is headroom lowest', or 'which VM is nearest saturation'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "sre_ingest_health", "roles": ["sre"], "description": "SRE view of Berserk ingest lag and dropped-data signals per host. Use for 'is ingest healthy', 'are we dropping telemetry', or 'is observability lagging'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "sre_service_health", "roles": ["sre"], "description": "SRE health rollup for one service: total events, error count, logs, metrics, last seen. Use for 'is service X healthy' or 'rollback signal for X'.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "service.name value"}}, **_since()), "required": ["service"]}},
    {"name": "sre_top_error_messages", "roles": ["sre"], "description": "SRE summary of the most repeated error messages by service. Use for 'what error is dominating', 'top error signatures', or 'which message to investigate first'.", "inputSchema": {"type": "object", "properties": _since()}},
    # --- SOC role tools (anomalies, spikes, first-seen, repeated failures, incident timelines) ---
    {"name": "soc_high_severity_logs", "roles": ["soc"], "description": "SOC view of recent CRITICAL/FATAL/ERROR logs with service and message text. Use for 'show critical events', 'recent incident logs', or 'what looks severe right now'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_log_spike", "roles": ["soc"], "description": "SOC view of services with the largest log volume per minute. Use for 'anything anomalous', 'which source is spiking', or 'suspicious burst of logs'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_new_services", "roles": ["soc"], "description": "SOC view of services ordered by first-seen time. Use for 'what is new', 'anything first-seen', or 'did a new source appear'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_repeated_errors", "roles": ["soc"], "description": "SOC view of error messages that appear more than 5 times — potential probes, loops, or persistent incidents. Use for 'what keeps repeating' or 'show recurring failures'.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "soc_timeline", "roles": ["soc"], "description": "SOC incident timeline for one service: timestamps, severity, metric names, and message snippets. Use for 'timeline for service X' or 'reconstruct incident for X'.", "inputSchema": {"type": "object", "properties": dict({"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "service.name value"}}, **_since()), "required": ["service"]}},
    # --- Claude Code activity (service.name == 'claude-code'); low-volume, keep windows bounded ---
    {"name": "claude_recent", "roles": ["claude"], "description": "Recent Claude Code activity (timestamp, type, role, model, tool names, error flag), newest first. Default window 1h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_sessions", "roles": ["claude"], "description": "Claude Code sessions rollup: events, first/last seen, assistant turns, tool turns, and error count per session. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_tools", "roles": ["claude"], "description": "Claude Code tool-use histogram — how many times each tool (Bash, Edit, Read, ...) was used. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_errors", "roles": ["claude"], "description": "Claude Code tool errors — failed tool results (is_error=true) with a body snippet. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_search", "roles": ["claude"], "description": "Full-text search across Claude Code message and tool bodies for a substring. Default 6h.", "inputSchema": {"type": "object", "properties": dict({"term": {"type": "string", "maxLength": MAX_SEARCH_TERM_CHARS, "description": "substring to find; may not contain quotes, pipe, backslash, backtick, or controls"}}, **_since()), "required": ["term"]}},
    {"name": "claude_loop_check", "roles": ["claude"], "description": "Claude Code loop detector. Heuristically flags sessions that repeat the same tool/target, retry errors, or oscillate between the same calls. Bodies are truncated; output is diagnostic, not raw transcript replay. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_model_fit", "roles": ["claude"], "description": "Claude Code model-fit heuristic. Uses observed tool count, errors, duration, and loop signals to flag frontier models on trivial work or cheap models on complex/repetitive work. Not a billing statement. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_token_burn", "roles": ["claude"], "description": "Claude Code token-burn analysis. Uses exact claude.tokens_input/output usage when present, falls back to a labeled body-length estimate per session, computes burn per distinct tool/file target, and joins high burn with loop signals. Default 6h.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_cost_report", "roles": ["claude"], "description": "Claude Code multi-day cost report: per-day token burn with exact/estimated labeling, per-model split, optional per-project attribution from file paths, and a burn-growing/flat/declining trend verdict. Default 7d.", "inputSchema": {"type": "object", "properties": dict({"group_by": {"type": "string", "enum": ["day", "model", "project"], "description": "Aggregation: by day (default), model, or inferred project."}}, **_since())}},
    {"name": "claude_session_deep_dive", "roles": ["claude"], "description": "Timeline drilldown for one Claude Code session: contiguous tool phases with error counts, activity gaps over 5 minutes, cumulative token burn (exact/estimated), and a loop verdict. Requires session_id (find them via claude_sessions).", "inputSchema": {"type": "object", "properties": dict({"session_id": {"type": "string", "maxLength": agent_analytics.MAX_SESSION_ID_CHARS, "description": "claude.session_id value"}}, **_since()), "required": ["session_id"]}},
    {"name": "claude_workflow_insights", "roles": ["claude"], "description": "Cross-session Claude Code workflow patterns: most common tool sequences, error hotspots by tool+target, and top-decile burn-per-target sessions. Use for 'how is my agent working overall?'. Default 7d.", "inputSchema": {"type": "object", "properties": _since()}},
    {"name": "claude_spend_overview", "roles": ["claude"], "description": "Enterprise Claude spend overview using exact native/legacy token classes and a versioned public pricing catalog. Groups by day, team, portfolio, project, repository, feature, work item, agent, harness, or model and always reports pricing/attribution coverage.", "inputSchema": {"type": "object", "properties": {"since": _since()["since"], "group_by": {"type": "string", "enum": ["day", "team", "portfolio", "project", "repository", "feature", "work_item", "agent", "harness", "model"], "default": "day"}, "team": {"type": "string"}, "project": {"type": "string"}, "repository": {"type": "string"}, "feature": {"type": "string"}, "agent": {"type": "string"}, "harness": {"type": "string"}, "model": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}}},
    {"name": "claude_feature_cost", "roles": ["claude"], "description": "Feature delivery economics: planned/actual developer hours, planned/actual AI API-equivalent cost, forecast, attribution, and delivery signals for one governed feature.", "inputSchema": {"type": "object", "properties": {"feature_id": {"type": "string"}, "since": _since()["since"]}, "required": ["feature_id"]}},
    {"name": "claude_project_economics", "roles": ["claude"], "description": "Project and codebase economics across governed features: developer hours, AI cost, budget, attribution, and feature-level breakdown.", "inputSchema": {"type": "object", "properties": {"project_id": {"type": "string"}, "since": _since()["since"]}, "required": ["project_id"]}},
    {"name": "claude_efficiency_insights", "roles": ["claude"], "description": "Matched-cohort agent/harness efficiency analysis for cache reuse, context size, tool-result volume, retries, errors, model fit, and cost per successful outcome.", "inputSchema": {"type": "object", "properties": {"since": _since()["since"], "project": {"type": "string"}, "agent": {"type": "string"}, "harness": {"type": "string"}, "model": {"type": "string"}}}},
    {"name": "claude_harness_recommendations", "roles": ["claude"], "description": "Generate deterministic, evidence-backed harness amendments. Only findings with sufficient samples/confidence are approval-eligible; this tool never modifies a harness.", "inputSchema": {"type": "object", "properties": {"since": _since()["since"], "project": {"type": "string"}, "agent": {"type": "string"}, "harness": {"type": "string"}, "model": {"type": "string"}}}},
    {"name": "claude_record_recommendation_decision", "roles": ["claude"], "description": "Record an approved, rejected, or deferred harness recommendation as a privacy-safe append-only audit event. Does not apply the amendment.", "inputSchema": {"type": "object", "properties": {"recommendation_id": {"type": "string", "pattern": "^rec_[a-f0-9]{16}$"}, "decision": {"type": "string", "enum": ["approved", "rejected", "deferred"]}, "owner": {"type": "string", "description": "Owner identity; stored only as a deployment-scoped HMAC pseudonym."}, "rationale": {"type": "string", "maxLength": 1000}}, "required": ["recommendation_id", "decision", "owner", "rationale"]}},
    {"name": "claude_optimization_impact", "roles": ["claude"], "description": "Compare matched pre/post harness cohorts and return keep, no-material-change, rollback, or insufficient-data using cost, error, and success signals.", "inputSchema": {"type": "object", "properties": {"agent_profile": {"type": "string"}, "before_harness": {"type": "string"}, "after_harness": {"type": "string"}, "project": {"type": "string"}, "since": _since()["since"]}, "required": ["agent_profile", "before_harness", "after_harness"]}},
    {"name": "claude_management_report", "roles": ["claude"], "description": "Management-ready portfolio, team, project, or feature report with readable text and a schema-versioned JSON envelope.", "inputSchema": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["portfolio", "team", "project", "feature"], "default": "portfolio"}, "identifier": {"type": "string"}, "since": _since()["since"]}}},
    {"name": "claude_generate_dashboard", "roles": ["claude"], "description": "Generate a privacy-safe Markdown or self-contained HTML dashboard beneath BERSERK_MCP_REPORT_DIR for use from Claude Code. This is an explicit local write.", "inputSchema": {"type": "object", "properties": {"dashboard": {"type": "string", "enum": ["portfolio", "project", "feature", "agent_efficiency", "data_quality"], "default": "portfolio"}, "identifier": {"type": "string"}, "since": _since()["since"], "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown"}, "filename": {"type": "string", "maxLength": 128}}}},
    {"name": "scan_secrets", "roles": ["soc"], "description": "Audit recent log bodies for potential credentials and optionally selected PII categories. Returns only aggregate service/type counts and first-seen timestamps; secret values are never returned. Default 1h.", "inputSchema": {"type": "object", "properties": {"since": _since()["since"], "include_entropy": {"type": "boolean", "description": "Enable false-positive-prone high-entropy token detection."}, "include_pii": {"type": "array", "items": {"type": "string", "enum": ["email", "ipv4", "ipv6", "credit_card"]}, "description": "Optional PII categories to include."}}}},
    {"name": "suggest_ingestion", "description": "Recommend concrete telemetry sources for a role/use case. With check_gap=true, compares service and metric hints against live Berserk inventory and marks each source present or missing. Catalog-backed and read-only.", "inputSchema": {"type": "object", "properties": {"role_or_usecase": {"type": "string", "description": "Catalog key such as sre/onprem-ad-health, soc/endpoint-identity, change-management/ansible, or scom."}, "check_gap": {"type": "boolean", "description": "Compare recommendations with live service and metric inventory."}, "since": _since()["since"]}, "required": ["role_or_usecase"]}},
]

MGMT_TOOLS = [
    {"name": "list_saved", "description": "List previously-saved custom queries (name + description). For a non-standard question, CHECK HERE FIRST before writing new KQL.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_saved", "description": "Run a previously-saved query by name (see list_saved). Deterministic - no KQL authoring.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "saved query name"}}, **_since()), "required": ["name"]}},
    {"name": "save_query", "description": "Persist a WORKING KQL query as a reusable named query so it never has to be figured out again. Call this after you answer a non-standard question with a custom search query. The query is run once to verify it works; if it errors it is NOT saved. Replacing an existing saved query of the same name requires overwrite=true.", "inputSchema": {"type": "object", "properties": dict({"name": {"type": "string", "description": "short snake_case name"}, "description": {"type": "string", "description": "what the query answers"}, "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"}, "roles": {"type": ["array", "string"], "description": "optional role(s) this query serves: sre, soc, claude, ops"}, "overwrite": {"type": "boolean", "description": "must be true to replace an existing saved query of the same name"}}, **_since()), "required": ["name", "description", "kql"]}},
    {"name": "request_discovery", "description": "Queue a newly-added service or metric for author-lane integration. Validates the source is currently visible in Berserk, then records a job for the discovery worker to drain. Use when a user says 'I added / connected / started shipping SOURCE'.", "inputSchema": {"type": "object", "properties": {"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "service.name to integrate"}, "metric": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "metric name to integrate"}, "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"}, "requested_by": {"type": "string", "description": "optional requester label"}, **_since()}}},
    {"name": "discovery_status", "description": "List pending and completed discovery jobs for new services or metrics.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "detect_new_sources", "description": "Scan Berserk for services/metrics never seen before (and optionally schema drift on known ones). Use for 'anything new reporting?', or run with auto_queue=true to queue newcomers for parser generation.", "inputSchema": {"type": "object", "properties": {"since": {"type": "string", "description": "Time window e.g. '24h ago'."}, "auto_queue": {"type": "boolean", "description": "queue newly-detected sources for parser generation"}, "check_drift": {"type": "boolean", "description": "also check known services for resource-key schema drift"}}}},
    {"name": "generate_parser", "description": "Generate and verify a query pack for one source right now (synchronous; may take minutes). An LLM authors 2-4 KQL queries from a live schema profile, validates each against Berserk, and saves the survivors. Requires at least one configured LLM provider (HERMES_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY).", "inputSchema": {"type": "object", "properties": {"service": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "service.name to generate a parser for"}, "metric": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "metric_name to generate a parser for"}, "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"}}}},
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
    "claude_record_recommendation_decision": _WRITE_EXTERNAL,
    "claude_generate_dashboard": _WRITE_LOCAL,
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
    "validate_kql": "Validate KQL",
    "search": "Run KQL",
    "detect_anomalies": "Detect Anomalies",
    "forecast_capacity": "Forecast Capacity",
    "find_similar": "Find Similar Logs",
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
    "claude_spend_overview": "Claude Code: Enterprise Spend",
    "claude_feature_cost": "Claude Code: Feature Cost",
    "claude_project_economics": "Claude Code: Project Economics",
    "claude_efficiency_insights": "Claude Code: Efficiency Insights",
    "claude_harness_recommendations": "Claude Code: Harness Recommendations",
    "claude_record_recommendation_decision": "Claude Code: Record Recommendation Decision",
    "claude_optimization_impact": "Claude Code: Optimization Impact",
    "claude_management_report": "Claude Code: Management Report",
    "claude_generate_dashboard": "Claude Code: Generate Dashboard",
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


def _job_identity(job):
    """(source, kind, ts) uniquely identifies one queue entry -- ts is set
    once at enqueue time in request_discovery/detect_new_sources and never
    changes, so this survives a reload of the queue between snapshot and
    save (F-007)."""
    return (job.get("source"), job.get("kind"), job.get("ts"))


def _drain_pending_jobs(max_jobs):
    """Drain up to max_jobs pending discovery jobs through the parser
    factory pipeline. Mutates and persists the discovery queue. Shared by
    the run_discovery_worker MCP tool and the --worker CLI mode.

    Returns (outcome_lines, any_needs_human), or (None, False) if there was
    nothing pending -- callers render their own "no jobs" message so the
    MCP tool and the CLI can phrase it appropriately for their contexts.

    F-007: generate_parser_for can run for minutes (LLM calls, retries),
    so this does NOT hold the queue lock across that work -- another
    writer (e.g. request_discovery enqueueing a new job) would otherwise
    be blocked or time out. Instead: snapshot the jobs to process under a
    brief lock, do the slow work unlocked, then re-acquire the lock,
    reload the CURRENT on-disk queue, and merge in only the status/report
    updates for the jobs we actually processed (matched by identity) --
    any change another writer made to the queue in the meantime (a new
    enqueue, a status change) is preserved rather than clobbered by a
    stale in-memory copy.
    """
    with _FileLock(DISCOVERY_QUEUE_PATH):
        queue = load_json_list(DISCOVERY_QUEUE_PATH)
        pending = [it for it in queue if it.get("status") == "pending"]
    if not pending:
        return None, False

    updates = {}  # job identity -> (status, report)
    outcomes = []
    any_needs_human = False
    for job in pending[:max_jobs]:
        report, ok = parser_factory.generate_parser_for(job)
        if ok:
            job_report = report.get("report", {})
            names = ", ".join(job_report.get("queries_saved", []))
            updates[_job_identity(job)] = ("done", job_report)
            outcomes.append(f"- {job['source']}: done ({names})")
        else:
            job_report = {
                "reason": report.get("reason"),
                "last_errors": report.get("last_errors", []),
            }
            updates[_job_identity(job)] = ("needs_human", job_report)
            outcomes.append(f"- {job['source']}: needs_human ({report.get('reason','')})")
            any_needs_human = True

    with _FileLock(DISCOVERY_QUEUE_PATH):
        fresh_queue = load_json_list(DISCOVERY_QUEUE_PATH)
        for it in fresh_queue:
            update = updates.get(_job_identity(it))
            if update is not None:
                it["status"], it["report"] = update
        save_json_list(DISCOVERY_QUEUE_PATH, fresh_queue)
    return outcomes, any_needs_human


def _handle_call_uncached(name, arguments):
    """Dispatch a tools/call. Returns (text, is_error)."""
    # --- learning-loop management tools ---
    if name == "list_saved":
        items = [it for it in load_learned() if item_visible(it)]
        if not items:
            return "No saved queries yet.", False
        lines = []
        for item in items:
            description = str(item.get("description", ""))
            if item.get("origin") == "generated":
                description = (
                    "<generated-description>" + description
                    + "</generated-description>"
                )
            lines.append("- " + item["name"] + ": " + description)
        return "Saved queries:\n" + "\n".join(lines), False
    if name == "run_saved":
        qn = sanitize_name(arguments.get("name", ""))
        items = [it for it in load_learned() if item_visible(it)]
        match = next((it for it in items if it["name"] == qn), None)
        if not match:
            avail = ", ".join(it["name"] for it in items) or "(none)"
            return "No saved query named '" + qn + "'. Available: " + avail, True
        since = arguments.get("since") or match.get("since") or "1h ago"
        prefix = ""
        if KQL_VALIDATION_MODE != "off":
            report = _validate_user_kql(match["kql"], since)
            stored_hash = match.get("schema_hash")
            current_hash = report.get("schema", {}).get("schema_hash")
            if stored_hash and current_hash and stored_hash != current_hash:
                prefix = (
                    f"Schema drift warning: saved query schema_hash={stored_hash}, "
                    f"current={current_hash}. Revalidated before execution.\n"
                )
            if _blocking_validation(report):
                return prefix + _format_validation_rejection(report), True
        out, err = bzrk_search(match["kql"], since)
        return prefix + out, err
    if name == "save_query":
        nm = sanitize_name(arguments.get("name", ""))
        desc = str(arguments.get("description", "")).strip()
        kql = str(arguments.get("kql", "")).strip()
        since = arguments.get("since") or "1h ago"
        if not kql or not desc:
            return "save_query needs name, description, and kql.", True
        validation_report = None
        if KQL_VALIDATION_MODE != "off":
            validation_report = _validate_user_kql(kql, since)
            if _blocking_validation(validation_report, persistence=True):
                return _format_validation_rejection(validation_report), True
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
        if validation_report:
            schema_info = validation_report.get("schema", {})
            entry.update({
                "validation_version": validation_report.get("validation_version", 1),
                "validation_risk": validation_report.get("risk"),
                "schema_hash": schema_info.get("schema_hash"),
                "schema_status": schema_info.get("schema_status"),
                "validated_at": now_iso(),
            })
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
        if not _valid_interpolated_name(target):
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
        job = {
            "source": target, "kind": kind,
            "role_hint": role_hint[0] if role_hint else (ACTIVE_ROLE if ACTIVE_ROLE != "all" else ""),
            "requested_by": str(arguments.get("requested_by") or "").strip() or "manual",
            "status": "pending", "ts": now_iso(),
        }
        with _FileLock(DISCOVERY_QUEUE_PATH):  # F-007: whole RMW cycle, not just the save
            queue = load_json_list(DISCOVERY_QUEUE_PATH)
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
        if not _valid_interpolated_name(target):
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

    if name == "validate_kql":
        kql = arguments.get("kql")
        if not kql:
            return "missing required 'kql'", True
        since = arguments.get("since") or "15m ago"
        mode = str(arguments.get("mode") or "static").strip().lower()
        if mode not in {"static", "live"}:
            return "mode must be 'static' or 'live'", True
        use_schema = arguments.get("use_schema", True) is not False
        report = _validate_user_kql(
            str(kql), since, use_schema=use_schema,
            allow_refresh_schema=(mode == "live"),
        )
        if mode == "live":
            if not KQL_LIVE_VALIDATION:
                return (
                    "live validation is disabled; set BERSERK_MCP_KQL_LIVE_VALIDATION=1 "
                    "to allow validate_kql mode=live.",
                    True,
                )
            if any(f.get("severity") == "error" for f in report.get("findings", [])):
                return json.dumps(report, indent=2), True
            budget = _window_budget(TOOL_BUDGET_SECONDS if TOOL_BUDGET_SECONDS > 0 else DEFAULT_TIMEOUT, since)
            argv = ["-P", PROFILE, "search", str(kql), "--since", since]
            if KQL_STATS_MODE != "off":
                argv.append("--stats")
            start = time.monotonic()
            with _query_semaphore_slot(budget) as acquired:
                if not acquired:
                    return "Local MCP query queue is full; retry later or narrow the time window.", True
                out, err = run_bzrk(argv, timeout=budget)
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
                report.setdefault("findings", []).append({
                    "code": "STATS_UNAVAILABLE",
                    "severity": "info",
                    "message": "Engine statistics were unavailable or unrecognized; duration was measured locally.",
                    "location": "runtime",
                    "recommendation": "",
                })
            if err:
                report["runtime_error"] = out
            return json.dumps(report, indent=2), bool(err)
        return json.dumps(report, indent=2), False

    if name == "detect_anomalies":
        service = str(arguments.get("service") or "").strip()
        if service and not _valid_interpolated_name(service):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "6h ago"
        out, err = bzrk_search(q_detect_anomalies(service or None), since)
        if err:
            return out, True
        if not out or out.strip() == "(no rows)":
            return f"No anomalies detected (window {since}).", False
        return f"Anomaly decomposition for window {since}; non-zero anomaly markers indicate spikes:\n{out}", False

    if name == "forecast_capacity":
        metric = str(arguments.get("metric") or "").strip()
        if metric not in _FORECAST_METRICS:
            return "metric is not allowlisted; use system.memory.usage, system.filesystem.usage, or system.disk.io", True
        host = str(arguments.get("host") or "").strip()
        if host and not _valid_interpolated_name(host):
            return "invalid host name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "7d ago"
        out, err = bzrk_search_json(q_forecast_capacity(metric, host or None), since)
        if err:
            return out, True
        if not out or out.strip() == "(no rows)":
            return f"No {metric} data found for forecast window {since}.", False
        fits = _forecast_fit_rows(out)
        if fits:
            lines = []
            for fit in fits:
                if fit["r2"] < 0.6 or fit["slope"] <= 0:
                    lines.append(
                        f"{fit['host']}: no reliable trend — not forecastable "
                        f"(R²={fit['r2']:.3f}, slope={fit['slope']:.3g})."
                    )
                else:
                    lines.append(
                        f"{fit['host']}: reliable upward trend "
                        f"(R²={fit['r2']:.3f}, slope={fit['slope']:.3g}); "
                        "native fit array returned, but no ceiling/date is inferred."
                    )
            return (
                f"Capacity trend for {metric} (window {since}):\n" + "\n".join(lines)
            ), False
        return (
            f"Capacity trend for {metric} (window {since}). Native fit arrays include "
            "R² and slope; unable to parse coefficients from this renderer, so no "
            "forecast date is inferred:\n" + out
        ), False

    if name == "find_similar":
        description = str(arguments.get("description") or "").strip()
        if not description:
            return "missing required 'description'", True
        if len(description) > 500:
            return "description is too long (maximum 500 characters)", True
        if _TEXT_GUARD_RE.search(description):
            return "description may not contain quotes, pipe, backslash, or backtick", True
        service = str(arguments.get("service") or "").strip()
        if service and not _valid_interpolated_name(service):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        try:
            k = max(1, min(50, int(arguments.get("k", 10))))
        except (TypeError, ValueError):
            return "k must be an integer between 1 and 50", True
        since = arguments.get("since") or "6h ago"
        out, err = bzrk_search(q_find_similar(description, service or None, k), since)
        if err:
            if "similarto" in str(out).lower() or "semantic" in str(out).lower():
                return (
                    "Semantic indexing is not enabled on this Berserk cluster — "
                    "falling back is not possible for meaning-based search; use "
                    "search with has '<term>' for exact terms.", False
                )
            return out, True
        if "_score" in out and not re.search(r"_score\s+(-?[1-9]\d*(?:\.\d+)?|0?\.\d*[1-9]\d*)", out):
            return (
                "Semantic indexing is not enabled on this Berserk cluster — falling "
                "back is not possible for meaning-based search; use search with "
                "has '<term>' for exact terms.", False
            )
        return out, False

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
        if svc and not _valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        svc_str = str(svc) if svc else None
        # Two perspectives: fieldstats (type/cardinality/representative values)
        # and a tiny structural sample. The sample keeps raw values out of the
        # inventory result while still showing which signal families exist.
        out1, e1 = bzrk_search(q_discover_fieldstats(svc_str), since)
        out2, e2 = bzrk_search(q_discover_sample(svc_str), since)
        return f"== resource fieldstats ==\n{out1}\n\n== sample rows ==\n{out2}", (e1 and e2)
    if name == "logs_for_service":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not _valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        return bzrk_search(q_logs(str(svc)), since)
    if name == "sre_service_health":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not _valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "1h ago"
        return bzrk_search(q_sre_service_health(str(svc)), since)
    if name == "soc_timeline":
        svc = arguments.get("service")
        if not svc:
            return "missing required 'service'", True
        if not _valid_interpolated_name(svc):
            return "invalid service name (allowed: letters, digits, '.', '_', '-')", True
        since = arguments.get("since") or "6h ago"
        return bzrk_search(q_soc_timeline(str(svc)), since)
    if name == "trace_analyze":
        trace_id = arguments.get("trace_id")
        if not trace_id:
            return "missing required 'trace_id'", True
        if (len(str(trace_id)) > MAX_TRACE_ID_CHARS
                or not _TRACE_ID_RE.fullmatch(str(trace_id))):
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
        warning = ""
        if KQL_VALIDATION_MODE != "off":
            report = _validate_user_kql(str(kql), since)
            if _blocking_validation(report):
                return _format_validation_rejection(report), True
            if KQL_VALIDATION_MODE == "warn":
                warning = _format_validation_warnings(report)
        out, err = bzrk_search(str(kql), since)
        if warning and not err:
            return warning + "\n\n" + out, False
        return out, err
    if name == "claude_search":
        term = arguments.get("term")
        if not term:
            return "missing required 'term'", True
        if len(str(term)) > MAX_SEARCH_TERM_CHARS:
            return f"term is too long (maximum {MAX_SEARCH_TERM_CHARS} characters)", True
        if _TEXT_GUARD_RE.search(str(term)):
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
    if name in {
        "claude_spend_overview", "claude_feature_cost", "claude_project_economics",
        "claude_efficiency_insights", "claude_harness_recommendations",
        "claude_optimization_impact", "claude_management_report",
        "claude_generate_dashboard",
    }:
        default_since = "7d ago" if name in {
            "claude_spend_overview", "claude_efficiency_insights",
        } else "90d ago"
        if name == "claude_harness_recommendations":
            default_since = "14d ago"
        if name == "claude_optimization_impact":
            default_since = "30d ago"
        since = arguments.get("since") or default_since
        if not valid_since(since):
            return (
                f"invalid 'since' value: {since!r}. Use forms like '15m ago', '1h ago', "
                f"'2d ago', or 'now'."
            ), True
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
                since, group_by=arguments.get("group_by") or "day",
                filters=filters, limit=limit,
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
    if name == "claude_record_recommendation_decision":
        return ai_finops.record_recommendation_decision(
            arguments.get("recommendation_id"), arguments.get("decision"),
            arguments.get("owner"), arguments.get("rationale"),
        )
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


_CACHEABLE_TOOLS = frozenset(set(SIMPLE) | {
    "sre_service_health", "soc_timeline",
    "claude_loop_check", "claude_model_fit", "claude_token_burn",
    "claude_cost_report", "claude_session_deep_dive", "claude_workflow_insights",
    "claude_spend_overview", "claude_feature_cost", "claude_project_economics",
    "claude_efficiency_insights", "claude_harness_recommendations",
    "claude_optimization_impact", "claude_management_report",
})


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
        hash(run_bzrk)
        backend = run_bzrk
    except TypeError:
        backend = (type(run_bzrk), id(run_bzrk))
    return (backend, str(name), encoded)


def _fleet_backend_fingerprint():
    try:
        hash(run_bzrk)
        return run_bzrk
    except TypeError:
        return (type(run_bzrk), id(run_bzrk))


def _cache_marker(text, age):
    return f"{text}\n(cached, {age:.1f}s old)"


def handle_call(name, arguments):
    """Dispatch one tool call with fleet-friendly budget/cache controls."""
    global _FLEET_CONTEXT, _FLEET_BACKEND_ID
    args = arguments if isinstance(arguments, dict) else {}
    backend_id = _fleet_backend_fingerprint()
    with _FLEET_LOCK:
        if _FLEET_BACKEND_ID != backend_id:
            _RESULT_CACHE.clear()
            _FAIL_COOLDOWN.clear()
            _FLEET_BACKEND_ID = backend_id
    key = _fleet_args_key(name, args)
    now = time.monotonic()

    with _FLEET_LOCK:
        if FAIL_COOLDOWN_SECONDS > 0:
            failed = _FAIL_COOLDOWN.get(key)
            if failed and now - failed[2] < FAIL_COOLDOWN_SECONDS:
                return (
                    f"{failed[0]}\n(fail-cooldown, {now - failed[2]:.1f}s old; "
                    "identical retry suppressed)",
                    True,
                )
            if failed:
                _FAIL_COOLDOWN.pop(key, None)
        if name in _CACHEABLE_TOOLS and CACHE_TTL_SECONDS > 0:
            cached = _RESULT_CACHE.get(key)
            if cached and now - cached[2] < CACHE_TTL_SECONDS:
                return _cache_marker(cached[0], now - cached[2]), cached[1]
            if cached:
                _RESULT_CACHE.pop(key, None)

    previous_context = _FLEET_CONTEXT
    _FLEET_CONTEXT = {
        "tool": str(name),
        "budget": TOOL_BUDGET_SECONDS if TOOL_BUDGET_SECONDS > 0 else None,
    }
    try:
        text, is_err = _handle_call_uncached(name, args)
    finally:
        _FLEET_CONTEXT = previous_context

    text = str(text)
    timed_out = is_err and text.startswith(f"{name} exceeded its ")
    with _FLEET_LOCK:
        if timed_out and FAIL_COOLDOWN_SECONDS > 0:
            _FAIL_COOLDOWN[key] = (text, True, time.monotonic())
        elif name in _CACHEABLE_TOOLS and not is_err and CACHE_TTL_SECONDS > 0:
            _RESULT_CACHE[key] = (text, False, time.monotonic())
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
# A non-dict `params` (e.g. a list or string) hit the same AttributeError
# class as (1) the moment any branch called params.get(...); validated here
# too rather than per-branch.
def _is_object(value):
    return isinstance(value, dict)


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
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": requested,
            },
        },
    }


def _valid_mcp_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _modern_mcp_enabled():
    return bool(ENABLE_MCP_2026_07_28)


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
    version = meta.get(MCP_META_PROTOCOL_VERSION)
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
    if (
        _modern_mcp_enabled()
        and _requested_protocol_version(params) == MCP_PROTOCOL_MODERN
    ):
        return PROTOCOL_MODE_MODERN
    return PROTOCOL_MODE_LEGACY


def _valid_modern_meta(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    requested = _requested_protocol_version(params)
    caps = meta.get(MCP_META_CLIENT_CAPABILITIES)
    client_info = meta.get(MCP_META_CLIENT_INFO)
    return (
        requested == MCP_PROTOCOL_MODERN
        and isinstance(caps, dict)
        and isinstance(client_info, dict)
    )


def _discover_result():
    capabilities = {
        "tools": {"listChanged": False},
        "extensions": {
            "tasks": {
                "uri": MCP_TASK_EXTENSION_URI,
                "methods": ["tasks/get", "tasks/cancel"],
                "createHint": "Set arguments.as_task=true on eligible long-running tools.",
            }
        },
    }
    return {
        "resultType": "complete",
        "supportedVersions": [MCP_PROTOCOL_MODERN, MCP_PROTOCOL_LEGACY],
        "capabilities": capabilities,
        "_meta": {
            MCP_META_SERVER_INFO: SERVER_INFO,
        },
        "instructions": INSTRUCTIONS,
        # Role and environment can change tool visibility/instructions, so this
        # is cacheable only for the current caller/deployment context.
        "ttlMs": MCP_PRIVATE_CACHE_TTL_MS,
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
    expired = [task_id for task_id, record in _TASKS.items()
               if record.get("expires_ts", 0) <= now]
    for task_id in expired:
        _TASKS.pop(task_id, None)
    if len(_TASKS) > MCP_MAX_TASKS:
        removable = sorted(
            (
                (record.get("updated_ts", 0), task_id)
                for task_id, record in _TASKS.items()
                if record.get("status") in {"complete", "failed", "cancelled"}
            )
        )
        for _, task_id in removable[:len(_TASKS) - MCP_MAX_TASKS]:
            _TASKS.pop(task_id, None)


def _launch_task_worker(target):
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    return worker


def _execute_task_tool(name, arguments, mode):
    text, is_err = handle_call(name, arguments)
    text = secret_scan.apply_output_filter(
        text,
        mode=REDACT_MODE,
        include_entropy=REDACT_ENTROPY,
        pii_types=REDACT_PII_TYPES,
    )
    return _tool_call_result(name, text, is_err, mode)


def _run_task(task_id, name, arguments, mode):
    with _TASK_LOCK:
        record = _TASKS.get(task_id)
        if record is None or record.get("status") == "cancelled":
            return
        record["status"] = "running"
        record["updated_ts"] = _task_now()
        record["updated_at"] = now_iso()
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
        record["updated_at"] = now_iso()


def _create_task(name, arguments, mode):
    now = _task_now()
    task_id = "task_" + uuid.uuid4().hex
    record = {
        "id": task_id,
        "status": "pending",
        "tool": name,
        "role": ACTIVE_ROLE,
        "created_ts": now,
        "updated_ts": now,
        "expires_ts": now + MCP_TASK_TTL_SECONDS,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + MCP_TASK_TTL_SECONDS)),
        "result": None,
        "error": "",
    }
    with _TASK_LOCK:
        _task_prune_locked(now)
        if len(_TASKS) >= MCP_MAX_TASKS:
            return None
        _TASKS[task_id] = record
    _launch_task_worker(lambda: _run_task(task_id, name, dict(arguments), mode))
    return {"resultType": "task", "task": _task_public(record)}


def _task_lookup(task_id):
    with _TASK_LOCK:
        _task_prune_locked()
        record = _TASKS.get(task_id)
        if record is None or record.get("role") != ACTIVE_ROLE:
            return None
        return dict(record)


def _client_supports_tasks(params):
    meta = _request_meta(params)
    if meta is None:
        return False
    caps = meta.get(MCP_META_CLIENT_CAPABILITIES)
    if not isinstance(caps, dict):
        return False
    extensions = caps.get("extensions")
    return (
        isinstance(caps.get("tasks"), dict)
        or (isinstance(extensions, dict) and (
            "tasks" in extensions or MCP_TASK_EXTENSION_URI in extensions
        ))
    )


def _task_id_from_params(params):
    task_id = params.get("taskId") or params.get("id")
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
        return None
    return task_id


def _tool_list_result(mode):
    allt = [t for t in TOOLS + MGMT_TOOLS if tool_visible(t)]
    tl = []
    for t in allt:
        visible_tool = _with_output_schema(t) if mode == PROTOCOL_MODE_MODERN else t
        item = {
            "name": visible_tool["name"],
            "title": TITLES.get(visible_tool["name"], visible_tool["name"]),
            "description": visible_tool["description"],
            "inputSchema": visible_tool["inputSchema"],
            "annotations": annotations_for(visible_tool["name"]),
        }
        if "outputSchema" in visible_tool:
            item["outputSchema"] = visible_tool["outputSchema"]
        tl.append(item)
    result = {"tools": tl}
    if mode == PROTOCOL_MODE_MODERN:
        result["resultType"] = "complete"
        result["ttlMs"] = MCP_PRIVATE_CACHE_TTL_MS
        result["cacheScope"] = "private"
    return result


def _extract_structured_content(name, text, is_error):
    if is_error or name not in _STRUCTURED_OUTPUT_TOOLS:
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
            valid_since(since)
            and _since_hours(since) > MCP_EXPENSIVE_SEARCH_WINDOW_HOURS
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
                    "window_hours": _since_hours(since),
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
    if mode == PROTOCOL_MODE_MODERN:
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
        return _dispatch_validated(method, params, id_, is_notification, mode=mode)
    except Exception as exc:
        log(f"dispatch failed: {type(exc).__name__}")
        if is_notification:
            return None
        return _jsonrpc_error(-32603, "Internal error", id_)


def _dispatch_validated(method, params, id_, is_notification, mode=PROTOCOL_MODE_LEGACY):
    """Dispatch a validated request envelope to the appropriate handler."""
    def _reply(result):
        if is_notification:
            return None
        return _jsonrpc_result(id_, result)

    if method == "server/discover":
        if is_notification:
            return None
        if mode != PROTOCOL_MODE_MODERN:
            if _modern_mcp_enabled() and _request_meta(params) is None:
                return _jsonrpc_error(-32602, "Invalid params", id_)
            requested = _requested_protocol_version(params)
            if _modern_mcp_enabled() and requested and requested not in SUPPORTED_PROTOCOL_VERSIONS:
                return _jsonrpc_unsupported_protocol(id_, requested)
            if _modern_mcp_enabled() and requested != MCP_PROTOCOL_MODERN:
                return _jsonrpc_error(-32602, "Invalid params", id_)
            return _jsonrpc_error(-32601, "Method not found", id_)
        if set(params) - {"_meta"}:
            return _jsonrpc_error(-32602, "Invalid params", id_)
        if not _valid_modern_meta(params):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _jsonrpc_result(id_, _discover_result())

    if method in {"tasks/get", "tasks/cancel"}:
        if is_notification:
            return None
        if mode != PROTOCOL_MODE_MODERN:
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
                if current is None or current.get("role") != ACTIVE_ROLE:
                    return _jsonrpc_error(-32602, "Unknown task", id_)
                if current.get("status") in {"pending", "running"}:
                    current["status"] = "cancelled"
                    current["updated_ts"] = _task_now()
                    current["updated_at"] = now_iso()
                record = dict(current)
        return _jsonrpc_result(id_, _task_result(record))

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
        if mode == PROTOCOL_MODE_MODERN:
            if set(params) - {"_meta"}:
                if is_notification:
                    return None
                return _jsonrpc_error(-32602, "Invalid params", id_)
            if not _valid_modern_meta(params):
                if is_notification:
                    return None
                return _jsonrpc_error(-32602, "Invalid params", id_)
            return _reply(_tool_list_result(mode))
        if params:
            if is_notification:
                return None
            return _jsonrpc_error(-32602, "Invalid params", id_)
        return _reply(_tool_list_result(mode))
    if method == "tools/call":
        if is_notification:
            return None
        if mode == PROTOCOL_MODE_MODERN and not _valid_modern_meta(params):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        name = params.get("name")
        if not name or not isinstance(name, str):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _jsonrpc_error(-32602, "Invalid params", id_)
        arguments = arguments or {}
        # F-008: tools/list already filters by role; tools/call must enforce
        # the SAME predicate, or a client can invoke a tool that was never
        # supposed to be visible in this role's lane just by naming it
        # directly. A role-hidden tool is treated exactly like an unknown
        # one (same message, same isError=true) -- it doesn't leak that a
        # tool with that name exists but is merely hidden.
        matched_tool = next((t for t in TOOLS + MGMT_TOOLS if t["name"] == name), None)
        if matched_tool is not None and not tool_visible(matched_tool):
            text, is_err = "unknown tool: " + name, True
        else:
            if mode == PROTOCOL_MODE_MODERN:
                input_required = _modern_preflight_input_required(name, arguments)
                if input_required is not None:
                    return _jsonrpc_result(id_, input_required)
                if (
                    name in _TASK_ELIGIBLE_TOOLS
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
            mode=REDACT_MODE,
            include_entropy=REDACT_ENTROPY,
            pii_types=REDACT_PII_TYPES,
        )
        return _jsonrpc_result(id_, _tool_call_result(name, text, is_err, mode))

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
    cli.add_argument("--agent-report-mode", choices=("operational", "daily", "weekly"),
                     default="operational", help="agent report depth")
    cli.add_argument("--agent-report-json", action="store_true",
                     help="emit a machine-readable agent report envelope")
    cli.add_argument("--auto-queue", action="store_true",
                     help="(worker) queue newly detected sources")
    cli.add_argument("--max-jobs", type=int, default=3,
                     help="(worker) max discovery jobs to drain")
    cli.add_argument("--check-drift", action="store_true",
                     help="(worker) check known services for schema drift")
    cli.add_argument("--since", default="6h ago",
                     help="(agent-report) time window")
    cli.add_argument("--import-business-data", choices=("feature", "effort"),
                     help="import governed feature catalog or developer-effort records")
    cli.add_argument("--input", help="input CSV/JSON/NDJSON file for business-data import")
    cli.add_argument("--input-format", choices=("csv", "json", "ndjson", "jsonl"),
                     help="override business-data input format")
    cli.add_argument("--export-bi", action="store_true",
                     help="export management-ready AI FinOps datasets")
    cli.add_argument("--output", help="absolute BI export directory")
    cli.add_argument("--export-format", choices=("csv", "ndjson"), default="csv",
                     help="BI export format")
    cli.add_argument("--generate-dashboard",
                     choices=("portfolio", "project", "feature", "agent_efficiency", "data_quality"),
                     help="generate a Claude Code dashboard snapshot")
    cli.add_argument("--identifier", help="project/feature identifier for dashboard generation")
    cli.add_argument("--dashboard-format", choices=("markdown", "html"), default="markdown")
    cli.add_argument("--set-hermes-url", metavar="URL",
                     help="persist the Hermes LLM endpoint and exit")
    ns = cli.parse_args()
    if ns.import_business_data:
        if not ns.input:
            cli.error("--import-business-data requires --input")
        try:
            result = ai_finops.import_business_data(
                ns.import_business_data, ns.input, fmt=ns.input_format,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"business-data import failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.export_bi:
        if not ns.output:
            cli.error("--export-bi requires --output")
        if not valid_since(ns.since):
            cli.error("--export-bi received an invalid --since value")
        try:
            manifest = ai_finops.export_bi(ns.since, ns.output, fmt=ns.export_format)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"BI export failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.generate_dashboard:
        if not valid_since(ns.since):
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
            apply_jitter=True,
        ))
    if ns.agent_report:
        sys.exit(run_agent_report(
            since=ns.since, mode=ns.agent_report_mode,
            output_json=ns.agent_report_json,
        ))
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
    if not DISCORD_ALERT_SECRET:
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
        _http.validate_http_url(DISCORD_ALERT_URL, label="discord alert endpoint")
    except _http.UrlPolicyError as e:
        log(f"discord alert: endpoint rejected: {e}")
        return False
    payload = json.dumps({"text": text[:DISCORD_ALERT_MAX_CHARS]}).encode("utf-8")
    try:
        status = _http.post_bytes_status(
            DISCORD_ALERT_URL,
            {
            "Content-Type": "application/json",
            "X-Auth-Token": DISCORD_ALERT_SECRET,
            },
            payload,
            timeout=10,
            label="discord alert endpoint",
        )
        return 200 <= status < 300
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        log(f"discord alert: bridge returned HTTP {code}")
        return False
    except Exception as e:
        log(f"discord alert failed: {type(e).__name__}")
        return False


_AMENDMENT_EMOJI = {"generated": "\U0001F916", "updated": "✏️", "created": "✨"}


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
    amendments_path = Path(LEARNED_PATH).parent / "amendments_log.json"
    with _FileLock(amendments_path):
        amendments = load_json_list(amendments_path)
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
            save_json_list(amendments_path, [])
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
    if apply_jitter and WORKER_JITTER_SECONDS > 0:
        delay = random.uniform(0, WORKER_JITTER_SECONDS)
        log(f"worker startup jitter: sleeping {delay:.1f}s (max {WORKER_JITTER_SECONDS:g}s)")
        time.sleep(delay)

    detect_summary = parser_factory.detect_new_sources(
        since="24h ago", auto_queue=auto_queue, check_drift=check_drift,
        load_json_list=load_json_list, save_json_list=save_json_list,
        discovery_queue_path=DISCOVERY_QUEUE_PATH, active_role=ACTIVE_ROLE,
    )
    print(detect_summary)

    outcomes, any_needs_human = _drain_pending_jobs(max_jobs)
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
    if not valid_since(since):
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
            effective_since, group_by="project", limit=20,
        )
    if output_json:
        print(json.dumps({
            "schema_version": ai_finops.SCHEMA_VERSION,
            "mode": mode,
            "since": effective_since,
            "operational_report": text,
            "spend_report": spend_text,
            "alert": bool(should_alert),
            "spend_error": bool(spend_error),
        }, indent=2, sort_keys=True))
    else:
        print(text)
        if spend_text:
            print("\n" + spend_text)
    return 1 if should_alert or spend_error else 0


if __name__ == "__main__":
    main()
