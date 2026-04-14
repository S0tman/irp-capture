#!/usr/bin/env python3
"""
IRP-aware prompt launcher.

Reads relevant IRP context, combines with a user prompt, calls an external
model, returns the response. That's it.

Not part of IRP. A disposable helper that removes copy-paste robot work
when working with multiple AI engines on the same project.

Rules:
  - Automation can prepare context. It cannot decide, hide, or infer.
  - This script does not modify .irp/ in any way.
  - This script maintains no state between calls.
  - The transport (API) is commodity. The protocol (.irp/) is sovereign.

Usage:
  # Basic: send a prompt with IRP context injected
  python3 tools/collab.py "Critique this approach: ..."

  # Filter IRP context to a topic
  python3 tools/collab.py --topic "positioning" "Is this positioning too broad?"

  # Pipe from stdin
  echo "Rewrite this for clarity: ..." | python3 tools/collab.py

  # Use a different model
  python3 tools/collab.py --model gpt-4o-mini "Summarize this thread"

  # Interactively pick a model before sending
  python3 tools/collab.py --pick "Critique this approach: ..."

  # Skip IRP context (plain call)
  python3 tools/collab.py --no-irp "What's the weather?"

  # Use a local model via Ollama (full sovereignty)
  COLLAB_API_BASE=http://localhost:11434/v1 python3 tools/collab.py --model llama3 "..."

Environment:
  OPENAI_API_KEY   — required (unless using a local model that doesn't need auth)
  COLLAB_API_BASE  — override API endpoint (default: https://api.openai.com/v1)
  COLLAB_MODEL     — override default model (default: gpt-4o)

Model tiers (for --pick menu):
  Fast/cheap   : gpt-4o-mini, gpt-4.1-mini
  Balanced     : gpt-4o, gpt-4.1
  Powerful     : gpt-4.5, o3
  Reasoning    : o3-mini, o4-mini
"""

import argparse
import json
import os
import ssl
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError

def load_dotenv(path):
    """Minimal .env loader. No dependencies."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:  # don't override existing env
                os.environ[key] = value

# Load .env from tools/ directory (next to this script)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

def read_irp_context(project_root, topic=None):
    """Read active IRP decisions. Optionally filter by topic keywords.

    Reads current.json (last 10 active decisions). If --topic is provided,
    filters to decisions whose what/why fields match any topic keyword.
    Falls back to ledger.jsonl if current.json doesn't exist.

    Returns formatted string or None.
    """
    current_path = os.path.join(project_root, ".irp", "current.json")
    ledger_path = os.path.join(project_root, ".irp", "ledger.jsonl")

    active = []

    # Try current.json first (derived state, fast)
    if os.path.exists(current_path):
        try:
            with open(current_path) as f:
                data = json.load(f)
            active = data.get("active", [])
        except (json.JSONDecodeError, KeyError):
            pass

    # Fall back to ledger if current.json is empty or missing
    if not active and os.path.exists(ledger_path):
        try:
            with open(ledger_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        active.append(json.loads(line))
            active = active[-10:]  # last 10, same as current.json
        except (json.JSONDecodeError, IOError):
            pass

    if not active:
        return None

    # Filter by topic if provided
    if topic:
        topic_words = set(topic.lower().split())
        filtered = []
        for d in active:
            text = (d.get("what", "") + " " + d.get("why", "")).lower()
            text_words = set(text.split())
            if topic_words & text_words:
                filtered.append(d)
        if filtered:
            active = filtered
        # If no match, keep all — better to over-include than miss

    # Format as readable context
    lines = ["Active project decisions (from .irp/):"]
    for d in active:
        lines.append(f"- [{d.get('id', '?')}] {d.get('what', '(no decision text)')}")
        if d.get("why"):
            lines.append(f"  Why: {d['why']}")
        if d.get("confidence"):
            lines.append(f"  Confidence: {d['confidence']}")
    lines.append("")
    lines.append("These decisions are context only. Do not modify or override them.")

    return "\n".join(lines)

def call_model(messages, model, api_key):
    """Call an OpenAI-compatible chat API. Returns response text."""
    base_url = os.environ.get("COLLAB_API_BASE", "https://api.openai.com/v1")
    url = f"{base_url}/chat/completions"

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.7,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = Request(url, data=payload, headers=headers)

    # macOS Python often lacks system CA certs
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        with urlopen(req, timeout=120, context=ssl_ctx) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except URLError as e:
        print(f"[collab] API error: {e}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, IndexError) as e:
        print(f"[collab] Unexpected API response: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="IRP-aware prompt launcher — send a prompt with project decision context to any model.",
        epilog="Transport is commodity. Protocol is sovereign.",
    )
    parser.add_argument("prompt", nargs="?",
                        help="Prompt to send (or pipe via stdin)")
    parser.add_argument("--topic", "-t",
                        help="Filter IRP context to decisions matching these keywords")
    parser.add_argument("--model", "-m",
                        default=os.environ.get("COLLAB_MODEL", "gpt-4o"),
                        help="Model to call (default: gpt-4o, or COLLAB_MODEL env)")
    parser.add_argument("--project-root", "-p",
                        default=".",
                        help="Project root containing .irp/ (default: cwd)")
    parser.add_argument("--pick", action="store_true",
                        help="Interactively pick a model before sending")
    parser.add_argument("--no-irp", action="store_true",
                        help="Skip IRP context injection")
    parser.add_argument("--system", "-s",
                        help="Additional system prompt")
    parser.add_argument("--raw", action="store_true",
                        help="Output raw response only (no headers/formatting)")
    args = parser.parse_args()

    # Interactive model picker
    MODELS = [
        ("gpt-4o",      "Balanced — fast, good for most tasks"),
        ("gpt-4.1",     "Balanced — newer, stronger reasoning than 4o"),
        ("gpt-4.5",     "Powerful — best for strategic/creative work"),
        ("o3",          "Powerful — deep reasoning, slower"),
        ("o4-mini",     "Reasoning — fast o-series, good cost/quality"),
        ("gpt-4o-mini", "Fast/cheap — quick lookups, simple tasks"),
        ("gpt-4.1-mini","Fast/cheap — newer mini, good for drafts"),
    ]

    if args.pick:
        print("\n[collab] Pick a model:", file=sys.stderr)
        for i, (name, desc) in enumerate(MODELS, 1):
            marker = " ← current default" if name == args.model else ""
            print(f"  {i}) {name:20s} {desc}{marker}", file=sys.stderr)
        print(file=sys.stderr)
        try:
            choice = input("  Enter number (or press Enter to keep default): ").strip()
            if choice:
                idx = int(choice) - 1
                if 0 <= idx < len(MODELS):
                    args.model = MODELS[idx][0]
                    print(f"[collab] Model set to: {args.model}\n", file=sys.stderr)
                else:
                    print("[collab] Invalid choice, keeping default.", file=sys.stderr)
        except (ValueError, KeyboardInterrupt):
            print("\n[collab] Keeping default model.", file=sys.stderr)

    # Read prompt from arg or stdin
    if args.prompt:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    else:
        parser.print_help()
        sys.exit(1)

    if not prompt:
        print("[collab] Empty prompt", file=sys.stderr)
        sys.exit(1)

    # Resolve API key
    api_key = os.environ.get("OPENAI_API_KEY", "")

    # Build messages
    messages = []

    # IRP context (read-only, never modified)
    irp_ctx = None
    if not args.no_irp:
        project_root = os.path.abspath(args.project_root)
        irp_ctx = read_irp_context(project_root, args.topic)
        if irp_ctx:
            messages.append({"role": "system", "content": irp_ctx})

    # Additional system prompt
    if args.system:
        messages.append({"role": "system", "content": args.system})

    # User prompt
    messages.append({"role": "user", "content": prompt})

    # Log what we're doing (unless --raw)
    if not args.raw:
        print(f"[collab] Model: {args.model}", file=sys.stderr)
        if irp_ctx:
            ctx_count = irp_ctx.count("- [IRP-")
            print(f"[collab] IRP context: {ctx_count} decision(s) injected", file=sys.stderr)
        else:
            print(f"[collab] IRP context: none", file=sys.stderr)
        print(f"[collab] Sending...", file=sys.stderr)

    # Call model
    response = call_model(messages, model=args.model, api_key=api_key)

    # Output
    print(response)

if __name__ == "__main__":
    main()
