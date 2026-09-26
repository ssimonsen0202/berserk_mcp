"""Text the model reads must not name a tool it cannot call.

A hidden tool answers "unknown tool" (deliberately non-leaking), so an
instruction, description or next step that points at one leaves a small model
with no way to recover. Review 2026-09-26 (docs/mcp-guidance-review-2026-09-26.md), P1.

Role and tier are resolved at import, so every lane and tier is checked in a
fresh interpreter.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402

ROLES = ("all", "ops", "sre", "soc", "claude", "windows-forensics")

# Collects every surface the model reads in one process and reports hidden
# tool names found in each.
_PROBE = r"""
import json, re, berserk_mcp as bm
# Computed from tool_visible directly, not from the helper under test.
hidden = {t["name"] for t in bm.TOOLS + bm.MGMT_TOOLS if not bm.tool_visible(t)}
# Same reference rule as berserk_mcp._tool_references, written out here so the
# guard does not depend on the code under test: snake_case names anywhere,
# one-word names (search, schema) only at the start of a code span.
def tokens(text):
    text = str(text)
    return {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text) if "_" in t} | set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)", text))
found = {}
def check(source, text):
    names = sorted(tokens(text) & hidden)
    if names:
        found[source] = names
import os
os.makedirs(os.path.dirname(bm.LEARNED_PATH), exist_ok=True)
with open(bm.LEARNED_PATH, "w") as fh:
    json.dump([{"name": "probe_saved", "kql": bm.TABLE + " | take 1", "since": "1h ago", "origin": "generated", "status": "approved",
                "description": "Counts rows. Refine it with `search` or see soc_log_spike."}], fh)
meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {"tasks": {}}}
init = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
check("initialize.instructions", init["result"]["instructions"])
discover = bm.dispatch({"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": meta}})
check("server/discover.instructions", discover.get("result", {}).get("instructions", ""))
visible = set()
for mode in ("legacy", "modern"):
    for tool in bm._tool_list_result(mode)["tools"]:
        visible.add(tool["name"])
        check(f"tools/list[{mode}].{tool['name']}", tool["description"])
if "find_tool" in visible:
    for intent in ("custom kql query", "new services first seen", "per-minute log volume", "discovery worker"):
        text, is_error = bm.handle_call("find_tool", {"intent": intent})
        assert not is_error and '"name"' in text, text[:200]  # the path must really run
        check(f"find_tool[{intent}]", text)
for name in bm._EMPTY_NEXT_STEP:
    if name in visible:
        check(f"empty_next_step.{name}", bm._envelope(name, "1h ago", "(no rows)"))
assert "saved__probe_saved" in visible, "the saved-query path must really run"
print(json.dumps({"tier": bm.ACTIVE_TIER_RESOLVED, "hidden": len(hidden), "found": found}))
"""


def _run_probe(role, tier):
    home = tempfile.mkdtemp()
    env = dict(os.environ)
    for key in [k for k in env if k.startswith(("BERSERK_MCP_", "BERSERK_LLM_"))]:
        del env[key]
    env.update(
        {
            "HOME": home,
            "USERPROFILE": home,
            "BERSERK_MCP_STORE_DIR": os.path.join(home, "store"),
            "BERSERK_MCP_ROLE": role,
            "BERSERK_MCP_TIER": tier,
            "BERSERK_MCP_ENABLE_2026_07_28": "1",
            "BZRK_BIN": os.path.join(home, "no-such-bzrk"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120
    )
    if result.returncode != 0:
        raise AssertionError(f"probe failed for role={role} tier={tier}: {result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class HiddenToolReferenceTest(unittest.TestCase):
    # Covers SECURITY.md#trust-boundaries
    def test_no_lane_or_tier_names_a_hidden_tool(self):
        for role in ROLES:
            for tier in ("", "small", "deep"):
                with self.subTest(role=role, tier=tier or "default"):
                    report = _run_probe(role, tier)
                    self.assertEqual(report["found"], {}, f"hidden tools named (tier={report['tier']})")

    def test_small_tier_actually_hides_tools(self):
        # Guards the guard: a probe that saw nothing hidden would pass vacuously.
        report = _run_probe("ops", "")
        self.assertEqual(report["tier"], "small")
        self.assertGreaterEqual(report["hidden"], len(bm._DEEP_TIER_TOOLS))


class InstructionTierTest(unittest.TestCase):
    def test_deep_tier_instructions_are_unchanged(self):
        # build_instructions(role) must return what it returned before tiers:
        # primer (markers stripped) + role prefix + _BASE_INSTRUCTIONS.
        for role in bm._ROLE_PREFIX:
            with self.subTest(role=role):
                raw = bm._load_primer(role)
                self.assertEqual(
                    bm.build_instructions(role),
                    raw.replace(bm._DEEP_ONLY_MARKER, "") + bm._ROLE_PREFIX[role] + bm._BASE_INSTRUCTIONS,
                )

    def test_small_tier_drops_marked_lines_and_keeps_a_fallback(self):
        for role in bm._ROLE_PREFIX:
            with self.subTest(role=role):
                text = bm.build_instructions(role, bm.TIER_SMALL)
                self.assertNotIn(bm._DEEP_ONLY_MARKER.strip(), text)
                self.assertNotIn("`search`", text)
                self.assertIn("do not cover it", text)
                self.assertIn(bm._UNTRUSTED_DATA_OPEN, text)
                self.assertIn("list_saved", text)

    def test_every_primer_marker_ends_a_line(self):
        # A marker mid-line would reach the model at both tiers.
        for primer in (REPO / "primers").glob("*.md"):
            for number, line in enumerate(primer.read_text(encoding="utf-8").split("\n"), 1):
                with self.subTest(primer=primer.name, line=number):
                    if bm._DEEP_ONLY_MARKER.strip() in line:
                        self.assertTrue(line.endswith(bm._DEEP_ONLY_MARKER))


class DescriptionFilterTest(unittest.TestCase):
    def test_one_word_tool_names_count_only_in_code_spans(self):
        text = "Full-text search across sessions. Default 6h."
        self.assertIs(bm._without_hidden_tool_sentences(text, {"search"}), text)
        self.assertEqual(bm._without_hidden_tool_sentences("Try `search` next. Keep.", {"search"}), "Keep.")

    def test_saved_query_descriptions_are_filtered_inside_the_fence(self):
        hidden = bm._hidden_tool_names
        try:
            bm._hidden_tool_names = lambda: {"search"}
            text = bm._saved_query_description(
                {"description": "Counts errors. Refine it with `search`.", "origin": "generated"}
            )
            self.assertEqual(text, "<generated-description>Counts errors.</generated-description>")
            self.assertEqual(bm._saved_query_description({"description": "Use `search`."}), "Saved query.")
        finally:
            bm._hidden_tool_names = hidden

    def test_marker_followed_by_trailing_spaces_still_counts(self):
        text = "Use `search`." + bm._DEEP_ONLY_MARKER + "   \nend"
        self.assertEqual(bm._primer_for_tier(text, bm.TIER_SMALL), "end")
        self.assertEqual(bm._primer_for_tier(text, bm.TIER_DEEP), "Use `search`.   \nend")

    def test_crlf_primer_markers_work_at_both_tiers(self):
        text = "keep\r\nUse `search`." + bm._DEEP_ONLY_MARKER + "\r\nend"
        self.assertEqual(bm._primer_for_tier(text, bm.TIER_SMALL), "keep\r\nend")
        self.assertEqual(bm._primer_for_tier(text, bm.TIER_DEEP), "keep\r\nUse `search`.\r\nend")

    def test_sentences_naming_hidden_tools_are_dropped(self):
        text = "Lists X. For raw volume, see soc_log_spike. Use it e.g. before a search."
        self.assertEqual(
            bm._without_hidden_tool_sentences(text, {"soc_log_spike"}), "Lists X. Use it e.g. before a search."
        )

    def test_abbreviations_do_not_split_a_sentence(self):
        text = "Time window e.g. '15m ago' via `search`. Other."
        self.assertEqual(bm._without_hidden_tool_sentences(text, {"search"}), "Other.")

    def test_text_without_hidden_names_is_returned_unchanged(self):
        text = "Keeps  its   spacing.  Exactly."
        self.assertIs(bm._without_hidden_tool_sentences(text, {"search"}), text)
        self.assertIs(bm._without_hidden_tool_sentences(text, set()), text)

    def test_every_description_keeps_its_first_sentence_when_all_deep_tools_are_hidden(self):
        hidden = set(bm._DEEP_TIER_TOOLS) | {t["name"] for t in bm.TOOLS if t.get("roles")}
        for tool in bm.TOOLS + bm.MGMT_TOOLS:
            if tool["name"] in hidden:
                continue
            with self.subTest(tool=tool["name"]):
                filtered = bm._without_hidden_tool_sentences(tool["description"], hidden)
                first = bm._SENTENCE_BREAK_RE.split(tool["description"])[0]
                self.assertTrue(filtered.strip())
                self.assertTrue(filtered.startswith(first), "the purpose sentence names a hidden tool; rewrite it")


if __name__ == "__main__":
    unittest.main()
