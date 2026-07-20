"""Data-driven ingestion recommendations and live gap analysis."""
import json
import os
from pathlib import Path
import sys

MATURITIES = frozenset({"turnkey", "collector-receiver", "bridge-required", "manual"})
REQUIRED_SOURCE_KEYS = frozenset({"name", "why", "signals", "how_to_ingest", "maturity"})

_list_services = None
_list_metrics = None
_catalog_path = None


def configure(list_services, list_metrics, catalog_path=None):
    global _list_services, _list_metrics, _catalog_path
    _list_services = list_services
    _list_metrics = list_metrics
    _catalog_path = Path(catalog_path) if catalog_path else None


def catalog_path():
    env = os.environ.get("BERSERK_MCP_INGESTION_CATALOG")
    if env:
        return Path(env)
    if _catalog_path is not None:
        return _catalog_path
    adjacent = Path(__file__).with_name("ingestion_catalog.json")
    if adjacent.exists():
        return adjacent
    return Path(sys.prefix) / "share" / "berserk-mcp" / "ingestion_catalog.json"


def load_catalog(path=None):
    target = Path(path) if path else catalog_path()
    with target.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("catalog must be a non-empty object")
    for usecase, sources in catalog.items():
        if not isinstance(usecase, str) or not usecase or not isinstance(sources, list) or not sources:
            raise ValueError("each catalog key must map to a non-empty source list")
        for source in sources:
            if not isinstance(source, dict) or not REQUIRED_SOURCE_KEYS.issubset(source):
                raise ValueError(f"catalog source in {usecase!r} is missing required keys")
            if source["maturity"] not in MATURITIES:
                raise ValueError(f"invalid maturity in {usecase!r}: {source['maturity']!r}")
            signals = source["signals"]
            if not isinstance(signals, dict) or not {"services", "metrics"}.issubset(signals):
                raise ValueError(f"signals in {usecase!r} need services and metrics lists")
            if not all(isinstance(signals[key], list) for key in ("services", "metrics")):
                raise ValueError(f"signals in {usecase!r} must be lists")
            for key in ("name", "why", "how_to_ingest"):
                if not isinstance(source[key], str) or not source[key].strip():
                    raise ValueError(f"{key} in {usecase!r} must be non-empty text")
    return catalog


def _extract_names(text):
    """Extract the first column (name) from each row of tabular output."""
    names = set()
    for line in text.strip().splitlines():
        parts = line.split()
        if parts:
            names.add(parts[0].lower())
    return names


def _match_source(source, services_text, metrics_text):
    service_names = _extract_names(services_text)
    metric_names = _extract_names(metrics_text)
    matches = []
    for hint in source["signals"]["services"]:
        h = str(hint).lower()
        if h in service_names:
            matches.append(f"service:{hint}")
    for hint in source["signals"]["metrics"]:
        h = str(hint).lower()
        if any(m == h or m.startswith(h) for m in metric_names):
            matches.append(f"metric:{hint}")
    return matches


def suggest_ingestion(role_or_usecase, check_gap=False, since="24h ago"):
    try:
        catalog = load_catalog()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return f"Could not load ingestion catalog: {type(exc).__name__}", True

    key = str(role_or_usecase or "").strip().lower()
    if key not in catalog:
        available = ", ".join(sorted(catalog))
        return f"Unknown role or use case: {key or '(empty)'}. Available: {available}", False

    services_text = ""
    metrics_text = ""
    inventory_warning = ""
    if check_gap:
        services_text, services_err = _list_services(since)
        metrics_text, metrics_err = _list_metrics(since)
        if services_err or metrics_err:
            failed = []
            if services_err:
                failed.append("services")
                services_text = ""
            if metrics_err:
                failed.append("metrics")
                metrics_text = ""
            inventory_warning = "Gap check incomplete; failed inventory: " + ", ".join(failed)

    body = []
    present_count = 0
    for source in catalog[key]:
        matches = _match_source(source, services_text, metrics_text) if check_gap else []
        if check_gap and matches:
            status = "present"
            present_count += 1
        elif check_gap:
            status = "missing"
        else:
            status = "recommended"
        body.append(f"- [{status}] {source['name']} (maturity={source['maturity']})")
        body.append(f"  Why: {source['why']}")
        if matches:
            body.append("  Matched: " + ", ".join(matches))
        body.append(f"  Ingest: {source['how_to_ingest']}")

    # Assemble the header block in order rather than insert()-ing into it,
    # so the gap summary and any inventory warning land predictably.
    header = [f"Ingestion recommendations for {key}:"]
    if check_gap:
        header.append(
            f"Gap summary: {present_count} present, "
            f"{len(catalog[key]) - present_count} missing."
        )
    if inventory_warning:
        header.append(inventory_warning)
    return "\n".join(header + body), False
