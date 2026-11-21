"""Static reference index over the pinned sqlglot sources (used by build_map.py).

For every identifier it records where it is READ:
  - runtime reads (inside function/lambda bodies that are not import-time lines),
    per file, as source line numbers -> build_map turns them into test sets;
  - import-time reads, with their context:
      ("class", file, class_qualname)               class body
      ("def", file, def_qualname, class_qualname, body_first, body_last)
                                                    decorator / default / (evaluated) annotation
      ("module", file, [names bound by that statement])
      ("body", file)                                function body line that runs at import
and which files import it: (module, name) -> [(file, local_name)], plus star importers.
Annotations are skipped in files with `from __future__ import annotations`.
"""

import ast
import os

ATTR_STR_MAX = 80


def module_of(rel):
    p = rel[:-3].replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


def resolve_from(rel, node):
    if node.level == 0:
        return node.module or ""
    pkg = module_of(rel).split(".")
    if not rel.endswith("__init__.py"):
        pkg = pkg[:-1]
    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
    return ".".join(pkg + ([node.module] if node.module else []))


def bound_names(stmt):
    out = set()
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            out.add((a.asname or a.name).split(".")[0])
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
    return out


class Indexer:
    def __init__(self, imp_lines):
        self.imp_lines = imp_lines  # {rel: set(lines)} lines executed by importing sqlglot
        # kind "n": plain Name reads (only visible where the name is bound/imported)
        # kind "a": attribute/string reads (x.NAME, getattr(x, "NAME")): visible anywhere
        self.rt = {"n": {}, "a": {}}  # kind -> name -> {rel: set(lines)}
        self.imp = {"n": {}, "a": {}}  # kind -> name -> list of contexts
        self.imports = {}  # "module:name" -> [(rel, local)]
        self.star = {}  # module -> [rel]

    def add_rt(self, kind, name, rel, line):
        self.rt[kind].setdefault(name, {}).setdefault(rel, set()).add(line)

    def add_imp(self, kind, name, ctx):
        lst = self.imp[kind].setdefault(name, [])
        if ctx not in lst:
            lst.append(ctx)

    def index_file(self, rel, src):
        tree = ast.parse(src)
        future_ann = any(
            isinstance(s, ast.ImportFrom) and s.module == "__future__" and any(a.name == "annotations" for a in s.names)
            for s in tree.body
        )
        self.rel, self.future_ann = rel, future_ann
        self.file_imp = self.imp_lines.get(rel, set())
        for s in ast.walk(tree):
            if isinstance(s, ast.ImportFrom):
                mod = resolve_from(rel, s)
                for a in s.names:
                    if a.name == "*":
                        self.star.setdefault(mod, []).append(rel)
                    else:
                        self.imports.setdefault(f"{mod}:{a.name}", []).append((rel, a.asname or a.name))
        self.stmts(tree.body, ("module",), None)

    # ---- walking
    def stmts(self, body, ctx, cls):
        for s in body:
            self.stmt(s, ctx, cls)

    def stmt(self, s, ctx, cls):
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{cls}.{s.name}" if cls else s.name
            body_first = s.body[0].lineno if s.body else s.lineno
            hctx = ("def", self.rel, qual, cls, body_first, s.end_lineno) if ctx[0] != "func" else ctx
            for d in s.decorator_list:
                self.expr(d, hctx)
            for d in s.args.defaults + [k for k in s.args.kw_defaults if k is not None]:
                self.expr(d, hctx)
            if not self.future_ann:
                for a in s.args.args + s.args.kwonlyargs + s.args.posonlyargs:
                    if a.annotation is not None:
                        self.expr(a.annotation, hctx)
                if s.returns is not None:
                    self.expr(s.returns, hctx)
            self.stmts(s.body, ("func",), cls)
            return
        if isinstance(s, ast.ClassDef):
            qual = f"{cls}.{s.name}" if (cls and ctx[0] == "class") else s.name
            hctx = ("class", self.rel, qual) if ctx[0] != "func" else ctx
            for e in s.bases + [k.value for k in s.keywords] + s.decorator_list:
                self.expr(e, hctx if ctx[0] != "func" else ctx)
            self.stmts(s.body, ("class", self.rel, qual) if ctx[0] != "func" else ctx, qual if ctx[0] != "func" else cls)
            return
        if ctx[0] == "module":
            sctx = ("module", self.rel, sorted(bound_names(s)))
        elif ctx[0] == "class":
            sctx = ctx
        else:  # function body: import-time only if the line ran during import
            sctx = ("body", self.rel) if s.lineno in self.file_imp else ("func",)
        if isinstance(s, ast.AnnAssign) and self.future_ann and ctx[0] != "func":
            # annotation not evaluated; value is
            if s.value is not None:
                self.expr(s.value, sctx)
            return
        for child in ast.iter_child_nodes(s):
            if isinstance(child, ast.stmt):
                self.stmt(child, ctx if ctx[0] != "module" else ctx, cls)
            elif isinstance(child, (ast.excepthandler,)):
                if child.type is not None:
                    self.expr(child.type, sctx)
                self.stmts(child.body, ctx, cls)
            elif isinstance(child, ast.AST):
                self.expr(child, sctx)

    def expr(self, e, ctx):
        stack = [(e, ctx)]
        while stack:
            n, c = stack.pop()
            if isinstance(n, ast.Lambda):
                for d in n.args.defaults + [k for k in n.args.kw_defaults if k is not None]:
                    stack.append((d, c))
                stack.append((n.body, ("func",)))
                continue
            name = kind = None
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                name, kind = n.id, "n"
            elif isinstance(n, ast.Attribute):
                name, kind = n.attr, "a"
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier() and len(n.value) <= ATTR_STR_MAX:
                name, kind = n.value, "a"
            if name is not None:
                if c[0] == "func":
                    end = getattr(n, "end_lineno", None) or n.lineno
                    for ln in range(n.lineno, end + 1):
                        self.add_rt(kind, name, self.rel, ln)
                else:
                    self.add_imp(kind, name, c)
            for child in ast.iter_child_nodes(n):
                stack.append((child, c))


def build(root, imp_lines):
    idx = Indexer({k: set(v) for k, v in imp_lines.items()})
    for top in ("sqlglot", "tests"):
        for dirpath, _, files in os.walk(os.path.join(root, top)):
            if "__pycache__" in dirpath:
                continue
            for f in files:
                if f.endswith(".py"):
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, root)
                    try:
                        idx.index_file(rel, open(full, encoding="utf-8").read())
                    except SyntaxError:
                        pass
    return idx
