"""Import-smoke tests for irp/mcp/server.py and irp/api/server.py.

Both are thin wrappers around already-tested command handlers
(run_capture, run_why, run_inherit, run_check), exposed over an external
framework (MCP / FastAPI + uvicorn). Neither `mcp` nor `fastapi` is part of
the dev extras in this environment, and both modules do real work at import
time (building a FastMCP/FastAPI app, parsing sys.argv) that isn't safe to
exercise without the real dependency present. Per the task's guidance for
"thin external-service entrypoints with little pure logic," these get an
import-skip smoke test rather than a forced/brittle functional test.

If the optional dependency IS installed, the smoke test still provides real
signal: it proves the module imports cleanly end-to-end (no typos, no
broken internal imports) and that the expected top-level objects exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401


class TestMcpServerSmoke:
    def test_imports_and_exposes_tools(self):
        pytest.importorskip("mcp", reason="mcp not installed (irp-capture[mcp] extra)")
        import irp.mcp.server as mcp_server

        assert hasattr(mcp_server, "mcp")
        assert callable(mcp_server.irp_capture)
        assert callable(mcp_server.irp_why)
        assert callable(mcp_server.irp_inherit)
        assert callable(mcp_server.irp_check)
        assert callable(mcp_server.main)

    def test_resolve_paths_honors_env_override(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp", reason="mcp not installed (irp-capture[mcp] extra)")
        import irp.mcp.server as mcp_server

        monkeypatch.setenv("IRP_PROJECT_ROOT", str(tmp_path))
        project_root, irp_dir = mcp_server._resolve_paths()
        assert project_root == tmp_path.resolve()
        assert irp_dir == tmp_path.resolve() / ".irp"
        assert irp_dir.is_dir()


class TestApiServerSmoke:
    def test_imports_and_exposes_fastapi_app(self, monkeypatch, tmp_path):
        pytest.importorskip("fastapi", reason="fastapi not installed (irp-capture[api] extra)")
        pytest.importorskip("uvicorn", reason="uvicorn not installed (irp-capture[api] extra)")

        # api/server.py parses --project-root from real sys.argv at import
        # time; pin it at a throwaway directory so importing the module in a
        # test run never touches (or creates .irp/ under) the real repo.
        monkeypatch.setattr(sys, "argv", ["irp-api", "--project-root", str(tmp_path)])

        import irp.api.server as api_server

        assert api_server.app.title == "IRP API"
        assert api_server.PROJECT_ROOT == Path(tmp_path).resolve()
