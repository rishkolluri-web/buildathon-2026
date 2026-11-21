#!/usr/bin/env python3
"""Isolated import-time coverage (offline, pinned suite only) -> iso_imp.json.gz.

Lines that run while sqlglot modules are imported (map `imp`: metaclasses, __init_subclass__, SQL
templates parsed in class bodies...) are credited by the shared-run coverage map to whichever test or
context imported the module first, so the selector sends any change on them to ALL. Here EVERY numbered
test runs ALONE in a fresh pytest process (no xdist, no cache) with coverage started in every Python
process it creates (sitecustomize: the test process and spawned pool children). For each import-time
line, the tests whose own run executes it -- at collection, import or runtime -- are recorded.

A test whose isolated run fails, errors or does not run is listed in `unknown` (the selector always
selects those). The build aborts if more than --max-unknown tests are unknown.

  python3 mapgen/iso_imp.py --src ../sqlglot --python ../.venv313/bin/python \
      --tests ../blast-radius-sample/sample/tests.json --map map.json.gz --out iso_imp.json.gz --jobs 7
"""

import argparse
import concurrent.futures
import gzip
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import coverage

SITE = '''
import atexit, json, os, sys
_out = os.environ.get("ISO_OUT")
_cov = None
if _out:
    import coverage
    _cov = coverage.Coverage(data_file=os.path.join(_out, ".cov.%d" % os.getpid()),
                             source=[os.path.join(os.environ["ISO_SRC"], "sqlglot")])
    _cov.start()
def _dump():
    _cov.stop()
    _cov.save()
    with open(os.path.join(_out, "%d.done" % os.getpid()), "w") as f:
        f.write("1")
if _out:
    atexit.register(_dump)
'''


def run_one(a, site, root, td, i, tid, imp):
    dump = Path(td) / f"t{i}"
    dump.mkdir()
    env = dict(os.environ, ISO_OUT=str(dump), ISO_SRC=root, PYTHONPATH=str(site), PYTHON_CPU_COUNT="2")
    r = subprocess.run([a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", tid],
                       cwd=root, env=env, capture_output=True, text=True)
    ok = r.returncode == 0 and re.search(r"\b1 passed\b", r.stdout) is not None
    # pool children may still be saving: wait until every process that saved coverage wrote its marker
    deadline = time.time() + 20
    while True:
        covs = list(dump.glob(".cov.*"))
        done = list(dump.glob("*.done"))
        if covs and len(covs) == len(done):
            time.sleep(0.2)
            if len(list(dump.glob(".cov.*"))) == len(covs):
                break
        if time.time() > deadline:
            ok = False
            break
        time.sleep(0.1)
    hit = set()
    for f in dump.glob(".cov.*"):
        data = coverage.CoverageData(basename=str(f))
        data.read()
        for mf in data.measured_files():
            rp = os.path.realpath(mf)
            if rp.startswith(root + os.sep):
                rel = rp[len(root) + 1:]
                il = imp.get(rel)
                if il:
                    hit.update((rel, n) for n in (set(data.lines(mf) or []) & il))
    for f in dump.iterdir():
        f.unlink()
    dump.rmdir()
    return i, ok, hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--max-unknown", type=int, default=10)
    a = ap.parse_args()
    root = str(Path(a.src).resolve())
    tests = [t["id"] for t in json.loads(Path(a.tests).read_text())["tests"]]
    imp = {k: set(v) for k, v in json.load(gzip.open(a.map, "rt"))["imp"].items()}
    by_line = {}
    unknown = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        site = Path(td) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITE)
        with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
            futs = [ex.submit(run_one, a, site, root, td, i, tid, imp) for i, tid in enumerate(tests)]
            for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                i, ok, hit = fut.result()
                if not ok:
                    unknown.append(i)
                for k in hit:
                    by_line.setdefault(k, set()).add(i)
                if n % 100 == 0:
                    print(f"{n}/{len(tests)} tests, {len(unknown)} unknown, {time.time() - t0:.0f}s", flush=True)
    if len(unknown) > a.max_unknown:
        raise SystemExit(f"{len(unknown)} tests did not pass in isolation: {[tests[i] for i in unknown][:20]}")
    sets, idx, lines = [], {}, {}
    for (rel, ln), s in sorted(by_line.items()):
        key = sum(1 << i for i in s)
        if key not in idx:
            idx[key] = len(sets)
            sets.append(format(key, "x"))
        lines.setdefault(rel, {})[str(ln)] = idx[key]
    out = {"tests": tests, "sets": sets, "lines": lines, "unknown": sorted(tests[i] for i in unknown)}
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(tests)} tests ({len(unknown)} unknown: {[tests[i] for i in unknown]}), "
          f"{sum(len(v) for v in lines.values())} import-time lines executed, {len(sets)} distinct sets, "
          f"{os.path.getsize(a.out)} bytes, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
