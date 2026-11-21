#!/usr/bin/env python3
"""Trace reproducibility: compare two builds of the same map (different hash seed / worker count / order) entry by
entry as sets of test ids. A test present in one build's entry and absent in the other's is a dependency that
'disappears' between runs: a rule relying on that entry could drop a test that really depends on the code.

  python3 scripts/audit_trace_repro.py vmap A.json.gz B.json.gz
  python3 scripts/audit_trace_repro.py map  A.json.gz B.json.gz
"""

import gzip
import json
import sys


def load(p):
    return json.load(gzip.open(p, "rt"))


def ids(d, idx):
    b = int(d["sets"][idx], 16)
    t = d["tests"]
    return frozenset(t[i] for i in range(len(t)) if b >> i & 1)


def flatten_vmap(d):
    out = {}
    for site, e in d.get("dict", {}).items():
        for k, idx in e.get("r", {}).items():
            out[("dict", site, k)] = ids(d, idx) | ({"<ALL>"} if k in e.get("all", []) else frozenset())
    for site, e in d.get("cmp", {}).items():
        for op, idx in e.get("r", {}).items():
            out[("cmp", site, op)] = ids(d, idx)
        out[("cmp-all", site)] = frozenset(e.get("all", []))
    for site, e in d.get("cv", {}).items():
        for typ, val, idx, all_ in e["vals"]:
            out[("cv", site, typ, repr(val))] = ids(d, idx) | ({"<ALL>"} if all_ else frozenset())
        if e.get("unk"):
            out[("cv-unk", site)] = ids(d, e["unk"][0]) | ({"<ALL>"} if e["unk"][1] else frozenset())
    for nm, per in d.get("attr", {}).items():
        for owner, (idx, all_) in per.items():
            out[("attr", nm, owner)] = ids(d, idx) | ({"<ALL>"} if all_ else frozenset())
    out[("untraced",)] = frozenset(d["tests"][i] for i in d.get("untraced", []))
    return out


def flatten_map(d):
    out = {}
    for f, per in d.get("lines", {}).items():
        for ln, idx in per.items():
            out[("line", f, ln)] = ids(d, idx)
    for k, idx in d.get("users", {}).items():
        out[("users", k)] = ids(d, idx)
    for k, idx in d.get("opens", {}).items():
        out[("opens", k)] = ids(d, idx)
    return out


def main():
    kind, pa, pb = sys.argv[1:4]
    fa, fb = (flatten_vmap if kind == "vmap" else flatten_map)(load(pa)), (flatten_vmap if kind == "vmap" else flatten_map)(load(pb))
    only_a = [k for k in fa if k not in fb]
    only_b = [k for k in fb if k not in fa]
    lost, gained = [], []
    for k in fa.keys() & fb.keys():
        if fa[k] - fb[k]:
            lost.append((k, sorted(fa[k] - fb[k])))
        if fb[k] - fa[k]:
            gained.append((k, sorted(fb[k] - fa[k])))
    print(f"{kind}: {len(fa)} vs {len(fb)} entries; only in A {len(only_a)}, only in B {len(only_b)}; "
          f"entries with tests in A not B {len(lost)}, in B not A {len(gained)}")
    for title, rows in (("only in A", only_a[:8]), ("only in B", only_b[:8])):
        for k in rows:
            print(f"  {title}: {k}")
    for title, rows in (("A\\B", lost[:12]), ("B\\A", gained[:12])):
        for k, t in rows:
            print(f"  {title} {k}: {len(t)} e.g. {t[:3]}")


if __name__ == "__main__":
    main()
