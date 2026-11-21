#!/usr/bin/env python3
"""Measure each safety rule's cost and safety, one configuration at a time.

For every configuration: official score.py on the 30 sample commits, total
simulated cost over all mutation patches, and misses on mutants that have
ground truth (scripts/stress_mutate.py truth). Also checks that idle filling
never raises the busiest worker.

  python3 scripts/ablate.py --kit ../blast-radius-sample --src ../sqlglot --stress ../stress \
      --python ../.venv313/bin/python --config NAME=SELECTOR,MAP[,BR_OFF] ...
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(python, selector, mp, src, data, off):
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src), BR_MAP=str(mp), BR_OFF=off)
    p = subprocess.run([python, selector], env=env, capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr)
    return p.stdout, p.stderr


def costs(stdout, ms):
    out = {}
    for line in stdout.splitlines():
        r = json.loads(line)
        w = r["workers"]
        out[r["commit"]] = ((max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) if w else 0,
                            {n for x in w for n in x}, w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--stress", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--config", action="append", required=True)
    a = ap.parse_args()

    kit = Path(a.kit).resolve()
    stress = Path(a.stress).resolve()
    tests = json.loads((kit / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])
    answers = json.loads((kit / "sample" / "answers.json").read_text())["commits"]
    meta = json.loads((stress / "meta.json").read_text())
    base = set(json.loads((stress / "truth" / "base.json").read_text()))
    truth = {}
    for mid in meta:
        f = stress / "truth" / f"{mid}.json"
        if f.exists():
            fail = set()
            for nid in set(json.loads(f.read_text())) - base:
                if nid.startswith("COLLECT::"):
                    fail |= by_file.get(nid[9:], set()) or set(ms)
                elif nid == "SESSION_FAILED":
                    fail |= set(ms)
                elif nid in num:
                    fail.add(num[nid])
            truth[mid] = fail
    mdata = Path(tempfile.mkdtemp())
    shutil.copytree(stress / "commits", mdata / "commits")
    shutil.copy(kit / "sample" / "tests.json", mdata / "tests.json")

    print(f"mutants with ground truth: {len(truth)}/{len(meta)}")
    print(f"{'config':22} {'sample':>8} {'s.miss':>6} {'mut.cost':>9} {'mut.miss':>8} {'all-tests':>9} {'fill+0':>6}")
    for spec in a.config:
        name, rest = spec.split("=", 1)
        parts = rest.split(",")
        selector, mp = parts[0], parts[1]
        off = ",".join(parts[2:])
        out, _ = run(a.python, selector, mp, a.src, kit / "sample", off)
        sc = costs(out, ms)
        s_cost = sum(c for c, _, _ in sc.values()) / 1000
        s_miss = sum(1 for cid, (_, sel, _) in sc.items() if set(answers[cid]["failing"]) - sel)
        out, _ = run(a.python, selector, mp, a.src, mdata, off)
        mc = costs(out, ms)
        m_cost = sum(c for c, _, _ in mc.values()) / 1000
        m_miss = [mid for mid, fail in truth.items() if fail - mc[mid][1]]
        n_all = sum(1 for _, sel, _ in mc.values() if len(sel) == len(ms))
        # fill check: cost from the output equals cost of the same workers without
        # any test outside the minimal set is not observable here; instead verify the
        # selector's own invariant: every worker load <= busiest worker load (trivial)
        # and that removing fill (BR_OFF=fill) yields the same per-commit cost.
        out_nf, _ = run(a.python, selector, mp, a.src, mdata, ",".join(filter(None, [off, "fill"])))
        nf = costs(out_nf, ms)
        fill_ok = all(nf[k][0] == mc[k][0] for k in mc)
        print(f"{name:22} {s_cost:8.1f} {s_miss:6d} {m_cost:9.1f} {len(m_miss):8d} {n_all:9d} {str(fill_ok):>6}"
              + (f"  missed {m_miss}" if m_miss else ""))
    shutil.rmtree(mdata, ignore_errors=True)


if __name__ == "__main__":
    main()
