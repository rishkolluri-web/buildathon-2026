#!/usr/bin/env python3
"""Single-command gate: is the working-tree selector a safe improvement over HEAD?

PASS only if ALL hold:
  1. sample misses = 0 (30 kit commits, answers.json)
  2. regression + mutation misses = 0: for every mutant with ground truth,
     failing(A) - selected(B) is empty (fast set check, no test runs)
  3. all unit tests pass (python -m unittest discover -s tests)
  4. sample + mutation simulated cost strictly lower than HEAD's
  5. free-rider fill adds 0.0 s to every commit's busiest worker
  6. every patch whose selection DROPS tests the base selected has ground truth
     (a narrowing nobody can check is not accepted)

  python3 scripts/gate.py            # defaults assume ../blast-radius-sample, ../sqlglot, ../stress
  python3 scripts/gate.py --stress ../stress --stress ../stress_h3 --stress ../stress_x
Exit code 0 = PASS, 1 = FAIL. Prints the measured numbers either way.
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


def run_selector(py, sel, mp, src, data, off=""):
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src), BR_MAP=str(mp), BR_OFF=off)
    p = subprocess.run([py, sel], env=env, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"selector crashed:\n{p.stderr[-2000:]}")
    return p.stdout


def parse(out, ms):
    res = {}
    for line in out.splitlines():
        r = json.loads(line)
        w = r["workers"]
        loads = [sum(ms[n] for n in x) for x in w]
        res[r["commit"]] = {"cost": (max(loads) + 2000 * len(w)) if w else 0,
                            "max": max(loads) if loads else 0, "sel": {n for x in w for n in x}}
    return res


def load_truth(stress, tests):
    ms = {t["number"] for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])
    truth = {}
    base_f = stress / "truth" / "base.json"
    if not base_f.exists():
        return truth
    base = set(json.loads(base_f.read_text()))

    def to_nums(nids):
        s = set()
        for nid in nids:
            if nid.startswith("COLLECT::"):
                s |= by_file.get(nid[9:], set()) or set(ms)
            elif nid == "SESSION_FAILED":
                s |= set(ms)
            elif nid in num:
                s.add(num[nid])
        return s

    # supplementary subset runs: truth/<id>.<tag>.json against truth/base.<tag>.json
    tags = {f.name.split(".")[1] for f in (stress / "truth").glob("base.*.json")}
    for f in sorted((stress / "truth").glob("*.json")):
        if f.stem == "base" or "." in f.stem:
            continue
        s = to_nums(set(json.loads(f.read_text())) - base)
        for tag in tags:
            sup = stress / "truth" / f"{f.stem}.{tag}.json"
            if sup.exists():
                s |= to_nums(set(json.loads(sup.read_text())) - set(json.loads((stress / "truth" / f"base.{tag}.json").read_text())))
        truth[f.stem] = s
    return truth


def unverifiable(stress):
    """Baseline-failing test ids whose breakage this corpus cannot detect: failing in
    truth/base.json and not re-run passing by a tagged subset run (truth/<tag>.paths.json
    lists the subset's test paths, truth/base.<tag>.json its baseline failures)."""
    base_f = stress / "truth" / "base.json"
    if not base_f.exists():
        return set()
    bad = set(json.loads(base_f.read_text()))
    for f in (stress / "truth").glob("*.paths.json"):
        tag = f.name[: -len(".paths.json")]
        paths = json.loads(f.read_text())
        sub_base = set(json.loads((stress / "truth" / f"base.{tag}.json").read_text()))
        bad = {n for n in bad if n in sub_base or not any(n.startswith(p) for p in paths)}
    return bad


def evaluate(py, sel, mp, src, kit, mdata, ms, off=""):
    r = {f"s:{k}": v for k, v in parse(run_selector(py, sel, mp, src, kit / "sample", off), ms).items()}
    r.update({f"m:{k}": v for k, v in parse(run_selector(py, sel, mp, src, mdata, off), ms).items()})
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", default=str(REPO.parent / "blast-radius-sample"))
    ap.add_argument("--src", default=str(REPO.parent / "sqlglot"))
    ap.add_argument("--stress", action="append", default=None,
                    help="mutation corpus dir (repeatable; default ../stress); mutant ids must be unique")
    ap.add_argument("--python", default=str(REPO.parent / ".venv313" / "bin" / "python"))
    ap.add_argument("--base", default="HEAD", help="git revision to beat")
    ap.add_argument("--json", help="write the measured numbers here")
    ap.add_argument("--allow-cost-increase", action="store_true",
                    help="correctness fixes: report cost but do not require it to drop "
                         "(all safety checks still apply)")
    a = ap.parse_args()

    kit = Path(a.kit).resolve()
    corpora = [Path(s).resolve() for s in (a.stress or [str(REPO.parent / "stress")])]
    tests = json.loads((kit / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    answers = {f"s:{k}": set(v["failing"]) for k, v in json.loads((kit / "sample" / "answers.json").read_text())["commits"].items()}
    truth = {f"m:{k}": v for c in corpora for k, v in load_truth(c, tests).items()}
    for c in corpora:
        u = unverifiable(c)
        print(f"  [info] {c.name}: {len(u)} baseline-failing test ids cannot be verified" + (f": {sorted(u)}" if u else ""))

    tmp = Path(tempfile.mkdtemp())
    mdata = tmp / "mdata"
    (mdata / "commits").mkdir(parents=True)
    for c in corpora:
        for f in (c / "commits").glob("*.patch"):
            assert not (mdata / "commits" / f.name).exists(), f"duplicate mutant id {f.name}"
            shutil.copy(f, mdata / "commits" / f.name)
    shutil.copy(kit / "sample" / "tests.json", mdata / "tests.json")
    basedir = tmp / "base"
    basedir.mkdir()
    for f in ("selector.py", "map.json.gz", "eqsites.json"):
        p = subprocess.run(["git", "show", f"{a.base}:{f}"], cwd=REPO, capture_output=True)
        if p.returncode:
            if f == "eqsites.json":  # absent before typecheck-v8
                continue
            raise SystemExit(f"git show {a.base}:{f} failed")
        (basedir / f).write_bytes(p.stdout)

    cand = evaluate(a.python, str(REPO / "selector.py"), REPO / "map.json.gz", a.src, kit, mdata, ms)
    base = evaluate(a.python, str(basedir / "selector.py"), basedir / "map.json.gz", a.src, kit, mdata, ms)
    nofill = evaluate(a.python, str(REPO / "selector.py"), REPO / "map.json.gz", a.src, kit, mdata, ms, "fill")

    s_miss = sorted(k for k, f in answers.items() if f - cand[k]["sel"])
    m_miss = sorted(k for k, f in truth.items() if f - cand[k]["sel"])
    unverified = sorted(k for k in cand if k.startswith("m:") and k not in truth and base[k]["sel"] - cand[k]["sel"])
    fill_bad = sorted(k for k in cand if cand[k]["max"] != nofill[k]["max"] or cand[k]["cost"] != nofill[k]["cost"])
    cost = lambda r, p: sum(v["cost"] for k, v in r.items() if k.startswith(p)) / 1000  # noqa: E731
    c_s, c_m, b_s, b_m = cost(cand, "s:"), cost(cand, "m:"), cost(base, "s:"), cost(base, "m:")

    env = dict(os.environ, BR_SRC=a.src, BR_KIT=str(kit))
    ut = subprocess.run([a.python, "-m", "unittest", "discover", "-s", "tests"], cwd=REPO, env=env,
                        capture_output=True, text=True)
    ut_ok = ut.returncode == 0
    ut_line = (ut.stderr.strip().splitlines() or ["?"])[-1]

    checks = [
        ("sample misses = 0", not s_miss, f"{len(s_miss)} {s_miss}" if s_miss else "0/30"),
        ("mutation misses = 0", not m_miss, f"{len(m_miss)} {m_miss}" if m_miss else f"0/{len(truth)} with ground truth"),
        ("unit tests pass", ut_ok, ut_line),
        ("cost strictly lower than " + a.base + (" (informational)" if a.allow_cost_increase else ""),
         a.allow_cost_increase or (c_s + c_m) < (b_s + b_m),
         f"sample {b_s:.1f}->{c_s:.1f}  mutants {b_m:.1f}->{c_m:.1f}  total {b_s + b_m:.1f}->{c_s + c_m:.1f} ({c_s + c_m - b_s - b_m:+.1f}s)"),
        ("fill adds 0.0 s to max worker", not fill_bad, f"{len(fill_bad)} commits differ" if fill_bad else "0.0 s on all"),
        ("narrowed patches have ground truth", not unverified,
         f"unverified: {unverified}" if unverified else "all narrowed patches verified"),
    ]
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ut_ok:
        print(ut.stderr[-1500:])
    passed = all(ok for _, ok, _ in checks)
    if a.json:
        Path(a.json).write_text(json.dumps({
            "pass": passed, "sample_cost": c_s, "mutant_cost": c_m, "base_sample_cost": b_s, "base_mutant_cost": b_m,
            "truth_mutants": len(truth), "sample_misses": s_miss, "mutant_misses": m_miss,
            "per_commit": {k: {"cost": v["cost"], "n": len(v["sel"])} for k, v in cand.items()},
        }, indent=1))
    print("GATE:", "PASS" if passed else "FAIL")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
