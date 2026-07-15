import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402
import secret_scan as ss  # noqa: E402


AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz123456"
SK_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def jsonl(rows):
    return "\n".join(json.dumps(row) for row in rows)


class SecretRedactionTest(unittest.TestCase):
    def test_required_secret_types_are_redacted_without_values_in_findings(self):
        text = f"aws {AWS_KEY} jwt {JWT} password=hunter2 {BEARER} {SK_KEY}"
        clean, findings = ss.redact(text, pii_types=())
        types = {item["type"] for item in findings}
        self.assertTrue({"aws_key", "jwt", "password", "bearer", "api_key"} <= types)
        self.assertIn("[REDACTED:aws_key]", clean)
        self.assertNotIn(AWS_KEY, clean)
        self.assertNotIn(AWS_KEY, json.dumps(findings))
        self.assertNotIn("hunter2", json.dumps(findings))

    def test_private_key_and_provider_tokens_are_specific_not_generic(self):
        private_key = (
            "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----"
        )
        github = "ghp_" + ("a" * 36)
        slack = "xoxb-1234567890-abcdefghij"
        clean, findings = ss.redact(f"{private_key} {github} {slack}", pii_types=())
        self.assertEqual(
            {item["type"] for item in findings},
            {"private_key", "github_token", "slack_token"},
        )
        self.assertNotIn("secret-material", clean)

    def test_aws_secret_context_is_typed_and_offset_is_original(self):
        value = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/AB"
        text = f"prefix aws_secret_access_key={value} suffix"
        clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(findings, [{
            "type": "aws_secret", "count": 1, "first_offset": len("prefix "),
        }])
        self.assertNotIn(value, clean)
        self.assertNotIn(value, repr(findings))

    def test_credit_card_requires_luhn(self):
        clean, findings = ss.redact(
            "valid 4111111111111111 invalid 1234567890123456",
            pii_types={"credit_card"},
        )
        self.assertIn("[REDACTED:credit_card]", clean)
        self.assertIn("1234567890123456", clean)
        self.assertEqual(sum(item["count"] for item in findings), 1)

    def test_pii_categories_are_individually_toggleable(self):
        text = "mail user@example.com hosts 192.168.1.10 and 2001:db8::1"
        clean, findings = ss.redact(text, pii_types={"email"})
        self.assertEqual({item["type"] for item in findings}, {"email"})
        self.assertIn("192.168.1.10", clean)
        self.assertIn("2001:db8::1", clean)

        clean, findings = ss.redact(text, pii_types={"ipv4", "ipv6"})
        self.assertEqual({item["type"] for item in findings}, {"ipv4", "ipv6"})
        self.assertIn("user@example.com", clean)

    def test_entropy_detection_is_opt_in(self):
        token = "A1b2C3d4E5f6G7h8I9j0KLMNOPqrstuv"
        clean, findings = ss.redact(token, include_entropy=False, pii_types=())
        self.assertEqual(clean, token)
        self.assertEqual(findings, [])
        clean, findings = ss.redact(token, include_entropy=True, pii_types=())
        self.assertEqual(clean, "[REDACTED:high_entropy]")
        self.assertEqual(findings[0]["type"], "high_entropy")

    def test_findings_are_aggregated_and_capped(self):
        text = " ".join([AWS_KEY] * (ss.MAX_MATCHES + 20))
        _clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(findings, [{"type": "aws_key", "count": ss.MAX_MATCHES, "first_offset": 0}])

    def test_redactor_does_not_write_secret_to_stderr(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _clean, findings = ss.redact(f"password=hunter2 {AWS_KEY}", pii_types=())
        self.assertEqual(buf.getvalue(), "")
        self.assertNotIn("hunter2", repr(findings))
        self.assertNotIn(AWS_KEY, repr(findings))


class AuditRowParsingTest(unittest.TestCase):
    def test_parse_audit_rows_accepts_json_array_and_wrapper(self):
        recs = [{"service": "api", "ts": "t1", "body": "x"},
                {"service": "web", "ts": "t2", "body": "y"}]
        self.assertEqual(len(ss._parse_audit_rows(json.dumps(recs))), 2)          # bare array
        self.assertEqual(len(ss._parse_audit_rows(json.dumps({"data": recs}))), 2)  # wrapped
        self.assertEqual(len(ss._parse_audit_rows("\n".join(json.dumps(r) for r in recs))), 2)  # jsonl
        self.assertEqual(ss._parse_audit_rows("(no rows)"), [])


class OutputFilterTest(unittest.TestCase):
    def setUp(self):
        self.orig_run = bm.run_bzrk
        self.orig_mode = bm.REDACT_MODE
        self.orig_entropy = bm.REDACT_ENTROPY
        self.orig_pii = bm.REDACT_PII_TYPES
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (
            f"service body password=hunter2 {AWS_KEY}", False,
        )
        bm.REDACT_ENTROPY = False
        bm.REDACT_PII_TYPES = frozenset()

    def tearDown(self):
        bm.run_bzrk = self.orig_run
        bm.REDACT_MODE = self.orig_mode
        bm.REDACT_ENTROPY = self.orig_entropy
        bm.REDACT_PII_TYPES = self.orig_pii

    def _call(self):
        return bm.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_services", "arguments": {}},
        })["result"]["content"][0]["text"]

    def test_flag_mode_warns_and_preserves_output(self):
        bm.REDACT_MODE = "flag"
        text = self._call()
        self.assertTrue(text.startswith("⚠ 2 potential secrets detected"))
        self.assertIn("password=hunter2", text)
        self.assertIn(AWS_KEY, text)

    def test_redact_mode_replaces_values(self):
        bm.REDACT_MODE = "redact"
        text = self._call()
        self.assertIn("[REDACTED:password]", text)
        self.assertIn("[REDACTED:aws_key]", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn(AWS_KEY, text)

    def test_off_mode_returns_output_unchanged(self):
        bm.REDACT_MODE = "off"
        text = self._call()
        self.assertEqual(text, f"service body password=hunter2 {AWS_KEY}")


class SecretAuditMcpTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.orig_run = bm.run_bzrk
        self.orig_mode = bm.REDACT_MODE
        bm.REDACT_MODE = "off"

        def fake_run(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return jsonl([
                {"service": "api", "ts": "2026-07-12T10:00:00Z", "body": f"key {AWS_KEY}"},
                {"service": "api", "ts": "2026-07-12T09:00:00Z", "body": "password=hunter2"},
                {"service": "worker", "ts": "2026-07-12T11:00:00Z", "body": SK_KEY},
            ]), False

        bm.run_bzrk = fake_run

    def tearDown(self):
        bm.run_bzrk = self.orig_run
        bm.REDACT_MODE = self.orig_mode

    def test_scan_secrets_returns_aggregate_report_without_values(self):
        text, err = bm.handle_call("scan_secrets", {})
        self.assertFalse(err)
        self.assertIn("3 potential secrets detected", text)
        self.assertIn("api: aws_key x1, password x1", text)
        self.assertIn("first_seen=2026-07-12T09:00:00Z", text)
        self.assertIn("worker: api_key x1", text)
        self.assertNotIn(AWS_KEY, text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn(SK_KEY, text)
        self.assertIn("body=tostring(body)", self.calls[-1][3])

    def test_scan_report_survives_default_output_filter(self):
        # Regression: scan_secrets output must not trip the global redaction
        # filter that runs in dispatch(). The old "name={count}" format made
        # "password=1"/"api_key=1" match _GENERIC_CREDENTIAL, so with the
        # default flag mode the audit report got a false-positive banner (and
        # in redact mode its counts were corrupted). Run through dispatch with
        # the real default mode and assert the report passes clean.
        bm.REDACT_MODE = "flag"
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "scan_secrets", "arguments": {}}})
        out = resp["result"]["content"][0]["text"]
        self.assertNotIn("potential secrets detected in this result", out)  # the filter banner
        self.assertTrue(out.startswith("Secret scan:"))
        self.assertIn("aws_key x1", out)

    def test_scan_options_are_validated_before_query(self):
        text, err = bm.handle_call("scan_secrets", {"include_entropy": "yes"})
        self.assertTrue(err)
        self.assertIn("must be a boolean", text)
        text, err = bm.handle_call("scan_secrets", {"include_pii": ["ssn"]})
        self.assertTrue(err)
        self.assertIn("include_pii", text)
        text, err = bm.handle_call("scan_secrets", {"since": "bad; value"})
        self.assertTrue(err)
        self.assertIn("invalid 'since'", text)
        self.assertEqual(self.calls, [])

    def test_scan_secrets_role_visibility(self):
        original_role = bm.ACTIVE_ROLE
        try:
            bm.ACTIVE_ROLE = "soc"
            tools = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertIn("scan_secrets", {tool["name"] for tool in tools["result"]["tools"]})
            bm.ACTIVE_ROLE = "claude"
            tools = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertNotIn("scan_secrets", {tool["name"] for tool in tools["result"]["tools"]})
            bm.ACTIVE_ROLE = "all"
            tools = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertIn("scan_secrets", {tool["name"] for tool in tools["result"]["tools"]})
        finally:
            bm.ACTIVE_ROLE = original_role


if __name__ == "__main__":
    unittest.main(verbosity=2)
