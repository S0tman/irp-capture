#!/usr/bin/env python3
"""IRP dispatcher — single entry point for irp/core."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from store import ensure_irp_dir
from commands.capture import run_capture
from commands.check import run_check
from commands.config import run_config
from commands.craft import run_craft
from commands.defer import run_defer
from commands.demo import run_demo
from commands.bootstrap import run_bootstrap
from commands.export import run_export
from commands.find import run_find
from commands.docs import run_docs
from commands.resolve import run_resolve
from commands.guard import run_guard
from commands.inherit import run_inherit
from commands.why import run_why


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irp", description="IRP project-local dispatcher")
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
    p = sub.add_parser("check", help="Check a proposal against active decisions (via resolver)")
    p.add_argument("proposal", type=str, help="Proposal text to check")
    p.add_argument("--json", action="store_true")

    # ── resolve ───────────────────────────────────────────────────────────────
    p_res = sub.add_parser("resolve", help="Query the Decision Resolver — ranked conflicts with provenance")
    p_res.add_argument("query", type=str, help="Proposal or action to resolve")
    p_res.add_argument("--tag", type=str, default=None, help="Filter to decisions with this tag")
    p_res.add_argument("--scope", type=str, default=None, help="Filter to decisions mentioning this scope")
    p_res.add_argument("--top", type=int, default=3, metavar="N", help="Show top N conflicts (default: 3)")
    p_res.add_argument("--json", action="store_true")

    # ── config ────────────────────────────────────────────────────────────────
    p_cfg = sub.add_parser("config", help="Read and write project-level IRP settings (.irp/config.json)")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)

    p_cfg_get = cfg_sub.add_parser("get", help="Show IRP project settings")
    p_cfg_get.add_argument("key", nargs="?", type=str, default=None,
                           help="Optional key to show (e.g. control_level)")
    p_cfg_get.add_argument("--json", action="store_true")

    p_cfg_set = cfg_sub.add_parser("set", help="Set an IRP project setting")
    p_cfg_set.add_argument("key", type=str, help="Key to set (e.g. control_level)")
    p_cfg_set.add_argument("value", type=str, help="Value (e.g. easy, medium, advanced)")
    p_cfg_set.add_argument("--json", action="store_true")

    # ── craft ─────────────────────────────────────────────────────────────────
    p_craft = sub.add_parser(
        "craft",
        help="Capture, list, and export individual craft knowledge (.irp/craft.jsonl)",
    )
    craft_sub = p_craft.add_subparsers(dest="craft_action", required=True)

    p_craft_add = craft_sub.add_parser("add", help="Add a craft knowledge entry")
    p_craft_add.add_argument(
        "--category", type=str, default=None,
        choices=["preference", "configuration", "gotcha", "way-of-working"],
        metavar="CATEGORY",
        help="Category: preference, configuration, gotcha, way-of-working",
    )
    p_craft_add.add_argument(
        "--what", type=str, default=None,
        help="The craft knowledge (non-interactive when combined with --category)",
    )
    p_craft_add.add_argument(
        "--context", type=str, default=None,
        help="Optional context (project, tool, situation)",
    )
    p_craft_add.add_argument("--json", action="store_true")

    p_craft_list = craft_sub.add_parser("list", help="List craft entries")
    p_craft_list.add_argument(
        "--category", type=str, default=None,
        choices=["preference", "configuration", "gotcha", "way-of-working"],
        metavar="CATEGORY",
        help="Filter by category",
    )
    p_craft_list.add_argument("--json", action="store_true")

    p_craft_export = craft_sub.add_parser("export", help="Export craft knowledge to CRAFT.md")
    p_craft_export.add_argument(
        "--category", type=str, default=None,
        choices=["preference", "configuration", "gotcha", "way-of-working"],
        metavar="CATEGORY",
        help="Export only this category (writes CRAFT-<category>.md by default)",
    )
    p_craft_export.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: CRAFT.md in project root)",
    )
    p_craft_export.add_argument("--force", action="store_true", help="Overwrite existing file")
    p_craft_export.add_argument(
        "--writable", action="store_true",
        help="Leave exported file writable (default: chmod 444 read-only)",
    )
    p_craft_export.add_argument("--json", action="store_true")

    # ── find ─────────────────────────────────────────────────────────────────
    p = sub.add_parser("find", help="Search ledger and craft entries by keyword or regex")
    p.add_argument("query", type=str, help="Search term (plain text or regex)")
    p.add_argument("--ledger-only", action="store_true", dest="ledger_only",
                   help="Search only the decision ledger")
    p.add_argument("--craft-only", action="store_true", dest="craft_only",
                   help="Search only the craft entries")
    p.add_argument("--graph", action="store_true",
                   help="Open matched entries as an interactive graph in the browser")
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

    # ── defer ─────────────────────────────────────────────────────────────────
    p_defer = sub.add_parser(
        "defer",
        help="Resolve a WARN/BLOCK critique and capture the human decision",
    )
    p_defer.add_argument(
        "question",
        nargs="?",
        type=str,
        default=None,
        help="The defer question to resolve (omit to read critique JSON from stdin)",
    )
    p_defer.add_argument("--json", action="store_true")

    # ── guard ─────────────────────────────────────────────────────────────────
    p_guard = sub.add_parser(
        "guard",
        help="Pre-commit hook — check staged changes against IRP decisions",
    )
    guard_sub = p_guard.add_subparsers(dest="guard_action", required=True)

    p_guard_install = guard_sub.add_parser(
        "install",
        help="Install IRP guard as a git pre-commit hook",
    )
    p_guard_install.add_argument("--force", action="store_true", help="Overwrite existing hook")
    p_guard_install.add_argument("--json", action="store_true")

    p_guard_run = guard_sub.add_parser(
        "run",
        help="Check staged diff against active decisions (called by hook or manually)",
    )
    p_guard_run.add_argument("--json", action="store_true")

    p_guard_status = guard_sub.add_parser(
        "status",
        help="Show whether the IRP guard hook is installed",
    )
    p_guard_status.add_argument("--json", action="store_true")

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

    # ── docs ─────────────────────────────────────────────────────────────────
    p_docs = sub.add_parser(
        "docs",
        help="Pull/push iCloud strategic docs to/from /tmp staging area",
    )
    docs_sub = p_docs.add_subparsers(dest="docs_action", required=True)

    p_docs_pull = docs_sub.add_parser("pull", help="Copy iCloud docs → /tmp")
    p_docs_pull.add_argument("--file", type=str, default=None,
                             help="Specific filename (default: all known docs)")
    p_docs_pull.add_argument("--json", action="store_true")

    p_docs_push = docs_sub.add_parser("push", help="Copy /tmp docs → iCloud")
    p_docs_push.add_argument("--file", type=str, default=None,
                             help="Specific filename (default: all known docs)")
    p_docs_push.add_argument("--json", action="store_true")

    p_docs_list = docs_sub.add_parser("list", help="List .md files in iCloud docs folder")
    p_docs_list.add_argument("--json", action="store_true")

    # ── export ───────────────────────────────────────────────────────────────
    p_export = sub.add_parser(
        "export",
        help="Export decision lineage to portable formats for downstream agents",
    )
    export_sub = p_export.add_subparsers(dest="export_action", required=True)

    p_export_ctx = export_sub.add_parser(
        "context",
        help="Export decision-derived working context (e.g. AGENTS.md)",
    )
    p_export_ctx.add_argument(
        "--target",
        type=str,
        required=True,
        choices=["agents.md", "decisions.md"],
        help="Target format: agents.md (agent constraints) or decisions.md (human log)",
    )
    p_export_ctx.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: AGENTS.md in project root)",
    )
    p_export_ctx.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    p_export_ctx.add_argument(
        "--writable",
        action="store_true",
        help="Leave the exported file writable (default: chmod 444 read-only)",
    )
    p_export_ctx.add_argument("--json", action="store_true")

    p_export_decisions = export_sub.add_parser(
        "decisions",
        help="Export DECISIONS.md — human-readable decision log (newest-first)",
    )
    p_export_decisions.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: DECISIONS.md in project root)",
    )
    p_export_decisions.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    p_export_decisions.add_argument(
        "--writable",
        action="store_true",
        help="Leave the exported file writable (default: chmod 444 read-only)",
    )
    p_export_decisions.add_argument(
        "--demo",
        action="store_true",
        help="Generate from built-in sample data — does not touch your ledger",
    )
    p_export_decisions.add_argument("--json", action="store_true")

    p_export_graph = export_sub.add_parser(
        "graph",
        help="Export decision graph as a self-contained interactive HTML file",
    )
    p_export_graph.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: GRAPH.html in project root)",
    )
    p_export_graph.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    p_export_graph.add_argument(
        "--from",
        dest="from_date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Show decisions from this date (inclusive). Nodes outside range are dimmed.",
    )
    p_export_graph.add_argument(
        "--to",
        dest="to_date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Show decisions up to this date (inclusive). Nodes outside range are dimmed.",
    )
    p_export_graph.add_argument(
        "--project",
        dest="project",
        type=str,
        default=None,
        metavar="TAG",
        help="Scope to decisions tagged with this value (case-insensitive).",
    )
    p_export_graph.add_argument(
        "--demo",
        action="store_true",
        help="Generate from built-in sample data — does not touch your ledger",
    )
    p_export_graph.add_argument("--json", action="store_true")

    p_export_evidence = export_sub.add_parser(
        "evidence",
        help="Export compliance evidence package from decision ledger (EU AI Act, SOC 2, GDPR, ISO 42001)",
    )
    p_export_evidence.add_argument(
        "--framework",
        type=str,
        default="euaiact",
        choices=["euaiact", "soc2", "gdpr", "iso42001", "custom"],
        metavar="FRAMEWORK",
        help=(
            "Compliance framework to map decisions against. "
            "Built-in: euaiact (default), soc2, gdpr, iso42001. "
            "Custom: pass 'custom' with --config path/to/framework.json"
        ),
    )
    p_export_evidence.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to custom framework JSON (required when --framework custom)",
    )
    p_export_evidence.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: EVIDENCE-<framework>.md in project root)",
    )
    p_export_evidence.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    p_export_evidence.add_argument(
        "--demo",
        action="store_true",
        help="Generate evidence package from built-in sample data (Nordic lending platform) — does not touch your ledger",
    )
    p_export_evidence.add_argument("--json", action="store_true")

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
            "find":      run_find,
            "check":     run_check,
            "config":    run_config,
            "craft":     run_craft,
            "defer":     run_defer,
            "demo":      run_demo,
            "bootstrap": run_bootstrap,
            "docs":      run_docs,
            "resolve":   run_resolve,
            "export":    run_export,
            "guard":     run_guard,
        }
        result = dispatch[args.command](project_root=project_root, irp_dir=irp_dir, args=args)
        print_result(result, getattr(args, "json", False))
        # exit 10 = conflict detected (warn-only signal for hook consumers)
        # exit 0  = clean
        # exit 1  = reserved for errors (handled in except block below)
        return 10 if result.get("status") == "conflict" else 0

    except KeyboardInterrupt:
        print("IRP cancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"IRP error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
