#!/usr/bin/env python3
"""Record which sqlglot lines execute just by importing every sqlglot module.

Lines in function bodies that run at import time (metaclass __new__,
__init_subclass__, factories called from class bodies, ...) shape state that
every later test depends on, but per-test coverage credits them to nobody (or
to whichever test imported first). The selector treats a change on such a line
like a module-level change.

  python mapgen/import_lines.py <sqlglot checkout> <out.json>
"""

import importlib
import json
import os
import pkgutil
import sys

import coverage

root = os.path.realpath(sys.argv[1])
sys.path.insert(0, root)
cov = coverage.Coverage(source=[os.path.join(root, "sqlglot")], data_file=None)
cov.start()
import sqlglot  # noqa: E402

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
# every dialect class is created by its module import above; also touch the registry
try:
    sqlglot.Dialect.classes  # noqa: B018
except Exception:
    pass
cov.stop()
data = cov.get_data()
out = {}
for f in data.measured_files():
    r = os.path.realpath(f)
    if r.startswith(root + os.sep):
        out[r[len(root) + 1:]] = sorted(data.lines(f) or [])
json.dump(out, open(sys.argv[2], "w"))
print(f"import-time lines: {sum(len(v) for v in out.values())} in {len(out)} files")
