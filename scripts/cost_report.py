#!/usr/bin/env python3
"""Per-patch cost analysis of the working-tree selector over the sample and every truth corpus.

For each patch: selected tests, simulated cost, expensive tests (>= 1 s) selected, the rule /
region kinds responsible (in-process instrumentation of Differ regions and fallbacks), known
failing tests, the oracle cost (packing only the failing tests) and the gap. Grouped by category.

  BR_SRC=../sqlglot python scripts/cost_report.py --kit ../blast-radius-sample --stress ../stress ... [--json out]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--stress", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    import selector as S
    from gate import load_truth

    kit = Path(a.kit)
    tests = json.loads((kit / "sample" / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    name = {t["number"]: t["id"].split("::", 1)[-1] for t in tests}
    every = set(ms)
    sel = S.Selector(S.Map(S.MAP, tests))

    # --- instrumentation: record region kinds + fallback reasons per patch
    events = []
    orig_log = S.log
    S.log = lambda *x: events.append(" ".join(map(str, x)))
    orig_body = S.Differ.body

    def body(self, old, new, ctx, parent):
        n0 = len(self.regions)
        orig_body(self, old, new, ctx, parent)
        if parent is None:
            for r in self.regions:
                events.append(f"REGION {r[0]}")
        return None

    S.Differ.body = body

    patches = [("sample", p, None) for p in sorted((kit / "sample" / "commits").glob("*.patch"))]
    answers = {k: set(v["failing"]) for k, v in json.loads((kit / "sample" / "answers.json").read_text())["commits"].items()}
    truth = {}
    for c in a.stress:
        c = Path(c).resolve()
        tr = load_truth(c, tests)
        meta = json.loads((c / "meta.json").read_text()) if (c / "meta.json").exists() else {}
        for p in sorted((c / "commits").glob("*.patch")):
            if p.stem in tr:
                truth[p.stem] = tr[p.stem]
                patches.append((c.name, p, meta.get(p.stem, {}).get("kind")))
    rows = []
    for corpus, p, kind in patches:
        events.clear()
        try:
            chosen = sel.select(p.read_text(errors="replace"))
        except Exception as e:  # noqa: BLE001
            events.append(f"analysis error {e!r}")
            chosen = S.ALL
        allt = chosen is S.ALL
        chosen = every if allt else {n for n in chosen if n in ms}
        _, cost = S.pack(chosen, ms)
        failing = answers.get(p.stem) if corpus == "sample" else truth[p.stem]
        _, oc = S.pack(set(failing), ms)
        text = p.read_text(errors="replace")
        files = [l.split(" b/")[-1] for l in text.splitlines() if l.startswith("diff --git ")]
        regions = sorted({e.split()[1] for e in events if e.startswith("REGION")})
        why = [e.strip() for e in events if not e.startswith("REGION")]
        cat = category(files, regions, why, allt, kind)
        rows.append({"corpus": corpus, "id": p.stem, "files": files, "kind": kind, "category": cat,
                     "n": len(chosen), "cost": cost / 1000, "oracle": (oc or 0) / 1000, "gap": (cost - (oc or 0)) / 1000,
                     "failing": len(failing), "all": allt, "regions": regions, "why": why[:3],
                     "big": sorted(((name[n], ms[n] / 1000) for n in chosen if ms[n] >= 1000), key=lambda x: -x[1])[:5],
                     "missed": sorted(set(failing) - chosen),
                     "unneeded_big": sorted(((name[n], ms[n] / 1000, (cost - S.pack(chosen - {n}, ms)[1]) / 1000)
                                             for n in chosen - set(failing) if ms[n] >= 1000), key=lambda x: -x[2])})
    S.log = orig_log
    S.Differ.body = orig_body
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    print(f"{'category':44s} {'n':>4s} {'cost':>9s} {'oracle':>9s} {'gap':>9s} {'gap/patch':>9s}")
    for cat, rs in sorted(by.items(), key=lambda kv: -sum(r["gap"] for r in kv[1])):
        g = sum(r["gap"] for r in rs)
        print(f"{cat:44s} {len(rs):4d} {sum(r['cost'] for r in rs):9.1f} {sum(r['oracle'] for r in rs):9.1f} {g:9.1f} {g / len(rs):9.2f}")
    print(f"\nTOTAL {len(rows)} patches: cost {sum(r['cost'] for r in rows):.1f}  oracle {sum(r['oracle'] for r in rows):.1f}  "
          f"gap {sum(r['gap'] for r in rows):.1f}  missed commits {sum(1 for r in rows if r['missed'])}")
    print(f"\nTop {a.top} patches by gap:")
    for r in sorted(rows, key=lambda r: -r["gap"])[: a.top]:
        print(f"  {r['corpus']:9s} {r['id'][:30]:30s} {r['category'][:34]:34s} sel {r['n']:4d} cost {r['cost']:6.2f} "
              f"oracle {r['oracle']:6.2f} fail {r['failing']:4d} big {[b[0][:28] for b in r['big'][:3]]} {r['why'][:1]}")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


def category(files, regions, why, allt, kind):
    py = [f for f in files if f.endswith(".py")]
    w = " ".join(why)
    if any("/fixtures/" in f or (f.startswith("tests/") and not f.endswith(".py")) for f in files) and not py:
        return "fixture/data file"
    if not py:
        return "non-python other"
    if allt:
        if "import-time" in w:
            return "ALL: import-time line"
        if "syntax error" in w:
            return "ALL: syntax error"
        if "module-level" in w or "import" in w or "star" in w or "class" in w:
            return "ALL: module-level/import/class registration"
        if "new module" in w:
            return "ALL: new module"
        return "ALL: other fallback"
    if "comparison operator change" in w:
        return "comparison op (value trace)"
    if "class dict value change" in w:
        return "class-dict value (value trace)"
    if "identity/equality swap" in w:
        return "is/== swap (eqsites)"
    if any(f.startswith("tests/") for f in py) and all(f.startswith("tests/") for f in py):
        return "test-file change"
    if not regions:
        return "no AST change (comment/docstring)"
    fam = any(f.split("/")[1] in ("dialects", "parsers", "generators", "tokenizers", "typing") for f in py if f.count("/") == 2)
    tag = "+".join(regions)
    if "class" in regions:
        return f"class-level data ({'dialect family' if fam else 'core'})"
    if "module" in regions:
        return "module-level statement (refs)"
    opt = any("/optimizer/" in f or "/executor/" in f for f in py)
    core = any(f in ("sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py")
               or f.startswith("sqlglot/expressions/") for f in py)
    where = "optimizer/executor" if opt else "dialect family" if fam else "parser/generator/tokenizer/exp core" if core else "other module"
    return f"function body ({where})" if tag in ("lines", "lambda", "lambda+lines") else f"{tag} ({where})"


if __name__ == "__main__":
    os.environ.setdefault("BR_SRC", str(REPO.parent / "sqlglot"))
    main()
