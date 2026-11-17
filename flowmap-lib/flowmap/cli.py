"""Command line:  flowmap extract | serve | apply | describe"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _write(spec: dict, path: Path) -> None:
    path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="flowmap", description="Editable flow diagrams for FastAPI apps.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("app", help="FastAPI app as package.module:attribute, e.g. main:app")
        p.add_argument("--root", default=".", help="project folder (default: current folder)")
        p.add_argument("-o", "--spec", default="flowmap.json", help="spec file (default: flowmap.json)")

    common(sub.add_parser("extract", help="read the code and write the spec file"))
    p = sub.add_parser("serve", help="open the interactive diagram")
    common(p)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="do not open a browser window")
    p = sub.add_parser("apply", help="write values you changed in the spec file back into the code")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    p = sub.add_parser("describe", help="fill missing summaries with an LLM (see flowmap/describe.py)")
    common(p)
    p.add_argument("--all", action="store_true", help="also rewrite docstring-based summaries")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    spec_path = Path(args.spec) if Path(args.spec).is_absolute() else root / args.spec

    from .extract import extract

    if args.command == "extract":
        spec = extract(args.app, root, previous=_load(spec_path))
        _write(spec, spec_path)
        print(f"{len(spec['routes'])} APIs, {len(spec['blocks'])} blocks -> {spec_path}", file=sys.stderr)
        return 0

    if args.command == "serve":
        from .server import serve

        serve(args.app, str(root), str(spec_path), args.port, not args.no_open)
        return 0

    if args.command == "apply":
        from .edit import EditError, changes_from_spec, make_diff, plan_edits, write_plan

        edited = _load(spec_path)
        if edited is None:
            print(f"{spec_path} not found. Run 'flowmap extract' first.", file=sys.stderr)
            return 1
        fresh = extract(args.app, root, previous=edited)
        try:
            plan = plan_edits(root, changes_from_spec(edited, fresh))
        except EditError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not plan:
            print("Spec and code already agree. Nothing to change.")
            return 0
        print(make_diff(plan))
        if args.dry_run:
            return 0
        write_plan(root, plan)
        print(f"Updated {len(plan)} file(s). Run 'flowmap extract' to refresh the spec.", file=sys.stderr)
        return 0

    if args.command == "describe":
        from .describe import describe

        spec = extract(args.app, root, previous=_load(spec_path))
        n = describe(spec, root, overwrite=args.all)
        _write(spec, spec_path)
        print(f"{n} summaries written to {spec_path}", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
