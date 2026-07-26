"""Static and live-result helpers for Berserk KQL validation.

The validator is intentionally conservative and stdlib-only. It is not a full
KQL parser; it tokenizes enough structure to catch control commands, malformed
pipelines, unbounded results, expensive operators, broad raw scans, and schema
misses before a user-originated query reaches ``bzrk``.
"""
import json
import re


VALIDATION_VERSION = 1

SCORE_WEIGHTS = {
    "blocked": 40,
    "expensive_operator": 25,
    "unbounded_result": 20,
    "wide_projection": 15,
    "raw_scan": 15,
    "sort_before_filter": 10,
    "missing_filter": 10,
    "early_predicate": -10,
    "small_bound": -10,
    "narrow_projection": -5,
}

RISK_LOW_MAX = 24
RISK_MEDIUM_MAX = 59

FINDING_ORDER = {
    "EMPTY_QUERY": 10,
    "QUERY_TOO_LONG": 20,
    "INVALID_SINCE": 30,
    "CONTROL_COMMAND": 40,
    "MULTI_STATEMENT_USER_QUERY": 50,
    "WRONG_TABLE": 60,
    "UNSAFE_OPERATOR": 70,
    "UNBOUNDED_RESULT": 80,
    "RESULT_BOUND_TOO_LARGE": 90,
    "UNKNOWN_FIELD": 100,
    "WIDE_PROJECTION": 110,
    "MISSING_SELECTIVE_FILTER": 120,
    "EXPENSIVE_OPERATOR": 130,
    "SORT_BEFORE_FILTER": 140,
    "SORT_WITHOUT_BOUND": 150,
    "RAW_CONTAINS_SCAN": 160,
    "HIGH_CARDINALITY_GROUP": 170,
    "SERIES_TOO_WIDE": 180,
}

_SINCE_RE = re.compile(
    r"^(now|\d+\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks)(\s+ago)?)$",
    re.IGNORECASE,
)

_STRING_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\\.|[^\"])*\"")
_BOUND_RE = re.compile(r"\b(take|tail|limit)\s+(\d+)\b|\btop\s+(\d+)\s+by\b", re.I)
_COUNT_RE = re.compile(r"\b(count|summarize\s+[^|]*\bcount\s*\()", re.I)
_SELECTIVE_RE = re.compile(
    r"\bwhere\b[^|]*(metric_name|severity_text|trace_id|span_id|status_code|"
    r"resource\s*\[\s*['\"](?:service\.name|host\.name|container\.name)['\"]\s*\]|"
    r"attributes\s*\[)",
    re.I,
)
_PROJECT_RE = re.compile(r"\bproject(?:-away|-keep)?\b", re.I)
_EXPENSIVE_PATTERNS = [
    (re.compile(r"\bjoin\b", re.I), "join"),
    (re.compile(r"\bmv-expand\b", re.I), "mv-expand"),
    (re.compile(r"\bbag_keys\s*\(", re.I), "bag_keys"),
    (re.compile(r"\bparse\b", re.I), "parse"),
    (re.compile(r"\bmatches\s+regex\b", re.I), "regex"),
]
_UNSAFE_RE = re.compile(r"\b(set|drop|alter|delete|update|ingest|create)\b", re.I)
_CONTROL_RE = re.compile(r"^\s*\.")
_RAW_SCAN_RE = re.compile(r"\b(body|\$raw)\b[^|]{0,80}\b(contains|has_any|matches\s+regex)\b|\b(contains|has_any|matches\s+regex)\b[^|]{0,80}\b(body|\$raw)\b", re.I)
_FIELD_REF_RE = re.compile(
    r"(resource|attributes)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]|"
    r"\b([A-Za-z_][A-Za-z0-9_]*)\b"
)
_FUNCTION_NAMES = {
    "avg", "count", "countif", "datetime", "extract_log_template", "iff", "isempty",
    "isnotempty", "isnotnull", "isnull", "max", "min", "not", "now", "strcat",
    "substring", "sum", "todynamic", "toint", "tolong", "toreal", "tostring",
    "series_decompose_anomalies", "series_fit_line", "otel_histogram_percentile",
}
_KQL_WORDS = {
    "and", "asc", "by", "contains", "default", "desc", "extend", "false",
    "fieldstats", "has", "in", "limit", "make", "make-series", "on", "or",
    "order", "project", "project-away", "project-keep", "regex", "sort",
    "summarize", "tail", "take", "top", "true", "where", "with",
}


def _strip_strings(text):
    return _STRING_RE.sub("''", str(text or ""))


def _split_pipeline(kql):
    parts = []
    buf = []
    quote = None
    escape = False
    for ch in str(kql or ""):
        if quote:
            buf.append(ch)
            if quote == '"' and ch == "\\" and not escape:
                escape = True
                continue
            if ch == quote and not escape:
                quote = None
            escape = False
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "|":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return parts


def _finding(code, severity, message, location="query", recommendation=""):
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "location": location,
        "recommendation": recommendation,
    }


def _risk(score):
    if score <= RISK_LOW_MAX:
        return "low"
    if score <= RISK_MEDIUM_MAX:
        return "medium"
    return "high"


def _normalize_schema_fields(schema_fields):
    out = set()
    for field in schema_fields or []:
        text = str(field)
        out.add(text)
        m = re.match(r"(resource|attributes)\[['\"](.+?)['\"]\]$", text)
        if m:
            out.add(f"{m.group(1)}.{m.group(2)}")
            out.add(m.group(2))
    return out


def _extract_referenced_fields(kql):
    refs = set()
    for bag, path in re.findall(r"(resource|attributes)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]", str(kql or "")):
        refs.add(f"{bag}['{path}']")
        refs.add(f"{bag}.{path}")
    stripped = _strip_strings(kql)
    for m in _FIELD_REF_RE.finditer(stripped):
        bag, path, bare = m.group(1), m.group(2), m.group(3)
        if bag and path:
            refs.add(f"{bag}['{path}']")
            refs.add(f"{bag}.{path}")
            continue
        if not bare:
            continue
        low = bare.lower()
        if low in _FUNCTION_NAMES or low in _KQL_WORDS:
            continue
        if bare in {"resource", "attributes"}:
            continue
        refs.add(bare)
    return refs


def validate_kql_static(kql, *, table, since, schema_fields=None, max_chars=50000,
                        max_rows=2000, suggest=None, schema_info=None):
    """Return a deterministic validation report; never runs a subprocess."""
    findings = []
    recommendations = []
    score = 0
    query = "" if kql is None else str(kql)
    table = str(table or "default")
    since = str(since or "")
    max_chars = int(max_chars or 50000)
    max_rows = int(max_rows or 2000)
    schema_info = dict(schema_info or {})

    try:
        if not query.strip():
            findings.append(_finding("EMPTY_QUERY", "error", "Query is empty."))
        if len(query) > max_chars:
            findings.append(_finding("QUERY_TOO_LONG", "error", f"Query exceeds {max_chars} characters."))
        if not _SINCE_RE.match(since.strip()) or len(since) > 32:
            findings.append(_finding("INVALID_SINCE", "error", f"Invalid since window: {since!r}."))
        if _CONTROL_RE.match(query):
            findings.append(_finding("CONTROL_COMMAND", "error", "Control commands are not allowed for user KQL."))
        if ";" in _strip_strings(query):
            findings.append(_finding("MULTI_STATEMENT_USER_QUERY", "error", "Multiple statements are not allowed."))

        parts = _split_pipeline(query)
        if not parts or parts[0] != table or len(parts) < 2:
            findings.append(_finding(
                "WRONG_TABLE", "error",
                f"Query must begin with '{table} | ...'.",
                recommendation=f"Start with: {table} | where ... | take 50",
            ))
        if _UNSAFE_RE.search(_strip_strings(query)):
            findings.append(_finding("UNSAFE_OPERATOR", "error", "Mutation-like or unsafe syntax is present."))

        stripped = _strip_strings(query)
        bounds = []
        for m in _BOUND_RE.finditer(stripped):
            n = m.group(2) or m.group(3)
            if n:
                bounds.append(int(n))
        has_count = bool(_COUNT_RE.search(stripped))
        has_bound = bool(bounds) or has_count
        if not has_bound and query.strip():
            findings.append(_finding(
                "UNBOUNDED_RESULT", "warning",
                "Query has no explicit terminal result bound.",
                "pipeline",
                "End with take, top, tail, count, or a bounded summarize.",
            ))
        for n in bounds:
            if n > max_rows:
                findings.append(_finding(
                    "RESULT_BOUND_TOO_LARGE", "warning",
                    f"Explicit result bound {n} exceeds max_rows={max_rows}.",
                    "pipeline",
                    f"Use {max_rows} rows or fewer for arbitrary queries.",
                ))

        expensive_names = []
        for rx, label in _EXPENSIVE_PATTERNS:
            if rx.search(stripped):
                expensive_names.append(label)
        if expensive_names:
            findings.append(_finding(
                "EXPENSIVE_OPERATOR", "warning",
                "Potentially expensive operator(s): " + ", ".join(sorted(set(expensive_names))) + ".",
                "pipeline",
                "Filter early and bound the result before using expensive operators.",
            ))

        has_selective = bool(_SELECTIVE_RE.search(stripped))
        stages = [p.lower() for p in parts[1:]]
        first_where = next((i for i, p in enumerate(stages) if p.startswith("where ")), None)
        first_sort = next((i for i, p in enumerate(stages) if p.startswith("sort ") or p.startswith("order ")), None)
        if first_sort is not None and (first_where is None or first_sort < first_where):
            findings.append(_finding("SORT_BEFORE_FILTER", "warning", "Sort occurs before a selective filter.", "pipeline"))
        if first_sort is not None:
            after_sort = " | ".join(stages[first_sort + 1:])
            if not _BOUND_RE.search(after_sort):
                findings.append(_finding("SORT_WITHOUT_BOUND", "warning", "Sort is not followed by a small take/top bound.", "pipeline"))
        if not has_selective and (expensive_names or len(parts) <= 2 or "summarize" in stripped.lower()):
            findings.append(_finding(
                "MISSING_SELECTIVE_FILTER", "warning",
                "Broad query has no early selective predicate.",
                "pipeline",
                "Filter by metric_name, service, host, severity, trace, or another selective field before aggregation.",
            ))
        if _RAW_SCAN_RE.search(stripped):
            findings.append(_finding(
                "RAW_CONTAINS_SCAN", "warning",
                "Broad text search scans raw body or $raw.",
                "pipeline",
                "Add a selective predicate and narrow time window before raw text search.",
            ))
        if re.search(r"\bproject\b[^|]*(\bbody\b|\bresource\b|\battributes\b|\$raw)", stripped, re.I):
            findings.append(_finding(
                "WIDE_PROJECTION", "warning",
                "Projection includes raw body/resource/attributes/$raw.",
                "pipeline",
                "Project specific bounded fields or substring raw text.",
            ))
        if re.search(r"\bsummarize\b[^|]*\bby\b[^|]*(body|resource|attributes|\$raw)", stripped, re.I):
            findings.append(_finding("HIGH_CARDINALITY_GROUP", "warning", "Grouping uses a high-cardinality/raw field.", "pipeline"))
        if re.search(r"make-series", stripped, re.I):
            dims = re.search(r"\bby\b([^|]+)", stripped, re.I)
            if dims and dims.group(1).count(",") >= 2:
                findings.append(_finding("SERIES_TOO_WIDE", "warning", "make-series groups by too many dimensions.", "pipeline"))

        known = _normalize_schema_fields(schema_fields)
        unknown = []
        if known:
            for ref in sorted(_extract_referenced_fields(query)):
                low = ref.lower()
                if low in _FUNCTION_NAMES or low in _KQL_WORDS:
                    continue
                if ref not in known:
                    unknown.append(ref)
        for ref in unknown:
            suggestions = []
            if suggest:
                try:
                    suggestions = list(suggest(ref))[:3]
                except Exception:
                    suggestions = []
            msg = f"Unknown field {ref!r}."
            if suggestions:
                msg += " Did you mean " + suggestions[0] + "?"
            findings.append(_finding(
                "UNKNOWN_FIELD", "warning", msg, "schema",
                "Use discover_schema or a listed resource/attribute field.",
            ))

        for f in findings:
            if f["severity"] == "error":
                score += SCORE_WEIGHTS["blocked"]
            elif f["code"] == "EXPENSIVE_OPERATOR":
                score += SCORE_WEIGHTS["expensive_operator"]
            elif f["code"] == "UNBOUNDED_RESULT":
                score += SCORE_WEIGHTS["unbounded_result"]
            elif f["code"] == "WIDE_PROJECTION":
                score += SCORE_WEIGHTS["wide_projection"]
            elif f["code"] == "RAW_CONTAINS_SCAN":
                score += SCORE_WEIGHTS["raw_scan"]
            elif f["code"] == "SORT_BEFORE_FILTER":
                score += SCORE_WEIGHTS["sort_before_filter"]
            elif f["code"] == "MISSING_SELECTIVE_FILTER":
                score += SCORE_WEIGHTS["missing_filter"]
        if has_selective:
            score += SCORE_WEIGHTS["early_predicate"]
        if bounds and max(bounds) <= min(max_rows, 100):
            score += SCORE_WEIGHTS["small_bound"]
        if _PROJECT_RE.search(stripped) and not re.search(r"\b(body|resource|attributes|\$raw)\b", stripped, re.I):
            score += SCORE_WEIGHTS["narrow_projection"]
        score = max(0, min(100, score))

        findings.sort(key=lambda f: (FINDING_ORDER.get(f["code"], 999), f["message"]))
        recommendations = [f["recommendation"] for f in findings if f.get("recommendation")]
        valid = not any(f["severity"] == "error" for f in findings)
        return {
            "valid": valid,
            "risk": _risk(score),
            "score": score,
            "findings": findings,
            "recommendations": recommendations,
            "query_shape": {
                "pipeline_stages": max(0, len(parts) - 1),
                "has_bound": has_bound,
                "bounds": bounds,
                "has_selective_filter": has_selective,
                "uses_expensive_operator": bool(expensive_names),
            },
            "schema": schema_info,
            "runtime": None,
            "validation_version": VALIDATION_VERSION,
        }
    except Exception as e:
        return {
            "valid": False,
            "risk": "high",
            "score": 100,
            "findings": [_finding("UNSAFE_OPERATOR", "error", "Validator failed safely: " + str(e))],
            "recommendations": ["Check query syntax and retry."],
            "query_shape": {},
            "schema": schema_info,
            "runtime": None,
            "validation_version": VALIDATION_VERSION,
        }


def parse_cli_stats(text):
    """Parse supported bzrk stats renderings defensively.

    Returns a dict with nullable counters. Unknown or malformed formats do not
    raise and do not fabricate values.
    """
    result = {
        "rows_returned": None,
        "rows_processed": None,
        "bytes_scanned": None,
        "engine_stats": {},
        "stats_available": False,
    }
    raw = str(text or "").strip()
    if not raw:
        return result
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        stats = parsed.get("stats") if isinstance(parsed.get("stats"), dict) else parsed
        for out_key, keys in {
            "rows_returned": ("rows_returned", "rowsReturned", "result_rows"),
            "rows_processed": ("rows_processed", "rowsProcessed", "input_rows"),
            "bytes_scanned": ("bytes_scanned", "bytesScanned", "bytes_scanned"),
        }.items():
            for key in keys:
                if key in stats:
                    try:
                        result[out_key] = int(stats[key])
                    except (TypeError, ValueError):
                        result[out_key] = None
                    break
        result["engine_stats"] = {k: v for k, v in stats.items() if k not in {
            "rows_returned", "rowsReturned", "result_rows", "rows_processed",
            "rowsProcessed", "input_rows", "bytes_scanned", "bytesScanned",
        }}
        result["stats_available"] = any(result[k] is not None for k in (
            "rows_returned", "rows_processed", "bytes_scanned"))
        return result
    pairs = re.findall(r"(?i)\b(rows returned|rows processed|bytes scanned)\b\s*[:=]\s*([0-9]+)", raw)
    for key, val in pairs:
        normalized = key.lower().replace(" ", "_")
        result[normalized] = int(val)
    result["stats_available"] = bool(pairs)
    return result
