"""Secret detection, output redaction, and aggregate Berserk audits."""
from collections import defaultdict
import ipaddress
import json
import math
import re

_bzrk_search = None
_table = None

MAX_MATCHES = 100
ALL_PII_TYPES = frozenset({"email", "ipv4", "ipv6", "credit_card"})


class AuditParseError(ValueError):
    """Raised when audit response cannot be fully decoded as a supported format."""
    pass

# Ordered most-specific-first. Later matches never replace an earlier overlap.
_SECRET_PATTERNS = (
    ("private_key", re.compile(
        r"-----BEGIN ((?:[A-Z]+ )?PRIVATE KEY)-----.*?-----END \1-----",
        re.DOTALL,
    )),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(
        r"(?i)\baws[_ -]?secret(?:[_ -]?(?:access)?[_ -]?key)?\s*[=:]\s*[A-Za-z0-9+/]{40}\b"
    )),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9-]{20,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b")),
)
_GENERIC_CREDENTIAL = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*[^\s,;]+"
)
_ENTROPY_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=-]{20,}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# Operators can extend this list locally with (type_name, compiled_regex).
EXTRA_PII_PATTERNS = []


def configure(bzrk_search, table):
    global _bzrk_search, _table
    _bzrk_search = bzrk_search
    _table = table


def _entropy(value):
    if not value:
        return 0.0
    counts = defaultdict(int)
    for char in value:
        counts[char] += 1
    length = float(len(value))
    return -sum((count / length) * math.log(count / length, 2) for count in counts.values())


def _luhn(value):
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _credential_type(match):
    key = match.group(1).lower().replace("-", "_")
    if key in {"password", "passwd", "pwd"}:
        return "password"
    if key == "api_key":
        return "api_key"
    return key


def _candidate_matches(text, include_entropy, pii_types):
    for secret_type, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            yield match.start(), match.end(), secret_type
    for match in _GENERIC_CREDENTIAL.finditer(text):
        yield match.start(), match.end(), _credential_type(match)

    if include_entropy:
        for match in _ENTROPY_TOKEN.finditer(text):
            value = match.group(0)
            if _entropy(value) >= 4.0:
                yield match.start(), match.end(), "high_entropy"

    if "email" in pii_types:
        for match in _EMAIL.finditer(text):
            yield match.start(), match.end(), "email"
    if "ipv4" in pii_types:
        for match in _IPV4.finditer(text):
            try:
                ipaddress.IPv4Address(match.group(0))
            except ipaddress.AddressValueError:
                continue
            yield match.start(), match.end(), "ipv4"
    if "ipv6" in pii_types:
        for match in _IPV6_CANDIDATE.finditer(text):
            value = match.group(0)
            if not value or value == ":":
                continue
            try:
                ipaddress.IPv6Address(value)
            except ipaddress.AddressValueError:
                continue
            yield match.start(), match.end(), "ipv6"
    if "credit_card" in pii_types:
        for match in _CARD_CANDIDATE.finditer(text):
            if _luhn(match.group(0)):
                yield match.start(), match.end(), "credit_card"
    for pii_type, pattern in EXTRA_PII_PATTERNS:
        if pii_type in pii_types:
            for match in pattern.finditer(text):
                yield match.start(), match.end(), pii_type


MAX_REDACT_CHARS = 1_000_000
MAX_REDACT_CANDIDATES = 50_000


def _limit_result(reason):
    return (
        "[REDACTED:redaction_limit]",
        [{"type": reason, "count": 1, "first_offset": 0}],
    )


def redact(text, include_entropy=False, pii_types=ALL_PII_TYPES):
    """Return (redacted text, aggregate findings) without retaining values.

    Uses a sort-merge-join pipeline with explicit bounds. If input exceeds
    MAX_REDACT_CHARS or candidates exceed MAX_REDACT_CANDIDATES, the entire
    input is replaced with a fail-closed marker — no partial original is
    ever returned.
    """
    original = str(text or "")
    if len(original) > MAX_REDACT_CHARS:
        return _limit_result("input_too_large")

    enabled_pii = frozenset(pii_types or ())
    candidates = []
    for order, (start, end, finding_type) in enumerate(
        _candidate_matches(original, include_entropy, enabled_pii)
    ):
        if order >= MAX_REDACT_CANDIDATES:
            return _limit_result("too_many_matches")
        candidates.append((start, end, finding_type, order))

    candidates.sort(key=lambda c: (c[0], c[1]))

    merged = []
    for start, end, finding_type, order in candidates:
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_type, prev_order = merged[-1]
            merged[-1] = (
                prev_start,
                max(prev_end, end),
                prev_type if prev_order <= order else finding_type,
                min(prev_order, order),
            )
        else:
            merged.append((start, end, finding_type, order))

    pieces = []
    cursor = 0
    summary = {}
    for start, end, finding_type, _order in merged:
        pieces.append(original[cursor:start])
        pieces.append(f"[REDACTED:{finding_type}]")
        cursor = end
        item = summary.setdefault(finding_type, {
            "type": finding_type, "count": 0, "first_offset": start,
        })
        item["count"] += 1
        item["first_offset"] = min(item["first_offset"], start)
    pieces.append(original[cursor:])
    clean = "".join(pieces)

    findings = sorted(summary.values(), key=lambda item: (item["first_offset"], item["type"]))
    return clean, findings


def apply_output_filter(text, mode="flag", include_entropy=False, pii_types=()):
    mode = str(mode or "flag").strip().lower()
    if mode == "off":
        return str(text or "")
    clean, findings = redact(text, include_entropy=include_entropy, pii_types=pii_types)
    if not findings:
        return str(text or "")
    if mode == "redact":
        return clean
    count = sum(item["count"] for item in findings)
    types = ", ".join(sorted(item["type"] for item in findings))
    banner = (
        f"⚠ {count} potential secrets detected in this result "
        f"(types: {types}) - source logs should be scrubbed at ingest."
    )
    return banner + "\n" + str(text or "")


def _audit_query():
    return (
        f"{_table} | where isnotnull(body) "
        f"| project service=tostring(resource['service.name']), ts=timestamp, body=tostring(body) "
        f"| tail 2000"
    )


def _normalize_audit_record(value):
    """Validate and normalize one audit record. Raises AuditParseError on invalid shape."""
    if not isinstance(value, dict):
        raise AuditParseError("row_not_object")
    if "body" not in value:
        raise AuditParseError("row_missing_body")
    if "service" not in value:
        raise AuditParseError("row_missing_service")
    if "ts" not in value and "timestamp" not in value:
        raise AuditParseError("row_missing_timestamp")
    return {
        "service": str(value.get("service") or "(unknown)"),
        "ts": str(value.get("ts") or value.get("timestamp") or ""),
        "body": str(value.get("body") or ""),
    }


def _json_records(parsed):
    """Extract and validate rows from a whole-document JSON value.

    Raises AuditParseError on any unrecognized or structurally invalid shape.
    Returns a list of validated audit-record dicts on success.
    """
    if isinstance(parsed, list):
        return [_normalize_audit_record(item) for item in parsed]

    if not isinstance(parsed, dict):
        raise AuditParseError("unsupported_shape")

    tables = parsed.get("Tables")
    if isinstance(tables, list):
        if not tables:
            raise AuditParseError("malformed_tables")
        if len(tables) != 1:
            raise AuditParseError("multiple_tables")
        table = tables[0]
        if not isinstance(table, dict):
            raise AuditParseError("malformed_tables")
        schema = table.get("schema")
        if not isinstance(schema, dict):
            raise AuditParseError("malformed_tables_schema")
        columns_raw = schema.get("columns")
        if not isinstance(columns_raw, list):
            raise AuditParseError("malformed_tables_columns")
        columns = []
        for c in columns_raw:
            if not isinstance(c, dict) or not c.get("name") or not isinstance(c["name"], str):
                raise AuditParseError("malformed_tables_column_entry")
            columns.append(c["name"])
        if len(columns) != len(set(columns)):
            raise AuditParseError("duplicate_column_names")
        rows = table.get("rows")
        if not isinstance(rows, list):
            raise AuditParseError("malformed_tables_rows")
        result = []
        for row in rows:
            if not isinstance(row, list):
                raise AuditParseError("tables_row_not_list")
            if len(row) != len(columns):
                raise AuditParseError("tables_row_length_mismatch")
            result.append(_normalize_audit_record(dict(zip(columns, row))))
        return result

    for key in ("rows", "data", "results", "records"):
        if key in parsed:
            value = parsed[key]
            if not isinstance(value, list):
                raise AuditParseError("wrapper_value_not_list")
            return [_normalize_audit_record(item) for item in value]

    if "body" in parsed and "service" in parsed:
        return [_normalize_audit_record(parsed)]

    raise AuditParseError("unsupported_shape")


def _parse_audit_rows(text):
    """Return a fully validated list of audit records or raise AuditParseError.

    A successful return of [] means the input was validly empty (e.g. "(no rows)"
    or a zero-row Tables response with valid schema). Any unrecognized, malformed,
    or truncated input raises rather than returning an ambiguous empty list.
    """
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)":
        return []

    if whole[0] in "[{":
        try:
            parsed = json.loads(whole)
        except json.JSONDecodeError:
            pass
        else:
            return _json_records(parsed)

        lines = [line for line in whole.splitlines() if line.strip()]
        jsonl_candidate = all(line.lstrip().startswith("{") for line in lines)
        if not jsonl_candidate:
            raise AuditParseError("malformed_json")

        rows = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditParseError("malformed_jsonl") from exc
            rows.append(_normalize_audit_record(value))
        return rows

    lines = [line for line in whole.splitlines() if line.strip()]
    if not lines:
        return []

    jsonl_candidate = all(line.lstrip().startswith("{") for line in lines)
    if jsonl_candidate:
        rows = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditParseError("malformed_jsonl") from exc
            rows.append(_normalize_audit_record(value))
        return rows

    header = re.split(r"\t+|\s{2,}", lines[0].strip())
    if not {"service", "ts", "body"}.issubset(set(header)):
        raise AuditParseError("unknown_table_header")
    if len(header) != len(set(header)):
        raise AuditParseError("duplicate_table_columns")

    rows = []
    for line in lines[1:]:
        parts = re.split(r"\t+|\s{2,}", line.strip(), maxsplit=len(header) - 1)
        if len(parts) < len(header):
            raise AuditParseError("table_row_too_short")
        rows.append({header[i]: parts[i] for i in range(len(header))})
    return rows


def scan_secrets(since="1h ago", include_entropy=False, pii_types=()):
    text, is_err = _bzrk_search(_audit_query(), since)
    if is_err:
        return text, True
    try:
        audit_rows = _parse_audit_rows(text)
    except AuditParseError:
        return (
            "Secret scan failed: the query response was malformed or unsupported; "
            "no clean result was produced."
        ), True
    by_service = {}
    total = 0
    for row in audit_rows:
        _clean, findings = redact(
            row.get("body", ""), include_entropy=include_entropy, pii_types=pii_types,
        )
        if not findings:
            continue
        service = row.get("service") or "(unknown)"
        report = by_service.setdefault(service, {"types": defaultdict(int), "first_seen": ""})
        for finding in findings:
            report["types"][finding["type"]] += finding["count"]
            total += finding["count"]
        timestamp = str(row.get("ts") or "")
        if timestamp and (not report["first_seen"] or timestamp < report["first_seen"]):
            report["first_seen"] = timestamp

    if not by_service:
        return "Secret scan: no potential secrets detected in this window.", False
    lines = [f"Secret scan: {total} potential secrets detected (values withheld)."]
    for service in sorted(by_service):
        report = by_service[service]
        # Use "name x{count}", NOT "name={count}": this report is itself passed
        # through the global output-redaction filter in dispatch(), and a
        # "password=1" / "api_key=1" token would trip the _GENERIC_CREDENTIAL
        # pattern and get banner-flagged (flag mode) or corrupted (redact mode).
        type_counts = ", ".join(
            f"{name} x{count}" for name, count in sorted(report["types"].items())
        )
        lines.append(
            f"- {service}: {type_counts}; first_seen={report['first_seen'] or 'unknown'}"
        )
    lines.append("Remediation: scrub secrets at ingest, rotate exposed credentials, and re-run this audit.")
    return "\n".join(lines), False
