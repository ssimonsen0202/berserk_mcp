import json
import tempfile
import unittest
from pathlib import Path

import schema_registry as sr


GETSCHEMA = """
ColumnName ColumnType
timestamp datetime
metric_name string
value real
body string
resource dynamic
attributes dynamic
"""

FIELDSTATS = """
resource.service.name string examples ['nginx', 'postgres']
resource.host.name string examples ['node-a']
"""

SAMPLE = """
resource_keys ['service.name', 'host.name'] attribute_keys ['state'] has_body true
"""


class SchemaRegistryTest(unittest.TestCase):
    def test_normalize_hash_and_context(self):
        snap = sr.normalize_snapshot(
            table="default",
            getschema_text=GETSCHEMA,
            fieldstats_text=FIELDSTATS,
            sample_text=SAMPLE,
        )
        self.assertIn("timestamp", snap["columns"])
        self.assertIn("service.name", snap["resource_fields"])
        self.assertIn("state", snap["attribute_fields"])
        self.assertTrue(snap["schema_hash"].startswith("sha256:"))
        ctx = sr.schema_context(snap, max_chars=500)
        self.assertIn("resource paths:", ctx)
        self.assertNotIn("body string value", ctx)

    def test_stable_hash_ordering_and_changes(self):
        a = sr.normalize_snapshot(table="default", getschema_text=GETSCHEMA, sample_text=SAMPLE)
        b = sr.normalize_snapshot(
            table="default", getschema_text="\n".join(reversed(GETSCHEMA.splitlines())), sample_text=SAMPLE
        )
        self.assertEqual(sr.schema_hash(a), sr.schema_hash(b))
        c = sr.normalize_snapshot(table="default", getschema_text=GETSCHEMA + "\nnew_col string\n", sample_text=SAMPLE)
        self.assertNotEqual(sr.schema_hash(a), sr.schema_hash(c))

    def test_cache_hit_force_expiry_and_corrupt_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def fetcher():
                calls.append(1)
                return {"getschema": GETSCHEMA, "fieldstats": FIELDSTATS, "sample": SAMPLE}

            first = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=fetcher)
            second = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=fetcher)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["schema_hash"], second["schema_hash"])
            sr.get_schema_snapshot(force=True, table="default", config_dir=d, fetcher=fetcher)
            self.assertEqual(len(calls), 2)
            cache = next(Path(d).glob("schema_snapshot_default.json"))
            cache.write_text("{bad json", encoding="utf-8")
            recovered = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=fetcher)
            self.assertEqual(recovered["source_status"], "fresh")

    def test_fetcher_failure_returns_stale_or_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            snap = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=lambda: {"getschema": GETSCHEMA})
            self.assertEqual(snap["source_status"], "fresh")
            stale = sr.get_schema_snapshot(
                force=True, table="default", config_dir=d, fetcher=lambda: (_ for _ in ()).throw(RuntimeError("x"))
            )
            self.assertEqual(stale["source_status"], "stale")
        with tempfile.TemporaryDirectory() as d:
            unavailable = sr.get_schema_snapshot(
                table="default", config_dir=d, fetcher=lambda: (_ for _ in ()).throw(RuntimeError("x"))
            )
            self.assertEqual(unavailable["source_status"], "unavailable")

    def test_suggestions_and_no_secret_context(self):
        snap = sr.normalize_snapshot(table="default", getschema_text=GETSCHEMA, sample_text=SAMPLE)
        self.assertEqual(sr.suggest_field("service_name", snap)[0], "resource['service.name']")
        snap["resource_fields"]["api.key"] = {"type": "string", "examples": ["secret-token-value"]}
        ctx = sr.schema_context(snap)
        self.assertNotIn("secret-token-value", ctx)

    def test_empty_schema_and_json_serializable(self):
        snap = sr.normalize_snapshot(table="default")
        self.assertEqual(snap["source_status"], "fresh")
        json.dumps(snap)
        self.assertEqual(sr.schema_fields(snap), set())


def _bzrk_json(columns, rows):
    """bzrk --json shape (verified live 2026-11-16 with CLI 2026-08-19)."""
    return json.dumps(
        {
            "Tables": [
                {
                    "schema": {"name": "PrimaryResult", "columns": [{"name": c, "type": 5} for c in columns]},
                    "rows": rows,
                }
            ]
        }
    )


TABLES_JSON = _bzrk_json(["TableName", "DatabaseName", "DocString", "Folder"], [["default", "default", "", ""]])
GETSCHEMA_JSON = _bzrk_json(
    ["ColumnName", "ColumnOrdinal", "DataType", "ColumnType"],
    [
        ["$raw", 0, "System.Object", "dynamic"],
        ["timestamp", 1, "System.DateTime", "datetime"],
        ["ingest_time", 2, "System.DateTime", "datetime"],
        ["start_time", 3, "System.DateTime", "datetime"],
        ["end_time", 4, "System.DateTime", "datetime"],
    ],
)
FIELDSTATS_JSON = _bzrk_json(
    ["AttributePath", "Type", "Cardinality", "Frequency", "Hint", "HintExhaustive"],
    [
        ["resource['service.name']", "string", 2, 0.2857142857142857, ["api", "web"], True],
        ["resource['host.name']", "string", 1, 1.0, ["node-a"], True],
        ["resource['telemetry.sdk.version']", "string", 1, 0.7142857142857143, ["0.31.0"], True],
        ["resource['pid']", "long", 1, 1.0, [], True],
    ],
)
SAMPLE_JSON = _bzrk_json(
    ["resource_keys", "attribute_keys", "metric_name", "has_body", "has_metric", "has_severity"],
    [
        [["service.name", "host.name", "process.pid"], ["state", "query_id"], None, True, False, True],
        [["service.name"], ["rss_delta_mb"], "process.cpu.time", False, True, False],
    ],
)

# The text table that bzrk 2026-08-19 prints: a "#" header with ":type"
# suffixes and a row-index column in front of every row.
GETSCHEMA_TEXT_WITH_INDEX = """#   ColumnName:string   ColumnOrdinal:int   DataType:string   ColumnType:string
 0   $raw                                0   System.Object     dynamic
 1   timestamp                           1   System.DateTime   datetime
 2   ingest_time                         2   System.DateTime   datetime
"""
FIELDSTATS_TEXT_WITH_INDEX = """#          AttributePath:string          Type:string   Cardinality:long   Frequency:real   Hint:dynamic   HintExhaustive:bool
  0   resource['service.name']            string                   2   0.2857142857142857   ["api", "web"]   true
  1   resource['telemetry.sdk.version']   string                   1   0.7142857142857143   ["0.31.0"]       true
  2   resource['pid']                     long                     1   1.0                  []               true
"""


class BzrkOutputFormatTest(unittest.TestCase):
    """The schema snapshot was empty for six weeks (2026-10-04 to 2026-11-16):
    the parser read the row-index column as the field name, so the
    unknown-field check silently had nothing to compare against."""

    def snapshot(self, **kw):
        return sr.normalize_snapshot(table="default", **kw)

    def test_reads_bzrk_json_output(self):
        snap = self.snapshot(
            tables_text=TABLES_JSON,
            getschema_text=GETSCHEMA_JSON,
            fieldstats_text=FIELDSTATS_JSON,
            sample_text=SAMPLE_JSON,
        )
        for col in ("$raw", "timestamp", "ingest_time", "start_time", "end_time"):
            self.assertIn(col, snap["columns"])
        self.assertEqual(snap["columns"]["timestamp"]["type"], "datetime")
        self.assertTrue(
            {"service.name", "host.name", "telemetry.sdk.version", "process.pid"} <= set(snap["resource_fields"])
        )
        self.assertEqual(snap["resource_fields"]["service.name"]["examples"], ["api", "web"])
        self.assertTrue({"state", "query_id", "rss_delta_mb"} <= set(snap["attribute_fields"]))

    def test_reads_the_text_table_with_a_row_index_column(self):
        snap = self.snapshot(getschema_text=GETSCHEMA_TEXT_WITH_INDEX, fieldstats_text=FIELDSTATS_TEXT_WITH_INDEX)
        self.assertTrue({"$raw", "timestamp", "ingest_time"} <= set(snap["columns"]))
        self.assertEqual(snap["columns"]["ingest_time"]["type"], "datetime")
        self.assertEqual(set(snap["resource_fields"]), {"service.name", "telemetry.sdk.version", "pid"})

    def test_numbers_are_never_taken_as_field_names(self):
        for fieldstats in (FIELDSTATS_TEXT_WITH_INDEX, FIELDSTATS_JSON):
            snap = self.snapshot(getschema_text=GETSCHEMA_JSON, fieldstats_text=fieldstats)
            bad = [k for k in snap["resource_fields"] if k[:1].isdigit()]
            self.assertEqual(bad, [], f"numeric field names from {fieldstats[:20]!r}")

    def test_top_level_otlp_fields_are_known_once_the_schema_is_read(self):
        # getschema lists five columns, but the repo's own queries use these
        # top-level fields; they must not be reported as unknown.
        snap = self.snapshot(getschema_text=GETSCHEMA_JSON)
        for col in ("body", "severity_text", "metric_name", "value", "trace_id", "span_id", "resource", "attributes"):
            self.assertIn(col, snap["columns"])
        self.assertEqual(sr.schema_fields(self.snapshot()), set(), "no schema read, no invented fields")

    def test_an_empty_fetch_is_not_cached_and_not_reported_fresh(self):
        with tempfile.TemporaryDirectory() as d:

            def empty_fetcher():
                return {
                    "tables": "(no rows)",
                    "getschema": "(no rows)",
                    "fieldstats": "(no rows)",
                    "sample": "(no rows)",
                }

            snap = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=empty_fetcher)
            self.assertEqual(snap["source_status"], "unavailable")
            self.assertEqual(list(Path(d).glob("schema_snapshot_*.json")), [], "an empty schema must not be cached")

    def test_a_cached_snapshot_without_columns_is_fetched_again(self):
        with tempfile.TemporaryDirectory() as d:
            empty = sr.normalize_snapshot(table="default")  # what the broken parser cached
            (Path(d) / "schema_snapshot_default.json").write_text(json.dumps(empty), encoding="utf-8")
            calls = []

            def fetcher():
                calls.append(1)
                return {"getschema": GETSCHEMA_JSON, "fieldstats": FIELDSTATS_JSON, "sample": SAMPLE_JSON}

            snap = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=fetcher)
            self.assertEqual(len(calls), 1, "an empty cached snapshot must not be served as fresh")
            self.assertIn("timestamp", snap["columns"])


class SchemaFetcherTest(unittest.TestCase):
    def test_every_schema_query_asks_bzrk_for_json(self):
        from unittest import mock

        import berserk_mcp
        from berserk_mcp import runner

        calls = []

        def fake_run_bzrk(args, *a, **k):
            calls.append(args)
            return GETSCHEMA_JSON, False

        with mock.patch.object(runner, "run_bzrk", side_effect=fake_run_bzrk):
            runner._schema_fetcher()
        self.assertEqual(len(calls), 4)
        for args in calls:
            self.assertIn("--json", args, args)
        self.assertTrue(berserk_mcp)


if __name__ == "__main__":
    unittest.main()
