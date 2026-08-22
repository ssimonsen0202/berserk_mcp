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
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_KNOWN_ATTR_KEYS = {
    "gen_ai.request.model": "model",
    "gen_ai.usage.prompt_tokens": "prompt_tokens",
    "gen_ai.usage.completion_tokens": "completion_tokens",
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


def _make_handler(out_path, raw_out_path, expected_secret, lock):
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

            self._respond(200, {"ok": True, "spans_received": len(rows)})

        def _respond(self, code, body_dict):
            payload = json.dumps(body_dict).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def run_server(port, out_path, raw_out_path, expected_secret):
    lock = threading.Lock()
    handler = _make_handler(out_path, raw_out_path, expected_secret, lock)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"listening on :{port} -> rows: {out_path}" + (f" raw: {raw_out_path}" if raw_out_path else ""))
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

    run_server(args.port, args.out, args.raw_out, secret)


if __name__ == "__main__":
    main()
