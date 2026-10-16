#!/usr/bin/env python3
"""IRP dispatcher — single entry point for irp/core."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent      # .../irp/core
REPO_ROOT = CURRENT_DIR.parent.parent              # the dir that contains the `irp` package

# Put REPO_ROOT ahead of irp/core so `import irp` resolves to the *package*, not
# this script file. Run directly as `python irp/core/irp.py`, sys.path[0] is
# irp/core, so `irp` would otherwise bind to irp/core/irp.py and shadow the real
# package: absolute imports like `from irp.core.store import ...` (used by capture
# and the MCP/API servers) then re-enter this script and raise a circular
# ImportError, and `irp.integrity` (needed by `export evidence --attest`) fails to
# import. Keeping irp/core on the path preserves the dispatcher-relative imports
# below (`from store import`, `from commands... import`).
for _p in (str(CURRENT_DIR), str(REPO_ROOT)):
    if _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, str(CURRENT_DIR))
sys.path.insert(0, str(REPO_ROOT))

import irp  # noqa: E402,F401  # cache the real package before the command imports below

from store import ProjectNotFound, find_named_project, find_project
from commands.attest import run_attest
from commands.capture import run_capture
from commands.check import run_check
from commands.config import run_config
from commands.craft import run_craft
from commands.defer import run_defer
from commands.demo import run_demo
from commands.doctor import ledger_tracked_by_git, run_doctor
from commands.bootstrap import run_bootstrap
from commands.export import run_export
from commands.find import run_find
from commands.docs import run_docs
from commands.resolve import run_resolve
from commands.gate import run_gate, _exit_code as _gate_exit_code
from commands.watch import run_watch
from commands.mod import run_mod
from commands.guard import run_guard
from commands.init import run_init
from commands.inherit import run_inherit
from commands.integrity import run_integrity
from commands.why import run_why


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irp", description="IRP project-local dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── init ─────────────────────────────────────────────────────────────────
    p = sub.add_parser("init", help="Start an IRP project in a folder (the only command that creates .irp/)")
    p.add_argument("path", nargs="?", default=None, metavar="PATH",
                   help="Folder to start the project in (default: the current folder)")
    p.add_argument("--nested", action="store_true",
                   help="Start a separate ledger inside another project or a linked worktree")

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

    # ── gate ──────────────────────────────────────────────────────────────────
    p_gate = sub.add_parser(
        "gate",
        help="Runtime gate — evaluate an action against active decisions (machine-readable JSON)",
    )
    p_gate.add_argument("query", type=str, help="Action or proposal to evaluate")
    p_gate.add_argument("--tag", type=str, default=None, help="Limit check to decisions with this tag")
    p_gate.add_argument("--scope", type=str, default=None, help="Limit check to decisions mentioning this scope")
    p_gate.add_argument("--strict", action="store_true",
                        help="Treat warn as block (exit 20 instead of 10)")

    # ── mod ───────────────────────────────────────────────────────────────────
    p_mod = sub.add_parser(
        "mod",
        help="Living mod — supersede or retire decisions, view recent changes",
    )
    mod_sub = p_mod.add_subparsers(dest="mod_action", required=True)

    p_mod_sup = mod_sub.add_parser("supersede", help="Replace a decision with a new one")
    p_mod_sup.add_argument("target_id", type=str, help="IRP ID to supersede (e.g. IRP-2026-04-01-001)")
    p_mod_sup.add_argument("--decision", type=str, required=True, help="New decision text")
    p_mod_sup.add_argument("--reason", type=str, required=True, help="Why this supersedes the old one")
    p_mod_sup.add_argument("--confidence", type=str, default="high",
                           choices=["low", "medium", "high"], help="Confidence level (default: high)")
    p_mod_sup.add_argument("--shared-id-ok", dest="shared_id_ok", action="store_true",
                           help="Go ahead although the id is shared by a bootstrap guess and a confirmed decision "
                                "(this changes the confirmed decision)")
    p_mod_sup.add_argument("--json", action="store_true")

    p_mod_ret = mod_sub.add_parser("retire", help="Retire a decision (no replacement)")
    p_mod_ret.add_argument("target_id", type=str, help="IRP ID to retire")
    p_mod_ret.add_argument("--reason", type=str, required=True, help="Why this decision is retired")
    p_mod_ret.add_argument("--shared-id-ok", dest="shared_id_ok", action="store_true",
                           help="Go ahead although the id is shared by a bootstrap guess and a confirmed decision "
                                "(this changes the confirmed decision)")
    p_mod_ret.add_argument("--json", action="store_true")

    p_mod_list = mod_sub.add_parser("list", help="Show recent mod events (supersessions, retirements)")
    p_mod_list.add_argument("--json", action="store_true")

    # ── watch ─────────────────────────────────────────────────────────────────
    p_watch = sub.add_parser(
        "watch",
        help="Streaming gate — read actions line-by-line from stdin or file, emit one JSON verdict per line",
    )
    p_watch.add_argument("--input", type=str, default=None, metavar="FILE",
                         help="Read actions from FILE instead of stdin")
    p_watch.add_argument("--tag", type=str, default=None,
                         help="Limit evaluation to decisions with this tag")
    p_watch.add_argument("--scope", type=str, default=None,
                         help="Limit evaluation to decisions mentioning this scope")
    p_watch.add_argument("--strict", action="store_true",
                         help="Treat warn as block (exit 20 instead of 10)")

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
    p_craft_add.add_argument(
        "--irp", type=str, nargs="+", default=None,
        metavar="IRP_ID",
        help="Source IRP decision ID(s) this craft entry traces back to (e.g. IRP-2026-05-09-004)",
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
        help="Output file path, relative to the current folder (default: CRAFT.md in the project folder)",
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
    p_guard_install.add_argument(
        "--shared", action="store_true",
        help="Write a hook that records no paths, for a hooks folder outside .git (core.hooksPath)",
    )
    p_guard_install.add_argument("--json", action="store_true")

    p_guard_run = guard_sub.add_parser(
        "run",
        help="Check staged diff against active decisions (called by hook or manually)",
    )
    p_guard_run.add_argument(
        "--hook-project", dest="hook_project", metavar="REL", default=None,
        help="The project folder relative to the repo's top folder (the hook passes this)",
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
        help=(
            "Look through git history and docs for decisions. Finds are recorded as "
            "unconfirmed reconstructions in .irp/reconstructions.jsonl, not in the ledger; "
            "--accept REC-... turns one into a real decision"
        ),
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
        help="Preview candidates and provisional REC ids; adds nothing to the ledger or reconstructions.jsonl",
    )
    p_boot.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of reconstructions to record (default: 50)",
    )
    p_boot.add_argument(
        "--accept",
        action="append",
        nargs="+",
        metavar="REC-ID",
        default=None,
        help=(
            "Accept one or more reconstructions as real decisions (your confirmation): "
            "appends a normal decision to the ledger and marks the REC line accepted. "
            "Repeatable, or list several ids. Skips the scan."
        ),
    )
    p_boot.add_argument(
        "--write-report",
        action="store_true",
        dest="write_report",
        help="Write a bootstrap report to .irp/bootstrap_reports/<timestamp>.md",
    )
    p_boot.add_argument("--json", action="store_true")

    # ── doctor ───────────────────────────────────────────────────────────────
    p_doctor = sub.add_parser("doctor", help="Check installation health and environment")
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="Rebuild current.json from the ledger if it still lists unconfirmed bootstrap guesses",
    )
    p_doctor.add_argument("--json", action="store_true")

    # ── docs ─────────────────────────────────────────────────────────────────
    p_docs = sub.add_parser(
        "docs",
        help="Pull/push your docs folder (IRP_DOCS_DIR) to/from /tmp staging area",
    )
    docs_sub = p_docs.add_subparsers(dest="docs_action", required=True)

    p_docs_pull = docs_sub.add_parser("pull", help="Copy docs from your docs folder → /tmp")
    p_docs_pull.add_argument("--file", type=str, default=None,
                             help="Specific filename (default: all known docs)")
    p_docs_pull.add_argument("--json", action="store_true")

    p_docs_push = docs_sub.add_parser("push", help="Copy /tmp docs → your docs folder")
    p_docs_push.add_argument("--file", type=str, default=None,
                             help="Specific filename (default: all known docs)")
    p_docs_push.add_argument("--json", action="store_true")

    p_docs_list = docs_sub.add_parser("list", help="List .md files in your docs folder")
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
        choices=["agents.md", "claude.md", "cursor", "copilot", "decisions.md"],
        help="Target format: agents.md, claude.md, cursor (.cursor/rules) or copilot "
             "(.github/copilot-instructions.md) for agents; decisions.md for the human log",
    )
    p_export_ctx.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path, relative to the current folder (default: AGENTS.md in the project folder)",
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
        help="Output file path, relative to the current folder (default: DECISIONS.md in the project folder)",
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
        help="Output file path, relative to the current folder (default: GRAPH.html in the project folder)",
    )
    p_export_graph.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    p_export_graph.add_argument(
        "--reconstruction",
        action="store_true",
        help=("Mark the export as a reconstruction: assembled from outside sources, "
              "not captured through IRP and carrying no attestation. Adds a standing "
              "notice and removes the attested-record language."),
    )
    p_export_graph.add_argument(
        "--title",
        type=str,
        default=None,
        help="Page title and heading (default: IRP Decision Graph)",
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
        "--tag",
        dest="tag",
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
    p_export_graph.add_argument(
        "--view",
        dest="view",
        type=str,
        default="structure",
        choices=["structure", "foundations", "lineage", "impact"],
        help=(
            "Provenance lens (default: structure, the plain reference graph). "
            "foundations = what the reasoning rests on; lineage = why this decision "
            "exists (needs --seed); impact = what depends on it (needs --seed)."
        ),
    )
    p_export_graph.add_argument(
        "--seed",
        dest="seed",
        type=str,
        default=None,
        metavar="IRP-ID",
        help="Decision to seed the lineage or impact lens on.",
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
        help="Output file path, relative to the current folder (default: EVIDENCE-<framework>.md in the project folder)",
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
    p_export_evidence.add_argument(
        "--attest",
        action="store_true",
        help=(
            "Anchor the package to an external RFC 3161 timestamp authority: snapshot the "
            "ledger, timestamp it, and add an 'External timestamp' block proving the decisions "
            "existed and were unaltered as of a date you cannot fake. Fail-closed if the TSA is "
            "unreachable. Requires: pip install \"irp-capture[integrity]\". Cannot combine with --demo."
        ),
    )
    p_export_evidence.add_argument(
        "--tsa-url",
        type=str,
        default=None,
        dest="tsa_url",
        metavar="URL",
        help="RFC 3161 timestamp authority URL for --attest (default: freetsa.org).",
    )
    p_export_evidence.add_argument("--json", action="store_true")

    # ── integrity ──────────────────────────────────────────────────────────────
    p_int = sub.add_parser(
        "integrity",
        help="Deterministic integrity snapshots and offline verification",
    )
    int_sub = p_int.add_subparsers(dest="integrity_action", required=True)

    p_int_snap = int_sub.add_parser(
        "snapshot",
        help="Create a deterministic, offline integrity snapshot of the ledger",
    )
    p_int_snap.add_argument(
        "--allow-malformed",
        action="store_true",
        dest="allow_malformed",
        help="Snapshot even if the ledger has malformed lines (records the count)",
    )
    p_int_snap.add_argument("--json", action="store_true")

    p_int_verify = int_sub.add_parser(
        "verify",
        help="Verify a ledger against a snapshot (offline). Exit 10 on FAIL.",
    )
    p_int_verify.add_argument("snapshot", type=str, help="Path to the snapshot JSON file")
    p_int_verify.add_argument(
        "--ledger",
        type=str,
        default=None,
        help="Ledger path to verify (default: .irp/ledger.jsonl)",
    )
    p_int_verify.add_argument("--json", action="store_true")

    # ── attest ─────────────────────────────────────────────────────────────────
    p_att = sub.add_parser(
        "attest",
        help="Anchor a snapshot to an external RFC 3161 timestamp authority, and verify",
    )
    att_sub = p_att.add_subparsers(dest="attest_action", required=True)

    p_att_create = att_sub.add_parser(
        "create",
        help="Timestamp a snapshot digest at a TSA (network) and store a detached receipt",
    )
    p_att_create.add_argument("snapshot", type=str, help="Path to the snapshot JSON file")
    p_att_create.add_argument(
        "--tsa-url",
        type=str,
        default=None,
        dest="tsa_url",
        help="RFC 3161 TSA URL (default: freetsa.org — a demo provider, not a configured trust root)",
    )
    p_att_create.add_argument("--json", action="store_true")

    p_att_verify = att_sub.add_parser(
        "verify",
        help="Verify a snapshot's external timestamp receipt (offline)",
    )
    p_att_verify.add_argument("snapshot", type=str, help="Path to the snapshot JSON file")
    p_att_verify.add_argument(
        "--receipt",
        type=str,
        default=None,
        help="Path to the .tsr token (default: auto-locate by snapshot id)",
    )
    p_att_verify.add_argument("--json", action="store_true")

    _add_project_option(parser)
    return parser


def _subparsers(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _add_project_option(parser: argparse.ArgumentParser) -> None:
    """Give this parser and every subcommand below it --project PATH.

    It works before or after the command (`irp --project P why`, `irp why
    --project P`). The default is SUPPRESS, so a subcommand's parser never
    overwrites a value given before the command.
    """
    parser.add_argument(
        "--project", dest="project_path", metavar="PATH", default=argparse.SUPPRESS,
        help="Use the IRP project in PATH instead of finding it from the current folder",
    )
    for child in _subparsers(parser).values():
        _add_project_option(child)


def print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result.get("text"):
        print(result["text"])
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


# ── what each command may do ──────────────────────────────────────────────────
#
# read    reads the project, never writes. No project: says so, exit 1
#         (`integrity verify --ledger` and `attest verify --receipt` need none)
# hook    `guard run`, called by the commit hook. No project: one line, exit 0
# output  writes only the file asked for. No project: says so, exit 1
# write   appends to an existing project. No project: nothing written, exit 1
# start   `init`, the only command that creates .irp/
# report  runs with or without a project, exit 0
# none    needs no project
#
# Every command and sub-action must be listed here; tests/test_no_write.py
# fails on one that isn't and runs every read and output command in an empty
# folder to check it creates nothing.
COMMAND_CLASSES: dict[str, str] = {
    "inherit": "read", "why": "read", "find": "read", "check": "read", "resolve": "read",
    "gate": "read", "watch": "read", "mod list": "read", "config get": "read",
    "craft list": "read", "integrity verify": "read", "attest verify": "read",
    "guard run": "hook",
    "export context": "output", "export decisions": "output", "export graph": "output",
    "export evidence": "output", "craft export": "output",
    "capture": "write", "mod supersede": "write", "mod retire": "write", "defer": "write",
    "craft add": "write", "demo generate": "write", "bootstrap": "write", "config set": "write",
    "integrity snapshot": "write", "attest create": "write", "guard install": "write",
    "init": "start",
    "doctor": "report", "guard status": "report",
    "docs pull": "none", "docs push": "none", "docs list": "none",
}

_DISPATCH = {
    "inherit":   run_inherit,
    "capture":   run_capture,
    "why":       run_why,
    "find":      run_find,
    "check":     run_check,
    "gate":      run_gate,
    "watch":     run_watch,
    "mod":       run_mod,
    "config":    run_config,
    "craft":     run_craft,
    "defer":     run_defer,
    "demo":      run_demo,
    "bootstrap": run_bootstrap,
    "doctor":    run_doctor,
    "docs":      run_docs,
    "resolve":   run_resolve,
    "export":    run_export,
    "guard":     run_guard,
    "integrity": run_integrity,
    "attest":    run_attest,
}

GUARD_NO_PROJECT_TEXT = "IRP guard: no IRP project in this repo, so this commit wasn't checked."

# Commands whose own text names the project they used. Their Project line is
# printed only when that text doesn't.
_SAYS_ITS_PROJECT = {"export context", "export decisions", "export graph", "export evidence",
                     "craft export", "bootstrap"}


def _command_key(args: argparse.Namespace) -> str:
    action = getattr(args, f"{args.command}_action", None)
    return f"{args.command} {action}" if action else args.command


# The flag that names the file each verify command checks. Given that file,
# the command reads only what's named on its command line, so it can check a
# snapshot someone sent you with no IRP project set up.
_VERIFY_FILE_FLAGS = {"integrity verify": "ledger", "attest verify": "receipt"}


def _verifies_named_files(key: str, args: argparse.Namespace) -> bool:
    flag = _VERIFY_FILE_FLAGS.get(key)
    return flag is not None and bool(getattr(args, flag, None))


def _no_project(key: str, cls: str, missing: ProjectNotFound, as_json: bool) -> int:
    """Say there's no project and return the exit code. Creates nothing."""
    if cls == "hook":
        print_result({"command": "guard.run", "status": "no_project", "text": GUARD_NO_PROJECT_TEXT}, as_json)
        return 0
    if key == "gate":
        payload = {"verdict": "no_project", "exit_code": 1}
        payload.update({k: v for k, v in missing.json_payload().items() if k != "status"})
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    if key == "watch":
        # watch always answers in JSON, one line per action, so it says there's
        # no project the way every --json command does, on one line.
        print(json.dumps(missing.json_payload(), ensure_ascii=False))
        return 1
    if as_json:
        print(json.dumps(missing.json_payload(), indent=2, ensure_ascii=False))
        return 1
    if key == "guard install" and not missing.named_by:
        text = (f"No IRP project here, so there's nothing for the hook to check. "
                f"Start one with {missing.hint}, then install the hook.")
    elif cls == "write":
        text = missing.write_message(key)
    else:
        text = missing.read_message(key)
    if key == "export graph" and missing.named_by == "--project" and not missing.searched_from.exists():
        # `export graph --project TAG` was the tag filter before 0.10.
        text += "\nTo filter by tag, use --tag NAME."
    print(text, file=sys.stderr)
    return 1


def _project_line(project) -> str | None:
    """`Project: /a`, only when the project isn't the folder you're in or
    IRP_PROJECT_ROOT chose it."""
    if project.source == "IRP_PROJECT_ROOT":
        return f"Project: {project.root} (from IRP_PROJECT_ROOT)"
    try:
        here = Path.cwd().resolve()
    except OSError:
        here = None
    return f"Project: {project.root}" if project.root != here else None


def _outer_notes(project) -> list[str]:
    notes = []
    further_up = [p for p in project.outer_projects if p not in project.main_checkout_projects]
    if further_up:
        notes.append(f"Note: there's another IRP project further up, at {further_up[0]}. "
                     f"This command uses the nearer one, {project.root}.")
    # Only worth a note when this worktree's own ledger goes with the worktree.
    # A ledger git tracks is kept, so then there's nothing to say.
    if project.main_checkout_projects and ledger_tracked_by_git(project.root) is not True:
        notes.append(f"Note: there's another IRP project in the main checkout, at "
                     f"{project.main_checkout_projects[0]}. This command uses the one in this worktree, "
                     f"{project.root}.")
    return notes


def _announce(key: str, cls: str, project, as_json: bool) -> None:
    """The Project line and notes, on stderr. Nothing appears when the
    project is the folder you're in."""
    # gate and watch always print JSON, so they get the notes but no Project
    # line. guard run and demo say what they used.
    machine = key in ("gate", "watch")
    quiet = as_json or machine or key == "guard run" or key.startswith("demo")
    if not quiet and key not in _SAYS_ITS_PROJECT:
        line = _project_line(project)
        if line:
            print(line, file=sys.stderr)
    if not quiet or machine:
        for note in _outer_notes(project):
            print(note, file=sys.stderr)
    if cls == "write":
        for folder in project.skipped_strays:
            print(f"Note: skipped an empty .irp folder at {folder} (an older IRP version left it). "
                  f"Writing to the project at {project.root}.", file=sys.stderr)
        if (key != "guard install" and project.worktree_of is not None
                and ledger_tracked_by_git(project.root) is not True):
            note = (f"Note: this ledger is inside a linked worktree of {project.worktree_of} and goes "
                    "when the worktree is removed.")
            if project.main_checkout_projects:
                note += (f" To record in the main checkout instead: "
                         f"irp --project {project.main_checkout_projects[0]} {key}")
            print(note, file=sys.stderr)


def _output_base(project) -> Path:
    """Where output files go by default: the project folder or, for a project
    found in the main checkout from a linked worktree, the matching folder in
    the worktree, so nothing lands in another branch's checkout."""
    if project.worktree_root is None:
        return project.root
    if project.worktree_root.is_dir():
        return project.worktree_root
    return Path.cwd()


def _here() -> Path:
    """The folder you're in, or / when it has been removed."""
    try:
        return Path.cwd()
    except OSError:
        return Path("/")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    key = _command_key(args)
    cls = COMMAND_CLASSES.get(key, "read")
    as_json = bool(getattr(args, "json", False))

    try:
        if cls == "start":
            result = run_init(args)
            print(result["text"], file=sys.stdout if result["exit_code"] == 0 else sys.stderr)
            return result["exit_code"]

        project = None
        missing = None
        if cls == "none":
            project_root, irp_dir = Path.cwd(), None
        else:
            hook_project = getattr(args, "hook_project", None)
            try:
                if key == "guard run" and hook_project is not None:
                    # The hook names its project: that path (or the same path
                    # in the main checkout) and nothing else.
                    project = find_named_project(Path.cwd() / hook_project, "--hook-project")
                else:
                    project = find_project(project=getattr(args, "project_path", None))
            except ProjectNotFound as exc:
                missing = exc
            except OSError:
                # A folder on the way can't be read, or the one you're in is
                # gone. A verify of files you named doesn't need a project.
                if not _verifies_named_files(key, args):
                    raise
            if project is not None:
                project_root, irp_dir = project.root, project.irp_dir
                _announce(key, cls, project, as_json)
            elif (cls == "report" or (key == "export graph" and getattr(args, "demo", False))
                  or _verifies_named_files(key, args)):
                # Reports run anyway and say there's no project; the demo
                # graph and a verify of files you named need none. None of
                # them creates .irp/.
                project_root, irp_dir = _here(), None
            else:
                return _no_project(key, cls, missing, as_json)

        # Commands that care how the project was found or where they were run
        # from (doctor, guard, watch, output commands) read these.
        args.irp_project = project
        args.irp_project_missing = missing
        args.irp_cwd = _here() if _verifies_named_files(key, args) else Path.cwd()
        args.irp_argv0 = sys.argv[0] if sys.argv else ""
        args.irp_output_base = _output_base(project) if project is not None else project_root

        result = _DISPATCH[args.command](project_root=project_root, irp_dir=irp_dir, args=args)

        if project is not None and project.outer_projects and isinstance(result, dict) and (
                as_json or args.command == "gate"):
            result["outer_projects"] = [str(p) for p in project.outer_projects]

        # gate uses its own 0/10/20 exit code semantics, read from the payload
        if args.command == "gate":
            print(json.dumps(result, ensure_ascii=False))
            return result.get("exit_code", 0)

        # watch emits its own lines; return worst exit code from payload
        if args.command == "watch":
            exit_code = result.get("_watch_exit", 0)
            if result.get("error"):
                print(f"IRP watch error: {result['error']}", file=sys.stderr)
            return exit_code

        if key in _SAYS_ITS_PROJECT and not as_json and project is not None and isinstance(result, dict):
            line = _project_line(project)
            text = str(result.get("text") or "")
            if line and (project.source == "IRP_PROJECT_ROOT" or f"Project: {project.root}\n" not in text + "\n"):
                print(line, file=sys.stderr)
        # `guard install`: 0 when the hook is in place (written or already
        # there), 1 for every refusal and error.
        if key == "guard install":
            installed = result.get("status") in ("ok", "exists")
            if as_json or installed:
                print_result(result, as_json)
            else:
                print(result.get("text", ""), file=sys.stderr)
            return 0 if installed else 1
        print_result(result, as_json)
        # `bootstrap --accept` that accepted nothing because of problems (unknown
        # or already-accepted id, ...) exits 1, like `irp mod` does for a bad id.
        if (
            args.command == "bootstrap"
            and result.get("problems")
            and not (result.get("accepted") or result.get("would_accept"))
        ):
            return 1
        # `docs` with no usable docs folder (IRP_DOCS_DIR unset or missing), or
        # nothing copied, exits 1 so scripts don't carry on with stale files.
        if args.command == "docs" and result.get("status") == "error":
            return 1
        # exit 10 = conflict detected (warn-only signal for hook consumers)
        # exit 0  = clean
        # exit 1  = reserved for errors (handled in except block below)
        has_conflict = (
            result.get("status") == "conflict"
            or result.get("verdict") in ("warn", "block")
        )
        return 10 if has_conflict else 0

    except KeyboardInterrupt:
        print("IRP cancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"IRP error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
