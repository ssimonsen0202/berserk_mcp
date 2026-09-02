import json, os, sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
import canary  # noqa: E402
# parser_factory lives at the repo root, not in evals/. canary.py only adds
# the repo root to sys.path lazily, inside run_canary() itself -- add it
# here too so mock.patch("parser_factory...") can resolve the module before
# run_canary() ever executes.
sys.path.insert(0, str(canary.REPO_ROOT))


class CaseSetVersionTest(unittest.TestCase):
    def test_same_content_same_version(self):
        a = canary._hash_bytes(b'{"id": "x"}\n')
        b = canary._hash_bytes(b'{"id": "x"}\n')
        self.assertEqual(a, b)

    def test_changed_content_changes_version(self):
        a = canary._hash_bytes(b'{"id": "x"}\n')
        b = canary._hash_bytes(b'{"id": "y"}\n')
        self.assertNotEqual(a, b)

    def test_version_is_short_and_stable_length(self):
        self.assertEqual(len(canary._hash_bytes(b"anything")), 12)


class BuildEvalRecordTest(unittest.TestCase):
    REPORT = {
        "backend": "openai", "model": "deepseek/deepseek-v4-flash", "repeats": 3,
        "tool_accuracy": 0.875, "arg_accuracy": 0.9,
        "total_cost_usd": 0.00205, "total_input_tokens": 115717,
        "total_output_tokens": 381, "rows": [],
    }

    def test_maps_harness_fields_onto_eval_attributes(self):
        rec = canary.build_eval_record(self.REPORT, "abc123def456", "run-1", 1234567890000000000)
        self.assertEqual(rec["eval.model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(rec["eval.backend"], "openai")
        self.assertEqual(rec["eval.tool_accuracy"], 0.875)
        self.assertEqual(rec["eval.arg_accuracy"], 0.9)
        self.assertEqual(rec["eval.repeats"], 3)
        self.assertEqual(rec["eval.case_set_version"], "abc123def456")
        self.assertEqual(rec["eval.run_id"], "run-1")
        self.assertEqual(rec["eval.status"], "ok")

    def test_every_produced_key_is_in_the_allowlist(self):
        """A key outside the allowlist would be silently dropped at emit time."""
        rec = canary.build_eval_record(self.REPORT, "v", "r", 1)
        for key in rec:
            self.assertIn(key, canary.EVAL_ATTRIBUTE_ALLOWLIST)

    def test_failure_record_has_no_score_fields(self):
        """An outage must never be stored as a score of zero."""
        rec = canary.build_failure_record(
            "deepseek/deepseek-v4-flash", "openai", "v", "r", 1, "connection refused")
        self.assertEqual(rec["eval.status"], "failed")
        self.assertNotIn("eval.tool_accuracy", rec)
        self.assertNotIn("eval.arg_accuracy", rec)


class RunCanaryProviderRoutingTest(unittest.TestCase):
    """run_canary must not silently fall through to run_eval.py's own
    OpenAI-proper default (api.openai.com + OPENAI_API_KEY) -- that default
    cannot serve a non-OpenAI model ID and was never noticed until the
    first real calibration run against this code, 2026-09-01."""

    def test_default_routes_through_configured_hermes_provider(self):
        captured = {}

        def fake_run_harness(model, backend, cases_path, repeats, **kwargs):
            captured.update(kwargs)
            return {"backend": backend, "model": model, "repeats": repeats,
                    "tool_accuracy": 1.0, "arg_accuracy": 1.0, "rows": []}

        with mock.patch.object(canary, "_run_harness", fake_run_harness), \
             mock.patch("parser_factory._hermes_url",
                        return_value="https://openrouter.ai/api/v1/chat/completions"):
            canary.run_canary("deepseek/deepseek-v4-flash")

        self.assertEqual(captured["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(captured["key_env"], "HERMES_API_KEY")
        self.assertEqual(captured["tool_choice"], "auto")

    def test_explicit_arguments_are_never_overridden(self):
        captured = {}

        def fake_run_harness(model, backend, cases_path, repeats, **kwargs):
            captured.update(kwargs)
            return {"backend": backend, "model": model, "repeats": repeats,
                    "tool_accuracy": 1.0, "arg_accuracy": 1.0, "rows": []}

        with mock.patch.object(canary, "_run_harness", fake_run_harness):
            canary.run_canary("m", base_url="https://x.example/v1",
                              key_env="MY_KEY", tool_choice="required")

        self.assertEqual(captured["base_url"], "https://x.example/v1")
        self.assertEqual(captured["key_env"], "MY_KEY")
        self.assertEqual(captured["tool_choice"], "required")

    def test_non_openai_backend_is_not_routed_through_hermes(self):
        captured = {}

        def fake_run_harness(model, backend, cases_path, repeats, **kwargs):
            captured.update(kwargs)
            return {"backend": backend, "model": model, "repeats": repeats,
                    "tool_accuracy": 1.0, "arg_accuracy": 1.0, "rows": []}

        with mock.patch.object(canary, "_run_harness", fake_run_harness):
            canary.run_canary("claude-opus-4-8", backend="anthropic")

        self.assertIsNone(captured["base_url"])
        self.assertIsNone(captured["key_env"])
        self.assertIsNone(captured["tool_choice"])


class EmitTest(unittest.TestCase):
    def test_emit_returns_false_when_no_otlp_endpoint_configured(self):
        """When OTLP_EXPORTER_OTLP_ENDPOINT is not set, emit should return False gracefully."""
        rec = {"eval.status": "ok", "eval.run_id": "test-run"}
        result = canary.emit([rec], int(1234567890 * 1_000_000_000))
        # emit() should not crash even when there's no provider configured.
        # ai_finops.emit_otlp_records returns False when _otlp_endpoint is not set.
        self.assertFalse(result)


class EnvironmentAttributionTest(unittest.TestCase):
    """run_eval.py spawns berserk_mcp.py as a subprocess with no env=
    override, so it inherits whatever BERSERK_MCP_ROLE / BERSERK_MCP_DISCOVERY
    the calling shell has set -- the same tool-schema restriction a real
    deployment applies. Without recording this, a role or discovery-mode
    change looks identical to a model regression under the same
    case_set_version. Found by Codex review, 2026-09-02."""

    REPORT = BuildEvalRecordTest.REPORT

    def test_default_role_and_discovery_when_unset(self):
        for k in ("BERSERK_MCP_ROLE", "BERSERK_MCP_DISCOVERY"):
            os.environ.pop(k, None)
        rec = canary.build_eval_record(self.REPORT, "v", "r", 1)
        self.assertEqual(rec["eval.role"], "all")
        self.assertEqual(rec["eval.discovery_mode"], "0")

    def test_captures_configured_role_and_discovery(self):
        old_role = os.environ.get("BERSERK_MCP_ROLE")
        old_disc = os.environ.get("BERSERK_MCP_DISCOVERY")
        os.environ["BERSERK_MCP_ROLE"] = "claude"
        os.environ["BERSERK_MCP_DISCOVERY"] = "1"
        try:
            rec = canary.build_eval_record(self.REPORT, "v", "r", 1)
            self.assertEqual(rec["eval.role"], "claude")
            self.assertEqual(rec["eval.discovery_mode"], "1")
        finally:
            if old_role is None:
                os.environ.pop("BERSERK_MCP_ROLE", None)
            else:
                os.environ["BERSERK_MCP_ROLE"] = old_role
            if old_disc is None:
                os.environ.pop("BERSERK_MCP_DISCOVERY", None)
            else:
                os.environ["BERSERK_MCP_DISCOVERY"] = old_disc

    def test_failure_record_also_captures_environment(self):
        os.environ["BERSERK_MCP_ROLE"] = "sre"
        try:
            rec = canary.build_failure_record("m", "openai", "v", "r", 1, "boom")
            self.assertEqual(rec["eval.role"], "sre")
            self.assertIn("eval.discovery_mode", rec)
        finally:
            os.environ.pop("BERSERK_MCP_ROLE", None)

    def test_role_and_discovery_keys_are_in_the_allowlist(self):
        self.assertIn("eval.role", canary.EVAL_ATTRIBUTE_ALLOWLIST)
        self.assertIn("eval.discovery_mode", canary.EVAL_ATTRIBUTE_ALLOWLIST)
