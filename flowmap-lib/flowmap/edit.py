"""Writes value edits made in the diagram (or in flowmap.json) back into the code.

Only literal values are rewritten, at the exact position the extractor recorded.
Nothing else in the file is touched, so formatting and comments stay as they are.
"""
from __future__ import annotations

import ast
import difflib
from pathlib import Path


class EditError(Exception):
    pass


def _compatible(old, new) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, (int, float)):
        return isinstance(new, (int, float))
    if isinstance(old, str):
        return isinstance(new, str)
    if isinstance(old, list):
        return isinstance(new, list) and all(isinstance(x, (int, float, str, bool)) for x in new)
    return False


def _literal(old_text: str, old, new) -> str:
    if isinstance(new, str) and old_text[:1] in ("'", '"') and old_text[:3] not in ("'''", '"""'):
        quote = old_text[0]
        if quote not in new and "\\" not in new and "\n" not in new:
            return f"{quote}{new}{quote}"
    if isinstance(old, list) and old_text.startswith("("):
        return "(" + ", ".join(repr(x) for x in new) + ("," if len(new) == 1 else "") + ")"
    return repr(new)


def plan_edits(root, changes: list[tuple[dict, object]]) -> dict[str, tuple[str, str]]:
    """changes: [(editable, new_value)].  Returns {relative file: (old source, new source)}."""
    root = Path(root).resolve()
    by_file: dict[str, list] = {}
    done = set()
    for editable, new in changes:
        key = (editable["file"], editable["line"], editable["col"])
        if key in done:  # same literal reachable from two blocks
            continue
        done.add(key)
        if not _compatible(editable["value"], new):
            raise EditError(
                f"{editable['label']}: new value {new!r} is not the same kind of value as {editable['value']!r}"
            )
        by_file.setdefault(editable["file"], []).append((editable, new))

    result = {}
    for rel, items in by_file.items():
        path = (root / rel).resolve()
        if root not in path.parents:
            raise EditError(f"{rel} is outside the project")
        old_source = path.read_text(encoding="utf-8")
        lines = [ln.encode("utf-8") for ln in old_source.splitlines(keepends=True)]
        for e, new in sorted(items, key=lambda it: (it[0]["line"], it[0]["col"]), reverse=True):
            first, last = e["line"] - 1, e["end_line"] - 1
            if last >= len(lines):
                raise EditError(f"{rel} changed since the diagram was built. Refresh and try again.")
            chunk = b"".join(lines[first: last + 1])
            end = sum(len(l) for l in lines[first:last]) + e["end_col"]
            current = chunk[e["col"]: end].decode("utf-8")
            if current != e["text"]:
                raise EditError(
                    f"{rel}:{e['line']} no longer contains {e['text']!r}. Refresh the diagram and try again."
                )
            replaced = chunk[: e["col"]] + _literal(e["text"], e["value"], new).encode("utf-8") + chunk[end:]
            lines[first: last + 1] = [l for l in replaced.splitlines(keepends=True)]
        new_source = b"".join(lines).decode("utf-8")
        try:
            ast.parse(new_source)
        except SyntaxError as exc:  # pragma: no cover - safety net
            raise EditError(f"Edit would break {rel}: {exc}") from exc
        if new_source != old_source:
            result[rel] = (old_source, new_source)
    return result


def make_diff(plan: dict[str, tuple[str, str]]) -> str:
    out = []
    for rel, (old, new) in sorted(plan.items()):
        out.extend(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=2
        ))
    return "".join(out)


def write_plan(root, plan: dict[str, tuple[str, str]]) -> None:
    root = Path(root).resolve()
    for rel, (_, new) in plan.items():
        (root / rel).write_text(new, encoding="utf-8")


def changes_from_spec(edited_spec: dict, fresh_spec: dict) -> list[tuple[dict, object]]:
    """Compare a hand-edited flowmap.json with the code as it is now."""
    from .extract import all_editables

    fresh = all_editables(fresh_spec)
    changes = []
    for eid, e in all_editables(edited_spec).items():
        if eid in fresh and fresh[eid]["value"] != e["value"]:
            changes.append((fresh[eid], e["value"]))
    return changes
