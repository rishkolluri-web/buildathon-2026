#!/usr/bin/env python3
"""Union a post-reset coverage map (RESET_PASS=1 mapgen/run_coverage.sh, mapgen/reset_plugin.py) into the
normal map: per line / file / class / identifier reader set, the tests of either map. Data-file opens are
not merged (tests that fail in the post-reset pass read sqlglot sources for their tracebacks). Selections only grow.

  python3 mapgen/union_map.py <normal map.json.gz> <post-reset map.json.gz> <out map.json.gz>
"""
import gzip
import json
import sys


def main(base_p, extra_p, out_p):
    base = json.load(gzip.open(base_p, "rt"))
    extra = json.load(gzip.open(extra_p, "rt"))
    pos = {t: i for i, t in enumerate(base["tests"])}
    remap = [pos.get(t) for t in extra["tests"]]  # extra bit -> base bit (None: test unknown to the base map)
    if any(r is None for r in remap):
        raise SystemExit("post-reset map has tests the normal map lacks")
    ecache = {}

    def ebits(idx):
        if idx not in ecache:
            b, i, out = int(extra["sets"][idx], 16), 0, 0
            while b:
                if b & 1:
                    out |= 1 << remap[i]
                b >>= 1
                i += 1
            ecache[idx] = out
        return ecache[idx]

    sets = [int(h, 16) for h in base["sets"]]
    index = {b: i for i, b in enumerate(sets)}

    def idx_of(bits):
        if bits not in index:
            index[bits] = len(sets)
            sets.append(bits)
        return index[bits]

    grown = 0
    for f, lines in extra["lines"].items():
        cur = base["lines"].setdefault(f, {})
        for ln, idx in lines.items():
            old = sets[cur[ln]] if ln in cur else 0
            new = old | ebits(idx)
            if new != old:
                grown += 1
                cur[ln] = idx_of(new)
    for key in ("touch", "users"):  # not "opens": failing tests read sqlglot sources for tracebacks (linecache)
        cur = base.setdefault(key, {})
        for k, idx in extra.get(key, {}).items():
            if not isinstance(idx, int):
                continue
            old = sets[cur[k]] if k in cur else 0
            new = old | ebits(idx)
            if new != old:
                grown += 1
                cur[k] = idx_of(new)
    # runtime readers per identifier (build_map: union of the line sets of its read lines): union per file
    for name, e in extra.get("refs", {}).items():
        be = base.setdefault("refs", {}).setdefault(name, {})
        for kind in ("rtn", "rta"):
            for rel, idx in e.get(kind, {}).items():
                cur = be.setdefault(kind, {})
                old = sets[cur[rel]] if rel in cur else 0
                new = old | ebits(idx)
                if new != old:
                    grown += 1
                    cur[rel] = idx_of(new)
    base["sets"] = [format(b, "x") for b in sets]
    with gzip.open(out_p, "wt", encoding="utf-8") as f:
        json.dump(base, f, separators=(",", ":"))
    print(f"{grown} entries grew -> {out_p}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
