"""IRP export — turn decision lineage into portable working context.

Subcommands:
  export context --target agents.md
  export context --target decisions.md

Design rules (matched to IRP-2026-04-28-002):
  - No new schema. Read .irp/ledger.jsonl as canonical source.
  - No LLM calls. No inference. Deterministic mapping only.
  - Provenance preserved on every line — every rule cites its IRP id.
  - If a decision cannot be safely converted to an instruction, list it
    under "Relevant decisions" instead of forcing a rule.
  - The exported file is always regenerable. The ledger remains the
    source of truth.

DECISIONS.md design (IRP-2026-04-28-004):
  - Human-readable decision log ordered newest-first (changelog style).
  - One H2 section per decision: ID · timestamp as the heading.
  - Renders what, why, tags, confidence, and source metadata.
  - Clearly marked as generated — the ledger is the source of truth.
  - No derived rules. Every decision is shown in full.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from irp.core.store import read_ledger

# ── header copy ──────────────────────────────────────────────────────────────

_HEADER_TEMPLATE = """# AGENTS.md

This file was generated from IRP decision records.

IRP records *why* decisions were made. The instructions below are derived
from confirmed decisions in the local `.irp/` ledger.

Do not treat this file as the source of truth. The source of truth is:

- `.irp/ledger.jsonl`
- `.irp/current.json`

Regenerate this file with:

```bash
irp export context --target agents.md
```

Generated: {generated_at}
Source: {decision_count} confirmed decision(s) from `.irp/ledger.jsonl`
"""

_FOOTER_TEMPLATE = """
---

To inspect any decision in detail:

```bash
irp why --id IRP-YYYY-MM-DD-NNN
```

Rules above represent constraints created by past decisions. They remain in
effect unless a later IRP decision overrides them. Always check `irp inherit`
for the latest active context before assuming a rule still applies.
"""

# ── deterministic conversion ─────────────────────────────────────────────────

# Common decision prefixes that are safe to strip when forming a rule.
_STRIPPABLE_PREFIXES = (
    "Decided to ",
    "Decision: ",
    "Decision to ",
    "Locked: ",
    "Locked ",
    "Adopted: ",
    "Adopted ",
)

def _is_decision(entry: dict[str, Any]) -> bool:
    """A ledger row counts as a decision if its type is 'decision' or
    if it has both `what` and `why` populated (defensive default for
    older entries that may pre-date the type field)."""
    if entry.get("type") == "decision":
        return True
    return bool(entry.get("what")) and bool(entry.get("why")) and entry.get("type") in (None, "")

def _derive_rule(entry: dict[str, Any]) -> str | None:
    """Convert a decision's `what` field into a single-line agent rule.

    Conservative: returns None for any decision that can't be safely
    summarised into one short imperative sentence. Those decisions still
    appear under 'Relevant decisions' in the exported file.
    """
    what = (entry.get("what") or "").strip()
    if not what:
        return None

    # Reject multi-sentence or long-form decisions — they belong in the
    # Relevant decisions list with full provenance, not as a rule.
    if len(what) > 160:
        return None
    if what.count(".") > 1:
        return None
    # Reject decisions that contain explanatory em-dash markers — these are
    # almost always summary-style and lose meaning when truncated. We only
    # reject the em dash (—), not the hyphen (-), to avoid pruning legitimate
    # decisions like "Use multi-tenant architecture".
    if " — " in what:
        return None
    if ": " in what and not what.lower().startswith(("decision:", "locked:", "adopted:")):
        return None

    # Strip common decision prefixes for cleaner reading.
    cleaned = what
    for prefix in _STRIPPABLE_PREFIXES:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].lstrip()
            break

    if not cleaned:
        return None

    # Capitalise first letter, ensure trailing period.
    cleaned = cleaned[0].upper() + cleaned[1:]
    if not cleaned.endswith("."):
        cleaned = cleaned + "."

    return cleaned

def _truncate(text: str, limit: int = 240) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

def _format_relevant_decision(entry: dict[str, Any]) -> str:
    """One bullet for the 'Relevant decisions' section."""
    irp_id = entry.get("id", "unknown")
    timestamp = entry.get("timestamp", "")
    what = _truncate(entry.get("what") or "")
    why = _truncate(entry.get("why") or "")
    line = f"- **{irp_id}**"
    if timestamp:
        line += f" ({timestamp})"
    if what:
        line += f" — {what}"
    if why:
        line += f"\n  - *Why:* {why}"
    return line

def _format_constraint(entry: dict[str, Any], rule: str) -> str:
    irp_id = entry.get("id", "unknown")
    return f"- **{rule}** *(Source: {irp_id})*"

def _build_agents_md(decisions: list[dict[str, Any]]) -> str:
    """Render the AGENTS.md body from a list of decision entries."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    parts: list[str] = []
    parts.append(_HEADER_TEMPLATE.format(
        generated_at=generated_at,
        decision_count=len(decisions),
    ))

    if not decisions:
        parts.append(
            "\n---\n\n"
            "## Working constraints\n\n"
            "No confirmed decisions in `.irp/ledger.jsonl` yet. "
            "Capture one with `irp capture` to populate this file.\n\n"
            "## Relevant decisions\n\n"
            "No decisions to list. The ledger at `.irp/ledger.jsonl` is empty.\n"
        )
        parts.append(_FOOTER_TEMPLATE)
        return "".join(parts)

    # Build the two sections.
    constraints: list[str] = []
    relevant: list[str] = []

    for entry in decisions:
        rule = _derive_rule(entry)
        if rule:
            constraints.append(_format_constraint(entry, rule))
        relevant.append(_format_relevant_decision(entry))

    parts.append("\n---\n\n## Working constraints\n\n")
    if constraints:
        parts.append(
            "These rules are derived from confirmed IRP decisions. "
            "Each rule preserves the constraint a past decision created on "
            "future work. Provenance follows every rule.\n\n"
        )
        parts.append("\n".join(constraints) + "\n")
    else:
        parts.append(
            "No decisions in the ledger could be deterministically converted "
            "to single-line rules. See *Relevant decisions* below for full "
            "context, or run `irp why --id <id>` to inspect any entry.\n"
        )

    parts.append("\n## Relevant decisions\n\n")
    parts.append(
        "Full decision lineage from `.irp/ledger.jsonl`. Use these as "
        "context — they explain *why* the rules above exist.\n\n"
    )
    parts.append("\n".join(relevant) + "\n")
    parts.append(_FOOTER_TEMPLATE)

    return "".join(parts)

# ── DECISIONS.md builder ────────────────────────────────────────────────────

_DECISIONS_HEADER_TEMPLATE = """# DECISIONS.md

> **This file is generated.** Do not edit manually — changes will be overwritten.
>
> Source of truth: `.irp/ledger.jsonl`
> Regenerate: `irp export context --target decisions.md`
>
> Generated: {generated_at}
> {decision_count} decision(s) — newest first

---
"""

_DECISIONS_FOOTER_TEMPLATE = """
---

*To inspect any decision in full detail:*

```bash
irp why --id IRP-YYYY-MM-DD-NNN
```

*To add a new decision:*

```bash
irp capture
```
"""

def _confidence_badge(confidence: str) -> str:
    badges = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}
    return badges.get((confidence or "").lower(), confidence or "")

def _source_label(entry: dict[str, Any]) -> str:
    src = entry.get("source", "")
    ref = entry.get("source_ref") or {}
    if src == "slack" and ref.get("channel_id"):
        return f"slack · channel {ref['channel_id']}"
    if src == "demo":
        scenario = entry.get("scenario", "")
        return f"demo · {scenario}" if scenario else "demo"
    return src or "—"

def _format_decision_entry(entry: dict[str, Any]) -> str:
    irp_id = entry.get("id", "unknown")
    timestamp = entry.get("timestamp", "")
    what = (entry.get("what") or "").strip()
    why = (entry.get("why") or "").strip()
    tags = entry.get("tags") or []
    confidence = entry.get("confidence", "")
    source = _source_label(entry)

    heading = f"## {irp_id}"
    if timestamp:
        heading += f" · {timestamp}"

    lines: list[str] = [heading, ""]

    if what:
        lines.append(f"**{what}**")
        lines.append("")

    if why:
        lines.append(f"*{why}*")
        lines.append("")

    meta: list[str] = []
    if confidence:
        meta.append(f"Confidence: {_confidence_badge(confidence)}")
    if tags:
        tag_str = " ".join(f"`{t}`" for t in tags)
        meta.append(f"Tags: {tag_str}")
    if source and source != "—":
        meta.append(f"Source: {source}")

    if meta:
        lines.append("  ".join(meta))
        lines.append("")

    return "\n".join(lines)

def _build_decisions_md(decisions: list[dict[str, Any]]) -> str:
    """Render DECISIONS.md — newest-first decision log."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    parts: list[str] = []
    parts.append(_DECISIONS_HEADER_TEMPLATE.format(
        generated_at=generated_at,
        decision_count=len(decisions),
    ))

    if not decisions:
        parts.append(
            "No decisions in `.irp/ledger.jsonl` yet.\n"
            "Capture one with `irp capture` to populate this file.\n"
        )
        parts.append(_DECISIONS_FOOTER_TEMPLATE)
        return "".join(parts)

    # Newest first — preserve insertion order stability within same timestamp.
    ordered = list(reversed(decisions))
    parts.append("\n".join(_format_decision_entry(e) for e in ordered))
    parts.append(_DECISIONS_FOOTER_TEMPLATE)

    return "".join(parts)

# ── public entry point ───────────────────────────────────────────────────────

def run_export(project_root: Path, irp_dir: Path, args) -> dict:
    """Dispatch on args.export_action."""
    action = getattr(args, "export_action", None)
    if action == "context":
        return _run_export_context(project_root, irp_dir, args)
    return {
        "command": "export",
        "status": "error",
        "text": f"Unknown export action: {action}",
    }

_TARGET_DEFAULTS = {
    "agents.md":    "AGENTS.md",
    "decisions.md": "DECISIONS.md",
}

_SUPPORTED_TARGETS = ", ".join(_TARGET_DEFAULTS)

def _run_export_context(project_root: Path, irp_dir: Path, args) -> dict:
    target = getattr(args, "target", None)
    output_arg = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))

    if target not in _TARGET_DEFAULTS:
        return {
            "command": "export.context",
            "status": "error",
            "text": f"Unsupported --target value: {target!r}. Supported: {_SUPPORTED_TARGETS}",
        }

    # Resolve output path.
    default_filename = _TARGET_DEFAULTS[target]
    output_path = Path(output_arg) if output_arg else (project_root / default_filename)
    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    # Read ledger and filter to decisions.
    ledger = read_ledger(irp_dir)
    decisions = [row for row in ledger if _is_decision(row)]

    # Build target-specific body.
    if target == "agents.md":
        body = _build_agents_md(decisions)
    else:  # decisions.md
        body = _build_decisions_md(decisions)

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        f"Command: export context --target {target}",
        "",
    ]

    if output_path.exists() and not force:
        if target == "agents.md":
            detail = (
                f"Would have written {len(decisions)} decision(s) "
                f"({sum(1 for d in decisions if _derive_rule(d))} as rules)."
            )
        else:
            detail = f"Would have written {len(decisions)} decision(s)."

        text = "\n".join(header + [
            f"Refusing to overwrite existing file: {output_path}",
            "Re-run with --force, or pass --output PATH to write elsewhere.",
            "",
            detail,
        ])
        return {
            "command": "export.context",
            "status": "exists",
            "target": target,
            "output_path": str(output_path),
            "decision_count": len(decisions),
            "text": text,
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # If a previous export locked the file read-only, restore write
    # permission before overwriting.
    if output_path.exists():
        try:
            output_path.chmod(0o644)
        except OSError:
            pass
    output_path.write_text(body, encoding="utf-8")

    # Lock the file read-only by default so accidental edits surface a
    # clear warning in editors. Override with --writable for downstream
    # tooling that needs to modify the file in place.
    writable = bool(getattr(args, "writable", False))
    if not writable:
        try:
            output_path.chmod(0o444)
        except OSError:
            pass

    if target == "agents.md":
        rule_count = sum(1 for d in decisions if _derive_rule(d))
        detail_lines = [
            f"Derived: {rule_count} rule(s) under 'Working constraints'",
            f"Listed:  {len(decisions)} decision(s) under 'Relevant decisions'",
        ]
        result_extra = {"rule_count": rule_count}
        regen_cmd = f"  irp export context --target {target}"
    else:
        rule_count = None
        detail_lines = [
            f"Listed:  {len(decisions)} decision(s) newest-first",
        ]
        result_extra = {}
        regen_cmd = f"  irp export context --target {target}"

    lock_note = (
        "Lock:    file is writable (--writable was passed)"
        if writable
        else "Lock:    file is read-only — `chmod +w` to override, or pass --writable on next export"
    )

    text = "\n".join(header + [
        f"Wrote {output_path}",
        f"Source: {len(decisions)} decision(s) from .irp/ledger.jsonl",
    ] + detail_lines + [
        lock_note,
        "",
        "Regenerate any time with:",
        regen_cmd,
    ])

    result = {
        "command": "export.context",
        "status": "ok",
        "target": target,
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "text": text,
    }
    result.update(result_extra)
    return result
