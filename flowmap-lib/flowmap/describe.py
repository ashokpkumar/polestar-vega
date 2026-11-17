"""Optional: ask an LLM for a one-sentence summary of every block that has no docstring.

Works with any OpenAI-compatible chat endpoint, including a local Ollama server, so
private code does not have to leave your machine:

    FLOWMAP_AI_BASE_URL   default https://api.openai.com/v1   (Ollama: http://localhost:11434/v1)
    FLOWMAP_AI_KEY        API key (any text for Ollama)
    FLOWMAP_AI_MODEL      model name, e.g. gpt-4o-mini or llama3.1
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

PROMPT = (
    "You document a FastAPI backend for people who will not read the code. "
    "In ONE plain sentence (max 25 words) say what this {kind} does to the request or the data. "
    "Mention concrete limits, checks or transformations with their numbers. No preamble.\n\n"
    "Name: {name}\n\n```python\n{code}\n```"
)


def _ask(prompt: str) -> str:
    base = os.environ.get("FLOWMAP_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("FLOWMAP_AI_KEY", "")
    model = os.environ.get("FLOWMAP_AI_MODEL", "")
    if not model:
        raise RuntimeError("Set FLOWMAP_AI_MODEL (and FLOWMAP_AI_KEY / FLOWMAP_AI_BASE_URL) first.")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return " ".join(data["choices"][0]["message"]["content"].split())


def describe(spec: dict, root=".", overwrite: bool = False, log=print) -> int:
    """Fill ``summary`` for blocks in place. Returns how many were written."""
    root = Path(root).resolve()
    count = 0
    for block in spec["blocks"].values():
        if block["kind"] not in ("function", "middleware", "model") or not block.get("file"):
            continue
        if block.get("external"):
            continue
        if block.get("summary") and not (overwrite and block.get("summary_source") != "manual"):
            continue
        lines = (root / block["file"]).read_text(encoding="utf-8").splitlines()
        code = "\n".join(lines[block["line"] - 1: block["end_line"]])[:6000]
        try:
            block["summary"] = _ask(PROMPT.format(kind=block["kind"], name=block["name"], code=code))
        except Exception as exc:
            log(f"  ! {block['name']}: {exc}")
            continue
        block["summary_source"] = "ai"
        count += 1
        log(f"  {block['name']}: {block['summary']}")
    return count
