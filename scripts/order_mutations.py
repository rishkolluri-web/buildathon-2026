#!/usr/bin/env python3
"""Order-dependence corpus: single-token mutants on code whose shared-run coverage differs between map builds
(scripts/audit_trace_repro.py): the lazy dialect-registration path of Dialect._try_load / Dialect.get. Which test
executes it depends on which test first asks for a dialect whose module is already imported.

  python3 scripts/order_mutations.py --src ../sqlglot --out ../stress_ord
"""

import argparse
import difflib
import json
from pathlib import Path

PATH = "sqlglot/dialects/dialect.py"
EDITS = {
    # line 249: register-if-missing guard (dropped `not`)
    "o1-tryload-guard-not": ("            if key not in cls._classes:\n                for attr_name in dir(module):",
                             "            if key in cls._classes:\n                for attr_name in dir(module):"),
    # line 252-254: class match (== -> !=)
    "o2-tryload-name-ne": ("                        and attr.__name__.lower() == key\n",
                           "                        and attr.__name__.lower() != key\n"),
    # line 253: issubclass check (and -> or)
    "o3-tryload-and-or": ("                        isinstance(attr, type)\n                        and issubclass(attr, Dialect)",
                          "                        isinstance(attr, type)\n                        or issubclass(attr, Dialect)"),
    # line 302: get() load guard (dropped `not`)
    "o4-get-guard-not": ("        if key not in cls._classes:\n            cls._try_load(key)\n\n        return cls._classes.get(key, default)",
                         "        if key in cls._classes:\n            cls._try_load(key)\n\n        return cls._classes.get(key, default)"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    text = (src / PATH).read_text()
    (out / "commits").mkdir(parents=True, exist_ok=True)
    meta = {}
    for k, (old, new) in EDITS.items():
        assert text.count(old) == 1, k
        body = "".join(difflib.unified_diff(text.splitlines(True), text.replace(old, new, 1).splitlines(True), f"a/{PATH}", f"b/{PATH}"))
        (out / "commits" / f"{k}.patch").write_text(f"diff --git a/{PATH} b/{PATH}\n" + body)
        meta[k] = {"kind": "order-dependent", "file": PATH}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"{len(meta)} mutants -> {out}")


if __name__ == "__main__":
    main()
