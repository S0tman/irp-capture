"""irp init: start an IRP project in a folder.

The only command that creates .irp/. Every other command needs a project that
already exists and finds it by walking up from the current folder.

    irp init [PATH] [--nested]
"""
from __future__ import annotations

import os
from pathlib import Path

from store import (
    ProjectNotFound,
    classify_irp_folder,
    git_top_folder,
    init_project,
    is_linked_worktree,
    main_checkout_of,
    walk_up,
)


def _entry_count(irp_dir: Path) -> int:
    path = irp_dir / "ledger.jsonl"
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except (OSError, UnicodeDecodeError):
        return 0


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def _done(text: str, status: str, exit_code: int, **extra) -> dict:
    return {"command": "init", "status": status, "exit_code": exit_code, "text": text, **extra}


def run_init(args) -> dict:
    raw = getattr(args, "path", None) or getattr(args, "project_path", None) or "."
    nested = bool(getattr(args, "nested", False))
    given = Path(os.path.abspath(os.path.expanduser(str(raw))))

    if not given.is_dir():
        return _done(f"There's no folder at {given}. Create it first, then run irp init {given}.",
                     "missing_folder", 1)
    folder = given.resolve()

    if folder == _home():
        return _done("Your home folder can't be an IRP project. Pick the project's own folder, "
                     "for example: irp init ~/my-project", "home_folder", 1)

    irp_dir = folder / ".irp"
    stray = False
    if os.path.lexists(irp_dir):
        kind = classify_irp_folder(irp_dir) if irp_dir.is_dir() else "not_irp"
        if kind == "project":
            return _done(f"{folder} is already an IRP project ({_entry_count(irp_dir)} entries). "
                         "Nothing changed.", "exists", 0, project_root=str(folder))
        if kind == "not_irp":
            return _done(f"{irp_dir} already exists and IRP didn't make it. "
                         "Left it alone, nothing changed.", "not_irp", 1)
        stray = True

    git_top = git_top_folder(folder)
    main = main_checkout_of(git_top) if git_top is not None else None

    if not nested:
        # A ledger in a linked worktree goes when the worktree is removed.
        try:
            outer = walk_up(folder)
        except ProjectNotFound:
            outer = None
        if main is not None and git_top is not None and (outer is None or outer.main_checkout is not None):
            anyway = "For a ledger in this worktree anyway, run:"
            if outer is not None:
                second = f"Decisions made here already go to the project at {outer.root}."
                third = f"{anyway} irp init --nested {folder}"
            else:
                start = "Start the project in the main checkout instead:"
                width = len(start) + 2
                second = start.ljust(width) + f"irp init {main / folder.relative_to(git_top)}"
                third = anyway.ljust(width) + f"irp init --nested {folder}"
            where = "is" if folder == git_top else "is inside"
            return _done(
                f"{folder} {where} a linked worktree of {main}. "
                "A ledger started here would go when the worktree is removed.\n"
                f"{second}\n{third}",
                "worktree", 1)
        if main is None and git_top is not None and outer is None and is_linked_worktree(git_top):
            where = "is" if folder == git_top else "is inside"
            return _done(
                f"{folder} {where} a linked worktree and IRP couldn't find its main checkout. "
                "A ledger started here would go when the worktree is removed.\n"
                f"For a ledger in this worktree anyway, run: irp init --nested {folder}",
                "worktree", 1)

        # Decisions made inside another project already go there.
        if outer is not None and outer.root != folder:
            return _done(
                f"{folder} is inside the IRP project at {outer.root}, so decisions made here already go there.\n"
                f"For a separate ledger in this folder, run: irp init --nested {folder}",
                "inside_project", 1)

    init_project(folder)

    if stray:
        lines = [f"Started an IRP project in {folder}, using the empty .irp folder "
                 "an older IRP version left here."]
    else:
        lines = [f"Started an IRP project in {folder}."]
    lines += [f"Decisions will be kept in {irp_dir / 'ledger.jsonl'}.", "Next: irp capture"]
    if git_top is not None and git_top != folder:
        lines.append(f"This folder is inside the git repo at {git_top}. "
                     "That's fine when the repo holds more than one project.")
    return _done("\n".join(lines), "adopted" if stray else "started", 0, project_root=str(folder))
