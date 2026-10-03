#!/usr/bin/env python3
"""Move the top-level code owned by one module out of berserk_mcp/__init__.py.

Dev tool for the v1.37.0 package split. Delete it, and split_owners.json,
when the split is done.

    python3 scripts/split_move.py MODULE [--dry-run]

MODULE is a value in scripts/split_owners.json (for example `config` or
`handlers.search`). The tool:

1. Takes every top-level statement whose names MODULE owns. A statement that
   binds no name (a call, an `if` that only logs) goes with the statement
   before it.
2. Refuses the move if that code reads a name whose owner is still in
   __init__.py (an upward reference), writes a name it does not own, or if a
   function shadows a name the tool must rewrite.
3. Writes berserk_mcp/<MODULE>.py. A name from an already-moved module is
   read as `bm_<module>.NAME`, never imported by value, so a test that sets
   `berserk_mcp.NAME` (the facade forwards the write) reaches every reader.
4. Removes the code from __init__.py, rewrites the remaining readers to
   `bm_<MODULE>.NAME`, imports the new module, and drops unused imports.
5. Separately, after ruff (`--rekey-ledger`): re-keys and re-fingerprints
   reviewed functions in tests/security_reviews.json. Their AST may differ
   from the last commit only by `bm_*.` prefixes; anything else stops it.
"""

import argparse
import ast
import builtins
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "berserk_mcp"
INIT = PKG / "__init__.py"
OWNERS = Path(__file__).resolve().parent / "split_owners.json"
LEDGER = ROOT / "tests" / "security_reviews.json"
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__"}

DOCS = {
    "config": "Settings read from the environment, shared state, and small helpers.",
    "fencing": "Wrap real telemetry as untrusted data and cap what reaches the model.",
    "queries": "Verified KQL queries and the builders that fill them in.",
    "runner": "Run bzrk: bounded subprocesses, search, schema and KQL validation.",
    "tools": "Tool definitions, metadata and the text the model reads about them.",
    "learned": "The learned and saved query store, and wiring for sibling modules.",
    "httpconfig": "Parse and check the HTTP transport settings.",
    "doctor": "The --doctor and self_check preflight, and admin commands.",
    "handlers.tail": "Handlers for the tail and CanonLoom tools.",
    "handlers.learning": "Handlers for the learning loop, jobs and discovery.",
    "handlers.diagnostics": "Handlers for diagnostics, model drift and the parser tools.",
    "handlers.search": "Handlers for query, search, analytics and FinOps tools.",
    "handlers.dispatch": "Tool-call dispatch with fleet budget, cache and cooldown.",
    "server": "JSON-RPC plumbing and the stdio and HTTP transports.",
    "cli": "Command-line entry point and the scheduled passes.",
}


class SplitError(Exception):
    pass


def alias(module):
    return "bm_" + module.rsplit(".", 1)[-1]


def module_file(module):
    return PKG.joinpath(*module.split(".")).with_suffix(".py")


def import_line(module):
    head, _, last = module.rpartition(".")
    package = "berserk_mcp" + (f".{head}" if head else "")
    return f"from {package} import {last} as {alias(module)}"


# ---------- names a statement binds at module level ----------

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def bound_names(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    out = []

    def visit(node):
        if isinstance(node, (*_SCOPES, *_COMPS)):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id not in out:
            out.append(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name and node.name not in out:
            out.append(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(stmt)
    return out


# ---------- which Name nodes refer to module globals ----------


def _scope_locals(node):
    """Names local to a function, lambda or comprehension scope."""
    names = set()
    declared_global = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None:
                names.add(arg.arg)
        body = node.body if isinstance(node.body, list) else [node.body]
    else:  # comprehension
        for gen in node.generators:
            for n in ast.walk(gen.target):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        return names, declared_global

    def visit(n):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            declared_global.update(n.names)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            for d in n.decorator_list:
                visit(d)
            return
        if isinstance(n, (ast.Lambda, *_COMPS)):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        if isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        for child in ast.iter_child_nodes(n):
            visit(child)

    for stmt in body:
        visit(stmt)
    return names - declared_global, declared_global


class GlobalRefs(ast.NodeVisitor):
    """Collect Name nodes that refer to module globals, and `global` names."""

    def __init__(self):
        self.scopes = []  # list of (kind, names)
        self.refs = []
        self.global_decls = []  # (lineno, name)

    def _is_local(self, name):
        for i, (kind, names) in enumerate(reversed(self.scopes)):
            if kind == "class" and i != 0:
                continue  # class bodies do not enclose nested functions
            if name in names:
                return True
        return False

    def visit_Name(self, node):
        if not self._is_local(node.id):
            self.refs.append(node)

    def visit_Global(self, node):
        for name in node.names:
            self.global_decls.append((node.lineno, name))

    def _visit_function(self, node):
        for d in node.decorator_list:
            self.visit(d)
        a = node.args
        for default in [*a.defaults, *[d for d in a.kw_defaults if d is not None]]:
            self.visit(default)
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None and arg.annotation is not None:
                self.visit(arg.annotation)
        if getattr(node, "returns", None) is not None:
            self.visit(node.returns)
        local, _ = _scope_locals(node)
        self.scopes.append(("function", local))
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(stmt)
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node):
        a = node.args
        for default in [*a.defaults, *[d for d in a.kw_defaults if d is not None]]:
            self.visit(default)
        local, _ = _scope_locals(node)
        self.scopes.append(("function", local))
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node):
        for n in [*node.decorator_list, *node.bases, *[k.value for k in node.keywords]]:
            self.visit(n)
        names = set()
        for stmt in node.body:
            names.update(bound_names(stmt))
        self.scopes.append(("class", names))
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def _visit_comp(self, node):
        # The first iterable is evaluated in the enclosing scope.
        self.visit(node.generators[0].iter)
        local, _ = _scope_locals(node)
        self.scopes.append(("function", local))
        for i, gen in enumerate(node.generators):
            self.visit(gen.target)
            if i:
                self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for field in ("elt", "key", "value"):
            if hasattr(node, field):
                self.visit(getattr(node, field))
        self.scopes.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comp


def global_refs(stmts):
    v = GlobalRefs()
    for stmt in stmts:
        v.visit(stmt)
    return v.refs, v.global_decls


# ---------- source surgery ----------


def segment_start(lines, stmt):
    start = stmt.decorator_list[0].lineno if getattr(stmt, "decorator_list", None) else stmt.lineno
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start


def apply_rewrites(lines, rewrites):
    """rewrites: (lineno, byte_col, byte_end, text). Columns are UTF-8 bytes."""
    by_line = {}
    for lineno, col, end, text in rewrites:
        by_line.setdefault(lineno, []).append((col, end, text))
    out = list(lines)
    for lineno, edits in by_line.items():
        raw = out[lineno - 1].encode("utf-8")
        for col, end, text in sorted(edits, reverse=True):
            raw = raw[:col] + text.encode("utf-8") + raw[end:]
        out[lineno - 1] = raw.decode("utf-8")
    return out


def import_bindings(tree):
    """binding -> import statement text for that one binding."""
    out = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                binding = a.asname or a.name.split(".")[0]
                out[binding] = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
        elif isinstance(stmt, ast.ImportFrom):
            mod = "." * stmt.level + (stmt.module or "")
            for a in stmt.names:
                binding = a.asname or a.name
                out[binding] = f"from {mod} import {a.name}" + (f" as {a.asname}" if a.asname else "")
    return out


def collapse_blank_lines(text):
    return re.sub(r"\n{4,}", "\n\n\n", text)


def load_ledger_fingerprint():
    spec = importlib.util.spec_from_file_location("_ledger", ROOT / "tests" / "test_security_reviews.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.fingerprint_source


# ---------- the move ----------


def _is_facade_install(stmt):
    """`_facade.install(...)` always stays at the end of __init__.py."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "_facade"
    )


def plan_move(target, owners, source):  # noqa: C901 -- a one-off dev tool, kept linear on purpose
    tree = ast.parse(source)
    lines = source.splitlines()
    imports = import_bindings(tree)
    moved_modules = {m for m in set(owners.values()) if module_file(m).exists()}
    if target in moved_modules:
        raise SplitError(f"{module_file(target)} already exists")

    body = [s for s in tree.body if not isinstance(s, (ast.Import, ast.ImportFrom)) and not _is_facade_install(s)]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # module docstring stays with the facade
    stmts = []  # (stmt, owner)
    prev = None
    for stmt in body:
        names = bound_names(stmt)
        if names:
            found = {owners.get(n) for n in names}
            if None in found:
                missing = [n for n in names if n not in owners]
                raise SplitError(f"line {stmt.lineno}: no owner for {missing}; add them to {OWNERS.name}")
            if len(found) > 1:
                raise SplitError(f"line {stmt.lineno}: one statement binds names of {sorted(found)}")
            prev = found.pop()
        if prev is None:
            raise SplitError(f"line {stmt.lineno}: nameless statement before any owned statement")
        stmts.append((stmt, prev))

    selected = [s for s, o in stmts if o == target]
    remaining = [s for s, o in stmts if o != target]
    if not selected:
        raise SplitError(f"nothing in __init__.py belongs to {target}")
    moved = set()
    for s in selected:
        moved.update(bound_names(s))

    # Code that moves.
    sel_refs, sel_globals = global_refs(selected)
    rewrites, needed_imports, needed_modules, errors = [], set(), set(), []
    for lineno, name in sel_globals:
        if name not in moved:
            errors.append(f"line {lineno}: moved code declares `global {name}`, owned by {owners.get(name)}")
    for node in sel_refs:
        name = node.id
        if name in moved or name in BUILTINS:
            continue
        if name in imports:
            needed_imports.add(name)
            continue
        owner = owners.get(name)
        if owner is None:
            errors.append(f"line {node.lineno}: moved code reads unknown global {name}")
        elif owner in moved_modules:
            if not isinstance(node.ctx, ast.Load):
                errors.append(f"line {node.lineno}: moved code writes {owner}.{name}")
            rewrites.append((node.lineno, node.col_offset, node.end_col_offset, f"{alias(owner)}.{name}"))
            needed_modules.add(owner)
        else:
            errors.append(f"line {node.lineno}: moved code reads {name}, owned by {owner} (not moved yet)")

    # Code that stays.
    rem_refs, rem_globals = global_refs(remaining)
    for lineno, name in rem_globals:
        if name in moved:
            errors.append(f"line {lineno}: `global {name}` stays in __init__.py; write {alias(target)}.{name} instead")
    for node in rem_refs:
        if node.id in moved:
            if not isinstance(node.ctx, ast.Load):
                errors.append(f"line {node.lineno}: __init__.py writes {node.id}; write {alias(target)}.{node.id}")
            rewrites.append((node.lineno, node.col_offset, node.end_col_offset, f"{alias(target)}.{node.id}"))

    # Shadowing: a function-local name equal to a rewritten global.
    rewritten = moved | {n.id for n in sel_refs if owners.get(n.id) in moved_modules}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            local, _ = _scope_locals(fn)
            clash = (local & rewritten) - {"_"}
            if clash:
                errors.append(f"line {fn.lineno}: local name(s) {sorted(clash)} shadow globals being rewritten")
    if errors:
        raise SplitError("refusing the move:\n  " + "\n  ".join(sorted(set(errors))))

    new_lines = apply_rewrites(lines, rewrites)
    spans = [(segment_start(lines, s), s.end_lineno) for s in selected]
    return {
        "tree": tree,
        "imports": imports,
        "new_lines": new_lines,
        "spans": spans,
        "moved": moved,
        "selected": selected,
        "needed_imports": needed_imports,
        "needed_modules": needed_modules,
    }


def render_module(target, plan):
    new_lines = plan["new_lines"]
    chunks = ["\n".join(new_lines[a - 1 : b]) for a, b in plan["spans"]]
    imports = plan["imports"]
    import_text = [imports[b] for b in sorted(plan["needed_imports"], key=lambda b: imports[b])]
    pkg_text = [import_line(m) for m in sorted(plan["needed_modules"])]
    head = f'"""{DOCS[target]}\n\nSplit out of berserk_mcp.py in v1.37.0.\n"""\n\n'
    imports_block = "\n".join(import_text + pkg_text)
    return head + (imports_block + "\n\n\n" if imports_block else "") + "\n\n\n".join(chunks) + "\n"


def render_init(target, plan):
    new_lines = list(plan["new_lines"])
    drop = set()
    for a, b in plan["spans"]:
        drop.update(range(a, b + 1))
    kept = [line for i, line in enumerate(new_lines, 1) if i not in drop]
    text = collapse_blank_lines("\n".join(kept) + "\n")

    # Import the new module after the last top-level import.
    tree = ast.parse(text)
    last_import = max(s.end_lineno for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom)))
    lines = text.splitlines()
    lines.insert(last_import, import_line(target))
    text = "\n".join(lines) + "\n"

    # Drop import bindings nothing reads any more.
    tree = ast.parse(text)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    lines = text.splitlines()
    for stmt in reversed([s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))]):
        keep = [
            a for a in stmt.names if (a.asname or a.name.split(".")[0]) in used or a.name in ("_facade", "__version__")
        ]
        if len(keep) == len(stmt.names):
            continue
        if isinstance(stmt, ast.Import):
            new = [f"import {a.name}" + (f" as {a.asname}" if a.asname else "") for a in keep]
        else:
            mod = "." * stmt.level + (stmt.module or "")
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep)
            new = [f"from {mod} import {names}"] if keep else []
        lines[stmt.lineno - 1 : stmt.end_lineno] = new
    text = "\n".join(lines) + "\n"
    # The facade re-exports the version; spell it so ruff sees a re-export.
    return text.replace(
        "from berserk_mcp._version import __version__\n",
        "from berserk_mcp._version import __version__ as __version__\n",
    )


def _local_names(func):
    """Names bound inside the function: parameters, assignments, imports, nested defs."""
    bound = {a.arg for a in ast.walk(func.args) if isinstance(a, ast.arg)}
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not func:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


class _StripPrefixes(ast.NodeTransformer):
    """bm_<owner>.NAME -> NAME, only when bm_<owner> is NAME's owner alias and not a local."""

    def __init__(self, owners, local):
        self.owners = owners
        self.local = local

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.value, ast.Name)
            and node.attr in self.owners
            and node.value.id == alias(self.owners[node.attr])
            and node.value.id not in self.local
        ):
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _function_shape(source, name, owners):
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.dump(_StripPrefixes(owners, _local_names(node)).visit(node), include_attributes=False)
    return None


def _git_show(ref, path):
    import subprocess

    rel = path.relative_to(ROOT).as_posix()
    result = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _current_file(owners, name):
    owned = owners.get(name)
    if owned and module_file(owned).exists():
        return f"berserk_mcp.{owned}", module_file(owned)
    return "berserk_mcp", INIT


def rekey_ledger(owners, ref="HEAD"):
    """Re-key and re-fingerprint reviewed functions changed only by the split.

    Run after ruff. A function counts as unchanged when its AST, with bm_*
    module prefixes removed, equals the one committed at `ref`. Any other
    change stops here: it needs a real review, not a re-key."""
    fingerprint_source = load_ledger_fingerprint()
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    out, changes = {}, []
    for key, entry in ledger.items():
        module, name = key.split(":")
        if module != "berserk_mcp" and not module.startswith("berserk_mcp."):
            out[key] = entry
            continue
        new_module, new_path = _current_file(owners, name)
        new_source = new_path.read_text(encoding="utf-8")
        old_path = INIT if module == "berserk_mcp" else module_file(module.split(".", 1)[1])
        old_source = _git_show(ref, old_path)
        if old_source is None or fingerprint_source(old_source, name) != entry["fingerprint"]:
            raise SplitError(f"{key}: the ledger does not match {ref}; fix that before the split")
        if _function_shape(new_source, name, owners) != _function_shape(old_source, name, owners):
            raise SplitError(f"{key}: the code changed beyond bm_* prefixes; this needs a real review")
        new_key = f"{new_module}:{name}"
        new_fp = fingerprint_source(new_source, name)
        if new_key != key or new_fp != entry["fingerprint"]:
            entry = dict(entry, fingerprint=new_fp)
            marker = "scripts/split_move.py"
            if marker not in entry["note"]:
                entry["note"] = (
                    entry["note"].rstrip() + " Package split (v1.37.0): moved or re-prefixed by"
                    f" {marker}; the AST is unchanged apart from bm_* module prefixes (checked by"
                    " the tool). PENDING re-review in the package-split Codex Security reviews."
                )
            changes.append(f"{key} -> {new_key} ({new_fp})")
        out[new_key] = entry
    LEDGER.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("module", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rekey-ledger", action="store_true", help="after ruff: update tests/security_reviews.json")
    args = ap.parse_args(argv)
    owners = json.loads(OWNERS.read_text(encoding="utf-8"))
    if args.rekey_ledger:
        for change in rekey_ledger(owners):
            print(f"ledger {change}")
        return 0
    if args.module not in set(owners.values()):
        raise SplitError(f"{args.module} is not a module in {OWNERS.name}")
    source = INIT.read_text(encoding="utf-8")
    plan = plan_move(args.module, owners, source)
    module_text = render_module(args.module, plan)
    init_text = render_init(args.module, plan)
    print(f"{args.module}: {len(plan['moved'])} names, {module_text.count(chr(10))} lines")
    if args.dry_run:
        return 0
    path = module_file(args.module)
    path.parent.mkdir(exist_ok=True)
    if path.parent != PKG and not (path.parent / "__init__.py").exists():
        (path.parent / "__init__.py").write_text(
            '"""Tool-call handlers, one module per tool group."""\n', encoding="utf-8"
        )
    path.write_text(module_text, encoding="utf-8")
    INIT.write_text(init_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SplitError as exc:
        print(f"split_move: {exc}", file=sys.stderr)
        sys.exit(2)
