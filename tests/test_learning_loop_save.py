"""WP1: save_query must see rows, and duplicate KQL is refused by fingerprint.

Every test names the new code inside its own body, so each one fails on its
own against code that lacks it, instead of the whole module failing to import.
bzrk is stubbed; nothing here touches a live Berserk or the real store.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "learned_legacy_v1_37.json"

TABLE = bm.TABLE
KQL = TABLE + " | where severity_text == 'ERROR' | take 5"
ROWS = "col_a\nrow1\nrow2"
JSON_NO_ROWS = json.dumps({"Tables": [{"schema": {"columns": [{"name": "n"}]}, "rows": []}]})
JSON_COUNT_ZERO = json.dumps({"Tables": [{"schema": {"columns": [{"name": "n"}]}, "rows": [[0]]}]})
JSON_ROWS = json.dumps({"Tables": [{"schema": {"columns": [{"name": "n"}]}, "rows": [[3]]}]})


def _since_of(args):
    return args[args.index("--since") + 1] if "--since" in args else None


class _SaveBase(unittest.TestCase):
    """Temporary store, stubbed bzrk, restored module state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        saved = (
            bm.LEARNED_PATH,
            bm.run_bzrk,
            bm.ACTIVE_ROLE,
            bm.ACTIVE_TIER_RESOLVED,
            bm.CACHE_TTL_SECONDS,
        )

        def restore():
            bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS = saved
            bm._RESULT_CACHE.clear()
            bm._FAIL_COOLDOWN.clear()

        self.addCleanup(restore)
        env = os.environ.pop("BERSERK_MCP_MGMT_TOKEN", None)
        if env is not None:
            self.addCleanup(os.environ.__setitem__, "BERSERK_MCP_MGMT_TOKEN", env)
        self.store_path = Path(self._tmp.name) / "learned.json"
        bm.LEARNED_PATH = self.store_path
        bm.CACHE_TTL_SECONDS = 0
        bm._RESULT_CACHE.clear()
        bm._FAIL_COOLDOWN.clear()
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_DEEP
        self.calls = []
        # Replies by window: {"1h ago": (out, err), "24h ago": (out, err)}.
        self.replies = {}
        self.default_reply = (ROWS, False)

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            return self.replies.get(_since_of(args), self.default_reply)

        bm.run_bzrk = fake

    def kql_calls(self, kql):
        """Windows of every bzrk call that ran exactly this KQL."""
        return [_since_of(c) for c in self.calls if len(c) > 3 and c[3] == kql]

    def save(self, name="probe", kql=KQL, **extra):
        args = {"name": name, "description": "Probe.", "kql": kql}
        args.update(extra)
        return bm.handle_call("save_query", args)

    def stored(self, name):
        return next((it for it in bm.load_learned() if it["name"] == name), None)

    def put(self, *entries):
        self.store_path.write_text(json.dumps(list(entries)), encoding="utf-8")


class ResultHelperTest(unittest.TestCase):
    def test_result_is_empty_text_table_with_several_rows_ending_in_zero_is_not_empty(self):
        table = "service  n\napi  4\nweb  0\ndb  0"
        self.assertEqual(bm.result_row_count(table), 3)
        self.assertFalse(bm.result_is_empty(table))
        # One data row ending in zero is the text form of count()==0.
        self.assertTrue(bm.result_is_empty("n\n0"))
        self.assertTrue(bm.result_is_empty("(no rows)"))
        self.assertTrue(bm.result_is_empty("n 0"))
        self.assertFalse(bm.result_is_empty("n\n7"))

    def test_result_row_count_shapes(self):
        self.assertEqual(bm.result_row_count("(no rows)"), 0)
        self.assertEqual(bm.result_row_count(JSON_NO_ROWS), 0)
        self.assertEqual(bm.result_row_count(JSON_ROWS), 1)
        self.assertEqual(bm.result_row_count("[1, 2, 3]"), 3)
        self.assertIsNone(bm.result_row_count("{not json"))
        self.assertIsNone(bm.result_row_count('{"unknown": 1}'))
        self.assertEqual(bm.result_row_count(ROWS), 2)
        self.assertEqual(bm.result_row_count("OK"), 0)

    def test_result_is_empty_json_rules(self):
        self.assertTrue(bm.result_is_empty(JSON_NO_ROWS))
        self.assertTrue(bm.result_is_empty(JSON_COUNT_ZERO))
        self.assertFalse(bm.result_is_empty(JSON_ROWS))
        # A bool is not a zero count; two values or two rows are data.
        false_doc = json.dumps({"Tables": [{"schema": {"columns": [{"name": "ok"}]}, "rows": [[False]]}]})
        self.assertFalse(bm.result_is_empty(false_doc))
        two_cols = json.dumps({"Tables": [{"schema": {"columns": [{"name": "a"}, {"name": "b"}]}, "rows": [[0, 0]]}]})
        self.assertFalse(bm.result_is_empty(two_cols))
        float_zero = json.dumps({"Tables": [{"schema": {"columns": [{"name": "x"}]}, "rows": [[0.0]]}]})
        self.assertTrue(bm.result_is_empty(float_zero))
        # Unparseable JSON cannot be counted, so it is not called empty.
        self.assertFalse(bm.result_is_empty("{broken"))


class EnvelopeHelperTest(unittest.TestCase):
    def test_envelope_output_unchanged_for_simple_tools_after_helper_extraction(self):
        self.assertTrue(callable(bm.result_row_count))  # the extraction exists
        saved = bm.ENVELOPE_ENABLED
        self.addCleanup(setattr, bm, "ENVELOPE_ENABLED", saved)
        bm.ENVELOPE_ENABLED = True
        for out, rows in (
            ("col_a\nrow1\nrow2", 2),
            ("col_a", 0),
            ("one line", 0),
            ("[1,2,3]", 3),
            (JSON_NO_ROWS, 0),
            ("{broken", None),
            ('{"unknown": 1}', None),
        ):
            text = bm._envelope("list_hosts", "1h ago", out)
            header = text.split("\n", 1)[0]
            if rows is None:
                self.assertTrue(header.startswith("window=1h ago  source="), (out, header))
            else:
                self.assertTrue(header.startswith(f"window=1h ago  rows={rows}  source="), (out, header))
            self.assertEqual(bm.result_row_count(out), rows, out)
        self.assertTrue(bm._envelope("list_hosts", "1h ago", "(no rows)").startswith("No rows in window 1h ago."))


class SaveRowsTest(_SaveBase):
    def test_save_query_rejects_zero_rows_after_24h_retry(self):
        self.default_reply = ("(no rows)", False)
        text, err = self.save()
        self.assertTrue(err)
        self.assertIn("24h", text)
        self.assertIn("discover_schema", text)
        self.assertIn("allow_empty=true", text)
        self.assertEqual(bm.load_learned(), [])
        self.assertEqual(self.kql_calls(KQL), ["1h ago", "24h ago"])
        self.assertTrue(callable(bm.result_is_empty))

    def test_save_query_accepts_rows_on_24h_retry_and_marks_verified(self):
        self.replies = {"1h ago": ("(no rows)", False), "24h ago": (ROWS, False)}
        text, err = self.save()
        self.assertFalse(err, text)
        entry = self.stored("probe")
        self.assertIs(entry["rows_verified"], True)
        self.assertIn("rows_verified_at", entry)
        self.assertEqual(self.kql_calls(KQL), ["1h ago", "24h ago"])

    def test_save_query_keeps_caller_since_after_24h_retry(self):
        self.replies = {"6h ago": ("(no rows)", False), "24h ago": (ROWS, False)}
        text, err = self.save(since="6h ago")
        self.assertFalse(err, text)
        self.assertEqual(self.stored("probe")["since"], "6h ago")
        self.assertEqual(self.kql_calls(KQL), ["6h ago", "24h ago"])
        # Default window when the caller gives none.
        self.replies = {"1h ago": ("(no rows)", False), "24h ago": (ROWS, False)}
        self.save(name="second", kql=KQL + " | project body")
        self.assertEqual(self.stored("second")["since"], "1h ago")

    def test_save_query_retry_error_is_not_saved(self):
        self.replies = {
            "1h ago": ("(no rows)", False),
            "24h ago": ("<untrusted_log_data>boom", True),
        }
        text, err = self.save()
        self.assertTrue(err)
        self.assertIn("NOT saved - the query failed when verified", text)
        self.assertEqual(bm.load_learned(), [])
        # Error text from bzrk is fenced like the first-run error.
        self.assertIn(bm._UNTRUSTED_DATA_OPEN, text)

    def test_save_query_allow_empty_requires_json_true(self):
        self.default_reply = ("(no rows)", False)
        for bad in ("true", "True", 1, "yes", [True], {"a": 1}):
            text, err = self.save(allow_empty=bad)
            self.assertTrue(err, bad)
            self.assertEqual(bm.load_learned(), [], bad)
        text, err = self.save(allow_empty=True)
        self.assertFalse(err, text)
        self.assertIs(self.stored("probe")["rows_verified"], False)
        self.assertTrue(callable(bm.result_is_empty))

    def test_save_query_allow_empty_marks_unverified(self):
        self.default_reply = ("(no rows)", False)
        text, err = self.save(allow_empty=True)
        self.assertFalse(err, text)
        entry = self.stored("probe")
        self.assertIs(entry["rows_verified"], False)
        self.assertNotIn("rows_verified_at", entry)
        self.assertIn("NOT verified to return rows (allow_empty)", text)

    def test_save_query_count_zero_treated_as_empty(self):
        self.default_reply = ("n\n0", False)
        text, err = self.save(kql=TABLE + " | summarize n=count()")
        self.assertTrue(err)
        self.assertEqual(bm.load_learned(), [])
        self.assertTrue(bm.result_is_empty("n\n0"))

    def test_save_query_json_tables_with_no_rows_treated_as_empty(self):
        self.default_reply = (JSON_NO_ROWS, False)
        text, err = self.save()
        self.assertTrue(err)
        self.assertEqual(bm.load_learned(), [])
        self.assertTrue(bm.result_is_empty(JSON_NO_ROWS))

    def test_save_query_json_count_zero_treated_as_empty(self):
        self.default_reply = (JSON_COUNT_ZERO, False)
        text, err = self.save(kql=TABLE + " | summarize n=count()")
        self.assertTrue(err)
        self.assertEqual(bm.load_learned(), [])
        self.assertTrue(bm.result_is_empty(JSON_COUNT_ZERO))

    def test_save_query_success_text_reports_rows_or_allow_empty(self):
        text, err = self.save()
        self.assertFalse(err, text)
        self.assertIn("Saved 'probe'. Verified: returned rows.", text)
        self.assertNotIn("verified, returned data", text)
        self.assertIn("run_saved name=probe", text)
        self.default_reply = ("(no rows)", False)
        text, err = self.save(name="quiet", kql=KQL + " | project body", allow_empty=True)
        self.assertFalse(err, text)
        self.assertIn("Saved 'quiet'. NOT verified to return rows (allow_empty).", text)
        self.assertTrue(callable(bm.result_is_empty))

    def test_save_query_schema_accepts_allow_empty(self):
        tool = next(t for t in bm.TOOLS + bm.MGMT_TOOLS if t["name"] == "save_query")
        self.assertEqual(tool["inputSchema"]["properties"]["allow_empty"]["type"], "boolean")
        self.assertNotIn("allow_empty", tool["inputSchema"]["required"])

    def test_legacy_entry_without_rows_verified_loads_as_verified(self):
        shutil.copyfile(FIXTURE, self.store_path)
        items = bm.load_learned()
        self.assertTrue(items)
        for item in items:
            self.assertNotIn("rows_verified", item)
            self.assertIs(bm.entry_rows_verified(item), True)
        self.assertIs(bm.entry_rows_verified({"rows_verified": False}), False)
        self.assertIs(bm.entry_rows_verified({"rows_verified": True}), True)


class FingerprintTest(unittest.TestCase):
    def test_kql_fingerprint_vectors(self):
        fp = bm.kql_fingerprint
        same = [
            ("T | take 5", "  T  |\n\ttake   5 \n"),
            ("T | where a == 'x'  | take 5", "T | where a == 'x' | take 5"),
        ]
        different = [
            ("T | where a == 'x  y'", "T | where a == 'x y'"),
            ('T | where a == "x  y"', 'T | where a == "x y"'),
            ("T | where a == 'it\\'s  ok'", "T | where a == 'it\\'s ok'"),
            ("T|take 5", "T | take 5"),
            ("T | Take 5", "T | take 5"),
        ]
        for a, b in same:
            self.assertEqual(fp(a), fp(b), (a, b))
        for a, b in different:
            self.assertNotEqual(fp(a), fp(b), (a, b))
        self.assertRegex(fp("T"), r"^[0-9a-f]{64}$")
        # An unterminated literal must not raise.
        self.assertRegex(fp("T | where a == 'open  and  unclosed"), r"^[0-9a-f]{64}$")
        self.assertEqual(bm.KQL_FINGERPRINT_VERSION, 1)

    def test_fingerprint_with_other_version_is_recomputed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = (bm.LEARNED_PATH, bm.run_bzrk, bm.CACHE_TTL_SECONDS, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED)
        self.addCleanup(lambda: _restore(saved))
        bm.LEARNED_PATH = Path(tmp.name) / "learned.json"
        bm.CACHE_TTL_SECONDS = 0
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_DEEP
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (ROWS, False)
        kql_a = TABLE + " | take 1"
        kql_b = TABLE + " | take 2"
        # Stored hash and version are both stale: the hash is B's, the text is A.
        entries = [
            {
                "name": "old",
                "description": "Old.",
                "kql": kql_a,
                "since": "1h ago",
                "kql_sha256": bm.kql_fingerprint(kql_b),
                "kql_fp_v": 99,
            },
            {"name": "unversioned", "description": "No version.", "kql": TABLE + " | take 3", "since": "1h ago"},
        ]
        bm.LEARNED_PATH.write_text(json.dumps(entries), encoding="utf-8")
        # B is not a duplicate: the stale hash is ignored, not compared as it stands.
        text, err = bm.handle_call("save_query", {"name": "b_new", "description": "B.", "kql": kql_b})
        self.assertFalse(err, text)
        # A is a duplicate of "old", found by recomputing from its kql.
        text, err = bm.handle_call("save_query", {"name": "a_new", "description": "A.", "kql": kql_a})
        self.assertTrue(err)
        self.assertIn("old", text)
        # A stored hash with the current version is used as stored.
        written = next(it for it in bm.load_learned() if it["name"] == "b_new")
        self.assertEqual(written["kql_sha256"], bm.kql_fingerprint(kql_b))
        self.assertEqual(written["kql_fp_v"], bm.KQL_FINGERPRINT_VERSION)
        # Missing version also recomputes.
        text, err = bm.handle_call("save_query", {"name": "c_new", "description": "C.", "kql": TABLE + " |  take 3"})
        self.assertTrue(err)
        self.assertIn("unversioned", text)


def _restore(saved):
    bm.LEARNED_PATH, bm.run_bzrk, bm.CACHE_TTL_SECONDS, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = saved
    bm._RESULT_CACHE.clear()


class DuplicateTest(_SaveBase):
    def test_save_query_refuses_duplicate_fingerprint_under_new_name(self):
        text, err = self.save(name="first")
        self.assertFalse(err, text)
        self.assertIn("kql_sha256", self.stored("first"))
        spaced = KQL.replace(" | ", "  |\n  ")
        text, err = self.save(name="second", kql=spaced)
        self.assertTrue(err)
        self.assertIn("first", text)
        self.assertIsNone(self.stored("second"))
        # overwrite=true governs same-name replacement only.
        text, err = self.save(name="second", kql=spaced, overwrite=True)
        self.assertTrue(err)
        self.assertIsNone(self.stored("second"))
        # Same name and same fingerprint with overwrite=true is a replacement.
        text, err = self.save(name="first", kql=spaced, overwrite=True)
        self.assertFalse(err, text)
        self.assertEqual(len(bm.load_learned()), 1)

    def test_save_query_ignores_duplicate_the_caller_cannot_see(self):
        self.put(
            {
                "name": "sre_only",
                "description": "Hidden from soc.",
                "kql": KQL,
                "since": "1h ago",
                "roles": ["sre"],
            }
        )
        bm.ACTIVE_ROLE = "soc"
        text, err = self.save(name="mine")
        self.assertFalse(err, text)
        self.assertNotIn("sre_only", text)
        self.assertEqual({it["name"] for it in bm.load_learned()}, {"sre_only", "mine"})
        # The same KQL under a visible name is refused and named.
        text, err = self.save(name="again")
        self.assertTrue(err)
        self.assertIn("mine", text)
        self.assertTrue(callable(bm.kql_fingerprint))

    def test_generated_duplicate_fingerprint_is_skipped(self):
        self.put({"name": "human", "description": "Human.", "kql": KQL, "since": "1h ago", "roles": ["sre"]})
        before = self.store_path.read_text(encoding="utf-8")
        notified = []
        bm.set_tools_changed_notifier(lambda: notified.append(1))
        self.addCleanup(bm.set_tools_changed_notifier, None)
        amend = self.store_path.parent / "amendments_log.json"
        entry = {
            "name": "gen_dup",
            "description": "Generated.",
            "kql": KQL.replace(" | ", "   | "),
            "since": "1h ago",
        }
        log = bm.persist_learned_query(entry, action_source="generated")
        self.assertEqual(log["action"], "duplicate")
        self.assertEqual(log["duplicate_of"], "human")
        # Invisible to the active role or not, nothing is written.
        self.assertEqual(self.store_path.read_text(encoding="utf-8"), before)
        self.assertFalse(amend.exists())
        self.assertEqual(notified, [])
        self.assertTrue(callable(bm.kql_fingerprint))

    def test_generated_same_name_same_fingerprint_keeps_approval(self):
        approved = {
            "name": "gen_q",
            "description": "Generated.",
            "kql": KQL,
            "since": "1h ago",
            "origin": "generated",
            "status": bm.GENERATED_APPROVED,
            "approved_at": "2026-09-26T00:00:00Z",
        }
        self.put(approved)
        before = self.store_path.read_text(encoding="utf-8")
        entry = {"name": "gen_q", "description": "Again.", "kql": KQL + "  ", "since": "1h ago"}
        log = bm.persist_learned_query(entry, action_source="generated")
        self.assertEqual(log, {"name": "gen_q", "action": "unchanged"})
        self.assertEqual(self.store_path.read_text(encoding="utf-8"), before)
        item = self.stored("gen_q")
        self.assertEqual(item["status"], bm.GENERATED_APPROVED)
        self.assertEqual(item["approved_at"], "2026-09-26T00:00:00Z")
        # New KQL under the same name still resets to pending.
        entry = {"name": "gen_q", "description": "New.", "kql": KQL + " | take 1", "since": "1h ago"}
        log = bm.persist_learned_query(entry, action_source="generated")
        self.assertEqual(log["action"], "generated")
        self.assertEqual(self.stored("gen_q")["status"], bm.GENERATED_PENDING)
        self.assertIn("kql_sha256", self.stored("gen_q"))

    def test_all_duplicate_job_is_done_and_lists_duplicates(self):
        self.put({"name": "human", "description": "Human.", "kql": KQL, "since": "1h ago"})
        queries = [{"name": "dup_q", "description": "Dup.", "kql": KQL, "since": "1h ago"}]
        report, ok = bm.parser_factory._persist_validated_queries(
            queries, "svc", "service", "p", "m", "hash", "fresh", [], "", 1
        )
        self.assertTrue(ok)
        self.assertEqual(report["status"], "done")
        inner = report["report"]
        self.assertEqual(inner["queries_saved"], [])
        self.assertEqual(inner["duplicates"], [{"name": "dup_q", "duplicate_of": "human"}])
        self.assertEqual([it["name"] for it in bm.load_learned()], ["human"])

    def test_job_report_does_not_name_a_duplicate_hidden_from_the_role(self):
        # generate_parser returns this report to the caller, so a duplicate
        # the active role cannot see must not be named in it.
        self.put({"name": "sre_hidden_query", "description": "H.", "kql": KQL, "since": "1h ago", "roles": ["sre"]})
        bm.ACTIVE_ROLE = "soc"
        queries = [{"name": "dup_q", "description": "Dup.", "kql": KQL, "since": "1h ago"}]
        report, ok = bm.parser_factory._persist_validated_queries(
            queries, "svc", "service", "p", "m", "hash", "fresh", [], "", 1
        )
        self.assertTrue(ok)
        self.assertNotIn("sre_hidden_query", json.dumps(report))
        self.assertEqual(report["report"]["duplicates"], [{"name": "dup_q", "duplicate_of": "(hidden)"}])
        self.assertEqual([it["name"] for it in bm.load_learned()], ["sre_hidden_query"])

    def test_persist_validated_queries_unchanged_counts_as_saved(self):
        self.put(
            {
                "name": "gen_q",
                "description": "G.",
                "kql": KQL,
                "since": "1h ago",
                "origin": "generated",
                "status": bm.GENERATED_APPROVED,
            }
        )
        queries = [{"name": "gen_q", "description": "G.", "kql": KQL, "since": "1h ago"}]
        report, ok = bm.parser_factory._persist_validated_queries(
            queries, "svc", "service", "p", "m", "hash", "fresh", [], "", 1
        )
        self.assertTrue(ok)
        self.assertEqual(report["report"]["queries_saved"], ["gen_q"])
        self.assertEqual(report["report"]["duplicates"], [])

    def test_persist_validated_queries_tolerates_mock_without_action(self):
        queries = [{"name": "q1", "description": "Q.", "kql": KQL, "since": "1h ago"}]
        original = bm.parser_factory._persist_learned_query
        self.addCleanup(setattr, bm.parser_factory, "_persist_learned_query", original)
        bm.parser_factory._persist_learned_query = lambda entry, action_source: {"name": "q1"}
        report, ok = bm.parser_factory._persist_validated_queries(
            queries, "svc", "service", "p", "m", "hash", "fresh", [], "", 1
        )
        self.assertTrue(ok)
        self.assertEqual(report["report"]["queries_saved"], ["q1"])
        self.assertEqual(report["report"]["duplicates"], [])


if __name__ == "__main__":
    unittest.main()
