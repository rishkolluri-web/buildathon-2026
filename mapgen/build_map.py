#!/usr/bin/env python3
"""Turn a per-test coverage run of sqlglot (pinned commit) into the selector's map.

Inputs (all produced by running sqlglot's OWN test suite at the pinned commit;
nothing here reads sample answers):
  --coverage  .coverage file from pytest-cov with --cov-context=test
  --opens     directory of opens-*.json from trace_plugin.py
  --tracks    directory of tracks-*.json from track_plugin.py (optional)
  --tests     tests.json (only to fix the test order / ids)
  --root      the sqlglot checkout the coverage was measured in

Output: a gzip JSON map
  tests: [test id, ...]                      (index = bit position)
  sets:  [hex bitset, ...]                   (deduplicated test sets)
  lines: {relpath: {line: set index}}        (who executed that line, per test)
  opens: {relpath: set index}                (who opened that data file)
  touch: {relpath: set index}                (who executed ANY line of the file)
  users: {"module:Class": set index}       (tests instantiating it or a subclass)
"""

import argparse
import glob
import gzip
import json
import os

import coverage


class SetTable:
    def __init__(self):
        self.index = {}
        self.sets = []

    def add(self, bits: int) -> int:
        if bits not in self.index:
            self.index[bits] = len(self.sets)
            self.sets.append(bits)
        return self.index[bits]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", required=True)
    ap.add_argument("--opens", required=True)
    ap.add_argument("--tracks")
    ap.add_argument("--tests", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = os.path.realpath(args.root) + os.sep
    test_ids = [t["id"] for t in json.load(open(args.tests))["tests"]]
    pos = {tid: i for i, tid in enumerate(test_ids)}
    table = SetTable()

    def bits_of(nodeids):
        b = 0
        for n in nodeids:
            i = pos.get(n)
            if i is not None:
                b |= 1 << i
        return b

    data = coverage.CoverageData(basename=args.coverage)
    data.read()
    lines_out, touch_out = {}, {}
    unknown = set()
    for path in sorted(data.measured_files()):
        real = os.path.realpath(path)
        if not real.startswith(root):
            continue
        rel = real[len(root):]
        by_line = data.contexts_by_lineno(path)
        per_line = {}
        touched = 0
        for line, ctxs in by_line.items():
            nodeids = set()
            for c in ctxs:
                nid = c.rsplit("|", 1)[0] if "|" in c else c
                if nid and nid not in pos:
                    unknown.add(nid)
                nodeids.add(nid)
            b = bits_of(nodeids)
            touched |= b
            per_line[str(line)] = table.add(b)
        lines_out[rel] = per_line
        touch_out[rel] = table.add(touched)

    opens_raw = {}
    for f in glob.glob(os.path.join(args.opens, "opens-*.json")):
        for nid, paths in json.load(open(f)).items():
            for p in paths:
                opens_raw.setdefault(p, set()).add(nid)
    opens_out = {p: table.add(bits_of(n)) for p, n in sorted(opens_raw.items())}

    # users[C] = tests that instantiated C or any subclass of C
    users_out = {}
    if args.tracks:
        mro = {}
        for f in glob.glob(os.path.join(args.tracks, "mro-*.json")):
            mro.update(json.load(open(f)))
        users = {}
        for f in glob.glob(os.path.join(args.tracks, "tracks-*.json")):
            for nid, types in json.load(open(f)).items():
                if nid not in pos:
                    continue
                for typ in types:
                    for base in mro.get(typ, [typ]):
                        users.setdefault(base, set()).add(nid)
        for cid in sorted(set(mro) | set(users)):
            users_out[cid] = table.add(bits_of(users.get(cid, ())))

    out = {
        "tests": test_ids,
        "sets": [format(b, "x") for b in table.sets],
        "lines": lines_out,
        "opens": opens_out,
        "touch": touch_out,
        "users": users_out,
    }
    with gzip.open(args.out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"wrote {args.out}: {os.path.getsize(args.out)} bytes, {len(table.sets)} sets, "
        f"{len(lines_out)} files, unknown contexts {len(unknown)} e.g. {sorted(unknown)[:3]}"
    )


if __name__ == "__main__":
    main()
