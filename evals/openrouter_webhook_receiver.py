"""Minimal receiver for OpenRouter's Observability -> Broadcast webhook.

Captures per-call token usage/cost telemetry (OTLP/JSON spans with GenAI
semantic-convention attributes) that OpenRouter pushes out-of-band, as a
passive complement to issue #37's in-harness cost computation. Every raw
payload is preserved as received; parsed rows are a best-effort flattening
of the fields OpenRouter documents (gen_ai.request.model,
gen_ai.usage.prompt_tokens, gen_ai.usage.completion_tokens, user/session
ids). Unrecognized attributes are kept under "attributes" rather than
dropped, since the exact attribute set isn't contractually fixed.

This binds to 0.0.0.0 and is meant to sit behind a Tailscale Funnel URL
reachable from OpenRouter's cloud. A shared secret (checked against the
X-Webhook-Signature header you configure in OpenRouter's webhook UI) is
mandatory at startup -- there is no unauthenticated fallback.

Optionally (issue #55) also forwards each received span into Berserk as
its own source (service.name, preserved as OpenRouter itself sets it on
the resource -- normally "openrouter") when --berserk-endpoint is given.
This is strictly additive: the local JSONL write always happens first and
is never affected by forwarding success or failure. Forwarding converts
OTLP spans into the OTLP log-record shape Berserk's ingest actually
indexes (confirmed live 2026-08-23 -- a payload posted to /v1/traces
returns 200 but is never queryable; /v1/logs with
timeUnixNano/severityNumber/severityText/body/attributes is what lands).
Every string-valued span attribute is redacted via secret_scan.redact()
before it leaves this box -- gen_ai.prompt/gen_ai.completion/trace.input/
trace.output carry full real prompt/completion text from live testing.

Signature modes (--signature-mode):
  static           (default) X-Webhook-Signature must equal the shared secret.
                   This is what OpenRouter's Broadcast webhook docs describe:
                   a custom header whose value you set yourself.
  openrouter-hmac  X-OpenRouter-Signature: t=<unix>,v1=<hex HMAC-SHA256 of
                   "<t>," + raw body>, accepted within 300 s. Taken from
                   OpenRouter's video-generation callback docs; NOT confirmed
                   for Broadcast deliveries. Use only if OpenRouter is shown
                   to sign Broadcast payloads this way.
Both modes compare in constant time. A wrong signature is always 401; an
absent one passes only the X-Test-Connection handshake.

Forward failures (--berserk-endpoint) are summarized on stderr: one line
when forwarding starts failing, a periodic "still FAILING" line while it
stays broken, and one line on recovery. Any 2xx from ingest is success.

Reply speed and duplicates: the webhook reply goes out as soon as the local
capture is written. Redaction and the forward to Berserk run on background
workers behind a bounded queue, because Berserk's ingest answers in about
two seconds and OpenRouter retries a delivery it thinks is slow (observed
2026-11-16: most spans arrived twice). Each (traceId, spanId) is also
claimed once; a retried delivery is kept in the raw capture but is not
written to the spans file or forwarded again. If the local capture cannot be
written the reply is 500 (so OpenRouter retries), and the claim is released
so that retry is processed.
"""

import argparse
import functools
import hashlib
import hmac
import json
import os
import queue
import sys
import threading
import time
import urllib.error
from collections import OrderedDict
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Resolves secret_scan.py whether run from a repo checkout (evals/ is a
# subdirectory of the repo root secret_scan.py lives in) or deployed
# standalone with secret_scan.py copied alongside this file (its own
# directory is already on sys.path by default in that case).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _http  # noqa: E402
import secret_scan

_KNOWN_ATTR_KEYS = {
    "gen_ai.request.model": "model",
    "gen_ai.response.model": "model",
    "gen_ai.model": "model",
    # OpenRouter's actual wire format uses input_tokens/output_tokens, not the
    # prompt_tokens/completion_tokens names its own docs page describes --
    # confirmed against a real captured trace on 2026-08-22.
    "gen_ai.usage.input_tokens": "prompt_tokens",
    "gen_ai.usage.output_tokens": "completion_tokens",
    "gen_ai.usage.total_cost": "cost_usd",
    "gen_ai.usage.total_tokens": "total_tokens",
    "user.id": "user_id",
    "session.id": "session_id",
}

_TOKEN_FIELDS = {"prompt_tokens", "completion_tokens", "total_tokens"}

_SAFE_METADATA_KEYS = {
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.model",
    "trace.metadata.arm",
    "trace.metadata.case_id",
    "trace.metadata.run_id",
    "trace.metadata.handoff_id",
    "trace.metadata.trial_id",
    "experiment.arm",
    "experiment.case_id",
    "experiment.run_id",
    "experiment.handoff_id",
    "experiment.trial_id",
}


def _safe_metadata(value):
    """Keep bounded identifiers queryable without shipping free-form text."""
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    if not all(char.isalnum() or char in "._:/-" for char in value):
        return None
    return value


def _model_from_attrs(attrs):
    for key in ("gen_ai.request.model", "gen_ai.response.model", "gen_ai.model"):
        model = _safe_metadata(attrs.get(key))
        if model:
            return model
    return "unknown-model"


def _unwrap_otlp_value(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        return [_unwrap_otlp_value(v) for v in value["arrayValue"].get("values", [])]
    return None


def _attrs_to_dict(attribute_list):
    out = {}
    for attr in attribute_list or []:
        key = attr.get("key")
        if key is None:
            continue
        out[key] = _unwrap_otlp_value(attr.get("value", {}))
    return out


def extract_spans(payload):
    """Flatten an OTLP/JSON resourceSpans payload into one dict per span."""
    rows = []
    for resource_span in payload.get("resourceSpans") or []:
        resource_attrs = _attrs_to_dict((resource_span.get("resource") or {}).get("attributes"))
        service_name = resource_attrs.get("service.name")
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                span_attrs = _attrs_to_dict(span.get("attributes"))
                row = {
                    "trace_id": span.get("traceId"),
                    "span_id": span.get("spanId"),
                    "name": span.get("name"),
                    "start_time_unix_nano": span.get("startTimeUnixNano"),
                    "end_time_unix_nano": span.get("endTimeUnixNano"),
                    "service_name": service_name,
                }
                catchall = {}
                for otlp_key, value in span_attrs.items():
                    field = _KNOWN_ATTR_KEYS.get(otlp_key)
                    if not field:
                        catchall[otlp_key] = value
                        continue
                    if field in _TOKEN_FIELDS and value is not None:
                        try:
                            value = int(value)
                        except (TypeError, ValueError):
                            # A bad count must not drop the whole span: keep the raw value.
                            catchall[otlp_key] = value
                            continue
                    row[field] = value
                for field in _KNOWN_ATTR_KEYS.values():
                    row.setdefault(field, None)
                row["attributes"] = catchall
                rows.append(row)
    return rows


# Default redaction: entropy-based secret detection plus every PII type
# secret_scan knows, matching the rest of this project's ingestion paths
# (claude-otel-forwarder redacts every line the same way before shipping).
def default_redact(text):
    return secret_scan.redact(text, include_entropy=True, pii_types=secret_scan.ALL_PII_TYPES)[0]


def span_to_log_record(span, redact=default_redact):
    """Convert one OTLP span dict (as parsed by extract_spans' inner loop,
    i.e. the raw span object with traceId/spanId/name/attributes) into the
    OTLP log-record shape Berserk's ingest actually indexes -- confirmed
    live 2026-08-23 that /v1/traces accepts but never stores span-shaped
    payloads, while /v1/logs with this exact shape (timeUnixNano,
    severityNumber, severityText, body, attributes) lands and is
    queryable. Every string-valued attribute is redacted before it leaves
    this box -- gen_ai.prompt/gen_ai.completion/trace.input/trace.output
    carry full real prompt/completion text."""
    span_attrs = _attrs_to_dict(span.get("attributes"))

    log_attrs = [
        {"key": "trace.id", "value": {"stringValue": str(span.get("traceId") or "")}},
        {"key": "span.id", "value": {"stringValue": str(span.get("spanId") or "")}},
        {"key": "span.name", "value": {"stringValue": str(span.get("name") or "")}},
    ]
    for key, value in span_attrs.items():
        if value is None:
            continue
        if key in _SAFE_METADATA_KEYS:
            text = _safe_metadata(value)
            if text is None:
                continue
        else:
            text = redact(value) if isinstance(value, str) else str(value)
        log_attrs.append({"key": key, "value": {"stringValue": text[:4000]}})

    model = _model_from_attrs(span_attrs)
    tokens = span_attrs.get("gen_ai.usage.total_tokens")
    cost = span_attrs.get("gen_ai.usage.total_cost")
    body_prefix = redact("OpenRouter generation")
    body = f"{body_prefix}: model={model} total_tokens={tokens} total_cost={cost}"

    # Canonical query fields survive provider-specific attribute naming and
    # avoid relying on a redacted free-form body for experiment joins.
    log_attrs.append({"key": "openrouter.model", "value": {"stringValue": model}})
    for key in _SAFE_METADATA_KEYS - {"gen_ai.request.model", "gen_ai.response.model", "gen_ai.model"}:
        safe_value = _safe_metadata(span_attrs.get(key))
        if safe_value:
            log_attrs.append({"key": key, "value": {"stringValue": safe_value}})

    start_ns = span.get("startTimeUnixNano")
    try:
        time_unix_nano = str(int(start_ns))
    except (TypeError, ValueError):
        time_unix_nano = str(int(time.time() * 1e9))

    return {
        "timeUnixNano": time_unix_nano,
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": body},
        "attributes": log_attrs,
    }


def spans_to_berserk_payload(payload, redact=default_redact):
    """Build one OTLP resourceLogs envelope per resourceSpans block in the
    original webhook payload, preserving whatever resource attributes
    OpenRouter itself set (service.name="openrouter" among them --
    confirmed present at the source, not something this forwarder needs
    to inject). Returns None if there is nothing to forward."""
    resource_logs = []
    for resource_span in payload.get("resourceSpans") or []:
        resource = resource_span.get("resource") or {}
        records = []
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                records.append(span_to_log_record(span, redact=redact))
        if not records:
            continue
        resource_logs.append(
            {
                "resource": resource,
                "scopeLogs": [
                    {
                        "scope": {"name": "openrouter-webhook-forwarder", "version": "1"},
                        "logRecords": records,
                    }
                ],
            }
        )
    if not resource_logs:
        return None
    return {"resourceLogs": resource_logs}


def post_to_berserk(endpoint, payload, timeout=10, *, allow_plaintext_remote=False, send=None):
    """Best-effort POST to Berserk's OTLP /v1/logs ingest. Returns
    (ok, detail) -- never raises. Forwarding failure must never affect the
    webhook response to OpenRouter or the local JSONL write, both of which
    have already completed by the time this is called.

    Goes through the shared HTTP client (_http.post_bytes_status): URL policy,
    no redirects, bounded response. Plaintext to a non-loopback host needs
    allow_plaintext_remote=True (the --allow-plaintext-remote flag)."""
    send = send or _http.post_bytes_status
    try:
        status = send(
            endpoint,
            {"Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
            timeout=timeout,
            label="berserk otlp endpoint",
            allow_plaintext_remote=allow_plaintext_remote,
        )
        # Any 2xx means ingest accepted the payload (202/204 included).
        return 200 <= status < 300, f"http {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001 -- forwarding must never raise
        return False, str(exc)


def is_test_connection(headers):
    for key, value in headers.items():
        if key.lower() == "x-test-connection":
            return str(value).strip().lower() == "true"
    return False


SIGNATURE_MODES = ("static", "openrouter-hmac")
HMAC_TOLERANCE_SECONDS = 300


def _header(headers, name):
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _equal(a, b):
    """Constant-time string comparison; non-ASCII input is simply unequal."""
    try:
        return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))
    except (UnicodeEncodeError, AttributeError):
        return False


def _check_openrouter_hmac(value, body, secret, now):
    parts = {}
    for item in value.split(","):
        key, sep, val = item.strip().partition("=")
        if not sep:
            return "invalid"
        parts.setdefault(key, val)
    ts_text, sig = parts.get("t", ""), parts.get("v1", "")
    # Bound the length before int(): a unix time has 10 digits, and int()
    # raises ValueError past ~4300 digits, which would drop the connection
    # instead of answering 401.
    if not ts_text.isascii() or not ts_text.isdigit() or len(ts_text) > 12 or not sig:
        return "invalid"
    ts = int(ts_text)
    if abs((time.time() if now is None else now) - ts) > HMAC_TOLERANCE_SECONDS:
        return "invalid"
    expected = hmac.new(secret.encode(), f"{ts},".encode() + body, hashlib.sha256).hexdigest()
    return "valid" if _equal(sig, expected) else "invalid"


def check_signature(headers, body, expected_secret, mode="static", now=None):
    """Return "valid", "invalid" or "absent" for one request.

    `body` must be the raw bytes as received (HMAC covers exact bytes).
    Each mode reads only its own header, so the static secret is never
    accepted in HMAC mode and vice versa."""
    if mode not in SIGNATURE_MODES:
        raise ValueError(f"unknown signature mode: {mode!r}")
    if not expected_secret:
        return "valid"
    if mode == "static":
        provided = _header(headers, "x-webhook-signature")
        if provided is None:
            return "absent"
        return "valid" if _equal(provided, expected_secret) else "invalid"
    provided = _header(headers, "x-openrouter-signature")
    if provided is None:
        return "absent"
    return _check_openrouter_hmac(provided, body, expected_secret, now)


def verify_signature(headers, expected_secret):
    """Static-mode check kept for callers and tests: True only if valid."""
    return check_signature(headers, b"", expected_secret) == "valid"


class ForwardStats:
    """Thread-safe Berserk forwarding counters that turn per-delivery
    outcomes into a few log lines: FAILING on the first failure, a
    periodic "still FAILING" summary, RECOVERED when it works again."""

    def __init__(self, summary_interval=900, clock=time.time):
        self._lock = threading.Lock()
        self._interval = summary_interval
        self._clock = clock
        self.ok = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.last_error = None
        self.last_ok_at = None
        self._last_summary_at = None

    def record(self, ok, detail):
        """Count one outcome; return the log lines it warrants (maybe none)."""
        with self._lock:
            now = self._clock()
            if ok:
                self.ok += 1
                self.last_ok_at = now
                lines = []
                if self.consecutive_failures:
                    lines.append(
                        f"berserk forward RECOVERED after {self.consecutive_failures} consecutive "
                        f"failures (last error: {self.last_error}); now {detail}"
                    )
                self.consecutive_failures = 0
                return lines
            self.failed += 1
            self.consecutive_failures += 1
            self.last_error = detail
            if self.consecutive_failures == 1:
                self._last_summary_at = now
                return [f"berserk forward FAILING: {detail} (ok={self.ok} failed={self.failed})"]
            if now - self._last_summary_at >= self._interval:
                self._last_summary_at = now
                return [
                    f"berserk forward still FAILING: {self.consecutive_failures} consecutive failures, "
                    f"ok={self.ok} failed={self.failed}, last error: {detail}"
                ]
            return []

    def snapshot(self):
        with self._lock:
            return {
                "ok": self.ok,
                "failed": self.failed,
                "consecutive_failures": self.consecutive_failures,
                "last_error": self.last_error,
                "last_ok_at": self.last_ok_at,
            }


class _Claim:
    """Proof of ownership of one (traceId, spanId) claim."""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key


class SpanDeduper:
    """Thread-safe, bounded set of (traceId, spanId) pairs already handled.

    claim() is atomic: of two concurrent deliveries of the same span exactly
    one wins and receives a _Claim; the other receives None. release() undoes
    claims whose local write failed, so the retry is processed. Only the owner
    can release: if a claim was evicted and a retry re-claimed the span, a late
    release from the first attempt does nothing. The oldest entries are evicted
    past max_size."""

    def __init__(self, max_size=5000):
        self._lock = threading.Lock()
        self._max = max_size
        self._seen = OrderedDict()

    def claim(self, key):
        with self._lock:
            if key in self._seen:
                return None
            claim = _Claim(key)
            self._seen[key] = claim
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return claim

    def release(self, claims):
        with self._lock:
            for claim in claims:
                if self._seen.get(claim.key) is claim:
                    del self._seen[claim.key]

    def __len__(self):
        with self._lock:
            return len(self._seen)


def span_key(span):
    """(traceId, spanId), or None when either is missing (never deduplicated)."""
    trace_id, span_id = span.get("traceId"), span.get("spanId")
    return (trace_id, span_id) if trace_id and span_id else None


def claim_new_spans(payload, deduper):
    """Split a webhook payload into spans not seen before.

    Returns (filtered_payload, claims, new_count, duplicate_count). The
    filtered payload keeps each resource/scope envelope and only the new spans.
    If the payload shape is wrong, partial claims are released and the
    exception propagates."""
    claimed, new, dup, out_resources = [], 0, 0, []
    try:
        for resource_span in payload.get("resourceSpans") or []:
            out_scopes = []
            for scope_span in resource_span.get("scopeSpans") or []:
                keep = []
                for span in scope_span.get("spans") or []:
                    key = span_key(span)
                    if key is not None:
                        claim = deduper.claim(key)
                        if claim is None:
                            dup += 1
                            continue
                        claimed.append(claim)
                    keep.append(span)
                    new += 1
                if keep:
                    out_scopes.append({**scope_span, "spans": keep})
            if out_scopes:
                out_resources.append({**resource_span, "scopeSpans": out_scopes})
    except (AttributeError, TypeError):
        deduper.release(claimed)
        raise
    return {**payload, "resourceSpans": out_resources}, claimed, new, dup


def _start_forwarders(berserk_endpoint, post_fn, stats, workers, queue_size):
    """Start the background workers that redact and forward deliveries to
    Berserk. Returns the bounded queue the handler feeds."""
    forward_queue = queue.Queue(maxsize=queue_size)

    def forward_one(item):
        lines = []
        try:
            berserk_payload = spans_to_berserk_payload(item)
            if berserk_payload is not None:
                ok, detail = post_fn(berserk_endpoint, berserk_payload)
                lines = stats.record(ok, detail)
        except Exception as exc:  # noqa: BLE001 -- forwarding must never kill the worker
            lines = stats.record(False, f"raised: {exc}")
        # Failures are logged by ForwardStats only (state changes and a
        # periodic summary), never once per delivery: a long outage
        # must not turn every webhook into a log line.
        for line in lines:
            sys.stderr.write(line + "\n")

    def forward_worker():
        while True:
            item = forward_queue.get()
            try:
                forward_one(item)
            except Exception as exc:  # noqa: BLE001 -- nothing may end the worker
                try:
                    sys.stderr.write(f"forward worker error: {exc!r}\n")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                forward_queue.task_done()

    for i in range(workers):
        threading.Thread(target=forward_worker, name=f"berserk-forward-{i}", daemon=True).start()
    return forward_queue


class _Delivery:
    """Everything one webhook delivery needs, shared by all requests."""

    def __init__(self, out_path, raw_out_path, lock, deduper, stats, berserk_endpoint, forward_queue):
        self.out_path = out_path
        self.raw_out_path = raw_out_path
        self.lock = lock
        self.deduper = deduper
        self.stats = stats
        self.berserk_endpoint = berserk_endpoint
        self.forward_queue = forward_queue

    def _capture_raw(self, body, received_at):
        with self.lock:
            if self.raw_out_path:
                with open(self.raw_out_path, "a") as f:
                    f.write(json.dumps({"received_at": received_at, "raw": body.decode("utf-8", "replace")}) + "\n")

    def _write_rows(self, rows, received_at):
        """Append all rows in one write. If it fails part-way, cut the file back
        to its old size so the sender's retry cannot leave duplicate rows."""
        data = "".join(json.dumps({**row, "received_at": received_at}) + "\n" for row in rows)
        with self.lock:
            try:
                size = os.path.getsize(self.out_path)
            except OSError:
                size = 0
            try:
                with open(self.out_path, "a") as f:
                    f.write(data)
            except OSError:
                try:
                    os.truncate(self.out_path, size)
                except OSError:
                    pass
                raise

    def _store(self, payload, received_at):
        """Claim, extract and write the new spans.

        Returns (early_reply, stored). early_reply is a (status, body) pair when
        the delivery cannot continue; otherwise stored is
        (filtered_payload, rows, new_count, duplicate_count)."""
        try:
            filtered, claimed, n_new, n_dup = claim_new_spans(payload, self.deduper)
        except (AttributeError, TypeError) as exc:
            sys.stderr.write(f"unexpected webhook payload shape ({exc}); kept in the raw capture only\n")
            return (200, {"ok": True, "parsed": False}), None
        try:
            rows = extract_spans(filtered)
            self._write_rows(rows, received_at)
        except OSError as exc:
            self.deduper.release(claimed)  # the retry must be processed, not skipped
            sys.stderr.write(f"storage failure writing spans: {exc}\n")
            return (500, {"ok": False, "error": "storage failure"}), None
        except Exception as exc:  # noqa: BLE001 -- any failure after the claim must release it
            self.deduper.release(claimed)
            sys.stderr.write(f"could not process webhook payload ({exc!r}); kept in the raw capture only\n")
            return (200, {"ok": True, "parsed": False}), None
        return None, (filtered, rows, n_new, n_dup)

    def _enqueue(self, filtered):
        """Queue the new spans for forwarding; returns the status word for the reply."""
        try:
            self.forward_queue.put_nowait(filtered)
            return "queued"
        except queue.Full:
            pending = self.forward_queue.maxsize
            for line in self.stats.record(False, f"forward queue full ({pending} pending)"):
                sys.stderr.write(line + "\n")
            return "dropped"

    def process(self, body):
        """Handle one authenticated, non-handshake delivery. Returns (status, reply)."""
        received_at = datetime.now(UTC).isoformat()
        try:
            self._capture_raw(body, received_at)
        except OSError as exc:
            sys.stderr.write(f"storage failure writing raw capture: {exc}\n")
            return 500, {"ok": False, "error": "storage failure"}

        try:
            payload = json.loads(body) if body else {}
        except ValueError as exc:
            sys.stderr.write(f"failed to parse webhook payload as JSON: {exc}\n")
            return 200, {"ok": True, "parsed": False}
        if not isinstance(payload, dict):
            sys.stderr.write("webhook payload is not a JSON object; kept in the raw capture only\n")
            return 200, {"ok": True, "parsed": False}

        early, stored = self._store(payload, received_at)
        if early:
            return early
        filtered, rows, n_new, n_dup = stored

        resp = {"ok": True, "spans_received": len(rows)}
        if n_dup:
            resp["duplicates_skipped"] = n_dup
        if self.berserk_endpoint:
            # Best-effort and off the reply path: the local capture is the
            # durable record, so a failed or dropped forward loses nothing
            # that backfill cannot recover.
            resp["berserk_forward"] = "skipped" if n_new == 0 else self._enqueue(filtered)
        return 200, resp


def _make_handler(
    out_path,
    raw_out_path,
    expected_secret,
    lock,
    berserk_endpoint=None,
    post_fn=post_to_berserk,
    signature_mode="static",
    stats=None,
    forward_workers=2,
    forward_queue_size=200,
    dedupe_size=5000,
    max_body_bytes=16 * 1024 * 1024,
):
    if signature_mode not in SIGNATURE_MODES:
        raise ValueError(f"unknown signature mode: {signature_mode!r}")
    stats = stats or ForwardStats()
    deduper = SpanDeduper(dedupe_size)
    forward_queue = (
        _start_forwarders(berserk_endpoint, post_fn, stats, forward_workers, forward_queue_size)
        if berserk_endpoint
        else queue.Queue(maxsize=forward_queue_size)
    )
    delivery = _Delivery(out_path, raw_out_path, lock, deduper, stats, berserk_endpoint, forward_queue)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

        def do_POST(self):
            headers = {k: v for k, v in self.headers.items()}
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > max_body_bytes:
                # Refuse before reading: the body would otherwise sit in memory
                # (and, queued for forwarding, stay there).
                self._respond(413, {"ok": False, "error": "payload too large"})
                return
            body = self.rfile.read(length) if length else b""

            # A *wrong* signature is always rejected, even on the test-connection
            # handshake -- only a genuinely absent header gets the handshake's
            # documented no-auth-required treatment. Checked against the raw
            # body bytes, before any parsing.
            sig_state = check_signature(headers, body, expected_secret, mode=signature_mode)
            if sig_state == "invalid":
                self._respond(401, {"ok": False, "error": "signature mismatch"})
                return

            if is_test_connection(headers):
                self._respond(200, {"ok": True, "test_connection": True})
                return

            if sig_state != "valid":
                self._respond(401, {"ok": False, "error": "signature mismatch"})
                return

            self._respond(*delivery.process(body))

        def _respond(self, code, body_dict):
            payload = json.dumps(body_dict).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    Handler.forward_queue = forward_queue
    Handler.stats = stats
    Handler.deduper = deduper
    return Handler


def run_server(
    port,
    out_path,
    raw_out_path,
    expected_secret,
    berserk_endpoint=None,
    allow_plaintext_remote=False,
    signature_mode="static",
):
    lock = threading.Lock()
    handler = _make_handler(
        out_path,
        raw_out_path,
        expected_secret,
        lock,
        berserk_endpoint=berserk_endpoint,
        post_fn=functools.partial(post_to_berserk, allow_plaintext_remote=allow_plaintext_remote),
        signature_mode=signature_mode,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(
        f"listening on :{port} -> rows: {out_path}"
        + (f" raw: {raw_out_path}" if raw_out_path else "")
        + (f" berserk: {berserk_endpoint}" if berserk_endpoint else " (berserk forwarding disabled)")
        + f" signature-mode: {signature_mode}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--out", default="evals/results/openrouter_webhook_spans.jsonl")
    parser.add_argument("--raw-out", default="evals/results/openrouter_webhook_raw.jsonl")
    parser.add_argument(
        "--secret-env",
        default="OPENROUTER_WEBHOOK_SECRET",
        help="env var holding the shared secret; must match the X-Webhook-Signature header value configured in OpenRouter's webhook UI",
    )
    parser.add_argument(
        "--secret-file",
        default=None,
        help="path to a file containing the shared secret (trailing newline stripped); "
        "takes precedence over --secret-env if both resolve to a value. Prefer this over "
        "an inline env assignment on the command line -- that form leaks the secret into "
        "argv, visible to anyone on the host via ps/pgrep.",
    )
    parser.add_argument(
        "--berserk-endpoint",
        default=None,
        help="OTLP /v1/logs URL to also forward each received span into Berserk as its own "
        "source (issue #55), e.g. http://100.87.29.100:14318/v1/logs (plain http to a "
        "non-loopback address needs --allow-plaintext-remote). Omit to disable "
        "forwarding entirely (default) -- local JSONL capture always happens either way.",
    )
    parser.add_argument(
        "--allow-plaintext-remote",
        action="store_true",
        help="allow plain http:// to a non-loopback --berserk-endpoint (e.g. a Tailscale address, "
        "where the tunnel itself encrypts). Without it such endpoints are refused. A configured http_proxy "
        "still applies to that plaintext; exempt the host with no_proxy.",
    )
    parser.add_argument(
        "--signature-mode",
        choices=SIGNATURE_MODES,
        default="static",
        help="static (default): X-Webhook-Signature must equal the secret, as OpenRouter's Broadcast "
        "webhook docs describe. openrouter-hmac: verify X-OpenRouter-Signature (t=..,v1=HMAC-SHA256 of "
        '"<t>," + raw body, 300 s window) -- from OpenRouter\'s video-callback docs, not confirmed '
        "for Broadcast.",
    )
    args = parser.parse_args(argv)

    secret = None
    if args.secret_file:
        try:
            with open(args.secret_file) as f:
                secret = f.read().strip()
        except OSError as exc:
            sys.stderr.write(f"refusing to start: could not read --secret-file {args.secret_file}: {exc}\n")
            sys.exit(1)
    if not secret:
        secret = os.environ.get(args.secret_env)

    if not secret:
        sys.stderr.write(
            f"refusing to start: neither --secret-file nor ${args.secret_env} provided a secret. This "
            "server is meant to sit behind a publicly reachable Tailscale Funnel URL, so it must not "
            "accept unauthenticated requests. Provide the same value configured as the X-Webhook-Signature "
            "header in OpenRouter's webhook destination settings, then retry.\n"
        )
        sys.exit(1)

    run_server(
        args.port,
        args.out,
        args.raw_out,
        secret,
        berserk_endpoint=args.berserk_endpoint,
        allow_plaintext_remote=args.allow_plaintext_remote,
        signature_mode=args.signature_mode,
    )


if __name__ == "__main__":
    main()
