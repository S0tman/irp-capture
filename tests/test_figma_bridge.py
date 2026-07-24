"""Tests for irp/figma_plugin/bridge/server.py, the local HTTP bridge that
receives decisions from the Figma plugin.

This module uses only the standard library (http.server, urllib), so unlike
the other "harder entrypoints" it can get real functional tests: a real
HTTPServer is started on an ephemeral loopback port and exercised with real
HTTP requests. This is a loopback-only server the test itself starts and
stops, no external network call is ever made, and FIGMA_PAT is left unset
so fetch_figma_comments (the one function that calls out to the real Figma
API) is never invoked.

The module's global PROJECT_ROOT is monkeypatched to a tmp_path before
starting the server, so a captured decision never touches the real repo's
working directory.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401


@pytest.fixture
def bridge_server(tmp_path, monkeypatch):
    """Import the bridge module, point it at tmp_path, and run a real
    HTTPServer on an ephemeral port for the duration of one test."""
    # Importing at module scope runs argparse.parse_known_args() against
    # pytest's own argv, harmless since --project-root isn't among pytest's
    # flags and parse_known_args ignores the rest.
    import irp.figma_plugin.bridge.server as bridge

    monkeypatch.setattr(bridge, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(bridge, "FIGMA_PAT", "")  # never call the real Figma API

    server = HTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, tmp_path
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _request(port, method, path, body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


class TestCapture:
    def test_valid_decision_returns_200_and_id(self, bridge_server):
        port, project_root = bridge_server
        status, raw = _request(port, "POST", "/capture", {
            "decision": "Add commenting to the dashboard",
            "why": "Users asked for it",
            "context": {"page": "Home", "selection": "Frame 1"},
        })
        assert status == 200
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["id"].startswith("IRP-")

    def test_captured_entry_written_to_project_ledger(self, bridge_server):
        port, project_root = bridge_server
        _request(port, "POST", "/capture", {"decision": "Ship it", "why": "Ready"})
        ledger_path = project_root / ".irp" / "ledger.jsonl"
        assert ledger_path.exists()
        entries = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert entries[0]["what"] == "Ship it"
        assert entries[0]["source"] == "figma"

    def test_missing_decision_returns_400(self, bridge_server):
        port, _ = bridge_server
        status, _ = _request(port, "POST", "/capture", {"why": "no decision text"})
        assert status == 400

    def test_invalid_json_body_returns_400(self, bridge_server):
        port, _ = bridge_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/capture", body=b"{not json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_unknown_post_path_returns_404(self, bridge_server):
        # do_POST's early-return branch for an unrecognized path never reads
        # self.rfile, so a request WITH a body can trigger a TCP RST (the
        # server closes the socket, protocol_version defaults to HTTP/1.0, 
        # while unread body bytes are still buffered). Use a bodyless
        # request here: this test is about the 404 routing, not body
        # handling, and a body-carrying variant is covered by the /capture
        # tests above.
        port, _ = bridge_server
        status, _ = _request(port, "POST", "/unknown", None)
        assert status == 404

    def test_context_fields_included_in_captured_entry(self, bridge_server):
        port, project_root = bridge_server
        _request(port, "POST", "/capture", {
            "decision": "Use 8px grid", "why": "Consistency",
            "context": {"page": "Design System", "selection": "Spacing"},
        })
        entries = [
            json.loads(l) for l in
            (project_root / ".irp" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert entries[0]["context"]["page"] == "Design System"


class TestComments:
    def test_no_figma_pat_returns_empty_with_reason(self, bridge_server):
        port, _ = bridge_server
        status, raw = _request(port, "GET", "/comments?file_key=abc123")
        assert status == 200
        data = json.loads(raw)
        assert data["comments"] == []
        assert "no FIGMA_PAT" in data["reason"]

    def test_unknown_get_path_returns_404(self, bridge_server):
        port, _ = bridge_server
        status, _ = _request(port, "GET", "/unknown")
        assert status == 404


class TestCors:
    def test_options_preflight_returns_200_with_cors_headers(self, bridge_server):
        port, _ = bridge_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("OPTIONS", "/capture")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Origin") == "*"
        conn.close()
