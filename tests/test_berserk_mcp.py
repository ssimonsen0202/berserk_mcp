"""Tests for berserk_mcp. Pure stdlib (unittest); no live Berserk needed.

Strategy: monkeypatch `run_bzrk` to capture the exact argv that would be sent to
the bzrk CLI and return canned output. This verifies the full dispatch path —
generated KQL, default time windows, injection guards, JSON-RPC shape, and the
learning loop — without a real backend.
"""
import os
import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402


class BerserkMcpTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("OK", False)

        self._orig = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        # Isolate the learned store in a temp file.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"

    def tearDown(self):
        bm.run_bzrk = self._orig
        bm.LEARNED_PATH = self._orig_learned
        self._tmp.cleanup()

    # ---- argv / KQL wiring ----
    def test_simple_tool_argv_and_default_since(self):
        text, err = bm.handle_call("list_containers", {})
        self.assertFalse(err)
        self.assertEqual(
            self.calls[-1],
            ["-P", bm.PROFILE, "search", bm.Q_CONTAINERS, "--since", "15m ago"],
        )

    def test_since_override(self):
        bm.handle_call("errors_by_service", {"since": "3d ago"})
        self.assertEqual(self.calls[-1][-1], "3d ago")
        # default would have been 1h ago
        bm.handle_call("errors_by_service", {})
        self.assertEqual(self.calls[-1][-1], "1h ago")

    def test_since_rejected(self):
        text, err = bm.handle_call("top_cpu", {"since": "garbage; rm -rf /"})
        self.assertTrue(err)
        self.assertIn("invalid 'since'", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_search_rejects_kql_not_starting_with_table(self):
        text, err = bm.handle_call("search", {"kql": "--profile x"})
        self.assertTrue(err)
        self.assertIn("invalid KQL", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_search_accepts_kql_starting_with_table(self):
        text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | take 1"})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], f"{bm.TABLE} | take 1")

    def test_since_various_valid(self):
        for s in ("now", "15m ago", "2 hours ago", "1d", "30 minutes ago", "3w ago"):
            self.calls.clear()
            text, err = bm.handle_call("top_cpu", {"since": s})
            self.assertFalse(err, s)
            self.assertEqual(self.calls[-1][-1], s)

    def test_locked_query_strings(self):
        """Guard the most-used KQL against accidental edits during refactors."""
        self.assertEqual(
            bm.Q_CONTAINERS,
            "default | where isnotnull(metric_name) | where isnotempty(resource['container.name']) "
            "| summarize samples=count() by container=tostring(resource['container.name']) "
            "| sort by container asc",
        )
        self.assertEqual(
            bm.Q_HOST_CPU,
            "default | where metric_name == 'system.cpu.load_average.1m' "
            "| summarize load_1m=avg(value) by host=tostring(resource['host.name']) "
            "| sort by load_1m desc",
        )
        self.assertEqual(
            bm.Q_CONTAINER_HOSTS,
            "default | where isnotempty(resource['container.name']) "
            "| summarize last_seen=max(timestamp) by "
            "container=tostring(resource['container.name']), host=tostring(resource['host.name']) "
            "| sort by host asc, container asc",
        )
        self.assertEqual(
            bm.Q_TRACE_FIND_SLOW,
            "default | where isnotnull(trace_id) | where isnotnull(span_name) "
            "| where isempty(parent_span_id) "
            "| extend dur=toint(duration) "
            "| where isnotnull(dur) and dur >= 0 "
            "| project trace_id, span_name, dur, timestamp, "
            "service=tostring(resource['service.name']) "
            "| sort by dur desc | take 10",
        )
        self.assertEqual(
            bm.Q_TRACE_FIND_ERRORS,
            "default | where isnotnull(trace_id) | where status_code == 'ERROR' "
            "| project trace_id, span_name, timestamp, "
            "service=tostring(resource['service.name']) "
            "| sort by timestamp desc | take 20",
        )

    def test_container_hosts_callable(self):
        text, err = bm.handle_call("container_hosts", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_CONTAINER_HOSTS)
        self.assertEqual(self.calls[-1][-1], "1h ago")

    def test_list_metrics_callable(self):
        text, err = bm.handle_call("list_metrics", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_METRICS)

    def test_bzrk_query_perf_callable(self):
        text, err = bm.handle_call("bzrk_query_perf", {})
        self.assertFalse(err)
        self.assertIn("$raw", self.calls[-1][3])
        self.assertIn("bzrk.query.execution_duration", self.calls[-1][3])

    def test_discover_schema_no_service(self):
        text, err = bm.handle_call("discover_schema", {})
        self.assertFalse(err)
        # makes TWO calls: bag_keys then row sample
        self.assertEqual(len(self.calls), 2)
        self.assertIn("bag_keys(resource)", self.calls[0][3])
        self.assertIn("take 6", self.calls[1][3])
        # neither call filters by service when none given
        for c in self.calls:
            self.assertNotIn("service.name", c[3])

    def test_discover_schema_with_service(self):
        text, err = bm.handle_call("discover_schema", {"service": "haproxy"})
        self.assertFalse(err)
        for c in self.calls:
            self.assertIn("resource['service.name'] == 'haproxy'", c[3])

    def test_discover_schema_rejects_bad_service(self):
        text, err = bm.handle_call("discover_schema", {"service": "a'; drop"})
        self.assertTrue(err)
        self.assertEqual(self.calls, [])  # must not shell out

    def test_all_simple_tools_callable(self):
        for name in bm.SIMPLE:
            self.calls.clear()
            text, err = bm.handle_call(name, {})
            self.assertFalse(err, name)
            self.assertEqual(self.calls[-1][0:3], ["-P", bm.PROFILE, "search"], name)

    # ---- injection guards ----
    def test_logs_rejects_bad_service(self):
        text, err = bm.handle_call("logs_for_service", {"service": "a' or '1'='1"})
        self.assertTrue(err)
        self.assertIn("invalid service", text)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_logs_accepts_good_service(self):
        text, err = bm.handle_call("logs_for_service", {"service": "postgres"})
        self.assertFalse(err)
        self.assertIn("resource['service.name'] == 'postgres'", self.calls[-1][3])

    def test_cc_search_rejects_quotes(self):
        for bad in ["a'b", 'a"b', "a|b", "a\\b", "a`b"]:
            self.calls.clear()
            text, err = bm.handle_call("claude_search", {"term": bad})
            self.assertTrue(err, bad)
            self.assertEqual(self.calls, [], bad)

    def test_cc_search_accepts_plain_term(self):
        text, err = bm.handle_call("claude_search", {"term": "TimeoutError"})
        self.assertFalse(err)
        self.assertIn("contains 'TimeoutError'", self.calls[-1][3])

    def test_missing_required_args(self):
        for name in ("logs_for_service", "search", "claude_search"):
            text, err = bm.handle_call(name, {})
            self.assertTrue(err, name)

    def test_schema_makes_two_calls(self):
        text, err = bm.handle_call("schema", {})
        self.assertFalse(err)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(".show tables", self.calls[0])
        self.assertIn("default | getschema", self.calls[1])

    def test_unknown_tool(self):
        text, err = bm.handle_call("does_not_exist", {})
        self.assertTrue(err)
        self.assertIn("unknown tool", text)

    # ---- learning loop ----
    def test_save_then_list_then_run(self):
        text, err = bm.handle_call("save_query", {
            "name": "Big Errors", "description": "errors over a day",
            "kql": "default | where severity_text=='ERROR' | count", "since": "1d ago"})
        self.assertFalse(err)
        # name is sanitized to snake_case
        text, err = bm.handle_call("list_saved", {})
        self.assertIn("big_errors", text)
        # run it back
        self.calls.clear()
        text, err = bm.handle_call("run_saved", {"name": "big_errors"})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], "default | where severity_text=='ERROR' | count")
        self.assertEqual(self.calls[-1][-1], "1d ago")

    def test_save_not_persisted_when_query_fails(self):
        def failing(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("PARSE ERROR", True)
        bm.run_bzrk = failing
        text, err = bm.handle_call("save_query", {
            "name": "broken", "description": "x", "kql": "default | nonsense"})
        self.assertTrue(err)
        self.assertIn("NOT saved", text)
        self.assertEqual(bm.load_learned(), [])

    def test_save_query_refuses_silent_overwrite(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1"})
        self.assertTrue(err)
        self.assertIn("already exists", text)
        self.assertIn("overwrite=true", text)
        # original entry must be untouched
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "first")

    def test_save_query_overwrite_requires_real_boolean(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        # The string "false" is truthy in Python but must not authorize overwrite.
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1",
            "overwrite": "false"})
        self.assertTrue(err)
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "first")

    def test_save_query_overwrite_true_replaces_entry(self):
        bm.handle_call("save_query", {
            "name": "dup", "description": "first", "kql": "default | count"})
        text, err = bm.handle_call("save_query", {
            "name": "dup", "description": "second", "kql": "default | take 1",
            "overwrite": True})
        self.assertFalse(err)
        items = bm.load_learned()
        match = next(it for it in items if it["name"] == "dup")
        self.assertEqual(match["description"], "second")
        self.assertEqual(match["kql"], "default | take 1")

    def test_run_saved_missing(self):
        text, err = bm.handle_call("run_saved", {"name": "nope"})
        self.assertTrue(err)
        self.assertIn("No saved query", text)

    def test_learned_store_capped_at_500(self):
        bm.save_learned([{"name": f"q{i}", "description": "x", "kql": "default | count"} for i in range(600)])
        bm.handle_call("save_query", {"name": "one_more", "description": "x", "kql": "default | count"})
        self.assertEqual(len(bm.load_learned()), 500)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_saved_store_has_private_permissions(self):
        bm.handle_call("save_query", {
            "name": "perms", "description": "x", "kql": "default | count"})
        self.assertEqual(oct(bm.LEARNED_PATH.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(bm.LEARNED_PATH.parent.stat().st_mode & 0o777), oct(0o700))

    def test_amendments_log_capped_at_1000(self):
        amendments_path = Path(bm.LEARNED_PATH).parent / "amendments_log.json"
        bm.save_json_list(amendments_path, [{"ts": "x", "name": f"q{i}"} for i in range(1200)])
        bm.handle_call("save_query", {"name": "one_more", "description": "x", "kql": "default | count"})
        self.assertEqual(len(bm.load_json_list(amendments_path)), 1000)

    # ---- FVR-005: primer routing/signal fields must reference real tools ----
    def test_primer_referenced_tools_all_exist(self):
        """FVR-005: every tool name that appears in a primer routing table
        or a 'Signals worth surfacing' bullet must be a registered tool.
        Backticked prose elsewhere may reference historical names, so this
        test only checks known-structured fields."""
        import re as _re
        registered = {t["name"] for t in bm.TOOLS + bm.MGMT_TOOLS}
        primers_dir = Path(bm.__file__).resolve().parent / "primers"
        code_re = _re.compile(r"`([a-z][a-z0-9_]*)`")
        skip_prefixes = ("$", "-", "\"")
        # Well-known non-tool identifiers that appear in backticks
        allow = {
            "search", "since", "service", "metric", "key", "term",
            "request_discovery", "system", "default",
        }
        for primer_path in primers_dir.glob("*.md"):
            in_relevant_section = False
            for line in primer_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    heading = stripped[3:].strip().lower()
                    in_relevant_section = (
                        "tool routing" in heading
                        or "signals worth surfacing" in heading
                        or "investigation flow" in heading
                    )
                    continue
                if not in_relevant_section:
                    continue
                for match in code_re.findall(line):
                    if match in allow or "_" not in match:
                        continue
                    if any(match.startswith(p) for p in skip_prefixes):
                        continue
                    self.assertIn(
                        match, registered,
                        f"{primer_path.name}: `{match}` is referenced but not a registered tool",
                    )

    # ---- BUG-002: generated-query collision ----
    def test_generated_query_renames_to_gen_on_human_collision(self):
        bm.persist_learned_query(
            {"name": "foo", "description": "human", "kql": "default | count"},
            action_source="manual")
        log = bm.persist_learned_query(
            {"name": "foo", "description": "machine", "kql": "default | take 1"},
            action_source="generated")
        self.assertEqual(log["name"], "foo_gen")
        items = bm.load_learned()
        self.assertTrue(any(it["name"] == "foo" and it["description"] == "human" for it in items))
        self.assertTrue(any(it["name"] == "foo_gen" for it in items))

    def test_generated_query_does_not_overwrite_human_gen_suffix(self):
        bm.persist_learned_query(
            {"name": "bar_gen", "description": "human named it _gen", "kql": "default | count"},
            action_source="manual")
        bm.persist_learned_query(
            {"name": "bar", "description": "also human", "kql": "default | take 1"},
            action_source="manual")
        log = bm.persist_learned_query(
            {"name": "bar", "description": "generated", "kql": "default | take 5"},
            action_source="generated")
        self.assertNotEqual(log["name"], "bar")
        self.assertNotEqual(log["name"], "bar_gen")
        items = bm.load_learned()
        self.assertTrue(any(it["name"] == "bar" and it["description"] == "also human" for it in items))
        self.assertTrue(any(it["name"] == "bar_gen" and it["description"] == "human named it _gen" for it in items))

    def test_generated_query_exhausted_suffixes_refuses_rather_than_overwrite(self):
        """FVR-003: if base, _gen, and _gen2.._gen99 are all human, generated
        must not overwrite any human entry. Search bounds at the store cap
        (500); if truly no name is available, raise."""
        # Pre-seed base and _gen with human entries
        bm.persist_learned_query(
            {"name": "collision", "description": "human base", "kql": "default | take 1"},
            action_source="manual")
        bm.persist_learned_query(
            {"name": "collision_gen", "description": "human gen", "kql": "default | take 1"},
            action_source="manual")
        # Fill _gen2 through _gen100 with human entries
        for i in range(2, 101):
            bm.persist_learned_query(
                {"name": f"collision_gen{i}", "description": "human",
                 "kql": "default | take 1"},
                action_source="manual")

        # Generated attempt must succeed with a NEW free name (>=101) and
        # crucially must NOT touch any of the human entries
        log = bm.persist_learned_query(
            {"name": "collision", "description": "generated", "kql": "default | take 5"},
            action_source="generated")

        items = bm.load_learned()
        # Human base survives
        base = next(it for it in items if it["name"] == "collision")
        self.assertEqual(base["description"], "human base")
        # Human _gen survives
        gen = next(it for it in items if it["name"] == "collision_gen")
        self.assertEqual(gen["description"], "human gen")
        # Every _gen2.._gen100 human survives
        for i in range(2, 101):
            entry = next(it for it in items if it["name"] == f"collision_gen{i}")
            self.assertEqual(entry["description"], "human")
        # Generated landed on _gen101 or higher
        self.assertRegex(log["name"], r"^collision_gen\d+$")
        self.assertNotEqual(log["name"], "collision_gen")

    def test_generated_query_can_overwrite_previous_generated(self):
        bm.persist_learned_query(
            {"name": "baz", "description": "gen1", "kql": "default | count"},
            action_source="generated")
        log = bm.persist_learned_query(
            {"name": "baz", "description": "gen2", "kql": "default | take 1"},
            action_source="generated")
        self.assertEqual(log["name"], "baz")
        items = bm.load_learned()
        matches = [it for it in items if it["name"] == "baz"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["description"], "gen2")

    # ---- JSON-RPC protocol ----
    def test_initialize_requires_protocol_version(self):
        """FVR-004: initialize without a protocolVersion must return -32602,
        not silently succeed with a default. Prior behavior returned a
        result envelope for `params: {}`."""
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_initialize_valid_returns_negotiated_version(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "berserk-q")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertTrue(resp["result"]["instructions"])

    def test_initialize_rejects_non_object_capabilities(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                       "capabilities": []}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_initialize_rejects_non_object_client_info(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                       "clientInfo": "berserk-cli"}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_notifications_initialized_as_request_form_rejected(self):
        """FVR-004: request-form of a notification must be rejected."""
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 2,
                            "method": "notifications/initialized"})
        self.assertIsNotNone(resp)
        self.assertEqual(resp["error"]["code"], -32600)

    def test_ping_rejects_nonempty_params(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 3, "method": "ping",
                            "params": {"extra": "junk"}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tools_list_rejects_nonempty_params(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                            "params": {"filter": "sre"}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unexpected_handler_exception_becomes_internal_error(self):
        """FVR-004: an unexpected exception from handle_call must surface as
        JSON-RPC -32603, not be silently converted to isError=True."""
        orig = bm.handle_call
        try:
            def raise_it(name, arguments):
                raise RuntimeError("boom")
            bm.handle_call = raise_it
            resp = bm.dispatch({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "list_hosts", "arguments": {}},
            })
        finally:
            bm.handle_call = orig
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32603)

    def test_module_execution_runs_main_exactly_once(self):
        """FVR-006: `python -m berserk_mcp` with closed stdin must run
        exactly one MCP-serve lifecycle, not two."""
        import subprocess
        project_root = str(Path(bm.__file__).resolve().parent)
        result = subprocess.run(
            [sys.executable, "-m", "berserk_mcp"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
        # log() writes to stderr with "starting" and "stdin closed" markers
        starts = result.stderr.count("starting v")
        closes = result.stderr.count("stdin closed")
        self.assertEqual(starts, 1, f"expected exactly one start, got {starts}:\n{result.stderr}")
        self.assertEqual(closes, 1, f"expected exactly one close, got {closes}:\n{result.stderr}")

    def test_serve_mcp_loop_handles_malformed_then_valid(self):
        """FVR-004: real stdio loop must emit responses for malformed JSON,
        an invalid request, and a valid ping on separate lines, and continue
        serving throughout."""
        import io
        payload = (
            "not json\n"
            + json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
        )
        orig_stdin, orig_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(payload)
            sys.stdout = io.StringIO()
            bm._serve_mcp()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = orig_stdin, orig_stdout
        lines = [json.loads(line) for line in out.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["error"]["code"], -32600)
        self.assertEqual(lines[2].get("result"), {})

    def test_initialize_negotiates_own_version_not_client_claim(self):
        """BUG-005: this server implements exactly one MCP version, so it
        must report that version regardless of what the client claims to
        speak -- previously it blindly echoed back an arbitrary client-
        supplied protocolVersion, including versions this server never
        actually implements."""
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(resp["result"]["protocolVersion"], bm.PROTOCOL_VERSION)

    def test_tools_list_count_and_metadata(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(len(names), len(bm.TOOLS) + len(bm.MGMT_TOOLS))
        self.assertIn("search", names)
        self.assertIn("save_query", names)
        # every tool has title, description, inputSchema, and annotations
        for t in resp["result"]["tools"]:
            self.assertTrue(t["title"], t["name"])
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", t["annotations"])

    def test_annotations_read_only_except_save(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        ann = {t["name"]: t["annotations"] for t in resp["result"]["tools"]}
        for n in ("top_cpu", "errors_by_service", "search", "run_saved",
                  "claude_errors", "logs_for_service", "schema"):
            self.assertTrue(ann[n]["readOnlyHint"], n)
        # save_query writes the local store -> not read-only
        self.assertFalse(ann["save_query"]["readOnlyHint"])
        # list_saved only touches the local store -> not open-world
        self.assertFalse(ann["list_saved"]["openWorldHint"])
        self.assertTrue(ann["top_cpu"]["openWorldHint"])

    def test_tools_call_shape(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "list_hosts", "arguments": {}}})
        self.assertEqual(resp["result"]["content"][0]["type"], "text")
        self.assertFalse(resp["result"]["isError"])

    def test_ping(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_notification_returns_none(self):
        self.assertIsNone(bm.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_errors(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 5, "method": "no/such"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_non_object_returns_invalid_request(self):
        """DR-004: non-object must return -32600, not None."""
        for val in ([], "not an object", None, 42, True):
            resp = bm.dispatch(val)
            self.assertIsNotNone(resp, f"None for {val!r}")
            self.assertEqual(resp["error"]["code"], -32600)
            self.assertIsNone(resp["id"])

    def test_missing_jsonrpc_or_method_returns_invalid_request(self):
        """DR-004: missing jsonrpc/method fields produce -32600."""
        resp = bm.dispatch({"id": 1, "method": "ping"})
        self.assertEqual(resp["error"]["code"], -32600)
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(resp["error"]["code"], -32600)
        resp = bm.dispatch({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(resp["error"]["code"], -32600)

    def test_invalid_id_type_returns_invalid_request(self):
        """DR-004: ID must be string or int, not bool/null/float/object/array."""
        for bad_id in (None, True, False, 3.14, [], {}):
            resp = bm.dispatch({"jsonrpc": "2.0", "id": bad_id, "method": "ping"})
            self.assertEqual(resp["error"]["code"], -32600, f"bad_id={bad_id!r}")
            self.assertIsNone(resp["id"])

    def test_valid_string_and_int_id_echoed(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": "abc", "method": "ping"})
        self.assertEqual(resp["id"], "abc")
        self.assertIn("result", resp)
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        self.assertEqual(resp["id"], 99)

    def test_notifications_get_no_response(self):
        """DR-004: valid notifications (no id) produce None for every method."""
        for method, params in (
            ("initialize", {"protocolVersion": "2024-11-05"}),
            ("ping", None),
            ("tools/list", None),
            ("tools/call", {"name": "search", "arguments": {"kql": "default | take 1"}}),
            ("no/such", None),
        ):
            req = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                req["params"] = params
            self.assertIsNone(bm.dispatch(req), method)

    def test_non_object_params_returns_invalid_params(self):
        """DR-004: scalar/list params return -32602 for requests."""
        for bad in ([], "bad", 42, True):
            resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": bad})
            self.assertEqual(resp["error"]["code"], -32602, f"params={bad!r}")
            self.assertEqual(resp["id"], 1)

    def test_non_object_params_notification_no_response(self):
        """DR-004: scalar params on notification produces no response."""
        resp = bm.dispatch({"jsonrpc": "2.0", "method": "ping", "params": "bad"})
        self.assertIsNone(resp)

    def test_tools_call_missing_name_returns_invalid_params(self):
        """DR-004: tools/call with no name is -32602."""
        resp = bm.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"arguments": {}},
        })
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tools_call_non_object_arguments_returns_invalid_params(self):
        """DR-004: tools/call with non-object arguments is -32602."""
        resp = bm.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": "not an object"},
        })
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method_request_returns_method_not_found(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 5, "method": "no/such"})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertEqual(resp["id"], 5)

    def test_unknown_method_notification_no_response(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "method": "no/such"})
        self.assertIsNone(resp)

    def test_invalid_then_valid_message_both_handled(self):
        """DR-004: server must survive invalid input and process the next."""
        resp1 = bm.dispatch([])
        self.assertEqual(resp1["error"]["code"], -32600)
        resp2 = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertIn("result", resp2)

    def test_no_secret_in_descriptions(self):
        """Sanity: no homelab IPs/usernames leaked into tool metadata."""
        blob = json.dumps(bm.TOOLS) + json.dumps(bm.MGMT_TOOLS)
        for leak in ("192.168.", "/opt/assistant", "/home/assistant", "HermesRuntime", "OpenClaw"):
            self.assertNotIn(leak, blob, leak)


class RunBzrkAuthTest(unittest.TestCase):
    """SEC-003: an exit-0 bzrk process with an auth failure on stderr must be
    treated as an error, not a successful empty result. Tests the real
    run_bzrk() against a mocked subprocess.run, unlike BerserkMcpTest which
    monkeypatches run_bzrk itself and so never exercises this logic."""

    def setUp(self):
        self._orig = subprocess.run
        self.calls = []

    def tearDown(self):
        subprocess.run = self._orig

    def _mock_run(self, returncode, stdout, stderr):
        def fake(args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)
        subprocess.run = fake

    def test_exit_zero_with_auth_error_on_stderr_returns_controlled_message(self):
        """DR-005: auth failure returns constant message, no raw stderr."""
        self._mock_run(0, "", "Refresh token rejected. Run `bzrk login` again.")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertEqual(text, bm.AUTH_FAILURE_MESSAGE)
        self.assertNotIn("Refresh token rejected", text)

    def test_exit_zero_nonempty_stdout_with_auth_stderr_returns_controlled(self):
        """DR-005: even when stdout has data, auth stderr means error with no content."""
        self._mock_run(0, "some query data", "Unauthorized: bearer token invalid")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertEqual(text, bm.AUTH_FAILURE_MESSAGE)
        self.assertNotIn("some query data", text)
        self.assertNotIn("bearer token invalid", text)

    def test_nonzero_exit_with_auth_stderr_returns_controlled(self):
        """DR-005: nonzero exit + auth stderr still gets constant message."""
        self._mock_run(1, "", "unauthenticated: refresh token rejected")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertEqual(text, bm.AUTH_FAILURE_MESSAGE)

    def test_exit_zero_with_real_empty_result_is_not_an_error(self):
        self._mock_run(0, "", "")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertFalse(is_err)
        self.assertEqual(text, "(no rows)")

    def test_exit_zero_harmless_warning_stderr_success(self):
        """DR-005: non-auth stderr warnings don't trigger auth failure."""
        self._mock_run(0, "result data", "deprecation warning: flag --old is obsolete")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertFalse(is_err)
        self.assertEqual(text, "result data")

    def test_stdout_containing_auth_words_not_misclassified(self):
        """DR-005: auth check only scans stderr, never stdout."""
        self._mock_run(0, '[{"status_code": "401", "note": "token expired"}]', "")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertFalse(is_err)
        self.assertIn("401", text)

    def test_auth_stderr_with_bearer_token_not_leaked(self):
        """DR-005: sensitive content in auth stderr never appears in output."""
        self._mock_run(0, "", "401 Unauthorized bearer opaque-dummy-token tenant=acme-corp")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertNotIn("opaque-dummy-token", text)
        self.assertNotIn("acme-corp", text)
        self.assertNotIn("401", text)

    def test_nonzero_exit_without_auth_wording_still_an_error(self):
        self._mock_run(2, "", "syntax error near 'foo'")
        text, is_err = bm.run_bzrk(["search", "default | take 1"])
        self.assertTrue(is_err)
        self.assertIn("syntax error", text)


class RoleFilterTest(unittest.TestCase):
    """tool_visible / item_visible / tools-list filtering by ACTIVE_ROLE."""

    def setUp(self):
        self._orig_role = bm.ACTIVE_ROLE
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("OK", False)

        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.run_bzrk = self._orig_run
        bm.LEARNED_PATH = self._orig_learned
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def _list_names(self, role):
        bm.ACTIVE_ROLE = role
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in resp["result"]["tools"]}

    def test_all_role_sees_everything(self):
        names = self._list_names("all")
        # sre and soc tagged tools must appear when role=all
        self.assertIn("sre_error_rate", names)
        self.assertIn("soc_high_severity_logs", names)
        self.assertIn("claude_errors", names)

    def test_sre_role_sees_sre_not_soc_or_claude(self):
        names = self._list_names("sre")
        self.assertIn("sre_error_rate", names)
        self.assertIn("sre_service_health", names)
        self.assertNotIn("soc_high_severity_logs", names)
        self.assertNotIn("claude_errors", names)
        # untagged tools (no "roles" key) always visible
        self.assertIn("list_hosts", names)

    def test_soc_role_sees_soc_not_sre_or_claude(self):
        names = self._list_names("soc")
        self.assertIn("soc_high_severity_logs", names)
        self.assertIn("soc_timeline", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertNotIn("claude_errors", names)

    def test_claude_role_sees_claude_not_sre_or_soc(self):
        names = self._list_names("claude")
        self.assertIn("claude_errors", names)
        self.assertNotIn("sre_error_rate", names)
        self.assertNotIn("soc_high_severity_logs", names)

    def test_untagged_tools_always_visible(self):
        for role in ("sre", "soc", "claude", "ops"):
            names = self._list_names(role)
            for always in ("list_hosts", "errors_by_service", "search", "save_query"):
                self.assertIn(always, names, f"role={role} missing {always}")

    def test_list_saved_filters_by_role(self):
        bm.ACTIVE_ROLE = "sre"
        bm.save_learned([
            {"name": "sre_q", "description": "SRE query", "kql": "x", "roles": ["sre"]},
            {"name": "soc_q", "description": "SOC query", "kql": "y", "roles": ["soc"]},
            {"name": "any_q", "description": "open query", "kql": "z"},
        ])
        text, err = bm.handle_call("list_saved", {})
        self.assertFalse(err)
        self.assertIn("sre_q", text)
        self.assertNotIn("soc_q", text)
        self.assertIn("any_q", text)

    def test_save_query_attaches_role(self):
        bm.ACTIVE_ROLE = "soc"
        bm.handle_call("save_query", {"name": "myq", "description": "test", "kql": f"{bm.TABLE} | take 1"})
        items = bm.load_learned()
        match = next((it for it in items if it["name"] == "myq"), None)
        self.assertIsNotNone(match)
        # normalize_roles falls back to ACTIVE_ROLE when no roles arg given
        self.assertEqual(match.get("roles"), ["soc"])


class DiscoveryToolTest(unittest.TestCase):
    """request_discovery / discovery_status handlers."""

    def setUp(self):
        self.calls = []
        self._orig_role = bm.ACTIVE_ROLE
        bm.ACTIVE_ROLE = "sre"

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            # request_discovery's visibility check now runs a `summarize
            # n=count()` query and reads the trailing numeric token, so the
            # canned result must look like a count, not a raw service name.
            return (self._search_result, False)

        self._search_result = "1"
        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.ACTIVE_ROLE = self._orig_role
        bm.run_bzrk = self._orig_run
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def test_request_discovery_queues_service(self):
        text, err = bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertFalse(err, text)
        self.assertIn("queued", text)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["source"], "my-new-service")
        self.assertEqual(queue[0]["kind"], "service")
        self.assertEqual(queue[0]["status"], "pending")

    def test_request_discovery_deduplicates(self):
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(len(queue), 1)

    def test_request_discovery_rejects_both_service_and_metric(self):
        text, err = bm.handle_call("request_discovery", {"service": "s", "metric": "m"})
        self.assertTrue(err)
        self.assertIn("exactly one", text)

    def test_request_discovery_rejects_neither(self):
        text, err = bm.handle_call("request_discovery", {})
        self.assertTrue(err)

    def test_request_discovery_rejects_invalid_name(self):
        text, err = bm.handle_call("request_discovery", {"service": "bad name!"})
        self.assertTrue(err)

    def test_request_discovery_rejects_unseen_source(self):
        self._search_result = "0"  # count query reports zero matches
        text, err = bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertTrue(err)
        self.assertIn("not currently visible", text)

    def test_discovery_queue_capped_at_500(self):
        bm.save_json_list(bm.DISCOVERY_QUEUE_PATH, [
            {"source": f"svc{i}", "kind": "service", "status": "done"} for i in range(600)
        ])
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        self.assertEqual(len(bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)), 500)

    def test_discovery_status_empty(self):
        text, err = bm.handle_call("discovery_status", {})
        self.assertFalse(err)
        self.assertIn("No discovery jobs", text)

    def test_discovery_status_lists_jobs(self):
        bm.handle_call("request_discovery", {"service": "my-new-service"})
        text, err = bm.handle_call("discovery_status", {})
        self.assertFalse(err)
        self.assertIn("my-new-service", text)
        self.assertIn("pending", text)

    def test_sre_service_health_dispatches(self):
        text, err = bm.handle_call("sre_service_health", {"service": "api-gateway"})
        self.assertFalse(err)
        kql_arg = self.calls[-1][3]
        self.assertIn("api-gateway", kql_arg)

    def test_soc_timeline_dispatches(self):
        text, err = bm.handle_call("soc_timeline", {"service": "api-gateway"})
        self.assertFalse(err)
        kql_arg = self.calls[-1][3]
        self.assertIn("api-gateway", kql_arg)

    def test_sre_service_health_rejects_bad_service(self):
        _, err = bm.handle_call("sre_service_health", {"service": "bad name!"})
        self.assertTrue(err)

    def test_soc_timeline_rejects_bad_service(self):
        _, err = bm.handle_call("soc_timeline", {"service": "bad name!"})
        self.assertTrue(err)

    def test_trace_find_slow_callable(self):
        text, err = bm.handle_call("trace_find_slow", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_TRACE_FIND_SLOW)
        self.assertEqual(self.calls[-1][-1], "1h ago")

    def test_trace_find_slow_query_validates_duration(self):
        """DR-007: query must convert duration and filter nulls/negatives."""
        q = bm.Q_TRACE_FIND_SLOW
        self.assertIn("extend dur=toint(duration)", q)
        self.assertIn("where isnotnull(dur)", q)
        self.assertIn("dur >= 0", q)
        self.assertIn("where isnotnull(span_name)", q)
        sort_idx = q.index("sort by dur")
        extend_idx = q.index("extend dur=toint(duration)")
        filter_idx = q.index("where isnotnull(dur)")
        self.assertLess(extend_idx, filter_idx)
        self.assertLess(filter_idx, sort_idx)

    def test_trace_find_errors_callable(self):
        text, err = bm.handle_call("trace_find_errors", {})
        self.assertFalse(err)
        self.assertEqual(self.calls[-1][3], bm.Q_TRACE_FIND_ERRORS)

    def test_trace_analyze_dispatches_both_halves(self):
        text, err = bm.handle_call("trace_analyze", {"trace_id": "abc123"})
        self.assertFalse(err)
        # makes TWO calls: span tree, then correlated logs
        self.assertEqual(len(self.calls), 2)
        self.assertIn("trace_id == 'abc123'", self.calls[0][3])
        self.assertIn("isnotnull(body)", self.calls[1][3])
        self.assertIn("== spans ==", text)
        self.assertIn("== correlated logs ==", text)

    def test_trace_analyze_requires_trace_id(self):
        _, err = bm.handle_call("trace_analyze", {})
        self.assertTrue(err)
        self.assertEqual(self.calls, [])  # must not have shelled out

    def test_trace_analyze_rejects_bad_trace_id(self):
        _, err = bm.handle_call("trace_analyze", {"trace_id": "abc'; drop"})
        self.assertTrue(err)
        self.assertEqual(self.calls, [])  # must not shell out


class ParserFactoryToolsTest(unittest.TestCase):
    """MCP-level wiring for the parser-factory tools (P5): tools/list
    metadata, dispatch, and basic error paths. Pipeline internals are
    covered in tests/test_parser_factory.py."""

    def setUp(self):
        self.calls = []

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return ("OK\n1", False)

        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

    def tearDown(self):
        bm.run_bzrk = self._orig_run
        bm.LEARNED_PATH = self._orig_learned
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        self._tmp.cleanup()

    def test_tools_list_includes_new_tools_with_annotations(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        ann = {t["name"]: t["annotations"] for t in resp["result"]["tools"]}
        for n in ("detect_new_sources", "generate_parser", "run_discovery_worker", "review_generated"):
            self.assertIn(n, ann)
        for n in ("detect_new_sources", "generate_parser", "run_discovery_worker"):
            self.assertTrue(ann[n]["openWorldHint"], n)
            self.assertFalse(ann[n]["readOnlyHint"], n)
        self.assertTrue(ann["review_generated"]["readOnlyHint"])

    def test_generate_parser_rejects_both_service_and_metric(self):
        text, err = bm.handle_call("generate_parser", {"service": "s", "metric": "m"})
        self.assertTrue(err)
        self.assertIn("exactly one", text)

    def test_generate_parser_rejects_neither(self):
        text, err = bm.handle_call("generate_parser", {})
        self.assertTrue(err)

    def test_generate_parser_rejects_invalid_name(self):
        text, err = bm.handle_call("generate_parser", {"service": "bad name!"})
        self.assertTrue(err)

    def test_run_discovery_worker_empty_queue(self):
        text, err = bm.handle_call("run_discovery_worker", {})
        self.assertFalse(err)
        self.assertIn("No pending discovery jobs", text)

    def test_review_generated_lists_only_generated_entries(self):
        bm.save_learned([
            {"name": "manual_q", "description": "human", "kql": "default | take 1"},
            {"name": "gen_q", "description": "auto", "kql": "default | take 1",
             "generated_by": {"provider": "hermes", "model": "m", "ts": "t", "job_source": "x"}},
        ])
        text, err = bm.handle_call("review_generated", {})
        self.assertFalse(err)
        self.assertIn("gen_q", text)
        self.assertNotIn("manual_q", text)

    def test_review_generated_empty(self):
        text, err = bm.handle_call("review_generated", {})
        self.assertFalse(err)
        self.assertIn("No generated queries", text)

    def test_review_generated_by_name(self):
        bm.save_learned([
            {"name": "gen_q", "description": "auto", "kql": "default | take 1",
             "generated_by": {"provider": "hermes", "model": "m", "ts": "t", "job_source": "x"}},
        ])
        text, err = bm.handle_call("review_generated", {"name": "gen_q"})
        self.assertFalse(err)
        self.assertIn("default | take 1", text)

    def test_detect_new_sources_dispatches(self):
        text, err = bm.handle_call("detect_new_sources", {})
        self.assertFalse(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
