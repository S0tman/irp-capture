"""IRP stats — surfaces the user's own activation pattern from the local ledger.

No telemetry. All data stays on the machine.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

from irp.core.store import read_ledger

_SENSOR_LABELS = {
    "cli": "CLI (irp capture)",
    "slack": "Slack sensor",
    "discord": "Discord sensor",
    "figma": "Figma plugin",
    "vscode": "VS Code extension",
    "mcp": "MCP server",
    "git": "Git hook",
    "api": "REST API",
    "stdin": "stdin / pipe",
    "interactive": "CLI (interactive)",
    "demo": "demo generate",
}

_SAMPLE_STATS = {
    "total": 18,
    "days_active": 105,
    "first_capture": "2026-01-10",
    "last_capture": "2026-04-25",
    "weekly": [3, 4, 5, 6],
    "ramp": "growing",
    "sources": {"slack": 9, "stdin": 9},
    "top_tags": [("design-system", 5), ("components", 4), ("tokens", 4), ("process", 4), ("figma", 3)],
    "demo": True,
}

def _parse_ts(ts: str) -> date | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[:19], fmt[:len(ts[:19])]).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(ts[:10])
    except ValueError:
        return None

def _weekly_buckets(dates: list[date], n: int = 4) -> list[int]:
    if not dates:
        return [0] * n
    today = date.today()
    buckets = [0] * n
    for d in dates:
        delta = (today - d).days
        week = delta // 7
        if week < n:
            buckets[n - 1 - week] += 1
    return buckets

def _ramp(buckets: list[int]) -> str:
    non_zero = [b for b in buckets if b > 0]
    if len(non_zero) < 2:
        return "too early to tell"
    if buckets[-1] >= buckets[-2] and buckets[-1] > 0:
        return "growing"
    if buckets[-1] == 0 and buckets[-2] > 0:
        return "stalled"
    return "steady"

def _format_stats(stats: dict[str, Any]) -> str:
    demo_note = "  [sample data — run irp capture to build your own]\n" if stats.get("demo") else ""
    lines = [
        "IRP Stats" + (" — sample data" if stats.get("demo") else ""),
        "",
    ]
    if demo_note:
        lines.append(demo_note.strip())
        lines.append("")

    lines += [
        f"  Total decisions captured : {stats['total']}",
        f"  Active since             : {stats['first_capture']} ({stats['days_active']} days)",
        f"  Last capture             : {stats['last_capture']}",
        "",
        "  Captures — last 4 weeks (oldest → newest)",
    ]

    buckets = stats["weekly"]
    max_b = max(buckets) if max(buckets) > 0 else 1
    bar_width = 20
    week_labels = ["w-3", "w-2", "w-1", "this"]
    for label, count in zip(week_labels, buckets):
        bar = "█" * int(bar_width * count / max_b) if count else "·"
        lines.append(f"    {label}  {bar:<{bar_width}}  {count}")

    ramp_label = {"growing": "↑ growing", "stalled": "→ stalled", "steady": "→ steady"}.get(
        stats["ramp"], stats["ramp"]
    )
    lines += ["", f"  Capture ramp             : {ramp_label}", ""]

    lines.append("  Sensors used")
    for src, count in sorted(stats["sources"].items(), key=lambda x: -x[1]):
        label = _SENSOR_LABELS.get(src, src)
        lines.append(f"    {label:<28} {count}")

    if stats["top_tags"]:
        lines += ["", "  Top tags"]
        for tag, count in stats["top_tags"]:
            lines.append(f"    #{tag:<27} {count}")

    lines += [
        "",
        "  ──────────────────────────────────────────",
        "  All data is local. No telemetry. Ever.",
    ]
    return "\n".join(lines)

def run_stats(project_root: Path, irp_dir: Path, args) -> dict:
    demo = bool(getattr(args, "demo", False))

    if demo:
        text = _format_stats(_SAMPLE_STATS)
        return {"command": "stats", "status": "ok", "demo": True, "text": text}

    ledger = read_ledger(irp_dir)
    decisions = [r for r in ledger if r.get("type") == "decision" or (r.get("what") and r.get("why"))]

    if not decisions:
        return {
            "command": "stats",
            "status": "empty",
            "text": (
                "No decisions captured yet.\n\n"
                "Start with: irp capture\n\n"
                "Or explore a populated example:\n"
                "  irp stats --demo"
            ),
        }

    dates = [_parse_ts(d.get("timestamp", "")) for d in decisions]
    dates = [d for d in dates if d]

    sources = Counter(d.get("source", "unknown") for d in decisions)
    all_tags = [t for d in decisions for t in (d.get("tags") or [])]
    top_tags = Counter(all_tags).most_common(5)
    buckets = _weekly_buckets(dates)

    stats = {
        "total": len(decisions),
        "days_active": (max(dates) - min(dates)).days if len(dates) > 1 else 0,
        "first_capture": str(min(dates)) if dates else "—",
        "last_capture": str(max(dates)) if dates else "—",
        "weekly": buckets,
        "ramp": _ramp(buckets),
        "sources": dict(sources),
        "top_tags": top_tags,
        "demo": False,
    }

    if getattr(args, "json", False):
        return {"command": "stats", "status": "ok", "stats": stats}

    return {"command": "stats", "status": "ok", "stats": stats, "text": _format_stats(stats)}
