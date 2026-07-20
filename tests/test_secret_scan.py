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

    def test_findings_are_aggregated_by_type_not_capped(self):
        """SEC-004: the summary is aggregated by finding_type, and there is
        no correctness reason for it to stop counting at MAX_MATCHES -- the
        type vocabulary is small and fixed regardless of match count."""
        text = " ".join([AWS_KEY] * (ss.MAX_MATCHES + 20))
        _clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(
            findings, [{"type": "aws_key", "count": ss.MAX_MATCHES + 20, "first_offset": 0}]
        )

    def test_redact_mode_leaves_nothing_past_the_old_cap(self):
        """SEC-004 minimal-evidence reproduction: 101 distinct dummy
        AWS-style credentials must ALL be redacted, not just the first 100.
        Before the fix, the 101st+ leaked verbatim in redact-mode output."""
        keys = [f"AKIA{str(i).zfill(16)}" for i in range(ss.MAX_MATCHES + 1)]
        text = " ".join(keys)
        clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(sum(f["count"] for f in findings), ss.MAX_MATCHES + 1)
        for key in keys:
            self.assertNotIn(key, clean)
        self.assertEqual(clean.count("[REDACTED:aws_key]"), ss.MAX_MATCHES + 1)

    def test_redactor_does_not_write_secret_to_stderr(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _clean, findings = ss.redact(f"password=hunter2 {AWS_KEY}", pii_types=())
        self.assertEqual(buf.getvalue(), "")
        self.assertNotIn("hunter2", repr(findings))
        self.assertNotIn(AWS_KEY, repr(findings))

    def test_overlapping_candidates_leave_no_suffix(self):
        text = "password=hunter2hunter2 secret=hunter2hunter2"
        clean, findings = ss.redact(text, pii_types=())
        self.assertNotIn("hunter2", clean)

    def test_adjacent_nonoverlapping_both_redacted(self):
        key1 = "AKIA" + "A" * 16
        key2 = "AKIA" + "B" * 16
        text = key1 + " " + key2
        clean, _ = ss.redact(text, pii_types=())
        self.assertNotIn(key1, clean)
        self.assertNotIn(key2, clean)
        self.assertEqual(clean.count("[REDACTED:aws_key]"), 2)

    def test_input_above_size_limit_returns_marker(self):
        text = "A" * (ss.MAX_REDACT_CHARS + 1)
        clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(clean, "[REDACTED:redaction_limit]")
        self.assertEqual(findings[0]["type"], "input_too_large")

    def test_candidate_count_above_limit_returns_marker(self):
        saved = ss.MAX_REDACT_CANDIDATES
        try:
            ss.MAX_REDACT_CANDIDATES = 10
            keys = [f"AKIA{str(i).zfill(16)}" for i in range(11)]
            text = " ".join(keys)
            clean, findings = ss.redact(text, pii_types=())
            self.assertEqual(clean, "[REDACTED:redaction_limit]")
            self.assertEqual(findings[0]["type"], "too_many_matches")
            for key in keys[:5]:
                self.assertNotIn(key, clean)
        finally:
            ss.MAX_REDACT_CANDIDATES = saved

    def test_findings_contain_no_values(self):
        text = f"password=hunter2 {AWS_KEY} user@example.com"
        _clean, findings = ss.redact(text, include_entropy=False, pii_types={"email"})
        serialized = repr(findings)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn(AWS_KEY, serialized)
        self.assertNotIn("user@example.com", serialized)

    def test_ordinary_text_unchanged(self):
        text = "Hello world, this is a normal log message with no secrets."
        clean, findings = ss.redact(text, pii_types=())
        self.assertEqual(clean, text)
        self.assertEqual(findings, [])

    def test_redaction_idempotent(self):
        text = f"password=hunter2 {AWS_KEY}"
        clean1, _ = ss.redact(text, pii_types=())
        clean2, findings2 = ss.redact(clean1, pii_types=())
        self.assertEqual(clean1, clean2)
        self.assertEqual(findings2, [])

    def test_apply_output_filter_redact_respects_limit(self):
        text = "A" * (ss.MAX_REDACT_CHARS + 1)
        result = ss.apply_output_filter(text, mode="redact")
        self.assertEqual(result, "[REDACTED:redaction_limit]")


class AuditRowParsingTest(unittest.TestCase):
    def test_parse_valid_bare_array(self):
        recs = [{"service": "api", "ts": "t1", "body": "x"},
                {"service": "web", "ts": "t2", "body": "y"}]
        result = ss._parse_audit_rows(json.dumps(recs))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["service"], "api")

    def test_parse_valid_wrapper_list(self):
        recs = [{"service": "api", "ts": "t1", "body": "x"}]
        for key in ("rows", "data", "results", "records"):
            result = ss._parse_audit_rows(json.dumps({key: recs}))
            self.assertEqual(len(result), 1, f"wrapper key={key}")

    def test_parse_valid_jsonl(self):
        recs = [{"service": "api", "ts": "t1", "body": "x"},
                {"service": "web", "ts": "t2", "body": "y"}]
        result = ss._parse_audit_rows("\n".join(json.dumps(r) for r in recs))
        self.assertEqual(len(result), 2)

    def test_parse_valid_single_jsonl_line(self):
        result = ss._parse_audit_rows(json.dumps({"service": "a", "ts": "t", "body": "b"}))
        self.assertEqual(len(result), 1)

    def test_parse_valid_empty_inputs(self):
        self.assertEqual(ss._parse_audit_rows("(no rows)"), [])
        self.assertEqual(ss._parse_audit_rows(""), [])
        self.assertEqual(ss._parse_audit_rows(json.dumps([])), [])

    def test_parse_valid_tables_shape(self):
        doc = {
            "Tables": [{
                "schema": {"columns": [
                    {"name": "service", "type": 5, "nullable": True},
                    {"name": "ts", "type": 6, "nullable": True},
                    {"name": "body", "type": 5, "nullable": True},
                ]},
                "rows": [["api", "2026-07-18T00:00:00Z", f"key {AWS_KEY}"]],
            }],
            "stats": {"rows_processed": 1},
        }
        rows = ss._parse_audit_rows(json.dumps(doc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "api")
        self.assertIn(AWS_KEY, rows[0]["body"])

    def test_parse_valid_tables_zero_rows(self):
        doc = {"Tables": [{"schema": {"columns": [
            {"name": "service"}, {"name": "ts"}, {"name": "body"},
        ]}, "rows": []}]}
        self.assertEqual(ss._parse_audit_rows(json.dumps(doc)), [])

    def test_truncated_whole_json_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows('{"Tables":')

    def test_clean_jsonl_plus_truncated_secret_raises(self):
        good = json.dumps({"service": "a", "ts": "t", "body": "clean"})
        bad = '{"service": "b", "ts": "t2", "body": "password=sec'
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(good + "\n" + bad)

    def test_jsonl_object_plus_nonjson_line_raises(self):
        good = json.dumps({"service": "a", "ts": "t", "body": "x"})
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(good + "\nnot json at all")

    def test_tables_row_shorter_than_columns_raises(self):
        doc = {"Tables": [{"schema": {"columns": [
            {"name": "service"}, {"name": "ts"}, {"name": "body"},
        ]}, "rows": [["api", "t1"]]}]}
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps(doc))

    def test_tables_row_longer_than_columns_raises(self):
        doc = {"Tables": [{"schema": {"columns": [
            {"name": "service"}, {"name": "ts"}, {"name": "body"},
        ]}, "rows": [["api", "t1", "x", "extra"]]}]}
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps(doc))

    def test_tables_row_is_scalar_raises(self):
        doc = {"Tables": [{"schema": {"columns": [
            {"name": "service"}, {"name": "ts"}, {"name": "body"},
        ]}, "rows": ["scalar_row"]}]}
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps(doc))

    def test_wrapper_list_contains_scalar_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps({"data": [42, "not_a_dict"]}))

    def test_unsupported_object_shape_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows('{"unexpected": "shape"}')

    def test_unknown_table_header_raises(self):
        text = "col_a\tcol_b\tcol_c\nval1\tval2\tval3"
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(text)

    def test_tables_malformed_schema_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps(
                {"Tables": [{"schema": {}, "rows": "not-a-list"}]}
            ))

    def test_jsonl_row_missing_required_field_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps({"service": "a", "body": "b"}))

    def test_bare_array_with_nondict_element_raises(self):
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(json.dumps([{"service": "a", "ts": "t", "body": "x"}, 42]))

    def test_multiple_tables_raises_and_does_not_hide_second_table(self):
        """FVR-001: a valid-empty first Tables entry must not hide a
        secret-bearing second entry. The parser must reject or aggregate;
        it must never process only tables[0]."""
        response = json.dumps({
            "Tables": [
                {
                    "schema": {"columns": [{"name": "service"}, {"name": "ts"}, {"name": "body"}]},
                    "rows": [],
                },
                {
                    "schema": {"columns": [{"name": "service"}, {"name": "ts"}, {"name": "body"}]},
                    "rows": [["svcX", "2026-07-19T00:00:00Z", "password=hunter2"]],
                },
            ]
        })
        with self.assertRaises(ss.AuditParseError):
            ss._parse_audit_rows(response)

    def test_scan_secrets_multi_table_returns_error_not_clean(self):
        """FVR-001 (end-to-end): scan_secrets must never report clean when the
        response has multiple tables and the second one contains a secret."""
        response = json.dumps({
            "Tables": [
                {
                    "schema": {"columns": [{"name": "service"}, {"name": "ts"}, {"name": "body"}]},
                    "rows": [],
                },
                {
                    "schema": {"columns": [{"name": "service"}, {"name": "ts"}, {"name": "body"}]},
                    "rows": [["svcX", "2026-07-19T00:00:00Z", "password=hunter2 AKIAIOSFODNN7EXAMPLE"]],
                },
            ]
        })
        orig = ss._bzrk_search
        try:
            ss._bzrk_search = lambda q, since: (response, False)
            text, err = ss.scan_secrets()
            self.assertTrue(err)
            self.assertIn("Secret scan failed", text)
            self.assertNotIn("hunter2", text)
            self.assertNotIn("AKIA", text)
            self.assertNotIn("no potential secrets", text.lower())
        finally:
            ss._bzrk_search = orig


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

    def test_scan_secrets_fails_closed_on_unparseable_response(self):
        """DR-001/DR-006: unparseable response must be is_error=True with NO
        response content echoed — not the raw text, not a redacted snippet,
        not any fragment of the original input."""
        sensitive = (
            f"session_id=shortsecret user@example.com 192.168.1.10 "
            f"4111111111111111 {AWS_KEY} private incident description"
        )
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (
            json.dumps({"unexpected_shape": sensitive}), False,
        )
        text, err = bm.handle_call("scan_secrets", {})
        self.assertTrue(err)
        self.assertIn("malformed or unsupported", text)
        self.assertNotIn("no potential secrets detected", text)
        self.assertNotIn(AWS_KEY, text)
        self.assertNotIn("shortsecret", text)
        self.assertNotIn("user@example.com", text)
        self.assertNotIn("192.168.1.10", text)
        self.assertNotIn("4111111111111111", text)
        self.assertNotIn("private incident description", text)

    def test_scan_secrets_fails_closed_on_truncated_jsonl(self):
        """DR-001: A valid clean first line followed by a truncated secret-
        bearing line must fail closed, never report clean."""
        good = json.dumps({"service": "a", "ts": "t", "body": "clean"})
        bad = '{"service": "b", "ts": "t2", "body": "password=topsecret'
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (
            good + "\n" + bad, False,
        )
        text, err = bm.handle_call("scan_secrets", {})
        self.assertTrue(err)
        self.assertNotIn("no potential secrets detected", text)
        self.assertNotIn("topsecret", text)

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
