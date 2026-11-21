#!/usr/bin/env python3
"""Corpus for the shared-container guard (selector.shared_mut): class-setting edits that steer the dialect
metaclass's import-time `gen_cls.TRANSFORMS.pop(...)` (dialects/dialect.py), which mutates whatever
TRANSFORMS dict the generator class resolves to -- possibly one inherited from (and shared with) a base
generator -- plus a negative control the guard must not catch.

  python3 scripts/sharedmut_mutations.py --src ../sqlglot --out ../stress_sm
then: scripts/stress_mutate.py truth --src ../sqlglot --out ../stress_sm --python ...
"""

import argparse
import difflib
import json
from pathlib import Path


def udiff(path, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"a/{path}", f"b/{path}"))
    assert body, f"no change in {path}"
    return f"diff --git a/{path} b/{path}\n" + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    read = lambda p: (src / p).read_text()  # noqa: E731

    def edit(path, old, new):
        text = read(path)
        assert text.count(old) == 1, (path, old)
        return udiff(path, text, text.replace(old, new, 1))

    muts = {
        # subclass inheriting TrinoGenerator.TRANSFORMS narrows its supported parts: pops hit the shared dict
        "sm1-dune-jsonparts-inherited": ("sqlglot/generators/dune.py", edit(
            "sqlglot/generators/dune.py", "    TRY_SUPPORTED = False\n",
            "    TRY_SUPPORTED = False\n    SUPPORTED_JSON_PATH_PARTS = {exp.JSONPathKey, exp.JSONPathRoot}\n")),
        # generator owning its TRANSFORMS drops one supported part
        "sm2-sqlite-jsonparts-owned": ("sqlglot/generators/sqlite.py", edit(
            "sqlglot/generators/sqlite.py", "        exp.JSONPathRoot,\n        exp.JSONPathSubscript,\n",
            "        exp.JSONPathRoot,\n")),
        # generator starts owning a TRANSFORMS copy (container binding added) with no supported parts
        "sm3-solr-own-transforms": ("sqlglot/generators/solr.py", edit(
            "sqlglot/generators/solr.py", "class SolrGenerator(generator.Generator):\n",
            "class SolrGenerator(generator.Generator):\n    TRANSFORMS = {**generator.Generator.TRANSFORMS}\n"
            "    SUPPORTED_JSON_PATH_PARTS = set()\n")),
        # negative control: unrelated single-token class-setting flip (guard must not fire)
        "sm4-dune-try-supported-flip": ("sqlglot/generators/dune.py", edit(
            "sqlglot/generators/dune.py", "    TRY_SUPPORTED = False\n", "    TRY_SUPPORTED = True\n")),
    }
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    for k, (path, patch) in muts.items():
        (out / "commits" / f"{k}.patch").write_text(patch)
        meta[k] = {"kind": "shared-container", "file": path}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"{len(muts)} mutants -> {out}")


if __name__ == "__main__":
    main()
