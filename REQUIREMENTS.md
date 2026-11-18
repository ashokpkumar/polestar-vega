# flowmap — Requirements

Editable flow diagrams for FastAPI applications.

- Status: prototype 0.1.0
- Language: Python (library + CLI), single-file HTML/JS diagram page
- Last updated: 17 Nov 2026

Status markers used below: ✅ done in 0.1.0 · 🟡 partly done · ⬜ not started

---

## 1. Problem

Understanding what an existing FastAPI API actually does means reading the code.
Regular flow diagrams and tools like fastapi-voyager show routes and models, but
they miss what happens in between: middleware that limits or transforms requests,
dependencies that reject calls, service functions, and where the data finally lands.
Changing behaviour (for example raising a limit from 5 to 10) still means finding
and editing the right line by hand.

## 2. Goal

A Python library that, added to an existing FastAPI project with no code changes:

1. Draws every API as a visual flow, block by block, in the style of fastapi-voyager.
2. Explains each block well enough that you understand the API without reading the code.
3. Records the whole app structure in a spec file.
4. Lets you change the behaviour by editing the diagram, and writes the change into the code
   without you writing a prompt or editing code yourself.

## 3. Users

- The developer who owns the codebase and wants a map of it.
- New team members onboarding onto an unfamiliar FastAPI project.
- Reviewers who want to see flow changes in a pull request.

---

## 4. Functional requirements

### 4.1 Works on existing code

| ID | Requirement | Status |
|---|---|---|
| F1.1 | Point the tool at an existing app (`module:app`) with no changes to the app's code | ✅ |
| F1.2 | Install as a normal Python package (`pip install`) with a CLI command | ✅ |
| F1.3 | Support app factories (a function that returns the app) | ✅ |
| F1.4 | Option to mount the diagram inside the app (`mount_flowmap(app)`, like Swagger's `/docs`); editing off unless `allow_edit=True` | ✅ |
| F1.5 | Support mounted sub-applications and `APIRouter` prefixes | 🟡 routers work; mounted sub-apps not drawn |

### 4.2 Flow diagram per API

Each API is drawn as a vertical flow from request to response, in execution order.

| ID | Requirement | Status |
|---|---|---|
| F2.1 | Show middleware in the order a request passes through them (outermost first) | ✅ |
| F2.2 | Show dependencies (`Depends`), including nested ones, and security schemes | ✅ |
| F2.3 | Show input validation: path, query, header, cookie and body parameters with their rules | ✅ |
| F2.4 | Show request body and response Pydantic models with field rules | ✅ |
| F2.5 | Show the handler and every project function it calls, as a call tree | ✅ (6 levels deep) |
| F2.6 | Show where data lands: database, HTTP calls, cache, files, queues, with SQL text when literal | ✅ |
| F2.7 | Show what data is passed between blocks (arguments in, return value out) on the arrows | ⬜ |
| F2.8 | Show only the middleware that actually applies to a path (skip ones that bypass it) | ⬜ |
| F2.9 | Show exception handlers and background tasks | ⬜ |
| F2.10 | Show WebSocket routes | ⬜ |
| F2.11 | Calls through objects whose type can't be inferred statically (dynamic dispatch, callbacks) | ⬜ |

### 4.3 Explaining each block

| ID | Requirement | Status |
|---|---|---|
| F3.1 | One-line description per block, taken from the docstring | ✅ |
| F3.2 | AI-generated description for blocks without a docstring (`flowmap describe`) | ✅ (tested against a mock endpoint only) |
| F3.3 | AI must work with a local model (Ollama) so private code stays on the machine | ✅ OpenAI-compatible endpoint |
| F3.4 | Edit a description by hand in the UI; manual and AI descriptions survive refreshes while the code is unchanged | ✅ |
| F3.5 | List what each block can do to the request, found from the code: rejects (status + message), early returns, headers set, headers/cookies read | ✅ |
| F3.6 | Detect data transformations (fields added, removed, renamed) | ⬜ |
| F3.7 | Show the block's source code in the detail panel, with file and line | ✅ |
| F3.8 | Show which other APIs use the same block | ✅ |
| F3.9 | Mark AI-written descriptions clearly so they can be checked | ✅ |

### 4.4 Spec file

| ID | Requirement | Status |
|---|---|---|
| F4.1 | Record the app's full structure in one file (`flowmap.json`) | ✅ |
| F4.2 | File is readable, diff-friendly, and suitable for committing to git | ✅ |
| F4.3 | Code is the source of truth; the spec is always rebuilt from code | ✅ |
| F4.4 | Each block records file, line range, content hash, description, effects, editable values | ✅ |
| F4.5 | Optional YAML or XML export | ⬜ |
| F4.6 | Versioned schema, documented, with a JSON Schema file | 🟡 version field exists; schema not written |

### 4.5 Editing the diagram changes the code

| ID | Requirement | Status |
|---|---|---|
| F5.1 | Change a value in the diagram (e.g. a limit 0–5 → 0–10) and write it into the code | ✅ |
| F5.2 | Show a diff and ask for approval before changing any file | ✅ |
| F5.3 | Edit values in `flowmap.json` by hand and apply with `flowmap apply` | ✅ |
| F5.4 | Editable values: Pydantic `Field` rules and defaults, `Query`/`Path`/`Header` rules, parameter defaults, `add_middleware` arguments, decorator arguments, module constants, numbers in comparisons | ✅ |
| F5.5 | Refuse an edit if the file changed since the diagram was built, or the value type is wrong | ✅ |
| F5.6 | Value edits are exact (no AI), preserving formatting and comments | ✅ |
| F5.7 | Structural edits in the diagram: add, remove or reorder blocks (e.g. add a middleware, add a validation step); AI generates the code change | ⬜ |
| F5.8 | Free-text change on a block ("also log the user id here"); AI generates the change, shown as a diff | ⬜ |
| F5.9 | Option to create a git branch / commit for each applied change | ⬜ |
| F5.10 | Run the project's tests after applying a change and report the result | ⬜ |

### 4.6 User interface

| ID | Requirement | Status |
|---|---|---|
| F6.1 | Look and feel close to fastapi-voyager: clean, interactive, explorable | ✅ |
| F6.2 | API list grouped by tag, with search | ✅ |
| F6.3 | Pan, zoom, fit to screen | ✅ |
| F6.4 | Colour per block type (middleware, dependency, validation, handler, data, model) | ✅ |
| F6.5 | Light and dark mode | ✅ |
| F6.6 | Pending-changes bar with discard and undo per value | ✅ |
| F6.7 | Whole-app overview (all APIs and shared blocks in one graph) | ⬜ |
| F6.8 | Click a block to open the file in VS Code | ⬜ |
| F6.9 | Export a diagram as PNG/SVG for documentation | ⬜ |
| F6.10 | Collapse / expand call-tree branches | ⬜ |

---

## 5. Non-functional requirements

| ID | Requirement | Status |
|---|---|---|
| N1 | Python 3.9+; only dependency is FastAPI | ✅ |
| N2 | No build step for the UI (single HTML file served by the library) | ✅ |
| N3 | Server listens on 127.0.0.1 only; rejects cross-site requests, since it can modify files | ✅ |
| N4 | Never write outside the project folder | ✅ |
| N5 | No code leaves the machine unless the user configures an AI endpoint | ✅ |
| N6 | Extraction runs in a fresh process so code changes are always picked up | ✅ |
| N7 | Extract a large app (200+ routes) in under 10 seconds | ⬜ not measured |
| N8 | Automated tests for extraction, editing, and refusal cases | ✅ 6 tests |
| N9 | Tested on real-world projects (beyond the sample app) | ⬜ |
| N10 | CI on GitHub Actions (tests on Python 3.9–3.13) | 🟡 workflow written, not yet run on GitHub |
| N11 | Published to PyPI | 🟡 package `polestar-vega` built and checked; release workflow written |

---

## 6. Known limitations (0.1.0)

- Call tracing is static; calls through untyped objects, dynamic dispatch and callbacks are not followed.
- Data calls are recognised by library and common names (`db.execute`, `session.add`); unusual names are missed.
- Every middleware is drawn on every API, even if its code skips some paths.
- Mounted sub-applications, WebSocket routes and background tasks are not drawn.
- Only value edits; no structural edits yet.

---

## 7. Suggested roadmap

1. **Hardening (0.2):** run on 2–3 real projects, fix tracing gaps, add CI, JSON Schema for the spec (N9, N10, F4.6).
2. **Richer flow (0.3):** data passed on arrows, per-path middleware, exception handlers, background tasks, collapse/expand (F2.7–F2.9, F6.10).
3. **AI edits (0.4):** free-text and structural edits from the diagram, always as an approved diff, optional git branch and test run (F5.7–F5.10).
4. **Polish (0.5):** whole-app overview, VS Code links, image export, mount-in-app option, PyPI release (F6.7–F6.9, F1.4, N11).

---

## 8. Repository layout

```
flowmap/extract.py    reads the app, builds the spec
flowmap/edit.py       writes value edits back into source files
flowmap/server.py     local web server for the diagram
flowmap/describe.py   optional LLM descriptions
flowmap/cli.py        flowmap extract | serve | apply | describe
flowmap/static/       the diagram page (one HTML file)
examples/sample_app/  small FastAPI app to try it on
tests/                pytest suite
```
