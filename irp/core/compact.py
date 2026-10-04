"""A slim view of decisions for agents.

The irp_inherit MCP tool returns this by default. It keeps what an agent needs
to act on a decision and drops bookkeeping fields (type, confidence, source,
tags, source references, the time of day). The 4 Oct 2026 eval found the full
entries made the longest context an agent got; this view is about 40% smaller.
"""
from __future__ import annotations

from typing import Any

# Field order is the reading order: which decision, when, what, why, what was
# turned down, and what it replaced.
_FIELDS = ("id", "date", "what", "why", "rejected", "supersedes")


def compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    values = {
        "id": entry.get("id"),
        "date": str(entry.get("timestamp") or "")[:10],
        "what": entry.get("what"),
        "why": entry.get("why"),
        "rejected": entry.get("alternatives"),
        "supersedes": entry.get("supersedes"),
    }
    return {k: values[k] for k in _FIELDS if values[k]}


def compact_inherit(result: dict[str, Any]) -> dict[str, Any]:
    """Shape a run_inherit result for an agent: counts plus slim entries."""
    return {
        "project_root": result.get("project_root"),
        "active_count": result.get("active_count"),
        "omitted": result.get("omitted", 0),
        "active": [compact_entry(e) for e in result.get("active", [])],
    }
