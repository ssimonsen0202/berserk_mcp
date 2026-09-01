import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import canary  # noqa: E402


class CaseSetVersionTest(unittest.TestCase):
    def test_same_content_same_version(self):
        a = canary._hash_bytes(b'{"id": "x"}\n')
        b = canary._hash_bytes(b'{"id": "x"}\n')
        self.assertEqual(a, b)

    def test_changed_content_changes_version(self):
        a = canary._hash_bytes(b'{"id": "x"}\n')
        b = canary._hash_bytes(b'{"id": "y"}\n')
        self.assertNotEqual(a, b)

    def test_version_is_short_and_stable_length(self):
        self.assertEqual(len(canary._hash_bytes(b"anything")), 12)


class BuildEvalRecordTest(unittest.TestCase):
    REPORT = {
        "backend": "openai", "model": "deepseek/deepseek-v4-flash", "repeats": 3,
        "tool_accuracy": 0.875, "arg_accuracy": 0.9,
        "total_cost_usd": 0.00205, "total_input_tokens": 115717,
        "total_output_tokens": 381, "rows": [],
    }

    def test_maps_harness_fields_onto_eval_attributes(self):
        rec = canary.build_eval_record(self.REPORT, "abc123def456", "run-1", 1234567890000000000)
        self.assertEqual(rec["eval.model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(rec["eval.backend"], "openai")
        self.assertEqual(rec["eval.tool_accuracy"], 0.875)
        self.assertEqual(rec["eval.arg_accuracy"], 0.9)
        self.assertEqual(rec["eval.repeats"], 3)
        self.assertEqual(rec["eval.case_set_version"], "abc123def456")
        self.assertEqual(rec["eval.run_id"], "run-1")
        self.assertEqual(rec["eval.status"], "ok")

    def test_every_produced_key_is_in_the_allowlist(self):
        """A key outside the allowlist would be silently dropped at emit time."""
        rec = canary.build_eval_record(self.REPORT, "v", "r", 1)
        for key in rec:
            self.assertIn(key, canary.EVAL_ATTRIBUTE_ALLOWLIST)

    def test_failure_record_has_no_score_fields(self):
        """An outage must never be stored as a score of zero."""
        rec = canary.build_failure_record(
            "deepseek/deepseek-v4-flash", "openai", "v", "r", 1, "connection refused")
        self.assertEqual(rec["eval.status"], "failed")
        self.assertNotIn("eval.tool_accuracy", rec)
        self.assertNotIn("eval.arg_accuracy", rec)


class EmitTest(unittest.TestCase):
    def test_emit_returns_false_when_no_otlp_endpoint_configured(self):
        """When OTLP_EXPORTER_OTLP_ENDPOINT is not set, emit should return False gracefully."""
        rec = {"eval.status": "ok", "eval.run_id": "test-run"}
        result = canary.emit([rec], int(1234567890 * 1_000_000_000))
        # emit() should not crash even when there's no provider configured.
        # ai_finops.emit_otlp_records returns False when _otlp_endpoint is not set.
        self.assertFalse(result)
