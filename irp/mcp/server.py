"""
IRP MCP Server — exposes IRP as MCP tools for any MCP-compatible client.

Tools:
  irp_capture   — Record a confirmed decision to the ledger
  irp_why       — Explain active reasoning lineage or look up a decision
  irp_inherit   — Return current active decisions (project context)
  irp_check     — Check a proposal against active decisions for conflicts

Usage:
  # stdio transport (default — for Claude Code, Cursor, UG, etc.)
  irp-mcp

  # Or run directly
  python -m irp.mcp.server

Configure in Claude Code / Claude Desktop / Cursor:
  {
    "mcpServers": {
      "irp": {
        "command": "irp-mcp"
      }
    }
  }
"""

import os
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

from irp.core.store import ensure_irp_dir
from irp.core.commands.capture import run_capture
from irp.core.commands.why import run_why
from irp.core.commands.inherit import run_inherit
from irp.core.commands.check import run_check

mcp = FastMCP(
    "irp",
    description="Intent Record Protocol — decision ledger for teams and agents",
)

def _resolve_paths() -> tuple[Path, Path]:
    """Resolve project root and .irp/ directory.

    Checks IRP_PROJECT_ROOT env var first, then falls back to cwd.
    """
    project_root = Path(os.environ.get("IRP_PROJECT_ROOT", ".")).resolve()
    irp_dir = ensure_irp_dir(project_root)
    return project_root, irp_dir

@mcp.tool()
def irp_capture(
    what: str,
    why: str = "",
    confidence: str = "medium",
    tags: str = "",
    source: str = "mcp",
) -> dict:
    """Record a confirmed decision to the IRP ledger.

    The decision is appended to .irp/ledger.jsonl and current.json is rebuilt.
    If Obsidian or MemPalace integrations are configured, the decision is
    also written to those targets.

    Args:
        what: What was decided (required)
        why: Why it was decided — the reasoning behind the decision
        confidence: Confidence level: "low", "medium", or "high"
        tags: Comma-separated tags, e.g. "architecture,backend"
        source: Source identifier (defaults to "mcp")
    """
    import json
    import sys
    from io import StringIO

    project_root, irp_dir = _resolve_paths()

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    candidate = {
        "what": what,
        "why": why,
        "confidence": confidence,
        "tags": tag_list,
        "source": source,
    }

    # run_capture reads from stdin when args.stdin is True
    old_stdin = sys.stdin
    sys.stdin = StringIO(json.dumps(candidate))

    try:
        args = SimpleNamespace(stdin=True, json=True)
        result = run_capture(project_root=project_root, irp_dir=irp_dir, args=args)
    finally:
        sys.stdin = old_stdin

    return {
        "status": result.get("status"),
        "id": result.get("entry", {}).get("id"),
        "entry": result.get("entry"),
        "integrations": result.get("integrations", []),
    }

@mcp.tool()
def irp_why(id: str = "") -> dict:
    """Explain active reasoning lineage or look up a specific decision.

    Without an ID, returns the most recent active decision.
    With an ID (e.g. "IRP-2026-04-17-001"), returns that specific entry.

    Args:
        id: Optional IRP entry ID to look up. Leave empty for the latest.
    """
    project_root, irp_dir = _resolve_paths()

    args = SimpleNamespace(id=id if id else None, json=True)
    result = run_why(project_root=project_root, irp_dir=irp_dir, args=args)

    return {
        "status": result.get("status"),
        "entry": result.get("entry"),
        "latest": result.get("latest"),
        "active_count": result.get("active_count"),
        "text": result.get("text"),
    }

@mcp.tool()
def irp_inherit() -> dict:
    """Return current active IRP decisions (project context).

    Returns the last 10 confirmed decisions from current.json.
    Use this to understand what has already been decided before
    making new decisions.
    """
    project_root, irp_dir = _resolve_paths()

    args = SimpleNamespace(json=True)
    result = run_inherit(project_root=project_root, irp_dir=irp_dir, args=args)

    return {
        "project_root": result.get("project_root"),
        "active_count": result.get("active_count"),
        "active": result.get("active", []),
    }

@mcp.tool()
def irp_check(proposal: str) -> dict:
    """Check a proposal against active decisions for conflicts.

    Tokenizes the proposal and compares against active decisions.
    Returns "conflict" with the matched decision if overlap is found,
    or "clear" if no conflicts detected.

    Args:
        proposal: The proposal text to check against active decisions.
    """
    project_root, irp_dir = _resolve_paths()

    args = SimpleNamespace(proposal=proposal, json=True)
    result = run_check(project_root=project_root, irp_dir=irp_dir, args=args)

    return {
        "status": result.get("status"),
        "proposal": result.get("proposal"),
        "match_id": result.get("match_id"),
        "matched_on": result.get("matched_on"),
        "checked": result.get("checked"),
        "text": result.get("text"),
    }

def main():
    """Entry point for the irp-mcp console script."""
    mcp.run()

if __name__ == "__main__":
    main()
