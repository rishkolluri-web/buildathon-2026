#!/usr/bin/env python3
"""Differential check: swap_only_sites / swap_only_hunks of a reference revision vs the
working tree, on synthetic patches over EVERY single-operator is/==/is not/!= comparison in
the pinned sqlglot sources (not only traced sites) plus adversarial mixes:

  swap      the operator swapped (is<->==, is not<->!=)
  wrong     is -> is not / == -> != (a real semantic change: must NOT be a swap)
  lt        == -> <= style non-swap operator change on the same line
  swap+edit the swap plus an unrelated constant edit elsewhere in the same function
  swap+swap two swaps in one patch (different lines)
  swap+ws   the swap plus a whitespace-only change on another line (still swap-only)

For each generated patch, (hunk prefilter, sites) must be identical between the two
implementations.  python3 scripts/diff_swap.py --ref 5bf7a1e --src ../sqlglot
"""

import argparse
import ast
import importlib.util
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OPS = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!="}
SWAP = {"is": "==", "==": "is", "is not": "!=", "!=": "is not"}
WRONG = {"is": "is not", "==": "!=", "is not": "is", "!=": "=="}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def op_span(line, col, tok):
    """Find the operator token after the comparison's start column on this line."""
    pat = {"is": r"\bis\b(?!\s+not\b)", "is not": r"\bis\s+not\b", "==": r"==", "!=": r"!="}[tok]
    m = re.compile(pat).search(line, col)
    return (m.start(), m.end()) if m else None


def edit_line(line, col, tok, new_tok):
    sp = op_span(line, col, tok)
    if not sp:
        return None
    return line[: sp[0]] + new_tok + line[sp[1]:]


def hunks_for(old_lines, changes):
    """changes: {lineno: new_text}; one hunk per changed line, -n/+n of equal length."""
    hunks = []
    for ln in sorted(changes):
        hunks.append([ln, [("-", old_lines[ln - 1]), ("+", changes[ln])]])
    return hunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="5bf7a1e")
    ap.add_argument("--src", default=str(REPO.parent / "sqlglot"))
    ap.add_argument("--limit", type=int, default=0, help="max sites (0 = all)")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ref_selector.py").write_bytes(subprocess.run(["git", "show", f"{a.ref}:selector.py"], cwd=REPO,
                                                         capture_output=True, check=True).stdout)
    ref = load(tmp / "ref_selector.py", "ref_selector")
    new = load(REPO / "selector.py", "new_selector")
    src = Path(a.src)
    rnd = random.Random(20261121)
    n = mismatch = swaps_ok = 0
    kinds = {}
    for f in sorted((src / "sqlglot").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        old_lines = text.split("\n")
        old_tree = ast.parse(text)
        cmps = [c for c in ast.walk(old_tree) if isinstance(c, ast.Compare) and len(c.ops) == 1
                and type(c.ops[0]) in OPS and c.lineno == c.end_lineno]
        consts = [c for c in ast.walk(old_tree) if isinstance(c, ast.Constant) and isinstance(c.value, int)
                  and not isinstance(c.value, bool) and c.lineno == c.end_lineno]
        for c in cmps:
            if a.limit and n >= a.limit:
                break
            tok = OPS[type(c.ops[0])]
            ln = c.lineno
            line = old_lines[ln - 1]
            col = c.left.end_col_offset  # operator follows the left operand
            variants = []
            s = edit_line(line, col, tok, SWAP[tok])
            if s is None:
                continue
            variants.append(("swap", {ln: s}))
            w = edit_line(line, col, tok, WRONG[tok])
            if w:
                variants.append(("wrong", {ln: w}))
            if tok == "==":
                variants.append(("lt", {ln: edit_line(line, col, tok, "<=")}))
            others = [x for x in cmps if x.lineno != ln]
            if others:
                o = rnd.choice(others)
                ot = OPS[type(o.ops[0])]
                s2 = edit_line(old_lines[o.lineno - 1], o.left.end_col_offset, ot, SWAP[ot])
                if s2:
                    variants.append(("swap+swap", {ln: s, o.lineno: s2}))
            cs = [k for k in consts if k.lineno != ln]
            if cs:
                k = rnd.choice(cs)
                kl = old_lines[k.lineno - 1]
                seg = kl[k.col_offset:k.end_col_offset]
                if seg.isdigit():
                    variants.append(("swap+edit", {ln: s, k.lineno: kl[:k.col_offset] + str(int(seg) + 1) + kl[k.end_col_offset:]}))
            wl = [i for i, t in enumerate(old_lines, 1) if i != ln and t.rstrip() and t.endswith(")")]
            if wl:
                i = rnd.choice(wl)
                variants.append(("swap+ws", {ln: s, i: old_lines[i - 1] + "  "}))
            for kind, changes in variants:
                hunks = hunks_for(old_lines, changes)
                new_lines, ot_, nt_ = new.apply_hunks(old_lines, [[h[0], list(h[1])] for h in hunks])
                try:
                    new_tree = ast.parse("\n".join(new_lines))
                except SyntaxError:
                    continue
                n += 1
                pr = ref.swap_only_hunks(hunks), new.swap_only_hunks(hunks)
                r_sites = ref.swap_only_sites(ast.parse(text), ast.parse("\n".join(new_lines)))
                n_sites = new.swap_only_sites(old_tree, new_tree, ot_ | nt_)
                rs = sorted(r_sites) if r_sites else None
                ns = sorted(n_sites) if n_sites else None
                kinds.setdefault(kind, [0, 0])[0] += 1
                if pr[0] != pr[1] or rs != ns:
                    mismatch += 1
                    print(f"MISMATCH {f.relative_to(src)}:{ln} {kind} prefilter {pr} ref {rs} new {ns}")
                elif ns:
                    kinds[kind][1] += 1
                    swaps_ok += 1
    print(f"{n} patches, {mismatch} mismatches; detected as swap-only: {swaps_ok}")
    for k, (tot, sw) in sorted(kinds.items()):
        print(f"  {k:10s} {tot:6d} patches, {sw:6d} classified swap-only by both")
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
