"""Cluster collision analysis over the tool catalog (issue: task brief
docs/task-brief-collision-clusters-2026-09-03.md, Task 2).

Finds groups of tools whose descriptions or names are lexically close enough
that a model routing on text similarity could plausibly confuse them --
proactively, using the same TF-IDF index `find_tool` already builds, rather
than waiting for a real eval miss to expose the pair (every fix before this
one was reactive: found only after a model actually got it wrong).

**This is a candidate generator, not a verdict.** Lexical proximity is a
signal a collision *might* be real, not proof that it is. A flagged cluster
still needs a real eval case to confirm it costs real accuracy before a
description gets touched -- fixing on proximity alone is exactly the
speculative description churn docs/tool-description-audit.md warns against.

**Two independent signals, not one.** An early version of this module used
only description-vs-description similarity and it missed two of three known
ground-truth collisions -- not from a bad threshold, but because those two
collisions are a different *mechanism*:

- `claude_search` vs `search`, and `claude_workflow_insights` vs
  `claude_efficiency_insights` share a significant word in their NAME
  ("search", "insight") -- and `tool_discovery.search()` weights a
  name-token match 3x a description match. Two tools sharing a rare word in
  their name is a strong, structural collision signal on its own, even when
  their full descriptions read nothing alike.
- `claude_workflow_insights` vs `claude_token_burn` (and `claude_errors`)
  collide on ordinary description-text overlap -- no shared name token, just
  similar wording.

`self_query_scores()` covers the second; `name_token_edges()` covers the
first. `find_clusters()` unions both into one graph.

**One known limitation, resolved as a side effect, not by design.**
`claude_session_deep_dive` and `claude_loop_check` were a real,
eval-confirmed collision (evals/run_ledger.jsonl, 2026-09-03T19:48) that
neither signal detected cleanly when this module was first written: they
shared no name token, and their description-ratio (~0.25-0.30) was too
weak to separate from noise without a threshold low enough to flood the
report with unrelated pairs. Lowering the threshold to catch it was tried
and rejected -- it cost far more false positives than the one true positive
was worth.

Task 1's fix to `claude_loop_check`'s description (adding the disambiguating
line "see claude_session_deep_dive instead") happened to give the two
descriptions enough shared vocabulary that this method now finds the pair
too (description ratio ~0.59) -- not because the detection method improved,
but because the underlying fix textually referenced the other tool by name.
Kept as a worked example: this method finds most lexical collisions, not
all of them, and a real eval remains the only ground truth that catches
everything -- including, sometimes, retroactively confirming a fix by
making a collision newly visible to this same tool.

Stdlib only, no API calls, no live model -- pure analysis of
`tool_discovery`'s existing index.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import berserk_mcp as bm  # noqa: E402
import tool_discovery as td  # noqa: E402

# Name tokens shared by more tools than this are treated as a lane/product
# prefix (e.g. "claude" appears in the name of all 21 claude_* tools --
# that's the intentional lane-grouping convention, not a collision) rather
# than a real collision signal. Calibrated against the catalog as it stood
# 2026-09-03: prefixes ("claude" 21, "error" 7, "list" 6, "host"/"sre"/
# "soc"/"canonloom" 5, "find"/"run" 4) all sit at or above 4; every
# confirmed real collision ("search", "insight", "session", ...) sits at 2.
# Re-check this constant if the catalog grows a new multi-tool family name.
MAX_NAME_TOKEN_SHARE = 3


def _self_score(per_tool, doc_freq, n_tools, query_tokens, name):
    """Score one specific tool against an already-tokenized query, using
    the same weighting tool_discovery.search() uses internally. Needed
    because search()'s own top_k cutoff can drop a tool below its own
    query if enough other tools score higher on shared vocabulary --
    exactly the collision signal this module is looking for, so the self
    score can't be assumed to come back inside a small top_k window."""
    name_tokens, desc_tokens = per_tool[name]
    score = 0.0
    for qt in query_tokens:
        weight = td._idf_weight(qt, doc_freq, n_tools)
        if qt in name_tokens:
            score += 3 * weight
        elif qt in desc_tokens:
            score += weight
    return score


def self_query_scores(index, tools, top_k=8):
    """Query the index with each tool's OWN description, and record which
    OTHER tools rank near it.

    Returns {tool_name: [(competitor_name, score, ratio_to_self), ...]},
    sorted by ratio_to_self descending, competitors only (self excluded).
    ratio_to_self = competitor_score / this tool's own self-score under its
    own query. A competitor at or near 1.0 is retrieved almost as strongly
    by the tool's own words as the tool itself -- that's the collision.
    """
    per_tool, doc_freq = index
    n_tools = len(per_tool)
    order = [t["name"] for t in tools]
    out = {}
    for t in tools:
        name = t["name"]
        query_tokens = td.tokenize(
            t.get("description", ""), expand_synonyms=True, drop_stopwords=True
        )
        self_score = _self_score(per_tool, doc_freq, n_tools, query_tokens, name)
        ranked = td.search(index, t.get("description", ""), top_k=top_k + 1, tool_order=order)
        competitors = []
        for cname, score in ranked:
            if cname == name:
                continue
            ratio = (score / self_score) if self_score else 0.0
            competitors.append((cname, score, ratio))
        competitors.sort(key=lambda c: -c[2])
        out[name] = competitors[:top_k]
    return out


def name_token_edges(index, max_share=MAX_NAME_TOKEN_SHARE):
    """Tools whose NAME shares a significant word with another tool's name,
    where that word is rare enough (shared by <= max_share tools) to be a
    real signal rather than a lane prefix. Returns
    {frozenset({a, b}): shared_token}, one entry per colliding pair (a pair
    sharing >1 token keeps the first found -- rare in practice and not
    worth the complexity of tracking multiple).

    This exists because tool_discovery.search() weights a name-token match
    3x a description match -- two tools sharing a name word is a stronger
    routing-collision signal than most description overlap, and
    self_query_scores() alone does not catch it when the rest of the two
    descriptions differ (see module docstring: claude_search/search,
    claude_workflow_insights/claude_efficiency_insights)."""
    per_tool, _doc_freq = index
    by_token = {}
    for name, (name_tokens, _desc_tokens) in per_tool.items():
        for tok in name_tokens:
            by_token.setdefault(tok, []).append(name)

    edges = {}
    for tok, names in by_token.items():
        if 2 <= len(names) <= max_share:
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    edges.setdefault(frozenset((names[i], names[j])), tok)
    return edges


class _DSU:
    """Minimal union-find -- collisions cluster transitively (A collides
    with B, B collides with C -> all three are one cluster), which a
    simple pairwise edge list can't express directly."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_clusters(scores, name_edges, ratio_threshold=0.5):
    """Connected components over the graph where an edge A<->B exists when
    EITHER: B's ratio_to_self under A's query (or vice versa) >=
    ratio_threshold, OR A and B share a rare name token (name_edges).
    Returns [(members, mean_edge_weight, edge_details)], worst (largest,
    then highest mean weight) first. edge_details lists each edge as
    (a, b, kind, strength) so a human can see *why* tools were grouped.
    """
    dsu = _DSU()
    edges = {}  # frozenset({a,b}) -> (kind, strength)
    for name, competitors in scores.items():
        dsu.find(name)
        for cname, _score, ratio in competitors:
            if ratio >= ratio_threshold:
                dsu.union(name, cname)
                key = frozenset((name, cname))
                if key not in edges or edges[key][1] < ratio:
                    edges[key] = ("description", ratio)
    for pair, tok in name_edges.items():
        a, b = tuple(pair)
        dsu.union(a, b)
        # A shared name token is treated as maximum-strength evidence --
        # it is a structural fact (the literal word is in both names), not
        # a fuzzy score, and should not be out-ranked by a marginal
        # description ratio.
        if pair not in edges or edges[pair][0] != "name":
            edges[pair] = ("name", 1.0, tok)

    groups = {}
    for name in scores:
        groups.setdefault(dsu.find(name), set()).add(name)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_edges = [(tuple(pair), val) for pair, val in edges.items() if pair <= members]
        weights = [val[1] for _pair, val in member_edges]
        mean_weight = sum(weights) / len(weights) if weights else 0.0
        clusters.append((sorted(members), mean_weight, member_edges))

    clusters.sort(key=lambda c: (-len(c[0]), -c[1]))
    return clusters


def _tool_visible_in_role(tool, role):
    roles = tool.get("roles")
    if not roles or role in (None, "all", ""):
        return True
    return role in roles


def report(clusters, tools_by_name=None, role=None, file=sys.stdout):
    """Print ranked clusters, with each edge's reason. When role is given,
    only tools visible under that role are shown -- a cluster that only
    collides through a cross-lane competitor disappears once that lane's
    role filter is applied, same as claude_search/search does at
    role=claude vs role=all."""
    shown = 0
    for members, weight, member_edges in clusters:
        if role is not None and tools_by_name is not None:
            visible = {m for m in members if _tool_visible_in_role(tools_by_name[m], role)}
            if len(visible) < 2:
                continue
            members = sorted(visible)
            member_edges = [(pair, val) for pair, val in member_edges
                            if set(pair) <= visible]
            if not member_edges:
                continue
        shown += 1
        print(f"[{shown}] {len(members)} tools, mean collision strength {weight:.2f}",
              file=file)
        for m in members:
            print(f"      {m}", file=file)
        for pair, val in member_edges:
            kind = val[0]
            if kind == "name":
                print(f"        edge: {pair[0]} <-> {pair[1]}  "
                      f"(shared name word '{val[2]}')", file=file)
            else:
                print(f"        edge: {pair[0]} <-> {pair[1]}  "
                      f"(description ratio {val[1]:.2f})", file=file)
    if shown == 0:
        print("(no clusters at this threshold/role)", file=file)
    return shown


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", default=None,
                    help="only report clusters with >=2 members visible in this role")
    ap.add_argument("--ratio-threshold", type=float, default=0.5,
                    help="description-similarity threshold for an edge (default 0.5)")
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()

    tools = bm.TOOLS + bm.MGMT_TOOLS
    tools_by_name = {t["name"]: t for t in tools}
    index = td.build_index(tools)
    scores = self_query_scores(index, tools, top_k=args.top_k)
    name_edges = name_token_edges(index)
    clusters = find_clusters(scores, name_edges, ratio_threshold=args.ratio_threshold)

    print(f"{len(tools)} tools indexed, {len(clusters)} raw clusters "
          f"(description ratio>={args.ratio_threshold} OR shared rare name word)\n")
    n = report(clusters, tools_by_name=tools_by_name, role=args.role)

    # Sanity guard: SRE and SOC lanes measured 95-96% accuracy even at the
    # full 74-tool schema (evals/run_ledger.jsonl, 2026-09-03) -- they are
    # known-good. If a role-scoped report for either lane surfaces a long
    # list of clusters, the metric is over-firing, not finding real risk.
    if args.role in ("sre", "soc") and n > 3:
        print(f"\nWARNING: {n} clusters flagged for role={args.role}, which "
              "measured 95-96% accuracy in real evals. This method is "
              "likely over-firing for this lane -- treat its output here "
              "with extra skepticism before acting on it.", file=sys.stderr)


if __name__ == "__main__":
    main()
