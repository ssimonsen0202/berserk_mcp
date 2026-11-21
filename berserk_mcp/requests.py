"""The request store for `request_query` (learning loop, WP7a).

A small model that cannot answer a question queues it here once. The file
`query_requests.json` sits beside learned.json: a versioned dict of requests
and of rate-limit admissions, written under its own lock through `_store`.

Rules this module keeps:
- A file that exists but is not a valid store stops all admissions (fail
  closed). Only a missing file is a fresh store.
- Question, failed KQL and error text are redacted before they are stored.
  The normalised question computes the id and is never stored.
- Rate-limit charges live apart from requests, so evicting a request never
  refunds a charge.
- Only a `done` entry older than DONE_KEEP_HOURS, or a failed entry past its
  negative-cache period, may be evicted. Pending and running entries never.
- `submit` takes the requests lock only. Never call it while holding the
  learned-store lock, and never take the learned-store lock inside it.
- The worker claims a request with a token before any author call; `complete`
  applies an outcome only while that token still matches. A claim older than
  STALE_CLAIM_SECONDS becomes `needs_human`; a retry goes back through
  `submit`, which charges an admission and honours the negative cache.
- One malformed record makes the whole file unreadable (`_valid_record`);
  the file is never repaired or rewritten during admission.

Nothing here echoes caller text into a reply: the handler builds replies from
server text and from saved-tool names that `item_visible` accepts.
"""

import hashlib
import os
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta

from berserk_mcp import config as bm_config
from berserk_mcp import queries as bm_queries
from berserk_mcp import runner as bm_runner
import _store
import secret_scan

REQUESTS_FILE_NAME = "query_requests.json"
STORE_VERSION = 1
MAX_REQUESTS = 500
MAX_ADMISSIONS = 1000  # bounded admission log; only the last hour is counted
QUESTION_MIN, QUESTION_MAX = 10, 500
FAILED_KQL_MAX, ERROR_MAX = 2000, 400
TRIED_MAX = 5
TRIED_RE = re.compile(r"[a-z0-9_]{1,64}")  # used with fullmatch only
STALE_CLAIM_SECONDS = 1800  # a claim older than this becomes a failure
DONE_KEEP_HOURS = 1  # a done entry may be evicted only after this
_NORM_DROP_RE = re.compile(r"[^a-z0-9 ._-]")
_SPACE_RE = re.compile(r"\s+")
# Unicode categories never allowed in any caller text: control, format
# (zero-width and bidi), surrogate, private use, unassigned, and the line
# and paragraph separators.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _has_forbidden(text, allow=""):
    """True when `text` holds a forbidden character. `allow` lists the only
    control characters a field may keep (newline, tab)."""
    return any(ch not in allow and unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in text)


FAILED = ("needs_human", "unsolvable")
# Lowest rank_discovery score a visible saved tool must reach for request_query
# to offer it before queueing. Interim value (spec A.1: fixed later on the WP13
# eval set). It sits above every score a router case for a built-in tool gets
# against the saved tools of tests/fixtures/learned_legacy_v1_37.json (highest:
# 25.2) and below a plain restatement of a saved entry (about 29 to 30).
# test_saved_match_threshold_clears_every_built_in_tool_router_case guards it.
SAVED_MATCH_MIN_SCORE = 26.0


class StoreUnreadable(Exception):
    """The request file exists but cannot be trusted; admissions stop."""


def requests_path():
    return bm_config.LEARNED_PATH.parent / REQUESTS_FILE_NAME


def _env_int(raw, default, minimum):
    """Integer setting from the raw environment value; anything invalid falls
    back to the documented default."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def negative_ttl_hours():
    return _env_int(os.environ.get("BERSERK_MCP_REQUEST_NEGATIVE_TTL_HOURS"), 1, 1)  # owner decision D4


def requests_per_hour():
    return _env_int(os.environ.get("BERSERK_MCP_REQUESTS_PER_HOUR"), 20, 1)


def normalise_question(question):
    """NFKC, lowercase, collapse whitespace, keep [a-z0-9 ._-]. Exact match
    only (owner decision D11). The normalised form is used for the id and
    is never stored."""
    text = unicodedata.normalize("NFKC", str(question)).lower()
    text = _NORM_DROP_RE.sub("", _SPACE_RE.sub(" ", text))
    return _SPACE_RE.sub(" ", text).strip()


def request_id(question_norm, role):
    return hashlib.sha256((question_norm + "\n" + (role or "")).encode("utf-8")).hexdigest()[:16]


def active_role():
    role = bm_config.ACTIVE_ROLE
    return "" if role in ("", "all") else role


def request_visible(entry):
    """The one visibility rule for requests: the 'all' role sees every
    request; any other role sees only its own."""
    return bm_config.ACTIVE_ROLE == "all" or entry.get("role", "") == active_role()


def _redact(text):
    """Same redactor as the parser factory. Raises on failure: the caller
    refuses the request rather than store anything unredacted."""
    out, _findings = secret_scan.redact(str(text), include_entropy=True, pii_types=secret_scan.ALL_PII_TYPES)
    if not isinstance(out, str):
        raise TypeError("redactor returned non-string")
    return out


def _needs_redaction(value):
    """True when the redactor would change `value` or fails. Identifier fields
    are refused rather than silently rewritten, which would change their
    meaning."""
    try:
        return _redact(value) != value
    except Exception:
        return True


def _optional_str(arguments, key):
    """(value, None) for a missing/None value ('') or a string; (None, error)
    for any other type. An explicitly wrong type is never coerced."""
    value = arguments.get(key)
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, f"'{key}' must be a string."
    return value, None


def validate_arguments(arguments):
    """(clean_dict, None) or (None, error_text). Error texts are server text
    and never echo caller input."""
    question = arguments.get("question")
    if not isinstance(question, str):
        return None, "request_query needs 'question' (a string)."
    if _has_forbidden(question):  # checked before trimming
        return None, "question contains control characters."
    question = question.strip()
    if not QUESTION_MIN <= len(question) <= QUESTION_MAX:
        return None, f"question must be {QUESTION_MIN} to {QUESTION_MAX} characters."
    if not normalise_question(question):
        return None, "question has no letters or digits."
    service, err = _optional_str(arguments, "service")
    if err:
        return None, err
    if service and not bm_queries._valid_interpolated_name(service):
        return None, "invalid service name (allowed: letters, digits, '.', '_', '-')."
    if service and _needs_redaction(service):
        return None, "service looks like a secret or personal data; use the service name."
    since, err = _optional_str(arguments, "since")
    if err:
        return None, err
    if since:
        if _has_forbidden(since):
            return None, "invalid 'since' value."
        since = bm_runner._normalize_since(since.strip())
        if not bm_runner.valid_since(since):
            return None, "invalid 'since' value."
    tried = arguments.get("tried")
    if tried is None:
        tried = []
    if (
        not isinstance(tried, list)
        or len(tried) > TRIED_MAX
        or not all(isinstance(t, str) and TRIED_RE.fullmatch(t) for t in tried)
    ):
        return None, f"'tried' must be a list of at most {TRIED_MAX} tool names matching [a-z0-9_]{{1,64}}."
    if any(_needs_redaction(t) for t in tried):
        return None, "'tried' must hold tool names only."
    failed_kql, err = _optional_str(arguments, "failed_kql")
    if err:
        return None, err
    error, err = _optional_str(arguments, "error")
    if err:
        return None, err
    if len(failed_kql) > FAILED_KQL_MAX or _has_forbidden(failed_kql, allow="\n\t"):
        return None, f"failed_kql must be at most {FAILED_KQL_MAX} characters with no control characters."
    if len(error) > ERROR_MAX or _has_forbidden(error, allow="\n\t"):
        return None, f"error must be at most {ERROR_MAX} characters with no control characters."
    return {
        "question": question,
        "service": service,
        "since": since,
        "tried": list(tried),
        "failed_kql": failed_kql,
        "error": error,
    }, None


def _now():
    return datetime.now(UTC)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(value, now):
    """A stored timestamp. A missing or malformed one counts as `now`, so it
    can only extend a block, never lift one (fail closed)."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return now


def _load(path):
    """The store dict. A missing file is a fresh store; a file that exists
    but is not a valid store raises StoreUnreadable (admissions stop)."""
    if not path.exists():
        return {"version": STORE_VERSION, "requests": [], "admissions": []}
    data = _store.load_json_dict(path, logger=bm_config.log)
    if (
        not isinstance(data, dict)
        or data.get("version") != STORE_VERSION
        or not isinstance(data.get("requests"), list)
        or not isinstance(data.get("admissions"), list)
    ):
        raise StoreUnreadable(str(path.name))
    seen = set()
    for it in data["requests"]:
        if not _valid_record(it) or it["id"] in seen:
            raise StoreUnreadable(str(path.name))
        seen.add(it["id"])
    if not all(isinstance(a, str) for a in data["admissions"]):
        raise StoreUnreadable(str(path.name))
    return data


_ID_RE = re.compile(r"[0-9a-f]{16}")
STATUSES = ("pending", "running", "done") + FAILED


def _valid_record(it):
    """Every field the store relies on is present and well-typed, and the
    status fits its fields. One bad record makes the whole store unreadable;
    it is never dropped or repaired during admission."""
    if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not _ID_RE.fullmatch(it["id"]):
        return False
    if it.get("status") not in STATUSES:
        return False
    for key in ("question", "role", "service", "since", "failed_kql", "error", "ts", "updated"):
        if not isinstance(it.get(key), str):
            return False
    if not isinstance(it.get("tried"), list) or not all(isinstance(t, str) for t in it["tried"]):
        return False
    attempts = it.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        return False
    if not isinstance(it.get("result"), dict):
        return False
    running = it["status"] == "running"
    return running == (isinstance(it.get("claim"), str) and isinstance(it.get("claimed_at"), str))


def _save(path, data):
    _store.save_json_dict(path, data, logger=bm_config.log)


def _evictable(entry, now):
    """Only entries whose protection has run out may be evicted: a done entry
    after DONE_KEEP_HOURS, a failed one after the negative-cache period.
    Pending and running entries never."""
    status = entry.get("status")
    age = now - _ts(entry.get("updated"), now)
    if status == "done":
        return age >= timedelta(hours=DONE_KEEP_HOURS)
    if status in FAILED:
        return age >= timedelta(hours=negative_ttl_hours())
    return False


def submit(clean, role):
    """Queue or answer one request. Returns (state, entry); state is one of
    'queued', 'pending', 'running', 'done', 'failed_recent', 'rate_limited',
    'queue_full', 'unavailable'. Whole read-modify-write under the requests
    lock. Never call while holding the learned-store lock."""
    norm = normalise_question(clean["question"])
    rid = request_id(norm, role)
    path = requests_path()
    try:
        redacted = {k: _redact(clean[k]) for k in ("question", "failed_kql", "error")}
    except Exception:
        return "unavailable", None
    with bm_config._FileLock(path):
        now = _now()  # sampled after the lock is held
        try:
            data = _load(path)
        except StoreUnreadable:
            bm_config.log("request store unreadable; request_query refuses new requests")
            return "unavailable", None
        items = data["requests"]
        existing = next((it for it in items if it["id"] == rid), None)
        if existing is not None:
            status = existing.get("status")
            if status in ("pending", "running", "done"):
                return status, existing
            if status in FAILED and now - _ts(existing.get("updated"), now) < timedelta(hours=negative_ttl_hours()):
                return "failed_recent", existing
        hour_ago = now - timedelta(hours=1)
        admissions = [a for a in data["admissions"] if _ts(a, now) > hour_ago][-MAX_ADMISSIONS:]
        if len(admissions) >= requests_per_hour():
            return "rate_limited", None
        others = [it for it in items if it["id"] != rid]
        if len(others) >= MAX_REQUESTS:
            victims = sorted((it for it in others if _evictable(it, now)), key=lambda it: _ts(it.get("updated"), now))
            need = len(others) - MAX_REQUESTS + 1
            if len(victims) < need:
                return "queue_full", None
            drop = {it["id"] for it in victims[:need]}
            others = [it for it in others if it["id"] not in drop]
        entry = {
            "id": rid,
            "question": redacted["question"],
            "role": role or "",
            "service": clean["service"],
            "since": clean["since"],
            "tried": clean["tried"],
            "failed_kql": redacted["failed_kql"],
            "error": redacted["error"],
            "status": "pending",
            "ts": _iso(now),
            "updated": _iso(now),
            "attempts": int(existing.get("attempts", 0)) if existing else 0,
            "result": {"queries_saved": [], "provider": "", "model": ""},
        }
        data["requests"] = others + [entry]
        data["admissions"] = admissions + [_iso(now)]
        _save(path, data)
        return "queued", entry


def claim(max_jobs):
    """For the worker: atomically mark up to max_jobs pending requests as
    running with a fresh claim token, and return copies. A claim older than
    STALE_CLAIM_SECONDS (a crashed worker) becomes needs_human first."""
    path = requests_path()
    with bm_config._FileLock(path):
        now = _now()
        try:
            data = _load(path)
        except StoreUnreadable:
            return []
        changed = False
        for it in data["requests"]:
            if it.get("status") == "running" and now - _ts(it.get("claimed_at"), now) >= timedelta(
                seconds=STALE_CLAIM_SECONDS
            ):
                # A crashed author run counts as a failure. A retry must come
                # back through submit(), which charges an admission and honours
                # the negative cache: a stale claim is never silently re-run.
                it["status"] = "needs_human"
                it["updated"] = _iso(now)
                it["attempts"] = int(it.get("attempts", 0)) + 1
                it["result"] = {"queries_saved": [], "provider": "", "model": "", "reason": "author run did not finish"}
                it.pop("claim", None)
                it.pop("claimed_at", None)
                changed = True
        picked = []
        for it in data["requests"]:
            if len(picked) >= max_jobs:
                break
            if it.get("status") == "pending":
                it["status"] = "running"
                it["claim"] = secrets.token_hex(8)
                it["claimed_at"] = _iso(now)
                picked.append(dict(it))
        if picked or changed:
            _save(path, data)
        return picked


def complete(rid, claim_token, status, result):
    """Record the outcome of one claimed request. Applied only when the
    stored claim still matches (a stale or reclaimed attempt is dropped).
    Returns True when applied."""
    if status not in ("done",) + FAILED:
        raise ValueError(status)
    path = requests_path()
    with bm_config._FileLock(path):
        now = _now()
        try:
            data = _load(path)
        except StoreUnreadable:
            return False
        for it in data["requests"]:
            if it["id"] == rid and it.get("status") == "running" and it.get("claim") == claim_token:
                it["status"] = status
                it["result"] = result
                it["updated"] = _iso(now)
                it["attempts"] = int(it.get("attempts", 0)) + 1
                it.pop("claim", None)
                it.pop("claimed_at", None)
                _save(path, data)
                return True
        return False


def visible_requests():
    """Requests the active role may see, for discovery_status. Never raises."""
    try:
        data = _load(requests_path())
    except StoreUnreadable:
        return []
    return [it for it in data["requests"] if request_visible(it)]
