"""irp guard — pre-commit hook that checks staged changes against IRP decisions.

Subcommands:
  irp guard install  — write .git/hooks/pre-commit
  irp guard run      — check staged diff against active decisions (called by hook)
  irp guard status   — show whether the IRP hook is installed

Severity model (v0):
  clear     — no token overlap with any active decision
  warning   — 1–2 tokens overlap (informational, does not block)
  conflict  — 3+ tokens overlap → dispatcher returns exit 10

Hook blocking:
  By default the hook warns but does not abort commits.
  Set IRP_GUARD_BLOCK=1 in the environment to abort on conflict.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from irp.core.store import read_current

# ── hook template ─────────────────────────────────────────────────────────────

_HOOK_MARKER = "# irp-guard-v0"

_HOOK_SCRIPT = """\
#!/bin/sh
{marker}
# IRP Guard — checks staged changes against the decision ledger.
# Warns on conflict. Does not block commits by default.
# To abort commits on conflict, set IRP_GUARD_BLOCK=1:
#   IRP_GUARD_BLOCK=1 git commit -m "..."
irp guard run
GUARD_EXIT=$?
if [ "$GUARD_EXIT" -eq 10 ] && [ "${{IRP_GUARD_BLOCK:-0}}" = "1" ]; then
  echo ""
  echo "IRP guard: commit aborted (IRP_GUARD_BLOCK=1). Run \`irp why\` to review the conflict."
  exit 1
fi
exit 0
""".format(marker=_HOOK_MARKER)

# ── token helpers (mirrors check.py — kept local to avoid coupling) ───────────

_STOPWORDS = {
    "a", "an", "the", "in", "to", "of", "and", "or", "for", "at", "by",
    "on", "with", "as", "into", "from", "up", "out", "about", "per",
    "it", "its", "this", "that", "we", "our", "they", "their", "i", "my",
    "add", "adding", "use", "using", "used", "implement", "implementing",
    "create", "make", "get", "set", "run", "update", "build", "introduce",
    "support", "allow", "enable", "provide", "include", "apply",
    "will", "not", "no", "be", "is", "are", "was", "were", "have", "has",
    "had", "can", "do", "so", "but", "if", "only", "also", "all", "new",
    "any", "each", "both", "already", "now", "just", "more", "better",
    "well", "good", "clear", "simple", "local", "same",
    "state", "thread", "project", "system", "single", "scale", "version",
    "v0", "approach", "management", "unnecessary", "complexity",
    # diff noise
    "def", "return", "import", "from", "class", "self", "true", "false",
    "none", "str", "int", "dict", "list", "path", "type", "args",
}

def _tokens(text: str) -> set[str]:
    words = re.split(r"[\s\W]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}

# ── staged diff extraction ─────────────────────────────────────────────────────

def _get_staged_text(project_root: Path) -> str:
    """Return added lines from the current staged diff as a single string."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--unified=0"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=10,
        )
        lines = []
        for line in result.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:].strip())
        return " ".join(lines)
    except Exception:
        return ""

def _get_commit_message(project_root: Path) -> str:
    """Read COMMIT_EDITMSG if present (populated by git before hook runs)."""
    msg_path = project_root / ".git" / "COMMIT_EDITMSG"
    if msg_path.exists():
        try:
            return msg_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""

# ── severity ──────────────────────────────────────────────────────────────────

def _severity(overlap_count: int) -> str:
    if overlap_count == 0:
        return "clear"
    if overlap_count <= 2:
        return "warning"
    return "conflict"

# ── subcommand: install ───────────────────────────────────────────────────────

def _run_guard_install(project_root: Path, irp_dir: Path, args) -> dict:
    force = bool(getattr(args, "force", False))

    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return {
            "command": "guard.install",
            "status": "error",
            "text": (
                "No .git directory found at:\n"
                f"  {project_root}\n\n"
                "Run `irp guard install` from the root of a git repository."
            ),
        }

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-commit"

    if hook_path.exists() and not force:
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if _HOOK_MARKER in content:
            return {
                "command": "guard.install",
                "status": "exists",
                "output_path": str(hook_path),
                "text": (
                    f"IRP guard hook already installed at:\n  {hook_path}\n\n"
                    "Re-run with --force to overwrite."
                ),
            }
        return {
            "command": "guard.install",
            "status": "conflict",
            "output_path": str(hook_path),
            "text": (
                f"A pre-commit hook already exists at:\n  {hook_path}\n\n"
                "It was not created by IRP. Options:\n"
                "  1. Add `irp guard run` to your existing hook manually.\n"
                "  2. Back up the hook, then re-run `irp guard install --force`."
            ),
        }

    hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)

    action = "Updated" if force else "Installed"
    return {
        "command": "guard.install",
        "status": "ok",
        "output_path": str(hook_path),
        "text": (
            f"{action} IRP guard hook at:\n  {hook_path}\n\n"
            "The hook warns on conflict. It does not block commits by default.\n"
            "To abort commits on conflict:\n"
            "  IRP_GUARD_BLOCK=1 git commit -m \"your message\"\n\n"
            "Run `irp guard status` to verify."
        ),
    }

# ── subcommand: run ───────────────────────────────────────────────────────────

def _run_guard_run(project_root: Path, irp_dir: Path, args) -> dict:
    # Gather text to check: staged diff + commit message
    staged_text = _get_staged_text(project_root)
    commit_msg = _get_commit_message(project_root)
    combined = f"{staged_text} {commit_msg}".strip()

    if not combined:
        return {
            "command": "guard.run",
            "status": "clear",
            "text": "IRP guard: nothing to check (no staged changes).",
        }

    current = read_current(irp_dir)
    active = current.get("active", [])

    if not active:
        return {
            "command": "guard.run",
            "status": "clear",
            "checked": 0,
            "text": "IRP guard: clear — no active decisions in ledger.",
        }

    proposal_tokens = _tokens(combined)

    # Score all active decisions; pick the one with most overlap.
    best_match: dict[str, Any] | None = None
    best_overlap: list[str] = []

    for entry in active:
        decision_text = (entry.get("what") or "") + " " + (entry.get("why") or "")
        decision_tokens = _tokens(decision_text)
        overlap = sorted(proposal_tokens & decision_tokens)
        if len(overlap) > len(best_overlap):
            best_match = entry
            best_overlap = overlap

    severity = _severity(len(best_overlap))

    if severity == "clear":
        return {
            "command": "guard.run",
            "status": "clear",
            "checked": len(active),
            "text": f"IRP guard: clear — {len(active)} decision(s) checked.",
        }

    icon = "⚠" if severity == "warning" else "✗"
    label = "Warning" if severity == "warning" else "Conflict"
    match_display = best_overlap[:8]
    ellipsis = "…" if len(best_overlap) > 8 else ""

    what = (best_match.get("what") or "")[:120]
    why = (best_match.get("why") or "")[:120]

    lines = [
        f"IRP guard: {icon} {label}",
        "",
        f"  Decision:   {best_match.get('id', '')}",
        f"  What:       {what}",
        f"  Why:        {why}",
        f"  Matched on: {', '.join(match_display)}{ellipsis}",
        f"  Checked:    {len(active)} active decision(s)",
        "",
        "Run `irp why` for full decision context.",
    ]

    if severity == "warning":
        lines.append("(Warning only — commit not blocked.)")
    else:
        lines.append("(Conflict detected — set IRP_GUARD_BLOCK=1 to abort this commit.)")

    return {
        "command": "guard.run",
        "status": severity,
        "match_id": best_match.get("id"),
        "matched_on": best_overlap,
        "severity": severity,
        "checked": len(active),
        "text": "\n".join(lines),
    }

# ── subcommand: status ────────────────────────────────────────────────────────

def _run_guard_status(project_root: Path, irp_dir: Path, args) -> dict:
    git_dir = project_root / ".git"
    hook_path = git_dir / "hooks" / "pre-commit"

    if not git_dir.is_dir():
        return {
            "command": "guard.status",
            "status": "no_git",
            "text": "Not a git repository — no pre-commit hook possible.",
        }

    if not hook_path.exists():
        return {
            "command": "guard.status",
            "status": "not_installed",
            "text": (
                "IRP guard hook: not installed.\n"
                "Run `irp guard install` to install."
            ),
        }

    content = hook_path.read_text(encoding="utf-8", errors="replace")
    if _HOOK_MARKER in content:
        executable = hook_path.stat().st_mode & 0o111
        exec_note = "" if executable else " (warning: hook is not executable — run chmod +x)"
        return {
            "command": "guard.status",
            "status": "installed",
            "output_path": str(hook_path),
            "text": f"IRP guard hook: installed{exec_note}\n  {hook_path}",
        }

    return {
        "command": "guard.status",
        "status": "other_hook",
        "output_path": str(hook_path),
        "text": (
            f"A pre-commit hook exists at:\n  {hook_path}\n\n"
            "It was not installed by IRP. Add `irp guard run` to it manually,\n"
            "or back it up and run `irp guard install --force`."
        ),
    }

# ── public entry point ────────────────────────────────────────────────────────

def run_guard(project_root: Path, irp_dir: Path, args) -> dict:
    action = getattr(args, "guard_action", None)
    if action == "install":
        return _run_guard_install(project_root, irp_dir, args)
    if action == "run":
        return _run_guard_run(project_root, irp_dir, args)
    if action == "status":
        return _run_guard_status(project_root, irp_dir, args)
    return {
        "command": "guard",
        "status": "error",
        "text": f"Unknown guard action: {action!r}",
    }
