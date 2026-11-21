#!/usr/bin/env python3
"""Independent RWX Blast Radius output-protocol checker (Person 2).

This is OUR check, not the official scorer. It validates a selector's stdout
against the protocol described in the RWX track rules:

  * exactly one JSON line per patch in <data>/commits/*.patch, no extras
  * each line is an object with exactly the keys "commit" and "workers"
  * "workers" is a list of at most 8 NONEMPTY lists of integer test numbers
  * every test number is known (from <data>/tests.json) and appears at most once
  * stdout is <= 2 MB

Usage:
  python3 tests/protocol_check.py --data DIR --output stdout.jsonl
  some_selector | python3 tests/protocol_check.py --data DIR --output -

Exit code 0 = valid, 1 = protocol violations (listed on stderr).

TODO(reconcile): the exact tests.json schema must be confirmed against the
official blast-radius-sample archive. load_test_numbers() tolerates a few
plausible shapes until then.
"""
import argparse
import json
import os
import sys

MAX_WORKERS = 8
MAX_STDOUT_BYTES = 2 * 1024 * 1024
_NUM_KEYS = ("num", "number", "n", "index", "idx", "id")


def load_test_numbers(tests_json_path):
    """Return the set of valid test numbers from tests.json."""
    with open(tests_json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    tests = doc["tests"] if isinstance(doc, dict) else doc
    nums = set()
    for i, entry in enumerate(tests):
        if isinstance(entry, dict):
            for key in _NUM_KEYS:
                if isinstance(entry.get(key), int) and not isinstance(entry.get(key), bool):
                    nums.add(entry[key])
                    break
            else:
                nums.add(i)  # fall back to positional numbering
        else:
            nums.add(i)
    return nums


def list_commit_ids(commits_dir):
    return {
        name[: -len(".patch")]
        for name in os.listdir(commits_dir)
        if name.endswith(".patch")
    }


def check_output(raw_bytes, commit_ids, valid_tests):
    """Validate raw stdout bytes. Returns a list of human-readable errors."""
    errors = []
    if len(raw_bytes) > MAX_STDOUT_BYTES:
        errors.append(f"stdout is {len(raw_bytes)} bytes (> {MAX_STDOUT_BYTES})")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return errors + [f"stdout is not valid UTF-8: {e}"]

    seen_commits = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            errors.append(f"line {lineno}: blank line on stdout")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"line {lineno}: not JSON ({e}): {line[:80]!r}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {lineno}: not a JSON object")
            continue
        if set(obj) != {"commit", "workers"}:
            errors.append(f"line {lineno}: keys must be exactly commit/workers, got {sorted(obj)}")
        commit = obj.get("commit")
        if not isinstance(commit, str):
            errors.append(f"line {lineno}: commit must be a string")
        elif commit not in commit_ids:
            errors.append(f"line {lineno}: unknown commit {commit!r}")
        elif commit in seen_commits:
            errors.append(f"line {lineno}: duplicate commit {commit!r}")
        else:
            seen_commits.add(commit)

        workers = obj.get("workers")
        if not isinstance(workers, list):
            errors.append(f"line {lineno}: workers must be a list")
            continue
        if len(workers) > MAX_WORKERS:
            errors.append(f"line {lineno}: {len(workers)} workers (> {MAX_WORKERS})")
        used = set()
        for w_i, worker in enumerate(workers):
            if not isinstance(worker, list):
                errors.append(f"line {lineno}: worker {w_i} is not a list")
                continue
            if not worker:
                errors.append(f"line {lineno}: worker {w_i} is empty")
            for t in worker:
                if not isinstance(t, int) or isinstance(t, bool):
                    errors.append(f"line {lineno}: worker {w_i} has non-integer test {t!r}")
                elif t not in valid_tests:
                    errors.append(f"line {lineno}: unknown test number {t}")
                elif t in used:
                    errors.append(f"line {lineno}: test {t} assigned more than once")
                else:
                    used.add(t)

    for missing in sorted(commit_ids - seen_commits):
        errors.append(f"missing output for commit {missing!r}")
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="dir containing tests.json and commits/")
    ap.add_argument("--output", required=True, help="selector stdout file, or - for stdin")
    args = ap.parse_args(argv)

    raw = sys.stdin.buffer.read() if args.output == "-" else open(args.output, "rb").read()
    errors = check_output(
        raw,
        list_commit_ids(os.path.join(args.data, "commits")),
        load_test_numbers(os.path.join(args.data, "tests.json")),
    )
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)
    print("PASS" if not errors else f"{len(errors)} protocol violation(s)", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
