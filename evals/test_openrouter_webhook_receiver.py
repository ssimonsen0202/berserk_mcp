import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.client import HTTPConnection
from unittest import mock

import openrouter_webhook_receiver as receiver
from openrouter_webhook_receiver import (
    ForwardStats,
    SpanDeduper,
    claim_new_spans,
    _make_handler,
    check_signature,
    extract_spans,
    is_test_connection,
    post_to_berserk,
    span_to_log_record,
    spans_to_berserk_payload,
    verify_signature,
)
from http.server import ThreadingHTTPServer


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _sample_payload(n=1):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "openrouter")]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": f"trace-{n}",
                                "spanId": f"span-{n}",
                                "name": "chat.completions",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000500000000",
                                "attributes": [
                                    _attr("gen_ai.request.model", "openai/gpt-4"),
                                    _attr("gen_ai.usage.input_tokens", 100),
                                    _attr("gen_ai.usage.output_tokens", 50),
                                    _attr("gen_ai.usage.total_cost", 0.02),
                                    _attr("user.id", "u-1"),
                                    _attr("session.id", "s-1"),
                                    _attr("trace.metadata.case_id", "router-07"),
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


class ExtractSpansTest(unittest.TestCase):
    def test_flattens_known_gen_ai_fields(self):
        rows = extract_spans(_sample_payload())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["trace_id"], "trace-1")
        self.assertEqual(row["span_id"], "span-1")
        self.assertEqual(row["name"], "chat.completions")
        self.assertEqual(row["model"], "openai/gpt-4")
        self.assertEqual(row["prompt_tokens"], 100)
        self.assertEqual(row["completion_tokens"], 50)
        self.assertEqual(row["cost_usd"], 0.02)
        self.assertEqual(row["user_id"], "u-1")
        self.assertEqual(row["session_id"], "s-1")
        self.assertEqual(row["service_name"], "openrouter")

    def test_keeps_unrecognized_attributes_in_catchall(self):
        rows = extract_spans(_sample_payload())
        self.assertEqual(rows[0]["attributes"]["trace.metadata.case_id"], "router-07")

    def test_numeric_token_attributes_are_ints_not_strings(self):
        rows = extract_spans(_sample_payload())
        self.assertIsInstance(rows[0]["prompt_tokens"], int)
        self.assertIsInstance(rows[0]["completion_tokens"], int)

    def test_a_non_numeric_token_value_is_kept_not_fatal(self):
        payload = _payload_with_spans([1])
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        span["attributes"].append(_attr("gen_ai.usage.total_tokens", "not-an-integer"))
        rows = extract_spans(payload)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["total_tokens"])
        self.assertEqual(rows[0]["attributes"]["gen_ai.usage.total_tokens"], "not-an-integer")

    def test_empty_resource_spans_yields_no_rows(self):
        self.assertEqual(extract_spans({"resourceSpans": []}), [])

    def test_missing_resource_spans_key_yields_no_rows(self):
        self.assertEqual(extract_spans({}), [])

    def test_multiple_spans_across_multiple_resource_and_scope_levels(self):
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", "svc-a")]},
                    "scopeSpans": [
                        {"spans": [{"traceId": "t1", "spanId": "s1", "name": "n1", "attributes": []}]},
                        {"spans": [{"traceId": "t2", "spanId": "s2", "name": "n2", "attributes": []}]},
                    ],
                },
                {
                    "resource": {"attributes": [_attr("service.name", "svc-b")]},
                    "scopeSpans": [
                        {"spans": [{"traceId": "t3", "spanId": "s3", "name": "n3", "attributes": []}]},
                    ],
                },
            ]
        }
        rows = extract_spans(payload)
        self.assertEqual([r["span_id"] for r in rows], ["s1", "s2", "s3"])
        self.assertEqual([r["service_name"] for r in rows], ["svc-a", "svc-a", "svc-b"])

    def test_span_missing_gen_ai_attributes_has_none_fields_not_a_crash(self):
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [{"spans": [{"traceId": "t", "spanId": "s", "name": "n", "attributes": []}]}],
                }
            ]
        }
        row = extract_spans(payload)[0]
        self.assertIsNone(row["model"])
        self.assertIsNone(row["prompt_tokens"])
        self.assertIsNone(row["completion_tokens"])

    def test_response_model_alias_is_preserved(self):
        rows = extract_spans(
            {
                "resourceSpans": [
                    {
                        "resource": {"attributes": []},
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "t",
                                        "spanId": "s",
                                        "name": "n",
                                        "attributes": [_attr("gen_ai.response.model", "qwen/qwen3.8-27b:free")],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(rows[0]["model"], "qwen/qwen3.8-27b:free")


def _raw_span(**attrs):
    return {
        "traceId": "trace-1",
        "spanId": "span-1",
        "name": "chat.completions",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }


class SpanToLogRecordTest(unittest.TestCase):
    def test_shape_matches_what_berserk_ingest_actually_indexes(self):
        # Confirmed live 2026-08-23: a payload posted to /v1/traces returns
        # 200 but is never queryable; this exact set of logRecord fields
        # (timeUnixNano/severityNumber/severityText/body/attributes) via
        # /v1/logs is what lands. Don't drop any of these without
        # re-verifying against the real ingest service.
        rec = span_to_log_record(_raw_span(**{"gen_ai.request.model": "openai/gpt-4"}))
        self.assertEqual(set(rec.keys()), {"timeUnixNano", "severityNumber", "severityText", "body", "attributes"})
        self.assertEqual(rec["timeUnixNano"], "1700000000000000000")
        self.assertIsInstance(rec["severityNumber"], int)

    def test_trace_and_span_id_always_present_as_attributes(self):
        rec = span_to_log_record(_raw_span())
        keys = {a["key"] for a in rec["attributes"]}
        self.assertIn("trace.id", keys)
        self.assertIn("span.id", keys)
        self.assertIn("span.name", keys)

    def test_missing_start_time_falls_back_to_now_not_a_crash(self):
        span = _raw_span()
        del span["startTimeUnixNano"]
        rec = span_to_log_record(span)
        self.assertTrue(rec["timeUnixNano"].isdigit())

    def test_string_attributes_are_redacted(self):
        seen = []

        def fake_redact(text):
            seen.append(text)
            return "[REDACTED]"

        rec = span_to_log_record(
            _raw_span(**{"gen_ai.prompt": "my api key is sk-proj-realsecret"}),
            redact=fake_redact,
        )
        prompt_attr = next(a for a in rec["attributes"] if a["key"] == "gen_ai.prompt")
        self.assertEqual(prompt_attr["value"]["stringValue"], "[REDACTED]")
        self.assertIn("my api key is sk-proj-realsecret", seen)

    def test_body_summary_is_also_redacted(self):
        rec = span_to_log_record(
            _raw_span(**{"gen_ai.request.model": "m"}),
            redact=lambda text: text.replace("OpenRouter", "REDACTED-VENDOR"),
        )
        self.assertIn("REDACTED-VENDOR", rec["body"]["stringValue"])

    def test_model_and_experiment_metadata_survive_redaction(self):
        rec = span_to_log_record(
            _raw_span(
                **{
                    "gen_ai.request.model": "qwen/qwen3.8-27b:free",
                    "trace.metadata.arm": "parent_only",
                    "trace.metadata.case_id": "kql-validation-regression",
                    "gen_ai.prompt": "sk-proj-realsecretvalue12345",
                }
            )
        )
        attrs = {item["key"]: item["value"]["stringValue"] for item in rec["attributes"]}
        self.assertEqual(attrs["openrouter.model"], "qwen/qwen3.8-27b:free")
        self.assertEqual(attrs["trace.metadata.arm"], "parent_only")
        self.assertEqual(attrs["trace.metadata.case_id"], "kql-validation-regression")
        self.assertTrue(attrs["gen_ai.prompt"].startswith("[REDACTED"))
        self.assertIn("model=qwen/qwen3.8-27b:free", rec["body"]["stringValue"])

    def test_non_string_attribute_values_are_not_passed_through_redact(self):
        # Numeric/bool values must not go through redact() (which expects
        # text); confirm no exception and the value is stringified as-is.
        rec = span_to_log_record(_raw_span(**{"gen_ai.usage.total_tokens": 150}))
        tok_attr = next(a for a in rec["attributes"] if a["key"] == "gen_ai.usage.total_tokens")
        self.assertEqual(tok_attr["value"]["stringValue"], "150")

    def test_none_valued_attributes_are_skipped(self):
        span = _raw_span()
        span["attributes"].append(_attr("some.null.field", None))
        # _attr() doesn't produce a None-valued attribute directly since it
        # str()s everything; simulate the real shape OTLP would send for a
        # present-but-empty value instead.
        span["attributes"][-1] = {"key": "some.null.field", "value": {}}
        rec = span_to_log_record(span)
        keys = {a["key"] for a in rec["attributes"]}
        self.assertNotIn("some.null.field", keys)


class SpansToBerserkPayloadTest(unittest.TestCase):
    def test_preserves_resource_attributes_as_is(self):
        payload = spans_to_berserk_payload(_sample_payload())
        resource = payload["resourceLogs"][0]["resource"]
        names = {a["key"]: a["value"] for a in resource["attributes"]}
        self.assertEqual(names["service.name"], {"stringValue": "openrouter"})

    def test_empty_payload_returns_none(self):
        self.assertIsNone(spans_to_berserk_payload({"resourceSpans": []}))
        self.assertIsNone(spans_to_berserk_payload({}))

    def test_one_log_record_per_span(self):
        payload = spans_to_berserk_payload(_sample_payload())
        records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(len(records), 1)


class PostToBerserkTest(unittest.TestCase):
    """post_to_berserk goes through the shared HTTP client (URL policy, no
    redirects, bounded response) and never raises."""

    @staticmethod
    def _send_status(status, seen=None):
        def send(url, headers, data, *, timeout, label, allow_plaintext_remote):
            if seen is not None:
                seen.append(allow_plaintext_remote)
            return status

        return send

    def test_returns_true_on_200(self):
        ok, detail = post_to_berserk("https://x/v1/logs", {"a": 1}, send=self._send_status(200))
        self.assertTrue(ok)

    def test_returns_false_on_non_200(self):
        ok, detail = post_to_berserk("https://x/v1/logs", {"a": 1}, send=self._send_status(500))
        self.assertFalse(ok)

    def test_any_2xx_is_success_and_status_is_in_detail(self):
        # Fix 1: ingest may answer 202/204 for an accepted payload. Treating
        # those as failures made every forward log as failed while data landed.
        for status in (200, 201, 202, 204, 299):
            with self.subTest(status=status):
                ok, detail = post_to_berserk("https://x/v1/logs", {"a": 1}, send=self._send_status(status))
                self.assertTrue(ok)
                self.assertEqual(detail, f"http {status}")

    def test_non_2xx_returned_status_is_failure(self):
        for status in (100, 199, 300, 301, 304):
            with self.subTest(status=status):
                ok, detail = post_to_berserk("https://x/v1/logs", {"a": 1}, send=self._send_status(status))
                self.assertFalse(ok)
                self.assertEqual(detail, f"http {status}")

    def test_never_raises_on_connection_error(self):
        def send(*args, **kwargs):
            raise OSError("connection refused")

        ok, detail = post_to_berserk("https://x/v1/logs", {"a": 1}, send=send)
        self.assertFalse(ok)
        self.assertIn("connection refused", detail)

    def test_plaintext_policy_defaults_to_refused_and_is_passed_through(self):
        seen = []
        post_to_berserk("https://x/v1/logs", {"a": 1}, send=self._send_status(200, seen))
        post_to_berserk("https://x/v1/logs", {"a": 1}, allow_plaintext_remote=True, send=self._send_status(200, seen))
        self.assertEqual(seen, [False, True])

    def test_shared_url_policy_applies_without_network(self):
        # Real shared client, no send override: these fail validation before any I/O.
        for url in (
            "http://100.87.29.100:14318/v1/logs",  # plaintext to non-loopback, no opt-in
            "https://user:pass@x.example/v1/logs",  # embedded credentials
            "file:///etc/passwd",  # not http(s)
        ):
            with self.subTest(url=url):
                ok, detail = post_to_berserk(url, {"a": 1})
                self.assertFalse(ok)
                self.assertTrue(detail)

    def test_plaintext_opt_in_reaches_the_network_layer(self):
        # With the opt-in, validation passes and the failure (if any) is a
        # connection error from an unroutable test address, not the URL policy.
        ok, detail = post_to_berserk("http://192.0.2.1:9/v1/logs", {"a": 1}, timeout=0.2, allow_plaintext_remote=True)
        self.assertFalse(ok)
        self.assertNotIn("plaintext http to a non-loopback host is rejected", detail)


class IsTestConnectionTest(unittest.TestCase):
    def test_true_value_case_insensitive_header_name(self):
        self.assertTrue(is_test_connection({"X-Test-Connection": "true"}))
        self.assertTrue(is_test_connection({"x-test-connection": "true"}))

    def test_absent_header_is_false(self):
        self.assertFalse(is_test_connection({"Content-Type": "application/json"}))

    def test_false_string_value_is_false(self):
        self.assertFalse(is_test_connection({"X-Test-Connection": "false"}))


class VerifySignatureTest(unittest.TestCase):
    def test_no_secret_configured_always_passes(self):
        self.assertTrue(verify_signature({}, None))
        self.assertTrue(verify_signature({"X-Webhook-Signature": "anything"}, None))

    def test_matching_secret_passes(self):
        headers = {"X-Webhook-Signature": "s3cr3t"}
        self.assertTrue(verify_signature(headers, "s3cr3t"))

    def test_mismatched_secret_fails(self):
        headers = {"X-Webhook-Signature": "wrong"}
        self.assertFalse(verify_signature(headers, "s3cr3t"))

    def test_missing_header_fails_when_secret_configured(self):
        self.assertFalse(verify_signature({}, "s3cr3t"))

    def test_header_lookup_is_case_insensitive(self):
        headers = {"x-webhook-signature": "s3cr3t"}
        self.assertTrue(verify_signature(headers, "s3cr3t"))

    def test_static_comparison_is_constant_time(self):
        # Fix 4: the endpoint is public (Tailscale Funnel); a plain == leaks
        # how many leading characters of a guess are right.
        with mock.patch.object(receiver.hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
            self.assertTrue(verify_signature({"X-Webhook-Signature": "s3cr3t"}, "s3cr3t"))
            self.assertFalse(verify_signature({"X-Webhook-Signature": "s3cr3x"}, "s3cr3t"))
        self.assertEqual(cd.call_count, 2)

    def test_non_ascii_header_value_is_rejected_not_a_crash(self):
        self.assertFalse(verify_signature({"X-Webhook-Signature": "sécret"}, "s3cr3t"))


SECRET = "whsec-test-secret"


def _hmac_header(body, secret=SECRET, ts=None):
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"{ts},".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class CheckSignatureStaticModeTest(unittest.TestCase):
    """Default mode: X-Webhook-Signature is a static value Simon configures
    in OpenRouter's Broadcast destination headers (the documented setup)."""

    def test_valid_invalid_absent(self):
        self.assertEqual(check_signature({"X-Webhook-Signature": SECRET}, b"{}", SECRET), "valid")
        self.assertEqual(check_signature({"X-Webhook-Signature": "nope"}, b"{}", SECRET), "invalid")
        self.assertEqual(check_signature({}, b"{}", SECRET), "absent")

    def test_hmac_header_alone_does_not_authenticate_in_static_mode(self):
        headers = {"X-OpenRouter-Signature": _hmac_header(b"{}")}
        self.assertEqual(check_signature(headers, b"{}", SECRET), "absent")

    def test_no_secret_configured_is_valid(self):
        self.assertEqual(check_signature({}, b"{}", None), "valid")


class CheckSignatureHmacModeTest(unittest.TestCase):
    """Opt-in mode (--signature-mode openrouter-hmac). Scheme from OpenRouter's
    video-generation callback docs: X-OpenRouter-Signature: t=<ts>,v1=<hex>,
    HMAC-SHA256 over "<ts>," + raw body, 300 s tolerance. Not verified for
    Broadcast deliveries."""

    MODE = "openrouter-hmac"

    def _check(self, headers, body=b'{"resourceSpans":[]}', now=None):
        return check_signature(headers, body, SECRET, mode=self.MODE, now=now)

    def test_valid_signature_over_exact_raw_body(self):
        body = b'{"resourceSpans": [],  "x": 1}'
        self.assertEqual(self._check({"X-OpenRouter-Signature": _hmac_header(body)}, body), "valid")

    def test_header_lookup_is_case_insensitive(self):
        body = b"{}"
        self.assertEqual(self._check({"x-openrouter-signature": _hmac_header(body)}, body), "valid")

    def test_absent_header(self):
        self.assertEqual(self._check({}), "absent")

    def test_forged_signature(self):
        ts = int(time.time())
        self.assertEqual(self._check({"X-OpenRouter-Signature": f"t={ts},v1={'0' * 64}"}), "invalid")

    def test_valid_signature_for_a_different_body(self):
        header = _hmac_header(b'{"resourceSpans":[]}')
        self.assertEqual(self._check({"X-OpenRouter-Signature": header}, b'{"resourceSpans":[1]}'), "invalid")

    def test_signature_over_reserialized_json_is_rejected(self):
        raw = b'{"b": 1,   "a": 2}'
        reserialized = json.dumps(json.loads(raw)).encode()
        header = _hmac_header(reserialized)
        self.assertEqual(self._check({"X-OpenRouter-Signature": header}, raw), "invalid")

    def test_wrong_secret(self):
        body = b"{}"
        header = _hmac_header(body, secret="other-secret")
        self.assertEqual(self._check({"X-OpenRouter-Signature": header}, body), "invalid")

    def test_stale_and_future_timestamps_rejected(self):
        body = b"{}"
        now = 1_800_000_000
        for ts in (now - 301, now + 301):
            with self.subTest(ts=ts):
                header = _hmac_header(body, ts=ts)
                self.assertEqual(self._check({"X-OpenRouter-Signature": header}, body, now=now), "invalid")
        for ts in (now - 300, now + 300):
            with self.subTest(ts=ts):
                header = _hmac_header(body, ts=ts)
                self.assertEqual(self._check({"X-OpenRouter-Signature": header}, body, now=now), "valid")

    def test_malformed_headers_are_invalid_not_a_crash(self):
        body = b"{}"
        good = _hmac_header(body)
        digest = good.split("v1=")[1]
        for value in (
            "",
            "garbage",
            f"v1={digest}",
            f"t=,v1={digest}",
            f"t=abc,v1={digest}",
            f"t=1.5,v1={digest}",
            f"t={int(time.time())}",
            f"t={int(time.time())},v1=",
            f"t={int(time.time())},v1=zz{digest[2:]}",
            f"t={int(time.time())},v1=é{digest[1:]}",
            "t=" + "9" * 5000 + f",v1={digest}",  # beyond int() digit limit
            "t=" + "9" * 30 + f",v1={digest}",
        ):
            with self.subTest(value=value):
                self.assertEqual(self._check({"X-OpenRouter-Signature": value}, body), "invalid")

    def test_static_header_alone_does_not_authenticate_in_hmac_mode(self):
        self.assertEqual(self._check({"X-Webhook-Signature": SECRET}), "absent")

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            check_signature({}, b"{}", SECRET, mode="md5-please")


class ForwardStatsTest(unittest.TestCase):
    """Fix 3: forward failures used to be one stderr line per delivery and
    nothing else. ForwardStats logs state changes and a periodic summary."""

    def setUp(self):
        self.now = 1_000.0
        self.stats = ForwardStats(summary_interval=900, clock=lambda: self.now)

    def test_success_from_the_start_logs_nothing(self):
        self.assertEqual(self.stats.record(True, "http 200"), [])
        self.assertEqual(self.stats.record(True, "http 202"), [])

    def test_first_failure_logs_a_failing_transition(self):
        lines = self.stats.record(False, "timed out")
        self.assertEqual(len(lines), 1)
        self.assertIn("FAILING", lines[0])
        self.assertIn("timed out", lines[0])

    def test_repeated_failures_inside_the_interval_stay_quiet(self):
        self.stats.record(False, "timed out")
        self.now += 60
        self.assertEqual(self.stats.record(False, "timed out"), [])

    def test_periodic_summary_while_still_failing(self):
        self.stats.record(True, "http 200")
        self.stats.record(False, "timed out")
        for _ in range(3):
            self.now += 300
            lines = self.stats.record(False, "http 429")
        self.assertEqual(len(lines), 1)
        self.assertIn("still FAILING", lines[0])
        self.assertIn("4 consecutive", lines[0])
        self.assertIn("http 429", lines[0])

    def test_recovery_logs_once_with_the_failure_count(self):
        self.stats.record(False, "timed out")
        self.stats.record(False, "timed out")
        lines = self.stats.record(True, "http 200")
        self.assertEqual(len(lines), 1)
        self.assertIn("RECOVERED", lines[0])
        self.assertIn("2 consecutive", lines[0])
        self.assertEqual(self.stats.record(True, "http 200"), [])

    def test_snapshot_counts(self):
        self.stats.record(True, "http 200")
        self.stats.record(False, "timed out")
        snap = self.stats.snapshot()
        self.assertEqual(snap["ok"], 1)
        self.assertEqual(snap["failed"], 1)
        self.assertEqual(snap["consecutive_failures"], 1)
        self.assertEqual(snap["last_error"], "timed out")

    def test_thread_safe_counts(self):
        stats = ForwardStats()

        def work():
            for _ in range(500):
                stats.record(False, "x")
                stats.record(True, "http 200")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = stats.snapshot()
        self.assertEqual(snap["ok"], 4000)
        self.assertEqual(snap["failed"], 4000)


class MainSignatureModeTest(unittest.TestCase):
    def test_unknown_signature_mode_refuses_to_start(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            receiver.main(["--signature-mode", "md5-please", "--secret-file", os.devnull])
        self.assertNotEqual(cm.exception.code, 0)

    def test_default_mode_is_static(self):
        with (
            mock.patch.object(receiver, "run_server") as run,
            mock.patch.dict(os.environ, {"OPENROUTER_WEBHOOK_SECRET": SECRET}),
        ):
            receiver.main([])
        self.assertEqual(run.call_args.kwargs["signature_mode"], "static")

    def test_hmac_mode_is_passed_through(self):
        with (
            mock.patch.object(receiver, "run_server") as run,
            mock.patch.dict(os.environ, {"OPENROUTER_WEBHOOK_SECRET": SECRET}),
        ):
            receiver.main(["--signature-mode", "openrouter-hmac"])
        self.assertEqual(run.call_args.kwargs["signature_mode"], "openrouter-hmac")


class HandlerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out_path = os.path.join(self.tmpdir, "spans.jsonl")
        self.raw_path = os.path.join(self.tmpdir, "raw.jsonl")
        handler = _make_handler(self.out_path, self.raw_path, "correct-secret", threading.Lock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, headers, body=b""):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/", body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return status

    def test_test_connection_with_no_signature_header_succeeds(self):
        body = json.dumps(_sample_payload()).encode()
        self.assertEqual(self._post({"X-Test-Connection": "true"}, body=body), 200)
        # The unsigned handshake must never record its body.
        self.assertFalse(os.path.exists(self.out_path))
        self.assertFalse(os.path.exists(self.raw_path))

    def test_test_connection_with_correct_signature_succeeds(self):
        self.assertEqual(self._post({"X-Test-Connection": "true", "X-Webhook-Signature": "correct-secret"}), 200)

    def test_test_connection_with_wrong_signature_is_rejected(self):
        self.assertEqual(self._post({"X-Test-Connection": "true", "X-Webhook-Signature": "wrong"}), 401)

    def test_real_payload_with_correct_signature_succeeds_and_is_recorded(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post({"X-Webhook-Signature": "correct-secret", "Content-Type": "application/json"}, body=payload)
        self.assertEqual(status, 200)
        with open(self.out_path) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "openai/gpt-4")

    def test_real_payload_with_wrong_signature_is_rejected_and_not_recorded(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post({"X-Webhook-Signature": "wrong", "Content-Type": "application/json"}, body=payload)
        self.assertEqual(status, 401)
        self.assertFalse(os.path.exists(self.out_path))

    def test_real_payload_with_no_signature_is_rejected(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post({"Content-Type": "application/json"}, body=payload)
        self.assertEqual(status, 401)


def _payload_with_spans(ids, with_ids=True):
    spans = []
    for i in ids:
        span = {"name": "chat.completions", "attributes": [_attr("gen_ai.request.model", "openai/gpt-4.1-mini")]}
        if with_ids:
            span["traceId"] = f"trace-{i}"
            span["spanId"] = f"span-{i}"
        spans.append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "openrouter")]},
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def _count_log_records(berserk_payload):
    return sum(
        len(sl.get("logRecords", []))
        for rl in berserk_payload.get("resourceLogs", [])
        for sl in rl.get("scopeLogs", [])
    )


class SpanDeduperTest(unittest.TestCase):
    def test_first_claim_wins_and_repeat_is_refused(self):
        d = SpanDeduper(max_size=10)
        self.assertTrue(d.claim(("t", "s")))
        self.assertFalse(d.claim(("t", "s")))

    def test_release_allows_a_retry_to_claim_again(self):
        d = SpanDeduper(max_size=10)
        claim = d.claim(("t", "s"))
        d.release([claim])
        self.assertTrue(d.claim(("t", "s")))

    def test_a_stale_release_cannot_remove_a_newer_claim(self):
        # The first attempt's claim is evicted, a retry re-claims the span, then
        # the first attempt fails late and releases. Only the owner may release.
        d = SpanDeduper(max_size=1)
        stale = d.claim(("t", "a"))
        d.claim(("t", "b"))  # evicts "a"
        fresh = d.claim(("t", "a"))
        self.assertTrue(fresh)
        d.release([stale])
        self.assertFalse(d.claim(("t", "a")), "the retry's claim must survive the stale release")
        d.release([fresh])
        self.assertTrue(d.claim(("t", "a")))

    def test_memory_is_bounded_oldest_is_evicted(self):
        d = SpanDeduper(max_size=2)
        for key in (("t", "a"), ("t", "b"), ("t", "c")):
            self.assertTrue(d.claim(key))
        self.assertEqual(len(d), 2)
        self.assertTrue(d.claim(("t", "a")))  # "a" was evicted, so it counts as new again


class ClaimNewSpansTest(unittest.TestCase):
    def test_keeps_only_unseen_spans_and_their_envelope(self):
        d = SpanDeduper()
        first, claimed1, new1, dup1 = claim_new_spans(_payload_with_spans([1, 2]), d)
        self.assertEqual((new1, dup1, len(claimed1)), (2, 0, 2))
        second, claimed2, new2, dup2 = claim_new_spans(_payload_with_spans([2, 3]), d)
        self.assertEqual((new2, dup2), (1, 1))
        spans = second["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([sp["spanId"] for sp in spans], ["span-3"])
        self.assertIn("resource", second["resourceSpans"][0])

    def test_all_duplicates_gives_an_empty_payload(self):
        d = SpanDeduper()
        claim_new_spans(_payload_with_spans([1]), d)
        filtered, claimed, new, dup = claim_new_spans(_payload_with_spans([1]), d)
        self.assertEqual((new, dup, claimed), (0, 1, []))
        self.assertEqual(extract_spans(filtered), [])

    def test_spans_without_ids_are_never_deduplicated(self):
        d = SpanDeduper()
        _f, _c, new1, dup1 = claim_new_spans(_payload_with_spans([1], with_ids=False), d)
        _f, _c, new2, dup2 = claim_new_spans(_payload_with_spans([1], with_ids=False), d)
        self.assertEqual((new1, dup1, new2, dup2), (1, 0, 1, 0))


class HandlerForwardingIntegrationTest(unittest.TestCase):
    """berserk_endpoint set. The webhook reply goes out as soon as the local
    capture is written; redaction and the forward to Berserk happen on a
    background worker, so a slow ingest can never make OpenRouter time out
    and retry. Forwarding outcomes never affect the reply or the capture."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out_path = os.path.join(self.tmpdir, "spans.jsonl")
        self.raw_path = os.path.join(self.tmpdir, "raw.jsonl")
        self.forward_calls = []
        self.release = threading.Event()
        self.release.set()  # workers are not held back unless a test clears it
        self.handler = None
        self.server = None

    def _start(self, post_fn, **kwargs):
        kwargs.setdefault("forward_workers", 1)  # deterministic order
        self.handler = _make_handler(
            kwargs.pop("out_path", self.out_path),
            kwargs.pop("raw_path", self.raw_path),
            "correct-secret",
            threading.Lock(),
            berserk_endpoint="http://fake-berserk/v1/logs",
            post_fn=post_fn,
            **kwargs,
        )
        self._serve(self.handler)

    def _serve(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.release.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def _post(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/",
            body=body,
            headers={"X-Webhook-Signature": "correct-secret", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        status, raw = resp.status, resp.read()
        conn.close()
        return status, json.loads(raw)

    def _drain(self, timeout=5):
        deadline = time.time() + timeout
        while self.handler.forward_queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.handler.forward_queue.unfinished_tasks, 0, "forward queue did not drain")

    def _lines(self, path):
        with open(path) as f:
            return f.readlines()

    def test_forwarding_success_is_queued_and_local_write_still_happens(self):
        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append((endpoint, payload))
            return True, "http 200"

        self._start(fake_post)
        status, resp = self._post(_sample_payload())
        self.assertEqual(status, 200)
        self.assertEqual(resp["berserk_forward"], "queued")
        self._drain()
        self.assertEqual(len(self.forward_calls), 1)
        self.assertEqual(self.forward_calls[0][0], "http://fake-berserk/v1/logs")
        self.assertEqual(len(self._lines(self.out_path)), 1)

    def test_forwarding_failure_does_not_break_response_or_local_write(self):
        def failing_post(endpoint, payload, timeout=10):
            return False, "connection refused"

        self._start(failing_post)
        status, resp = self._post(_sample_payload())
        self.assertEqual((status, resp["berserk_forward"]), (200, "queued"))
        self._drain()
        self.assertEqual(self.handler.stats.snapshot()["failed"], 1)
        self.assertEqual(len(self._lines(self.out_path)), 1)

    def test_forwarding_exception_does_not_break_response_or_local_write(self):
        def raising_post(endpoint, payload, timeout=10):
            raise RuntimeError("unexpected")

        self._start(raising_post)
        status, resp = self._post(_sample_payload())
        self.assertEqual((status, resp["berserk_forward"]), (200, "queued"))
        self._drain()
        self.assertEqual(self.handler.stats.snapshot()["failed"], 1)
        self.assertEqual(len(self._lines(self.out_path)), 1)

    def test_no_berserk_endpoint_means_no_forwarding_key_in_response(self):
        self._serve(_make_handler(self.out_path, self.raw_path, "correct-secret", threading.Lock()))
        status, resp = self._post(_sample_payload())
        self.assertEqual(status, 200)
        self.assertNotIn("berserk_forward", resp)
        self.assertNotIn("berserk_forwarded", resp)

    def test_forward_state_changes_are_logged_and_detail_not_leaked_to_sender(self):
        results = iter([(False, "http 503 from 10.0.0.9"), (False, "timed out"), (True, "http 202")])

        def flaky_post(endpoint, payload, timeout=10):
            return next(results)

        self._start(flaky_post)
        err = io.StringIO()
        with redirect_stderr(err):
            bodies = [self._post(_sample_payload(n))[1] for n in (1, 2, 3)]
            self._drain()
        log = err.getvalue()
        self.assertIn("berserk forward FAILING", log)
        self.assertIn("berserk forward RECOVERED after 2 consecutive", log)
        # Bounded logging: two failures and a recovery give exactly two
        # forward lines, not one per failed delivery.
        forward_lines = [line for line in log.splitlines() if line.startswith("berserk forward")]
        self.assertEqual(len(forward_lines), 2, forward_lines)
        self.assertEqual([b["berserk_forward"] for b in bodies], ["queued"] * 3)
        # The webhook response goes to OpenRouter: no internal error detail.
        self.assertNotIn("10.0.0.9", json.dumps(bodies))

    def test_sustained_failure_logs_once_per_interval_not_per_delivery(self):
        def failing_post(endpoint, payload, timeout=10):
            return False, "connection refused"

        self._start(failing_post)
        err = io.StringIO()
        with redirect_stderr(err):
            for n in range(1, 6):
                self._post(_sample_payload(n))
            self._drain()
        forward_lines = [line for line in err.getvalue().splitlines() if line.startswith("berserk forward")]
        self.assertEqual(len(forward_lines), 1, forward_lines)
        self.assertIn("FAILING: connection refused", forward_lines[0])

    def test_raised_forward_is_counted_and_logged_once(self):
        def raising_post(endpoint, payload, timeout=10):
            raise RuntimeError("unexpected")

        self._start(raising_post)
        err = io.StringIO()
        with redirect_stderr(err):
            for n in range(1, 4):
                self._post(_sample_payload(n))
            self._drain()
        forward_lines = [line for line in err.getvalue().splitlines() if line.startswith("berserk forward")]
        self.assertEqual(len(forward_lines), 1, forward_lines)
        self.assertIn("unexpected", forward_lines[0])

    def test_handshake_is_never_forwarded(self):
        def counting_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(counting_post)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/", body=json.dumps(_sample_payload()).encode(), headers={"X-Test-Connection": "true"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self._drain()
        self.assertEqual(self.forward_calls, [])
        self.assertFalse(os.path.exists(self.out_path))

    # ---- fast reply -------------------------------------------------------

    def test_reply_is_fast_while_the_forward_is_still_running(self):
        started = threading.Event()
        self.release.clear()

        def slow_post(endpoint, payload, timeout=10):
            started.set()
            self.release.wait(10)
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(slow_post)
        # The forward stays blocked until self.release is set, so a reply that
        # came back at all did not wait for it (a waiting reply would hit the
        # client's 5 s timeout and fail this call). No wall-clock threshold.
        status, resp = self._post(_sample_payload())
        self.assertEqual((status, resp["berserk_forward"]), (200, "queued"))
        self.assertTrue(started.wait(5))
        self.assertEqual(self.forward_calls, [], "the forward is still in flight")
        self.release.set()
        self._drain()
        self.assertEqual(len(self.forward_calls), 1)

    def test_queue_full_drops_the_forward_but_still_replies_and_records(self):
        started = threading.Event()
        self.release.clear()

        def blocked_post(endpoint, payload, timeout=10):
            started.set()
            self.release.wait(10)
            return True, "http 200"

        self._start(blocked_post, forward_queue_size=1)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self._post(_sample_payload(1))[1]["berserk_forward"], "queued")
            self.assertTrue(started.wait(5))  # the worker holds item 1
            self.assertEqual(self._post(_sample_payload(2))[1]["berserk_forward"], "queued")  # fills the queue
            status, resp = self._post(_sample_payload(3))  # no room left
        self.assertEqual((status, resp["berserk_forward"]), (200, "dropped"))
        self.assertEqual(len(self._lines(self.out_path)), 3, "dropped forwards are still captured locally")
        self.assertIn("forward queue full", err.getvalue())
        self.release.set()
        self._drain()

    # ---- duplicates -------------------------------------------------------

    def test_duplicate_delivery_is_recorded_once_and_forwarded_once(self):
        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(fake_post)
        s1, r1 = self._post(_sample_payload())
        s2, r2 = self._post(_sample_payload())  # OpenRouter retry of the same trace
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(r1["spans_received"], 1)
        self.assertEqual((r2["spans_received"], r2["duplicates_skipped"]), (0, 1))
        self.assertEqual(r2["berserk_forward"], "skipped")
        self._drain()
        self.assertEqual(len(self.forward_calls), 1)
        self.assertEqual(len(self._lines(self.out_path)), 1)
        self.assertEqual(len(self._lines(self.raw_path)), 2, "the raw capture keeps every delivery")

    def test_partial_overlap_forwards_only_the_new_spans(self):
        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(fake_post)
        self._post(_payload_with_spans([1, 2]))
        self._post(_payload_with_spans([2, 3]))
        self._drain()
        self.assertEqual([_count_log_records(p) for p in self.forward_calls], [2, 1])
        self.assertEqual(len(self._lines(self.out_path)), 3)

    def test_spans_without_ids_are_never_treated_as_duplicates(self):
        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(fake_post)
        self._post(_payload_with_spans([1], with_ids=False))
        self._post(_payload_with_spans([1], with_ids=False))
        self._drain()
        self.assertEqual(len(self.forward_calls), 2)
        self.assertEqual(len(self._lines(self.out_path)), 2)

    # ---- review fixes -----------------------------------------------------

    def test_a_failure_after_the_claim_releases_it_so_the_retry_is_processed(self):
        real_extract = receiver.extract_spans
        calls = {"n": 0}

        def flaky_extract(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_extract(payload)

        self._start(lambda *a, **k: (True, "http 200"))
        with mock.patch.object(receiver, "extract_spans", flaky_extract), redirect_stderr(io.StringIO()):
            status, resp = self._post(_sample_payload())
            self.assertEqual((status, resp), (200, {"ok": True, "parsed": False}))
            status, resp = self._post(_sample_payload())  # the retry
        self.assertEqual(status, 200)
        self.assertEqual(resp["spans_received"], 1, "a failed attempt must not count as already seen")
        self.assertNotIn("duplicates_skipped", resp)

    def test_a_partial_span_write_is_rolled_back_so_the_retry_does_not_duplicate_rows(self):
        class HalfWriteFile:
            """Writes part of the data, then fails like a full disk."""

            def __init__(self, real):
                self._real = real

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

            def write(self, data):
                self._real.write(data[: len(data) // 2])
                self._real.flush()
                raise OSError(28, "No space left on device")

        real_open = open
        state = {"fail": True}

        def flaky_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            if state["fail"] and str(path) == self.out_path and "a" in mode:
                return HalfWriteFile(f)
            return f

        self._start(lambda *a, **k: (True, "http 200"))
        with mock.patch.object(receiver, "open", flaky_open, create=True), redirect_stderr(io.StringIO()):
            status, _ = self._post(_payload_with_spans([1, 2]))
        self.assertEqual(status, 500)
        self.assertEqual(os.path.getsize(self.out_path), 0, "the half-written rows must be rolled back")
        state["fail"] = False
        with mock.patch.object(receiver, "open", flaky_open, create=True):
            status, resp = self._post(_payload_with_spans([1, 2]))
        self.assertEqual((status, resp["spans_received"]), (200, 2))
        self.assertEqual(len(self._lines(self.out_path)), 2, "exactly one row per span")

    def test_a_forward_worker_survives_an_exception_in_its_own_bookkeeping(self):
        class BrokenStats(ForwardStats):
            def __init__(self):
                super().__init__()
                self.explode = 2  # the first call and the error-path retry both raise

            def record(self, ok, detail):
                if self.explode:
                    self.explode -= 1
                    raise RuntimeError("stats broke")
                return super().record(ok, detail)

        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(fake_post, stats=BrokenStats())
        with redirect_stderr(io.StringIO()):
            self._post(_sample_payload(1))
            self._drain()
            self._post(_sample_payload(2))  # the worker must still be alive
            self._drain()
        self.assertEqual(len(self.forward_calls), 2)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        self._start(lambda *a, **k: (True, "http 200"), max_body_bytes=100)
        status, resp = self._post(_sample_payload())
        self.assertEqual((status, resp["error"]), (413, "payload too large"))
        self.assertFalse(os.path.exists(self.out_path))
        self.assertFalse(os.path.exists(self.raw_path))

    # ---- storage failures -------------------------------------------------

    def test_storage_failure_answers_500_and_the_retry_is_processed(self):
        missing_dir = os.path.join(self.tmpdir, "missing")
        bad_out = os.path.join(missing_dir, "spans.jsonl")

        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append(payload)
            return True, "http 200"

        self._start(fake_post, out_path=bad_out)
        err = io.StringIO()
        with redirect_stderr(err):
            status, resp = self._post(_sample_payload())
        self.assertEqual(status, 500)
        self.assertEqual(resp, {"ok": False, "error": "storage failure"})
        self.assertIn("storage failure", err.getvalue())
        os.makedirs(missing_dir)  # the disk problem is fixed; OpenRouter retries
        status, resp = self._post(_sample_payload())
        self.assertEqual(status, 200)
        self.assertEqual(resp["spans_received"], 1, "a failed attempt must not count as already seen")
        self._drain()
        self.assertEqual(len(self.forward_calls), 1)

    def test_raw_capture_failure_also_answers_500(self):
        self._start(lambda *a, **k: (True, "http 200"), raw_path=os.path.join(self.tmpdir, "nodir", "raw.jsonl"))
        with redirect_stderr(io.StringIO()):
            status, resp = self._post(_sample_payload())
        self.assertEqual((status, resp["error"]), (500, "storage failure"))

    def test_non_object_payload_is_tolerated(self):
        self._start(lambda *a, **k: (True, "http 200"))
        status, resp = self._post(b"[1, 2, 3]")
        self.assertEqual((status, resp), (200, {"ok": True, "parsed": False}))


class HandlerHmacModeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out_path = os.path.join(self.tmpdir, "spans.jsonl")
        self.raw_path = os.path.join(self.tmpdir, "raw.jsonl")
        handler = _make_handler(
            self.out_path, self.raw_path, SECRET, threading.Lock(), signature_mode="openrouter-hmac"
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, headers, body=b""):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/", body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return status

    def test_valid_hmac_payload_is_recorded(self):
        body = json.dumps(_sample_payload()).encode()
        status = self._post({"X-OpenRouter-Signature": _hmac_header(body), "Content-Type": "application/json"}, body)
        self.assertEqual(status, 200)
        with open(self.out_path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_tampered_body_is_rejected_and_not_recorded(self):
        body = json.dumps(_sample_payload()).encode()
        header = _hmac_header(body)
        status = self._post({"X-OpenRouter-Signature": header}, body.replace(b"gpt-4", b"gpt-5"))
        self.assertEqual(status, 401)
        self.assertFalse(os.path.exists(self.raw_path))

    def test_static_secret_header_is_not_accepted_in_hmac_mode(self):
        body = json.dumps(_sample_payload()).encode()
        self.assertEqual(self._post({"X-Webhook-Signature": SECRET}, body), 401)

    def test_handshake_without_signature_passes_but_invalid_signature_fails(self):
        body = json.dumps(_sample_payload()).encode()
        self.assertEqual(self._post({"X-Test-Connection": "true"}, body), 200)
        self.assertEqual(self._post({"X-Test-Connection": "true", "X-OpenRouter-Signature": "t=1,v1=00"}, body), 401)
        self.assertFalse(os.path.exists(self.out_path))
        self.assertFalse(os.path.exists(self.raw_path))

    def test_oversized_timestamp_is_401_not_a_dropped_connection(self):
        header = "t=" + "9" * 5000 + ",v1=" + "0" * 64
        self.assertEqual(self._post({"X-OpenRouter-Signature": header}, b"{}"), 401)


if __name__ == "__main__":
    unittest.main()
