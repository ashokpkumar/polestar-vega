"""Reads a FastAPI app and turns it into a flow spec (plain dict / JSON).

Two sources are combined:
  * runtime introspection - what FastAPI itself knows: middleware stack, routes,
    dependencies, parameters, Pydantic models
  * static analysis (ast) - what the code does: which functions a handler calls,
    where data lands (DB / HTTP calls), which literal values can be edited
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import inspect
import os
import sys
import typing
from pathlib import Path

SPEC_VERSION = 2
MAX_DEPTH = 6

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"}
PARAM_CALLS = {"Query", "Path", "Body", "Header", "Cookie", "Field", "Form", "File"}
SKIP_KEYWORDS = {
    "description", "title", "alias", "example", "examples", "summary", "deprecated",
    "include_in_schema", "tags", "name", "response_description", "operation_id",
}

SINK_MODULES = {
    "sqlalchemy": "db", "sqlite3": "db", "psycopg2": "db", "psycopg": "db", "asyncpg": "db",
    "pymongo": "db", "motor": "db", "pyodbc": "db", "pymssql": "db", "aiosqlite": "db",
    "databases": "db", "tortoise": "db", "peewee": "db", "sqlmodel": "db",
    "redis": "cache", "aioredis": "cache", "pymemcache": "cache",
    "requests": "http", "httpx": "http", "aiohttp": "http", "urllib": "http",
    "boto3": "cloud", "botocore": "cloud", "azure": "cloud", "google": "cloud",
    "smtplib": "email", "celery": "queue", "pika": "queue", "kafka": "queue",
}
DB_HOLDERS = {"db", "session", "conn", "connection", "cursor", "cur", "engine", "collection", "database"}
DB_METHODS = {
    "execute", "executemany", "add", "add_all", "commit", "query", "get", "delete", "merge",
    "flush", "insert_one", "insert_many", "find", "find_one", "update_one", "update_many",
    "delete_one", "scalar", "scalars", "fetchall", "fetchone", "fetch", "fetchrow", "refresh",
}
HTTP_HOLDERS = {"requests", "httpx", "client", "http", "http_client", "aiohttp"}
HTTP_METHOD_NAMES = {"get", "post", "put", "patch", "delete", "request", "send"}
CACHE_HOLDERS = {"redis", "cache", "memcache"}

BUILTIN_MIDDLEWARE = {
    "CORSMiddleware": "Adds CORS headers so browsers on allowed origins can call the API.",
    "GZipMiddleware": "Compresses responses larger than the minimum size.",
    "TrustedHostMiddleware": "Rejects requests whose Host header is not on the allowed list.",
    "HTTPSRedirectMiddleware": "Redirects every http request to https.",
    "SessionMiddleware": "Keeps a signed cookie-based session for each client.",
}


# --------------------------------------------------------------------------- helpers
def load_app(target: str, root: str | os.PathLike = "."):
    """Import ``package.module:attribute`` and return the FastAPI app object."""
    root = str(Path(root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    if ":" not in target:
        raise ValueError("App must be given as 'package.module:app'")
    mod_name, attr = target.split(":", 1)
    module = importlib.import_module(mod_name)
    app = getattr(module, attr)
    if callable(app) and not hasattr(app, "routes"):
        app = app()  # app factory
    return app


def _is_literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float, str, bool)) and not isinstance(node.value, bytes)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)
    if isinstance(node, (ast.List, ast.Tuple)):
        return len(node.elts) <= 12 and all(
            _is_literal(e) and not isinstance(e, (ast.List, ast.Tuple)) for e in node.elts
        )
    return False


def _type_name(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "list"


def _tname(t) -> str:
    if t is None or t is inspect.Parameter.empty:
        return ""
    if isinstance(t, type) and not typing.get_args(t):
        return t.__name__
    import re

    return re.sub(r"\b[\w.]+\.(\w+)", r"\1", str(t)).replace("NoneType", "None")


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class _File:
    """Parsed source file with lookup tables."""

    def __init__(self, path: str):
        self.path = path
        self.source = Path(path).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.by_line: dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                self.by_line.setdefault(first, node)
                self.by_line.setdefault(node.lineno, node)
        self.constants: dict[str, ast.AST] = {}
        self.imports: dict[str, tuple[str, int, str]] = {}
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                if _is_literal(stmt.value):
                    self.constants[stmt.targets[0].id] = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                if _is_literal(stmt.value):
                    self.constants[stmt.target.id] = stmt.value
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    self.imports[alias.asname or alias.name] = (stmt.module or "", stmt.level, alias.name)

    def segment(self, node) -> str:
        return ast.get_source_segment(self.source, node) or ""


# --------------------------------------------------------------------------- extractor
class Extractor:
    def __init__(self, app, target: str, root: str | os.PathLike = ".", previous: dict | None = None):
        self.app = app
        self.target = target
        self.root = Path(root).resolve()
        self.blocks: dict[str, dict] = {}
        self.files: dict[str, _File | None] = {}
        self.previous = (previous or {}).get("blocks", {})
        self._stack: list[str] = []

    # ---- files / source -------------------------------------------------
    def in_project(self, path: str | None) -> bool:
        if not path:
            return False
        try:
            rel = Path(path).resolve().relative_to(self.root)
        except ValueError:
            return False
        return not any(p in ("site-packages", ".venv", "venv", "node_modules") for p in rel.parts)

    def rel(self, path: str) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def file(self, path: str) -> _File | None:
        path = str(Path(path).resolve())
        if path not in self.files:
            try:
                self.files[path] = _File(path)
            except (OSError, SyntaxError, UnicodeDecodeError):
                self.files[path] = None
        return self.files[path]

    def locate(self, obj):
        """Return (file, ast node) for a function or class defined in the project."""
        try:
            obj = inspect.unwrap(obj)
            path = inspect.getsourcefile(obj)
            if not self.in_project(path):
                return None, None
            _, start = inspect.getsourcelines(obj)
        except (TypeError, OSError):
            return None, None
        f = self.file(path)
        if f is None:
            return None, None
        return f, f.by_line.get(start)

    # ---- editable values ------------------------------------------------
    def _editable(self, out: list, f: _File, label: str, group: str, node) -> None:
        try:
            value = ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return
        if isinstance(value, tuple):
            value = list(value)
        labels = {e["label"] for e in out}
        if label in labels:
            label = f"{label} (line {node.lineno})"
            if label in labels:
                return
        out.append({
            "label": label,
            "group": group,
            "value": value,
            "type": _type_name(value),
            "file": self.rel(f.path),
            "line": node.lineno,
            "col": node.col_offset,
            "end_line": node.end_lineno,
            "end_col": node.end_col_offset,
            "text": f.segment(node),
        })

    def _call_literals(self, out, f, call: ast.Call, prefix: str, group: str) -> None:
        fname = (_dotted(call.func) or "").split(".")[-1]
        for i, arg in enumerate(call.args):
            if _is_literal(arg):
                name = "default" if (i == 0 and fname in PARAM_CALLS) else f"arg{i + 1}"
                self._editable(out, f, f"{prefix} {name}", group, arg)
        for kw in call.keywords:
            if kw.arg and kw.arg not in SKIP_KEYWORDS and _is_literal(kw.value):
                self._editable(out, f, f"{prefix} {kw.arg}", group, kw.value)

    def _param_editables(self, out, f, fn, group="param") -> None:
        args = fn.args
        pos = args.posonlyargs + args.args
        pairs = list(zip(pos[len(pos) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for a in pos + args.kwonlyargs:
            if a.annotation is not None:
                for sub in ast.walk(a.annotation):
                    if isinstance(sub, ast.Call):
                        self._call_literals(out, f, sub, a.arg, group)
        for a, default in pairs:
            if _is_literal(default):
                self._editable(out, f, f"{a.arg} default", group, default)
            elif isinstance(default, ast.Call):
                self._call_literals(out, f, default, a.arg, group)

    def _body_editables(self, out, f, fn, runtime=None) -> None:
        # numbers used in comparisons, e.g.  if len(x) >= 5
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare):
                for side in [node.left] + node.comparators:
                    if _is_literal(side) and not isinstance(side, (ast.List, ast.Tuple)):
                        value = ast.literal_eval(side)
                        if type(value) in (int, float):
                            self._editable(out, f, f"check: {_short(f.segment(node), 48)}", "inline", side)
        # module-level constants the code refers to, e.g. MAX_ITEMS
        seen = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in seen:
                seen.add(node.id)
                self._constant(out, f, node.id, runtime)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and runtime is not None:
                mod = getattr(runtime, "__globals__", {}).get(node.value.id)
                if inspect.ismodule(mod) and self.in_project(getattr(mod, "__file__", None)):
                    mf = self.file(mod.__file__)
                    if mf and node.attr in mf.constants and (node.value.id, node.attr) not in seen:
                        seen.add((node.value.id, node.attr))
                        self._editable(out, mf, node.attr, "constant", mf.constants[node.attr])

    def _constant(self, out, f: _File, name: str, runtime) -> None:
        if name in f.constants:
            self._editable(out, f, name, "constant", f.constants[name])
            return
        if name in f.imports and runtime is not None:
            module, level, original = f.imports[name]
            try:
                package = sys.modules[runtime.__module__].__package__
                full = importlib.util.resolve_name("." * level + module, package) if level else module
                mod_file = getattr(sys.modules.get(full), "__file__", None)
            except (KeyError, ImportError, ValueError, AttributeError):
                return
            if self.in_project(mod_file):
                mf = self.file(mod_file)
                if mf and original in mf.constants:
                    self._editable(out, mf, name, "constant", mf.constants[original])

    def _function_editables(self, f, fn, runtime=None) -> list:
        out: list = []
        self._param_editables(out, f, fn)
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call):
                name = _dotted(dec.func) or "decorator"
                if name.split(".")[-1] in ("middleware", "on_event", "exception_handler"):
                    continue
                is_route = name.split(".")[-1] in HTTP_METHODS
                for i, arg in enumerate(dec.args):
                    if _is_literal(arg):
                        label = "path" if (is_route and i == 0) else f"arg{i + 1}"
                        self._editable(out, f, f"@{name} {label}", "decorator", arg)
                for kw in dec.keywords:
                    if kw.arg and kw.arg not in SKIP_KEYWORDS and _is_literal(kw.value):
                        self._editable(out, f, f"@{name} {kw.arg}", "decorator", kw.value)
        self._body_editables(out, f, fn, runtime)
        return out

    def _class_editables(self, f, cls_node, runtime_cls=None) -> list:
        out: list = []
        for stmt in cls_node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                for sub in ast.walk(stmt.annotation):
                    if isinstance(sub, ast.Call):
                        self._call_literals(out, f, sub, name, "field")
                if stmt.value is not None:
                    if _is_literal(stmt.value):
                        self._editable(out, f, f"{name} default", "field", stmt.value)
                    elif isinstance(stmt.value, ast.Call):
                        self._call_literals(out, f, stmt.value, name, "field")
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt.name == "__init__":
                    self._param_editables(out, f, stmt)
                method = getattr(runtime_cls, stmt.name, None) if runtime_cls else None
                self._body_editables(out, f, stmt, inspect.unwrap(method) if callable(method) else None)
        return out

    # ---- effects: what a block visibly does to the request/response ------
    def _effects(self, f, node) -> list:
        effects, seen = [], set()

        def add(kind, text, line):
            if text not in seen:
                seen.add(text)
                effects.append({"type": kind, "text": text, "line": line})

        def lit(n):
            return ast.literal_eval(n) if n is not None and _is_literal(n) else None

        for n in ast.walk(node):
            if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
                name = (_dotted(n.exc.func) or "").split(".")[-1]
                if name.endswith("HTTPException"):
                    kws = {k.arg: k.value for k in n.exc.keywords}
                    status = lit(kws.get("status_code") or (n.exc.args[0] if n.exc.args else None))
                    detail = lit(kws.get("detail") or (n.exc.args[1] if len(n.exc.args) > 1 else None))
                    add("reject", f"Can reject with {status or 'an error'}" + (f": {detail}" if detail else ""), n.lineno)
                elif name:
                    add("raise", f"Can raise {name}", n.lineno)
            elif isinstance(n, ast.Return) and isinstance(n.value, ast.Call):
                for k in n.value.keywords:
                    status = lit(k.value) if k.arg == "status_code" else None
                    if isinstance(status, int) and status >= 300:
                        add("reject", f"Can stop the request early with status {status}", n.lineno)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) and t.value.attr == "headers":
                        key = lit(t.slice)
                        owner = (_dotted(t.value.value) or "").lower()
                        which = "request" if "request" in owner else "response"
                        add("transform", f"Sets {which} header {key or '(dynamic)'}", n.lineno)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get":
                base = n.func.value
                if isinstance(base, ast.Attribute) and base.attr in ("headers", "cookies", "query_params") and n.args:
                    key = lit(n.args[0])
                    if key:
                        add("read", f"Reads {base.attr[:-1].replace('query_param', 'query parameter')} {key}", n.lineno)
            elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
                if isinstance(n.value, ast.Attribute) and n.value.attr in ("headers", "cookies"):
                    key = lit(n.slice)
                    if key:
                        add("read", f"Reads {n.value.attr[:-1]} {key}", n.lineno)
        return effects

    # ---- blocks ----------------------------------------------------------
    def _finish(self, block: dict, f: _File | None, node, doc: str | None) -> dict:
        if f is not None and node is not None:
            first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            block.update(file=self.rel(f.path), line=first, end_line=node.end_lineno)
            src = "\n".join(f.source.splitlines()[first - 1: node.end_lineno])
            block["hash"] = hashlib.sha1(src.encode()).hexdigest()[:10]
        summary, source = "", "none"
        if doc:
            summary, source = inspect.cleandoc(doc).split("\n\n")[0].replace("\n", " "), "docstring"
        old = self.previous.get(block["id"])
        if old and old.get("summary_source") in ("ai", "manual") and old.get("hash") == block.get("hash"):
            summary, source = old.get("summary", ""), old["summary_source"]
        block.setdefault("summary", summary)
        block.setdefault("summary_source", source)
        for e in block.get("editables", []):
            e["id"] = f"{block['id']}#{e['label']}"
        return block

    def function_block(self, func, owner_cls=None, depth=0) -> str | None:
        func = inspect.unwrap(func)
        if inspect.ismethod(func):
            func = func.__func__
        f, node = self.locate(func)
        if node is None or isinstance(node, ast.ClassDef):
            return None
        bid = f"fn:{self.rel(f.path)}:{func.__qualname__}"
        if bid in self.blocks:
            return bid
        block = {
            "id": bid,
            "kind": "function",
            "name": func.__qualname__,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "editables": self._function_editables(f, node, func),
            "effects": self._effects(f, node),
            "calls": [],
        }
        self.blocks[bid] = block
        self._finish(block, f, node, func.__doc__)
        block["calls"] = self._calls(func, f, node, owner_cls, bid, depth)
        return bid

    def model_block(self, model) -> str | None:
        f, node = self.locate(model)
        where = self.rel(f.path) if f else model.__module__
        bid = f"model:{where}:{model.__qualname__}"
        if bid in self.blocks:
            return bid
        fields = []
        for name, info in getattr(model, "model_fields", {}).items():
            required = info.is_required() if hasattr(info, "is_required") else False
            fields.append({
                "name": name,
                "type": _tname(info.annotation),
                "required": required,
                "default": None if required else _short(repr(info.default), 40),
                "constraints": [str(m) for m in getattr(info, "metadata", [])],
            })
        block = {
            "id": bid,
            "kind": "model",
            "name": model.__name__,
            "fields": fields,
            "editables": self._class_editables(f, node, model) if isinstance(node, ast.ClassDef) else [],
            "effects": [],
            "calls": [],
        }
        self.blocks[bid] = block
        doc = model.__doc__ if "__doc__" in vars(model) else None
        self._finish(block, f, node, doc)
        return bid

    # ---- call tree -------------------------------------------------------
    def _owner_class(self, func):
        parts = func.__qualname__.split(".")
        if len(parts) < 2 or "<locals>" in parts:
            return None
        obj = sys.modules.get(func.__module__)
        for p in parts[:-1]:
            obj = getattr(obj, p, None)
        return obj if inspect.isclass(obj) else None

    def _calls(self, func, f: _File, node, owner_cls, parent_id: str, depth: int) -> list:
        if depth >= MAX_DEPTH or parent_id in self._stack:
            return []
        self._stack.append(parent_id)
        try:
            return self._calls_inner(func, f, node, owner_cls, parent_id, depth)
        finally:
            self._stack.pop()

    def _calls_inner(self, func, f, node, owner_cls, parent_id, depth) -> list:
        env = dict(getattr(func, "__globals__", {}))
        try:
            env.update(inspect.getclosurevars(func).nonlocals)
        except (TypeError, ValueError):
            pass
        local_types: dict[str, type] = {}
        try:
            for name, hint in typing.get_type_hints(func).items():
                if inspect.isclass(hint):
                    local_types[name] = hint
        except Exception:
            pass
        owner_cls = owner_cls or self._owner_class(func)
        if owner_cls is not None:
            local_types.setdefault("self", owner_cls)
            local_types.setdefault("cls", owner_cls)

        def resolve(expr):
            """-> (object or None, owning class or None)"""
            if isinstance(expr, ast.Name):
                if expr.id in local_types:
                    return None, None
                return env.get(expr.id), None
            dotted = _dotted(expr)
            if not dotted:
                return None, None
            parts = dotted.split(".")
            if parts[0] in local_types:
                obj, owner = local_types[parts[0]], local_types[parts[0]]
            elif parts[0] in env:
                obj, owner = env[parts[0]], None
            else:
                return None, None
            for p in parts[1:]:
                try:
                    obj = getattr(obj, p)
                except Exception:
                    return None, None
                if inspect.isclass(obj):
                    owner = obj
            return obj, owner

        events = [n for n in ast.walk(node) if isinstance(n, (ast.Assign, ast.Call))]
        events.sort(key=lambda n: (n.lineno, n.col_offset, isinstance(n, ast.Call)))
        out, seen_sinks = [], set()
        sites = self.blocks[parent_id].setdefault("call_sites", {})

        def note_site(child_id: str, call: ast.Call) -> None:
            """Remember which top-level statement of this function makes the call (for drag and drop)."""
            if child_id in sites:
                return
            for st in getattr(node, "body", []):
                if st.lineno <= call.lineno <= st.end_lineno:
                    arg0 = ""
                    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                        arg0 = call.args[0].value
                    sites[child_id] = {
                        "line": st.lineno, "end_line": st.end_lineno,
                        "movable": isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)),
                        "text": _dotted(call.func) or "", "arg0": arg0,
                    }
                    return
        for n in events:
            if isinstance(n, ast.Assign):
                value = n.value.value if isinstance(n.value, ast.Await) else n.value
                if len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(value, ast.Call):
                    obj, _ = resolve(value.func)
                    if inspect.isclass(obj):
                        local_types[n.targets[0].id] = obj
                    elif inspect.isfunction(obj):
                        try:
                            ret = typing.get_type_hints(obj).get("return")
                        except Exception:
                            ret = None
                        if inspect.isclass(ret):
                            local_types[n.targets[0].id] = ret
                continue
            obj, owner = resolve(n.func)
            text = _dotted(n.func)
            if inspect.isfunction(obj) or inspect.ismethod(obj):
                child = self.function_block(obj, owner, depth + 1)
                if child:
                    if child not in out and child != parent_id:
                        out.append(child)
                        note_site(child, n)
                    continue
            if inspect.isclass(obj) or text is None:
                continue
            sink = self._sink_type(obj, text)
            if sink:
                detail = ""
                if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    detail = _short(n.args[0].value, 80)
                key = (text, detail)
                if key in seen_sinks:
                    continue
                seen_sinks.add(key)
                sid = f"sink:{parent_id}:{text}:{len(seen_sinks)}"
                self.blocks[sid] = {
                    "id": sid, "kind": "sink", "sink_type": sink, "name": text, "summary": detail,
                    "summary_source": "code" if detail else "none",
                    "file": self.rel(f.path), "line": n.lineno, "end_line": n.end_lineno,
                    "editables": [], "effects": [], "calls": [],
                }
                out.append(sid)
                note_site(sid, n)
        return out

    @staticmethod
    def _sink_type(obj, text: str) -> str | None:
        if obj is open:
            return "file"
        if obj is not None:
            mod = getattr(obj, "__module__", None) or type(obj).__module__ or ""
            top = mod.split(".")[0].lstrip("_")
            if top in SINK_MODULES:
                return SINK_MODULES[top]
            self_obj = getattr(obj, "__self__", None)
            if self_obj is not None:
                top = (type(self_obj).__module__ or "").split(".")[0].lstrip("_")
                if top in SINK_MODULES:
                    return SINK_MODULES[top]
        parts = [p.lstrip("_").lower() for p in text.split(".")]
        holders, method = set(parts[:-1]), parts[-1]
        if holders & DB_HOLDERS and method in DB_METHODS:
            return "db"
        if holders & HTTP_HOLDERS and method in HTTP_METHOD_NAMES:
            return "http"
        if holders & CACHE_HOLDERS:
            return "cache"
        return None

    # ---- middleware --------------------------------------------------------
    def _add_middleware_call(self, cls_name: str):
        for mod in list(sys.modules.values()):
            path = getattr(mod, "__file__", None)
            if not self.in_project(path):
                continue
            f = self.file(path)
            if f is None:
                continue
            for stmt in ast.walk(f.tree):
                n = stmt.value if isinstance(stmt, ast.Expr) else None
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "add_middleware" and n.args
                        and (_dotted(n.args[0]) or "").split(".")[-1] == cls_name):
                    return f, n
        return None, None

    def middleware_block(self, mw, index: int) -> str:
        cls = mw.cls
        kwargs = dict(getattr(mw, "kwargs", {}) or {})
        dispatch = kwargs.get("dispatch")
        if cls.__name__ == "BaseHTTPMiddleware" and callable(dispatch):
            bid = self.function_block(dispatch)
            if bid:
                self.blocks[bid]["kind"] = "middleware"
                df, dnode = self.locate(dispatch)
                for d in getattr(dnode, "decorator_list", []):
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "middleware":
                        self.blocks[bid]["site"] = {"file": self.rel(df.path), "line": d.lineno,
                                                    "end_line": d.end_lineno, "kind": "decorator"}
                return bid
        f, node = self.locate(cls)
        where = self.rel(f.path) if f else cls.__module__
        bid = f"mw:{where}:{cls.__qualname__}"
        if bid in self.blocks:
            return bid
        editables: list = []
        cf, call = self._add_middleware_call(cls.__name__)
        if call is not None:
            for kw in call.keywords:
                if kw.arg and _is_literal(kw.value):
                    self._editable(editables, cf, kw.arg, "config", kw.value)
        block = {
            "id": bid, "kind": "middleware", "name": cls.__name__,
            "config": {k: _short(repr(v), 60) for k, v in kwargs.items()},
            "editables": editables, "effects": [], "calls": [],
        }
        if call is not None:
            block["site"] = {"file": self.rel(cf.path), "line": call.lineno, "end_line": call.end_lineno, "kind": "call"}
        self.blocks[bid] = block
        if isinstance(node, ast.ClassDef):
            block["editables"] += [e for e in self._class_editables(f, node, cls)
                                   if e["label"] not in {x["label"] for x in editables}]
            block["effects"] = self._effects(f, node)
            doc = cls.__doc__ if "__doc__" in vars(cls) else None
            self._finish(block, f, node, doc)
            entry = getattr(cls, "dispatch", None) or getattr(cls, "__call__", None)
            if inspect.isfunction(entry):
                ef, enode = self.locate(entry)
                if enode is not None:
                    block["calls"] = self._calls(entry, ef, enode, cls, bid, 0)
        else:
            block["external"] = True
            self._finish(block, None, None, None)
            if cls.__name__ in BUILTIN_MIDDLEWARE:
                block["summary"], block["summary_source"] = BUILTIN_MIDDLEWARE[cls.__name__], "builtin"
            if call is not None:
                block.update(file=self.rel(cf.path), line=call.lineno, end_line=call.end_lineno)
        return bid

    # ---- routes ------------------------------------------------------------
    @staticmethod
    def _params(dependant) -> list:
        out = []
        for location in ("path", "query", "header", "cookie", "body"):
            for p in getattr(dependant, f"{location}_params", []) or []:
                info = getattr(p, "field_info", None)
                required = bool(info.is_required()) if info is not None and hasattr(info, "is_required") else False
                out.append({
                    "name": getattr(p, "alias", None) or p.name,
                    "in": location,
                    "type": _tname(getattr(info, "annotation", None)),
                    "required": required,
                    "default": None if required else _short(repr(getattr(info, "default", None)), 40),
                    "constraints": [str(m) for m in getattr(info, "metadata", [])],
                })
        return out

    @staticmethod
    def _models_in(tp) -> list:
        try:
            from pydantic import BaseModel
        except ImportError:  # pragma: no cover
            return []
        found, todo = [], [tp]
        while todo:
            t = todo.pop()
            if inspect.isclass(t) and issubclass(t, BaseModel):
                if t not in found:
                    found.append(t)
            else:
                todo.extend(typing.get_args(t))
        return found

    def _dependency_steps(self, dependant, steps: list, top: bool = True) -> None:
        for dep in getattr(dependant, "dependencies", []) or []:
            self._dependency_steps(dep, steps, False)
            call = dep.call
            is_security = type(call).__module__.startswith("fastapi.security")
            target = call if inspect.isfunction(call) or inspect.ismethod(call) else getattr(type(call), "__call__", None)
            bid = self.function_block(target) if target and not is_security else None
            if bid is None:
                name = getattr(call, "__name__", None) or type(call).__name__
                bid = f"dep:{getattr(call, '__module__', '') or type(call).__module__}:{name}"
                self.blocks.setdefault(bid, {
                    "id": bid, "kind": "function", "name": name, "external": True,
                    "summary": "Security scheme supplied by FastAPI." if is_security else "",
                    "summary_source": "builtin" if is_security else "none",
                    "editables": [], "effects": [], "calls": [],
                })
            params = self._params(dep)
            if params:
                self.blocks[bid]["params"] = params
            role = "security" if is_security else "dependency"
            # where it is declared: the route decorator, a handler parameter, or inside another dependency
            where = {"source": ("param" if dep.name else "decorator") if top else "nested"}
            if where["source"] == "param":
                where["param"] = dep.name
            existing = next((s for s in steps if s["block"] == bid), None)
            if existing is None:
                steps.append({"block": bid, "role": role, "label": dep.name or "", **where})
            elif existing.get("source") == "nested" and top:
                existing.update(where)

    def route(self, route, middleware_ids: list) -> list:
        endpoint = getattr(route, "endpoint", None)
        dependant = getattr(route, "dependant", None)
        if endpoint is None or dependant is None:
            return []
        out = []
        methods = sorted(getattr(route, "methods", None) or ["WS"])
        handler_id = self.function_block(endpoint)
        decorator = None
        hf, hnode = self.locate(endpoint)
        for d in getattr(hnode, "decorator_list", []):
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP_METHODS:
                decorator = {"file": self.rel(hf.path), "line": d.lineno, "end_line": d.end_lineno}
        for method in methods:
            if method == "HEAD" and "GET" in methods:
                continue
            rid = f"{method} {route.path}"
            steps = [{"block": m, "role": "middleware", "label": ""} for m in middleware_ids]
            self._dependency_steps(dependant, steps)
            params = self._params(dependant)
            if params:
                vid = f"validation:{rid}"
                hb = self.blocks.get(handler_id, {})
                editables = []
                for e in hb.get("editables", []):
                    if e["group"] == "param":
                        e2 = dict(e)
                        e2["id"] = f"{vid}#{e['label']}"
                        editables.append(e2)
                self.blocks[vid] = {
                    "id": vid, "kind": "validation", "name": "Validate inputs",
                    "summary": "FastAPI checks these inputs and returns 422 if any is invalid.",
                    "summary_source": "builtin", "params": params, "editables": editables,
                    "effects": [{"type": "reject", "text": "Can reject with 422: validation error", "line": hb.get("line")}],
                    "calls": [], "file": hb.get("file"), "line": hb.get("line"), "end_line": hb.get("end_line"),
                }
                steps.append({"block": vid, "role": "validation", "label": ""})
                for p in getattr(dependant, "body_params", []) or []:
                    for model in self._models_in(getattr(p.field_info, "annotation", None)):
                        steps.append({"block": self.model_block(model), "role": "body", "label": p.name})
            if handler_id:
                steps.append({"block": handler_id, "role": "handler", "label": ""})
            response_model = getattr(route, "response_model", None)
            models = self._models_in(response_model) if response_model is not None else []
            for model in models:
                steps.append({"block": self.model_block(model), "role": "response", "label": _tname(response_model)})
            out.append({
                "id": rid,
                "method": method,
                "path": route.path,
                "name": getattr(route, "name", "") or "",
                "summary": getattr(route, "summary", None) or "",
                "tags": [str(t) for t in (getattr(route, "tags", None) or [])],
                "status_code": getattr(route, "status_code", None),
                "response_type": _tname(response_model) if response_model is not None else "",
                "handler": handler_id,
                "decorator": decorator,
                "steps": steps,
            })
        return out

    def run(self) -> dict:
        middleware_ids = [self.middleware_block(mw, i) for i, mw in enumerate(getattr(self.app, "user_middleware", []))]
        routes = []
        for r in self._walk_routes(self.app.routes):
            routes.extend(self.route(r, middleware_ids))
        # handler params are shown on the validation block, not twice
        handler_ids = {s["block"] for r in routes for s in r["steps"] if s["role"] == "handler"}
        for hid in handler_ids:
            b = self.blocks[hid]
            b["editables"] = [e for e in b["editables"] if e["group"] != "param"]
        return {
            "flowmap_version": SPEC_VERSION,
            "app": self.target,
            "title": getattr(self.app, "title", "") or "",
            "middleware": middleware_ids,
            "routes": routes,
            "blocks": self.blocks,
        }

    def _walk_routes(self, routes):
        for r in routes:
            if getattr(r, "dependant", None) is not None and getattr(r, "include_in_schema", True):
                yield r


def extract(target: str, root: str | os.PathLike = ".", previous: dict | None = None) -> dict:
    """Build the flow spec for ``package.module:app``."""
    app = load_app(target, root)
    return Extractor(app, target, root, previous).run()


def all_editables(spec: dict) -> dict:
    """Map editable id -> editable for the whole spec."""
    return {e["id"]: e for b in spec["blocks"].values() for e in b.get("editables", [])}
