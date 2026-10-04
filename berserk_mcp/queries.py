"""Verified KQL queries and the builders that fill them in.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
import agent_analytics
import json
import re


# ---------- verified queries (do not edit field names; they are confirmed
# against the live `default` schema — see docs/claude-code.md) ----------
T = bm_config.TABLE


# Issue #42: the claude_* lane's OTLP records are tagged by ingesting agent
# via resource['service.name']. "claude-code" stays the default for zero
# behavior change on existing callers; other agents register here as their
# ingestion adapter ships (see ingestion/codex_adapter.py for the first one).
_AGENT_SERVICE_NAMES = {
    "claude-code": "claude-code",
    "codex": "codex-cli",
}


def _service_filter(agent):
    """KQL filter clause for one ingesting agent's OTLP records. Unknown
    agent names fall back to 'claude-code' rather than erroring, matching
    every other tool's default-since-style leniency on optional args."""
    service = _AGENT_SERVICE_NAMES.get(agent, _AGENT_SERVICE_NAMES["claude-code"])
    return f"{T} | where resource['service.name'] == '{service}'"


CC = _service_filter("claude-code")


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
    f"{T} | where isnotnull(body) or isnotnull(metric_name) "
    f"| summarize total=count(), logs=countif(isnotnull(body)), "
    f"metrics=countif(isnotnull(metric_name)) by service=tostring(resource['service.name']) "
    f"| sort by total desc"
)


Q_HOSTS = f"{T} | summarize total=count() by host=tostring(resource['host.name']) | sort by total desc"


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


def q_soc_log_spike_for_service(service):
    """investigate_error_rate-only variant of Q_SOC_LOG_SPIKE, scoped to one
    service before make-series groups by service (issue #24 Codex review,
    P2, 2026-08-28): the unscoped query's `take 60` is an unsorted cap on
    the grouped series, so past 60 distinct services the target service's
    own series can be dropped before investigation.py's Python-side lookup
    ever sees it, producing a false "no log-volume data" halt. `service`
    must already be validated by _valid_interpolated_name at the call
    site."""
    return (
        f"{T} | where isnotnull(body) "
        f"| where resource['service.name'] == '{service}' "
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


def q_trace_find_errors_for_service(service):
    """investigate_error_rate-only variant of Q_TRACE_FIND_ERRORS, scoped to
    one service and collapsed to one row per trace_id before the `take`
    cap runs. `service` must already be validated by
    _valid_interpolated_name at the call site.

    Three Codex review fixes are folded into this query (2026-08-28):
    round 1 (P1) -- the original unscoped query's `tail 20` is global
    across every service, so a service whose failing spans aren't among
    the latest 20 error spans overall gets silently dropped before
    investigation.py's Python-side filter ever sees them, producing a
    false "no failing traces" verdict. Scoping by service here fixes
    that. Round 2 (P2) -- capping *raw spans* (even service-scoped) before
    grouping by trace_id still undercounts: if one trace_id contributes
    most of the 20 rows (e.g. several retried spans), older distinct
    traces are pushed out of the window and never counted, even though
    investigation.py's own dedup only sees what's left after the cap.
    Grouping by trace_id in KQL, before `take`, makes the cap apply to
    distinct traces instead of raw spans. Round 3 (P2) -- `summarize`
    doesn't preserve input row order, so the round-2 fix's unsorted
    `take 20` after grouping returned an arbitrary subset of traces
    instead of the most recent ones, regressing the recency behavior the
    original `tail 20` gave for free. Sorting by the aggregated
    `timestamp` descending before the cap restores that."""
    return (
        f"{T} | where isnotnull(trace_id) | where status_code == 'ERROR' "
        f"| where resource['service.name'] == '{service}' "
        f"| summarize span_name=take_any(span_name), timestamp=max(timestamp) "
        f"by trace_id, service=tostring(resource['service.name']) "
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
        f"| tail 100"
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
    without exporting the raw resource bag. Global discovery uses depth 1 to
    limit scan cost; a selective service filter permits depth 2. Keep the row
    sample separate so callers can inspect value shape without widening the
    inventory result.
    """
    filt = f"| where resource['service.name'] == '{service}' " if service else ""
    depth = 2 if service else 1
    return f"{T} {filt}| fieldstats resource with limit=50 depth={depth}"


def q_cc_recent(agent="claude-code"):
    cc = _service_filter(agent)
    return (
        f"{cc} | tail 60 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"role=tostring(attributes['claude.message_role']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"err=tostring(attributes['claude.error'])"
    )


def q_cc_sessions(agent="claude-code"):
    cc = _service_filter(agent)
    return (
        f"{cc} | summarize events=count(), first=min(timestamp), last=max(timestamp), "
        f"assistant_turns=countif(tostring(attributes['claude.type'])=='assistant'), "
        f"tool_turns=countif(isnotempty(tostring(attributes['claude.tool_names']))), "
        f"errors=countif(tostring(attributes['claude.error'])=='true') "
        f"by session=tostring(attributes['claude.session_id']) | sort by last desc | take 40"
    )


def q_cc_tools(agent="claude-code"):
    cc = _service_filter(agent)
    return (
        f"{cc} | where isnotempty(tostring(attributes['claude.tool_names'])) "
        f"| mv-expand t=split(tostring(attributes['claude.tool_names']), ',') "
        f"| summarize uses=count() by tool=tostring(t) | sort by uses desc | take 40"
    )


def q_cc_errors(agent="claude-code"):
    cc = _service_filter(agent)
    return (
        f"{cc} | where tostring(attributes['claude.error'])=='true' "
        f"| tail 40 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"body=substring(tostring(body),0,2000)"
    )


def q_logs(svc: str) -> str:
    return (
        f"{T} | where isnotnull(body) | where resource['service.name'] == '{svc}' "
        f"| project timestamp, severity_text, body=substring(tostring(body), 0, 500) "
        f"| tail 50"
    )


def q_cc_search(term: str, agent="claude-code") -> str:
    cc = _service_filter(agent)
    return (
        f"{cc} | where tostring(body) contains '{term}' "
        f"| tail 40 | project ts=timestamp, typ=tostring(attributes['claude.type']), "
        f"model=tostring(attributes['claude.message_model']), "
        f"tools=tostring(attributes['claude.tool_names']), "
        f"body=substring(tostring(body),0,2000)"
    )


MAX_INTERPOLATED_NAME_CHARS = 128


MAX_TRACE_ID_CHARS = 64


MAX_SEARCH_TERM_CHARS = 500


_SERVICE_RE = re.compile(r"[A-Za-z0-9._-]+")


_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._/-]+")


_TRACE_ID_RE = re.compile(r"[A-Za-z0-9]+")


_TEXT_GUARD_RE = re.compile(r"['\"|\\`\x00-\x1f\x7f]")


_FORECAST_METRICS = frozenset(
    {
        "system.memory.usage",
        "system.filesystem.usage",
        "system.disk.io",
    }
)


def _valid_interpolated_name(value, max_chars=MAX_INTERPOLATED_NAME_CHARS):
    text = str(value or "")
    return len(text) <= max_chars and bool(_SERVICE_RE.fullmatch(text))


def _valid_model_id(value, max_chars=MAX_INTERPOLATED_NAME_CHARS):
    """Model IDs are vendor/model (deepseek/deepseek-v4-flash), so they need
    the slash that _SERVICE_RE deliberately excludes. Everything else stays
    as strict: no quotes, spaces, backslashes, or KQL metacharacters, so the
    value remains safe to interpolate into a query string."""
    text = str(value or "")
    return len(text) <= max_chars and bool(_MODEL_ID_RE.fullmatch(text))


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
    return f'{T} {filt}| where isnotnull(body) | top {k} by body similarto "{description}"'


# Legacy fallback only: pre-existing bzrk builds that reject --json return
# plain table text from bzrk_search_json's fallback, where a `_score` column
# renders as a single line ("_score   0.83") immediately adjacent to its
# value -- unlike --json's Tables/schema/rows shape, where the column name
# and its value are never textually adjacent (see _find_similar_has_real_score).
_FIND_SIMILAR_SCORE_TABLE_RE = re.compile(r"_score\s+(-?[1-9]\d*(?:\.\d+)?|0?\.\d*[1-9]\d*)")


def _find_similar_has_real_score(out):
    """True if a genuinely non-zero `_score` value is present. bzrk's real
    --json output is the Kusto-style {"Tables": [{"schema": {"columns":
    [...]}, "rows": [[...]]}]} shape (confirmed live 2026-07-17, see
    agent_analytics._json_records) -- rows are positional arrays zipped
    against column order, so the column name "_score" and its value are
    never textually adjacent and can't be found by pattern-matching the raw
    text. Reuses the same column/row-zip helper agent_analytics already
    validates against real output, rather than re-parsing it here. Falls
    back to the table-adjacency regex only when `out` isn't parseable JSON
    at all, i.e. bzrk_search_json degraded to its legacy table fallback."""
    whole = str(out or "").strip()
    if not whole or whole[0] not in "[{":
        return bool(_FIND_SIMILAR_SCORE_TABLE_RE.search(out))
    try:
        records = agent_analytics._json_records(json.loads(whole))
    except (TypeError, ValueError, json.JSONDecodeError):
        return bool(_FIND_SIMILAR_SCORE_TABLE_RE.search(out))
    if records is None:
        return bool(_FIND_SIMILAR_SCORE_TABLE_RE.search(out))
    for row in records:
        if not isinstance(row, dict) or "_score" not in row:
            continue
        try:
            if float(row["_score"]) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
