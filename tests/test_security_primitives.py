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


class SharedHttpTest(unittest.TestCase):
    def test_header_parser_fails_on_malformed_and_controls(self):
        with self.assertRaisesRegex(ValueError, "expected name=value"):
            _http.parse_header_items("Authorization Bearer token")
        with self.assertRaisesRegex(ValueError, "control"):
            _http.parse_header_items("Authorization=Bearer token\nX-Evil=yes")

    def test_header_parser_keeps_json_content_type(self):
        headers = _http.parse_header_items(
            "Content-Type=text/plain,Authorization=Bearer token"
        )
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertNotIn("text/plain", headers.values())

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
                "run_eval.py", "--backend", "mock", "--limit", "1", str(cases),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(stdout):
                eval_module.main()
            reports = list((Path(directory) / "results").glob("mock_mock-*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["backend"], "mock")
            self.assertEqual(len(report["rows"]), 1)
            self.assertIn("tool-selection accuracy", stdout.getvalue())

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
            BERSERK_MCP_ROLE="sre", BERSERK_MCP_PRIMERS_DIR="relative/primers",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BERSERK_MCP_PRIMERS_DIR", result.stderr)

    def test_traversal_primer_directory_fails_startup(self):
        traversal = str(Path(tempfile.gettempdir()) / "safe" / ".." / "primers")
        result = self._fresh_import(
            BERSERK_MCP_ROLE="sre", BERSERK_MCP_PRIMERS_DIR=traversal,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not contain '..'", result.stderr)

    def test_configured_empty_primer_directory_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._fresh_import(
                BERSERK_MCP_ROLE="sre", BERSERK_MCP_PRIMERS_DIR=directory,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sre.md", result.stderr)

    def test_configured_primer_loads_and_all_role_needs_no_primer(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sre.md").write_text("custom-secure-primer", encoding="utf-8")
            sre = self._fresh_import(
                BERSERK_MCP_ROLE="sre", BERSERK_MCP_PRIMERS_DIR=directory,
            )
        self.assertEqual(sre.returncode, 0, sre.stderr)
        self.assertIn("custom-secure-primer", sre.stdout)
        all_role = self._fresh_import(BERSERK_MCP_ROLE="all")
        self.assertEqual(all_role.returncode, 0, all_role.stderr)
        self.assertIn("Answer observability questions", all_role.stdout)


if __name__ == "__main__":
    unittest.main()
