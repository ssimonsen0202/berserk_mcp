"""Model-facing output budget, compact JSON tool output, and bounded fleet
caches (v1.36.0).

search and saved queries run user-written KQL and used to return the whole
bzrk result to the model, bounded only by MAX_BZRK_RESULT_BYTES (10 MB). The
budget is applied to the raw result *before* fencing, so the
<untrusted_log_data> fence stays intact and the truncation note is server
text outside it.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402


def _tables_doc(n_rows, cell="x" * 90):
    return json.dumps(
        {
            "Tables": [
                {
                    "schema": {"columns": [{"name": "i"}, {"name": "body"}]},
                    "rows": [[i, cell] for i in range(n_rows)],
                }
            ]
        }
    )


def _fenced_body(text):
    start = text.index(bm._UNTRUSTED_DATA_OPEN) + len(bm._UNTRUSTED_DATA_OPEN)
    end = text.index(bm._UNTRUSTED_DATA_CLOSE)
    return text[start:end].strip()


class _BzrkHarness(unittest.TestCase):
    """Patches run_bzrk with a canned --json result and isolates the learned
    store and the fleet caches, so every test starts from a clean server."""

    def setUp(self):
        self.result = "(no rows)"
        self._patches = [
            mock.patch.object(bm, "run_bzrk", self._fake_run_bzrk),
            mock.patch.object(bm, "MAX_OUTPUT_CHARS", 5000),
        ]
        self._tmp = tempfile.TemporaryDirectory()
        self._patches.append(mock.patch.object(bm, "LEARNED_PATH", Path(self._tmp.name) / "learned.json"))
        for p in self._patches:
            p.start()
        bm._reset_fleet_state()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        bm._reset_fleet_state()

    def _fake_run_bzrk(self, args, timeout=bm.DEFAULT_TIMEOUT):
        return self.result, False

    def assert_single_fence_with_note_after(self, text):
        self.assertEqual(text.count(bm._UNTRUSTED_DATA_OPEN), 1, text[:300])
        self.assertEqual(text.count(bm._UNTRUSTED_DATA_CLOSE), 1, text[-300:])
        close_at = text.index(bm._UNTRUSTED_DATA_CLOSE)
        note_at = text.index("[berserk-mcp: result truncated")
        self.assertGreater(note_at, close_at, "truncation note must sit outside the fence")


class SearchOutputBudgetTest(_BzrkHarness):
    def test_small_result_passes_through_unchanged(self):
        self.result = _tables_doc(3)
        text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | take 3"})
        self.assertFalse(err, text)
        self.assertEqual(_fenced_body(text), self.result)
        self.assertNotIn("[berserk-mcp: result truncated", text)

    def test_large_result_is_cut_to_the_budget(self):
        self.result = _tables_doc(1000)
        text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | take 1000"})
        self.assertFalse(err, text)
        body = _fenced_body(text)
        self.assertLessEqual(len(body), bm.MAX_OUTPUT_CHARS)
        rows = json.loads(body)["Tables"][0]["rows"]
        self.assertGreater(len(rows), 0)
        self.assertLess(len(rows), 1000)
        self.assertEqual(rows[0][0], 0, "keeps the first rows, in order")
        self.assertIn(f"showing {len(rows)} of 1000 rows", text)
        self.assert_single_fence_with_note_after(text)

    def test_unbounded_query_is_cut_too(self):
        # The case that motivated the budget: no take/top at all.
        self.result = _tables_doc(1000)
        text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | where body has 'x'"})
        self.assertFalse(err, text)
        self.assertLessEqual(len(_fenced_body(text)), bm.MAX_OUTPUT_CHARS)
        self.assert_single_fence_with_note_after(text)

    def test_zero_budget_disables_the_cap(self):
        self.result = _tables_doc(1000)
        with mock.patch.object(bm, "MAX_OUTPUT_CHARS", 0):
            text, err = bm.handle_call("search", {"kql": f"{bm.TABLE} | take 1000"})
        self.assertFalse(err, text)
        self.assertEqual(_fenced_body(text), self.result)


class SavedQueryOutputBudgetTest(_BzrkHarness):
    def _save(self):
        self.result = _tables_doc(1)
        text, err = bm.handle_call(
            "save_query",
            {"name": "budget_probe", "description": "d", "kql": f"{bm.TABLE} | take 1000"},
        )
        self.assertFalse(err, text)
        bm._reset_fleet_state()
        self.result = _tables_doc(1000)

    def test_run_saved_is_cut_to_the_budget(self):
        self._save()
        text, err = bm.handle_call("run_saved", {"name": "budget_probe"})
        self.assertFalse(err, text)
        self.assertLessEqual(len(_fenced_body(text)), bm.MAX_OUTPUT_CHARS)
        self.assert_single_fence_with_note_after(text)

    def test_projected_saved_tool_is_cut_to_the_budget(self):
        self._save()
        text, err = bm.handle_call("saved__budget_probe", {})
        self.assertFalse(err, text)
        self.assertLessEqual(len(_fenced_body(text)), bm.MAX_OUTPUT_CHARS)
        self.assert_single_fence_with_note_after(text)


class LimitModelOutputTest(unittest.TestCase):
    """The helper itself, for shapes the dispatch tests don't reach."""

    def test_row_larger_than_budget_keeps_zero_rows(self):
        out, note = bm._limit_model_output(_tables_doc(5, cell="y" * 400), 300)
        self.assertEqual(json.loads(out)["Tables"][0]["rows"], [])
        self.assertIn("showing 0 of 5 rows", note)

    def test_bare_json_list_is_cut_by_rows(self):
        raw = json.dumps([{"i": i, "body": "z" * 50} for i in range(200)])
        out, note = bm._limit_model_output(raw, 2000)
        rows = json.loads(out)
        self.assertLessEqual(len(out), 2000)
        self.assertEqual(rows[0]["i"], 0)
        self.assertIn(f"showing {len(rows)} of 200 rows", note)

    def test_table_text_is_cut_at_a_line_boundary(self):
        lines = ["| i | body |"] + [f"| {i} | {'w' * 40} |" for i in range(300)]
        raw = "\n".join(lines)
        out, note = bm._limit_model_output(raw, 1000)
        self.assertLessEqual(len(out), 1000)
        self.assertTrue(out.startswith("| i | body |"), "keeps the header line")
        self.assertIn(out.splitlines()[-1], lines, "never cuts mid-line")
        self.assertIn("result truncated", note)

    def test_unrecognized_json_shape_is_cut_as_text(self):
        raw = json.dumps({"something": "q" * 5000})
        out, note = bm._limit_model_output(raw, 1000)
        self.assertLessEqual(len(out), 1000)
        self.assertIn("result truncated", note)

    def test_sentinels_and_small_output_are_untouched(self):
        for raw in ("(no rows)", bm.AUTH_FAILURE_MESSAGE, _tables_doc(2)):
            self.assertEqual(bm._limit_model_output(raw, 5000), (raw, ""))


class CompactJsonOutputTest(unittest.TestCase):
    def test_find_tool_returns_compact_json(self):
        text, err = bm.handle_call("find_tool", {"intent": "which services have the most errors"})
        self.assertFalse(err, text)
        payload = json.loads(text)
        self.assertIn("candidates", payload)
        self.assertNotIn("\n  ", text, "tool output JSON should not be pretty-printed")


class BoundedFleetCacheTest(_BzrkHarness):
    def test_result_cache_never_exceeds_max_entries(self):
        self.result = _tables_doc(1)
        with mock.patch.object(bm, "CACHE_MAX_ENTRIES", 10):
            for minutes in range(1, 40):
                text, err = bm.handle_call("list_services", {"since": f"{minutes}m ago"})
                self.assertFalse(err, text)
            self.assertLessEqual(len(bm._RESULT_CACHE), 10)

    def test_newest_entries_survive_eviction(self):
        cache = {}
        with mock.patch.object(bm, "CACHE_MAX_ENTRIES", 3):
            for i in range(6):
                bm._bounded_put(cache, f"k{i}", ("v", False, 100.0), ttl=60, now=100.0)
        self.assertEqual(list(cache), ["k3", "k4", "k5"])

    def test_expired_entries_are_swept_on_insert(self):
        cache = {}
        with mock.patch.object(bm, "CACHE_MAX_ENTRIES", 100):
            bm._bounded_put(cache, "old", ("v", False, 0.0), ttl=60, now=0.0)
            bm._bounded_put(cache, "new", ("v", False, 500.0), ttl=60, now=500.0)
        self.assertEqual(list(cache), ["new"])

    def test_reinserting_a_key_moves_it_to_newest(self):
        cache = {}
        with mock.patch.object(bm, "CACHE_MAX_ENTRIES", 2):
            bm._bounded_put(cache, "a", ("v", False, 1.0), ttl=60, now=1.0)
            bm._bounded_put(cache, "b", ("v", False, 2.0), ttl=60, now=2.0)
            bm._bounded_put(cache, "a", ("v", False, 3.0), ttl=60, now=3.0)
            bm._bounded_put(cache, "c", ("v", False, 4.0), ttl=60, now=4.0)
        self.assertEqual(list(cache), ["a", "c"])


def _rpc_text(name, arguments):
    """Call a tool through dispatch(), the boundary where
    secret_scan.apply_output_filter runs -- handle_call() never reaches it."""
    resp = bm.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    return resp["result"]["content"][0]["text"]


class RedactionAfterCompactionTest(_BzrkHarness):
    """v1.36.0 re-serializes truncated results and eight tool outputs as
    compact JSON ("key":"value", no space, ensure_ascii=False). The redactor
    runs after that, so it must still find credentials in the new form."""

    SECRETS = ("hunter2-secret-0042", "sk-live-abcdef0123456789abcdef")

    def assert_redacted(self, text):
        for secret in self.SECRETS:
            self.assertNotIn(secret, text)

    def test_truncated_bare_list_rows_are_redacted(self):
        self.result = json.dumps(
            [{"i": i, "password": self.SECRETS[0], "api_key": self.SECRETS[1], "pad": "p" * 80} for i in range(500)]
        )
        text = _rpc_text("search", {"kql": f"{bm.TABLE} | take 500"})
        self.assertIn("[berserk-mcp: result truncated", text)
        self.assert_redacted(text)
        self.assert_single_fence_with_note_after(text)

    def test_truncated_tables_rows_are_redacted(self):
        body = f'login failed password="{self.SECRETS[0]}" api_key={self.SECRETS[1]}'
        self.result = _tables_doc(500, cell=body)
        text = _rpc_text("search", {"kql": f"{bm.TABLE} | take 500"})
        self.assertIn("[berserk-mcp: result truncated", text)
        self.assert_redacted(text)
        self.assert_single_fence_with_note_after(text)


class CanonloomCompactRedactionTest(unittest.TestCase):
    def setUp(self):
        import _http

        payload = {
            "artifacts": [{"name": "a", "password": "hunter2-secret-0042", "api_key": "sk-live-abcdef0123456789abcdef"}]
        }
        self._patches = [
            mock.patch.object(_http, "http_get_json", lambda *a, **k: (payload, None)),
            mock.patch.dict("os.environ", {"CANONLOOM_SERVER_URL": "http://127.0.0.1:19999", "CANONLOOM_API_KEY": "k"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def test_compact_canonloom_payload_is_redacted(self):
        text = _rpc_text("canonloom_list_artifacts", {})
        self.assertIn('"artifacts"', text, text[:300])
        self.assertNotIn("hunter2-secret-0042", text)
        self.assertNotIn("sk-live-abcdef0123456789abcdef", text)


if __name__ == "__main__":
    unittest.main()
