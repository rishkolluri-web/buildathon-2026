"""pytest plugin: record which sqlglot classes each test instantiates.

At session start it imports every sqlglot module and wraps each Python-level
__init__ defined on a sqlglot class, logging type(self) for the running test.
Output per pytest process in $MAPGEN_OUT:
  tracks-<pid>.json  {nodeid: [class id, ...]}
  mro-<pid>.json     {class id: [ids of sqlglot classes in its MRO]}
Class id = "<module>:<qualname>".
"""

import functools
import importlib
import inspect
import json
import os
import pkgutil

import pytest

_OUT = os.environ.get("MAPGEN_OUT", "/tmp/mapgen")
_current = ["<session>"]
_used: dict = {}
_classes: dict = {}


def _cid(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def _wrap(cls):
    orig = cls.__dict__["__init__"]

    @functools.wraps(orig)
    def __init__(self, *a, **k):
        _used.setdefault(_current[0], set()).add(type(self))
        return orig(self, *a, **k)

    setattr(cls, "__init__", __init__)


def _instrument():
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            continue
    import sys

    seen = set()
    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for _, obj in list(vars(mod).items()):
            if inspect.isclass(obj) and obj.__module__.startswith("sqlglot") and obj not in seen:
                seen.add(obj)
                _classes[_cid(obj)] = obj
                if inspect.isfunction(obj.__dict__.get("__init__")):
                    try:
                        _wrap(obj)
                    except (TypeError, AttributeError):
                        pass
    # nested classes (e.g. Dialect.Tokenizer)
    for obj in list(seen):
        for _, inner in list(vars(obj).items()):
            if inspect.isclass(inner) and inner.__module__.startswith("sqlglot") and inner not in seen:
                seen.add(inner)
                _classes[_cid(inner)] = inner
                if inspect.isfunction(inner.__dict__.get("__init__")):
                    try:
                        _wrap(inner)
                    except (TypeError, AttributeError):
                        pass


def pytest_configure(config):
    _instrument()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = "<between>"


# shared setup/teardown and collection-time work: credited file-wide by build_map
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


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if not isinstance(collector, pytest.Module):
        yield
        return
    prev = _current[0]
    _current[0] = "collect:" + os.path.relpath(str(collector.path), os.environ.get("SQLGLOT_ROOT", os.getcwd()))
    try:
        yield
    finally:
        _current[0] = prev


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(_OUT, exist_ok=True)
    pid = os.getpid()
    types = set()
    for v in _used.values():
        types |= v
    types |= set(_classes.values())
    with open(os.path.join(_OUT, f"tracks-{pid}.json"), "w") as f:
        json.dump({k: sorted(_cid(c) for c in v) for k, v in _used.items()}, f)
    with open(os.path.join(_OUT, f"mro-{pid}.json"), "w") as f:
        json.dump(
            {
                _cid(c): [_cid(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
                for c in types
            },
            f,
        )
