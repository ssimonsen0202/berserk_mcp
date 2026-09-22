#!/usr/bin/env python3
"""Tests for evals/run_eval.py. Pure stdlib (unittest); no live Berserk, no
network, no subprocess -- only the pure/easily-mockable functions get unit
tests here, matching this repo's existing convention (test_ci_gate.py,
evals/test_run_eval_usage.py, evals/test_run_eval_multiturn.py) of not
unit-testing the MCP-handshake/subprocess/network glue (get_mcp_tools_and_
instructions, main, _run_tier_policy) -- that's covered by
mcp_protocol_smoke.py-style integration checks instead.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# run_eval.py lives in evals/ with no package __init__.py. `import
# evals.run_eval` works as an implicit namespace-package import as long as
# the repo root is on sys.path (true when running `python3 -m unittest
# tests.test_run_eval` from the repo root, since -m puts the cwd there).
# Fall back to adding evals/ directly to sys.path and importing it as a
# top-level module, matching the convention already used by
# evals/test_run_eval_usage.py and evals/test_run_eval_multiturn.py.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from evals import run_eval  # noqa: E402
except ImportError:
    sys.path.insert(0, str(ROOT / "evals"))
    import run_eval  # noqa: E402


# ---------- to_openai_tools / to_anthropic_tools ----------
class ToOpenaiToolsTest(unittest.TestCase):
    def test_wraps_each_tool_in_type_function_shape(self):
        tools = [{"name": "top_cpu", "description": "Top CPU containers", "inputSchema": {"type": "object"}}]
        out = run_eval.to_openai_tools(tools)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["function"]["name"], "top_cpu")
        self.assertEqual(out[0]["function"]["description"], "Top CPU containers")
        self.assertEqual(out[0]["function"]["parameters"], {"type": "object"})

    def test_input_schema_maps_to_parameters_key(self):
        tools = [{"name": "x", "description": "d", "inputSchema": {"properties": {"since": {"type": "string"}}}}]
        out = run_eval.to_openai_tools(tools)
        self.assertIn("properties", out[0]["function"]["parameters"])
        self.assertNotIn("inputSchema", out[0]["function"])

    def test_preserves_order_and_count_across_multiple_tools(self):
        tools = [
            {"name": "a", "description": "d1", "inputSchema": {}},
            {"name": "b", "description": "d2", "inputSchema": {}},
            {"name": "c", "description": "d3", "inputSchema": {}},
        ]
        out = run_eval.to_openai_tools(tools)
        self.assertEqual([t["function"]["name"] for t in out], ["a", "b", "c"])

    def test_empty_tools_list_gives_empty_result(self):
        self.assertEqual(run_eval.to_openai_tools([]), [])


class ToAnthropicToolsTest(unittest.TestCase):
    def test_maps_input_schema_to_input_schema_key(self):
        tools = [{"name": "top_cpu", "description": "Top CPU containers", "inputSchema": {"type": "object"}}]
        out = run_eval.to_anthropic_tools(tools)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "top_cpu")
        self.assertEqual(out[0]["description"], "Top CPU containers")
        self.assertEqual(out[0]["input_schema"], {"type": "object"})
        self.assertNotIn("type", out[0])  # no OpenAI-style function wrapper
        self.assertNotIn("inputSchema", out[0])

    def test_empty_tools_list_gives_empty_result(self):
        self.assertEqual(run_eval.to_anthropic_tools([]), [])

    def test_preserves_order_across_multiple_tools(self):
        tools = [
            {"name": "a", "description": "d1", "inputSchema": {}},
            {"name": "b", "description": "d2", "inputSchema": {}},
        ]
        out = run_eval.to_anthropic_tools(tools)
        self.assertEqual([t["name"] for t in out], ["a", "b"])


# ---------- call_mock / _mock_route ----------
class MockRouteTest(unittest.TestCase):
    """Representative keyword-routing cases for the mock backend's dumb
    router. Each prompt is fed through call_mock() end-to-end (not
    _mock_route directly) so the host/vm/machine/node detection in
    call_mock is exercised too, matching how the real eval harness calls it."""

    def _route(self, prompt):
        name, args, latency, usage = run_eval.call_mock(prompt, tools=[])
        return name

    def test_exact_phrase_search_routes_to_search(self):
        self.assertEqual(self._route("search for exact phrase in logs"), "search")

    def test_show_logs_for_service_routes_to_logs_for_service(self):
        self.assertEqual(self._route("show me logs for the auth service"), "logs_for_service")

    def test_similar_errors_routes_to_find_similar(self):
        self.assertEqual(self._route("find similar errors to connection timeout"), "find_similar")

    def test_cpu_with_host_keyword_routes_to_host_cpu(self):
        self.assertEqual(self._route("what's the cpu usage on host web-01"), "host_cpu")

    def test_cpu_without_host_keyword_routes_to_top_cpu(self):
        self.assertEqual(self._route("what's the cpu usage"), "top_cpu")

    def test_list_all_services_routes_to_list_services(self):
        self.assertEqual(self._route("list all services"), "list_services")

    def test_claude_loop_check_routes_to_claude_loop_check(self):
        self.assertEqual(self._route("claude code loop check"), "claude_loop_check")

    def test_claude_errors_routes_to_claude_errors(self):
        self.assertEqual(self._route("claude code errors"), "claude_errors")

    def test_service_healthy_routes_to_sre_service_health(self):
        self.assertEqual(self._route("is the payment service healthy"), "sre_service_health")

    def test_detect_anomalies_routes_to_detect_anomalies(self):
        self.assertEqual(self._route("detect anomalies in traffic"), "detect_anomalies")

    def test_forecast_capacity_routes_to_forecast_capacity(self):
        self.assertEqual(self._route("forecast capacity trends"), "forecast_capacity")

    def test_save_query_routes_to_save_query(self):
        self.assertEqual(self._route("save this query"), "save_query")

    def test_list_saved_queries_routes_to_list_saved(self):
        self.assertEqual(self._route("what saved queries do I have"), "list_saved")

    def test_schema_of_logs_table_routes_to_schema(self):
        self.assertEqual(self._route("schema of the logs table"), "schema")

    def test_returns_four_tuple_shape_with_empty_args_zero_latency_empty_usage(self):
        result = run_eval.call_mock("list all services", tools=[])
        self.assertEqual(len(result), 4)
        name, args, latency, usage = result
        self.assertIsInstance(name, str)
        self.assertEqual(args, {})
        self.assertEqual(latency, 0.0)
        self.assertEqual(usage, {})

    def test_host_vm_machine_node_all_trigger_host_scoped_metric(self):
        for word in ("host", "vm", "machine", "node"):
            with self.subTest(word=word):
                self.assertEqual(self._route(f"cpu usage on {word} foo-1"), "host_cpu")

    def test_claude_loop_check_with_session_digit_escalates_to_session_deep_dive(self):
        # _mock_route: cc-agent + "loop" + any digit in the prompt routes to
        # claude_session_deep_dive instead of claude_loop_check.
        self.assertEqual(self._route("claude code loop check for session 12345"), "claude_session_deep_dive")

    # One prompt per _MOCK_ROUTES entry, in table order, with the tool the original
    # if/elif chain returned. The mock is the CI gate's baseline, so every entry is pinned.
    ROUTE_CASES = [
        ("search the exact phrase timeout", "search"),
        ("show me logs for the auth service", "logs_for_service"),
        ("forecast disk usage", "forecast_capacity"),
        ("find similar messages", "find_similar"),
        ("anything abnormal", "detect_anomalies"),
        ("list saved queries", "list_saved"),
        ("save this", "save_query"),
        ("run this kql", "search"),
        ("what tables exist", "schema"),
        ("cost per successful outcome", "claude_efficiency_insights"),
        ("token burn", "claude_token_burn"),
        ("which tool breaks most", "claude_workflow_insights"),
        ("claude hotspot", "claude_workflow_insights"),
        ("claude loop", "claude_loop_check"),
        ("claude error", "claude_errors"),
        ("claude tool use", "claude_tools"),
        ("claude session", "claude_sessions"),
        ("claude search timeout", "claude_search"),
        ("what did codex do", "claude_recent"),
        ("is checkout healthy", "sre_service_health"),
        ("nginx log", "logs_for_service"),
        ("root cause of errors", "investigate_error_rate"),
        ("are errors climbing", "sre_error_rate"),
        ("any error", "errors_by_service"),
        ("cpu", "top_cpu"),
        ("memory", "top_memory"),
        ("list service names", "list_services"),
        ("which host", "list_hosts"),
        ("hello", "list_containers"),
    ]

    def _matched_index(self, prompt):
        p = prompt.lower()
        host = any(w in p for w in ("host", "vm", "machine", "node"))
        cc = "claude" in p or "codex" in p
        for i, (condition, _result) in enumerate(run_eval._MOCK_ROUTES):
            if condition(p, host, cc):
                return i
        return None

    def test_every_mock_route_is_reached_with_its_original_result(self):
        self.assertEqual(len(self.ROUTE_CASES), len(run_eval._MOCK_ROUTES))
        for index, (prompt, expected) in enumerate(self.ROUTE_CASES):
            with self.subTest(route=index, prompt=prompt):
                self.assertEqual(self._matched_index(prompt), index)
                self.assertEqual(self._route(prompt), expected)


# ---------- score_case ----------
class ScoreCaseTest(unittest.TestCase):
    def test_correct_tool_and_correct_args_scores_both_true(self):
        case = {"expect_tool": "top_cpu", "expect_args": {"since": "1h ago"}}
        tool_ok, arg_ok = run_eval.score_case(case, "top_cpu", {"since": "1h ago"})
        self.assertTrue(tool_ok)
        self.assertTrue(arg_ok)

    def test_wrong_tool_scores_tool_false(self):
        case = {"expect_tool": "top_cpu", "expect_args": {}}
        tool_ok, arg_ok = run_eval.score_case(case, "top_memory", {})
        self.assertFalse(tool_ok)
        self.assertTrue(arg_ok)

    def test_correct_tool_wrong_arg_value_scores_arg_false(self):
        case = {"expect_tool": "logs_for_service", "expect_args": {"service": "checkout"}}
        tool_ok, arg_ok = run_eval.score_case(case, "logs_for_service", {"service": "auth"})
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

    def test_missing_expected_arg_scores_arg_false(self):
        case = {"expect_tool": "logs_for_service", "expect_args": {"service": "checkout"}}
        tool_ok, arg_ok = run_eval.score_case(case, "logs_for_service", {})
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

    def test_expect_since_any_matches_when_since_contains_one_option(self):
        case = {"expect_tool": "top_cpu", "expect_since_any": ["1h", "60m"]}
        tool_ok, arg_ok = run_eval.score_case(case, "top_cpu", {"since": "1h ago"})
        self.assertTrue(tool_ok)
        self.assertTrue(arg_ok)

    def test_expect_since_any_fails_when_since_matches_none(self):
        case = {"expect_tool": "top_cpu", "expect_since_any": ["1h", "60m"]}
        tool_ok, arg_ok = run_eval.score_case(case, "top_cpu", {"since": "2h ago"})
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

    def test_expect_since_any_fails_when_since_is_missing(self):
        case = {"expect_tool": "top_cpu", "expect_since_any": ["1h"]}
        tool_ok, arg_ok = run_eval.score_case(case, "top_cpu", {})
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

    def test_arg_comparison_is_case_and_whitespace_insensitive(self):
        case = {"expect_tool": "logs_for_service", "expect_args": {"service": " Checkout "}}
        tool_ok, arg_ok = run_eval.score_case(case, "logs_for_service", {"service": "checkout"})
        self.assertTrue(arg_ok)

    def test_no_expect_args_or_since_any_scores_arg_true_by_default(self):
        case = {"expect_tool": "list_services"}
        tool_ok, arg_ok = run_eval.score_case(case, "list_services", {})
        self.assertTrue(tool_ok)
        self.assertTrue(arg_ok)

    def test_none_tool_name_fails_tool_scoring(self):
        case = {"expect_tool": "top_cpu", "expect_args": {}}
        tool_ok, arg_ok = run_eval.score_case(case, None, {})
        self.assertFalse(tool_ok)


# ---------- usage_fields ----------
class UsageFieldsTest(unittest.TestCase):
    def test_openai_style_keys(self):
        f = run_eval.usage_fields({"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(f["prompt_tokens"], 100)
        self.assertEqual(f["completion_tokens"], 20)

    def test_anthropic_style_keys(self):
        f = run_eval.usage_fields({"input_tokens": 55, "output_tokens": 7})
        self.assertEqual(f["prompt_tokens"], 55)
        self.assertEqual(f["completion_tokens"], 7)

    def test_empty_usage_gives_zero_tokens_and_none_cost(self):
        f = run_eval.usage_fields({})
        self.assertEqual(f["prompt_tokens"], 0)
        self.assertEqual(f["completion_tokens"], 0)
        self.assertIsNone(f["cost_usd"])
        self.assertIsNone(f["cached_tokens"])

    def test_none_usage_gives_zero_tokens_and_none_cost(self):
        f = run_eval.usage_fields(None)
        self.assertEqual(f["prompt_tokens"], 0)
        self.assertEqual(f["completion_tokens"], 0)
        self.assertIsNone(f["cost_usd"])
        self.assertIsNone(f["cached_tokens"])

    def test_cost_field_extracted(self):
        f = run_eval.usage_fields({"cost": 0.0042})
        self.assertEqual(f["cost_usd"], 0.0042)

    def test_cached_tokens_extracted_from_nested_details(self):
        f = run_eval.usage_fields({"prompt_tokens_details": {"cached_tokens": 900}})
        self.assertEqual(f["cached_tokens"], 900)


# ---------- aggregate_usage ----------
class AggregateUsageTest(unittest.TestCase):
    def test_sums_tokens_across_rows(self):
        rows = [
            {"prompt_tokens": 10, "completion_tokens": 1},
            {"prompt_tokens": 20, "completion_tokens": 2},
        ]
        agg = run_eval.aggregate_usage(rows)
        self.assertEqual(agg["total_input_tokens"], 30)
        self.assertEqual(agg["total_output_tokens"], 3)

    def test_total_cost_is_none_when_no_row_has_cost(self):
        rows = [{"prompt_tokens": 10, "completion_tokens": 1, "cost_usd": None}]
        agg = run_eval.aggregate_usage(rows)
        self.assertIsNone(agg["total_cost_usd"])

    def test_total_cost_is_sum_when_rows_have_cost(self):
        rows = [
            {"prompt_tokens": 10, "completion_tokens": 1, "cost_usd": 0.01},
            {"prompt_tokens": 20, "completion_tokens": 2, "cost_usd": 0.02},
        ]
        agg = run_eval.aggregate_usage(rows)
        self.assertAlmostEqual(agg["total_cost_usd"], 0.03)

    def test_empty_rows_gives_zero_tokens_and_none_cost(self):
        agg = run_eval.aggregate_usage([])
        self.assertEqual(agg["total_input_tokens"], 0)
        self.assertEqual(agg["total_output_tokens"], 0)
        self.assertIsNone(agg["total_cost_usd"])


# ---------- build_multi_turn_messages ----------
class BuildMultiTurnMessagesTest(unittest.TestCase):
    def test_anthropic_format_has_tool_use_and_tool_result_blocks(self):
        messages = run_eval.build_multi_turn_messages(True, "why did errors spike?", "HOP1")
        self.assertEqual(messages[0], {"role": "user", "content": "why did errors spike?"})
        self.assertEqual(messages[1]["role"], "assistant")
        tool_use = messages[1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["name"], "investigate_error_rate")
        self.assertEqual(tool_use["input"], {})
        result_block = messages[2]["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertEqual(result_block["tool_use_id"], tool_use["id"])
        self.assertEqual(result_block["content"], "HOP1")

    def test_openai_format_has_tool_calls_and_tool_role(self):
        messages = run_eval.build_multi_turn_messages(False, "why did errors spike?", "HOP1")
        self.assertEqual(messages[0], {"role": "user", "content": "why did errors spike?"})
        self.assertEqual(messages[1]["role"], "assistant")
        call = messages[1]["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "investigate_error_rate")
        self.assertEqual(call["function"]["arguments"], "{}")
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": call["id"], "content": "HOP1"})

    def test_both_formats_carry_the_original_prompt_verbatim(self):
        prompt = "is checkout healthy?"
        anthropic_messages = run_eval.build_multi_turn_messages(True, prompt, "x")
        openai_messages = run_eval.build_multi_turn_messages(False, prompt, "x")
        self.assertEqual(anthropic_messages[0]["content"], prompt)
        self.assertEqual(openai_messages[0]["content"], prompt)


# ---------- append_to_ledger ----------
class AppendToLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._orig_ledger_path = run_eval.LEDGER_PATH
        self.ledger_path = Path(self._tmpdir.name) / "run_ledger.jsonl"
        run_eval.LEDGER_PATH = self.ledger_path
        self.addCleanup(self._restore_ledger_path)

        cases_dir = Path(self._tmpdir.name)
        self.cases_path = cases_dir / "cases.jsonl"
        self.cases_path.write_text('{"id": "c1"}\n', encoding="utf-8")

    def _restore_ledger_path(self):
        run_eval.LEDGER_PATH = self._orig_ledger_path

    def test_mock_backend_is_skipped_no_file_written(self):
        run_eval.append_to_ledger(
            backend="mock",
            model="",
            cases_path=str(self.cases_path),
            tool_count=5,
            rows=[],
            tool_accuracy=1.0,
            arg_accuracy=1.0,
            agg={"total_cost_usd": None},
            lat=[],
        )
        self.assertFalse(self.ledger_path.exists())

    def test_real_backend_writes_jsonl_line_with_expected_fields(self):
        rows = [
            {"id": "c1", "expect": "top_cpu", "got": "top_cpu", "tool_ok": True, "arg_ok": True},
            {"id": "c2", "expect": "top_memory", "got": "top_cpu", "tool_ok": False, "arg_ok": False},
        ]
        run_eval.append_to_ledger(
            backend="openai",
            model="gpt-4o",
            cases_path=str(self.cases_path),
            tool_count=42,
            rows=rows,
            tool_accuracy=0.5,
            arg_accuracy=0.5,
            agg={"total_cost_usd": 0.0123},
            lat=[0.1, 0.2],
        )
        self.assertTrue(self.ledger_path.exists())
        lines = self.ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["backend"], "openai")
        self.assertEqual(record["model"], "gpt-4o")
        self.assertEqual(record["cases_file"], "cases.jsonl")
        self.assertEqual(record["case_count"], 2)
        self.assertEqual(record["tool_count"], 42)
        self.assertEqual(record["tool_accuracy"], 0.5)
        self.assertEqual(record["arg_accuracy"], 0.5)
        self.assertEqual(record["total_cost_usd"], 0.0123)
        # Only the miss (tool_ok=False) row is distilled into "misses".
        self.assertEqual(len(record["misses"]), 1)
        self.assertEqual(record["misses"][0], {"id": "c2", "expect": "top_memory", "got": "top_cpu"})
        self.assertIn("case_set_version", record)
        self.assertIn("ts", record)

    def test_multiple_calls_append_rather_than_overwrite(self):
        common = dict(
            backend="anthropic",
            model="claude-haiku-4-5",
            cases_path=str(self.cases_path),
            tool_count=10,
            rows=[],
            tool_accuracy=1.0,
            arg_accuracy=1.0,
            agg={"total_cost_usd": None},
            lat=[],
        )
        run_eval.append_to_ledger(**common)
        run_eval.append_to_ledger(**common)
        lines = self.ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_latency_median_ms_computed_from_lat_list(self):
        run_eval.append_to_ledger(
            backend="openai",
            model="gpt-4o",
            cases_path=str(self.cases_path),
            tool_count=1,
            rows=[],
            tool_accuracy=1.0,
            arg_accuracy=1.0,
            agg={"total_cost_usd": None},
            lat=[0.1, 0.2, 0.3],
        )
        record = json.loads(self.ledger_path.read_text(encoding="utf-8").strip())
        self.assertEqual(record["latency_median_ms"], 200)

    def test_empty_lat_gives_none_latency_median(self):
        run_eval.append_to_ledger(
            backend="openai",
            model="gpt-4o",
            cases_path=str(self.cases_path),
            tool_count=1,
            rows=[],
            tool_accuracy=1.0,
            arg_accuracy=1.0,
            agg={"total_cost_usd": None},
            lat=[],
        )
        record = json.loads(self.ledger_path.read_text(encoding="utf-8").strip())
        self.assertIsNone(record["latency_median_ms"])


class PromptCachingTest(unittest.TestCase):
    TOOLS = [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]

    def _capture(self, call):
        sent = {}

        def fake_post(url, headers, body, timeout=120):
            sent["body"] = body
            if "anthropic.com" in url:
                return {"content": [], "usage": {}}, 0.0
            return {"choices": [{"message": {}}], "usage": {}}, 0.0

        orig = run_eval._post
        run_eval._post = fake_post
        try:
            call()
        finally:
            run_eval._post = orig
        return sent["body"]

    OPENROUTER = "https://openrouter.ai/api/v1"

    def _openai(self, model, base_url=OPENROUTER):
        return self._capture(
            lambda: run_eval.call_openai_compatible(base_url, "", model, "SYS", "question", self.TOOLS, "auto")
        )

    def test_openrouter_anthropic_model_marks_system_as_cache_breakpoint(self):
        for model in ("anthropic/claude-haiku-4.5", "~anthropic/claude-haiku-latest"):
            with self.subTest(model=model):
                system, user = self._openai(model)["messages"]
                self.assertEqual(
                    system["content"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
                )
                self.assertEqual(user["content"], "question")

    def test_non_anthropic_models_keep_plain_string_system(self):
        for model in ("deepseek/deepseek-v4.1-flash", "qwen2.5:7b", "gpt-4o"):
            with self.subTest(model=model):
                self.assertEqual(self._openai(model)["messages"][0]["content"], "SYS")

    def test_anthropic_model_on_other_openai_compatible_endpoint_is_not_marked(self):
        for base_url in (
            "http://127.0.0.1:4000/v1",
            "https://openrouter.ai.evil.example/v1",
            "https://api.openai.com/v1",
        ):
            with self.subTest(base_url=base_url):
                body = self._openai("anthropic/claude-haiku-4.5", base_url)
                self.assertEqual(body["messages"][0]["content"], "SYS")

    def test_multi_turn_caches_system_without_mutating_prior_messages(self):
        prior = [{"role": "user", "content": "q"}]
        body = self._capture(
            lambda: run_eval.call_openai_compatible_multi_turn(
                self.OPENROUTER, "", "anthropic/claude-haiku-4.5", "SYS", prior, self.TOOLS, "auto"
            )
        )
        self.assertIn("cache_control", body["messages"][0]["content"][0])
        self.assertEqual(prior, [{"role": "user", "content": "q"}])

    def test_direct_anthropic_marks_system_as_cache_breakpoint(self):
        body = self._capture(
            lambda: run_eval.call_anthropic("k", "claude-haiku-4-5", "SYS", "question", self.TOOLS, {"type": "any"})
        )
        self.assertEqual(body["system"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])

    def test_usage_fields_counts_anthropic_cache_reads_and_writes(self):
        f = run_eval.usage_fields(
            {"input_tokens": 40, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 0, "output_tokens": 9}
        )
        self.assertEqual(f["prompt_tokens"], 30040)
        self.assertEqual(f["cached_tokens"], 30000)
        self.assertEqual(f["completion_tokens"], 9)

    def test_usage_fields_anthropic_first_call_counts_cache_write_as_input(self):
        f = run_eval.usage_fields({"input_tokens": 40, "cache_creation_input_tokens": 30000, "output_tokens": 9})
        self.assertEqual(f["prompt_tokens"], 30040)
        self.assertIsNone(f["cached_tokens"])


if __name__ == "__main__":
    unittest.main()
