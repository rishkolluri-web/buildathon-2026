"""Robustness checks for selector.py on change kinds the sample never shows.

Patches are generated from the real pinned sources, so they apply cleanly.
Needs the pinned sources and map; skipped otherwise:
  BR_SRC=../sqlglot BR_KIT=../blast-radius-sample python3 -m unittest tests.test_selector_edgecases -v
"""

import difflib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = os.environ.get("BR_SRC")
KIT = os.environ.get("BR_KIT")
sys.path.insert(0, str(REPO / "tests"))
from protocol_check import check_output, list_commit_ids, load_test_numbers  # noqa: E402


def udiff(path, old, new):
    a = [] if old is None else old.splitlines(keepends=True)
    b = [] if new is None else new.splitlines(keepends=True)
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    if new is None:
        head += "deleted file mode 100644\n"
    body = "".join(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{path}",
                                        "/dev/null" if new is None else f"b/{path}"))
    return head + body


@unittest.skipUnless(SRC and KIT, "set BR_SRC (pinned sqlglot) and BR_KIT (sample kit)")
class EdgeCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = Path(SRC)
        read = lambda p: (src / p).read_text()  # noqa: E731
        scope = read("sqlglot/optimizer/scope.py")
        tsql = read("sqlglot/dialects/tsql.py")
        helper = read("sqlglot/helper.py")
        transpile = read("tests/test_transpile.py")
        helpers = read("tests/helpers.py")
        cls.patches = {
            "synt": udiff("sqlglot/optimizer/scope.py", scope, scope.replace("def traverse_scope(", "def traverse_scope((", 1)),
            "cmnt": udiff("sqlglot/helper.py", helper, helper.replace("\ndef ", "\n# just a note\ndef ", 1)),
            "newf": udiff("sqlglot/brand_new.py", None, "X = 1\n"),
            "delf": udiff("sqlglot/dialects/tsql.py", tsql, None),
            "tfil": udiff("tests/test_transpile.py", transpile,
                          transpile.replace("def test_weird_chars(self):\n", "def test_weird_chars(self):\n        self.assertTrue(False)\n", 1)),
            "hlpr": udiff("tests/helpers.py", helpers, helpers.replace("def load_sql_fixtures(filename):\n", "def load_sql_fixtures(filename):\n    filename = filename.strip()\n", 1)),
            "mcon": udiff("sqlglot/helper.py", helper, helper.replace("\nlogger = ", "\nUNUSED_CONST = 3\nlogger = ", 1)),
            "rdme": udiff("README.md", "a\n", "b\n"),
            "conf": udiff("pyproject.toml", "a\n", "b\n"),
            "junk": "this is not a diff\n@@ nonsense @@\n+++\n",
            "empt": "",
        }
        for k, v in cls.patches.items():
            if k not in ("junk", "empt", "rdme", "conf"):
                assert v.count("@@") >= 2, f"patch {k} did not change anything"
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "commits").mkdir()
        (data / "tests.json").write_text((Path(KIT) / "sample" / "tests.json").read_text())
        for k, v in cls.patches.items():
            (data / "commits" / f"{k}.patch").write_text(v)
        env = dict(os.environ, BR_DATA=str(data), BR_SRC=SRC, BR_MAP=str(REPO / "map.json.gz"))
        proc = subprocess.run([sys.executable, str(REPO / "selector.py")], env=env, capture_output=True)
        cls.stderr = proc.stderr.decode()
        assert proc.returncode == 0, cls.stderr
        cls.raw = proc.stdout
        cls.errors = check_output(proc.stdout, list_commit_ids(data / "commits"), load_test_numbers(data / "tests.json"))
        cls.tests = json.loads((data / "tests.json").read_text())["tests"]
        cls.sel = {}
        for line in proc.stdout.decode().splitlines():
            row = json.loads(line)
            cls.sel[row["commit"]] = {n for w in row["workers"] for n in w}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def num(self, test_id):
        return next(t["number"] for t in self.tests if t["id"] == test_id)

    def test_protocol_valid(self):
        self.assertEqual(self.errors, [])

    def test_syntax_error_selects_everything(self):
        self.assertEqual(len(self.sel["synt"]), len(self.tests))

    def test_deleted_module_selects_everything(self):
        self.assertEqual(len(self.sel["delf"]), len(self.tests))

    def test_config_change_selects_everything(self):
        self.assertEqual(len(self.sel["conf"]), len(self.tests))

    def test_module_level_constant_selects_everything(self):
        self.assertEqual(len(self.sel["mcon"]), len(self.tests))

    def test_comment_only_selects_nothing(self):
        self.assertEqual(self.sel["cmnt"], set())

    def test_new_unimported_file_selects_nothing(self):
        self.assertEqual(self.sel["newf"], set())

    def test_readme_selects_nothing(self):
        self.assertEqual(self.sel["rdme"], set())

    def test_test_file_edit_selects_that_test(self):
        self.assertIn(self.num("tests/test_transpile.py::TestTranspile::test_weird_chars"), self.sel["tfil"])

    def test_helper_edit_selects_its_users(self):
        self.assertTrue(self.sel["hlpr"], "tests/helpers.py change must select fixture users")

    def test_garbage_and_empty_patches_do_not_crash(self):
        self.assertIn("junk", self.sel)
        self.assertIn("empt", self.sel)


if __name__ == "__main__":
    unittest.main()
