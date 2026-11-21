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

WORKER_MS = 2000
MAX_WORKERS = 8
GLOBAL_CONFIG = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py", "tests/conftest.py"}
ALL = object()  # sentinel: select every test


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
                self.regions.append(("class", ctx[1]))
            else:
                self.regions.append(("module",))

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
                if ctx[0] == "class" and o.name != n.name:
                    self.regions.append(("class", ctx[1]))
                elif ctx[0] == "module" and o.name != n.name:
                    self.regions.append(("module",))
                return
            if isinstance(o, ast.ClassDef):
                qual = (ctx[1] + "." if ctx[0] == "class" and ctx[1] else "") + o.name
                self.regions.append(("class", qual) if ctx[0] != "func" else ("lines", span(o)))
                if o.name != n.name and ctx[0] == "module":
                    self.regions.append(("module",))
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
            self.regions.append(("lines", lam))
            return
        self.unit_change(o, ctx)

    def unit_change(self, o, ctx):
        if ctx[0] == "class":
            self.regions.append(("class", ctx[1]))
        elif ctx[0] == "func":
            self.regions.append(("lines", span(o)))
        else:
            self.regions.append(("module",))


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


def module_name(path):
    p = path[:-3].replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


class Selector:
    def __init__(self, m: Map):
        self.m = m

    def lines_bits(self, path, lines):
        per = self.m.lines.get(path, {})
        b = 0
        for l in lines:
            idx = per.get(str(l))
            if idx is not None:
                b |= self.m.set(idx)
        return b

    def file_tests(self, path):
        return self.m.by_file.get(path, set())

    def py_change(self, path, old_text, deleted, hunks):
        """-> (set of numbers) or ALL"""
        is_test_file = path.startswith("tests/")
        if deleted:  # deleted module
            return ALL if not is_test_file or "helpers" in path else self.file_tests(path)
        if old_text is None:  # brand-new file: nothing imports it at the pinned commit
            return set()
        old_lines = old_text.split("\n")
        new_lines, old_touched, new_touched = apply_hunks(old_lines, hunks)
        try:
            new_tree = ast.parse("\n".join(new_lines))
        except SyntaxError:
            log(f"  {path}: syntax error after patch -> import breaks")
            return ALL if not path.startswith("tests/test_") and not path.startswith("tests/dialects/test_") else self.file_tests(path)
        old_tree = ast.parse(old_text)
        d = Differ(old_lines, new_lines, old_touched, new_touched)
        d.body(old_tree.body, new_tree.body, ("module", None), None)
        out = set()
        mod = module_name(path)
        for r in d.regions:
            if r[0] == "lines":
                out |= self.m.nums(self.lines_bits(path, r[1]))
            elif r[0] == "class":
                idx = self.m.users.get(f"{mod}:{r[1]}")
                users = self.m.nums(self.m.set(idx)) if idx is not None else set()
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
                else:
                    log(f"  {path}: module-level change -> all tests")
                    return ALL
        return out

    def select(self, patch_text):
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
            if path in GLOBAL_CONFIG or (old_path and old_path in GLOBAL_CONFIG):
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
                r = self.py_change(path, old_text, f["new"] is None, f["hunks"])
                if r is ALL:
                    return ALL
                chosen |= r
            else:
                for p in (f["old"], f["new"]):
                    if p and p in self.m.opens:
                        chosen |= self.m.nums(self.m.set(self.m.opens[p]))
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


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
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
        total += cost
        log(f"{cid}: {len(chosen)} tests, {len(workers)} workers, {cost / 1000:.3f}s")
        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    log(f"estimated simulated cost without misses: {total / 1000:.3f}s")


if __name__ == "__main__":
    main()
