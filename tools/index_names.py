"""Add a name-reference index to the dependency map.

Usage (sqlglot checkout as the working directory):

    python /path/to/tools/index_names.py /path/to/depmap.json.gz

For every code object a test entered, compile the pinned source, read the
code object's co_names (the global and attribute names it uses), and store
name -> [code key index]. The selector uses it for definition-time changes
(class attributes, module constants) that no test "enters" as a function.
"""

from __future__ import annotations

import gzip
import json
import os
import sys


def walk_code(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            yield from walk_code(const)


def main() -> None:
    path = sys.argv[1]
    data = json.load(gzip.open(path, "rt"))
    keys = sorted(data["code"])
    index_of = {key: i for i, key in enumerate(keys)}
    by_file: dict[str, list[str]] = {}
    for key in keys:
        by_file.setdefault(key.split("::", 1)[0], []).append(key)

    names: dict[str, set[int]] = {}
    for relpath in by_file:
        source = open(relpath, encoding="utf-8").read()
        module = compile(source, relpath, "exec", dont_inherit=True)
        for code in walk_code(module):
            key = f"{relpath}::{code.co_qualname}::{code.co_firstlineno}"
            if key in index_of:
                for name in code.co_names:
                    names.setdefault(name, set()).add(index_of[key])

    data["code_keys"] = keys
    data["code"] = [data["code"][key] for key in keys]
    data["names"] = {name: sorted(v) for name, v in sorted(names.items())}
    with gzip.open(path, "wt") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"{len(keys)} code objects, {len(names)} names", file=sys.stderr)


if __name__ == "__main__":
    main()
