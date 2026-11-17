"""Handlers for the learning loop, jobs and discovery.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import learned as bm_learned
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
from berserk_mcp import usage as bm_usage
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
    # Whitespace collapsed: valid_since() accepts it, and since is
    # written into the server-text header, outside the fence.
    since = bm_tools.display_since(since_arg or match.get("since") or "1h ago")
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
    _record_usage(match, out, err)
    if err:
        return prefix + bm_fencing._fence_untrusted(out), err
    if not bm_config.ENVELOPE_ENABLED:
        return prefix + bm_fencing._fence_limited(out), err
    try:
        # Empty means zero rows only: a saved count() of 0 is an answer.
        if bm_tools.result_row_count(out) == 0:
            return prefix + _empty_saved_text(match, since), err
        header = bm_tools._envelope_header(
            "saved:" + bm_learned.sanitize_name(match["name"]),
            "saved__" + bm_learned.sanitize_name(match["name"]),
            since,
            out,
            extra=_saved_header_extra(match),
        )
    except Exception:
        return prefix + bm_fencing._fence_limited(out), err
    # The header holds only counts, a sanitised name and a digest of the rows,
    # so no telemetry text reaches it; the rows themselves are fenced.
    body = bm_fencing._fence_limited(out)
    return prefix + header + "\n\n" + body, err  # nosemgrep: unfenced-bzrk-output-reaches-return


def _record_usage(entry, out, err):
    """Count this run in usage_stats.json. Best effort: usage.record never
    raises, and a failure to classify the result is only a missed count."""
    try:
        outcome = "error" if err else ("empty" if bm_tools.result_row_count(out) == 0 else "rows")
        bm_usage.record(bm_learned.sanitize_name(entry["name"]), outcome)
    except Exception as exc:
        bm_config.log(f"usage not recorded: {type(exc).__name__}: {exc}")


def _usage_suffix(item, snapshot):
    """Server text after a list_saved line: how often and when the entry ran.
    Built from counters and a validated timestamp only, so no stored text
    reaches it. `snapshot` is one usage.snapshot() read for the whole list."""
    stats = snapshot.get(bm_learned.sanitize_name(item["name"]), {})
    last = bm_usage.valid_timestamp(stats.get("last_called")) or "never"
    return f"  (calls={stats.get('calls', 0)}, last_called={last})"


def _saved_header_extra(entry):
    """origin=, review= (generated only) and rows_verified=false for the header."""
    generated = bm_config.is_generated(entry)
    extra = ["origin=generated" if generated else "origin=manual"]
    if generated:
        approved = entry.get("status") == bm_config.GENERATED_APPROVED
        extra.append("review=approved" if approved else "review=pending")
    if not bm_learned.entry_rows_verified(entry):
        extra.append("rows_verified=false")
    return extra


def _empty_saved_text(entry, since):
    """Server text for a saved query that returned zero rows."""
    text = f"No rows in window {since}. "
    if not bm_learned.entry_rows_verified(entry):
        return (
            text + "This saved query has never been seen returning rows; "
            "do not treat an empty result as a clean answer."
        )
    last_rows = bm_usage.last_rows_at(bm_learned.sanitize_name(entry["name"]))
    if last_rows:
        text += f"It last returned rows at {last_rows}. "
    return text + "Try since='24h ago'."


def _empty_result_check(kql, out, allow_empty):
    """Zero-row check for save_query. Returns (rows_verified, error_text).

    An empty first result triggers one retry over 24h through the same
    bzrk_search_json call, so the query slot and window budget apply. The KQL
    is unchanged, so it is not validated again. allow_empty must be a real
    JSON true (the caller passes `is True`), like overwrite."""
    if not bm_tools.result_is_empty(out):
        return True, None
    out, is_err = bm_runner.bzrk_search_json(kql, "24h ago")
    if is_err:
        return False, "NOT saved - the query failed when verified:\n" + bm_fencing._fence_untrusted(out)
    if not bm_tools.result_is_empty(out):
        return True, None
    if allow_empty:
        return False, None
    return False, (
        "NOT saved - the query ran but matched nothing in 24h. A bare column name "
        "silently matches zero rows; check the field access with discover_schema. "
        "Pass allow_empty=true only if an empty result is the expected normal state."
    )


def _save_query_input_error(name, desc, kql):
    """Error text for save_query input that must be refused before any query
    runs, else None."""
    if not kql or not desc:
        return "save_query needs name, description, and kql."
    if len(name) > bm_learned.MAX_SAVED_NAME_CHARS:
        return (
            f"name is too long (maximum {bm_learned.MAX_SAVED_NAME_CHARS} characters after "
            "sanitising), because saved__<name> must fit the 64-character tool-name limit."
        )
    if len(desc) > bm_learned.SAVE_QUERY_DESCRIPTION_MAX_CHARS:
        return (
            f"description is too long (maximum {bm_learned.SAVE_QUERY_DESCRIPTION_MAX_CHARS} "
            "characters). LEARNED_STORE_CAP (500) bounds entry count, not size -- "
            "tools/list parses the whole store on every call, so an unbounded "
            "description compounds into a mandatory-path cost for every client."
        )
    return _hidden_name_refusal(name)


def _hidden_name_refusal(name):
    """Refusal text when `name` is held by an entry this role or tier cannot
    see, else None. Runs before any query, and says nothing about why, so a
    hidden entry's role, description and KQL stay hidden; overwrite=true
    cannot replace it."""
    held = next((it for it in bm_learned.load_learned() if it.get("name") == name), None)
    if held is not None and not bm_config.item_visible(held):
        return bm_learned.HIDDEN_NAME_MESSAGE.format(name=name)
    return None


def _visible_duplicate(all_items, name, kql):
    """A visible saved entry under another name with the same KQL fingerprint."""
    fingerprint = bm_learned.kql_fingerprint(kql)
    return next(
        (
            it
            for it in all_items
            if it.get("name") != name and bm_config.item_visible(it) and bm_learned.entry_fingerprint(it) == fingerprint
        ),
        None,
    )


LIST_SAVED_QUERY_MAX_CHARS = 200
LIST_SAVED_QUERY_LIMIT = 10


def _list_saved_ranked(query):
    """list_saved with a query: (text, is_error) for the best matches, or None
    when no query was given and the caller lists every entry."""
    if query is None:
        return None
    if not isinstance(query, str):
        return "query must be a string", True
    if len(query) > LIST_SAVED_QUERY_MAX_CHARS:
        return f"query is too long (maximum {LIST_SAVED_QUERY_MAX_CHARS} characters)", True
    if not query.strip():
        return None
    # Same index as find_tool, so a client that ignores list_changed can
    # still find a saved query added mid-session.
    ranked = bm_learned.search_saved_entries(query, LIST_SAVED_QUERY_LIMIT)
    if not ranked:
        return "No saved queries match that query.", False
    snapshot = bm_usage.snapshot()
    lines = [
        "- "
        + bm_learned.projectable_name(it)
        + ": "
        + bm_learned._saved_query_description(it)
        + _usage_suffix(it, snapshot)
        for it in ranked
        if bm_learned.projectable_name(it)
    ]
    return "Saved queries (best match first):\n" + "\n".join(lines), False


def _list_saved_all():
    """list_saved without a query: every visible entry, in store order."""
    items = [it for it in bm_learned.load_learned() if bm_config.item_visible(it) and bm_learned.projectable_name(it)]
    if not items:
        return "No saved queries yet.", False
    lines = []
    snapshot = bm_usage.snapshot()
    for item in items:
        # Same sanitising and fence as the tools/list projection, so a
        # forged closing tag cannot end the fence early.
        description = bm_learned._saved_query_description(item)
        # Stored names are not trusted either; show the sanitised form,
        # the same name saved__ dispatch and run_saved resolve.
        lines.append("- " + bm_learned.projectable_name(item) + ": " + description + _usage_suffix(item, snapshot))
    return "Saved queries:\n" + "\n".join(lines), False


def _handle_learning_loop(name, arguments):
    """list_saved / run_saved / save_query. Returns (text, is_error) or None."""
    if name == "list_saved":
        ranked = _list_saved_ranked(arguments.get("query"))
        if ranked is not None:
            return ranked
        return _list_saved_all()
    if name == "run_saved":
        qn = bm_learned.sanitize_name(arguments.get("name", ""))
        items = [it for it in bm_learned.load_learned() if bm_config.item_visible(it)]
        # Same lookup as saved__* dispatch, so both resolve the same entry.
        match = next((it for it in items if bm_learned.sanitize_name(it["name"]) == qn), None)
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
        refusal = _save_query_input_error(nm, desc, kql)
        if refusal:
            return refusal, True
        validation_report = None
        if bm_config.KQL_VALIDATION_MODE != "off":
            validation_report = bm_runner._validate_user_kql(kql, since)
            if bm_runner._blocking_validation(validation_report, persistence=True):
                return bm_runner._format_validation_rejection(validation_report), True
        out, is_err = bm_runner.bzrk_search_json(kql, since)
        if is_err:
            return "NOT saved - the query failed when verified:\n" + bm_fencing._fence_untrusted(out), True
        rows_verified, empty_error = _empty_result_check(kql, out, arguments.get("allow_empty") is True)
        if empty_error:
            # Safe: fixed server text, or bzrk text already fenced in _empty_result_check.
            return empty_error, True  # nosemgrep: unfenced-bzrk-output-reaches-return
        all_items = bm_learned.load_learned()
        same_name = next((it for it in all_items if it["name"] == nm), None)
        is_amendment = same_name is not None
        # Require a real JSON boolean true — a string like "false" is truthy
        # in Python and must not authorize an overwrite.
        if is_amendment and arguments.get("overwrite") is not True:
            return (
                f"A saved query named '{nm}' already exists. Pass overwrite=true to replace it (this will be logged)."
            ), True
        duplicate = _visible_duplicate(all_items, nm, kql)
        if duplicate is not None:
            # Entries the caller cannot see are skipped: naming one, or
            # refusing because of one, would reveal that it exists.
            return (
                f"NOT saved - the same KQL is already saved as '{duplicate['name']}'. "
                f"Use run_saved name={duplicate['name']}. overwrite=true does not bypass this check."
            ), True
        entry = {"name": nm, "description": desc, "kql": kql, "since": since, "rows_verified": rows_verified}
        if rows_verified:
            entry["rows_verified_at"] = bm_config.now_iso()
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
        try:
            bm_learned.persist_learned_query(entry, action_source="manual")
        except bm_learned.HiddenNameError:
            return bm_learned.HIDDEN_NAME_MESSAGE.format(name=nm), True
        except bm_learned.StoreFullError as exc:
            # Nothing was written: the store check runs before the save.
            return f"NOT saved - {exc}", True
        verdict = "Verified: returned rows." if rows_verified else "NOT verified to return rows (allow_empty)."
        return "Saved '" + nm + "'. " + verdict + " Reusable now via run_saved name=" + nm + ".", False
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
