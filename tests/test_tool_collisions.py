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
        for other in ("claude_token_burn", "claude_errors", "claude_efficiency_insights"):
            with self.subTest(other=other):
                self.assertTrue(
                    _same_cluster(self.clusters, "claude_workflow_insights", other),
                    f"claude_workflow_insights and {other} should be in the same "
                    "cluster -- this is the confirmed 2026-09-03 collision.",
                )

    def test_search_pair_found_and_isolated(self):
        self.assertTrue(_same_cluster(self.clusters, "claude_search", "search"))
        members = next(m for m, _w, _e in self.clusters if "claude_search" in m)
        self.assertEqual(
            sorted(members), ["claude_search", "search"],
            "claude_search/search should be an isolated pair, not merged into "
            "a larger cluster -- a bigger cluster here would mean the method "
            "is over-firing on this pair specifically.",
        )

    def test_session_deep_dive_loop_check_is_a_documented_miss(self):
        """This collision is real (confirmed by eval data) but this method
        cannot find it cleanly -- see the module docstring for why. If this
        assertion starts failing (the pair IS found), that's not a bug to
        fix reactively -- update the docstring and this test together,
        since the "known limitation" claim would no longer be true."""
        self.assertFalse(
            _same_cluster(self.clusters, "claude_session_deep_dive", "claude_loop_check"),
            "If this now passes, tool_collisions.py's docstring claim about "
            "this being an undetected collision is stale -- update both.",
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
