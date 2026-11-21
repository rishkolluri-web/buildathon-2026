#!/usr/bin/env python3
"""Per-test import closure of the slowest tests (offline, pinned suite only) -> heavy.json.

Each test longer than --min-seconds in tests.json runs ALONE in a fresh pytest process (no xdist,
no cache), with a sitecustomize that records, at exit of EVERY Python process (the test process and
spawned ProcessPool children), which sqlglot modules are in sys.modules. The union is the test's
closure: every sqlglot module its collection or execution imports, anywhere.

selector.py uses it to keep such a test out of an ALL fallback whose changed modules it never
imports (plus fail-closed side-effect checks there).

  python3 mapgen/heavy_closure.py --src ../sqlglot --python ../.venv313/bin/python \
      --tests ../blast-radius-sample/sample/tests.json --out heavy.json
"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

SITE = '''
import atexit, json, os, sys
_out = os.environ.get("HEAVY_CLOSURE_OUT")
def _dump():
    mods = sorted(m for m in list(sys.modules) if m == "sqlglot" or m.startswith("sqlglot."))
    with open(os.path.join(_out, "%d.json" % os.getpid()), "w") as f:
        json.dump(mods, f)
if _out:
    atexit.register(_dump)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-seconds", type=float, default=3.0)
    a = ap.parse_args()
    tests = json.loads(Path(a.tests).read_text())["tests"]
    heavy = [t["id"] for t in tests if t["seconds"] > a.min_seconds]
    out = {}
    with tempfile.TemporaryDirectory() as td:
        site = Path(td) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITE)
        for tid in heavy:
            dump = Path(td) / f"d{len(out)}"
            dump.mkdir()
            env = dict(os.environ, HEAVY_CLOSURE_OUT=str(dump), PYTHONPATH=str(site), PYTHON_CPU_COUNT="2")
            r = subprocess.run([a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", tid],
                               cwd=a.src, env=env, capture_output=True, text=True)
            if r.returncode != 0:  # a failing/erroring run's closure is not trustworthy: leave the test out
                raise SystemExit(f"{tid}: pytest exit {r.returncode}\n{r.stdout[-2000:]}")
            mods = set()
            dumps = list(dump.glob("*.json"))
            if not dumps:
                raise SystemExit(f"{tid}: no closure dump")
            for f in dumps:
                mods |= set(json.loads(f.read_text()))
            out[tid] = sorted(mods)
            print(f"{tid}: {len(mods)} modules, {len(dumps)} process(es)")
    Path(a.out).write_text(json.dumps({"min_seconds": a.min_seconds, "closure": out}, indent=0) + "\n")


if __name__ == "__main__":
    main()
