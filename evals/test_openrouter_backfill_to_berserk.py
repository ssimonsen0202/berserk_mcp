import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openrouter_backfill_to_berserk import (
    _read_state,
    _write_state,
    merge_payloads,
    run_backfill,
)


def _attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _otlp_payload(model="m", trace_id="t1"):
    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", "openrouter")]},
            "scopeSpans": [{
                "spans": [{
                    "traceId": trace_id, "spanId": "s1", "name": "gen",
                    "startTimeUnixNano": "1700000000000000000",
                    "attributes": [_attr("gen_ai.request.model", model)],
                }],
            }],
        }],
    }


def _raw_line(payload):
    return json.dumps({"received_at": "2026-08-23T00:00:00Z", "raw": json.dumps(payload)})


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "state.json")

    def test_missing_state_file_returns_zero(self):
        self.assertEqual(_read_state(self.state_path), 0)

    def test_write_then_read_round_trips(self):
        _write_state(self.state_path, 42)
        self.assertEqual(_read_state(self.state_path), 42)

    def test_corrupt_state_file_treated_as_zero_not_a_crash(self):
        with open(self.state_path, "w") as f:
            f.write("not json")
        self.assertEqual(_read_state(self.state_path), 0)

    def test_write_is_atomic_no_tmp_file_left_behind(self):
        _write_state(self.state_path, 1)
        leftovers = [f for f in os.listdir(self.tmpdir) if f.startswith(".ortmp-")]
        self.assertEqual(leftovers, [])


class MergePayloadsTest(unittest.TestCase):
    def test_merges_multiple_resourceLogs_into_one_payload(self):
        from openrouter_webhook_receiver import spans_to_berserk_payload
        p1 = spans_to_berserk_payload(_otlp_payload(trace_id="t1"))
        p2 = spans_to_berserk_payload(_otlp_payload(trace_id="t2"))
        merged = merge_payloads([p1, p2])
        self.assertEqual(len(merged["resourceLogs"]), 2)

    def test_all_none_payloads_returns_none(self):
        self.assertIsNone(merge_payloads([None, None]))

    def test_empty_list_returns_none(self):
        self.assertIsNone(merge_payloads([]))


class RunBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.raw_path = os.path.join(self.tmpdir, "raw.jsonl")
        self.state_path = os.path.join(self.tmpdir, "state.json")
        self.posted = []

    def _write_raw(self, n):
        with open(self.raw_path, "w") as f:
            for i in range(n):
                f.write(_raw_line(_otlp_payload(trace_id=f"t{i}")) + "\n")

    def _fake_post(self, endpoint, payload):
        self.posted.append(payload)
        return True, "http 200"

    def test_forwards_all_records_in_one_batch_when_under_batch_size(self):
        self._write_raw(3)
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, post_fn=self._fake_post)
        self.assertTrue(ok)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(len(self.posted[0]["resourceLogs"]), 3)
        self.assertEqual(_read_state(self.state_path), 3)

    def test_splits_into_multiple_batches(self):
        self._write_raw(5)
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=2, post_fn=self._fake_post)
        self.assertTrue(ok)
        self.assertEqual(len(self.posted), 3)  # 2, 2, 1
        self.assertEqual(_read_state(self.state_path), 5)

    def test_resumes_from_saved_state_not_from_scratch(self):
        self._write_raw(5)
        _write_state(self.state_path, 3)
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, post_fn=self._fake_post)
        self.assertTrue(ok)
        self.assertEqual(len(self.posted[0]["resourceLogs"]), 2)  # only lines 3,4
        self.assertEqual(_read_state(self.state_path), 5)

    def test_failed_batch_stops_and_does_not_advance_state(self):
        self._write_raw(5)
        calls = []
        def failing_after_first(endpoint, payload):
            calls.append(payload)
            return (True, "ok") if len(calls) == 1 else (False, "connection refused")
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=2, post_fn=failing_after_first)
        self.assertFalse(ok)
        # first batch (lines 0-1) succeeded and was saved; second batch failed
        self.assertEqual(_read_state(self.state_path), 2)

    def test_dry_run_never_calls_post_fn(self):
        self._write_raw(3)
        def should_not_be_called(endpoint, payload):
            raise AssertionError("post_fn must not be called in dry-run")
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, dry_run=True, post_fn=should_not_be_called)
        self.assertTrue(ok)

    def test_dry_run_does_not_persist_state_a_real_run_afterward_still_sees_everything(self):
        # Regression: an earlier version advanced and wrote the state file
        # even in dry-run, so a real run right after a preview would think
        # this range was already done and silently skip it -- caught by
        # actually dry-running the deployed script before the real
        # backfill, not by reasoning about it in advance.
        self._write_raw(3)
        run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                      batch_size=25, dry_run=True, post_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        self.assertEqual(_read_state(self.state_path), 0)
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, post_fn=self._fake_post)
        self.assertTrue(ok)
        self.assertEqual(len(self.posted[0]["resourceLogs"]), 3)
        self.assertEqual(_read_state(self.state_path), 3)

    def test_malformed_line_is_skipped_not_fatal(self):
        with open(self.raw_path, "w") as f:
            f.write(_raw_line(_otlp_payload(trace_id="t0")) + "\n")
            f.write("not valid json at all\n")
            f.write(_raw_line(_otlp_payload(trace_id="t2")) + "\n")
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, post_fn=self._fake_post)
        self.assertTrue(ok)
        self.assertEqual(len(self.posted[0]["resourceLogs"]), 2)  # 2 valid spans
        self.assertEqual(_read_state(self.state_path), 3)  # all 3 lines counted as processed

    def test_redaction_applied_to_backfilled_data(self):
        payload = _otlp_payload(model="m")
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
            _attr("gen_ai.prompt", "sk-proj-realsecretvalue12345")
        )
        with open(self.raw_path, "w") as f:
            f.write(_raw_line(payload) + "\n")
        seen_texts = []
        def spy_redact(text):
            seen_texts.append(text)
            return "[REDACTED]"
        ok = run_backfill(self.raw_path, self.state_path, "http://x/v1/logs",
                           batch_size=25, post_fn=self._fake_post, redact=spy_redact)
        self.assertTrue(ok)
        self.assertTrue(any("sk-proj-realsecretvalue12345" in t for t in seen_texts))
        posted_str = json.dumps(self.posted[0])
        self.assertNotIn("sk-proj-realsecretvalue12345", posted_str)


if __name__ == "__main__":
    unittest.main()
