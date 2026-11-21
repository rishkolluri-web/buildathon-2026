#!/usr/bin/env python3
"""Order / shared-state audit: run mutants (and the base) under several test orders and worker layouts and compare
the failing sets with the corpus truth (xdist -n 4). A failure that appears only in some layouts is order-dependent:
the official labeling could see it, so the selector must still select it.

Configurations (each a full-suite pytest run in a disposable patched copy):
  seq        -p no:xdist, collection order
  shufA/B    -p no:xdist, items shuffled with a fixed seed (ORDER_SEED, via the bundled plugin)
  n8         -n 8 (load)
  loadfile   -n 4 --dist loadfile
  python3 scripts/audit_order.py --src ../sqlglot --python ABS --corpus ../stress_sa --ids base,sa1,... --out DIR
Results: DIR/<corpus>/<id>.<cfg>.json (failing node ids, COLLECT::file for collection errors).
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
sys.path.insert(0, str(REPO / "scripts"))
from stress_mutate import apply_patch_files  # noqa: E402

PLUGIN = '''
import json, os, random
_failed = set()
def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get("ORDER_SEED")
    if seed:
        random.Random(int(seed)).shuffle(items)
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT::" + report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    if not hasattr(session.config, "workerinput"):
        with open(os.environ["MUT_RESULT"], "w") as f:
            json.dump(sorted(_failed), f)
'''

CONFIGS = {
    "seq": (["-p", "no:xdist"], None),
    "shufA": (["-p", "no:xdist"], "11"),
    "shufB": (["-p", "no:xdist"], "22"),
    "n8": (["-n", "8"], None),
    "loadfile": (["-n", "4", "--dist", "loadfile"], None),
}


def run(mid, cfg, a, src, corpus, plugin_dir, res):
    if res.exists():
        return "cached"
    work = Path(tempfile.mkdtemp(prefix=f"ord-{mid}-"))
    try:
        for d in ("sqlglot", "tests", "sqlglotc"):
            shutil.copytree(src / d, work / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("pyproject.toml", "setup.cfg", "setup.py"):
            if (src / f).exists():
                shutil.copy(src / f, work / f)
        if mid != "base":
            for path, new in apply_patch_files(src, (corpus / "commits" / f"{mid}.patch").read_text()):
                if new is None:
                    (work / path).unlink(missing_ok=True)
                else:
                    (work / path).parent.mkdir(parents=True, exist_ok=True)
                    (work / path).write_text(new)
        args, seed = CONFIGS[cfg]
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{work}:{plugin_dir}", MUT_RESULT=str(res) + ".tmp",
                   PATH=f"{Path(a.python).parent}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1",
                   PYTHON_CPU_COUNT=str(a.pool_cpus))
        env.pop("ORDER_SEED", None)
        if seed:
            env["ORDER_SEED"] = seed
        p = subprocess.run([a.python, "-m", "pytest", "tests", "-p", "order_plugin", "-p", "no:cacheprovider",
                            *args, "--timeout", str(a.timeout), "-q"], cwd=work, env=env, capture_output=True,
                           text=True, timeout=3600)
        if not Path(str(res) + ".tmp").exists():
            Path(str(res) + ".tmp").write_text(json.dumps(["SESSION_FAILED"]))
            Path(str(res) + ".log").write_text(p.stdout[-4000:] + p.stderr[-4000:])
        os.replace(str(res) + ".tmp", res)
        return (p.stdout.strip().splitlines() or ["?"])[-1]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--pool-cpus", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    src, corpus = Path(a.src).resolve(), Path(a.corpus).resolve()
    out = Path(a.out) / corpus.name
    out.mkdir(parents=True, exist_ok=True)
    plugin_dir = Path(a.out) / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "order_plugin.py").write_text(PLUGIN)
    for mid in a.ids.split(","):
        for cfg in a.configs.split(","):
            r = run(mid, cfg, a, src, corpus, plugin_dir, out / f"{mid}.{cfg}.json")
            print(f"{mid:40s} {cfg:9s} {r}", flush=True)


if __name__ == "__main__":
    main()
