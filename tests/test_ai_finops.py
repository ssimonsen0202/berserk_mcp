import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ai_finops as af
import berserk_mcp as bm


ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "pricing_catalog.json"


def usage(**overrides):
    row = {
        "day": "2026-07-25",
        "team": "platform",
        "project": "berserk",
        "feature": "FEAT-1",
        "repository": "berserk-mcp",
        "agent": "claude-dev",
        "harness": "h1",
        "model": "claude-sonnet-4-6",
        "events": 10,
        "successes": 8,
        "errors": 0,
        "attempts": 10,
        "tokens_in_sum": 100000,
        "tokens_out_sum": 10000,
        "cache_read_tokens_sum": 20000,
        "cache_creation_tokens_sum": 5000,
        "reported_cost_usd": 1.0,
    }
    row.update(overrides)
    return row


class FinopsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rows = [usage()]

        def search(query, since):
            self.last_query = query
            self.last_since = since
            return json.dumps(self.rows), False

        self.search = search
        af.configure(
            search=search,
            table="default",
            catalog_path=CATALOG,
            business_store_path=self.root / "business.json",
            decision_store_path=self.root / "decisions.json",
            report_dir=self.root / "reports",
        )

    def tearDown(self):
        af.configure(
            search=bm.bzrk_search_json,
            table=bm.TABLE,
            catalog_path=bm.FINOPS_PRICING_CATALOG_PATH,
            business_store_path=bm.FINOPS_BUSINESS_STORE_PATH,
            decision_store_path=bm.FINOPS_DECISION_STORE_PATH,
            report_dir=bm.FINOPS_REPORT_DIR,
            otlp_endpoint=bm.FINOPS_OTLP_ENDPOINT,
            otlp_headers=bm.FINOPS_OTLP_HEADERS,
        )
        self.tmp.cleanup()


class NormalizationAndPricingTest(FinopsTestCase):
    def test_native_otel_row_normalizes(self):
        row = af.normalize_usage_row({
            "timestamp": "2026-07-25T10:00:00Z",
            "attributes": {
                "event.name": "api_request",
                "session.id": "s1",
                "model": "claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 40,
                "success": True,
            },
            "resource": {
                "business.project.id": "p1",
                "business.feature.id": "f1",
                "berserk.agent.profile": "developer",
            },
        })
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["project_id"], "p1")
        self.assertEqual(row["feature_id"], "f1")
        self.assertEqual(row["cache_read_tokens"], 40)
        self.assertEqual(row["token_source"], "exact")
        self.assertEqual(row["native_events"], 1)

    def test_duration_is_not_inferred_as_claude_active_time(self):
        row = af.normalize_usage_row({
            "timestamp": "2026-07-25T10:00:00Z",
            "attributes": {"event.name": "api_request", "duration_ms": 9000},
        })
        self.assertEqual(row["active_seconds"], 0)

    def test_native_and_legacy_overlap_is_deduplicated(self):
        native = {
            "timestamp": "2026-07-25T10:00:00.123Z",
            "attributes": {"event.name": "api_request", "session.id": "s1",
                           "request_id": "req-1", "model": "claude-sonnet-4-6",
                           "input_tokens": 100, "output_tokens": 20},
        }
        legacy = {
            "timestamp": "2026-07-25T10:00:00.123Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 100, "claude.tokens_output": 20},
        }
        rows = af.deduplicate_usage_rows([legacy, native, dict(native)])
        self.assertEqual(rows, [native])

    def test_message_id_content_blocks_collapse_to_one_row(self):
        # Distinct, unrelated mechanism from native/legacy fingerprinting
        # above: Claude Code logs one JSONL row per content block
        # (thinking/text/tool_use) of a response, and every block carries an
        # identical copy of that response's usage. See
        # DUPLICATE_INGESTION_BUG_HANDOFF.md / PROPER_FIX_PLAN.md.
        thinking = {
            "timestamp": "2026-07-25T10:00:00.000Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_id": "msg_A", "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 0, "claude.tokens_output": 200},
        }
        tool_use = {
            "timestamp": "2026-07-25T10:00:00.003Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_id": "msg_A", "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 0, "claude.tokens_output": 200},
        }
        rows = af.deduplicate_usage_rows([thinking, tool_use])
        self.assertEqual(rows, [thinking])

    def test_message_id_different_ids_do_not_collapse(self):
        # Regression test for the wrong (content-fingerprint) design the plan
        # explicitly warns against: two genuinely distinct calls sharing a
        # token count and landing close together must not merge.
        first = {
            "timestamp": "2026-07-25T10:00:00.000Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_id": "msg_C", "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 10, "claude.tokens_output": 20},
        }
        second = {
            "timestamp": "2026-07-25T10:00:01.000Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_id": "msg_D", "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 10, "claude.tokens_output": 20},
        }
        rows = af.deduplicate_usage_rows([first, second])
        self.assertEqual(rows, [first, second])

    def test_message_id_absent_rows_never_collapse_into_each_other(self):
        # Historical data predates message_id capture -- an absent id must
        # never be treated as a shared grouping key across rows, or distinct
        # historical events would silently vanish.
        rows_in = [{
            "timestamp": f"2026-07-25T10:00:0{i}.000Z",
            "attributes": {"claude.type": "assistant", "claude.session_id": "s1",
                           "claude.message_model": "claude-sonnet-4-6",
                           "claude.tokens_input": 10, "claude.tokens_output": 20},
        } for i in range(3)]
        rows = af.deduplicate_usage_rows(rows_in)
        self.assertEqual(rows, rows_in)

    def test_mixed_exact_and_estimated_aggregate_preserves_coverage(self):
        row = af.normalize_usage_row({
            "day": "2026-07-25", "events": 10, "tokens_in_sum": 100,
            "tokens_out_sum": 20, "exact_usage_events": 6,
            "estimated_usage_events": 4, "body_chars_sum": 400,
            "native_events": 6, "legacy_events": 4,
        })
        self.assertEqual(row["input_tokens"], 200)
        self.assertEqual(row["token_source"], "mixed")
        self.assertEqual((row["exact_usage_events"], row["estimated_usage_events"]), (6, 4))

    def test_berserk_epoch_nanosecond_day_normalizes_for_effective_pricing(self):
        row = af.normalize_usage_row({
            "day": 1785068810908000000,
            "model": "claude-sonnet-5",
            "events": 1,
            "tokens_in_sum": 100,
            "tokens_out_sum": 20,
            "exact_usage_events": 1,
        })
        self.assertEqual(row["day"], "2026-07-26")
        priced = af.calculate_public_cost(row, af.load_pricing_catalog(CATALOG))
        self.assertEqual(priced["pricing_status"], "priced")

    def test_legacy_row_and_estimate_normalize(self):
        exact = af.normalize_usage_row({
            "attributes": {
                "claude.session_id": "legacy",
                "claude.tokens_input": "40",
                "claude.tokens_output": "10",
                "claude.message_model": "claude-haiku-4-5",
            }
        })
        self.assertEqual(exact["input_tokens"], 40)
        self.assertEqual(exact["token_source"], "exact")
        estimated = af.normalize_usage_row({"body_chars": 41})
        self.assertEqual(estimated["input_tokens"], 11)
        self.assertEqual(estimated["token_source"], "estimated")

    def test_bzrk_tables_shape_parses(self):
        doc = {"Tables": [{
            "schema": {"columns": [{"name": "model"}, {"name": "tokens_in_sum"}]},
            "rows": [["claude-sonnet-4-6", 12]],
        }]}
        rows = af.parse_records(json.dumps(doc))
        self.assertEqual(rows, [{"model": "claude-sonnet-4-6", "tokens_in_sum": 12}])

    def test_pricing_catalog_resolves_specific_aliases(self):
        catalog = af.load_pricing_catalog(CATALOG)
        price = af.resolve_model_price(catalog, "claude-sonnet-4-6")
        self.assertEqual(price["id"], "claude-sonnet-4.6")
        result = af.calculate_public_cost({
            "model": "claude-opus-4-8",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_creation_1h_tokens": 0,
        }, catalog)
        self.assertEqual(result["public_api_equivalent_usd"], 30.0)
        self.assertEqual(result["pricing_status"], "priced")

    def test_cache_and_long_context_pricing(self):
        catalog = af.load_pricing_catalog(CATALOG)
        cache = af.calculate_public_cost({
            "model": "claude-sonnet-4-6", "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 100_000, "cache_creation_tokens": 0,
            "cache_creation_1h_tokens": 0,
        }, catalog)
        self.assertAlmostEqual(cache["public_api_equivalent_usd"], 0.03)
        long_context = af.calculate_public_cost({
            "model": "claude-sonnet-4-5", "input_tokens": 250_000,
            "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_1h_tokens": 0,
        }, catalog)
        self.assertTrue(long_context["long_context"])
        self.assertAlmostEqual(long_context["public_api_equivalent_usd"], 1.5)

    def test_aggregated_long_context_split_and_server_tool_cost(self):
        catalog = af.load_pricing_catalog(CATALOG)
        result = af.calculate_public_cost(af.normalize_usage_row({
            "model": "claude-sonnet-4-5", "events": 10,
            "tokens_in_sum": 500_000, "tokens_out_sum": 10_000,
            "long_input_tokens_sum": 250_000, "long_output_tokens_sum": 5_000,
            "long_cache_read_tokens_sum": 0, "long_cache_creation_tokens_sum": 0,
            "web_search_requests_sum": 2,
        }), catalog)
        expected = (250_000 * 3 + 250_000 * 6 + 5_000 * 15 + 5_000 * 22.5) / 1_000_000
        expected += 2 * 10 / 1000
        self.assertAlmostEqual(result["public_api_equivalent_usd"], expected)
        self.assertTrue(result["long_context"])
        self.assertFalse(result["long_context_unknown"])

    def test_unknown_model_never_uses_default_price(self):
        result = af.calculate_public_cost({
            "model": "future-unknown", "input_tokens": 500,
            "output_tokens": 100, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_1h_tokens": 0,
        }, af.load_pricing_catalog(CATALOG))
        self.assertEqual(result["pricing_status"], "unknown")
        self.assertEqual(result["public_api_equivalent_usd"], 0)
        self.assertEqual(result["unpriced_tokens"], 600)

    def test_unknown_version_does_not_match_generic_family_alias(self):
        result = af.calculate_public_cost({
            "day": "2026-07-26", "model": "claude-opus-5",
            "input_tokens": 500, "output_tokens": 100,
        }, af.load_pricing_catalog(CATALOG))
        self.assertEqual(result["pricing_status"], "unknown")
        self.assertEqual(result["public_api_equivalent_usd"], 0)
        self.assertEqual(result["unpriced_tokens"], 600)

    def test_effective_dated_catalog_selects_matching_version(self):
        catalog = {
            "effective_from": "2026-01-01",
            "models": [
                {"id": "m-old", "aliases": ["model-x"], "effective_from": "2026-01-01",
                 "effective_to": "2026-06-30", "input_usd_per_mtok": 1},
                {"id": "m-new", "aliases": ["model-x"], "effective_from": "2026-07-01",
                 "input_usd_per_mtok": 2},
            ],
        }
        self.assertEqual(af.resolve_model_price(catalog, "model-x", "2026-06-01")["id"], "m-old")
        self.assertEqual(af.resolve_model_price(catalog, "model-x", "2026-07-25")["id"], "m-new")

    def test_current_catalog_prices_haiku_and_sonnet5_by_effective_date(self):
        catalog = af.load_pricing_catalog(CATALOG)
        haiku = af.calculate_public_cost({
            "day": "2026-07-25", "model": "claude-haiku-4-5",
            "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        }, catalog)
        self.assertEqual(haiku["public_api_equivalent_usd"], 6.0)
        july = af.resolve_model_price(catalog, "claude-sonnet-5", "2026-07-25")
        september = af.resolve_model_price(catalog, "claude-sonnet-5", "2026-09-01")
        self.assertEqual((july["input_usd_per_mtok"], july["output_usd_per_mtok"]), (2, 10))
        self.assertEqual((september["input_usd_per_mtok"], september["output_usd_per_mtok"]), (3, 15))

    def test_fast_mode_uses_separate_effective_rates(self):
        result = af.calculate_public_cost({
            "day": "2026-07-25", "model": "claude-opus-4-8", "speed": "fast",
            "input_tokens": 1_000_000, "output_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
        }, af.load_pricing_catalog(CATALOG))
        self.assertEqual(result["public_api_equivalent_usd"], 61.0)
        self.assertEqual(result["pricing_variant"], "fast")

    def test_query_is_bounded_and_aggregate_first(self):
        query = af.usage_aggregate_query()
        self.assertIn("summarize", query)
        self.assertIn("take 2000", query)
        self.assertIn("business.feature.id", query)
        self.assertNotIn("project body", query)
        self.assertIn("raw_attributes=$raw['attributes']", query)
        self.assertIn("raw_resource=$raw['resource']", query)
        self.assertNotIn("toint(attributes[", query)
        self.assertNotIn("todouble(attributes[", query)
        self.assertNotIn("tostring(resource['business.", query)
        self.assertNotIn("arg_max", query)
        projected = query.split("| project ", 1)[1].split(
            "| summarize timestamp=max(timestamp)", 1
        )[0]
        self.assertIn("tool_name", projected)
        # Third, distinct stage: collapse content-block rows sharing one
        # claude.message_id before the final day/model rollup, so duplicate
        # per-content-block rows aren't summed more than once. Separate from
        # the request_id/timestamp-fingerprint dedupe_key stage above it.
        self.assertEqual(query.count("| summarize timestamp=max(timestamp)"), 2)
        self.assertIn("claude.message_id", query)
        self.assertIn("by msg_gkey", query)


class BusinessDataAndAttributionTest(FinopsTestCase):
    def _feature(self):
        return {
            "feature_id": "FEAT-1", "work_item_id": "WI-1",
            "project_id": "PROJ-1", "portfolio_id": "PORT-1",
            "team_id": "TEAM-1", "name": "Feature one", "planned_hours": 20,
            "planned_ai_budget_usd": 10, "completion_pct": 50,
            "repositories": ["repo-one"], "branches": ["feat/one"],
            "pull_requests": ["123"], "source_system": "jira",
            "source_record_id": "FEAT-1", "source_updated_at": "2026-07-25T00:00:00Z",
        }

    def test_attribution_precedence_and_fallbacks(self):
        store = {"features": [self._feature()], "effort": []}
        explicit = af.attribute_usage({"feature_id": "EXPLICIT", "repository_id": "repo-one"}, store)
        self.assertEqual(explicit["feature_id"], "EXPLICIT")
        self.assertEqual(explicit["attribution_source"], "explicit")
        pr = af.attribute_usage({"feature_id": "", "pull_request_id": "123"}, store)
        self.assertEqual((pr["feature_id"], pr["attribution_source"]), ("FEAT-1", "pull_request"))
        work_item = af.attribute_usage({"feature_id": "", "work_item_id": "WI-1"}, store)
        self.assertEqual((work_item["feature_id"], work_item["attribution_source"]),
                         ("FEAT-1", "work_item"))
        branch = af.attribute_usage({"feature_id": "", "branch_id": "feat/one"}, store)
        self.assertEqual(branch["attribution_source"], "branch")
        repo = af.attribute_usage({"feature_id": "", "repository_id": "repo-one"}, store)
        self.assertEqual(repo["project_id"], "PROJ-1")
        missing = af.attribute_usage({"feature_id": "", "repository_id": "other"}, store)
        self.assertEqual(missing["attribution_source"], "unattributed")

    def test_ambiguous_repository_falls_back_only_to_project(self):
        first = self._feature()
        second = dict(first)
        second.update({"feature_id": "FEAT-2", "source_record_id": "FEAT-2"})
        store = {"features": [first, second], "effort": []}
        result = af.attribute_usage({"feature_id": "", "repository_id": "repo-one"}, store)
        self.assertEqual(result.get("feature_id"), "")
        self.assertEqual(result["project_id"], "PROJ-1")
        self.assertEqual(result["attribution_source"], "repository_project")

    def test_import_feature_and_effort_csv(self):
        feature_csv = self.root / "features.csv"
        feature_csv.write_text(
            "feature_id,project_id,name,planned_hours,planned_ai_budget_usd,completion_pct,repositories,source_system,source_record_id,source_updated_at\n"
            "FEAT-1,PROJ-1,Feature one,20,10,50,repo-one,jira,FEAT-1,2026-07-25T00:00:00Z\n",
            encoding="utf-8",
        )
        effort_json = self.root / "effort.ndjson"
        effort_json.write_text(json.dumps({
            "worklog_id": "WL-1", "feature_id": "FEAT-1", "work_date": "2026-07-25",
            "hours": 4, "source_system": "jira", "source_updated_at": "2026-07-25T12:00:00Z",
        }) + "\n", encoding="utf-8")
        first = af.import_business_data("feature", feature_csv, emit_otlp=False)
        second = af.import_business_data("effort", effort_json, emit_otlp=False)
        self.assertEqual((first["imported"], second["imported"]), (1, 1))
        store = af.load_business_store()
        self.assertEqual(store["features"][0]["feature_id"], "FEAT-1")
        self.assertEqual(store["effort"][0]["hours"], 4)

    def test_import_rejects_bad_hours_and_identifier(self):
        with self.assertRaises(ValueError):
            af.normalize_business_record("effort", {
                "worklog_id": "bad id", "feature_id": "FEAT-1",
                "work_date": "2026-07-25", "hours": 30,
            })
        with self.assertRaises(ValueError):
            af.normalize_business_record("feature", {
                "feature_id": "FEAT 1", "project_id": "P1",
            })
        with self.assertRaises(ValueError):
            af.normalize_business_record("effort", {
                "worklog_id": "WL-1", "feature_id": "FEAT-1",
                "work_date": "2026-07-25", "actual_hours": -1,
            })
        with self.assertRaises(ValueError):
            af.normalize_business_record("feature", {
                "feature_id": "FEAT-1", "project_id": "P1",
                "planned_ai_budget_usd": "NaN",
            })

    def test_effort_actual_hours_alias_and_stale_conflict_handling(self):
        effort = af.normalize_business_record("effort", {
            "worklog_id": "WL-1", "feature_id": "FEAT-1",
            "work_date": "2026-07-25", "actual_hours": 3.5,
            "source_updated_at": "2026-07-25T10:00:00+00:00",
        })
        self.assertEqual((effort["hours"], effort["actual_hours"]), (3.5, 3.5))
        stale = dict(effort, actual_hours=2, hours=2,
                     source_updated_at="2026-07-25T09:00:00Z")
        self.assertEqual(
            af._merge_latest([effort], [stale], ("source_system", "worklog_id")),
            [effort],
        )
        conflict = dict(effort, actual_hours=4, hours=4)
        with self.assertRaises(ValueError):
            af._merge_latest([effort], [conflict], ("source_system", "worklog_id"))

    def test_remote_plaintext_otlp_is_rejected(self):
        af.configure(self.search, catalog_path=CATALOG,
                     business_store_path=self.root / "business.json",
                     decision_store_path=self.root / "decisions.json",
                     report_dir=self.root / "reports",
                     otlp_endpoint="http://example.com/v1/logs")
        with self.assertRaises(ValueError):
            af.emit_otlp_records([self._feature()], "engineering-work")


class ReportingAndRecommendationTest(FinopsTestCase):
    def _write_store(self):
        af._atomic_write_json(self.root / "business.json", {
            "schema_version": 1,
            "updated_at": "2026-07-25T12:00:00Z",
            "features": [{
                "feature_id": "FEAT-1", "project_id": "berserk", "name": "Feature one",
                "planned_hours": 20, "planned_ai_budget_usd": 10,
                "completion_pct": 50, "repositories": ["berserk-mcp"],
                "branches": [], "pull_requests": [], "source_system": "jira",
                "source_record_id": "FEAT-1", "source_updated_at": "2026-07-25T00:00:00Z",
            }],
            "effort": [{
                "worklog_id": "WL-1", "feature_id": "FEAT-1",
                "work_date": "2026-07-25", "hours": 4,
                "source_system": "jira", "source_updated_at": "2026-07-25T10:00:00Z",
            }],
        })

    def test_spend_overview_has_coverage_and_structured_envelope(self):
        text, error = af.spend_overview("7d ago", group_by="project")
        self.assertFalse(error)
        self.assertIn("API-equivalent cost", text)
        self.assertIn("attribution_coverage", text)
        self.assertIn("```json", text)
        self.assertIn("summarize", self.last_query)

    def test_feature_cost_joins_hours_and_forecasts(self):
        self._write_store()
        text, error = af.feature_cost("FEAT-1")
        self.assertFalse(error)
        self.assertIn("4.00 actual", text)
        self.assertIn("Forecast at completion", text)
        payload = af._feature_snapshot("FEAT-1", self.rows, af.load_pricing_catalog(),
                                       af.load_business_store())
        self.assertEqual(payload["actual_hours"], 4)
        self.assertGreater(payload["forecast_ai_cost_at_completion_usd"], 0)

    def test_efficiency_recommendations_are_stable_and_thresholded(self):
        rows = [usage(cache_read_tokens_sum=0, tokens_in_sum=150000, events=10)]
        catalog = af.load_pricing_catalog()
        one = af.analyze_efficiency_rows(rows, catalog)
        two = af.analyze_efficiency_rows(rows, catalog)
        self.assertEqual(one, two)
        codes = {item["code"] for item in one["findings"]}
        self.assertIn("low_cache_reuse", codes)
        self.assertTrue(all(item["recommendation_id"].startswith("rec_")
                            for item in one["findings"]))
        changed = af.analyze_efficiency_rows([
            usage(cache_read_tokens_sum=0, tokens_in_sum=160000, events=12)
        ], catalog)
        first_ids = {item["code"]: item["recommendation_id"] for item in one["findings"]}
        changed_ids = {item["code"]: item["recommendation_id"] for item in changed["findings"]}
        self.assertEqual(first_ids["low_cache_reuse"], changed_ids["low_cache_reuse"])
        self.assertTrue(all(item["expected_result"] and item["risks"]
                            for item in one["findings"]))

    def test_operation_specific_recommendation_mappings(self):
        report = af.analyze_efficiency_rows([
            usage(events=0, tool_calls=25, tool="Read", result_tokens_sum=15000),
            usage(events=25, query_source="subagent", tokens_in_sum=200000),
            usage(events=10, tool="search_kql", result_tokens_sum=20000),
            usage(events=5, compactions=5),
        ], af.load_pricing_catalog())
        codes = {item["code"] for item in report["findings"]}
        self.assertTrue({"repeated_file_reads", "expensive_kql",
                         "excessive_compaction", "subagent_fanout"}.issubset(codes))

    def test_low_sample_findings_cannot_be_approved(self):
        report = af.analyze_efficiency_rows([
            usage(events=1, tokens_in_sum=100000, cache_read_tokens_sum=0)
        ], af.load_pricing_catalog())
        self.assertTrue(report["findings"])
        self.assertTrue(all(not item["eligible_for_approval"] for item in report["findings"]))

    def test_recommendation_decision_is_private_append_only_and_idempotent(self):
        rec_id = "rec_0123456789abcdef"
        text, error = af.record_recommendation_decision(
            rec_id, "approved", "owner@example.com", "Apply to team harness",
        )
        self.assertFalse(error)
        self.assertNotIn("owner@example.com", (self.root / "decisions.json").read_text())
        self.assertNotIn("Apply to team harness", (self.root / "decisions.json").read_text())
        text2, error2 = af.record_recommendation_decision(
            rec_id, "approved", "owner@example.com", "Apply to team harness",
        )
        self.assertFalse(error2)
        self.assertIn('"idempotent": true', text2)
        self.assertEqual(len(json.loads((self.root / "decisions.json").read_text())), 1)

    def test_optimization_impact_keeps_lower_cost_cohort(self):
        self.rows = [
            usage(harness="before", events=10, successes=10, tokens_in_sum=1_000_000),
            usage(harness="after", events=10, successes=10, tokens_in_sum=500_000),
        ]
        text, error = af.optimization_impact("claude-dev", "before", "after")
        self.assertFalse(error)
        self.assertIn('"verdict": "keep"', text)

    def test_optimization_impact_rolls_back_on_latency_regression(self):
        self.rows = [
            usage(harness="before", events=10, successes=10,
                  tokens_in_sum=1_000_000, duration_seconds_sum=10),
            usage(harness="after", events=10, successes=10,
                  tokens_in_sum=500_000, duration_seconds_sum=30),
        ]
        text, error = af.optimization_impact("claude-dev", "before", "after")
        self.assertFalse(error)
        self.assertIn('"verdict": "recommend-rollback"', text)

    def test_million_event_fixture_stays_aggregate_and_bounded(self):
        report = af.build_spend_overview([
            usage(events=2_000_000, successes=1_900_000)
        ], af.load_pricing_catalog(), group_by="project", limit=20)
        self.assertEqual(report["overall"]["events"], 2_000_000)
        self.assertEqual(len(report["groups"]), 1)
        self.assertIn("take 2000", af.usage_aggregate_query())


class DashboardAndExportTest(FinopsTestCase):
    def test_dashboard_markdown_and_html_are_self_contained(self):
        text, error = af.generate_dashboard("portfolio", since="30d ago", fmt="markdown")
        self.assertFalse(error)
        md = self.root / "reports" / "claude-portfolio.md"
        self.assertTrue(md.exists())
        self.assertIn("# Claude Portfolio", md.read_text())
        text, error = af.generate_dashboard("portfolio", since="30d ago", fmt="html",
                                            filename="portfolio.html")
        self.assertFalse(error)
        html_text = (self.root / "reports" / "portfolio.html").read_text()
        self.assertIn("<svg", html_text)
        self.assertNotIn("<script", html_text)
        self.assertNotIn("cdn", html_text.lower())

    def test_dashboard_rejects_path_escape(self):
        text, error = af.generate_dashboard("portfolio", filename="../escape")
        self.assertTrue(error)
        self.assertIn("basename", text)

    def test_bi_export_writes_all_datasets_and_manifest(self):
        output = self.root / "bi"
        manifest = af.export_bi("30d ago", output, fmt="csv")
        self.assertEqual(set(manifest["datasets"]), {
            "ai_usage_daily", "feature_cost_snapshot", "project_cost_snapshot",
            "human_effort_daily", "agent_harness_efficiency",
            "harness_recommendation_status", "attribution_quality",
        })
        self.assertTrue((output / "manifest.json").exists())
        self.assertTrue((output / "ai_usage_daily.csv").exists())
        on_disk = json.loads((output / "manifest.json").read_text())
        self.assertEqual(on_disk["datasets"]["ai_usage_daily"]["rows"], 1)
        immutable = output / on_disk["datasets"]["ai_usage_daily"]["filename"]
        self.assertTrue(immutable.exists())
        self.assertIn("coverage", on_disk)

    def test_failed_bi_generation_retains_previous_manifest_and_snapshot(self):
        output = self.root / "bi-retain"
        first = af.export_bi("30d ago", output, fmt="csv")
        manifest_before = (output / "manifest.json").read_bytes()
        snapshot_before = output / first["datasets"]["ai_usage_daily"]["filename"]
        original = af._atomic_write_text

        def fail_new_snapshot(path, content):
            if ".snapshots" in Path(path).parts:
                raise OSError("simulated export failure")
            return original(path, content)

        with mock.patch.object(af, "_atomic_write_text", side_effect=fail_new_snapshot):
            with self.assertRaises(OSError):
                af.export_bi("30d ago", output, fmt="csv")
        self.assertEqual((output / "manifest.json").read_bytes(), manifest_before)
        self.assertTrue(snapshot_before.exists())

    def test_work_context_merges_and_validates(self):
        attrs = af.build_work_context_attributes(
            "service.namespace=dev", feature="FEAT-1", project="PROJ-1",
            harness_version="v2",
        )
        self.assertIn("business.feature.id=FEAT-1", attrs)
        self.assertIn("service.namespace=dev", attrs)
        with self.assertRaises(ValueError):
            af.build_work_context_attributes(feature="bad value")


class McpIntegrationTest(FinopsTestCase):
    def test_claude_role_lists_all_finops_tools(self):
        original = bm.ACTIVE_ROLE
        try:
            bm.ACTIVE_ROLE = "claude"
            response = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = {tool["name"] for tool in response["result"]["tools"]}
            expected = {
                "claude_spend_overview", "claude_feature_cost",
                "claude_project_economics", "claude_efficiency_insights",
                "claude_harness_recommendations",
                "claude_record_recommendation_decision",
                "claude_optimization_impact", "claude_management_report",
                "claude_generate_dashboard",
            }
            self.assertTrue(expected.issubset(names))
        finally:
            bm.ACTIVE_ROLE = original

    def test_spend_dispatch_validates_since_and_calls_aggregate_query(self):
        text, error = bm._handle_call_uncached(
            "claude_spend_overview", {"since": "bad; value"}
        )
        self.assertTrue(error)
        self.assertIn("invalid 'since'", text)
        text, error = bm._handle_call_uncached(
            "claude_spend_overview", {"since": "7d ago", "group_by": "project"}
        )
        self.assertFalse(error)
        self.assertIn("enterprise spend overview", text)

    def test_dashboard_and_decision_annotations_are_writes(self):
        self.assertFalse(bm.annotations_for("claude_generate_dashboard")["readOnlyHint"])
        self.assertFalse(bm.annotations_for("claude_record_recommendation_decision")["readOnlyHint"])


class AssetTest(unittest.TestCase):
    def test_grafana_assets_are_valid_and_bounded(self):
        paths = sorted((ROOT / "dashboards" / "grafana").glob("*.json"))
        self.assertEqual(len(paths), 5)
        required_panels = {
            "agent-harness-efficiency.json": {
                "Cost by agent and harness", "Measured harness-version impact",
                "Expensive MCP operations and large results",
            },
            "data-quality-governance.json": {
                "Attributed versus unattributed spend",
                "Native versus legacy telemetry coverage",
                "Recommendation approval audit",
            },
            "executive-ai-spend.json": {
                "Daily Claude-reported approximate AI spend",
                "Planned AI budget by project and feature",
                "Feature AI budget, actual spend, and forecast",
            },
            "feature-delivery-economics.json": {
                "AI spend by feature", "Developer hours by feature",
                "AI spend per actual developer hour",
            },
            "project-codebase-spend.json": {
                "Spend by project and repository", "Cost by pull request",
                "AI spend per commit by project",
            },
        }
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(data["panels"]), 7, path.name)
            self.assertTrue(
                required_panels[path.name].issubset(
                    {panel["title"] for panel in data["panels"]}
                ),
                path.name,
            )
            self.assertEqual(
                {item["name"] for item in data["templating"]["list"]},
                {"team", "project", "repository", "feature", "model", "agent",
                 "harness", "date_range"},
            )
            for panel in data["panels"]:
                query = panel["targets"][0]["query"]
                self.assertIn("timestamp >= now() -", query)
                self.assertIn("service.name", query)

    def test_berserk_explore_pack_uses_render_and_bounds(self):
        text = (ROOT / "dashboards" / "berserk" / "ai-finops.kql").read_text()
        self.assertGreaterEqual(text.count("| render "), 9)
        self.assertGreaterEqual(text.count("timestamp >= now() - 30d"), 9)
        self.assertIn("Planned versus actual AI spend", text)
        self.assertIn("Top expensive agents and operations", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
