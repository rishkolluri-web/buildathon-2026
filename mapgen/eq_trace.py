#!/usr/bin/env python3
"""Identity/equality trace: for every single-operator `is`, `is not`, `==`, `!=` comparison in
the pinned sqlglot sources, record whether EVERY execution during the test suite compared a
type pair for which identity and equality are interchangeable by construction:

  - None against None, or against a type whose __eq__/__ne__ are object's or a builtin's
    (builtins never compare equal to None)
  - two members of the same Enum class (member values are unique; aliases are the same object)
  - two objects whose types both use object.__eq__/__ne__ (== falls back to identity)
  - two bools (True/False are singletons)

Ints, strings, floats, tuples... are NOT interchangeable (equal values may be distinct objects),
nor is anything with a custom __eq__ (Expression, metaclasses such as _Dialect, reverse_key).

A patch whose only change swaps `is`<->`==` (or `is not`<->`!=`) at a site where every recorded
execution was interchangeable cannot change any comparison result, so the patched program runs
exactly like the original. Sites never executed, or executed with any other type pair, are
unsafe and keep coverage-based selection.

Installed through sitecustomize (EQTRACE_SRC, EQTRACE_OUT set) so spawned worker processes are
traced too; each process writes EQTRACE_OUT/eq-<pid>.json at exit.

  python mapgen/eq_trace.py merge <dir with eq-*.json> <out.json>
"""

import ast
import atexit
import enum
import importlib.abc
import importlib.machinery
import json
import os
import sys

_SAFE_BUILTINS = (int, float, complex, str, bytes, bytearray, tuple, list, dict, set, frozenset, bool, type(None))
_OBJ_EQ, _OBJ_NE = object.__eq__, object.__ne__
_records = {}  # site -> [executions, all_safe]


def _plain_identity(tp):
    return getattr(tp, "__eq__", None) is _OBJ_EQ and getattr(tp, "__ne__", None) is _OBJ_NE


def _builtin_eq(tp):
    # exact builtin types only (subclasses may override __eq__)
    return tp in _SAFE_BUILTINS


def interchangeable(left, right):
    tl, tr = type(left), type(right)
    if left is None or right is None:
        other = right if left is None else left
        to = type(other)
        return other is None or _plain_identity(to) or _builtin_eq(to)
    if tl is tr and isinstance(left, enum.Enum):
        return True
    if tl is bool and tr is bool:
        return True
    return _plain_identity(tl) and _plain_identity(tr)


def _rec(site, left, right, op):
    r = _records.get(site)
    ok = interchangeable(left, right)
    if r is None:
        _records[site] = [1, ok]
    else:
        r[0] += 1
        if not ok:
            r[1] = False
    if op == 0:
        return left is right
    if op == 1:
        return left is not right
    if op == 2:
        return left == right
    return left != right


_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(
            func=ast.Name(id="__br_eq_rec__", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(_OPS[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, rel):
        super().__init__(fullname, path)
        self.rel = rel

    def source_to_code(self, data, path, *, _optimize=-1):
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(_Rewrite(self.rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def exec_module(self, module):
        module.__dict__["__br_eq_rec__"] = _rec
        super().exec_module(module)

    # never read or write bytecode caches: the rewritten code must not leak into __pycache__
    def get_code(self, fullname):
        source = self.get_data(self.path)
        return self.source_to_code(source, self.path)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = os.path.realpath(root)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        parts = fullname.split(".")
        base = os.path.join(self.root, *parts)
        for cand, pkg in ((os.path.join(base, "__init__.py"), True), (base + ".py", False)):
            if os.path.isfile(cand):
                rel = os.path.relpath(cand, self.root)
                spec = importlib.machinery.ModuleSpec(fullname, _Loader(fullname, cand, rel), origin=cand,
                                                      is_package=pkg)
                spec.has_location = True
                if pkg:
                    spec.submodule_search_locations = [os.path.dirname(cand)]
                return spec
        return None


def install():
    root, out = os.environ.get("EQTRACE_SRC"), os.environ.get("EQTRACE_OUT")
    if not root or not out or any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _Finder(root))

    def dump():
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"eq-{os.getpid()}.json"), "w") as f:
            json.dump(_records, f)

    atexit.register(dump)


def merge(indir, outfile):
    total = {}
    for name in sorted(os.listdir(indir)):
        if not name.startswith("eq-"):
            continue
        for site, (n, ok) in json.load(open(os.path.join(indir, name))).items():
            t = total.setdefault(site, [0, True])
            t[0] += n
            t[1] = t[1] and ok
    safe = sorted(s for s, (n, ok) in total.items() if ok and n > 0)
    json.dump({"safe": safe, "observed": len(total)}, open(outfile, "w"))
    print(f"comparison sites observed: {len(total)}; interchangeable on every execution: {len(safe)}")


if __name__ == "__main__":
    if sys.argv[1] == "merge":
        merge(sys.argv[2], sys.argv[3])
