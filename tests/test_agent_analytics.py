import json
import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_analytics as aa  # noqa: E402
import berserk_mcp as bm  # noqa: E402


def row(session, ts, tools, body="", err=False, model="claude-sonnet-5"):
    return {
        "session": session,
        "ts": ts,
        "tools": tools,
        "body": body,
        "err": "true" if err else "false",
        "model": model,
    }


def usage_row(session, ts, tools, tokens_in, tokens_out, body=""):
    event = row(session, ts, tools, body)
    event.update({
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "has_token_usage": True,
    })
    return event


def jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows)


class AgentAnalyticsPureTest(unittest.TestCase):
    def test_repeated_same_tool_target_is_likely_looping(self):
        events = [
            row("s1", f"2026-07-12T10:0{i}:00Z", "Edit", "src/app.py")
            for i in range(4)
        ]
        report = aa.analyze_loop_events(events)[0]
        self.assertEqual(report["verdict"], "likely-looping")
        self.assertGreaterEqual(report["repetition_ratio"], 0.7)
        self.assertIn("Edit", report["top_repeated_call"])

    def test_distinct_tool_targets_are_healthy(self):
        events = [
            row("s1", f"2026-07-12T10:{i:02d}:00Z", "Tool", f"target-{i}")
            for i in range(10)
        ]
        report = aa.analyze_loop_events(events)[0]
        self.assertEqual(report["verdict"], "healthy")

    def test_error_retry_cycle_is_counted(self):
        events = [
            row("s1", "2026-07-12T10:00:00Z", "Bash", "npm test", err=True),
            row("s1", "2026-07-12T10:01:00Z", "Bash", "npm test", err=True),
            row("s1", "2026-07-12T10:02:00Z", "Bash", "npm test", err=True),
            row("s1", "2026-07-12T10:03:00Z", "Bash", "npm test", err=True),
        ]
        report = aa.analyze_loop_events(events)[0]
        self.assertGreaterEqual(report["error_retry_count"], 3)
        self.assertIn(report["verdict"], {"some-repetition", "likely-looping"})

    def test_model_fit_overpowered_underpowered_and_ok(self):
        events = [
            row("opus-short", "2026-07-12T10:00:00Z", "Read", "README", model="claude-opus-4-8"),
            row("opus-short", "2026-07-12T10:01:00Z", "Read", "README", model="claude-opus-4-8"),
            *[
                row("haiku-loop", f"2026-07-12T11:{i:02d}:00Z", "Bash", "pytest", err=True, model="claude-haiku")
                for i in range(40)
            ],
            *[
                row("sonnet-ok", f"2026-07-12T12:{i:02d}:00Z", "Read" if i % 2 else "Edit", f"file-{i}", model="claude-sonnet")
                for i in range(8)
            ],
        ]
        reports = {r["session_id"]: r for r in aa.analyze_model_fit_events(events)}
        self.assertTrue(reports["opus-short"]["verdict"].startswith("overpowered"))
        self.assertTrue(reports["haiku-loop"]["verdict"].startswith("underpowered"))
        self.assertEqual(reports["sonnet-ok"]["verdict"], "ok")

    def test_token_burn_estimate_flags_high_burn_loop(self):
        events = [
            row("high", f"2026-07-12T10:0{i}:00Z", "Read", "src/app.py " + ("x" * 1000))
            for i in range(4)
        ] + [
            row("low", "2026-07-12T11:00:00Z", "Read", "a.py"),
            row("low", "2026-07-12T11:01:00Z", "Edit", "b.py"),
        ]
        reports = {r["session_id"]: r for r in aa.analyze_token_burn_events(events)}
        self.assertEqual(reports["high"]["tokens"], 1011)
        self.assertEqual(reports["high"]["token_source"], "estimated")
        self.assertEqual(reports["high"]["progress_units"], 2)
        self.assertEqual(reports["high"]["verdict"], "high-burn + likely-looping")
        self.assertEqual(reports["low"]["verdict"], "normal-burn")

    def test_token_burn_prefers_exact_usage(self):
        events = [
            usage_row("exact", "2026-07-12T10:00:00Z", "Read", 1200, 300, "x" * 8000),
            usage_row("exact", "2026-07-12T10:01:00Z", "Edit", 500, 100, "y" * 8000),
        ]
        report = aa.analyze_token_burn_events(events)[0]
        self.assertEqual(report["tokens"], 2100)
        self.assertEqual(report["token_source"], "exact")

    def test_token_burn_supports_mixed_exact_and_estimated_sessions(self):
        parsed = aa._parse_rows(jsonl([
            {**row("exact", "2026-07-12T10:00:00Z", "Read", "x" * 400),
             "tokens_in": "80", "tokens_out": "20"},
            row("estimated", "2026-07-12T11:00:00Z", "Read", "y" * 400),
        ]))
        reports = {r["session_id"]: r for r in aa.analyze_token_burn_events(parsed)}
        self.assertEqual((reports["exact"]["tokens"], reports["exact"]["token_source"]), (100, "exact"))
        self.assertEqual((reports["estimated"]["tokens"], reports["estimated"]["token_source"]), (100, "estimated"))

    def test_token_burn_only_flags_top_decile(self):
        events = []
        for i in range(10):
            events.append(row(
                f"s{i}", f"2026-07-12T{i:02d}:00:00Z", "Read", "x" * ((i + 1) * 40)
            ))
        reports = aa.analyze_token_burn_events(events)
        flagged = [r["session_id"] for r in reports if r["verdict"].startswith("high-burn")]
        self.assertEqual(flagged, ["s9"])

    def test_token_burn_handles_zero_progress_without_division_error(self):
        report = aa.analyze_token_burn_events([
            row("message-only", "2026-07-12T10:00:00Z", "", "x" * 40)
        ])[0]
        self.assertEqual(report["progress_units"], 0)
        self.assertEqual(report["burn_per_progress"], 10.0)

    def test_malformed_usage_falls_back_and_negative_values_are_not_counted(self):
        malformed = aa._parse_rows(jsonl([{
            **row("bad", "2026-07-12T10:00:00Z", "Read", "x" * 40),
            "tokens_in": "not-a-number",
        }]))
        report = aa.analyze_token_burn_events(malformed)[0]
        self.assertEqual((report["tokens"], report["token_source"]), (10, "estimated"))

        negative = usage_row("negative", "2026-07-12T11:00:00Z", "Read", -50, 20)
        report = aa.analyze_token_burn_events([negative])[0]
        self.assertEqual(report["tokens"], 20)

    def test_parse_rows_accepts_json_array_and_wrapper(self):
        recs = [row("s1", "2026-07-12T10:00:00Z", "Edit", "a.py")]
        self.assertEqual(len(aa._parse_rows(json.dumps(recs))), 1)          # bare array
        self.assertEqual(len(aa._parse_rows(json.dumps({"rows": recs}))), 1)  # wrapped
        self.assertEqual(len(aa._parse_rows(jsonl(recs))), 1)              # jsonl still works
        self.assertEqual(aa._parse_rows("(no rows)"), [])

    def test_parse_rows_accepts_real_bzrk_tables_shape(self):
        """bzrk's actual `--json` output (confirmed live 2026-07-17) is
        {"Tables": [{"schema": {"columns": [...]}, "rows": [[...]]}], ...} --
        positional arrays keyed by column order, not row-dicts. This shape
        went unrecognized by _json_records until this test's fix: every
        caller (claude_token_burn, claude_loop_check, claude_model_fit)
        silently returned zero rows against real data, which unit tests
        alone never caught because they only exercised jsonl and the
        {"rows": [...]}-of-dicts shape above."""
        doc = {
            "Tables": [{
                "schema": {
                    "name": "PrimaryResult",
                    "columns": [
                        {"name": "session", "type": 5, "nullable": True},
                        {"name": "ts", "type": 6, "nullable": True},
                        {"name": "typ", "type": 9, "nullable": True},
                        {"name": "tools", "type": 9, "nullable": True},
                        {"name": "tokens_in", "type": 9, "nullable": True},
                        {"name": "tokens_out", "type": 9, "nullable": True},
                    ],
                },
                "rows": [
                    ["s1", 1784314514467508988, "assistant", "Bash", "10", "20"],
                ],
            }],
            "stats": {"rows_processed": 1},
            "trace_id": "abc123",
            "warnings": [],
        }
        parsed = aa._parse_rows(json.dumps(doc))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["session"], "s1")
        self.assertEqual(parsed[0]["tools"], "Bash")
        self.assertEqual(parsed[0]["tokens_in"], 10)
        self.assertEqual(parsed[0]["tokens_out"], 20)
        self.assertTrue(parsed[0]["has_token_usage"])

    def test_parse_rows_tables_shape_with_zero_rows(self):
        # Realistic "no matching data" response: real bzrk always populates
        # schema.columns even when the row set is empty.
        doc = {
            "Tables": [{
                "schema": {"columns": [{"name": "session", "type": 5, "nullable": True}]},
                "rows": [],
            }],
            "stats": {"rows_processed": 0},
        }
        self.assertEqual(aa._parse_rows(json.dumps(doc)), [])


class ProjectInferenceTest(unittest.TestCase):
    def test_marker_segment_yields_parent_dir(self):
        self.assertEqual(aa._infer_project("/home/u/proj-a/src/app.py"), "proj-a")
        self.assertEqual(aa._infer_project("/home/u/berserk-mcp/tests/test_x.py"), "berserk-mcp")

    def test_windows_separators_normalized(self):
        self.assertEqual(aa._infer_project(r"C:\Users\u\proj-b\src\main.py"), "proj-b")

    def test_no_marker_is_unattributed(self):
        self.assertEqual(aa._infer_project("/etc/hosts"), "(unattributed)")
        self.assertEqual(aa._infer_project(""), "(unattributed)")
        self.assertEqual(aa._infer_project("notes.md"), "(unattributed)")

    def test_marker_at_root_has_no_parent(self):
        self.assertEqual(aa._infer_project("src/app.py"), "(unattributed)")

    def test_first_marker_wins_for_nested(self):
        self.assertEqual(aa._infer_project("/h/proj-c/src/vendor/tests/x.py"), "proj-c")

    def test_path_with_spaces(self):
        self.assertEqual(aa._infer_project("/Users/u/My Project/src/a.py"), "My Project")


class BzrkSearchJsonTest(unittest.TestCase):
    def setUp(self):
        self._orig = bm.run_bzrk

    def tearDown(self):
        bm.run_bzrk = self._orig

    def test_requests_json_and_returns_when_supported(self):
        seen = []
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (
            seen.append(list(args)) or ('[{"session": "s"}]', False)
        )
        out, err = bm.bzrk_search_json(f"{bm.TABLE} | take 1", "1h ago")
        self.assertFalse(err)
        self.assertIn("--json", seen[0])
        self.assertEqual(len(seen), 1)  # no fallback needed

    def test_falls_back_when_json_flag_rejected(self):
        seen = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            seen.append(list(args))
            if "--json" in args:
                return ("error: unexpected argument '--json' found", True)
            return ("table out", False)

        bm.run_bzrk = fake
        out, err = bm.bzrk_search_json(f"{bm.TABLE} | take 1", "1h ago")
        self.assertFalse(err)
        self.assertEqual(out, "table out")
        self.assertIn("--json", seen[0])
        self.assertNotIn("--json", seen[1])

    def test_real_error_is_not_masked_by_json_fallback(self):
        seen = []
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (
            seen.append(list(args)) or ("bzrk timed out after 120s", True)
        )
        out, err = bm.bzrk_search_json(f"{bm.TABLE} | take 1", "1h ago")
        self.assertTrue(err)
        self.assertEqual(len(seen), 1)  # a genuine error must not trigger a retry


class AgentAnalyticsMcpTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig_run = bm.run_bzrk

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return (jsonl([
                row("s1", "2026-07-12T10:00:00Z", "Edit", "secret-value-" + ("x" * 100)),
                row("s1", "2026-07-12T10:01:00Z", "Edit", "secret-value-" + ("x" * 100)),
                row("s1", "2026-07-12T10:02:00Z", "Edit", "secret-value-" + ("x" * 100)),
                row("s1", "2026-07-12T10:03:00Z", "Edit", "secret-value-" + ("x" * 100)),
            ]), False)

        bm.run_bzrk = fake_run_bzrk

    def tearDown(self):
        bm.run_bzrk = self._orig_run

    def test_loop_tool_dispatches_and_truncates_body(self):
        text, err = bm.handle_call("claude_loop_check", {})
        self.assertFalse(err)
        self.assertIn("likely-looping", text)
        self.assertIn("substring(tostring(body), 0, 80)", self.calls[-1][3])
        self.assertNotIn("x" * 90, text)

    def test_loop_check_scrubs_secrets_in_body_snippet(self):
        # A secret in the first 60 chars of a body reaches top_repeated_call.
        # It must be scrubbed by the injected redactor regardless of the global
        # output-filter mode (roadmap A1: no raw secret-bearing body echoed).
        aws = "AKIAIOSFODNN7EXAMPLE"
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (jsonl([
            row("s1", f"2026-07-12T10:0{i}:00Z", "Bash", f"aws {aws} deploy")
            for i in range(4)
        ]), False)
        text, err = bm.handle_call("claude_loop_check", {})
        self.assertFalse(err)
        self.assertNotIn(aws, text)
        self.assertIn("[REDACTED:aws_key]", text)

    def test_model_fit_tool_dispatches(self):
        text, err = bm.handle_call("claude_model_fit", {})
        self.assertFalse(err)
        self.assertIn("heuristic, not a billing statement", text)

    def test_token_burn_tool_dispatches_as_labeled_proxy(self):
        text, err = bm.handle_call("claude_token_burn", {})
        self.assertFalse(err)
        self.assertIn("body-length fallback", text)
        self.assertIn("0 exact sessions, 1 estimated sessions", text)
        self.assertIn("body=tostring(body)", self.calls[-1][3])
        self.assertIn("claude.tokens_input", self.calls[-1][3])
        self.assertIn("claude.tokens_output", self.calls[-1][3])
        self.assertNotIn("substring(tostring(body), 0, 80)", self.calls[-1][3])

    def test_invalid_since_rejected_before_shelling_out(self):
        text, err = bm.handle_call("claude_loop_check", {"since": "bad; nope"})
        self.assertTrue(err)
        self.assertIn("invalid 'since'", text)

        text, err = bm.handle_call("claude_token_burn", {"since": "bad; nope"})
        self.assertTrue(err)
        self.assertIn("invalid 'since'", text)
        self.assertEqual(self.calls, [])

    def test_tools_list_includes_claude_analytics_for_claude_role(self):
        orig_role = bm.ACTIVE_ROLE
        try:
            bm.ACTIVE_ROLE = "claude"
            resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = {t["name"] for t in resp["result"]["tools"]}
            self.assertIn("claude_loop_check", names)
            self.assertIn("claude_model_fit", names)
            self.assertIn("claude_token_burn", names)
        finally:
            bm.ACTIVE_ROLE = orig_role

    def test_agent_report_returns_alert_status(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bm.run_agent_report("6h ago")
        self.assertEqual(code, 1)
        self.assertIn("likely-looping", buf.getvalue())
        self.assertIn("Claude Code token burn", buf.getvalue())

    def test_token_burn_propagates_query_errors(self):
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: ("query failed", True)
        text, err = bm.handle_call("claude_token_burn", {})
        self.assertTrue(err)
        self.assertEqual(text, "query failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
