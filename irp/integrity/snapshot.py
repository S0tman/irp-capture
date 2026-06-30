"""Create deterministic integrity snapshots.

Offline and read-only with respect to the ledger: snapshot creation never
mutates ledger.jsonl or current.json. The ledger is read once into a single
byte buffer (TOCTOU-safe), and both digests plus the strict parse derive from
exactly those bytes. The snapshot file is written atomically.
"""
from __future__ import annotations

import json
import secrets
from datetime import date
from pathlib import Path
from typing import Any

from .errors import LedgerIntegrityError
from .manifest import build_snapshot_file
from .strict import parse_ledger_strict


def integrity_dir(irp_dir: Path) -> Path:
    """Return .irp/integrity/, creating it and snapshots/ if needed."""
    base = irp_dir / "integrity"
    (base / "snapshots").mkdir(parents=True, exist_ok=True)
    return base


def get_or_create_ledger_id(irp_dir: Path) -> str:
    """Return the stable random ledger_id, creating it on first use.

    A random identifier (not a path, repo or customer name) so snapshots from
    different projects cannot be confused or replayed across each other. It does
    not prove ownership.
    """
    ident = integrity_dir(irp_dir) / "identity.json"
    if ident.exists():
        try:
            value = json.loads(ident.read_text(encoding="utf-8")).get("ledger_id")
            if isinstance(value, str) and value:
                return value
        except (ValueError, OSError):
            pass
    new_id = "ILID-" + secrets.token_hex(16)
    ident.write_text(json.dumps({"ledger_id": new_id}, indent=2) + "\n", encoding="utf-8")
    return new_id


def _next_snapshot_id(snapshots_dir: Path) -> str:
    today = date.today().isoformat()
    existing = list(snapshots_dir.glob(f"IRPS-{today}-*.json"))
    return f"IRPS-{today}-{len(existing) + 1:03d}"


def create_snapshot(irp_dir: Path, *, allow_malformed: bool = False) -> dict[str, Any]:
    """Create a snapshot of the current ledger. Returns a result dict."""
    ledger_path = irp_dir / "ledger.jsonl"
    raw = ledger_path.read_bytes() if ledger_path.exists() else b""  # single read

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerIntegrityError(f"ledger is not valid UTF-8: {exc}")

    parsed = parse_ledger_strict(text)
    if parsed.errors and not allow_malformed:
        preview = "; ".join(f"line {e['line']}: {e['kind']}" for e in parsed.errors[:5])
        raise LedgerIntegrityError(
            f"refusing to snapshot a malformed ledger ({len(parsed.errors)} bad line(s)): "
            f"{preview}. Fix the ledger, or pass allow_malformed to snapshot anyway."
        )

    base = integrity_dir(irp_dir)
    snapshots_dir = base / "snapshots"
    snapshot_id = _next_snapshot_id(snapshots_dir)
    ledger_id = get_or_create_ledger_id(irp_dir)

    snapshot_file = build_snapshot_file(
        snapshot_id=snapshot_id,
        ledger_id=ledger_id,
        raw_bytes=raw,
        entries=parsed.entries,
    )

    out_path = snapshots_dir / f"{snapshot_id}.json"
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(snapshot_file, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp_path.replace(out_path)  # atomic

    return {
        "snapshot_id": snapshot_id,
        "path": str(out_path),
        "file": snapshot_file,
        "entry_count": len(parsed.entries),
        "malformed": parsed.errors,
        "duplicate_ids": parsed.duplicate_ids,
    }
