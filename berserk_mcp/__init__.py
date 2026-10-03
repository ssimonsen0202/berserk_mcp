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
  BERSERK_MCP_CACHE_MAX_ENTRIES entries kept in the result cache and the fail-cooldown table (default: "256")
  BERSERK_MCP_KQL_VALIDATION validation policy: off/warn/strict (default: "warn")
  BERSERK_MCP_KQL_LIVE_VALIDATION enable validate_kql mode=live (default: "0")
  BERSERK_MCP_MAX_CONCURRENT_QUERIES in-process query concurrency (default: "2")
  BERSERK_MCP_KQL_MAX_CHARS maximum user KQL length (default: "50000")
  BERSERK_MCP_KQL_MAX_ROWS recommended arbitrary-query row bound (default: "2000")
  BERSERK_MCP_KQL_STATS stats handling: off/auto/required (default: "auto")
  BERSERK_MCP_MAX_RESULT_BYTES hard cap for bzrk stdout (default: 10485760)
  BERSERK_MCP_MAX_OUTPUT_CHARS characters of search/saved-query result sent to the model; 0 = no cap (default: "40000")
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
import os
import time
import urllib.error
import random
from pathlib import Path

import _http
import agent_analytics
import ai_finops
import parser_factory
import secret_scan

from berserk_mcp._version import __version__ as __version__
from berserk_mcp import _facade
from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import runner as bm_runner
from berserk_mcp import httpconfig as bm_httpconfig
from berserk_mcp import doctor as bm_doctor
from berserk_mcp.handlers import learning as bm_learning
from berserk_mcp import server as bm_server


# ---------- configuration (env-overridable) ----------


# ---------- learned-query store ----------


# ── CanonLoom knowledge-pipeline bridge ──────────────────────────────────────


# ---------- --doctor / self_check preflight ----------
# Fleet/air-gapped deployments can't iterate interactively (see F-008's
# fail-fast comment above, extended here from one env var to the whole
# config surface): everything from a missing `bzrk` binary to a wrong
# BZRK_PROFILE to an empty database fails late and opaquely, addressed to
# an agent rather than an operator. These checks give one ordered,
# pass/fail/skip readiness report usable as a fleet probe, a container
# healthcheck, or something an agent hitting repeated errors can call
# itself to tell "wired wrong" apart from "genuinely nothing to report".


def main():
    import argparse

    cli = argparse.ArgumentParser(
        prog="berserk-mcp",
        description="Berserk MCP observability server",
        add_help=True,
    )
    cli.add_argument("--worker", action="store_true", help="run one headless discovery pass (for cron)")
    cli.add_argument("--agent-report", action="store_true", help="run Claude Code agent analytics report")
    cli.add_argument(
        "--agent-report-mode",
        choices=("operational", "daily", "weekly"),
        default="operational",
        help="agent report depth",
    )
    cli.add_argument("--agent-report-json", action="store_true", help="emit a machine-readable agent report envelope")
    cli.add_argument(
        "--canary-run",
        action="store_true",
        help="run the model canary for BERSERK_MCP_CANARY_MODELS and ingest results",
    )
    cli.add_argument(
        "--drift-report",
        action="store_true",
        help="evaluate stored canary history; exit non-zero if any model is degrading",
    )
    cli.add_argument("--auto-queue", action="store_true", help="(worker) queue newly detected sources")
    cli.add_argument("--max-jobs", type=int, default=3, help="(worker) max discovery jobs to drain")
    cli.add_argument("--check-drift", action="store_true", help="(worker) check known services for schema drift")
    cli.add_argument("--since", default="6h ago", help="(agent-report) time window")
    cli.add_argument(
        "--import-business-data",
        choices=("feature", "effort"),
        help="import governed feature catalog or developer-effort records",
    )
    cli.add_argument("--input", help="input CSV/JSON/NDJSON file for business-data import")
    cli.add_argument(
        "--input-format", choices=("csv", "json", "ndjson", "jsonl"), help="override business-data input format"
    )
    cli.add_argument("--export-bi", action="store_true", help="export management-ready AI FinOps datasets")
    cli.add_argument("--output", help="absolute BI export directory")
    cli.add_argument("--export-format", choices=("csv", "ndjson"), default="csv", help="BI export format")
    cli.add_argument(
        "--generate-dashboard",
        choices=("portfolio", "project", "feature", "agent_efficiency", "data_quality"),
        help="generate a Claude Code dashboard snapshot",
    )
    cli.add_argument("--identifier", help="project/feature identifier for dashboard generation")
    cli.add_argument("--dashboard-format", choices=("markdown", "html"), default="markdown")
    cli.add_argument("--set-hermes-url", metavar="URL", help="persist the Hermes LLM endpoint and exit")
    cli.add_argument(
        "--http", action="store_true", help="serve HTTP transport instead of stdio; requires BERSERK_MCP_HTTP_ENABLE=1"
    )
    cli.add_argument(
        "--doctor", action="store_true", help="run preflight readiness checks and exit (0 pass / 1 degraded / 2 broken)"
    )
    cli.add_argument("--json", action="store_true", help="(--doctor) emit the report as JSON instead of a table")
    cli.add_argument(
        "--approve-generated",
        metavar="NAME",
        help="approve a generated saved query so the small tier can use it; prints what was approved",
    )
    ns = cli.parse_args()
    admin_exit = bm_doctor._run_admin_command(ns)
    if admin_exit is not None:
        sys.exit(admin_exit)
    if ns.import_business_data:
        if not ns.input:
            cli.error("--import-business-data requires --input")
        try:
            result = ai_finops.import_business_data(
                ns.import_business_data,
                ns.input,
                fmt=ns.input_format,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"business-data import failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.export_bi:
        if not ns.output:
            cli.error("--export-bi requires --output")
        if not bm_runner.valid_since(ns.since):
            cli.error("--export-bi received an invalid --since value")
        try:
            manifest = ai_finops.export_bi(ns.since, ns.output, fmt=ns.export_format)
            print(json.dumps(manifest, indent=2, sort_keys=True))
            sys.exit(0)
        except Exception as e:
            print(f"BI export failed: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.generate_dashboard:
        if not bm_runner.valid_since(ns.since):
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
            print(
                f"Saved Hermes URL to {path} (0600). It overrides the "
                f"localhost default; BERSERK_LLM_HERMES_URL still takes priority."
            )
            sys.exit(0)
        except Exception as e:
            print(f"failed to save Hermes URL: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(2)
    if ns.worker:
        sys.exit(
            run_worker_pass(
                auto_queue=ns.auto_queue,
                max_jobs=max(1, min(ns.max_jobs, 5)),
                check_drift=ns.check_drift,
                apply_jitter=True,
            )
        )
    if ns.agent_report:
        sys.exit(
            run_agent_report(
                since=ns.since,
                mode=ns.agent_report_mode,
                output_json=ns.agent_report_json,
            )
        )
    if ns.canary_run:
        models = [m.strip() for m in os.environ.get("BERSERK_MCP_CANARY_MODELS", "").split(",") if m.strip()]
        if not models:
            print("BERSERK_MCP_CANARY_MODELS is unset; nothing to do.")
            sys.exit(0)
        repeats = int(os.environ.get("BERSERK_MCP_CANARY_REPEATS", "3"))
        cases_path = os.environ.get(
            "BERSERK_MCP_CANARY_CASES", str(bm_config.REPO_ROOT / "evals" / "canary_cases.jsonl")
        )
        sys.exit(run_canary_pass(models, cases_path, repeats))
    if ns.drift_report:
        sys.exit(run_drift_report())
    if ns.http or bm_config.HTTP_ENABLE:
        try:
            bm_server._serve_http()
        except bm_httpconfig.HttpConfigError as e:
            print(f"HTTP configuration error: {e}", file=sys.stderr)
            sys.exit(2)
    bm_server._serve_mcp()


def _post_discord_alert(text):
    """POST a text alert to the local Discord bridge (see
    DISCORD_ALERT_URL/_SECRET above). No-ops silently if the secret isn't
    configured -- this is an opt-in feature for the --worker cron path,
    never a requirement. Never raises: a failed or unconfigured alert must
    never affect the worker pass's own exit code or job outcomes.

    Returns True on a confirmed post, False otherwise (unconfigured,
    validation failure, network error, or a non-2xx bridge response).
    """
    if not bm_config.DISCORD_ALERT_SECRET:
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
        _http.validate_http_url(bm_config.DISCORD_ALERT_URL, label="discord alert endpoint")
    except _http.UrlPolicyError as e:
        bm_config.log(f"discord alert: endpoint rejected: {e}")
        return False
    payload = json.dumps({"text": text[: bm_config.DISCORD_ALERT_MAX_CHARS]}).encode("utf-8")
    try:
        status = _http.post_bytes_status(
            bm_config.DISCORD_ALERT_URL,
            {
                "Content-Type": "application/json",
                "X-Auth-Token": bm_config.DISCORD_ALERT_SECRET,
            },
            payload,
            timeout=10,
            label="discord alert endpoint",
        )
        return 200 <= status < 300
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        bm_config.log(f"discord alert: bridge returned HTTP {code}")
        return False
    except Exception as e:
        bm_config.log(f"discord alert failed: {type(e).__name__}")
        return False


_AMENDMENT_EMOJI = {"generated": "\U0001f916", "updated": "✏️", "created": "✨"}


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
    amendments_path = Path(bm_config.LEARNED_PATH).parent / "amendments_log.json"
    with bm_config._FileLock(amendments_path):
        amendments = bm_config.load_json_list(amendments_path)
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
            bm_config.save_json_list(amendments_path, [])
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
    if apply_jitter and bm_config.WORKER_JITTER_SECONDS > 0:
        delay = random.uniform(0, bm_config.WORKER_JITTER_SECONDS)
        bm_config.log(f"worker startup jitter: sleeping {delay:.1f}s (max {bm_config.WORKER_JITTER_SECONDS:g}s)")
        time.sleep(delay)

    detect_summary = parser_factory.detect_new_sources(
        since="24h ago",
        auto_queue=auto_queue,
        check_drift=check_drift,
        load_json_list=bm_config.load_json_list,
        save_json_list=bm_config.save_json_list,
        discovery_queue_path=bm_config.DISCOVERY_QUEUE_PATH,
        active_role=bm_config.ACTIVE_ROLE,
    )
    print(detect_summary)

    outcomes, any_needs_human = bm_learning._drain_pending_jobs(max_jobs)
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
    if not bm_runner.valid_since(since):
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
            effective_since,
            group_by="project",
            limit=20,
        )
    if output_json:
        print(
            json.dumps(
                {
                    "schema_version": ai_finops.SCHEMA_VERSION,
                    "mode": mode,
                    "since": effective_since,
                    "operational_report": text,
                    "spend_report": spend_text,
                    "alert": bool(should_alert),
                    "spend_error": bool(spend_error),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(text)
        if spend_text:
            print("\n" + spend_text)
    return 1 if should_alert or spend_error else 0


def run_drift_report(since="30d ago"):
    """Evaluate stored canary history and return an exit code: 2 if the
    query itself failed, 1 if any model is degrading/step-change, else 0.
    A standalone function, not inline in main(), for the same reason as
    run_canary_pass -- testable in isolation, and every path already
    returns explicitly so there is nothing to accidentally fall through.

    group_by_model() raising BzrkResultParseError (an older bzrk build's
    non-JSON output) must be caught and reported as a failure here, not
    left to propagate as an uncaught exception or -- the actual bug this
    fixes -- silently treated as "no data", which made this function
    print "All models stable" and return 0 on a totally broken read path
    (found by Codex backtest, 2026-09-02)."""
    import model_drift

    out, err = bm_runner.bzrk_search_json(model_drift.series_kql(None), since)
    if err:
        print(f"Drift report failed: {out}", file=sys.stderr)
        return 2
    if not out or out.strip() == "(no rows)":
        print("No canary results found.")
        return 0

    try:
        grouped = model_drift.group_by_model(out)
    except model_drift.BzrkResultParseError as exc:
        print(f"Drift report failed: could not read canary results: {exc}", file=sys.stderr)
        return 2

    degraded_models = []
    for model_name, series in grouped.items():
        verdict = model_drift.classify(series)
        if verdict["verdict"] in ("degrading", "step-change"):
            degraded_models.append((model_name, verdict))

    if degraded_models:
        lines = ["Models with degraded or changed behavior:"]
        for model_name, verdict in degraded_models:
            fenced = bm_fencing._fence_untrusted(model_name, inline=True)
            lines.append(f"  {fenced}: {verdict['verdict']} ({verdict['confidence']}) — {verdict['reason']}")
        text = "\n".join(lines)
        print(text, file=sys.stderr)
        _post_discord_alert(text)
        return 1
    print("All models stable (tool-routing quality).")
    return 0


def run_canary_pass(models, cases_path, repeats):
    """One headless canary pass for cron: score each configured model
    against the frozen case set, attach fingerprints for successful runs,
    and persist every result via OTLP. Returns an exit code: 1 if any
    result failed to persist (even a successfully-scored one -- see
    canary.emit()'s return value), else 0.

    A standalone function returning an exit code, not inline code in
    main(), matching run_worker_pass/run_agent_report's own convention --
    deliberately, not incidentally. An earlier inline version had no
    return/exit after its loop, so execution fell through into whatever
    CLI branch happened to follow it (the HTTP/MCP server start) on every
    real invocation, turning a one-shot cron command into a hang (found by
    Codex review, 2026-09-02). Structuring this as sys.exit(run_canary_pass(...))
    at the call site makes that class of bug structurally impossible here:
    there is no code path through this function that does not return.
    """
    sys.path.insert(0, str(bm_config.REPO_ROOT / "evals"))
    import canary

    any_emit_failed = False
    for model in models:
        record = canary.run_canary(model, cases_path=cases_path, repeats=repeats)
        if record.get("eval.status") == "ok":
            bm_doctor._attach_fingerprints(record, model)
        # canary.emit() -> ai_finops.emit_otlp_records() returns False when
        # BERSERK_MCP_OTLP_LOGS_ENDPOINT is unset or the POST fails --
        # previously discarded, so a run could spend money, score a model,
        # print "ok", and store nothing, with cron seeing exit 0 throughout
        # (found by Codex review, 2026-09-02).
        emitted = canary.emit([record], int(time.time() * 1_000_000_000))
        if not emitted:
            any_emit_failed = True
        print(f"{model}: {record.get('eval.status')} tool_accuracy={record.get('eval.tool_accuracy')} stored={emitted}")
    return 1 if any_emit_failed else 0


_facade.install(sys.modules[__name__])
