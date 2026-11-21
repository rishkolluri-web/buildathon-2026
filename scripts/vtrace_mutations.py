#!/usr/bin/env python3
"""Adversarial ground-truth corpus for the value-trace rules (vmap.json.gz):

  d-*  class-body dict entry value changes (constant leaf: int+1, bool flip, str edit), split
       into entries the trace says NO test reads (rule selects nothing) and entries some tests
       read (rule selects only those)
  r-*  relational operator changes (< <-> <=, > <-> >=, and direction flips < <-> >) at traced
       sites, split by whether the trace saw a differing result

Sites are drawn at random (seeded) from those where the rule applies; within each stratum the
sites with the largest coverage (normal-rule selection) come first, so the corpus probes the
narrowings that matter. Ground truth: stress_mutate.py truth --out <dir>.

  python3 scripts/vtrace_mutations.py --src ../sqlglot --vmap vmap.json.gz --out ../stress_d
"""

import argparse
import ast
import difflib
import gzip
import json
import random
from pathlib import Path

OPTOK = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
NAMES = {ast.Lt: "Lt", ast.LtE: "LtE", ast.Gt: "Gt", ast.GtE: "GtE"}
BOUNDARY = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
FLIP = {ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE}


def udiff(path, old, new):
    a, b = old.splitlines(True), new.splitlines(True)
    return f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}"))


def const_leaf(v):
    """First mutable Constant directly usable: the value itself or an element of a tuple/list."""
    cands = [v] if isinstance(v, ast.Constant) else [e for e in getattr(v, "elts", []) if isinstance(e, ast.Constant)]
    for c in cands:
        if c.lineno == c.end_lineno and isinstance(c.value, (bool, int, str, float)) and c.value is not None:
            return c
    return None


def mutated(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    return (value[:-1] + ("Y" if value[-1:] != "Y" else "Z")) if value else "Y"


def replace_span(lines, node, text):
    i = node.lineno - 1
    line = lines[i]
    seg = line[node.col_offset:node.end_col_offset]
    if not seg or (isinstance(node.value, str) and seg[:1] not in "'\""):
        return None  # prefixed/implicitly concatenated strings: skip
    lines = list(lines)
    lines[i] = line[:node.col_offset] + text + line[node.end_col_offset:]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--vmap", required=True)
    ap.add_argument("--map", default=str(Path(__file__).resolve().parent.parent / "map.json.gz"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--dzero", type=int, default=14)
    ap.add_argument("--dsome", type=int, default=10)
    ap.add_argument("--rzero", type=int, default=10)
    ap.add_argument("--rsome", type=int, default=6)
    ap.add_argument("--rflip", type=int, default=4)
    ap.add_argument("--fzero", type=int, default=0, help="closure-factory call argument changes, unread entries")
    ap.add_argument("--fsome", type=int, default=0, help="closure-factory call argument changes, read entries")
    ap.add_argument("--prefix", default="", help="mutant id prefix (keeps ids unique across corpora)")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    src, out = Path(a.src), Path(a.out)
    v = json.load(gzip.open(a.vmap, "rt"))
    m = json.load(gzip.open(a.map, "rt"))

    def cov(path, line):  # number of tests the normal coverage rule would select for this line
        idx = m["lines"].get(path, {}).get(str(line))
        return bin(int(m["sets"][idx], 16)).count("1") if idx is not None else 0

    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ["BR_SRC"] = str(src)
    import selector as S
    fsel = S.Selector.__new__(S.Selector)
    fact_c = {"zero": [], "some": []}
    dict_c = {"zero": [], "some": []}
    cmp_c = {"zero": [], "some": [], "flip": []}
    for f in sorted((src / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(src))
        text = f.read_text()
        tree = ast.parse(text)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for st in cls.body:
                val = st.value if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
                if not isinstance(val, ast.Dict):
                    continue
                e = v["dict"].get(f"{rel}:{val.lineno}:{val.col_offset}")
                if not e or not e["ok"] or "*" in e["all"]:
                    continue
                j = -1
                for k, value in zip(val.keys, val.values):
                    if k is None:
                        continue
                    j += 1
                    if str(j) in e["all"]:
                        continue
                    reads = str(j) in e["r"] or "*" in e["r"]
                    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                            and fsel.closure_factory(rel, value.func.id)):
                        args = [x for x in value.args + [k.value for k in value.keywords]
                                if isinstance(x, ast.Constant) and isinstance(x.value, (bool, int, str))
                                and x.lineno == x.end_lineno]
                        if args:
                            fact_c["some" if reads else "zero"].append((rel, val.lineno, j, args[0], cls.name))
                        continue
                    c = const_leaf(value)
                    if c is None:
                        continue
                    dict_c["some" if reads else "zero"].append((rel, val.lineno, j, c, cls.name))
        for c in ast.walk(tree):
            if not (isinstance(c, ast.Compare) and len(c.ops) == 1 and type(c.ops[0]) in OPTOK):
                continue
            site = f"{rel}:{c.lineno}:{c.col_offset}"
            if site not in set(v["cmp_seen"]) or c.lineno != c.end_lineno:
                continue
            e = v["cmp"].get(site, {"r": {}, "all": []})
            op = type(c.ops[0])
            for kind, new in (("b", BOUNDARY[op]), ("f", FLIP[op])):
                if NAMES[new] in e["all"]:
                    continue
                differs = NAMES[new] in e["r"]
                bucket = "flip" if kind == "f" else ("some" if differs else "zero")
                cmp_c[bucket].append((rel, c, op, new, cov(rel, c.lineno)))

    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}

    def take(cands, n, key):
        rnd.shuffle(cands)
        cands.sort(key=key, reverse=True)  # largest normal-rule coverage first (stable after shuffle)
        return cands[: n * 3]  # oversample; dedupe by file below

    seen_files = {}
    for bucket, n, pool, tag in (("zero", a.dzero, dict_c, "d"), ("some", a.dsome, dict_c, "d"),
                                 ("zero", a.fzero, fact_c, "f"), ("some", a.fsome, fact_c, "f")):
        got = 0
        for rel, line, j, c, cname in take(pool[bucket], n, key=lambda x: 0):
            if got >= n:
                break
            if seen_files.get((tag + bucket, rel), 0) >= 2:
                continue
            text = (src / rel).read_text()
            new_lines = replace_span(text.split("\n"), c, repr(mutated(c.value)))
            if new_lines is None:
                continue
            new = "\n".join(new_lines)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            mid = f"{a.prefix}{tag}{bucket[0]}{got + 1:02d}-{Path(rel).stem}-{line}-{j}"
            (out / "commits" / f"{mid}.patch").write_text(udiff(rel, text, new))
            meta[mid] = {"kind": f"{'factory-arg' if tag == 'f' else 'dict-value'}-{bucket}", "file": rel,
                         "site": f"{rel}:{line}", "entry": j, "class": cname}
            seen_files[(tag + bucket, rel)] = seen_files.get((tag + bucket, rel), 0) + 1
            got += 1
    for bucket, n in (("zero", a.rzero), ("some", a.rsome), ("flip", a.rflip)):
        got = 0
        for rel, c, op, new, cv in take(cmp_c[bucket], n, key=lambda x: x[4]):
            if got >= n:
                break
            if seen_files.get((bucket, rel), 0) >= 2:
                continue
            text = (src / rel).read_text()
            lines = text.split("\n")
            line = lines[c.lineno - 1]
            seg = line[c.left.end_col_offset:c.comparators[0].col_offset]
            tok = OPTOK[op]
            pos = seg.find(tok)
            if pos < 0 or (tok in ("<", ">") and seg[pos:pos + 2] in ("<=", ">=")):
                continue
            s0 = c.left.end_col_offset + pos
            lines[c.lineno - 1] = line[:s0] + OPTOK[new] + line[s0 + len(tok):]
            newtext = "\n".join(lines)
            try:
                t2 = ast.parse(newtext)
            except SyntaxError:
                continue
            mid = f"r{bucket[0]}{got + 1:02d}-{Path(rel).stem}-{c.lineno}"
            (out / "commits" / f"{mid}.patch").write_text(udiff(rel, text, newtext))
            meta[mid] = {"kind": f"relational-{bucket}", "file": rel, "site": f"{rel}:{c.lineno}:{c.col_offset}",
                         "op": f"{tok} -> {OPTOK[new]}", "coverage_tests": cv}
            seen_files[(bucket, rel)] = seen_files.get((bucket, rel), 0) + 1
            got += 1
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"candidates: factory zero {len(fact_c['zero'])} some {len(fact_c['some'])}; "
          f"dict zero {len(dict_c['zero'])} some {len(dict_c['some'])}; "
          f"cmp zero {len(cmp_c['zero'])} some {len(cmp_c['some'])} flip {len(cmp_c['flip'])}")
    print(f"generated {len(meta)} mutants in {out}")


if __name__ == "__main__":
    main()
