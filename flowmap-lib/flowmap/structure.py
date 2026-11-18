"""Structural edits made in the diagram: reorder, remove and add blocks.

Like ``edit.py`` this works by exact text surgery on the spans the Python ``ast`` reports, so formatting and
comments outside the touched lines are left alone, and no AI is involved: the same edit always produces the
same code.

A batch of operations is turned into the same ``{relative file: (old source, new source)}`` plan that
``edit.make_diff`` and ``edit.write_plan`` already understand. Every plan is then *verified*: it is applied to a
temporary copy of the project and the app is read again in a fresh process, and the result must be what was asked.
If the app no longer starts, or the flow is not what was requested, the plan is refused.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .edit import EditError

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "site-packages"}
MAX_VERIFY_FILES = 20000


# --------------------------------------------------------------------------- source documents
class Doc:
    """One source file: bytes, line offsets and the parsed tree. ``ast`` columns are UTF-8 byte offsets."""

    def __init__(self, text: str):
        self.text = text
        self.b = text.encode("utf-8")
        self.lines = self.b.splitlines(keepends=True)
        self.starts = [0]
        for ln in self.lines:
            self.starts.append(self.starts[-1] + len(ln))
        self.tree = ast.parse(text)

    def off(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col

    def node_span(self, node) -> tuple[int, int]:
        return self.off(node.lineno, node.col_offset), self.off(node.end_lineno, node.end_col_offset)

    def lines_span(self, first: int, last: int) -> tuple[int, int]:
        return self.starts[first - 1], self.starts[last]

    def stmt_span(self, stmt, comments: bool = True) -> tuple[int, int]:
        """Whole lines of a statement, including its decorators and the comment lines right above it."""
        first = min([stmt.lineno] + [d.lineno for d in getattr(stmt, "decorator_list", [])])
        last = stmt.end_lineno
        before = self.lines[first - 1][: stmt.col_offset if first == stmt.lineno else 0]
        if before.strip():
            raise EditError(f"Line {first} holds more than one statement, so it can't be moved safely.")
        after = self.lines[last - 1][stmt.end_col_offset:].strip()
        if after and not after.startswith(b"#"):
            raise EditError(f"Line {last} holds more than one statement, so it can't be moved safely.")
        while comments and first > 1 and self.lines[first - 2].strip().startswith(b"#"):
            first -= 1
        return self.lines_span(first, last)


def _apply(doc: Doc, edits: list[tuple[int, int, bytes]]) -> str:
    edits = sorted(edits, key=lambda e: (e[0], e[1]))
    for (_, e1, _), (s2, _, _) in zip(edits, edits[1:]):
        if e1 > s2:
            raise EditError("Two changes would touch the same lines. Apply one first, then the other.")
    b = doc.b
    for s, e, rep in reversed(edits):
        b = b[:s] + rep + b[e:]
    return b.decode("utf-8")


class Workspace:
    """The project's files as they are while a batch of operations is being planned."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.docs: dict[str, Doc] = {}
        self.original: dict[str, str] = {}

    def _path(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if self.root not in path.parents:
            raise EditError(f"{rel} is outside the project")
        return path

    def doc(self, rel: str) -> Doc:
        if rel not in self.docs:
            try:
                text = self._path(rel).read_text(encoding="utf-8")
                self.docs[rel] = Doc(text)
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                raise EditError(f"Can't read {rel}: {exc}") from exc
            self.original[rel] = text
        return self.docs[rel]

    def put(self, rel: str, edits: list[tuple[int, int, bytes]]) -> None:
        doc = self.doc(rel)
        new = _apply(doc, edits)
        try:
            self.docs[rel] = Doc(new)
        except SyntaxError as exc:
            raise EditError(f"That change would break {rel}: {exc.msg} (line {exc.lineno})") from exc

    def plan(self) -> dict[str, tuple[str, str]]:
        return {rel: (self.original[rel], d.text) for rel, d in self.docs.items() if d.text != self.original[rel]}


# --------------------------------------------------------------------------- locating things in the code
def _dotted(node) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _split_id(block_id: str) -> tuple[str, str]:
    """'fn:path/file.py:Qual.name' -> ('path/file.py', 'Qual.name')"""
    try:
        _, rel, qual = block_id.split(":", 2)
    except ValueError:
        raise EditError(f"{block_id} is not something that is defined in your code.") from None
    return rel, qual


def find_function(doc: Doc, qualname: str):
    if "<locals>" in qualname:
        raise EditError(f"{qualname} is defined inside another function and can't be edited.")
    scope = doc.tree.body
    node = None
    for part in qualname.split("."):
        node = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part), None)
        if node is None:
            raise EditError(f"{qualname} was not found any more. Refresh the diagram and try again.")
        scope = node.body
    return node


def _route_of(spec: dict, route_id: str) -> dict:
    route = next((r for r in spec["routes"] if r["id"] == route_id), None)
    if route is None:
        raise EditError(f"{route_id} no longer exists. Refresh the diagram and try again.")
    return route


def _block(spec: dict, block_id: str) -> dict:
    b = spec["blocks"].get(block_id)
    if b is None:
        raise EditError(f"{block_id} no longer exists. Refresh the diagram and try again.")
    return b


def _handler(ws: Workspace, spec: dict, route: dict):
    if not route.get("handler"):
        raise EditError(f"The handler of {route['id']} is not in your project, so its flow can't be edited.")
    rel, qual = _split_id(route["handler"])
    doc = ws.doc(rel)
    return rel, doc, find_function(doc, qual)


def _route_decorator(fn) -> ast.Call:
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP_METHODS:
            return d
    raise EditError(
        f"{fn.name} is not registered with a plain @app.get(...) / @router.post(...) decorator "
        "(for example it uses add_api_route), so its dependencies can't be edited."
    )


# --------------------------------------------------------------------------- generic text helpers
def _permute(doc: Doc, spans: list[tuple[int, int]], source_of_slot: list[int]) -> list:
    """Edits that put the text of span ``source_of_slot[i]`` into slot ``i`` (for items inside a line)."""
    return [(s, e, doc.b[spans[source_of_slot[i]][0]: spans[source_of_slot[i]][1]]) for i, (s, e) in enumerate(spans)]


def _is_def(text: bytes) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(b"#"):
            return line.startswith((b"@", b"def ", b"async def ", b"class "))
    return False


def _blank_lines(chunk: bytes, from_end: bool) -> int:
    lines = chunk.splitlines(keepends=True)
    n = 0
    for ln in (reversed(lines) if from_end else lines):
        if ln.strip():
            break
        n += 1
    return n


def _permute_lines(doc: Doc, spans: list[tuple[int, int]], source_of_slot: list[int]) -> list:
    """Like _permute but for whole statements: code between them stays, and a function that lands
    somewhere new keeps the two blank lines around it that PEP 8 asks for."""
    items = []
    for i in range(len(spans)):
        s, e = spans[source_of_slot[i]]
        text = doc.b[s:e]
        items.append(text if text.endswith((b"\n", b"\r")) else text + b"\n")
    out = b""
    lead = doc.b[: spans[0][0]]
    if spans[0][0] and _is_def(items[0]):
        out += b"\n" * max(0, 2 - _blank_lines(lead, from_end=True))
    for i, item in enumerate(items):
        out += item
        if i + 1 < len(items):
            gap = doc.b[spans[i][1]: spans[i + 1][0]]
            wants = _is_def(item) or _is_def(items[i + 1])
            if wants:
                if gap.strip() == b"":
                    gap += b"\n" * max(0, 2 - _blank_lines(gap, False))
                else:
                    gap = b"\n" * max(0, 2 - _blank_lines(gap, False)) + gap
                    gap += b"\n" * max(0, 2 - _blank_lines(gap, True))
            out += gap
    tail = doc.b[spans[-1][1]:]
    if tail and _is_def(items[-1]):
        out += b"\n" * max(0, 2 - _blank_lines(tail, False))
    return [(spans[0][0], spans[-1][1], out)]


def _remove_item(spans: list[tuple[int, int]], idx: int) -> tuple[int, int, bytes]:
    """Delete item ``idx`` of a comma separated list together with one neighbouring comma."""
    if idx < len(spans) - 1:
        return spans[idx][0], spans[idx + 1][0], b""
    if idx > 0:
        return spans[idx - 1][1], spans[idx][1], b""
    return spans[idx][0], spans[idx][1], b""


def _param_specs(doc: Doc, fn) -> list[tuple[str, tuple[int, int]]]:
    """[(parameter name, (start, end) including annotation and default)] in source order."""
    a = fn.args
    if a.posonlyargs or (a.kwonlyargs and not a.vararg):
        raise EditError(f"{fn.name} uses '/' or a bare '*' in its parameters, which flowmap can't reorder safely.")
    pos = list(a.args)
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    items = list(zip(pos, defaults))
    if a.vararg:
        items.append((a.vararg, None))
    items += list(zip(a.kwonlyargs, a.kw_defaults))
    if a.kwarg:
        items.append((a.kwarg, None))
    out = []
    for arg, default in items:
        start = doc.off(arg.lineno, arg.col_offset)
        end = doc.off(default.end_lineno, default.end_col_offset) if default is not None else doc.off(arg.end_lineno, arg.end_col_offset)
        out.append((arg.arg, (start, end)))
    return out


def _name_read_at(fn, name: str) -> Optional[int]:
    for stmt in fn.body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
                return n.lineno
    return None


def _decorator_deps(doc: Doc, fn, expected: int):
    """(decorator call, keyword, [element nodes]) of dependencies=[...] on the route decorator."""
    deco = _route_decorator(fn)
    kw = next((k for k in deco.keywords if k.arg == "dependencies"), None)
    elts = list(kw.value.elts) if kw is not None and isinstance(kw.value, (ast.List, ast.Tuple)) else []
    if len(elts) != expected:
        raise EditError(
            f"{fn.name} shows {expected} dependencies from its decorator but the code lists {len(elts)}. "
            "The rest come from a router or from the app itself, which can't be edited here yet."
        )
    return deco, kw, elts


# --------------------------------------------------------------------------- middleware
def _middleware_slot(ws: Workspace, spec: dict, bid: str, whole: bool) -> tuple[str, tuple[int, int]]:
    """Where a middleware is registered. whole=True moves/deletes the full statement (or whole decorated function)."""
    b = _block(spec, bid)
    site = b.get("site")
    if not site:
        raise EditError(
            f"{b['name']} is not registered with a plain app.add_middleware(...) call or @app.middleware decorator "
            "in your code, so it can't be moved or removed."
        )
    doc = ws.doc(site["file"])
    if site["kind"] == "decorator":
        _, qual = _split_id(bid)
        fn = find_function(doc, qual)
        deco = next((d for d in fn.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "middleware"), None)
        if deco is None:
            raise EditError(f"{b['name']} is no longer decorated with @app.middleware. Refresh and try again.")
        return site["file"], (doc.stmt_span(fn) if whole else doc.lines_span(deco.lineno, deco.end_lineno))
    for stmt in ast.walk(doc.tree):
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_middleware"
                and call.args and (_dotted(call.args[0]) or "").split(".")[-1] == b["name"]):
            return site["file"], doc.stmt_span(stmt)
    raise EditError(f"The add_middleware({b['name']}, ...) call was not found any more. Refresh and try again.")


def _reject_warning(spec: dict, bid: str, what: str, warnings: list) -> None:
    b = spec["blocks"].get(bid, {})
    if any(e.get("type") == "reject" for e in b.get("effects", [])):
        warnings.append(f"{b.get('name', bid)} can reject requests. After you {what} it, those checks no longer run.")


def _reorder_middleware(ws, spec, op, expect, warnings):
    current, want = list(spec.get("middleware", [])), list(op["order"])
    if sorted(current) != sorted(want):
        raise EditError("The list of middleware changed. Refresh the diagram and try again.")
    if current == want:
        return
    slots = {bid: _middleware_slot(ws, spec, bid, whole=True) for bid in current}
    files = {rel for rel, _ in slots.values()}
    if len(files) > 1:
        raise EditError("These middleware are registered in different files (" + ", ".join(sorted(files)) + "), so they can't be reordered.")
    rel = files.pop()
    in_source = sorted(current, key=lambda b: slots[b][1][0])
    spans = [slots[b][1] for b in in_source]
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        if e1 > s2:
            raise EditError("Two middleware overlap in the code and can't be reordered.")
    wanted_in_source = list(reversed(want))      # the last one added is the outermost
    ws.put(rel, _permute_lines(ws.doc(rel), spans, [in_source.index(b) for b in wanted_in_source]))
    expect.append(lambda new: _same(new.get("middleware", []), want, "middleware order"))


def _remove_middleware(ws, spec, op, expect, warnings):
    bid = op["block"]
    if bid not in spec.get("middleware", []):
        raise EditError("That middleware is no longer in the app. Refresh the diagram and try again.")
    rel, (s, e) = _middleware_slot(ws, spec, bid, whole=False)
    ws.put(rel, [(s, e, b"")])
    _reject_warning(spec, bid, "remove", warnings)
    expect.append(lambda new: _absent(bid in new.get("middleware", []), _block(spec, bid)["name"]))


# --------------------------------------------------------------------------- dependencies
def _top_deps(route: dict) -> list[dict]:
    return [s for s in route["steps"] if s["role"] in ("dependency", "security") and s.get("source") in ("decorator", "param")]


def _reorder_dependencies(ws, spec, op, expect, warnings):
    route = _route_of(spec, op["route"])
    steps = _top_deps(route)
    current, want = [s["block"] for s in steps], list(op["order"])
    if sorted(current) != sorted(want):
        raise EditError(f"The dependencies of {route['id']} changed. Refresh the diagram and try again.")
    if current == want:
        return
    dec = [s for s in steps if s["source"] == "decorator"]
    par = [s for s in steps if s["source"] == "param"]
    if {s["block"] for s in dec} != set(want[: len(dec)]):
        raise EditError(
            "Dependencies listed on the route decorator always run before the ones declared as parameters, "
            "so they can't swap places. Move them within their own group."
        )
    rel, doc, fn = _handler(ws, spec, route)
    edits = []
    if len(dec) > 1:
        _, _, elts = _decorator_deps(doc, fn, len(dec))
        order = [s["block"] for s in dec]
        edits += _permute(doc, [doc.node_span(e) for e in elts], [order.index(b) for b in want[: len(dec)]])
    if len(par) > 1:
        specs = dict(_param_specs(doc, fn))
        names = [s["param"] for s in par]
        missing = [n for n in names if n not in specs]
        if missing:
            raise EditError(f"Parameter {missing[0]} was not found in {fn.name}. Refresh and try again.")
        by_block = {s["block"]: s["param"] for s in par}
        new_names = [by_block[b] for b in want[len(dec):]]
        edits += _permute(doc, [specs[n] for n in names], [names.index(n) for n in new_names])
    ws.put(rel, edits)
    expect.append(lambda new: _same(_route_deps(new, route["id"]), want, f"dependency order of {route['id']}"))


def _route_deps(spec: dict, route_id: str) -> list[str]:
    route = next((r for r in spec["routes"] if r["id"] == route_id), None)
    return [s["block"] for s in _top_deps(route)] if route else []


def _remove_dependency(ws, spec, op, expect, warnings):
    route = _route_of(spec, op["route"])
    steps = _top_deps(route)
    step = next((s for s in steps if s["block"] == op["block"]), None)
    if step is None:
        nested = any(s["block"] == op["block"] and s.get("source") == "nested" for s in route["steps"])
        raise EditError(
            "That block is used inside another dependency, not declared on this API. Edit the dependency that uses it."
            if nested else "That dependency is no longer on this API. Refresh the diagram and try again."
        )
    rel, doc, fn = _handler(ws, spec, route)
    if step["source"] == "decorator":
        dec = [s for s in steps if s["source"] == "decorator"]
        deco, kw, elts = _decorator_deps(doc, fn, len(dec))
        idx = [s["block"] for s in dec].index(op["block"])
        if len(elts) > 1:
            edit = _remove_item([doc.node_span(e) for e in elts], idx)
        else:
            items = sorted(list(deco.args) + list(deco.keywords), key=lambda n: (n.lineno, n.col_offset))
            edit = _remove_item([doc.node_span(n) for n in items], items.index(kw))
    else:
        name = step["param"]
        line = _name_read_at(fn, name)
        if line is not None:
            raise EditError(
                f"{fn.name} uses '{name}' on line {line}, so the dependency that provides it can't be removed "
                "without changing what the function does."
            )
        specs = _param_specs(doc, fn)
        idx = next((i for i, (n, _) in enumerate(specs) if n == name), None)
        if idx is None:
            raise EditError(f"Parameter {name} was not found in {fn.name}. Refresh and try again.")
        edit = _remove_item([sp for _, sp in specs], idx)
    ws.put(rel, [edit])
    _reject_warning(spec, op["block"], "remove", warnings)
    expect.append(lambda new: _absent(op["block"] in _route_deps(new, route["id"]), _block(spec, op["block"])["name"]))


# --------------------------------------------------------------------------- batches and verification
def _same(got, want, what: str) -> None:
    if list(got) != list(want):
        raise EditError(f"After the change the {what} would be {got}, not {want}. Nothing was changed.")


def _absent(still_there: bool, name: str) -> None:
    if still_there:
        raise EditError(f"{name} would still be in the flow after removing it. Nothing was changed.")


Handler = Callable[..., None]
HANDLERS: dict[tuple[str, str], Handler] = {
    ("reorder", "middleware"): _reorder_middleware,
    ("reorder", "dependency"): _reorder_dependencies,
    ("remove", "middleware"): _remove_middleware,
    ("remove", "dependency"): _remove_dependency,
}


def plan_flow(root, target: str, spec: dict, ops: list[dict], verify: bool = True) -> dict:
    """Turn diagram operations into a plan. Returns {'plan': {file: (old, new)}, 'warnings': [...]}."""
    if not ops:
        return {"plan": {}, "warnings": []}
    ws = Workspace(root)
    expect: list[Callable[[dict], None]] = []
    warnings: list[str] = []
    for op in ops:
        handler = HANDLERS.get((op.get("op"), op.get("group")))
        if handler is None:
            raise EditError(f"Unknown change: {op.get('op')} {op.get('group')}")
        try:
            handler(ws, spec, op, expect, warnings)
        except (KeyError, TypeError) as exc:
            raise EditError(f"Malformed change ({op.get('op')} {op.get('group')}): missing {exc}") from exc
    plan = ws.plan()
    if plan and verify:
        note = verify_plan(Path(root).resolve(), target, plan, expect)
        if note:
            warnings.append(note)
    return {"plan": plan, "warnings": warnings}


def _copy_project(root: Path, dest: Path) -> bool:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(root)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            count += 1
            if count > MAX_VERIFY_FILES:
                return False
            shutil.copy2(Path(dirpath) / name, dest / rel / name)
    return True


def verify_plan(root: Path, target: str, plan: dict, expect: list) -> Optional[str]:
    """Apply the plan to a copy of the project, read the app again and check the result.

    Returns a warning if verification had to be skipped, raises EditError if the change would not work."""
    tmp = Path(tempfile.mkdtemp(prefix="flowmap-verify-"))
    try:
        if not _copy_project(root, tmp):
            return f"The project has more than {MAX_VERIFY_FILES} files, so the change could not be test-run first. Review the diff carefully."
        for rel, (_, new) in plan.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(new, encoding="utf-8")
        out = tmp / "_flowmap_verify.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH", "")]))
        proc = subprocess.run(
            [sys.executable, "-m", "flowmap", "extract", target, "--root", str(tmp), "-o", str(out)],
            capture_output=True, text=True, cwd=tmp, env=env,
        )
        if proc.returncode != 0:
            last = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
            raise EditError(f"That change would stop the app from starting ({last}). Nothing was changed.")
        new_spec = json.loads(out.read_text(encoding="utf-8"))
        for check in expect:
            check(new_spec)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
