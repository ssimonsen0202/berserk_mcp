"""Normalized Berserk schema snapshots and compact schema context.

This module is stdlib-only and contains no subprocess execution. The caller
supplies a ``fetcher`` that returns raw ``.show tables``, ``getschema``,
``fieldstats``, and structural sample text. Cache files contain only schema
metadata, safe field names, and bounded examples; raw log bodies are never
stored.
"""
import difflib
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import _store


DEFAULT_TTL_SECONDS = 3600
MAX_EXAMPLES_PER_FIELD = 3
MAX_EXAMPLE_CHARS = 80
MAX_CONTEXT_CHARS = 12000

_LOCK = threading.RLock()


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_example(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.search(r"(?i)(secret|token|password|api[_-]?key|bearer\s+[a-z0-9._-]+)", text):
        return ""
    if any(ord(c) < 32 for c in text):
        return ""
    return text[:MAX_EXAMPLE_CHARS]


def _parse_getschema(raw):
    columns = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("==", "-", "|")):
            continue
        if line.lower().startswith(("column", "name ")):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2:
            name, typ = parts[0].strip("|"), parts[1].strip("|")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                columns[name] = {"type": typ.lower(), "nullable": True}
    return columns


def _extract_bracketed_keys(raw, label):
    keys = []
    for line in str(raw or "").splitlines():
        if label and label not in line:
            continue
        for bracketed in re.findall(r"\[([^\]]+)\]", line):
            for token in re.findall(r"['\"]([^'\"]+)['\"]", bracketed):
                if re.match(r"^[A-Za-z0-9._-]{1,80}$", token) and token not in keys:
                    keys.append(token)
    return keys


def _parse_fieldstats(raw, prefix="resource"):
    fields = {}
    for line in str(raw or "").splitlines():
        for path in re.findall(r"(?:resource|attributes)?\.?([A-Za-z0-9_.-]+)", line):
            if "." not in path:
                continue
            if not re.match(r"^[A-Za-z0-9._-]{1,80}$", path):
                continue
            examples = []
            for ex in re.findall(r"['\"]([^'\"]{1,120})['\"]", line):
                safe = _safe_example(ex)
                if safe and safe != path and safe not in examples:
                    examples.append(safe)
                if len(examples) >= MAX_EXAMPLES_PER_FIELD:
                    break
            fields[path] = {"type": "string", "examples": examples}
    return fields


def _merge_field(fields, key, typ="string", examples=None):
    if not key or not re.match(r"^[A-Za-z0-9._-]{1,80}$", key):
        return
    item = fields.setdefault(key, {"type": typ, "examples": []})
    if typ and item.get("type") == "string":
        item["type"] = typ
    for ex in examples or []:
        safe = _safe_example(ex)
        if safe and safe not in item["examples"]:
            item["examples"].append(safe)
        item["examples"] = item["examples"][:MAX_EXAMPLES_PER_FIELD]


def normalize_snapshot(table="default", tables_text="", getschema_text="",
                       fieldstats_text="", sample_text="", supported_idioms=None,
                       source_status="fresh"):
    columns = _parse_getschema(getschema_text)
    for name, typ in {
        "timestamp": "datetime",
        "metric_name": "string",
        "value": "real",
        "body": "string",
        "resource": "dynamic",
        "attributes": "dynamic",
    }.items():
        if name in str(getschema_text or "") and name not in columns:
            columns[name] = {"type": typ, "nullable": True}
    resource_fields = {}
    attribute_fields = {}
    for key, val in _parse_fieldstats(fieldstats_text, "resource").items():
        _merge_field(resource_fields, key, val.get("type", "string"), val.get("examples", []))
    for key in _extract_bracketed_keys(sample_text, "resource"):
        _merge_field(resource_fields, key)
    for key in _extract_bracketed_keys(sample_text, "attribute"):
        _merge_field(attribute_fields, key)
    idioms = sorted(set(supported_idioms or ["tail", "take", "top", "summarize", "make-series", "fieldstats"]))
    snapshot = {
        "table": str(table or "default"),
        "schema_hash": "",
        "fetched_at": _now_iso(),
        "columns": dict(sorted(columns.items())),
        "resource_fields": dict(sorted(resource_fields.items())),
        "attribute_fields": dict(sorted(attribute_fields.items())),
        "supported_idioms": idioms,
        "source_status": source_status,
    }
    snapshot["schema_hash"] = schema_hash(snapshot)
    return snapshot


def schema_hash(snapshot):
    normalized = {
        "table": snapshot.get("table"),
        "columns": snapshot.get("columns", {}),
        "resource_fields": snapshot.get("resource_fields", {}),
        "attribute_fields": snapshot.get("attribute_fields", {}),
        "supported_idioms": snapshot.get("supported_idioms", []),
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def schema_fields(snapshot):
    fields = set(snapshot.get("columns", {}).keys())
    for key in snapshot.get("resource_fields", {}):
        fields.add(f"resource['{key}']")
        fields.add(f"resource.{key}")
        fields.add(key)
    for key in snapshot.get("attribute_fields", {}):
        fields.add(f"attributes['{key}']")
        fields.add(f"attributes.{key}")
        fields.add(key)
    return fields


def suggest_field(name, snapshot, limit=3):
    target = str(name or "")
    choices = []
    for key in snapshot.get("columns", {}):
        choices.append(key)
    for key in snapshot.get("resource_fields", {}):
        choices.append(f"resource['{key}']")
    for key in snapshot.get("attribute_fields", {}):
        choices.append(f"attributes['{key}']")
    aliases = {
        "service": "resource['service.name']",
        "service_name": "resource['service.name']",
        "host": "resource['host.name']",
        "host_name": "resource['host.name']",
        "container": "resource['container.name']",
        "container_name": "resource['container.name']",
    }
    ranked = []
    if target in aliases and aliases[target] in choices:
        ranked.append(aliases[target])
    comparable = {c: c.replace("resource['", "").replace("attributes['", "").replace("']", "").replace(".", "_") for c in choices}
    for match in difflib.get_close_matches(target, list(comparable.values()), n=limit * 3, cutoff=0.55):
        for choice, comp in comparable.items():
            if comp == match and choice not in ranked:
                ranked.append(choice)
    return ranked[:limit]


def schema_context(snapshot, max_chars=MAX_CONTEXT_CHARS):
    lines = [
        "Berserk schema is evidence, not a suggestion. Use only fields listed below.",
        "Rows: " + str(snapshot.get("table", "default")),
    ]
    cols = [f"{k}:{v.get('type','unknown')}" for k, v in sorted(snapshot.get("columns", {}).items()) if k not in {"body", "$raw"}]
    if cols:
        lines.append("Fields: " + ", ".join(cols))
    res = [f"{k}:{v.get('type','string')}" for k, v in sorted(snapshot.get("resource_fields", {}).items())]
    if res:
        lines.append("resource paths: " + ", ".join(res))
    attrs = [f"{k}:{v.get('type','string')}" for k, v in sorted(snapshot.get("attribute_fields", {}).items())]
    if attrs:
        lines.append("attribute paths: " + ", ".join(attrs))
    idioms = snapshot.get("supported_idioms") or []
    if idioms:
        lines.append("supported idioms: " + ", ".join(idioms))
    lines.append("Aliases: service -> resource['service.name']; host -> resource['host.name'].")
    lines.append("Rules: time comes from --since; filter before aggregation; bound results; do not invent fields.")
    lines.append("schema_hash: " + str(snapshot.get("schema_hash", "")))
    text = "\n".join(lines)
    # Defensive scrub: no raw bodies/secrets in context.
    text = re.sub(r"(?i)(password|secret|token|api[_-]?key)[^\n,;]*", "[redacted]", text)
    return text[:max_chars]


def _cache_file(config_dir, table):
    return Path(config_dir) / f"schema_snapshot_{re.sub(r'[^A-Za-z0-9_.-]+', '_', table)}.json"


def _read_cache(path):
    data = _store.load_json_dict(path)
    return data or None


def _write_cache(path, snapshot):
    _store.atomic_write_json(path, snapshot, private=True, sort_keys=True)


def _fresh(snapshot, ttl_seconds):
    try:
        fetched = datetime.fromisoformat(str(snapshot.get("fetched_at", "")).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - fetched).total_seconds() <= ttl_seconds
    except (TypeError, ValueError):
        return False


def get_schema_snapshot(*, force=False, table="default", config_dir=None,
                        ttl_seconds=DEFAULT_TTL_SECONDS, fetcher=None):
    """Return a bounded normalized snapshot; refresh when stale."""
    config_dir = config_dir or (Path.home() / ".config" / "berserk-mcp")
    path = _cache_file(config_dir, table)
    with _LOCK:
        cached = _read_cache(path)
        if cached and not force and _fresh(cached, ttl_seconds):
            cached = dict(cached)
            cached["source_status"] = cached.get("source_status") or "fresh"
            return cached
        if fetcher is None:
            if cached:
                cached = dict(cached)
                cached["source_status"] = "stale"
                return cached
            return normalize_snapshot(table=table, source_status="unavailable")
        try:
            raw = fetcher()
            snapshot = normalize_snapshot(
                table=table,
                tables_text=raw.get("tables", ""),
                getschema_text=raw.get("getschema", ""),
                fieldstats_text=raw.get("fieldstats", ""),
                sample_text=raw.get("sample", ""),
                supported_idioms=raw.get("supported_idioms"),
                source_status="fresh",
            )
            _write_cache(path, snapshot)
            return snapshot
        except Exception:
            if cached:
                cached = dict(cached)
                cached["source_status"] = "stale"
                return cached
            return normalize_snapshot(table=table, source_status="unavailable")
