from __future__ import annotations

from pathlib import Path

from irp.core.resolver import active_decisions
from irp.core.store import read_current, read_ledger

# How many active decisions inherit hands an agent. The most recent ones win;
# older active decisions are still reachable through `irp why <id>`.
INHERIT_LIMIT = 50


def run_inherit(project_root: Path, irp_dir: Path, args) -> dict:
    """Return the active decisions: superseded and retired ones are left out."""
    ledger = read_ledger(irp_dir)
    if any(e.get("type") == "decision" for e in ledger):
        active, superseded_count = active_decisions(ledger)
    else:
        # A project with only current.json (no ledger) keeps working.
        active, superseded_count = read_current(irp_dir).get("active", []), 0

    shown = active[-INHERIT_LIMIT:]
    omitted = len(active) - len(shown)

    lines = [
        "IRP",
        f"Project: {project_root}",
        "Command: inherit",
        "",
    ]

    if not shown:
        lines.append("No active IRP context found.")
    else:
        lines.append("Active IRP context:")
        if omitted:
            lines.append(f"({omitted} older active decisions not shown; use `irp why <id>`.)")
        for item in shown:
            lines.append(f"- {item.get('id', 'unknown')}: {item.get('what', '')}")
            if item.get("why"):
                lines.append(f"  Why: {item['why']}")
            if item.get("alternatives"):
                lines.append(f"  Rejected: {item['alternatives']}")
            supersedes = item.get("supersedes")
            if supersedes:
                refs = supersedes if isinstance(supersedes, list) else [supersedes]
                lines.append(f"  Supersedes: {', '.join(refs)}")

    return {
        "command": "inherit",
        "project_root": str(project_root),
        "active_count": len(active),
        "omitted": omitted,
        "superseded_count": superseded_count,
        "active": shown,
        "text": "\n".join(lines),
    }
