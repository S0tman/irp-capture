"""Helpers shared by the project-discovery tests.

Everything here builds throwaway folders and git repos under pytest's temp
folder, inside the seal from conftest.py. Nothing touches a real project.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import make_project

REPO = Path(__file__).resolve().parent.parent
IRP_PY = str(REPO / "irp" / "core" / "irp.py")

# `irp` must be the package, not irp/core/irp.py (see the note in that file).
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

DECISION_ID = "IRP-2026-01-01-001"
DECISION = {
    "type": "decision",
    "id": DECISION_ID,
    "what": "Use Postgres for the reporting service",
    "why": "Redis rejected because the query patterns need joins",
    "confidence": "high",
    "timestamp": "2026-01-01",
}
# Text that contradicts DECISION: check, gate and the hook all flag it.
CONTRADICTION = "move the reporting service to redis, drop postgres joins"

# The default current.json as today's ensure_irp_dir writes it: indented, 34 bytes.
DEFAULT_CURRENT_INDENTED = json.dumps({"version": 1, "active": []}, indent=2, ensure_ascii=False)
DEFAULT_CURRENT_COMPACT = json.dumps({"version": 1, "active": []})

GUARD_NO_PROJECT = "IRP guard: no IRP project in this repo, so this commit wasn't checked."
GUARD_NO_IRP = ("IRP guard: couldn't find the irp command, so this commit wasn't checked. "
      "To fix it, run irp guard install --force in the project.")


def run_irp(args, cwd, *, input=None, env=None, timeout=120):
    """Run the irp CLI under test as a separate program."""
    return subprocess.run(
        [sys.executable, IRP_PY, *args],
        cwd=str(cwd), input=input, capture_output=True, text=True, env=env, timeout=timeout,
    )


def git(args, cwd, *, check=True, env=None):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stdout}{result.stderr}")
    return result


def git_repo(folder: Path, *, ignore_irp: bool = True) -> Path:
    """An initialised repo with one commit, so worktrees and branches work."""
    folder.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], folder)
    if ignore_irp:
        (folder / ".gitignore").write_text(".irp/\n", encoding="utf-8")
    else:
        (folder / "README.md").write_text("test repo\n", encoding="utf-8")
    git(["add", "-A"], folder)
    git(["commit", "-q", "-m", "start", "--no-verify"], folder)
    return folder


def commit_all(folder: Path, message: str = "change", *, no_verify: bool = True) -> None:
    git(["add", "-A"], folder)
    git(["commit", "-q", "-m", message] + (["--no-verify"] if no_verify else []), folder)


def start_project(folder: Path, entries=(DECISION,)) -> Path:
    """A started project holding `entries`. Returns its .irp/ folder."""
    folder.mkdir(parents=True, exist_ok=True)
    irp_dir = make_project(folder)
    if entries:
        with (irp_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        active = [e for e in entries if e.get("type") == "decision"][-10:]
        (irp_dir / "current.json").write_text(
            json.dumps({"version": 1, "active": active}, indent=2), encoding="utf-8")
    return irp_dir


def write_empty_stray(folder: Path, current: str | None = DEFAULT_CURRENT_INDENTED) -> Path:
    """An empty .irp/ as older IRP versions left them: empty ledger, default current.json."""
    irp_dir = folder / ".irp"
    irp_dir.mkdir(parents=True, exist_ok=True)
    (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
    if current is not None:
        (irp_dir / "current.json").write_text(current, encoding="utf-8")
    return irp_dir


def ledger_lines(irp_dir: Path) -> list[dict]:
    path = irp_dir / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tree_state(*roots: Path) -> dict[str, str]:
    """Every file, folder and link under `roots`, with a content hash for files."""
    state: dict[str, str] = {}
    for root in roots:
        if not os.path.lexists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames:
                p = Path(dirpath) / name
                state[str(p)] = "link" if p.is_symlink() else "dir"
            for name in filenames:
                p = Path(dirpath) / name
                if p.is_symlink():
                    state[str(p)] = "link:" + os.readlink(p)
                else:
                    state[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return state


def state_diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return [f"+ {p}" for p in added] + [f"- {p}" for p in removed] + [f"~ {p}" for p in changed]


def irp_folders(root: Path) -> list[str]:
    """Every .irp under root (a folder or anything else by that name)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if name == ".irp":
                found.append(str(Path(dirpath) / name))
    return sorted(found)


def out(result) -> str:
    """stdout and stderr together, for checks that don't care which stream."""
    return (result.stdout or "") + (result.stderr or "")
