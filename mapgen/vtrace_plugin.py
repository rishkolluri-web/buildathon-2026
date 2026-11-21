"""pytest side of vtrace.py: per-test contexts, inline process pools, per-test cache reset."""

import os
import sys

import vtrace

import concurrent.futures as _cf
import pickle as _pickle

import pytest

vtrace.install()
vtrace._current[0] = "<session>"

class _InlineExecutor(_cf.Executor):
    """Run pool work in-process (with the pickling round trip) so it is attributed to the test."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_pickle.loads(_pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        fut = _cf.Future()
        try:
            args, kwargs = _pickle.loads(_pickle.dumps((args, kwargs)))
            fut.set_result(_pickle.loads(_pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

_cf.ProcessPoolExecutor = _InlineExecutor

def _clear_process_caches():
    gen = sys.modules.get("sqlglot.generator")
    cache = getattr(gen, "_DISPATCH_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _clear_process_caches()
    vtrace._current[0] = item.nodeid
    yield
    vtrace._current[0] = "<between>"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    vtrace._current[0] = item.nodeid + "|setup"
    yield
    vtrace._current[0] = item.nodeid

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    vtrace._current[0] = item.nodeid + "|teardown"
    yield
    vtrace._current[0] = item.nodeid

@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if not isinstance(collector, pytest.Module):
        yield
        return
    prev = vtrace._current[0]
    vtrace._current[0] = "collect:" + os.path.relpath(str(collector.path), os.path.realpath(os.environ["VTRACE_SRC"]))
    try:
        yield
    finally:
        vtrace._current[0] = prev




def pytest_runtest_logreport(report):
    # completion record: a test absent from DONE is untraced (the selector always keeps it)
    if report.when == "call" and (report.passed or report.skipped):
        vtrace.DONE.add(report.nodeid)
    elif report.when == "setup" and report.skipped:
        vtrace.DONE.add(report.nodeid)
