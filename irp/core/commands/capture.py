from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from irp.core.store import append_ledger_entry, next_irp_id, read_ledger, rebuild_current, write_current
from irp.integrations import dispatch as _dispatch

def confirm_token(prompt: str = "Confirm capture? [c=confirm / s=skip]: ") -> bool:
    value = input(prompt).strip().lower()
    return value == "c"

def read_candidate_from_stdin() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("No stdin payload provided")
    candidate = json.loads(raw)
    if not isinstance(candidate, dict):
        raise ValueError("stdin payload must be a JSON object")
    return candidate

def read_candidate_interactive() -> dict[str, Any]:
    what = input("What was decided? ").strip()
    why = input("Why does it matter? ").strip()
    confidence = input("Confidence [low/medium/high]: ").strip().lower() or "medium"

    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"

    return {
        "type": "decision",
        "what": what,
        "why": why,
        "confidence": confidence,
        "timestamp": date.today().isoformat(),
        "source": "interactive",
        "tags": [],
    }

def run_capture(project_root: Path, irp_dir: Path, args) -> dict:
    ledger = read_ledger(irp_dir)

    candidate = read_candidate_from_stdin() if args.stdin else read_candidate_interactive()

    candidate.setdefault("type", "decision")
    candidate.setdefault("timestamp", date.today().isoformat())
    candidate.setdefault("confidence", "medium")
    candidate.setdefault("source", "interactive" if not args.stdin else "stdin")
    candidate.setdefault("tags", [])

    candidate["id"] = next_irp_id(ledger)

    preview = json.dumps(candidate, indent=2, ensure_ascii=False)

    header = [
        "IRP",
        f"Project: {project_root}",
        "Command: capture",
        "",
        "Candidate preview:",
        preview,
        "",
    ]

    if not args.stdin and not confirm_token():
        return {
            "command": "capture",
            "status": "skipped",
            "candidate": candidate,
            "text": "\n".join(header + ["Capture skipped."]),
        }

    append_ledger_entry(irp_dir, candidate)

    updated_ledger = read_ledger(irp_dir)
    current = rebuild_current(updated_ledger)
    write_current(irp_dir, current)

    integrations = _dispatch.run(candidate, project_root)

    integration_lines = []
    for r in integrations:
        name = r.get("integration", "unknown")
        status = r.get("status", "?")
        if status == "ok":
            detail = r.get("path") or r.get("id") or ""
            integration_lines.append(f"  ✓ {name}: {detail}")
        elif status == "error":
            integration_lines.append(f"  ✗ {name}: {r.get('error', '')}")

    return {
        "command": "capture",
        "status": "captured",
        "entry": candidate,
        "integrations": integrations,
        "text": "\n".join(header + [f"Captured {candidate['id']}"] + integration_lines),
    }