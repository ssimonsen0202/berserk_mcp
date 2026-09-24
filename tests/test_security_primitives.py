"""Regression tests for the shared Phase 2 store and HTTP boundaries."""

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import warnings
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _http  # noqa: E402
import _store  # noqa: E402
import ai_finops as af  # noqa: E402
import berserk_mcp as bm  # noqa: E402
import parser_factory as pf  # noqa: E402
import schema_registry as sr  # noqa: E402


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("berserk_run_eval", ROOT / "evals" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SharedStoreTest(unittest.TestCase):
    def test_modules_share_one_path_error_type(self):
        self.assertIs(bm.StorePathError, _store.StorePathError)
        self.assertIs(pf.StorePathError, _store.StorePathError)

    # Covers SECURITY.md#filesystem-stores-and-publication-outputs
    def test_schema_cache_rejects_relative_path(self):
        with self.assertRaises(_store.StorePathError):
            sr._write_cache("relative/schema.json", {"x": 1})

    def test_ai_finops_output_rejects_controls_and_relative_paths(self):
        with self.assertRaises(_store.StorePathError):
            af._atomic_write_text("relative/report.md", "x")
        bad = str(Path(tempfile.gettempdir()) / "report") + "\n.md"
        with self.assertRaises(_store.StorePathError):
            af._atomic_write_text(bad, "x")

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    # Covers SECURITY.md#filesystem-stores-and-publication-outputs
    def test_private_write_does_not_chmod_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o755)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _store.atomic_write_json(shared / "store.json", {"ok": True})
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((shared / "store.json").stat().st_mode), 0o600)
            self.assertTrue(any("permissions left unchanged" in str(item.message) for item in caught))

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_private_write_hardens_only_directories_it_creates(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new" / "private" / "store.json"
            _store.atomic_write_json(target, {"ok": True})
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_public_write_preserves_existing_directory_and_file_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            published = Path(directory) / "published"
            published.mkdir()
            os.chmod(published, 0o755)
            target = published / "report.csv"
            target.write_text("old", encoding="utf-8")
            os.chmod(target, 0o640)
            _store.atomic_write_text(target, "new", private=False)
            self.assertEqual(stat.S_IMODE(published.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    @unittest.skipUnless(os.name == "nt", "Windows DACL assertion")
    def test_private_write_sets_current_user_only_windows_dacl(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private" / "store.json"
            _store.atomic_write_json(target, {"ok": True})
            self.assertTrue(_store.windows_private_dacl(target.parent))
            self.assertTrue(_store.windows_private_dacl(target))


class PlaintextPolicySeparationTest(unittest.TestCase):
    """Security review 2026-09-24 finding 6: the LLM plaintext opt-in must not
    weaken OTLP or CanonLoom, which require HTTPS for every non-loopback host."""

    REMOTE_HTTP = "http://192.0.2.10:4318/v1/logs"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_network(self):
        return mock.patch.object(_http.NO_REDIRECT_OPENER, "open", side_effect=AssertionError("network used"))

    def test_json_wrappers_reject_remote_plaintext_when_caller_forbids_it(self):
        with self._no_network():
            for call in (
                lambda: _http.http_post_json(self.REMOTE_HTTP, {}, {}, allow_plaintext_remote=False),
                lambda: _http.http_get_json(self.REMOTE_HTTP, {}, allow_plaintext_remote=False),
            ):
                data, err = call()
                self.assertIsNone(data)
                self.assertIn("plaintext http to a non-loopback host is rejected", err)

    def test_llm_callers_keep_the_documented_opt_in(self):
        with mock.patch.object(_http, "request_json", return_value={"ok": True}) as request:
            data, err = _http.http_post_json(self.REMOTE_HTTP, {}, {})
        self.assertEqual((data, err), ({"ok": True}, None))
        self.assertIsNone(request.call_args.kwargs["allow_plaintext_remote"])

    def test_codex_adapter_sends_otlp_with_plaintext_forbidden(self):
        spec = importlib.util.spec_from_file_location(
            "codex_adapter_under_test", ROOT / "ingestion" / "codex_adapter.py"
        )
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state:
            sessions = Path(home) / "sessions" / "2026" / "09" / "24"
            sessions.mkdir(parents=True)
            event = {
                "timestamp": "2026-09-24T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hi"},
            }
            (sessions / "rollout-2026-09-24T00-00-00-11111111-2222-3333-4444-555555555555.jsonl").write_text(
                json.dumps(event) + "\n"
            )
            seen = []

            def fake_post(url, headers, payload, timeout=120, *, allow_plaintext_remote=None):
                seen.append(allow_plaintext_remote)
                return {}, None

            with mock.patch.object(adapter._http, "http_post_json", fake_post):
                adapter.run(home, str(Path(state) / "codex_state"), self.REMOTE_HTTP, "bearer", "host")
        self.assertTrue(seen, "adapter posted nothing; the fixture did not exercise the OTLP path")
        self.assertEqual(set(seen), {False})


class LoopbackBypassesProxyTest(unittest.TestCase):
    """Finding 4: a request validated as loopback must not be sent to a proxy."""

    def test_loopback_request_goes_direct_even_with_http_proxy_set(self):
        seen = {}

        class Target(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Target)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # urllib reads proxy variables when an opener is built (at import), so the
        # client runs in a subprocess whose environment names a dead proxy from the
        # start, as a real deployment would. Through that proxy the request fails.
        port = server.server_address[1]
        script = (
            "import sys, _http; "
            f"print(_http.request_json('http://127.0.0.1:{port}/v1', {{'Authorization': 'Bearer t'}}, {{'a': 1}}, timeout=5))"
        )
        env = dict(
            os.environ, http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", no_proxy="", NO_PROXY=""
        )
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertIn("'ok': True", result.stdout)
        self.assertEqual(seen["auth"], "Bearer t")

    def test_fully_qualified_loopback_spellings_are_loopback(self):
        for host in ("localhost.", "127.0.0.1.", "LOCALHOST."):
            with self.subTest(host=host):
                self.assertTrue(_http.is_loopback_host(host))
                self.assertIs(_http._opener_for(f"http://{host}:8080/v1"), _http.LOOPBACK_OPENER)
        self.assertFalse(_http.is_loopback_host("example.com."))
        self.assertFalse(_http.is_loopback_host("."))

    def test_remote_hosts_keep_the_default_opener(self):
        self.assertIs(_http._opener_for("https://api.example.com/v1"), _http.NO_REDIRECT_OPENER)
        # An empty ProxyHandler registers no methods, so it is absent from .handlers,
        # but supplying it stops urllib adding the environment-driven default.
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9"}):
            fresh = urllib.request.build_opener(urllib.request.ProxyHandler({}), _http.NoRedirectHandler)
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in fresh.handlers))
        self.assertFalse(
            any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in _http.LOOPBACK_OPENER.handlers)
        )


class CodexAdapterStatePathTest(unittest.TestCase):
    """Finding 9: the Codex adapter validates its state path before any read or write."""

    def _adapter(self):
        spec = importlib.util.spec_from_file_location(
            "codex_adapter_state_test", ROOT / "ingestion" / "codex_adapter.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_relative_and_traversal_paths_are_refused_before_io(self):
        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as cwd:
            with mock.patch("os.getcwd", return_value=cwd):
                for bad in ("state", "./state", "/tmp/base/../unexpected"):
                    with self.subTest(state_dir=bad):
                        with self.assertRaises(_store.StorePathError):
                            adapter.load_state(bad)
                        with self.assertRaises(_store.StorePathError):
                            adapter.save_state(bad, {"offsets": {}})
            self.assertEqual(os.listdir(cwd), [])
        self.assertFalse(Path("/tmp/base").exists() and Path("/tmp/unexpected").exists())

    def test_cli_refuses_a_relative_state_dir(self):
        adapter = self._adapter()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
            with mock.patch("sys.stderr", io.StringIO()):
                adapter.main(["--state-dir", "relative/state", "--dry-run"])
        self.assertEqual(raised.exception.code, 2)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_state_is_saved_privately_and_round_trips(self):
        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as base:
            state_dir = Path(base) / "fresh" / "codex_adapter"
            adapter.save_state(str(state_dir), {"offsets": {"a": 3}})
            state_file = state_dir / "codex_adapter_state.json"
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)
            self.assertEqual(adapter.load_state(str(state_dir)), {"offsets": {"a": 3}})


class SharedHttpTest(unittest.TestCase):
    # Covers SECURITY.md#outbound-http
    def test_header_parser_fails_on_malformed_and_controls(self):
        with self.assertRaisesRegex(ValueError, "expected name=value"):
            _http.parse_header_items("Authorization Bearer token")
        with self.assertRaisesRegex(ValueError, "control"):
            _http.parse_header_items("Authorization=Bearer token\nX-Evil=yes")

    def test_header_parser_keeps_json_content_type(self):
        headers = _http.parse_header_items("Content-Type=text/plain,Authorization=Bearer token")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertNotIn("text/plain", headers.values())

    # Covers SECURITY.md#outbound-http
    def test_bounded_reader_rejects_one_byte_over_limit(self):
        with self.assertRaisesRegex(ValueError, "exceeds 4 bytes"):
            _http.read_bounded(io.BytesIO(b"12345"), cap=4)
        self.assertEqual(_http.read_bounded(io.BytesIO(b"1234"), cap=4), b"1234")

    def test_eval_http_error_does_not_read_or_echo_body(self):
        eval_module = _load_eval_module()

        class Error:
            code = 401

            def __init__(self):
                self.closed = False

            def read(self):
                raise AssertionError("provider body must not be read")

            def close(self):
                self.closed = True

        error = Error()
        message = eval_module._http_error_message(error)
        self.assertEqual(message, "HTTP 401 from backend")
        self.assertTrue(error.closed)

    def test_eval_mock_backend_runs_end_to_end(self):
        eval_module = _load_eval_module()
        cases = ROOT / "evals" / "router_cases.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            eval_module.HERE = Path(directory)
            stdout = io.StringIO()
            argv = [
                "run_eval.py",
                "--backend",
                "mock",
                "--limit",
                "1",
                str(cases),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(stdout):
                eval_module.main()
            reports = list((Path(directory) / "results").glob("mock_mock-*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["backend"], "mock")
            self.assertEqual(len(report["rows"]), 1)
            self.assertIn("tool-selection accuracy", stdout.getvalue())

    # Covers SECURITY.md#outbound-http
    def test_eval_post_does_not_follow_redirect_with_credential(self):
        eval_module = _load_eval_module()
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(dict(self.headers.items()))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        target_server = HTTPServer(("127.0.0.1", 0), Target)
        target_port = target_server.server_address[1]

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/")
                self.end_headers()

            def log_message(self, *args):
                pass

        redirect_server = HTTPServer(("127.0.0.1", 0), Redirect)
        redirect_port = redirect_server.server_address[1]
        for server in (target_server, redirect_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                eval_module._post(
                    f"http://127.0.0.1:{redirect_port}/",
                    {"Authorization": "Bearer test-secret"},
                    {"x": 1},
                )
            self.assertEqual(caught.exception.code, 302)
            caught.exception.close()
            self.assertEqual(received, [])
        finally:
            redirect_server.shutdown()
            target_server.shutdown()
            redirect_server.server_close()
            target_server.server_close()


class PrimerConfigurationTest(unittest.TestCase):
    def _fresh_import(self, **updates):
        env = dict(os.environ)
        env.pop("BERSERK_MCP_PRIMERS_DIR", None)
        env.pop("BERSERK_MCP_ROLE", None)
        env.update(updates)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-c", "import berserk_mcp; print(berserk_mcp.INSTRUCTIONS)"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_relative_primer_directory_fails_startup(self):
        result = self._fresh_import(
            BERSERK_MCP_ROLE="sre",
            BERSERK_MCP_PRIMERS_DIR="relative/primers",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BERSERK_MCP_PRIMERS_DIR", result.stderr)

    def test_traversal_primer_directory_fails_startup(self):
        traversal = str(Path(tempfile.gettempdir()) / "safe" / ".." / "primers")
        result = self._fresh_import(
            BERSERK_MCP_ROLE="sre",
            BERSERK_MCP_PRIMERS_DIR=traversal,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain '..'", result.stderr)

    def test_configured_empty_primer_directory_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._fresh_import(
                BERSERK_MCP_ROLE="sre",
                BERSERK_MCP_PRIMERS_DIR=directory,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sre.md", result.stderr)

    def test_configured_primer_loads_and_all_role_needs_no_primer(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sre.md").write_text("custom-secure-primer", encoding="utf-8")
            sre = self._fresh_import(
                BERSERK_MCP_ROLE="sre",
                BERSERK_MCP_PRIMERS_DIR=directory,
            )
        self.assertEqual(sre.returncode, 0, sre.stderr)
        self.assertIn("custom-secure-primer", sre.stdout)
        all_role = self._fresh_import(BERSERK_MCP_ROLE="all")
        self.assertEqual(all_role.returncode, 0, all_role.stderr)
        self.assertIn("Answer observability questions", all_role.stdout)


if __name__ == "__main__":
    unittest.main()
