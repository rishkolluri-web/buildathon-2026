"""Blast Radius selector: static change analysis + a coverage map of sqlglot's own tests.

For every patch in /data/commits it decides which tests can observe the change:

  * data files (SQL fixtures, ...): the tests that opened the file (map.opens)
  * Python files: the patch is applied to the pinned source, both versions are
    parsed, and the smallest differing statements are located.
      - statements inside functions/lambdas -> tests whose coverage hit those lines
      - class-level data (dict entries, constants, arg_types, ...) -> tests that
        instantiated that class or a subclass (map.users)
      - comment/whitespace/docstring-only edits -> AST identical -> nothing
      - syntax errors, module-level edits, config files -> every test (safe)
Then it packs the chosen tests onto 1..8 workers (longest first) and keeps the
cheapest: busiest worker + 2 s per worker.

The map is built only from the pinned sqlglot test suite (mapgen/); no sample
answers are used. Logs go to stderr; stdout holds one JSON line per commit.
"""

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", HERE / "map.json.gz"))
EQ = Path(os.environ.get("BR_EQ", HERE / "eqsites.json"))
# Static indexes over the pinned sources (family_dependents, import_uses, derived/non-data
# names), written at image build time by `selector.py --build-index` from the same BR_SRC.
# Missing or unreadable -> computed lazily at runtime exactly as before.
IDX = Path(os.environ.get("BR_IDX", HERE / "static_index.pkl"))
# Per-test value trace of the pinned suite (mapgen/vtrace.py): class-dict entry reads and
# relational comparison outcomes.
VMAP = Path(os.environ.get("BR_VMAP", HERE / "vmap.json.gz"))
# Import closure of the slowest tests, each run alone (mapgen/heavy_closure.py): sqlglot modules
# imported by their collection or execution, in any process.
HEAVY = Path(os.environ.get("BR_HEAVY", HERE / "heavy.json"))
# Import-time lines executed by each test's own isolated run (mapgen/iso_imp.py).
ISO = Path(os.environ.get("BR_ISO", HERE / "iso_imp.json.gz"))

WORKER_MS = 2000
MAX_WORKERS = 8
GLOBAL_CONFIG = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py", "tests/conftest.py"}
ALL = object()  # sentinel: select every test


class TrimmedAll:
    """Every test except `exclude`: slow tests that provably never import the changed module(s)."""

    def __init__(self, exclude):
        self.exclude = exclude


# calls that can mutate an object they did not create (import-time side effects on shared state)
_MUTATORS = {"update", "setdefault", "pop", "popitem", "append", "extend", "insert", "remove", "discard",
             "add", "clear", "sort", "reverse", "__setitem__", "__delitem__", "__setattr__", "__delattr__",
             "register", "difference_update", "intersection_update", "symmetric_difference_update"}
_DYNAMIC = {"setattr", "delattr", "exec", "eval", "globals", "vars", "__import__", "compile", "locals"}


def _norm(name):
    return name.lower().replace("_", "")


def mutation_sites(tree):
    """Multiset of what this code can mutate, position-free: targets of attribute/subscript stores and
    deletes, receivers+names of mutating method calls, dynamic/registration calls, global/nonlocal.
    Equal for old and new code <=> the edit changed only values flowing into the same targets."""
    sites = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            sites.append(ast.dump(n))
        elif isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            sites.append(ast.dump(n))
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and (f.attr in _MUTATORS or f.attr.startswith("register")
                                                 or f.attr in ("import_module", "__import__")):
                sites.append(ast.dump(f))
            elif isinstance(f, ast.Name) and (f.id in _DYNAMIC or f.id.startswith("register")):
                sites.append(ast.dump(f))
    return sorted(sites)


def shared_mutation_keys(fn):
    """In-place mutations in function `fn` of a container reached through an attribute (x.Y.pop(..),
    x.Y[k] = .., x.Y.update(..)): such a container may be inherited from a base class and shared. Returns
    [(container attribute names, value keys)] per mutation: the value keys are attribute names / string
    constants whose value flows into it (data or control dependence, intra-procedural, source-order
    reaching definitions). Containers the function itself assigned
    (X.Y = {...}) and containers reached only by subscripting a local name are private."""
    loops = [(n.lineno, n.end_lineno) for n in ast.walk(fn) if isinstance(n, (ast.For, ast.AsyncFor, ast.While))]
    defs = {}
    for n in ast.walk(fn):
        pairs = []
        if isinstance(n, ast.Assign):
            pairs = [(t, n.value) for t in n.targets]
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
            pairs = [(n.target, n.value)]
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            pairs = [(n.target, n.iter)]
        elif isinstance(n, ast.NamedExpr):
            pairs = [(n.target, n.value)]
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            pairs = [(i.optional_vars, i.context_expr) for i in n.items if i.optional_vars is not None]
        for tgt, val in pairs:
            stack = [tgt]
            while stack:  # names rebound by the target (not x in `x.attr =` / `x[k] =`)
                t = stack.pop()
                if isinstance(t, ast.Name):
                    defs.setdefault(t.id, []).append((getattr(val, "lineno", 0), val))
                elif isinstance(t, (ast.Tuple, ast.List)):
                    stack.extend(t.elts)
                elif isinstance(t, ast.Starred):
                    stack.append(t.value)

    def keys_of(expr, line, seen):
        ks = set()
        for n in ast.walk(expr):
            if isinstance(n, ast.Attribute):
                ks.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                ks.add(n.value)
            elif isinstance(n, ast.Name) and (n.id, line) not in seen:
                seen.add((n.id, line))
                for dl, val in defs.get(n.id, ()):
                    if dl < line or any(a <= line <= b and a <= dl <= b for a, b in loops):
                        ks |= keys_of(val, getattr(val, "lineno", line), seen)
        return ks

    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    fresh = {ast.unparse(t) for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AnnAssign))
             for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Attribute)}
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS:
            recv, data = n.func.value, [*n.args, *[k.value for k in n.keywords]]
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
            recv, data = n.value, [n.slice]
        else:
            continue
        chain, r = [], recv
        while isinstance(r, (ast.Attribute, ast.Subscript)):
            chain.append(r)
            r = r.value
        if not any(isinstance(c, ast.Attribute) for c in chain) or ast.unparse(recv) in fresh:
            continue  # local container, or one this function created
        line = n.lineno
        seen = set()
        values = set()
        for e in data:
            values |= keys_of(e, line, seen)
        p = parents.get(n)
        while p is not None and p is not fn:  # control dependence on enclosing conditions / loops
            if isinstance(p, (ast.If, ast.While, ast.IfExp)):
                values |= keys_of(p.test, line, seen)
            elif isinstance(p, (ast.For, ast.AsyncFor)):
                values |= keys_of(p.iter, line, seen)
            p = parents.get(p)
        out.append(({c.attr for c in chain if isinstance(c, ast.Attribute)}, values))
    return out


def import_time_mutations(tree, imp_quals, dialect_keys):
    """True if code that runs while this module is imported could change state other modules see:
    stores/deletes into attributes or subscripts, mutating method calls, dynamic setattr/exec...,
    global/nonlocal, or a class whose name could replace a registered dialect (_Dialect registers
    every subclass under its lower-cased name). Walks module and class bodies, decorators, defaults,
    and every function body that runs at import (`imp_quals`, or called by name from import-time
    code, transitively). Fail-closed: anything unrecognised counts as a mutation."""
    funcs = {}

    def collect(body, prefix):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(st.name, []).append(st)
                funcs.setdefault(prefix + st.name, []).append(st)
            elif isinstance(st, ast.ClassDef):
                collect(st.body, prefix + st.name + ".")
    collect(tree.body, "")
    todo, done = [], set()

    def bad_node(n):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return True
        if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and (f.attr in _MUTATORS or f.attr.startswith("register")):
                return True
            if isinstance(f, ast.Name):
                if f.id in _DYNAMIC or f.id.startswith("register"):
                    return True
                if f.id in funcs:
                    todo.append(f.id)
            if isinstance(f, ast.Attribute) and f.attr in ("import_module", "__import__"):
                return True
        if isinstance(n, ast.ClassDef) and (_norm(n.name) in dialect_keys or _norm(n.name) == "dialect"):
            return True
        return False

    def walk_import_time(node):
        """Nodes evaluated at import: everything except function (and lambda) bodies."""
        stack = [node]
        while stack:
            n = stack.pop()
            if bad_node(n):
                return True
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.extend(n.decorator_list)
                stack.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
                continue
            if isinstance(n, ast.Lambda):
                stack.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
                continue
            stack.extend(ast.iter_child_nodes(n))
        return False

    if walk_import_time(tree):
        return True
    todo.extend(q for q in imp_quals if q in funcs)
    while todo:
        q = todo.pop()
        if q in done:
            continue
        done.add(q)
        for fn in funcs[q]:
            if any(bad_node(n) for n in ast.walk(fn)):
                return True
    return False
# Ablation switches for measuring each safety rule (never set in the image):
# BR_OFF=imp,hdrimp,refs,classread,family,classhdr,argshdr,fixdir,fill
OFF = set(filter(None, os.environ.get("BR_OFF", "").split(",")))


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------------------- map


class Map:
    def __init__(self, path, tests):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            m = json.load(f)
        self.raw_sets = m["sets"]
        self.cache = {}
        self.lines = m["lines"]
        self.opens = m["opens"]
        self.touch = m["touch"]
        self.users = m.get("users", {})
        self.imp = {k: set(v) for k, v in m.get("imp", {}).items()}
        self.refs = m.get("refs", {})
        self.imports = m.get("imports", {})
        self.star = m.get("star", {})
        # map bit positions -> tests.json numbers (by id, robust to reordering)
        num_by_id = self.num_by_id = {t["id"]: t["number"] for t in tests}
        self.bit_to_num = [num_by_id.get(tid) for tid in m["tests"]]
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])

    def set(self, idx):
        b = self.cache.get(idx)
        if b is None:
            b = self.cache[idx] = int(self.raw_sets[idx], 16)
        return b

    def nums(self, bits):
        out = set()
        i = 0
        while bits:
            if bits & 1:
                n = self.bit_to_num[i]
                if n is not None:
                    out.add(n)
            bits >>= 1
            i += 1
        return out


# ----------------------------------------------------------------------------- patch


def parse_patch(text):
    """-> list of dicts {old, new, hunks:[(old_start, [(tag, text)])], binary}"""
    files = []
    cur = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None, "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif hunk is None and line.startswith("--- "):
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif hunk is None and line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("new file mode"):
            cur["old"] = None
        elif line.startswith("deleted file mode"):
            cur["new"] = None
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            hunk = [int(m.group(1)), []]
            cur["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append((line[0], line[1:]))
        elif hunk is not None and line == "":
            hunk[1].append((" ", ""))  # some tools drop the space on empty context lines
    return files


def apply_hunks(old_lines, hunks):
    """old_lines: list without newlines. Returns (new_lines, old_touched, new_touched)."""
    new = []
    pos = 0  # index into old_lines
    old_touched, new_touched = set(), set()
    for start, body in hunks:
        # trim trailing phantom context produced by the final newline split
        while body and body[-1] == (" ", "") and (start - 1 + sum(1 for t, _ in body if t != "+")) > len(old_lines):
            body.pop()
        s = max(start - 1, 0) if any(t != "+" for t, _ in body) else start
        new.extend(old_lines[pos:s])
        pos = s
        for tag, txt in body:
            if tag == " ":
                old_touched.add(pos + 1)
                new.append(old_lines[pos] if pos < len(old_lines) else txt)
                new_touched.add(len(new))
                pos += 1
            elif tag == "-":
                old_touched.add(pos + 1)
                pos += 1
            else:
                new.append(txt)
                new_touched.add(len(new))
                old_touched.add(pos)  # insertion point neighbours
                old_touched.add(pos + 1)
    new.extend(old_lines[pos:])
    return new, old_touched, new_touched


# ----------------------------------------------------------------------------- AST diff

BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
COMPOUND = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor,
    ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler, ast.match_case,
) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ()) + ((ast.Match,) if hasattr(ast, "Match") else ())


def first_line(node):
    decs = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decs])


ENUM_BASES = ("Enum", "Flag", "AutoName")


def enum_like_classes(tree):
    """Qualified names of classes that are (or derive, within this file, from) enums.
    Enum member values interact (aliases, auto() numbering), so a member change can be
    observed through reads of OTHER members."""
    out = set()
    defs = {}

    def walk(body, prefix):
        for st in body:
            if isinstance(st, ast.ClassDef):
                q = prefix + st.name
                defs[q] = st
                walk(st.body, q + ".")

    walk(tree.body, "")
    bases_of = {q: [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "" for b in st.bases]
                for q, st in defs.items()}
    auto = {q: any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "auto" for n in ast.walk(st))
            for q, st in defs.items()}  # computed once per class, not once per fixpoint round
    changed = True
    while changed:
        changed = False
        for q in defs:
            if q in out:
                continue
            if auto[q] or any(b.endswith(ENUM_BASES) or b in out for b in bases_of[q]):
                out.add(q)
                changed = True
    return out


def is_stub(fn):
    return all(
        isinstance(b, ast.Pass)
        or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and (b.value.value is Ellipsis or isinstance(b.value.value, str)))
        for b in fn.body
    )


def is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def header_dump(node):
    saved = {}
    for f in BODY_FIELDS:
        if hasattr(node, f):
            saved[f] = getattr(node, f)
            setattr(node, f, [])
    try:
        return ast.dump(node)
    finally:
        for f, v in saved.items():
            setattr(node, f, v)


def header_lines(node):
    start = first_line(node)
    bodies = [s for f in BODY_FIELDS for s in (getattr(node, f, None) or [])]
    end = (min(first_line(s) if not isinstance(s, (ast.ExceptHandler, ast.match_case)) else s.lineno for s in bodies) - 1) if bodies else node.end_lineno
    return set(range(start, max(start, end) + 1))


def span(node):
    return set(range(first_line(node), (node.end_lineno or node.lineno) + 1))


class Differ:
    """Collects regions: ('lines', set) | ('class', qualname) | ('module',)."""

    def __init__(self, old_src, new_src, old_touched, new_touched):
        self.old_src = old_src
        self.new_src = new_src
        self.old_touched = old_touched
        self.new_touched = new_touched
        self.regions = []

    def key(self, node, src, touched):
        a, b = first_line(node), node.end_lineno or node.lineno
        if any(a <= l <= b for l in touched):
            return ("D", ast.dump(node))
        return ("S", "\n".join(src[a - 1:b]))

    def body(self, old, new, ctx, parent):
        import difflib

        ok = [self.key(s, self.old_src, self.old_touched) for s in old]
        nk = [self.key(s, self.new_src, self.new_touched) for s in new]
        if ok == nk:
            return
        sm = difflib.SequenceMatcher(None, ok, nk, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and i2 - i1 == j2 - j1:
                for o, n, a, b in zip(old[i1:i2], new[j1:j2], ok[i1:i2], nk[j1:j2]):
                    if a[0] == "D" and b[0] == "D" and a[1] == b[1]:
                        continue
                    self.node(o, n, ctx)
                continue
            removed, added = old[i1:i2], new[j1:j2]
            if all(is_docstring(s) for s in removed + added):
                continue
            # structural insert/delete
            if ctx[0] == "func":
                lines = set()
                for o in removed:
                    lines |= span(o)
                if added:
                    if i1 > 0:
                        lines |= self.stmt_lines(old[i1 - 1])
                    if i2 < len(old):
                        lines |= self.stmt_lines(old[i2])
                    if parent is not None:
                        lines |= header_lines(parent)
                self.regions.append(("lines", lines))
            elif ctx[0] == "class":
                for o in removed:
                    self.regions.append(("lines", span(o)))
                self.regions.append(("class", ctx[1], list(removed) + list(added)))
            else:
                self.regions.append(("module", list(removed), list(added)))

    def stmt_lines(self, s):
        return header_lines(s) if isinstance(s, COMPOUND) else span(s)

    def node(self, o, n, ctx):
        if is_docstring(o) and is_docstring(n):
            return
        if type(o) is type(n) and isinstance(o, COMPOUND):
            if header_dump(o) == header_dump(n):
                if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub = ("func", None)
                elif isinstance(o, ast.ClassDef):
                    sub = ("class", (ctx[1] + "." if ctx[0] == "class" and ctx[1] else "") + o.name) if ctx[0] != "func" else ctx
                else:
                    sub = ctx
                for f in BODY_FIELDS:
                    ob, nb = getattr(o, f, None) or [], getattr(n, f, None) or []
                    if f == "handlers" or f == "cases":
                        if len(ob) == len(nb):
                            for x, y in zip(ob, nb):
                                self.node(x, y, sub)
                        else:
                            self.unit_change(o, ctx)
                    else:
                        self.body(ob, nb, sub, o)
                return
            # header changed
            if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.regions.append(("lines", span(o)))
                decorators_changed = [ast.dump(d) for d in o.decorator_list] != [ast.dump(d) for d in n.decorator_list]
                if ctx[0] == "class" and ("classhdr" not in OFF or o.name != n.name) and (
                    o.name != n.name or decorators_changed or "argshdr" in OFF
                ):
                    # a rename or decorator change alters the class itself (decorators run
                    # at class creation and may register the method). An args/defaults-only
                    # change matters only when the method is called, and every test that
                    # called it under the old code executed its body: span coverage above.
                    self.regions.append(("class", ctx[1]))
                elif ctx[0] == "module" and o.name != n.name:
                    self.regions.append(("module", [o], [n]))
                return
            if isinstance(o, ast.ClassDef):
                qual = (ctx[1] + "." if ctx[0] == "class" and ctx[1] else "") + o.name
                self.regions.append(("class", qual) if ctx[0] != "func" else ("lines", span(o)))
                if o.name != n.name and ctx[0] == "module":
                    self.regions.append(("module", [o], [n]))
                return
            if ctx[0] == "func":
                self.regions.append(("lines", header_lines(o)))
                return
            self.unit_change(o, ctx)
            return
        if ctx[0] == "func":
            self.regions.append(("lines", span(o)))
            return
        # import-time statement in a class body / module: is the change only inside lambdas?
        lam = lambda_only_diff(o, n)
        if lam is not None:
            # lambda bodies run when called, never at import: plain coverage applies
            self.regions.append(("lambda", lam))
            return
        self.unit_change(o, ctx, n)

    def unit_change(self, o, ctx, n=None):
        if ctx[0] == "class":
            # 4th element marks a true old/new statement pair (not removed+added lists)
            self.regions.append(("class", ctx[1], [o, n] if n is not None else None, "pair" if n is not None else None))
        elif ctx[0] == "func":
            self.regions.append(("lines", span(o)))
        else:
            self.regions.append(("module", [o], [n] if n is not None else None))


def lambda_only_diff(o, n):
    """If o and n differ only inside lambda bodies, return the old lines of those lambdas."""
    lines = set()

    def walk(a, b, in_lambda):
        if type(a) is not type(b):
            if in_lambda is None:
                return False
            lines.update(span(in_lambda))
            return True
        if isinstance(a, ast.AST):
            lam = a if isinstance(a, ast.Lambda) else in_lambda
            for f in a._fields:
                if not walk(getattr(a, f, None), getattr(b, f, None), lam):
                    return False
            return True
        if isinstance(a, list):
            if len(a) != len(b):
                if in_lambda is None:
                    return False
                lines.update(span(in_lambda))
                return True
            return all(walk(x, y, in_lambda) for x, y in zip(a, b))
        if a != b:
            if in_lambda is None:
                return False
            lines.update(span(in_lambda))
        return True

    return lines if walk(o, n, None) and lines else None


# ----------------------------------------------------------------------------- selection


def bound_names(stmt):
    out = set()
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            out.add((a.asname or a.name).split(".")[0])
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif not isinstance(n, (ast.Tuple, ast.List, ast.Starred, ast.Load, ast.Store)):
                out.add("__complex_target__")
    return out


def name_of(st):
    """The single name a module-level def/class/simple assignment binds, else None."""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return st.name
    b = bound_names(st) if isinstance(st, (ast.Assign, ast.AnnAssign)) else set()
    return next(iter(b)) if len(b) == 1 else None


def module_name(path):
    p = path[:-3].replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


_SWAPS = ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq})


_SWAP_NORM = re.compile(r"\bis\s+not\b|!=|\bis\b|==")


def swap_only_hunks(hunks):
    """Cheap prefilter: every removed line pairs with an added line that differs only by
    is/== or is not/!= (the AST check below is the real test)."""
    norm = lambda t: _SWAP_NORM.sub(lambda m: "!=" if m.group(0) in ("!=",) or m.group(0).startswith("is") and "not" in m.group(0) else "==", t)  # noqa: E731
    for _, lines in hunks:
        rem = [t for tag, t in lines if tag == "-"]
        add = [t for tag, t in lines if tag == "+"]
        if not rem or len(rem) != len(add) or any(norm(x) != norm(y) for x, y in zip(rem, add)):
            return False
    return True


def swap_only_sites(old_tree, new_tree, touched=None):
    """["line:col", ...] of single-operator comparisons whose operator is swapped between
    is/== or is not/!=, if that is the ONLY difference between the two trees; else None.

    One parallel walk over both trees (no copies). `touched`: lines a hunk touched in either
    version; only valid when every hunk removes as many lines as it adds (swap_only_hunks),
    so line numbers coincide. A statement at the same position in both trees whose lines
    are all untouched has identical source text, hence an identical subtree: skipped."""
    sites = []
    skip_ok = touched is not None

    def same(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            if skip_ok and isinstance(a, ast.stmt):
                s = first_line(a)
                if (s == first_line(b) and a.end_lineno == b.end_lineno and a.col_offset == b.col_offset
                        and a.end_col_offset == b.end_col_offset
                        and not any(s <= ln <= a.end_lineno for ln in touched)):
                    return True
            if isinstance(a, ast.Compare):
                ta, tb = [type(o) for o in a.ops], [type(o) for o in b.ops]
                if ta != tb:
                    if len(ta) != 1 or len(tb) != 1 or {ta[0], tb[0]} not in _SWAPS:
                        return False
                    sites.append(f"{a.lineno}:{a.col_offset}")
                return same(a.left, b.left) and same(a.comparators, b.comparators)
            for f in a._fields:
                if not same(getattr(a, f, None), getattr(b, f, None)):
                    return False
            return True
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    if not same(old_tree, new_tree):
        return None
    return sites or None


_OP_NORM = re.compile(r"\bis\s+not\b|\bis\b|<=|>=|==|!=|<|>")
_REL_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_CMP_OPS = {ast.Lt: "Lt", ast.LtE: "LtE", ast.Gt: "Gt", ast.GtE: "GtE", ast.Eq: "Eq", ast.NotEq: "NotEq"}


def op_only_hunks(hunks):
    """Cheap prefilter: every hunk removes as many lines as it adds and each removed line pairs
    with an added line that differs only in comparison operators (the AST check is the real test)."""
    for _, lines in hunks:
        rem = [t for tag, t in lines if tag == "-"]
        add = [t for tag, t in lines if tag == "+"]
        if not rem or len(rem) != len(add) or any(_OP_NORM.sub("@", x) != _OP_NORM.sub("@", y) for x, y in zip(rem, add)):
            return False
    return True


def op_change_sites(old_tree, new_tree, touched):
    """[("line:col", old op type, new op type)] of single-operator comparisons whose operator
    differs, if that is the ONLY difference between the trees; else None. Same parallel walk
    and untouched-statement skip as swap_only_sites (requires op_only_hunks)."""
    sites = []

    def same(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            if isinstance(a, ast.stmt):
                s = first_line(a)
                if (s == first_line(b) and a.end_lineno == b.end_lineno and a.col_offset == b.col_offset
                        and a.end_col_offset == b.end_col_offset
                        and not any(s <= ln <= a.end_lineno for ln in touched)):
                    return True
            if isinstance(a, ast.Compare):
                ta, tb = [type(o) for o in a.ops], [type(o) for o in b.ops]
                if ta != tb:
                    if len(ta) != 1 or len(tb) != 1:
                        return False
                    sites.append((f"{a.lineno}:{a.col_offset}", ta[0], tb[0]))
                return same(a.left, b.left) and same(a.comparators, b.comparators)
            for f in a._fields:
                if not same(getattr(a, f, None), getattr(b, f, None)):
                    return False
            return True
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    if not same(old_tree, new_tree):
        return None
    return sites or None


_UPPER_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_CV_OPS = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge", ast.Eq: "eq", ast.NotEq: "ne"}


def balanced_hunks(hunks):
    """Every hunk removes as many lines as it adds (line numbers coincide outside hunks)."""
    for _, lines in hunks:
        if sum(1 for t, _ in lines if t == "-") != sum(1 for t, _ in lines if t == "+"):
            return False
    return True


def const_change_sites(old_tree, new_tree, touched):
    """[("line:col" of the Compare, op type, constant side 0/1, old value, new value)] if the ONLY
    differences between the trees are int/str constants that are the single constant operand of a
    single-operator ==, !=, <, <=, >, >= comparison (same type before and after); else None."""
    sites = []

    def const(x):
        return isinstance(x, ast.Constant) and type(x.value) in (int, str)

    def same(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            if isinstance(a, ast.stmt):
                st = first_line(a)
                if (st == first_line(b) and a.end_lineno == b.end_lineno and a.col_offset == b.col_offset
                        and a.end_col_offset == b.end_col_offset and not any(st <= ln <= a.end_lineno for ln in touched)):
                    return True
            if isinstance(a, ast.Compare) and len(a.ops) == 1 and type(a.ops[0]) in _CV_OPS \
                    and [type(o) for o in a.ops] == [type(o) for o in b.ops]:
                sa, sb = [a.left, a.comparators[0]], [b.left, b.comparators[0]]
                ca = [i for i, x in enumerate(sa) if const(x)]
                if len(ca) == 1 and [i for i, x in enumerate(sb) if const(x)] == ca:
                    i = ca[0]
                    x, y = sa[i], sb[i]
                    if type(x.value) is type(y.value) and x.value != y.value and x.kind == y.kind:
                        sites.append((f"{a.lineno}:{a.col_offset}", type(a.ops[0]), i, x.value, y.value))
                        return same(sa[1 - i], sb[1 - i])
            if isinstance(a, ast.Constant):
                return a.value == b.value and type(a.value) is type(b.value) and a.kind == b.kind
            for f in a._fields:
                if not same(getattr(a, f, None), getattr(b, f, None)):
                    return False
            return True
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    if not same(old_tree, new_tree):
        return None
    return sites or None


def cmp_result(op, v, c, cside):
    import operator

    f = getattr(operator, _CV_OPS[op])
    return f(c, v) if cside == 0 else f(v, c)


# expression nodes whose evaluation cannot raise or run code when only Constant leaves change
_INERT_PARENTS = (ast.Tuple, ast.List, ast.Set, ast.Dict)
_ACTIVE = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.ListComp, ast.SetComp,
           ast.DictComp, ast.GeneratorExp, ast.Starred)


_INERT_ARGS = (ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Load)


def is_closure_factory(fn, lazy_annotations=False):
    """def f(...): [docstring] def g(...): ...  return g   (or: return lambda ...: ...)
    Calling it binds the arguments and returns a new function; nothing else runs and nothing can
    raise (inner defs/lambdas without defaults or decorators: no expression is evaluated;
    annotations only under `from __future__ import annotations`, which never evaluates them)."""
    body = [b for b in fn.body if not is_docstring(b)]
    if not body or not isinstance(body[-1], ast.Return) or fn.decorator_list:
        return False
    ret = body[-1].value
    if isinstance(ret, ast.Lambda):
        return len(body) == 1 and not ret.args.defaults and not any(ret.args.kw_defaults)
    inner = body[:-1]
    return (bool(inner) and isinstance(ret, ast.Name)
            and all(isinstance(d, ast.FunctionDef) and not d.decorator_list and not d.args.defaults
                    and not any(d.args.kw_defaults) and (lazy_annotations or (not d.returns and not any(
                        a.annotation for a in d.args.args + d.args.kwonlyargs + d.args.posonlyargs
                        + [x for x in (d.args.vararg, d.args.kwarg) if x])))
                    for d in inner)
            and ret.id in {d.name for d in inner})


def const_leaf_diff(a, b, factory=lambda name: False):
    """True if a and b (class-body entry values) differ only in Constant leaves that sit directly
    in a tuple/list/set/dict display, are the value itself, are a numeric operand of unary -/+, are
    an argument of a call to a closure factory (`factory(name)`; arguments plain constants, names,
    attributes or displays), or sit anywhere inside a lambda body (runs only when called); and
    neither value contains other code that runs at class creation (calls, comprehensions...)."""
    for v in (a, b):
        stack = [v]
        while stack:
            n = stack.pop()
            if isinstance(n, ast.Lambda):
                stack.append(n.args)  # defaults are evaluated at class creation; only the body waits
                continue
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and factory(n.func.id) and all(
                    isinstance(x, _INERT_ARGS) for arg in n.args + [k.value for k in n.keywords] for x in ast.walk(arg)):
                continue  # bound into a closure; runs only when a test calls the entry
            if isinstance(n, _ACTIVE):
                return False
            stack.extend(ast.iter_child_nodes(n))

    def walk(x, y, parent, in_lambda):
        if type(x) is not type(y):
            return False
        if isinstance(x, ast.Constant):
            if x.value == y.value and type(x.value) is type(y.value):
                return True
            if in_lambda or parent is None or isinstance(parent, _INERT_PARENTS + (ast.keyword,)):
                return True
            if isinstance(parent, ast.Call) and x is not parent.func:
                return True  # argument of an accepted closure-factory call (checked above)
            if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, (ast.USub, ast.UAdd)):
                num = (int, float, complex)
                return isinstance(x.value, num) and isinstance(y.value, num) \
                    and not isinstance(x.value, bool) and not isinstance(y.value, bool)
            return False
        if isinstance(x, ast.Lambda):  # body runs only when called; defaults run now
            return walk(x.args, y.args, x, in_lambda) and walk(x.body, y.body, x, True)
        if isinstance(x, ast.AST):
            return all(walk(getattr(x, f, None), getattr(y, f, None), x, in_lambda) for f in x._fields)
        if isinstance(x, list):
            return len(x) == len(y) and all(walk(p, q, parent, in_lambda) for p, q in zip(x, y))
        return x == y

    return walk(a, b, None, False)


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.sole = False
        self.trim_lines = []
        try:  # comparison sites where is/== are interchangeable on every traced execution
            self.eq_safe = set(json.loads(EQ.read_text())["safe"]) if "eqswap" not in OFF else set()
        except (OSError, ValueError, KeyError):
            self.eq_safe = set()
        if not OFF:  # ablation switches change import_uses/family results: never use the index then
            self.load_static_index()
        self._vmap = None

    def heavy_closures(self):
        """{test number: set of sqlglot modules its isolated run imports} for the slowest tests."""
        if getattr(self, "_heavy", None) is None:
            self._heavy = {}
            if "heavyall" not in OFF:
                try:
                    c = json.loads(HEAVY.read_text())["closure"]
                    self._heavy = {self.m.num_by_id[t]: set(mods) for t, mods in c.items()
                                   if t in self.m.num_by_id and isinstance(mods, list) and mods}
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    log(f"heavy-test closures unavailable ({e!r})")
        return self._heavy

    def iso_tests(self, path, lines):
        """Tests whose isolated run executes any of `lines` of `path` (import time or runtime), plus every
        test without an isolated run; None when the data is unavailable (callers fall back to ALL)."""
        if getattr(self, "_iso", None) is None:
            self._iso = False
            if "isoimp" not in OFF:
                try:
                    with gzip.open(ISO, "rt", encoding="utf-8") as f:
                        v = json.load(f)
                    known = set(v["tests"]) - set(v["unknown"])
                    self._iso = {
                        "bit_to_num": [self.m.num_by_id.get(t) for t in v["tests"]],
                        "lines": v["lines"], "sets": v["sets"], "cache": {},
                        "missing": {n for t, n in self.m.num_by_id.items() if t not in known},
                    }
                except Exception as e:  # noqa: BLE001 - no data: import-time lines -> ALL as before
                    log(f"isolated import-time coverage unavailable ({e!r})")
        iso = self._iso
        if not iso:
            return None
        bits = 0
        per = iso["lines"].get(path, {})
        for ln in lines:
            idx = per.get(str(ln))
            if idx is not None:
                b = iso["cache"].get(idx)
                if b is None:
                    b = iso["cache"][idx] = int(iso["sets"][idx], 16)
                bits |= b
        out = set(iso["missing"])
        i = 0
        while bits:
            if bits & 1:
                n = iso["bit_to_num"][i]
                if n is not None:
                    out.add(n)
            bits >>= 1
            i += 1
        return out

    def heavy_lines(self):
        """{test number: {path: set of lines}}: every sqlglot line each slow test's isolated run executes
        (collection, runtime and spawned pool children, import time included)."""
        if getattr(self, "_heavy_lines", None) is None:
            self._heavy_lines = {}
            if "heavyall" not in OFF and self.heavy_closures():
                try:
                    raw = json.loads(HEAVY.read_text())["lines"]
                    self._heavy_lines = {
                        self.m.num_by_id[t]: {f: {n for a, b in rs for n in range(a, b + 1)} for f, rs in files.items()}
                        for t, files in raw.items() if t in self.m.num_by_id and files}
                    if set(self._heavy_lines) != set(self.heavy_closures()):
                        self._heavy_lines = {}
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    log(f"heavy-test lines unavailable ({e!r})")
                    self._heavy_lines = {}
        return self._heavy_lines

    def trimmed_all(self, trim, chosen):
        """An ALL fallback caused only by in-place edits of the sqlglot modules in `trim`: drop each
        slow test that (a) the other files' rules did not select, (b) never imports any of those
        modules, in any process, when run alone, and (c) cannot see their import-time effects
        through shared state (no import-time mutation of other objects, no dialect-name clash).
        A module that is never imported cannot run, fail to compile, or change anything else; so
        such a test runs exactly as on the pinned code. Anything unclear -> plain ALL."""
        heavy = self.heavy_closures()
        trees = []
        for path, old_text, hunks in trim:
            mod = module_name(path)
            try:
                old_tree = ast.parse(old_text)
            except SyntaxError:
                return ALL
            imp = self.m.imp.get(path, set())
            imp_quals = set()

            def scan(body, prefix):
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if (span(st) - header_lines(st)) & imp:  # body ran at import (headers always run)
                            imp_quals.add(prefix + st.name)
                    elif isinstance(st, ast.ClassDef):
                        scan(st.body, prefix + st.name + ".")
            scan(old_tree.body, "")
            new_lines = apply_hunks(old_text.split("\n"), hunks)[0]
            try:
                new_tree = ast.parse("\n".join(new_lines))
            except SyntaxError:
                new_tree = None  # never compiles: only the old module's import effects disappear
            trees.append((mod, imp_quals, [t for t in (old_tree, new_tree) if t is not None]))
        exclude = set()
        hl = self.heavy_lines() if self.trim_lines else {}
        for h, clos in heavy.items():
            if h in chosen:
                continue
            if self.trim_lines and (h not in hl or any(lines & hl[h].get(p, set()) for p, lines in self.trim_lines)):
                continue  # the slow test's own run executes a changed import-time line
            keys = {_norm(m.rsplit(".", 1)[1]) for m in clos if m.startswith("sqlglot.dialects.")}
            if all(mod not in clos and not any(import_time_mutations(t, quals, keys) for t in ts)
                   for mod, quals, ts in trees):
                exclude.add(h)
        if not exclude:
            return ALL
        log(f"  ALL minus {len(exclude)} slow test(s) that never import {[p for p, _, _ in trim]}"
            f" nor execute changed import-time lines in {sorted({p for p, _ in self.trim_lines})}")
        return TrimmedAll(exclude)

    def vmap(self):
        """Per-test value trace (mapgen/vtrace.py), loaded on first use; False if unavailable."""
        if self._vmap is None:
            self._vmap = False
            if "vtrace" not in OFF:
                try:
                    with gzip.open(VMAP, "rt", encoding="utf-8") as f:
                        v = json.load(f)
                    v["bit_to_num"] = [self.m.num_by_id.get(t) for t in v["tests"]]
                    v["seen"] = set(v["cmp_seen"])
                    self._vmap = v
                except Exception as e:  # noqa: BLE001 - no trace: the rules below just don't apply
                    log(f"value trace unavailable ({e!r})")
        return self._vmap

    def vnums(self, v, idx):
        out, bits, i = set(), int(v["sets"][idx], 16), 0
        b2n = v["bit_to_num"]
        while bits:
            if bits & 1 and b2n[i] is not None:
                out.add(b2n[i])
            bits >>= 1
            i += 1
        return out

    def op_change(self, path, old_tree, new_tree, touched):
        """Only comparison operators changed in this file: tests whose traced comparisons at the
        changed sites would have returned a different result (exactly those can run differently).
        None -> rule not applicable (normal selection)."""
        diffs = op_change_sites(old_tree, new_tree, touched)
        if not diffs:
            return None
        v = self.vmap()
        if not v:
            return None
        out = set()
        for site, a, b in diffs:
            key = f"{path}:{site}"
            if {a, b} in _SWAPS:
                if self.sole and key in self.eq_safe:
                    continue  # the v8 rule's own conditions, unchanged
                return None
            if a not in _REL_OPS or b not in _CMP_OPS or key not in v["seen"]:
                return None  # operator kind not traced, or site never executed in the trace
            e = v["cmp"].get(key)
            alt = _CMP_OPS[b]
            if e is None or alt in e["all"]:
                return None  # differing result outside any test (import/session/subprocess)
            if alt in e["r"]:
                out |= self.vnums(v, e["r"][alt])
        log(f"  {path}: comparison operator change(s) {[(s, a.__name__, b.__name__) for s, a, b in diffs]} "
            f"-> {len(out)} tests with a differing result")
        return out

    def closure_factory(self, path, name):
        """Is `name`, as bound at module level in pinned `path` (defined there or imported with
        `from sqlglot... import name`), a closure factory (is_closure_factory)?"""
        cache = self.__dict__.setdefault("_factory", {})
        key = (path, name)
        if key not in cache:
            cache[key] = False
            tree = self.pinned_tree(path)
            target = None
            for st in tree.body if tree else ():
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)) \
                        and name in bound_names(st):
                    target = (path, st) if target is None else (None, None)  # rebound: ambiguous
                elif isinstance(st, ast.ImportFrom) and st.module and st.module.startswith("sqlglot") and not st.level:
                    for a in st.names:
                        if (a.asname or a.name) == name and a.name != "*":
                            mod = st.module.replace(".", "/")
                            src = f"{mod}.py" if (SRC / f"{mod}.py").is_file() else f"{mod}/__init__.py"
                            target = (src, a.name) if target is None else (None, None)
            if target and target[0]:
                where, st = target
                t2 = self.pinned_tree(where)
                if isinstance(st, str):  # imported: the definition in its own module
                    defs = [d for d in (t2.body if t2 else ()) if name_of(d) == st]
                    st = defs[0] if len(defs) == 1 else None
                lazy = any(isinstance(x, ast.ImportFrom) and x.module == "__future__"
                           and any(a.name == "annotations" for a in x.names) for x in (t2.body if t2 else ()))
                cache[key] = isinstance(st, ast.FunctionDef) and is_closure_factory(st, lazy)
        return cache[key]

    def pinned_tree(self, rel):
        cache = self.__dict__.setdefault("_trees", {})
        if rel not in cache:
            try:
                cache[rel] = ast.parse((SRC / rel).read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                cache[rel] = None
        return cache[rel]

    def const_change(self, path, old_tree, new_tree, touched):
        """Only the constant of single-constant comparisons changed in this file: the tests that observed
        an operand value for which old and new constant give different results, plus tests with unknown
        observations there, plus untraced tests. None -> rule not applicable (normal selection)."""
        diffs = const_change_sites(old_tree, new_tree, touched)
        if not diffs:
            return None
        v = self.vmap()
        if not v or "cv" not in v:
            return None
        out = set(v["bit_to_num"][i] for i in v.get("untraced", []) if v["bit_to_num"][i] is not None)
        for site, op, cside, old_c, new_c in diffs:
            e = v["cv"].get(f"{path}:{site}")
            if e is None:
                return None  # never executed under tracing (or not instrumented): coverage rules
            if e["unk"]:
                if e["unk"][1]:
                    return None  # unknown observation outside a test
                out |= self.vnums(v, e["unk"][0])
            for typ, val, idx, all_ in e["vals"]:
                if cmp_result(op, val, old_c, cside) != cmp_result(op, val, new_c, cside):
                    if all_:
                        return None  # result changes during import/session/subprocess
                    out |= self.vnums(v, idx)
        log(f"  {path}: comparison constant change(s) {[(s, c0, c1) for s, _, _, c0, c1 in diffs]} "
            f"-> {len(out)} tests whose observed values change the result")
        return out

    def dict_value_change(self, path, o, n):
        """A class-body dict literal whose keys are unchanged and whose entry values differ only
        in constant leaves: tests that read one of those entries' values or read the dict in
        bulk (traced). None -> rule not applicable."""
        if type(o) is not type(n) or not isinstance(o, (ast.Assign, ast.AnnAssign)):
            return None
        if isinstance(o, ast.Assign):
            if len(o.targets) != 1 or not isinstance(o.targets[0], ast.Name) or ast.dump(o.targets[0]) != ast.dump(n.targets[0]) \
                    or len(n.targets) != 1:
                return None
        elif not isinstance(o.target, ast.Name) or ast.dump(o.target) != ast.dump(n.target) \
                or ast.dump(o.annotation) != ast.dump(n.annotation):
            return None
        ov, nv = o.value, n.value
        if not isinstance(ov, ast.Dict) or not isinstance(nv, ast.Dict) or len(ov.keys) != len(nv.keys):
            return None
        changed, explicit = [], 0
        for ok, nk, oval, nval in zip(ov.keys, nv.keys, ov.values, nv.values):
            if (ok is None) != (nk is None) or (ok is not None and ast.dump(ok) != ast.dump(nk)):
                return None  # keys changed: not a value-only change
            same = ast.dump(oval) == ast.dump(nval)
            if ok is None:
                if not same:
                    return None  # **spread source changed
                continue
            if not same:
                if not const_leaf_diff(oval, nval, lambda name: self.closure_factory(path, name)):
                    return None
                changed.append(explicit)
            explicit += 1
        v = self.vmap()
        if not changed or not v:
            return None
        e = v["dict"].get(f"{path}:{ov.lineno}:{ov.col_offset}")
        if e is None or not e["ok"] or e["n"] != explicit:
            return None  # dict never created in the trace, or ambiguous entry positions
        keys = [str(j) for j in changed] + ["*"]
        if any(k in e["all"] for k in keys):
            return None  # read outside any test (class creation, metaclass, session...)
        out = set()
        for k in keys:
            if k in e["r"]:
                out |= self.vnums(v, e["r"][k])
        log(f"  {path}: class dict value change at {ov.lineno} entries {changed} -> {len(out)} reading tests")
        return out

    STATIC = ("_fam_deps", "_derived", "_non_data", "_imp_uses", "_test_importers", "_shared_mut")

    def shared_mut_hit(self, old_tree, new_tree, qual):
        """Names of class `qual` whose edit can steer an import-time in-place mutation of a shared
        container: a changed/added/removed binding of a value key, or an added/removed binding of a
        container key (the class starts/stops owning the container). Unresolvable class -> its
        changed names are compared against both key sets."""
        def body_bindings(tree):
            node, parts = tree, qual.split(".") if qual else []
            for p in parts:
                node = next((s for s in getattr(node, "body", []) if isinstance(s, ast.ClassDef) and s.name == p), None)
                if node is None:
                    return None
            out = {}
            for st in node.body:
                for nm in bound_names(st):
                    out.setdefault(nm, []).append(ast.dump(st))
            return out
        a, b = body_bindings(old_tree), body_bindings(new_tree)
        values, containers = self.shared_mut()
        if a is None or b is None:
            names = set(a or {}) | set(b or {})
            return (names & values) | (names & containers)
        changed = {nm for nm in set(a) | set(b) if a.get(nm) != b.get(nm)}
        return (changed & values) | ((set(a) ^ set(b)) & containers)

    def shared_mut(self):
        """(value_keys, container_keys) over every function body that runs at import (map `imp`): class
        settings whose value steers an import-time in-place mutation of a possibly inherited/shared
        container, and the attributes holding such containers. See shared_mutation_keys."""
        if "_shared_mut" not in self.__dict__:
            # only a class attribute can be inherited and so shared between classes; instance state
            # (expression.args, self.errors...) is per object
            class_attrs = set()
            for f in sorted((SRC / "sqlglot").rglob("*.py")):
                tree = self.pinned_tree(str(f.relative_to(SRC)))
                for c in ast.walk(tree) if tree is not None else ():
                    if isinstance(c, ast.ClassDef):
                        for st in c.body:  # bound with a value (an annotation alone declares instance state)
                            if isinstance(st, ast.Assign) or (isinstance(st, ast.AnnAssign) and st.value is not None):
                                class_attrs |= bound_names(st)
            m = self.__dict__.get("m")
            if m is not None:
                imp = m.imp
            else:  # --build-index runs without a loaded Map: read the import-time lines from the map file
                with gzip.open(MAP, "rt") as f:
                    imp = {k: set(v) for k, v in json.load(f).get("imp", {}).items()}
            values, containers = set(), set()
            for rel, lines in sorted(imp.items()):
                tree = self.pinned_tree(rel)
                if tree is None:
                    continue
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and (span(fn) - header_lines(fn)) & lines:
                        for conts, vals in shared_mutation_keys(fn):
                            if conts & class_attrs:
                                values |= vals
                                containers |= conts & class_attrs
            self._shared_mut = (frozenset(values), frozenset(containers))
        return self._shared_mut

    def load_static_index(self):
        import pickle

        try:
            with open(IDX, "rb") as f:
                idx = pickle.load(f)
            if idx.get("src") != str(SRC) or set(idx) != set(self.STATIC) | {"src"}:
                return
        except Exception:  # absent/corrupt: lazy computation as before
            return
        for k in self.STATIC:
            self.__dict__[k] = idx[k]

    def build_static_index(self, out):
        """Precompute every pinned-source index family_users() can consult."""
        import pickle

        deps = self.family_dependents()
        self.derived_names()
        for rel in sorted({rel for lst in deps.values() for rel, _, _ in lst}):
            self.import_uses(rel)
        keys = set(deps)
        for d in self.FAMILY_DIRS:
            keys |= {p.stem for p in (SRC / "sqlglot" / d).glob("*.py")}
        for k in sorted(keys):
            self.non_data_names(k)
        self.shared_mut()
        if self.test_importers() is ALL:  # never ship a sentinel: recompute lazily (-> ALL) at run time
            raise SystemExit("test import graph unresolved at the pinned commit")
        idx = {k: self.__dict__[k] for k in self.STATIC}
        idx["src"] = str(SRC)
        with open(out, "wb") as f:
            pickle.dump(idx, f, protocol=pickle.HIGHEST_PROTOCOL)
        log(f"static index: {len(idx['_fam_deps'])} families, {len(idx['_imp_uses'])} files, "
            f"{len(idx['_non_data'])} keys -> {out}")

    MODULE_STMTS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom,
                    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def module_change(self, path, olds, news):
        """Module-level statements changed in sqlglot/<path>. A module-level value can
        only matter where its name is read: runtime readers' tests; import-time readers
        resolved to class users / function callers / derived module names. Anything
        not provably limited -> ALL."""
        if "refs" in OFF or not self.m.refs or olds is None or news is None or not path.startswith("sqlglot/"):
            log(f"  {path}: module-level change -> all tests")
            return ALL
        for st in olds + news:
            if not isinstance(st, self.MODULE_STMTS):
                log(f"  {path}: module-level {type(st).__name__} -> all tests")
                return ALL
        # a newly added/changed import may import a module for the first time
        old_imps = {ast.dump(st) for st in olds if isinstance(st, (ast.Import, ast.ImportFrom))}
        if any(isinstance(st, (ast.Import, ast.ImportFrom)) and ast.dump(st) not in old_imps for st in news):
            log(f"  {path}: new/changed import -> all tests")
            return ALL
        for st in olds + news:
            if isinstance(st, ast.ClassDef):
                # class creation runs __init_subclass__/metaclass hooks that register the
                # class (e.g. Expression subclasses); a new or removed class is observable
                # without anyone reading its name
                log(f"  {path}: module-level class {st.name} added/removed/renamed -> all tests")
                return ALL
            if isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names):
                log(f"  {path}: star import changed -> all tests")  # binds unknown names
                return ALL
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.decorator_list:
                log(f"  {path}: decorated module-level function {st.name} -> all tests")  # may register
                return ALL
        names = set()
        for st in olds + news:
            names |= bound_names(st)
        if not names or any(nm.startswith("__") for nm in names):
            log(f"  {path}: module-level change binds {sorted(names)[:3]} -> all tests")
            return ALL
        return self.name_users(path, names)

    def name_users(self, path, names, budget=40):
        out = set()
        seen = set()
        work = [(path, nm, 0) for nm in names]
        while work:
            f, nm, depth = work.pop()
            if (f, nm) in seen:
                continue
            seen.add((f, nm))
            if len(seen) > budget or depth > 4:
                log(f"  {path}: reference chain too long -> all tests")
                return ALL
            mod = module_name(f)
            # files where `nm` (from f) is visible as a plain name, and its local alias
            visible = [(f, nm)] + [tuple(x) for x in self.m.imports.get(f"{mod}:{nm}", [])]
            visible += [(g, nm) for g in self.m.star.get(mod, [])]
            entry_cache = {}
            for g, local in visible:
                if g != f:
                    work.append((g, local, depth + 1))  # re-exports
                e = entry_cache.setdefault(local, self.m.refs.get(local, {}))
                idx = e.get("rtn", {}).get(g)
                if idx is not None:
                    out |= self.m.nums(self.m.set(idx))
                for ctx in e.get("impn", []):
                    if ctx[1] == g:
                        r = self.import_ctx_users(ctx, depth, work)
                        if r is ALL:
                            log(f"  {path}: {nm} read at import in {g} -> all tests")
                            return ALL
                        out |= r
            e = self.m.refs.get(nm, {})
            for g, idx in e.get("rta", {}).items():  # x.NAME / getattr(x, "NAME")
                out |= self.m.nums(self.m.set(idx))
            for ctx in e.get("impa", []):
                r = self.import_ctx_users(ctx, depth, work)
                if r is ALL:
                    log(f"  {path}: .{nm} read at import in {ctx[1]} -> all tests")
                    return ALL
                out |= r
        return out

    def traced_attr_readers(self, path, qual, nm):
        """Tests whose execution actually evaluated a read of attribute `nm` whose value came from class
        `qual` of `path` (traced: mapgen/vtrace.py, owner = defining class in the lookup MRO), plus reads
        with an unresolvable owner, untraced tests, and every test of a test file mentioning the name
        (test code is not instrumented). None -> not established (use the static index)."""
        v = self.vmap()
        if not v or "attr" not in v or "attrtrace" in OFF or not _UPPER_NAME.match(nm):
            return None
        per = v["attr"].get(nm, {})
        owner = f"{module_name(path)}:{qual}"
        out = set(v["bit_to_num"][i] for i in v.get("untraced", []) if v["bit_to_num"][i] is not None)
        for o in (owner, "?"):
            if o in per:
                idx, all_ = per[o]
                if all_:
                    return None  # read at import / session / subprocess: derived or shared state
                out |= self.vnums(v, idx)
        out |= self.test_files_mentioning(nm)
        return out

    def test_files_mentioning(self, nm):
        cache = self.__dict__.setdefault("_mention", {})
        if nm not in cache:
            hit = set()
            pat = re.compile(r"\b%s\b" % re.escape(nm))
            for f in sorted((SRC / "tests").rglob("*.py")) if (SRC / "tests").is_dir() else []:
                try:
                    if pat.search(f.read_text(encoding="utf-8")):
                        hit |= self.file_tests(str(f.relative_to(SRC)))
                except OSError:
                    return set(self.m.bit_to_num) - {None}
            cache[nm] = hit
        return cache[nm]

    def class_data_readers(self, path, stmts, is_test_file, qual=None):
        """Tests that execute a runtime read of the class attributes bound by `stmts`,
        or None when that is not a safe bound (import-time reads, non-data statements,
        private/dunder or method-like names, unknown statements)."""
        if "classread" in OFF or is_test_file or not stmts or not self.m.refs:
            return None
        names = set()
        for st in stmts:
            if not isinstance(st, (ast.Assign, ast.AnnAssign)):
                return None
            b = bound_names(st)
            if not b or "__complex_target__" in b:
                return None
            names |= b
        out = set()
        for nm in names:
            if nm.startswith("__") or nm.endswith("_sql") or nm.startswith("_parse"):
                return None  # reached through generic dispatch (dir()/getattr f-strings)
            e = self.m.refs.get(nm, {})
            if e.get("impa") or e.get("impn"):
                return None  # read while classes/modules are built: derived state
            traced = self.traced_attr_readers(path, qual, nm) if qual else None
            if traced is not None:
                out |= traced  # reads that actually executed (a covered line need not evaluate them)
                continue
            for idx in e.get("rta", {}).values():
                out |= self.m.nums(self.m.set(idx))
        return out

    def import_ctx_users(self, ctx, depth, work):
        kind, g = ctx[0], ctx[1]
        if kind == "class":
            idx = self.m.users.get(f"{module_name(g)}:{ctx[2]}")
            users = self.m.nums(self.m.set(idx)) if idx is not None else set()
            users |= self.family_users(g)
            return users if users else ALL
        if kind == "def":
            _, _, qual, cls, b1, b2 = ctx
            r = self.m.nums(self.lines_bits(g, range(b1, b2 + 1)))
            if cls:
                idx = self.m.users.get(f"{module_name(g)}:{cls}")
                if idx is not None:
                    r |= self.m.nums(self.m.set(idx))
            return r
        if kind == "module":
            if not ctx[2]:
                return ALL
            for nm2 in ctx[2]:
                work.append((g, nm2, depth + 1))
            return set()
        return ALL  # function body executed at import

    FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")

    def family_users(self, path, attrs=None):
        """Users of this dialect family, plus tests reached through cross-dialect
        references to it. attrs: names bound by the changed class-level statements (None =
        unknown). A reference that provably reads only OTHER attributes adds nothing; one
        that only uses the class as a base adds nothing itself (subclass instances already
        count as users of the base) but references to the subclass family are followed."""
        if "family" in OFF:
            return set()
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "sqlglot" or parts[1] not in self.FAMILY_DIRS:
            return set()
        base = parts[2][:-3]
        if base in ("__init__", "dialect"):
            return set()
        if attrs is not None and any(a.startswith("__") for a in attrs):
            attrs = None
        fams, sites = {base}, []
        follow = {(base, None if attrs is None else frozenset(attrs))}
        if "famdeps" not in OFF:
            refs = self.family_dependents()
            work = list(follow)
            while work:
                k, hop_attrs = work.pop()
                todo = []
                for rel, ln, fam in refs.get(k, ()):
                    uses = self.import_uses(rel).get(k) if hop_attrs is not None and "attrdeps" not in OFF else None
                    if uses is None:  # string literal, or attribute set unknown: as before
                        todo.append((rel, ln, fam, "use"))
                    elif ln in uses:
                        todo.append((rel, ln, fam, self.use_kind(uses[ln], hop_attrs, k)))
                    else:  # the import statement: the data flows through each line using it
                        todo += [(rel, uln, fam, self.use_kind(lst, hop_attrs, k)) for uln, lst in uses.items()]
                for rel, ln, fam, kind in dict.fromkeys(todo):
                    if kind == "none":
                        continue
                    if kind == "base":  # subclass: same attributes, inherited under the same names
                        if fam and (fam, hop_attrs) not in follow:
                            follow.add((fam, hop_attrs))
                            work.append((fam, hop_attrs))
                        elif not fam:
                            sites.append((rel, None))
                        continue
                    if ln in self.m.imp.get(rel, set()):
                        # import-time reference (class-body copy such as Presto.TIME_MAPPING =
                        # MySQL.TIME_MAPPING, or a lookup bound at import): the data spreads to
                        # every user of the referencing family, and on to ITS dependents
                        if fam:
                            fams.add(fam)
                            # the copy may be stored under ANY name: follow with attributes unknown
                            if (fam, None) not in follow:
                                follow.add((fam, None))
                                work.append((fam, None))
                        else:
                            sites.append((rel, None))
                    else:
                        # runtime reference (Dialect["teradata"] inside a function): only tests
                        # executing that line read the data; the line runs on every call
                        sites.append((rel, ln))
        mods = {f"sqlglot.{d}.{b}:" for d in self.FAMILY_DIRS for b in fams}
        bits = 0
        for cid, idx in self.m.users.items():
            if cid[: cid.index(":") + 1] in mods:
                bits |= self.m.set(idx)
        for rel, ln in sites:
            if ln is None:  # import-time line outside the families: anyone running the file
                tidx = self.m.touch.get(rel)
                if tidx is not None:
                    bits |= self.m.set(tidx)
            else:
                bits |= self.lines_bits(rel, [ln])
        return self.m.nums(bits)

    DERIVED = re.compile(r"\.([A-Z][A-Z0-9_]*)(?:\s*=[^=]|\[[^\]]*\]\s*=[^=]|\.(?:update|setdefault|pop|popitem|"
                         r"append|extend|insert|add|remove|discard|clear)\()")

    def derived_names(self):
        """UPPER_CASE attribute names that some code stores or mutates in place
        (klass.INVERSE_TIME_MAPPING = ..., gen_cls.TRANSFORMS.pop(...)): their values can
        depend on OTHER settings (the dialect metaclass derives them at import). Derived
        from the pinned source, cached."""
        cache = self.__dict__.get("_derived")
        if cache is None:
            cache = set()
            root = SRC / "sqlglot"
            for f in sorted(root.rglob("*.py")) if root.is_dir() else []:
                try:
                    cache |= {m.group(1) for m in self.DERIVED.finditer(f.read_text(encoding="utf-8"))}
                except OSError:
                    continue
            self._derived = cache
        return cache

    def use_kind(self, uses, attrs, key=None):
        """Combine the uses on one line: "use" if any may read the changed attributes,
        else "base" if the imported class is subclassed there, else "none"."""
        derived = self.derived_names() | (self.non_data_names(key) if key else set())
        kinds = {self.classify(u, attrs, derived) for u in uses}
        return "use" if "use" in kinds else ("base" if "base" in kinds else "none")

    @staticmethod
    def classify(use, attrs, derived=frozenset()):
        kind, chain = use
        if kind == "base":
            return "base"
        if kind == "typecheck":
            return "none"
        # X.NAME... reads exactly NAME from the class (what follows is read from NAME's value).
        # Provably unrelated only if NAME is a raw data constant: UPPER_CASE, never stored or
        # mutated by code (so it holds its class-body value), and not one of the changed names.
        # Methods (format_time), nested classes (Parser) and derived constants
        # (INVERSE_TIME_MAPPING, built from TIME_MAPPING by the metaclass) can carry it on.
        if kind == "attr" and chain and chain[0].isupper() and chain[0] not in attrs and chain[0] not in derived:
            return "none"
        return "use"

    def non_data_names(self, key):
        """Names defined by `class`/`def` in family `key`'s files (nested classes, methods,
        properties): reading one is not reading a plain constant."""
        cache = self.__dict__.setdefault("_non_data", {})
        if key not in cache:
            names = set()
            for d in self.FAMILY_DIRS:
                f = SRC / "sqlglot" / d / f"{key}.py"
                try:
                    names |= set(re.findall(r"^\s*(?:async\s+)?(?:class|def)\s+(\w+)", f.read_text(encoding="utf-8"), re.M))
                except OSError:
                    continue
            cache[key] = names
        return cache[key]

    def import_uses(self, rel):
        """{family key: {line: [(kind, attribute chain)]}} for every load of a name a file
        imports from a dialect family (`from sqlglot.<fam>.<key> import X` -> X..., or
        `import sqlglot.<fam>.<key>` -> sqlglot.<fam>.<key>.X...). kind: "base" (in a class's
        bases), "attr" (X.a.b read: chain [a, b]), "other" (anything else). Cached per file."""
        cache = self.__dict__.setdefault("_imp_uses", {})
        if rel in cache:
            return cache[rel]
        out = {}
        cache[rel] = out
        try:
            tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return out
        local, dotted, starred = {}, {}, set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                m = n.module.split(".")
                if len(m) == 3 and m[0] == "sqlglot" and m[1] in self.FAMILY_DIRS:
                    for a in n.names:
                        if a.name == "*":  # names unknown: this key stays conservative
                            starred.add(m[2])
                        local[a.asname or a.name] = m[2]
            elif isinstance(n, ast.Import):
                for a in n.names:
                    m = a.name.split(".")
                    if len(m) == 3 and m[0] == "sqlglot" and m[1] in self.FAMILY_DIRS and not a.asname:
                        dotted[(m[1], m[2])] = m[2]
        if not local and not dotted:
            return out
        parent, in_bases, typechecks = {}, set(), set()
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[id(c)] = n
            if isinstance(n, ast.ClassDef):
                for b in n.bases:
                    for x in ast.walk(b):
                        in_bases.add(id(x))
            # isinstance(x, C) / issubclass(x, (C, D)): the class operand is only identity-
            # checked against the MRO (no sqlglot metaclass defines __instancecheck__ /
            # __subclasscheck__), so it reads none of C's attributes
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("isinstance", "issubclass")
                    and len(n.args) == 2 and not n.keywords):
                cls_arg = n.args[1]
                elts = cls_arg.elts if isinstance(cls_arg, ast.Tuple) else [cls_arg]
                for e in elts:
                    if isinstance(e, ast.Name):
                        typechecks.add(id(e))

        def climb(node):
            chain, top = [], node
            while isinstance(parent.get(id(top)), ast.Attribute) and parent[id(top)].value is top:
                top = parent[id(top)]
                chain.append(top.attr)
            return chain, top

        for n in ast.walk(tree):
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                continue
            if n.id in local:
                key, skip = local[n.id], 0
            elif n.id == "sqlglot" and dotted:
                chain, _ = climb(n)
                key = dotted.get(tuple(chain[:2]))
                if key is None or len(chain) < 3:
                    continue
                skip = 3  # sqlglot.<fam>.<key>.<Class>.attr... -> attributes after the class
            else:
                continue
            chain, top = climb(n)
            chain = chain[skip:]
            if id(n) in in_bases:
                kind = "base"
            elif not chain and id(n) in typechecks and "typecheck" not in OFF:
                kind = "typecheck"
            elif chain and isinstance(top.ctx, ast.Load):
                kind = "attr"
            else:
                kind = "other"
            out.setdefault(key, {}).setdefault(n.lineno, []).append((kind, chain))
        for k in starred:
            out.pop(k, None)  # -> reference_kind/use: conservative
        return out

    FAM_IMPORT = (r"^\s*(?:from\s+sqlglot\.(?:dialects|parsers|generators|tokenizers|typing)\.(\w+)\s+import"
                  r"|import\s+sqlglot\.(?:dialects|parsers|generators|tokenizers|typing)\.(\w+)\s*$)")
    REGISTRY = ("sqlglot/dialects/dialect.py", "sqlglot/dialects/__init__.py")

    def family_dependents(self):
        """Cross-dialect data flow derived from the pinned source:
        {family: [(file, line, referencing family or None)]}.
        A dialect family's settings reach other tests without any instance of it:
        class-object lookups by name (build_formatted_time(..., "teradata") ->
        Dialect["teradata"].TIME_MAPPING), import-time copies (Presto.TIME_MAPPING =
        MySQL.TIME_MAPPING) and runtime class reads (Hive.INVERSE_TIME_MAPPING). Every line
        that imports another family's module or names its dialect key in a string literal
        is a reference. The registry (Dialects enum, lazy loader) is skipped."""
        cache = self.__dict__.get("_fam_deps")
        if cache is None:
            root = SRC / "sqlglot"
            keys = sorted(p.stem for p in (root / "dialects").glob("*.py")
                          if p.stem not in ("__init__", "dialect")) if root.is_dir() else []
            cache = {}
            if keys:
                imp = re.compile(self.FAM_IMPORT)
                lit = re.compile(r"""(?<![\w.])["'](%s)["']""" % "|".join(keys))
                for f in sorted(root.rglob("*.py")):
                    rel = str(f.relative_to(SRC))
                    if rel in self.REGISTRY:
                        continue
                    parts = rel.split("/")
                    own = f.stem if len(parts) == 3 and parts[1] in self.FAMILY_DIRS else None
                    try:
                        text = f.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    for i, line in enumerate(text.split("\n"), 1):
                        found = {m.group(1) or m.group(2) for m in imp.finditer(line)} | {m.group(1) for m in lit.finditer(line)}
                        for k in found:
                            if k in keys and k != own:
                                cache.setdefault(k, []).append((rel, i, own))
            self._fam_deps = cache
        return cache

    def lines_bits(self, path, lines):
        per = self.m.lines.get(path, {})
        b = 0
        for l in lines:
            idx = per.get(str(l))
            if idx is not None:
                b |= self.m.set(idx)
        return b

    ENUMERATORS = ("iter_modules", "walk_packages", "listdir", "scandir", "glob(", "iterdir")

    def package_enumerator_tests(self):
        cache = self.__dict__.get("_enum_tests")
        if cache is None:
            cache = set()
            tests_dir = SRC / "tests"
            for f in sorted(tests_dir.rglob("*.py")) if tests_dir.is_dir() else []:
                try:
                    text = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                if any(tok in text for tok in self.ENUMERATORS):
                    cache |= self.file_tests(str(f.relative_to(SRC)))
            self._enum_tests = cache
        return set(cache)

    DYN_IMPORT = re.compile(r"""import_module\(\s*f["']([\w.]*?)\.?\{""")
    DYN_SELF = re.compile(r"""import_module\(\s*f["']\{__name__\}\.""")

    def name_loaded_packages(self):
        """{package (dotted): [(file, line) of the import call]} for packages whose submodules
        the pinned sqlglot imports by a name computed at runtime: import_module(f"pkg.{x}")
        anywhere, or import_module(f"{__name__}.{x}") in a package __init__. Derived from
        the pinned source, cached."""
        cache = self.__dict__.get("_dyn_pkgs")
        if cache is None:
            cache = {}
            root = SRC / "sqlglot"
            for f in sorted(root.rglob("*.py")) if root.is_dir() else []:
                try:
                    text = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "import_module" not in text:
                    continue
                rel = str(f.relative_to(SRC))
                for i, line in enumerate(text.split("\n"), 1):
                    for m in self.DYN_IMPORT.finditer(line):
                        if m.group(1):
                            cache.setdefault(m.group(1), []).append((rel, i))
                    if f.name == "__init__.py" and self.DYN_SELF.search(line):
                        cache.setdefault(module_name(rel), []).append((rel, i))
            self._dyn_pkgs = cache
        return cache

    def opened_by(self, path):
        idx = self.m.opens.get(path)
        return self.m.nums(self.m.set(idx)) if idx is not None else set()

    def file_tests(self, path):
        return self.m.by_file.get(path, set())

    def test_importers(self):
        """{tests/x.py: set of test modules that import it, directly or transitively} from the pinned
        test sources (import / from-import / relative / sibling, anywhere in the file), or ALL when an
        import that names the tests package cannot be resolved to a file."""
        cache = self.__dict__.get("_test_importers")
        if cache is not None:
            return cache
        root = SRC / "tests"
        files = sorted(str(f.relative_to(SRC)) for f in root.rglob("*.py")) if root.is_dir() else []
        known = set(files)
        direct = {}
        unresolved = False

        def resolve(dotted, rel):
            parts = dotted.split(".") if dotted else []
            cands = []
            if parts and parts[0] == "tests":
                cands.append("/".join(parts))
            elif parts and f"{'/'.join(rel.split('/')[:-1])}/__init__.py" not in known:
                # importing file's directory is not a package: pytest puts it on sys.path (rootdir
                # "prepend" mode), so a bare name may be a sibling module
                cands.append("/".join(rel.split("/")[:-1] + parts))
            for c in cands:
                for f in (c + ".py", c + "/__init__.py"):
                    if f in known:
                        return f
            return None

        def mod_of(n):
            if isinstance(n, ast.ImportFrom):
                if n.level:
                    base = pkg[: len(pkg) - (n.level - 1)] if n.level > 1 else pkg
                    return ".".join(base + ([n.module] if n.module else []))
                return n.module or ""
            return None

        for rel in files:
            try:
                tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                unresolved = True
                continue
            pkg = rel.split("/")[:-1]
            for n in ast.walk(tree):
                names = []
                if isinstance(n, ast.Import):
                    names = [a.name for a in n.names]
                elif isinstance(n, ast.ImportFrom):
                    mod = mod_of(n)
                    names = [mod] + [f"{mod}.{a.name}" for a in n.names if a.name != "*"]
                for dotted in names:
                    f = resolve(dotted, rel)
                    if f is not None:
                        if f != rel:
                            direct.setdefault(f, set()).add(rel)
                    elif (dotted == "tests" or dotted.startswith("tests.")) and dotted != mod_of(n) \
                            and resolve(dotted.rsplit(".", 1)[0], rel) is not None:
                        pass  # `from tests.x import name`: name is an attribute of tests/x.py (resolved above)
                    elif dotted == "tests" or dotted.startswith("tests."):
                        unresolved = True  # names the tests package but no file: cannot bound it
        if unresolved:
            self._test_importers = ALL
            return ALL
        closure = {}
        for f in known:
            seen, work = set(), [f]
            while work:
                g = work.pop()
                for h in direct.get(g, ()):
                    if h not in seen:
                        seen.add(h)
                        work.append(h)
            closure[f] = seen
        self._test_importers = closure
        return closure

    def test_import_time_users(self, path, tree, lines, kind):
        imp = self.test_importers()
        if imp is ALL:
            return ALL
        uses, enclosing = self.test_import_refs(tree, lines)
        if kind == "lambda" or enclosing & uses:
            return self.test_module_tests(path)
        out = set()
        for g in imp.get(path, ()):
            t2 = self.pinned_tree(g) if hasattr(self, "pinned_tree") else None
            if t2 is None:
                try:
                    t2 = ast.parse((SRC / g).read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    return ALL
            if enclosing & self.test_import_refs(t2, ())[0]:
                r = self.test_module_tests(g)
                if r is ALL:
                    return ALL
                out |= r
        return out

    @staticmethod
    def test_import_refs(tree, lines):
        """(names used by code that runs at import of this module, names of functions enclosing `lines`)."""
        """Do the changed lines of a test module possibly run while it is imported? Lambdas at module/
        class level: yes (conservative). Function bodies: if the enclosing function's name is used by
        code that runs at import (module statements, class bodies, decorators, defaults, bases)."""
        imp_names = set()
        enclosing = set()

        def names(node):
            for x in ast.walk(node):
                if isinstance(x, ast.Name):
                    imp_names.add(x.id)
                elif isinstance(x, ast.Attribute):
                    imp_names.add(x.attr)

        def visit(body, in_func):
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(first_line(st) <= ln <= st.end_lineno for ln in lines):
                        enclosing.add(st.name)
                    if not in_func:
                        for d in st.decorator_list + st.args.defaults + [d for d in st.args.kw_defaults if d]:
                            names(d)
                    visit(st.body, True)
                elif isinstance(st, ast.ClassDef):
                    if not in_func:
                        for e in st.bases + [k.value for k in st.keywords] + st.decorator_list:
                            names(e)
                    visit(st.body, in_func)
                elif not in_func:
                    names(st)
                    for f in BODY_FIELDS:
                        visit(getattr(st, f, None) or [], in_func)

        visit(tree.body, False)
        return imp_names, enclosing

    def test_module_tests(self, path):
        """Tests of a test module AND of every test module importing it (an error while importing it
        breaks their collection too). ALL if the import graph is not reliable."""
        imp = self.test_importers()
        if imp is ALL:
            log(f"  {path}: test import graph unresolved -> all tests")
            return ALL
        out = set(self.file_tests(path))
        for f in imp.get(path, ()):
            out |= self.file_tests(f)
        if imp.get(path):
            log(f"  {path}: imported by {len(imp[path])} test module(s) -> {len(out)} tests")
        return out

    def py_change(self, path, old_text, deleted, hunks):
        """-> (set of numbers) or ALL"""
        is_test_file = path.startswith("tests/")
        if deleted:  # deleted module
            return ALL if not is_test_file or "helpers" in path else self.test_module_tests(path)
        if old_text is None:
            # brand-new file: nothing imports it by name at the pinned commit, but tests
            # that enumerate packages/directories (pkgutil.iter_modules, listdir, glob...)
            # will find and import it
            if is_test_file:
                return set()
            mod = module_name(path)
            sites = [ln for p, lns in self.name_loaded_packages().items() if mod.startswith(p + ".") for ln in lns]
            if path.startswith("sqlglot/dialects/") or sites:
                # Dialect._try_load also imports a module named by the requested dialect.
                # Existing tests may request that name expecting an unknown-dialect error.
                # More generally sqlglot imports submodules of this package by a runtime
                # name (dialect lookup, lazy __getattr__): the new module changes what such
                # a name resolves to. Only a test that executes one of those import calls
                # can probe it, and a failed lookup is never cached, so the coverage of
                # those lines is complete for names that do not exist yet.
                tests = set()
                for rel, ln in sites:
                    tests |= self.m.nums(self.lines_bits(rel, [ln]))
                if not tests or "dynsite" in OFF:
                    log(f"  {path}: new module in a package loaded by name -> all tests")
                    return ALL
                log(f"  {path}: new module in a package loaded by name -> {len(tests)} probing tests")
                return tests | self.package_enumerator_tests()
            return self.package_enumerator_tests()
        old_lines = old_text.split("\n")
        new_lines, old_touched, new_touched = apply_hunks(old_lines, hunks)
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except SyntaxError:
            log(f"  {path}: syntax error after patch -> import breaks")
            return ALL if not path.startswith("tests/test_") and not path.startswith("tests/dialects/test_") else self.test_module_tests(path)
        old_tree = ast.parse(old_text)
        if self.sole and not is_test_file and self.eq_safe and swap_only_hunks(hunks):
            sites = swap_only_sites(old_tree, new_tree, old_touched | new_touched)
            if sites and all(f"{path}:{site}" in self.eq_safe for site in sites):
                # the patch only swaps is<->== / is not<->!= where every traced execution compared
                # operands for which identity and equality agree by construction: every
                # comparison returns what it did before, so the program runs unchanged
                log(f"  {path}: identity/equality swap at interchangeable site(s) {sites} -> no tests")
                return self.opened_by(path)  # tests reading this source file as text still see it
        if not is_test_file and op_only_hunks(hunks):
            r = self.op_change(path, old_tree, new_tree, old_touched | new_touched)
            if r is not None:
                return r | self.opened_by(path)
        if not is_test_file and "cvconst" not in OFF and balanced_hunks(hunks):
            r = self.const_change(path, old_tree, new_tree, old_touched | new_touched)
            if r is not None:
                return r | self.opened_by(path)
        # def/class header lines (decorators, signature, defaults) execute at import to
        # *create* the function/class; their effect shows up only through callers and
        # class users, which the header-change rules already select. Only statements
        # inside bodies that actually ran at import count as import-time changes.
        imp_lines = self.m.imp.get(path, set()) if "imp" not in OFF else set()
        headers = None  # header lines of the whole file: only needed once a region hits imp_lines

        def import_time(lines):
            nonlocal headers
            hit = lines & imp_lines
            if not hit or "hdrimp" in OFF:
                return hit
            if headers is None:
                headers = set()
                for node in ast.walk(old_tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        headers |= header_lines(node)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_stub(node):
                            headers |= span(node)  # `def f(...): ...` stubs: body does nothing
            return hit - headers
        d = Differ(old_lines, new_lines, old_touched, new_touched)
        d.body(old_tree.body, new_tree.body, ("module", None), None)
        out = set()
        mod = module_name(path)
        enum_like = None  # computed on the first class-level region only
        if d.regions:
            tidx = self.m.touch.get(path)
            if tidx is not None:
                self.related |= self.m.nums(self.m.set(tidx))
            self.related |= self.family_users(path)
        for r in d.regions:
            if r[0] in ("lines", "lambda") and is_test_file:
                # code of a test module that runs while some test module is imported (called from its
                # own module/class level, or from the import-time code of a module importing it): the
                # effect reaches every test module importing that one
                t = self.test_import_time_users(path, old_tree, r[1], r[0])
                if t is ALL:
                    return ALL
                out |= t
            if r[0] == "lines":
                hit = import_time(r[1]) if not is_test_file else None
                if hit:
                    # runs while sqlglot is imported (metaclass, __init_subclass__,
                    # factories used in class bodies)
                    iso = self.iso_tests(path, r[1])
                    if iso is not None and "isomut" not in OFF and mutation_sites(old_tree) != mutation_sites(new_tree):
                        # the edit changes WHAT import-time code mutates (another receiver, a new or removed
                        # store/mutator call): shared objects reach tests that never execute the line in
                        # isolation (stress_sa sa1: gen_cls.TRANSFORMS.pop -> Generator.TRANSFORMS.pop)
                        # (the slow-test line trim has the same blind spot: plain ALL, as v14)
                        log(f"  {path}: import-time edit changes mutation targets -> all tests")
                        self.no_trim = True
                        return ALL
                    if iso is not None:
                        # each test ran alone (collection, runtime, pool children): only a test whose
                        # own run executes the line can see what it computes at import or at runtime
                        log(f"  {path}: import-time line(s) {sorted(hit)[:5]} -> {len(iso)} tests executing them in isolation")
                        out |= iso | self.m.nums(self.lines_bits(path, r[1]))
                        continue
                    if "heavylines" in OFF or not self.heavy_lines():
                        log(f"  {path}: import-time line(s) {sorted(hit)[:5]} -> all tests")
                        return ALL
                    # every test, except slow tests whose own run (collection, runtime, pool children)
                    # never executes these lines: see trimmed_all
                    log(f"  {path}: import-time line(s) {sorted(hit)[:5]} -> all tests (trimmable)")
                    self.trim_lines.append((path, set(r[1])))
                out |= self.m.nums(self.lines_bits(path, r[1]))
            elif r[0] == "lambda":
                out |= self.m.nums(self.lines_bits(path, r[1]))
            elif r[0] == "class":
                if not is_test_file and "sharedmut" not in OFF:
                    hit = self.shared_mut_hit(old_tree, new_tree, r[1])
                    if hit:
                        # an import-time mutation of a possibly inherited container depends on this
                        # setting (or on which class owns the container): it can reach every class
                        # sharing that container (stress_sa sa2: SUPPORTED_JSON_PATH_PARTS -> pops from
                        # the base Generator.TRANSFORMS)
                        log(f"  {path}: class {r[1]} changes {sorted(hit)} steering import-time mutation "
                            f"of shared containers -> all tests")
                        self.no_trim = True  # importing this module anywhere in a process pollutes the
                        return ALL           # shared container: closures of slow tests are no bound
                if len(r) > 3 and r[3] == "pair" and not is_test_file:
                    dv = self.dict_value_change(path, r[2][0], r[2][1])
                    if dv is not None:
                        out |= dv
                        continue
                idx = self.m.users.get(f"{mod}:{r[1]}")
                users = self.m.nums(self.m.set(idx)) if idx is not None else set()
                # dialect metaclasses copy tokenizer/parser/generator settings across the
                # family at import, so any class of the family can carry the change
                attrs = None
                if len(r) > 2 and r[2] and all(isinstance(st, (ast.Assign, ast.AnnAssign)) for st in r[2]):
                    attrs = set()
                    for st in r[2]:
                        attrs |= bound_names(st)
                    if not attrs or "__complex_target__" in attrs:
                        attrs = None
                users |= self.family_users(path, attrs)
                if enum_like is None:
                    enum_like = enum_like_classes(old_tree)
                readers = None if r[1] in enum_like else \
                    self.class_data_readers(path, r[2] if len(r) > 2 else None, is_test_file, r[1])
                if readers is not None:
                    # a class attribute that is never read at import can only be observed by
                    # executing a read of it (readers, by attribute NAME), on this class or a
                    # subclass. Generic names (NULL_ORDERING, _hash_raw_args...) are read on
                    # many unrelated classes, so also require that the test reaches this
                    # class: instantiates it or a subclass (users, rolled up to bases), uses
                    # its dialect family, or executes any line of this file (covers reads
                    # through the class object, e.g. dialect.parser_class.X, with no instance).
                    scope = set(users)
                    if "classscope" not in OFF:
                        tidx = self.m.touch.get(path)
                        if tidx is not None:
                            scope |= self.m.nums(self.m.set(tidx))
                        out |= readers & scope
                    else:
                        out |= readers
                    continue
                if not users:
                    # never instantiated (enum, namespace, mixin...): fall back to file users
                    tidx = self.m.touch.get(path)
                    users = self.m.nums(self.m.set(tidx)) if tidx is not None else set()
                    if not users:
                        log(f"  {path}: class {r[1]} has no known users -> all tests")
                        return ALL
                if is_test_file:
                    t = self.test_module_tests(path)  # e.g. a base TestCase subclassed elsewhere
                    if t is ALL:
                        return ALL
                    users |= t
                out |= users
            else:  # module-level change
                if path.startswith("tests/test_") or path.startswith("tests/dialects/test_"):
                    t = self.test_module_tests(path)  # runs at import of every importing test module
                    if t is ALL:
                        return ALL
                    out |= t
                    continue
                r2 = self.module_change(path, r[1] if len(r) > 1 else None, r[2] if len(r) > 2 else None)
                if r2 is ALL:
                    return ALL
                out |= r2
        if d.regions and path in self.m.opens:  # tests that read this source file as data
            out |= self.m.nums(self.m.set(self.m.opens[path]))
        return out

    def select(self, patch_text):
        self.related = set()
        files = parse_patch(patch_text)
        if not files:
            return set()
        chosen = set()
        trim = []  # sqlglot modules whose in-place edit alone forces ALL
        self.trim_lines = []  # (path, lines): import-time function-body lines (ALL unless trimmed)
        self.no_trim = False  # set by rules whose ALL reaches shared state: never trimmed
        for f in files:
            path = f["new"] or f["old"]
            old_path = f["old"]
            if f["binary"]:
                if path.startswith("sqlglot/"):
                    return ALL
                for p in (f["old"], f["new"]):
                    if p and p in self.m.opens:
                        chosen |= self.m.nums(self.m.set(self.m.opens[p]))
                continue
            if (path in GLOBAL_CONFIG or (old_path and old_path in GLOBAL_CONFIG)
                    or path.rsplit("/", 1)[-1] == "conftest.py"
                    or (path.startswith("tests/") and path.endswith("__init__.py") and f["old"] is None)):
                return ALL
            if f["old"] and f["new"] and f["old"] != f["new"] and f["old"].endswith(".py"):
                # renamed python module: importers of the old name break
                return ALL
            if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
                old_text = None
                if old_path:
                    try:
                        old_text = (SRC / old_path).read_text(encoding="utf-8")
                    except OSError:
                        log(f"  {old_path}: not in pinned source -> all tests")
                        return ALL
                if not f["hunks"] and f["new"] and f["old"]:
                    continue  # mode-only change
                self.sole = len(files) == 1
                r = self.py_change(path, old_text, f["new"] is None, f["hunks"])
                if r is ALL:
                    if (not self.no_trim and old_text is not None and f["new"] == old_path and f["hunks"]
                            and path.startswith("sqlglot/") and self.heavy_closures()):
                        trim.append((path, old_text, f["hunks"]))  # in-place module edit: maybe trimmable
                        continue
                    return ALL
                chosen |= r
            else:
                hit = False
                for p in (f["old"], f["new"]):
                    if p and p in self.m.opens:
                        chosen |= self.m.nums(self.m.set(self.m.opens[p]))
                        hit = True
                if not hit and path.startswith("tests/") and "fixdir" not in OFF:
                    # data file no test opened at the pinned commit (new or listed by
                    # directory): take the readers of its sibling files
                    folder = path.rsplit("/", 1)[0] + "/"
                    for p, idx in self.m.opens.items():
                        if p.startswith(folder):
                            chosen |= self.m.nums(self.m.set(idx))
                if path.endswith(".py") and "/" in path and not path.startswith(("sqlglot/", "tests/")):
                    # e.g. sqlglotc/*.py: exercised by tests/test_sqlglotc.py
                    chosen |= self.file_tests(f"tests/test_{path.split('/', 1)[0]}.py")
        if trim or self.trim_lines:
            return self.trimmed_all(trim, chosen)
        return chosen


# ----------------------------------------------------------------------------- packing


def pack(nums, ms):
    if not nums:
        return [], 0
    items = sorted(nums, key=lambda n: (-ms[n], n))
    best, best_cost = None, None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return best, best_cost


def fill_idle(workers, ms, candidates):
    """Add candidate tests to workers' idle time; the busiest load never grows."""
    if not workers:
        return 0
    loads = [sum(ms[n] for n in w) for w in workers]
    cap = max(loads)
    added = 0
    for n in sorted(candidates, key=lambda n: (ms[n], n)):
        d = ms[n]
        i = max(range(len(workers)), key=lambda j: (cap - loads[j], -j))
        if cap - loads[i] < d:
            if d == 0:
                continue
            break  # sorted by duration: nothing longer fits either
        workers[i].append(n)
        loads[i] += d
        added += 1
    return added


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
    sel_file = {t["number"]: t["file"] for t in tests}
    try:
        sel = Selector(Map(MAP, tests))
    except Exception as e:  # no usable map: run everything rather than crash
        log(f"map unavailable ({e!r}); selecting all tests")
        sel = None
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            chosen = sel.select(patch.read_text(encoding="utf-8", errors="replace")) if sel else ALL
        except Exception as e:  # never crash, never miss: fall back to everything
            log(f"{cid}: analysis error {e!r} -> all tests")
            chosen = ALL
        if chosen is ALL:
            chosen = every
        elif isinstance(chosen, TrimmedAll):
            chosen = every - chosen.exclude
        chosen = {n for n in chosen if n in ms}
        workers, cost = pack(chosen, ms)
        extra = 0
        if sel and workers and len(chosen) < len(ms) and "fill" not in OFF:
            related = set(sel.related)
            for f in {sel_file[n] for n in chosen}:
                related |= sel.m.by_file.get(f, set())
            extra = fill_idle(workers, ms, (related & set(ms)) - chosen)
        total += cost
        log(f"{cid}: {len(chosen)} tests (+{extra} free), {len(workers)} workers, {cost / 1000:.3f}s")
        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    log(f"estimated simulated cost without misses: {total / 1000:.3f}s")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--build-index"]:  # image build only (docker/Dockerfile), never at scoring time
        Selector.__new__(Selector).build_static_index(sys.argv[2])
    else:
        main()
