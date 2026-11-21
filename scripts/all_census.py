#!/usr/bin/env python3
"""Census of ALL fallbacks: run selector.py once over the sample and every local mutation corpus
and tabulate, per patch that selects every test, the logged reason and the changed module(s).

  python3 scripts/all_census.py --kit ../blast-radius-sample --src ../sqlglot --python ../.venv313/bin/python
"""

import argparse
import collections
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", default="python3")
    ap.add_argument("--corpora", default=str(REPO.parent / "stress*"))
    ap.add_argument("--selector", default=str(REPO / "selector.py"))
    ap.add_argument("--map", default=str(REPO / "map.json.gz"))
    ap.add_argument("--json", help="write per-patch rows here")
    a = ap.parse_args()
    kit = Path(a.kit).resolve()
    tests = json.loads((kit / "sample" / "tests.json").read_text())["tests"]
    n_all = len(tests)
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        (data / "commits").mkdir()
        os.symlink(kit / "sample" / "tests.json", data / "tests.json")
        origin = {}
        for p in sorted((kit / "sample" / "commits").glob("*.patch")):
            cid = f"sample__{p.stem}"
            os.symlink(p, data / "commits" / f"{cid}.patch")
            origin[cid] = p
        for d in sorted(Path(a.corpora).parent.glob(Path(a.corpora).name)):
            for p in sorted((d / "commits").glob("*.patch")):
                cid = f"{d.name}__{p.stem}"
                os.symlink(p, data / "commits" / f"{cid}.patch")
                origin[cid] = p
        env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(Path(a.src).resolve()), BR_MAP=a.map)
        r = subprocess.run([a.python, a.selector], env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stderr[-3000:])
    out = {json.loads(l)["commit"]: json.loads(l) for l in r.stdout.splitlines()}
    # stderr: reason lines for a commit precede its "<cid>: N tests" summary line
    pending, reasons = [], {}
    summary = re.compile(r"^(\S+): (\d+) tests \(")
    for line in r.stderr.splitlines():
        m = summary.match(line)
        if m and m.group(1) in out:
            reasons[m.group(1)] = pending
            pending = []
        else:
            pending.append(line.strip())
    rows = []
    for cid, o in out.items():
        n = sum(len(w) for w in o["workers"])
        if n < n_all:
            continue
        why = [l for l in reasons.get(cid, []) if "all tests" in l or "error" in l.lower()] or ["(no reason logged)"]
        files = re.findall(r"^diff --git a/(\S+) b/(\S+)", origin[cid].read_text(errors="replace"), re.M)
        rows.append({"commit": cid, "reason": why[0], "files": sorted({b for _, b in files})})
    kinds = collections.Counter()
    for row in rows:
        w = row["reason"]
        k = ("import-time line" if "import-time" in w else "syntax error" if "syntax" in w
             else "new module" if "new module" in w else "no known users" if "no known users" in w
             else "analysis error" if "error" in w.lower() else "not in pinned source" if "pinned" in w
             else "other: " + w[:80])
        row["kind"] = k
        kinds[k] += 1
    print(f"{len(out)} patches, {len(rows)} select ALL")
    for k, c in kinds.most_common():
        print(f"  {c:4d}  {k}")
    mods = collections.Counter(f for row in rows for f in row["files"])
    print("changed files of ALL patches:")
    for f, c in mods.most_common(40):
        print(f"  {c:4d}  {f}")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
