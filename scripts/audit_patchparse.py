#!/usr/bin/env python3
"""Patch-parser differential check: the selector's reconstruction of each patched file (parse_patch +
apply_hunks over BR_SRC) vs `git apply` in a disposable copy. Also reports patches git rejects that the
selector still analyses (it must fail closed on those) and the selector's verdict for each edge case.

  python3 scripts/audit_patchparse.py --src ../sqlglot --patches DIR [DIR ...] [--edge] [--json out.json]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_selector(src):
    os.environ["BR_SRC"] = str(src)
    sys.path.insert(0, str(REPO))
    import selector  # noqa: E402
    return selector


def git_apply(src, patch_text, files):
    """-> (ok, {path: new text or None if deleted}, stderr)"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for p in files:
            if p and (src / p).is_file():
                (td / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src / p, td / p)
        (td / "x.patch").write_text(patch_text)
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", "x.patch"], cwd=td, capture_output=True, text=True)
        if r.returncode:
            return False, {}, r.stderr.strip()[:300]
        out = {}
        for p in files:
            if p:
                out[p] = (td / p).read_text() if (td / p).is_file() else None
        return True, out, ""


def selector_view(S, src, patch_text):
    files = S.parse_patch(patch_text)
    out, notes = {}, []
    for f in files:
        path = f["new"] or f["old"]
        if f["binary"]:
            notes.append(f"binary {path}")
            continue
        if f["new"] is None:
            out[f["old"]] = None
            continue
        old_text = None
        if f["old"]:
            try:
                old_text = (src / f["old"]).read_text()
            except OSError:
                notes.append(f"missing old {f['old']}")
                continue
        old_lines = [] if old_text is None else old_text.split("\n")
        if old_text is None:
            old_lines = []
        new_lines = S.apply_hunks(old_lines, f["hunks"])[0]
        text = "\n".join(new_lines)
        if old_text is None and text and not text.endswith("\n"):
            text += "\n"  # new file: the selector never needs its exact text; compare modulo final newline
        out[path] = text
    return files, out, notes


def edge_cases(src):
    """Synthetic patches built with git itself (git diff in a scratch repo) for formats the corpora lack."""
    cases = {}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        rel_a, rel_b = "sqlglot/optimizer/scope.py", "sqlglot/helper.py"

        def run(*a):
            return subprocess.run(["git", *a], cwd=td, capture_output=True, text=True, check=True).stdout

        for p in (rel_a, rel_b, "sqlglot/dialects/tsql.py"):
            (td / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src / p, td / p)
        run("init", "-q")
        run("add", "-A")
        run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base")
        a = (td / rel_a).read_text()
        b = (td / rel_b).read_text()

        def diff(name, *extra):
            cases[name] = run("diff", *extra)
            run("checkout", "-q", "--", ".")
            run("clean", "-qfd")

        lines = a.split("\n")
        # two hunks far apart, comment-only + code edit
        lines2 = list(lines)
        lines2[10] = lines2[10] + "  # note"
        lines2[-20] = lines2[-20].replace("return", "return  ", 1) if "return" in lines2[-20] else lines2[-20] + " "
        (td / rel_a).write_text("\n".join(lines2))
        diff("multi_hunk")
        (td / rel_a).write_text(a.replace("def traverse_scope(", "def traverse_scope((", 1))
        diff("syntax_err_U0", "-U0")
        (td / rel_a).write_text(a.replace("def traverse_scope(", "def traverse_scope((", 1))
        diff("syntax_err_U10", "-U10")
        (td / rel_b).write_text(b.rstrip("\n"))  # drop final newline
        diff("no_final_newline")
        (td / rel_b).write_text(b + "X = 1")  # append without final newline
        diff("append_no_newline")
        (td / "sqlglot/zz_new.py").write_text("Y = 2\n")
        run("add", "-N", "sqlglot/zz_new.py")
        diff("new_file")
        (td / "sqlglot/dialects/tsql.py").unlink()
        diff("deleted_file")
        run("mv", rel_b, "sqlglot/helper2.py")
        cases["renamed"] = run("diff", "-M", "--cached")
        run("reset", "-q", "--hard")
        (td / rel_a).write_text(a.replace("def traverse_scope(", "def traverse_scope((", 1))
        (td / rel_b).write_text(b.replace("\ndef ", "\n# c\ndef ", 1))
        diff("two_files")
        # plain unified diff without a git header (diff -u style)
        import difflib
        cases["plain_unified"] = "".join(difflib.unified_diff(
            a.splitlines(True), a.replace("def traverse_scope(", "def traverse_scope((", 1).splitlines(True),
            f"a/{rel_a}", f"b/{rel_a}"))
        cases["crlf"] = cases["two_files"].replace("\n", "\r\n")
        # context that does not match the pinned file (stale patch): git rejects it
        stale = cases["syntax_err_U10"].split("\n")
        for i, l in enumerate(stale):
            if l.startswith(" ") and len(l) > 3:
                stale[i] = " " + l[1:] + "  # stale"
                break
        cases["stale_context"] = "\n".join(stale)
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--patches", nargs="*", default=[])
    ap.add_argument("--edge", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    src = Path(a.src).resolve()
    S = load_selector(src)
    items = []
    for d in a.patches:
        for p in sorted(Path(d).glob("*.patch")):
            items.append((f"{Path(d).parent.name}/{p.stem}", p.read_text(encoding="utf-8", errors="replace")))
    if a.edge:
        items += [(f"edge/{k}", v) for k, v in edge_cases(src).items()]
    rows, bad = [], 0
    for name, text in items:
        files, mine, notes = selector_view(S, src, text)
        touched = sorted({p for f in files for p in (f["old"], f["new"]) if p})
        ok, theirs, err = git_apply(src, text, touched)
        row = {"id": name, "files": touched, "git_ok": ok, "notes": notes}
        if not files:
            row["verdict"] = "selector: no files parsed"
        elif not ok:
            row["verdict"] = "git rejects: " + err.splitlines()[0] if err else "git rejects"
        else:
            diffs = [p for p in mine if mine[p] != theirs.get(p, "<absent>")]
            row["verdict"] = "match" if not diffs else f"MISMATCH {diffs}"
        if row["verdict"] != "match":
            bad += 1
        rows.append(row)
    for r in rows:
        if r["verdict"] != "match" or r["id"].startswith("edge/"):
            print(f"{r['id']:55s} {r['verdict']}  {r['notes'] or ''}")
    print(f"{len(rows)} patches, {len(rows) - bad} match git apply, {bad} need a look")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
