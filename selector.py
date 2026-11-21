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

WORKER_MS = 2000
MAX_WORKERS = 8
GLOBAL_CONFIG = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py", "tests/conftest.py"}
ALL = object()  # sentinel: select every test
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
        num_by_id = {t["id"]: t["number"] for t in tests}
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
            self.regions.append(("class", ctx[1], [o, n] if n is not None else None))
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


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.sole = False
        try:  # comparison sites where is/== are interchangeable on every traced execution
            self.eq_safe = set(json.loads(EQ.read_text())["safe"]) if "eqswap" not in OFF else set()
        except (OSError, ValueError, KeyError):
            self.eq_safe = set()

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

    def class_data_readers(self, path, stmts, is_test_file):
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

    def file_tests(self, path):
        return self.m.by_file.get(path, set())

    def py_change(self, path, old_text, deleted, hunks):
        """-> (set of numbers) or ALL"""
        is_test_file = path.startswith("tests/")
        if deleted:  # deleted module
            return ALL if not is_test_file or "helpers" in path else self.file_tests(path)
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
            return ALL if not path.startswith("tests/test_") and not path.startswith("tests/dialects/test_") else self.file_tests(path)
        old_tree = ast.parse(old_text)
        if self.sole and not is_test_file and self.eq_safe and swap_only_hunks(hunks):
            sites = swap_only_sites(old_tree, new_tree, old_touched | new_touched)
            if sites and all(f"{path}:{site}" in self.eq_safe for site in sites):
                # the patch only swaps is<->== / is not<->!= where every traced execution compared
                # operands for which identity and equality agree by construction: every
                # comparison returns what it did before, so the program runs unchanged
                log(f"  {path}: identity/equality swap at interchangeable site(s) {sites} -> no tests")
                return set()
        # def/class header lines (decorators, signature, defaults) execute at import to
        # *create* the function/class; their effect shows up only through callers and
        # class users, which the header-change rules already select. Only statements
        # inside bodies that actually ran at import count as import-time changes.
        imp_lines = self.m.imp.get(path, set()) if "imp" not in OFF else set()
        if imp_lines and "hdrimp" not in OFF:
            headers = set()
            for node in ast.walk(old_tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    headers |= header_lines(node)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_stub(node):
                        headers |= span(node)  # `def f(...): ...` stubs: body does nothing
            imp_lines = imp_lines - headers
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
            if r[0] == "lines":
                if not is_test_file and r[1] & imp_lines:
                    # runs while sqlglot is imported (metaclass, __init_subclass__,
                    # factories used in class bodies): every test inherits its effect
                    log(f"  {path}: import-time line(s) {sorted(r[1] & imp_lines)[:5]} -> all tests")
                    return ALL
                out |= self.m.nums(self.lines_bits(path, r[1]))
            elif r[0] == "lambda":
                out |= self.m.nums(self.lines_bits(path, r[1]))
            elif r[0] == "class":
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
                    self.class_data_readers(path, r[2] if len(r) > 2 else None, is_test_file)
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
                    users |= self.file_tests(path)
                out |= users
            else:  # module-level change
                if path.startswith("tests/test_") or path.startswith("tests/dialects/test_"):
                    out |= self.file_tests(path)
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
    main()
