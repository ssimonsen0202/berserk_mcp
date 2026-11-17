"""Usage statistics for saved queries (WP4).

One small file, `usage_stats.json`, sits beside learned.json. It holds, per
saved-query name, how often the query ran and how the runs ended. It lives
outside learned.json on purpose: recording a call must not rewrite the query
store, change tools/list or send notifications/tools/list_changed.

Two rules hold for every function here:
- A tool call never fails, and never waits more than a short lock attempt,
  because of usage statistics. `record` logs and returns on any error.
- This module never holds the learned-store lock. Readers take no lock at all
  (writes are atomic replaces), so a caller that holds the learned-store lock
  can still read a snapshot without taking two locks together.

Keys are sanitised saved-query names only (lowercase letters, digits and
underscores), the same names saved__<name> dispatch uses.
"""

from berserk_mcp import config as bm_config
from pathlib import Path
import _store
import re

USAGE_FILE_NAME = "usage_stats.json"

# Entries kept in the file; names absent from the learned store go first.
MAX_ENTRIES = 500

# `record` gives up on the lock after this long and skips the write.
LOCK_TIMEOUT_SECONDS = 1.0

OUTCOMES = ("rows", "empty", "error")

# MCP clients limit tool names to 64 characters; "saved__" takes 7.
_MAX_NAME_CHARS = 64 - len("saved__")
_NAME_RE = re.compile(r"[a-z0-9_]+")
_ISO_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
_COUNTERS = ("calls", "rows_calls", "zero_row_calls", "error_calls")


def usage_path():
    """The usage file, resolved on each call so a changed LEARNED_PATH applies."""
    return Path(bm_config.LEARNED_PATH).parent / USAGE_FILE_NAME


def _valid_name(name):
    return isinstance(name, str) and len(name) <= _MAX_NAME_CHARS and _NAME_RE.fullmatch(name) is not None


def valid_timestamp(value):
    """`value` when it is an ISO UTC timestamp as now_iso writes it, else None.
    Anything else read from the file is never shown or compared."""
    return value if isinstance(value, str) and _ISO_RE.fullmatch(value) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _clean(raw):
    """Entries with a valid name and a dict value; counters coerced to ints."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, stats in raw.items():
        if not _valid_name(name) or not isinstance(stats, dict):
            continue
        entry = dict(stats)
        for key in _COUNTERS:
            entry[key] = _count(stats.get(key))
        out[name] = entry
    return out


def snapshot():
    """The usage dict, {name: stats}. Takes no lock. Never raises: a missing or
    damaged file gives an empty dict."""
    try:
        return _clean(_store.load_json_dict(usage_path(), logger=bm_config.log))
    except Exception as exc:
        bm_config.log(f"usage snapshot failed: {type(exc).__name__}: {exc}")
        return {}


def stats_for(name):
    """The stats dict for one name (empty when it has none)."""
    return snapshot().get(name, {})


def last_rows_at(name):
    """When the query last returned rows, as an ISO string, or None."""
    return valid_timestamp(stats_for(name).get("last_rows_at"))


def prune(data, valid_names):
    """`data` reduced to names in `valid_names` and to at most MAX_ENTRIES.
    With valid_names None (the store could not be read), only the cap applies.
    When the cap still binds, the most recently called entries stay."""
    kept = {n: s for n, s in data.items() if valid_names is None or n in valid_names}
    if len(kept) > MAX_ENTRIES:
        newest_first = sorted(kept, key=lambda n: (valid_timestamp(kept[n].get("last_called")) or "", n), reverse=True)
        kept = {n: kept[n] for n in newest_first[:MAX_ENTRIES]}
    return kept


# The learned store lives in a higher layer, so it registers a callable that
# returns the sanitised names it holds (see learned.py).
_stored_names_source = None


def set_stored_names_source(source):
    """Register the callable that returns the set of names in the learned store."""
    global _stored_names_source
    _stored_names_source = source


def _stored_names():
    """Names in the learned store, or None when they cannot be read. The source
    reads the store without its lock: the file is replaced atomically, and
    taking the learned-store lock here would hold two locks together."""
    try:
        return None if _stored_names_source is None else set(_stored_names_source())
    except Exception:
        return None


def record(name, outcome):
    """Count one run of saved query `name`; outcome is rows, empty or error.

    Best effort. A bad name or outcome, a held lock, a full disk or any other
    error is logged and swallowed, so the tool call that ran the query is never
    failed or delayed by more than LOCK_TIMEOUT_SECONDS."""
    try:
        if outcome not in OUTCOMES or not _valid_name(name):
            return
        path = usage_path()
        with _store.FileLock(path, timeout_seconds=LOCK_TIMEOUT_SECONDS):
            data = _clean(_store.load_json_dict(path, logger=bm_config.log))
            now = bm_config.now_iso()
            entry = data.setdefault(name, {key: 0 for key in _COUNTERS})
            entry["calls"] = _count(entry.get("calls")) + 1
            entry["last_called"] = now
            if outcome == "rows":
                entry["rows_calls"] = _count(entry.get("rows_calls")) + 1
                entry["last_rows_at"] = now
            elif outcome == "empty":
                entry["zero_row_calls"] = _count(entry.get("zero_row_calls")) + 1
            else:
                entry["error_calls"] = _count(entry.get("error_calls")) + 1
            if len(data) > MAX_ENTRIES:
                valid = _stored_names()
                data = prune(data, None if valid is None else valid | {name})
            _store.save_json_dict(path, data, logger=bm_config.log)
    except Exception as exc:
        bm_config.log(f"usage record skipped for {name!r}: {type(exc).__name__}: {exc}")
