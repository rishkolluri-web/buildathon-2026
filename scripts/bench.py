#!/usr/bin/env python3
"""Repeatable selector benchmark: CPU, wall, peak RSS and simulated cost per revision.

  python3 scripts/bench.py --rev 8238fd6 --rev 5bf7a1e --rev WORKTREE [--runs 7] [--data DIR ...]

Each revision's selector.py / map.json.gz / eqsites.json are exported to a temp dir and
run as a fresh process (cold start, as the scorer does) `--runs` times per dataset.
Reports median and p95 of user+sys CPU, wall, max RSS, and the simulated cost (+ outputs
identical across runs). --phases additionally times the phases in-process (one run).
"""

import argparse
import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILES = ("selector.py", "map.json.gz", "eqsites.json", "vmap.json.gz", "heavy.json", "iso_imp.json.gz", "heavy_vt.json")


def export(rev, dest):
    dest.mkdir(parents=True)
    for f in FILES:
        if rev == "WORKTREE":
            if (REPO / f).exists():
                shutil.copy(REPO / f, dest / f)
            continue
        p = subprocess.run(["git", "show", f"{rev}:{f}"], cwd=REPO, capture_output=True)
        if p.returncode == 0:
            (dest / f).write_bytes(p.stdout)
    return dest


def run_once(py, d, data, src):
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src), BR_MAP=str(d / "map.json.gz"),
               BR_EQ=str(d / "eqsites.json"), BR_VMAP=str(d / "vmap.json.gz"), PYTHONDONTWRITEBYTECODE="1")
    env.pop("BR_OFF", None)
    # wait4 gives this child's own rusage (CPU + max RSS)
    t0 = time.perf_counter()
    p = subprocess.Popen([py, str(d / "selector.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.stdout.read()
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.perf_counter() - t0
    if status:
        raise SystemExit(f"selector failed in {d}")
    rss = ru.ru_maxrss / (1 if sys.platform != "darwin" else 1024)  # -> KiB
    return ru.ru_utime + ru.ru_stime, wall, rss, out


def sim_cost(out, ms):
    tot = 0
    for line in out.decode().splitlines():
        w = [x for x in json.loads(line)["workers"] if x]
        if w:
            tot += max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)
    return tot / 1000


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", action="append", required=True, help="git revision or WORKTREE")
    ap.add_argument("--data", action="append", help="dir with commits/ and tests.json (default: kit sample)")
    ap.add_argument("--kit", default=str(REPO.parent / "blast-radius-sample"))
    ap.add_argument("--src", default=str(REPO.parent / "sqlglot"))
    ap.add_argument("--python", default=str(REPO.parent / ".venv313" / "bin" / "python"))
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--json")
    a = ap.parse_args()
    datas = [Path(x).resolve() for x in (a.data or [Path(a.kit) / "sample"])]
    tmp = Path(tempfile.mkdtemp())
    dirs = {r: export(r, tmp / r.replace("/", "_")) for r in a.rev}
    results = {}
    for data in datas:
        tests = json.loads((data / "tests.json").read_text())["tests"]
        ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        ncommits = len(list((data / "commits").glob("*.patch")))
        print(f"== {data} ({ncommits} commits), {a.runs} runs each, interleaved")
        samples = {r: [] for r in a.rev}
        outs = {r: set() for r in a.rev}
        run_once(a.python, dirs[a.rev[0]], data, a.src)  # warm the OS file cache
        for _ in range(a.runs):
            for r in a.rev:  # interleave revisions so machine-load drift hits all equally
                cpu, wall, rss, out = run_once(a.python, dirs[r], data, a.src)
                samples[r].append((cpu, wall, rss))
                outs[r].add(out)
        for r in a.rev:
            cpus = [s[0] for s in samples[r]]
            walls = [s[1] for s in samples[r]]
            rss = max(s[2] for s in samples[r])
            out = next(iter(outs[r]))
            res = {"cpu_median": statistics.median(cpus), "cpu_p95": pct(cpus, 0.95), "cpu_min": min(cpus),
                   "cpu_stdev": statistics.stdev(cpus) if len(cpus) > 1 else 0.0,
                   "wall_median": statistics.median(walls), "rss_mib": rss / 1024,
                   "sim": sim_cost(out, ms), "deterministic": len(outs[r]) == 1}
            results[f"{data.name}:{r}"] = res
            print(f"  {r:>10}: cpu med {res['cpu_median']:.3f}s p95 {res['cpu_p95']:.3f}s min {res['cpu_min']:.3f}s "
                  f"sd {res['cpu_stdev']:.3f}  wall {res['wall_median']:.3f}s  rss {res['rss_mib']:.1f} MiB  "
                  f"sim {res['sim']:.3f}s  {'det' if res['deterministic'] else 'NONDETERMINISTIC'}")
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
