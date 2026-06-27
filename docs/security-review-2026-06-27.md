# Security review — 2026-06-27

First end-to-end security review of berserk-mcp v1.6 / homelab v2.1, covering
`berserk_mcp.py`, the homelab fork, and `discover-worker.py`.

## Scope

- KQL injection through tool arguments
- Subprocess invocation safety
- Local file integrity (learned store, discovery queue, amendments log)
- Secret handling (`bzrk` bearer token, Hermes `ALERT_SECRET`)
- Trust boundary between the MCP server and the discovery worker
- Operational exception handling and log poisoning

## Verdict

**Generally tight.** Fixed KQL strings throughout; the two free-text vectors
(`logs_for_service`-style service names, `claude_search` term) are guarded with
allowlists that block single-quote injection into KQL string literals. No
`shell=True`, no `eval`, no secrets in code, and atomic writes for the JSON
stores.

The findings below cover defense-in-depth gaps, not active vulnerabilities.

---

## Findings

### M1 — Worker re-validates source name before KQL interpolation

`discover-worker.py:build_query()` reads `source` and `kind` from
`discovery_queue.json` and interpolates them into KQL via `.format(source=…)`.
Inputs to the queue are validated by `request_discovery` in the MCP, but the
worker is a separate process that trusts the queue file. If a future code path
adds queue entries without going through `request_discovery`, or if filesystem
permissions on the queue file regress, an attacker with local write access
could inject arbitrary KQL.

KQL is read-only against Berserk, so this cannot mutate data — but it can read
anything the configured Berserk profile is authorized to see.

**Fix applied:** worker now validates `source` against `^[A-Za-z0-9._-]+$` and
`kind` against `{"service", "metric"}` inside `build_query()`. A failing job
is marked `failed` with the rejection reason and skipped — the drain run
continues.

### M2 — Hermes webhook secret file permissions

`discover-worker.py:post_discord()` reads `/opt/assistant/discord/.env` for
`ALERT_SECRET`. If that file isn't `chmod 600` owned by `assistant`, any local
user can read the secret and post messages to the team's Discord channel
through the Hermes alert webhook.

**Action:** operational — verify with `ls -la /opt/assistant/discord/.env`
on VM-A.

### M3 — `/opt/assistant/bin/` write permissions

The worker imports `berserk-mcp.py` via `importlib.util.spec_from_file_location`,
which executes the file. Anyone with write access to `/opt/assistant/bin/`
gets code execution as `assistant` on the daily cron run.

**Action:** operational — verify with `ls -la /opt/assistant/bin/`.

### L1 — Silent exception swallowing

`load_json_list()`, `load_learned()`, and `post_discord()` previously caught
bare `Exception` and returned silently. A `PermissionError` or corrupted store
file disappeared without trace.

**Fix applied:** narrowed to specific exceptions (`FileNotFoundError`,
`OSError`, `json.JSONDecodeError`) and now log via stderr. `post_discord`
catches `(subprocess.SubprocessError, OSError)`.

### L3 — Bad-JSON log path could include attacker-controlled text

`log("bad json: " + str(e))` includes the JSON parser's error message, which
contains the unparseable input excerpt. If Hermes captures stderr, an attacker
who can send messages to the MCP can plant data (e.g., ANSI escape sequences)
in logs.

**Fix applied:** log only the exception type, not the message.

### L4 — Unhandled handler exceptions kill the server

`dispatch()` did not catch exceptions from `handle_call()`. A bug in a tool
implementation crashed the whole MCP loop. Not a security issue (Hermes
respawns), but worth fixing for stability.

**Fix applied:** `tools/call` now wraps `handle_call` in `try/except` and
returns an error JSON-RPC response on failure.

---

## Not vulnerabilities (verified safe)

- **`search` and `save_query` accept arbitrary KQL** — by design. KQL is read-only against Berserk; `save_query` verifies before persist.
- **`subprocess.run([BZRK_BIN] + args, …)`** — argv list, no `shell=True`.
- **`sanitize_name()`** — collapses non-`[a-zA-Z0-9_]` to `_`; no path traversal or injection via saved query names.
- **JSON writes** — atomic `os.replace` after `.tmp` write.
- **Secrets** — bearer token lives only in `bzrk`'s 0600 config; this server never reads or stores it.

---

## Lessons / recurring patterns to remember

1. **Cross-process trust boundaries need explicit re-validation.** Even if process A validates an input before writing to a file, process B that reads the file must re-validate before using the input in a sensitive context. This is the M1 pattern.
2. **`except Exception: pass` always wrong.** Narrow to the expected exception types and log the rest. Bare catches hide real bugs and are unrecoverable in production.
3. **Don't log unparsed attacker-controlled text verbatim.** Log the exception type, not the message — log poisoning is easy to overlook.
4. **`importlib.util.spec_from_file_location` executes the loaded file.** Anything that imports another file as a module is implicitly trusting that file's filesystem permissions.
5. **Allowlists, not denylists, for KQL string literal contexts.** Reject everything except `[A-Za-z0-9._-]` (or your domain's safe set). Blocking specific characters always misses something.
