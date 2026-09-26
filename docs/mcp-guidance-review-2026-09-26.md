# berserk-mcp repository review against the MCP guidance brief

**Date:** 2026-09-26
**Repository:** `main` at `313297b` (version 1.29.1)
**Brief:** `docs/mcp-guidance-and-repository-review-brief-2026-09-25.md` (committed alongside this review)
**Verification:** every finding was reproduced offline, then checked by a read-only Codex pass that tried to refute each one. That pass confirmed four, adjusted one fix (`since`), and refuted one as a defect (envelope, moved to section 7).
**Mode:** review only. No code was changed. No live Berserk profile was used: every probe ran offline with `BZRK_BIN` pointing at a missing binary or a local stub.

## 1. Executive summary

berserk-mcp is already well ahead of a typical MCP server on safety. It has fixed verified queries, a mandatory KQL execution boundary, fenced untrusted data, redaction on by default, role lanes plus a small/deep tier, and fail-closed CI gates. The protocol layer behaved correctly on every probe.

The main problems are consistency and cost, not missing features:

1. **The small tier contradicts itself (P1).** Every small-tier lane's instructions and primer tell the agent to use `search`, `save_query` and `validate_kql`, which that tier hides. A small model that follows its instructions gets `unknown tool`, and that deliberately non-leaking error gives it no way to recover.
2. **Misspelled optional arguments silently widen the scope (P2).** No tool rejects unknown arguments. `detect_anomalies` with `svc` instead of `service` runs across all services and returns a normal-looking answer.
3. **LLM-generated queries become small-tier tools without review (P2).** Parser-factory output, built from untrusted telemetry, is projected into operational lanes as soon as it passes validation and a test run.
4. **`since` costs about 45% of every lane's `tools/list` (P2).** Its hand-expanded case-insensitive regex alone is about 30%.
5. **The error-rate investigation can report "nothing to investigate" when a source has gone silent (P2).**
6. **Gap against the brief, not a defect:** the result envelope carries only the window and row count, as its own spec (issue #2) defines. The brief also asks for source, freshness, redaction status and an evidence reference (section 7).

Answers to the brief's six outcome questions:

| Question | Answer |
|---|---|
| Lean enough for small/local models? | Partly. The small tier cuts `ops` from 36 to 23 tools, but about 6,100 tokens of first-turn context remain, and 45% of the tool bytes are the repeated `since` schema. |
| Typed and self-guiding enough? | Not yet. The instructions point to hidden tools (P1), and unknown arguments are accepted silently (P2). |
| Safe against arbitrary query, shell, injection and exfiltration? | Yes, for the paths reviewed. No shell, SSH or URL input exists. KQL is bounded by `_kql_boundary`, and telemetry is fenced. The open point is the unreviewed generated-tool path (P2). |
| Operational and deep authoring separated? | In `tools/list` and `tools/call`, yes, through one predicate. In instructions and primers, no (P1). The generated-query path also crosses from deep to small (P2). |
| Reproducible evidence for correlation and RCA? | For the error-rate tree and `trace_analyze`, yes. There is no baseline comparison, no missing-telemetry check before a "healthy" verdict, and no deployment correlation. |
| Compatible with Claude Code, Codex and ordinary MCP clients? | Yes. Both protocol eras pass the smoke test, and every negotiation and error case probed behaved per JSON-RPC and MCP. |

## 2. What Berserk already does better than typical MCP servers

- **Fixed verified queries** are the default path. Free KQL goes through a mandatory boundary that refuses other tables in every validation mode (`_kql_boundary.check`, since `b221755`).
- **One visibility predicate.** `tool_visible` covers both `tools/list` and `tools/call`. A hidden tool returns the same `unknown tool: <name>` as a tool that doesn't exist (probed: `search` in the `ops` lane).
- **Untrusted data is fenced** in every spelling a model can read, including entity, JSON and URL encodings (`_tag_guard`). Redaction is on by default, and secrets are scanned in full.
- **Bounded execution.** Argv-only subprocesses, stdout and stderr caps, timeouts, a query semaphore, per-tool budgets derived from static risk, a result cache and fail cooldown.
- **Fail-closed CI gates:** a security-review fingerprint registry, SECURITY.md coverage, Cisco mcp-scan with a baseline, the pinned Trail of Bits semgrep rules with a canary, and a packaging import walk.
- **Protocol hygiene.** Legacy and 2026-07-28 modes, correct `-32700`, `-32601` and `-32602` codes, notifications without responses, and `initialize` negotiating to the implemented version.

## 3. P1/P2 findings

### P1: Small-tier instructions and primers direct agents to hidden tools

*Severity note: the Codex verification pass confirmed this finding but rated it P2. I keep P1 because it is the only finding that affects every default lane in every session, and it works directly against the brief's first two outcome goals.*

- **File:line:**
  - `primers/ops.md` routing table ("Ad-hoc KQL → `search`", "Validate custom KQL → `validate_kql`"), and the same `search` / `validate_kql` references in `sre.md`, `soc.md` and `claude.md`; `windows-forensics.md` names `suggest_ingestion`;
  - the shared text built into `INSTRUCTIONS` by `build_instructions` (`berserk_mcp.py`): "get it working with `search`, then `save_query`";
  - `_ROLE_PREFIX["windows-forensics"]` (`berserk_mcp.py:549`): "before authoring or saving any query".
- **Category:** tool-contract
- **Impact:** in every small-tier lane (the default for every lane except `all`), the text the model reads first names tools it can't call. A small model that follows it gets `unknown tool: search`. That error is deliberately non-leaking, so the model can't tell a hidden tool from a typo, and retries or stalls. This defeats the purpose of the small tier. The `windows-forensics` lane also has no tools of its own: at the small tier it is identical to `ops` (23 tools, 21,713 bytes), while its instructions describe authoring work the tier forbids.
- **Evidence:** measured per lane, in a fresh process per role:

  | Lane (small tier) | Hidden tools named in INSTRUCTIONS | Hidden tools named in visible descriptions |
  |---|---|---|
  | ops, sre, soc, claude | `save_query`, `search`, `validate_kql` | `save_query`, `search`, `run_discovery_worker`, plus `soc_*` in some lanes |
  | windows-forensics | `save_query`, `search`, `suggest_ingestion` | same as ops |
  | all (deep) | none | none |

  Some description hits are the English word "search", but `discover_schema` → `save_query` and `discovery_status` → `run_discovery_worker` are real tool references.
- **Repro:**
  ```bash
  env -i PATH=/usr/bin:/bin HOME=$(mktemp -d) BERSERK_MCP_ROLE=ops python3 -c "
  import berserk_mcp as bm, re
  vis={t['name'] for t in bm.TOOLS+bm.MGMT_TOOLS if bm.tool_visible(t)}
  hidden={t['name'] for t in bm.TOOLS+bm.MGMT_TOOLS}-vis
  print(sorted(set(re.findall(r'[a-z_]+', bm.INSTRUCTIONS)) & hidden))"
  ```
- **Why existing tests miss it:** the tier tests check what `tools/list` and `tools/call` expose. Nothing cross-checks the text the model reads against the visible set. The tiers spec (FR-1 to FR-5) changed visibility only, and the primers were never revisited. This is fault 2 in the review-loop doc: one call site was fixed while the concern spans the whole surface.
- **Recommended fix:**
  - Make tool references in instructions and primers tier-aware. For example, mark the deep-only lines in the primers and drop them when the tier is small, or generate the routing table from the visible tools.
  - Add a test that fails when any instruction or visible description names a hidden tool, per lane and tier.
  - Decide what `windows-forensics` is for: either give it its own tools, or make its default tier deep, since it is an authoring lane.
- **Compatibility risk:** low. Text only; no tool changes.

### P2: Unknown or misspelled arguments are silently ignored, widening the scope

- **File:line:** `tool_catalog.py` (no `inputSchema` declares `additionalProperties: false`, 0 of 64); `handle_call` / `_dispatch_tools_call` in `berserk_mcp.py` (no check of argument names against the schema).
- **Category:** tool-contract
- **Impact:** a misspelled required argument is caught (`logs_for_service` → `missing required 'service'`). A misspelled optional filter is not. The call runs unfiltered and the answer looks filtered:
  - `detect_anomalies {"svc": "nginx"}` ran across all services and returned "Anomaly decomposition for window 6h ago…" with `is_error=false`.
  - `host_cpu {"host": "web-01"}` passes a filter the tool doesn't have. The answer covers every host.
- **Evidence:** run with a stub `bzrk` that records its argv. In both the misspelled and the no-such-filter case, the filter value never reached the query.
- **Repro:** call `bm.handle_call("detect_anomalies", {"svc": "nginx"})` with `BZRK_BIN` pointing at a stub script that logs its argv. The logged KQL has no `nginx`, and the result has `is_error=False`.
- **Why existing tests miss it:** the tests exercise correct argument names, and the protocol smoke test sends well-formed calls.
- **Recommended fix:**
  - In `handle_call`, reject argument names that are not in the tool's `inputSchema.properties`. Return an error that lists the valid names, for example: `unknown argument 'svc'; valid: service, since`.
  - Do this server-side rather than adding `additionalProperties: false` to every schema, which would cost bytes.
  - Allow-list any keys that clients legitimately inject, if one turns out to exist.
- **Compatibility risk:** medium. A client that sends extra keys today will start getting errors. Find out first with a count-only log line for one release.

### P2: LLM-generated queries become small-tier tools with no review gate

- **File:line:**
  - `parser_factory.py:1354` persists generated queries through `_persist_learned_query(..., action_source="generated")` after static validation and a test run;
  - `item_visible` (`berserk_mcp.py`) gates projection by role only;
  - `request_discovery` is visible in the `ops` small tier and queues sources for this pipeline.
- **Category:** safety
- **Impact:** a query and description written by an LLM from untrusted telemetry become a `saved__*` tool in operational lanes. A small model routes by name and description. A generated `errors_by_service_fast`, described as a "faster replacement for errors_by_service", appeared in the `ops` small tier even though its KQL counted INFO rows.

  Existing mitigations limit the damage: the boundary keeps it to the configured table and read-only, the description is fenced and redacted, and origin collisions are protected. So this is a confused-deputy and wrong-answer risk, not exfiltration.
- **Evidence:** I wrote one `origin: "generated"` entry into a temporary learned-query store. With `BERSERK_MCP_ROLE=ops` it appeared in `tools/list` as `saved__errors_by_service_fast`, under the tier `small`.
- **Repro:** write `[{"name": "...", "kql": "...", "description": "...", "origin": "generated", "since": "1h ago"}]` to `bm.LEARNED_PATH` in a temporary store, then list tools with `BERSERK_MCP_ROLE=ops`.
- **Why existing tests miss it:** the tests cover naming collisions, fencing and redaction of generated entries. None asserts that generated entries need approval before projection. `mcp-scan` in CI checks a fixture store, not a deployment's live store.
- **Recommended fix:**
  - Store generated entries as `status: "pending"`.
  - Project them only to the deep tier until an explicit approval (a new `review_generated action=approve` or a CLI flag) sets `status: "approved"`.
  - Human-authored `save_query` entries keep today's behaviour.
- **Compatibility risk:** medium. Generated tools that already reach small lanes would disappear until approved. Say so in the release note.

### P2: The `since` schema is about 45% of every lane's `tools/list`

- **File:line:** `_since()` and `_SINCE_SCHEMA_PATTERN` (`berserk_mcp.py:2085-2105`); the pattern comes from `_case_insensitive_literal`.
- **Category:** efficiency
- **Impact:** the pattern writes out case-insensitivity one letter at a time (`[mM][iI][nN][uU][tT][eE]…`), 372 characters (377 bytes serialized), repeated in every tool that takes `since`.

  | Lane | tools/list bytes | `since` field | pattern only |
  |---|---:|---:|---:|
  | ops (small) | 21,713 | 9,828 (45%) | 6,786 (31%) |
  | sre (small) | 32,209 | 14,742 (46%) | 10,179 (32%) |
  | claude (small) | 48,356 | 21,840 (45%) | 15,080 (31%) |
  | all | 77,271 | 32,214 (42%) | 22,243 (29%) |

- **Evidence:** by-field breakdown of `_tool_list_result("legacy")` per lane: input schemas are 11,979 of 21,713 bytes in `ops`, and descriptions are 5,058.
- **Repro:** see the per-lane measurement script in the appendix.
- **Why existing tests miss it:** no test or CI artifact reports `tools/list` bytes per lane. The tiers spec asked for that artifact, and it was not added.
- **Recommended fix:**
  - Case-insensitivity in the schema is deliberate: `test_since_schema_pattern_matches_uppercase_forms` requires it, so that grammar-constrained clients accept what the server accepts. Keep that, but stop spelling out every unit. For example, `^([Nn][Oo][Ww]|\d+\s*[A-Za-z]{1,7}(\s+[Aa][Gg][Oo])?)$` is 54 characters. It is looser (it accepts an unknown unit such as `5 xyz`), and the server's existing validation rejects that with a clear message.
  - Keep the examples.
  - The pattern was added on purpose in #7, "machine-constrained schema", so run the router eval before and after on a real small model.
  - Emit per-lane bytes as a CI artifact.

  Expected saving: 318 bytes per tool (377 down to 59 bytes serialized), which is about 5.7 KB (about 1,400 tokens) in `ops` and about 18.8 KB in `all`. I checked that the example pattern accepts every form the two existing `since` schema tests require, including `NOW`, `2 HOURS AGO` and `1D`.
- **Compatibility risk:** low. Upper-case forms stay valid. A client doing grammar-constrained decoding could now produce an unknown unit, which the server rejects with a clear message.

- **Outcome (2026-09-26):** shipped as a 59-byte case-insensitive pattern. Before/after router evals with role `all` (DeepSeek v4.1 Flash and Claude Haiku 4.5 via OpenRouter; `router_cases`, `router_cases_nearmiss` and a new `router_cases_since` with server-validity scoring; 1 repeat each):
  - input tokens fell about 47% per run, far more than the 24% byte saving, because the per-letter pattern tokenised badly; cost fell 35–41%;
  - tool and argument accuracy were unchanged within one case per run, and no difference involved `since`;
  - every `since` value was server-valid, and the new `since` cases scored 100% on both sides.
  The 12 runs are in `evals/run_ledger.jsonl`.

### P2: The error-rate investigation reports "healthy" when a source has gone silent

- **File:line:** `investigation._node_start` (`investigation.py:114-160`); `ERROR_RATE_INVESTIGATE_PER_MIN = 10` (`investigation.py:25`).
- **Category:** correlation
- **Impact:**
  - An empty `errors_by_service` result ends with "Verdict: no errors in window, nothing to investigate". That result is also what a stopped source, or a stopped ingest, produces.
  - "Elevated" is a fixed absolute rate with no baseline. A service that normally runs above 10 errors a minute always triggers, and a tenfold spike that stays below 10 a minute never does.
- **Evidence:** code path above. The empty-rows branch returns a final verdict without any freshness check.
- **Repro:** `tests/test_investigation.py` pattern with `bzrk_search` returning `(no rows)` for every query: the verdict is "no errors".
- **Why existing tests miss it:** the tests assert the verdict for empty input as correct behaviour.
- **Recommended fix:**
  - Before a no-errors verdict, check freshness with a bounded fixed query (last-seen per service, or `sre_ingest_health`), and report "no errors, and sources X and Y are reporting" or "no data from X since T".
  - Compare the window with the previous window of the same length before calling a rate elevated.
- **Compatibility risk:** low.

## 4. Lean-tooling and context-cost findings

First-turn context per lane at the default tier, measured as instructions plus `tools/list` (divide by 4 for rough tokens):

| Lane | Tier | Tools | tools/list | Instructions | ~Tokens |
|---|---|---:|---:|---:|---:|
| all | deep | 74 | 77,271 | 1,754 | 19,800 |
| claude | small | 46 | 48,356 | 6,944 | 13,800 |
| sre | small | 32 | 32,209 | 4,248 | 9,100 |
| soc | small | 31 | 30,785 | 4,154 | 8,700 |
| windows-forensics | small | 23 | 21,713 | 3,796 | 6,400 |
| ops | small | 23 | 21,713 | 2,780 | 6,100 |

- The small tier works: in each operational lane it removes 12–13 tools and about 12 KB, matching the tiers spec's target.
- The largest remaining cost is the `since` schema (P2 above). Descriptions are about 23% and annotations about 10%.
- The instructions carry deep-only guidance in small lanes: the full-text `search "term"` tokenisation notes and `validate_kql mode=live`. Removing it saves bytes and fixes P1.
- `detect_new_sources`, `request_discovery` and `discovery_status` are among the largest tools in `ops` and are pipeline tools. The tiers spec kept them in the small tier on purpose ("queueing and status, no authoring"). With the generated-tool finding, it is worth revisiting that choice for `request_discovery`.
- Cache: keyed by tool name, every argument after `since` normalisation, and the backend identity, and cleared when the backend changes. There is no finding.

## 5. MCP protocol and compatibility findings

No defects found. Probed over real stdio:

| Probe | Result |
|---|---|
| `initialize` with 2025-11-25, 2025-03-26, 2024-11-05, 2026-07-28, bogus | answers `2025-06-18`, the implemented legacy version. Modern mode is chosen per request through `_meta`, per 2026-07-28. Correct. |
| `ping` before `initialize` | `{}` |
| `tools/call` before `initialized` | served. This is lenient, not a defect. |
| hidden tool / unknown tool | identical `unknown tool: <name>`, `isError: true` |
| `arguments` not an object | `-32602 Invalid params` |
| unknown method / malformed JSON | `-32601` / `-32700` with `id: null` |
| protocol smoke (legacy + modern, subscriptions, tasks, list_changed) | 16/16 PASS |

Two minor points, below P2:
- The comment above `_jsonrpc_error` says the server "implements exactly one version". That has been out of date since modern-mode support.
- Server-authored errors ("bzrk not found", "invalid 'since' value") come back inside `<untrusted_log_data>`. Echoing the caller's value inside the fence is right, but the server's own configuration guidance is then marked as untrusted data. Consider fencing only the echoed value.

## 6. Security, PII, prompt-injection and supply-chain findings

- The generated-tool path is covered as a P2 above.
- **No arbitrary shell, SSH, URL or filesystem input** reaches an operational lane. Filesystem paths come from operator configuration and go through `_store.validate_store_path`.
- **KQL:** mandatory source confinement and literal-only operands since `b221755`. The documented residual risk is a stored function called in scalar context.
- **Injection:** telemetry, saved-query descriptions and parser samples are fenced by `_tag_guard`. Redaction is on by default and the secret scan is complete.
- **Supply chain:** stdlib only (`dependencies = []`), and GitHub Actions are pinned by SHA. The Trail of Bits rules are pinned and their files verified. `pip install semgrep` in CI is not version-pinned; that is low risk because it only runs a scanner.
- **Open security items from earlier work, not re-reviewed here:** the `_store` path race and symlink findings (scan findings 3 and 5), and the non-JSON redaction gaps.

## 7. Tool-contract and correlation findings

- The contract findings are the P1 and the two envelope and argument P2s above.
- **Envelope fields (gap against the brief, not a defect):** `_envelope` (`berserk_mcp.py:2279-2305`) emits `window=… rows=…`, exactly as its spec for issue #2 defines. The brief also asks for the source or query kind, freshness, redaction status and a stable evidence reference. The model can't tell today whether redaction was on (`BERSERK_MCP_REDACT=off` only logs a warning at startup). If you adopt the brief's direction, add those fields to the same header line, for example `source=fixed:host_cpu redaction=redact ref=<tool>@<since>#<hash of rows>`. Keep the rows unchanged and keep the `BERSERK_MCP_ENVELOPE=0` escape. Compatibility risk is low, since only the header changes.
- Descriptions of the fixed tools are generally good. They give intended use and point confusable pairs at each other (`top_cpu` ↔ `host_cpu`), and `_EMPTY_NEXT_STEP` gives a next step for every SIMPLE tool.
- Correlation that works deterministically today:
  - the error-rate tree: errors → log-volume spike → failing traces;
  - `trace_analyze`: every span of one trace, plus the log lines for that `trace_id`.
- Gaps:
  - no baseline (before, during and after) comparison;
  - no missing-telemetry check before a healthy verdict (P2);
  - no deployment or change correlation. The code has no deployment signal, and whether the telemetry contains one needs a read-only live check;
  - no host ↔ service ↔ container identity map beyond `container_hosts`.

## 8. Test and evaluation gaps

- No test cross-checks instruction or description text against the visible tool set (P1).
- No test sends unknown or misspelled arguments (P2).
- No CI artifact of per-lane `tools/list` bytes, although the tiers spec asked for one.
- `evals/router_cases.jsonl`: 54 cases, 16 with a `tier` label and none with a `lane` label. The spec asked for both, plus confusable-pair cases.
- The CI router gate uses the mock (keyword) backend, currently 79.6% against a 75% threshold. It tests the harness plumbing, not real small-model routing against the spec's target of 95% or more. Real-model runs are manual.
- Investigation tests assert "no errors" on empty input as correct (P2).

## 9. Recommended implementation sequence

Each step is a separate change, run through the usual loop: test first, full suite, Codex review, CI.

1. **P1: tier-aware instructions and primers**, plus a guard test that no visible text names a hidden tool, for every lane and tier. Decide the `windows-forensics` default tier. Small; no behaviour change to tools.
2. **P2: reject unknown arguments** in `handle_call` with the valid names listed. Log-only for one release first if you want to measure client impact.
3. **P2: review gate for generated queries** (`pending`, projected to the deep tier only until approved).
4. **P2: freshness check and baseline in the error-rate investigation.**
5. **P2: `since` schema diet**, with a router eval before and after on a real small model, plus a per-lane bytes CI artifact.
6. **Brief-driven, optional: envelope fields** (source, redaction, reference). This extends the issue #2 spec rather than fixing it.
7. **Evals:** lane labels, confusable-pair cases, and a small-tier real-model run recorded in the ledger.

Steps 1 and 2 give the most improvement in small-model reliability for the least change.

## 10. Items that are not problems or should not be changed

- `initialize` always answering `2025-06-18`: correct negotiation, since modern mode is per request.
- `unknown tool: <name>` for hidden tools: keep it non-leaking. Fix P1 by not naming hidden tools, not by making the error say "hidden".
- Keeping `list_saved`, `run_saved`, `discover_schema` and `schema` in the small tier, as the tiers spec decided.
- Accepting `tools/call` before `notifications/initialized`: lenient and harmless.
- Case-insensitivity in the `since` schema pattern: deliberate and tested. Shrink how it is spelled, not what it accepts.
- The cache key design.
- Stdlib only, the fixed-query model, and the mandatory KQL boundary. The brief says to preserve these, and nothing here argues against them.
- No generic arbitrary-KQL, shell, SSH or execute tool should be added.

## Findings verified false-positive

- *The result envelope is defective because it lacks source, freshness and redaction fields.* Refuted as a defect by the Codex verification pass: the issue #2 spec defines exactly window and rows. It stays in section 7 as a gap against the brief.
- *The envelope breaks its spec's "raw rows byte-identical" rule by fencing the rows, and covers `_AGENT_AWARE_SIMPLE` tools the spec didn't name.* Both are deliberate later changes (fencing in `9bc3a73`, #11), not drift.

- *`initialize` ignores a request for 2026-07-28.* By design: 2026-07-28 moves the protocol version into per-request `_meta`, and the smoke test covers modern mode.
- *The cache could serve a result for different arguments.* The key covers the name, all normalised arguments and the backend.
- *Server-authored errors fenced as untrusted are an injection gap.* No: fencing is conservative. It is only a clarity point (section 5).

## Unexercised areas

- **Live Berserk:** no read-only smoke test was authorised. Real output shapes, deployment signals in the telemetry, and real freshness fields are unverified.
- **External references** listed in the brief (MCP spec repo, SDKs, registry, skills catalogs, security checklists): not inspected in depth in this pass. The findings rest on this repo's code and real output, not on comparison with those repos.
- **Real-model routing:** only the mock backend ran. The P1 impact on a real small model is inferred from the text the model receives, not measured.
- Windows and the HTTP transport (`--include-http`) were not re-run in this review. CI covers them.

## Claude Code quality trend

(Per AGENTS.md, for the work reviewed in this session.)

- **Improvements:**
  - Findings were verified by running before being reported in every change this session.
  - Security gates fail closed, and each was checked with a planted failure.
  - Scans found gaps outside the requested scope and fixed them (the third fence in `parser_factory`).
- **Repeated mistake:** fault 2, fixing one surface while the concern spans several. The tier change (issue #4) updated visibility but not the primers or instructions (P1 here). In this session, a doc sentence quoting an insecure curl flag would have failed the new semgrep gate (and this review's first draft did the same); it was caught before push.
- **Regression risk found by testing, not reading:** a worktree copied with `cp -R` shared its git index, so a test file got staged into the real branch. It was caught and removed before commit. Use `git init` copies for experiments.
- **Unexercised:** real small-model routing, and live read-only checks.
- **Strengths preserved:** stdlib only, the fixed-query model, a single visibility predicate, the non-leaking hidden-tool error, and a verified-by-execution save.

## Appendix: measurement script

```bash
for role in all ops sre soc claude windows-forensics; do
  env -i PATH=/usr/bin:/bin HOME=$(mktemp -d) BERSERK_MCP_ROLE=$role python3 -c "
import json, berserk_mcp as bm
r=bm._tool_list_result('legacy'); b=len(json.dumps(r))
s=sum(len(json.dumps(t['inputSchema']['properties']['since'])) for t in r['tools'] if 'since' in t['inputSchema'].get('properties',{}))
print(f'{\"$role\":18} tier={bm.ACTIVE_TIER_RESOLVED:5} tools={len(r[\"tools\"]):3} bytes={b:6} since={s:6} ({100*s//b}%) instructions={len(bm.INSTRUCTIONS)}')"
done
```
