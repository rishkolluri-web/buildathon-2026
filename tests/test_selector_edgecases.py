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
        errors = read("sqlglot/errors.py")
        ddl = read("sqlglot/expressions/ddl.py")
        functions = read("sqlglot/expressions/functions.py")
        cls.patches = {
            "synt": udiff("sqlglot/optimizer/scope.py", scope, scope.replace("def traverse_scope(", "def traverse_scope((", 1)),
            "cmnt": udiff("sqlglot/helper.py", helper, helper.replace("\ndef ", "\n# just a note\ndef ", 1)),
            "newf": udiff("sqlglot/brand_new.py", None, "X = 1\n"),
            "ndia": udiff("sqlglot/dialects/myqsl.py", None,
                          "from sqlglot.dialects.dialect import Dialect\n\nclass Myqsl(Dialect):\n    pass\n"),
            # cross-dialect: parsers/presto.py reads Dialect["teradata"].TIME_MAPPING at runtime
            "tdtm": udiff("sqlglot/dialects/teradata.py", read("sqlglot/dialects/teradata.py"),
                          read("sqlglot/dialects/teradata.py").replace('"DD": "%d",', '"DD": "%e",', 1)),
            # cross-dialect: dialects/presto.py copies MySQL.TIME_MAPPING into its class at import
            "mytm": udiff("sqlglot/dialects/mysql.py", read("sqlglot/dialects/mysql.py"),
                          read("sqlglot/dialects/mysql.py").replace('"%e": "%-d",', '"%e": "%d",', 1)),
            # cross-dialect: generators/presto.py reads Hive.INVERSE_TIME_MAPPING (derived at import)
            "hvtm": udiff("sqlglot/dialects/hive.py", read("sqlglot/dialects/hive.py"),
                          read("sqlglot/dialects/hive.py").replace('"d": "%-d",', '"d": "%d",', 1)),
            # identity/equality swaps (eqsites.json: interchangeable on every traced execution)
            "eqok": udiff("sqlglot/parser.py", read("sqlglot/parser.py"),
                          read("sqlglot/parser.py").replace("        if self.error_level == ErrorLevel.WARN:\n",
                                                            "        if self.error_level is ErrorLevel.WARN:\n", 1)),
            "eqbad": udiff("sqlglot/parser.py", read("sqlglot/parser.py"),
                           read("sqlglot/parser.py").replace("        if index != self._index:\n",
                                                             "        if index is not self._index:\n", 1)),
            "eqplus": udiff("sqlglot/parser.py", read("sqlglot/parser.py"),
                            read("sqlglot/parser.py").replace("        if self.error_level == ErrorLevel.WARN:\n",
                                                              "        if self.error_level is ErrorLevel.WARN:\n", 1)
                            .replace("        if index != self._index:\n", "        if index != self._index + 0:\n", 1)),
            "eqtwo": udiff("sqlglot/parser.py", read("sqlglot/parser.py"),
                           read("sqlglot/parser.py").replace("        if self.error_level == ErrorLevel.WARN:\n",
                                                             "        if self.error_level is ErrorLevel.WARN:\n", 1))
                     + udiff("sqlglot/helper.py", helper, helper.replace("\ndef ", "\ndef _unused_zz():\n    return 1\n\n\ndef ", 1)),
            # athena settings: pushdown_predicates only isinstance-checks Athena/Presto
            "athk": udiff("sqlglot/parsers/athena.py", read("sqlglot/parsers/athena.py"),
                          read("sqlglot/parsers/athena.py").replace("{*TrinoParser.DDL_SELECT_TOKENS, TokenType.USING}",
                                                                    "{*TrinoParser.DDL_SELECT_TOKENS}", 1)),
            "nopt": udiff("sqlglot/optimizer/zz_rule.py", None, "def zz_rule(expression):\n    return expression\n"),
            "delf": udiff("sqlglot/dialects/tsql.py", tsql, None),
            "tfil": udiff("tests/test_transpile.py", transpile,
                          transpile.replace("def test_weird_chars(self):\n", "def test_weird_chars(self):\n        self.assertTrue(False)\n", 1)),
            "hlpr": udiff("tests/helpers.py", helpers, helpers.replace("def load_sql_fixtures(filename):\n", "def load_sql_fixtures(filename):\n    filename = filename.strip()\n", 1)),
            "mcon": udiff("sqlglot/helper.py", helper, helper.replace("\nlogger = ", "\nUNUSED_CONST = 3\nlogger = ", 1)),
            "rdme": udiff("README.md", "a\n", "b\n"),
            "ncls": udiff("sqlglot/expressions/ddl.py", ddl, ddl.rstrip("\n") + "\n\n\nclass BrandNewProperty(Expression):\n    arg_types = {\"this\": True}\n"),
            "star": udiff("sqlglot/expressions/functions.py", functions,
                          functions.replace("from sqlglot.expressions.math import *  # noqa: F401,F403\n", "", 1)),
            "mrd": udiff("sqlglot/errors.py", errors, errors.replace("ERROR_MESSAGE_CONTEXT_DEFAULT = 100", "ERROR_MESSAGE_CONTEXT_DEFAULT = 101", 1)),
            "fxnw": udiff("tests/fixtures/optimizer/brand_new_case.sql", None, "SELECT 1;\nSELECT 1;\n"),
            "sqgc": udiff("sqlglotc/setup.py", "a = 1\n", "a = 2\n"),
            "conf": udiff("pyproject.toml", "a\n", "b\n"),
            "cftn": udiff("tests/dialects/conftest.py", None, "import pytest\n"),
            "junk": "this is not a diff\n@@ nonsense @@\n+++\n",
            "empt": "",
        }
        for k, v in cls.patches.items():
            if k not in ("junk", "empt", "rdme", "conf", "fxnw", "sqgc", "cftn"):
                assert v.count("@@") >= 2, f"patch {k} did not change anything"
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "commits").mkdir()
        (data / "tests.json").write_text((Path(KIT) / "sample" / "tests.json").read_text())
        for k, v in cls.patches.items():
            (data / "commits" / f"{k}.patch").write_text(v)
        env = dict(os.environ, BR_DATA=str(data), BR_SRC=SRC, BR_MAP=str(REPO / "map.json.gz"),
                   BR_IDX=str(data / "none.pkl"))
        proc = subprocess.run([sys.executable, str(REPO / "selector.py")], env=env, capture_output=True)
        cls.stderr = proc.stderr.decode()
        assert proc.returncode == 0, cls.stderr
        cls.raw = proc.stdout
        # same patches with the build-time static index (as in the image) and with a corrupt one
        idx = data / "static_index.pkl"
        subprocess.run([sys.executable, str(REPO / "selector.py"), "--build-index", str(idx)],
                       env=env, capture_output=True, check=True)
        cls.raw_idx = subprocess.run([sys.executable, str(REPO / "selector.py")], env=dict(env, BR_IDX=str(idx)),
                                     capture_output=True, check=True).stdout
        (data / "bad.pkl").write_bytes(b"not a pickle")
        cls.raw_bad = subprocess.run([sys.executable, str(REPO / "selector.py")], env=dict(env, BR_IDX=str(data / "bad.pkl")),
                                     capture_output=True, check=True).stdout
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

    def test_new_module_level_class_selects_everything(self):
        # regression (Person 1): class creation registers subclasses; 0 tests was a miss
        self.assertEqual(len(self.sel["ncls"]), len(self.tests))

    def test_removed_star_import_selects_everything(self):
        # regression (Person 1): removing `from x import *` broke imports with 0 tests selected
        self.assertEqual(len(self.sel["star"]), len(self.tests))

    def test_new_unread_module_constant_selects_nothing(self):
        # nothing can read a brand-new name, so it cannot change any test's behaviour
        self.assertEqual(self.sel["mcon"], set())

    def test_read_module_constant_selects_its_readers(self):
        sel = self.sel["mrd"]
        self.assertTrue(sel and len(sel) < len(self.tests), f"{len(sel)} selected")
        # highlight_sql's default argument reads the constant; its callers must be selected
        self.assertIn(self.num("tests/test_errors.py::TestErrors::test_highlight_sql_single_character"), sel)

    def test_comment_only_selects_nothing(self):
        self.assertEqual(self.sel["cmnt"], set())

    def test_new_module_selects_package_enumerators_only(self):
        # nothing imports a new module by name, but tests that enumerate packages
        # (tests/test_typing.py uses pkgutil.iter_modules) will import it
        typing_tests = {t["number"] for t in self.tests if t["file"] == "tests/test_typing.py"}
        self.assertTrue(typing_tests and typing_tests <= self.sel["newf"])
        self.assertLess(len(self.sel["newf"]), len(self.tests))

    def test_new_dialect_selects_unknown_name_checks(self):
        # Dialect._try_load imports a module by the requested name; this addition
        # breaks test_get_or_raise's expectation that "myqsl" is unknown.
        self.assertIn(self.num("tests/dialects/test_dialect.py::TestDialect::test_get_or_raise"),
                      self.sel["ndia"])

    def test_new_module_in_name_loaded_package_selects_probing_tests(self):
        # sqlglot.optimizer.__getattr__ imports f"{__name__}.{name}" and Dialect._try_load
        # imports f"sqlglot.dialects.{key}": a new submodule changes what a probed name
        # resolves to. The tests that execute those import calls must be selected.
        sys.path.insert(0, str(REPO))
        import selector
        sel = selector.Selector(selector.Map(selector.MAP, self.tests))
        for case, pkg in (("nopt", "sqlglot.optimizer"), ("ndia", "sqlglot.dialects")):
            sites = sel.name_loaded_packages()[pkg]
            probing = set()
            for rel, ln in sites:
                probing |= sel.m.nums(sel.lines_bits(rel, [ln]))
            self.assertTrue(probing and probing <= self.sel[case], case)
            self.assertLess(len(self.sel[case]), len(self.tests), case)

    def test_cross_dialect_time_mapping_selects_consumer(self):
        # Teradata's TIME_MAPPING is read through the class object by Presto's TO_CHAR
        # builder; the Presto test fails on this edit (verified on the pinned source)
        self.assertIn(self.num("tests/dialects/test_presto.py::TestPresto::test_to_char"), self.sel["tdtm"])
        self.assertLess(len(self.sel["tdtm"]), len(self.tests))

    def test_cross_dialect_import_time_copy_selects_consumers(self):
        # full-suite truth for this edit: 16 failing tests; c003640 and b2c241e missed #434
        for t in ("tests/dialects/test_hive.py::TestHive::test_time",
                  "tests/dialects/test_presto.py::TestPresto::test_to_char",
                  "tests/dialects/test_presto.py::TestPresto::test_time",
                  "tests/dialects/test_singlestore.py::TestSingleStore::test_time_formatting",
                  "tests/dialects/test_tsql.py::TestTSQL::test_convert"):
            self.assertIn(self.num(t), self.sel["mytm"], t)

    def test_cross_dialect_derived_mapping_selects_consumers(self):
        # full-suite truth for this edit: 9 failing tests (INVERSE_TIME_MAPPING is derived)
        for t in ("tests/dialects/test_presto.py::TestPresto::test_time",
                  "tests/dialects/test_spark.py::TestSpark::test_spark",
                  "tests/dialects/test_tsql.py::TestTSQL::test_format"):
            self.assertIn(self.num(t), self.sel["hvtm"], t)

    def test_family_dependents_found_in_source(self):
        sys.path.insert(0, str(REPO))
        import selector
        refs = selector.Selector(selector.Map(selector.MAP, self.tests)).family_dependents()
        self.assertIn("sqlglot/parsers/presto.py", {r[0] for r in refs["teradata"]})   # runtime lookup
        self.assertIn("presto", {r[2] for r in refs["mysql"]})                         # import-time copy
        self.assertIn("presto", {r[2] for r in refs["hive"]})                          # generator reads Hive

    def test_identity_equality_swap_at_interchangeable_site_selects_nothing(self):
        self.assertEqual(self.sel["eqok"], set())

    def test_identity_equality_swap_elsewhere_keeps_tests(self):
        # ints are not interchangeable (equal ints can be distinct objects)
        self.assertGreater(len(self.sel["eqbad"]), 0)
        # a safe swap plus any other edit, or a second changed file: normal selection
        self.assertGreater(len(self.sel["eqplus"]), 0)
        self.assertGreater(len(self.sel["eqtwo"]), 0)

    def test_isinstance_only_import_is_not_a_data_dependency(self):
        # optimizer/pushdown_predicates.py only does isinstance(dialect, (Athena, Presto)):
        # an Athena setting must not pull in every test that runs pushdown_predicates
        sys.path.insert(0, str(REPO))
        import selector
        sel = selector.Selector(selector.Map(selector.MAP, self.tests))
        push = sel.m.nums(sel.lines_bits("sqlglot/optimizer/pushdown_predicates.py", [42]))
        self.assertFalse(push <= self.sel["athk"])

    def test_nested_conftest_selects_everything(self):
        self.assertEqual(len(self.sel["cftn"]), len(self.tests))

    def test_readme_selects_nothing(self):
        self.assertEqual(self.sel["rdme"], set())

    def test_test_file_edit_selects_that_test(self):
        self.assertIn(self.num("tests/test_transpile.py::TestTranspile::test_weird_chars"), self.sel["tfil"])

    def test_helper_edit_selects_its_users(self):
        self.assertTrue(self.sel["hlpr"], "tests/helpers.py change must select fixture users")

    def test_unopened_fixture_selects_sibling_readers(self):
        self.assertIn(self.num("tests/test_optimizer.py::TestOptimizer::test_simplify"), self.sel["fxnw"])

    def test_sqlglotc_change_selects_its_tests(self):
        nums = {t["number"] for t in self.tests if t["file"] == "tests/test_sqlglotc.py"}
        self.assertTrue(nums and nums <= self.sel["sqgc"])

    def test_static_index_changes_nothing(self):
        self.assertEqual(self.raw_idx, self.raw)  # build-time index == lazy computation
        self.assertEqual(self.raw_bad, self.raw)  # corrupt index -> ignored

    def test_garbage_and_empty_patches_do_not_crash(self):
        self.assertIn("junk", self.sel)
        self.assertIn("empt", self.sel)


class FillIdle(unittest.TestCase):
    def test_never_raises_busiest_worker(self):
        sys.path.insert(0, str(REPO))
        import random

        from selector import fill_idle, pack

        rng = random.Random(1)
        for _ in range(200):
            ms = {i: rng.choice([0, 0, 1, 3, 10, 50, 400, 2000]) for i in range(60)}
            chosen = set(rng.sample(range(60), rng.randint(1, 20)))
            workers, cost = pack(chosen, ms)
            before = max(sum(ms[n] for n in w) for w in workers)
            fill_idle(workers, ms, set(ms) - chosen)
            flat = [n for w in workers for n in w]
            self.assertEqual(len(flat), len(set(flat)))
            self.assertEqual(max(sum(ms[n] for n in w) for w in workers), before)
            self.assertTrue(chosen <= set(flat))


if __name__ == "__main__":
    unittest.main()
