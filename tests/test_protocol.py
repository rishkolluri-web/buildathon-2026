"""Unit tests for our independent protocol checker (NOT the official scorer).

Run: python3 -m unittest discover -s tests -v

Optional integration check against a real selector run:
  RWX_DATA=/path/to/sample/data RWX_OUTPUT=/path/to/stdout.jsonl \
      python3 -m unittest discover -s tests -v
"""
import json
import os
import unittest

from protocol_check import MAX_STDOUT_BYTES, check_output, list_commit_ids, load_test_numbers

COMMITS = {"k7q2", "a1b2"}
TESTS = set(range(10))


def run(lines):
    raw = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines).encode()
    return check_output(raw, COMMITS, TESTS)


class ValidOutputs(unittest.TestCase):
    def test_spec_example_shape(self):
        self.assertEqual(run([
            {"commit": "k7q2", "workers": [[1, 2, 3], [4]]},
            {"commit": "a1b2", "workers": []},
        ]), [])

    def test_zero_test_selection_is_valid(self):
        self.assertEqual(run([
            {"commit": "k7q2", "workers": []},
            {"commit": "a1b2", "workers": []},
        ]), [])

    def test_eight_workers_ok(self):
        self.assertEqual(run([
            {"commit": "k7q2", "workers": [[i] for i in range(8)]},
            {"commit": "a1b2", "workers": []},
        ]), [])

    def test_same_test_in_different_commits_ok(self):
        self.assertEqual(run([
            {"commit": "k7q2", "workers": [[1]]},
            {"commit": "a1b2", "workers": [[1]]},
        ]), [])


class InvalidOutputs(unittest.TestCase):
    def assertFails(self, lines_or_raw, needle):
        if isinstance(lines_or_raw, bytes):
            errs = check_output(lines_or_raw, COMMITS, TESTS)
        else:
            errs = run(lines_or_raw)
        self.assertTrue(any(needle in e for e in errs), f"expected {needle!r} in {errs}")

    def test_missing_commit(self):
        self.assertFails([{"commit": "k7q2", "workers": []}], "missing output")

    def test_duplicate_commit(self):
        self.assertFails([
            {"commit": "k7q2", "workers": []},
            {"commit": "k7q2", "workers": []},
            {"commit": "a1b2", "workers": []},
        ], "duplicate commit")

    def test_unknown_commit(self):
        self.assertFails([
            {"commit": "k7q2", "workers": []},
            {"commit": "a1b2", "workers": []},
            {"commit": "zzzz", "workers": []},
        ], "unknown commit")

    def test_extra_key(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [], "debug": 1},
            {"commit": "a1b2", "workers": []},
        ], "keys must be exactly")

    def test_nine_workers(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [[i] for i in range(9)]},
            {"commit": "a1b2", "workers": []},
        ], "workers (> 8)")

    def test_empty_worker(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [[1], []]},
            {"commit": "a1b2", "workers": []},
        ], "is empty")

    def test_duplicate_test_across_workers(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [[1, 2], [2]]},
            {"commit": "a1b2", "workers": []},
        ], "more than once")

    def test_unknown_test_number(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [[999]]},
            {"commit": "a1b2", "workers": []},
        ], "unknown test number")

    def test_string_and_bool_test_ids_rejected(self):
        self.assertFails([
            {"commit": "k7q2", "workers": [["1"], [True]]},
            {"commit": "a1b2", "workers": []},
        ], "non-integer")

    def test_debug_text_on_stdout(self):
        raw = b'loading tests...\n{"commit":"k7q2","workers":[]}\n{"commit":"a1b2","workers":[]}\n'
        self.assertFails(raw, "not JSON")

    def test_stdout_over_2mb(self):
        body = b'{"commit":"k7q2","workers":[]}\n{"commit":"a1b2","workers":[]}\n'
        raw = body + b" " * (MAX_STDOUT_BYTES + 1)
        self.assertFails(raw, "bytes (>")


class LoadTestsJson(unittest.TestCase):
    def test_numbered_dict_entries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tests.json")
            with open(p, "w") as f:
                json.dump({"tests": [{"num": 5, "file": "a.py", "duration": 1.0}], "skipped": []}, f)
            self.assertEqual(load_test_numbers(p), {5})


@unittest.skipUnless(os.environ.get("RWX_DATA") and os.environ.get("RWX_OUTPUT"),
                     "set RWX_DATA and RWX_OUTPUT to check a real selector run")
class RealSelectorRun(unittest.TestCase):
    def test_real_output_is_protocol_valid(self):
        data = os.environ["RWX_DATA"]
        with open(os.environ["RWX_OUTPUT"], "rb") as f:
            raw = f.read()
        errs = check_output(raw, list_commit_ids(os.path.join(data, "commits")),
                            load_test_numbers(os.path.join(data, "tests.json")))
        self.assertEqual(errs, [], "\n".join(errs))


if __name__ == "__main__":
    unittest.main()
