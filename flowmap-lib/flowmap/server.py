"""Local web server for the diagram. Standard library only; listens on 127.0.0.1."""
from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .edit import EditError, make_diff, plan_edits, write_plan
from .extract import all_editables

STATIC = Path(__file__).parent / "static"


class Project:
    def __init__(self, target: str, root: str, spec_path: str):
        self.target = target
        self.root = Path(root).resolve()
        self.spec_path = (self.root / spec_path).resolve()

    def refresh(self) -> dict:
        """Re-read the code in a fresh Python process so edits are always picked up."""
        cmd = [sys.executable, "-m", "flowmap", "extract", self.target,
               "--root", str(self.root), "-o", str(self.spec_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "extract failed")
        return self.spec()

    def spec(self) -> dict:
        return json.loads(self.spec_path.read_text(encoding="utf-8"))

    def plan(self, changes: list) -> dict:
        editables = all_editables(self.refresh())
        pairs = []
        for c in changes:
            if c["id"] not in editables:
                raise EditError(f"{c['id']} no longer exists in the code. Refresh the diagram.")
            pairs.append((editables[c["id"]], c["value"]))
        return plan_edits(self.root, pairs)

    def source(self, block_id: str) -> dict:
        block = self.spec()["blocks"].get(block_id)
        if not block or not block.get("file"):
            return {"code": "", "file": None}
        path = (self.root / block["file"]).resolve()
        if self.root not in path.parents:
            return {"code": "", "file": None}
        lines = path.read_text(encoding="utf-8").splitlines()
        first, last = block["line"], block.get("end_line") or block["line"]
        return {"file": block["file"], "line": first, "code": "\n".join(lines[first - 1: last])}

    def set_summary(self, block_id: str, summary: str) -> dict:
        spec = self.spec()
        block = spec["blocks"][block_id]
        block["summary"], block["summary_source"] = summary.strip(), "manual"
        self.spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return spec


def make_handler(project: Project):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/spec":
                    self._send(200, project.spec())
                elif url.path == "/api/source":
                    self._send(200, project.source(parse_qs(url.query).get("block", [""])[0]))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:
                self._send(500, {"error": str(exc)})

        def do_POST(self):
            # this server can change code, so only accept calls from its own page
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self._send(403, {"error": "cross-site request refused"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "JSON only"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/refresh":
                    self._send(200, {"spec": project.refresh()})
                elif self.path == "/api/preview":
                    self._send(200, {"diff": make_diff(project.plan(body.get("changes", [])))})
                elif self.path == "/api/apply":
                    plan = project.plan(body.get("changes", []))
                    write_plan(project.root, plan)
                    self._send(200, {"diff": make_diff(plan), "files": sorted(plan), "spec": project.refresh()})
                elif self.path == "/api/summary":
                    self._send(200, {"spec": project.set_summary(body["block"], body.get("summary", ""))})
                else:
                    self._send(404, {"error": "not found"})
            except (EditError, RuntimeError, KeyError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._send(500, {"error": str(exc)})

    return Handler


def serve(target: str, root: str = ".", spec_path: str = "flowmap.json", port: int = 8765, open_browser: bool = True):
    project = Project(target, root, spec_path)
    project.refresh()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(project))
    url = f"http://127.0.0.1:{port}/"
    print(f"flowmap: {target}  ->  {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
