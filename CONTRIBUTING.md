# Contributing

Thanks for taking a look. This project is intentionally small and easy to change —
one Python file, zero dependencies, an offline test suite. Drive-by improvements
are welcome.

If you're not sure whether something fits, **open an issue first** — quick "would
you take a PR for X?" works fine. Better to talk for five minutes than burn an
afternoon on something that won't land.

## Setup

```bash
git clone https://github.com/ssi0202/berserk-mcp
cd berserk-mcp
python tests/test_berserk_mcp.py     # must pass before you touch anything
```

You don't need a live Berserk to develop. The tests stub the `bzrk` CLI and verify
the generated KQL, time defaults, injection guards, JSON-RPC protocol, and the
learning loop offline. To run the server against a real Berserk locally, install
the [`bzrk`](https://berserk.dev) CLI and log in to a profile — see the
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
- **New dependencies.** The single-file, stdlib-only build is the whole story —
  trivially auditable, trivially vendored. If you genuinely need a library, open
  an issue and let's talk it through first.

## Style notes

- **Tool descriptions are narrow and unambiguous.** Cross-reference close cousins
  (per-host vs. per-container) so a small model can disambiguate without context.
- **Annotations are honest.** A tool that only reads gets `readOnlyHint=true`; one
  that doesn't touch the network gets `openWorldHint=false`. Don't lie.
- **Don't store secrets.** The Berserk bearer token lives in `bzrk`'s own 0600
  config; that's intentional. Don't add code that reads, logs, or proxies it.
- **No `print` to stdout from the server.** stdio is the MCP transport — log to
  stderr via `log()`.

## Tests

Every PR runs the suite on Linux + Windows × Python 3.8 / 3.11 / 3.12. Locally:

```bash
python tests/test_berserk_mcp.py     # must stay green
```

New tools should add a locked-string KQL test and a callable test (see the existing
`test_*` methods for templates).

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
