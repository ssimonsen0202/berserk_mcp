import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402
import ingestion_advisor as ia  # noqa: E402


class IngestionCatalogTest(unittest.TestCase):
    def test_catalog_is_pure_json_and_every_source_has_required_shape(self):
        path = Path(__file__).resolve().parent.parent / "ingestion_catalog.json"
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        catalog = ia.validate_catalog(raw)
        self.assertIn("sre/onprem-ad-health", catalog)
        self.assertIn("soc/endpoint-identity", catalog)
        self.assertIn("change-management/ansible", catalog)
        for sources in catalog.values():
            for source in sources:
                self.assertEqual(set(source) & ia.REQUIRED_SOURCE_KEYS, ia.REQUIRED_SOURCE_KEYS)
                self.assertIn(source["maturity"], ia.MATURITIES)

    def test_invalid_catalog_is_rejected(self):
        with self.assertRaises(ValueError):
            ia.validate_catalog({"bad": [{"name": "incomplete"}]})


class IngestionAdvisorPureTest(unittest.TestCase):
    def setUp(self):
        self.orig_services = ia._list_services
        self.orig_metrics = ia._list_metrics
        ia.configure(
            list_services=lambda since: ("cloudtrail 20", False),
            list_metrics=lambda since: ("system.cpu.load_average.1m 100", False),
            catalog_path=Path(__file__).resolve().parent.parent / "ingestion_catalog.json",
        )

    def tearDown(self):
        ia._list_services = self.orig_services
        ia._list_metrics = self.orig_metrics
        ia._catalog_path = None

    def test_ad_health_recommends_channels_and_windowseventlog(self):
        text, err = ia.suggest_ingestion("sre/onprem-ad-health")
        self.assertFalse(err)
        self.assertIn("Domain Controller Security events", text)
        self.assertIn("System and Directory Service", text)
        self.assertIn("windowseventlog", text)
        self.assertIn("4768/4769/4771", text)

    def test_gap_marks_cloudtrail_present_and_windows_missing(self):
        text, err = ia.suggest_ingestion("soc/endpoint-identity", check_gap=True)
        self.assertFalse(err)
        self.assertIn("[present] AWS CloudTrail", text)
        self.assertIn("Matched: service:cloudtrail", text)
        self.assertIn("[missing] Windows Security and identity events", text)
        self.assertIn("windowseventlog", text)
        self.assertIn("Gap summary: 1 present, 3 missing.", text)

    def test_unknown_key_lists_available_keys(self):
        text, err = ia.suggest_ingestion("does/not-exist")
        self.assertFalse(err)
        self.assertIn("Available:", text)
        self.assertIn("sre/aws-cloud-native", text)
        self.assertIn("scom", text)

    def test_inventory_failure_is_reported_without_hiding_recommendations(self):
        ia.configure(
            list_services=lambda since: ("service query failed", True),
            list_metrics=lambda since: ("metric query failed", True),
            catalog_path=Path(__file__).resolve().parent.parent / "ingestion_catalog.json",
        )
        text, err = ia.suggest_ingestion("change-management/ansible", check_gap=True)
        self.assertFalse(err)
        self.assertIn("Gap check incomplete; failed inventory: services, metrics", text)
        self.assertIn("community.general.opentelemetry", text)


class IngestionAdvisorMcpTest(unittest.TestCase):
    def setUp(self):
        self.orig_run = bm.run_bzrk
        self.orig_mode = bm.REDACT_MODE
        self.calls = []
        bm.REDACT_MODE = "off"

        def fake_run(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            kql = args[3]
            if "summarize total=count()" in kql:
                return "cloudtrail 20\nnginx 10", False
            if "where isnotnull(metric_name)" in kql:
                return "system.cpu.load_average.1m 100", False
            return "unexpected query", True

        bm.run_bzrk = fake_run

    def tearDown(self):
        bm.run_bzrk = self.orig_run
        bm.REDACT_MODE = self.orig_mode

    def test_mcp_gap_check_runs_service_and_metric_inventories(self):
        text, err = bm.handle_call("suggest_ingestion", {
            "role_or_usecase": "soc/endpoint-identity", "check_gap": True,
        })
        self.assertFalse(err)
        self.assertIn("[present] AWS CloudTrail", text)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("summarize total=count()" in call[3] for call in self.calls))
        self.assertTrue(any("where isnotnull(metric_name)" in call[3] for call in self.calls))

    def test_invalid_arguments_do_not_query_berserk(self):
        for arguments, expected in (
            ({}, "role_or_usecase"),
            ({"role_or_usecase": "sre/azure", "check_gap": "yes"}, "check_gap"),
            ({"role_or_usecase": "sre/azure", "since": "bad; value"}, "invalid 'since'"),
        ):
            with self.subTest(arguments=arguments):
                text, err = bm.handle_call("suggest_ingestion", arguments)
                self.assertTrue(err)
                self.assertIn(expected, text)
        self.assertEqual(self.calls, [])

    def test_tool_is_visible_in_every_role(self):
        original_role = bm.ACTIVE_ROLE
        try:
            for role in ("all", "sre", "soc", "claude", "ops"):
                with self.subTest(role=role):
                    bm.ACTIVE_ROLE = role
                    response = bm.dispatch({
                        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    })
                    names = {tool["name"] for tool in response["result"]["tools"]}
                    self.assertIn("suggest_ingestion", names)
        finally:
            bm.ACTIVE_ROLE = original_role


if __name__ == "__main__":
    unittest.main(verbosity=2)
