#!/usr/bin/env python3
"""Mutation stress test for selector.py (Person 2 verification tool; not used at runtime).

1. generate: deterministic, diverse, realistic mutations of the pinned sqlglot
   checkout (operator swaps, constants, class dicts, module constants, default
   args, decorators, multi-line continuation lines, import-time helpers,
   inserted/deleted lines, comments, docstrings, imports, fixtures, test files,
   syntax errors) -> unified diffs.
2. truth: apply each mutation to a scratch copy and run sqlglot's full test suite
   (SKIP_INTEGRATION=0) to get the ground-truth failing tests. Cached per mutant.
3. eval: run selector.py on all mutation patches and report misses + cost.

  python3 scripts/stress_mutate.py generate --src ../sqlglot --out WORK [--n 72 --seed 7]
  python3 scripts/stress_mutate.py truth --src ../sqlglot --out WORK --python PY [--jobs 2]
  python3 scripts/stress_mutate.py eval --src ../sqlglot --out WORK --tests tests.json [--map map.json.gz]
"""

import argparse
import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------------- generation

SWAPS = [
    (" is not ", " is "), (" is ", " is not "), (" not in ", " in "), (" in ", " not in "),
    (" and ", " or "), (" or ", " and "), ("==", "!="), ("!=", "=="),
    ("<=", "<"), (">=", ">"), (" < ", " <= "), (" > ", " >= "),
    ("True", "False"), ("False", "True"), ("not ", ""), (" + 1", " - 1"), (" - 1", " + 1"),
    ("None", "0"),
]


def text_mutate(line, rng):
    ops = SWAPS[:]
    rng.shuffle(ops)
    for a, b in ops:
        if a in line:
            i = line.index(a)
            return line[:i] + b + line[i + len(a):]
    m = re.search(r"(?<![\w.])(\d+)(?![\w.])", line)
    if m:
        return line[: m.start()] + str(int(m.group(1)) + 1) + line[m.end():]
    m = re.search(r"\"([A-Za-z_ ]{2,})\"|'([A-Za-z_ ]{2,})'", line)
    if m:
        return line[: m.end() - 1] + "X" + line[m.end() - 1:]
    return None


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


class Pools:
    def __init__(self, path, src):
        self.path, self.src = path, src
        self.lines = src.split("\n")
        tree = ast.parse(src)
        self.func_stmt, self.func_cont, self.class_assign, self.module_assign = [], [], [], []
        self.defaults, self.decorators, self.docstrings, self.func_starts = [], [], [], []
        self.imports, self.import_time = [], []
        self._walk(tree.body, "module", False)

    def _walk(self, body, ctx, import_time_fn):
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(d, ast.Constant) for d in s.args.defaults + s.args.kw_defaults if d is not None):
                    self.defaults.append(s.lineno)
                if s.decorator_list:
                    self.decorators.append(s.decorator_list[0].lineno)
                if ast.get_docstring(s):
                    self.docstrings.append(s.body[0].lineno)
                if s.body and not ast.get_docstring(s) and len(s.body) > 1:
                    self.func_starts.append(s.body[0].lineno)
                imp = import_time_fn or s.name in ("__new__", "__init_subclass__") or (ctx == "class" and s.name.startswith("__init_sub"))
                self._walk(s.body, "func", imp)
            elif isinstance(s, ast.ClassDef):
                if ast.get_docstring(s):
                    self.docstrings.append(s.body[0].lineno)
                self._walk(s.body, "class", import_time_fn)
            else:
                if ctx == "func":
                    (self.import_time if import_time_fn else self.func_stmt).append(s.lineno)
                    if s.end_lineno and s.end_lineno > s.lineno and not isinstance(s, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                        self.func_cont.extend(range(s.lineno + 1, s.end_lineno + 1))
                elif ctx == "class" and isinstance(s, (ast.Assign, ast.AnnAssign)):
                    self.class_assign.extend(range(s.lineno, (s.end_lineno or s.lineno) + 1))
                elif ctx == "module" and isinstance(s, (ast.Assign, ast.AnnAssign)):
                    self.module_assign.extend(range(s.lineno, (s.end_lineno or s.lineno) + 1))
                elif ctx == "module" and isinstance(s, ast.ImportFrom) and s.end_lineno == s.lineno:
                    self.imports.append(s.lineno)
                for f in ("body", "orelse", "finalbody"):
                    if isinstance(getattr(s, f, None), list):
                        self._walk(getattr(s, f), ctx, import_time_fn)
                for h in getattr(s, "handlers", []) or []:
                    self._walk(h.body, ctx, import_time_fn)


def mutate_line(pools, lineno, rng):
    lines = pools.lines[:]
    new = text_mutate(lines[lineno - 1], rng)
    if new is None or new == lines[lineno - 1]:
        return None
    lines[lineno - 1] = new
    return "\n".join(lines)


def valid_change(old, new, kind):
    try:
        a, b = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return kind == "syntax"
    if kind == "comment":
        return ast.dump(a) == ast.dump(b)
    return ast.dump(a) != ast.dump(b)


def gen_one(kind, files, pools_of, rng, src_root):
    for _ in range(200):
        if kind in ("fixture",):
            fx = sorted((src_root / "tests" / "fixtures").rglob("*.sql"))
            p = rng.choice(fx)
            text = p.read_text()
            lines = text.split("\n")
            cand = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("--", "#"))]
            if not cand:
                continue
            i = rng.choice(cand)
            new = text_mutate(lines[i], rng) or (lines[i].replace(" ", "  x ", 1) if " " in lines[i] else None)
            if not new or new == lines[i]:
                continue
            lines[i] = new
            return str(p.relative_to(src_root)), text, "\n".join(lines)
        if kind == "testfile":
            tf = sorted((src_root / "tests").rglob("test_*.py"))
            p = rng.choice(tf)
            pools = Pools(str(p.relative_to(src_root)), p.read_text())
            if not pools.func_stmt:
                continue
            new = mutate_line(pools, rng.choice(pools.func_stmt), rng)
            if new and valid_change(pools.src, new, kind):
                return pools.path, pools.src, new
            continue
        path = rng.choice(files)
        pools = pools_of(path)
        L = pools.lines
        new = None
        if kind == "code" and pools.func_stmt:
            new = mutate_line(pools, rng.choice(pools.func_stmt), rng)
        elif kind == "multiline" and pools.func_cont:
            new = mutate_line(pools, rng.choice(pools.func_cont), rng)
        elif kind == "classdata" and pools.class_assign:
            new = mutate_line(pools, rng.choice(pools.class_assign), rng)
        elif kind == "moduledata" and pools.module_assign:
            new = mutate_line(pools, rng.choice(pools.module_assign), rng)
        elif kind == "default" and pools.defaults:
            ln = rng.choice(pools.defaults)
            new = mutate_line(pools, ln, rng)
        elif kind == "decorator" and pools.decorators:
            ln = rng.choice(pools.decorators)
            lines = L[:]
            del lines[ln - 1]
            new = "\n".join(lines)
        elif kind == "importtime" and pools.import_time:
            new = mutate_line(pools, rng.choice(pools.import_time), rng)
        elif kind == "insert" and pools.func_starts:
            ln = rng.choice(pools.func_starts)
            lines = L[:]
            lines.insert(ln - 1, indent_of(L[ln - 1]) + "return None")
            new = "\n".join(lines)
        elif kind == "delete" and pools.func_stmt:
            ln = rng.choice(pools.func_stmt)
            node_line = L[ln - 1]
            if node_line.rstrip().endswith((":", "(", "[", "{", ",", "\\")) or node_line.strip().startswith(("return", "raise", "yield")):
                continue
            lines = L[:]
            del lines[ln - 1]
            new = "\n".join(lines)
        elif kind == "comment":
            cand = pools.func_stmt + pools.class_assign[:1]
            if not cand:
                continue
            ln = rng.choice(cand)
            lines = L[:]
            lines.insert(ln - 1, indent_of(L[ln - 1]) + rng.choice(["# Cheap check first", "# TODO: revisit", "# noqa", "# keep in sync with the parser"]))
            new = "\n".join(lines)
        elif kind == "docstring" and pools.docstrings:
            ln = rng.choice(pools.docstrings)
            lines = L[:]
            m = re.search(r"[A-Za-z]{4,}", lines[ln - 1])
            if not m:
                continue
            lines[ln - 1] = lines[ln - 1][: m.start()] + "Very " + lines[ln - 1][m.start():]
            new = "\n".join(lines)
        elif kind == "import" and pools.imports:
            ln = rng.choice(pools.imports)
            lines = L[:]
            del lines[ln - 1]
            new = "\n".join(lines)
        elif kind == "syntax" and pools.func_stmt:
            ln = rng.choice(pools.func_stmt)
            lines = L[:]
            lines[ln - 1] = lines[ln - 1] + " )"
            new = "\n".join(lines)
        if new and valid_change(pools.src, new, kind):
            return path, pools.src, new
    return None


PLAN = [("code", 22), ("multiline", 6), ("classdata", 10), ("moduledata", 5), ("default", 4),
        ("decorator", 3), ("importtime", 4), ("insert", 3), ("delete", 3), ("comment", 4),
        ("docstring", 3), ("import", 2), ("syntax", 1), ("fixture", 5), ("testfile", 3)]


def udiff(path, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\n" + body


def cmd_generate(a):
    src = Path(a.src).resolve()
    out = Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    files = sorted(str(p.relative_to(src)) for p in (src / "sqlglot").rglob("*.py") if "__pycache__" not in str(p))
    cache = {}

    def pools_of(path):
        if path not in cache:
            cache[path] = Pools(path, (src / path).read_text())
        return cache[path]

    meta = {}
    i = 0
    for kind, count in PLAN:
        for _ in range(count):
            r = gen_one(kind, files, pools_of, rng, src)
            if r is None:
                print(f"could not generate {kind}", file=sys.stderr)
                continue
            path, old, new = r
            mid = f"m{i:03d}"
            i += 1
            (out / "commits" / f"{mid}.patch").write_text(udiff(path, old, new))
            meta[mid] = {"kind": kind, "file": path}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"generated {len(meta)} mutations in {out}")


# ----------------------------------------------------------------------------- ground truth

FAIL_PLUGIN = '''
import json, os
_failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT::" + report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    if not hasattr(session.config, "workerinput"):
        with open(os.environ["MUT_RESULT"], "w") as f:
            json.dump(sorted(_failed), f)
'''


def apply_patch_text(src_root, patch):
    """Return (path, new_text) for our own single-file generated diffs."""
    path = re.search(r"^\+\+\+ b/(\S+)", patch, re.M).group(1)
    old = (src_root / path).read_text().split("\n")
    sys.path.insert(0, str(REPO))
    import selector  # reuse the selector's hunk applier

    files = selector.parse_patch(patch)
    new, _, _ = selector.apply_hunks(old, files[0]["hunks"])
    return path, "\n".join(new)


def run_truth(mid, a, src, out, plugin_dir):
    res = out / "truth" / f"{mid}.json"
    if res.exists():
        return mid, "cached"
    work = Path(tempfile.mkdtemp(prefix=f"mut-{mid}-", dir=a.tmp))
    try:
        for d in ("sqlglot", "tests"):
            shutil.copytree(src / d, work / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("pyproject.toml", "setup.cfg", "setup.py"):
            if (src / f).exists():
                shutil.copy(src / f, work / f)
        if mid != "base":
            path, new = apply_patch_text(src, (out / "commits" / f"{mid}.patch").read_text())
            (work / path).write_text(new)
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{work}:{plugin_dir}", MUT_RESULT=str(res) + ".tmp",
                   PATH=f"{Path(a.python).parent}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1",
                   # tests spawn ProcessPoolExecutors sized to the CPU count; cap them so
                   # parallel mutant runs don't oversubscribe the machine (parallelism only)
                   PYTHON_CPU_COUNT=str(a.pool_cpus))
        p = subprocess.run([a.python, "-m", "pytest", "tests", "-p", "mut_fail_plugin", "-p", "no:cacheprovider",
                            "-n", str(a.workers), "--timeout", str(a.timeout), "-q"],
                           cwd=work, env=env, capture_output=True, text=True, timeout=1800)
        if not Path(str(res) + ".tmp").exists():
            Path(str(res) + ".tmp").write_text(json.dumps(["SESSION_FAILED"]))
            (out / "truth" / f"{mid}.log").write_text(p.stdout[-5000:] + p.stderr[-5000:])
        os.replace(str(res) + ".tmp", res)
        return mid, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "no output"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_truth(a):
    src = Path(a.src).resolve()
    out = Path(a.out).resolve()
    (out / "truth").mkdir(exist_ok=True)
    plugin_dir = out / "plugin"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "mut_fail_plugin.py").write_text(FAIL_PLUGIN)
    mids = ["base"] + sorted(json.loads((out / "meta.json").read_text()))
    if a.first:  # run these first (e.g. the mutants a narrowing change must be safe on)
        pri = [m for m in a.first.split(",") if m in mids]
        mids = ["base"] + pri + [m for m in mids[1:] if m not in pri]
    with ThreadPoolExecutor(a.jobs) as ex:
        for mid, status in ex.map(lambda m: run_truth(m, a, src, out, plugin_dir), mids):
            print(mid, status, flush=True)


# ----------------------------------------------------------------------------- evaluation


def cmd_eval(a):
    out = Path(a.out).resolve()
    tests = json.loads(Path(a.tests).read_text())["tests"]
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["number"])
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    meta = json.loads((out / "meta.json").read_text())
    base = set(json.loads((out / "truth" / "base.json").read_text())) if (out / "truth" / "base.json").exists() else set()

    data = Path(tempfile.mkdtemp())
    (data / "commits").mkdir()
    shutil.copy(a.tests, data / "tests.json")
    for mid in meta:
        if (out / "truth" / f"{mid}.json").exists():
            shutil.copy(out / "commits" / f"{mid}.patch", data / "commits" / f"{mid}.patch")
    env = dict(os.environ, BR_DATA=str(data), BR_SRC=str(Path(a.src).resolve()), BR_MAP=str(Path(a.map).resolve()))
    p = subprocess.run([a.python, a.selector], env=env, capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr)
    sel = {}
    for line in p.stdout.splitlines():
        row = json.loads(line)
        sel[row["commit"]] = (row["workers"], {n for w in row["workers"] for n in w})

    misses, total, n, by_kind = [], 0, 0, {}
    for mid, info in sorted(meta.items()):
        tf = out / "truth" / f"{mid}.json"
        if mid not in sel or not tf.exists():
            continue
        raw = set(json.loads(tf.read_text())) - base
        failing = set()
        for nid in raw:
            if nid.startswith("COLLECT::"):
                failing |= by_file.get(nid[len("COLLECT::"):], set()) or set(ms)
            elif nid == "SESSION_FAILED":
                failing |= set(ms)
            elif nid in num:
                failing.add(num[nid])
        workers, chosen = sel[mid]
        cost = (max(sum(ms[x] for x in w) for w in workers) + 2000 * len(workers)) if workers else 0
        missed = failing - chosen
        n += 1
        total += cost + (300000 if missed else 0)
        k = by_kind.setdefault(info["kind"], [0, 0])
        k[0] += 1
        k[1] += bool(missed)
        flag = "MISS" if missed else "ok  "
        if missed or a.verbose:
            print(f"{flag} {mid} {info['kind']:10} {info['file']:45} fail={len(failing):4} sel={len(chosen):4} cost={cost/1000:6.2f}"
                  + (f" missed={sorted(missed)[:6]}" if missed else ""))
        if missed:
            misses.append(mid)
    print("by kind (n, misses):", {k: tuple(v) for k, v in sorted(by_kind.items())})
    print(f"mutants {n}, missed {len(misses)} ({100 * len(misses) / max(n, 1):.1f}%), "
          f"total {total/1000:.1f}s, avg {total/1000/max(n,1):.2f}s/commit, selector CPU via {a.python}")
    shutil.rmtree(data, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--src", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--n", type=int, default=0)
    t = sub.add_parser("truth")
    t.add_argument("--src", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--python", required=True)
    t.add_argument("--jobs", type=int, default=2)
    t.add_argument("--workers", type=int, default=4)
    t.add_argument("--tmp", default=None)
    t.add_argument("--pool-cpus", type=int, default=2)
    t.add_argument("--first", default="", help="comma-separated mutant ids to run first")
    t.add_argument("--timeout", type=int, default=300, help="per-test pytest timeout; keep load low so only real hangs hit it")
    e = sub.add_parser("eval")
    e.add_argument("--src", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--tests", required=True)
    e.add_argument("--map", default=str(REPO / "map.json.gz"))
    e.add_argument("--python", default=sys.executable)
    e.add_argument("--verbose", action="store_true")
    e.add_argument("--selector", default=str(REPO / "selector.py"))
    a = ap.parse_args()
    {"generate": cmd_generate, "truth": cmd_truth, "eval": cmd_eval}[a.cmd](a)


if __name__ == "__main__":
    main()
