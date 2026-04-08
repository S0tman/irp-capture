#!/usr/bin/env python3
"""IRP dispatcher — single entry point for irp/core."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from irp.core.store import ensure_irp_dir
from irp.core.commands.capture import run_capture
from irp.core.commands.check import run_check
from irp.core.commands.demo import run_demo
from irp.core.commands.bootstrap import run_bootstrap
from irp.core.commands.inherit import run_inherit
from irp.core.commands.why import run_why


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irp", description="IRP — Intent Record Protocol")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── inherit ──────────────────────────────────────────────────────────────
    p = sub.add_parser("inherit", help="Show current IRP context")
    p.add_argument("--json", action="store_true")

    # ── capture ──────────────────────────────────────────────────────────────
    p = sub.add_parser("capture", help="Capture a new IRP entry")
    p.add_argument("--stdin", action="store_true", help="Read candidate JSON from stdin")
    p.add_argument("--json", action="store_true")

    # ── why ──────────────────────────────────────────────────────────────────
    p = sub.add_parser("why", help="Explain active reasoning lineage")
    p.add_argument("--id", type=str)
    p.add_argument("--json", action="store_true")

    # ── check ─────────────────────────────────────────────────────────────────
    p = sub.add_parser("check", help="Check a proposal against the project bridge")
    p.add_argument("proposal", type=str, help="Proposal text to check")
    p.add_argument("--json", action="store_true")

    # ── demo ─────────────────────────────────────────────────────────────────
    p_demo = sub.add_parser("demo", help="Demo utilities (generate synthetic threads + ledger entries)")
    demo_sub = p_demo.add_subparsers(dest="demo_action", required=True)

    p_demo_gen = demo_sub.add_parser(
        "generate",
        help="Generate a synthetic demo thread and a matching ledger entry",
    )
    p_demo_gen.add_argument(
        "--scenario",
        type=str,
        required=True,
        choices=["product-decision", "architecture", "pricing", "workflow", "policy"],
        help="Demo scenario to generate",
    )
    p_demo_gen.add_argument(
        "--confidence",
        type=str,
        default="high",
        choices=["low", "medium", "high"],
        help="Confidence level for the generated thread and entry (default: high)",
    )
    p_demo_gen.add_argument(
        "--write-thread",
        action="store_true",
        dest="write_thread",
        help="Save the generated thread to .irp/demo_threads/<timestamp>-<scenario>-<confidence>.md",
    )
    p_demo_gen.add_argument(
        "--post-to-slack",
        type=str,
        default=None,
        dest="post_to_slack",
        metavar="CHANNEL_ID",
        help=(
            "Post the synthetic thread + Ledger bot candidate block to a Slack channel. "
            "Provide the channel ID (e.g. C0AMXC2E069). "
            "Requires SLACK_BOT_TOKEN in env or irp/slack_capture/.env. "
            "In this mode, the local ledger is NOT written — the Confirm button handles capture."
        ),
    )
    p_demo_gen.add_argument("--json", action="store_true")

    # ── bootstrap ─────────────────────────────────────────────────────────────
    p_boot = sub.add_parser(
        "bootstrap",
        help="Initialise or enrich .irp/ from existing project artifacts (git, docs, files)",
    )
    p_boot.add_argument(
        "--from",
        dest="from_source",
        type=str,
        default="all",
        choices=["git", "docs", "files", "all"],
        help="Source to bootstrap from (default: all)",
    )
    p_boot.add_argument(
        "--path",
        type=str,
        default=None,
        help="Directory to scan for docs/files mode (default: project root)",
    )
    p_boot.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview candidates without writing to ledger",
    )
    p_boot.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of entries to write (default: 50)",
    )
    p_boot.add_argument(
        "--write-report",
        action="store_true",
        dest="write_report",
        help="Write a bootstrap report to .irp/bootstrap_reports/<timestamp>.md",
    )
    p_boot.add_argument("--json", action="store_true")

    return parser


def print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result.get("text"):
        print(result["text"])
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        project_root = Path.cwd()
        irp_dir = ensure_irp_dir(project_root)

        dispatch = {
            "inherit":   run_inherit,
            "capture":   run_capture,
            "why":       run_why,
            "check":     run_check,
            "demo":      run_demo,
            "bootstrap": run_bootstrap,
        }
        result = dispatch[args.command](project_root=project_root, irp_dir=irp_dir, args=args)
        print_result(result, getattr(args, "json", False))
        return 10 if result.get("status") == "conflict" else 0

    except KeyboardInterrupt:
        print("IRP cancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"IRP error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
