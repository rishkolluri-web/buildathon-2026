"""Regression guards for three selector risks found while auditing narrow-v3 (71f6133).

Each case is a one-line edit of the pinned sqlglot source (eb11aec), generated here
from BR_SRC so it always applies. The expected tests are ones that really fail on
that edit: observed by applying it to a copy of the pinned source and running the
relevant test files in one pytest process. Only the selector runs here, once, as a
subprocess; no sqlglot tests run.

    BR_SRC=../sqlglot BR_KIT=../blast-radius-sample python3 -m unittest tests.test_selector_risks -v

Optional overrides: SELECTOR (default ./selector.py), BR_MAP (default ./map.json.gz),
TESTS_JSON (default $BR_KIT/sample/tests.json).

Risk 1 (decorator returns before the body) was not confirmed: guards only.
Risk 2 (new class joins a subclass-scan registry) and risk 3 (removed star import)
were real misses on 71f6133 (it selected no tests); module_change now covers them.
"""

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = os.environ.get("BR_SRC")
KIT = os.environ.get("BR_KIT")
MAP = os.environ.get("BR_MAP", str(REPO / "map.json.gz"))
SELECTOR = os.environ.get("SELECTOR", str(REPO / "selector.py"))
TESTS_JSON = os.environ.get("TESTS_JSON", str(Path(KIT) / "sample" / "tests.json") if KIT else None)


def udiff(rel, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
    return f"diff --git a/{rel} b/{rel}\n" + body


def edit(rel, old, new):
    text = (Path(SRC) / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{rel}: anchor not unique"
    return udiff(rel, text, text.replace(old, new, 1))


def add_class_after(rel, anchor_class, lines):
    text = (Path(SRC) / rel).read_text(encoding="utf-8").split("\n")
    i = next(k for k, l in enumerate(text) if re.match(rf"class {anchor_class}\(", l))
    j = next(k for k in range(i + 1, len(text)) if text[k].startswith("class "))
    new = text[:j] + lines + ["", ""] + text[j:]
    return udiff(rel, "\n".join(text), "\n".join(new))


@unittest.skipUnless(SRC and TESTS_JSON, "set BR_SRC (pinned sqlglot) and BR_KIT (sample kit) or TESTS_JSON")
class SelectorRisks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env_py = "sqlglot/executor/env.py"
        cls.patches = {
            # risk 1: decorator wrapper returns before the body (null_if_any)
            "r1_str_position": edit(env_py, '@null_if_any("this", "substr")\ndef str_position',
                                    '@null_if_any("this")\ndef str_position'),
            "r1_substring": edit(env_py, '@null_if_any("this")\ndef substring',
                                 '@null_if_any("this_X")\ndef substring'),
            # risk 2: new class picked up by the subclass scan behind exp.FUNCTION_BY_NAME
            "r2_dynamic_registration": add_class_after(
                "sqlglot/expressions/string.py", "Reverse",
                ["class ReverseText(Expression, Func):", '    _sql_names = ["REVERSE"]']),
            # risk 3: pure removal of a star import that is the only export path
            "r3_builders_star": edit("sqlglot/expressions/__init__.py",
                                     "from sqlglot.expressions.builders import *  # noqa: F401,F403\n", ""),
            # risk 3 control: removal of a named import the module itself reads
            "r3_subclasses": edit("sqlglot/expressions/__init__.py", "from sqlglot.helper import subclasses\n", ""),
        }
        tests = json.loads(Path(TESTS_JSON).read_text())["tests"]
        cls.num = {t["id"]: t["number"] for t in tests}
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "commits").mkdir()
            (data / "tests.json").write_text(Path(TESTS_JSON).read_text())
            for name, patch in cls.patches.items():
                (data / "commits" / f"{name}.patch").write_text(patch)
            env = dict(os.environ, BR_DATA=str(data), BR_SRC=SRC, BR_MAP=MAP)
            proc = subprocess.run([sys.executable, SELECTOR], env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        cls.sel = {}
        for line in proc.stdout.splitlines():
            row = json.loads(line)
            cls.sel[row["commit"]] = {n for w in row["workers"] for n in w}

    def assertSelects(self, patch, *test_ids):
        missing = [t for t in test_ids if self.num[t] not in self.sel[patch]]
        self.assertFalse(missing, f"{patch}: selected {len(self.sel[patch])} tests but not the failing {missing}")

    # risk 1: guards (pass on narrow-v3; the NULL cases share a test with non-NULL cases)
    def test_r1_null_if_any_str_position(self):
        self.assertSelects("r1_str_position", "tests/test_executor.py::TestExecutor::test_scalar_functions")

    def test_r1_null_if_any_substring(self):
        self.assertSelects("r1_substring", "tests/test_executor.py::TestExecutor::test_scalar_functions")

    # risk 2: confirmed miss on narrow-v3 (selects nothing)
    def test_r2_new_class_joins_function_registry(self):
        self.assertSelects("r2_dynamic_registration",
                           "tests/dialects/test_snowflake.py::TestSnowflake::test_reverse",
                           "tests/test_executor.py::TestExecutor::test_scalar_functions")

    # risk 3: confirmed miss on narrow-v3 (selects nothing; test modules fail to import)
    def test_r3_removed_star_import(self):
        self.assertSelects("r3_builders_star",
                           "tests/test_build.py::TestBuild::test_build",
                           "tests/test_expressions.py::TestExprs::test_alias",
                           "tests/test_diff.py::TestDiff::test_cte")

    # risk 3 control: named import removal is already resolved through its readers
    def test_r3_removed_named_import(self):
        self.assertSelects("r3_subclasses", "tests/test_build.py::TestBuild::test_build")


if __name__ == "__main__":
    unittest.main()
