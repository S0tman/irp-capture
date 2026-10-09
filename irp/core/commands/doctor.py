"""irp doctor — installation and environment health check."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

try:
    from importlib.metadata import version as pkg_version, PackageNotFoundError
except ImportError:
    from importlib_metadata import version as pkg_version, PackageNotFoundError  # type: ignore

from irp.core.resolver import build_retirement_set, build_supersession_map
from irp.core.store import (
    is_unconfirmed,
    read_current,
    read_ledger,
    read_reconstructions,
    rebuild_current,
    shared_unconfirmed_ids,
    write_current,
)

_ACCEPT_HINT = "irp bootstrap --accept REC-..."


def _scan_unconfirmed(irp_path: Path) -> tuple:
    """Legacy bootstrap guesses still sitting in the ledger.

    An older `irp bootstrap` wrote them there as if they were decisions; the
    ledger is append-only, so they can only be dealt with by superseding or
    retiring them. Returns (own_ids, pending_count, shared_ids):
      own_ids       ids that belong to a guess alone, so retire/supersede by id is safe
      pending_count every guess line not yet superseded or retired
      shared_ids    ids a guess shares with a confirmed decision (older versions
                    could give both the same id); retire/supersede by id would
                    hit the confirmed decision too, so these are never advised
    """
    ledger = read_ledger(irp_path)
    done = build_supersession_map(ledger) | build_retirement_set(ledger)
    shared = shared_unconfirmed_ids(ledger) - done
    pending = [
        e for e in ledger
        if is_unconfirmed(e) and e.get("type") == "decision" and e.get("id") not in done
    ]
    own_ids = list(dict.fromkeys(
        str(e.get("id", "")) for e in pending if e.get("id") not in shared
    ))
    return own_ids, len(pending), sorted(shared)


def _ledger_notes(
    unconfirmed_ids: list,
    shared_ids: list,
    pending_reconstructions: int,
    current_stale: bool,
) -> list:
    notes = []
    n = len(unconfirmed_ids)
    if n:
        noun = "guess" if n == 1 else "guesses"
        shown = ", ".join(unconfirmed_ids[:5]) + (f" and {n - 5} more" if n > 5 else "")
        notes += [
            f"    ! {n} unconfirmed bootstrap {noun} in ledger.jsonl: {shown}",
            "        An older `irp bootstrap` wrote these as if they were decisions. Agents, exports,",
            "        checks and evidence already ignore them, and they can't be deleted (the ledger is append-only).",
            "        If one was a real decision, confirm it: irp mod supersede <id> --decision \"...\" --reason \"...\"",
            "        If it was not: irp mod retire <id> --reason \"bootstrap guess, not a decision\"",
        ]
    k = len(shared_ids)
    if k:
        noun = "guess shares its id" if k == 1 else "guesses share their ids"
        notes += [
            f"    ! {k} bootstrap {noun} with a confirmed decision: {', '.join(shared_ids)}",
            "        Older versions could give a guess and a confirmed decision the same id, so both lines are in the file.",
            "        The guess is already ignored by agents, exports, checks and evidence, so it can stay.",
            "        irp mod refuses to retire or supersede one of these ids unless you add --shared-id-ok.",
            "        That flag changes the confirmed decision (the guess half is already ignored), so use it only if you mean to.",
        ]
    if current_stale:
        notes += [
            "    ! current.json still lists unconfirmed bootstrap guesses from an older version",
            "        Readers recompute the list from the ledger, so agents are not affected.",
            "        Rebuild the file from the ledger with: irp doctor --fix",
        ]
    if pending_reconstructions:
        noun = "reconstruction" if pending_reconstructions == 1 else "reconstructions"
        notes += [
            f"    ! {pending_reconstructions} unconfirmed {noun} waiting in .irp/reconstructions.jsonl (not in the ledger)",
            f"        Read them, then accept the real ones: {_ACCEPT_HINT}",
        ]
    return notes

def _check(label: str, ok: bool, detail: str = "") -> dict:
    return {"label": label, "ok": ok, "detail": detail}

def run_doctor(project_root: Path, irp_dir: Path, args) -> dict:
    checks: list[dict] = []
    warnings: list[str] = []

    # ── Python version ────────────────────────────────────────────────────────
    py = sys.version_info
    py_ok = py >= (3, 9)
    py_str = f"{py.major}.{py.minor}.{py.micro}"
    checks.append(_check("Python", py_ok, py_str if py_ok else f"{py_str} — requires 3.9+"))

    # ── irp-capture package version ───────────────────────────────────────────
    try:
        irp_ver = pkg_version("irp-capture")
        checks.append(_check("irp-capture", True, f"v{irp_ver}"))
    except PackageNotFoundError:
        checks.append(_check("irp-capture", False, "package not found — run: pip install irp-capture"))

    # ── .irp/ directory ───────────────────────────────────────────────────────
    irp_path = project_root / ".irp"
    irp_exists = irp_path.is_dir()
    checks.append(_check(
        ".irp/ directory",
        irp_exists,
        str(irp_path) if irp_exists else "not found — run: irp inherit \"Project: <name>\""
    ))

    # ── ledger.jsonl ──────────────────────────────────────────────────────────
    entry_count = 0
    if irp_exists:
        ledger = irp_path / "ledger.jsonl"
        if ledger.exists():
            try:
                lines = [l for l in ledger.read_text().splitlines() if l.strip()]
                valid = 0
                for line in lines:
                    json.loads(line)
                    valid += 1
                entry_count = valid
                checks.append(_check("ledger.jsonl", True, f"{valid} {'entry' if valid == 1 else 'entries'}"))
            except (json.JSONDecodeError, OSError) as e:
                checks.append(_check("ledger.jsonl", False, f"corrupt — {e}"))
        else:
            checks.append(_check("ledger.jsonl", False, "no entries yet — capture your first decision with: irp capture"))

        # ── current.json ──────────────────────────────────────────────────────
        current = irp_path / "current.json"
        if current.exists():
            try:
                data = json.loads(current.read_text())
                if isinstance(data, list):
                    n = len(data)
                elif isinstance(data, dict):
                    # Guesses an old bootstrap left in the file are not active decisions.
                    listed = data.get("active", [])
                    n = len([e for e in listed if not is_unconfirmed(e)]) if isinstance(listed, list) else 0
                else:
                    n = 0
                checks.append(_check("current.json", True, f"{n} active {'decision' if n == 1 else 'decisions'}"))
            except (json.JSONDecodeError, OSError) as e:
                checks.append(_check("current.json", False, f"corrupt — {e}"))
        else:
            checks.append(_check("current.json", False, "missing — will be created on next capture"))
    else:
        checks.append(_check("ledger.jsonl", False, "skipped — .irp/ not found"))
        checks.append(_check("current.json", False, "skipped — .irp/ not found"))

    # ── bootstrap guesses (legacy ledger lines, pending reconstructions) ──────
    unconfirmed_ids: list = []
    shared_ids: list = []
    unconfirmed_count = 0
    pending_reconstructions = 0
    current_stale = False
    fixed: list = []
    if irp_exists:
        unconfirmed_ids, unconfirmed_count, shared_ids = _scan_unconfirmed(irp_path)
        pending_reconstructions = sum(
            1 for r in read_reconstructions(irp_path) if r.get("status", "unconfirmed") == "unconfirmed"
        )
        if (irp_path / "current.json").exists():
            try:
                listed = read_current(irp_path).get("active", [])
            except (json.JSONDecodeError, OSError, AttributeError):
                listed = []
            current_stale = any(is_unconfirmed(e) for e in listed)
            if current_stale and getattr(args, "fix", False):
                # current.json is derived from the ledger, so rebuilding it is safe.
                write_current(irp_path, rebuild_current(read_ledger(irp_path)))
                fixed.append("current.json")
                current_stale = False
    ledger_notes = _ledger_notes(unconfirmed_ids, shared_ids, pending_reconstructions, current_stale)
    if fixed:
        ledger_notes.append("    ✓ current.json rebuilt from the ledger (unconfirmed guesses removed)")

    # ── Claude Code skill ─────────────────────────────────────────────────────
    skill = project_root / "SKILL.md"
    checks.append(_check(
        "Claude Code skill",
        skill.exists(),
        "SKILL.md found" if skill.exists() else "not found — add with: curl -O https://raw.githubusercontent.com/S0tman/irp-capture/main/SKILL.md"
    ))

    # ── Optional integrations ─────────────────────────────────────────────────
    # Obsidian
    vault = os.environ.get("IRP_OBSIDIAN_VAULT")
    if vault:
        vault_path = Path(vault)
        obsidian_ok = vault_path.is_dir()
        checks.append(_check(
            "Obsidian",
            obsidian_ok,
            str(vault_path) if obsidian_ok else f"IRP_OBSIDIAN_VAULT set but path not found: {vault}"
        ))
    else:
        checks.append(_check("Obsidian", False, "not configured — set IRP_OBSIDIAN_VAULT=/path/to/vault"))

    # MemPalace
    mp_spec = importlib.util.find_spec("chromadb")
    mp_path = os.environ.get("IRP_MEMPALACE_PATH", str(Path.home() / ".mempalace" / "palace"))
    mp_dir_exists = Path(mp_path).is_dir()
    if mp_spec and mp_dir_exists:
        checks.append(_check("MemPalace", True, mp_path))
    elif mp_spec:
        checks.append(_check("MemPalace", False, f"chromadb installed but palace not found at {mp_path}"))
    else:
        checks.append(_check("MemPalace", False, "not installed — run: pip install 'irp-capture[mempalace]'"))

    # MCP server
    mcp_spec = importlib.util.find_spec("mcp")
    checks.append(_check(
        "MCP server",
        mcp_spec is not None,
        "installed — run: irp-mcp" if mcp_spec else "not installed — run: pip install 'irp-capture[mcp]'"
    ))

    # REST API
    fastapi_spec = importlib.util.find_spec("fastapi")
    checks.append(_check(
        "REST API",
        fastapi_spec is not None,
        "installed — run: irp-api" if fastapi_spec else "not installed — run: pip install 'irp-capture[api]'"
    ))

    # ── Summary ───────────────────────────────────────────────────────────────
    core_checks = checks[:6]   # Python, irp-capture, .irp/, ledger, current, Claude skill
    core_failed = [c for c in core_checks if not c["ok"]]
    all_ok = len(core_failed) == 0

    # ── Render text output ────────────────────────────────────────────────────
    lines = ["", "IRP Doctor", ""]

    sections = [
        ("System",       checks[0:2]),
        ("Ledger",       checks[2:5]),
        ("Editor",       checks[5:6]),
        ("Integrations", checks[6:]),
    ]

    for section_name, section_checks in sections:
        lines.append(f"  {section_name}")
        for c in section_checks:
            mark = "✓" if c["ok"] else "✗"
            detail = f" — {c['detail']}" if c["detail"] else ""
            lines.append(f"    {mark} {c['label']}{detail}")
        if section_name == "Ledger":
            lines.extend(ledger_notes)
        lines.append("")

    if all_ok:
        lines.append("  All core checks passed.")
    else:
        lines.append(f"  {len(core_failed)} core check(s) failed:")
        for c in core_failed:
            lines.append(f"    → {c['label']}: {c['detail']}")

    lines.append("")

    return {
        "status": "ok" if all_ok else "issues_found",
        "checks": checks,
        "entry_count": entry_count,
        "unconfirmed_count": unconfirmed_count,
        "unconfirmed_ids": unconfirmed_ids,
        "shared_ids": shared_ids,
        "current_stale": current_stale,
        "fixed": fixed,
        "reconstructions_pending": pending_reconstructions,
        "text": "\n".join(lines),
    }
