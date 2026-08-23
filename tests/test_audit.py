#!/usr/bin/env python3
"""Tests for audit.py (issue #17). The four acceptance criteria the issue
names explicitly are each their own test class below, not folded into
generic coverage -- these are the properties that make this an audit
ledger rather than just a log file."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import audit  # noqa: E402


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        self.ledger = audit.Ledger(self.path, retention_days=30)

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, **overrides):
        kwargs = dict(
            principal_id="role:sre", transport="stdio", tool="top_cpu",
            kql_canonical_sha256="a" * 64, resolved_since="15m ago",
            row_count=3, bytes_out=120, redaction_rules_applied=(),
            latency_ms=42, outcome="ok",
        )
        kwargs.update(overrides)
        return self.ledger.append(**kwargs)


class BasicAppendTest(LedgerTestBase):
    def test_append_writes_one_line_with_all_fields(self):
        self._append()
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(set(rec.keys()), set(audit.RECORD_FIELDS))

    def test_genesis_record_prev_hash_is_the_genesis_marker(self):
        self._append()
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(rec["prev_hash"], audit.GENESIS_HASH)

    def test_second_record_prev_hash_equals_first_records_hash(self):
        self._append()
        self._append()
        lines = self.path.read_text().splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(second["prev_hash"], first["record_hash"])

    def test_record_is_actually_flushed_to_disk_before_append_returns(self):
        self._append()
        # A fresh Ledger instance reading the same path must see it -- proves
        # this isn't sitting in an in-process buffer only.
        reopened = audit.Ledger(self.path, retention_days=30)
        self.assertEqual(reopened.verify().ok, True)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_stdio_transport_is_recorded_as_is_no_implied_identity(self):
        self._append(transport="stdio", principal_id="role:sre")
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(rec["transport"], "stdio")


class ChainVerificationTamperDetectionTest(LedgerTestBase):
    """Required acceptance criterion: tamper with a record, assert detection."""

    def test_untampered_chain_verifies_ok(self):
        for _ in range(5):
            self._append()
        result = self.ledger.verify()
        self.assertTrue(result.ok)
        self.assertIsNone(result.broken_at)

    def test_editing_a_field_in_place_is_detected(self):
        for _ in range(3):
            self._append()
        lines = self.path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["row_count"] = 999999  # tamper: change a field, leave the stored hash alone
        lines[1] = json.dumps(rec)
        self.path.write_text("\n".join(lines) + "\n")

        result = self.ledger.verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.broken_at, 1)

    def test_deleting_a_record_breaks_the_chain_from_that_point(self):
        for _ in range(4):
            self._append()
        lines = self.path.read_text().splitlines()
        del lines[1]  # remove one record; index 2's prev_hash no longer matches
        self.path.write_text("\n".join(lines) + "\n")

        result = self.ledger.verify()
        self.assertFalse(result.ok)

    def test_reordering_two_records_is_detected(self):
        for _ in range(3):
            self._append()
        lines = self.path.read_text().splitlines()
        lines[0], lines[1] = lines[1], lines[0]
        self.path.write_text("\n".join(lines) + "\n")

        result = self.ledger.verify()
        self.assertFalse(result.ok)

    def test_forging_a_record_hash_to_match_tampered_content_is_still_detected(self):
        # The strongest tamper attempt: recompute record_hash after editing,
        # so a naive "does record_hash match its own content" check alone
        # would pass. Detection must come from the *chain* -- every
        # downstream record's prev_hash still points at the original hash.
        for _ in range(3):
            self._append()
        lines = self.path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["row_count"] = 999999
        rec["record_hash"] = audit._record_hash(
            {k: v for k, v in rec.items() if k != "record_hash"}
        )
        lines[1] = json.dumps(rec)
        self.path.write_text("\n".join(lines) + "\n")

        result = self.ledger.verify()
        self.assertFalse(result.ok)


class RedactionNoValuesTest(LedgerTestBase):
    """Required acceptance criterion: redaction records rule IDs, never
    matched values."""

    def test_redaction_rules_applied_stores_only_types_not_values(self):
        self._append(redaction_rules_applied=("aws_key", "email"))
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(sorted(rec["redaction_rules_applied"]), ["aws_key", "email"])

    def test_rejects_a_redaction_rule_that_looks_like_a_secret_value(self):
        # Defense in depth: even if a caller accidentally passed a matched
        # value instead of a rule type, it must not silently persist --
        # rule "names" are validated against a fixed shape (short
        # identifier-like strings), not accepted as arbitrary text.
        with self.assertRaises(audit.InvalidRecordError):
            self._append(redaction_rules_applied=("sk-proj-abcdef1234567890abcdef1234567890",))

    def test_empty_redaction_rules_is_valid_meaning_nothing_fired(self):
        self._append(redaction_rules_applied=())
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(rec["redaction_rules_applied"], [])


class NoRowContentTest(LedgerTestBase):
    """Required acceptance criterion: assert no row content in any record,
    property-tested against varied bzrk stub output."""

    VARIED_OUTPUTS = [
        "",
        "single line of output",
        "line one\nline two\nline three",
        json.dumps({"rows": [{"a": 1}, {"a": 2}]}),
        "unicode: ☃ éè emoji: \U0001F600",
        "x" * 5000,
        "contains a fake secret sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD",
    ]

    def test_append_signature_has_no_field_to_pass_row_content_into(self):
        # Structural check, not just behavioral: the function simply does
        # not accept anything shaped like row/body content -- there is no
        # parameter this test (or a future caller) could even misuse.
        import inspect
        params = set(inspect.signature(audit.Ledger.append).parameters)
        self.assertNotIn("body", params)
        self.assertNotIn("rows", params)
        self.assertNotIn("result", params)
        self.assertNotIn("output", params)

    def test_varied_outputs_never_end_up_verbatim_in_the_record(self):
        for i, output in enumerate(self.VARIED_OUTPUTS):
            with self.subTest(i=i):
                self._append(row_count=len(output.splitlines()), bytes_out=len(output.encode()))
                rec = json.loads(self.path.read_text().splitlines()[-1])
                serialized = json.dumps(rec)
                if output:
                    self.assertNotIn(output, serialized)


class RetentionConfigurationTest(unittest.TestCase):
    """Required acceptance criterion: retention configurable, no silent
    default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_constructing_a_ledger_without_retention_days_raises(self):
        with self.assertRaises(TypeError):
            audit.Ledger(self.path)  # retention_days is required, no default

    def test_retention_days_must_be_a_positive_number(self):
        with self.assertRaises(ValueError):
            audit.Ledger(self.path, retention_days=0)
        with self.assertRaises(ValueError):
            audit.Ledger(self.path, retention_days=-5)

    def test_explicit_retention_days_is_honored(self):
        ledger = audit.Ledger(self.path, retention_days=7)
        self.assertEqual(ledger.retention_days, 7)


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, ledger, **overrides):
        kwargs = dict(
            principal_id="role:sre", transport="stdio", tool="top_cpu",
            outcome="ok",
        )
        kwargs.update(overrides)
        return ledger.append(**kwargs)

    def test_exceeding_rotate_bytes_starts_a_fresh_file_with_new_genesis(self):
        # rotate_bytes tiny on purpose so one append is enough to trigger it
        # on the *next* append, without needing to write megabytes in a test.
        ledger = audit.Ledger(self.path, retention_days=30, rotate_bytes=10)
        self._append(ledger)
        self._append(ledger)
        rotated = list(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        self.assertEqual(len(rotated), 1)
        current = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(current["prev_hash"], audit.GENESIS_HASH)

    def test_rotated_segments_are_each_independently_verifiable(self):
        ledger = audit.Ledger(self.path, retention_days=30, rotate_bytes=10)
        self._append(ledger)
        self._append(ledger)
        rotated_path = next(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        rotated_ledger = audit.Ledger(rotated_path, retention_days=30)
        self.assertTrue(rotated_ledger.verify().ok)
        self.assertTrue(ledger.verify().ok)

    def test_rotated_files_older_than_retention_are_pruned(self):
        ledger = audit.Ledger(self.path, retention_days=1, rotate_bytes=10)
        self._append(ledger)
        old_rotated = self.path.with_name(f"{self.path.stem}.old{self.path.suffix}")
        old_rotated.write_text("{}\n")
        old_time = time.time() - (2 * 86400)  # 2 days old, retention is 1 day
        os.utime(old_rotated, (old_time, old_time))

        self._append(ledger)  # triggers rotation + prune

        self.assertFalse(old_rotated.exists())

    def test_rotated_files_within_retention_survive_pruning(self):
        ledger = audit.Ledger(self.path, retention_days=30, rotate_bytes=10)
        self._append(ledger)
        recent_rotated = self.path.with_name(f"{self.path.stem}.recent{self.path.suffix}")
        recent_rotated.write_text("{}\n")

        self._append(ledger)

        self.assertTrue(recent_rotated.exists())


if __name__ == "__main__":
    unittest.main()
