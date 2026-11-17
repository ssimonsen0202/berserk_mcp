"""WP2: saved queries are findable in every tool-listing mode (F1).

Every test names the new code (a new function, constant or argument) inside
its own body, so each one fails on its own against code that lacks it. bzrk
is stubbed; nothing here touches a live Berserk or the real store.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

import berserk_mcp as bm  # noqa: E402
import tool_collisions as tc  # noqa: E402
import tool_discovery as td  # noqa: E402
from berserk_mcp import learned as bm_learned  # noqa: E402

TABLE = bm.TABLE
KQL = TABLE + " | where severity_text == 'ERROR' | take 5"
_MISSING = object()


class _SavedDiscoveryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        names = ("LEARNED_PATH", "ACTIVE_ROLE", "ACTIVE_TIER_RESOLVED", "SAVED_TOOL_PROJECTION_CAP")
        saved = {n: getattr(bm, n) for n in names}
        saved_discovery = bm.BERSERK_MCP_DISCOVERY
        saved_cap = getattr(bm, "DISCOVERY_SAVED_CAP", _MISSING)

        def restore():
            for n, v in saved.items():
                setattr(bm, n, v)
            bm.BERSERK_MCP_DISCOVERY = saved_discovery
            if saved_cap is _MISSING:
                bm.__dict__.pop("DISCOVERY_SAVED_CAP", None)
            else:
                bm.DISCOVERY_SAVED_CAP = saved_cap

        self.addCleanup(restore)
        self.store_path = Path(self._tmp.name) / "learned.json"
        bm.LEARNED_PATH = self.store_path
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_DEEP
        bm.SAVED_TOOL_PROJECTION_CAP = 25
        bm.BERSERK_MCP_DISCOVERY = False

    def put(self, *entries):
        self.store_path.write_text(json.dumps(list(entries)), encoding="utf-8")

    @staticmethod
    def entry(name, description, **extra):
        item = {"name": name, "description": description, "kql": KQL, "since": "1h ago"}
        item.update(extra)
        return item

    @staticmethod
    def generated(name, description, **extra):
        return _SavedDiscoveryBase.entry(name, description, origin="generated", **extra)

    def find(self, intent):
        text, is_error = bm.handle_call("find_tool", {"intent": intent})
        self.assertFalse(is_error, text)
        return json.loads(text)

    def candidate_names(self, intent):
        return [c["name"] for c in self.find(intent)["candidates"]]

    def listed_names(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return [t["name"] for t in resp["result"]["tools"]]


class FindToolSavedTest(_SavedDiscoveryBase):
    def test_find_tool_returns_saved_query_by_description(self):
        self.put(self.entry("zeta_probe", "Counts quarantined widgets per shipping lane."))
        docs = bm.saved_query_index_docs()
        self.assertEqual([d["name"] for d in docs], ["saved__zeta_probe"])
        payload = self.find("quarantined widgets shipping lane")
        self.assertEqual(payload["resultType"], "complete")
        cand = payload["candidates"][0]
        self.assertEqual(cand["name"], "saved__zeta_probe")
        projected = {t["name"]: t for t in bm._saved_query_tools()}["saved__zeta_probe"]
        self.assertEqual(cand["description"], projected["description"])
        self.assertEqual(cand["inputSchema"], projected["inputSchema"])
        self.assertIn("since", cand["inputSchema"]["properties"])

    def test_find_tool_candidate_is_the_fenced_projection_for_generated_entries(self):
        self.put(self.generated("gen_probe", "Counts quarantined widgets.", status="approved"))
        cand = self.find("quarantined widgets")["candidates"][0]
        self.assertEqual(cand["name"], "saved__gen_probe")
        self.assertTrue(cand["description"].startswith("<generated-description>"))
        self.assertTrue(cand["description"].endswith("</generated-description>"))
        # The index text is never shown: no fence in the index document.
        doc = bm.saved_query_index_docs()[0]
        self.assertNotIn("generated-description", doc["description"])

    def test_find_tool_searches_beyond_projection_cap(self):
        bm.SAVED_TOOL_PROJECTION_CAP = 2
        self.put(
            self.entry("old_target", "Tracks flurbo exhaustion on gateways."),
            self.entry("q2", "Second."),
            self.entry("q3", "Third."),
            self.entry("q4", "Fourth."),
            self.entry("q5", "Fifth."),
            self.entry("q6", "Sixth."),
        )
        self.assertEqual(len(bm.saved_query_index_docs()), 6)
        self.assertNotIn("saved__old_target", {t["name"] for t in bm._saved_query_tools()})
        self.assertEqual(self.candidate_names("flurbo exhaustion gateways")[0], "saved__old_target")

    def test_find_tool_hides_role_hidden_and_pending_entries_in_small_tier(self):
        self.put(
            self.entry("open_one", "Tallies wibble drift."),
            self.entry("soc_only", "Tallies wibble drift for the SOC.", roles=["soc"]),
            self.generated("pending_one", "Tallies wibble drift, generated."),
        )
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "sre", bm.TIER_SMALL
        names = {d["name"] for d in bm.saved_query_index_docs()}
        self.assertEqual(names, {"saved__open_one"})
        found = self.candidate_names("tallies wibble drift")
        self.assertIn("saved__open_one", found)
        self.assertNotIn("saved__soc_only", found)
        self.assertNotIn("saved__pending_one", found)
        # Same store, deep tier: the pending entry is visible again, the
        # role-hidden one is not. A cached index must not leak across tiers.
        bm.ACTIVE_TIER_RESOLVED = bm.TIER_DEEP
        found = self.candidate_names("tallies wibble drift")
        self.assertIn("saved__pending_one", found)
        self.assertNotIn("saved__soc_only", found)

    def test_find_tool_saved_candidates_capped_at_two_when_builtins_match(self):
        self.put(*[self.entry(f"cpu_{i}", "Top cpu containers by load, ranked cpu cpu cpu.") for i in range(6)])
        names = self.candidate_names("top cpu containers")
        self.assertEqual(len(names), 5)
        self.assertEqual(len([n for n in names if n.startswith("saved__")]), 2)
        self.assertIn("top_cpu", names)

    def test_find_tool_saved_candidates_fill_the_list_when_no_builtin_matches(self):
        self.put(*[self.entry(f"grommet_{i}", "Measures grommet wear.") for i in range(7)])
        names = self.candidate_names("grommet wear")
        self.assertEqual(len(names), 5)
        self.assertTrue(all(n.startswith("saved__") for n in names))
        self.assertTrue(callable(bm.saved_query_index_docs))

    def test_find_tool_builtin_wins_ties(self):
        # "introspection" is in the schema tool's description only, never in a
        # tool name, so a saved entry with the same word in its description
        # scores exactly the same.
        builtin_only = td.search(bm._DISCOVERY_INDEX, "introspection", top_k=5)
        self.assertEqual([n for n, _ in builtin_only], ["schema"])
        self.put(
            self.entry("aaa_early", "Introspection helper."),
            self.entry("bbb_late", "Introspection helper too."),
        )
        self.assertEqual(len(bm.saved_query_index_docs()), 2)
        names = self.candidate_names("introspection")
        self.assertEqual(names[0], "schema")
        self.assertEqual(set(names[1:]), {"saved__aaa_early", "saved__bbb_late"})
        self.assertEqual(names, self.candidate_names("introspection"))

    def test_find_tool_keyword_stuffed_generated_description_cannot_displace_builtins(self):
        stuffed = ("top cpu containers memory host errors services logs list " * 6)[:240]
        self.put(*[self.generated(f"stuff_{i}", stuffed, status="approved") for i in range(8)])
        names = self.candidate_names("top cpu containers")
        self.assertEqual(len(names), 5)
        self.assertLessEqual(len([n for n in names if n.startswith("saved__")]), 2)
        self.assertIn("top_cpu", names)
        self.assertGreaterEqual(len([n for n in names if not n.startswith("saved__")]), 3)
        self.assertTrue(bm.saved_query_index_docs())

    def test_find_tool_without_saved_entries_matches_builtin_index(self):
        self.put()
        names = self.candidate_names("which containers use the most cpu")
        expected = [n for n, _ in td.search(bm._DISCOVERY_INDEX, "which containers use the most cpu", top_k=5)]
        self.assertEqual(names, expected)
        self.assertEqual(bm.saved_query_index_docs(), [])

    def test_index_rebuilds_after_store_change(self):
        self.put(self.entry("first_one", "Measures zorblax latency."))
        with mock.patch.object(td, "build_index", wraps=td.build_index) as build:
            self.assertEqual(self.candidate_names("zorblax latency")[0], "saved__first_one")
            self.assertEqual(build.call_count, 1)
            self.candidate_names("zorblax latency")
            self.candidate_names("something else entirely")
            self.assertEqual(build.call_count, 1, "an unchanged store must not rebuild the index")
            # Size changes.
            self.put(
                self.entry("first_one", "Measures zorblax latency."),
                self.entry("second_one", "Measures quuxal throughput."),
            )
            self.assertEqual(self.candidate_names("quuxal throughput")[0], "saved__second_one")
            self.assertEqual(build.call_count, 2)
            # Same size, newer mtime: still a rebuild.
            before = self.store_path.stat()
            text = self.store_path.read_text(encoding="utf-8").replace("quuxal", "quuxam")
            self.store_path.write_text(text, encoding="utf-8")
            os.utime(self.store_path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
            self.assertEqual(self.store_path.stat().st_size, before.st_size)
            self.assertEqual(self.candidate_names("quuxam throughput")[0], "saved__second_one")
            self.assertEqual(build.call_count, 3)

    def test_corrupt_or_missing_store_falls_back_to_builtin_only(self):
        self.assertEqual(bm.saved_query_index_docs(), [])
        self.assertEqual(self.candidate_names("top cpu containers")[0], "top_cpu")
        self.store_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(bm.saved_query_index_docs(), [])
        self.assertEqual(self.candidate_names("top cpu containers")[0], "top_cpu")
        bm.BERSERK_MCP_DISCOVERY = True
        self.assertIn("find_tool", self.listed_names())

    def test_saved_tool_found_through_find_tool_is_callable(self):
        self.put(self.entry("callable_one", "Reports blorp counts."))
        cand = self.find("blorp counts")["candidates"][0]
        self.assertEqual(cand["name"], "saved__callable_one")
        self.assertTrue(bm.saved_query_index_docs())
        calls = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            calls.append(list(args))
            return ("col\nrow", False)

        with mock.patch.object(bm, "run_bzrk", fake):
            text, is_error = bm.handle_call(cand["name"], {})
        self.assertFalse(is_error, text)
        self.assertTrue(calls)


class DiscoveryModeListingTest(_SavedDiscoveryBase):
    def seed_many(self, n=8):
        self.put(*[self.entry(f"q{i}", f"Query number {i}.") for i in range(1, n + 1)])

    def test_discovery_mode_lists_saved_tools_up_to_cap(self):
        bm.BERSERK_MCP_DISCOVERY = True
        bm.DISCOVERY_SAVED_CAP = 3
        self.seed_many()
        names = self.listed_names()
        saved = [n for n in names if n.startswith("saved__")]
        # WP4: projection order is rank order; with no usage that is newest first.
        self.assertEqual(saved, ["saved__q8", "saved__q7", "saved__q6"])
        self.assertTrue(set(bm._ANCHOR_TOOL_NAMES) <= set(names))
        self.assertEqual(len(names), len(bm._ANCHOR_TOOL_NAMES) + 3)

    def test_discovery_mode_saved_cap_zero_lists_none(self):
        bm.BERSERK_MCP_DISCOVERY = True
        bm.DISCOVERY_SAVED_CAP = 0
        self.seed_many()
        names = self.listed_names()
        self.assertEqual([n for n in names if n.startswith("saved__")], [])
        self.assertEqual(set(names), set(bm._ANCHOR_TOOL_NAMES))
        self.assertEqual(len(bm.saved_query_index_docs()), 8)

    def test_discovery_mode_hides_role_hidden_saved_tools(self):
        bm.BERSERK_MCP_DISCOVERY = True
        bm.DISCOVERY_SAVED_CAP = 5
        bm.ACTIVE_ROLE = "sre"
        self.put(self.entry("shown", "Shown."), self.entry("hidden", "Hidden.", roles=["soc"]))
        saved = [n for n in self.listed_names() if n.startswith("saved__")]
        self.assertEqual(saved, ["saved__shown"])

    def test_full_listing_is_unchanged_outside_discovery_mode(self):
        bm.DISCOVERY_SAVED_CAP = 0
        self.seed_many(3)
        saved = [n for n in self.listed_names() if n.startswith("saved__")]
        # Same entries as before WP4; they now list best ranked first, and with
        # no usage the ranking is newest first.
        self.assertEqual(saved, ["saved__q3", "saved__q2", "saved__q1"])
        self.assertEqual(len(bm.saved_query_index_docs()), 3)

    def test_saved_cap_default_and_invalid_values(self):
        parse = bm.discovery_saved_cap_from_env
        self.assertEqual(parse(None), 5)
        self.assertEqual(parse(""), 5)
        self.assertEqual(parse("0"), 0)
        self.assertEqual(parse("3"), 3)
        self.assertEqual(parse("lots"), 5)
        self.assertEqual(parse("-2"), 5)


class ListSavedQueryArgumentTest(_SavedDiscoveryBase):
    def lines(self, arguments):
        text, is_error = bm.handle_call("list_saved", arguments)
        self.assertFalse(is_error, text)
        return [ln for ln in text.splitlines() if ln.startswith("- ")]

    def test_list_saved_query_argument_ranks_and_limits(self):
        entries = [self.entry(f"misc_{i}", "Reports kelvar usage for batch jobs.") for i in range(11)]
        entries.append(self.entry("kelvar_report", "Reports kelvar kelvar usage."))
        entries.append(self.entry("unrelated", "Counts something else."))
        self.put(*entries)
        self.assertEqual(len(bm.saved_query_index_docs()), 13)
        lines = self.lines({"query": "kelvar usage"})
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[0].startswith("- kelvar_report:"), lines[0])
        self.assertFalse(any("unrelated" in ln for ln in lines))
        # Without the argument the listing is unchanged: every entry.
        self.assertEqual(len(self.lines({})), 13)
        self.assertEqual(len(self.lines({"query": ""})), 13)

    def test_list_saved_query_argument_hides_hidden_entries(self):
        self.put(
            self.entry("seen", "Reports kelvar usage."),
            self.entry("soc_only", "Reports kelvar usage.", roles=["soc"]),
            self.generated("pending", "Reports kelvar usage."),
        )
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "sre", bm.TIER_SMALL
        lines = self.lines({"query": "kelvar usage"})
        self.assertEqual(len(lines), 1)
        self.assertIn("seen", lines[0])
        self.assertTrue(bm.saved_query_index_docs())

    def test_list_saved_query_argument_with_no_match_says_so(self):
        self.put(self.entry("only", "Reports kelvar usage."))
        text, is_error = bm.handle_call("list_saved", {"query": "zzzzqqqq"})
        self.assertFalse(is_error)
        self.assertNotIn("- only", text)
        self.assertIn("No saved queries match", text)

    def test_list_saved_query_argument_rejects_long_or_non_string(self):
        self.put(self.entry("only", "Reports kelvar usage."))
        text, is_error = bm.handle_call("list_saved", {"query": "x" * 201})
        self.assertTrue(is_error)
        self.assertIn("200", text)
        text, is_error = bm.handle_call("list_saved", {"query": "x" * 200})
        self.assertFalse(is_error)
        text, is_error = bm.handle_call("list_saved", {"query": ["kelvar"]})
        self.assertTrue(is_error)

    def test_list_saved_schema_declares_query_argument(self):
        tool = next(t for t in bm.TOOLS + bm.MGMT_TOOLS if t["name"] == "list_saved")
        spec = tool["inputSchema"]["properties"]["query"]
        self.assertEqual(spec["type"], "string")
        self.assertEqual(spec["maxLength"], 200)
        self.assertNotIn("required", tool["inputSchema"])


class BuiltinWeightsFrozenTest(_SavedDiscoveryBase):
    QUERIES = (
        "what containers are running",
        "top cpu containers",
        "find traces with errors",
        "logs for the checkout service",
    )

    def docs(self, n=500):
        return [
            {"name": f"saved__s{i}", "description": f"Counts containers errors logs cpu traces service {i}."}
            for i in range(n)
        ]

    def test_saved_entries_never_change_builtin_scores(self):
        tools = bm.TOOLS + bm.MGMT_TOOLS
        base = bm._DISCOVERY_INDEX
        docs = self.docs()
        combined = td.build_index(tools + docs)
        order = [t["name"] for t in tools] + [d["name"] for d in docs]
        builtin_names = {t["name"] for t in tools}
        for q in self.QUERIES:
            expected = dict(td.search(base, q, top_k=len(tools)))
            got = dict(td.search(combined, q, top_k=len(order), tool_order=order, weights_from=base))
            self.assertEqual({n: v for n, v in got.items() if n in builtin_names}, expected, q)
        # Without frozen weights the scores do move, so the check above bites.
        shifted = dict(td.search(combined, self.QUERIES[0], top_k=len(order), tool_order=order))
        plain = dict(td.search(base, self.QUERIES[0], top_k=len(tools)))
        self.assertNotEqual({n: v for n, v in shifted.items() if n in builtin_names}, plain)

    def test_saved_only_word_is_still_findable(self):
        self.put(self.entry("only_one", "Measures snorfle saturation."))
        # "snorfle" is in no built-in description: df=0 with the same smoothing.
        self.assertNotIn("snorfl", bm._DISCOVERY_INDEX[1])
        self.assertEqual(self.candidate_names("snorfle saturation")[0], "saved__only_one")
        ranked, _tools = bm.rank_discovery("snorfle")
        self.assertEqual(ranked[0][0], "saved__only_one")
        self.assertGreater(ranked[0][1], 0)
        self.assertEqual(td.search(bm._DISCOVERY_INDEX, "snorfle", weights_from=bm._DISCOVERY_INDEX), [])


class DescriptionTest(unittest.TestCase):
    def test_find_tool_description_mentions_saved_tools_and_passes_collision_check(self):
        tools = bm.TOOLS + bm.MGMT_TOOLS
        desc = next(t for t in tools if t["name"] == "find_tool")["description"]
        self.assertIn("saved", desc.lower())
        self.assertIn("saved__", desc)
        self.assertIn("built-in", desc.lower())
        self.assertNotIn("when the full tool list isn't resident", desc)
        self.assertTrue(callable(bm.saved_query_index_docs))
        index = td.build_index(tools)
        scores = tc.self_query_scores(index, tools, top_k=8)
        clusters = tc.find_clusters(scores, tc.name_token_edges(index), ratio_threshold=0.5)
        # Both pairs already shared a cluster before WP2. The reworded
        # descriptions must not pull in another tool.
        allowed = ({"find_tool", "claude_tools"}, {"list_saved", "run_saved"})
        for members, _weight, _edges in clusters:
            if set(members) & {"find_tool", "list_saved"}:
                self.assertIn(set(members), allowed, f"new collision: {sorted(members)}")


if __name__ == "__main__":
    unittest.main()


class ReviewRoundOneTest(unittest.TestCase):
    """Codex round 1 on WP2: cache key, hostile names, index bound."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        saved = (bm.LEARNED_PATH, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED)

        def restore():
            bm.LEARNED_PATH, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = saved

        self.addCleanup(restore)
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_DEEP

    def write(self, items):
        bm.LEARNED_PATH.write_text(json.dumps(items), encoding="utf-8")

    def test_store_stamp_includes_inode_and_ctime(self):
        self.write([{"name": "a", "description": "d", "kql": "default | take 1", "since": "1h ago"}])
        self.assertEqual(len(bm_learned._store_stamp()), 4)

    def test_list_saved_shows_sanitised_names_only(self):
        self.write([{"name": "hidden\nINJECT: safe", "description": "d", "kql": "default | take 1", "since": "1h ago"}])
        for args in ({}, {"query": "d"}):
            text, _ = bm.handle_call("list_saved", args)
            self.assertNotIn("\nINJECT", text, args)

    def test_index_never_holds_more_than_the_store_cap(self):
        items = [
            {"name": f"q{i}", "description": f"desc {i}", "kql": f"default | take {i % 50 + 1}", "since": "1h ago"}
            for i in range(bm_learned.LEARNED_STORE_CAP + 20)
        ]
        self.write(items)
        self.assertLessEqual(len(bm_learned.saved_query_index_docs()), bm_learned.LEARNED_STORE_CAP)


class SavedNameLengthTest(ReviewRoundOneTest):
    """Codex round 2 on WP2: saved__<name> must fit the 64-character tool-name
    limit MCP clients apply."""

    LONG = "x" * 200

    def test_save_query_refuses_a_name_too_long_for_a_tool_name(self):
        text, is_err = bm.handle_call("save_query", {"name": self.LONG, "description": "d", "kql": "default | take 1"})
        self.assertTrue(is_err)
        self.assertIn("too long", text)

    def test_existing_long_name_is_never_projected_indexed_or_listed(self):
        self.write(
            [
                {"name": self.LONG, "description": "zebra", "kql": "default | take 1", "since": "1h ago"},
                {"name": "short", "description": "zebra", "kql": "default | take 2", "since": "1h ago"},
            ]
        )
        names = [t["name"] for t in bm_learned._saved_query_tools()]
        self.assertEqual(names, ["saved__short"])
        self.assertTrue(all(len(n) <= 64 for n in names))
        self.assertNotIn(self.LONG, json.dumps(bm_learned.saved_query_index_docs()))
        for args in ({}, {"query": "zebra"}):
            self.assertNotIn(self.LONG, bm.handle_call("list_saved", args)[0], args)
        self.assertNotIn(self.LONG, bm.handle_call("find_tool", {"intent": "zebra"})[0])
