"""IRP defer mechanic — surface a WARN/BLOCK critique and capture the human resolution.

Usage:
    irp defer "Should users be notified before deletion?"  # explicit question
    collab.py --mode critique "proposal" | irp defer       # piped from critique

Behaviour:
  1. Parse input — from critique JSON (stdin) or a positional question arg
  2. Surface the defer question + verdict + flagged principles
  3. Show any conflicting IRP decisions via keyword check
  4. Prompt the user for their resolution answer
  5. Capture the answer as a new IRP ledger entry (confirmed_by = $USER)
  6. Exit 0 on capture, exit 130 on cancel

Design rules:
  - No LLM calls — deterministic, no inference
  - Human confirmation is mandatory — cannot be skipped
  - The resolution entry links back to the original defer_question in `why`
  - confirmed_by defaults to $USER env var, falls back to "user"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store import append_ledger_entry, next_irp_id, read_config, read_ledger, rebuild_current, write_current


# ── verdict display ──────────────────────────────────────────────────────────

_VERDICT_STYLE = {
    "WARN":  ("⚠ WARN",  "yellow"),
    "BLOCK": ("✗ BLOCK", "red"),
    "CLEAR": ("✓ CLEAR", "green"),
}

_PRINCIPLE_LABELS = {
    "human_control":   "Human Control",
    "transparency":    "Transparency",
    "value_alignment": "Value Alignment",
    "privacy":         "Privacy",
    "security":        "Security",
}


def _fmt_verdict(verdict: str) -> str:
    label, _ = _VERDICT_STYLE.get(verdict.upper(), (verdict, ""))
    return label


def _fmt_principles(flags: list[str]) -> str:
    if not flags:
        return ""
    labels = [_PRINCIPLE_LABELS.get(f, f) for f in flags]
    return ", ".join(labels)


# ── stdin JSON parsing ───────────────────────────────────────────────────────

def _parse_critique_from_stdin() -> dict[str, Any] | None:
    """Read stdin and extract the last valid critique JSON object.

    collab.py prints human-readable output followed by a JSON line as the
    final line of stdout. We scan for the last line that parses as a dict
    with a 'verdict' key.
    """
    if sys.stdin.isatty():
        return None
    try:
        lines = sys.stdin.read().splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ── keyword-based relevance check ───────────────────────────────────────────

def _relevant_decisions(question: str, ledger: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Return up to `limit` active decisions whose what/why overlaps with question words."""
    words = {w.lower() for w in question.split() if len(w) > 3}
    if not words:
        return []

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in ledger:
        text = f"{entry.get('what', '')} {entry.get('why', '')}".lower()
        score = sum(1 for w in words if w in text)
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


# ── interactive prompt ───────────────────────────────────────────────────────

def _prompt_resolution(defer_question: str, verdict: str, principles: list[str],
                       relevant: list[dict[str, Any]],
                       control_level: str = "advanced") -> str | None:
    """Show context and prompt the user for their resolution. Returns None on cancel."""
    sep = "─" * 60
    level = control_level if control_level in ("easy", "medium", "advanced") else "advanced"

    # Header — verbosity depends on control_level.
    print(f"\n{sep}")
    if level == "easy":
        print("  IRP check")
    elif level == "medium":
        print(f"  IRP defer required  {_fmt_verdict(verdict)}")
    else:
        print(f"  IRP DEFER  {_fmt_verdict(verdict)}")
    print(sep)

    # Principles — easy level uses plain language; medium+ uses canonical names.
    if principles and level != "easy":
        if level == "medium":
            print(f"  Principles: {_fmt_principles(principles)}")
        else:
            print(f"  Principles: {_fmt_principles(principles)}")

    # The defer question.
    if level == "easy":
        print(f"\n  Quick check — {defer_question}\n")
    else:
        print(f"\n  {defer_question}\n")

    # Relevant decisions — only shown on advanced.
    if relevant and level == "advanced":
        print("  Relevant decisions:")
        for d in relevant:
            irp_id = d.get("id", "?")
            what = (d.get("what") or "")[:80]
            print(f"    {irp_id} — {what}")
        print()

    # Prompt options copy.
    print("  Options:")
    if level == "easy":
        print("    Type your answer      →  saved")
        print("    Press Ctrl-C          →  skip\n")
    elif level == "medium":
        print("    Enter your resolution →  captures a decision")
        print("    Press Ctrl-C          →  cancel\n")
    else:
        print("    Enter your resolution  →  captures a new IRP decision")
        print("    Press Ctrl-C           →  cancel (no capture)\n")

    # Prompt label.
    if level == "easy":
        prompt_label = "  Your answer: "
    else:
        prompt_label = "  Your resolution: "

    try:
        answer = input(prompt_label).strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled — no entry captured.")
        return None

    if not answer:
        print("  Empty response — no entry captured.")
        return None

    return answer


# ── capture ──────────────────────────────────────────────────────────────────

def _capture_resolution(
    irp_dir: Path,
    answer: str,
    defer_question: str,
    verdict: str,
    principles: list[str],
    reasoning: str,
) -> dict[str, Any]:
    """Append a new ledger entry recording the human resolution."""
    ledger = read_ledger(irp_dir)
    irp_id = next_irp_id(ledger)
    confirmed_by = os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    flags_str = f" Principles flagged: {', '.join(principles)}." if principles else ""
    why = (
        f"Resolved defer question after {verdict} critique.{flags_str} "
        f"Defer question: '{defer_question}'. "
        f"Critique reasoning: {reasoning[:200].rstrip()}{'…' if len(reasoning) > 200 else ''}"
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "id": irp_id,
        "type": "decision",
        "what": answer,
        "why": why,
        "tags": ["defer", "resolution", verdict.lower()],
        "confidence": "high",
        "source": "defer",
        "confirmed_by": confirmed_by,
        "timestamp": timestamp,
    }

    append_ledger_entry(irp_dir, entry)
    updated_ledger = read_ledger(irp_dir)
    current = rebuild_current(updated_ledger)
    write_current(irp_dir, current)

    return entry


# ── public entry point ───────────────────────────────────────────────────────

def run_defer(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    question_arg: str | None = getattr(args, "question", None)
    as_json = bool(getattr(args, "json", False))

    # ── 1. Resolve input source ──────────────────────────────────────────────
    critique: dict[str, Any] = {}
    defer_question: str = ""

    if question_arg:
        # Explicit positional argument — simple defer question, no critique context
        defer_question = question_arg.strip()
        verdict = "WARN"
        principles: list[str] = []
        reasoning = ""
    else:
        # Try to parse critique JSON from stdin
        critique = _parse_critique_from_stdin() or {}
        verdict = (critique.get("verdict") or "WARN").upper()
        principles = critique.get("principle_flags") or []
        reasoning = critique.get("reasoning") or ""
        defer_question = (critique.get("defer_question") or "").strip()

        if not defer_question:
            return {
                "command": "defer",
                "status": "error",
                "text": (
                    "No defer question found.\n\n"
                    "Usage:\n"
                    "  irp defer \"Your question here\"\n"
                    "  collab.py --mode critique \"proposal\" | irp defer\n\n"
                    "The defer command expects either a positional question argument\n"
                    "or a critique JSON object on stdin (from collab.py --mode critique)."
                ),
            }

    # CLEAR verdicts don't need deferral — but allow it if explicitly invoked
    if verdict == "CLEAR" and not question_arg:
        return {
            "command": "defer",
            "status": "clear",
            "text": (
                "Verdict is CLEAR — no deferral needed.\n"
                "Use irp defer only on WARN or BLOCK verdicts,\n"
                "or pass a question explicitly: irp defer \"your question\""
            ),
        }

    # ── 2. Load ledger + config ───────────────────────────────────────────────
    ledger = read_ledger(irp_dir)
    decisions = [r for r in ledger if r.get("type") == "decision" or (r.get("what") and r.get("why"))]
    relevant = _relevant_decisions(defer_question, decisions)

    cfg = read_config(irp_dir)
    control_level = cfg.get("control_level", "advanced")

    # On easy level, WARN verdicts from stdin are advisory — skip the prompt.
    if control_level == "easy" and verdict == "WARN" and not question_arg:
        return {
            "command": "defer",
            "status": "advisory",
            "verdict": verdict,
            "defer_question": defer_question,
            "control_level": control_level,
            "text": (
                f"WARN verdict — advisory only (control_level: easy).\n"
                f"Question: {defer_question}\n"
                f"No entry captured. Upgrade to control_level: medium to gate on WARN."
            ),
        }

    # ── 3. Interactive prompt ─────────────────────────────────────────────────
    if as_json:
        # Non-interactive mode: return the defer context as JSON without prompting
        return {
            "command": "defer",
            "status": "pending",
            "verdict": verdict,
            "defer_question": defer_question,
            "principle_flags": principles,
            "relevant_decisions": [d.get("id") for d in relevant],
            "control_level": control_level,
            "text": f"Defer question: {defer_question}",
        }

    answer = _prompt_resolution(defer_question, verdict, principles, relevant,
                                control_level=control_level)

    if answer is None:
        return {
            "command": "defer",
            "status": "cancelled",
            "text": "Defer cancelled — no entry captured.",
        }

    # ── 4. Capture resolution ─────────────────────────────────────────────────
    entry = _capture_resolution(irp_dir, answer, defer_question, verdict, principles, reasoning)
    irp_id = entry["id"]

    sep = "─" * 60
    text = "\n".join([
        sep,
        f"  ✓ Resolution captured  {irp_id}",
        sep,
        f"  What: {answer}",
        f"  Tags: defer · resolution · {verdict.lower()}",
        "",
        "  The resolution is now part of the audit trail.",
        "  Run `irp why` to confirm.",
    ])

    return {
        "command": "defer",
        "status": "ok",
        "irp_id": irp_id,
        "verdict": verdict,
        "defer_question": defer_question,
        "answer": answer,
        "text": text,
    }
