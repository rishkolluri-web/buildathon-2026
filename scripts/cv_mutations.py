#!/usr/bin/env python3
"""Independent ground-truth corpus for the comparison-constant rule (selector.const_change).

Mutates the single int/str constant of executed single-operator comparisons in the pinned sources:
  ri-*  int relational threshold +1 / -1
  ei-*  int equality constant -> another int (+1, or 0 <-> 1)
  es-*  str equality constant -> another str (last char changed)
  ci-*  negative controls: sites whose values were also seen outside tests (import/session)
  cu-*  negative controls: sites with unknown observations (non-int/str operands, overflow)
Within each stratum, sites with the largest line coverage come first (where narrowing matters).

  python3 scripts/cv_mutations.py --src ../sqlglot --vmap vmap.json.gz --out ../stress_cv
"""

import argparse
import ast
import difflib
import gzip
import json
import random
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OPS = {ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq}


def udiff(path, old, new):
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--vmap", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--per", type=int, default=8)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    src, out = Path(a.src), Path(a.out)
    v = json.load(gzip.open(a.vmap, "rt"))
    m = json.load(gzip.open(REPO / "map.json.gz", "rt"))

    def cov(rel, ln):
        idx = m["lines"].get(rel, {}).get(str(ln))
        return bin(int(m["sets"][idx], 16)).count("1") if idx is not None else 0

    pools = {k: [] for k in ("ri", "ei", "es", "ci", "cu")}
    for f in sorted((src / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(src))
        text = f.read_text()
        lines = text.split("\n")
        for c in ast.walk(ast.parse(text)):
            if not (isinstance(c, ast.Compare) and len(c.ops) == 1 and type(c.ops[0]) in OPS):
                continue
            sides = [c.left, c.comparators[0]]
            ks = [x for x in sides if isinstance(x, ast.Constant) and type(x.value) in (int, str)]
            if len(ks) != 1 or ks[0].lineno != ks[0].end_lineno:
                continue
            k = ks[0]
            e = v["cv"].get(f"{rel}:{c.lineno}:{c.col_offset}")
            if e is None:
                continue
            seg = lines[k.lineno - 1][k.col_offset:k.end_col_offset]
            if type(k.value) is int:
                if not seg.lstrip("-").isdigit():
                    continue
                if type(c.ops[0]) in (ast.Eq, ast.NotEq):
                    newv, bucket = (1 - k.value if k.value in (0, 1) else k.value + 1), "ei"
                else:
                    newv, bucket = k.value + rnd.choice((1, -1)), "ri"
            else:
                if seg[:1] not in "'\"" or not k.value:
                    continue
                newv, bucket = k.value[:-1] + ("Y" if k.value[-1:] != "Y" else "Z"), "es"
            if e["unk"]:
                bucket = "cu"
            elif any(x[3] for x in e["vals"]):
                bucket = "ci"
            new_line = lines[k.lineno - 1][:k.col_offset] + repr(newv) + lines[k.lineno - 1][k.end_col_offset:]
            new = list(lines)
            new[k.lineno - 1] = new_line
            pools[bucket].append((cov(rel, c.lineno), rel, c.lineno, udiff(rel, text, "\n".join(new))))
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    for bucket, items in pools.items():
        rnd.shuffle(items)
        items.sort(key=lambda x: -x[0])
        n = a.per if bucket in ("ri", "ei", "es") else 4
        per_file, got = {}, 0
        for cv, rel, ln, patch in items:
            if got >= n or per_file.get(rel, 0) >= 2:
                continue
            per_file[rel] = per_file.get(rel, 0) + 1
            mid = f"{bucket}{got + 1:02d}-{Path(rel).stem}-{ln}"
            (out / "commits" / f"{mid}.patch").write_text(patch)
            meta[mid] = {"kind": f"cmp-constant-{bucket}", "file": rel, "line": ln, "coverage_tests": cv}
            got += 1
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print({k: len(x) for k, x in pools.items()}, "->", len(meta), "mutants")


if __name__ == "__main__":
    main()
