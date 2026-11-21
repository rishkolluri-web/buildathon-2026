"""Post-reset coverage pass (pytest plugin, used by run_coverage.sh with RESET_PASS=1).

Some test classes mutate sqlglot's shared state in setUp/tearDown (at the pinned commit:
TestDialectEntryPoints clears Dialect._classes). Every test that runs after them in the same process then
executes "first time" code paths again (dialect re-registration in Dialect._try_load), so which tests the
normal coverage pass credits with those lines depends on the order tests happened to run in.

This pass finds such methods statically in the collected test classes (a mutating call, store or delete
whose receiver is rooted at a name bound to a sqlglot module or object in the test module) and runs every
one of them before each OTHER test, i.e. every test runs as if it came right after a resetting test.
build_map.py's output of this pass is unioned into the normal map (mapgen/union_map.py): coverage only grows.
Found methods are written to $MAPGEN_OUT/resets-<pid>.json.
"""

import ast
import inspect
import json
import os
import sys
import textwrap

import pytest

_MUTATORS = {"update", "setdefault", "pop", "popitem", "append", "extend", "insert", "remove", "discard",
             "clear", "add", "sort", "reverse", "__setitem__", "__delitem__"}
_METHODS = ("setUp", "tearDown", "setUpClass", "tearDownClass")
_RESETS = []  # (class, method name)


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_sqlglot(obj):
    mod = obj.__name__ if inspect.ismodule(obj) else getattr(obj, "__module__", None)
    return isinstance(mod, str) and (mod == "sqlglot" or mod.startswith("sqlglot."))


def _mutates_sqlglot(fn, glb):
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        recv = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS:
            recv = node.func.value
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            for t in getattr(node, "targets", None) or [node.target]:
                if isinstance(t, (ast.Attribute, ast.Subscript)):
                    recv = t
        if recv is not None:
            name = _root(recv)
            if name and name in glb and _is_sqlglot(glb[name]):
                return True
    return False


def pytest_collection_finish(session):
    seen = set()
    for item in session.items:
        cls = getattr(item, "cls", None)
        if cls is None or cls in seen:
            continue
        seen.add(cls)
        glb = vars(sys.modules.get(cls.__module__, object()))
        for m in _METHODS:
            fn = cls.__dict__.get(m)
            if isinstance(fn, (classmethod, staticmethod)):
                fn = fn.__func__
            if fn is not None and _mutates_sqlglot(fn, glb):
                _RESETS.append((cls, m))
    out = os.environ.get("MAPGEN_OUT")
    if out:
        with open(os.path.join(out, f"resets-{os.getpid()}.json"), "w") as f:
            json.dump([f"{c.__module__}.{c.__qualname__}.{m}" for c, m in _RESETS], f)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    for cls, m in _RESETS:
        if getattr(item, "cls", None) is cls:
            continue
        try:
            if m in ("setUpClass", "tearDownClass"):
                getattr(cls, m)()
            else:
                getattr(cls, m)(cls.__new__(cls))
        except Exception:  # noqa: BLE001 - a reset that cannot run alone simply adds nothing
            pass
