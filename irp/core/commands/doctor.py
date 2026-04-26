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
                n = len(data) if isinstance(data, list) else 1
                checks.append(_check("current.json", True, f"{n} active {'decision' if n == 1 else 'decisions'}"))
            except (json.JSONDecodeError, OSError) as e:
                checks.append(_check("current.json", False, f"corrupt — {e}"))
        else:
            checks.append(_check("current.json", False, "missing — will be created on next capture"))
    else:
        checks.append(_check("ledger.jsonl", False, "skipped — .irp/ not found"))
        checks.append(_check("current.json", False, "skipped — .irp/ not found"))

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
        "text": "\n".join(lines),
    }
