#!/usr/bin/env python3
"""Offline evaluator (no Docker): run selector.py on the sample kit and score it.

  python3 tests/evaluate.py --kit ../blast-radius-sample --src ../sqlglot --map map.json.gz

Uses the kit's official score.py for the headline number, measures the
selector's own CPU time, and prints per-commit selections vs. the answers.
answers.json is read HERE only, for evaluation; the selector never sees it.
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--src", required=True, help="sqlglot checkout at the pinned commit")
    ap.add_argument("--map", default=str(REPO / "map.json.gz"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    kit = Path(args.kit).resolve()
    sample = kit / "sample"
    env = dict(os.environ, BR_DATA=str(sample), BR_SRC=str(Path(args.src).resolve()), BR_MAP=str(Path(args.map).resolve()))
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    proc = subprocess.run([args.python, str(REPO / "selector.py")], env=env, capture_output=True)
    wall = time.time() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if proc.returncode != 0:
        print(proc.stderr.decode(), file=sys.stderr)
        sys.exit(f"selector exited {proc.returncode}")
    out = kit / "output.offline.jsonl"
    out.write_bytes(proc.stdout)

    tests = json.loads((sample / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    answers = json.loads((sample / "answers.json").read_text())["commits"]
    if not args.quiet:
        print(f"{'commit':6} {'kind':8} {'fail':>4} {'sel':>5} {'wk':>2} {'cost':>7}  status")
        for line in proc.stdout.decode().splitlines():
            row = json.loads(line)
            sel = {n for w in row["workers"] for n in w}
            ans = answers[row["commit"]]
            fail = set(ans["failing"])
            cost = (max(sum(ms[n] for n in w) for w in row["workers"]) + 2000 * len(row["workers"])) if row["workers"] else 0
            missed = fail - sel
            status = f"MISS {sorted(missed)}" if missed else "ok"
            print(f"{row['commit']:6} {ans['kind']:8} {len(fail):>4} {len(sel):>5} {len(row['workers']):>2} {cost/1000:7.2f}  {status}")

    print("--- official score.py ---")
    r = subprocess.run([sys.executable, str(kit / "score.py"), str(sample), str(sample / "answers.json"), str(out),
                        "--cpu-seconds", f"{cpu:.3f}"], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    print(f"selector CPU {cpu:.2f}s (local, {args.python}), wall {wall:.2f}s; baseline 1614.1s")


if __name__ == "__main__":
    main()
