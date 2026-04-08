from __future__ import annotations

from pathlib import Path

from irp.core.store import read_current, read_ledger

_SOURCE_LABELS = {
    "slack": "Slack thread",
    "stdin": "IRP Capture SKILL",
    "cli": "IRP Capture SKILL",
}

def _source_label(raw: str) -> str:
    return _SOURCE_LABELS.get(raw, raw)

def _source_lines(entry: dict) -> list[str]:
    """Return human-readable source + provenance lines for a ledger entry."""
    lines = [f"Source:    {_source_label(entry.get('source', ''))}"]
    if entry.get("source") == "slack":
        ref = entry.get("source_ref", {})
        lines.append(f"Channel:   {ref.get('channel_id', '')}")
        lines.append(f"Thread:    {ref.get('thread_ts', '')}")
    return lines

def run_why(project_root: Path, irp_dir: Path, args) -> dict:
    ledger = read_ledger(irp_dir)
    current = read_current(irp_dir)

    header = [
        "IRP",
        f"Project: {project_root}",
        "Command: why",
        "",
    ]

    if args.id:
        matches = [x for x in ledger if x.get("id") == args.id]
        if not matches:
            return {
                "command": "why",
                "status": "not_found",
                "text": "\n".join(header + [f"No IRP entry found for id {args.id}"]),
            }

        entry = matches[0]
        lines = [
            f"IRP: {entry.get('id', '')}",
            f"What: {entry.get('what', '')}",
            f"Why: {entry.get('why', '')}",
            f"Confidence: {entry.get('confidence', '')}",
            f"Timestamp: {entry.get('timestamp', '')}",
        ] + _source_lines(entry) + [
            "",
            "Source of truth: project .irp/current.json (shared bridge)",
        ]

        return {
            "command": "why",
            "status": "ok",
            "entry": entry,
            "text": "\n".join(header + lines),
        }

    active = current.get("active", [])
    if not active:
        return {
            "command": "why",
            "status": "empty",
            "text": "\n".join(header + ["No active IRP context found."]),
        }

    latest = active[-1]
    lines = [
        f"Latest active decision: {latest.get('id', 'unknown')}",
        f"What: {latest.get('what', '')}",
        f"Why: {latest.get('why', '')}",
        f"Timestamp: {latest.get('timestamp', '')}",
    ] + _source_lines(latest) + [
        "",
        "Source of truth: project .irp/current.json (shared bridge)",
    ]

    return {
        "command": "why",
        "status": "ok",
        "latest": latest,
        "active_count": len(active),
        "text": "\n".join(header + lines),
    }