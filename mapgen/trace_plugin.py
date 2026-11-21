"""pytest plugin: record which non-Python files under the sqlglot checkout each test opens.

Loaded with `-p trace_plugin` (this directory on PYTHONPATH) while running
sqlglot's own test suite at the pinned commit. Uses a sys audit hook, so it
sees open() from builtins, io and pathlib alike. Output: one JSON file per
pytest process in $MAPGEN_OUT, {nodeid: [relative paths opened]}.
"""

import json
import os
import sys

import pytest

_ROOT = os.path.realpath(os.environ.get("SQLGLOT_ROOT", os.getcwd())) + os.sep
_OUT = os.environ.get("MAPGEN_OUT", "/tmp/mapgen")
_current = ["<session>"]
_opens: dict = {}


def _hook(event, args):
    if event != "open" or not args:
        return
    path = args[0]
    if isinstance(path, int):
        return
    try:
        path = os.path.realpath(os.fsdecode(path))
    except Exception:
        return
    if path.startswith(_ROOT) and not path.endswith((".py", ".pyc")) and "__pycache__" not in path:
        _opens.setdefault(_current[0], set()).add(path[len(_ROOT):])


sys.addaudithook(_hook)


# sqlglot's optimizer/executor tests farm work out to spawned ProcessPoolExecutor
# workers, which per-test coverage cannot see. For map generation only, run that
# work inline in the test's own process so its lines are credited to the test.
import concurrent.futures as _cf


import pickle as _pickle


class _InlineExecutor(_cf.Executor):
    """Runs work in-process but keeps the process boundary's pickling (sqlglot
    pickles expressions via serde.py, so that code must stay covered)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_pickle.loads(_pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        fut = _cf.Future()
        try:
            args, kwargs = _pickle.loads(_pickle.dumps((args, kwargs)))
            result = fn(*args, **kwargs)
            fut.set_result(_pickle.loads(_pickle.dumps(result)))
        except BaseException as e:  # noqa: BLE001 - mirror executor semantics
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


_cf.ProcessPoolExecutor = _InlineExecutor


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(_OUT, exist_ok=True)
    with open(os.path.join(_OUT, f"opens-{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
