"""Lane-aware scoring: a case whose expected tool the server under test does
not serve (hidden by role or tier) is not applicable, not a routing miss."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_eval  # noqa: E402


class ApplicableTest(unittest.TestCase):
    def test_applicable_checks_the_served_tool_set(self):
        self.assertTrue(run_eval.applicable({"expect_tool": "top_cpu"}, {"top_cpu", "host_cpu"}))
        self.assertFalse(run_eval.applicable({"expect_tool": "soc_timeline"}, {"top_cpu"}))


class SplitApplicableTest(unittest.TestCase):
    # main() and --tier-policy both call this helper. --tier-policy itself
    # needs two real model backends, so it is not run offline.
    def test_split_keeps_order_and_reports_ids(self):
        cases = [
            {"id": "a", "expect_tool": "top_cpu"},
            {"id": "b", "expect_tool": "soc_timeline"},
            {"id": "c", "expect_tool": "host_cpu"},
        ]
        scored, skipped = run_eval.split_applicable(cases, [{"name": "top_cpu"}, {"name": "host_cpu"}])
        self.assertEqual([c["id"] for c in scored], ["a", "c"])
        self.assertEqual(skipped, ["b"])


class ResolveCaseTest(unittest.TestCase):
    CASE = {
        "id": "t",
        "expect_tool": "search",
        "expect_args": {"kql": "x"},
        "also_accept": ["saved__q"],
        "expect_tool_when_hidden": "saved__q",
    }

    def test_expected_tool_served_is_scored_as_is(self):
        self.assertIs(run_eval.resolve_case(self.CASE, {"search", "saved__q"}), self.CASE)

    def test_hidden_expected_tool_falls_back(self):
        resolved = run_eval.resolve_case(self.CASE, {"saved__q"})
        self.assertEqual(resolved["expect_tool"], "saved__q")
        self.assertNotIn("expect_args", resolved)

    def test_no_served_answer_is_not_applicable(self):
        self.assertIsNone(run_eval.resolve_case(self.CASE, {"top_cpu"}))

    def test_also_accept_counts_as_correct(self):
        self.assertTrue(run_eval.score_case(self.CASE, "saved__q", {})[0])
        self.assertFalse(run_eval.score_case(self.CASE, "top_cpu", {})[0])


class LaneRunTest(unittest.TestCase):
    def _invoke(self, role, cases_path, tier="", extra=()):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ)
        for key in [k for k in env if k.startswith(("BERSERK_MCP_", "BERSERK_LLM_"))]:
            del env[key]
        env.update({"HOME": tmp, "USERPROFILE": tmp, "BERSERK_MCP_ROLE": role, "BERSERK_MCP_TIER": tier})
        out = Path(tmp) / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                str(HERE / "run_eval.py"),
                "--backend",
                "mock",
                "--out",
                str(out),
                *extra,
                str(cases_path),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        return result, out

    def _run(self, role, tier=""):
        result, out = self._invoke(role, HERE / "router_cases_confusable.jsonl", tier)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        return result.stdout, json.loads(out.read_text())

    def test_small_tier_hides_deep_tools_from_scoring(self):
        # `search` is deep-tier only; top_cpu is served at both tiers.
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "router_cases_tiers.jsonl"
            cases.write_text(
                json.dumps({"id": "kql", "prompt": "run custom KQL", "expect_tool": "search"})
                + "\n"
                + json.dumps({"id": "cpu", "prompt": "top containers by CPU", "expect_tool": "top_cpu"})
                + "\n"
            )
            small, small_out = self._invoke("ops", cases, tier="small")
            deep, deep_out = self._invoke("ops", cases, tier="deep")
            self.assertEqual(small.returncode, 0, small.stderr[-1000:])
            self.assertEqual(deep.returncode, 0, deep.stderr[-1000:])
            self.assertEqual(json.loads(small_out.read_text())["not_applicable"], ["kql"])
            self.assertEqual(json.loads(deep_out.read_text())["not_applicable"], [])

    def test_tier_specific_answer_uses_the_saved_query_fixture(self):
        cases = HERE / "router_cases_tiered.jsonl"
        fixture = ("--saved-queries", str(HERE / "fixtures" / "saved_queries.json"))
        small, small_out = self._invoke("ops", cases, tier="small", extra=fixture)
        deep, deep_out = self._invoke("ops", cases, tier="deep", extra=fixture)
        bare, bare_out = self._invoke("ops", cases, tier="small")
        self.assertEqual(small.returncode, 0, small.stderr[-800:])
        self.assertEqual(deep.returncode, 0, deep.stderr[-800:])
        self.assertNotEqual(bare.returncode, 0)
        small_rows = json.loads(small_out.read_text())["rows"]
        deep_rows = json.loads(deep_out.read_text())["rows"]
        self.assertEqual(
            {r["expect"] for r in small_rows},
            {"saved__nginx_5xx_by_path", "saved__postgres_slow_statements", "saved__failed_logins_by_user"},
        )
        self.assertEqual({r["expect"] for r in deep_rows}, {"search"})
        # Without the fixture the small tier serves neither answer.
        self.assertIn("no applicable cases", bare.stderr + bare.stdout)

    def test_no_applicable_cases_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            only_hidden = Path(tmp) / "router_cases_hidden.jsonl"
            only_hidden.write_text(json.dumps({"id": "x", "prompt": "timeline", "expect_tool": "soc_timeline"}) + "\n")
            result, _ = self._invoke("ops", only_hidden)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no applicable cases", result.stderr + result.stdout)

    def test_ops_lane_marks_other_lanes_cases_not_applicable(self):
        stdout, report = self._run("ops")
        self.assertIn("soc_timeline", " ".join(report["not_applicable"]) + stdout)
        self.assertIn("conf_timeline", report["not_applicable"])
        self.assertNotIn("conf_cpu_box", report["not_applicable"])
        scored = {row["id"] for row in report["rows"]}
        self.assertFalse(scored & set(report["not_applicable"]))
        self.assertIn("not applicable", stdout)

    def test_all_role_skips_nothing(self):
        _, report = self._run("all")
        self.assertEqual(report["not_applicable"], [])
        self.assertEqual(len(report["rows"]), 21)


if __name__ == "__main__":
    unittest.main()
