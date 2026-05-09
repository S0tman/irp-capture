"""Living Mod — IRP-US-013.

irp mod supersede <IRP-ID> --decision "..." --reason "..."
    Creates a new decision that supersedes the target.

irp mod retire <IRP-ID> --reason "..."
    Appends a retirement event; resolver excludes the target from active set.

irp mod list
    Shows recent modification events from the ledger.
"""
from __future__ import annotations

import sys
from datetime import date, timezone, datetime
from pathlib import Path

from store import (
    read_ledger,
    append_ledger_entry,
    next_irp_id,
    rebuild_current,
    write_current,
)


def run_mod(project_root: Path, irp_dir: Path, args) -> dict:
    action = getattr(args, "mod_action", None)
    if action == "supersede":
        return _supersede(irp_dir, args)
    if action == "retire":
        return _retire(irp_dir, args)
    if action == "list":
        return _list(irp_dir, args)
    return {"error": f"Unknown mod action: {action}"}


# ── supersede ─────────────────────────────────────────────────────────────────

def _supersede(irp_dir: Path, args) -> dict:
    old_id: str = args.target_id
    new_decision: str | None = getattr(args, "decision", None)
    reason: str | None = getattr(args, "reason", None)
    confidence: str = getattr(args, "confidence", "high") or "high"

    if not new_decision:
        print("IRP mod error: --decision is required for supersede", file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("IRP mod error: --reason is required for supersede", file=sys.stderr)
        sys.exit(1)

    ledger = read_ledger(irp_dir)

    # Confirm old_id exists in ledger
    existing_ids = {e.get("id") for e in ledger if e.get("type") == "decision"}
    if old_id not in existing_ids:
        print(f"IRP mod error: {old_id} not found in ledger", file=sys.stderr)
        sys.exit(1)

    new_id = next_irp_id(ledger)
    timestamp = date.today().isoformat()

    entry = {
        "type": "decision",
        "id": new_id,
        "decision": new_decision,
        "reasoning": reason,
        "supersedes": old_id,
        "confidence": confidence,
        "tags": [],
        "timestamp": timestamp,
        "source": "irp mod",
    }

    append_ledger_entry(irp_dir, entry)

    # Rebuild current.json to keep it fresh
    updated_ledger = read_ledger(irp_dir)
    write_current(irp_dir, rebuild_current(updated_ledger))

    return {
        "action": "supersede",
        "old_id": old_id,
        "new_id": new_id,
        "decision": new_decision,
        "reason": reason,
    }


# ── retire ────────────────────────────────────────────────────────────────────

def _retire(irp_dir: Path, args) -> dict:
    target_id: str = args.target_id
    reason: str | None = getattr(args, "reason", None)

    if not reason:
        print("IRP mod error: --reason is required for retire", file=sys.stderr)
        sys.exit(1)

    ledger = read_ledger(irp_dir)

    existing_ids = {e.get("id") for e in ledger if e.get("type") == "decision"}
    if target_id not in existing_ids:
        print(f"IRP mod error: {target_id} not found in ledger", file=sys.stderr)
        sys.exit(1)

    timestamp = date.today().isoformat()

    event = {
        "type": "retirement",
        "id": target_id,
        "reason": reason,
        "timestamp": timestamp,
        "source": "irp mod",
    }

    append_ledger_entry(irp_dir, event)

    # Rebuild current.json — retired decision no longer appears in active
    updated_ledger = read_ledger(irp_dir)
    write_current(irp_dir, rebuild_current(updated_ledger))

    return {
        "action": "retire",
        "retired_id": target_id,
        "reason": reason,
    }


# ── list ──────────────────────────────────────────────────────────────────────

def _list(irp_dir: Path, args) -> dict:
    ledger = read_ledger(irp_dir)

    mod_types = {"retirement"}
    events = []

    for entry in ledger:
        entry_type = entry.get("type", "")
        is_retirement = entry_type == "retirement"
        is_supersession = (
            entry_type == "decision" and entry.get("supersedes")
        )
        if is_retirement or is_supersession:
            events.append(entry)

    # Newest first (sort by timestamp string — ISO format sorts correctly)
    events.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)

    return {"events": events}
