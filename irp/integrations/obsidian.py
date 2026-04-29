"""Obsidian vault integration — writes each captured decision as a .md file.

Config (env var):
    IRP_OBSIDIAN_VAULT  Path to your Obsidian vault root.
                        Decisions land in <vault>/decisions/<id>.md

No extra dependencies needed — Obsidian vaults are plain directories.

IRP ids found in 'why' fields are converted to [[wikilinks]] so that
Obsidian's built-in graph view draws provenance edges between decisions.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_IRP_ID_RE = re.compile(r"\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b")

def _wikilink_irp_ids(text: str) -> str:
    """Replace bare IRP ids with Obsidian wikilinks: IRP-X → [[IRP-X]]."""
    return _IRP_ID_RE.sub(lambda m: f"[[{m.group()}]]", text or "")

def write_decision(decision: dict[str, Any], vault_path: str | Path) -> Path:
    vault = Path(vault_path).expanduser()
    decisions_dir = vault / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)

    irp_id = decision.get("id", "unknown")
    what = decision.get("what", "")
    why = decision.get("why", "")
    confidence = decision.get("confidence", "medium")
    timestamp = decision.get("timestamp", "")
    tags = decision.get("tags", [])
    source = decision.get("source", "")

    tags_yaml = "[" + ", ".join(str(t) for t in tags) + "]" if tags else "[]"

    # Convert bare IRP ids in why field to [[wikilinks]] so Obsidian's
    # graph view draws provenance edges automatically.
    why_linked = _wikilink_irp_ids(why)

    content = (
        f"---\n"
        f"id: {irp_id}\n"
        f"type: decision\n"
        f"timestamp: {timestamp}\n"
        f"confidence: {confidence}\n"
        f"tags: {tags_yaml}\n"
        f"source: {source}\n"
        f"---\n"
        f"\n"
        f"# {what}\n"
        f"\n"
        f"## Why it matters\n"
        f"\n"
        f"{why_linked}\n"
    )

    out = decisions_dir / f"{irp_id}.md"
    out.write_text(content, encoding="utf-8")
    return out

def sync(decision: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    vault_path = os.environ.get("IRP_OBSIDIAN_VAULT")
    if not vault_path:
        return None
    try:
        out = write_decision(decision, vault_path)
        return {"integration": "obsidian", "status": "ok", "path": str(out)}
    except Exception as e:
        return {"integration": "obsidian", "status": "error", "error": str(e)}
