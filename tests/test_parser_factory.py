"""Tests for parser_factory (LLM-driven parser generation, spec
docs/parser-factory-spec-2026-07-09.md). Pure stdlib (unittest); no live
Berserk or LLM backend needed.

Strategy: monkeypatch bm.run_bzrk the same way test_berserk_mcp.py does
(parser_factory calls berserk_mcp.bzrk_search, which calls run_bzrk as a
module global looked up at call time, so patching bm.run_bzrk propagates
through). LLM calls are faked by monkeypatching parser_factory's
_http_post_json / _http_get_json seams directly.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import berserk_mcp as bm  # noqa: E402
import parser_factory as pf  # noqa: E402


class ParserFactoryTestBase(unittest.TestCase):
    """Shared fixture: fake bzrk backend + isolated stores + fake LLM HTTP."""

    def setUp(self):
        self.calls = []
        self.responses = {}  # KQL substring -> (text, is_error)
        self.default_response = ("OK\n1", False)

        def fake_run_bzrk(args, timeout=bm.DEFAULT_TIMEOUT):
            self.calls.append(list(args))
            kql = args[3] if len(args) > 3 else ""
            for substr, resp in self.responses.items():
                if substr in kql:
                    return resp
            return self.default_response

        self._orig_run = bm.run_bzrk
        bm.run_bzrk = fake_run_bzrk

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_learned = bm.LEARNED_PATH
        self._orig_queue = bm.DISCOVERY_QUEUE_PATH
        bm.LEARNED_PATH = Path(self._tmp.name) / "learned.json"
        bm.DISCOVERY_QUEUE_PATH = Path(self._tmp.name) / "queue.json"

        self.llm_responses = []  # list of (json_or_None, err_or_None), consumed in order
        self._llm_calls = []  # (url, headers, payload)
        self._llm_get_calls = []

        def fake_post(url, headers, payload, timeout=pf.LLM_TIMEOUT):
            self._llm_calls.append((url, headers, payload))
            if self.llm_responses:
                return self.llm_responses.pop(0)
            return None, "no fake response configured"

        def fake_get(url, headers, timeout=pf.LLM_TIMEOUT):
            self._llm_get_calls.append((url, headers))
            return {"data": [{"id": "discovered-model"}]}, None

        self._orig_post = pf._http_post_json
        self._orig_get = pf._http_get_json
        pf._http_post_json = fake_post
        pf._http_get_json = fake_get

        self._env_keys = [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY",
            "BERSERK_LLM_LADDER", "BERSERK_LLM_HERMES_MODEL",
            "BERSERK_LLM_OPENAI_MODEL", "BERSERK_LLM_ANTHROPIC_MODEL",
            "BERSERK_LLM_HERMES_URL",
        ]
        self._orig_env = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)
        pf._reset_hermes_model_cache()

    def tearDown(self):
        bm.run_bzrk = self._orig_run
        bm.LEARNED_PATH = self._orig_learned
        bm.DISCOVERY_QUEUE_PATH = self._orig_queue
        pf._http_post_json = self._orig_post
        pf._http_get_json = self._orig_get
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        pf._reset_hermes_model_cache()

    # convenience for the profiling responses every generation test needs
    def _stub_profile_responses(self):
        self.responses["bag_keys(resource)"] = ("key n\nservice.name 5\n", False)
        self.responses["take 6"] = ("resource attributes metric_name severity_text body\n", False)
        self.responses[f"{bm.TABLE} | getschema"] = ("col1 string\n", False)


# ---------- P1: LLM client with escalation ladder ----------
class LlmClientTest(ParserFactoryTestBase):
    def test_anthropic_no_key_returns_error_without_http_call(self):
        text, err = pf.llm_complete("anthropic", "s", "u")
        self.assertIsNone(text)
        self.assertEqual(err, "anthropic: no ANTHROPIC_API_KEY")
        self.assertEqual(self._llm_calls, [])

    def test_openai_returns_message_content(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.llm_responses = [({"choices": [{"message": {"content": "hello"}}]}, None)]
        text, err = pf.llm_complete("openai", "s", "u")
        self.assertIsNone(err)
        self.assertEqual(text, "hello")

    def test_hermes_returns_message_content(self):
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self.llm_responses = [({"choices": [{"message": {"content": "hi hermes"}}]}, None)]
        text, err = pf.llm_complete("hermes", "s", "u")
        self.assertIsNone(err)
        self.assertEqual(text, "hi hermes")

    def test_hermes_discovers_model_when_unset(self):
        # no BERSERK_LLM_HERMES_MODEL -> should GET /api/models once
        self.llm_responses = [({"choices": [{"message": {"content": "ok"}}]}, None)]
        text, err = pf.llm_complete("hermes", "s", "u")
        self.assertIsNone(err)
        self.assertEqual(len(self._llm_get_calls), 1)
        self.assertEqual(self._llm_calls[0][2]["model"], "discovered-model")

    def test_http_error_propagates_without_key_material(self):
        os.environ["OPENAI_API_KEY"] = "sk-secret-value"
        self.llm_responses = [(None, "HTTP 500: boom")]
        text, err = pf.llm_complete("openai", "s", "u")
        self.assertIsNone(text)
        self.assertIn("HTTP 500", err)
        self.assertNotIn("sk-secret-value", err)

    def test_ladder_default(self):
        self.assertEqual(pf.ladder(), ["hermes", "openai", "anthropic"])

    def test_ladder_custom(self):
        os.environ["BERSERK_LLM_LADDER"] = "anthropic"
        self.assertEqual(pf.ladder(), ["anthropic"])

    def test_hermes_url_default_is_localhost_not_a_private_ip(self):
        # No env, no local config file -> privacy-safe default; the repo must
        # never hardcode a private endpoint.
        self.assertEqual(pf._hermes_url(), "http://localhost:3000/api/chat/completions")

    def test_hermes_url_precedence_env_over_config_over_default(self):
        path = pf.save_hermes_url("https://config-host:3000/api/chat/completions")
        # config used when no env var is set
        self.assertEqual(pf._hermes_url(), "https://config-host:3000/api/chat/completions")
        # env var wins over the config file
        os.environ["BERSERK_LLM_HERMES_URL"] = "https://env-host:3000/api/chat/completions"
        self.assertEqual(pf._hermes_url(), "https://env-host:3000/api/chat/completions")

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_saved_hermes_config_is_private(self):
        path = pf.save_hermes_url("https://config-host:3000/api/chat/completions")
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), oct(0o700))

    # ---- Snyk SSRF-scheme defense-in-depth (CWE-918) ----
    def test_save_hermes_url_rejects_file_scheme(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("file:///etc/passwd")

    def test_save_hermes_url_rejects_gopher_scheme(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("gopher://internal-service:70/")

    def test_save_hermes_url_rejects_ftp_scheme(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("ftp://mirror.example.com/pkg")

    def test_save_hermes_url_rejects_control_chars(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("http://example.com/\r\nHost: evil.internal")

    def test_save_hermes_url_rejects_empty(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("")
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("   ")

    def test_save_hermes_url_rejects_missing_host(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("http:///path-only")

    def test_save_hermes_url_accepts_http_and_https(self):
        pf.save_hermes_url("http://localhost:3000/v1")
        pf.save_hermes_url("http://127.0.0.1:3000/v1")
        pf.save_hermes_url("https://host-b/v1")

    # ---- F-013: plaintext http to non-loopback hosts is fail-closed by default ----
    def test_save_hermes_url_rejects_plaintext_remote_by_default(self):
        with self.assertRaises(pf.LlmUrlError) as ctx:
            pf.save_hermes_url("http://100.64.1.2:3000/api/chat/completions")
        self.assertIn("plaintext", str(ctx.exception))

    def test_save_hermes_url_rejects_plaintext_hostname_by_default(self):
        with self.assertRaises(pf.LlmUrlError):
            pf.save_hermes_url("http://hermes-box.tailnet.ts.net:3000/api/chat/completions")

    def test_save_hermes_url_allows_plaintext_remote_with_explicit_opt_in(self):
        orig = os.environ.get("BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE")
        try:
            os.environ["BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE"] = "1"
            pf.save_hermes_url("http://100.64.1.2:3000/api/chat/completions")  # must not raise
        finally:
            if orig is None:
                os.environ.pop("BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE", None)
            else:
                os.environ["BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE"] = orig

    def test_https_to_remote_host_never_needs_the_opt_in(self):
        orig = os.environ.pop("BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE", None)
        try:
            pf.save_hermes_url("https://100.64.1.2:3000/api/chat/completions")  # must not raise
        finally:
            if orig is not None:
                os.environ["BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE"] = orig

    def test_is_loopback_host_recognizes_localhost_ipv4_ipv6(self):
        self.assertTrue(pf._is_loopback_host("localhost"))
        self.assertTrue(pf._is_loopback_host("127.0.0.1"))
        self.assertTrue(pf._is_loopback_host("127.5.5.5"))
        self.assertTrue(pf._is_loopback_host("::1"))
        self.assertFalse(pf._is_loopback_host("100.64.1.2"))
        self.assertFalse(pf._is_loopback_host("example.com"))
        self.assertFalse(pf._is_loopback_host(""))
        self.assertFalse(pf._is_loopback_host(None))

    # ---- F-002: no automatic redirect-following (credential/downgrade leak) ----
    def test_http_post_json_does_not_follow_redirect(self):
        """A 302 from the configured endpoint must surface as an HTTP-302
        error, never be silently followed to a different origin with our
        Authorization header intact."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received_by_target = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received_by_target.append(dict(self.headers.items()))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"leaked"}}]}')

            def log_message(self, *a):
                pass

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/")
                self.end_headers()

            def log_message(self, *a):
                pass

        target_server = HTTPServer(("127.0.0.1", 0), TargetHandler)
        target_port = target_server.server_address[1]
        redirect_server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_port = redirect_server.server_address[1]
        for s in (target_server, redirect_server):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            out, err = self._orig_post(
                f"http://127.0.0.1:{redirect_port}/",
                {"Authorization": "Bearer secret-token"},
                {"x": 1},
            )
            self.assertIsNone(out)
            self.assertIn("HTTP 302", err)
            self.assertEqual(received_by_target, [])  # redirect was never followed
        finally:
            target_server.shutdown()
            target_server.server_close()
            redirect_server.shutdown()
            redirect_server.server_close()

    # ---- SNYK-003: dict-store helpers refuse tainted paths at the sink ----
    def test_load_json_dict_refuses_relative_path(self):
        self.assertEqual(pf.load_json_dict("relative/x.json"), {})

    def test_load_json_dict_refuses_traversal_path(self):
        import tempfile
        bad = str(Path(tempfile.gettempdir()) / ".." / "etc" / "shadow")
        self.assertEqual(pf.load_json_dict(bad), {})

    def test_save_json_dict_refuses_tainted_path(self):
        import tempfile
        traversal = str(Path(tempfile.gettempdir()) / ".." / "etc" / "x.json")
        with self.assertRaises(pf.StorePathError):
            pf.save_json_dict("relative/x.json", {})
        with self.assertRaises(pf.StorePathError):
            pf.save_json_dict(traversal, {})

    def test_http_helpers_refuse_non_http_scheme_at_call_time(self):
        """Even if a bad URL somehow reached _http_post_json/_http_get_json
        (e.g. a stale config file predating this validation), the request
        must be refused before urlopen is called. Calls the real (unstubbed)
        helpers via _orig_post / _orig_get so we exercise the validator,
        not the test double."""
        result, err = self._orig_post("file:///etc/passwd", {}, {"x": 1})
        self.assertIsNone(result)
        self.assertIn("invalid endpoint", err)
        result, err = self._orig_get("gopher://x/", {})
        self.assertIsNone(result)
        self.assertIn("invalid endpoint", err)

    # ---- F-005: bounded HTTP reads ----
    def test_http_post_json_rejects_oversized_response(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        oversized = b'{"x":"' + b"a" * (pf.MAX_PROVIDER_RESPONSE_BYTES + 100) + b'"}'

        class BigHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(oversized)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), BigHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            out, err = self._orig_post(f"http://127.0.0.1:{port}/", {}, {"x": 1})
            self.assertIsNone(out)
            self.assertIn("exceeds", err)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_post_json_accepts_response_at_the_cap(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class OkHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), OkHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            out, err = self._orig_post(f"http://127.0.0.1:{port}/", {}, {"x": 1})
            self.assertIsNone(err)
            self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        finally:
            server.shutdown()
            server.server_close()

    # ---- F-005: hermes model discovery is cached, not re-fetched per attempt ----
    def test_hermes_model_discovery_is_cached_across_calls(self):
        self.llm_responses = [
            ({"choices": [{"message": {"content": "1"}}]}, None),
            ({"choices": [{"message": {"content": "2"}}]}, None),
            ({"choices": [{"message": {"content": "3"}}]}, None),
        ]
        for _ in range(3):
            text, err = pf.llm_complete("hermes", "s", "u")
            self.assertIsNone(err)
        self.assertEqual(len(self._llm_get_calls), 1)

    def test_reset_hermes_model_cache_forces_rediscovery(self):
        self.llm_responses = [
            ({"choices": [{"message": {"content": "1"}}]}, None),
            ({"choices": [{"message": {"content": "2"}}]}, None),
        ]
        pf.llm_complete("hermes", "s", "u")
        pf._reset_hermes_model_cache()
        pf.llm_complete("hermes", "s", "u")
        self.assertEqual(len(self._llm_get_calls), 2)


# ---------- P2: source profiling and schema knowledge store ----------
class SourceProfileTest(ParserFactoryTestBase):
    def test_profile_uses_structural_sample_keys_and_schema_cache(self):
        self.responses["take 6"] = (
            'resource_keys attribute_keys\n["service.name", "host.name"] []\n', False)
        self.responses[f"{bm.TABLE} | getschema"] = ("col1 string\n", False)

        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertEqual(profile["resource_keys"], ["service.name", "host.name"])
        first_call_count = len(self.calls)

        profile2, err2 = pf.build_source_profile("mysvc2", "service", "24h ago")
        self.assertIsNone(err2)
        self.assertEqual(profile2["resource_keys"], ["service.name", "host.name"])
        # Each profile needs one sample query; getschema is reused and the
        # redundant per-source keys query is never issued.
        self.assertEqual(len(self.calls) - first_call_count, 1)
        self.assertNotIn("project k=bag_keys(resource)", "\n".join(c[3] for c in self.calls))

    def test_build_source_profile_truncates_and_persists_private_files(self):
        self.responses["bag_keys(resource)"] = ("key n\nservice.name 5\nhost.name 3\n", False)
        self.responses["take 6"] = ("x" * 10000, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 5000, False)

        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertEqual(profile["resource_keys"], ["service.name", "host.name"])
        self.assertLessEqual(len(profile["sample_excerpt"]), pf.SAMPLE_EXCERPT_CAP)
        self.assertLessEqual(len(profile["getschema_excerpt"]), pf.GETSCHEMA_EXCERPT_CAP)

        schema_path = Path(bm.LEARNED_PATH).parent / pf.SCHEMA_KNOWLEDGE_PATH_NAME
        self.assertTrue(schema_path.exists())
        knowledge = json.loads(schema_path.read_text())
        self.assertIn("service:mysvc", knowledge["sources"])

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_schema_knowledge_store_has_private_permissions(self):
        self._stub_profile_responses()
        pf.build_source_profile("x", "service", "1h ago")
        schema_path = Path(bm.LEARNED_PATH).parent / pf.SCHEMA_KNOWLEDGE_PATH_NAME
        self.assertEqual(oct(schema_path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(schema_path.parent.stat().st_mode & 0o777), oct(0o700))

    def test_all_subqueries_error_returns_none_and_writes_nothing(self):
        self.default_response = ("boom", True)
        profile, err = pf.build_source_profile("x", "service", "1h ago")
        self.assertIsNone(profile)
        self.assertTrue(err)
        schema_path = Path(bm.LEARNED_PATH).parent / pf.SCHEMA_KNOWLEDGE_PATH_NAME
        self.assertFalse(schema_path.exists())

    # ---- F-004: raw bzrk diagnostic text is redacted before it crosses
    # into the profiling error message ----
    def test_profiling_error_redacts_raw_bzrk_output(self):
        self.default_response = ("password=dummy-backend-secret-987654", True)
        profile, err = pf.build_source_profile("x", "service", "1h ago")
        self.assertIsNone(profile)
        self.assertNotIn("password=dummy-backend-secret-987654", err)

    def test_validate_generated_query_redacts_execution_failure(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}
        self.default_response = ("password=dummy-backend-secret-987654", True)
        ok, verr, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertNotIn("password=dummy-backend-secret-987654", verr)

    def test_validate_generated_query_redacts_retry_execution_failure(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}
        self.responses[f"{bm.TABLE} | take 1"] = ("(no rows)", False)
        self.default_response = ("password=dummy-backend-secret-987654", True)
        ok, verr, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertNotIn("password=dummy-backend-secret-987654", verr)

    def test_bzrk_error_never_reaches_persisted_report_or_next_prompt(self):
        """End-to-end: a backend failure containing secret-shaped text must
        not appear in either the final worker report (last_errors) or any
        prompt sent to a subsequent provider attempt."""
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        # Generation query executes but always fails with secret-shaped text.
        self.responses[f"{bm.TABLE} | take 1"] = ("password=dummy-backend-secret-987654", True)
        captured_prompts = []

        def spy_llm_complete(provider, system_prompt, user_prompt):
            captured_prompts.append(user_prompt)
            return json.dumps({"queries": [
                {"name": "q", "description": "d",
                 "kql": f"{bm.TABLE} | take 1", "since": "1h ago"},
            ]}), None
        orig = pf.llm_complete
        pf.llm_complete = spy_llm_complete
        try:
            report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        finally:
            pf.llm_complete = orig
        self.assertFalse(ok)
        blob = json.dumps(report)
        self.assertNotIn("password=dummy-backend-secret-987654", blob)
        for prompt in captured_prompts:
            self.assertNotIn("password=dummy-backend-secret-987654", prompt)

    # ---- F-003: resource-key tokens are strictly validated and bounded ----
    def test_instruction_shaped_resource_key_is_dropped(self):
        self.responses["bag_keys(resource)"] = (
            "key n\nservice.name 5\npassword=dummy-resource-key-secret 1\n", False)
        self.responses["take 6"] = ("x" * 100, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 100, False)
        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertEqual(profile["resource_keys"], ["service.name"])
        self.assertNotIn("password", " ".join(profile["resource_keys"]))

    def test_control_character_resource_key_is_dropped(self):
        # \x07 (BEL) is not whitespace, so it stays embedded in one token
        # rather than being split off -- this exercises the character-class
        # rejection, not accidental whitespace tokenization.
        self.responses["bag_keys(resource)"] = (
            "key n\nservice.name 5\nweird\x07key 1\n", False)
        self.responses["take 6"] = ("x" * 100, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 100, False)
        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertEqual(profile["resource_keys"], ["service.name"])

    def test_oversized_resource_key_is_dropped(self):
        huge = "a" * 200
        self.responses["bag_keys(resource)"] = (
            f"key n\nservice.name 5\n{huge} 1\n", False)
        self.responses["take 6"] = ("x" * 100, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 100, False)
        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertEqual(profile["resource_keys"], ["service.name"])

    def test_resource_key_count_is_capped(self):
        lines = "\n".join(f"k{i}.attr {i}" for i in range(pf.MAX_RESOURCE_KEYS + 20))
        self.responses["bag_keys(resource)"] = (f"key n\n{lines}\n", False)
        self.responses["take 6"] = ("x" * 100, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 100, False)
        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        self.assertLessEqual(len(profile["resource_keys"]), pf.MAX_RESOURCE_KEYS)

    def test_resource_keys_never_reach_the_generation_prompt_when_malicious(self):
        """End-to-end: an instruction-shaped resource key must not appear in
        either the persisted schema knowledge or the LLM prompt built from
        it (F-003 was a true positive on both sinks)."""
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self.responses["bag_keys(resource)"] = (
            "key n\nservice.name 5\npassword=dummy-resource-key-secret 1\n", False)
        self.responses["take 6"] = ("x" * 100, False)
        self.responses[f"{bm.TABLE} | getschema"] = ("y" * 100, False)
        captured_prompts = []
        orig_llm_complete = pf.llm_complete

        def spy_llm_complete(provider, system_prompt, user_prompt):
            captured_prompts.append(user_prompt)
            return None, "stub: no completion needed for this test"
        pf.llm_complete = spy_llm_complete
        try:
            pf.generate_parser_for({"source": "mysvc", "kind": "service", "role_hint": ""})
        finally:
            pf.llm_complete = orig_llm_complete
        self.assertTrue(captured_prompts)
        for prompt in captured_prompts:
            self.assertNotIn("password=dummy-resource-key-secret", prompt)
        schema_path = Path(bm.LEARNED_PATH).parent / pf.SCHEMA_KNOWLEDGE_PATH_NAME
        knowledge_text = schema_path.read_text()
        self.assertNotIn("password=dummy-resource-key-secret", knowledge_text)


# ---------- P3: new-source detection ----------
class DetectNewSourcesTest(ParserFactoryTestBase):
    def _detect(self, **kw):
        return pf.detect_new_sources(
            since=kw.pop("since", "24h ago"),
            auto_queue=kw.pop("auto_queue", False),
            check_drift=kw.pop("check_drift", False),
            load_json_list=bm.load_json_list,
            save_json_list=bm.save_json_list,
            discovery_queue_path=bm.DISCOVERY_QUEUE_PATH,
            active_role=kw.pop("active_role", "all"),
        )

    # ---- lower-severity: BERSERK_MAX_AUTOQUEUE clamping ----
    def _with_max_autoqueue_env(self, value):
        orig = os.environ.get("BERSERK_MAX_AUTOQUEUE")
        if value is None:
            os.environ.pop("BERSERK_MAX_AUTOQUEUE", None)
        else:
            os.environ["BERSERK_MAX_AUTOQUEUE"] = value
        try:
            return pf._parse_max_autoqueue()
        finally:
            if orig is None:
                os.environ.pop("BERSERK_MAX_AUTOQUEUE", None)
            else:
                os.environ["BERSERK_MAX_AUTOQUEUE"] = orig

    def test_negative_autoqueue_clamps_to_zero_not_inverted_slice(self):
        """A negative cap must never reach list[:N] -- Python reinterprets
        list[:-1] as 'all but the last one', which would queue nearly
        everything instead of nothing, inverting the flood-control
        invariant this constant exists to enforce."""
        self.assertEqual(self._with_max_autoqueue_env("-1"), 0)
        self.assertEqual(self._with_max_autoqueue_env("-500"), 0)

    def test_huge_autoqueue_clamps_to_ceiling(self):
        self.assertEqual(self._with_max_autoqueue_env("999999999"), pf._MAX_AUTOQUEUE_CEILING)

    def test_unparseable_autoqueue_falls_back_to_documented_default(self):
        self.assertEqual(self._with_max_autoqueue_env("not-a-number"), 5)

    def test_valid_autoqueue_value_passes_through_unchanged(self):
        self.assertEqual(self._with_max_autoqueue_env("10"), 10)
        self.assertEqual(self._with_max_autoqueue_env(None), 5)  # documented default

    def test_first_run_initializes_baseline_no_queue(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nsvcB 3\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nmetricA 5\n", False)
        summary = self._detect(auto_queue=True)
        self.assertIn("baseline initialized with 2 services, 1 metrics", summary)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(queue, [])

    def test_second_run_detects_new_service_and_auto_queues(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\n", False)
        self._detect(auto_queue=False)

        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nsvcC 2\n", False)
        summary = self._detect(auto_queue=True)
        self.assertIn("svcC", summary)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        match = next((j for j in queue if j["source"] == "svcC"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["requested_by"], "auto-detect")
        self.assertEqual(match["status"], "pending")

    def test_drift_detection_flags_changed_keys(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\n", False)
        self._detect(auto_queue=False)  # seed baseline (first run)

        self.responses["bag_keys(resource)"] = ("key n\nservice.name 5\n", False)
        self._detect(auto_queue=False, check_drift=True)  # seeds keys_hash, no drift yet

        self.responses["bag_keys(resource)"] = ("key n\nservice.name 5\nnew_attr 2\n", False)
        summary = self._detect(auto_queue=False, check_drift=True)
        self.assertIn("drifted_services", summary)
        self.assertIn("svcA", summary)

    def test_drift_check_batches_known_services_into_one_query(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nsvcB 3\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\n", False)
        self._detect(auto_queue=False)

        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nsvcB 3\n", False)
        self.responses["by service, key=tostring(k)"] = (
            "service key n\nsvcA service.name 5\nsvcA host.name 5\n"
            "svcB service.name 5\n", False)
        before = len(self.calls)
        self._detect(auto_queue=False, check_drift=True)
        drift_calls = self.calls[before:]
        self.assertEqual(len(drift_calls), 3)  # services, metrics, one grouped drift scan
        self.assertEqual(
            sum("service['service.name'] == 'svc" in c[3] for c in drift_calls), 0
        )

    def test_malformed_rows_dont_crash(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\n\n   \nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = ("(no rows)", False)
        summary = self._detect(auto_queue=False)
        self.assertIn("baseline initialized with 1 services, 0 metrics", summary)

    def test_metrics_never_autoqueued_and_services_capped(self):
        # Seed an empty-ish baseline, then a big cluster appears on run 2.
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nknown 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\n", False)
        self._detect(auto_queue=False)  # first run seeds {known}

        svcs = "service total\nknown 5\n" + "\n".join(f"s{i} 1" for i in range(8))
        mets = "metric_name samples\n" + "\n".join(f"bzrk.m{i} 1" for i in range(40))
        self.responses["by service=tostring(resource['service.name'])"] = (svcs, False)
        self.responses["summarize samples=count() by metric_name"] = (mets, False)
        self._detect(auto_queue=True)

        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        pending = [j for j in queue if j.get("status") == "pending"]
        self.assertLessEqual(len(pending), pf.MAX_AUTOQUEUE_PER_RUN)
        self.assertTrue(all(j["kind"] == "service" for j in pending))
        self.assertFalse(any(j["kind"] == "metric" for j in queue))

    def test_ephemeral_numeric_service_names_never_queued(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nknown 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\n", False)
        self._detect(auto_queue=False)  # seed {known}

        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nknown 5\n3919786 2\nreal-svc 3\n", False)
        self._detect(auto_queue=True)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        sources = {j["source"] for j in queue if j.get("status") == "pending"}
        self.assertIn("real-svc", sources)
        self.assertNotIn("3919786", sources)
        baseline = pf.load_json_dict(pf._known_sources_path())
        self.assertNotIn("3919786", baseline.get("services", {}))

    def test_seed_then_autoqueue_finds_nothing(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nsvcB 3\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nsystem.cpu 5\n", False)
        self._detect(auto_queue=False)          # first run seeds
        summary = self._detect(auto_queue=True)  # nothing new now
        self.assertEqual(summary, "No new sources.")
        self.assertEqual(bm.load_json_list(bm.DISCOVERY_QUEUE_PATH), [])

    def test_both_queries_fail_returns_error_baseline_unchanged(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nmet1 2\n", False)
        self._detect()  # seed baseline

        baseline_before = pf.load_json_dict(pf._known_sources_path())

        self.responses["by service=tostring(resource['service.name'])"] = (
            "connection timeout", True)
        self.responses["summarize samples=count() by metric_name"] = (
            "connection timeout", True)
        result = self._detect()
        self.assertIn("failed", result)
        self.assertIn("Baseline unchanged", result)

        baseline_after = pf.load_json_dict(pf._known_sources_path())
        self.assertEqual(baseline_before, baseline_after)

    def test_first_run_with_partial_failure_refuses_to_seed(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "backend error", True)
        result = self._detect()
        self.assertIn("failed", result)
        self.assertIn("cannot initialize baseline", result)
        self.assertFalse(pf.load_json_dict(pf._known_sources_path()))

    def test_services_failure_skips_services_dimension(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nmet1 2\n", False)
        self._detect()  # seed

        self.responses["by service=tostring(resource['service.name'])"] = (
            "error", True)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nmet1 2\nnewmet 3\n", False)
        result = self._detect()
        self.assertIn("services query failed", result)
        self.assertIn("new_metrics", result)
        baseline = pf.load_json_dict(pf._known_sources_path())
        self.assertIn("svcA", baseline["services"])
        self.assertIn("newmet", baseline["metrics"])

    def test_metrics_failure_skips_metrics_dimension(self):
        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "metric_name samples\nmet1 2\n", False)
        self._detect()  # seed

        self.responses["by service=tostring(resource['service.name'])"] = (
            "service total\nsvcA 5\nnewsvc 3\n", False)
        self.responses["summarize samples=count() by metric_name"] = (
            "error", True)
        result = self._detect()
        self.assertIn("metrics query failed", result)
        self.assertIn("newsvc", result)
        baseline = pf.load_json_dict(pf._known_sources_path())
        self.assertIn("newsvc", baseline["services"])
        self.assertIn("met1", baseline["metrics"])


# ---------- P4: generation pipeline ----------
class GenerationPipelineTest(ParserFactoryTestBase):
    def _reply(self, queries):
        return {"choices": [{"message": {"content": json.dumps({"queries": queries})}}]}

    def test_happy_path_two_queries_saved_with_metadata(self):
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        self.default_response = ("row1 col\nval 5", False)
        queries = [
            {"name": "overview", "description": "overview", "kql": f"{bm.TABLE} | where resource['service.name'] == 'mysvc' | summarize n=count() | take 1", "since": "1h ago"},
            {"name": "errors", "description": "errors", "kql": f"{bm.TABLE} | where resource['service.name'] == 'mysvc' | where severity_text == 'ERROR' | take 10", "since": "1h ago"},
        ]
        self.llm_responses = [(self._reply(queries), None)]

        report, ok = pf.generate_parser_for({"source": "mysvc", "kind": "service", "role_hint": "sre"})
        self.assertTrue(ok, report)
        self.assertEqual(report["status"], "done")
        saved = report["report"]["queries_saved"]
        self.assertEqual(len(saved), 2)
        for nm in saved:
            self.assertTrue(nm.startswith("mysvc_"), nm)

        items = bm.load_learned()
        names = [it["name"] for it in items]
        for nm in saved:
            self.assertIn(nm, names)
        entry = next(it for it in items if it["name"] == saved[0])
        self.assertEqual(entry["generated_by"]["provider"], "hermes")
        self.assertEqual(entry.get("roles"), ["sre"])

        amendments = bm.load_json_list(Path(bm.LEARNED_PATH).parent / "amendments_log.json")
        gen_actions = [a for a in amendments if a["action"] == "generated"]
        self.assertEqual(len(gen_actions), 2)

    def test_invalid_kql_prefix_feeds_back_and_succeeds_next_attempt(self):
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        self.default_response = ("row1\nval 5", False)
        bad = [{"name": "bad", "description": "d", "kql": "NOT_TABLE | take 1", "since": "1h ago"}]
        good = [{"name": "good", "description": "d", "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}]
        self.llm_responses = [(self._reply(bad), None), (self._reply(good), None)]

        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertTrue(ok, report)
        self.assertEqual(report["report"]["attempts"], 2)
        second_call_prompt = self._llm_calls[1][2]["messages"][-1]["content"]
        self.assertIn("invalid KQL", second_call_prompt)

    def test_escalation_to_next_provider_after_repeated_failures(self):
        os.environ["BERSERK_LLM_LADDER"] = "hermes,openai"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self._stub_profile_responses()
        self.default_response = ("row1\nval 5", False)
        good = [{"name": "ok", "description": "d", "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}]
        # 5 unparseable hermes replies exhaust that provider, then openai succeeds
        self.llm_responses = (
            [({"choices": [{"message": {"content": "not json"}}]}, None)] * 5
            + [(self._reply(good), None)]
        )
        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertTrue(ok, report)
        self.assertEqual(report["report"]["provider"], "openai")

    def test_all_providers_fail_first_call_needs_human(self):
        os.environ["BERSERK_LLM_LADDER"] = "anthropic"  # no ANTHROPIC_API_KEY set
        self._stub_profile_responses()
        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertFalse(ok)
        self.assertEqual(report["status"], "needs_human")
        self.assertEqual(bm.load_learned(), [])

    def test_name_collision_appends_gen_suffix(self):
        bm.save_learned([{"name": "x_overview", "description": "human", "kql": f"{bm.TABLE} | take 1"}])
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        self.default_response = ("row1\nval 5", False)
        queries = [{"name": "overview", "description": "d", "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}]
        self.llm_responses = [(self._reply(queries), None)]

        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertTrue(ok, report)
        saved = report["report"]["queries_saved"]
        self.assertEqual(saved, ["x_overview_gen"])

        items = bm.load_learned()
        human_entry = next(it for it in items if it["name"] == "x_overview")
        self.assertEqual(human_entry["description"], "human")
        gen_entry = next(it for it in items if it["name"] == "x_overview_gen")
        self.assertIn("generated_by", gen_entry)

    # ---- FVR-002 regressions: policy bypass via quoted operator text ----
    def test_summarize_inside_quoted_string_is_rejected(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | where body contains '| summarize ' | project body",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertIn("take", err.lower())

    def test_summarize_alone_without_terminal_take_is_rejected(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | summarize n=count() by service",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertIn("take", err.lower())

    def test_take_inside_comment_is_ignored(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | where isnotnull(body) // ends with | take 5",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)

    def test_take_zero_is_rejected(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 0",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)

    def test_take_fifty_one_is_rejected(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 51",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)

    def test_terminal_take_fifty_is_accepted(self):
        self.default_response = ("row\nval", False)
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 50",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertTrue(ok, err)

    # ---- lower-severity: semicolons rejected unconditionally ----
    def test_semicolon_in_query_is_rejected(self):
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 1; {bm.TABLE} | take 999",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertIn("semicolon", err.lower())

    def test_semicolon_inside_quoted_string_is_still_rejected(self):
        """Even a 'legitimate' semicolon inside a string literal is
        rejected -- the policy is unconditional, not just for bare
        statement-separator semicolons, since Berserk's real handling of
        the character can't be verified without a live authenticated
        check."""
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | where body contains 'a;b' | take 1",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertFalse(ok)
        self.assertIn("semicolon", err.lower())

    def test_query_without_semicolon_is_unaffected(self):
        self.default_response = ("row\nval", False)
        q = {"name": "n", "description": "d",
             "kql": f"{bm.TABLE} | take 1",
             "since": "1h ago"}
        ok, err, _ = pf.validate_generated_query(q)
        self.assertTrue(ok, err)

    def test_fenced_reply_parses(self):
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        self.default_response = ("row1\nval 5", False)
        inner = json.dumps({"queries": [
            {"name": "ok", "description": "d", "kql": f"{bm.TABLE} | take 1", "since": "1h ago"}
        ]})
        fenced = f"```json\n{inner}\n```"
        self.llm_responses = [({"choices": [{"message": {"content": fenced}}]}, None)]

        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertTrue(ok, report)

    # ---- lower-severity: non-string provider content must not crash ----
    def test_non_string_content_produces_controlled_error_not_a_crash(self):
        """A provider can legitimately return content: null for a tool-
        call-only reply (no exception raised inside llm_complete -- the
        key exists, its value just isn't text). _parse_generated_reply
        previously called _strip_fences(None), an unhandled AttributeError
        that would propagate uncaught through generate_parser_for."""
        os.environ["BERSERK_LLM_LADDER"] = "hermes"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        self._stub_profile_responses()
        self.llm_responses = [
            ({"choices": [{"message": {"content": None}}]}, None)
            for _ in range(pf.MAX_TOTAL_ATTEMPTS)
        ]
        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertFalse(ok)
        self.assertEqual(report["status"], "needs_human")

    def test_parse_generated_reply_rejects_every_non_string_type_directly(self):
        for bad in (None, 42, 3.14, [], {}, True):
            queries, err = pf._parse_generated_reply(bad, "x")
            self.assertIsNone(queries)
            self.assertIn("non-string content", err)
            self.assertIn(type(bad).__name__, err)

    # ---- F-005: one total-attempt budget across the WHOLE ladder ----
    def test_total_attempt_budget_across_ladder_is_capped(self):
        """Previously MAX_REFINEMENT_ATTEMPTS was a PER-PROVIDER budget, so
        a 3-provider ladder could make up to 15 LLM calls for one job.
        Every attempt below returns malformed JSON (a parse_err), which
        never triggers the immediate-provider-failure shortcut, so the old
        code would burn a full 5-attempt budget on each of the 3
        providers. The new code must stop at MAX_TOTAL_ATTEMPTS regardless
        of ladder length."""
        os.environ["BERSERK_LLM_LADDER"] = "hermes,openai,anthropic"
        os.environ["BERSERK_LLM_HERMES_MODEL"] = "test-model"
        os.environ["OPENAI_API_KEY"] = "dummy-key"
        os.environ["ANTHROPIC_API_KEY"] = "dummy-key"
        self._stub_profile_responses()
        self.llm_responses = [
            ({"choices": [{"message": {"content": "not valid json"}}]}, None)
            for _ in range(20)
        ]
        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertFalse(ok)
        self.assertLessEqual(len(self._llm_calls), pf.MAX_TOTAL_ATTEMPTS)
        self.assertEqual(len(self._llm_calls), pf.MAX_TOTAL_ATTEMPTS)

    # ---- F-005: one monotonic deadline spans the whole job ----
    def test_job_deadline_aborts_before_any_llm_call(self):
        self._stub_profile_responses()
        orig_deadline = pf.JOB_DEADLINE_SECONDS
        pf.JOB_DEADLINE_SECONDS = -1  # already expired before the function even starts
        try:
            report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        finally:
            pf.JOB_DEADLINE_SECONDS = orig_deadline
        self.assertFalse(ok)
        self.assertIn("deadline", report["reason"])
        self.assertEqual(len(self._llm_calls), 0)

    # ---- F-005: _bound_report guarantees a serialization bound ----
    def test_bound_report_trims_oversized_list_field(self):
        report = {
            "status": "needs_human",
            "reason": "all providers exhausted",
            "last_errors": ["x" * 500 for _ in range(20)],
        }
        bounded = pf._bound_report(report)
        self.assertLessEqual(len(json.dumps(bounded)), pf.REPORT_CAP)
        self.assertIn("last_errors", bounded)

    def test_bound_report_caps_oversized_scalar_field_in_skeleton_fallback(self):
        report = {
            "status": "needs_human",
            "reason": "y" * 5000,
            "last_errors": ["z"],
        }
        bounded = pf._bound_report(report)
        self.assertTrue(bounded.get("_truncated"))
        self.assertLessEqual(len(json.dumps(bounded)), pf.REPORT_CAP)
        self.assertLessEqual(len(bounded["reason"]), 200)

    def test_bound_report_passthrough_when_already_small(self):
        report = {"status": "done", "report": {"provider": "hermes"}}
        self.assertEqual(pf._bound_report(report), report)


# ---------- P6: headless worker mode ----------
class WorkerCliTest(ParserFactoryTestBase):
    def test_run_worker_pass_no_jobs_exit_zero(self):
        self.responses["by service=tostring(resource['service.name'])"] = ("service total\n", False)
        self.responses["summarize samples=count() by metric_name"] = ("metric_name samples\n", False)
        code = bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)
        self.assertEqual(code, 0)

    def test_run_worker_pass_needs_human_exit_one(self):
        bm.save_json_list(bm.DISCOVERY_QUEUE_PATH, [
            {"source": "x", "kind": "service", "status": "pending",
             "role_hint": "", "requested_by": "manual", "ts": "t"},
        ])
        os.environ["BERSERK_LLM_LADDER"] = "anthropic"  # no key -> immediate failure
        self.responses["by service=tostring(resource['service.name'])"] = ("service total\n", False)
        self.responses["summarize samples=count() by metric_name"] = ("metric_name samples\n", False)
        self._stub_profile_responses()

        code = bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)
        self.assertEqual(code, 1)
        queue = bm.load_json_list(bm.DISCOVERY_QUEUE_PATH)
        self.assertEqual(queue[0]["status"], "needs_human")

    # ---- run_worker_pass Discord alert wiring ----
    def _discord_alert_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        received = []

        class AlertHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length).decode("utf-8"))["text"])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), AlertHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port, received

    def test_run_worker_pass_skips_alert_when_nothing_noteworthy(self):
        server, port, received = self._discord_alert_server()
        orig_url, orig_secret = bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET
        bm.DISCORD_ALERT_URL = f"http://127.0.0.1:{port}/alert"
        bm.DISCORD_ALERT_SECRET = "s3cr3t"
        try:
            self.responses["by service=tostring(resource['service.name'])"] = (
                "service total\nknown 5\n", False)
            self.responses["summarize samples=count() by metric_name"] = (
                "metric_name samples\n", False)
            bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)  # seed baseline
            received.clear()
            bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)  # nothing new
            self.assertEqual(received, [])
        finally:
            bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET = orig_url, orig_secret
            server.shutdown()
            server.server_close()

    def test_run_worker_pass_posts_alert_when_new_source_found(self):
        server, port, received = self._discord_alert_server()
        orig_url, orig_secret = bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET
        bm.DISCORD_ALERT_URL = f"http://127.0.0.1:{port}/alert"
        bm.DISCORD_ALERT_SECRET = "s3cr3t"
        try:
            self.responses["by service=tostring(resource['service.name'])"] = (
                "service total\n", False)
            self.responses["summarize samples=count() by metric_name"] = (
                "metric_name samples\n", False)
            bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)  # seed empty baseline
            received.clear()
            self.responses["by service=tostring(resource['service.name'])"] = (
                "service total\nnewsvc 5\n", False)
            bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)
            self.assertEqual(len(received), 1)
            self.assertIn("newsvc", received[0])
        finally:
            bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET = orig_url, orig_secret
            server.shutdown()
            server.server_close()

    def test_run_worker_pass_drains_amendments_changelog(self):
        server, port, received = self._discord_alert_server()
        orig_url, orig_secret = bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET
        bm.DISCORD_ALERT_URL = f"http://127.0.0.1:{port}/alert"
        bm.DISCORD_ALERT_SECRET = "s3cr3t"
        try:
            amendments_path = Path(bm.LEARNED_PATH).parent / "amendments_log.json"
            bm.save_json_list(amendments_path, [
                {"name": "q1", "description": "d1", "action": "created"},
            ])
            self.responses["by service=tostring(resource['service.name'])"] = (
                "service total\nknown 5\n", False)
            self.responses["summarize samples=count() by metric_name"] = (
                "metric_name samples\n", False)
            bm.run_worker_pass(auto_queue=False, max_jobs=1, check_drift=False)
            self.assertEqual(bm.load_json_list(amendments_path), [])
            self.assertTrue(any("q1" in text for text in received))
        finally:
            bm.DISCORD_ALERT_URL, bm.DISCORD_ALERT_SECRET = orig_url, orig_secret
            server.shutdown()
            server.server_close()


# ---------- P7: security posture ----------
class SecurityTest(ParserFactoryTestBase):
    def test_no_key_material_in_report(self):
        os.environ["OPENAI_API_KEY"] = "sk-supersecretvalue"
        os.environ["BERSERK_LLM_LADDER"] = "openai"
        self.llm_responses = [(None, "HTTP 401")]
        self._stub_profile_responses()

        report, ok = pf.generate_parser_for({"source": "x", "kind": "service", "role_hint": ""})
        self.assertFalse(ok)
        blob = json.dumps(report)
        self.assertNotIn("sk-supersecretvalue", blob)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits only")
    def test_known_sources_store_has_private_permissions(self):
        self.responses["by service=tostring(resource['service.name'])"] = ("service total\n", False)
        self.responses["summarize samples=count() by metric_name"] = ("metric_name samples\n", False)
        pf.detect_new_sources(
            since="24h ago", auto_queue=False, check_drift=False,
            load_json_list=bm.load_json_list, save_json_list=bm.save_json_list,
            discovery_queue_path=bm.DISCOVERY_QUEUE_PATH, active_role="all",
        )
        known_path = Path(bm.LEARNED_PATH).parent / pf.KNOWN_SOURCES_PATH_NAME
        self.assertEqual(oct(known_path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(known_path.parent.stat().st_mode & 0o777), oct(0o700))

    def test_sample_body_secret_is_redacted_before_persistence_and_prompt(self):
        """DR-002: ALL PII/secret types must be absent from profile and prompt."""
        sensitive_values = [
            "password=dummy-telemetry-secret",
            "user@example.com",
            "192.168.1.10",
            "A1b2C3d4E5f6G7h8I9j0KLMNOPqrstuv",
        ]
        sensitive_body = " ".join(sensitive_values)
        self._stub_profile_responses()
        self.responses["take 6"] = (sensitive_body, False)
        os.environ["BERSERK_LLM_LADDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "test-key"
        self.llm_responses = [(None, "HTTP 401")]

        profile, err = pf.build_source_profile("mysvc", "service", "24h ago")
        self.assertIsNone(err)
        for val in sensitive_values:
            self.assertNotIn(val, profile["sample_excerpt"], f"leaked: {val}")

        knowledge_path = Path(bm.LEARNED_PATH).parent / pf.SCHEMA_KNOWLEDGE_PATH_NAME
        persisted = knowledge_path.read_text(encoding="utf-8")
        for val in sensitive_values:
            self.assertNotIn(val, persisted, f"leaked in persisted: {val}")

        report, ok = pf.generate_parser_for({"source": "mysvc", "kind": "service", "role_hint": ""})
        self.assertFalse(ok)
        self.assertEqual(len(self._llm_calls), 1)
        _url, _headers, payload = self._llm_calls[0]
        payload_text = json.dumps(payload)
        for val in sensitive_values:
            self.assertNotIn(val, payload_text, f"leaked in prompt: {val}")

    def test_missing_redactor_fails_closed(self):
        """DR-002: if redactor is not provided, configure must raise."""
        with self.assertRaises(ValueError):
            pf.configure(
                bzrk_search=lambda q, s: ("", False), table="T",
                get_store_dir=lambda: Path(self._tmp.name),
                ensure_private_dir=lambda p: None, now_iso=lambda: "",
                log=lambda m: None, persist_learned_query=lambda e, a: {},
                sanitize_name=lambda n: n, redact=None,
            )
        # configure raised before mutating state, so globals are unchanged

    def test_broken_redactor_fails_before_persistence(self):
        """DR-002: a redactor that returns non-string must not allow persistence."""
        orig = pf._redact
        try:
            pf._redact = lambda text: 42
            self._stub_profile_responses()
            self.responses["take 6"] = ("some sample data", False)
            profile, err = pf.build_source_profile("badsvc", "service", "24h ago")
            self.assertIsNone(profile)
            self.assertIn("redaction failed", err)
        finally:
            pf._redact = orig

    def test_default_query_projects_structural_info_not_values(self):
        """DR-002: default queries must not project raw resource/attributes/body values."""
        query = pf._q_discover_sample("myservice")
        project_clause = query.split("project", 1)[1]
        self.assertIn("bag_keys(resource)", project_clause)
        self.assertIn("has_body", project_clause)
        fields = [f.strip().split("=")[0] for f in project_clause.split(",")]
        for f in fields:
            self.assertNotIn(f.strip(), ("resource", "attributes", "body"),
                             f"raw value field '{f.strip()}' projected")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SourceNameGuardTest(ParserFactoryTestBase):
    def test_build_source_profile_rejects_injection_source(self):
        # a source with a single quote must be refused before any bzrk call
        self.default_response = ("SHOULD-NOT-RUN", False)
        profile, err = pf.build_source_profile("x'; drop", "service", "1h ago")
        self.assertIsNone(profile)
        self.assertIn("invalid source name", err)
        self.assertEqual(self.calls, [])  # no query was executed
