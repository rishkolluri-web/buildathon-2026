#!/usr/bin/env python3
"""Ground-truth corpus for the trimmed-ALL rule (selector.trimmed_all): mutants whose selection is an
ALL fallback in a sqlglot module that some slow test never imports.

Candidates over every sqlglot module outside the smallest heavy closure, then filtered by running the
selector (kept only if it logs "ALL minus"):
  syn-*  syntax error: ' )' appended to a statement line (import fails)
  itb-*  import-time function body line: first int/str constant changed (int+1, str last char)
  mod-*  module-level / class-level assignment: first int/str constant changed
  ops-*  import-time body line: first == / != / < / > comparison flipped
At most 2 kept per file per kind; seeded.

  python3 scripts/all_mutations.py --src ../sqlglot --python ../.venv313/bin/python \
      --kit ../blast-radius-sample --out ../stress_hv --n 32
"""

import argparse
import ast
import difflib
import gzip
import json
import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def udiff(path, old, new):
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))


def header_lines(node):
    first = node.body[0].lineno if node.body else node.lineno
    lo = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
    return set(range(lo, first))


def const_mut(line, node):
    """Replace the first single-line int/str constant inside `node` on `line` text."""
    for c in ast.walk(node):
        if isinstance(c, ast.Constant) and c.lineno == c.end_lineno == node.lineno and type(c.value) in (int, str):
            seg = line[c.col_offset:c.end_col_offset]
            if type(c.value) is int and seg.isdigit():
                new = str(c.value + 1)
            elif type(c.value) is str and c.value and seg[:1] in "'\"" and "\\" not in seg:
                new = repr(c.value[:-1] + ("Y" if c.value[-1] != "Y" else "Z"))
            else:
                continue
            return line[:c.col_offset] + new + line[c.end_col_offset:]
    return None


OPFLIP = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", ">="), ast.Gt: (">", "<=")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--kit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=32)
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--kinds", default="syn,itb,mod,ops", help="comma list of mutation kinds to generate")
    ap.add_argument("--core", action="store_true", help="only modules INSIDE the smallest heavy closure")
    ap.add_argument("--marker", default="ALL minus", help="keep candidates whose selector log contains this")
    ap.add_argument("--per-file", type=int, default=2)
    a = ap.parse_args()
    kinds_on = set(a.kinds.split(","))
    rnd = random.Random(a.seed)
    src = Path(a.src)
    heavy = json.loads((REPO / "heavy.json").read_text())["closure"]
    smallest = min(heavy.values(), key=len)
    imp = {k: set(v) for k, v in json.load(gzip.open(REPO / "map.json.gz", "rt"))["imp"].items()}
    cands = []
    for f in sorted((src / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(src))
        mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
        if (mod in smallest) != a.core:
            continue
        text = f.read_text()
        lines = text.split("\n")
        tree = ast.parse(text)
        il = imp.get(rel, set())
        body_imp = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = set(range(n.lineno, n.end_lineno + 1)) - header_lines(n)
                body_imp |= body & il
        stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt) and n.lineno == n.end_lineno]
        for st in stmts:
            ln = st.lineno
            line = lines[ln - 1]

            def emit(kind, new_line):
                if kind not in kinds_on:
                    return
                new = list(lines)
                new[ln - 1] = new_line
                cands.append((kind, rel, ln, udiff(rel, text, "\n".join(new))))
            if isinstance(st, (ast.Expr, ast.Assign, ast.Return)) and not line.rstrip().endswith((":", ",", "\\")):
                emit("syn", line.rstrip() + " )")
            if ln in body_imp:
                m = const_mut(line, st)
                if m:
                    emit("itb", m)
                for c in ast.walk(st):
                    if isinstance(c, ast.Compare) and len(c.ops) == 1 and type(c.ops[0]) in OPFLIP and c.lineno == ln:
                        old, newop = OPFLIP[type(c.ops[0])]
                        seg = line[c.left.end_col_offset:c.comparators[0].col_offset]
                        if seg.count(old) == 1 and not (old in "<>" and "=" in seg):
                            s0 = c.left.end_col_offset + seg.index(old)
                            emit("ops", line[:s0] + newop + line[s0 + len(old):])
                        break
            elif isinstance(st, (ast.Assign, ast.AnnAssign)) and ln in il:
                m = const_mut(line, st)
                if m:
                    emit("mod", m)
    rnd.shuffle(cands)
    out = Path(a.out)
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        (data / "commits").mkdir()
        shutil.copy(Path(a.kit) / "sample" / "tests.json", data / "tests.json")
        ids = {}
        for i, (kind, rel, ln, patch) in enumerate(cands):
            cid = f"c{i:05d}"
            (data / "commits" / f"{cid}.patch").write_text(patch)
            ids[cid] = (kind, rel, ln, patch)
        env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src.resolve()))
        r = subprocess.run([a.python, str(REPO / "selector.py")], env=env, capture_output=True, text=True)
    trimmed, pending = set(), False
    for line in r.stderr.splitlines():
        if a.marker in line:
            pending = True
        elif ": " in line and " tests (" in line:
            cid = line.split(":", 1)[0]
            if pending:
                trimmed.add(cid)
            pending = False
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta, per = {}, {}
    for cid in sorted(trimmed, key=lambda c: int(c[1:])):
        kind, rel, ln, patch = ids[cid]
        if len(meta) >= a.n or per.get((kind, rel), 0) >= a.per_file:
            continue
        per[(kind, rel)] = per.get((kind, rel), 0) + 1
        mid = f"{kind}-{Path(rel).parent.name}-{Path(rel).stem}-{ln}"
        (out / "commits" / f"{mid}.patch").write_text(patch)
        meta[mid] = {"kind": f"trimmed-all-{kind}", "file": rel, "line": ln}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    kinds = {}
    for v in meta.values():
        kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
    print(f"{len(cands)} candidates, {len(trimmed)} trimmed by the selector -> {len(meta)} mutants {kinds}")


if __name__ == "__main__":
    main()
