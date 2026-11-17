"""Mount the diagram inside a FastAPI app, the way Swagger lives at /docs.

    from flowmap import mount_flowmap
    mount_flowmap(app)                    # diagram at /flowmap
    mount_flowmap(app, "/map", allow_edit=True)

The diagram routes are hidden from the schema, so they never show up in the diagram itself.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .edit import EditError, make_diff, write_plan
from .server import STATIC, Project


def _infer_target(app: FastAPI, depth: int) -> str:
    """Work out 'module:variable' for the app from the module that called mount_flowmap."""
    caller = sys._getframe(depth).f_globals
    module = caller.get("__name__", "")
    if module and module != "__main__":
        for name, value in caller.items():
            if value is app:
                return f"{module}:{name}"
    raise ValueError(
        "flowmap could not tell where the app lives. Pass it explicitly: "
        "mount_flowmap(app, target='package.module:app')"
    )


def mount_flowmap(
    app: FastAPI,
    path: str = "/flowmap",
    *,
    target: Optional[str] = None,
    root: str = ".",
    spec_path: str = "flowmap.json",
    allow_edit: bool = False,
) -> None:
    """Serve the flow diagram at ``path`` inside ``app``.

    target      'package.module:app'. Found automatically when called from the app's own module.
    root        project folder (default: current folder). Edits never leave it.
    allow_edit  let the page write value changes into the source files. Off by default because
                a mounted app may be reachable by other machines. Turn it on for local development only.
    """
    path = "/" + path.strip("/")
    project = Project(target or _infer_target(app, 2), root, spec_path)
    router = APIRouter(prefix=path, include_in_schema=False)
    lock = threading.Lock()
    state = {"fresh": False}

    def fail(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})

    def ok(body: dict) -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def spec() -> dict:
        # rebuild once per server start so the page never shows a stale file
        with lock:
            if not state["fresh"] or not project.spec_path.exists():
                project.refresh()
                state["fresh"] = True
        return project.spec()

    def guard(request: Request, writes: bool) -> Optional[JSONResponse]:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return fail(403, "cross-site request refused")
        if writes and not allow_edit:
            return fail(403, "Editing is switched off. Start with mount_flowmap(app, allow_edit=True) to enable it.")
        return None

    @router.get("", include_in_schema=False)
    def no_slash():
        return RedirectResponse(path + "/")  # keeps the page's relative URLs working

    @router.get("/")
    def index():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @router.get("/api/spec")
    def get_spec():
        try:
            return ok(spec())
        except RuntimeError as exc:
            return fail(500, str(exc))

    @router.get("/api/source")
    def get_source(block: str = ""):
        return ok(project.source(block))

    @router.post("/api/refresh")
    def refresh(request: Request, body: dict = Body(default={})):
        if (denied := guard(request, False)):
            return denied
        try:
            with lock:
                new = project.refresh()
                state["fresh"] = True
            return ok({"spec": new})
        except RuntimeError as exc:
            return fail(400, str(exc))

    @router.post("/api/preview")
    def preview(request: Request, body: dict = Body(default={})):
        if (denied := guard(request, True)):
            return denied
        try:
            return ok({"diff": make_diff(project.plan(body.get("changes", [])))})
        except (EditError, RuntimeError, KeyError) as exc:
            return fail(400, str(exc))

    @router.post("/api/apply")
    def apply(request: Request, body: dict = Body(default={})):
        if (denied := guard(request, True)):
            return denied
        try:
            plan = project.plan(body.get("changes", []))
            write_plan(project.root, plan)
            return ok({"diff": make_diff(plan), "files": sorted(plan), "spec": project.refresh()})
        except (EditError, RuntimeError, KeyError) as exc:
            return fail(400, str(exc))

    @router.post("/api/summary")
    def summary(request: Request, body: dict = Body(default={})):
        if (denied := guard(request, True)):
            return denied
        try:
            return ok({"spec": project.set_summary(body["block"], body.get("summary", ""))})
        except KeyError as exc:
            return fail(400, str(exc))

    app.include_router(router)
