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


def redact(text, include_entropy=False, pii_types=ALL_PII_TYPES):
    """Return (redacted text, aggregate findings) without retaining values."""
    original = str(text or "")
    enabled_pii = frozenset(pii_types or ())
    accepted = []
    occupied = []
    for start, end, finding_type in _candidate_matches(original, include_entropy, enabled_pii):
        if len(accepted) >= MAX_MATCHES:
            break
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        accepted.append((start, end, finding_type))
        occupied.append((start, end))

    clean = original
    for start, end, finding_type in sorted(accepted, reverse=True):
        clean = clean[:start] + f"[REDACTED:{finding_type}]" + clean[end:]

    summary = {}
    for start, _end, finding_type in accepted:
        item = summary.setdefault(finding_type, {
            "type": finding_type, "count": 0, "first_offset": start,
        })
        item["count"] += 1
        item["first_offset"] = min(item["first_offset"], start)
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
        f"| sort by ts desc | take 2000"
    )


def _audit_row(obj):
    return {
        "service": str(obj.get("service") or "(unknown)"),
        "ts": str(obj.get("ts") or obj.get("timestamp") or ""),
        "body": str(obj.get("body") or ""),
    }


def _json_records(parsed):
    """Rows from a whole-document JSON value: a bare array or a wrapper object
    keying rows under a common name. None if unrecognizable."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("rows", "data", "results", "records"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
    return None


def _parse_audit_rows(text):
    rows = []
    whole = str(text or "").strip()
    if not whole or whole == "(no rows)":
        return rows

    # Whole-document JSON (array or {rows:[...]}) from --json. jsonl and a
    # single object fall through to the line loop below.
    if whole[0] in "[{":
        try:
            records = _json_records(json.loads(whole))
        except json.JSONDecodeError:
            records = None
        if records is not None:
            return [_audit_row(o) for o in records if isinstance(o, dict)]

    lines = [line for line in whole.splitlines() if line.strip()]
    for line in lines:
        if not line.lstrip().startswith("{"):
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            break
        rows.append(_audit_row(obj))
    if rows or not lines or lines == ["(no rows)"]:
        return rows

    header = re.split(r"\t+|\s{2,}", lines[0].strip())
    if not {"service", "ts", "body"}.issubset(set(header)):
        return []
    for line in lines[1:]:
        parts = re.split(r"\t+|\s{2,}", line.strip(), maxsplit=len(header) - 1)
        obj = {header[i]: parts[i] if i < len(parts) else "" for i in range(len(header))}
        rows.append(obj)
    return rows


def scan_secrets(since="1h ago", include_entropy=False, pii_types=()):
    text, is_err = _bzrk_search(_audit_query(), since)
    if is_err:
        return text, True
    by_service = {}
    total = 0
    for row in _parse_audit_rows(text):
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
