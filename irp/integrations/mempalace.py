"""MemPalace integration — writes each captured decision into the MemPalace
ChromaDB palace so agents can semantically query past decisions.

Config (env vars):
    IRP_MEMPALACE_PATH  Path to your palace directory.
                        Defaults to ~/.mempalace/palace
                        If unset AND the default path doesn't exist, sync is skipped.

Optional dependency:
    pip install 'irp-capture[mempalace]'
    (installs chromadb>=0.5)

MemPalace schema (collection: mempalace_drawers):
    document  — formatted decision text chunk
    metadata  — source_file, source_mtime, normalize_version, irp_id, confidence, tags
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

_DEFAULT_PALACE = Path.home() / ".mempalace" / "palace"
_COLLECTION = "mempalace_drawers"

def _format_decision(decision: dict[str, Any]) -> str:
    irp_id = decision.get("id", "unknown")
    what = decision.get("what", "")
    why = decision.get("why", "")
    confidence = decision.get("confidence", "medium")
    timestamp = decision.get("timestamp", "")
    tags = decision.get("tags", [])
    tags_str = ", ".join(str(t) for t in tags) if tags else "none"
    return (
        f"IRP Decision {irp_id}\n"
        f"Timestamp: {timestamp}\n"
        f"Confidence: {confidence}\n"
        f"Tags: {tags_str}\n\n"
        f"Decision: {what}\n\n"
        f"Rationale: {why}"
    )

def write_decision(decision: dict[str, Any], palace_path: str | Path) -> str:
    try:
        import chromadb
    except ImportError:
        raise ImportError(
            "chromadb is required for MemPalace integration. "
            "Install with: pip install 'irp-capture[mempalace]'"
        )

    palace = Path(palace_path).expanduser()
    palace.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(palace))
    collection = client.get_or_create_collection(
        name=_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    irp_id = decision.get("id", "unknown")
    collection.upsert(
        ids=[irp_id],
        documents=[_format_decision(decision)],
        metadatas=[{
            "source_file": "irp",
            "source_mtime": float(time.time()),
            "normalize_version": "v2",
            "irp_id": irp_id,
            "confidence": decision.get("confidence", "medium"),
            "tags": ",".join(str(t) for t in decision.get("tags", [])),
        }],
    )
    return irp_id

def sync(decision: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    explicit = "IRP_MEMPALACE_PATH" in os.environ
    palace_path = Path(os.environ.get("IRP_MEMPALACE_PATH", str(_DEFAULT_PALACE))).expanduser()

    # Skip silently if not configured and palace doesn't exist yet
    if not explicit and not palace_path.exists():
        return None

    try:
        irp_id = write_decision(decision, palace_path)
        return {"integration": "mempalace", "status": "ok", "id": irp_id}
    except ImportError as e:
        return {"integration": "mempalace", "status": "skipped", "reason": str(e)}
    except Exception as e:
        return {"integration": "mempalace", "status": "error", "error": str(e)}
