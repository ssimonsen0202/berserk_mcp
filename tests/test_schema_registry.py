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
        b = sr.normalize_snapshot(table="default", getschema_text="\n".join(reversed(GETSCHEMA.splitlines())), sample_text=SAMPLE)
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
            stale = sr.get_schema_snapshot(force=True, table="default", config_dir=d, fetcher=lambda: (_ for _ in ()).throw(RuntimeError("x")))
            self.assertEqual(stale["source_status"], "stale")
        with tempfile.TemporaryDirectory() as d:
            unavailable = sr.get_schema_snapshot(table="default", config_dir=d, fetcher=lambda: (_ for _ in ()).throw(RuntimeError("x")))
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


if __name__ == "__main__":
    unittest.main()
