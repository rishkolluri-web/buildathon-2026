#!/usr/bin/env python3
"""Compare selector configurations patch by patch against a reference (known-good V2).

For each config: per-patch cost, tests REMOVED vs the reference, and whether any
removed test is a known failing test (sample answers / mutation ground truth).
Also lists the patches where the reference selects > 200 tests, ranked by cost,
and checks that idle fill adds 0.0 s to the busiest worker.

  python3 scripts/compare_rules.py --kit KIT --src SQLGLOT --stress STRESS --python PY \
      --ref NAME=SELECTOR,MAP[,BR_OFF] --config NAME=SELECTOR,MAP[,BR_OFF] ...
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_spec(spec):
    name, rest = spec.split("=", 1)
    parts = rest.split(",")
    return name, parts[0], parts[1], ",".join(parts[2:])


def run(py, sel, mp, src, data, off):
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(src), BR_MAP=str(mp), BR_OFF=off)
    p = subprocess.run([py, sel], env=env, capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr)
    return p.stdout


def parse(out, ms):
    res = {}
    for line in out.splitlines():
        r = json.loads(line)
        w = r["workers"]
        loads = [sum(ms[n] for n in x) for x in w]
        res[r["commit"]] = {"cost": (max(loads) + 2000 * len(w)) if w else 0, "sel": {n for x in w for n in x},
                            "max": max(loads) if loads else 0, "k": len(w)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--stress", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--config", action="append", default=[])
    ap.add_argument("--show", type=int, default=12)
    ap.add_argument("--ref-prefill", help="config that reproduces the reference but supports BR_OFF=fill "
                                          "(used only to count the reference's selection before free fill)")
    a = ap.parse_args()

    kit, stress = Path(a.kit).resolve(), Path(a.stress).resolve()
    tests = json.loads((kit / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])

    # known failing sets: sample answers + mutation ground truth
    fails = {f"s:{k}": set(v["failing"]) for k, v in json.loads((kit / "sample" / "answers.json").read_text())["commits"].items()}
    base = set(json.loads((stress / "truth" / "base.json").read_text()))
    meta = json.loads((stress / "meta.json").read_text())
    for mid in meta:
        f = stress / "truth" / f"{mid}.json"
        if f.exists():
            s = set()
            for nid in set(json.loads(f.read_text())) - base:
                if nid.startswith("COLLECT::"):
                    s |= by_file.get(nid[9:], set()) or set(ms)
                elif nid == "SESSION_FAILED":
                    s |= set(ms)
                elif nid in num:
                    s.add(num[nid])
            fails[f"m:{mid}"] = s
    mdata = Path(tempfile.mkdtemp())
    shutil.copytree(stress / "commits", mdata / "commits")
    shutil.copy(kit / "sample" / "tests.json", mdata / "tests.json")

    def results(spec):
        name, sel, mp, off = parse_spec(spec)
        r = {f"s:{k}": v for k, v in parse(run(a.python, sel, mp, a.src, kit / "sample", off), ms).items()}
        r.update({f"m:{k}": v for k, v in parse(run(a.python, sel, mp, a.src, mdata, off), ms).items()})
        nofill = {f"s:{k}": v for k, v in parse(run(a.python, sel, mp, a.src, kit / "sample", ",".join(filter(None, [off, "fill"]))), ms).items()}
        nofill.update({f"m:{k}": v for k, v in parse(run(a.python, sel, mp, a.src, mdata, ",".join(filter(None, [off, "fill"]))), ms).items()})
        fill_delta = max(abs(r[k]["max"] - nofill[k]["max"]) for k in r) / 1000
        return name, r, nofill, fill_delta

    ref_name, ref, ref_nf, ref_fd = results(a.ref)
    if a.ref_prefill:
        _, pre, pre_nf, _ = results(a.ref_prefill)
        assert all(pre[k]["cost"] == ref[k]["cost"] for k in ref), "ref-prefill does not reproduce the reference"
        ref_nf = pre_nf
    n_truth = sum(1 for k in fails if k.startswith("m:"))
    print(f"ground truth: 30 sample commits + {n_truth}/{len(meta)} mutants")

    big = sorted(((k, v) for k, v in ref.items() if len(ref_nf[k]["sel"]) > 200), key=lambda kv: -kv[1]["cost"])
    print(f"\n[{ref_name}] patches selecting > 200 tests (before free fill), by cost:")
    for k, v in big[: a.show]:
        print(f"  {k:7} sel={len(ref_nf[k]['sel']):5} cost={v['cost']/1000:6.2f}s truth_fail={len(fails[k]) if k in fails else '?'}")
    print(f"  ... {len(big)} such patches, {sum(1 for k,_ in big if len(ref_nf[k]['sel']) == len(ms))} select all {len(ms)}")

    def summary(name, r, nf, fd):
        s_cost = sum(v["cost"] for k, v in r.items() if k.startswith("s:")) / 1000
        m_cost = sum(v["cost"] for k, v in r.items() if k.startswith("m:")) / 1000
        misses = sorted(k for k in fails if fails[k] - r[k]["sel"])
        n_all = sum(1 for k in r if len(nf[k]["sel"]) == len(ms))
        return s_cost, m_cost, misses, n_all, fd

    print(f"\n{'config':24} {'sample':>8} {'mutants':>9} {'all1620':>7} {'misses':>6} {'fill+max':>8}")
    s = summary(ref_name, ref, ref_nf, ref_fd)
    print(f"{ref_name:24} {s[0]:8.1f} {s[1]:9.1f} {s[3]:7d} {len(s[2]):6d} {s[4]:8.1f}" + (f"  MISSED {s[2]}" if s[2] else ""))
    for spec in a.config:
        name, r, nf, fd = results(spec)
        s = summary(name, r, nf, fd)
        print(f"{name:24} {s[0]:8.1f} {s[1]:9.1f} {s[3]:7d} {len(s[2]):6d} {s[4]:8.1f}" + (f"  MISSED {s[2]}" if s[2] else ""))
        # removed = tests in the reference's full output (incl. free fill) missing from ours
        changed = sorted((k for k in r if r[k]["cost"] != ref[k]["cost"] or not ref[k]["sel"] <= r[k]["sel"]),
                         key=lambda k: r[k]["cost"] - ref[k]["cost"])
        print(f"    {len(changed)} patches differ from {ref_name}; cost change {sum(r[k]['cost'] - ref[k]['cost'] for k in changed)/1000:+.1f}s")
        for k in changed[: a.show]:
            removed = ref[k]["sel"] - r[k]["sel"]
            hit = removed & fails.get(k, set())
            tf = "?" if k not in fails else len(fails[k])
            print(f"    {k:7} cost {ref[k]['cost']/1000:6.2f} -> {r[k]['cost']/1000:6.2f}  removed {len(removed):5} tests"
                  f"  truth_fail={tf}{'  REMOVED-FAILING ' + str(sorted(hit)) if hit else ''}")
    shutil.rmtree(mdata, ignore_errors=True)


if __name__ == "__main__":
    main()
