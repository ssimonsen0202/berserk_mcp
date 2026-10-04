"""The learned and saved query store, and wiring for sibling modules.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from berserk_mcp import fencing as bm_fencing
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
from berserk_mcp import tools as bm_tools
from pathlib import Path
import _store
import _tag_guard
import agent_analytics
import ai_finops
import ingestion_advisor
import investigation
import parser_factory
import re
import secret_scan
import unicodedata


# persist_learned_query tells MCP clients that the tool list changed. The
# sender lives in the transport layer, which sits above this store, so the
# transport registers it with set_tools_changed_notifier when it loads.
_tools_changed_notifier = None


def set_tools_changed_notifier(notify):
    """Register the callable that sends notifications/tools/list_changed."""
    global _tools_changed_notifier
    _tools_changed_notifier = notify


def load_learned():
    return _store.load_json_list(bm_config.LEARNED_PATH, logger=bm_config.log)


def save_learned(items):
    bm_config._validate_store_path(bm_config.LEARNED_PATH, "LEARNED_PATH")
    return _store.save_json_list(bm_config.LEARNED_PATH, items, logger=bm_config.log)


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


# Saved queries projected into tools/list as saved__<name> (issue #5). Not
# `_nonnegative_int_env(...) or 25` -- that pattern (used for KQL_MAX_CHARS
# etc.) treats an explicit 0 as "use the default", which is wrong here: 0
# must disable the projection entirely, a real and intended configuration.
SAVED_TOOL_PROJECTION_CAP = bm_config._nonnegative_int_env("BERSERK_MCP_SAVED_TOOL_CAP", 25)


# FR-4: a generated entry's description was authored by an LLM and is
# therefore untrusted (list_saved already fences it for the same reason).
# Projecting it into tools/list moves that untrusted text into a model's
# tool-selection context -- a stronger position than a tool *result* -- so
# the same fencing posture applies here, plus the extra care a routing
# surface needs: no structural tokens a client could misparse.
_DESCRIPTION_CAP = 240


_DESCRIPTION_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


_DESCRIPTION_STRUCTURAL_TOKENS = ("inputSchema", '"tools"', "\n\n---")


# Bounds save_query's input, not the projected 240-char display cap above.
# LEARNED_STORE_CAP (500) bounds entry count only, not bytes -- an unbounded
# description compounds with it into a store tools/list must fully parse on
# every call, a mandatory path for every client.
SAVE_QUERY_DESCRIPTION_MAX_CHARS = 2000


def _saved_query_description(item):
    text = str(item.get("description", ""))
    # tools/call output (e.g. list_saved) is redacted via
    # secret_scan.apply_output_filter at the dispatch() boundary; tools/list
    # has no equivalent boundary, so a saved description must be redacted
    # here or a credential in it goes to every client on every listing --
    # not just the rare caller of list_saved.
    text = secret_scan.apply_output_filter(
        text,
        mode=bm_config.REDACT_MODE,
        include_entropy=bm_config.REDACT_ENTROPY,
        pii_types=bm_config.REDACT_PII_TYPES,
    )
    # Normalize line endings before anything that pattern-matches on them --
    # a CRLF-styled "\r\n\r\n---\r\n" must be caught by the same structural-
    # token check as its LF form, not survive as an unrecognized variant.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # NFKC before any tag matching, for the same reason _fence_untrusted does
    # it: a fullwidth "＜/generated-description＞" is a compatibility variant
    # that collapses to its ASCII form here, so the literal replacement below
    # catches it instead of it surviving as an unrecognized shape.
    text = unicodedata.normalize("NFKC", text)
    text = _DESCRIPTION_CONTROL_CHARS_RE.sub("", text)
    text = text[:_DESCRIPTION_CAP]
    for token in _DESCRIPTION_STRUCTURAL_TOKENS:
        text = text.replace(token, " ")
    # Neutralize literal angle brackets in every saved description, not just
    # generated ones, so a forged "</generated-description>" can't masquerade
    # as a real closing tag and make trailing injected text look like it fell
    # outside the fence. This used to run only for origin == "generated",
    # but every saved description -- generated or user-origin -- lands in the
    # SAME tools/list payload, so a forged tag in a user-origin description
    # can corrupt the fence boundary of a generated one sitting beside it.
    # user-origin text is not trusted either: save_query's description is
    # authored by the model, which may have just read attacker-controlled log
    # content (the premise the whole untrusted-fencing regime rests on).
    # Found 2026-09-06 while validating the Cisco mcp-scanner against a
    # deliberately poisoned learned-query store.
    #
    # The entity-encoded pass must come first: the literal replacement below
    # rewrites "<" and ">" but leaves "&lt;/generated-description&gt;" whole,
    # which still reads as a closing tag to a model. Found the same day by a
    # Codex Security review, which asked whether "entity and Unicode
    # delimiter variants retain security significance at the tool-description
    # LLM trust boundary". They do: 5 of 6 encoded variants survived the
    # literal-only replacement.
    text = _tag_guard.neutralize(text, bm_config._GENERATED_DESC_TAG_RE, "generated-description")
    text = text.replace("<", "(").replace(">", ")")
    # Same rule as built-in descriptions: a saved query projected into a lane
    # must not point the model at a tool hidden there. Applied to the inner
    # text, before the fence is added, so it can never cut the fence.
    filtered = bm_tools._without_hidden_tool_sentences(text, bm_tools._hidden_tool_names())
    text = filtered if filtered is text or filtered.strip() else "Saved query."
    if item.get("origin") == "generated":
        text = "<generated-description>" + text + "</generated-description>"
    return text


def _saved_query_tools():
    """Tool definitions projected from the learned-query store, most recent
    first-class. Never raises: a missing or malformed store must not break
    tools/list, which every client depends on to function at all."""
    if SAVED_TOOL_PROJECTION_CAP <= 0:
        return []
    try:
        items = [it for it in load_learned() if bm_config.item_visible(it)]
    except Exception:
        return []
    tools = []
    for item in items[-SAVED_TOOL_PROJECTION_CAP:]:
        try:
            nm = sanitize_name(item["name"])
        except (KeyError, TypeError):
            continue
        tool = {
            "name": "saved__" + nm,
            "description": _saved_query_description(item),
            "inputSchema": {"type": "object", "properties": bm_tools._since()},
        }
        if item.get("roles"):
            tool["roles"] = item["roles"]
        tools.append(tool)
    return tools


def approve_generated_query(name):
    """Mark a generated query approved (operator CLI). Returns (entry, error)."""
    nm = sanitize_name(name)
    with bm_config._FileLock(bm_config.LEARNED_PATH):
        items = load_learned()
        match = next((it for it in items if it["name"] == nm), None)
        if match is None:
            return None, f"no saved query named {nm!r}"
        if not bm_config.is_generated(match):
            return None, f"{nm!r} is not a generated query; only generated queries need approval"
        match["status"] = bm_config.GENERATED_APPROVED
        match["approved_at"] = bm_config.now_iso()
        save_learned(items)
    return match, None


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
    with bm_config._FileLock(bm_config.LEARNED_PATH):
        all_items = load_learned()
        nm = entry["name"]
        existing = next((it for it in all_items if it["name"] == nm), None)
        is_amendment = existing is not None
        if action_source == "generated":
            # Every generated write starts pending, including a regenerated
            # query replacing an approved one: new KQL needs a new approval.
            entry = {**entry, "origin": "generated", "status": bm_config.GENERATED_PENDING}
            entry.pop("approved_at", None)
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
        "ts": bm_config.now_iso(),
        "name": nm,
        "description": entry.get("description", ""),
        "kql_preview": entry.get("kql", "")[:120],
        "action": "generated" if action_source == "generated" else ("updated" if is_amendment else "created"),
        "role": bm_config.ACTIVE_ROLE,
    }
    # save_learned(items) above already succeeded -- the query is persisted
    # from here on regardless of what follows. The amendments log is a
    # best-effort audit trail (already evicted on a rolling basis below);
    # its failure must not undo the save, raise past the caller, or -- as a
    # prior version did -- skip the list_changed notification for a change
    # that genuinely happened. Wrapped the same way the notification itself
    # already was.
    amendments_path = Path(bm_config.LEARNED_PATH).parent / "amendments_log.json"
    try:
        with bm_config._FileLock(amendments_path):
            amendments = bm_config.load_json_list(amendments_path)
            amendments.append(log_entry)
            amendments = amendments[-1000:]  # cap to prevent unbounded growth
            bm_config.save_json_list(amendments_path, amendments)
    except Exception as exc:
        bm_config.log(f"failed to write amendments log: {type(exc).__name__}: {exc}")
    # This is the single write path for both save_query and the generated
    # writes from generate_parser/run_discovery_worker, and is only reached
    # after a persist actually succeeds -- a rejected save (validation
    # failure, execution failure, refused overwrite) returns before this
    # point, so it never notifies. _list_changed_supported() (not a bare
    # transport check) so this can never fire when the capability was
    # advertised as unsupported, e.g. SAVED_TOOL_PROJECTION_CAP=0. Best-
    # effort: a notification failure must never undo or fail a save that
    # already landed on disk.
    try:
        if _tools_changed_notifier is not None:
            _tools_changed_notifier()
    except Exception as exc:
        bm_config.log(f"failed to send tools/list_changed notification: {type(exc).__name__}: {exc}")
    return log_entry


parser_factory.configure(
    bzrk_search=bm_runner.bzrk_search,
    table=bm_config.TABLE,
    # A callable, not a captured Path: tests monkeypatch bm.LEARNED_PATH
    # per-test to isolate stores into a tempdir, so this must resolve
    # LEARNED_PATH fresh on every call rather than freezing it here at
    # import time.
    get_store_dir=lambda: Path(bm_config.LEARNED_PATH).parent,
    ensure_private_dir=bm_config._ensure_private_dir,
    now_iso=bm_config.now_iso,
    log=bm_config.log,
    persist_learned_query=persist_learned_query,
    sanitize_name=sanitize_name,
    validate_static=bm_runner._parser_static_validation,
    schema_context_provider=bm_runner._parser_schema_context,
    redact=lambda text: secret_scan.redact(
        text,
        include_entropy=True,
        pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
)


agent_analytics.configure(
    bzrk_search=bm_runner.bzrk_search_json,
    table=bm_config.TABLE,
    redact=lambda text: secret_scan.redact(
        text,
        include_entropy=True,
        pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
    fence=lambda text: bm_fencing._fence_untrusted(text, inline=True),
)


investigation.configure(
    bzrk_search=bm_runner.bzrk_search_json,
    since_hours=bm_config._since_hours,
    q_errors=bm_queries.Q_ERRORS,
    q_soc_log_spike=bm_queries.q_soc_log_spike_for_service,
    q_trace_find_errors=bm_queries.q_trace_find_errors_for_service,
    q_services=bm_queries.Q_SERVICES,
)


def finops_since_hours(since):
    """Window length in hours for ai_finops, after the same normalization
    bzrk_search applies; None when the value is not a valid window."""
    since = bm_runner._normalize_since(since)
    if not bm_runner.valid_since(since):
        return None
    return bm_config._since_hours(since) or None


ai_finops.configure(
    search=bm_runner.bzrk_search_json,
    table=bm_config.TABLE,
    redact=lambda text: secret_scan.redact(
        text,
        include_entropy=False,
        pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
    redact_aggressive=lambda text: secret_scan.redact(
        text,
        include_entropy=bm_config.FINOPS_REDACT_ENTROPY,
        pii_types=secret_scan.ALL_PII_TYPES,
    )[0],
    catalog_path=bm_config.FINOPS_PRICING_CATALOG_PATH,
    business_store_path=bm_config.FINOPS_BUSINESS_STORE_PATH,
    decision_store_path=bm_config.FINOPS_DECISION_STORE_PATH,
    pseudonym_key_path=bm_config.FINOPS_PSEUDONYM_KEY_PATH,
    report_dir=bm_config.FINOPS_REPORT_DIR,
    otlp_endpoint=bm_config.FINOPS_OTLP_ENDPOINT,
    otlp_headers=bm_config.FINOPS_OTLP_HEADERS,
    since_hours=finops_since_hours,
)


secret_scan.configure(
    bzrk_search=bm_runner.bzrk_search_json,
    table=bm_config.TABLE,
)


ingestion_advisor.configure(
    list_services=lambda since: bm_runner.bzrk_search(bm_queries.Q_SERVICES, since),
    list_metrics=lambda since: bm_runner.bzrk_search(bm_queries.Q_METRICS, since),
)
