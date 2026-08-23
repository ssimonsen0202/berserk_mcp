import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_analytics as aa  # noqa: E402
import quota_status as qs  # noqa: E402


def fake_completed_process(returncode, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class ReadOauthTokenTest(unittest.TestCase):
    def test_returns_none_on_nonzero_returncode(self):
        run = lambda *a, **k: fake_completed_process(1, "")
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_on_empty_stdout(self):
        run = lambda *a, **k: fake_completed_process(0, "")
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_on_malformed_json(self):
        run = lambda *a, **k: fake_completed_process(0, "not json")
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_when_claudeAiOauth_key_missing(self):
        run = lambda *a, **k: fake_completed_process(0, json.dumps({"other": "shape"}))
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_when_claudeAiOauth_is_not_a_dict(self):
        run = lambda *a, **k: fake_completed_process(0, json.dumps({"claudeAiOauth": "nope"}))
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_when_accessToken_missing_or_empty(self):
        run = lambda *a, **k: fake_completed_process(
            0, json.dumps({"claudeAiOauth": {"subscriptionType": "max"}}))
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

        run2 = lambda *a, **k: fake_completed_process(
            0, json.dumps({"claudeAiOauth": {"accessToken": ""}}))
        self.assertIsNone(qs._read_oauth_token(run=run2, platform_name="Darwin"))

    def test_returns_token_on_well_formed_blob(self):
        run = lambda *a, **k: fake_completed_process(
            0, json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-fake", "subscriptionType": "max"}}))
        self.assertEqual(qs._read_oauth_token(run=run, platform_name="Darwin"), "sk-ant-oat-fake")

    def test_skips_entirely_on_non_macos(self):
        # The `security` CLI doesn't exist outside macOS -- never even try.
        calls = []
        def run(*a, **k):
            calls.append(a)
            return fake_completed_process(0, json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Linux"))
        self.assertEqual(calls, [])

    def test_returns_none_on_subprocess_timeout(self):
        def run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="security", timeout=5)
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))

    def test_returns_none_on_missing_security_binary(self):
        def run(*a, **k):
            raise FileNotFoundError("no such file")
        self.assertIsNone(qs._read_oauth_token(run=run, platform_name="Darwin"))


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchLiveUsageTest(unittest.TestCase):
    def test_returns_parsed_dict_on_200(self):
        opener = lambda req, timeout: _FakeResponse(200, json.dumps({"a": 1}).encode())
        self.assertEqual(qs._fetch_live_usage("tok", opener=opener), {"a": 1})

    def test_returns_none_on_non_200_status(self):
        opener = lambda req, timeout: _FakeResponse(403, b"{}")
        self.assertIsNone(qs._fetch_live_usage("tok", opener=opener))

    def test_returns_none_on_malformed_json_body(self):
        opener = lambda req, timeout: _FakeResponse(200, b"not json")
        self.assertIsNone(qs._fetch_live_usage("tok", opener=opener))

    def test_returns_none_when_body_is_a_json_array_not_object(self):
        opener = lambda req, timeout: _FakeResponse(200, b"[1,2,3]")
        self.assertIsNone(qs._fetch_live_usage("tok", opener=opener))

    def test_returns_none_on_connection_error(self):
        def opener(req, timeout):
            raise OSError("connection refused")
        self.assertIsNone(qs._fetch_live_usage("tok", opener=opener))

    def test_never_raises_on_completely_unexpected_exception_shape(self):
        # Defensive: the live endpoint is undocumented and unverified: an
        # unexpected error type here must still degrade to None, not crash
        # the whole quota-status call.
        def opener(req, timeout):
            raise ValueError("surprise")
        self.assertIsNone(qs._fetch_live_usage("tok", opener=opener))


class GetQuotaStatusTest(unittest.TestCase):
    def test_uses_live_path_when_token_and_endpoint_both_succeed(self):
        run = lambda *a, **k: fake_completed_process(
            0, json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        opener = lambda req, timeout: _FakeResponse(200, json.dumps({"five_hour_utilization": 42}).encode())
        result = qs.get_quota_status(run=run, opener=opener, platform_name="Darwin")
        self.assertEqual(result["source"], "live")
        self.assertTrue(result["ok"])
        self.assertEqual(result["five_hour_utilization"], 42)

    def test_falls_back_to_estimate_when_no_token(self):
        run = lambda *a, **k: fake_completed_process(1, "")  # keychain miss
        result = qs.get_quota_status(
            run=run, platform_name="Darwin",
            _total_tokens_estimate=lambda since: (500, True, False),
        )
        self.assertEqual(result["source"], "estimated")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_tokens"], 500)
        self.assertTrue(result["all_exact"])

    def test_falls_back_to_estimate_when_live_endpoint_fails(self):
        run = lambda *a, **k: fake_completed_process(
            0, json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        opener = lambda req, timeout: _FakeResponse(500, b"{}")
        result = qs.get_quota_status(
            run=run, opener=opener, platform_name="Darwin",
            _total_tokens_estimate=lambda since: (300, False, False),
        )
        self.assertEqual(result["source"], "estimated")
        self.assertEqual(result["total_tokens"], 300)
        self.assertFalse(result["all_exact"])

    def test_reports_unavailable_when_both_paths_fail(self):
        run = lambda *a, **k: fake_completed_process(1, "")
        result = qs.get_quota_status(
            run=run, platform_name="Darwin",
            _total_tokens_estimate=lambda since: (None, False, True),
        )
        self.assertEqual(result["source"], "unavailable")
        self.assertFalse(result["ok"])

    def test_never_requires_a_daemon_or_forwarder_running(self):
        # Sanity: get_quota_status must not import/touch anything that
        # implies a running background process -- it only calls run/opener
        # and (on fallback) the injected estimator.
        run = lambda *a, **k: fake_completed_process(1, "")
        result = qs.get_quota_status(
            run=run, platform_name="Darwin",
            _total_tokens_estimate=lambda since: (0, True, False),
        )
        self.assertIn(result["source"], {"estimated", "unavailable"})


class QuotaStatusToolIntegrationTest(unittest.TestCase):
    """Confirms total_tokens_estimate really is what the fallback wires to
    by default (no injected estimator) -- catches drift between the two
    modules without re-mocking agent_analytics internals here."""

    def test_default_estimator_is_agent_analytics_total_tokens_estimate(self):
        import inspect
        sig = inspect.signature(qs.get_quota_status)
        default = sig.parameters["_total_tokens_estimate"].default
        self.assertIs(default, aa.total_tokens_estimate)


if __name__ == "__main__":
    unittest.main()
