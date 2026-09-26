"""The MCP tool catalog: static tool definitions, management tools and titles.

Moved out of berserk_mcp.py unchanged. The definitions depend on a few runtime
values (the configured table, input limits, schema helpers) that berserk_mcp
owns, so they are built by functions that take those values as keyword
arguments named as in berserk_mcp. This module imports nothing from berserk_mcp,
so there is no import cycle.
"""

import agent_analytics  # noqa: F401  (referenced inside the TOOLS definitions)


def build_tools(
    *,
    TABLE,
    MAX_INTERPOLATED_NAME_CHARS,
    MAX_SEARCH_TERM_CHARS,
    MAX_TRACE_ID_CHARS,
    _FORECAST_METRICS,
    _agent_prop,
    _since,
):  # noqa: N803
    return [
        {
            "name": "list_containers",
            "description": "List all containers currently sending metrics to Berserk (with sample counts).",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "top_cpu",
            "description": "Containers ranked by CPU percent, highest first. PER-CONTAINER — use ONLY when the user names a container, says 'docker'/'container', or asks for 'top containers'. For ambiguous whole-machine questions ('the box', 'the system', 'the server', 'the machine', 'what’s hammering/running hot') use host_cpu instead.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "top_memory",
            "description": "Containers ranked by memory usage in MB, highest first. PER-CONTAINER — use ONLY when the user names a container or says 'docker'/'container'. For ambiguous whole-machine memory questions ('the box', 'the system', 'the server') use host_memory instead.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "errors_by_service",
            "description": "Count of ERROR-level log lines grouped by service. Use for 'how many errors', 'which services have errors', or 'any errors?' — gives counts, not log text. For the actual error messages, use logs_for_service with the service name from this result.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "list_services",
            "description": "All services/sources sending data, with log vs metric breakdown. Best default for 'what's running?', 'what's reporting?', or 'what services are there?' — shows everything. For just hosts use list_hosts; for just containers use list_containers.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "list_hosts",
            "description": "All hosts reporting telemetry, by record count.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "host_cpu",
            "description": "Average CPU load (1-minute load average) per host. Use for per-host CPU AND as the DEFAULT for ambiguous whole-machine questions — 'the box', 'the system', 'the server', 'the machine', 'what's hammering/running hot' are about the hosts, not containers (top_cpu is per-CONTAINER).",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "host_memory",
            "description": "Used memory in GB per host. Use for per-host memory AND as the DEFAULT for ambiguous whole-machine memory questions ('the box', 'the system', 'the server') — these are about the hosts, not containers (top_memory is per-CONTAINER).",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "container_hosts",
            "description": "Map each container to the host/VM it runs on. Use to answer 'which host runs container X' or to JOIN per-container metrics (top_cpu/top_memory) with per-host metrics (host_cpu/host_memory) — don't infer the host from the container's name.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "logs_for_service",
            "description": "Recent log lines for a specific service e.g. 'nginx', 'postgres'. Use for 'show me the errors/logs from X' — returns actual log text. For error COUNTS across all services, use errors_by_service first, then drill into a specific service here.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "service.name value",
                        }
                    },
                    **_since(),
                ),
                "required": ["service"],
            },
        },
        {
            "name": "schema",
            "description": "Show Berserk tables + column schema (live introspection).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_metrics",
            "description": "List every metric name currently being ingested, with sample counts + last-seen. Use to DISCOVER what telemetry exists before writing a `search` query.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "bzrk_query_perf",
            "description": "Berserk query engine latency percentiles: p50, p95, p99 in µs. Use for 'how fast is Berserk?', 'query latency', or 'p50/p95/p99 execution time'. Uses otel_histogram_percentile($raw, N) — the native Berserk histogram aggregate.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "discover_schema",
            "description": "Discover the shape of a data source: returns (1) every key present under `resource` with row counts, AND (2) a small structural sample with resource/attribute keys and body/metric presence flags. It never exports raw resource, attributes, or body values. Use to learn an unknown or newly-ingested source before querying it. Optional `service` filter. Pair with list_services / list_metrics. Once you work out a query with `search`, persist it with save_query so it becomes reusable.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "optional: limit to one service.name",
                        }
                    },
                    **_since(),
                ),
            },
        },
        {
            "name": "self_check",
            "description": "Preflight readiness report for this berserk-mcp server: bzrk resolvable, bzrk version, auth, table reachable, recent row count, primers dir, learned-store writable, HTTP config coherence, and optional LLM/CanonLoom reachability. Use when a tool call keeps failing and it's unclear whether it's a wiring problem versus genuinely nothing to report. Same checks as `berserk-mcp --doctor`.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "validate_kql",
            "roles": ["sre", "soc", "claude", "ops"],
            "description": "Validate custom Berserk KQL before saving or running it. Use for 'check this query before I save it' or 'will this KQL work'. Static mode does not contact Berserk except for cached schema context; live mode is opt-in, executes a bounded read-only query, and may consume query budget.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'."},
                        "mode": {"type": "string", "enum": ["static", "live"], "default": "static"},
                        "use_schema": {
                            "type": "boolean",
                            "default": True,
                            "description": "Use cached/discovered schema for unknown-field checks.",
                        },
                    },
                    **_since(),
                ),
                "required": ["kql"],
            },
        },
        {
            "name": "search",
            "description": "Run an arbitrary Kusto/KQL query against the Berserk table. Use when the other tools do not fit; once it works, persist it with save_query. Fields are nested OTLP resource/log attributes, NOT flat columns — access as resource['service.name'], resource['host.name'], attributes['systemd.unit'], etc. (bare service_name/host_name do not exist and silently match zero rows instead of erroring). If you don't already know the exact field names for this source, call discover_schema first instead of guessing.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "kql": {
                            "type": "string",
                            "description": f"KQL starting with '{TABLE} | ...'. OTLP resource/log attributes (service name, host name, etc.) need resource['key'] / attributes['key'] access, not a bare column name; some other fields (trace_id, span_id, timestamp, metric_name, ...) are genuinely top-level. Use discover_schema if unsure which a given field is.",
                        }
                    },
                    **_since(),
                ),
                "required": ["kql"],
            },
        },
        {
            "name": "detect_anomalies",
            "roles": ["sre", "soc"],
            "description": "Statistical anomaly detection for service event volume over time. Uses zero-filled make-series and series_decompose_anomalies; use for 'is anything behaving abnormally?' or 'anything anomalous?' rather than guessing a threshold. Optional service filter. For raw per-minute volume without statistics, see soc_log_spike.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "optional service.name filter",
                        }
                    },
                    **_since(),
                ),
            },
        },
        {
            "name": "find_similar",
            "roles": ["sre", "soc"],
            "description": "Find log messages by meaning rather than exact text, for example 'database timeouts' or 'authentication failures'. Semantic indexing must be enabled on the Berserk cluster; use `search` with has for exact terms. Optional service filter and k (1-50).",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "description": {
                            "type": "string",
                            "maxLength": 500,
                            "description": "natural-language description; quotes, pipes, backslashes, backticks, and controls are rejected",
                        },
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "optional service.name filter",
                        },
                        "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    },
                    **_since(),
                ),
                "required": ["description"],
            },
        },
        # --- Trace tools (span-level latency/error triage; UNVERIFIED field names — see the
        # comment above Q_TRACE_FIND_SLOW. Descriptions below flag this to the model too.) ---
        {
            "name": "trace_find_slow",
            "description": "Find the highest-duration root spans in the time window. Use for 'what's slow', 'find the slowest requests', or as the entry point before trace_analyze.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "trace_find_errors",
            "description": "Find spans whose status indicates an error. Use for 'which requests failed' or as the entry point before trace_analyze.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "trace_analyze",
            "description": "Full breakdown of one trace by trace_id — every span in time order plus correlated log lines from the same trace_id. Use after trace_find_slow/trace_find_errors surface a trace_id worth investigating.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "trace_id": {
                        "type": "string",
                        "maxLength": MAX_TRACE_ID_CHARS,
                        "description": "trace_id from trace_find_slow/trace_find_errors/search",
                    }
                },
                "required": ["trace_id"],
            },
        },
        # --- SRE role tools (reliability, headroom, saturation, error rates, rollback signals) ---
        {
            "name": "sre_error_rate",
            "roles": ["sre"],
            "description": "SRE view of ERROR log events grouped by service and minute. Use for 'is the error rate climbing', 'which service is burning error budget', or 'what should we rollback first' — a rate check, not a root-cause investigation (see investigate_error_rate for that).",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "investigate_error_rate",
            "roles": ["sre"],
            "description": "Root-cause investigation for an elevated error rate — checks errors_by_service, and if elevated, walks correlated log-volume spike and failing-trace checks to find the cause, one hop per call, reproducible, no agent-authored composition. Use for 'why is X's error rate up', 'find the root cause', or 'what's causing the errors' — not just a rate check (see sre_error_rate for that) or a health summary (see sre_service_health for that). Start with no arguments (or node='start'); each response tells you the next call to make.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "node": {
                            "type": "string",
                            "description": "which hop to run; omit or 'start' to begin a new investigation",
                        },
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "required for node='check_log_spike'/'check_traces' — the service name the previous step's response gave you",
                        },
                    },
                    **_since(),
                ),
            },
        },
        {
            "name": "forecast_capacity",
            "roles": ["sre"],
            "description": "Forecast when an allowlisted host gauge may reach its ceiling using a native series fit. Use for 'when will memory fill?' or 'at this trend when does capacity run out?'. Refuses unreliable trends instead of inventing a date.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "metric": {"type": "string", "enum": sorted(_FORECAST_METRICS)},
                        "host": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "optional host.name filter",
                        },
                    },
                    **_since(),
                ),
                "required": ["metric"],
            },
        },
        {
            "name": "sre_host_headroom",
            "roles": ["sre"],
            "description": "SRE view of host CPU load and memory used side-by-side. Use for 'which host is hottest', 'where is headroom lowest', or 'which VM is nearest saturation'.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "sre_ingest_health",
            "roles": ["sre"],
            "description": "SRE view of Berserk ingest lag and dropped-data signals per host. Use for 'is ingest healthy', 'are we dropping telemetry', or 'is observability lagging'.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "sre_service_health",
            "roles": ["sre"],
            "description": "SRE health rollup for one service: total events, error count, logs, metrics, last seen. Use for 'is service X healthy' or 'rollback signal for X'.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "service.name value",
                        }
                    },
                    **_since(),
                ),
                "required": ["service"],
            },
        },
        {
            "name": "sre_top_error_messages",
            "roles": ["sre"],
            "description": "SRE summary of the most repeated error messages by service. Use for 'what error is dominating', 'top error signatures', or 'which message to investigate first'.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        # --- SOC role tools (anomalies, spikes, first-seen, repeated failures, incident timelines) ---
        {
            "name": "soc_high_severity_logs",
            "roles": ["soc"],
            "description": "SOC view of recent CRITICAL/FATAL/ERROR logs with service and message text. Use for 'show critical events', 'recent incident logs', or 'what looks severe right now'.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "soc_log_spike",
            "roles": ["soc"],
            "description": "SOC view of per-minute log volume for each service over the window: raw counts, no statistics. Use for 'which source is spiking', 'log volume per minute', or 'suspicious burst of logs'. To test whether volume is statistically abnormal, use detect_anomalies.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "soc_new_services",
            "roles": ["soc"],
            "description": "SOC view of services ordered by their earliest event inside the query window (first seen in this window, not first ever), with last-seen and event counts. Use for 'list services by when they first appeared in the last N hours'. To find sources Berserk has never seen before, use detect_new_sources.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "soc_repeated_errors",
            "roles": ["soc"],
            "description": "SOC view of error messages that appear more than 5 times — potential probes, loops, or persistent incidents. Use for 'what keeps repeating' or 'show recurring failures'.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "soc_timeline",
            "roles": ["soc"],
            "description": "SOC incident timeline for one service: timestamps, severity, metric names, and message snippets. Use for 'timeline for service X' or 'reconstruct incident for X'.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "service": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "service.name value",
                        }
                    },
                    **_since(),
                ),
                "required": ["service"],
            },
        },
        # --- Claude Code activity (service.name == 'claude-code'); low-volume, keep windows bounded ---
        {
            "name": "claude_recent",
            "roles": ["claude"],
            "description": "Recent Claude Code (or other ingested agent) activity (timestamp, type, role, model, tool names, error flag), newest first. Default window 1h.",
            "inputSchema": {"type": "object", "properties": dict(_since(), **_agent_prop())},
        },
        {
            "name": "claude_sessions",
            "roles": ["claude"],
            "description": "Claude Code (or other ingested agent) sessions rollup: events, first/last seen, assistant turns, tool turns, and error count per session. Default 6h.",
            "inputSchema": {"type": "object", "properties": dict(_since(), **_agent_prop())},
        },
        {
            "name": "claude_tools",
            "roles": ["claude"],
            "description": "Claude Code (or other ingested agent) tool-use histogram — how many times each tool (Bash, Edit, Read, ...) was used. Default 6h.",
            "inputSchema": {"type": "object", "properties": dict(_since(), **_agent_prop())},
        },
        {
            "name": "claude_errors",
            "roles": ["claude"],
            "description": "Claude Code (or other ingested agent) tool errors — failed tool results (is_error=true) with a body snippet. Not a cross-session view of which tools fail most, or repeated hotspot patterns — see claude_workflow_insights for that. Default 6h.",
            "inputSchema": {"type": "object", "properties": dict(_since(), **_agent_prop())},
        },
        {
            "name": "claude_search",
            "roles": ["claude"],
            "description": "Full-text search across Claude Code, Codex CLI, or another ingested agent's message and tool bodies for a substring. Default 6h.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "term": {
                            "type": "string",
                            "maxLength": MAX_SEARCH_TERM_CHARS,
                            "description": "substring to find; may not contain quotes, pipe, backslash, backtick, or controls",
                        }
                    },
                    **_since(),
                    **_agent_prop(),
                ),
                "required": ["term"],
            },
        },
        {
            "name": "claude_loop_check",
            "roles": ["claude"],
            "description": "Claude Code loop detector, scanning across sessions to find ones stuck in a loop — does not take a session_id. If the prompt already names a specific session (has a session_id), use claude_session_deep_dive instead for that session's full timeline, loop verdict included. Heuristically flags sessions that repeat the same tool/target, retry errors, or oscillate between the same calls. Bodies are truncated; output is diagnostic, not raw transcript replay. Default 6h.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "claude_model_fit",
            "roles": ["claude"],
            "description": "Claude Code model-fit heuristic. Uses observed tool count, errors, duration, and loop signals to flag frontier models on trivial work or cheap models on complex/repetitive work. Not a billing statement. Default 6h.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "claude_token_burn",
            "roles": ["claude"],
            "description": "Claude Code token-burn analysis. Uses exact claude.tokens_input/output usage when present, falls back to a labeled body-length estimate per session, computes burn per distinct tool/file target, and joins high burn with loop signals. Not a ranking across many sessions — see claude_workflow_insights for that. Default 6h.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "claude_quota_status",
            "roles": ["claude"],
            "description": "Live Claude Code quota-window check. Tries a real-time reading from Anthropic's own account usage endpoint first (macOS only, reads Claude Code's local Keychain credential — an undocumented endpoint, so treat exact fields as best-effort); falls back to a log-derived token estimate over the trailing window when the live path is unavailable for any reason. Does not require the ingestion daemon/forwarder to be running. 'since' only affects the fallback window, default 5h.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "claude_cost_report",
            "roles": ["claude"],
            "description": "Claude Code multi-day cost report: per-day token burn with exact/estimated labeling, per-model split, optional per-project attribution from file paths, and a burn-growing/flat/declining trend verdict. Default 7d.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "group_by": {
                            "type": "string",
                            "enum": ["day", "model", "project"],
                            "description": "Aggregation: by day (default), model, or inferred project.",
                        }
                    },
                    **_since(),
                ),
            },
        },
        {
            "name": "claude_session_deep_dive",
            "roles": ["claude"],
            "description": "Timeline drilldown for one Claude Code session: contiguous tool phases with error counts, activity gaps over 5 minutes, cumulative token burn (exact/estimated), and a loop verdict. Requires session_id (find them via claude_sessions).",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "session_id": {
                            "type": "string",
                            "maxLength": agent_analytics.MAX_SESSION_ID_CHARS,
                            "description": "claude.session_id value",
                        }
                    },
                    **_since(),
                ),
                "required": ["session_id"],
            },
        },
        {
            "name": "claude_workflow_insights",
            "roles": ["claude"],
            "description": "Cross-session Claude Code workflow patterns: most common tool sequences, error hotspots by tool+target, and top-decile burn-per-target sessions. Use for 'how is my agent working overall?', 'where are the error hotspots', or 'which sessions are burning the most tokens'. Default 7d.",
            "inputSchema": {"type": "object", "properties": _since()},
        },
        {
            "name": "claude_spend_overview",
            "roles": ["claude"],
            "description": "Enterprise Claude spend overview using exact native/legacy token classes and a versioned public pricing catalog. Groups by day, team, portfolio, project, repository, feature, work item, agent, harness, or model and always reports pricing/attribution coverage.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": _since()["since"],
                    "group_by": {
                        "type": "string",
                        "enum": [
                            "day",
                            "team",
                            "portfolio",
                            "project",
                            "repository",
                            "feature",
                            "work_item",
                            "agent",
                            "harness",
                            "model",
                        ],
                        "default": "day",
                    },
                    "team": {"type": "string"},
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "feature": {"type": "string"},
                    "agent": {"type": "string"},
                    "harness": {"type": "string"},
                    "model": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        },
        {
            "name": "claude_feature_cost",
            "roles": ["claude"],
            "description": "Feature delivery economics: planned/actual developer hours, planned/actual AI API-equivalent cost, forecast, attribution, and delivery signals for one governed feature.",
            "inputSchema": {
                "type": "object",
                "properties": {"feature_id": {"type": "string"}, "since": _since()["since"]},
                "required": ["feature_id"],
            },
        },
        {
            "name": "claude_project_economics",
            "roles": ["claude"],
            "description": "Project and codebase economics across governed features: developer hours, AI cost, budget, attribution, and feature-level breakdown.",
            "inputSchema": {
                "type": "object",
                "properties": {"project_id": {"type": "string"}, "since": _since()["since"]},
                "required": ["project_id"],
            },
        },
        {
            "name": "claude_efficiency_insights",
            "roles": ["claude"],
            "description": "Matched-cohort agent/harness efficiency analysis for cache reuse, context size, tool-result volume, retries, errors, model fit, and cost per successful outcome. Not a per-session or cross-session token-burn ranking — see claude_token_burn (one session) or claude_workflow_insights (across sessions) for that.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": _since()["since"],
                    "project": {"type": "string"},
                    "agent": {"type": "string"},
                    "harness": {"type": "string"},
                    "model": {"type": "string"},
                },
            },
        },
        {
            "name": "claude_harness_recommendations",
            "roles": ["claude"],
            "description": "Generate deterministic, evidence-backed harness amendments. Only findings with sufficient samples/confidence are approval-eligible; this tool never modifies a harness.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": _since()["since"],
                    "project": {"type": "string"},
                    "agent": {"type": "string"},
                    "harness": {"type": "string"},
                    "model": {"type": "string"},
                },
            },
        },
        {
            "name": "claude_record_recommendation_decision",
            "roles": ["claude"],
            "description": "Record an approved, rejected, or deferred harness recommendation as a privacy-safe append-only audit event. Does not apply the amendment.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recommendation_id": {"type": "string", "pattern": "^rec_[a-f0-9]{16}$"},
                    "decision": {"type": "string", "enum": ["approved", "rejected", "deferred"]},
                    "owner": {
                        "type": "string",
                        "description": "Owner identity; stored only as a deployment-scoped HMAC pseudonym.",
                    },
                    "rationale": {"type": "string", "maxLength": 1000},
                },
                "required": ["recommendation_id", "decision", "owner", "rationale"],
            },
        },
        {
            "name": "claude_optimization_impact",
            "roles": ["claude"],
            "description": "Compare matched pre/post harness cohorts and return keep, no-material-change, rollback, or insufficient-data using cost, error, and success signals.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_profile": {"type": "string"},
                    "before_harness": {"type": "string"},
                    "after_harness": {"type": "string"},
                    "project": {"type": "string"},
                    "since": _since()["since"],
                },
                "required": ["agent_profile", "before_harness", "after_harness"],
            },
        },
        {
            "name": "claude_management_report",
            "roles": ["claude"],
            "description": "Management-ready portfolio, team, project, or feature report with readable text and a schema-versioned JSON envelope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["portfolio", "team", "project", "feature"],
                        "default": "portfolio",
                    },
                    "identifier": {"type": "string"},
                    "since": _since()["since"],
                },
            },
        },
        {
            "name": "claude_generate_dashboard",
            "roles": ["claude"],
            "description": "Generate a privacy-safe Markdown or self-contained HTML dashboard beneath BERSERK_MCP_REPORT_DIR for use from Claude Code. This is an explicit local write.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dashboard": {
                        "type": "string",
                        "enum": ["portfolio", "project", "feature", "agent_efficiency", "data_quality"],
                        "default": "portfolio",
                    },
                    "identifier": {"type": "string"},
                    "since": _since()["since"],
                    "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown"},
                    "filename": {"type": "string", "maxLength": 128},
                },
            },
        },
        {
            "name": "model_drift_check",
            "roles": ["claude"],
            "description": "Check whether a canaried model still routes as well as it did. Returns stable, degrading, step-change, or insufficient-data per model, with the provider fingerprint status. Measures tool-routing quality only -- not prose, reasoning, or code quality. Use for 'has the model got worse' or 'did the provider change the model'.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "model": {
                            "type": "string",
                            "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                            "description": "optional single model to check",
                        }
                    },
                    **_since(),
                ),
            },
        },
        {
            "name": "model_drift_history",
            "roles": ["claude"],
            "description": "Score and fingerprint history for one canaried model over time, for investigating a flagged drift verdict. Measures tool-routing quality only. Use after model_drift_check reports degrading or step-change.",
            "inputSchema": {
                "type": "object",
                "properties": dict({"model": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS}}, **_since()),
                "required": ["model"],
            },
        },
        {
            "name": "scan_secrets",
            "roles": ["soc"],
            "description": "Audit recent log bodies for potential credentials and optionally selected PII categories. Returns only aggregate service/type counts and first-seen timestamps; secret values are never returned. Default 1h.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": _since()["since"],
                    "include_entropy": {
                        "type": "boolean",
                        "description": "Enable false-positive-prone high-entropy token detection.",
                    },
                    "include_pii": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["email", "ipv4", "ipv6", "credit_card"]},
                        "description": "Optional PII categories to include.",
                    },
                },
            },
        },
        {
            "name": "suggest_ingestion",
            "description": "Recommend concrete telemetry sources for a role/use case. With check_gap=true, compares service and metric hints against live Berserk inventory and marks each source present or missing. Catalog-backed and read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role_or_usecase": {
                        "type": "string",
                        "description": "Catalog key such as sre/onprem-ad-health, soc/endpoint-identity, change-management/ansible, or scom.",
                    },
                    "check_gap": {
                        "type": "boolean",
                        "description": "Compare recommendations with live service and metric inventory.",
                    },
                    "since": _since()["since"],
                },
                "required": ["role_or_usecase"],
            },
        },
        # ── CanonLoom knowledge-pipeline tools ────────────────────────────────────
        {
            "name": "canonloom_run_pipeline",
            "description": "Submit a URL to the CanonLoom knowledge lifecycle pipeline. Acquires the source, scores it for relevance, compares with existing skills, and optionally generates a validated skill artifact. Requires CANONLOOM_SERVER_URL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Source URL to process through the pipeline"},
                    "stop_after": {
                        "type": "string",
                        "enum": ["clp1", "clp2", "clp3", "clp4", "clp5"],
                        "description": "Stop after this phase (default: run all phases)",
                    },
                    "auto_promote": {
                        "type": "boolean",
                        "description": "Promote to validated on passing CLP-4 (default: false)",
                    },
                    "record_telemetry": {"type": "boolean", "description": "Record run telemetry (default: true)"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "canonloom_list_artifacts",
            "description": "List skill artifacts in the CanonLoom knowledge repository. By default returns only promoted artifacts (validated/approved/published). Pass include_staging=true to also include draft artifacts in staging. Requires CANONLOOM_SERVER_URL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_staging": {
                        "type": "boolean",
                        "description": "Also return draft artifacts from staging (default: false)",
                    }
                },
            },
        },
        {
            "name": "canonloom_get_artifact",
            "description": "Retrieve a single artifact manifest by artifact_id from the CanonLoom knowledge repository. Requires CANONLOOM_SERVER_URL.",
            "inputSchema": {
                "type": "object",
                "properties": {"artifact_id": {"type": "string", "description": "Artifact ID (art_...)"}},
                "required": ["artifact_id"],
            },
        },
        {
            "name": "canonloom_freshness_report",
            "description": "Compute a freshness score for all validated skills in the CanonLoom repository and surface deprecation candidates. Requires CANONLOOM_SERVER_URL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "half_life_days": {
                        "type": "integer",
                        "description": "Days until freshness score halves (default: 365)",
                    },
                    "min_age_days": {
                        "type": "integer",
                        "description": "Minimum age for deprecation candidates (default: 90)",
                    },
                },
            },
        },
        {
            "name": "canonloom_run_history",
            "description": "List recent CanonLoom pipeline runs with outcome, phase, and artifact info. Requires CANONLOOM_SERVER_URL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ok", "rejected"], "description": "Filter by outcome"},
                    "limit": {"type": "integer", "description": "Maximum number of runs to return (default: 20)"},
                },
            },
        },
    ]


def build_mgmt_tools(*, TABLE, MAX_INTERPOLATED_NAME_CHARS, MAX_SEARCH_TERM_CHARS, _since):  # noqa: N803
    return [
        {
            "name": "list_saved",
            "description": "List previously-saved custom queries (name + description). For a non-standard question, CHECK HERE FIRST before writing new KQL.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "run_saved",
            "description": "Run a previously-saved query by name (see list_saved). Deterministic - no KQL authoring.",
            "inputSchema": {
                "type": "object",
                "properties": dict({"name": {"type": "string", "description": "saved query name"}}, **_since()),
                "required": ["name"],
            },
        },
        {
            "name": "save_query",
            "description": "Persist a WORKING KQL query as a reusable named query so it never has to be figured out again. Call this after you answer a non-standard question with a custom search query. The query is run once to verify it works; if it errors it is NOT saved. Replacing an existing saved query of the same name requires overwrite=true.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    {
                        "name": {"type": "string", "description": "short snake_case name"},
                        "description": {"type": "string", "description": "what the query answers"},
                        "kql": {"type": "string", "description": f"KQL starting with '{TABLE} | ...'"},
                        "roles": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "string"},
                            ],
                            "description": "optional role(s) this query serves: sre, soc, claude, ops",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "must be true to replace an existing saved query of the same name",
                        },
                    },
                    **_since(),
                ),
                "required": ["name", "description", "kql"],
            },
        },
        {
            "name": "request_discovery",
            "description": "Queue a newly-added service or metric for author-lane integration. Validates the source is currently visible in Berserk, then records a job for the discovery worker to drain. Use when a user says 'I added / connected / started shipping SOURCE'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                        "description": "service.name to integrate",
                    },
                    "metric": {
                        "type": "string",
                        "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                        "description": "metric name to integrate",
                    },
                    "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"},
                    "requested_by": {"type": "string", "description": "optional requester label"},
                    **_since(),
                },
            },
        },
        {
            "name": "discovery_status",
            "description": "Read-only: list pending and completed discovery jobs for new services or metrics. Use for 'what is in the discovery queue' or 'did that job finish'. It processes nothing; to process the pending jobs, use run_discovery_worker.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "detect_new_sources",
            "description": "Scan Berserk for services/metrics never seen before by comparing against a stored baseline (the first run only records the baseline), and optionally schema drift on known ones. Use for 'anything new reporting?' or 'did a new source appear?', or run with auto_queue=true to queue newcomers for parser generation. For services ordered by first-seen time within a window, see soc_new_services.",
            "inputSchema": {
                "type": "object",
                "properties": dict(
                    _since(),
                    auto_queue={"type": "boolean", "description": "queue newly-detected sources for parser generation"},
                    check_drift={
                        "type": "boolean",
                        "description": "also check known services for resource-key schema drift",
                    },
                ),
            },
        },
        {
            "name": "generate_parser",
            "description": "Generate and verify a query pack for one source right now (synchronous; may take minutes). An LLM authors 2-4 KQL queries from a live schema profile, validates each against Berserk, and saves the survivors. Requires at least one configured LLM provider (HERMES_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                        "description": "service.name to generate a parser for",
                    },
                    "metric": {
                        "type": "string",
                        "maxLength": MAX_INTERPOLATED_NAME_CHARS,
                        "description": "metric_name to generate a parser for",
                    },
                    "role_hint": {"type": "string", "description": "optional target role: sre, soc, claude, ops"},
                },
            },
        },
        {
            "name": "run_discovery_worker",
            "description": "Process the discovery queue: take up to max_jobs pending jobs (default 1, maximum 5), and for each one an LLM authors a verified query pack for the new source. Use for 'process the queue' or 'run the queued discovery jobs'. Requires at least one configured LLM provider; may take minutes per job. To only look at the queue without processing it, use discovery_status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_jobs": {
                        "type": "integer",
                        "description": "max jobs to process this call, default 1, capped at 5",
                    }
                },
            },
        },
        {
            "name": "review_generated",
            "description": "List or inspect LLM-generated saved queries for audit before trusting them. No arg: list all generated queries with their provider/model/timestamp. With name: full entry including the KQL.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "optional: a specific generated query name to inspect in full",
                    }
                },
            },
        },
        {
            "name": "find_tool",
            "description": "Find a tool by what you're trying to do, when the full tool list isn't resident (BERSERK_MCP_DISCOVERY=1). Returns up to 5 candidates with their full inputSchema inline, so no second round trip is needed before calling one. If nothing matches confidently, returns the always-resident anchor set instead and says so explicitly.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "maxLength": MAX_SEARCH_TERM_CHARS,
                        "description": "what you're trying to find out or do, in your own words",
                    }
                },
                "required": ["intent"],
            },
        },
    ]


TITLES = {
    "find_tool": "Find Tool",
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
    "investigate_error_rate": "Investigate: Error Rate",
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
    "claude_quota_status": "Claude Code: Quota Status",
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
    "model_drift_check": "Model Drift Check",
    "model_drift_history": "Model Drift History",
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
