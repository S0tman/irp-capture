#!/usr/bin/env python3
"""
IRP Figma Bridge — local HTTP server that receives decisions from the
Figma plugin and writes them to the IRP ledger via irp capture.
"""

import json
import sys
import os
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs
from urllib.error import URLError
import ssl

# Ensure IRP package is importable regardless of how bridge was launched
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Parse --project-root argument
parser = argparse.ArgumentParser()
parser.add_argument("--project-root", default=None,
                    help="The IRP project to write to (default: found from the current folder)")
args_global, _ = parser.parse_known_args()
# Named with --project-root: that folder only. Otherwise found from here.
_NAMED = args_global.project_root is not None
PROJECT_ROOT = os.path.abspath(args_global.project_root if _NAMED else os.getcwd())


def _find_project():
    """The project, found on each capture. Raises ProjectNotFound; creates nothing."""
    from irp.core.store import find_project

    if _NAMED:
        return find_project(project=PROJECT_ROOT)
    return find_project(start=PROJECT_ROOT)


# Figma PAT for comment fetching (optional — auto-populate feature)
FIGMA_PAT = os.environ.get("FIGMA_PAT", "")

PORT = 3002

def fetch_figma_comments(file_key):
    """Fetch resolved comments from a Figma file via REST API."""
    url = f"https://api.figma.com/v1/files/{file_key}/comments"
    req = Request(url, headers={
        "X-Figma-Token": FIGMA_PAT,
    })
    # macOS Python often lacks system CA certs — skip verification for local bridge
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(req, timeout=10, context=ssl_ctx) as resp:
            data = json.loads(resp.read())
    except URLError as e:
        print(f"[bridge] Figma API error: {e}")
        return []

    # Filter to resolved comments, most recent first
    resolved = [
        {
            "id": c["id"],
            "message": c.get("message", ""),
            "resolved_at": c.get("resolved_at"),
            "created_at": c.get("created_at"),
            "user": c.get("user", {}).get("handle", "unknown"),
        }
        for c in data.get("comments", [])
        if c.get("resolved_at")
    ]
    resolved.sort(key=lambda c: c["resolved_at"], reverse=True)
    return resolved[:10]

class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[bridge] {args[0]} {args[1]} {args[2]}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/comments":
            self.send_response(404)
            self.end_headers()
            return

        if not FIGMA_PAT:
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"comments": [], "reason": "no FIGMA_PAT configured"}).encode())
            return

        params = parse_qs(parsed.query)
        file_key = params.get("file_key", [None])[0]
        if not file_key:
            self.send_response(400)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "file_key required"}).encode())
            return

        try:
            comments = fetch_figma_comments(file_key)
            print(f"[bridge] fetched {len(comments)} resolved comments for file {file_key[:12]}…")
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"comments": comments}).encode())
        except Exception as e:
            print(f"[bridge] comment fetch error: {e}")
            self.send_response(500)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_OPTIONS(self):
        # CORS preflight — Figma plugin iframe needs this
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path != "/capture":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            return

        decision = payload.get("decision", "").strip()
        why = payload.get("why", "").strip()
        context = payload.get("context", {})

        if not decision:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            return

        # Build IRP input
        parts = [decision]
        if why:
            parts.append(f"Why: {why}")
        if context.get("page"):
            parts.append(f"Page: {context['page']}")
        if context.get("selection"):
            parts.append(f"Selection: {context['selection']}")

        irp_input = "\n".join(parts)

        try:
            from datetime import date
            from irp.core.store import (
                ProjectNotFound, read_ledger, next_irp_id,
                append_ledger_entry, rebuild_current, write_current
            )

            try:
                irp_dir = _find_project().irp_dir
            except ProjectNotFound as exc:
                message = exc.write_message("capture")
                print(f"[bridge] not captured: {message.splitlines()[0]}")
                self.send_response(409)
                self._cors_headers()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": message}).encode())
                return
            # read_ledger is unfiltered on purpose: ids must see every line,
            # including legacy bootstrap guesses. rebuild_current below leaves
            # those guesses out of current.json.
            ledger = read_ledger(irp_dir)

            entry = {
                "type": "decision",
                "id": next_irp_id(ledger),
                "what": decision,
                "why": why or "",
                "confidence": "medium",
                "timestamp": date.today().isoformat(),
                "source": "figma",
                "tags": [],
                "context": context,
            }

            append_ledger_entry(irp_dir, entry)
            updated_ledger = read_ledger(irp_dir)
            write_current(irp_dir, rebuild_current(updated_ledger))

            print(f"[bridge] captured: {entry['id']} — {decision[:60]}")
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "id": entry["id"]}).encode())
        except Exception as e:
            print(f"[bridge] exception: {e}")
            self.send_response(500)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

if __name__ == "__main__":
    from irp.core.store import ProjectNotFound

    try:
        PROJECT_ROOT = str(_find_project().root)
    except ProjectNotFound as exc:
        print(f"[bridge] No IRP project at {exc.searched_from}, so the bridge didn't start. "
              f"Start one with: {exc.hint}", file=sys.stderr)
        sys.exit(1)
    _NAMED = True
    print(f"[bridge] Project root: {PROJECT_ROOT}")
    if FIGMA_PAT:
        print("[bridge] Figma PAT loaded: comment auto-populate enabled")
    else:
        print("[bridge] No FIGMA_PAT set: comment auto-populate disabled (manual entry only)")
    server = HTTPServer(("localhost", PORT), BridgeHandler)
    print(f"[bridge] IRP Figma bridge running on http://localhost:{PORT}")
    print(f"[bridge] Waiting for captures from Figma plugin…")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Stopped.")
        sys.exit(0)
