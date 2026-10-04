"""Handlers for the learning loop, jobs and discovery.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import learned as bm_learned
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
import hmac
import os
import parser_factory


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
    with bm_config._FileLock(bm_config.DISCOVERY_QUEUE_PATH):
        queue = bm_config.load_json_list(bm_config.DISCOVERY_QUEUE_PATH)
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
            outcomes.append(f"- {job['source']}: needs_human ({report.get('reason', '')})")
            any_needs_human = True

    with bm_config._FileLock(bm_config.DISCOVERY_QUEUE_PATH):
        fresh_queue = bm_config.load_json_list(bm_config.DISCOVERY_QUEUE_PATH)
        for it in fresh_queue:
            update = updates.get(_job_identity(it))
            if update is not None:
                it["status"], it["report"] = update
        bm_config.save_json_list(bm_config.DISCOVERY_QUEUE_PATH, fresh_queue)
    return outcomes, any_needs_human


def _run_saved_entry(match, since_arg):
    """Execute one resolved saved-query entry. Shared by run_saved and the
    saved__<name> projected-tool dispatch (issue #5) so the two code paths
    can never drift -- see docs/claude-code-review-feedback-loop.md fault 2
    for what happens when a fix lands at only one call site."""
    since = since_arg or match.get("since") or "1h ago"
    prefix = ""
    if bm_config.KQL_VALIDATION_MODE != "off":
        report = bm_runner._validate_user_kql(match["kql"], since)
        stored_hash = match.get("schema_hash")
        current_hash = report.get("schema", {}).get("schema_hash")
        if stored_hash and current_hash and stored_hash != current_hash:
            prefix = (
                f"Schema drift warning: saved query schema_hash={stored_hash}, "
                f"current={current_hash}. Revalidated before execution.\n"
            )
        if bm_runner._blocking_validation(report):
            return prefix + bm_runner._format_validation_rejection(report), True
    out, err = bm_runner.bzrk_search_json(match["kql"], since)
    if err:
        return prefix + bm_fencing._fence_untrusted(out), err
    return prefix + bm_fencing._fence_limited(out), err


def _handle_learning_loop(name, arguments):
    """list_saved / run_saved / save_query. Returns (text, is_error) or None."""
    if name == "list_saved":
        items = [it for it in bm_learned.load_learned() if bm_config.item_visible(it)]
        if not items:
            return "No saved queries yet.", False
        lines = []
        for item in items:
            description = str(item.get("description", ""))
            if item.get("origin") == "generated":
                description = "<generated-description>" + description + "</generated-description>"
            lines.append("- " + item["name"] + ": " + description)
        return "Saved queries:\n" + "\n".join(lines), False
    if name == "run_saved":
        qn = bm_learned.sanitize_name(arguments.get("name", ""))
        items = [it for it in bm_learned.load_learned() if bm_config.item_visible(it)]
        match = next((it for it in items if it["name"] == qn), None)
        if not match:
            avail = ", ".join(it["name"] for it in items) or "(none)"
            return "No saved query named '" + qn + "'. Available: " + avail, True
        return _run_saved_entry(match, arguments.get("since"))
    if name == "save_query":
        # Operator gate on writing saved queries (they become saved__ tools in
        # every lane). Read per call, compared in constant time; unset = open.
        mgmt_token = os.environ.get("BERSERK_MCP_MGMT_TOKEN", "")
        supplied = str(arguments.get("mgmt_token") or "")
        if mgmt_token and not hmac.compare_digest(supplied.encode("utf-8"), mgmt_token.encode("utf-8")):
            return "save_query requires the management token (mgmt_token); ask the operator", True
        nm = bm_learned.sanitize_name(arguments.get("name", ""))
        desc = str(arguments.get("description", "")).strip()
        kql = str(arguments.get("kql", "")).strip()
        since = arguments.get("since") or "1h ago"
        if not kql or not desc:
            return "save_query needs name, description, and kql.", True
        if len(desc) > bm_learned.SAVE_QUERY_DESCRIPTION_MAX_CHARS:
            return (
                f"description is too long (maximum {bm_learned.SAVE_QUERY_DESCRIPTION_MAX_CHARS} "
                "characters). LEARNED_STORE_CAP (500) bounds entry count, not size -- "
                "tools/list parses the whole store on every call, so an unbounded "
                "description compounds into a mandatory-path cost for every client."
            ), True
        validation_report = None
        if bm_config.KQL_VALIDATION_MODE != "off":
            validation_report = bm_runner._validate_user_kql(kql, since)
            if bm_runner._blocking_validation(validation_report, persistence=True):
                return bm_runner._format_validation_rejection(validation_report), True
        out, is_err = bm_runner.bzrk_search_json(kql, since)
        if is_err:
            return "NOT saved - the query failed when verified:\n" + bm_fencing._fence_untrusted(out), True
        all_items = bm_learned.load_learned()
        is_amendment = any(it["name"] == nm for it in all_items)
        # Require a real JSON boolean true — a string like "false" is truthy
        # in Python and must not authorize an overwrite.
        if is_amendment and arguments.get("overwrite") is not True:
            return (
                f"A saved query named '{nm}' already exists. Pass overwrite=true to replace it (this will be logged)."
            ), True
        entry = {"name": nm, "description": desc, "kql": kql, "since": since}
        if validation_report:
            schema_info = validation_report.get("schema", {})
            entry.update(
                {
                    "validation_version": validation_report.get("validation_version", 1),
                    "validation_risk": validation_report.get("risk"),
                    "schema_hash": schema_info.get("schema_hash"),
                    "schema_status": schema_info.get("schema_status"),
                    "validated_at": bm_config.now_iso(),
                }
            )
        roles = bm_config.normalize_roles(arguments.get("roles"))
        if roles:
            entry["roles"] = roles
        bm_learned.persist_learned_query(entry, action_source="manual")
        return "Saved '" + nm + "'. Reusable now via run_saved name=" + nm + " (verified, returned data).", False
    return None


def _handle_discovery(name, arguments):
    """request_discovery / discovery_status. Returns (text, is_error) or None."""
    if name == "request_discovery":
        service = str(arguments.get("service") or "").strip()
        metric = str(arguments.get("metric") or "").strip()
        if bool(service) == bool(metric):
            return "request_discovery needs exactly one of 'service' or 'metric'.", True
        target = service or metric
        if not bm_queries._valid_interpolated_name(target):
            return "invalid source name (allowed: letters, digits, '.', '_', '-')", True
        kind = "service" if service else "metric"
        since = arguments.get("since") or "1h ago"
        # Exact-match count, not a substring check against the raw output —
        # a short target would otherwise match as a substring of an unrelated
        # service name. `target` is allowlist-validated above, so it is safe
        # to interpolate into the single-quoted KQL literal.
        if kind == "service":
            check_kql = f"{bm_queries.T} | where resource['service.name'] == '{target}' | summarize n=count()"
        else:
            check_kql = f"{bm_queries.T} | where metric_name == '{target}' | summarize n=count()"
        visible, is_err = bm_runner.bzrk_search(check_kql, since)
        if is_err:
            return "Could not verify source visibility:\n" + bm_fencing._fence_untrusted(visible), True
        if bm_runner.count_result_is_zero(visible):
            return f"{target} is not currently visible in Berserk; verify it is ingesting before queueing.", True
        role_hint = bm_config.normalize_roles(arguments.get("role_hint"))
        job = {
            "source": target,
            "kind": kind,
            "role_hint": role_hint[0]
            if role_hint
            else (bm_config.ACTIVE_ROLE if bm_config.ACTIVE_ROLE != "all" else ""),
            "requested_by": str(arguments.get("requested_by") or "").strip() or "manual",
            "status": "pending",
            "ts": bm_config.now_iso(),
        }
        with bm_config._FileLock(bm_config.DISCOVERY_QUEUE_PATH):  # F-007: whole RMW cycle, not just the save
            queue = bm_config.load_json_list(bm_config.DISCOVERY_QUEUE_PATH)
            queue = [
                it
                for it in queue
                if not (it.get("source") == target and it.get("kind") == kind and it.get("status") == "pending")
            ]
            queue.append(job)
            queue = queue[-500:]  # cap to prevent unbounded growth
            bm_config.save_json_list(bm_config.DISCOVERY_QUEUE_PATH, queue)
        return (
            f"{target} queued for integration ({kind}). The author lane will author, verify, and save a query for it.",
            False,
        )
    if name == "discovery_status":
        items = bm_config.load_json_list(bm_config.DISCOVERY_QUEUE_PATH)
        if not items:
            return "No discovery jobs queued.", False
        lines = []
        saved_by_name = {
            entry["name"]: entry
            for entry in bm_learned.load_learned()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        for it in items:
            lines.append(
                f"- {it.get('source', '?')} [{it.get('kind', '?')}] status={it.get('status', '?')} "
                f"role={it.get('role_hint', '') or 'none'} requested_by={it.get('requested_by', '?')} ts={it.get('ts', '?')}"
            )
            report = it.get("report")
            if report:
                if "queries_saved" in report:
                    # Name only the queries this lane can run: a pending
                    # generated query (or another role's) is just counted.
                    saved = report.get("queries_saved", [])
                    shown = [n for n in saved if n in saved_by_name and bm_config.item_visible(saved_by_name[n])]
                    line = f"  -> {report.get('provider', '?')}: saved {', '.join(shown) or 'none usable here'}"
                    if len(saved) > len(shown):
                        line += (
                            f" ({len(saved) - len(shown)} not available here: pending operator "
                            "approval, another role, or removed)"
                        )
                    lines.append(line)
                elif bm_config.ACTIVE_TIER_RESOLVED == bm_config.TIER_SMALL:
                    # A failure reason is pipeline output that can name a
                    # pending query; the small tier cannot act on it anyway.
                    lines.append("  -> not completed; details are shown at the deep tier")
                else:
                    lines.append(f"  -> {report.get('reason', '')}")
        return "Discovery jobs:\n" + "\n".join(lines), False
    return None
