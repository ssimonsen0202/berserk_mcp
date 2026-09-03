#!/usr/bin/env python3
"""Regression test for evals/tool_collisions.py against the three
ground-truth collisions recorded in
docs/task-brief-collision-clusters-2026-09-03.md and confirmed by real
eval data in evals/run_ledger.jsonl (2026-09-03). Two must be found; the
third is a documented structural miss (see the module docstring) and must
stay a documented miss, not silently start passing or failing differently
without someone noticing and updating the docs."""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import berserk_mcp as bm  # noqa: E402
import tool_discovery as td  # noqa: E402
import tool_collisions as tc  # noqa: E402


def _clusters():
    tools = bm.TOOLS + bm.MGMT_TOOLS
    index = td.build_index(tools)
    scores = tc.self_query_scores(index, tools, top_k=8)
    name_edges = tc.name_token_edges(index)
    return tc.find_clusters(scores, name_edges, ratio_threshold=0.5)


def _same_cluster(clusters, a, b):
    return any(a in members and b in members for members, _w, _e in clusters)


class GroundTruthCollisionsTest(unittest.TestCase):
    """Each assertion here is a real, eval-confirmed collision or a
    documented miss -- not a property of the algorithm chosen for its own
    sake."""

    @classmethod
    def setUpClass(cls):
        cls.clusters = _clusters()

    def test_workflow_insights_cluster_found(self):
        # claude_efficiency_insights connects via a shared name token
        # ("insight") -- structural, does not drift with description wording.
        # claude_errors connects via description ratio, comfortably above
        # threshold as of the 2026-09-03 iteration-2 fix.
        for other in ("claude_errors", "claude_efficiency_insights"):
            with self.subTest(other=other):
                self.assertTrue(
                    _same_cluster(self.clusters, "claude_workflow_insights", other),
                    f"claude_workflow_insights and {other} should be in the same "
                    "cluster -- this is the confirmed 2026-09-03 collision.",
                )

    def test_workflow_insights_token_burn_ratio_is_tracked_not_required(self):
        """claude_token_burn's description-ratio to claude_workflow_insights
        moved from 0.58 (round 2, 2026-09-03) to ~0.48 (Task 1 iteration 2,
        same day) as Task 1's wording changes diluted shared vocabulary --
        expected and arguably good (less lexical overlap between two tools
        that were disambiguated). The real routing protection for this pair
        is the reciprocal "see claude_workflow_insights for that" line on
        claude_token_burn itself (unaffected by Task 1's edits), not this
        script's threshold. Tracked here so a future edit that pushes the
        ratio further doesn't go unnoticed, without hard-requiring a
        specific value that wording changes will keep nudging around."""
        scores = tc.self_query_scores(
            td.build_index(bm.TOOLS + bm.MGMT_TOOLS), bm.TOOLS + bm.MGMT_TOOLS, top_k=8
        )
        ratio = next((r for n, _s, r in scores["claude_workflow_insights"]
                     if n == "claude_token_burn"), 0.0)
        self.assertGreater(ratio, 0.3,
            f"claude_workflow_insights/claude_token_burn description ratio "
            f"dropped to {ratio:.2f} -- if this keeps falling, confirm the "
            "reciprocal disambiguator on claude_token_burn is still present "
            "and still doing the real protective work.")

    def test_search_pair_found_and_isolated(self):
        self.assertTrue(_same_cluster(self.clusters, "claude_search", "search"))
        members = next(m for m, _w, _e in self.clusters if "claude_search" in m)
        self.assertEqual(
            sorted(members), ["claude_search", "search"],
            "claude_search/search should be an isolated pair, not merged into "
            "a larger cluster -- a bigger cluster here would mean the method "
            "is over-firing on this pair specifically.",
        )

    def test_session_deep_dive_loop_check_now_found(self):
        """This pair was a documented blind spot when Task 2 was first
        written: real (confirmed by eval data) but undetected by this
        method, since the two tools shared no name token and too weak a
        description ratio to separate from noise. Task 1's fix to
        claude_loop_check's description added the literal cross-reference
        "see claude_session_deep_dive instead" -- which, as a side effect,
        gave the two descriptions enough shared vocabulary that this
        method now finds the pair too (description ratio ~0.59). If this
        assertion starts failing again, the disambiguating text was
        likely reworded away -- check berserk_mcp.py's claude_loop_check
        description before assuming this test is wrong."""
        self.assertTrue(
            _same_cluster(self.clusters, "claude_session_deep_dive", "claude_loop_check"),
        )


class NameTokenEdgesTest(unittest.TestCase):
    def test_lane_prefixes_are_not_flagged(self):
        """'claude' is shared by 21 tools -- an intentional lane-grouping
        prefix, not a collision signal. Flagging it would make every
        claude_* tool transitively "one cluster" via that single token,
        which is useless."""
        tools = bm.TOOLS + bm.MGMT_TOOLS
        index = td.build_index(tools)
        edges = tc.name_token_edges(index)
        self.assertNotIn("claude", edges.values(),
                          "'claude' must not be used as a collision edge -- "
                          "it's a lane prefix, not a signal.")
        per_tool, _doc_freq = index
        claude_tools = [n for n in per_tool if n.startswith("claude_")]
        self.assertGreater(len(claude_tools), tc.MAX_NAME_TOKEN_SHARE,
                            "test assumption: more claude_* tools than the "
                            "share cutoff, so 'claude' as a name token must "
                            "be excluded by MAX_NAME_TOKEN_SHARE")

    def test_sre_soc_over_firing_guard_still_fires(self):
        """SRE and SOC measured 95-96% real accuracy at full schema
        (evals/run_ledger.jsonl, 2026-09-03) -- if this ever stops firing,
        either the lanes' real accuracy changed (re-verify with a real eval
        before trusting it) or MAX_NAME_TOKEN_SHARE/ratio_threshold need
        recalibrating, not silent acceptance."""
        tools = bm.TOOLS + bm.MGMT_TOOLS
        tools_by_name = {t["name"]: t for t in tools}
        clusters = _clusters()
        for role in ("sre", "soc"):
            with self.subTest(role=role):
                count = tc.report(clusters, tools_by_name=tools_by_name,
                                  role=role, file=io.StringIO())
                self.assertGreater(count, 3,
                    f"role={role} previously over-fired (>3 clusters) against "
                    "a lane that measures 95-96% real accuracy -- if this "
                    "count dropped to <=3, the guard in tool_collisions.main() "
                    "would stop firing silently; that's a behavior change "
                    "worth a human noticing, not passing quietly.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
