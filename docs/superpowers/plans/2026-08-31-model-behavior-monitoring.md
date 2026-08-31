# Model-Behavior Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a model berserk-mcp depends on degrades, drifts, or is silently replaced by its provider — within one day, with no human running anything, and without firing on ordinary run-to-run noise.

**Architecture:** A daily canary runs the existing `evals/run_eval.py` harness against a frozen case set and emits scores as OTLP into Berserk under `service.name="berserk-mcp-eval"`. Two independent fingerprints (provider `/models` metadata, and temperature-0 behavioral hashing) detect provider-side changes. Pure verdict logic reads the stored series back, separates step changes from gradual drift using Berserk's native `series_fit_line`, and reports `stable` / `degrading` / `step-change` / `insufficient-data`. Two MCP tools and a headless CLI flag expose the result.

**Tech Stack:** Python 3.9+ standard library only. Existing modules: `evals/run_eval.py`, `ai_finops.py`, `agent_analytics.py`, `parser_factory.py`. Berserk KQL via the `bzrk` CLI. No third-party runtime dependencies.

**Spec:** [`docs/superpowers/specs/2026-08-30-model-behavior-monitoring-design.md`](../specs/2026-08-30-model-behavior-monitoring-design.md)

**Issues:** #89 (Tasks 1-2), #90 (Task 3), #91 (Task 4), #92 (Task 5)

## Global Constraints

Every task's requirements implicitly include these.

- **Standard library only.** No third-party runtime dependencies. This is a load-bearing property of the project (`pip install` pulls nothing beyond the package itself).
- **Python 3.9+ syntax floor.** No `match`, no `X | Y` type unions at runtime.
- **Never `shell=True`.** Subprocesses take an argument list.
- **Alert and record only.** No build gating, no automatic escalation-policy or routing changes. A human decides what to do about a verdict.
- **A failed canary run is recorded as a failure, never as a score of 0.** An outage must not masquerade as a catastrophic quality regression.
- **The behavioral fingerprint is reported as a signal, never as proof.** Output says "behavioral fingerprint changed", never "the provider swapped the model". Temperature 0 is not guaranteed deterministic across providers.
- **Thresholds are measured, not guessed.** Any numeric alert threshold ships as a named, overridable constant marked provisional until Task 2's baselining produces a real number.
- **Tests:** `python -m pytest tests/ -q` must pass. Unit tests never require a live provider, a running daemon, or network access.
- **The canary measures tool-routing quality specifically, not general model quality.** Every user-facing description must state this boundary.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `evals/canary_cases.jsonl` | Frozen case set. Seeded from `router_cases.jsonl`, then never edited casually. |
| `evals/canary.py` | Runs the scored canary: invoke `run_eval.py`, load its JSON, build eval records, emit to Berserk. |
| `evals/fingerprint.py` | Provider metadata fingerprint and behavioral fingerprint. Pure hashing + a thin fetch layer. |
| `model_drift.py` | Verdict logic. Pure functions over a score series, plus the KQL to read that series back. Top-level, matching `agent_analytics.py`'s precedent as an analytics module consumed by MCP tools. |
| `evals/test_canary.py` | Canary record building, case-set versioning, failure handling. |
| `evals/test_fingerprint.py` | Hash normalization and change detection. |
| `tests/test_model_drift.py` | Verdict classification, noise band, step-vs-drift separation. |

**Test placement follows the existing split, which is not arbitrary.** There is **no `evals/__init__.py`** — `evals/` is a directory of scripts, not a package, so `from evals import canary` raises `ModuleNotFoundError`. Tests for code *inside* `evals/` live *inside* `evals/` and import via `sys.path.insert(0, Path(__file__).resolve().parent)` then a bare `import canary` (see `evals/test_ci_gate.py:11-12`). Tests for top-level modules live in `tests/` and insert the repo root instead (see `tests/test_agent_analytics.py:8`). Do not add an `__init__.py` to make the import cleaner — that changes how every existing eval script resolves its own imports.

**Modified files:**

| Path | Change |
|---|---|
| `ai_finops.py` | Add three optional parameters to `emit_otlp_records()` and one to `_otlp_attributes()`. Defaults preserve today's behavior exactly. |
| `berserk_mcp.py` | Two `TOOLS` entries, two `TITLES` entries, two dispatcher branches, two CLI flags. |
| `tests/test_ai_finops.py` | Regression tests proving existing emission is unchanged. |

**Why `model_drift.py` is top-level and not under `evals/`:** it is imported by `berserk_mcp.py` at tool-dispatch time. `evals/` holds the offline harness; runtime analytics modules live at the top level next to `agent_analytics.py` and `ai_finops.py`.

---

## Task 1: Extend `emit_otlp_records` without changing existing behavior

**Files:**
- Modify: `ai_finops.py:1100-1167`
- Test: `tests/test_ai_finops.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ai_finops.emit_otlp_records(records, service_name, *, scope_name=..., timestamp_ns=None, allowed_keys=None) -> bool` and `ai_finops._otlp_attributes(record, allowed=None) -> list`. Task 2 calls both.

**Critical context — read before writing code.** `_otlp_attributes()` currently filters every record through a hardcoded `allowed` set. Any key not in that set is **silently dropped**. A caller emitting `eval.*` attributes today would get an HTTP 200 and records containing zero attributes. That allowlist is a deliberate privacy control for the FinOps path, so it must not be removed or widened — the fix is an optional parameter letting a caller supply its own allowlist.

Two further mismatches for the canary caller: `timeUnixNano` is hardcoded to *now* for every record (a re-send would stamp history as current), and the scope name is hardcoded to `berserk-mcp.ai-finops` (which would mislabel eval records).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ai_finops.py

class EmitOtlpRecordsOptionsTest(unittest.TestCase):
    def test_default_allowlist_unchanged(self):
        """Existing callers keep the FinOps allowlist and its filtering."""
        attrs = ai_finops._otlp_attributes({"feature_id": "OBS-1", "eval.model": "x"})
        keys = [a["key"] for a in attrs]
        self.assertIn("feature_id", keys)
        self.assertNotIn("eval.model", keys)

    def test_custom_allowlist_admits_only_its_own_keys(self):
        attrs = ai_finops._otlp_attributes(
            {"feature_id": "OBS-1", "eval.model": "x"},
            allowed={"eval.model"},
        )
        keys = [a["key"] for a in attrs]
        self.assertEqual(keys, ["eval.model"])

    def test_explicit_timestamp_is_used_verbatim(self):
        captured = {}

        def fake_post(url, headers, body, **kwargs):
            captured["body"] = json.loads(body.decode("utf-8"))
            return 200

        with mock.patch.object(ai_finops, "_otlp_endpoint", "https://example.invalid/v1/logs"), \
             mock.patch.object(ai_finops._http, "post_bytes_status", fake_post):
            ok = ai_finops.emit_otlp_records(
                [{"eval.model": "m"}], "berserk-mcp-eval",
                scope_name="berserk-mcp.canary",
                timestamp_ns=1234567890000000000,
                allowed_keys={"eval.model"},
            )
        self.assertTrue(ok)
        logs = captured["body"]["resourceLogs"][0]["scopeLogs"][0]
        self.assertEqual(logs["scope"]["name"], "berserk-mcp.canary")
        self.assertEqual(logs["logRecords"][0]["timeUnixNano"], "1234567890000000000")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ai_finops.py -k EmitOtlpRecordsOptions -v`
Expected: FAIL — `_otlp_attributes() takes 1 positional argument`, and `emit_otlp_records()` rejects the keyword arguments.

- [ ] **Step 3: Implement**

Extract the existing set to a module constant, then add the parameters:

```python
# ai_finops.py — replace the hardcoded set inside _otlp_attributes

_FINOPS_ATTRIBUTE_ALLOWLIST = frozenset({
    "feature_id", "work_item_id", "project_id", "portfolio_id", "team_id",
    "cost_center", "status", "planned_start", "planned_end",
    "planned_hours", "planned_ai_budget_usd", "completion_pct", "repositories",
    "branches", "pull_requests", "source_system", "source_record_id",
    "source_updated_at", "worklog_id", "work_date", "hours", "actual_hours",
    "recommendation_id", "decision", "owner_hash", "rationale_hash", "ts",
})


def _otlp_attributes(record, allowed=None):
    """Build OTLP attributes for one record.

    ``allowed`` is an allowlist, not a convenience filter: it stops a caller
    leaking arbitrary record fields into telemetry. It defaults to the FinOps
    set so existing callers are unaffected. A caller emitting a different
    attribute family (the eval canary, for example) passes its own set --
    without one, its attributes are silently dropped and the POST still
    returns 200.
    """
    if allowed is None:
        allowed = _FINOPS_ATTRIBUTE_ALLOWLIST
    attrs = []
    for key in sorted(record):
        if key not in allowed:
            continue
        # ... existing body unchanged ...
```

```python
# ai_finops.py — emit_otlp_records signature and the two changed lines

def emit_otlp_records(records, service_name, scope_name="berserk-mcp.ai-finops",
                      timestamp_ns=None, allowed_keys=None):
    if not _otlp_endpoint or not records:
        return False
    # ... existing URL validation unchanged ...
    logs = []
    if timestamp_ns is None:
        stamp = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))
    else:
        stamp = str(int(timestamp_ns))
    for record in records:
        logs.append({
            "timeUnixNano": stamp,
            "body": {"stringValue": service_name},
            "attributes": _otlp_attributes(record, allowed=allowed_keys),
        })
    payload = {
        "resourceLogs": [{
            "resource": {"attributes": [{
                "key": "service.name", "value": {"stringValue": service_name}
            }]},
            "scopeLogs": [{"scope": {"name": scope_name},
                           "logRecords": logs}],
        }]
    }
    # ... existing post unchanged ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ai_finops.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Verify no call site changed behavior**

Run: `grep -rn "emit_otlp_records\|_otlp_attributes" --include="*.py" .`
Confirm every existing call passes neither new keyword, and therefore keeps the FinOps allowlist, the `berserk-mcp.ai-finops` scope, and now-timestamps. Do not assume this — read each call site.

- [ ] **Step 6: Commit**

```bash
git add ai_finops.py tests/test_ai_finops.py
git commit -m "feat: optional scope, timestamp, and allowlist on emit_otlp_records"
```

---

## Task 2: Frozen case set, canary run, Berserk ingestion

**Files:**
- Create: `evals/canary_cases.jsonl`, `evals/canary.py`
- Test: `evals/test_canary.py` (in `evals/`, not `tests/` — see File Structure)

**Interfaces:**
- Consumes: `ai_finops.emit_otlp_records(..., scope_name=, timestamp_ns=, allowed_keys=)` from Task 1.
- Produces:
  - `evals.canary.case_set_version(path) -> str` — 12-char content hash.
  - `evals.canary.build_eval_record(report, case_set_version, run_id, started_ns) -> dict` — the record Task 4 later reads back from Berserk.
  - `evals.canary.EVAL_ATTRIBUTE_ALLOWLIST` — frozenset of `eval.*` keys.
  - `evals.canary.run_canary(model, backend, cases_path, repeats) -> dict` — runs and returns the record; raises on harness failure.

**Design note — the case-set version is a content hash, not a hand-maintained string.** The spec described bumping a version manually. A content hash is strictly better: it cannot be forgotten. Editing `canary_cases.jsonl` automatically changes the version, which automatically stops Task 4 comparing scores across incompatible case sets.

- [ ] **Step 1: Create the frozen case set**

```bash
cp evals/router_cases.jsonl evals/canary_cases.jsonl
```

Add a note to the top of `evals/canary_cases.jsonl`'s companion documentation (not the JSONL itself, which must stay parseable) — record in the commit message that this file is frozen and that editing it resets the comparison baseline.

- [ ] **Step 2: Write the failing tests**

```python
# evals/test_canary.py
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import canary  # noqa: E402


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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest evals/test_canary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.canary'`.

- [ ] **Step 4: Implement `evals/canary.py`**

```python
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
})


def _hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()[:12]


def case_set_version(path=DEFAULT_CASES):
    """Version is the content hash, so it can never be forgotten on edit."""
    return _hash_bytes(Path(path).read_bytes())


def build_eval_record(report, version, run_id, started_ns):
    record = {
        "eval.model": report.get("model", ""),
        "eval.backend": report.get("backend", ""),
        "eval.case_set_version": version,
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
    return {
        "eval.model": model,
        "eval.backend": backend,
        "eval.case_set_version": version,
        "eval.run_id": run_id,
        "eval.status": "failed",
        "eval.error": str(error)[:500],
    }


def _run_harness(model, backend, cases_path, repeats, timeout=900):
    """Invoke run_eval.py and return its saved report. Mirrors ci_gate.py."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    before = set(RESULTS_DIR.glob("*.json"))
    proc = subprocess.run(
        [sys.executable, str(HERE / "run_eval.py"),
         "--backend", backend, "--model", model,
         "--repeats", str(repeats), str(cases_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"run_eval.py exited {proc.returncode}: {proc.stderr[:500]}")
    new_files = set(RESULTS_DIR.glob("*.json")) - before
    if not new_files:
        raise RuntimeError("run_eval.py produced no new results file")
    newest = max(new_files, key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text(encoding="utf-8"))


def run_canary(model, backend="openai", cases_path=DEFAULT_CASES, repeats=3):
    version = case_set_version(cases_path)
    run_id = uuid.uuid4().hex[:16]
    started_ns = int(time.time() * 1_000_000_000)
    try:
        report = _run_harness(model, backend, cases_path, repeats)
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest evals/test_canary.py -v`
Expected: PASS.

- [ ] **Step 6: Baseline the natural variance — this produces the number Task 4 needs**

Run the canary repeatedly against one known-stable model and record the spread. Do **not** proceed to Task 4's threshold with a guessed number.

```bash
for i in 1 2 3 4 5; do
  python -c "
import sys; sys.path.insert(0, 'evals')
import canary
r = canary.run_canary('deepseek/deepseek-v4-flash', repeats=3)
print(r.get('eval.tool_accuracy'), r.get('eval.status'))
"
done
```

Record the observed min, max, and standard deviation in the commit message and in `docs/model-routing-cost-validation-2026-08-23.md` as a dated addendum. Task 4's `DEFAULT_NOISE_BAND` is set from this measurement.

- [ ] **Step 7: Commit**

```bash
git add evals/canary_cases.jsonl evals/canary.py evals/test_canary.py
git commit -m "feat: frozen canary case set and scored daily canary run (#89)"
```

---

## Task 3: Provider-update detection via two fingerprints

**Files:**
- Create: `evals/fingerprint.py`
- Test: `evals/test_fingerprint.py` (in `evals/`, not `tests/`)

**Interfaces:**
- Consumes: `parser_factory.hermes_models_url(url)` — derives a `/models` endpoint from a chat-completions URL. Suffix-based and depth-agnostic (fixed in PR #88 on 2026-08-29 precisely because a positional derivation produced a doubled, wrong URL for a real provider). Do not reimplement this derivation.
- Produces:
  - `evals.fingerprint.metadata_fingerprint(models_payload, model_id) -> str | None`
  - `evals.fingerprint.behavioral_fingerprint(completions) -> str`
  - Both feed `eval.provider_metadata_fingerprint` and `eval.behavioral_fingerprint` in Task 2's record.

- [ ] **Step 1: Write the failing tests**

```python
# evals/test_fingerprint.py
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fingerprint  # noqa: E402


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest evals/test_fingerprint.py -v`
Expected: FAIL — `No module named 'evals.fingerprint'`.

- [ ] **Step 3: Implement `evals/fingerprint.py`**

```python
#!/usr/bin/env python3
"""Provider-change fingerprints for model-behavior monitoring (issue #90).

Two independent signals, because either alone has a blind spot:

  metadata_fingerprint   -- hashes the provider's own /models entry for one
                            model (context length, pricing, version fields).
                            Catches a declared change.
  behavioral_fingerprint -- hashes temperature-0 completions for a fixed
                            prompt set. Catches a silent weights swap that
                            left /models metadata untouched.

IMPORTANT: a changed behavioral fingerprint is a SIGNAL TO INVESTIGATE, not
proof the provider swapped the model. Temperature 0 is not guaranteed
deterministic across providers -- batching and hardware nondeterminism can
change output with no model change. Every caller must report it that way.
"""
import hashlib
import json

# Deliberately short, deterministic, and cheap. These are fingerprint probes,
# not quality tests -- quality is measured by the scored canary.
FINGERPRINT_PROMPTS = (
    "Reply with exactly the word: ready",
    "Return only the number 42.",
    "Answer with one word: what colour is a clear midday sky?",
)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalize(value):
    """Order-independent, whitespace-insensitive canonical form."""
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def metadata_fingerprint(models_payload, model_id):
    """Hash one model's /models entry. Returns None if the model is absent --
    which is itself worth reporting, since a model vanishing from the catalog
    is a provider change."""
    entries = models_payload.get("data") or []
    for entry in entries:
        if entry.get("id") == model_id:
            canonical = json.dumps(_normalize(entry), sort_keys=True, separators=(",", ":"))
            return _hash(canonical)
    return None


def behavioral_fingerprint(completions):
    """Hash normalized completions for the fixed prompt set."""
    canonical = json.dumps([_normalize(c) for c in completions],
                           sort_keys=True, separators=(",", ":"))
    return _hash(canonical)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest evals/test_fingerprint.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the fetch layer**

Add a `fetch_models(chat_completions_url, api_key)` helper that derives the endpoint with `parser_factory.hermes_models_url()` and fetches through the shared HTTP policy layer (`_http`), so scheme allowlisting, redirect refusal, and body caps all apply. Return the parsed payload for `metadata_fingerprint`.

- [ ] **Step 6: Commit**

```bash
git add evals/fingerprint.py evals/test_fingerprint.py
git commit -m "feat: provider metadata and behavioral fingerprints (#90)"
```

---

## Task 4: Drift and regression verdict logic

**Files:**
- Create: `model_drift.py`
- Test: `tests/test_model_drift.py`

**Interfaces:**
- Consumes: records produced by `evals.canary.build_eval_record` (Task 2), read back from Berserk; fingerprint values from Task 3.
- Produces: `model_drift.classify(series, noise_band, fingerprint_changed) -> dict` with keys `verdict`, `reason`, `confidence`. Task 5's tools render this.

**Reuse, do not reimplement.** `agent_analytics._trend_fit(text)` already parses `series_fit_line`'s `[R², slope, ...]` output and handles both the JSON and TSV renderers. The established "trend is unreliable" floor is `r2 < 0.6` (`berserk_mcp.py:2913`). Match both rather than inventing new ones.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_model_drift.py
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_drift


def series(*scores, version="v1"):
    return [{"tool_accuracy": s, "case_set_version": version, "status": "ok"}
            for s in scores]


class VerdictTest(unittest.TestCase):
    BAND = 0.05

    def test_insufficient_history_is_explicit_not_stable(self):
        out = model_drift.classify(series(0.88), self.BAND, False)
        self.assertEqual(out["verdict"], "insufficient-data")

    def test_flat_series_is_stable(self):
        out = model_drift.classify(series(0.88, 0.87, 0.88, 0.89), self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_drop_inside_the_noise_band_does_not_fire(self):
        out = model_drift.classify(series(0.88, 0.88, 0.86, 0.85), self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_single_degraded_run_does_not_fire(self):
        out = model_drift.classify(series(0.88, 0.88, 0.88, 0.60), self.BAND, False)
        self.assertNotIn(out["verdict"], ("degrading", "step-change"))

    def test_two_consecutive_degraded_runs_fire_as_step_change(self):
        out = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, False)
        self.assertEqual(out["verdict"], "step-change")

    def test_mixed_case_set_versions_are_not_compared(self):
        mixed = series(0.88, 0.88, version="v1") + series(0.60, 0.59, version="v2")
        out = model_drift.classify(mixed, self.BAND, False)
        self.assertEqual(out["verdict"], "insufficient-data")

    def test_failed_runs_are_excluded_not_scored_zero(self):
        s = series(0.88, 0.88, 0.88)
        s.append({"status": "failed", "case_set_version": "v1"})
        out = model_drift.classify(s, self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_fingerprint_change_raises_confidence_on_step_change(self):
        without = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, False)
        with_fp = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, True)
        self.assertEqual(with_fp["verdict"], "step-change")
        self.assertGreater(
            model_drift.CONFIDENCE_RANK[with_fp["confidence"]],
            model_drift.CONFIDENCE_RANK[without["confidence"]],
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_model_drift.py -v`
Expected: FAIL — `No module named 'model_drift'`.

- [ ] **Step 3: Implement `model_drift.py`**

```python
#!/usr/bin/env python3
"""Verdict logic for model-behavior monitoring (issue #91).

Reads the score series the canary stored in Berserk and answers one
question: is this model still behaving the way it did?

The hard part is not detecting a drop -- it is NOT firing on noise. At 48
cases and repeats=1, one case flipping moves accuracy ~2.1 points. A canary
that alerts on that gets ignored within a week, which is worse than no
canary. Hence: a drop must exceed a measured noise band AND persist across
two consecutive runs before anything fires.

The noise band is measured, not guessed -- see the baselining step in the
implementation plan. DEFAULT_NOISE_BAND below is provisional until that
measurement lands.
"""

MIN_HISTORY = 4          # runs at one case_set_version before any verdict
CONSECUTIVE_REQUIRED = 2  # degraded runs in a row before firing
MIN_TREND_R2 = 0.6        # matches berserk_mcp.py:2913's forecastability floor
DEFAULT_NOISE_BAND = 0.05  # PROVISIONAL -- replace with measured variance

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _usable(series):
    """Drop failed runs. A provider outage is not a score of zero."""
    return [r for r in series if r.get("status") == "ok"]


def _single_version(rows):
    versions = {r.get("case_set_version") for r in rows}
    return versions.pop() if len(versions) == 1 else None


def classify(series, noise_band=DEFAULT_NOISE_BAND, fingerprint_changed=False):
    rows = _usable(series)
    if len(rows) < MIN_HISTORY or _single_version(rows) is None:
        return {"verdict": "insufficient-data",
                "reason": "not enough runs at a single case-set version",
                "confidence": "low"}

    scores = [float(r["tool_accuracy"]) for r in rows]
    baseline = sum(scores[:-CONSECUTIVE_REQUIRED]) / len(scores[:-CONSECUTIVE_REQUIRED])
    recent = scores[-CONSECUTIVE_REQUIRED:]

    degraded = [s for s in recent if baseline - s > noise_band]
    if len(degraded) < CONSECUTIVE_REQUIRED:
        return {"verdict": "stable",
                "reason": "no sustained drop beyond the noise band",
                "confidence": "medium"}

    drop = baseline - min(recent)
    sharp = drop > 2 * noise_band
    verdict = "step-change" if sharp else "degrading"
    confidence = "high" if (sharp and fingerprint_changed) else "medium"
    reason = f"{drop:.3f} below baseline {baseline:.3f} across {CONSECUTIVE_REQUIRED} runs"
    if fingerprint_changed:
        reason += "; provider fingerprint also changed"
    return {"verdict": verdict, "reason": reason, "confidence": confidence}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_model_drift.py -v`
Expected: PASS.

- [ ] **Step 5: Add the series-read KQL**

Add `series_kql(model, since)` returning KQL that filters `resource['service.name'] == 'berserk-mcp-eval'`, projects the `eval.*` attributes, and orders by timestamp. Reuse `agent_analytics._trend_fit` for any `series_fit_line` parsing rather than writing a second reader.

- [ ] **Step 6: Replace the provisional noise band**

Using Task 2 Step 6's measured variance, set `DEFAULT_NOISE_BAND` and delete the `PROVISIONAL` comment. If that measurement has not been taken, stop and take it — do not ship the guess.

- [ ] **Step 7: Commit**

```bash
git add model_drift.py tests/test_model_drift.py
git commit -m "feat: drift and regression verdict logic with measured noise band (#91)"
```

---

## Task 5: MCP tools, headless CLI, and alerting

**Files:**
- Modify: `berserk_mcp.py` (TOOLS, TITLES, dispatcher, CLI)
- Test: `tests/test_berserk_mcp.py`, `tests/test_roles.py`

**Interfaces:**
- Consumes: `model_drift.classify(...)` and `model_drift.series_kql(...)` from Task 4; `evals.canary.run_canary/emit` from Task 2.
- Produces: tools `model_drift_check`, `model_drift_history`; CLI flags `--canary-run`, `--drift-report`.

**Only two tools, deliberately.** The 2026-08-22/23 eval sweep measured 7-8B models scoring 5-7% tool-selection accuracy against the full schema, below a 66% keyword-matching baseline. The server registers 72 tools today. Five monitoring tools would erode the property berserk-mcp exists to protect.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_berserk_mcp.py

class ModelDriftToolsTest(unittest.TestCase):
    def test_both_tools_are_registered(self):
        names = {t["name"] for t in bm.TOOLS}
        self.assertIn("model_drift_check", names)
        self.assertIn("model_drift_history", names)

    def test_tools_are_on_the_claude_lane(self):
        for t in bm.TOOLS:
            if t["name"].startswith("model_drift"):
                self.assertIn("claude", t["roles"])

    def test_descriptions_state_the_routing_only_boundary(self):
        """A monitoring tool that overstates its coverage is worse than none."""
        for t in bm.TOOLS:
            if t["name"].startswith("model_drift"):
                self.assertIn("tool-routing", t["description"])

    def test_both_tools_have_titles(self):
        self.assertIn("model_drift_check", bm.TITLES)
        self.assertIn("model_drift_history", bm.TITLES)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_berserk_mcp.py -k ModelDriftTools -v`
Expected: FAIL — the names are absent from `bm.TOOLS`.

- [ ] **Step 3: Register the tools**

```python
# berserk_mcp.py -- add to TOOLS, near the other claude-lane entries

    {"name": "model_drift_check", "roles": ["claude"], "description": "Check whether a canaried model still routes as well as it did. Returns stable, degrading, step-change, or insufficient-data per model, with the provider fingerprint status. Measures tool-routing quality only -- not prose, reasoning, or code quality. Use for 'has the model got worse' or 'did the provider change the model'.", "inputSchema": {"type": "object", "properties": dict({"model": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS, "description": "optional single model to check"}}, **_since())}},
    {"name": "model_drift_history", "roles": ["claude"], "description": "Score and fingerprint history for one canaried model over time, for investigating a flagged drift verdict. Measures tool-routing quality only. Use after model_drift_check reports degrading or step-change.", "inputSchema": {"type": "object", "properties": dict({"model": {"type": "string", "maxLength": MAX_INTERPOLATED_NAME_CHARS}}, **_since()), "required": ["model"]}},
```

```python
# berserk_mcp.py -- TITLES
TITLES["model_drift_check"] = "Model Drift Check"
TITLES["model_drift_history"] = "Model Drift History"
```

- [ ] **Step 4: Add dispatcher branches**

Follow the `forecast_capacity` branch at `berserk_mcp.py:2892` as the template: build KQL via `model_drift.series_kql`, run through the existing `bzrk` path, classify, and return a human summary plus the structured JSON envelope other analytical tools already return.

- [ ] **Step 5: Add the CLI flags**

Alongside `--worker` and `--agent-report` (near `berserk_mcp.py:4923`):

```python
    cli.add_argument("--canary-run", action="store_true",
                     help="run the model canary for BERSERK_MCP_CANARY_MODELS and ingest results")
    cli.add_argument("--drift-report", action="store_true",
                     help="evaluate stored canary history; exit non-zero if any model is degrading")
```

`--drift-report` exits non-zero when any verdict is `degrading` or `step-change`, so cron and systemd can pipe it to an alert transport — matching `--agent-report`'s established contract. Discord posting reuses the existing bridge and stays off unless `BERSERK_DISCORD_ALERT_SECRET` is set; a run with nothing noteworthy posts nothing.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, including `tests/test_roles.py` (lane isolation) and `tests/test_tool_discovery.py` (every shipped tool reachable via `find_tool`).

- [ ] **Step 7: Commit**

```bash
git add berserk_mcp.py tests/test_berserk_mcp.py tests/test_roles.py
git commit -m "feat: model_drift_check and model_drift_history tools, canary CLI (#92)"
```

---

## Task 6: Documentation

**Files:**
- Modify: `README.md`, `docs/configuration.md`, `docs/otel-setup.md`

- [ ] **Step 1: Document the three new environment variables**

`BERSERK_MCP_CANARY_MODELS` (unset = feature entirely off), `BERSERK_MCP_CANARY_REPEATS` (default 3), `BERSERK_MCP_CANARY_CASES` (default `evals/canary_cases.jsonl`).

- [ ] **Step 2: State the boundary and the limitations**

In the README's tool tables and a short section: this measures tool-routing quality only; the behavioral fingerprint is a signal not proof; canary runs cost real money (~$0.002/run measured for `deepseek-v4-flash` on 2026-08-29); a failed run is recorded as a failure, not a zero.

- [ ] **Step 3: Add the BROCS row**

The comparison table already covers Berserk's native agent. Add that model-drift detection is a berserk-mcp capability with no default-Berserk equivalent.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/configuration.md docs/otel-setup.md
git commit -m "docs: model-behavior monitoring configuration and limitations"
```

---

## Self-review notes

**Spec coverage.** All three spec mechanisms map to tasks: quality regression → Tasks 2+4; provider updates → Task 3; drift → Task 4. Storage → Tasks 1+2. Exposure → Task 5. Limitations → Task 6. The noise handling the spec calls the primary correctness risk is Task 4's core, with its threshold sourced from Task 2 Step 6 rather than invented.

**Deviation from the spec, deliberate.** The spec described `case_set_version` as a manually bumped string. Task 2 uses a content hash instead — it cannot be forgotten on edit, which makes the cross-version comparison guard self-maintaining rather than dependent on discipline.

**Not in the spec, found while planning.** `_otlp_attributes()`'s allowlist silently drops unknown keys. Without Task 1, the canary would emit successfully and store nothing. Issue #89 should be updated to mention this.

**Type consistency.** `build_eval_record` produces exactly the `eval.*` keys in `EVAL_ATTRIBUTE_ALLOWLIST` (locked by a test), which is what Task 2's `emit` passes as `allowed_keys`, and what Task 4's `classify` reads. `classify`'s return shape (`verdict`, `reason`, `confidence`) is what Task 5 renders.
