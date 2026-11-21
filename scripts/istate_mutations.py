#!/usr/bin/env python3
"""Adversarial ground-truth corpus for the import-state rules (selector.import_state_diff):

  k-*  class-level settings whose names are read at import (derived state): a constant leaf
       changed, or one single-line dict entry deleted (like b1)
  i-*  function-body lines that run at import: an operator/constant/boolean change on the line

Every candidate is run through the working-tree selector; only patches where a new rule fires
AND narrows vs the selector with BR_OFF=istate,derived are kept, largest narrowing first.

  BR_SRC=../sqlglot BR_ISTATE=... python3 scripts/istate_mutations.py --src ../sqlglot --out ../stress_k --n 24 --ni 12
"""

import argparse
import ast
import difflib
import gzip
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def udiff(path, old, new):
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))


def mutate_const(line, rnd):
    """One constant edit on a source line: True<->False, an int literal +1, or a string edited."""
    opts = []
    for m in re.finditer(r"\bTrue\b|\bFalse\b", line):
        opts.append((m.start(), m.end(), "False" if m.group() == "True" else "True"))
    for m in re.finditer(r"(?<![\w.])\d+(?![\w.])", line):
        opts.append((m.start(), m.end(), str(int(m.group()) + 1)))
    for m in re.finditer(r'"([A-Za-z_ %]{1,30})"', line):
        opts.append((m.start(), m.end(), '"' + m.group(1)[:-1] + ("Y" if m.group(1)[-1:] != "Y" else "Z") + '"'))
    if not opts:
        return None
    s, e, t = rnd.choice(opts)
    return line[:s] + t + line[e:]


OPS = [(" == ", " != "), (" != ", " == "), (" < ", " <= "), (" > ", " >= "), (" and ", " or "), (" or ", " and "),
       ("not ", "")]


def mutate_op(line, rnd):
    opts = [(a, b) for a, b in OPS if a in line]
    if not opts:
        return mutate_const(line, rnd)
    a, b = rnd.choice(opts)
    i = line.index(a)
    return line[:i] + b + line[i + len(a):]


def run_selector(commits, src, off=""):
    data = Path(tempfile.mkdtemp())
    (data / "commits").mkdir()
    for mid, text in commits.items():
        (data / "commits" / f"{mid}.patch").write_text(text)
    (data / "tests.json").write_text((REPO.parent / "blast-radius-sample" / "sample" / "tests.json").read_text())
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src), BR_OFF=off)
    p = subprocess.run([sys.executable, str(REPO / "selector.py")], env=env, capture_output=True, text=True, check=True)
    sel = {}
    for line in p.stdout.splitlines():
        r = json.loads(line)
        sel[r["commit"]] = {n for w in r["workers"] for n in w}
    return sel, p.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--ni", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20261121)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    src, out = Path(a.src).resolve(), Path(a.out)
    m = json.load(gzip.open(REPO / "map.json.gz", "rt"))
    imp = {k: set(v) for k, v in m["imp"].items()}
    refs = m["refs"]
    cands = {}
    for f in sorted((src / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(src))
        text = f.read_text()
        lines = text.split("\n")
        tree = ast.parse(text)
        # k: class-level data whose bound name is read at import
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for st in cls.body:
                if not isinstance(st, (ast.Assign, ast.AnnAssign)):
                    continue
                tgt = st.targets[0] if isinstance(st, ast.Assign) else st.target
                if not isinstance(tgt, ast.Name):
                    continue
                e = refs.get(tgt.id, {})
                if not (e.get("impa") or e.get("impn")):
                    continue
                for ln in range(st.lineno, st.end_lineno + 1):
                    line = lines[ln - 1]
                    if st.end_lineno > st.lineno and ln not in (st.lineno, st.end_lineno) and line.strip().endswith(","):
                        if ":" in line and "**" not in line and "lambda" not in line:
                            new = lines[: ln - 1] + lines[ln:]
                            cands[f"kdel-{Path(rel).stem}-{ln}"] = (rel, text, "\n".join(new))
                    mut = mutate_const(line, rnd)
                    if mut and mut != line:
                        new = list(lines)
                        new[ln - 1] = mut
                        cands[f"kval-{Path(rel).stem}-{ln}"] = (rel, text, "\n".join(new))
        # i: function-body lines executed at import
        hdr = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = n.body
                hdr |= set(range(n.lineno, body[0].lineno)) if body else set()
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in funcs:
            for st in ast.walk(fn):
                if isinstance(st, ast.stmt) and st is not fn and st.lineno == st.end_lineno \
                        and st.lineno in imp.get(rel, ()) and st.lineno not in hdr \
                        and not isinstance(st, (ast.FunctionDef, ast.ClassDef, ast.Return)) or (
                        isinstance(st, ast.Return) and st.lineno in imp.get(rel, ())):
                    line = lines[st.lineno - 1]
                    mut = mutate_op(line, rnd)
                    if mut and mut != line:
                        new = list(lines)
                        new[st.lineno - 1] = mut
                        cands[f"iop-{Path(rel).stem}-{st.lineno}"] = (rel, text, "\n".join(new))
    valid = {}
    for mid, (rel, old, new) in cands.items():
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        valid[mid] = udiff(rel, old, new)
    keys = sorted(valid)
    rnd.shuffle(keys)
    keys = [k for k in keys if k.startswith("k")][:400] + [k for k in keys if k.startswith("i")][:200]
    print(f"candidates {len(cands)}, valid {len(valid)}, screening {len(keys)}", flush=True)
    sub = {k: valid[k] for k in keys}
    new_sel, err = run_selector(sub, src)
    old_sel, _ = run_selector(sub, src, "istate,derived")
    tests = json.loads((REPO.parent / "blast-radius-sample" / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: t["seconds"] for t in tests}
    narrowed = []
    for k in keys:
        if new_sel[k] < old_sel[k] or (new_sel[k] != old_sel[k] and not new_sel[k] >= old_sel[k]):
            saved = sum(ms[n] for n in old_sel[k] - new_sel[k])
            narrowed.append((saved, k))
    narrowed.sort(reverse=True)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    per_file = {}
    for kind, n in (("k", a.n), ("i", a.ni)):
        got = 0
        for saved, k in narrowed:
            if got >= n or not k.startswith(kind):
                continue
            f = k.split("-")[1]
            if per_file.get((kind, f), 0) >= 3:
                continue
            per_file[(kind, f)] = per_file.get((kind, f), 0) + 1
            (out / "commits" / f"{k}.patch").write_text(valid[k])
            meta[k] = {"kind": "import-state-" + ("class-data" if kind == "k" else "import-line"),
                       "saved_test_seconds": round(saved, 2), "old_n": len(old_sel[k]), "new_n": len(new_sel[k])}
            got += 1
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"narrowed {len(narrowed)} of {len(keys)}; wrote {len(meta)} mutants to {out}")


if __name__ == "__main__":
    main()
