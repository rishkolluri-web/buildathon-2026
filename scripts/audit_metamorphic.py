#!/usr/bin/env python3
"""Metamorphic checks: semantically equivalent rewrites of each patch must give the same selection.
Variants per patch (built from the git-apply result, so they describe the same edit):
  U0 / U1 / U10   context width
  rev             file sections in reverse order (multi-file patches)
  ws              plus a whitespace-only change (blank line added after the file's last line) in each .py file
  python3 scripts/audit_metamorphic.py --src ../sqlglot --patches DIR [DIR ...] --out WORKDIR
Runs the working-tree selector once over all variants and reports every patch whose variants disagree."""

import argparse
import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from audit_patchparse import git_apply, load_selector  # noqa: E402


def git_section(path_old, path_new, a, b, n):
    if a is None:
        head = f"diff --git a/{path_new} b/{path_new}\nnew file mode 100644\n"
        body = "".join(difflib.unified_diff([], b.splitlines(True), "/dev/null", f"b/{path_new}", n=n))
    elif b is None:
        head = f"diff --git a/{path_old} b/{path_old}\ndeleted file mode 100644\n"
        body = "".join(difflib.unified_diff(a.splitlines(True), [], f"a/{path_old}", "/dev/null", n=n))
    else:
        head = f"diff --git a/{path_old} b/{path_new}\n"
        body = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{path_old}", f"b/{path_new}", n=n))
    return head + body if body else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--patches", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--idx")
    a = ap.parse_args()
    src = Path(a.src).resolve()
    S = load_selector(src)
    out = Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    (out / "tests.json").write_text(Path(a.tests).read_text())
    groups = {}
    for d in a.patches:
        for p in sorted(Path(d).glob("*.patch")):
            text = p.read_text(encoding="utf-8", errors="replace")
            files = S.parse_patch(text)
            if not files or any(f["binary"] for f in files):
                continue
            if any(f["old"] and f["new"] and f["old"] != f["new"] for f in files):
                continue  # renames: rewriting would change their meaning
            touched = sorted({x for f in files for x in (f["old"], f["new"]) if x})
            ok, new, _ = git_apply(src, text, touched)
            if not ok:
                continue
            secs = []
            for f in files:
                po, pn = f["old"], f["new"]
                old = (src / po).read_text() if po and (src / po).is_file() else None
                secs.append((po, pn, old, new.get(pn) if pn else None))
            cid = f"{Path(d).parent.name}__{p.stem}"
            variants = {"orig": text}
            for n in (0, 1, 10):
                variants[f"U{n}"] = "".join(git_section(po or pn, pn or po, o, nw, n) for po, pn, o, nw in secs)
            if len(secs) > 1:
                variants["rev"] = "".join(git_section(po or pn, pn or po, o, nw, 3) for po, pn, o, nw in reversed(secs))
            ws = []
            for po, pn, o, nw in secs:
                if nw is not None and o is not None and (pn or "").endswith(".py"):
                    nw = nw + ("\n" if nw.endswith("\n") else "\n\n")
                ws.append(git_section(po or pn, pn or po, o, nw, 3))
            variants["ws"] = "".join(ws)
            for k, v in variants.items():
                if v:
                    (out / "commits" / f"{cid}--{k}.patch").write_text(v)
            groups[cid] = [k for k, v in variants.items() if v]
    env = dict(os.environ, BR_DATA=str(out), BR_SRC=str(src))
    if a.idx:
        env["BR_IDX"] = a.idx
    r = subprocess.run([sys.executable, str(REPO / "selector.py")], env=env, capture_output=True, text=True)
    (out / "stderr.txt").write_text(r.stderr)
    sel = {}
    for line in r.stdout.splitlines():
        row = json.loads(line)
        sel[row["commit"]] = sorted(n for w in row["workers"] for n in w)
    diff = []
    for cid, ks in groups.items():
        base = sel.get(f"{cid}--orig")
        bad = {k: len(sel.get(f"{cid}--{k}", [])) for k in ks if sel.get(f"{cid}--{k}") != base}
        if bad:
            diff.append((cid, len(base or []), bad))
    for cid, nb, bad in diff:
        print(f"{cid:60s} orig {nb:5d} tests; differing variants {bad}")
    print(f"{len(groups)} patches x variants: {len(diff)} with differing selections")
    (out / "result.json").write_text(json.dumps({"groups": groups, "diff": diff}, indent=1))


if __name__ == "__main__":
    main()
