from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


def ensure_irp_dir(project_root: Path) -> Path:
    irp_dir = project_root / ".irp"
    irp_dir.mkdir(exist_ok=True)

    ledger_file = irp_dir / "ledger.jsonl"
    current_file = irp_dir / "current.json"

    if not ledger_file.exists():
        ledger_file.write_text("", encoding="utf-8")

    if not current_file.exists():
        current_file.write_text(
            json.dumps({"version": 1, "active": []}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return irp_dir


def read_current(irp_dir: Path) -> dict[str, Any]:
    raw = (irp_dir / "current.json").read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else {"version": 1, "active": []}


def write_current(irp_dir: Path, data: dict[str, Any]) -> None:
    (irp_dir / "current.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def append_ledger_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    with (irp_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_irp_id(ledger: list[dict[str, Any]]) -> str:
    """Return the next sequential IRP-YYYY-MM-DD-NNN id for today."""
    today = date.today().isoformat()
    todays_entries = [x for x in ledger if str(x.get("timestamp", "")).startswith(today)]
    seq = len(todays_entries) + 1
    return f"IRP-{today}-{seq:03d}"


def rebuild_current(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive current.json from ledger — last 10 decision entries."""
    active = [x for x in ledger if x.get("type") == "decision"]
    return {"version": 1, "active": active[-10:]}


def read_ledger(irp_dir: Path) -> list[dict[str, Any]]:
    path = irp_dir / "ledger.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ── project config ────────────────────────────────────────────────────────────

_CONFIG_DEFAULTS: dict[str, Any] = {
    "control_level": "advanced",
}


def read_config(irp_dir: Path) -> dict[str, Any]:
    """Read .irp/config.json. Missing keys fall back to defaults."""
    path = irp_dir / "config.json"
    if not path.exists():
        return dict(_CONFIG_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = dict(_CONFIG_DEFAULTS)
        result.update(data)
        return result
    except (json.JSONDecodeError, OSError):
        return dict(_CONFIG_DEFAULTS)


def write_config(irp_dir: Path, data: dict[str, Any]) -> None:
    """Write .irp/config.json (full replace)."""
    (irp_dir / "config.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── craft ledger ──────────────────────────────────────────────────────────────

def read_craft(irp_dir: Path) -> list[dict[str, Any]]:
    """Read .irp/craft.jsonl — individual craft knowledge entries."""
    path = irp_dir / "craft.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def append_craft_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    """Append a single entry to .irp/craft.jsonl."""
    with (irp_dir / "craft.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_craft_id(craft_entries: list[dict[str, Any]]) -> str:
    """Return the next sequential CRAFT-YYYY-MM-DD-NNN id for today."""
    today = date.today().isoformat()
    todays = [x for x in craft_entries if str(x.get("timestamp", "")).startswith(today)]
    seq = len(todays) + 1
    return f"CRAFT-{today}-{seq:03d}"
