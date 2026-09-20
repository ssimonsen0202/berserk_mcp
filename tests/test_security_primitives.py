"""Regression tests for the shared Phase 2 store and HTTP boundaries."""

import argparse
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

    def test_eval_empty_cases_file_exits_cleanly_instead_of_dividing_by_zero(self):
        # Codex round-11 finding: both the single-backend path and
        # _run_tier_policy divide by `total` (cases actually scored) when
        # printing/saving accuracy -- an empty or all-blank cases file
        # reached that division with total == 0, crashing with a raw
        # ZeroDivisionError traceback (and no report, no metric) instead
        # of a controlled error. Checked once, up front, before either
        # path calls a single model. Covers the single-backend path here;
        # the sibling --tier-policy path shares the exact same check.
        eval_module = _load_eval_module()
        with tempfile.TemporaryDirectory() as directory:
            eval_module.HERE = Path(directory)
            empty_cases = Path(directory) / "empty.jsonl"
            empty_cases.write_text("", encoding="utf-8")
            argv = ["run_eval.py", "--backend", "mock", str(empty_cases)]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as caught:
                    eval_module.main()
            self.assertIn("no cases to evaluate", str(caught.exception))
            self.assertEqual(list((Path(directory) / "results").glob("*.json")), [])

    def test_score_case_reports_arg_mismatch_independently_of_tool_match(self):
        eval_module = _load_eval_module()
        case = {
            "id": "c1",
            "expect_tool": "logs_for_service",
            "expect_args": {"service": "checkout"},
        }
        # Right tool, wrong argument value -- must NOT be tool_ok=True,
        # arg_ok=True; arg_ok must independently reflect the mismatch.
        tool_ok, arg_ok = eval_module.score_case(
            case, "logs_for_service", {"service": "wrong-service"},
        )
        self.assertTrue(tool_ok)
        self.assertFalse(arg_ok)

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

    def test_egress_connection_is_pinned_to_one_resolution(self):
        # Round-7 adversarial-review finding: validate_egress_destination()
        # approves a BERSERK_EGRESS_ALLOWED_HOSTS entry by comparing the
        # hostname as a STRING, then (without pinning) urllib performs its
        # OWN, separate DNS resolution moments later when connecting -- a
        # DNS-rebinding TOCTOU window. _http.NO_REDIRECT_OPENER now pins
        # every connection to exactly one getaddrinfo() resolution's
        # candidate list. This proves getaddrinfo is called at most once
        # per request by monkeypatching the MODULE-LEVEL socket.getaddrinfo
        # to count calls and confirm a real, successful request against
        # loopback still succeeds end to end.
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers.items()))
                self.send_response(200)
                body = b'{"ok": true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Target)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        import socket as socket_module
        orig_getaddrinfo = socket_module.getaddrinfo
        calls = []

        def counting_getaddrinfo(*args, **kwargs):
            calls.append(args[:2])
            return orig_getaddrinfo(*args, **kwargs)

        try:
            with mock.patch.object(socket_module, "getaddrinfo", counting_getaddrinfo):
                data, err = _http.http_get_json(f"http://localhost:{port}/mcp", {})
            self.assertIsNone(err)
            self.assertEqual(data, {"ok": True})
            self.assertEqual(len(received), 1)
            # Exactly one resolution for this one request -- proves the
            # actual TCP connect() reused the pinned candidate list rather
            # than triggering a second, independent lookup.
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "localhost")
        finally:
            server.shutdown()
            server.server_close()

    def test_egress_connection_pinning_preserves_host_header_and_errors(self):
        # The pin must not change what the server sees (Host header still
        # carries the original hostname the caller asked for) or how
        # resolution failures surface (still urllib.error.URLError via
        # http_get_json's existing "connection failed" branch, not a new
        # exception type/message introduced by the pinning mechanism).
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Host"))
                self.send_response(200)
                body = b'{"ok": true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Target)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            data, err = _http.http_get_json(f"http://127.0.0.1:{port}/mcp", {})
            self.assertIsNone(err)
            self.assertEqual(data, {"ok": True})
            self.assertEqual(received, [f"127.0.0.1:{port}"])
        finally:
            server.shutdown()
            server.server_close()

        # https, not http: a non-loopback plaintext-http URL is rejected by
        # validate_http_url's own, unrelated policy before ever reaching
        # resolution -- using https isolates the resolution-failure path
        # this test actually means to exercise.
        data, err = _http.http_get_json(
            "https://this-host-should-not-resolve.invalid/mcp", {}, timeout=2,
        )
        self.assertIsNone(data)
        self.assertEqual(err, "connection failed")

    def test_environment_http_proxy_is_never_used_even_for_a_loopback_request(self):
        # Round-9 adversarial-review finding: urllib.request.build_opener()'s
        # own default handler list always includes a ProxyHandler that reads
        # HTTP_PROXY/HTTPS_PROXY from the environment -- without overriding
        # it, an inherited/ambient proxy env var would silently reroute
        # EVERY outbound call through this module (including a loopback
        # destination validate_egress_destination() always treats as safe)
        # to the configured proxy host instead of the caller's actual
        # logical URL. Connection pinning does not help here: for a proxied
        # request, self.host at connect() time IS the proxy, so pinning
        # just pins resolution of the proxy's own hostname. Proves the real
        # target is reached DIRECTLY and the proxy is never contacted, by
        # running a real stub "proxy" server and asserting it receives
        # nothing.
        proxy_hits = []

        class ProxyStub(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        proxy_server = HTTPServer(("127.0.0.1", 0), ProxyStub)
        proxy_port = proxy_server.server_address[1]

        real_hits = []

        class RealTarget(BaseHTTPRequestHandler):
            def do_GET(self):
                real_hits.append(self.path)
                self.send_response(200)
                body = b'{"ok": true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        real_server = HTTPServer(("127.0.0.1", 0), RealTarget)
        real_port = real_server.server_address[1]
        for server in (proxy_server, real_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

        orig_http_proxy = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        try:
            data, err = _http.http_get_json(f"http://127.0.0.1:{real_port}/mcp", {})
            self.assertIsNone(err)
            self.assertEqual(data, {"ok": True})
            self.assertEqual(real_hits, ["/mcp"])
            self.assertEqual(proxy_hits, [], "the proxy must never be contacted")
        finally:
            proxy_server.shutdown()
            real_server.shutdown()
            proxy_server.server_close()
            real_server.server_close()
            if orig_http_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = orig_http_proxy


class LocalOnlyAndEgressPolicyTest(unittest.TestCase):
    """_http primitives backing task-01: BERSERK_LOCAL_ONLY and the
    approved-destination policy for outbound integrations."""

    _ENV_KEYS = (
        "BERSERK_LOCAL_ONLY",
        "BERSERK_EGRESS_ALLOWED_HOSTS",
        "BERSERK_EGRESS_ALLOWED_CIDRS",
    )

    def setUp(self):
        self._orig_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_local_only_enabled_defaults_false(self):
        self.assertFalse(_http.local_only_enabled())

    def test_local_only_enabled_accepts_common_truthy_spellings(self):
        for value in ("1", "true", "True", "yes", "on"):
            os.environ["BERSERK_LOCAL_ONLY"] = value
            self.assertTrue(_http.local_only_enabled(), value)

    def test_local_only_enabled_rejects_other_values(self):
        for value in ("0", "false", "no", "", "  "):
            os.environ["BERSERK_LOCAL_ONLY"] = value
            self.assertFalse(_http.local_only_enabled(), value)

    def test_validate_egress_destination_allows_loopback_always(self):
        _http.validate_egress_destination("http://127.0.0.1:8765/alert", label="x")
        _http.validate_egress_destination("http://localhost:3000/x", label="x")
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        _http.validate_egress_destination("http://127.0.0.1:8765/alert", label="x")

    def test_validate_egress_destination_unrestricted_without_policy(self):
        # No BERSERK_LOCAL_ONLY and no allowlist configured -> destination
        # shape is not restricted here (preserves the existing supported
        # case of an operator-chosen remote endpoint).
        _http.validate_egress_destination("https://example.com/x", label="x")

    def test_validate_egress_destination_fails_closed_on_malformed_url_instead_of_raising_valueerror(self):
        # Codex round-8 finding: several callers (doctor's reachability
        # checks, the startup egress-policy summary) call this directly on
        # operator-supplied configuration without first routing through
        # validate_http_url()'s own parse step. A malformed URL used to let
        # ValueError escape unhandled from urlsplit()/.hostname -- which, at
        # the startup-log call site, aborted server startup outright over
        # what is, for every affected caller, an optional integration.
        # Applies even with no policy active (the parse happens before the
        # policy-active check), so this is checked with defaults.
        with self.assertRaisesRegex(_http.UrlPolicyError, "malformed"):
            _http.validate_egress_destination("http://[::1/malformed", label="x")

    def test_validate_egress_destination_fails_closed_on_non_string_url(self):
        # Codex round-10 finding: a hand-edited llm_config.json can hold a
        # non-string value for "hermes_url" (an int, dict, or list survive
        # JSON parsing fine, and _hermes_url()'s `or` chain returns one if
        # it's truthy). That used to reach urlsplit() as a non-string
        # argument, raising AttributeError/TypeError -- not ValueError, so
        # not caught by the round-9 fix above -- crashing the same startup
        # call site all over again with a different exception type.
        for bad in (123, {}, [], None, "", "   "):
            with self.assertRaisesRegex(_http.UrlPolicyError, "non-empty string"):
                _http.validate_egress_destination(bad, label="x")

    def test_validate_egress_destination_blocks_unlisted_remote_host_under_local_only(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
            _http.validate_egress_destination("https://example.com/x", label="hermes endpoint")

    def test_validate_egress_destination_allows_listed_host_under_local_only(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        os.environ["BERSERK_EGRESS_ALLOWED_HOSTS"] = "example.com, other.example"
        _http.validate_egress_destination("https://example.com/x", label="x")

    def test_validate_egress_destination_allows_listed_cidr_under_local_only(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "10.0.0.0/8"
        _http.validate_egress_destination("https://10.1.2.3/x", label="x")

    def test_validate_egress_destination_rejects_ip_outside_cidr_under_local_only(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "10.0.0.0/8"
        with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
            _http.validate_egress_destination("https://192.168.1.5/x", label="x")

    def test_validate_egress_destination_configuring_allowlist_alone_activates_policy(self):
        # An allowlist opts into the policy even without local-only mode.
        os.environ["BERSERK_EGRESS_ALLOWED_HOSTS"] = "other.example"
        with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
            _http.validate_egress_destination("https://example.com/x", label="x")

    def test_request_json_and_post_bytes_status_enforce_egress_policy_before_connecting(self):
        # Round-8 adversarial-review finding: evals/run_eval.py called
        # _http.request_json() directly, and request_json()/post_bytes_status()
        # only validated URL shape (validate_http_url), never the
        # approved-destination policy (validate_egress_destination) -- so
        # BERSERK_LOCAL_ONLY=1 with an inherited cloud API key could still
        # reach a cloud provider through any caller that skipped the
        # separate, easy-to-forget validate_egress_destination() call. The
        # policy is now enforced inside the shared primitives themselves,
        # so every caller (existing and future) gets it for free, and a
        # rejected destination never opens a connection.
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        with mock.patch.object(_http.NO_REDIRECT_OPENER, "open") as mock_open:
            with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
                _http.request_json(
                    "https://api.openai.com/v1/chat/completions",
                    {"Authorization": "Bearer sk-test"},
                    {"x": 1},
                    label="eval backend endpoint",
                )
            mock_open.assert_not_called()
        with mock.patch.object(_http.NO_REDIRECT_OPENER, "open") as mock_open:
            with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
                _http.post_bytes_status(
                    "https://api.openai.com/v1/x", {}, b"{}",
                    label="eval backend endpoint",
                )
            mock_open.assert_not_called()

    def test_eval_backend_call_is_blocked_under_local_only_with_inherited_cloud_key(self):
        # Exercises the real eval-harness call path (evals/run_eval.py's
        # own _post()), not just the shared primitive directly -- proves
        # the fix actually closes the reported gap end to end, for the
        # exact scenario the finding described.
        eval_module = _load_eval_module()
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        orig_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-test-inherited"
        try:
            with mock.patch.object(_http.NO_REDIRECT_OPENER, "open") as mock_open:
                with self.assertRaisesRegex(_http.UrlPolicyError, "not loopback"):
                    eval_module._post(
                        "https://api.openai.com/v1/chat/completions",
                        {"Authorization": "Bearer sk-test-inherited"},
                        {"x": 1},
                    )
                mock_open.assert_not_called()
        finally:
            if orig_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = orig_key

    def test_eval_backend_call_against_loopback_still_works_under_local_only(self):
        # Local-only must not break the eval harness's normal, supported
        # use -- pointing it at a local backend -- only unapproved remote
        # destinations are rejected.
        eval_module = _load_eval_module()
        received = []

        class Target(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append(True)
                self.send_response(200)
                body = b'{"ok": true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Target)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        try:
            data, dt = eval_module._post(f"http://127.0.0.1:{port}/", {}, {"x": 1})
            self.assertEqual(data, {"ok": True})
            self.assertEqual(len(received), 1)
        finally:
            server.shutdown()
            server.server_close()


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
