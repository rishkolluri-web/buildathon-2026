#!/usr/bin/env python3
"""Real-change corpus: genuine upstream sqlglot changes adapted to the pinned commit, as REVERTS of the most
recent commits before it (the revert of a fix re-introduces the bug its regression tests catch; refactors and
multi-file changes come along as they are). Only the sqlglot/ part of each commit is reverted (tests/ stay pinned).
Patches that do not apply cleanly to the pinned tree are skipped. Each commit is assigned to `dev` or `holdout`
by a hash of its id, fixed before anything is evaluated.

  python3 scripts/real_mutations.py --src ../sqlglot --out ../stress_real --n 40
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(src, *a, inp=None):
    return subprocess.run(["git", "-C", str(src), *a], capture_output=True, text=True, input=inp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--scan", type=int, default=150)
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    head = git(src, "rev-parse", "HEAD").stdout.strip()
    revs = git(src, "rev-list", "--first-parent", f"--max-count={a.scan}", "HEAD").stdout.split()
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    for c in revs:
        if len(meta) >= a.n:
            break
        files = [f for f in git(src, "diff", "--name-only", f"{c}^", c, "--", "sqlglot/").stdout.split() if f.endswith(".py")]
        if not files:
            continue
        patch = git(src, "diff", "--no-color", "--no-ext-diff", "-M", c, f"{c}^", "--", *files).stdout
        if not patch.strip() or "GIT binary patch" in patch:
            continue
        if git(src, "apply", "--check", "-", inp=patch).returncode:
            continue  # later commits touched the same lines: not applicable to the pinned tree
        subj = git(src, "log", "-1", "--format=%s", c).stdout.strip()
        cid = f"real-{c[:9]}"
        split = "holdout" if int(hashlib.sha256(c.encode()).hexdigest(), 16) % 5 < 2 else "dev"
        (out / "commits" / f"{cid}.patch").write_text(patch)
        meta[cid] = {"kind": "revert", "file": files[0], "commit": c, "subject": subj, "files": files, "split": split,
                     "lines": sum(1 for l in patch.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))  # flat: stress_mutate.py reads ids from the keys
    (out / "pinned.txt").write_text(head + "\n")
    n_h = sum(m["split"] == "holdout" for m in meta.values())
    print(f"{len(meta)} reverts ({n_h} holdout, {len(meta) - n_h} dev) -> {out}")


if __name__ == "__main__":
    main()
