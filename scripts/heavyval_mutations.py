#!/usr/bin/env python3
"""Candidate corpus for the heavy-test value rule (v20): single-token edits of the sample's kinds at
comparison sites (operator swaps incl. is/==, int/str constant of a single-constant comparison)
anywhere in sqlglot/*.py. Writes every candidate; `pick` later keeps those whose v19 selection holds a
test > 3 s, balanced between "rule would drop it" and negative controls.

  python3 scripts/heavyval_mutations.py gen  --src ../sqlglot --out ../hvv_all
  python3 scripts/heavyval_mutations.py pick --all ../hvv_all --out ../stress_hvv --ids ids.txt
"""
import argparse
import ast
import difflib
import json
import shutil
from pathlib import Path

SWAP = {ast.Is: "==", ast.Eq: "is", ast.IsNot: "!=", ast.NotEq: "is not", ast.Lt: "<=", ast.LtE: "<",
        ast.Gt: ">=", ast.GtE: ">"}
FLIP = {ast.Eq: "!=", ast.NotEq: "=="}
TXT = {ast.Is: "is", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
       ast.IsNot: "is not"}


def sites(root):
    out = []
    for f in sorted((root / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(root))
        src = f.read_text()
        lines = src.splitlines(keepends=True)
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
                continue
            op = type(node.ops[0])
            a, b = node.left, node.comparators[0]
            if op in SWAP and a.end_lineno == b.lineno == node.lineno:
                seg = lines[node.lineno - 1][a.end_col_offset:b.col_offset]
                if seg.strip() == TXT[op]:
                    for kind, rep in (("swap", SWAP[op]), ("flip", FLIP.get(op))):
                        if rep:
                            out.append((kind, rel, node.lineno, a.end_col_offset, b.col_offset, seg.replace(TXT[op], rep)))
            for c in (a, b):
                if isinstance(c, ast.Constant) and c.lineno == c.end_lineno and op in TXT and op not in (ast.Is, ast.IsNot):
                    seg = lines[c.lineno - 1][c.col_offset:c.end_col_offset]
                    if type(c.value) is int and seg.isdigit():
                        for d in (1, -1):
                            if c.value + d >= 0:
                                out.append((f"int{d:+d}", rel, c.lineno, c.col_offset, c.end_col_offset, str(c.value + d)))
                    elif type(c.value) is str and len(seg) >= 2 and seg[0] in "'\"" and seg[-1] == seg[0] and "\\" not in seg:
                        out.append(("str", rel, c.lineno, c.col_offset, c.end_col_offset, seg[0] + "Y" + seg[0] if seg[1:-1] != "Y" else seg[0] + "Z" + seg[0]))
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--src", required=True)
    g.add_argument("--out", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--all", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--ids", required=True)
    a = ap.parse_args()
    if a.cmd == "gen":
        root, out = Path(a.src), Path(a.out)
        (out / "commits").mkdir(parents=True, exist_ok=True)
        meta = {}
        for i, (kind, rel, ln, c0, c1, rep) in enumerate(sites(root)):
            lines = (root / rel).read_text().splitlines(keepends=True)
            new = list(lines)
            new[ln - 1] = lines[ln - 1][:c0] + rep + lines[ln - 1][c1:]
            body = "".join(difflib.unified_diff(lines, new, f"a/{rel}", f"b/{rel}"))
            cid = f"hv{i:05d}-{kind}"
            (out / "commits" / f"{cid}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + body)
            meta[cid] = {"kind": kind, "file": rel, "line": ln, "col": c0}
        (out / "sites.json").write_text(json.dumps(meta, indent=0))
        print(len(meta), "candidates")
    else:
        src, out = Path(a.all), Path(a.out)
        ids = [x.strip() for x in Path(a.ids).read_text().split() if x.strip()]
        (out / "commits").mkdir(parents=True, exist_ok=True)
        meta = json.loads((src / "sites.json").read_text())
        for cid in ids:
            shutil.copy(src / "commits" / f"{cid}.patch", out / "commits" / f"{cid}.patch")
        (out / "meta.json").write_text(json.dumps({c: meta[c] for c in ids}, indent=0))
        print(len(ids), "picked")


if __name__ == "__main__":
    main()
