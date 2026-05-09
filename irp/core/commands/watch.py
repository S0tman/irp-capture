"""Streaming Gate — IRP-US-012.

Reads one action per line from stdin (or --input FILE), evaluates each
against active decisions via the gate, and emits one JSON verdict line per
input. Exit code reflects the worst verdict seen across all lines.

Exit codes: 0=all clear, 10=any warn, 20=any block.
--strict upgrades warn exits to 20.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from store import read_ledger
from commands.gate import run_gate as _gate_evaluate, _exit_code


def _parse_line(line: str) -> str | None:
    """Return action string from plain text or {"action": "..."} JSON. None if blank."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and "action" in obj:
            return str(obj["action"])
    except (json.JSONDecodeError, ValueError):
        pass
    return stripped


def run_watch(project_root: Path, irp_dir: Path, args) -> dict:
    tag: str | None = getattr(args, "tag", None)
    scope: str | None = getattr(args, "scope", None)
    strict: bool = getattr(args, "strict", False)
    input_file: str | None = getattr(args, "input", None)

    # Choose input source
    if input_file:
        try:
            lines = Path(input_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return {
                "error": f"Cannot open --input file: {exc}",
                "_watch_exit": 1,
            }
    else:
        lines = sys.stdin.read().splitlines()

    ledger = read_ledger(irp_dir)
    worst = "clear"  # track worst verdict

    results = []

    for raw in lines:
        action = _parse_line(raw)
        if action is None:
            continue

        # Reuse gate internals directly
        from resolver import resolve
        result = resolve(action, ledger, tag=tag, scope=scope)
        verdict = result.verdict

        top_match_dict = None
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

        defer_question = None
        if verdict in ("warn", "block") and result.top_match:
            tm = result.top_match
            defer_question = (
                f"Should we proceed given {tm.id} states: '{tm.decision}'?"
            )

        line_result = {
            "verdict": verdict,
            "score": result.score,
            "action": action,
            "top_match": top_match_dict,
            "active_count": result.active_count,
            "superseded_count": result.superseded_count,
        }
        if defer_question:
            line_result["defer_question"] = defer_question

        # Emit immediately (line-buffered)
        print(json.dumps(line_result, ensure_ascii=False), flush=True)
        results.append(line_result)

        # Track worst verdict: clear < warn < block
        if verdict == "block":
            worst = "block"
        elif verdict == "warn" and worst == "clear":
            worst = "warn"

    exit_code = _exit_code(worst, strict)
    return {"_watch_exit": exit_code, "_results_count": len(results)}
