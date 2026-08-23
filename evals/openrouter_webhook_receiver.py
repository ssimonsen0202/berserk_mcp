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
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Resolves secret_scan.py whether run from a repo checkout (evals/ is a
# subdirectory of the repo root secret_scan.py lives in) or deployed
# standalone with secret_scan.py copied alongside this file (its own
# directory is already on sys.path by default in that case).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secret_scan

_KNOWN_ATTR_KEYS = {
    "gen_ai.request.model": "model",
    # OpenRouter's actual wire format uses input_tokens/output_tokens, not the
    # prompt_tokens/completion_tokens names its own docs page describes --
    # confirmed against a real captured trace on 2026-08-22.
    "gen_ai.usage.input_tokens": "prompt_tokens",
    "gen_ai.usage.output_tokens": "completion_tokens",
    "gen_ai.usage.total_cost": "cost_usd",
    "user.id": "user_id",
    "session.id": "session_id",
}

_TOKEN_FIELDS = {"prompt_tokens", "completion_tokens"}


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
        resource_attrs = _attrs_to_dict(
            (resource_span.get("resource") or {}).get("attributes")
        )
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
                    if field:
                        row[field] = int(value) if field in _TOKEN_FIELDS and value is not None else value
                    else:
                        catchall[otlp_key] = value
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
        text = redact(value) if isinstance(value, str) else str(value)
        log_attrs.append({"key": key, "value": {"stringValue": text[:4000]}})

    model = span_attrs.get("gen_ai.request.model") or "unknown-model"
    tokens = span_attrs.get("gen_ai.usage.total_tokens")
    cost = span_attrs.get("gen_ai.usage.total_cost")
    body = redact(f"OpenRouter generation: model={model} total_tokens={tokens} total_cost={cost}")

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
        resource_logs.append({
            "resource": resource,
            "scopeLogs": [{
                "scope": {"name": "openrouter-webhook-forwarder", "version": "1"},
                "logRecords": records,
            }],
        })
    if not resource_logs:
        return None
    return {"resourceLogs": resource_logs}


def post_to_berserk(endpoint, payload, timeout=10, opener=urllib.request.urlopen):
    """Best-effort POST to Berserk's OTLP /v1/logs ingest. Returns
    (ok, detail) -- never raises. Forwarding failure must never affect the
    webhook response to OpenRouter or the local JSONL write, both of which
    have already completed by the time this is called."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return status == 200, f"http {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001 -- forwarding must never raise
        return False, str(exc)


def is_test_connection(headers):
    for key, value in headers.items():
        if key.lower() == "x-test-connection":
            return str(value).strip().lower() == "true"
    return False


def verify_signature(headers, expected_secret):
    if not expected_secret:
        return True
    for key, value in headers.items():
        if key.lower() == "x-webhook-signature":
            return value == expected_secret
    return False


def _provided_signature(headers):
    for key, value in headers.items():
        if key.lower() == "x-webhook-signature":
            return value
    return None


def _make_handler(out_path, raw_out_path, expected_secret, lock, berserk_endpoint=None, post_fn=post_to_berserk):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_POST(self):
            headers = {k: v for k, v in self.headers.items()}
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""

            # A *wrong* signature is always rejected, even on the test-connection
            # handshake -- only a genuinely absent header gets the handshake's
            # documented no-auth-required treatment.
            provided_sig = _provided_signature(headers)
            if provided_sig is not None and provided_sig != expected_secret:
                self._respond(401, {"ok": False, "error": "signature mismatch"})
                return

            if is_test_connection(headers):
                self._respond(200, {"ok": True, "test_connection": True})
                return

            if not verify_signature(headers, expected_secret):
                self._respond(401, {"ok": False, "error": "signature mismatch"})
                return

            received_at = datetime.now(timezone.utc).isoformat()

            with lock:
                if raw_out_path:
                    with open(raw_out_path, "a") as f:
                        f.write(json.dumps({"received_at": received_at, "raw": body.decode("utf-8", "replace")}) + "\n")

            try:
                payload = json.loads(body) if body else {}
            except ValueError as exc:
                sys.stderr.write(f"failed to parse webhook payload as JSON: {exc}\n")
                self._respond(200, {"ok": True, "parsed": False})
                return

            rows = extract_spans(payload)
            with lock:
                with open(out_path, "a") as f:
                    for row in rows:
                        row["received_at"] = received_at
                        f.write(json.dumps(row) + "\n")

            forwarded, forward_detail = None, None
            if berserk_endpoint:
                # Best-effort: local archival above has already succeeded
                # and is the durable record regardless of what happens
                # here. Never let a forwarding failure surface as a
                # webhook error to OpenRouter -- that's the local write's
                # job, already done.
                try:
                    berserk_payload = spans_to_berserk_payload(payload)
                    if berserk_payload is not None:
                        forwarded, forward_detail = post_fn(berserk_endpoint, berserk_payload)
                        if not forwarded:
                            sys.stderr.write(f"berserk forward failed: {forward_detail}\n")
                except Exception as exc:  # noqa: BLE001
                    forwarded, forward_detail = False, str(exc)
                    sys.stderr.write(f"berserk forward raised: {exc}\n")

            resp = {"ok": True, "spans_received": len(rows)}
            if forwarded is not None:
                resp["berserk_forwarded"] = forwarded
            self._respond(200, resp)

        def _respond(self, code, body_dict):
            payload = json.dumps(body_dict).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def run_server(port, out_path, raw_out_path, expected_secret, berserk_endpoint=None):
    lock = threading.Lock()
    handler = _make_handler(out_path, raw_out_path, expected_secret, lock, berserk_endpoint=berserk_endpoint)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(
        f"listening on :{port} -> rows: {out_path}"
        + (f" raw: {raw_out_path}" if raw_out_path else "")
        + (f" berserk: {berserk_endpoint}" if berserk_endpoint else " (berserk forwarding disabled)")
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
        "source (issue #55), e.g. http://100.87.29.100:14318/v1/logs. Omit to disable "
        "forwarding entirely (default) -- local JSONL capture always happens either way.",
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

    run_server(args.port, args.out, args.raw_out, secret, berserk_endpoint=args.berserk_endpoint)


if __name__ == "__main__":
    main()
