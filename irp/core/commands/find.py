from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from store import read_ledger, read_craft


def _snippet(value: str, max_len: int = 120) -> str:
    flat = value.replace("\n", " ").strip()
    return flat[:max_len] + ("..." if len(flat) > max_len else "")


def _match_entry(entry: dict, pattern: re.Pattern) -> list[str]:
    hits = []
    for key, val in entry.items():
        if isinstance(val, str) and pattern.search(val):
            hits.append(f"{key}: {_snippet(val)}")
    return hits


def run_find(project_root: Path, irp_dir: Path, args) -> dict:
    query = args.query

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    results = []

    if not getattr(args, "craft_only", False):
        for entry in read_ledger(irp_dir):
            hits = _match_entry(entry, pattern)
            if hits:
                results.append({
                    "source": "ledger",
                    "id": entry.get("id", ""),
                    "timestamp": entry.get("timestamp", ""),
                    "hits": hits,
                    "entry": entry,
                })

    if not getattr(args, "ledger_only", False):
        for entry in read_craft(irp_dir):
            hits = _match_entry(entry, pattern)
            if hits:
                results.append({
                    "source": "craft",
                    "id": entry.get("id", ""),
                    "timestamp": entry.get("timestamp", ""),
                    "hits": hits,
                    "entry": entry,
                })

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        "Command: find",
        f'Query:   "{query}"',
        "",
    ]

    if not results:
        return {
            "command": "find",
            "status": "no_results",
            "query": query,
            "count": 0,
            "text": "\n".join(header + [f'No matches found for "{query}"']),
        }

    lines = [f"Found {len(results)} match(es):", ""]
    for r in results:
        lines.append(f"[{r['source'].upper()}] {r['id']}  ({r['timestamp']})")
        for hit in r["hits"]:
            lines.append(f"  {hit}")
        lines.append("")

    out = {
        "command": "find",
        "status": "ok",
        "query": query,
        "count": len(results),
        "results": results,
        "text": "\n".join(header + lines),
    }

    if getattr(args, "graph", False):
        graph_path = _open_find_graph(results, query, irp_dir)
        out["graph_path"] = str(graph_path)
        out["text"] += f"\n\nGraph: {graph_path}"

    return out


def _open_find_graph(results: list[dict], query: str, irp_dir: Path) -> Path:
    """Build a graph scoped to find results + their causal references, open in browser."""
    from commands.graph import build_graph_html, IRP_ID_RE

    # Collect matched ledger entries only (craft entries have no graph nodes)
    matched_ids = {r["id"] for r in results if r["source"] == "ledger"}
    matched_entries = [r["entry"] for r in results if r["source"] == "ledger"]

    # Collect all IRP IDs referenced in matched entries' text fields (causal context)
    full_ledger = read_ledger(irp_dir)
    ledger_by_id = {e.get("id"): e for e in full_ledger if e.get("id")}

    referenced_ids: set[str] = set()
    for entry in matched_entries:
        for val in entry.values():
            if isinstance(val, str):
                for ref in IRP_ID_RE.findall(val):
                    if ref not in matched_ids and ref in ledger_by_id:
                        referenced_ids.add(ref)

    # Also include entries that reference the matched entries (reverse edges)
    for entry in full_ledger:
        if entry.get("id") in matched_ids:
            continue
        for val in entry.values():
            if isinstance(val, str):
                for ref in IRP_ID_RE.findall(val):
                    if ref in matched_ids and entry.get("id") in ledger_by_id:
                        referenced_ids.add(entry["id"])

    # Build node list: matched (bright) + context (dimmed)
    nodes = [{**e, "dimmed": False} for e in matched_entries]
    for rid in referenced_ids:
        if rid in ledger_by_id:
            nodes.append({**ledger_by_id[rid], "dimmed": True})

    matched_count = len(matched_entries)
    context_count = len(referenced_ids)
    filter_badge = (
        f" &nbsp;&middot;&nbsp; "
        f"<span style='color:#60a5fa'>{matched_count} matched</span>"
        f"<span style='color:#374151'> &middot; {context_count} causal context (dimmed)</span>"
    )

    html = build_graph_html(nodes, filter_badge=filter_badge, title_suffix=f'find: "{query}"')

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_query = re.sub(r"[^\w\-]", "_", query)[:40]
    out_path = Path(tempfile.gettempdir()) / f"irp-find-{safe_query}-{ts}.html"
    out_path.write_text(html, encoding="utf-8")

    # Open in default browser (macOS / Linux)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(out_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return out_path
