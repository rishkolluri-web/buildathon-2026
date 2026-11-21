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
            # trimmed ALL (heavy.json): ALL fallbacks in modules some slow tests never import
            "hmy": udiff("sqlglot/parsers/mysql.py", read("sqlglot/parsers/mysql.py"),
                         read("sqlglot/parsers/mysql.py").replace("            options.append(opt)\n", "            options.append(opt) )\n", 1)),
            "hsp": udiff("sqlglot/parsers/spark2.py", read("sqlglot/parsers/spark2.py"),
                         read("sqlglot/parsers/spark2.py").replace("seq_get(args, 0), to=exp.DataType", "seq_get(args, 1), to=exp.DataType", 1)),
            # import-time metaclass edit redirecting a mutation to a shared object (stress_sa sa1) vs a
            # value-only flip of the same block's own-class store (sa3): full-suite truth for both
            "imut": udiff("sqlglot/dialects/dialect.py", read("sqlglot/dialects/dialect.py"),
                          read("sqlglot/dialects/dialect.py").replace("gen_cls.TRANSFORMS.pop(part, None)", "Generator.TRANSFORMS.pop(part, None)", 1)),
            "ival": udiff("sqlglot/dialects/dialect.py", read("sqlglot/dialects/dialect.py"),
                          read("sqlglot/dialects/dialect.py").replace("klass.INITCAP_SUPPORTS_CUSTOM_DELIMITERS = False", "klass.INITCAP_SUPPORTS_CUSTOM_DELIMITERS = True", 1)),
            # class settings steering the metaclass's import-time TRANSFORMS.pop on a possibly inherited dict
            # (stress_sa sa2, stress_sm sm1) vs an unrelated flip on the same class (stress_sm sm4)
            "shsolr": udiff("sqlglot/generators/solr.py", read("sqlglot/generators/solr.py"),
                            read("sqlglot/generators/solr.py").replace("class SolrGenerator(generator.Generator):\n",
                                                                       "class SolrGenerator(generator.Generator):\n    SUPPORTED_JSON_PATH_PARTS = set()\n", 1)),
            "shdune": udiff("sqlglot/generators/dune.py", read("sqlglot/generators/dune.py"),
                            read("sqlglot/generators/dune.py").replace("    TRY_SUPPORTED = False\n",
                                                                       "    TRY_SUPPORTED = False\n    SUPPORTED_JSON_PATH_PARTS = {exp.JSONPathKey, exp.JSONPathRoot}\n", 1)),
            "shneg": udiff("sqlglot/generators/dune.py", read("sqlglot/generators/dune.py"),
                           read("sqlglot/generators/dune.py").replace("    TRY_SUPPORTED = False\n", "    TRY_SUPPORTED = True\n", 1)),
            "hmut": udiff("sqlglot/parsers/spark2.py", read("sqlglot/parsers/spark2.py"),
                          read("sqlglot/parsers/spark2.py").replace("seq_get(args, 0), to=exp.DataType", "seq_get(args, 1), to=exp.DataType", 1)
                          .replace("from sqlglot.parsers.hive import HiveParser\n", "from sqlglot.parsers.hive import HiveParser\n\nexp.Select.EXTRA = 1\n", 1)),
            "hdk": udiff("sqlglot/parsers/spark2.py", read("sqlglot/parsers/spark2.py"),
                         read("sqlglot/parsers/spark2.py").replace("seq_get(args, 0), to=exp.DataType", "seq_get(args, 1), to=exp.DataType", 1)
                         .replace("from sqlglot.parsers.hive import HiveParser\n", "from sqlglot.parsers.hive import HiveParser\n\n\nclass Dialect_:\n    pass\n", 1)),
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
            # value trace (vmap.json.gz): class dict entry values / relational operators
            "dval": udiff("sqlglot/dialects/snowflake.py", read("sqlglot/dialects/snowflake.py"),
                          read("sqlglot/dialects/snowflake.py").replace('"u": (16, 4, 4, 0xFFFF)', '"u": (17, 4, 4, 0xFFFF)', 1)),
            "dkey": udiff("sqlglot/dialects/snowflake.py", read("sqlglot/dialects/snowflake.py"),
                          read("sqlglot/dialects/snowflake.py").replace('"u": (16, 4, 4, 0xFFFF)', '"v": (16, 4, 4, 0xFFFF)', 1)),
            "dfac": udiff("sqlglot/parsers/singlestore.py", read("sqlglot/parsers/singlestore.py"),
                          read("sqlglot/parsers/singlestore.py").replace('"GROUPS FOR USER": _show_parser("GROUPS FOR USER", target=True)',
                                                                         '"GROUPS FOR USER": _show_parser("GROUPS FOR USER", target=False)', 1)),
            "dcall": udiff("sqlglot/parsers/singlestore.py", read("sqlglot/parsers/singlestore.py"),
                           read("sqlglot/parsers/singlestore.py").replace('"GROUPS FOR USER": _show_parser("GROUPS FOR USER", target=True)',
                                                                          '"GROUPS FOR USER": _show_parser("GROUPS FOR USER", target=bool(1))', 1)),
            "oplt": udiff("sqlglot/generators/clickhouse.py", read("sqlglot/generators/clickhouse.py"),
                          read("sqlglot/generators/clickhouse.py").replace("version < (23, 12) and unit", "version <= (23, 12) and unit", 1)),
            "opmix": udiff("sqlglot/generators/clickhouse.py", read("sqlglot/generators/clickhouse.py"),
                           read("sqlglot/generators/clickhouse.py").replace("version < (23, 12) and unit", "version <= (23, 12) and unit", 1)
                           .replace('return self.func("dateTrunc", unit,', 'return self.func("dateTrunX", unit,', 1)),
            "opeq": udiff("sqlglot/parsers/dremio.py", read("sqlglot/parsers/dremio.py"),
                          read("sqlglot/parsers/dremio.py").replace("        if len(args) == 2:\n", "        if len(args) != 2:\n", 1)),
            # test-module dependencies: a test module imported by other test modules
            "tdep": udiff("tests/dialects/test_dialect.py", read("tests/dialects/test_dialect.py"),
                          read("tests/dialects/test_dialect.py").replace(
                              "_PARSER_IS_COMPILED = is_compiled(_base_module)\n", "_PARSER_IS_COMPILED = is_compiled(_base_modulex)\n", 1)),
            "thlp": udiff("tests/helpers.py", helpers, helpers.replace(
                '    return not getattr(module, "__file__", ".py").endswith(".py")',
                '    return getattr(module, "__file__", ".py").endswith(".py")', 1)),
            "tloc": udiff("tests/test_parser.py", read("tests/test_parser.py"), read("tests/test_parser.py").replace(
                'parse_one("SELECT * FROM (SELECT 1) UNION SELECT 2"), exp.Union)',
                'parse_one("SELECT * FROM (SELECT 1) UNION SELECT 2"), exp.Intersect)', 1)),
            # comparison-constant rule (value trace of single-constant comparisons)
            "cvok": udiff("sqlglot/errors.py", errors, "\n".join(
                l.replace("> 0", "> 1", 1) if i == 169 and "> 0" in l else l for i, l in enumerate(errors.split("\n")))),
            "cvmix": udiff("sqlglot/errors.py", errors, "\n".join(
                l.replace("> 0", "> 1", 1) if i == 169 and "> 0" in l else l for i, l in enumerate(errors.split("\n")))
                .replace("ERROR_MESSAGE_CONTEXT_DEFAULT = 100", "ERROR_MESSAGE_CONTEXT_DEFAULT = 101", 1)),
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
            # plain unified diff (no `diff --git` header) and a patch whose context does not match the pinned file
            "pldf": "".join(difflib.unified_diff(scope.splitlines(keepends=True),
                                                 scope.replace("def traverse_scope(", "def traverse_scope((", 1).splitlines(keepends=True),
                                                 "a/sqlglot/optimizer/scope.py", "b/sqlglot/optimizer/scope.py")),
            "stal": udiff("sqlglot/helper.py", helper.replace("\nlogger = ", "\nlogger  = ", 1),
                          helper.replace("\nlogger = ", "\nUNUSED_CONST = 3\nlogger  = ", 1)),
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

    def test_unparseable_or_stale_patches_fail_closed(self):
        self.assertEqual(len(self.sel["pldf"]), len(self.tests))  # was 0 tests before v19
        self.assertEqual(len(self.sel["stal"]), len(self.tests))  # context not in the pinned file
        self.assertEqual(self.sel["empt"], set())

    def test_syntax_error_selects_everything(self):
        self.assertEqual(len(self.sel["synt"]), len(self.tests))

    def test_trimmed_all_keeps_importers_of_broken_module(self):
        # parsers/mysql.py syntax error: full-suite truth (m068) fails test_execute_tpcds, whose
        # spawned pool workers import it; test_max_nodes never imports it, in any process
        sel = self.sel["hmy"]
        self.assertIn(self.num("tests/test_executor.py::TestExecutor::test_execute_tpcds"), sel)
        self.assertNotIn(self.num("tests/test_parser.py::TestParser::test_max_nodes"), sel)
        self.assertIn(self.num("tests/test_generator.py::TestGenerator::test_generator_memory"), sel)
        self.assertGreater(len(sel), len(self.tests) - 5)

    def test_import_time_mutation_target_change_selects_everything(self):
        # sa1 truth: 7 failing, 3 of them (test_jsonpath x2, eliminate_subqueries) never import a Trino
        # dialect alone, but share Generator.TRANSFORMS with one in the same process
        self.assertEqual(len(self.sel["imut"]), len(self.tests))
        # sa3 truth: 2 failing, both executing the line in isolation; same targets -> still isolated
        sel = self.sel["ival"]
        self.assertLess(len(sel), len(self.tests))
        self.assertIn(self.num("tests/dialects/test_dialect.py::TestDialect::test_lazy_load"), sel)

    def test_shared_container_setting_selects_everything_untrimmed(self):
        # sa2 truth: test_jsonpath x2 + test_exasol::test_json fail although they never import Solr alone;
        # a process that imported it earlier shares the popped base Generator.TRANSFORMS -> no slow-test trim
        self.assertEqual(len(self.sel["shsolr"]), len(self.tests))
        self.assertEqual(len(self.sel["shdune"]), len(self.tests))
        self.assertLess(len(self.sel["shneg"]), len(self.tests))

    def test_import_time_line_uses_isolated_coverage(self):
        # spark2 build_as_cast body runs at import (iso_imp.json.gz): tests whose own isolated run
        # executes it. Full-suite truth for this edit (stress_k iop-spark2-18): these two fail.
        sel = self.sel["hsp"]
        self.assertIn(self.num("tests/dialects/test_spark.py::TestSpark::test_spark"), sel)
        self.assertIn(self.num("tests/dialects/test_spark.py::TestSpark::test_string"), sel)
        self.assertNotIn(self.num("tests/test_parser.py::TestParser::test_max_nodes"), sel)
        # a test with no isolated run (fails alone) is always selected
        self.assertIn(self.num("tests/dialects/test_dialect.py::TestDialect::test_lazy_load"), sel)
        self.assertLess(len(sel), len(self.tests))

    def test_trimmed_all_fails_closed_on_import_side_effects(self):
        # import-time store into another module's object / a class that could replace a registered
        # dialect: shared state every test can see -> plain ALL
        self.assertEqual(len(self.sel["hmut"]), len(self.tests))
        self.assertEqual(len(self.sel["hdk"]), len(self.tests))

    def test_core_syntax_error_is_not_trimmed(self):
        # optimizer/scope.py is imported by test_max_nodes: plain ALL
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

    def test_value_trace_unread_dict_entry_selects_nothing(self):
        self.assertEqual(self.sel["dval"], set())  # no test reads NUMERIC_ESCAPES["u"]'s value

    def test_value_trace_key_or_call_changes_fall_back(self):
        # a changed key is visible to key-only uses (in, iteration): normal class rules
        self.assertGreater(len(self.sel["dkey"]), 100)
        # a new call inside the value runs code at class creation: normal class rules
        self.assertGreater(len(self.sel["dcall"]), 0)

    def test_value_trace_closure_factory_argument(self):
        # _show_parser only binds its arguments into a closure; no test reads this entry
        self.assertEqual(self.sel["dfac"], set())

    def test_value_trace_comparison_rules(self):
        self.assertEqual(self.sel["oplt"], set())  # no traced execution had version == (23, 12)
        self.assertGreater(len(self.sel["opmix"]), 0)  # an extra edit in the file: normal coverage
        self.assertGreater(len(self.sel["opeq"]), 0)  # == -> != is not a traced relational site
        self.assertIn("comparison operator change", self.stderr)

    def test_test_module_change_selects_importing_test_modules(self):
        # Person 1 audit: an import error in tests/dialects/test_dialect.py breaks collection of
        # every dialect test module importing Validator from it
        importers = {t["number"] for t in self.tests if t["file"].startswith("tests/dialects/test_")
                     and t["file"] != "tests/dialects/test_dialect.py"}
        self.assertTrue(len(importers) > 500 and importers <= self.sel["tdep"])

    def test_helper_called_at_import_by_importer(self):
        # helpers.is_compiled runs at import of test_dialect (module level) -> its importers too
        nums = {t["number"] for t in self.tests if t["file"] == "tests/dialects/test_bigquery.py"}
        self.assertTrue(nums and nums <= self.sel["thlp"])

    def test_isolated_test_body_stays_local(self):
        self.assertLess(len(self.sel["tloc"]), 20)

    def test_comparison_constant_rule_applies_only_to_pure_constant_changes(self):
        self.assertIn("sqlglot/errors.py: comparison constant change", self.stderr)
        self.assertGreater(len(self.sel["cvmix"]), len(self.sel["cvok"]))  # extra edit -> normal rules

    def test_static_index_changes_nothing(self):
        self.assertEqual(self.raw_idx, self.raw)  # build-time index == lazy computation
        self.assertEqual(self.raw_bad, self.raw)  # corrupt index -> ignored

    def test_garbage_and_empty_patches_do_not_crash(self):
        self.assertIn("junk", self.sel)
        self.assertIn("empt", self.sel)

    def test_indexed_mentions_equal_regex_scan(self):
        # the build-time word index must answer test_files_mentioning exactly like the regex scan, for
        # every traced attribute name the selector can ask about
        import gzip
        sys.path.insert(0, str(REPO))
        import selector as S
        sel = S.Selector(S.Map(S.MAP, self.tests))
        names = sorted(n for n in json.load(gzip.open(REPO / "vmap.json.gz", "rt"))["attr"] if S._UPPER_NAME.match(n))
        lazy = {n: sel.test_files_mentioning(n) for n in names}
        sel2 = S.Selector(S.Map(S.MAP, self.tests))
        sel2.test_words()
        self.assertGreater(len(names), 100)
        for n in names:
            self.assertEqual(sel2.test_files_mentioning(n), lazy[n], n)


class PatchReconstruction(unittest.TestCase):
    """apply_hunks must agree with git apply: offsets, -U0 insertions, and fail closed on unknown context."""

    def setUp(self):
        sys.path.insert(0, str(REPO))
        import selector
        self.S = selector
        self.old = [f"line{i}" for i in range(1, 41)]

    def apply(self, patch):
        f = self.S.parse_patch(patch)[0]
        return self.S.apply_hunks(list(self.old), f["hunks"])[0]

    def patch(self, new, n=3, shift=0):
        body = "".join(difflib.unified_diff([x + "\n" for x in self.old], [x + "\n" for x in new], "a/f.py", "b/f.py", n=n))
        if shift:
            import re
            body = re.sub(r"@@ -(\d+)", lambda m: f"@@ -{int(m.group(1)) + shift}", body)
        return "diff --git a/f.py b/f.py\n" + body

    def test_offset_hunk_matches_unshifted(self):
        new = list(self.old)
        new[20] = "changed"
        want = self.apply(self.patch(new))
        self.assertEqual(want[20], "changed")
        for shift in (-5, -1, 2, 7):
            self.assertEqual(self.apply(self.patch(new, shift=shift)), want, shift)

    def test_zero_context_insertion_lands_after_its_line(self):
        new = self.old[:6] + ["inserted"] + self.old[6:]
        self.assertEqual(self.apply(self.patch(new, n=0)), self.apply(self.patch(new)))
        self.assertEqual(self.apply(self.patch(new, n=0))[6], "inserted")

    def test_unknown_context_raises(self):
        p = self.patch([*self.old[:20], "changed", *self.old[21:]]).replace(" line19\n", " lineXX\n", 1)
        with self.assertRaises(self.S.PatchMismatch):
            self.apply(p)


class ConstLeafDiff(unittest.TestCase):
    """Which class-dict entry value edits the value-trace rule may treat as 'value only'."""

    def test_cases(self):
        import ast

        sys.path.insert(0, str(REPO))
        from selector import const_leaf_diff

        fac = lambda n: n == "fac"  # noqa: E731
        cases = [
            ("(1, 2)", "(1, 3)", True), ("{'a': 1}", "{'a': 2}", True), ("-1", "-2", True),
            ("1/2", "1/0", False),  # evaluated at class creation, may raise
            ("lambda s: s.f(1)", "lambda s: s.f(2)", True),  # body runs only when called
            ("lambda s, k=1: k", "lambda s, k=2: k", False), ("lambda s, k=1/2: k", "lambda s, k=1/0: k", False),
            ("f(1)", "f(2)", False),  # unknown call runs at class creation
            ("fac('A')", "fac('B')", True), ("fac(x='A')", "fac(x='B')", True),  # closure factory
            ("fac(g(1))", "fac(g(2))", False), ("x[0]", "x[1]", False),
            ("[i for i in (1,)]", "[i for i in (2,)]", False),
        ]
        for a, b, want in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(const_leaf_diff(ast.parse(a, mode="eval").body, ast.parse(b, mode="eval").body, fac), want)


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
