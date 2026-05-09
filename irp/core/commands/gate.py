"""Runtime Gate — IRP-US-011.

Machine-readable JSON evaluator for agentic loops.
Exit codes: 0=allow, 10=warn, 20=block.
--strict upgrades warn to block (exit 20).
Always outputs JSON — no human-prose mode.
"""
from __future__ import annotations

import json
from pathlib import Path

from store import read_ledger
from resolver import resolve


def run_gate(project_root: Path, irp_dir: Path, args) -> dict:
    query: str = args.query
    tag: str | None = getattr(args, "tag", None)
    scope: str | None = getattr(args, "scope", None)
    strict: bool = getattr(args, "strict", False)

    ledger = read_ledger(irp_dir)
    result = resolve(query, ledger, tag=tag, scope=scope)

    verdict = result.verdict  # "clear" | "warn" | "block"

    top_match_dict: dict | None = None
    if result.top_match:
        tm = result.top_match
        top_match_dict = {
            "id": tm.id,
            "decision": tm.decision,
            "score": tm.score,
            "matched_on": tm.matched_on,
            "confidence": tm.confidence,
            "tags": tm.tags,
            "timestamp": tm.timestamp,
        }

    defer_question: str | None = None
    if verdict in ("warn", "block") and result.top_match:
        tm = result.top_match
        defer_question = (
            f"Should we proceed given {tm.id} states: '{tm.decision}'?"
        )

    payload: dict = {
        "verdict": verdict,
        "score": result.score,
        "action": query,
        "top_match": top_match_dict,
        "active_count": result.active_count,
        "superseded_count": result.superseded_count,
    }
    if defer_question:
        payload["defer_question"] = defer_question

    # Attach exit_code for callers that want it inline (gate handler sets it separately)
    _exit = _exit_code(verdict, strict)
    payload["exit_code"] = _exit

    return payload


def _exit_code(verdict: str, strict: bool) -> int:
    if verdict == "clear":
        return 0
    if verdict == "warn":
        return 20 if strict else 10
    # block
    return 20
