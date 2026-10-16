"""irp doctor — installation and environment health check."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from importlib.metadata import version as pkg_version, PackageNotFoundError
except ImportError:
    from importlib_metadata import version as pkg_version, PackageNotFoundError  # type: ignore

from irp.core.resolver import build_retirement_set, build_supersession_map
from irp.core.store import (
    classify_irp_folder,
    is_unconfirmed,
    read_current,
    read_ledger,
    read_reconstructions,
    rebuild_current,
    shared_unconfirmed_ids,
    write_current,
)

_ACCEPT_HINT = "irp bootstrap --accept REC-..."


def _scan_unconfirmed(irp_path: Path) -> tuple:
    """Legacy bootstrap guesses still sitting in the ledger.

    An older `irp bootstrap` wrote them there as if they were decisions; the
    ledger is append-only, so they can only be dealt with by superseding or
    retiring them. Returns (own_ids, pending_count, shared_ids):
      own_ids       ids that belong to a guess alone, so retire/supersede by id is safe
      pending_count every guess line not yet superseded or retired
      shared_ids    ids a guess shares with a confirmed decision (older versions
                    could give both the same id); retire/supersede by id would
                    hit the confirmed decision too, so these are never advised
    """
    ledger = read_ledger(irp_path)
    done = build_supersession_map(ledger) | build_retirement_set(ledger)
    shared = shared_unconfirmed_ids(ledger) - done
    pending = [
        e for e in ledger
        if is_unconfirmed(e) and e.get("type") == "decision" and e.get("id") not in done
    ]
    own_ids = list(dict.fromkeys(
        str(e.get("id", "")) for e in pending if e.get("id") not in shared
    ))
    return own_ids, len(pending), sorted(shared)


def _ledger_notes(
    unconfirmed_ids: list,
    shared_ids: list,
    pending_reconstructions: int,
    current_stale: bool,
) -> list:
    notes = []
    n = len(unconfirmed_ids)
    if n:
        noun = "guess" if n == 1 else "guesses"
        shown = ", ".join(unconfirmed_ids[:5]) + (f" and {n - 5} more" if n > 5 else "")
        notes += [
            f"    ! {n} unconfirmed bootstrap {noun} in ledger.jsonl: {shown}",
            "        An older `irp bootstrap` wrote these as if they were decisions. Agents, exports,",
            "        checks and evidence already ignore them, and they can't be deleted (the ledger is append-only).",
            "        If one was a real decision, confirm it: irp mod supersede <id> --decision \"...\" --reason \"...\"",
            "        If it was not: irp mod retire <id> --reason \"bootstrap guess, not a decision\"",
        ]
    k = len(shared_ids)
    if k:
        noun = "guess shares its id" if k == 1 else "guesses share their ids"
        notes += [
            f"    ! {k} bootstrap {noun} with a confirmed decision: {', '.join(shared_ids)}",
            "        Older versions could give a guess and a confirmed decision the same id, so both lines are in the file.",
            "        The guess is already ignored by agents, exports, checks and evidence, so it can stay.",
            "        irp mod refuses to retire or supersede one of these ids unless you add --shared-id-ok.",
            "        That flag changes the confirmed decision (the guess half is already ignored), so use it only if you mean to.",
        ]
    if current_stale:
        notes += [
            "    ! current.json still lists unconfirmed bootstrap guesses from an older version",
            "        Readers recompute the list from the ledger, so agents are not affected.",
            "        Rebuild the file from the ledger with: irp doctor --fix",
        ]
    if pending_reconstructions:
        noun = "reconstruction" if pending_reconstructions == 1 else "reconstructions"
        notes += [
            f"    ! {pending_reconstructions} unconfirmed {noun} waiting in .irp/reconstructions.jsonl (not in the ledger)",
            f"        Read them, then accept the real ones: {_ACCEPT_HINT}",
        ]
    return notes

def _check(label: str, ok: bool, detail: str = "") -> dict:
    return {"label": label, "ok": ok, "detail": detail}


# ── stray .irp folders (report only) ──────────────────────────────────────────
#
# doctor looks in a few bounded places and never walks a tree: each folder from
# where it runs up to the project (or the git top folder), the git top folder,
# each immediate subfolder of the project and git's index of tracked files.
# It removes nothing.

STRAY_HEADER = "Stray .irp folders (older IRP versions made these when run outside a project)"
STRAY_FOOTER = "An empty one can go by hand once you've checked it. IRP never removes a folder holding entries."

# Folder names that mean a cloud-synced folder, where listing a folder can
# start downloads. Matched case-insensitively against the start of each part.
_CLOUD_PREFIXES = ("dropbox", "onedrive", "proton drive", "protondrive", "google drive",
                   "googledrive", "icloud", "mobile documents", "cloudstorage")


def _cloud_synced(path: Path) -> bool:
    parts = path.parts
    # Windows drives under WSL (/mnt/c/...)
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "mnt" and len(parts[2]) == 1:
        return True
    return any(part.lower().startswith(_CLOUD_PREFIXES) for part in parts)


def _home_folder() -> Path | None:
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def _entry_count(ledger: Path) -> int:
    try:
        with ledger.open("rb") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


# git sets these for hooks, `!` aliases and `rebase --exec`. Left in place,
# git would treat the folder it runs in as the top of the worktree, so it's
# asked to find the repo from that folder instead.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def _tracked_ledgers(git_top: Path) -> list[Path] | None:
    """Folders holding a ledger git tracks, at any depth. Reads only git's index."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", ":(glob)**/.irp/ledger.jsonl"],
            cwd=str(git_top), capture_output=True, timeout=20, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    found = []
    for raw in result.stdout.split(b"\0"):
        if raw:
            ledger = git_top / os.fsdecode(raw)
            found.append(ledger.parent.parent)
    return found


def ledger_tracked_by_git(project_root: Path) -> bool | None:
    """True when git tracks the project's ledger, False when it doesn't, None
    when git can't say (not a repo, or git missing). Reads only git's index."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", ".irp/ledger.jsonl"],
            cwd=str(project_root), capture_output=True, timeout=20, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode == 0:
        return True
    return False if result.returncode == 1 else None


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def scan_strays(here: Path, project_root: Path | None) -> dict:
    """Find stray .irp folders in the bounded places. Creates and removes nothing.

    Returns {"strays": [...], "looked": "...", "cloud_note": str or None}. Each
    stray is {"path", "kind": "empty" or "entries", "entries"}. The project's own
    .irp/ and folders that aren't IRP's are never listed.
    """
    here = Path(os.path.abspath(here)).resolve()
    home = _home_folder()
    candidates: list[Path] = []

    # Each folder from here up to the project folder, or the git top folder.
    git_top = None
    last = None
    folder = here
    while not (home is not None and folder == home):
        candidates.append(folder)
        last = folder
        if project_root is not None and folder == project_root:
            break
        if os.path.lexists(folder / ".git"):
            git_top = folder
            break
        if folder.parent == folder:
            break
        folder = folder.parent
    if git_top is None:
        for above in (folder, *folder.parents):
            if home is not None and above == home:
                break
            if os.path.lexists(above / ".git"):
                git_top = above
                break
    if git_top is not None and git_top not in candidates:
        candidates.append(git_top)

    looked: list[str] = []
    if last is not None:
        looked.append(f"from here up to {last}")

    # One level below the project folder (or the git top folder with no project).
    base = project_root if project_root is not None else git_top
    cloud_note = None
    if base is not None:
        if _cloud_synced(base):
            cloud_note = (f"Didn't look in the folders below {base}: it's in a cloud-synced folder, "
                          "where listing a folder can start downloads.")
        else:
            try:
                with os.scandir(base) as entries:
                    subfolders = sorted(Path(e.path) for e in entries
                                        if e.name not in (".git", ".irp") and e.is_dir(follow_symlinks=False))
            except OSError:
                subfolders = []
            candidates.extend(sub for sub in subfolders if os.path.lexists(sub / ".irp"))
            looked.append(f"one level below {base}")

    # Ledgers git tracks, at any depth. Like the walk up, this never reaches
    # the home folder, which can be a git repo of its own.
    index_top = git_top
    if project_root is not None:
        index_top = None
        for above in (project_root, *project_root.parents):
            if home is not None and above == home:
                break
            if os.path.lexists(above / ".git"):
                index_top = above
                break
    if index_top is not None:
        tracked = _tracked_ledgers(index_top)
        if tracked is not None:
            candidates.extend(tracked)
            looked.append("in git's list of tracked files")

    own = project_root / ".irp" if project_root is not None else None
    strays = []
    seen = set()
    for folder in candidates:
        irp_dir = folder / ".irp"
        if irp_dir in seen or irp_dir == own:
            continue
        seen.add(irp_dir)
        if not irp_dir.is_dir():
            continue
        kind = classify_irp_folder(irp_dir)
        if kind == "empty_stray":
            strays.append({"path": str(irp_dir), "kind": "empty", "entries": 0})
        elif kind == "project":
            strays.append({"path": str(irp_dir), "kind": "entries",
                           "entries": _entry_count(irp_dir / "ledger.jsonl")})
    strays.sort(key=lambda s: s["path"])
    return {
        "strays": strays,
        "looked": f"Looked {_join(looked)}." if looked else "",
        "cloud_note": cloud_note,
    }


def _stray_lines(report: dict) -> list[str]:
    strays = report["strays"]
    lines = []
    if strays:
        lines.append(f"  {STRAY_HEADER}")
        width = max(len(s["path"]) for s in strays)
        for s in strays:
            if s["kind"] == "empty":
                what = "empty, holds nothing"
            else:
                n = s["entries"]
                what = f"holds {n} ledger {'entry' if n == 1 else 'entries'}, kept"
            lines.append(f"    {s['path'].ljust(width)}   {what}")
    else:
        lines.append("  Stray .irp folders: none found")
    for extra in (report["cloud_note"], report["looked"]):
        if extra:
            lines.append(f"    {extra}")
    if strays:
        lines.append(f"    {STRAY_FOOTER}")
    return lines

def run_doctor(project_root: Path, irp_dir: Path, args) -> dict:
    checks: list[dict] = []
    warnings: list[str] = []

    # ── Python version ────────────────────────────────────────────────────────
    py = sys.version_info
    py_ok = py >= (3, 9)
    py_str = f"{py.major}.{py.minor}.{py.micro}"
    checks.append(_check("Python", py_ok, py_str if py_ok else f"{py_str} — requires 3.9+"))

    # ── irp-capture package version ───────────────────────────────────────────
    try:
        irp_ver = pkg_version("irp-capture")
        checks.append(_check("irp-capture", True, f"v{irp_ver}"))
    except PackageNotFoundError:
        checks.append(_check("irp-capture", False, "package not found — run: pip install irp-capture"))

    # ── .irp/ directory ───────────────────────────────────────────────────────
    # From the CLI, irp_dir is None when no project was found and
    # args.irp_project_missing says where it looked.
    missing = getattr(args, "irp_project_missing", None)
    irp_path = Path(irp_dir) if irp_dir is not None else project_root / ".irp"
    irp_exists = irp_dir is not None and irp_path.is_dir()
    if irp_exists:
        project_detail = str(irp_path)
    elif missing is not None:
        project_detail = (f"no IRP project found from {missing.searched_from} "
                          f"(looked up to {missing.stopped_at}). Start one with: {missing.hint}")
    else:
        project_detail = f"not found. Start one with: irp init {project_root}"
    checks.append(_check(".irp/ directory", irp_exists, project_detail))

    # ── ledger.jsonl ──────────────────────────────────────────────────────────
    entry_count = 0
    if irp_exists:
        ledger = irp_path / "ledger.jsonl"
        if ledger.exists():
            try:
                lines = [l for l in ledger.read_text().splitlines() if l.strip()]
                valid = 0
                for line in lines:
                    json.loads(line)
                    valid += 1
                entry_count = valid
                checks.append(_check("ledger.jsonl", True, f"{valid} {'entry' if valid == 1 else 'entries'}"))
            except (json.JSONDecodeError, OSError) as e:
                checks.append(_check("ledger.jsonl", False, f"corrupt — {e}"))
        else:
            checks.append(_check("ledger.jsonl", False, "no entries yet — capture your first decision with: irp capture"))

        # ── current.json ──────────────────────────────────────────────────────
        current = irp_path / "current.json"
        if current.exists():
            try:
                data = json.loads(current.read_text())
                if isinstance(data, list):
                    n = len(data)
                elif isinstance(data, dict):
                    # Guesses an old bootstrap left in the file are not active decisions.
                    listed = data.get("active", [])
                    n = len([e for e in listed if not is_unconfirmed(e)]) if isinstance(listed, list) else 0
                else:
                    n = 0
                checks.append(_check("current.json", True, f"{n} active {'decision' if n == 1 else 'decisions'}"))
            except (json.JSONDecodeError, OSError) as e:
                checks.append(_check("current.json", False, f"corrupt — {e}"))
        else:
            checks.append(_check("current.json", False, "missing — will be created on next capture"))
    else:
        checks.append(_check("ledger.jsonl", False, "skipped — .irp/ not found"))
        checks.append(_check("current.json", False, "skipped — .irp/ not found"))

    # ── bootstrap guesses (legacy ledger lines, pending reconstructions) ──────
    unconfirmed_ids: list = []
    shared_ids: list = []
    unconfirmed_count = 0
    pending_reconstructions = 0
    current_stale = False
    fixed: list = []
    if irp_exists:
        unconfirmed_ids, unconfirmed_count, shared_ids = _scan_unconfirmed(irp_path)
        pending_reconstructions = sum(
            1 for r in read_reconstructions(irp_path) if r.get("status", "unconfirmed") == "unconfirmed"
        )
        if (irp_path / "current.json").exists():
            try:
                listed = read_current(irp_path).get("active", [])
            except (json.JSONDecodeError, OSError, AttributeError):
                listed = []
            current_stale = any(is_unconfirmed(e) for e in listed)
            if current_stale and getattr(args, "fix", False):
                # current.json is derived from the ledger, so rebuilding it is safe.
                write_current(irp_path, rebuild_current(read_ledger(irp_path)))
                fixed.append("current.json")
                current_stale = False
    ledger_notes = _ledger_notes(unconfirmed_ids, shared_ids, pending_reconstructions, current_stale)
    if fixed:
        ledger_notes.append("    ✓ current.json rebuilt from the ledger (unconfirmed guesses removed)")

    # A ledger inside a linked worktree goes when the worktree is removed,
    # unless git tracks it.
    project = getattr(args, "irp_project", None)
    worktree_of = getattr(project, "worktree_of", None)
    at_risk = bool(irp_exists and worktree_of is not None
                   and ledger_tracked_by_git(Path(project.root)) is not True)
    if at_risk:
        ledger_notes += [
            f"    ! This ledger is inside a linked worktree of {worktree_of} and git doesn't track it, "
            "so it goes when the worktree is removed.",
            "        Back it up before you remove the worktree.",
        ]
        if project.main_checkout_projects:
            ledger_notes.append(f"        The main checkout's project is at {project.main_checkout_projects[0]}.")

    # ── stray .irp folders (report only, never removed) ───────────────────────
    scan_root = Path(project.root) if project is not None else (
        Path(project_root).resolve() if irp_exists else None)
    strays = scan_strays(Path(getattr(args, "irp_cwd", None) or project_root), scan_root)

    # ── Claude Code skill ─────────────────────────────────────────────────────
    skill = project_root / "SKILL.md"
    checks.append(_check(
        "Claude Code skill",
        skill.exists(),
        "SKILL.md found" if skill.exists() else "not found — add with: curl -O https://raw.githubusercontent.com/S0tman/irp-capture/main/SKILL.md"
    ))

    # ── Optional integrations ─────────────────────────────────────────────────
    # Obsidian
    vault = os.environ.get("IRP_OBSIDIAN_VAULT")
    if vault:
        vault_path = Path(vault)
        obsidian_ok = vault_path.is_dir()
        checks.append(_check(
            "Obsidian",
            obsidian_ok,
            str(vault_path) if obsidian_ok else f"IRP_OBSIDIAN_VAULT set but path not found: {vault}"
        ))
    else:
        checks.append(_check("Obsidian", False, "not configured — set IRP_OBSIDIAN_VAULT=/path/to/vault"))

    # MemPalace
    mp_spec = importlib.util.find_spec("chromadb")
    mp_path = os.environ.get("IRP_MEMPALACE_PATH", str(Path.home() / ".mempalace" / "palace"))
    mp_dir_exists = Path(mp_path).is_dir()
    if mp_spec and mp_dir_exists:
        checks.append(_check("MemPalace", True, mp_path))
    elif mp_spec:
        checks.append(_check("MemPalace", False, f"chromadb installed but palace not found at {mp_path}"))
    else:
        checks.append(_check("MemPalace", False, "not installed — run: pip install 'irp-capture[mempalace]'"))

    # MCP server
    mcp_spec = importlib.util.find_spec("mcp")
    checks.append(_check(
        "MCP server",
        mcp_spec is not None,
        "installed — run: irp-mcp" if mcp_spec else "not installed — run: pip install 'irp-capture[mcp]'"
    ))

    # REST API
    fastapi_spec = importlib.util.find_spec("fastapi")
    checks.append(_check(
        "REST API",
        fastapi_spec is not None,
        "installed — run: irp-api" if fastapi_spec else "not installed — run: pip install 'irp-capture[api]'"
    ))

    # ── Summary ───────────────────────────────────────────────────────────────
    core_checks = checks[:6]   # Python, irp-capture, .irp/, ledger, current, Claude skill
    core_failed = [c for c in core_checks if not c["ok"]]
    all_ok = len(core_failed) == 0

    # ── Render text output ────────────────────────────────────────────────────
    lines = ["", "IRP Doctor", ""]

    sections = [
        ("System",       checks[0:2]),
        ("Ledger",       checks[2:5]),
        ("Editor",       checks[5:6]),
        ("Integrations", checks[6:]),
    ]

    for section_name, section_checks in sections:
        lines.append(f"  {section_name}")
        for c in section_checks:
            mark = "✓" if c["ok"] else "✗"
            detail = f" — {c['detail']}" if c["detail"] else ""
            lines.append(f"    {mark} {c['label']}{detail}")
        if section_name == "Ledger":
            lines.extend(ledger_notes)
            lines.append("")
            lines.extend(_stray_lines(strays))
        lines.append("")

    if all_ok:
        lines.append("  All core checks passed.")
    else:
        lines.append(f"  {len(core_failed)} core check(s) failed:")
        for c in core_failed:
            lines.append(f"    → {c['label']}: {c['detail']}")

    lines.append("")

    return {
        "status": "ok" if all_ok else "issues_found",
        "checks": checks,
        "entry_count": entry_count,
        "unconfirmed_count": unconfirmed_count,
        "unconfirmed_ids": unconfirmed_ids,
        "shared_ids": shared_ids,
        "current_stale": current_stale,
        "fixed": fixed,
        "reconstructions_pending": pending_reconstructions,
        "strays": strays["strays"],
        "strays_looked": strays["looked"],
        "worktree_ledger_at_risk": at_risk,
        "text": "\n".join(lines),
    }
