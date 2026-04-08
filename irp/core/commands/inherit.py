from __future__ import annotations

from pathlib import Path

from irp.core.store import read_current


def run_inherit(project_root: Path, irp_dir: Path, args) -> dict:
    current = read_current(irp_dir)
    active = current.get("active", [])

    lines = [
        "IRP",
        f"Project: {project_root}",
        "Command: inherit",
        "",
    ]

    if not active:
        lines.append("No active IRP context found.")
    else:
        lines.append("Active IRP context:")
        for item in active:
            item_id = item.get("id", "unknown")
            what = item.get("what", "")
            why = item.get("why", "")
            lines.append(f"- {item_id}: {what}")
            if why:
                lines.append(f"  Why: {why}")

    return {
        "command": "inherit",
        "project_root": str(project_root),
        "active_count": len(active),
        "active": active,
        "text": "\n".join(lines),
    }