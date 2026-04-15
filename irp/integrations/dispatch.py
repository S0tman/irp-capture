"""Integration dispatcher — fires all enabled integrations after a capture.

Called by run_capture() after the ledger write. Never raises; integration
errors are captured in the returned results list and surfaced in the capture
result dict under "integrations".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from irp.integrations import obsidian, mempalace

def _try_load_dotenv(project_root: Path) -> None:
    """Best-effort dotenv load from project root or .irp/. Silent if python-dotenv absent."""
    try:
        from dotenv import load_dotenv
        for candidate in [project_root / ".env", project_root / ".irp" / ".env"]:
            if candidate.exists():
                load_dotenv(candidate, override=False)
    except ImportError:
        pass

def run(decision: dict[str, Any], project_root: Path) -> list[dict[str, Any]]:
    """Fire all enabled integrations. Returns a list of result dicts."""
    _try_load_dotenv(project_root)

    results: list[dict[str, Any]] = []
    for fn in (obsidian.sync, mempalace.sync):
        try:
            result = fn(decision, project_root)
            if result is not None:
                results.append(result)
        except Exception as e:
            results.append({"integration": fn.__module__, "status": "error", "error": str(e)})
    return results
