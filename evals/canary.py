#!/usr/bin/env python3
"""Daily scored canary for model-behavior monitoring (issue #89).

Runs the existing router eval harness against a FROZEN case set and emits
the scores into Berserk, so a later verdict pass can tell whether a model
still behaves the way it did when it was chosen.

Why a separate frozen case set: evals/router_cases.jsonl is expected to grow
(issue #13 Phase 2 adds targeted phrasings). If the canary read that file, a
score drop would conflate "we added harder cases" with "the model got worse"
and the signal would be uninterpretable. canary_cases.jsonl is frozen, and
its version is the hash of its own contents -- editing it automatically
changes the version, which automatically stops cross-version comparison.

This module runs run_eval.py as a subprocess and reads its saved JSON rather
than importing it, mirroring evals/ci_gate.py deliberately: run_eval.py is
the actively-growing harness, and this consumer should not be entangled with
its internals.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_CASES = HERE / "canary_cases.jsonl"
RESULTS_DIR = HERE / "results"

EVAL_SERVICE_NAME = "berserk-mcp-eval"
EVAL_SCOPE_NAME = "berserk-mcp.canary"

EVAL_ATTRIBUTE_ALLOWLIST = frozenset({
    "eval.model", "eval.backend", "eval.case_set_version",
    "eval.tool_accuracy", "eval.arg_accuracy", "eval.repeats",
    "eval.total_cost_usd", "eval.run_id", "eval.status", "eval.error",
    "eval.behavioral_fingerprint", "eval.provider_metadata_fingerprint",
    "eval.role", "eval.discovery_mode",
})


def _hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()[:12]


def case_set_version(path=DEFAULT_CASES):
    """Version is the content hash, so it can never be forgotten on edit."""
    return _hash_bytes(Path(path).read_bytes())


def _current_environment():
    """BERSERK_MCP_ROLE and BERSERK_MCP_DISCOVERY, read the same way
    berserk_mcp.py itself reads them (berserk_mcp.py:144, :2408-2410).

    run_eval.py spawns berserk_mcp.py as a subprocess with no env=
    override, so it inherits whatever role/discovery mode the calling
    shell has set -- the same tool schema restriction a real deployment
    applies. A canary run under BERSERK_MCP_ROLE=claude sees only the
    claude-lane tools; canary_cases.jsonl was written against the full
    unrestricted schema. Without recording this, a role or discovery-mode
    change looks identical to a model regression under the same
    case_set_version (found by Codex review, 2026-09-02)."""
    role = os.environ.get("BERSERK_MCP_ROLE", "all").strip().lower() or "all"
    discovery = os.environ.get("BERSERK_MCP_DISCOVERY", "").strip().lower() in \
        {"1", "true", "yes", "on"}
    return role, ("1" if discovery else "0")


def build_eval_record(report, version, run_id, started_ns):
    role, discovery_mode = _current_environment()
    record = {
        "eval.model": report.get("model", ""),
        "eval.backend": report.get("backend", ""),
        "eval.case_set_version": version,
        "eval.role": role,
        "eval.discovery_mode": discovery_mode,
        "eval.tool_accuracy": float(report["tool_accuracy"]),
        "eval.arg_accuracy": float(report["arg_accuracy"]),
        "eval.repeats": int(report.get("repeats", 1)),
        "eval.run_id": run_id,
        "eval.status": "ok",
    }
    cost = report.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        record["eval.total_cost_usd"] = float(cost)
    return record


def build_failure_record(model, backend, version, run_id, started_ns, error):
    """A failed run is recorded as a failure, never as a score of zero --
    otherwise a provider outage looks like a catastrophic quality drop."""
    role, discovery_mode = _current_environment()
    return {
        "eval.model": model,
        "eval.backend": backend,
        "eval.case_set_version": version,
        "eval.role": role,
        "eval.discovery_mode": discovery_mode,
        "eval.run_id": run_id,
        "eval.status": "failed",
        "eval.error": str(error)[:500],
    }


def _run_harness(model, backend, cases_path, repeats, base_url=None, key_env=None,
                 tool_choice=None, timeout=900):
    """Invoke run_eval.py and return its saved report. Mirrors ci_gate.py."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    before = set(RESULTS_DIR.glob("*.json"))
    cmd = [sys.executable, str(HERE / "run_eval.py"),
           "--backend", backend, "--model", model, "--repeats", str(repeats)]
    if base_url:
        cmd += ["--base-url", base_url]
    if key_env:
        cmd += ["--key-env", key_env]
    if tool_choice:
        cmd += ["--tool-choice", tool_choice]
    cmd.append(str(cases_path))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"run_eval.py exited {proc.returncode}: {proc.stderr[:500]}")
    new_files = set(RESULTS_DIR.glob("*.json")) - before
    if not new_files:
        raise RuntimeError("run_eval.py produced no new results file")
    newest = max(new_files, key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text(encoding="utf-8"))


def run_canary(model, backend="openai", cases_path=DEFAULT_CASES, repeats=3,
               base_url=None, key_env=None, tool_choice=None):
    """base_url/key_env default to this project's own configured Hermes
    provider (BERSERK_LLM_HERMES_URL / HERMES_API_KEY) when the backend is
    "openai" and neither is given explicitly -- the same provider
    parser_factory's generate_parser already uses. Without this,
    run_eval.py's own default ("openai" backend -> api.openai.com,
    OPENAI_API_KEY) would silently target the wrong provider with the
    wrong key for any non-OpenAI model ID like "vendor/model" (found while
    running the first real calibration against this code, 2026-09-01).

    tool_choice defaults to "auto" for the same reason: run_eval.py itself
    defaults "openai" backend to tool_choice="required", which silently
    disables DeepSeek's prompt caching -- the entire reason
    deepseek-v4-flash costs what it does. "auto" matches how models are
    actually used in production (see README's Choosing a model section)."""
    if backend == "openai":
        if base_url is None:
            sys.path.insert(0, str(REPO_ROOT))
            import parser_factory
            hermes_url = parser_factory._hermes_url()
            suffix = "/chat/completions"
            if hermes_url and hermes_url.endswith(suffix):
                base_url = hermes_url[: -len(suffix)]
        if key_env is None:
            key_env = "HERMES_API_KEY"
        if tool_choice is None:
            tool_choice = "auto"
    version = case_set_version(cases_path)
    run_id = uuid.uuid4().hex[:16]
    started_ns = int(time.time() * 1_000_000_000)
    try:
        report = _run_harness(model, backend, cases_path, repeats,
                              base_url=base_url, key_env=key_env, tool_choice=tool_choice)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return build_failure_record(model, backend, version, run_id, started_ns, exc)
    return build_eval_record(report, version, run_id, started_ns)


def emit(records, started_ns):
    """Emit via ai_finops's OTLP path, passing our own allowlist -- without it
    every eval.* attribute is silently dropped (see Task 1)."""
    sys.path.insert(0, str(REPO_ROOT))
    import ai_finops
    return ai_finops.emit_otlp_records(
        records, EVAL_SERVICE_NAME,
        scope_name=EVAL_SCOPE_NAME,
        timestamp_ns=started_ns,
        allowed_keys=EVAL_ATTRIBUTE_ALLOWLIST,
    )
