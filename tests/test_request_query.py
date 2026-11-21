"""WP7a: request_query, the request store, and the handler replies.

Every test names the new code (module, function or tool) inside its own body,
so each one fails on its own against code that lacks it. bzrk is stubbed;
nothing here touches a live Berserk or the real store directory. The store
probes come from spec appendix A (probe2.py, F1-F6 and F16-F18, plus the
concurrency probe); the prompt probes (F12-F14) belong to WP7b.
"""

import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import berserk_mcp as bm  # noqa: E402
from berserk_mcp import config as bm_config  # noqa: E402

QUESTION = "which hosts are running hot right now"
SAVED_QUESTION = "which checkout errors happened in the last hour"
MARK = "zqmarkerq7"
WIDGETS = "Quarterly widget inventory summary."
ENV_NAMES = ("BERSERK_MCP_REQUESTS_PER_HOUR", "BERSERK_MCP_REQUEST_NEGATIVE_TTL_HOURS")


def requests_module():
    """berserk_mcp.requests, imported inside each test so a missing module
    fails that test alone."""
    return importlib.import_module("berserk_mcp.requests")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now():
    return datetime.now(UTC)


def rid_of(label):
    """A valid 16-hex request id for a fixture label."""
    return hashlib.sha256(str(label).encode()).hexdigest()[:16]


def rec(label, status, updated, **extra):
    """A fully valid stored request record (the store rejects any other)."""
    item = {
        "id": label if len(str(label)) == 16 else rid_of(label),
        "question": "q",
        "role": "sre",
        "service": "",
        "since": "",
        "tried": [],
        "failed_kql": "",
        "error": "",
        "status": status,
        "ts": updated,
        "updated": updated,
        "attempts": 0,
        "result": {},
    }
    if status == "running":
        item.update(claim="c", claimed_at=updated)
    item.update(extra)
    return item


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        names = ("LEARNED_PATH", "ACTIVE_ROLE", "ACTIVE_TIER_RESOLVED", "run_bzrk")
        saved = {n: getattr(bm, n) for n in names}
        saved_discovery = bm.BERSERK_MCP_DISCOVERY
        saved_env = {n: os.environ.get(n) for n in ENV_NAMES}

        def restore():
            for n, v in saved.items():
                setattr(bm, n, v)
            bm.BERSERK_MCP_DISCOVERY = saved_discovery
            for n, v in saved_env.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v

        self.addCleanup(restore)
        for n in ENV_NAMES:
            os.environ.pop(n, None)
        self.dir = Path(self._tmp.name)
        self.learned_path = self.dir / "learned.json"
        self.requests_file = self.dir / "query_requests.json"
        bm.LEARNED_PATH = self.learned_path
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "sre", bm.TIER_SMALL
        bm.BERSERK_MCP_DISCOVERY = False
        self.bzrk_calls = []

        def fake(args, timeout=bm.DEFAULT_TIMEOUT):
            self.bzrk_calls.append(list(args))
            return "col\nrow", False

        bm.run_bzrk = fake

    # ---- helpers ----
    def ask(self, question=QUESTION, **extra):
        args = {"question": question}
        args.update(extra)
        return bm.handle_call("request_query", args)

    def stored(self):
        return json.loads(self.requests_file.read_text(encoding="utf-8"))

    def put_store(self, requests=(), admissions=()):
        self.requests_file.write_text(
            json.dumps({"version": 1, "requests": list(requests), "admissions": list(admissions)}), encoding="utf-8"
        )

    def put_learned(self, *entries):
        self.learned_path.write_text(json.dumps(list(entries)), encoding="utf-8")

    @staticmethod
    def saved(name, description, **extra):
        item = {"name": name, "description": description, "kql": bm.TABLE + " | take 5", "since": "1h ago"}
        item.update(extra)
        return item

    def clean(self, question=QUESTION, **extra):
        args = {"question": question}
        args.update(extra)
        clean, err = requests_module().validate_arguments(args)
        self.assertIsNone(err)
        return clean

    def rid(self, question=QUESTION, role=None):
        r = requests_module()
        return r.request_id(r.normalise_question(question), bm.ACTIVE_ROLE if role is None else role)

    def finish(self, question, status, result=None, role=None):
        """Queue `question`, claim it and complete it with `status`."""
        r = requests_module()
        state, _entry = r.submit(self.clean(question), bm.ACTIVE_ROLE if role is None else role)
        self.assertEqual(state, "queued")
        (job,) = r.claim(1)
        self.assertTrue(r.complete(job["id"], job["claim"], status, result or {"queries_saved": []}))
        return job["id"]


class QueueingTest(_Base):
    def test_new_question_is_queued_with_stable_id(self):
        requests_module()
        text, is_err = self.ask()
        expected = hashlib.sha256(b"which hosts are running hot right now\nsre").hexdigest()[:16]
        self.assertFalse(is_err)
        self.assertEqual(
            text,
            f"Queued ({expected}). An author pass will write and verify a query. "
            "Tell the user it is not available yet.",
        )
        (entry,) = self.stored()["requests"]
        self.assertEqual(entry["id"], expected)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["role"], "sre")
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["result"], {"queries_saved": [], "provider": "", "model": ""})
        # A queued request never reaches an author or Berserk in WP7a.
        self.assertEqual(self.bzrk_calls, [])

    def test_request_id_uses_empty_role_for_the_all_role(self):
        r = requests_module()
        bm.ACTIVE_ROLE = "all"
        text, _ = self.ask()
        expected = hashlib.sha256(b"which hosts are running hot right now\n").hexdigest()[:16]
        self.assertIn(f"({expected})", text)
        self.assertEqual(r.active_role(), "")
        self.assertEqual(self.stored()["requests"][0]["role"], "")

    def test_same_question_different_wording_case_and_spacing_dedupes(self):
        requests_module()
        first, _ = self.ask("Which hosts are running HOT right now?")
        second, is_err = self.ask("  which   hosts are   running hot   right now  ")
        self.assertFalse(is_err)
        self.assertTrue(first.startswith("Queued ("))
        self.assertTrue(second.startswith("Already queued ("))
        self.assertEqual(len(self.stored()["requests"]), 1)

    def test_different_role_is_a_different_request(self):
        requests_module()
        self.ask()
        bm.ACTIVE_ROLE = "soc"
        text, _ = self.ask()
        self.assertTrue(text.startswith("Queued ("))
        self.assertEqual(len(self.stored()["requests"]), 2)

    def test_pending_request_is_not_requeued(self):
        requests_module()
        self.ask()
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, f"Already queued ({self.rid()}). Do not ask again.")
        self.assertEqual(len(self.stored()["requests"]), 1)
        self.assertEqual(len(self.stored()["admissions"]), 1)

    def test_running_request_reads_as_already_queued(self):
        r = requests_module()
        self.ask()
        self.assertEqual(len(r.claim(1)), 1)
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, f"Already queued ({self.rid()}). Do not ask again.")

    def test_question_is_redacted_before_persist(self):
        requests_module()
        self.ask(
            "Show failures for 192.0.2.10 and jo.doe@example.com please",
            failed_kql="default | where ip == '192.0.2.77'",
            error="denied for ops@example.org",
        )
        blob = self.requests_file.read_text(encoding="utf-8")
        for secret in ("192.0.2.10", "jo.doe@example.com", "192.0.2.77", "ops@example.org"):
            self.assertNotIn(secret, blob)
        # F1: the normalised question only ever computes the id.
        self.assertNotIn("question_norm", blob)

    def test_redaction_failure_refuses_and_writes_nothing(self):
        r = requests_module()
        with mock.patch.object(r.secret_scan, "redact", side_effect=RuntimeError("boom")):
            text, is_err = self.ask()
        self.assertTrue(is_err)
        self.assertEqual(text, "Requests cannot be accepted right now; the operator has been told.")
        self.assertFalse(self.requests_file.exists())

    def test_submit_state_for_a_redaction_failure_is_unavailable(self):
        r = requests_module()
        with mock.patch.object(r.secret_scan, "redact", return_value=(None, [])):
            state, entry = r.submit(self.clean(), "sre")
        self.assertEqual((state, entry), ("unavailable", None))
        self.assertFalse(self.requests_file.exists())

    def test_optional_fields_are_stored_when_valid(self):
        requests_module()
        self.ask(service="checkout.api", since=" 2 hours ago ", tried=["saved__a", "find_tool"], error="boom")
        (entry,) = self.stored()["requests"]
        self.assertEqual(entry["service"], "checkout.api")
        self.assertEqual(entry["since"], bm._normalize_since("2 hours ago"))
        self.assertEqual(entry["tried"], ["saved__a", "find_tool"])
        self.assertEqual(entry["error"], "boom")


class ReplyTest(_Base):
    def test_done_request_points_to_saved_tool(self):
        requests_module()
        self.put_learned(self.saved("hot_hosts", WIDGETS))
        self.finish(QUESTION, "done", {"queries_saved": ["hot_hosts"]})
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, "Already answered: call saved__hot_hosts.")

    def test_done_but_pending_approval_says_so_in_small_tier(self):
        requests_module()
        self.put_learned(self.saved("hot_hosts", WIDGETS, origin="generated", status="pending"))
        self.finish(QUESTION, "done", {"queries_saved": ["hot_hosts"]})
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, "Answered, awaiting operator approval.")
        self.assertNotIn("hot_hosts", text)

    def test_done_reply_names_only_a_saved_tool_item_visible_accepts_now(self):
        requests_module()
        self.put_learned(
            self.saved("other_role", WIDGETS, roles=["soc"]),
            self.saved("hot_hosts", WIDGETS),
        )
        self.finish(QUESTION, "done", {"queries_saved": ["other_role", "gone", 5, "hot_hosts"]})
        self.assertEqual(self.ask()[0], "Already answered: call saved__hot_hosts.")
        # Once the only visible entry is hidden for this role, no name is offered.
        self.put_learned(self.saved("other_role", WIDGETS, roles=["soc"]))
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, "Answered, awaiting operator approval.")

    def test_done_reply_uses_the_stored_entry_name_not_the_request_text(self):
        requests_module()
        self.put_learned(self.saved("hot_hosts", WIDGETS))
        self.finish(QUESTION, "done", {"queries_saved": ["hot_hosts", "saved__x; drop"]})
        text, _ = self.ask()
        self.assertEqual(text, "Already answered: call saved__hot_hosts.")

    def test_needs_human_blocks_until_ttl_then_requeues(self):
        requests_module()
        rid = self.finish(QUESTION, "needs_human")
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(text, "Could not be answered automatically; an operator has been notified. Do not ask again.")
        # D4: the default negative-cache period is one hour.
        data = self.stored()
        data["requests"][0]["updated"] = iso(now() - timedelta(minutes=59))
        self.requests_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("Do not ask again", self.ask()[0])
        data["requests"][0]["updated"] = iso(now() - timedelta(minutes=61))
        self.requests_file.write_text(json.dumps(data), encoding="utf-8")
        text, is_err = self.ask()
        self.assertFalse(is_err)
        self.assertEqual(
            text,
            f"Queued ({rid}). An author pass will write and verify a query. Tell the user it is not available yet.",
        )
        (entry,) = self.stored()["requests"]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["attempts"], 1)  # kept across the requeue

    def test_unsolvable_is_blocked_like_needs_human(self):
        requests_module()
        self.finish(QUESTION, "unsolvable")
        self.assertIn("Do not ask again", self.ask()[0])

    def test_negative_ttl_env_is_read_and_bad_values_fall_back(self):
        r = requests_module()
        self.assertEqual(r.negative_ttl_hours(), 1)
        for raw, expected in (("3", 3), ("0", 1), ("-2", 1), ("abc", 1), (" 5 ", 5)):
            with mock.patch.dict(os.environ, {"BERSERK_MCP_REQUEST_NEGATIVE_TTL_HOURS": raw}):
                self.assertEqual(r.negative_ttl_hours(), expected, raw)

    def test_rate_limit_per_hour(self):
        requests_module()
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "3"
        for i in range(3):
            text, is_err = self.ask(f"distinct question number {i} here")
            self.assertFalse(is_err)
            self.assertTrue(text.startswith("Queued ("))
        text, is_err = self.ask("distinct question number 9 here")
        self.assertTrue(is_err)
        self.assertEqual(text, "Too many requests this hour; try later.")
        self.assertEqual(len(self.stored()["requests"]), 3)
        # A repeat of a queued question is not a new admission and is still answered.
        self.assertTrue(self.ask("distinct question number 1 here")[0].startswith("Already queued"))

    def test_requests_per_hour_default_is_twenty(self):
        r = requests_module()
        self.assertEqual(r.requests_per_hour(), 20)
        with mock.patch.dict(os.environ, {"BERSERK_MCP_REQUESTS_PER_HOUR": "x"}):
            self.assertEqual(r.requests_per_hour(), 20)

    def test_existing_saved_match_is_offered_before_queueing(self):
        requests_module()
        self.put_learned(self.saved("checkout_errors", "Errors from the checkout service in the last hour."))
        text, is_err = self.ask(SAVED_QUESTION)
        self.assertFalse(is_err)
        self.assertEqual(
            text,
            "A saved query may already answer this: saved__checkout_errors. "
            "Call it; request again with tried including it if it does not fit.",
        )
        self.assertFalse(self.requests_file.exists())

    def test_tried_saved_tool_bypasses_offer(self):
        requests_module()
        self.put_learned(self.saved("checkout_errors", "Errors from the checkout service in the last hour."))
        text, is_err = self.ask(SAVED_QUESTION, tried=["saved__checkout_errors"])
        self.assertFalse(is_err)
        self.assertTrue(text.startswith("Queued ("))
        self.assertEqual(len(self.stored()["requests"]), 1)

    def test_saved_match_is_not_offered_when_item_visible_hides_it(self):
        requests_module()
        self.put_learned(
            self.saved("checkout_errors", "Errors from the checkout service in the last hour.", roles=["soc"]),
            self.saved(
                "checkout_pending",
                "Errors from the checkout service in the last hour.",
                origin="generated",
                status="pending",
            ),
        )
        text, _ = self.ask(SAVED_QUESTION)
        self.assertTrue(text.startswith("Queued ("))
        self.assertNotIn("checkout_errors", text)
        self.assertNotIn("checkout_pending", text)

    def test_unrelated_question_is_not_offered_a_saved_tool(self):
        requests_module()
        self.put_learned(self.saved("checkout_errors", "Errors from the checkout service in the last hour."))
        self.assertTrue(self.ask("how much disk space is left on the backup volume")[0].startswith("Queued ("))

    def test_saved_match_threshold_clears_every_built_in_tool_router_case(self):
        # Appendix A.1: no router case whose answer is a built-in tool may match a
        # saved tool of the legacy fixture. Cases about saved queries themselves
        # (they name the fixture entries on purpose) and about request_query are
        # exempt; the document explains why.
        r = requests_module()
        shutil.copy(REPO / "tests" / "fixtures" / "learned_legacy_v1_37.json", self.learned_path)
        exempt = {"list_saved", "run_saved", "save_query", "request_query"}
        prompts = []
        for path in sorted((REPO / "evals").glob("router_cases*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                case = json.loads(line) if line.strip() else {}
                if isinstance(case.get("prompt"), str) and case.get("expect_tool") not in exempt:
                    prompts.append((case["id"], case["prompt"]))
        self.assertGreater(len(prompts), 100)
        for role in ("all", "sre", "soc", "claude", "ops"):
            for tier in (bm.TIER_SMALL, bm.TIER_DEEP):
                bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = role, tier
                for case_id, prompt in prompts:
                    ranked, _ = bm.rank_discovery(prompt)
                    top = next((s for n, s in ranked if n.startswith("saved__")), 0.0)
                    self.assertLess(top, r.SAVED_MATCH_MIN_SCORE, f"{case_id} ({role}/{tier})")
        # ...and a plain restatement of a fixture entry still clears it.
        bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = "sre", bm.TIER_SMALL
        ranked, _ = bm.rank_discovery(SAVED_QUESTION)
        self.assertGreaterEqual(ranked[0][1], r.SAVED_MATCH_MIN_SCORE)
        self.assertEqual(ranked[0][0], "saved__checkout_errors")

    def test_request_query_handler_exists_in_the_learning_handlers(self):
        import berserk_mcp.handlers.learning as learning

        requests_module()
        self.assertTrue(callable(learning._handle_request_query))

    def test_queue_full_reply_is_an_error(self):
        r = requests_module()
        self.put_store([rec(f"p{i}", "pending", iso(now())) for i in range(r.MAX_REQUESTS)])
        text, is_err = self.ask()
        self.assertTrue(is_err)
        self.assertEqual(text, "The request queue is full; try later.")

    def test_unreadable_store_reply_is_an_error(self):
        requests_module()
        self.requests_file.write_text("{not json", encoding="utf-8")
        text, is_err = self.ask()
        self.assertTrue(is_err)
        self.assertEqual(text, "Requests cannot be accepted right now; the operator has been told.")

    def test_validation_errors_are_errors(self):
        requests_module()
        for args in ({}, {"question": "short"}, {"question": QUESTION, "service": "a b"}, {"question": 5}):
            text, is_err = bm.handle_call("request_query", args)
            self.assertTrue(is_err, args)
            self.assertFalse(self.requests_file.exists())

    def test_question_never_echoed_in_any_reply(self):
        r = requests_module()
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "6"
        q = f"what is {MARK} doing on every host"
        extra = {"service": f"svc{MARK}", "failed_kql": f"x {MARK}", "error": f"e {MARK}", "tried": [f"t{MARK}"]}
        replies = []
        replies.append(self.ask(q, **extra))  # queued
        replies.append(self.ask(q, **extra))  # pending
        job = r.claim(1)[0]
        replies.append(self.ask(q, **extra))  # running
        r.complete(job["id"], job["claim"], "needs_human", {"queries_saved": []})
        replies.append(self.ask(q, **extra))  # failed_recent
        other = f"another {MARK} question about disks"
        job = self.finish(other, "done", {"queries_saved": [f"gone{MARK}"]})
        replies.append(self.ask(other))  # done, nothing visible
        self.put_learned(self.saved("hot_hosts", f"{MARK} hosts running hot and more words."))
        replies.append(self.ask(f"{MARK} hosts running hot"))  # saved offer
        replies.append(bm.handle_call("request_query", {"question": q, "service": f"bad service {MARK}"}))
        replies.append(bm.handle_call("request_query", {"question": q, "tried": f"{MARK}"}))
        replies.append(bm.handle_call("request_query", {"question": f"{MARK}\n" + q}))
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "1"
        replies.append(self.ask(f"{MARK} yet another new question"))  # rate limited
        self.requests_file.write_text("{bad", encoding="utf-8")
        replies.append(self.ask(f"{MARK} one more new question here"))  # unavailable
        self.assertGreaterEqual(len(replies), 11)
        for text, _is_err in replies:
            self.assertNotIn(MARK, text)
        # The offered saved-tool name is the stored entry's, never the question's.
        self.assertIn("saved__hot_hosts", replies[5][0])

    def test_request_content_never_reaches_tools_list(self):
        requests_module()
        self.ask(f"what is {MARK} doing on every host", service=f"svc{MARK}", failed_kql=f"x {MARK}", error=f"e {MARK}")
        for discovery in (False, True):
            for tier in (bm.TIER_SMALL, bm.TIER_DEEP):
                bm.BERSERK_MCP_DISCOVERY = discovery
                bm.ACTIVE_TIER_RESOLVED = tier
                resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                blob = json.dumps(resp["result"])
                self.assertNotIn(MARK, blob)
                self.assertNotIn(self.rid(f"what is {MARK} doing on every host"), blob)


class ToolSurfaceTest(_Base):
    def tool_names(self):
        resp = bm.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return [t["name"] for t in resp["result"]["tools"]]

    def test_request_query_visible_in_small_tier_and_discovery_mode(self):
        requests_module()
        for role in ("sre", "soc", "claude", "ops", "all"):
            for tier in (bm.TIER_SMALL, bm.TIER_DEEP):
                for discovery in (False, True):
                    bm.ACTIVE_ROLE, bm.ACTIVE_TIER_RESOLVED = role, tier
                    bm.BERSERK_MCP_DISCOVERY = discovery
                    with self.subTest(role=role, tier=tier, discovery=discovery):
                        self.assertIn("request_query", self.tool_names())
        self.assertIn("request_query", bm._ANCHOR_TOOL_NAMES)

    def test_request_query_has_title_and_write_local_annotation(self):
        requests_module()
        self.assertEqual(bm.TITLES["request_query"], "Request Query")
        self.assertEqual(bm.annotations_for("request_query"), bm._WRITE_LOCAL)
        self.assertEqual(bm.annotations_for("request_query")["openWorldHint"], False)

    def test_schema_declares_the_spec_arguments(self):
        requests_module()
        tool = next(t for t in bm.TOOLS + bm.MGMT_TOOLS if t["name"] == "request_query")
        props = tool["inputSchema"]["properties"]
        self.assertEqual(tool["inputSchema"]["required"], ["question"])
        for name in ("question", "service", "since", "tried", "failed_kql", "error"):
            self.assertIn(name, props)
        self.assertEqual(props["question"]["minLength"], 10)
        self.assertEqual(props["question"]["maxLength"], 500)
        self.assertEqual(props["tried"]["maxItems"], 5)
        self.assertEqual(props["failed_kql"]["maxLength"], 2000)
        self.assertEqual(props["error"]["maxLength"], 400)

    def test_descriptions_separate_request_query_from_request_discovery(self):
        requests_module()
        by_name = {t["name"]: t["description"] for t in bm.TOOLS + bm.MGMT_TOOLS}
        self.assertIn("request_discovery", by_name["request_query"])
        self.assertIn("request_query", by_name["request_discovery"])

    def test_unknown_argument_is_rejected_before_the_handler(self):
        requests_module()
        resp = bm.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "request_query", "arguments": {"question": QUESTION, "svc": "x"}},
            }
        )
        self.assertTrue(resp["result"]["isError"])
        self.assertFalse(self.requests_file.exists())

    def test_requests_layer_sits_after_runner_and_before_tools(self):
        requests_module()
        layers = list(bm._facade.LAYERS)
        self.assertEqual(layers.index("requests"), layers.index("runner") + 1)
        self.assertEqual(layers.index("requests") + 1, layers.index("tools"))

    def test_request_query_takes_only_the_requests_lock(self):
        r = requests_module()
        locked = []
        real = bm_config._FileLock

        def spy(path, *a, **k):
            locked.append(Path(path).name)
            return real(path, *a, **k)

        self.put_learned(self.saved("hot_hosts", "Hosts running hot."))
        with mock.patch.object(bm_config, "_FileLock", spy):
            self.ask()
            self.ask()
            self.ask(SAVED_QUESTION)
        self.assertIn(r.REQUESTS_FILE_NAME, locked)
        self.assertNotIn("learned.json", locked)

    def test_requests_file_name_and_private_directory(self):
        r = requests_module()
        self.assertEqual(r.REQUESTS_FILE_NAME, "query_requests.json")
        self.assertEqual(r.requests_path(), self.dir / "query_requests.json")


class StoreProbeTest(_Base):
    """The store probes of appendix A (probe2.py)."""

    def test_f2_cap_with_all_pending_refuses_and_leaves_store_unchanged(self):
        r = requests_module()
        self.put_store([rec(f"p{i}", "pending", iso(now())) for i in range(r.MAX_REQUESTS)])
        before = self.requests_file.read_text(encoding="utf-8")
        state, entry = r.submit(self.clean("distinct question number 2 here"), "sre")
        self.assertEqual((state, entry), ("queue_full", None))
        self.assertEqual(self.requests_file.read_text(encoding="utf-8"), before)

    def test_f3_eviction_cannot_refund_rate_limit_charges(self):
        r = requests_module()
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "20"
        old = iso(now() - timedelta(hours=5))
        self.put_store([rec(f"f{i}", "needs_human", old) for i in range(r.MAX_REQUESTS - 1)])
        states = [r.submit(self.clean(f"distinct question number {100 + i} here"), "sre")[0] for i in range(21)]
        self.assertEqual(states[:20].count("queued"), 20)
        self.assertEqual(states[20], "rate_limited")

    def test_f4_live_negative_cache_entry_is_not_evicted(self):
        r = requests_module()
        recent = iso(now() - timedelta(minutes=5))
        c5 = self.clean("distinct question number 5 here")
        rid5 = r.request_id(r.normalise_question(c5["question"]), "sre")
        self.put_store(
            [rec(rid5, "needs_human", recent)] + [rec(f"p{i}", "pending", recent) for i in range(r.MAX_REQUESTS - 1)]
        )
        state, _ = r.submit(self.clean("distinct question number 6 here"), "sre")
        self.assertEqual(state, "queue_full")
        self.assertTrue(any(it["id"] == rid5 for it in self.stored()["requests"]))
        self.assertEqual(r.submit(c5, "sre")[0], "failed_recent")

    def test_eviction_takes_only_expired_done_and_failed_entries_oldest_first(self):
        r = requests_module()
        old, recent = iso(now() - timedelta(hours=5)), iso(now() - timedelta(minutes=5))
        rows = [
            rec("done_old", "done", old),
            rec("done_new", "done", recent),
        ]
        rows += [rec(f"p{i}", "pending", recent) for i in range(r.MAX_REQUESTS - 2)]
        self.put_store(rows)
        self.assertEqual(r.submit(self.clean("distinct question number 7 here"), "sre")[0], "queued")
        ids = {it["id"] for it in self.stored()["requests"]}
        self.assertNotIn(rid_of("done_old"), ids)
        self.assertIn(rid_of("done_new"), ids)
        self.assertEqual(len(ids), r.MAX_REQUESTS)
        # Nothing else may go: the next submit finds no evictable slot.
        self.assertEqual(r.submit(self.clean("distinct question number 8 here"), "sre")[0], "queue_full")

    def test_f5_claims_are_exclusive_and_stale_completion_is_dropped(self):
        r = requests_module()
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "1000"
        for i in range(3):
            r.submit(self.clean(f"distinct question number {200 + i} here"), "sre")
        first, second = r.claim(3), r.claim(3)
        self.assertEqual((len(first), second), (3, []))
        self.assertFalse(r.complete(first[0]["id"], "bogus", "done", {}))
        self.assertTrue(r.complete(first[0]["id"], first[0]["claim"], "done", {"queries_saved": []}))
        data = self.stored()
        data["requests"][1]["claimed_at"] = iso(now() - timedelta(hours=2))
        self.requests_file.write_text(json.dumps(data), encoding="utf-8")
        again = r.claim(5)
        status = {it["id"]: it["status"] for it in self.stored()["requests"]}
        # R5: a stale claim is a failure, never silently re-run.
        self.assertEqual(status[first[1]["id"]], "needs_human")
        self.assertFalse(any(x["id"] == first[1]["id"] for x in again))
        stale = next(it for it in self.stored()["requests"] if it["id"] == first[1]["id"])
        self.assertEqual(stale["attempts"], 1)
        self.assertEqual(stale["result"]["reason"], "author run did not finish")
        self.assertNotIn("claim", stale)
        self.assertFalse(r.complete(first[1]["id"], first[1]["claim"], "done", {}))
        # The retry goes through submit and meets the negative cache.
        self.assertEqual(r.submit(self.clean(first[1]["question"]), "sre")[0], "failed_recent")

    def test_claim_respects_max_jobs_and_complete_rejects_unknown_status(self):
        r = requests_module()
        for i in range(3):
            r.submit(self.clean(f"distinct question number {220 + i} here"), "sre")
        self.assertEqual(len(r.claim(2)), 2)
        self.assertEqual(len(r.claim(2)), 1)
        with self.assertRaises(ValueError):
            r.complete("x", "y", "pending", {})

    def test_complete_counts_attempts_and_clears_the_claim(self):
        requests_module()
        self.finish(QUESTION, "needs_human")
        (entry,) = self.stored()["requests"]
        self.assertEqual(entry["attempts"], 1)
        self.assertNotIn("claim", entry)
        self.assertNotIn("claimed_at", entry)

    def test_f6_requests_are_visible_only_to_their_own_role(self):
        r = requests_module()
        r.submit(self.clean("distinct question number 300 here"), "sre")
        r.submit(self.clean("distinct question number 301 here"), "soc")
        bm_config.ACTIVE_ROLE = "soc"
        visible = r.visible_requests()
        self.assertEqual([it["role"] for it in visible], ["soc"])
        bm_config.ACTIVE_ROLE = "all"
        self.assertEqual(len(r.visible_requests()), 2)

    def test_visible_requests_never_raises_on_a_corrupt_store(self):
        r = requests_module()
        self.requests_file.write_text("[]", encoding="utf-8")
        self.assertEqual(r.visible_requests(), [])

    def test_f16_corrupt_store_fails_closed_and_is_left_untouched(self):
        r = requests_module()
        for bad in ("{not json", "[]", json.dumps({"version": 2, "requests": [], "admissions": []})):
            self.requests_file.write_text(bad, encoding="utf-8")
            state, entry = r.submit(self.clean("distinct question number 400 here"), "sre")
            self.assertEqual((state, entry), ("unavailable", None), bad)
            self.assertEqual(self.requests_file.read_text(encoding="utf-8"), bad)
            self.assertEqual(r.claim(1), [])

    def test_f17_malformed_timestamps_fail_closed(self):
        r = requests_module()
        c7 = self.clean("distinct question number 7 here")
        rid7 = r.request_id(r.normalise_question(c7["question"]), "sre")
        self.put_store([rec(rid7, "needs_human", "garbage")], ["garbage"] * 3)
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "3"
        self.assertEqual(r.submit(c7, "sre")[0], "failed_recent")
        self.assertEqual(r.submit(self.clean("distinct question number 8 here"), "sre")[0], "rate_limited")

    def test_f18_validation_rejects_bad_input(self):
        r = requests_module()
        base = {"question": QUESTION}
        cases = {
            "tried newline": {"tried": ["saved__x\n"]},
            "tried not list": {"tried": "x"},
            "tried too many": {"tried": ["a"] * 6},
            "service int": {"service": 5},
            "error CR": {"error": "a\rb"},
            "kql ESC": {"failed_kql": "a\x1bb"},
            "since int": {"since": 3},
            "since junk": {"since": "whenever"},
            "since newline": {"since": "\n1h ago\n"},
            "service IP": {"service": "192.0.2.10"},
            "tried card number": {"tried": ["4111111111111111"]},
            "question C1 CSI": {"question": "show \u009b31m errors now"},
            "question zero width": {"question": "which\u200bhosts are hot"},
            "question bidi": {"question": "which \u202ehosts are hot"},
            "question leading control": {"question": "\x1bwhich hosts are hot"},
            "question trailing newline": {"question": "which hosts are hot now\n"},
            "question private use": {"question": "which hosts \ue000 hot"},
            "question line separator": {"question": "which hosts\u2028are hot now"},
            "kql zero width": {"failed_kql": "a\u200bb"},
            "error bidi": {"error": "a\u202eb"},
            "kql too long": {"failed_kql": "x" * 2001},
            "error too long": {"error": "x" * 401},
            "punct only": {"question": "?!?!?!?!?!?!"},
            "question newline": {"question": "which hosts\nare hot now"},
            "question NEL": {"question": "which hosts\u0085are hot now"},
            "question too short": {"question": "too short"},
            "question too long": {"question": "a" * 501},
            "question not str": {"question": ["x"]},
        }
        for label, extra in cases.items():
            args = dict(base)
            args.update(extra)
            clean, err = r.validate_arguments(args)
            self.assertIsNone(clean, label)
            self.assertIsInstance(err, str, label)

    def test_f18_canonical_since_and_newline_in_failed_kql(self):
        r = requests_module()
        clean, err = r.validate_arguments({"question": QUESTION, "since": " 2 hours ago "})
        self.assertIsNone(err)
        self.assertEqual(clean["since"], bm._normalize_since("2 hours ago"))
        self.assertIsNone(r.validate_arguments({"question": QUESTION, "failed_kql": "a\n| take 1"})[1])
        self.assertIsNone(r.validate_arguments({"question": QUESTION, "failed_kql": "a\t| take 1"})[1])
        self.assertIsNone(r.validate_arguments({"question": QUESTION, "service": "checkout"})[1])

    def test_redaction_failure_refuses_service_and_tried(self):
        r = requests_module()
        with mock.patch.object(r.secret_scan, "redact", side_effect=RuntimeError("boom")):
            self.assertIsNotNone(r.validate_arguments({"question": QUESTION, "service": "checkout"})[1])
            self.assertIsNotNone(r.validate_arguments({"question": QUESTION, "tried": ["find_tool"]})[1])

    def test_r7_one_bad_record_makes_the_store_unreadable_and_untouched(self):
        r = requests_module()
        old = iso(now() - timedelta(hours=9))
        cases = {
            "unknown status": {"status": "weird"},
            "attempts str": {"attempts": "1"},
            "attempts bool": {"attempts": True},
            "attempts negative": {"attempts": -1},
            "bad id": {"id": "XYZ"},
            "missing question": {"question": None},
            "tried not strings": {"tried": [1]},
            "result not dict": {"result": []},
            "running without claim": {"status": "running"},
            "claim on a pending record": {"claim": "c", "claimed_at": old},
        }
        for label, mut in cases.items():
            item = rec("x1", "needs_human", old)
            item.update(mut)
            if label == "running without claim":
                item.pop("claim", None)
                item.pop("claimed_at", None)
            self.put_store([item])
            before = self.requests_file.read_text(encoding="utf-8")
            state, entry = r.submit(self.clean("distinct question number 900 here"), "sre")
            self.assertEqual((state, entry), ("unavailable", None), label)
            self.assertEqual(r.claim(5), [], label)
            self.assertFalse(r.complete("x1", "c", "done", {}), label)
            self.assertEqual(r.visible_requests(), [], label)
            self.assertEqual(self.requests_file.read_text(encoding="utf-8"), before, label)

    def test_r7_duplicate_ids_and_non_string_admissions_make_the_store_unreadable(self):
        r = requests_module()
        item = rec("dup", "pending", iso(now()))
        self.put_store([item, dict(item)])
        self.assertEqual(r.submit(self.clean("distinct question number 901 here"), "sre")[0], "unavailable")
        self.put_store([item], [5])
        self.assertEqual(r.submit(self.clean("distinct question number 902 here"), "sre")[0], "unavailable")
        self.assertEqual(r.claim(1), [])

    def test_normalise_question_is_nfkc_lowercase_and_drops_punctuation(self):
        r = requests_module()
        self.assertEqual(r.normalise_question("  Which   HOSTS\tare hot?! "), "which hosts are hot")
        self.assertEqual(r.normalise_question("Ｈot hosts？"), "hot hosts")

    def test_concurrent_requests_do_not_lose_updates(self):
        r = requests_module()
        os.environ["BERSERK_MCP_REQUESTS_PER_HOUR"] = "1000"
        clean = [self.clean(f"distinct question number {500 + i} here") for i in range(20)]
        threads = [threading.Thread(target=r.submit, args=(c, "sre")) for c in clean]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = self.stored()
        self.assertEqual((len(data["requests"]), len(data["admissions"])), (20, 20))


if __name__ == "__main__":
    unittest.main()
