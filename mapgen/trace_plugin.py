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
    # .py files count too: some tests read sqlglot sources as data (e.g. test_typing).
    # Lazy imports during a test also show up here; that only adds a few extra tests.
    if path.startswith(_ROOT) and not path.endswith(".pyc") and "__pycache__" not in path:
        if path.endswith(".py") and _from_import_system():
            return
        _opens.setdefault(_current[0], set()).add(path[len(_ROOT):])


def _from_import_system():
    f = sys._getframe(2)
    for _ in range(25):
        if f is None:
            return False
        name = f.f_code.co_filename
        if "importlib" in name or name.startswith("<frozen"):
            return True
        f = f.f_back
    return False


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


def _clear_process_caches():
    # Process-wide memo caches credit their builder code only to the first test
    # that fills them; clearing them makes every test re-run (and own) that code.
    gen = sys.modules.get("sqlglot.generator")
    cache = getattr(gen, "_DISPATCH_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _clear_process_caches()
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"


# Shared setup runs inside ONE test's setup phase (unittest setUpClass/setUpModule,
# class/module-scoped fixtures) and teardown likewise; its effects are shared by
# every test of the file. Key those opens as "<nodeid>|setup"/"|teardown";
# build_map credits them to all tests of the file. (Coverage already separates
# phases via pytest-cov's "|setup"/"|teardown" contexts.)
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _current[0] = item.nodeid + "|setup"
    yield
    _current[0] = item.nodeid


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _current[0] = item.nodeid + "|teardown"
    yield
    _current[0] = item.nodeid


# Test modules may read fixtures or run sqlglot code at import (collection) time.
# Attribute that to "collect:<file>" for opens AND coverage; build_map credits it
# to every test of that file.
@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if not isinstance(collector, pytest.Module):
        yield
        return
    try:
        rel = os.path.relpath(str(collector.path), _ROOT.rstrip(os.sep))
    except Exception:
        rel = str(collector.path)
    ctx = "collect:" + rel
    prev = _current[0]
    _current[0] = ctx
    cov = _coverage()
    if cov is not None:
        cov.switch_context(ctx)
    try:
        yield
    finally:
        _current[0] = prev
        if cov is not None:
            cov.switch_context("")


def _coverage():
    try:
        import coverage

        return coverage.Coverage.current()
    except Exception:
        return None


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(_OUT, exist_ok=True)
    with open(os.path.join(_OUT, f"opens-{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
