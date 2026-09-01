#!/usr/bin/env python3
"""Tests for evals/fingerprint.py (provider-update detection, issue #90)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fingerprint  # noqa: E402
import _http  # noqa: E402
import parser_factory as pf  # noqa: E402


PAYLOAD = {"data": [
    {"id": "deepseek/deepseek-v4-flash", "context_length": 128000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000002"}},
    {"id": "other/model", "context_length": 8192, "pricing": {}},
]}


class MetadataFingerprintTest(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        """Otherwise every poll looks like a provider update."""
        reordered = {"data": [
            {"pricing": {"completion": "0.0000002", "prompt": "0.0000001"},
             "context_length": 128000, "id": "deepseek/deepseek-v4-flash"},
            {"id": "other/model", "context_length": 8192, "pricing": {}},
        ]}
        self.assertEqual(
            fingerprint.metadata_fingerprint(PAYLOAD, "deepseek/deepseek-v4-flash"),
            fingerprint.metadata_fingerprint(reordered, "deepseek/deepseek-v4-flash"),
        )

    def test_unrelated_model_changing_does_not_change_the_hash(self):
        changed = {"data": [PAYLOAD["data"][0], {"id": "other/model", "context_length": 4096, "pricing": {}}]}
        self.assertEqual(
            fingerprint.metadata_fingerprint(PAYLOAD, "deepseek/deepseek-v4-flash"),
            fingerprint.metadata_fingerprint(changed, "deepseek/deepseek-v4-flash"),
        )

    def test_price_change_changes_the_hash(self):
        changed = {"data": [
            {"id": "deepseek/deepseek-v4-flash", "context_length": 128000,
             "pricing": {"prompt": "0.0000009", "completion": "0.0000002"}},
        ]}
        self.assertNotEqual(
            fingerprint.metadata_fingerprint(PAYLOAD, "deepseek/deepseek-v4-flash"),
            fingerprint.metadata_fingerprint(changed, "deepseek/deepseek-v4-flash"),
        )

    def test_absent_model_returns_none(self):
        self.assertIsNone(fingerprint.metadata_fingerprint(PAYLOAD, "nope/nope"))


class BehavioralFingerprintTest(unittest.TestCase):
    def test_same_completions_same_hash(self):
        self.assertEqual(
            fingerprint.behavioral_fingerprint(["a", "b"]),
            fingerprint.behavioral_fingerprint(["a", "b"]),
        )

    def test_whitespace_only_difference_is_normalized_away(self):
        self.assertEqual(
            fingerprint.behavioral_fingerprint(["hello  world"]),
            fingerprint.behavioral_fingerprint([" hello world "]),
        )

    def test_different_completions_differ(self):
        self.assertNotEqual(
            fingerprint.behavioral_fingerprint(["a"]),
            fingerprint.behavioral_fingerprint(["b"]),
        )


class FetchModelsTest(unittest.TestCase):
    def setUp(self):
        self._orig_get = _http.http_get_json
        self._get_calls = []

    def tearDown(self):
        _http.http_get_json = self._orig_get

    def test_fetch_models_success(self):
        """Successfully fetch and return models payload."""
        expected_payload = {"data": [{"id": "test-model"}]}

        def fake_get(url, headers, timeout=120):
            self._get_calls.append((url, headers))
            return expected_payload, None

        _http.http_get_json = fake_get
        payload, err = fingerprint.fetch_models("https://example.com/api/v1/chat/completions", "key-123")
        self.assertIsNone(err)
        self.assertEqual(payload, expected_payload)
        self.assertEqual(len(self._get_calls), 1)
        self.assertEqual(self._get_calls[0][0], "https://example.com/api/v1/models")

    def test_fetch_models_with_bearer_token(self):
        """Include API key in Authorization header."""
        def fake_get(url, headers, timeout=120):
            self._get_calls.append((url, headers))
            return {"data": []}, None

        _http.http_get_json = fake_get
        fingerprint.fetch_models("https://example.com/api/v1/chat/completions", "key-xyz")
        self.assertEqual(self._get_calls[0][1], {"Authorization": "Bearer key-xyz"})

    def test_fetch_models_no_api_key(self):
        """Omit Authorization header when no API key provided."""
        def fake_get(url, headers, timeout=120):
            self._get_calls.append((url, headers))
            return {"data": []}, None

        _http.http_get_json = fake_get
        fingerprint.fetch_models("https://example.com/api/v1/chat/completions", "")
        self.assertEqual(self._get_calls[0][1], {})

    def test_fetch_models_invalid_url(self):
        """Return error when URL doesn't end with /chat/completions."""
        payload, err = fingerprint.fetch_models("https://example.com/invalid", "key")
        self.assertIsNone(payload)
        self.assertIn("cannot derive /models", err)

    def test_fetch_models_http_error(self):
        """Return error when HTTP request fails."""
        def fake_get(url, headers, timeout=120):
            return None, "HTTP 401"

        _http.http_get_json = fake_get
        payload, err = fingerprint.fetch_models("https://example.com/api/v1/chat/completions", "bad-key")
        self.assertIsNone(payload)
        self.assertIn("failed to fetch", err)
        self.assertIn("HTTP 401", err)


if __name__ == "__main__":
    unittest.main()
