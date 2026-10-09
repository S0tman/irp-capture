from __future__ import annotations

from pathlib import Path

from irp.core.store import UNCONFIRMED_LABEL, decision_rows_for_id, is_unconfirmed, read_active, read_ledger

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
    """Explain one decision, or the latest active one.

    `irp why <id>` is a human view: it will show a legacy bootstrap guess, labelled
    as unconfirmed. Callers that speak to an agent (the MCP server) pass
    `args.confirmed_only = True`, and a guess is then reported as not found.
    The "latest active decision" is always a confirmed one.
    """
    ledger = read_ledger(irp_dir)
    only_confirmed = bool(getattr(args, "confirmed_only", False))

    header = [
        "IRP",
        f"Project: {project_root}",
        "Command: why",
        "",
    ]

    if args.id:
        # Decision rows only: a retired guess also has a retirement event under
        # its id, and that event is not a decision. A confirmed decision always
        # wins over a guess that happens to share its id (older versions could
        # hand out one id twice). A guess is shown only when nothing else has the
        # id, labelled, and never to an agent.
        confirmed, guesses = decision_rows_for_id(ledger, args.id)
        matches = confirmed or ([] if only_confirmed else guesses)
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
        if is_unconfirmed(entry):
            lines[1:1] = [
                f"Status: {UNCONFIRMED_LABEL}",
                "Nobody confirmed this as a decision. An older version of `irp bootstrap` guessed it",
                "from git history or documents. Agents, exports and checks ignore it.",
            ]

        if is_unconfirmed(entry):
            # Not "ok": a caller reading the JSON must not take a guess for a decision.
            return {
                "command": "why",
                "status": "unconfirmed",
                "unconfirmed": True,
                "entry": entry,
                "text": "\n".join(header + lines),
            }
        return {
            "command": "why",
            "status": "ok",
            "entry": entry,
            "text": "\n".join(header + lines),
        }

    # An old current.json can still hold unconfirmed bootstrap guesses; then the
    # list is recomputed from the ledger (nothing is written).
    active, _ = read_active(irp_dir)
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