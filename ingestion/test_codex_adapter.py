#!/usr/bin/env python3
"""Tests for ingestion/codex_adapter.py (issue #42). Pure parsing/normalization
logic gets full unit coverage; the file-reading/state/POST wrapper is thin
enough to verify by live smoke test against real ~/.codex data instead,
matching this repo's existing convention (see test_ci_gate.py, test_run_eval_usage.py)."""
import json
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
