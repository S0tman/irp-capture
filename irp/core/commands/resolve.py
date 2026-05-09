"""irp resolve — query the Decision Resolver directly."""
from __future__ import annotations

from pathlib import Path

from store import read_ledger
from resolver import resolve as _resolve

_DIVIDER = "─" * 48

_VERDICT_ICON = {
    "clear": "✓ ",
    "warn":  "⚠  WARN",
    "block": "✗  BLOCK",
}

_SOURCE_LABELS = {
    "slack": "Slack thread",
    "stdin": "IRP Capture SKILL",
    "cli":   "IRP Capture SKILL",
}


def _source_label(raw: str) -> str:
    return _SOURCE_LABELS.get(raw, raw) if raw else ""


def run_resolve(project_root: Path, irp_dir: Path, args) -> dict:
    query = args.query.strip()
    tag   = getattr(args, "tag", None)
    scope = getattr(args, "scope", None)
    top_n = getattr(args, "top", 3)

    ledger = read_ledger(irp_dir)
    result = _resolve(query, ledger, tag=tag, scope=scope)

    lines: list[str] = [
        f"IRP Decision Resolver",
        f"Query:  {query}",
    ]
    if tag:
        lines.append(f"Filter: tag={tag}")
    if scope:
        lines.append(f"Filter: scope={scope}")
    lines.append("")

    # ── verdict banner ────────────────────────────────────────────────────────
    if result.verdict == "clear":
        lines += [
            "✓  No conflicts detected.",
            "",
            f"  Active decisions checked:  {result.active_count}",
            f"  Superseded (skipped):      {result.superseded_count}",
        ]
    else:
        icon = _VERDICT_ICON[result.verdict]
        lines += [
            f"{icon} — {len(result.conflicts)} conflict(s) found",
            "",
            f"  Active decisions checked:  {result.active_count}",
            f"  Superseded (skipped):      {result.superseded_count}",
            f"  Conflict score:            {result.score}",
            "",
        ]

        for i, c in enumerate(result.conflicts[:top_n], 1):
            lines += [
                f"  [{i}] {c.id}  (score: {c.score}, confidence: {c.confidence})",
                f"      Decision:   {c.decision}",
                f"      Reasoning:  {c.reasoning[:120]}{'…' if len(c.reasoning) > 120 else ''}",
                f"      Matched on: {', '.join(c.matched_on)}",
            ]
            if c.confirmed_by:
                lines.append(f"      Confirmed:  {c.confirmed_by}")
            if c.tags:
                lines.append(f"      Tags:       {', '.join(c.tags)}")
            lines.append(f"      Source:     {_source_label(c.source) or c.source}")
            lines.append("")

    lines += [_DIVIDER, "Source of truth: .irp/ledger.jsonl"]

    return {
        "command": "resolve",
        **result.to_dict(),
        "text": "\n".join(lines),
    }
