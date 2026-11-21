#!/usr/bin/env python3
"""Solo value traces of the slow tests (> 3 s in tests.json) -> heavy_vt.json (v20 heavy value rule).

Each slow test is run ALONE (mapgen/run_heavy_vt.sh) under mapgen/vtrace.py (relational comparison
alternatives, constant-comparison operand values; process pools run inline with a pickle round trip) and
under mapgen/eq_trace.py (is/== interchangeability; spawned pool children traced through sitecustomize),
once per hash seed. In a solo run every observation (import, collection, session, pool work) belongs to
that one test, so the per-test result covers the test's whole isolated life, imports included.

Output, per test id (only if every run passed and recorded the test as done):
  seen  relational comparison sites executed          cmp  site -> alternative ops with a differing result
  cv    site -> [[type, value], ...] observed non-constant operand values, or "U" (unknown)
  eq    site -> true if every execution compared an identity/equality-interchangeable pair
A site absent from `seen`/`cv` was not executed by that test (only meaningful for sites the full-suite
trace proves instrumented: the selector checks that); absent from `eq` -> unknown (the selector keeps the test).

  python3 mapgen/heavy_vt.py <solo root: <name>/s<seed>/{vraw,eraw,ok}> <tests.json> <out heavy_vt.json>
"""
import json
import sys
from pathlib import Path


def main(root, tests_json, out):
    tests = json.loads(Path(tests_json).read_text())["tests"]
    slow = {t["id"] for t in tests if t["seconds"] > 3}
    res, bad = {}, []
    for tdir in sorted(Path(root).iterdir()):
        runs = sorted(p for p in tdir.glob("s*") if p.is_dir())
        if not runs:
            continue
        rec = {"seen": set(), "cmp": {}, "cv": {}, "eq": {}}
        tid = None
        ok = True
        for r in runs:
            if not (r / "ok").exists():
                ok = False
                break
            vt = [json.loads(p.read_text()) for p in sorted((r / "vraw").glob("vt-*.json"))]
            eqs = [json.loads(p.read_text()) for p in sorted((r / "eraw").glob("eq-*.json"))]
            done = {d for v in vt for d in v.get("done", ())}
            ids = (r / "ok").read_text().split()
            if len(ids) != 1 or ids[0] not in done or not eqs:
                ok = False
                break
            tid = ids[0]
            for v in vt:
                rec["seen"].update(v["cmp_seen"])
                for s, alts in v["cmp"].items():
                    rec["cmp"].setdefault(s, set()).update(alts)
                for s, per in v["cv"].items():
                    cur = rec["cv"].get(s, set())
                    for vals in per.values():
                        if vals == "U" or cur == "U":
                            cur = "U"
                        else:
                            cur |= {(type(x).__name__, x) for x in vals}
                    rec["cv"][s] = cur
            for e in eqs:
                for s, (_, safe) in e.items():
                    rec["eq"][s] = rec["eq"].get(s, True) and bool(safe)
        if not ok or tid not in slow:
            bad.append(tdir.name)
            continue
        res[tid] = {
            "seen": sorted(rec["seen"]),
            "cmp": {s: sorted(a) for s, a in sorted(rec["cmp"].items())},
            "cv": {s: ("U" if v == "U" else sorted(v)) for s, v in sorted(rec["cv"].items())},
            "eq": dict(sorted(rec["eq"].items())),
            "runs": len(runs),
        }
    missing = slow - set(res)
    Path(out).write_text(json.dumps({"tests": res}, separators=(",", ":")))
    print(f"{len(res)} slow tests traced ({', '.join(t.split('::')[-1] for t in res)}); "
          f"rejected {bad}; untraced slow tests (rule never drops them): {sorted(missing)}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
