"""Outbound egress policy, connect-time address checks, and related gates.

Ported from the unmerged feat/local-only-egress-hardening branch onto the
current code, with the DNS pinning changed to check resolved addresses.
"""

import json
import os
import socket
import sys
import tempfile
import subprocess
import ssl
import shutil
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _http  # noqa: E402

POLICY_VARS = ("BERSERK_LOCAL_ONLY", "BERSERK_EGRESS_ALLOWED_HOSTS", "BERSERK_EGRESS_ALLOWED_CIDRS")


def _env(**values):
    """Patch the policy variables: unset unless given."""
    patch = mock.patch.dict(os.environ)
    patch.start()
    for name in POLICY_VARS:
        os.environ.pop(name, None)
    os.environ.update(values)
    return patch


def _addrinfo(*ips):
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 80, 0, 0) if family == socket.AF_INET6 else (ip, 80)
        out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return out


class EgressDecisionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_env().stop)

    # Covers SECURITY.md#outbound-http
    def test_inactive_policy_allows_any_destination(self):
        _http.validate_egress_destination("https://api.example.com/v1")
        self.assertFalse(_http.egress_policy_active())

    def test_local_only_refuses_remote_and_allows_loopback(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        with self.assertRaisesRegex(_http.UrlPolicyError, "BERSERK_LOCAL_ONLY"):
            _http.validate_egress_destination("https://api.openai.com/v1")
        for url in ("http://localhost:3000/x", "http://127.0.0.1/x", "http://[::1]:1/x", "http://localhost.:1/x"):
            with self.subTest(url=url):
                _http.validate_egress_destination(url)

    def test_allowed_host_by_name_and_ip_by_network(self):
        os.environ["BERSERK_EGRESS_ALLOWED_HOSTS"] = "hermes.internal.example"
        _http.validate_egress_destination("https://hermes.internal.example/api")
        _http.validate_egress_destination("https://HERMES.internal.example./api")
        with self.assertRaises(_http.UrlPolicyError):
            _http.validate_egress_destination("https://evil.example/api")
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "10.0.0.0/8"
        _http.validate_egress_destination("http://10.1.2.3/x")
        with self.assertRaises(_http.UrlPolicyError):
            _http.validate_egress_destination("http://192.0.2.1/x")
        # A hostname is deferred to the connect-time address check when networks exist.
        _http.validate_egress_destination("https://other.example/api")

    def test_local_only_refuses_cloud_llm_hosts_even_if_allowlisted(self):
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        os.environ["BERSERK_EGRESS_ALLOWED_HOSTS"] = "api.openai.com,api.anthropic.com,openrouter.ai,eu.openrouter.ai"
        for url in (
            "https://api.openai.com/v1",
            "https://api.anthropic.com/v1",
            "https://openrouter.ai/api/v1",
            "https://eu.openrouter.ai/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(_http.UrlPolicyError, "cloud LLM API"):
                    _http.validate_egress_destination(url)
        del os.environ["BERSERK_LOCAL_ONLY"]
        _http.validate_egress_destination("https://api.openai.com/v1")  # an allowlist alone permits it

    def test_bad_policy_configuration_fails_closed(self):
        for cidrs in ("not-a-cidr", "0.0.0.0/0", "::/0"):
            with self.subTest(cidrs=cidrs):
                os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = cidrs
                with self.assertRaises(_http.UrlPolicyError):
                    _http.validate_egress_destination("https://api.example.com/v1")

    def test_malformed_urls_are_refused(self):
        for url in ("", None, 5, "http://[::1/x"):
            with self.subTest(url=url):
                with self.assertRaises(_http.UrlPolicyError):
                    _http.validate_egress_destination(url)

    def test_policy_ignores_ambient_proxies(self):
        self.assertIs(_http._opener_for("https://api.example.com/"), _http.NO_REDIRECT_OPENER)
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        self.assertIs(_http._opener_for("https://api.example.com/"), _http.LOOPBACK_OPENER)
        proxy_handlers = [h for h in _http.LOOPBACK_OPENER.handlers if isinstance(h, _http.urllib.request.ProxyHandler)]
        self.assertTrue(all(not h.proxies for h in proxy_handlers))


class ConnectTimeCheckTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_env().stop)

    def test_loopback_name_must_resolve_to_loopback(self):
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("203.0.113.9")):
            with self.assertRaisesRegex(_http.EgressRefused, "non-loopback"):
                _http._checked_candidates("localhost", 80)
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1", "::1")):
            self.assertEqual(len(_http._checked_candidates("localhost", 80)), 2)

    def test_hostname_must_resolve_inside_allowed_networks(self):
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "10.0.0.0/8"
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("10.0.0.5", "203.0.113.9")):
            kept = _http._checked_candidates("svc.example", 80)
        self.assertEqual([c[4][0] for c in kept], ["10.0.0.5"])
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("203.0.113.9")):
            with self.assertRaisesRegex(_http.EgressRefused, "outside"):
                _http._checked_candidates("svc.example", 80)

    def test_host_approved_by_name_keeps_all_addresses(self):
        os.environ["BERSERK_EGRESS_ALLOWED_HOSTS"] = "svc.example"
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("203.0.113.9")):
            self.assertEqual(len(_http._checked_candidates("svc.example", 80)), 1)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PinnedConnectionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_env().stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_request_resolves_once_and_keeps_the_host_header(self):
        real = socket.getaddrinfo
        calls = []

        def counting(host, *args, **kwargs):
            calls.append(host)
            return [c for c in real(host, *args, **kwargs) if c[0] == socket.AF_INET]

        with mock.patch.object(_http.socket, "getaddrinfo", side_effect=counting):
            out, err = _http.http_get_json(f"http://localhost:{self.port}/", {}, timeout=5)
        self.assertIsNone(err)
        self.assertEqual(out["host"], f"localhost:{self.port}")
        self.assertEqual(calls, ["localhost"])

    def test_loopback_name_resolving_elsewhere_is_refused_as_policy(self):
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=_addrinfo("203.0.113.9")):
            out, err = _http.http_get_json(f"http://localhost:{self.port}/", {}, timeout=5)
        self.assertIsNone(out)
        self.assertIn("invalid endpoint", err)
        self.assertIn("non-loopback", err)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class PinnedHttpsTest(unittest.TestCase):
    """Pinning must not weaken TLS: the certificate and SNI are still checked
    against the original hostname, not the pinned address."""

    def setUp(self):
        self.addCleanup(_env().stop)
        self.tmp = tempfile.mkdtemp()
        self.cert, self.key = Path(self.tmp) / "c.pem", Path(self.tmp) / "k.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-passout",
                "pass:test-only",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
            ],
            check=True,
            capture_output=True,
        )
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.cert, self.key, password="test-only")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.socket = server_ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _get(self, host):
        ctx = ssl.create_default_context(cafile=str(self.cert))
        ipv4_only = [c for c in socket.getaddrinfo("127.0.0.1", self.port, 0, socket.SOCK_STREAM)]
        with mock.patch.object(_http.socket, "getaddrinfo", return_value=ipv4_only):
            conn = _http._PinnedHTTPSConnection(host, self.port, context=ctx, timeout=5)
            try:
                conn.request("GET", "/")
                return json.loads(conn.getresponse().read())
            finally:
                conn.close()

    def test_certificate_is_verified_against_the_original_hostname(self):
        self.assertEqual(self._get("localhost")["host"], f"localhost:{self.port}")

    def test_a_name_the_certificate_does_not_cover_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self._get("127.0.0.1")


class LocalOnlyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_env(BERSERK_LOCAL_ONLY="1").stop)

    def test_cloud_llm_providers_are_refused_even_with_keys(self):
        import parser_factory

        with (
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}),
            mock.patch.object(parser_factory, "_http_post_json", side_effect=AssertionError("no request may be sent")),
        ):
            for provider in ("openai", "anthropic"):
                with self.subTest(provider=provider):
                    text, err = parser_factory.llm_complete(provider, "s", "u")
                    self.assertIsNone(text)
                    self.assertIn("BERSERK_LOCAL_ONLY", err)

    def test_quota_endpoint_is_not_called(self):
        import quota_status

        opener = mock.Mock(side_effect=AssertionError("no request may be sent"))
        self.assertIsNone(quota_status._fetch_live_usage("token", opener=opener))
        opener.assert_not_called()


class DoctorAndGateTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_env().stop)
        import berserk_mcp

        self.bm = berserk_mcp

    def test_doctor_reports_the_policy(self):
        self.assertIn("inactive", self.bm._doctor_check_egress_policy()["detail"])
        os.environ["BERSERK_LOCAL_ONLY"] = "1"
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "10.0.0.0/8"
        detail = self.bm._doctor_check_egress_policy()["detail"]
        self.assertIn("BERSERK_LOCAL_ONLY", detail)
        self.assertIn("10.0.0.0/8", detail)
        os.environ["BERSERK_EGRESS_ALLOWED_CIDRS"] = "bogus"
        self.assertEqual(self.bm._doctor_check_egress_policy()["status"], "fail")

    def test_stuck_probes_are_bounded(self):
        # A fresh limit: probes left running by other tests hold the shared one.
        fresh = threading.BoundedSemaphore(2)
        patch = mock.patch.object(self.bm, "_DOCTOR_PROBE_SEMAPHORE", fresh)
        patch.start()
        self.addCleanup(patch.stop)
        release = threading.Event()
        started = []

        def stuck():
            started.append(1)
            release.wait(10)
            return "late"

        try:
            for _ in range(2):
                self.assertIsNone(self.bm._with_wall_clock_timeout(stuck, 0.05))
            self.assertIsNone(self.bm._with_wall_clock_timeout(stuck, 0.05))
            self.assertEqual(len(started), 2)  # the third probe never started a thread
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.bm._with_wall_clock_timeout(lambda: "ok", 0.5) != "ok":
            time.sleep(0.05)
        self.assertEqual(self.bm._with_wall_clock_timeout(lambda: "ok", 1), "ok")

    def test_save_query_management_token(self):
        args = {"name": "q", "description": "d", "kql": self.bm.TABLE + " | take 1"}
        with mock.patch.dict(os.environ, {"BERSERK_MCP_MGMT_TOKEN": "s3cret"}):
            for supplied in (None, "", "wrong"):
                with self.subTest(supplied=supplied):
                    call = dict(args, mgmt_token=supplied) if supplied is not None else dict(args)
                    text, is_err = self.bm._handle_learning_loop("save_query", call)
                    self.assertTrue(is_err)
                    self.assertIn("management token", text)
        tool = next(t for t in self.bm.TOOLS + self.bm.MGMT_TOOLS if t["name"] == "save_query")
        self.assertIsNone(self.bm._unknown_argument_error(tool, dict(args, mgmt_token="s3cret")))


if __name__ == "__main__":
    unittest.main()
