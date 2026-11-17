import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP = "examples.sample_app.main:app"


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(REPO / "examples", tmp_path / "examples")
    return tmp_path


def run(project, *args):
    return subprocess.run(
        [sys.executable, "-m", "flowmap", *args, "--root", str(project)],
        capture_output=True, text=True, cwd=project, env={"PYTHONPATH": str(REPO), "PATH": ""},
    )


def spec_of(project):
    assert run(project, "extract", APP).returncode == 0
    return json.loads((project / "flowmap.json").read_text())


def block(spec, suffix):
    return next(b for i, b in spec["blocks"].items() if i.endswith(suffix))


def editable(spec, suffix):
    return next(e for b in spec["blocks"].values() for e in b["editables"] if e["id"].endswith(suffix))


def test_flow_order_and_roles(project):
    spec = spec_of(project)
    route = next(r for r in spec["routes"] if r["id"] == "POST /items")
    roles = [s["role"] for s in route["steps"]]
    assert roles == ["middleware"] * 3 + ["dependency"] * 2 + ["validation", "body", "handler", "response"]
    names = [spec["blocks"][s["block"]]["name"] for s in route["steps"][:3]]
    assert names == ["add_timing_header", "RateLimitMiddleware", "CORSMiddleware"]  # outermost first


def test_middleware_effects_and_config(project):
    spec = spec_of(project)
    limiter = block(spec, ":RateLimitMiddleware")
    assert any("429" in e["text"] for e in limiter["effects"])
    assert {e["label"]: e["value"] for e in limiter["editables"]}["max_requests"] == 5
    timing = block(spec, ":add_timing_header")
    assert any("X-Process-Time" in e["text"] for e in timing["effects"])


def test_call_tree_reaches_database(project):
    spec = spec_of(project)
    service = block(spec, "services.py:create_item")
    called = [spec["blocks"][c]["name"] for c in service["calls"]]
    assert called == ["ItemRepository.count_for", "add_tax", "ItemRepository.insert"]
    insert = block(spec, ":ItemRepository.insert")
    sinks = [spec["blocks"][c] for c in insert["calls"]]
    assert sinks[0]["sink_type"] == "db" and sinks[0]["summary"].startswith("INSERT INTO items")
    assert any("409" in e["text"] for e in service["effects"])


def test_apply_edit_from_spec_file(project):
    spec = spec_of(project)
    editable(spec, "#quantity le")["value"] = 10
    editable(spec, "RateLimitMiddleware#max_requests")["value"] = 100
    editable(spec, "#MAX_ITEMS_PER_OWNER")["value"] = 50
    (project / "flowmap.json").write_text(json.dumps(spec))
    result = run(project, "apply", APP)
    assert result.returncode == 0, result.stderr
    assert "ge=0, le=10)" in (project / "examples/sample_app/models.py").read_text()
    main = (project / "examples/sample_app/main.py").read_text()
    assert "add_middleware(RateLimitMiddleware, max_requests=100, window_seconds=60)" in main
    assert "max_requests: int = 5" in main  # the constructor default was not touched
    assert "MAX_ITEMS_PER_OWNER = 50" in (project / "examples/sample_app/services.py").read_text()
    fresh = spec_of(project)
    assert editable(fresh, "#quantity le")["value"] == 10
    assert any("le=10" in c for f in block(fresh, ":ItemCreate")["fields"] for c in f["constraints"])


def test_stale_and_wrong_type_edits_are_refused(project):
    from flowmap.edit import EditError, plan_edits

    spec = spec_of(project)
    e = editable(spec, "#quantity le")
    with pytest.raises(EditError):
        plan_edits(project, [(e, "ten")])
    models = project / "examples/sample_app/models.py"
    models.write_text("# new first line\n" + models.read_text())
    with pytest.raises(EditError):
        plan_edits(project, [(e, 10)])


def test_manual_summary_survives_refresh(project):
    spec = spec_of(project)
    b = block(spec, ":ItemRepository.insert")
    b["summary"], b["summary_source"] = "Saves the item row.", "manual"
    (project / "flowmap.json").write_text(json.dumps(spec))
    assert block(spec_of(project), ":ItemRepository.insert")["summary"] == "Saves the item row."


def test_mounted_in_app(project):
    main = project / "examples" / "sample_app" / "main.py"
    main.write_text(main.read_text() + "\nfrom flowmap import mount_flowmap\nmount_flowmap(app, allow_edit=False)\n")
    code = (
        "from fastapi.testclient import TestClient\n"
        "from examples.sample_app.main import app\n"
        "c = TestClient(app)\n"
        "assert 'flowmap' in c.get('/flowmap/', follow_redirects=False).text.lower()\n"
        "assert c.get('/flowmap', follow_redirects=False).status_code in (307, 308)\n"
        "spec = c.get('/flowmap/api/spec').json()\n"
        "assert spec['routes'] and not any('/flowmap' in str(r) for r in spec['routes'])\n"
        "r = c.post('/flowmap/api/apply', json={'changes': []})\n"
        "assert r.status_code == 403, r.text\n"
        "r = c.post('/flowmap/api/refresh', json={}, headers={'Origin': 'http://evil.example'})\n"
        "assert r.status_code == 403\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=project,
                          env={"PYTHONPATH": str(REPO), "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
    assert proc.stdout.strip() == "ok", proc.stderr
