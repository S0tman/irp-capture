"""IRP craft — individual craft knowledge capture, list, and export.

Stores personal craft knowledge (preferences, configurations, gotchas,
ways of working) in .irp/craft.jsonl, separate from the decision ledger.
Exports to CRAFT.md — a human-readable knowledge document.

Usage:
    irp craft add                           # interactive
    irp craft add --category gotcha --what "Always check X before Y"
    irp craft add --category preference --what "Use --json everywhere" --context "CI pipelines"
    irp craft list                          # all entries
    irp craft list --category gotcha        # filter by category
    irp craft export                        # write CRAFT.md
    irp craft export --category preference  # single-category export
    irp craft export --output path/to/CRAFT.md

Categories:
    preference       Personal defaults, coding preferences, tool choices
    configuration    Machine-local or project-specific technical setup
    gotcha           Known pitfalls, traps, non-obvious failure modes
    way-of-working   Workflow patterns, collaboration approaches, habits

Design rules:
  - Separate substrate from the decision ledger (.irp/craft.jsonl)
  - No LLM calls. No inference. Human-typed only.
  - CRAFT.md is always regenerable — craft.jsonl is the source of truth.
  - Does not affect irp why, irp check, or irp guard.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store import append_craft_entry, next_craft_id, read_craft

# ── categories ────────────────────────────────────────────────────────────────

CATEGORIES = ["preference", "configuration", "gotcha", "way-of-working"]

_CATEGORY_LABELS: dict[str, str] = {
    "preference":     "Preferences",
    "configuration":  "Configuration",
    "gotcha":         "Gotchas",
    "way-of-working": "Ways of Working",
}

_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "preference":     "Personal defaults, coding preferences, tool choices",
    "configuration":  "Machine-local or project-specific technical setup",
    "gotcha":         "Known pitfalls, traps, non-obvious failure modes",
    "way-of-working": "Workflow patterns, collaboration approaches, habits",
}


# ── CRAFT.md builder ──────────────────────────────────────────────────────────

_CRAFT_HEADER = """\
# CRAFT.md

> **This file is generated.** Do not edit manually — changes will be overwritten.
>
> Source of truth: `.irp/craft.jsonl`
> Regenerate: `irp craft export --force`
>
> Generated: {generated_at}
> {entry_count} craft entr{entry_plural} across {category_count} categor{cat_plural}{filter_note}

---
"""

_CRAFT_FOOTER = """\

---

*To add craft knowledge:*

```bash
irp craft add
```

*To regenerate this file:*

```bash
irp craft export --force
```
"""


def _format_craft_entry(entry: dict[str, Any]) -> str:
    what = (entry.get("what") or "").strip()
    context = (entry.get("context") or "").strip()
    craft_id = entry.get("id", "")
    ts = str(entry.get("timestamp", ""))[:10]

    line = f"- **{what}**"
    if context:
        line += f"  \n  *{context}*"
    meta = []
    if craft_id:
        meta.append(craft_id)
    if ts:
        meta.append(ts)
    if meta:
        line += f"  \n  <sub>{' · '.join(meta)}</sub>"
    return line


def _build_craft_md(
    entries: list[dict[str, Any]],
    category_filter: str | None = None,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if category_filter:
        filtered = [e for e in entries if e.get("category") == category_filter]
    else:
        filtered = entries

    categories_present = list(dict.fromkeys(
        e.get("category", "uncategorised") for e in filtered
        if e.get("category") in CATEGORIES
    ))
    # Preserve canonical order.
    ordered_cats = [c for c in CATEGORIES if c in categories_present]
    uncategorised = [e for e in filtered if e.get("category") not in CATEGORIES]

    filter_note = f" — category: {category_filter}" if category_filter else ""
    entry_plural = "y" if len(filtered) == 1 else "ies"
    cat_plural = "y" if len(ordered_cats) == 1 else "ies"

    parts: list[str] = []
    parts.append(_CRAFT_HEADER.format(
        generated_at=generated_at,
        entry_count=len(filtered),
        entry_plural=entry_plural,
        category_count=len(ordered_cats),
        cat_plural=cat_plural,
        filter_note=filter_note,
    ))

    if not filtered:
        parts.append(
            "No craft entries yet.\n"
            "Add one with `irp craft add` to populate this file.\n"
        )
        parts.append(_CRAFT_FOOTER)
        return "".join(parts)

    for cat in ordered_cats:
        label = _CATEGORY_LABELS.get(cat, cat.title())
        desc = _CATEGORY_DESCRIPTIONS.get(cat, "")
        cat_entries = [e for e in filtered if e.get("category") == cat]

        parts.append(f"## {label}\n\n")
        if desc:
            parts.append(f"*{desc}*\n\n")

        parts.append("\n".join(_format_craft_entry(e) for e in cat_entries))
        parts.append("\n\n")

    if uncategorised:
        parts.append("## Other\n\n")
        parts.append("\n".join(_format_craft_entry(e) for e in uncategorised))
        parts.append("\n\n")

    parts.append(_CRAFT_FOOTER)
    return "".join(parts)


# ── interactive add ───────────────────────────────────────────────────────────

def _prompt_craft_add(category_arg: str | None, what_arg: str | None,
                      context_arg: str | None) -> dict[str, str] | None:
    """Interactive prompt for craft add. Returns dict or None on cancel."""
    sep = "─" * 60
    print(f"\n{sep}")
    print("  IRP CRAFT  Add knowledge entry")
    print(sep)

    # Category
    if category_arg and category_arg in CATEGORIES:
        category = category_arg
        print(f"\n  Category: {_CATEGORY_LABELS[category]}")
    else:
        print("\n  Categories:")
        for i, cat in enumerate(CATEGORIES, 1):
            label = _CATEGORY_LABELS[cat]
            desc = _CATEGORY_DESCRIPTIONS[cat]
            print(f"    {i}. {label} — {desc}")
        print()
        try:
            raw = input("  Category (1-4 or name): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return None
        if not raw:
            print("  Empty — cancelled.")
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(CATEGORIES):
            category = CATEGORIES[int(raw) - 1]
        elif raw in CATEGORIES:
            category = raw
        else:
            # Try prefix match.
            matches = [c for c in CATEGORIES if c.startswith(raw.lower())]
            if len(matches) == 1:
                category = matches[0]
            else:
                print(f"  Unknown category: {raw!r}. Use: {', '.join(CATEGORIES)}")
                return None

    # What
    if what_arg:
        what = what_arg.strip()
        print(f"\n  What: {what}")
    else:
        print(f"\n  What do you know? (the craft knowledge — be concrete)")
        try:
            what = input("  What: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return None
        if not what:
            print("  Empty — cancelled.")
            return None

    # Context (optional)
    if context_arg is not None:
        context = context_arg.strip()
    else:
        print(f"\n  Context (optional — project, tool, situation)")
        try:
            context = input("  Context: ").strip()
        except (KeyboardInterrupt, EOFError):
            context = ""

    return {"category": category, "what": what, "context": context}


def _capture_craft(irp_dir: Path, category: str, what: str,
                   context: str) -> dict[str, Any]:
    """Write a new entry to .irp/craft.jsonl."""
    existing = read_craft(irp_dir)
    craft_id = next_craft_id(existing)
    contributor = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entry: dict[str, Any] = {
        "id": craft_id,
        "category": category,
        "what": what,
        "timestamp": timestamp,
        "contributor": contributor,
    }
    if context:
        entry["context"] = context

    append_craft_entry(irp_dir, entry)
    return entry


# ── subcommand runners ────────────────────────────────────────────────────────

def _run_craft_add(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    as_json = bool(getattr(args, "json", False))
    category_arg = getattr(args, "category", None)
    what_arg = getattr(args, "what", None)
    context_arg = getattr(args, "context", None)

    # Non-interactive path: all required fields provided.
    if category_arg and what_arg:
        if category_arg not in CATEGORIES:
            return {
                "command": "craft.add",
                "status": "error",
                "text": (
                    f"Unknown category: {category_arg!r}\n"
                    f"Valid: {', '.join(CATEGORIES)}"
                ),
            }
        entry = _capture_craft(irp_dir, category_arg, what_arg.strip(),
                               (context_arg or "").strip())
        craft_id = entry["id"]
        label = _CATEGORY_LABELS[category_arg]
        text = f"  ✓ Craft entry captured  {craft_id}\n  Category: {label}\n  What: {what_arg}"
        return {
            "command": "craft.add",
            "status": "ok",
            "craft_id": craft_id,
            "category": category_arg,
            "text": text,
        }

    # Interactive path.
    if as_json:
        return {
            "command": "craft.add",
            "status": "error",
            "text": "Non-interactive mode requires --category and --what flags.",
        }

    result = _prompt_craft_add(category_arg, what_arg, context_arg)
    if result is None:
        return {"command": "craft.add", "status": "cancelled", "text": "Cancelled."}

    entry = _capture_craft(irp_dir, result["category"], result["what"],
                           result.get("context", ""))
    craft_id = entry["id"]
    label = _CATEGORY_LABELS[result["category"]]

    sep = "─" * 60
    text = "\n".join([
        "",
        sep,
        f"  ✓ Craft entry captured  {craft_id}",
        sep,
        f"  Category: {label}",
        f"  What:     {result['what']}",
        *([f"  Context:  {result['context']}"] if result.get("context") else []),
        "",
        "  Run `irp craft list` to review, `irp craft export` to write CRAFT.md.",
    ])

    return {
        "command": "craft.add",
        "status": "ok",
        "craft_id": craft_id,
        "category": result["category"],
        "text": text,
    }


def _run_craft_list(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    as_json = bool(getattr(args, "json", False))
    category_filter: str | None = getattr(args, "category", None)

    entries = read_craft(irp_dir)

    if category_filter:
        if category_filter not in CATEGORIES:
            return {
                "command": "craft.list",
                "status": "error",
                "text": (
                    f"Unknown category: {category_filter!r}\n"
                    f"Valid: {', '.join(CATEGORIES)}"
                ),
            }
        entries = [e for e in entries if e.get("category") == category_filter]

    if not entries:
        nudge = (
            "No craft entries yet. Add one with `irp craft add`."
            if not category_filter
            else f"No craft entries in category '{category_filter}' yet."
        )
        return {"command": "craft.list", "status": "empty", "text": nudge}

    if as_json:
        return {
            "command": "craft.list",
            "status": "ok",
            "entries": entries,
            "count": len(entries),
            "text": f"{len(entries)} craft entries",
        }

    lines: list[str] = []
    current_cat = None
    ordered = sorted(entries, key=lambda e: (
        CATEGORIES.index(e.get("category")) if e.get("category") in CATEGORIES else 99,
        e.get("timestamp", ""),
    ))
    for entry in ordered:
        cat = entry.get("category", "uncategorised")
        if cat != current_cat:
            current_cat = cat
            label = _CATEGORY_LABELS.get(cat, cat.title())
            lines.append(f"\n{label}")
            lines.append("─" * len(label))
        craft_id = entry.get("id", "?")
        what = (entry.get("what") or "").strip()
        context = (entry.get("context") or "").strip()
        line = f"  {craft_id}  {what}"
        if context:
            line += f"\n             ({context})"
        lines.append(line)

    header = f"{len(entries)} craft entr{'y' if len(entries) == 1 else 'ies'}"
    if category_filter:
        header += f" in '{_CATEGORY_LABELS.get(category_filter, category_filter)}'"

    text = header + "\n" + "\n".join(lines)
    return {
        "command": "craft.list",
        "status": "ok",
        "count": len(entries),
        "text": text,
    }


def _run_craft_export(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    as_json = bool(getattr(args, "json", False))
    output_arg = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))
    writable = bool(getattr(args, "writable", False))
    category_filter: str | None = getattr(args, "category", None)

    if category_filter and category_filter not in CATEGORIES:
        return {
            "command": "craft.export",
            "status": "error",
            "text": (
                f"Unknown category: {category_filter!r}\n"
                f"Valid: {', '.join(CATEGORIES)}"
            ),
        }

    entries = read_craft(irp_dir)
    if not entries:
        return {
            "command": "craft.export",
            "status": "empty",
            "text": "No craft entries yet. Add one with `irp craft add`.",
        }

    # Resolve output path.
    if output_arg:
        output_path = Path(output_arg)
    elif category_filter:
        output_path = project_root / f"CRAFT-{category_filter}.md"
    else:
        output_path = project_root / "CRAFT.md"
    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    body = _build_craft_md(entries, category_filter=category_filter)

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        f"Command: craft export{f' --category {category_filter}' if category_filter else ''}",
        "",
    ]

    if output_path.exists() and not force:
        return {
            "command": "craft.export",
            "status": "exists",
            "output_path": str(output_path),
            "text": "\n".join(header + [
                f"Refusing to overwrite existing file: {output_path}",
                "Re-run with --force to overwrite.",
            ]),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            output_path.chmod(0o644)
        except OSError:
            pass

    output_path.write_text(body, encoding="utf-8")

    if not writable:
        try:
            output_path.chmod(0o444)
        except OSError:
            pass

    filtered = [e for e in entries if not category_filter or e.get("category") == category_filter]
    by_cat = {cat: sum(1 for e in filtered if e.get("category") == cat) for cat in CATEGORIES}
    cat_summary = " · ".join(
        f"{_CATEGORY_LABELS[c]}: {by_cat[c]}" for c in CATEGORIES if by_cat[c] > 0
    )

    lock_note = (
        "Lock:    file is writable (--writable was passed)"
        if writable
        else "Lock:    file is read-only — pass --writable to override"
    )
    regen = f"  irp craft export{f' --category {category_filter}' if category_filter else ''} --force"

    text = "\n".join(header + [
        f"Wrote {output_path}",
        f"Entries: {len(filtered)} → {cat_summary}",
        lock_note,
        "",
        "Regenerate any time with:",
        regen,
    ])

    return {
        "command": "craft.export",
        "status": "ok",
        "output_path": str(output_path),
        "entry_count": len(filtered),
        "by_category": by_cat,
        "text": text,
    }


# ── public entry point ────────────────────────────────────────────────────────

def run_craft(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    craft_action = getattr(args, "craft_action", None)

    if craft_action == "add":
        return _run_craft_add(project_root, irp_dir, args)
    if craft_action == "list":
        return _run_craft_list(project_root, irp_dir, args)
    if craft_action == "export":
        return _run_craft_export(project_root, irp_dir, args)

    return {
        "command": "craft",
        "status": "error",
        "text": "Unknown craft action. Use: irp craft add | irp craft list | irp craft export",
    }
