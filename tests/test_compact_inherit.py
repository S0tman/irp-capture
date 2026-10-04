"""The irp_inherit MCP tool returns a slim view of the active decisions by default.

The 4 Oct 2026 eval (eval/inherit-vs-agents) found the full JSON was the longest
context an agent got: 12,215 characters for four projects, against 7,502 for an
AGENTS.md holding the same facts. The slim view keeps what an agent needs (id,
date, what, why, rejected options, supersedes) and drops bookkeeping fields.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from irp.core.compact import compact_entry, compact_inherit  # noqa: E402

FULL = {
    "type": "decision", "id": "IRP-2026-02-02-001", "timestamp": "2026-02-02T09:00:00Z",
    "what": "Internal services talk over gRPC.", "why": "Latency.",
    "alternatives": "REST over HTTP/2: rejected, too slow.", "supersedes": "IRP-2025-11-10-001",
    "confidence": "high", "source": "cli", "tags": ["backend"],
    "source_ref": {"channel_id": "C1"},
}


def test_compact_entry_keeps_what_an_agent_needs():
    assert compact_entry(FULL) == {
        "id": "IRP-2026-02-02-001", "date": "2026-02-02",
        "what": "Internal services talk over gRPC.", "why": "Latency.",
        "rejected": "REST over HTTP/2: rejected, too slow.",
        "supersedes": "IRP-2025-11-10-001",
    }


def test_compact_entry_drops_empty_fields():
    e = compact_entry({"id": "IRP-2026-01-01-001", "what": "Use X", "why": "", "timestamp": ""})
    assert e == {"id": "IRP-2026-01-01-001", "what": "Use X"}


def test_compact_inherit_shape():
    result = {"project_root": "/repo", "active_count": 1, "omitted": 0, "active": [FULL], "text": "..."}
    out = compact_inherit(result)
    assert set(out) == {"project_root", "active_count", "omitted", "active"}
    assert out["active"] == [compact_entry(FULL)]


def test_compact_is_much_smaller_than_full():
    result = {"project_root": "/repo", "active_count": 20, "omitted": 0, "active": [FULL] * 20}
    full = json.dumps({k: result[k] for k in ("project_root", "active_count", "omitted", "active")}, indent=2)
    slim = json.dumps(compact_inherit(result))
    assert len(slim) < 0.7 * len(full)


def test_server_defaults_to_compact_and_can_return_full():
    src = (ROOT / "irp" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "def irp_inherit(full: bool = False)" in src
    assert "compact_inherit(" in src


def test_mcp_server_modules_import_like_an_installed_package():
    """irp-mcp crashed on start in 0.9.1: check.py used script-style imports
    (`from store import ...`) that only work when the CLI puts irp/core on
    sys.path. Import every module the server needs in a clean interpreter."""
    import subprocess
    code = ("import irp.core.commands.capture, irp.core.commands.why, "
            "irp.core.commands.inherit, irp.core.commands.check, irp.core.compact")
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-c", code], cwd="/", env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_mcp_extra_is_capped_below_2():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'mcp = ["mcp>=1.0,<2"]' in text


def test_server_uses_the_fastmcp_1x_constructor():
    """mcp 1.x FastMCP takes `instructions`, not `description`; the old
    keyword made irp-mcp crash on start."""
    src = (ROOT / "irp" / "mcp" / "server.py").read_text(encoding="utf-8")
    ctor = src[src.index("mcp = FastMCP("):]
    ctor = ctor[: ctor.index(")") + 1]
    assert "description=" not in ctor
    assert "instructions=" in ctor
