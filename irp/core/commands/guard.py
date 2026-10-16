"""irp guard: a pre-commit hook that checks staged changes against IRP decisions.

Subcommands:
  irp guard install  write the hook into the repo's hooks folder
  irp guard run      check the staged diff against active decisions (the hook calls this)
  irp guard status   say whether the IRP hook is installed

Severity model (v0):
  clear     no token overlap with any active decision
  warning   1 or 2 tokens overlap (informational, does not block)
  conflict  3 or more tokens overlap, so the dispatcher returns exit 10

Hook blocking:
  By default the hook warns and never stops a commit.
  Set IRP_GUARD_BLOCK=1 in the environment to stop a commit on a conflict.

The hook (v1) names its project relative to the repo's top folder and records
the command that installed it. `guard run --hook-project` decides whether that
path holds a project, with the same rules as every other command, so the shell
part stays thin. Decisions come from the project and the staged diff from the
repo the hook runs in, which differ in a linked worktree.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from irp.core.store import ProjectNotFound, find_named_project, read_active

# ── hook templates ────────────────────────────────────────────────────────────

_HOOK_MARKER = "# irp-guard-v1"
_HOOK_MARKER_V0 = "# irp-guard-v0"
_SHARED_MARKER = "# irp-guard-shared"

# The dispatcher, recorded when the install didn't come through the CLI.
_DISPATCHER = Path(__file__).resolve().parent.parent / "irp.py"

NO_IRP_COMMAND_TEXT = ("IRP guard: couldn't find the irp command, so this commit wasn't checked. "
                       "To fix it, run irp guard install --force in the project.")

_BLOCK_TAIL = """\
GUARD_EXIT=$?
if [ "$GUARD_EXIT" -eq 10 ] && [ "${IRP_GUARD_BLOCK:-0}" = "1" ]; then
  echo ""
  echo "IRP guard: commit aborted (IRP_GUARD_BLOCK=1). Run \\`irp why\\` to review the conflict."
  exit 1
fi
exit 0
"""

_HOOK_V1 = """\
#!/bin/sh
@MARKER@
# IRP guard checks staged changes against the project's decisions.
# It warns on a conflict. It only stops a commit when IRP_GUARD_BLOCK=1:
#   IRP_GUARD_BLOCK=1 git commit -m "..."
# Written by irp guard install. To rewrite it: irp guard install --force
IRP_PROJECT="@PROJECT@"
IRP_PY="@PY@"
IRP_ENTRY="@ENTRY@"

top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$top" || exit 0
if [ -n "$IRP_PY" ] && [ -x "$IRP_PY" ] && [ -f "$IRP_ENTRY" ]; then
  "$IRP_PY" "$IRP_ENTRY" guard run --hook-project "$IRP_PROJECT"
elif [ -z "$IRP_PY" ] && [ -n "$IRP_ENTRY" ] && [ -x "$IRP_ENTRY" ]; then
  "$IRP_ENTRY" guard run --hook-project "$IRP_PROJECT"
elif command -v irp >/dev/null 2>&1; then
  irp guard run --hook-project "$IRP_PROJECT"
else
  echo "@NO_IRP@"
  exit 0
fi
""" + _BLOCK_TAIL

_HOOK_SHARED = """\
#!/bin/sh
@MARKER@
@SHARED@
# IRP guard checks staged changes against the project's decisions.
# It warns on a conflict. It only stops a commit when IRP_GUARD_BLOCK=1:
#   IRP_GUARD_BLOCK=1 git commit -m "..."
# A shared hook records no paths. It runs the irp on PATH, so it only finds
# a project at the repo's top folder (or the main checkout's).

top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$top" || exit 0
if command -v irp >/dev/null 2>&1; then
  irp guard run --hook-project .
else
  echo "@NO_IRP@"
  exit 0
fi
""" + _BLOCK_TAIL


def _dq(value: str) -> str:
    """Escape a value for a double-quoted POSIX sh string."""
    return re.sub(r'([\\"$`])', r"\\\1", value)


def _undq(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _hook_text(project_rel: str, py: str, entry: str) -> str:
    text = _HOOK_V1
    for key, value in (("@MARKER@", _HOOK_MARKER), ("@NO_IRP@", _dq(NO_IRP_COMMAND_TEXT)),
                       ("@PROJECT@", _dq(project_rel)), ("@PY@", _dq(py)), ("@ENTRY@", _dq(entry))):
        text = text.replace(key, value)
    return text


def _shared_hook_text() -> str:
    return (_HOOK_SHARED.replace("@MARKER@", _HOOK_MARKER)
            .replace("@SHARED@", _SHARED_MARKER).replace("@NO_IRP@", _dq(NO_IRP_COMMAND_TEXT)))


_SETTING = re.compile(r'^(IRP_PROJECT|IRP_PY|IRP_ENTRY)="((?:[^"\\]|\\.)*)"[ \t]*$', re.M)


def _hook_info(hook_path: Path) -> dict | None:
    """What sits at `hook_path`: None when nothing does.

    version  "v1", "v0" or None (a hook IRP didn't write)
    shared   True for a v1 hook that records no paths
    project, py, entry  the settings a v1 hook recorded
    """
    if not os.path.lexists(hook_path):
        return None
    try:
        text = hook_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if _HOOK_MARKER in text:
        settings = {m.group(1): _undq(m.group(2)) for m in _SETTING.finditer(text)}
        return {
            "version": "v1",
            "shared": _SHARED_MARKER in text,
            "project": settings.get("IRP_PROJECT"),
            "py": settings.get("IRP_PY", ""),
            "entry": settings.get("IRP_ENTRY", ""),
        }
    if _HOOK_MARKER_V0 in text:
        return {"version": "v0", "shared": False, "project": None, "py": "", "entry": ""}
    return {"version": None, "shared": False, "project": None, "py": "", "entry": ""}


def _command_that_ran(args) -> tuple[str, str]:
    """The command to record in the hook: (python, entry).

    Run as a script (`python3 .../irp/core/irp.py`) it's the Python plus the
    script. Run as a pip-installed `irp` it's that script alone, with no
    Python. The CLI passes its argv[0] as args.irp_argv0.
    """
    argv0 = getattr(args, "irp_argv0", None)
    if argv0:
        path = Path(os.path.abspath(argv0))
        if path.suffix == ".py" and path.is_file():
            return sys.executable or "", str(path)
        if path.is_file() and os.access(path, os.X_OK):
            return "", str(path)
    return sys.executable or "", str(_DISPATCHER)


def _show_command(py: str, entry: str) -> str:
    if not entry:
        return "the irp on your PATH"
    return f"{py} {entry}" if py else entry


def _recorded_command_works(py: str, entry: str) -> bool:
    if not entry:
        return False
    if py:
        return os.path.isfile(entry) and os.access(py, os.X_OK)
    return os.path.isfile(entry) and os.access(entry, os.X_OK)

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

# ── git ───────────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> str | None:
    """Run git in `cwd` and return its output, or None when it fails."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def _git_path(value: str, base: Path) -> Path:
    """A path git printed, relative to the folder it ran in, made absolute."""
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _repo_locations(folder: Path) -> tuple[Path, Path, Path] | None:
    """(top folder, git folder, hooks folder) of the repo holding `folder`.

    The git folder is the one every linked worktree shares. The hooks folder
    honours core.hooksPath. None outside a git work tree.
    """
    top = _git(["rev-parse", "--show-toplevel"], folder)
    if not top:
        return None
    top_path = Path(top).resolve()
    common = _git(["rev-parse", "--git-common-dir"], top_path)
    hooks = _git(["rev-parse", "--git-path", "hooks"], top_path)
    if not common or not hooks:
        return None
    return top_path, _git_path(common, top_path), _git_path(hooks, top_path)


def _is_within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


# ── staged diff extraction ─────────────────────────────────────────────────────

def _get_staged_text(repo_folder: Path) -> str | None:
    """Added lines from the staged diff as one string. None when git fails."""
    out = _git(["diff", "--cached", "--unified=0"], repo_folder)
    if out is None:
        return None
    lines = []
    for line in out.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:].strip())
    return " ".join(lines)


def _get_commit_message(repo_folder: Path) -> str:
    """Read COMMIT_EDITMSG, found through git (linked worktrees keep their own)."""
    where = _git(["rev-parse", "--git-path", "COMMIT_EDITMSG"], repo_folder)
    if not where:
        return ""
    msg_path = _git_path(where, repo_folder)
    try:
        return msg_path.read_text(encoding="utf-8").strip() if msg_path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return ""

# ── severity ──────────────────────────────────────────────────────────────────

def _severity(overlap_count: int) -> str:
    if overlap_count == 0:
        return "clear"
    if overlap_count <= 2:
        return "warning"
    return "conflict"

# ── subcommand: install ───────────────────────────────────────────────────────

def _install_result(status: str, text: str, **extra) -> dict:
    return {"command": "guard.install", "status": status, "text": text, **extra}


def _what_is_there(info: dict, top: Path) -> str:
    if info["version"] == "v0":
        return "older version, v0"
    if info["shared"]:
        return "v1, shared, records no paths"
    project = info.get("project") or "."
    return f"v1, for the project at {os.path.normpath(top / project)}"


def _write_hook(hook_path: Path, text: str) -> None:
    """Write the hook in one step. A symlinked hook is replaced, never followed."""
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = hook_path.with_name(f".{hook_path.name}.irp-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, hook_path)


def _run_guard_install(project_root: Path, irp_dir: Path, args) -> dict:
    force = bool(getattr(args, "force", False))
    shared = bool(getattr(args, "shared", False))
    project_root = Path(project_root).resolve()

    locations = _repo_locations(project_root)
    if locations is None:
        return _install_result("error", "There's no git repo here, so there's nowhere to put a commit hook.")
    top, git_dir, hooks_dir = locations
    hook_path = hooks_dir / "pre-commit"

    # The hook records this machine's paths, so it only goes where git never
    # commits it: a hooks folder inside the repo's git folder.
    if not _is_within(hooks_dir, git_dir) and not shared:
        return _install_result(
            "error",
            f"This repo's hooks folder is {hooks_dir} (set by core.hooksPath), outside its .git folder.\n"
            "A hook there could get committed with this machine's paths in it or run in every repo "
            "that shares the folder. Nothing was written.\n"
            "For a hook there that records no paths, run: irp guard install --shared",
            reason="hooks_path_outside_git",
            hooks_dir=str(hooks_dir),
        )

    rel = os.path.relpath(project_root, top)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return _install_result("error", f"The project at {project_root} isn't inside the git repo at {top}.")
    rel = Path(rel).as_posix()
    py, entry = _command_that_ran(args)
    if "\n" in py or "\n" in entry:
        py, entry = "", ""
    if "\n" in rel:
        return _install_result("error", "The project's path has a line break in it, so the hook can't record it.")

    info = _hook_info(hook_path)
    if info is not None and not force:
        same_kind = info["version"] == "v1" and info["shared"] == shared
        if same_kind and (shared or info.get("project") == rel):
            which = "(v1, shared)" if shared else "for this project (v1)"
            lines = [f"IRP guard hook already installed {which} at {hook_path}. Nothing changed."]
            if not shared and (info.get("py"), info.get("entry")) != (py, entry):
                lines.append(f"It runs {info.get('entry') or 'the irp on your PATH'}. "
                             "To switch it to this one: irp guard install --force")
            return _install_result("exists", "\n".join(lines), output_path=str(hook_path))
        if info["version"] is None:
            text = (f"There's already a pre-commit hook at {hook_path} and IRP didn't write it. "
                    "Nothing was written.\n"
                    f"To have it check this project, add this line to it: irp guard run --hook-project {rel}\n"
                    "Or back it up, then replace it: irp guard install --force")
        else:
            text = (f"There's already an IRP guard hook at {hook_path} ({_what_is_there(info, top)}). "
                    "Nothing was written.\n"
                    "To replace it with one for this project: irp guard install --force")
        return _install_result("conflict", text, output_path=str(hook_path),
                               existing=info["version"] or "not_irp")

    _write_hook(hook_path, _shared_hook_text() if shared else _hook_text(rel, py, entry))

    if shared:
        return _install_result(
            "ok",
            f"Wrote a shared hook to {hook_path}. It records no paths and uses the irp on your PATH, "
            "so it only finds a project at a repo's top folder.",
            output_path=str(hook_path), shared=True,
        )
    action = "Updated" if info is not None else "Installed"
    return _install_result(
        "ok",
        f"{action} the IRP guard hook at {hook_path}.\n"
        f"It checks the project at {project_root} and runs {_show_command(py, entry)}.\n"
        "It warns on a conflict and lets the commit through. To stop a commit on a conflict:\n"
        "  IRP_GUARD_BLOCK=1 git commit -m \"your message\"\n"
        "Run irp guard status to check it.",
        output_path=str(hook_path), project=rel,
    )

# ── subcommand: run ───────────────────────────────────────────────────────────

def _project_note(project_root: Path, repo_folder: Path) -> str | None:
    """The project's path, when it isn't the top of the repo being committed."""
    top = _git(["rev-parse", "--show-toplevel"], repo_folder)
    if not top:
        return None
    root = Path(project_root).resolve()
    return None if Path(top).resolve() == root else str(root)


def _with_note(result: dict, note: str) -> dict:
    result["text"] += note
    return result


def _run_guard_run(project_root: Path, irp_dir: Path, args) -> dict:
    # Decisions come from the project; the staged diff and the commit message
    # from the repo guard run runs in (the CLI passes its folder as irp_cwd).
    # Those differ in a linked worktree and for a project in a subfolder.
    repo_folder = Path(getattr(args, "irp_cwd", None) or project_root)
    staged_text = _get_staged_text(repo_folder)
    if staged_text is None and repo_folder != Path(project_root):
        repo_folder = Path(project_root)
        staged_text = _get_staged_text(repo_folder)
    if staged_text is None:
        # Outside git, or git missing or broken: it can't tell, so it never says clear.
        return {
            "command": "guard.run",
            "status": "error",
            "text": "IRP guard: couldn't read the staged changes, so nothing was checked.",
        }
    commit_msg = _get_commit_message(repo_folder)
    combined = f"{staged_text} {commit_msg}".strip()

    if not combined:
        return {
            "command": "guard.run",
            "status": "clear",
            "text": "IRP guard: nothing to check (no staged changes).",
        }

    # An old current.json can still hold unconfirmed bootstrap guesses, and a
    # window full of them pushes real decisions out; read_active then recomputes
    # the list from the ledger instead of just dropping the guesses.
    active, _ = read_active(irp_dir)
    project_note = _project_note(project_root, repo_folder)
    checked_against = f"\n  Checked against the project at {project_note}." if project_note else ""

    if not active:
        return _with_note({
            "command": "guard.run",
            "status": "clear",
            "checked": 0,
            "text": "IRP guard: clear — no active decisions in ledger.",
        }, checked_against)

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
        return _with_note({
            "command": "guard.run",
            "status": "clear",
            "checked": len(active),
            "text": f"IRP guard: clear — {len(active)} decision(s) checked.",
        }, checked_against)

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
    ] + ([f"  Project:    {project_note}"] if project_note else []) + [
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

V0_HOOK_STATUS_TEXT = ("IRP guard hook installed (older version, v0). It works, but it can't find a project "
                       "outside the repo's top folder. To upgrade: irp guard install --force")


def _status_result(status: str, lines: list[str], **extra) -> dict:
    return {"command": "guard.status", "status": status, "text": "\n".join(lines), **extra}


def _run_guard_status(project_root: Path, irp_dir: Path, args) -> dict:
    """Report only: finds the hook through git and creates nothing."""
    missing = getattr(args, "irp_project_missing", None)
    no_project = [f"No IRP project here. Start one with {missing.hint}"] if missing is not None else []

    locations = _repo_locations(Path(project_root))
    if locations is None:
        return _status_result("no_git", ["Not a git repository, so there's no commit hook here."] + no_project)
    top, _git_dir, hooks_dir = locations
    hook_path = hooks_dir / "pre-commit"
    info = _hook_info(hook_path)

    if info is None:
        return _status_result("not_installed", [
            "IRP guard hook: not installed.",
            "Run irp guard install to install it.",
        ] + no_project)

    if info["version"] is None:
        return _status_result("other_hook", [
            f"A pre-commit hook exists at {hook_path} and IRP didn't install it.",
            "Add irp guard run to it, or back it up and run: irp guard install --force",
        ] + no_project, output_path=str(hook_path))

    executable = hook_path.stat().st_mode & 0o111 if hook_path.exists() else 0
    exec_note = "" if executable else " (warning: the hook isn't executable, run chmod +x on it)"

    if info["version"] == "v0":
        return _status_result("installed", [
            V0_HOOK_STATUS_TEXT + exec_note,
            f"  {hook_path}",
        ] + no_project, output_path=str(hook_path), version="v0")

    lines = [f"IRP guard hook: installed (v1){exec_note}", f"  {hook_path}"]
    extra: dict[str, Any] = {"version": "v1", "shared": info["shared"]}
    if info["shared"]:
        lines.append("  Shared: it records no paths and runs the irp on your PATH.")
    else:
        named = Path(os.path.normpath(top / (info.get("project") or ".")))
        try:
            found = find_named_project(named, "--hook-project")
            lines.append(f"  Checks the project at {found.root}.")
            extra["project"] = str(found.root)
        except ProjectNotFound:
            lines.append(f"  It names {named}, but there's no IRP project there, so commits aren't checked.")
            extra["project"] = None
        py, entry = info.get("py", ""), info.get("entry", "")
        if _recorded_command_works(py, entry):
            lines.append(f"  Runs {_show_command(py, entry)}.")
        elif entry:
            lines.append(f"  The command it recorded ({_show_command(py, entry)}) is gone, "
                         "so it uses the irp on your PATH.")
        else:
            lines.append("  Runs the irp on your PATH.")
    return _status_result("installed", lines + no_project, output_path=str(hook_path), **extra)

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
