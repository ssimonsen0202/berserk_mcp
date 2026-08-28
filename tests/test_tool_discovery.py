#!/usr/bin/env python3
"""Tests for tool_discovery.py (issue #14). The recall gate at the bottom is
the acceptance bar the issue itself specifies as hard: >=99% at K<=5, because
per the market brief's own math, a discovery hop below that makes
per-question accuracy *worse* than today's single hop (95% recall x 95%
selection = 90%, worse than one call at 95%). Shipping below this threshold
regresses the product -- this test exists to make that impossible to do by
accident."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tool_discovery as td  # noqa: E402
import berserk_mcp as bm  # noqa: E402


class TokenizeTest(unittest.TestCase):
    def test_lowercases_and_splits_on_non_alnum(self):
        self.assertEqual(td.tokenize("Top CPU-usage!"), ["top", "cpu", "usage"])

    def test_strips_common_suffixes(self):
        self.assertIn("error", td.tokenize("errors"))
        self.assertIn("log", td.tokenize("logging"))

    def test_empty_and_none_are_safe(self):
        self.assertEqual(td.tokenize(""), [])
        self.assertEqual(td.tokenize(None), [])


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tools = [
            {"name": "top_cpu", "description": "Top CPU consuming containers."},
            {"name": "host_cpu", "description": "Per-host CPU utilization."},
            {"name": "list_saved", "description": "List saved queries."},
        ]
        self.index = td.build_index(self.tools)
        self.order = [t["name"] for t in self.tools]

    def test_name_token_match_outranks_description_only_match(self):
        results = td.search(self.index, "cpu", top_k=5, tool_order=self.order)
        names = [n for n, _ in results]
        self.assertIn("top_cpu", names)
        self.assertIn("host_cpu", names)
        self.assertNotIn("list_saved", names)

    def test_no_match_returns_empty(self):
        self.assertEqual(td.search(self.index, "banana", tool_order=self.order), [])

    def test_ties_break_by_tool_order_deterministically(self):
        r1 = td.search(self.index, "saved", tool_order=self.order)
        r2 = td.search(self.index, "saved", tool_order=self.order)
        self.assertEqual(r1, r2)

    def test_empty_query_returns_empty(self):
        self.assertEqual(td.search(self.index, "", tool_order=self.order), [])


class RecallGateTest(unittest.TestCase):
    """The hard gate. PHRASINGS covers every currently-shipped tool with
    natural-language ways an engineer or agent might actually ask for it."""

    @classmethod
    def setUpClass(cls):
        cls.tools = bm.TOOLS + bm.MGMT_TOOLS
        cls.tool_names = {t["name"] for t in cls.tools}
        cls.index = td.build_index(cls.tools)
        cls.order = [t["name"] for t in cls.tools]

    def test_every_phrasing_recalls_its_tool_in_top5(self):
        misses = []
        total = 0
        for tool_name, phrasings in PHRASINGS.items():
            self.assertIn(tool_name, self.tool_names, f"{tool_name} in PHRASINGS is not a real tool -- stale fixture")
            for phrase in phrasings:
                total += 1
                results = td.search(self.index, phrase, top_k=5, tool_order=self.order)
                names = [n for n, _ in results]
                if tool_name not in names:
                    misses.append((tool_name, phrase, names))

        recall = 1 - (len(misses) / total) if total else 0
        detail = "\n".join(f"  {t!r} <- {p!r} -> got {got}" for t, p, got in misses[:25])
        self.assertGreaterEqual(
            recall, 0.99,
            f"recall@5 = {recall:.4f} ({len(misses)}/{total} misses), needs >=0.99\n{detail}",
        )

    def test_every_shipped_tool_has_at_least_one_phrasing(self):
        # A tool with zero phrasings can't be recall-tested at all -- this
        # keeps the fixture honest as new tools ship.
        missing = self.tool_names - set(PHRASINGS.keys())
        self.assertEqual(missing, set(), f"tools with no recall-test coverage: {sorted(missing)}")


PHRASINGS = {
    "list_containers": ["what containers are running", "list all containers", "show me the containers"],
    "top_cpu": ["which container is using the most cpu", "top cpu containers", "highest cpu usage container"],
    "top_memory": ["which container is using the most memory", "top memory containers", "highest ram usage container"],
    "errors_by_service": ["which service has the most errors", "error counts by service", "show errors per service"],
    "list_services": ["what services do we have", "list all services", "show me the services"],
    "list_hosts": ["what hosts exist", "list all hosts", "show me the machines"],
    "host_cpu": ["cpu usage on the host", "whole machine cpu utilization", "host level cpu"],
    "host_memory": ["memory usage on the host", "whole machine ram", "host level memory"],
    "container_hosts": ["which host is a container running on", "map containers to hosts"],
    "logs_for_service": ["show me logs for a service", "get logs from checkout service", "tail logs for a service"],
    "schema": ["what fields are available", "show me the schema", "what columns exist in the data"],
    "list_metrics": ["what metrics are available", "list all metrics", "show me the metric names"],
    "bzrk_query_perf": ["how slow is the query engine", "query performance stats", "bzrk query latency"],
    "discover_schema": ["find the real field names for a source", "discover schema for a service", "what are the actual field names"],
    "self_check": ["run a self diagnostic", "check if the server is wired up correctly", "doctor check"],
    "validate_kql": ["check if this kql query is valid", "validate my query syntax", "lint this kusto query"],
    "search": ["run a raw kql query", "search the logs with kql", "run an arbitrary query"],
    "detect_anomalies": ["find anomalies", "detect unusual patterns", "spot outliers in the data"],
    "find_similar": ["find similar log lines", "search by meaning not exact text", "semantic search the logs"],
    "trace_find_slow": ["find slow traces", "which requests were slowest", "show me slow spans"],
    "trace_find_errors": ["find traces with errors", "which requests failed", "show me error traces"],
    "trace_analyze": ["analyze this specific trace", "break down a trace by id", "inspect one trace"],
    "sre_error_rate": ["what is the error rate", "error rate trend", "show error rate over time"],
    "investigate_error_rate": ["investigate why error rate is elevated", "troubleshoot high error rate", "step-by-step error investigation"],
    "forecast_capacity": ["forecast when we run out of capacity", "predict disk usage trend", "capacity forecast"],
    "sre_host_headroom": ["how much headroom does a host have", "host capacity remaining", "spare host capacity"],
    "sre_ingest_health": ["is ingestion healthy", "check ingest pipeline health", "ingestion lag status"],
    "sre_service_health": ["is this service healthy", "overall health of a service", "service health check"],
    "sre_top_error_messages": ["what are the most common error messages", "top recurring errors", "most frequent error text"],
    "soc_high_severity_logs": ["show high severity security logs", "critical severity events", "high severity alerts"],
    "soc_log_spike": ["was there a log volume spike", "sudden increase in logs", "log spike detection"],
    "soc_new_services": ["were any new services seen", "detect a new unexpected service", "new service appeared"],
    "soc_repeated_errors": ["what error keeps repeating", "recurring failures", "errors that keep happening"],
    "soc_timeline": ["build an incident timeline for a service", "reconstruct what happened for a service", "timeline of events"],
    "claude_recent": ["recent claude code activity", "what has claude been doing", "latest claude session events"],
    "claude_sessions": ["claude code sessions summary", "rollup of claude sessions", "list claude sessions"],
    "claude_tools": ["which tools does claude use most", "claude tool usage histogram", "how often is bash used"],
    "claude_errors": ["claude code tool errors", "failed tool calls from claude", "claude error results"],
    "claude_search": ["search claude code messages", "full text search claude sessions", "find text in claude transcripts"],
    "claude_loop_check": ["is claude stuck in a loop", "detect claude retry loops", "loop detector for claude"],
    "claude_model_fit": ["is claude using the wrong model size for the task", "model fit heuristic", "frontier model on trivial work"],
    "claude_token_burn": ["how many tokens is claude burning", "token burn analysis", "which session used the most tokens"],
    "claude_quota_status": ["how much of my 5 hour quota is left", "live claude usage window check", "am I about to hit my rate limit"],
    "claude_cost_report": ["claude code cost report", "daily token cost breakdown", "cost per day for claude"],
    "claude_session_deep_dive": ["deep dive into one claude session", "timeline drilldown for a session", "inspect a single session in detail"],
    "claude_workflow_insights": ["claude workflow patterns", "common tool sequences", "cross session workflow analysis"],
    "claude_spend_overview": ["enterprise claude spend overview", "total ai spend", "native token spend report"],
    "claude_feature_cost": ["cost of building a feature with ai", "feature delivery economics", "ai cost for a specific feature"],
    "claude_project_economics": ["project level ai economics", "budget and ai cost for a project", "codebase cost across features"],
    "claude_efficiency_insights": ["harness efficiency analysis", "cache reuse efficiency", "agent efficiency cohort comparison"],
    "claude_harness_recommendations": ["recommend harness improvements", "evidence backed harness changes", "suggest agent config changes"],
    "claude_record_recommendation_decision": ["record that we approved a recommendation", "log a decision on a harness recommendation", "approve or reject a recommendation"],
    "claude_optimization_impact": ["did the harness change help", "compare before and after harness cohorts", "optimization impact analysis"],
    "claude_management_report": ["management report on ai usage", "portfolio level ai report", "team level cost report"],
    "claude_generate_dashboard": ["generate a dashboard", "make an html dashboard of ai usage", "build a markdown report"],
    "scan_secrets": ["scan for leaked secrets", "check logs for exposed credentials", "find secrets in the data"],
    "suggest_ingestion": ["what telemetry sources should we add", "recommend ingestion sources", "suggest what to ingest for sre"],
    "canonloom_run_pipeline": ["run the canonloom pipeline", "kick off a canonloom run", "process a url through canonloom"],
    "canonloom_list_artifacts": ["list canonloom artifacts", "what artifacts has canonloom produced", "show canonloom outputs"],
    "canonloom_get_artifact": ["get a specific canonloom artifact", "fetch one canonloom artifact by id"],
    "canonloom_freshness_report": ["how fresh is canonloom data", "canonloom freshness report", "is canonloom data stale"],
    "canonloom_run_history": ["canonloom run history", "past canonloom runs", "history of pipeline runs"],
    "list_saved": ["what saved queries exist", "list my saved queries", "show saved query packs"],
    "run_saved": ["run a saved query", "execute a saved query by name", "re-run a saved query"],
    "save_query": ["save this query for later", "persist a kql query", "store a named query"],
    "request_discovery": ["kick off telemetry discovery", "request source discovery", "start discovering new sources"],
    "discovery_status": ["what is the discovery job status", "check discovery progress", "is discovery still running"],
    "detect_new_sources": ["were any new telemetry sources found", "detect new data sources", "check for unknown sources"],
    "generate_parser": ["generate a parser for a new source", "create a log parser automatically"],
    "run_discovery_worker": ["run the discovery worker", "process the discovery queue", "advance discovery jobs"],
    "review_generated": ["review a generated parser", "audit an auto generated parser", "check a generated parser for issues"],
    "find_tool": ["find the right tool for a task", "search for a tool by what I want to do", "which tool should I use"],
}


if __name__ == "__main__":
    unittest.main()
