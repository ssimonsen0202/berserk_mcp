#!/usr/bin/env python3
"""Tests for ingestion/codex_adapter.py (issue #42). Pure parsing/normalization
logic gets full unit coverage. process_file() and run() -- the stateful
file-reading/offset/POST wrapper -- also get direct tests here (a manual live
smoke test against real ~/.codex data caught the happy path but missed the
partial-line, failed-POST, and dry-run state-mutation bugs a real reviewer
found; those paths are exercised explicitly below now)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_adapter as ca  # noqa: E402


def _line(type_, payload):
    return json.dumps({"timestamp": "2026-08-21T20:10:56.402Z", "type": type_, "payload": payload})


class ParseCodexLineTest(unittest.TestCase):
    def test_token_count_event_extracts_usage_fields(self):
        rec = ca.parse_codex_line(_line("event_msg", {
            "type": "token_count",
            "info": {"last_token_usage": {
                "input_tokens": 25821, "cached_input_tokens": 11008,
                "output_tokens": 264, "total_tokens": 26085,
            }},
            "rate_limits": {"primary": {"used_percent": 82.0}},
        }))
        self.assertEqual(rec["type"], "token_count")
        self.assertEqual(rec["input_tokens"], 25821)
        self.assertEqual(rec["cached_input_tokens"], 11008)
        self.assertEqual(rec["output_tokens"], 264)
        self.assertEqual(rec["total_tokens"], 26085)
        self.assertEqual(rec["quota_used_percent"], 82.0)

    def test_token_count_event_without_rate_limits_omits_quota_field(self):
        rec = ca.parse_codex_line(_line("event_msg", {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        }))
        self.assertNotIn("quota_used_percent", rec)

    def test_function_call_extracts_tool_name(self):
        rec = ca.parse_codex_line(_line("response_item", {
            "type": "function_call", "name": "spawn_agent", "arguments": "{}",
        }))
        self.assertEqual(rec["type"], "tool_call")
        self.assertEqual(rec["tool_names"], "spawn_agent")

    def test_function_call_output_is_a_tool_result_record(self):
        rec = ca.parse_codex_line(_line("response_item", {
            "type": "function_call_output", "call_id": "call_123", "output": "{\"ok\":true}",
        }))
        self.assertEqual(rec["type"], "tool_result")

    def test_user_message_extracts_role_and_text(self):
        rec = ca.parse_codex_line(_line("event_msg", {
            "type": "user_message", "message": "how do I connect codex to claude code",
        }))
        self.assertEqual(rec["type"], "user")
        self.assertEqual(rec["body"], "how do I connect codex to claude code")

    def test_unmapped_record_types_return_none(self):
        self.assertIsNone(ca.parse_codex_line(_line("world_state", {})))
        self.assertIsNone(ca.parse_codex_line(_line("session_meta", {"session_id": "x"})))
        self.assertIsNone(ca.parse_codex_line(_line("event_msg", {"type": "task_started"})))

    def test_malformed_json_returns_none_not_a_crash(self):
        self.assertIsNone(ca.parse_codex_line("{not valid json"))

    def test_empty_line_returns_none(self):
        self.assertIsNone(ca.parse_codex_line(""))
        self.assertIsNone(ca.parse_codex_line("   "))

    def test_missing_payload_returns_none_not_a_crash(self):
        self.assertIsNone(ca.parse_codex_line(json.dumps({"type": "event_msg"})))

    def test_token_count_with_missing_usage_fields_defaults_to_zero_not_crash(self):
        rec = ca.parse_codex_line(_line("event_msg", {"type": "token_count", "info": {}}))
        self.assertEqual(rec["input_tokens"], 0)
        self.assertEqual(rec["output_tokens"], 0)


class ExtractSessionIdTest(unittest.TestCase):
    def test_extracts_from_session_meta_line(self):
        line = _line("session_meta", {"session_id": "01a025f2-a5e3-7b52-b2d4-c9ba463ddebb"})
        self.assertEqual(ca.extract_session_id_from_line(line), "01a025f2-a5e3-7b52-b2d4-c9ba463ddebb")

    def test_falls_back_to_filename_uuid_when_no_session_meta_line(self):
        path = Path("rollout-2026-08-21T22-10-54-01a025f2-a5e3-7b52-b2d4-c9ba463ddebb.jsonl")
        self.assertEqual(ca.extract_session_id_from_filename(path), "01a025f2-a5e3-7b52-b2d4-c9ba463ddebb")

    def test_non_session_meta_line_returns_none(self):
        line = _line("event_msg", {"type": "token_count"})
        self.assertIsNone(ca.extract_session_id_from_line(line))


class RedactTest(unittest.TestCase):
    def test_openai_style_key_is_redacted(self):
        text = "here is my key sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGH ok"
        redacted, count = ca.redact_text(text)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGH", redacted)
        self.assertGreaterEqual(count, 1)

    def test_ordinary_text_is_untouched(self):
        text = "what is the capital of France"
        redacted, count = ca.redact_text(text)
        self.assertEqual(redacted, text)
        self.assertEqual(count, 0)

    def test_already_encrypted_looking_payload_is_still_redacted_not_assumed_safe(self):
        # Fernet-token-shaped strings (gAAAAAB... prefix) showed up in real
        # spawn_agent call arguments during investigation -- opaque already,
        # but redact_text must not special-case skipping them; it should
        # apply the same high-entropy-string treatment as anything else.
        text = "gAAAAABqiKrA0HJEI_e9dUDMeXRWAUz5IwDm9R6F9F-Q_7U1puS97atrpQ-isFgbVKU"
        redacted, count = ca.redact_text(text)
        self.assertGreaterEqual(count, 1)


def _token_count_line():
    return _line("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})


class ProcessFileTest(unittest.TestCase):
    """A trailing partial line (Codex still writing) must never be consumed --
    doing so would land the next run's offset mid-record (issue found in review)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rollout-test.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_complete_lines_are_consumed(self):
        line = _token_count_line()
        self.path.write_bytes((line + "\n" + line + "\n").encode() + b'{"type": "event_msg", "pay')  # partial 3rd line
        records, new_offset, _ = ca.process_file(self.path, 0, {})
        self.assertEqual(len(records), 2)
        # new_offset must land exactly after the 2nd line's newline, not past the partial 3rd
        self.assertEqual(new_offset, len((line + "\n") * 2))

    def test_next_call_with_returned_offset_does_not_reread_or_skip(self):
        line = _token_count_line()
        self.path.write_bytes((line + "\n").encode())
        records1, offset1, _ = ca.process_file(self.path, 0, {})
        self.assertEqual(len(records1), 1)
        # nothing new since offset1 yet
        records2, offset2, _ = ca.process_file(self.path, offset1, {})
        self.assertEqual(records2, [])
        self.assertEqual(offset2, offset1)
        # now the partial line completes
        with open(self.path, "ab") as f:
            f.write((line + "\n").encode())
        records3, offset3, _ = ca.process_file(self.path, offset1, {})
        self.assertEqual(len(records3), 1)
        self.assertGreater(offset3, offset1)

    def test_all_complete_lines_consume_whole_file(self):
        line = _token_count_line()
        content = (line + "\n") * 3
        self.path.write_bytes(content.encode())
        records, new_offset, _ = ca.process_file(self.path, 0, {})
        self.assertEqual(len(records), 3)
        self.assertEqual(new_offset, len(content))


class RunTest(unittest.TestCase):
    """run()'s offset/state bookkeeping around POST failures and --dry-run."""

    def setUp(self):
        self.codex_home = tempfile.TemporaryDirectory()
        self.state_dir = tempfile.TemporaryDirectory()
        sessions = Path(self.codex_home.name) / "sessions" / "2026" / "01" / "01"
        sessions.mkdir(parents=True)
        self.rollout = sessions / "rollout-2026-01-01T00-00-00-01a025f2-a5e3-7b52-b2d4-c9ba463ddebb.jsonl"
        self.rollout.write_bytes((_token_count_line() + "\n").encode())

    def tearDown(self):
        self.codex_home.cleanup()
        self.state_dir.cleanup()

    def test_failed_post_does_not_advance_offset_or_count_as_emitted(self):
        with mock.patch.object(ca._http, "http_post_json", return_value=(None, "connection failed")):
            n = ca.run(self.codex_home.name, self.state_dir.name, "https://example.invalid/v1/logs", None, "host")
        self.assertEqual(n, 0)
        state = ca.load_state(self.state_dir.name)
        self.assertEqual(state["offsets"].get(str(self.rollout), 0), 0)

    def test_successful_post_advances_offset_and_counts(self):
        with mock.patch.object(ca._http, "http_post_json", return_value=({}, None)):
            n = ca.run(self.codex_home.name, self.state_dir.name, "https://example.invalid/v1/logs", None, "host")
        self.assertEqual(n, 1)
        state = ca.load_state(self.state_dir.name)
        self.assertGreater(state["offsets"][str(self.rollout)], 0)

    def test_dry_run_does_not_persist_state(self):
        ca.run(self.codex_home.name, self.state_dir.name, "", None, "host", dry_run=True)
        self.assertFalse((Path(self.state_dir.name) / "codex_adapter_state.json").exists())

    def test_dry_run_then_real_run_still_emits(self):
        # Regression check: a dry run must not "consume" offsets that a
        # subsequent real run needs to see.
        ca.run(self.codex_home.name, self.state_dir.name, "", None, "host", dry_run=True)
        with mock.patch.object(ca._http, "http_post_json", return_value=({}, None)):
            n = ca.run(self.codex_home.name, self.state_dir.name, "https://example.invalid/v1/logs", None, "host")
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
