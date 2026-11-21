"""Uniform census of single-token mutation sites (the sample generator's operator set) over sqlglot/*.py.
  python sitecensus.py <sqlglot root> <out dir> <n> <seed>
Writes <out>/commits/<id>.patch, <out>/tests.json (copied by caller), <out>/sites.json."""
import ast, difflib, json, random, sys
from pathlib import Path

root, out, n, seed = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
SWAP = {ast.Is: "==", ast.Eq: "is", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">",
        ast.IsNot: "!="}
TXT = {ast.Is: "is", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
       ast.IsNot: "is not"}
sites = []
for f in sorted((root / "sqlglot").rglob("*.py")):
    rel = str(f.relative_to(root))
    src = f.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP:
            a, b = node.left, node.comparators[0]
            if a.end_lineno == b.lineno == node.lineno:
                seg = lines[node.lineno - 1][a.end_col_offset:b.col_offset]
                t = TXT[type(node.ops[0])]
                if seg.strip() == t:
                    sites.append(("cmp", rel, node.lineno, a.end_col_offset, b.col_offset, seg.replace(t, SWAP[type(node.ops[0])])))
        elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
            a, b = node.values
            if a.end_lineno == b.lineno:
                seg = lines[a.end_lineno - 1][a.end_col_offset:b.col_offset]
                w = "and" if isinstance(node.op, ast.And) else "or"
                if seg.strip() == w:
                    sites.append(("andor", rel, a.end_lineno, a.end_col_offset, b.col_offset, seg.replace(w, "or" if w == "and" else "and")))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and node.lineno == node.operand.lineno:
            seg = lines[node.lineno - 1][node.col_offset:node.operand.col_offset]
            if seg.strip() == "not":
                sites.append(("dropnot", rel, node.lineno, node.col_offset, node.operand.col_offset, ""))
        elif isinstance(node, ast.Constant) and node.lineno == node.end_lineno:
            seg = lines[node.lineno - 1][node.col_offset:node.end_col_offset]
            if isinstance(node.value, bool) and seg in ("True", "False"):
                sites.append(("bool", rel, node.lineno, node.col_offset, node.end_col_offset, "False" if seg == "True" else "True"))
            elif type(node.value) is int and seg.isdigit():
                sites.append(("int", rel, node.lineno, node.col_offset, node.end_col_offset, str(node.value + random.Random(f"{rel}{node.lineno}{node.col_offset}").choice((1, -1)) if node.value > 0 else node.value + 1)))
rng = random.Random(seed)
pick = rng.sample(sites, min(n, len(sites)))
(out / "commits").mkdir(parents=True, exist_ok=True)
meta = {}
for i, (kind, rel, ln, c0, c1, rep) in enumerate(pick):
    lines = (root / rel).read_text().splitlines(keepends=True)
    new = list(lines)
    new[ln - 1] = lines[ln - 1][:c0] + rep + lines[ln - 1][c1:]
    body = "".join(difflib.unified_diff(lines, new, f"a/{rel}", f"b/{rel}"))
    cid = f"u{i:04d}-{kind}"
    (out / "commits" / f"{cid}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + body)
    meta[cid] = {"kind": kind, "file": rel, "line": ln}
(out / "sites.json").write_text(json.dumps(meta, indent=0))
from collections import Counter
print(len(sites), "sites;", Counter(s[0] for s in sites), "; picked", len(pick))
