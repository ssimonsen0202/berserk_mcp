import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection

from openrouter_webhook_receiver import (
    _make_handler,
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


def _sample_payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_attr("service.name", "openrouter")]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "trace-1",
                                "spanId": "span-1",
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
        self.assertEqual(
            rows[0]["attributes"]["trace.metadata.case_id"], "router-07"
        )

    def test_numeric_token_attributes_are_ints_not_strings(self):
        rows = extract_spans(_sample_payload())
        self.assertIsInstance(rows[0]["prompt_tokens"], int)
        self.assertIsInstance(rows[0]["completion_tokens"], int)

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
    class _FakeResponse:
        def __init__(self, status):
            self.status = status
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def test_returns_true_on_200(self):
        ok, detail = post_to_berserk("http://x/v1/logs", {"a": 1}, opener=lambda req, timeout: self._FakeResponse(200))
        self.assertTrue(ok)

    def test_returns_false_on_non_200(self):
        ok, detail = post_to_berserk("http://x/v1/logs", {"a": 1}, opener=lambda req, timeout: self._FakeResponse(500))
        self.assertFalse(ok)

    def test_never_raises_on_connection_error(self):
        def opener(req, timeout):
            raise OSError("connection refused")
        ok, detail = post_to_berserk("http://x/v1/logs", {"a": 1}, opener=opener)
        self.assertFalse(ok)
        self.assertIn("connection refused", detail)


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
        self.assertEqual(self._post({"X-Test-Connection": "true"}), 200)

    def test_test_connection_with_correct_signature_succeeds(self):
        self.assertEqual(
            self._post({"X-Test-Connection": "true", "X-Webhook-Signature": "correct-secret"}), 200
        )

    def test_test_connection_with_wrong_signature_is_rejected(self):
        self.assertEqual(
            self._post({"X-Test-Connection": "true", "X-Webhook-Signature": "wrong"}), 401
        )

    def test_real_payload_with_correct_signature_succeeds_and_is_recorded(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post(
            {"X-Webhook-Signature": "correct-secret", "Content-Type": "application/json"}, body=payload
        )
        self.assertEqual(status, 200)
        with open(self.out_path) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "openai/gpt-4")

    def test_real_payload_with_wrong_signature_is_rejected_and_not_recorded(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post(
            {"X-Webhook-Signature": "wrong", "Content-Type": "application/json"}, body=payload
        )
        self.assertEqual(status, 401)
        self.assertFalse(os.path.exists(self.out_path))

    def test_real_payload_with_no_signature_is_rejected(self):
        payload = json.dumps(_sample_payload()).encode()
        status = self._post({"Content-Type": "application/json"}, body=payload)
        self.assertEqual(status, 401)


class HandlerForwardingIntegrationTest(unittest.TestCase):
    """berserk_endpoint set -- forwarding is attempted, but its outcome
    never affects the webhook response or the local JSONL write, both of
    which are independent by design."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out_path = os.path.join(self.tmpdir, "spans.jsonl")
        self.raw_path = os.path.join(self.tmpdir, "raw.jsonl")
        self.forward_calls = []

    def _start(self, post_fn):
        handler = _make_handler(
            self.out_path, self.raw_path, "correct-secret", threading.Lock(),
            berserk_endpoint="http://fake-berserk/v1/logs", post_fn=post_fn,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, body):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST", "/", body=body,
            headers={"X-Webhook-Signature": "correct-secret", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        status, raw = resp.status, resp.read()
        conn.close()
        return status, json.loads(raw)

    def test_forwarding_success_is_reported_and_local_write_still_happens(self):
        def fake_post(endpoint, payload, timeout=10):
            self.forward_calls.append((endpoint, payload))
            return True, "http 200"
        self._start(fake_post)
        status, resp = self._post(json.dumps(_sample_payload()).encode())
        self.assertEqual(status, 200)
        self.assertTrue(resp["berserk_forwarded"])
        self.assertEqual(len(self.forward_calls), 1)
        self.assertEqual(self.forward_calls[0][0], "http://fake-berserk/v1/logs")
        with open(self.out_path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_forwarding_failure_does_not_break_response_or_local_write(self):
        def failing_post(endpoint, payload, timeout=10):
            return False, "connection refused"
        self._start(failing_post)
        status, resp = self._post(json.dumps(_sample_payload()).encode())
        self.assertEqual(status, 200)
        self.assertFalse(resp["berserk_forwarded"])
        with open(self.out_path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_forwarding_exception_does_not_break_response_or_local_write(self):
        def raising_post(endpoint, payload, timeout=10):
            raise RuntimeError("unexpected")
        self._start(raising_post)
        status, resp = self._post(json.dumps(_sample_payload()).encode())
        self.assertEqual(status, 200)
        self.assertFalse(resp["berserk_forwarded"])
        with open(self.out_path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_no_berserk_endpoint_means_no_forwarding_key_in_response(self):
        handler = _make_handler(self.out_path, self.raw_path, "correct-secret", threading.Lock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, resp = self._post(json.dumps(_sample_payload()).encode())
        self.assertEqual(status, 200)
        self.assertNotIn("berserk_forwarded", resp)


if __name__ == "__main__":
    unittest.main()
