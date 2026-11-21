"""Per-test value trace (pytest plugin + import hook) over the pinned sqlglot sources.

Two kinds of sites are instrumented by rewriting sqlglot's modules as they are imported:

  dict   every dict literal assigned to a name directly in a class body
         (`NUMERIC_ESCAPES = {"x": ..., "u": ...}`, `SHOW_PARSERS = {**Base.SHOW_PARSERS, ...}`).
         The dict is replaced by a recording dict subclass that logs, per test, WHICH explicit
         literal entry's VALUE was read (d[k], d.get(k), setdefault, pop) and every bulk read
         that exposes all values at once (items(), values(), copy(), ==, repr, |, pickling,
         dict(d)/{**d} go through keys()+__getitem__). Key-only uses (in, len, iteration,
         keys()) expose no value and are not recorded.
  cmp    every single-operator <, <=, >, >= comparison. Each execution also evaluates the
         other five relational/equality operators on the same operands and logs, per test,
         each alternative whose result would have differed (an exception on either side
         counts as differing).

A value change of one dict entry, or an operator change at a comparison, can only influence
a test that read that entry's value / executed that comparison with a differing result: until
then the program runs exactly as the original. The selector uses this to narrow such patches.
Reads outside any test (import, collection, session fixtures, subprocesses) are recorded under
those contexts and make the selector fall back to its normal rules.

Contexts: "<nodeid>", "<nodeid>|setup", "<nodeid>|teardown", "collect:<file>", "<session>",
"<between>", "<subprocess>". Output: $VTRACE_OUT/vt-<pid>.json per process.

  python -m pytest tests -p vtrace_plugin ...   (mapgen/ and mapgen/vtsite on PYTHONPATH, VTRACE_SRC, VTRACE_OUT set)
  python mapgen/vtrace.py merge <dir> <tests.json> <out vmap.json.gz>
"""

import ast
import atexit
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

_current = ["<subprocess>"]  # the pytest plugin hooks below overwrite this in pytest processes
DREADS = {}  # site -> {entry index | "*": set(ctx)}
DMETA = {}  # site -> [n explicit entries, ok]
CMP = {}  # site -> {alt op index: set(ctx)}
_CMP_DONE = {}  # (site, ctx) -> bitmask of alternatives already recorded
_OPS = (operator.lt, operator.le, operator.gt, operator.ge, operator.eq, operator.ne)
_OPNAMES = ("Lt", "LtE", "Gt", "GtE", "Eq", "NotEq")
_REL = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3}
_ALL6 = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Eq: 4, ast.NotEq: 5}
CV = {}  # site -> {ctx: set of observed non-constant operand values, or "U" (unknown)}
CV_MAX = 64  # distinct values per (site, context); beyond -> "U"
_PLAIN = (int, float, str, bytes, bool, type(None))


def _plain(x, depth=0):
    """Builtin value whose comparisons are pure (no user __lt__/__eq__, no side effects)."""
    t = type(x)
    if t in _PLAIN:
        return True
    if t in (tuple, list) and depth < 3 and len(x) <= 64:
        return all(_plain(e, depth + 1) for e in x)
    return False


def _cv_rec(site, ctx, other, const, op):
    """Value of the non-constant operand of a single-constant comparison. Only exact int/str values
    are recorded (== / != with int or str constants, relational ops with int constant and int value);
    anything else (bools, floats, subclasses, custom objects, mixed types for <) -> unknown."""
    s = CV.get(site)
    if s is None:
        s = CV[site] = {}
    cur = s.get(ctx)
    if cur == "U":
        return
    t = type(other)
    ok = (t is int or t is str) and (op >= 4 or (t is int and type(const) is int))
    if not ok:
        s[ctx] = "U"
        return
    if cur is None:
        cur = s[ctx] = set()
    cur.add(other)
    if len(cur) > CV_MAX:
        s[ctx] = "U"
_FULL = 0b111111
DONE = set()  # nodeids whose call phase completed (passed/skipped) under tracing
# >0 while a sqlglot module body executes. sqlglot imports dialect modules lazily (inside the
# first test that needs one), so class creation, metaclasses and derived tables (MySQL's
# INVERSE_TIME_MAPPING built from TIME_MAPPING) run within some test; their reads feed state
# every later test uses. They are import-time work, never one test's: context "<import>".
_IMPORTING = [0]


def _ctx():
    return "<import>" if _IMPORTING[0] else _current[0]


def _rec(site, idx):
    ctx = _ctx()
    s = DREADS.get(site)
    if s is None:
        s = DREADS[site] = {}
    t = s.get(idx)
    if t is None:
        t = s[idx] = set()
    t.add(ctx)


class VDict(dict):
    """dict that records value reads of its explicit literal entries (see module doc)."""

    def _vt_read(self, k):
        try:
            i = self.__dict__["_vt_idx"].get(k)
        except (KeyError, TypeError):  # unhashable key / unpickled copy without index
            i = None
        if i is not None:
            for j in i:
                _rec(self.__dict__["_vt_site"], j)

    def _vt_bulk(self):
        site = self.__dict__.get("_vt_site")
        if site is not None:
            _rec(site, "*")

    def __getitem__(self, k):
        self._vt_read(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._vt_read(k)
        return dict.get(self, k, default)

    def setdefault(self, k, default=None):
        self._vt_read(k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *default):
        self._vt_read(k)
        return dict.pop(self, k, *default)

    def popitem(self):
        self._vt_bulk()
        return dict.popitem(self)

    def items(self):
        self._vt_bulk()
        return dict.items(self)

    def values(self):
        self._vt_bulk()
        return dict.values(self)

    def copy(self):
        self._vt_bulk()
        return dict.copy(self)

    def __eq__(self, other):
        self._vt_bulk()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._vt_bulk()
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        self._vt_bulk()
        return dict.__repr__(self)

    def __or__(self, other):
        self._vt_bulk()
        return dict.__or__(self, other)

    def __ror__(self, other):
        self._vt_bulk()
        return dict.__ror__(self, other)

    def __reduce_ex__(self, proto):
        self._vt_bulk()
        return dict(self).__reduce_ex__(proto)  # copies/pickles become plain dicts

    def __iter__(self):  # keys only, but overriding it forces C merges through keys()+[]
        return dict.__iter__(self)


def _vt_dict(site, d, keys):
    v = VDict(d)
    idx = {}
    ok = True
    for i, k in enumerate(keys):
        try:
            idx.setdefault(k, []).append(i)
        except TypeError:
            ok = False
    if any(len(v_) > 1 for v_ in idx.values()):
        ok = False  # duplicate explicit keys: the later value wins; positions ambiguous
    v.__dict__["_vt_site"] = site
    v.__dict__["_vt_idx"] = idx
    m = DMETA.get(site)
    DMETA[site] = [len(keys), ok and (m is None or m[1])]
    return v


def _vt_cmp(site, a, b, op, cside=-1):
    """Original comparison `a <op> b` (operands evaluated once, in order, by the caller).
    cside: 0 / 1 = the left / right operand is the site's only int/str constant (-1: none)."""
    ctx = _ctx()
    try:
        r = _OPS[op](a, b)
    except BaseException:
        if cside >= 0:
            CV.setdefault(site, {})[ctx] = "U"
        if op < 4:
            _cmp_rec(site, ctx, _FULL & ~(1 << op))
            _CMP_DONE[(site, ctx)] = _FULL
        raise
    if cside >= 0:
        _cv_rec(site, ctx, b if cside == 0 else a, a if cside == 0 else b, op)
    if op >= 4:
        return r
    key = (site, ctx)
    done = _CMP_DONE.get(key, 1 << op)
    if done == _FULL:
        return r
    if not (_plain(a) and _plain(b)):
        # custom/unknown operand types: never evaluate other operators (user code, side effects)
        _cmp_rec(site, ctx, _FULL & ~done)
        _CMP_DONE[key] = _FULL
        return r
    new = 0
    for j in range(6):
        if done >> j & 1:
            continue
        try:
            rj = _OPS[j](a, b)
            same = type(rj) is type(r) and (rj is r or (type(r) is bool and rj == r))
        except BaseException:
            same = False
        if not same:
            new |= 1 << j
    if new:
        _cmp_rec(site, ctx, new)
        _CMP_DONE[key] = done | new
    elif key not in _CMP_DONE:
        _CMP_DONE[key] = done
    return r


def _cmp_rec(site, ctx, mask):
    s = CMP.get(site)
    if s is None:
        s = CMP[site] = {}
    for j in range(6):
        if mask >> j & 1:
            s.setdefault(j, set()).add(ctx)


_KEY_OK = (ast.Constant, ast.Name, ast.Attribute)


def _plain_key(k):
    if isinstance(k, ast.Attribute):
        return _plain_key(k.value)
    return isinstance(k, (ast.Constant, ast.Name))


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for st in node.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                val = st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                val = st.value
            else:
                continue
            if not isinstance(val, ast.Dict):
                continue
            explicit = [k for k in val.keys if k is not None]
            if not all(_plain_key(k) for k in explicit):
                continue
            site = f"{self.rel}:{val.lineno}:{val.col_offset}"
            call = ast.Call(func=ast.Name(id="__vt_dict__", ctx=ast.Load()),
                            args=[ast.Constant(site), val,
                                  ast.Tuple(elts=[_copy_load(k) for k in explicit], ctx=ast.Load())],
                            keywords=[])
            st.value = ast.copy_location(call, val)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _ALL6:
            return node
        sides = [node.left, node.comparators[0]]
        consts = [i for i, x in enumerate(sides) if isinstance(x, ast.Constant) and type(x.value) in (int, str)]
        cside = consts[0] if len(consts) == 1 else -1
        if type(node.ops[0]) not in _REL and cside < 0:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name(id="__vt_cmp__", ctx=ast.Load()),
                        args=[ast.Constant(site), node.left, node.comparators[0],
                              ast.Constant(_ALL6[type(node.ops[0])]), ast.Constant(cside)],
                        keywords=[])
        return ast.copy_location(call, node)


def _copy_load(k):
    return ast.parse(ast.unparse(k), mode="eval").body


class _Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, rel):
        super().__init__(fullname, path)
        self.rel = rel

    def source_to_code(self, data, path, *, _optimize=-1):
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(_Rewrite(self.rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def exec_module(self, module):
        module.__dict__["__vt_dict__"] = _vt_dict
        module.__dict__["__vt_cmp__"] = _vt_cmp
        _IMPORTING[0] += 1
        try:
            super().exec_module(module)
        finally:
            _IMPORTING[0] -= 1

    def get_code(self, fullname):  # never read/write bytecode caches
        return self.source_to_code(self.get_data(self.path), self.path)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = os.path.realpath(root)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        base = os.path.join(self.root, *fullname.split("."))
        for cand, pkg in ((os.path.join(base, "__init__.py"), True), (base + ".py", False)):
            if os.path.isfile(cand):
                rel = os.path.relpath(cand, self.root)
                spec = importlib.machinery.ModuleSpec(fullname, _Loader(fullname, cand, rel), origin=cand, is_package=pkg)
                spec.has_location = True
                if pkg:
                    spec.submodule_search_locations = [os.path.dirname(cand)]
                return spec
        return None


def _dump():
    out = os.environ.get("VTRACE_OUT")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    data = {
        "dict": {s: {str(i): sorted(c) for i, c in v.items()} for s, v in DREADS.items()},
        "dmeta": DMETA,
        "cmp": {s: {_OPNAMES[j]: sorted(c) for j, c in v.items()} for s, v in CMP.items()},
        "cmp_seen": sorted({s for s, _ in _CMP_DONE}),
        "cv": {s: {c: (v if v == "U" else sorted(v, key=lambda x: (type(x).__name__, x))) for c, v in d.items()}
               for s, d in CV.items()},
        "done": sorted(DONE),
    }
    with open(os.path.join(out, f"vt-{os.getpid()}.json"), "w") as f:
        json.dump(data, f)


def install():
    root = os.environ.get("VTRACE_SRC")
    if not root or any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    if any(m == "sqlglot" or m.startswith("sqlglot.") for m in sys.modules):
        raise RuntimeError("vtrace: sqlglot imported before the hook was installed")
    sys.meta_path.insert(0, _Finder(root))
    atexit.register(_dump)


# ----------------------------------------------------------------------------- merge


def merge(indir, tests_json, outfile):
    import gzip

    tests = [t["id"] for t in json.load(open(tests_json))["tests"]]
    files = sorted({t.split("::")[0] for t in tests})
    pos = {t: i for i, t in enumerate(tests)}
    by_file = {}
    for t, i in pos.items():
        by_file.setdefault(t.split("::")[0], set()).add(i)
    dreads, dmeta, cmp, seen = {}, {}, {}, set()
    cvals, done = {}, set()  # site -> {("int"|"str", value) | "U": set(ctx)}
    for name in sorted(os.listdir(indir)):
        if not name.startswith("vt-"):
            continue
        d = json.load(open(os.path.join(indir, name)))
        for s, v in d["dict"].items():
            for i, c in v.items():
                dreads.setdefault(s, {}).setdefault(i, set()).update(c)
        for s, (n, ok) in d["dmeta"].items():
            m = dmeta.get(s)
            dmeta[s] = [n, ok and (m is None or (m[0] == n and m[1]))]
        for s, v in d["cmp"].items():
            for j, c in v.items():
                cmp.setdefault(s, {}).setdefault(j, set()).update(c)
        seen.update(d["cmp_seen"])
        done.update(d.get("done", ()))
        for site, per in d.get("cv", {}).items():
            e = cvals.setdefault(site, {})
            for ctx, vals in per.items():
                if vals == "U":
                    e.setdefault("U", set()).add(ctx)
                else:
                    for v in vals:
                        e.setdefault((type(v).__name__, v), set()).add(ctx)
    sets, set_idx = [], {}

    def encode(ctxs):
        """-> (bitset index, all-tests flag). Shared setup/teardown/collection work is
        credited to every test of the file; anything outside a test means ALL."""
        bits, all_ = 0, False
        for c in ctxs:
            base = c.split("|")[0]
            if c.startswith("collect:"):
                for i in by_file.get(c[len("collect:"):], ()):
                    bits |= 1 << i
            elif base in pos:
                if "|" in c:
                    for i in by_file.get(base.split("::")[0], ()):
                        bits |= 1 << i
                else:
                    bits |= 1 << pos[base]
            elif "::" in base and base.split("::")[0] in files:
                # parametrized/subtest ids not in tests.json: credit the whole file
                for i in by_file.get(base.split("::")[0], ()):
                    bits |= 1 << i
            else:
                all_ = True
        h = format(bits, "x")
        if h not in set_idx:
            set_idx[h] = len(sets)
            sets.append(h)
        return set_idx[h], all_

    out = {"tests": tests, "sets": sets, "dict": {}, "cmp": {}, "cmp_seen": sorted(seen)}
    for s, (n, ok) in dmeta.items():
        entry = {"n": n, "ok": ok, "r": {}, "all": []}
        for i, c in dreads.get(s, {}).items():
            idx, all_ = encode(c)
            entry["r"][i] = idx
            if all_:
                entry["all"].append(i)
        out["dict"][s] = entry
    for s, v in cmp.items():
        e = {"r": {}, "all": []}
        for j, c in v.items():
            idx, all_ = encode(c)
            e["r"][j] = idx
            if all_:
                e["all"].append(j)
        out["cmp"][s] = e
    out["cv"] = {}
    for site, e in cvals.items():
        rec = {"vals": [], "unk": None}
        for k, ctxs in e.items():
            idx, all_ = encode(ctxs)
            if k == "U":
                rec["unk"] = [idx, all_]
            else:
                rec["vals"].append([k[0], k[1], idx, all_])
        out["cv"][site] = rec
    # tests that never completed under tracing: every value-based narrowing must keep them
    untraced = {i for t, i in pos.items() if t not in done}
    out["untraced"] = sorted(untraced)
    out["sets"] = sets
    with gzip.open(outfile, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"dict sites {len(out['dict'])}, cmp sites seen {len(seen)} (with differing alternatives {len(cmp)}), "
          f"sets {len(sets)} -> {outfile}")


if __name__ == "__main__":
    if sys.argv[1] == "merge":
        merge(sys.argv[2], sys.argv[3], sys.argv[4])
