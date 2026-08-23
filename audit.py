"""Append-only, hash-chained audit ledger (issue #17).

Evidence, not a log file -- the hash chain is what makes tampering
detectable. Every record's `record_hash` covers its own content AND the
previous record's hash, so altering a record breaks the link the *next*
record's `prev_hash` depends on, even if the tamperer recomputes the
altered record's own hash to stay internally consistent. verify() checks
both: each record's stored hash against a fresh recompute of its content,
and each record's prev_hash against the actual previous record's hash --
either mismatch is tamper evidence. (The one record this can't catch is a
consistently-tampered *last* record with no following record to contradict
it -- an inherent limit of a hash chain without an external checkpoint,
out of scope here.)

Invariant #1 (issue #17): the ledger never contains telemetry rows or query
results -- hashes, counts, and metadata only. Enforced structurally, not
just by convention: append()'s signature has no field shaped like row/body
content for a caller to even misuse (see test_audit.py's
test_append_signature_has_no_field_to_pass_row_content_into).

Invariant #2: stdio has no authenticated principal. Callers pass
transport="stdio" as-is; this module doesn't infer or upgrade identity.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import _store

RECORD_FIELDS = (
    "ts_utc", "principal_id", "transport", "tool", "kql_canonical_sha256",
    "resolved_since", "row_count", "bytes_out", "redaction_rules_applied",
    "latency_ms", "outcome", "prev_hash", "record_hash",
)

GENESIS_HASH = "0" * 64
DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024  # 64MB

_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class InvalidRecordError(ValueError):
    pass


class ChainCorruptError(Exception):
    pass


class VerifyResult:
    def __init__(self, ok, broken_at=None, detail=None):
        self.ok = ok
        self.broken_at = broken_at
        self.detail = detail

    def __repr__(self):
        return f"VerifyResult(ok={self.ok}, broken_at={self.broken_at}, detail={self.detail!r})"


def _canonical_json(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _record_hash(record_without_hash):
    return hashlib.sha256(_canonical_json(record_without_hash).encode("utf-8")).hexdigest()


def _validate_redaction_rules(rules):
    rules = tuple(rules or ())
    for r in rules:
        if not isinstance(r, str) or not _RULE_ID_RE.match(r):
            raise InvalidRecordError(
                f"redaction_rules_applied entries must be short lowercase "
                f"identifiers (rule types, never matched values): {r!r}"
            )
    return list(rules)


def _last_record_hash(path):
    """Read only the last line of the file without loading it all into
    memory -- ledgers are meant to grow large. Returns GENESIS_HASH if the
    file doesn't exist or is empty."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return GENESIS_HASH
            chunk = min(size, 8192)
            f.seek(-chunk, os.SEEK_END)
            tail = f.read()
    except FileNotFoundError:
        return GENESIS_HASH
    lines = [ln for ln in tail.split(b"\n") if ln.strip()]
    if not lines:
        return GENESIS_HASH
    return json.loads(lines[-1])["record_hash"]


class Ledger:
    def __init__(self, path, *, retention_days, rotate_bytes=DEFAULT_ROTATE_BYTES):
        if not isinstance(retention_days, (int, float)) or retention_days <= 0:
            raise ValueError("retention_days must be a positive number -- no silent default")
        self.path = Path(path)
        self.retention_days = retention_days
        self.rotate_bytes = rotate_bytes
        _store.ensure_parent(self.path, private=True)

    def append(self, *, principal_id, transport, tool, outcome,
               kql_canonical_sha256=None, resolved_since=None,
               row_count=None, bytes_out=None, redaction_rules_applied=(),
               latency_ms=None):
        rules = _validate_redaction_rules(redaction_rules_applied)
        with _store.FileLock(self.path):
            self._rotate_if_needed_locked()
            prev_hash = _last_record_hash(self.path)
            record = {
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "principal_id": str(principal_id),
                "transport": str(transport),
                "tool": str(tool),
                "kql_canonical_sha256": kql_canonical_sha256,
                "resolved_since": resolved_since,
                "row_count": row_count,
                "bytes_out": bytes_out,
                "redaction_rules_applied": rules,
                "latency_ms": latency_ms,
                "outcome": str(outcome),
                "prev_hash": prev_hash,
            }
            record["record_hash"] = _record_hash(record)
            line = _canonical_json(record) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return record

    def _rotate_if_needed_locked(self):
        """Caller already holds the file lock. Size-based rotation: rename
        the current file with a timestamp suffix and start a fresh chain
        (new genesis) -- each rotated file is its own verifiable segment.
        Also prunes rotated files past retention_days."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= self.rotate_bytes:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            rotated = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
            os.replace(self.path, rotated)
        self._prune_locked()

    def _prune_locked(self):
        cutoff = time.time() - (self.retention_days * 86400)
        pattern = f"{self.path.stem}.*{self.path.suffix}"
        for f in self.path.parent.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def verify(self):
        """Walk the current ledger file and confirm every record's stored
        hash matches a fresh recompute, and every record's prev_hash
        matches the actual previous record's hash. Only checks the current
        (unrotated) file -- rotated segments are each independently
        verifiable the same way, given their own path."""
        try:
            lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except FileNotFoundError:
            return VerifyResult(ok=True)

        expected_prev = GENESIS_HASH
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                return VerifyResult(ok=False, broken_at=i, detail="unparseable record")
            if rec.get("prev_hash") != expected_prev:
                return VerifyResult(ok=False, broken_at=i, detail="prev_hash mismatch")
            stored_hash = rec.get("record_hash")
            recomputed = _record_hash({k: v for k, v in rec.items() if k != "record_hash"})
            if stored_hash != recomputed:
                return VerifyResult(ok=False, broken_at=i, detail="record_hash mismatch")
            expected_prev = stored_hash
        return VerifyResult(ok=True)
