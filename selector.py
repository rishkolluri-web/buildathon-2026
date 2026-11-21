"""Dependency-aware test selector for Blast Radius.

For each patch:
  1. Rebuild the changed files from the pinned sqlglot source plus the patch.
  2. Compare old and new Python ASTs scope by scope (module, class, function,
     lambda) to find what changed in substance. Comments and formatting do not
     change the AST, so they select nothing on their own.
  3. Look the changed scopes up in the per-test map built from the pinned commit
     (tools/build_map.py): which tests entered each function, which tests opened
     each fixture, and which tests entered code that names a given symbol.
  4. Anything we cannot analyse falls back to every test.
  5. Split the selected tests over the worker count that minimises
     busiest worker + 2 s per worker.

The original run-everything selector is kept in baseline/selector_all_tests.py.
"""

from __future__ import annotations

import ast
import difflib
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/app/sqlglot-src"))
MAP = Path(os.environ.get("BR_MAP", "/app/maps/depmap.json.gz"))

MAX_WORKERS = 8
WORKER_COST_MS = 2000
SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini"}
HARMLESS_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg")


class Fallback(Exception):
    """Evidence is incomplete: select every test."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


# ---------------------------------------------------------------- map


class DepMap:
    def __init__(self, path: Path, tests: list[dict]):
        data = json.load(gzip.open(path, "rt"))
        number_by_id = {test["id"]: test["number"] for test in tests}
        # Map indices refer to the map's own test list; translate to numbers.
        self.translate = [number_by_id.get(test_id) for test_id in data["test_ids"]]
        mapped = {number for number in self.translate if number is not None}
        # Tests the map does not know are always selected.
        self.unmapped = {test["number"] for test in tests} - mapped
        self.code: dict[str, set[int]] = {}
        self.by_file: dict[str, set[int]] = {}
        keys = data["code_keys"]
        for key, indices in zip(keys, data["code"]):
            numbers = self._numbers(indices)
            self.code[key] = numbers
            self.by_file.setdefault(key.split("::", 1)[0], set()).update(numbers)
        self.files = {path: self._numbers(v) for path, v in data["files"].items()}
        self.names = {
            name: set().union(*(self.code[keys[i]] for i in indices))
            for name, indices in data["names"].items()
        }
        self.import_code = set(data["import_code"])

    def _numbers(self, indices) -> set[int]:
        return {self.translate[i] for i in indices if self.translate[i] is not None}


# ---------------------------------------------------------------- patches

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[dict]:
    files: list[dict] = []
    current = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = {"old": None, "new": None, "hunks": []}
            files.append(current)
        elif line.startswith("Binary files ") or line.startswith("rename from "):
            raise Fallback("binary or rename")
        elif line.startswith("--- ") and current is not None and not current["hunks"]:
            current["old"] = _strip_path(line[4:])
        elif line.startswith("+++ ") and current is not None and not current["hunks"]:
            current["new"] = _strip_path(line[4:])
        elif line.startswith("@@"):
            match = HUNK.match(line)
            if not match or current is None:
                raise Fallback("bad hunk")
            old_start, old_len = int(match[1]), int(match[2] or 1)
            new_len = int(match[4] or 1)
            body: list[str] = []
            seen_old = seen_new = 0
            i += 1
            while i < len(lines) and (seen_old < old_len or seen_new < new_len):
                piece = lines[i]
                tag = piece[:1]
                if tag == "\\":
                    i += 1
                    continue
                if tag in (" ", ""):
                    seen_old += 1
                    seen_new += 1
                elif tag == "-":
                    seen_old += 1
                elif tag == "+":
                    seen_new += 1
                else:
                    raise Fallback("bad hunk line")
                body.append(piece if piece else " ")
                i += 1
            current["hunks"].append((old_start, body))
            continue
        i += 1
    return files


def _strip_path(raw: str) -> str | None:
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def apply_hunks(old_text: str, hunks: list) -> str:
    old = old_text.split("\n")
    out: list[str] = []
    position = 0  # index into old
    for old_start, body in hunks:
        start = max(old_start - 1, 0) if any(p[0] in " -" for p in body) else old_start
        out.extend(old[position:start])
        position = start
        for piece in body:
            tag, content = piece[0], piece[1:]
            if tag == " ":
                if position >= len(old) or old[position] != content:
                    raise Fallback("context mismatch")
                out.append(content)
                position += 1
            elif tag == "-":
                if position >= len(old) or old[position] != content:
                    raise Fallback("removed line mismatch")
                position += 1
            else:
                out.append(content)
    out.extend(old[position:])
    return "\n".join(out)


# ---------------------------------------------------------------- AST scopes


def _stub(node) -> str:
    if isinstance(node, ast.Lambda):
        return f"lambda({own_dump(node.args)})"
    decorators = [own_dump(d) for d in node.decorator_list]
    if isinstance(node, ast.ClassDef):
        return f"class({node.name},{[own_dump(b) for b in node.bases]},{[own_dump(k) for k in node.keywords]},{decorators})"
    returns = own_dump(node.returns) if node.returns else ""
    return f"def({type(node).__name__},{node.name},{own_dump(node.args)},{decorators},{returns})"


def own_dump(node, top: bool = False) -> str:
    """ast.dump of a node with every nested scope's body left out."""
    if not top and isinstance(node, SCOPE_TYPES):
        return _stub(node)
    parts = [type(node).__name__]
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            parts.append(field + "=[" + ",".join(
                own_dump(v) if isinstance(v, ast.AST) else repr(v) for v in value) + "]")
        elif isinstance(value, ast.AST):
            parts.append(f"{field}={own_dump(value)}")
        else:
            parts.append(f"{field}={value!r}")
    return "(" + " ".join(parts) + ")"


def collect_scopes(tree: ast.Module) -> dict[str, ast.AST]:
    """Scope path -> node, with occurrence numbers so old and new line up."""
    scopes: dict[str, ast.AST] = {"": tree}

    def visit(node, path):
        counts: dict[str, int] = {}

        def walk(child):
            for sub in ast.iter_child_nodes(child):
                if isinstance(sub, SCOPE_TYPES):
                    name = getattr(sub, "name", "<lambda>")
                    counts[name] = counts.get(name, 0) + 1
                    sub_path = f"{path}/{name}#{counts[name]}"
                    scopes[sub_path] = sub
                    visit(sub, sub_path)
                else:
                    walk(sub)

        walk(node)

    visit(tree, "")
    return scopes


def body_of(node) -> list:
    if isinstance(node, ast.Lambda):
        return [node.body]
    return list(node.body)


def changed_statement_names(old_node, new_node) -> set[str]:
    """Names bound by the top-level statements that differ inside a module or class body."""
    old_body = body_of(old_node) if old_node is not None else []
    new_body = body_of(new_node) if new_node is not None else []
    old_dumps = [own_dump(s) for s in old_body]
    new_dumps = [own_dump(s) for s in new_body]
    names: set[str] = set()
    matcher = difflib.SequenceMatcher(a=old_dumps, b=new_dumps, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for statement in old_body[a0:a1] + new_body[b0:b1]:
            names |= bound_names(statement)
    return names


def bound_names(statement) -> set[str]:
    names: set[str] = set()
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(statement.name)
    elif isinstance(statement, (ast.Import, ast.ImportFrom)):
        for alias in statement.names:
            names.add((alias.asname or alias.name).split(".")[0])
    else:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def code_keys_for(relpath: str, source: str, node) -> list[str]:
    """Code-object keys (as in the map) for an AST function or lambda node."""
    first = node.lineno
    if not isinstance(node, ast.Lambda) and node.decorator_list:
        first = min(first, min(d.lineno for d in node.decorator_list))
    name = "<lambda>" if isinstance(node, ast.Lambda) else node.name
    keys = []
    for code in _code_objects(relpath, source):
        if code.co_name == name and first <= code.co_firstlineno <= node.lineno:
            keys.append(f"{relpath}::{code.co_qualname}::{code.co_firstlineno}")
    return keys


_CODE_CACHE: dict[str, list] = {}


def _code_objects(relpath: str, source: str) -> list:
    if relpath not in _CODE_CACHE:
        found = []
        stack = [compile(source, relpath, "exec", dont_inherit=True)]
        while stack:
            code = stack.pop()
            found.append(code)
            stack.extend(c for c in code.co_consts if hasattr(c, "co_code"))
        _CODE_CACHE[relpath] = found
    return _CODE_CACHE[relpath]


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, depmap: DepMap, tests: list[dict]):
        self.map = depmap
        self.all = {test["number"] for test in tests}
        self.ids = {test["number"]: test["id"] for test in tests}

    def select(self, patch_text: str) -> tuple[set[int], list[str]]:
        selected: set[int] = set()
        reasons: list[str] = []
        for file_patch in parse_patch(patch_text):
            path = file_patch["old"] or file_patch["new"]
            if path is None:
                raise Fallback("no path")
            got = self.select_file(file_patch)
            reasons.append(f"{path}:{len(got)}")
            selected |= got
        return selected | self.map.unmapped, reasons

    def select_file(self, file_patch: dict) -> set[int]:
        old_path, new_path = file_patch["old"], file_patch["new"]
        path = old_path or new_path
        name = path.rsplit("/", 1)[-1]
        if old_path is None:  # a new file: reached only through other changes
            if name == "conftest.py":
                raise Fallback("new conftest")
            return set()
        if new_path is None or old_path != new_path:
            raise Fallback("deleted or moved file")
        if not path.endswith(".py"):
            if path.startswith("tests/"):
                return set(self.map.files.get(path, set()))
            if name in CONFIG_FILES:
                raise Fallback("config file")
            if path.startswith("sqlglot/") or not path.endswith(HARMLESS_SUFFIXES):
                raise Fallback(f"unknown file {path}")
            return set()
        if name == "conftest.py":
            raise Fallback("conftest")
        if not (path.startswith("sqlglot/") or path.startswith("tests/")):
            return set() if name not in CONFIG_FILES else self._fallback("config")
        old_source = (SRC / path).read_text(encoding="utf-8")
        new_source = apply_hunks(old_source, file_patch["hunks"])
        return self.select_python(path, old_source, new_source)

    def _fallback(self, why: str):
        raise Fallback(why)

    def select_python(self, path: str, old_source: str, new_source: str) -> set[int]:
        try:
            new_tree = ast.parse(new_source)
        except SyntaxError:
            raise Fallback(f"syntax error in {path}")
        old_tree = ast.parse(old_source)
        old_scopes = collect_scopes(old_tree)
        new_scopes = collect_scopes(new_tree)
        selected: set[int] = set()
        for scope_path in set(old_scopes) | set(new_scopes):
            old_node = old_scopes.get(scope_path)
            new_node = new_scopes.get(scope_path)
            if old_node is not None and new_node is not None:
                if own_dump(old_node, top=True) == own_dump(new_node, top=True):
                    continue
            if old_node is None:
                continue  # added scope: its parent's body changed and covers it
            if isinstance(old_node, FUNCTION_TYPES):
                keys = code_keys_for(path, old_source, old_node)
                for key in keys:
                    selected |= self.map.code.get(key, set())
                if not keys or any(key in self.map.import_code for key in keys):
                    selected |= self.definition_change(path, scope_path, old_scopes, old_node, new_node)
            else:
                selected |= self.definition_change(path, scope_path, old_scopes, old_node, new_node)
        return selected

    def definition_change(self, path, scope_path, old_scopes, old_node, new_node) -> set[int]:
        """A change that takes effect when the module is imported (class or module body)."""
        selected = set(self.map.by_file.get(path, set()))
        names: set[str] = set()
        # Every enclosing class name: code that names the class uses it.
        parts = scope_path.split("/")[1:]
        for i in range(1, len(parts) + 1):
            node = old_scopes.get("/" + "/".join(parts[:i]))
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
        if isinstance(old_node, ast.Module):
            names |= changed_statement_names(old_node, new_node)
        for name in names:
            selected |= self.map.names.get(name, set())
        if path.startswith("tests/") and isinstance(old_node, (ast.Module, ast.ClassDef)):
            prefix = path + "::"
            selected |= {n for n, test_id in self.ids.items() if test_id.startswith(prefix)}
        return selected


# ---------------------------------------------------------------- scheduling


def schedule(numbers: set[int], duration_ms: dict[int, int]) -> list[list[int]]:
    """Pick the worker count and greedy split minimising busiest worker + 2 s each."""
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda n: (-duration_ms[n], n))
    best = None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += duration_ms[number]
        cost = max(loads) + WORKER_COST_MS * count
        if best is None or cost < best[0]:
            best = (cost, [w for w in workers if w])
    return best[1]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    every = {t["number"] for t in tests}
    try:
        selector = Selector(DepMap(MAP, tests), tests)
    except Exception as error:  # noqa: BLE001
        log(f"map unavailable ({error!r}); selecting every test")
        selector = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            if selector is None:
                raise Fallback("no map")
            chosen, reasons = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # noqa: BLE001 - any doubt means run everything
            chosen, reasons = set(every), [f"fallback: {error!r}"]
        workers = schedule(chosen, duration_ms)
        print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
        log(f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers; {' '.join(reasons)}")


if __name__ == "__main__":
    main()
