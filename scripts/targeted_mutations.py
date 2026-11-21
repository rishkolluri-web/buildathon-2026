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
    ap.add_argument("--set", default="t", choices=["t", "u", "v"], help="t: first targeted set; u: follow-up set; v: scoped-class-data/new-dialect audit")
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
    if a.set == "u":
        drill = edit("sqlglot/generators/drill.py", 'ARRAY_SIZE_NAME = "REPEATED_COUNT"', 'ARRAY_SIZE_NAME = "REPEATED_COUNT_X"')
        muts = {
            # new dialect module: Dialect._try_load imports sqlglot.dialects.<requested name>
            "u1-newdialect": [("sqlglot/dialects/myqsl.py", None,
                               "from sqlglot.dialects.dialect import Dialect\n\n\nclass Myqsl(Dialect):\n    pass\n")],
            # new optimizer module: sqlglot.optimizer.__getattr__ falls back to importing <name>
            "u2-newoptimizer": [("sqlglot/optimizer/zz_rule.py", None, "def zz_rule(expression):\n    return expression\n")],
            # registration across files: new module with a Func subclass + star import of it
            "u3-registration-multifile": [
                ("sqlglot/expressions/zz_extra.py", None,
                 "from sqlglot.expressions.core import Expression, Func\n\n\n"
                 "class ReverseText(Expression, Func):\n    _sql_names = [\"REVERSE\"]\n"),
                edit("sqlglot/expressions/__init__.py", "from sqlglot.expressions.string import *  # noqa: F401,F403\n",
                     "from sqlglot.expressions.string import *  # noqa: F401,F403\n"
                     "from sqlglot.expressions.zz_extra import *  # noqa: F401,F403\n")],
            # two class-data edits in two files: selection must cover both failure sets
            "u4-multifile-classattr": [
                edit("sqlglot/parser.py", "LOG_DEFAULTS_TO_LN: t.ClassVar = False", "LOG_DEFAULTS_TO_LN: t.ClassVar = True"),
                drill],
            # base-class flag read via self.X, overridden in some dialect subclasses
            "u5-base-flag": [edit("sqlglot/generator.py", "    SUPPORTS_UESCAPE = True\n", "    SUPPORTS_UESCAPE = False\n")],
            # dialect name list consumed by dynamic lookups
            "u6-dialects-list": [edit("sqlglot/dialects/__init__.py", '    "Drill",\n', "")],
            # lazy attribute table: __getattr__ result is cached in globals() after first use
            "u7-lazy-attr": [edit("sqlglot/optimizer/__init__.py",
                                  '"traverse_scope": ("sqlglot.optimizer.scope", "traverse_scope"),',
                                  '"traverse_scope": ("sqlglot.optimizer.scope", "walk_in_scope"),')],
        }
    elif a.set == "v":
        dia = "from sqlglot.dialects.dialect import Dialect\n\n\nclass {}(Dialect):\n    NORMALIZE_FUNCTIONS = False\n"
        muts = {
            # class-object read dialect.parser_class.LOG_DEFAULTS_TO_LN (parser.py:107): most
            # executing tests are outside the scoped selection for these classes
            "v1-dremio-logln": [edit("sqlglot/parsers/dremio.py", "    LOG_DEFAULTS_TO_LN = True\n", "    LOG_DEFAULTS_TO_LN = False\n")],
            "v2-drill-logln": [edit("sqlglot/parsers/drill.py", "    LOG_DEFAULTS_TO_LN = True\n", "    LOG_DEFAULTS_TO_LN = False\n")],
            # generic generator flag (Person 1's f21/f11 risk class; not their exact patches)
            "v3-teradata-tablehints": [edit("sqlglot/generators/teradata.py", "    TABLE_HINTS = False\n", "    TABLE_HINTS = True\n")],
            "v4-drill-tablehints": [edit("sqlglot/generators/drill.py", "    TABLE_HINTS = False\n", "    TABLE_HINTS = True\n")],
            # generic dialect-level setting
            "v5-drill-nullordering": [edit("sqlglot/dialects/drill.py", '    NULL_ORDERING = "nulls_are_last"\n', '    NULL_ORDERING = "nulls_are_large"\n')],
            # generic expression flag read by hashing code
            "v6-hash-raw-args": [edit("sqlglot/expressions/query.py", "    _hash_raw_args = True\n", "    _hash_raw_args = False\n")],
            # new dialect modules: second probed unknown name, and a name a test defines itself
            "v7-newdialect-unknownname": [("sqlglot/dialects/asdfjasodiufjsd.py", None, dia.format("Asdfjasodiufjsd"))],
            "v8-newdialect-testdefined": [("sqlglot/dialects/mydialect.py", None, dia.format("Mydialect"))],
        }
    else:
        muts = {k: [v] for k, v in muts.items()}
    meta = {}
    for mid, files in muts.items():
        (out / "commits" / f"{mid}.patch").write_text("".join(udiff(*f) for f in files))
        meta[mid] = {"kind": mid.split("-", 1)[1], "file": ",".join(f[0] for f in files)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {len(meta)} targeted mutations to {out}")


if __name__ == "__main__":
    main()
