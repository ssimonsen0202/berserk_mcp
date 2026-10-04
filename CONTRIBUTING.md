# Contributing

Thanks for taking a look. This project is intentionally small and easy to change:
focused standard-library-only Python modules and an offline test suite. Drive-by
improvements are welcome.

If you're not sure whether something fits, **open an issue first** — quick "would
you take a PR for X?" works fine. Better to talk for five minutes than burn an
afternoon on something that won't land.

## Setup

```bash
git clone https://github.com/ssimonsen0202/berserk_mcp
cd berserk_mcp
python -m unittest discover -s tests     # must pass before you touch anything
```

You don't need a live Berserk to develop. The tests stub the `bzrk` CLI and verify
the generated KQL, time defaults, injection guards, JSON-RPC protocol, and the
learning loop offline. To run the server against a real Berserk locally, install
the [`bzrk`](https://docs.bzrk.dev) CLI and log in to a profile — see the
[README](README.md#requirements).

## What kinds of contributions land easily

- **A new fixed-query tool.** See the [five-step ritual](README.md#extending--add-a-new-tool-in-five-minutes)
  in the README. The bar: real, verified KQL + a locked test + a narrow description.
- **A sharper tool description.** Tool descriptions are the router — if a model is
  picking the wrong tool, a clearer description is a correctness fix.
- **A worked example** for a stack we don't cover (Kubernetes, ECS, Nomad, app code,
  edge devices, …) — put it under [docs/](docs/) or expand the README examples.
- **Bug fixes**, especially anything subtle around quoting, time windows, or the
  injection guards.

## What we'll push back on

- **Tools without a verified query.** "I think this works" — let's see it return rows
  against a real Berserk first, then we lock it. The whole value is determinism.
- **A growing routing surface.** We try to keep the top-level tool list ≈ 20 items;
  past that, small/cheap models start mis-routing. If a tool only matters to one
  niche, the [learning loop](README.md#self-extending-discovery--learning)
  (`save_query` / `run_saved`) is the right home.
- **Shell-strings and `eval`.** All `bzrk` invocations use `subprocess` with an argv
  list. No `shell=True`, no `eval`, no `os.system`. Free-text inputs need allow-lists
  (see `logs_for_service` for the pattern).
- **New dependencies.** The stdlib-only build is the whole story —
  trivially auditable, trivially vendored. If you genuinely need a library, open
  an issue and let's talk it through first.

## Style notes

- **Tool descriptions are narrow and unambiguous.** Cross-reference close cousins
  (per-host vs. per-container) so a small model can disambiguate without context.
- **Annotations are honest.** A tool that only reads gets `readOnlyHint=true`; one
  that doesn't touch the network gets `openWorldHint=false`. Don't lie.
- **Don't store secrets.** The Berserk bearer token lives in `bzrk`'s own private
  configuration (POSIX mode or Windows ACL); that's intentional. Don't add code
  that reads, logs, or proxies it.
- **Reuse security boundaries.** Filesystem code goes through `_store.py` and
  outbound HTTP goes through `_http.py`. Do not add a local `urlopen`, redirect
  policy, chmod helper, path validator, or atomic-write variant in another module.
- **No `print` to stdout from the server.** stdio is the MCP transport — log to
  stderr via `log()`.

## Tests

Every PR runs the full suite on Linux + Windows × Python 3.11 / 3.12 / 3.13 / 3.14. Any
non-draft PR targeting `main` also gets a CodeRabbit review (config:
`.coderabbit.yaml`), and security-sensitive changes get a manual Codex review
pass.

CodeRabbit's own automatic-review trigger needs 10+ GitHub stars on the repo,
which this project doesn't have yet — reviews still happen, just via an
explicit `@coderabbitai review` comment. A scheduled workflow
(`.github/workflows/coderabbit-review-trigger.yml`, hourly, matching the
account's 1-review/hour plan cap) posts that comment on the oldest open PR
that hasn't been reviewed at its current commit yet, so this doesn't depend
on a human remembering to ask. Once the repo passes the star threshold,
CodeRabbit's own automatic review takes over and this workflow becomes a
no-op (it always checks for an unreviewed commit first).

Locally:

```bash
python tests/test_berserk_mcp.py     # fast focused run, must stay green
python -m unittest discover -s tests # full suite (all test_*.py files) -- run this before opening a PR
```

New tools should add a locked-string KQL test and a callable test (see the existing
`test_*` methods for templates).

Security regressions must be offline and fail before the fix. Loopback
`HTTPServer` instances are allowed for redirect and credential-forwarding tests.
Private-file tests are platform split: POSIX checks modes and Windows checks the
current-user-only DACL. Keep `tests/test_security_invariants.py` green; it enforces
the no-shell/no-eval process contract across tracked Python files.

## Module map

The server code lives in the `berserk_mcp/` package. `berserk_mcp.py` in the
repository root is only a launcher. Edit the module that owns the code. The
list below runs from the lowest layer to the highest. A module may import
only lower layers (earlier in the list).

- `_version`: berserk-mcp version.
- `config`: settings read from the environment, shared state, and small helpers.
- `fencing`: wrap real telemetry as untrusted data and cap what reaches the model.
- `queries`: verified KQL queries and the builders that fill them in.
- `runner`: run bzrk: bounded subprocesses, search, schema and KQL validation.
- `tools`: tool definitions, metadata and the text the model reads about them.
- `learned`: the learned and saved query store, and wiring for sibling modules.
- `httpconfig`: parse and check the HTTP transport settings.
- `doctor`: the --doctor and self_check preflight, and admin commands.
- `handlers.tail`: handlers for the tail and CanonLoom tools.
- `handlers.learning`: handlers for the learning loop, jobs and discovery.
- `handlers.diagnostics`: handlers for diagnostics, model drift and the parser tools.
- `handlers.search`: handlers for query, search, analytics and FinOps tools.
- `handlers.dispatch`: tool-call dispatch with fleet budget, cache and cooldown.
- `server`: JSON-RPC plumbing and the stdio and HTTP transports.
- `cli`: command-line entry point and the scheduled passes.

`berserk_mcp/__init__.py` is a facade. `berserk_mcp.NAME` reads and writes
`NAME` in the module that owns it. Tests use this form. The facade is why
`mock.patch.object(berserk_mcp, "NAME", ...)` changes the value that the
owning module reads.

### Import rule

Inside the package, import a module, not a name:

```python
from berserk_mcp import config as bm_config

bm_config.TABLE  # right: read at call time
```

Never import a name by value (`from berserk_mcp.config import TABLE`). A copy
of the name does not see later changes, and a test patch would miss it.

## Security

If you spot something that looks like a vulnerability, please **don't** open a
public issue. Use GitHub's private vulnerability reporting on the repo
("Security → Report a vulnerability"). See [SECURITY.md](SECURITY.md) for scope.

## Code of Conduct

Be kind, assume good faith, and prioritise the contributor over the contribution.
That's it.

## License

By contributing, you agree your contributions are licensed under the same
[MIT License](LICENSE) as the rest of the project.
