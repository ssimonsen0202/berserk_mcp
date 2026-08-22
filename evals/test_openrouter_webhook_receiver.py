import unittest

from openrouter_webhook_receiver import (
    extract_spans,
    is_test_connection,
    verify_signature,
)


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
                                    _attr("gen_ai.usage.prompt_tokens", 100),
                                    _attr("gen_ai.usage.completion_tokens", 50),
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


if __name__ == "__main__":
    unittest.main()
