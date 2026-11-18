"""Structural edits: reorder / remove / add blocks, always verified by re-reading the app."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from flowmap.edit import EditError, make_diff, write_plan  # noqa: E402
from flowmap.structure import plan_flow, verify_plan  # noqa: E402

APP = "examples.sample_app.main:app"
MAIN = "examples/sample_app/main.py"


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(REPO / "examples", tmp_path / "examples")
    return tmp_path


def extract(project):
    proc = subprocess.run(
        [sys.executable, "-m", "flowmap", "extract", APP, "--root", str(project)],
        capture_output=True, text=True, cwd=project, env={"PYTHONPATH": str(REPO), "PATH": ""},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads((project / "flowmap.json").read_text())


def flow(project, ops, apply=True):
    spec = extract(project)
    result = plan_flow(project, APP, spec, ops)
    if apply:
        write_plan(project, result["plan"])
    return result, spec


def short(spec, bid):
    return bid.split(":")[-1]


def route(spec, rid):
    return next(r for r in spec["routes"] if r["id"] == rid)


def deps(spec, rid):
    return [short(spec, s["block"]) for s in route(spec, rid)["steps"] if s["role"] == "dependency"]


def edit_main(project, old, new):
    p = project / MAIN
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new))


# --------------------------------------------------------------------------- spec
def test_spec_records_where_things_are_written(project):
    spec = extract(project)
    assert spec["flowmap_version"] == 2
    assert [short(spec, m) for m in spec["middleware"]] == ["add_timing_header", "RateLimitMiddleware", "CORSMiddleware"]
    assert spec["blocks"][spec["middleware"][0]]["site"]["kind"] == "decorator"
    assert spec["blocks"][spec["middleware"][2]]["site"]["kind"] == "call"
    r = route(spec, "POST /items")
    assert r["decorator"]["file"] == MAIN and r["handler"].endswith(":create_item")
    assert [(s["source"], s.get("param")) for s in r["steps"] if s["role"] == "dependency"] == [("param", "user"), ("param", "db")]


# --------------------------------------------------------------------------- middleware
def test_reorder_middleware_rewrites_only_those_statements(project):
    spec = extract(project)
    timing, rate, cors = spec["middleware"]
    result, _ = flow(project, [{"op": "reorder", "group": "middleware", "order": [cors, timing, rate]}])
    after = extract(project)
    assert after["middleware"] == [cors, timing, rate]
    diff = make_diff(result["plan"])
    assert "app.add_middleware(CORSMiddleware" in diff and "def add_timing_header" in diff
    assert "def current_user" not in diff.replace(" def current_user", "")  # untouched code is not in the diff body
    assert (project / MAIN).read_text().count("add_timing_header") == 1


def test_reorder_to_same_order_changes_nothing(project):
    spec = extract(project)
    result, _ = flow(project, [{"op": "reorder", "group": "middleware", "order": spec["middleware"]}], apply=False)
    assert result["plan"] == {}


def test_remove_class_middleware_deletes_the_call(project):
    spec = extract(project)
    cors = spec["middleware"][2]
    flow(project, [{"op": "remove", "group": "middleware", "block": cors}])
    assert "CORSMiddleware" not in (project / MAIN).read_text().split("RateLimitMiddleware(BaseHTTPMiddleware)")[0].replace("from fastapi.middleware.cors import CORSMiddleware", "")
    assert len(extract(project)["middleware"]) == 2


def test_remove_decorator_middleware_keeps_the_function(project):
    spec = extract(project)
    timing = spec["middleware"][0]
    result, _ = flow(project, [{"op": "remove", "group": "middleware", "block": timing}])
    text = (project / MAIN).read_text()
    assert "async def add_timing_header" in text and '@app.middleware("http")' not in text
    assert short(extract(project), timing) not in [short(extract(project), m) for m in extract(project)["middleware"]]


def test_unknown_middleware_is_refused(project):
    spec = extract(project)
    with pytest.raises(EditError, match="no longer in the app"):
        plan_flow(project, APP, spec, [{"op": "remove", "group": "middleware", "block": "mw:nope.py:Gone"}])


# --------------------------------------------------------------------------- dependencies
def test_reorder_parameter_dependencies(project):
    spec = extract(project)
    ids = [s["block"] for s in route(spec, "POST /items")["steps"] if s["role"] == "dependency"]
    flow(project, [{"op": "reorder", "group": "dependency", "route": "POST /items", "order": list(reversed(ids))}])
    assert deps(extract(project), "POST /items") == ["get_db", "current_user"]
    text = (project / MAIN).read_text()
    assert "db=Depends(services.get_db), user: str = Depends(current_user)" in text.replace("\n", " ") or text.index("get_db") < text.index("current_user", text.index("def create_item"))


def test_remove_unused_parameter_dependency(project):
    spec = extract(project)
    current_user = next(s["block"] for s in route(spec, "GET /items/{item_id}")["steps"] if s["block"].endswith("current_user"))
    flow(project, [{"op": "remove", "group": "dependency", "route": "GET /items/{item_id}", "block": current_user}])
    assert deps(extract(project), "GET /items/{item_id}") == ["get_db"]
    assert "def read_item(item_id: int, db=Depends(services.get_db))" in (project / MAIN).read_text()


def test_removing_a_dependency_the_function_uses_is_refused(project):
    spec = extract(project)
    current_user = next(s["block"] for s in route(spec, "POST /items")["steps"] if s["block"].endswith("current_user"))
    with pytest.raises(EditError, match="uses 'user'"):
        plan_flow(project, APP, spec, [{"op": "remove", "group": "dependency", "route": "POST /items", "block": current_user}])


def _with_decorator_deps(project):
    edit_main(project, '@app.get("/health", tags=["system"])\ndef health():',
              'def slow_a():\n    """First check."""\n\n\ndef slow_b():\n    """Second check."""\n\n\n'
              '@app.get("/health", tags=["system"], dependencies=[Depends(slow_a), Depends(slow_b)])\n'
              'def health(user: str = Depends(current_user)):')


def test_reorder_and_remove_decorator_dependencies(project):
    _with_decorator_deps(project)
    spec = extract(project)
    steps = [s for s in route(spec, "GET /health")["steps"] if s["role"] == "dependency"]
    assert [s["source"] for s in steps] == ["decorator", "decorator", "param"]
    a, b, user = (s["block"] for s in steps)
    flow(project, [{"op": "reorder", "group": "dependency", "route": "GET /health", "order": [b, a, user]}])
    assert deps(extract(project), "GET /health") == ["slow_b", "slow_a", "current_user"]
    spec = extract(project)
    flow(project, [{"op": "remove", "group": "dependency", "route": "GET /health", "block": b}])
    assert "dependencies=[Depends(slow_a)]" in (project / MAIN).read_text()
    flow(project, [{"op": "remove", "group": "dependency", "route": "GET /health", "block": a}])
    text = (project / MAIN).read_text()
    assert "dependencies" not in text and '@app.get("/health", tags=["system"])' in text
    assert deps(extract(project), "GET /health") == ["current_user"]


def test_dependencies_cannot_swap_across_decorator_and_parameter(project):
    _with_decorator_deps(project)
    spec = extract(project)
    a, b, user = (s["block"] for s in route(spec, "GET /health")["steps"] if s["role"] == "dependency")
    with pytest.raises(EditError, match="always run before"):
        plan_flow(project, APP, spec, [{"op": "reorder", "group": "dependency", "route": "GET /health", "order": [user, a, b]}])


# --------------------------------------------------------------------------- safety net
def test_verification_refuses_a_change_that_breaks_the_app(project):
    broken = (project / MAIN).read_text() + "\nraise RuntimeError('boom')\n"
    with pytest.raises(EditError, match="stop the app from starting"):
        verify_plan(project, APP, {MAIN: ("", broken)}, [])


def test_verification_refuses_a_result_that_is_not_what_was_asked(project):
    spec = extract(project)
    timing, rate, cors = spec["middleware"]
    with pytest.raises(EditError, match="Nothing was changed"):
        verify_plan(project, APP, {}, [lambda new: (_ for _ in ()).throw(EditError("Nothing was changed."))])


def test_edits_never_leave_the_project(project):
    from flowmap.structure import Workspace
    with pytest.raises(EditError, match="outside the project"):
        Workspace(project).doc("../outside.py")


def test_the_project_is_not_touched_by_planning(project):
    before = (project / MAIN).read_text()
    spec = extract(project)
    timing, rate, cors = spec["middleware"]
    plan_flow(project, APP, spec, [{"op": "reorder", "group": "middleware", "order": [cors, timing, rate]}])
    assert (project / MAIN).read_text() == before
