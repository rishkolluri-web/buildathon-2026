"""Build the per-test dependency map from the pinned sqlglot checkout.

Usage (Python 3.12+, sqlglot checkout as the working directory, its deps installed):

    SKIP_INTEGRATION=0 python /path/to/tools/build_map.py /path/to/tests.json /path/to/out.json.gz [jobs]

Each test runs in its own forked child, so caches warmed by one test never hide
work from another. In every child we record:

  * every Python code object (function, method, lambda) under sqlglot/ or tests/
    that the test entered, via sys.monitoring PY_START (cheap: each code object
    is disabled after its first hit), and
  * every file under tests/ that the test opened (fixtures), via an audit hook.

Everything sqlglot does at import time happens once in the parent before any
fork, and is recorded separately as "import_code".
"""

from __future__ import annotations

import gzip
import importlib
import json
import os
import pkgutil
import sys
import tempfile
import time

ROOT = os.getcwd()
TOOL = sys.monitoring.PROFILER_ID if hasattr(sys, "monitoring") else None
PREFIXES = (os.path.join(ROOT, "sqlglot") + os.sep, os.path.join(ROOT, "tests") + os.sep)


def code_key(code) -> str | None:
    filename = code.co_filename
    if not filename.startswith(PREFIXES):
        return None
    return f"{os.path.relpath(filename, ROOT)}::{code.co_qualname}::{code.co_firstlineno}"


def start_recording(entered: set[str]) -> None:
    mon = sys.monitoring
    mon.use_tool_id(TOOL, "blast-radius-map")

    def on_start(code, offset):
        key = code_key(code)
        if key is not None:
            entered.add(key)
        return mon.DISABLE

    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.PY_START)
    # Code objects disabled while recording the parent's imports must fire again.
    mon.restart_events()


def stop_recording() -> None:
    mon = sys.monitoring
    mon.set_events(TOOL, 0)
    mon.register_callback(TOOL, mon.events.PY_START, None)
    mon.free_tool_id(TOOL)


def preimport() -> None:
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(info.name)
        except Exception as error:  # noqa: BLE001
            print(f"preimport {info.name} failed: {error}", file=sys.stderr)


SITECUSTOMIZE = """
import os, sys
_out = os.environ.get("BR_MAP_SUB")
if _out and hasattr(sys, "monitoring"):
    _root = os.environ["BR_MAP_ROOT"]
    _prefixes = (os.path.join(_root, "sqlglot") + os.sep, os.path.join(_root, "tests") + os.sep)
    _file = open(os.path.join(_out, f"{os.getpid()}.keys"), "a", buffering=1)
    _mon = sys.monitoring
    def _on_start(code, offset):
        if code.co_filename.startswith(_prefixes):
            _file.write(f"{os.path.relpath(code.co_filename, _root)}::{code.co_qualname}::{code.co_firstlineno}\\n")
        return _mon.DISABLE
    _mon.use_tool_id(_mon.PROFILER_ID, "blast-radius-map-sub")
    _mon.register_callback(_mon.PROFILER_ID, _mon.events.PY_START, _on_start)
    _mon.set_events(_mon.PROFILER_ID, _mon.events.PY_START)
"""


def run_child(test_id: str, out_path: str) -> None:
    # Processes the test spawns (test_optimizer uses a spawn pool) record too,
    # through a sitecustomize module on their PYTHONPATH.
    sub_dir = out_path + ".sub"
    os.makedirs(sub_dir, exist_ok=True)
    os.environ["BR_MAP_SUB"] = sub_dir
    os.environ["BR_MAP_ROOT"] = ROOT
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(os.path.dirname(out_path), "site"), ROOT, os.environ.get("PYTHONPATH", "")])
    entered: set[str] = set()
    opened: set[str] = set()
    tests_dir = os.path.join(ROOT, "tests") + os.sep

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.startswith(tests_dir) and not path.endswith((".py", ".pyc")):
                opened.add(os.path.relpath(path, ROOT))

    sys.addaudithook(audit)
    import pytest

    start_recording(entered)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    code = pytest.main([test_id, "-q", "-p", "no:cacheprovider", "-p", "no:randomly"])
    stop_recording()
    for name in os.listdir(sub_dir):
        with open(os.path.join(sub_dir, name)) as handle:
            entered.update(line.strip() for line in handle if line.strip())
    with open(out_path, "w") as handle:
        json.dump({"code": sorted(entered), "files": sorted(opened), "rc": int(code)}, handle)
    os._exit(0)


def main() -> None:
    tests_json, out_file = sys.argv[1], sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count() or 2
    tests = json.load(open(tests_json))["tests"]
    sys.path.insert(0, ROOT)

    import_code: set[str] = set()
    start_recording(import_code)
    preimport()
    stop_recording()
    print(f"preimported, {len(import_code)} code objects ran at import", file=sys.stderr)

    tmp = tempfile.mkdtemp(prefix="brmap")
    os.makedirs(os.path.join(tmp, "site"))
    with open(os.path.join(tmp, "site", "sitecustomize.py"), "w") as handle:
        handle.write(SITECUSTOMIZE)
    pending = list(enumerate(tests))
    running: dict[int, int] = {}
    started = time.time()
    while pending or running:
        while pending and len(running) < jobs:
            index, test = pending.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    run_child(test["id"], os.path.join(tmp, f"{index}.json"))
                finally:
                    os._exit(1)
            running[pid] = index
        pid, _ = os.wait()
        running.pop(pid, None)
        done = len(tests) - len(pending) - len(running)
        if done % 100 == 0:
            print(f"{done}/{len(tests)} in {time.time() - started:.0f}s", file=sys.stderr)

    code_tests: dict[str, list[int]] = {}
    file_tests: dict[str, list[int]] = {}
    failed = []
    for index, test in enumerate(tests):
        path = os.path.join(tmp, f"{index}.json")
        if not os.path.exists(path):
            failed.append(test["id"])
            continue
        result = json.load(open(path))
        if result["rc"] != 0:
            failed.append(test["id"])
        for key in result["code"]:
            code_tests.setdefault(key, []).append(index)
        for key in result["files"]:
            file_tests.setdefault(key, []).append(index)

    data = {
        "test_ids": [test["id"] for test in tests],
        "code": code_tests,
        "files": file_tests,
        "import_code": sorted(import_code),
        "unrecorded_or_failed": failed,
    }
    with gzip.open(out_file, "wt") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"done in {time.time() - started:.0f}s; {len(code_tests)} code objects, "
          f"{len(file_tests)} files, {len(failed)} tests failed or unrecorded", file=sys.stderr)


if __name__ == "__main__":
    main()
