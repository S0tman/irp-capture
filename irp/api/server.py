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

All reads/writes go to .irp/ under --project-root.
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
    decision_rows_for_id,
    ensure_irp_dir,
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
parser.add_argument("--project-root", default=os.getcwd())
parser.add_argument("--port", type=int, default=3100)
parser.add_argument("--host", default="127.0.0.1")
_args, _ = parser.parse_known_args()

PROJECT_ROOT = Path(os.path.abspath(_args.project_root))

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

def get_irp_dir() -> Path:
    return ensure_irp_dir(PROJECT_ROOT)

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
    irp_dir = get_irp_dir()
    return {
        "status": "ok",
        "project_root": str(PROJECT_ROOT),
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

    irp_dir = get_irp_dir()
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

    irp_dir = get_irp_dir()

    class _Args:
        proposal = req.proposal.strip()

    result = run_check(PROJECT_ROOT, irp_dir, _Args())
    return result

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[irp-api] Project root: {PROJECT_ROOT}")
    print(f"[irp-api] Starting on http://{_args.host}:{_args.port}")
    uvicorn.run(app, host=_args.host, port=_args.port)
