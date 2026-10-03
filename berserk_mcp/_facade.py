"""Make `berserk_mcp.NAME` read and write NAME in the module that owns it.

Tests, scripts and sibling modules use `berserk_mcp.NAME` for names that now
live in submodules. Reads resolve at call time, so a value a submodule changes
at runtime is never stale. Writes, including unittest.mock.patch.object, land
in the owning module, where its readers look.
"""

import importlib
import importlib.util
import sys
import types

# Lowest layer first. A module may import only modules listed before it.
LAYERS = (
    "_version",
    "config",
    "fencing",
    "queries",
    "runner",
    "tools",
    "learned",
    "httpconfig",
    "doctor",
    "handlers.tail",
    "handlers.learning",
    "handlers.diagnostics",
    "handlers.search",
    "handlers.dispatch",
    "server",
    "cli",
)


# (package, name) -> owning module, kept after a delete. mock.patch.object
# restores a non-local attribute by deleting it and setting it again; without
# this memory the second step would land on the package instead of the owner.
_owners = {}


def owner_of(package, name):
    """The loaded submodule of `package` that owns `name`, or None."""
    for layer in LAYERS:
        module = sys.modules.get(f"{package.__name__}.{layer}")
        if module is not None and name in module.__dict__:
            _owners[(package.__name__, name)] = module
            return module
    return _owners.get((package.__name__, name))


class Facade(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        owner = owner_of(self, name)
        if owner is None or name not in owner.__dict__:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return owner.__dict__[name]

    def __setattr__(self, name, value):
        owner = None if name in self.__dict__ else owner_of(self, name)
        if owner is None:
            super().__setattr__(name, value)
        else:
            setattr(owner, name, value)

    def __delattr__(self, name):
        if name in self.__dict__:
            super().__delattr__(name)
            return
        owner = owner_of(self, name)
        if owner is None or name not in owner.__dict__:
            raise AttributeError(name)
        delattr(owner, name)


def install(package):
    """Load every layer that exists, then route package attributes to them."""
    for layer in LAYERS:
        name = f"{package.__name__}.{layer}"
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError as exc:
            # Skip only when the parent package (handlers/) does not exist yet;
            # an import error inside it must not hide every layer under it.
            if exc.name != name.rpartition(".")[0]:
                raise
            spec = None
        if spec is not None:
            importlib.import_module(name)
    package.__class__ = Facade
