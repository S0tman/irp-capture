#!/usr/bin/env python3
"""
IRP REST API v0 — local-first HTTP interface to the IRP substrate.

Endpoints:
  GET  /health               — liveness check
  GET  /decisions            — active decisions (current.json)
  GET  /decisions/{id}       — specific decision from ledger
  POST /capture              — write a new decision
  POST /check                — check a proposal for conflicts

Run:
  python3 -m irp.api.server --project-root /path/to/project
  uvicorn irp.api.server:app --port 3100

All reads/writes go to the project's .irp/. --project-root names the project;
without it the project is found from the current folder (IRP_PROJECT_ROOT, then
walking up). With no project, `python3 -m irp.api.server` refuses to start.
Started with uvicorn directly, it answers 503 on every route, /health
included, until there is a project.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

# Ensure package is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    print("[irp-api] Missing dependencies. Run: pip install irp-capture[api]")
    sys.exit(1)

from irp.core.store import (
    ProjectNotFound,
    decision_rows_for_id,
    find_project,
    read_active,
    read_ledger,
    append_ledger_entry,
    next_irp_id,
    rebuild_current,
    write_current,
)
from irp.core.commands.check import run_check

# ── Parse --project-root (for direct invocation) ─────────────────────────────

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--project-root", default=None)
parser.add_argument("--port", type=int, default=3100)
parser.add_argument("--host", default="127.0.0.1")
_args, _ = parser.parse_known_args()

# Named with --project-root: that folder only. Otherwise found from here.
_NAMED = _args.project_root is not None
PROJECT_ROOT = Path(os.path.abspath(_args.project_root if _NAMED else os.getcwd()))

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="IRP API",
    description="Local-first REST interface to the IRP decision substrate.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _find():
    """The project, found on each call. Raises ProjectNotFound; creates nothing."""
    return find_project(project=PROJECT_ROOT) if _NAMED else find_project(start=PROJECT_ROOT)


def get_project(command: str = "why", write: bool = False):
    try:
        return _find()
    except ProjectNotFound as exc:
        detail = exc.write_message(command) if write else exc.read_message(command)
        raise HTTPException(status_code=503, detail=detail) from None


def get_irp_dir() -> Path:
    return get_project().irp_dir

# ── Models ────────────────────────────────────────────────────────────────────

class CaptureRequest(BaseModel):
    what: str
    why: str = ""
    confidence: str = "medium"
    source: str = "api"
    tags: list[str] = []
    context: Optional[dict[str, Any]] = None

class CheckRequest(BaseModel):
    proposal: str

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    project = get_project()
    irp_dir = project.irp_dir
    return {
        "status": "ok",
        "project_root": str(project.root),
        "irp_dir": str(irp_dir),
        "substrate": {
            "ledger": (irp_dir / "ledger.jsonl").exists(),
            "current": (irp_dir / "current.json").exists(),
        },
    }

@app.get("/decisions")
def get_decisions():
    """Return active decisions from current.json (last 10).

    Unconfirmed bootstrap guesses (legacy ledger lines) are never served as
    decisions, even if an old current.json still lists them.
    """
    irp_dir = get_irp_dir()
    active, _ = read_active(irp_dir)
    return {
        "count": len(active),
        "decisions": active,
    }

@app.get("/decisions/{decision_id}")
def get_decision(decision_id: str):
    """Return a specific decision by ID from the full ledger.

    A legacy bootstrap guess is not a decision, so it is answered with a 404.
    """
    irp_dir = get_irp_dir()
    ledger = read_ledger(irp_dir)
    # Decision rows only (a retirement event shares its target's id and is not a
    # decision). A confirmed decision wins over a guess that shares its id.
    confirmed, guesses = decision_rows_for_id(ledger, decision_id)
    if confirmed:
        return confirmed[0]
    if guesses:
        raise HTTPException(
            status_code=404,
            detail=f"{decision_id} is an unconfirmed bootstrap guess, not a decision",
        )
    raise HTTPException(status_code=404, detail=f"{decision_id} not found in ledger")

@app.post("/capture", status_code=201)
def capture(req: CaptureRequest):
    """Write a new decision to the ledger."""
    if not req.what.strip():
        raise HTTPException(status_code=400, detail="'what' field is required")

    irp_dir = get_project("capture", write=True).irp_dir
    ledger = read_ledger(irp_dir)

    entry: dict[str, Any] = {
        "type": "decision",
        "id": next_irp_id(ledger),
        "what": req.what.strip(),
        "why": req.why.strip(),
        "confidence": req.confidence,
        "timestamp": date.today().isoformat(),
        "source": req.source,
        "tags": req.tags,
    }
    if req.context:
        entry["context"] = req.context

    append_ledger_entry(irp_dir, entry)
    updated = read_ledger(irp_dir)
    write_current(irp_dir, rebuild_current(updated))

    return {"ok": True, "id": entry["id"], "entry": entry}

@app.post("/check")
def check(req: CheckRequest):
    """Check a proposal against active decisions for conflicts."""
    if not req.proposal.strip():
        raise HTTPException(status_code=400, detail="'proposal' field is required")

    project = get_project("check")

    class _Args:
        proposal = req.proposal.strip()

    result = run_check(project.root, project.irp_dir, _Args())
    return result

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        _project = _find()
    except ProjectNotFound as exc:
        print(f"[irp-api] No IRP project at {exc.searched_from}, so the API didn't start. "
              f"Start one with: {exc.hint}", file=sys.stderr)
        sys.exit(1)
    print(f"[irp-api] Project root: {_project.root}")
    print(f"[irp-api] Starting on http://{_args.host}:{_args.port}")
    uvicorn.run(app, host=_args.host, port=_args.port)
