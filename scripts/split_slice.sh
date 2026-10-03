#!/bin/bash
# Move one layer out of berserk_mcp/__init__.py and run every gate.
# Dev tool for the v1.37.0 package split; deleted with scripts/split_move.py.
#   scripts/split_slice.sh MODULE
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
MODULE=${1:?usage: scripts/split_slice.sh MODULE}
BASELINE=/Users/ssi/Downloads/projects/berserk-mcp-internal-notes/2026-10-03-split-baseline.json
fail() { echo "SLICE FAILED ($MODULE): $*"; exit 1; }
# /Users/ssi/Downloads is itself a git repo: never run outside the worktree.
[ "$(basename "$(git rev-parse --show-toplevel)")" = "berserk-mcp-server-wt-split" ] || fail "not in the split worktree"
[ "$(git branch --show-current)" = "refactor/package-split" ] || fail "not on refactor/package-split"
# pyproject.toml may be pre-edited: the first handler slice lists berserk_mcp.handlers there.
[ -z "$(git status --porcelain | grep -v '^ M pyproject.toml$')" ] || fail "uncommitted changes; commit or discard them first"
python3 scripts/split_move.py "$MODULE" || fail "split_move refused the move"
ruff check --fix -q berserk_mcp/ >/dev/null 2>&1
ruff format -q berserk_mcp/
ruff check -q . || fail "ruff check"
ruff format --check -q . || fail "ruff format"
python3 scripts/split_move.py --rekey-ledger || fail "ledger re-key"
python3 -m unittest discover -s tests >/dev/null 2>&1 || fail "unit tests"
make -s typecheck >/dev/null || fail "mypy"
semgrep --config .semgrep/ --error --quiet berserk_mcp/ berserk_mcp.py >/dev/null 2>&1 || fail "semgrep"
python3 scripts/split_parity.py | cmp -s - "$BASELINE" || fail "parity with main"
git status --short
echo "SLICE OK: $MODULE"
