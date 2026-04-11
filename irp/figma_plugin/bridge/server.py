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
parser.add_argument("--project-root", default=os.getcwd(),
                    help="Path to the project root containing .irp/ (default: cwd)")
args_global, _ = parser.parse_known_args()
PROJECT_ROOT = os.path.abspath(args_global.project_root)
print(f"[bridge] Project root: {PROJECT_ROOT}")

# Figma PAT for comment fetching (optional — auto-populate feature)
FIGMA_PAT = os.environ.get("FIGMA_PAT", "")
if FIGMA_PAT:
    print(f"[bridge] Figma PAT loaded — comment auto-populate enabled")
else:
    print(f"[bridge] No FIGMA_PAT set — comment auto-populate disabled (manual entry only)")

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
            from pathlib import Path
            from irp.core.store import (
                ensure_irp_dir, read_ledger, next_irp_id,
                append_ledger_entry, rebuild_current, write_current
            )

            project_root = Path(PROJECT_ROOT)
            irp_dir = ensure_irp_dir(project_root)
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
    server = HTTPServer(("localhost", PORT), BridgeHandler)
    print(f"[bridge] IRP Figma bridge running on http://localhost:{PORT}")
    print(f"[bridge] Waiting for captures from Figma plugin…")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Stopped.")
        sys.exit(0)
