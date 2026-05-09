"""irp check — conflict preview powered by the Decision Resolver."""
from __future__ import annotations

from pathlib import Path

from store import read_ledger
from resolver import resolve as _resolve

_DIVIDER = "─" * 48

_SOURCE_LABELS = {
    "slack": "Slack thread",
    "stdin": "IRP Capture SKILL",
    "cli":   "IRP Capture SKILL",
}


def _source_label(raw: str) -> str:
    return _SOURCE_LABELS.get(raw, raw) if raw else ""


def run_check(project_root: Path, irp_dir: Path, args) -> dict:
    proposal = args.proposal.strip()
    ledger   = read_ledger(irp_dir)
    result   = _resolve(proposal, ledger)

    if result.verdict == "clear":
        lines = [
            "Checking proposal against decision ledger (.irp/ledger.jsonl)...",
            "",
            "✓  No conflicts detected against active decisions.",
            "",
            f"  Proposal: {proposal}",
            f"  Checked:  {result.active_count} active decision{'s' if result.active_count != 1 else ''}",
            f"  Skipped:  {result.superseded_count} superseded",
            "",
            _DIVIDER,
            "Source of truth: .irp/ledger.jsonl",
        ]
        return {
            "command": "check",
            "status": "clear",
            "proposal": proposal,
            "checked": result.active_count,
            "superseded": result.superseded_count,
            "text": "\n".join(lines),
        }

    # conflict or warn
    top = result.top_match
    icon = "⚠ " if result.verdict == "warn" else "✗ "
    lines = [
        "Checking proposal against decision ledger (.irp/ledger.jsonl)...",
        "",
        f"{icon} Potential conflict with an active decision",
        "",
        f"  Decision:   {top.id}",
        f"  What:       {top.decision}",
        f"  Reasoning:  {top.reasoning[:200]}{'…' if len(top.reasoning) > 200 else ''}",
        f"  Confidence: {top.confidence}",
        f"  Source:     {_source_label(top.source) or top.source}",
        f"  Timestamp:  {top.timestamp}",
        f"  Matched on: {', '.join(top.matched_on)}",
        "",
        f"  Active checked:  {result.active_count}",
        f"  Superseded skip: {result.superseded_count}",
    ]

    if len(result.conflicts) > 1:
        lines += [
            "",
            f"  Other conflicts: {len(result.conflicts) - 1} more",
            "  Run `irp resolve \"<proposal>\"` to see all conflicts ranked.",
        ]

    lines += ["", _DIVIDER, "Source of truth: .irp/ledger.jsonl"]

    return {
        "command": "check",
        "status": "conflict",
        "verdict": result.verdict,
        "proposal": proposal,
        "match_id": top.id,
        "matched_on": top.matched_on,
        "score": result.score,
        "checked": result.active_count,
        "superseded": result.superseded_count,
        "text": "\n".join(lines),
    }
