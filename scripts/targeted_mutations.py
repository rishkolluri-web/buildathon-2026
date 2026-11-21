#!/usr/bin/env python3
"""Targeted regression mutations for selector risk areas (Person 2 verification tool).

Writes a stress-style corpus (commits/*.patch + meta.json) whose ground truth is
then produced with `stress_mutate.py truth --out <dir>` (real pytest runs).

Risk areas covered:
  newmod-typing   new module in a package that a test enumerates (pkgutil.iter_modules)
  newmod-plain    new module nothing enumerates or imports
  classobj-read   class attribute read only through a class object (no instance)
  enum-alias      enum member value change that turns it into an alias of another member
  inherited-dict  base-class dict read via self.<ATTR> in subclasses
  classvar-set    class-level set read via Class.<ATTR> from other modules

  python3 scripts/targeted_mutations.py --src ../sqlglot --out ../stress_h3
"""

import argparse
import difflib
import json
from pathlib import Path


def udiff(path, old, new):
    a = [] if old is None else old.splitlines(True)
    b = new.splitlines(True)
    head = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if old is None else "")
    return head + "".join(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{path}", f"b/{path}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    read = lambda p: (src / p).read_text()  # noqa: E731

    def edit(p, old, new):
        text = read(p)
        assert text.count(old) >= 1, (p, old)
        return p, text, text.replace(old, new, 1)

    pg = read("sqlglot/typing/postgres.py")
    muts = {
        # T1: restates an existing postgres entry -> test_typing's redundancy check can fail
        "t1-newmod-typing": ("sqlglot/typing/zz_newdialect.py", None,
                             "from sqlglot.typing.postgres import EXPRESSION_METADATA as _P\n\n"
                             "EXPRESSION_METADATA = {**_P, **{k: dict(v) for k, v in list(_P.items())[:1]}}\n"),
        # T2: plain new module nobody enumerates or imports
        "t2-newmod-plain": ("sqlglot/optimizer/zz_unused_helper.py", None, "def helper():\n    return 1\n"),
        # T3: read only as dialect.parser_class.LOG_DEFAULTS_TO_LN (no parser instance needed)
        "t3-classobj-read": edit("sqlglot/parser.py", "LOG_DEFAULTS_TO_LN: t.ClassVar = False", "LOG_DEFAULTS_TO_LN: t.ClassVar = True"),
        # T4: PREFIX becomes an alias of FAILED
        "t4-enum-alias": edit("sqlglot/trie.py", "    PREFIX = auto()", "    PREFIX = 1"),
        # T5: inherited dict on the base Generator
        "t5-inherited-dict": edit("sqlglot/generator.py", '"SECONDS": "SECOND",', '"SECONDS": "SECONDX",'),
        # T6: class-level set read via exp.DataType.SIGNED_INTEGER_TYPES elsewhere
        "t6-classvar-set": edit("sqlglot/expressions/datatypes.py", "        DType.MEDIUMINT,\n", ""),
    }
    assert pg  # postgres typing module exists (T1 imports it)
    meta = {}
    for mid, (path, old, new) in muts.items():
        (out / "commits" / f"{mid}.patch").write_text(udiff(path, old, new))
        meta[mid] = {"kind": mid.split("-", 1)[1], "file": path}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {len(meta)} targeted mutations to {out}")


if __name__ == "__main__":
    main()
