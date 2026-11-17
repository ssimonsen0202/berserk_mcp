"""save_query must not reveal or replace a saved query the caller cannot see.

The name check used to run over every entry, so a caller in one role learned
that a name existed in another role ("already exists"), and with
overwrite=true replaced that entry and moved it to its own role. Found by the
Codex review of the learning-loop WP1 PR.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402

KQL_HIDDEN = "default | where resource['service.name'] == 'payments' | take 5"
KQL_NEW = "default | where resource['service.name'] == 'checkout' | take 5"


class SaveQueryHiddenNameTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        saved = (bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS)

        def restore():
            bm.LEARNED_PATH, bm.run_bzrk, bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED, bm.CACHE_TTL_SECONDS = saved

        self.addCleanup(restore)
        self.store = Path(self._tmp.name) / "learned.json"
        bm.LEARNED_PATH = self.store
        bm.CACHE_TTL_SECONDS = 0
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: ("col\n1", False)
        bm.ACTIVE_TIER_RESOLVED = bm.TIER_DEEP
        self.hidden = {
            "name": "shared_name",
            "description": "Written for sre.",
            "kql": KQL_HIDDEN,
            "since": "1h ago",
            "roles": ["sre"],
        }
        bm.save_learned([dict(self.hidden)])
        bm.ACTIVE_ROLE = "soc"

    def stored(self):
        return json.loads(self.store.read_text(encoding="utf-8"))

    def save(self, **extra):
        args = {"name": "shared_name", "description": "Mine.", "kql": KQL_NEW, **extra}
        return bm.handle_call("save_query", args)

    def test_hidden_name_is_refused_without_revealing_it(self):
        text, is_err = self.save()
        self.assertTrue(is_err)
        self.assertNotIn("already exists", text)
        self.assertNotIn("overwrite", text)
        self.assertIn("not available", text)
        self.assertEqual(self.stored(), [self.hidden])

    def test_overwrite_true_cannot_replace_a_hidden_entry(self):
        text, is_err = self.save(overwrite=True)
        self.assertTrue(is_err)
        self.assertEqual(self.stored(), [self.hidden])

    def test_refusal_is_the_same_as_for_a_name_hidden_by_tier(self):
        bm.save_learned(
            [
                dict(self.hidden),
                {
                    "name": "pending_gen",
                    "description": "G.",
                    "kql": "default | take 3",
                    "since": "1h ago",
                    "origin": "generated",
                    "status": "pending",
                },
            ]
        )
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "all", bm.TIER_SMALL
        text_tier, err_tier = bm.handle_call(
            "save_query", {"name": "pending_gen", "description": "Mine.", "kql": KQL_NEW, "overwrite": True}
        )
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "soc", bm.TIER_DEEP
        text_role, err_role = self.save(overwrite=True)
        self.assertTrue(err_tier and err_role)
        self.assertEqual(text_tier.replace("pending_gen", "X"), text_role.replace("shared_name", "X"))
        self.assertEqual(self.stored()[1]["status"], "pending")

    def test_owner_role_can_still_overwrite_with_overwrite_true(self):
        bm.ACTIVE_ROLE = "sre"
        text, is_err = self.save(overwrite=True)
        self.assertFalse(is_err, text)
        self.assertEqual(self.stored()[0]["kql"], KQL_NEW)

    def test_persist_refuses_a_hidden_same_name_entry_under_the_lock(self):
        # Defence in depth: the store write itself refuses, so a caller that
        # skips the handler's check (or races it) still cannot replace it.
        entry = {"name": "shared_name", "description": "Mine.", "kql": KQL_NEW, "since": "1h ago"}
        with self.assertRaises(ValueError):
            bm.persist_learned_query(entry, action_source="manual")
        self.assertEqual(self.stored(), [self.hidden])

    def test_hidden_name_is_refused_before_any_query_runs(self):
        calls = []
        bm.run_bzrk = lambda args, timeout=bm.DEFAULT_TIMEOUT: (calls.append(args), ("col\n1", False))[1]
        text, is_err = self.save(overwrite=True)
        self.assertTrue(is_err)
        self.assertEqual(calls, [])

    def test_review_generated_hides_entries_of_another_role(self):
        bm.save_learned(
            [
                dict(self.hidden),
                {
                    "name": "sre_secret",
                    "description": "Generated for sre.",
                    "kql": "default | take 4",
                    "since": "1h ago",
                    "roles": ["sre"],
                    "origin": "generated",
                    "status": "pending",
                    "generated_by": {"provider": "p", "model": "m", "ts": "t"},
                },
            ]
        )
        text, is_err = bm.handle_call("review_generated", {})
        self.assertNotIn("sre_secret", text)
        text, is_err = bm.handle_call("review_generated", {"name": "sre_secret"})
        self.assertTrue(is_err)
        self.assertNotIn("Generated for sre", text)
        self.assertNotIn("default | take 4", text)

    def test_generated_write_does_not_replace_a_hidden_generated_entry(self):
        hidden_gen = {
            "name": "gen_name",
            "description": "Generated for sre.",
            "kql": "default | take 4",
            "since": "1h ago",
            "roles": ["sre"],
            "origin": "generated",
            "status": "approved",
        }
        bm.save_learned([dict(self.hidden), dict(hidden_gen)])
        entry = {"name": "gen_name", "description": "Mine.", "kql": KQL_NEW, "since": "1h ago", "roles": ["soc"]}
        log = bm.persist_learned_query(entry, action_source="generated")
        self.assertNotEqual(log["name"], "gen_name")
        by_name = {it["name"]: it for it in self.stored()}
        self.assertEqual(by_name["gen_name"], hidden_gen)


if __name__ == "__main__":
    unittest.main()
