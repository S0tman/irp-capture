from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: no advisory locking, the lock below is a no-op
    fcntl = None  # type: ignore


# ── finding the project ───────────────────────────────────────────────────────
#
# IRP finds its project the way git finds .git: the current folder, then each
# folder above it, up to the top of the git repo. The finder only reads. It
# never writes, never runs git or any other program and never lists a folder
# other than a .irp/ it has found. Only `irp init` (init_project below) creates
# a .irp/ folder.

_DEFAULT_CURRENT = {"version": 1, "active": []}
_STRAY_FILES = {"ledger.jsonl", "current.json", ".gitignore", ".lock"}
_LOCK_LINES = {".lock", "/.lock"}


def _has_text(path: Path) -> bool:
    """True when the file holds anything other than whitespace."""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                return False
            if chunk.strip():
                return True


def classify_irp_folder(irp_dir: Path) -> str:
    """Say what a .irp/ folder is: "project", "empty_stray" or "not_irp".

    - not_irp: it holds neither ledger.jsonl nor current.json.
    - empty_stray: it holds only an empty ledger.jsonl, a missing, blank or
      default current.json (compared as JSON, so spacing doesn't matter), a
      .gitignore listing only .lock and an empty .lock. Older IRP versions left
      these wherever a command ran.
    - project: anything else that holds a ledger.jsonl or current.json.
    """
    try:
        entries = {entry.name: entry for entry in os.scandir(irp_dir)}
    except OSError:
        return "not_irp"
    if "ledger.jsonl" not in entries and "current.json" not in entries:
        return "not_irp"
    try:
        for name, entry in entries.items():
            if name not in _STRAY_FILES or not entry.is_file():
                return "project"
        if "ledger.jsonl" in entries and _has_text(irp_dir / "ledger.jsonl"):
            return "project"
        if "current.json" in entries:
            text = (irp_dir / "current.json").read_text(encoding="utf-8")
            if text.strip():
                try:
                    if json.loads(text) != _DEFAULT_CURRENT:
                        return "project"
                except ValueError:
                    return "project"
        if ".gitignore" in entries:
            lines = (irp_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
            if any(line.strip() and line.strip() not in _LOCK_LINES for line in lines):
                return "project"
        if ".lock" in entries and entries[".lock"].stat().st_size:
            return "project"
    except (OSError, UnicodeDecodeError):
        return "project"
    return "empty_stray"


class Project:
    """The project a command works on, and how it was found.

    root            the folder holding .irp/
    source          "--project", "IRP_PROJECT_ROOT" or "walk"
    main_checkout   the main checkout, when the project was found there from a
                    linked worktree (the worktree rule)
    worktree_root   then, the folder in the worktree that matches root: where
                    output files go by default
    worktree_of     the main checkout, when root itself sits in a linked
                    worktree (its ledger goes when the worktree is removed,
                    unless git tracks it)
    skipped_strays  empty strays passed on the way up, nearest first
    outer_projects  other projects further up in the same repo, nearest first,
                    then any in the main checkout (main_checkout_projects)
    """

    def __init__(
        self,
        root: Path,
        source: str,
        *,
        main_checkout: Path | None = None,
        worktree_root: Path | None = None,
        worktree_of: Path | None = None,
        skipped_strays: list[Path] | None = None,
        outer_projects: list[Path] | None = None,
        main_checkout_projects: list[Path] | None = None,
    ) -> None:
        self.root = root
        self.irp_dir = root / ".irp"
        self.source = source
        self.main_checkout = main_checkout
        self.worktree_root = worktree_root
        self.worktree_of = worktree_of
        self.skipped_strays = list(skipped_strays or [])
        self.main_checkout_projects = list(main_checkout_projects or [])
        self.outer_projects = list(outer_projects or []) + [
            p for p in self.main_checkout_projects if p not in (outer_projects or [])]

    def __repr__(self) -> str:
        return f"Project(root={str(self.root)!r}, source={self.source!r})"


class ProjectNotFound(Exception):
    """No IRP project where the command looked.

    searched_from  where the search started (the named path for --project or
                   IRP_PROJECT_ROOT)
    stopped_at     the last folder looked at: the git top folder, the home
                   folder, the top of the disk or the named path
    stop_reason    "git", "home", "root" or "named"
    named_by       None, "--project" or "IRP_PROJECT_ROOT"
    main_checkout  the main checkout when the search ran in a linked worktree
    main_from      where the search carried on in the main checkout
    unknown_main   True in a linked worktree whose main checkout can't be
                   found (a bare repo's, or a repo made with
                   --separate-git-dir): the hint then names no folder, so it
                   never suggests a ledger inside the worktree
    """

    def __init__(
        self,
        searched_from: Path,
        stopped_at: Path,
        *,
        stop_reason: str,
        named_by: str | None = None,
        main_checkout: Path | None = None,
        main_from: Path | None = None,
        unknown_main: bool = False,
    ) -> None:
        self.searched_from = searched_from
        self.stopped_at = stopped_at
        self.stop_reason = stop_reason
        self.named_by = named_by
        self.main_checkout = main_checkout
        self.main_from = main_from
        self.unknown_main = unknown_main
        super().__init__(self.read_message())

    # ── what to suggest ──────────────────────────────────────────────────────

    def init_target(self) -> str:
        """The folder the `irp init` hint names. Never the home folder or a linked worktree."""
        if self.unknown_main:
            return "PATH/TO/PROJECT"
        if self.named_by:
            target = self.main_from if self.main_from is not None else self.searched_from
        elif self.main_checkout is not None:
            target = self.main_checkout
        elif self.stop_reason == "git":
            target = self.stopped_at
        else:
            target = self.searched_from
        home = _home()
        if home is not None and target == home:
            return "PATH/TO/PROJECT"
        return str(target)

    @property
    def hint(self) -> str:
        return f"irp init {self.init_target()}"

    def _stop_text(self) -> str:
        if self.stop_reason == "git" and self.unknown_main:
            return "a linked worktree whose main checkout IRP couldn't find"
        if self.stop_reason == "git":
            return "the top of this git repo"
        if self.stop_reason == "home":
            return "your home folder, which is never used as a project"
        return "the top of the disk"

    # ── messages ─────────────────────────────────────────────────────────────

    def _named_message(self) -> str:
        return (f"{self.named_by} points at {self.searched_from}, but there's no IRP project there. "
                f"Start one with: {self.hint}")

    def _hint_lines(self, command: str) -> list[str]:
        if self.main_checkout is not None:
            labels = ("Start one in the main checkout:", "Use another one:")
            other = f"irp --project {self.main_checkout}/PATH {command}"
        else:
            labels = ("Start one:", "Use another one:")
            other = f"irp --project PATH {command}"
        width = max(len(label) for label in labels) + 2
        return [labels[0].ljust(width) + self.hint, labels[1].ljust(width) + other]

    def read_message(self, command: str = "why") -> str:
        """No project, for a read or output command (for a named path, says the path)."""
        if self.named_by:
            return self._named_message()
        if self.main_checkout is not None:
            first = (f"No IRP project found. Looked in {self.searched_from} and the folders above it, "
                     f"up to {self.stopped_at} (a linked worktree of {self.main_checkout}), "
                     f"then in {self.main_from} and up to {self.main_checkout}.")
        else:
            first = (f"No IRP project found. Looked in {self.searched_from} and the folders above it, "
                     f"up to {self.stopped_at} ({self._stop_text()}).")
        return "\n".join([first] + self._hint_lines(command))

    def write_message(self, command: str = "capture") -> str:
        """No project, for a write command: nothing was written (for a named path, says the path)."""
        if self.named_by:
            return self._named_message()
        if self.main_checkout is not None:
            first = (f"Nothing was written. No IRP project found from {self.searched_from} "
                     f"(looked up to {self.stopped_at}, a linked worktree of {self.main_checkout}, "
                     f"then in {self.main_from} up to {self.main_checkout}).")
        else:
            first = (f"Nothing was written. No IRP project found from {self.searched_from} "
                     f"(looked up to {self.stopped_at}, {self._stop_text()}).")
        return "\n".join([first] + self._hint_lines(command))

    def json_payload(self) -> dict[str, Any]:
        """The no-project status for --json output."""
        return {
            "status": "no_project",
            "searched_from": str(self.searched_from),
            "stopped_at": str(self.stopped_at),
            "hint": self.hint,
        }


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def _real(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path)))).resolve()


def _is_project(folder: Path) -> bool:
    irp_dir = folder / ".irp"
    try:
        return irp_dir.is_dir() and classify_irp_folder(irp_dir) == "project"
    except OSError:
        return False        # a folder we can't read holds no project we can use


def git_top_folder(path: Path) -> Path | None:
    """The first folder at or above `path` that holds .git (a folder or a file)."""
    for folder in (path, *path.parents):
        if os.path.lexists(folder / ".git"):
            return folder
    return None


def _gitdir_of(git_top: Path) -> Path | None:
    """The folder a .git file points at (linked worktrees and submodules), or None."""
    dotgit = git_top / ".git"
    try:
        if not dotgit.is_file():
            return None
        first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(first[len("gitdir:"):].strip())
    return gitdir if gitdir.is_absolute() else git_top / gitdir


def is_linked_worktree(git_top: Path) -> bool:
    """True when `git_top` is a linked worktree: its .git file leads to a git
    folder holding `commondir`. A submodule's doesn't. Reads two plain files."""
    gitdir = _gitdir_of(git_top)
    return gitdir is not None and (gitdir / "commondir").is_file()


def main_checkout_of(git_top: Path) -> Path | None:
    """The main checkout when `git_top` is a linked worktree of an ordinary repo.

    Reads the worktree's .git file and the `commondir` file it leads to, as
    plain files. A submodule has no `commondir`; a worktree of a bare repo
    leads to something like `repo.git`. Both give None.
    """
    gitdir = _gitdir_of(git_top)
    if gitdir is None:
        return None
    commondir_file = gitdir / "commondir"
    try:
        if not commondir_file.is_file():
            return None
        common = Path(commondir_file.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return None
    if not common.is_absolute():
        common = gitdir / common
    common = _real(common)
    if common.name != ".git" or not common.is_dir():
        return None
    return common.parent


def _outer_projects(found_at: Path, home: Path | None) -> list[Path]:
    """Real projects above `found_at`, up to where a walk would stop."""
    outer: list[Path] = []
    if os.path.lexists(found_at / ".git"):
        return outer
    for folder in found_at.parents:
        if home is not None and folder == home:
            break
        if _is_project(folder):
            outer.append(folder)
        if os.path.lexists(folder / ".git"):
            break
    return outer


def _linked_worktree_of(folder: Path, home: Path | None) -> tuple[Path, Path] | None:
    """(worktree top, main checkout) when `folder` sits in a linked worktree of an ordinary repo."""
    for candidate in (folder, *folder.parents):
        if home is not None and candidate == home:
            return None
        if os.path.lexists(candidate / ".git"):
            main = main_checkout_of(candidate)
            return (candidate, main) if main is not None else None
    return None


def _main_checkout_projects(folder: Path, worktree_top: Path, main: Path, home: Path | None) -> list[Path]:
    """Projects at the same relative path in the main checkout and above it, up to its top."""
    found: list[Path] = []
    mapped = main / folder.relative_to(worktree_top)
    for candidate in (mapped, *mapped.parents):
        if home is not None and candidate == home:
            break
        if _is_project(candidate):
            found.append(candidate)
        if candidate == main:
            break
    return found


def _found_at(folder: Path, source: str, home: Path | None, *, outer: list[Path],
              skipped: list[Path] | None = None) -> Project:
    """A project found right where it is. In a linked worktree, it also notes
    the main checkout and any project there, since a ledger inside a worktree
    goes when the worktree is removed."""
    linked = _linked_worktree_of(folder, home)
    if linked is None:
        return Project(folder, source, skipped_strays=skipped, outer_projects=outer)
    worktree_top, main = linked
    return Project(folder, source, worktree_of=main, skipped_strays=skipped, outer_projects=outer,
                   main_checkout_projects=_main_checkout_projects(folder, worktree_top, main, home))


def walk_up(start: Path) -> Project:
    """Walk up from `start` to the nearest project. Reads only."""
    start = _real(start)
    home = _home()
    skipped: list[Path] = []
    folder = start
    while True:
        if home is not None and folder == home:
            raise ProjectNotFound(start, folder, stop_reason="home")
        irp_dir = folder / ".irp"
        if irp_dir.is_dir():
            kind = classify_irp_folder(irp_dir)
            if kind == "project":
                return _found_at(folder, "walk", home, outer=_outer_projects(folder, home),
                                 skipped=skipped)
            if kind == "empty_stray":
                skipped.append(folder)
        if os.path.lexists(folder / ".git"):
            break
        if folder.parent == folder:
            raise ProjectNotFound(start, folder, stop_reason="root")
        folder = folder.parent

    # The walk stopped at a git top folder. In a linked worktree, try the same
    # relative path in the main checkout, up to its top.
    git_top = folder
    main = main_checkout_of(git_top)
    if main is None:
        raise ProjectNotFound(start, git_top, stop_reason="git",
                              unknown_main=is_linked_worktree(git_top))
    main_from = main / start.relative_to(git_top)
    for candidate in (main_from, *main_from.parents):
        if home is not None and candidate == home:
            break
        irp_dir = candidate / ".irp"
        if irp_dir.is_dir():
            kind = classify_irp_folder(irp_dir)
            if kind == "project":
                return Project(candidate, "walk", main_checkout=main, skipped_strays=skipped,
                               worktree_root=git_top / candidate.relative_to(main),
                               outer_projects=_outer_projects(candidate, home))
            if kind == "empty_stray":
                skipped.append(candidate)
        if candidate == main:
            break
    raise ProjectNotFound(start, git_top, stop_reason="git", main_checkout=main, main_from=main_from)


def _named(path: Path | str, named_by: str) -> Project:
    """A path given with --project or IRP_PROJECT_ROOT. Strict: no walking up."""
    folder = _real(path)
    if _is_project(folder):
        return _found_at(folder, named_by, _home(), outer=[])
    git_top = git_top_folder(folder)
    main = main_checkout_of(git_top) if git_top is not None else None
    if main is not None and git_top is not None:
        mapped = main / folder.relative_to(git_top)
        if _is_project(mapped):
            return Project(mapped, named_by, main_checkout=main, worktree_root=folder)
        raise ProjectNotFound(folder, folder, stop_reason="named", named_by=named_by,
                              main_checkout=main, main_from=mapped)
    raise ProjectNotFound(folder, folder, stop_reason="named", named_by=named_by,
                          unknown_main=git_top is not None and is_linked_worktree(git_top))


def find_named_project(path: Path | str, named_by: str) -> Project:
    """The project at `path` and nowhere else (no walking up).

    In a linked worktree with no project at `path`, the same path in the main
    checkout is checked (the worktree rule). `guard run --hook-project` uses
    this. Raises ProjectNotFound when neither holds a project.
    """
    return _named(path, named_by)


def find_project(start: Path | str | None = None, project: Path | str | None = None) -> Project:
    """Find the project a command works on.

    Order: `project` (the --project flag), then IRP_PROJECT_ROOT, then walking
    up from `start` (default: the current folder). A named path with no
    project raises ProjectNotFound with no fallback. Never creates anything.
    """
    if project is not None and str(project) != "":
        return _named(project, "--project")
    env = os.environ.get("IRP_PROJECT_ROOT")
    if env:
        return _named(env, "IRP_PROJECT_ROOT")
    return walk_up(Path.cwd() if start is None else Path(start))


def init_project(folder: Path | str) -> Path:
    """Start a project in `folder` and return its .irp/ folder.

    Creates only what's missing: ledger.jsonl, current.json, config.json and a
    .gitignore holding .lock. An existing file is never changed, so running it
    on a started project changes nothing and an empty stray keeps its files.
    """
    irp_dir = Path(folder) / ".irp"
    if os.path.lexists(irp_dir) and irp_dir.is_dir() and any(irp_dir.iterdir()) \
            and classify_irp_folder(irp_dir) == "not_irp":
        raise FileExistsError(f"{irp_dir} already exists and IRP didn't make it")
    irp_dir.mkdir(exist_ok=True)
    defaults = {
        "ledger.jsonl": "",
        "current.json": json.dumps(_DEFAULT_CURRENT, indent=2, ensure_ascii=False),
        "config.json": json.dumps(_CONFIG_DEFAULTS, indent=2, ensure_ascii=False),
        ".gitignore": ".lock\n",
    }
    for name, text in defaults.items():
        path = irp_dir / name
        if not os.path.lexists(path):
            path.write_text(text, encoding="utf-8")
    return irp_dir


def read_current(irp_dir: Path) -> dict[str, Any]:
    raw = (irp_dir / "current.json").read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else {"version": 1, "active": []}


def write_current(irp_dir: Path, data: dict[str, Any]) -> None:
    """Replace current.json whole: temp file in the same folder, fsync, os.replace, folder fsync.
    A crash leaves either the old file or the new one, never half of each."""
    irp_dir = Path(irp_dir)
    text = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    tmp = irp_dir / f"current.json.tmp-{os.getpid()}-{time.monotonic_ns()}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        try:
            view = memoryview(text)
            done = 0
            while done < len(text):
                done += os.write(fd, view[done:])
            _sync_fd(fd)
        finally:
            os.close(fd)
        os.replace(tmp, irp_dir / "current.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _sync_dir(irp_dir)


def append_ledger_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    """Append one decision line to .irp/ledger.jsonl, flushed to disk.

    Refuses (TornTailError) when the file ends in a half-written line.
    """
    durable_append(irp_dir / "ledger.jsonl", json.dumps(entry, ensure_ascii=False))


def next_irp_id(ledger: list[dict[str, Any]]) -> str:
    """Return the next sequential IRP-YYYY-MM-DD-NNN id for today.

    The sequence is counted from existing *ids* carrying today's prefix, not
    from entries whose `timestamp` is today. Those are not the same thing: a
    record may legitimately carry a backdated timestamp, the date the decision
    was actually taken, while being captured today. Counting timestamps meant
    such a record never incremented the counter, so the next capture reused
    the same number and duplicate ids appeared in an append-only ledger.
    Seeding one project's ledger with true decision dates produced fourteen
    records sharing IRP-2026-07-26-001.

    The ledger passed in must be the raw one (`read_ledger`), never
    `confirmed_only`: a legacy bootstrap guess still owns its id.

    Taking max + 1 rather than a count also survives gaps, so a ledger that
    has had an entry removed by hand still allocates a fresh id rather than
    colliding with the highest one already present.
    """
    today = date.today().isoformat()
    prefix = f"IRP-{today}-"

    used: list[int] = []
    for entry in ledger:
        entry_id = str(entry.get("id", ""))
        if not entry_id.startswith(prefix):
            continue
        suffix = entry_id[len(prefix):]
        if suffix.isdigit():
            used.append(int(suffix))

    seq = max(used) + 1 if used else 1
    return f"IRP-{today}-{seq:03d}"


def rebuild_current(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive current.json from the ledger: the last 10 confirmed decision entries.

    Unconfirmed bootstrap guesses (see `is_unconfirmed`) are left out: current.json
    is what agents and the guard read, and a guess nobody confirmed is not a
    decision.
    """
    active = [x for x in confirmed_only(ledger) if x.get("type") == "decision"]
    return {"version": 1, "active": active[-10:]}


# ── unconfirmed entries (legacy bootstrap guesses) ────────────────────────────
#
# `irp bootstrap` used to append guesses from git history and documents straight
# to ledger.jsonl, flagged `bootstrapped: true`. It no longer does: guesses now
# wait in .irp/reconstructions.jsonl until a person accepts them. But a ledger
# is append-only, so lines written by older versions are still there, and every
# reader has to decide what to do with them. Those lines are "unconfirmed":
# nobody ever said they were decisions.
#
# Readers that speak to an agent or an auditor, or that enforce decisions, skip
# them with `confirmed_only`. Human views may show them, labelled
# UNCONFIRMED_LABEL. `read_ledger` and `next_irp_id` deliberately see every
# line, so a new capture can never reuse a guess's id.

UNCONFIRMED_LABEL = "unconfirmed (bootstrap guess)"


def is_unconfirmed(entry: Any) -> bool:
    """True for a legacy bootstrap guess: a ledger line flagged `bootstrapped: true`."""
    return isinstance(entry, dict) and entry.get("bootstrapped") is True


def confirmed_only(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries minus unconfirmed bootstrap guesses, order preserved."""
    return [e for e in entries if not is_unconfirmed(e)]


def _is_decision_row(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "decision" or (
        not entry.get("type") and bool(entry.get("what")) and bool(entry.get("why"))
    )


def shared_unconfirmed_ids(ledger: list[dict[str, Any]]) -> set[str]:
    """Ids carried by both an unconfirmed guess and a confirmed decision.

    Up to v0.7.0 a bootstrap guess and a same-day capture could be given the same
    IRP id. Retiring or superseding such an id by id would hit the confirmed
    decision too, so callers refuse to. A retirement event shares its target's id
    by design and is not a decision, so it never counts here.
    """
    guesses = {e.get("id") for e in ledger if is_unconfirmed(e) and _is_decision_row(e) and e.get("id")}
    real = {e.get("id") for e in ledger if not is_unconfirmed(e) and _is_decision_row(e) and e.get("id")}
    return {i for i in guesses & real if i}


def decision_rows_for_id(
    ledger: list[dict[str, Any]], entry_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The decision rows carrying this id, as (confirmed, unconfirmed guesses).

    Only decision rows count. A retirement event shares its target's id and is
    not bootstrapped, so a lookup that merely drops guesses would hand back the
    retirement of a retired guess as if it were the confirmed decision. Lookups by
    id (`irp why --id`, the MCP irp_why tool, GET /decisions/{id}) use this and
    show a confirmed decision if there is one, else (humans only) the labelled
    guess.
    """
    rows = [e for e in ledger if e.get("id") == entry_id and _is_decision_row(e)]
    return confirmed_only(rows), [e for e in rows if is_unconfirmed(e)]


def read_confirmed_ledger(irp_dir: Path) -> list[dict[str, Any]]:
    """read_ledger without the unconfirmed guesses. For agent-facing readers."""
    return confirmed_only(read_ledger(irp_dir))


def read_active(irp_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """The active decisions as current.json lists them, and whether it was stale.

    An old `irp bootstrap` could leave current.json full of guesses, pushing real
    decisions out of its last-10 window. Dropping the guesses from that list would
    leave readers with too few decisions, or none. So when current.json holds any
    guess, the list is recomputed from the ledger (in memory; nothing is written)
    and the second value is True. A project with only a current.json and no
    decisions in its ledger has nothing to recompute from, so it is filtered
    instead.
    """
    listed = read_current(irp_dir).get("active", [])
    if not any(is_unconfirmed(e) for e in listed):
        return listed, False
    ledger = read_ledger(irp_dir)
    if any(e.get("type") == "decision" for e in ledger):
        return rebuild_current(ledger)["active"], True
    return confirmed_only(listed), True


def read_ledger(irp_dir: Path) -> list[dict[str, Any]]:
    path = irp_dir / "ledger.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ── advisory lock ─────────────────────────────────────────────────────────────
#
# Two `irp bootstrap` runs (or a run and an accept) can overlap. Without a lock
# they can hand out the same REC id, and the read-modify-replace in
# update_reconstruction can drop a line another process appended meanwhile. The
# lock is advisory (flock on .irp/.lock), re-entrant within a thread, and a no-op
# where fcntl is missing. Plain captures do not take it.

_lock_state = threading.local()

# errno values a filesystem answers when it does not support flock at all.
_FLOCK_UNSUPPORTED = {
    getattr(errno, name)
    for name in ("ENOTSUP", "EOPNOTSUPP", "ENOLCK", "EINVAL", "ENOSYS")
    if hasattr(errno, name)
}


def _may_hold_record(torn: bytes) -> bool:
    """True when some '{' inside the torn bytes starts a complete JSON object up to the end: the legacy damage
    where a whole line was written behind a half line. Such a record is NOT restored automatically (cutting at
    a guessed brace could split a half line's own text); it stays in the side file for a person to read."""
    body = torn.strip()
    start = body.find(b"{", 1)
    tries = 0
    while start != -1 and tries < 256:
        if _is_json_object(body[start:]):
            return True
        start = body.find(b"{", start + 1)
        tries += 1
    return False


def _ensure_ignored(irp_dir: Path, pattern: str) -> None:
    """Add `pattern` to .irp/.gitignore if no line already says it. Other lines are left as they are."""
    path = irp_dir / ".gitignore"
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if any(line.strip() in (pattern, "/" + pattern) for line in text.splitlines()):
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n") + pattern + "\n")
        else:
            path.write_text(pattern + "\n", encoding="utf-8")
    except OSError:
        pass


def _ensure_lock_ignored(irp_dir: Path) -> None:
    """Keep .irp/.lock out of projects that commit .irp/. Failure to write is ignored (a read-only folder can't
    hold a lock either)."""
    _ensure_ignored(irp_dir, ".lock")


@contextmanager
def irp_lock(irp_dir: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    held = getattr(_lock_state, "held", None)
    if held is None:
        held = _lock_state.held = {}
    key = os.path.realpath(str(irp_dir))
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    _ensure_lock_ignored(irp_dir)
    try:
        fd = os.open(str(irp_dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # Cannot even create the lock file (read-only folder): the write that
        # follows would fail on its own, so do not add a second error here.
        yield
        return
    locked = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
    except OSError as exc:
        os.close(fd)
        if exc.errno not in _FLOCK_UNSUPPORTED:
            raise
    if not locked:
        # This mount rejects flock (NFS without a lock daemon, some FUSE mounts):
        # run unlocked, as when the lock file cannot be opened. There is no
        # LOCK_UN to call because there was no lock.
        yield
        return
    try:
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)



# ── durable appends ───────────────────────────────────────────────────────────
#
# An append used to be open("a") plus write, with no flush to disk. A hard reset
# could leave half a line at the end of the file, and the next capture then wrote
# behind it, which broke every later read of that line and stopped checkpoints
# until someone repaired the file by hand. Now:
#
# - each append is ONE os.write on an O_APPEND descriptor, under the advisory lock
#   where the filesystem has one, followed by fsync (F_FULLFSYNC on macOS), and by
#   an fsync of the folder when the file was just created. What this guarantees:
#   with the lock, writers take turns. On a local filesystem the single O_APPEND
#   write also keeps lines apart without the lock. On 9p, network or FUSE mounts,
#   or where flock is refused, nothing here promises that two writers at once
#   cannot interleave, and a mount can ignore fsync; the torn-tail check is the
#   safety net, not a guarantee;
# - before writing, the file's tail is checked. A last line with no newline, or
#   one that is not a JSON object, means an earlier crash: the writer refuses
#   (TornTailError) and changes nothing;
# - if a write fails part-way, the file is cut back to its old length;
# - `irp doctor --fix-tail` (fix_torn_tail) copies the torn bytes to a side file,
#   flushes that, and only then truncates. No byte is ever deleted.


class TornTailError(OSError):
    """The file ends in a half-written line. Nothing was added."""

    def __init__(self, path: Path, tail: "TornTail"):
        self.path = Path(path)
        self.tail = tail
        name = self.path.name
        super().__init__(
            f"{name} ends in a half-written line ({tail.describe()}), probably left by a crash or power cut. "
            f"Nothing was added. Move the half line aside with: irp doctor --fix-tail "
            f"(it keeps the bytes in a side file), then try again."
        )


class DurabilityError(OSError):
    """The write went through but the disk did not confirm it."""


class TornTail:
    """What torn_tail found: `kind` is 'unterminated' or 'invalid', `offset` is where the torn bytes start."""

    __slots__ = ("kind", "offset", "size")

    def __init__(self, kind: str, offset: int, size: int):
        self.kind, self.offset, self.size = kind, offset, size

    @property
    def bytes(self) -> int:
        return self.size - self.offset

    def describe(self) -> str:
        what = "no line break at the end" if self.kind == "unterminated" else "the last line is not valid JSON"
        return f"{what}, {self.bytes} bytes"


_TAIL_CHUNK = 65536


def _is_json_object(raw: bytes) -> bool:
    try:
        return isinstance(json.loads(raw.decode("utf-8")), dict)
    except (UnicodeDecodeError, ValueError):
        return False


def torn_tail(path: Path) -> "TornTail | None":
    """None when the file is absent, empty, or ends in a complete JSON line; else what is wrong with its end.

    Reads backwards from the end, so the cost follows the length of the last line, not of the file.
    A bad line in the middle is not a torn tail (readers skip it), and trailing blank lines or
    spaces are fine.
    """
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return None
    with f:
        size = os.fstat(f.fileno()).st_size
        if size == 0:
            return None
        f.seek(size - 1)
        ends_with_newline = f.read(1) == b"\n"
        # Find the last non-blank line: read back until a line break precedes it.
        data = b""
        pos = size
        while True:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
            body = data.rstrip(b" \t\r\n")
            cut = body.rfind(b"\n")
            if cut != -1 or pos == 0:
                break
        last_break = data.rfind(b"\n")
        blank_tail = not data[last_break + 1:].strip(b" \t\r")
        if not ends_with_newline and not blank_tail:
            # Everything after the last line break is unfinished (fix_torn_tail adds just the
            # newline when that text turns out to be a whole JSON object).
            return TornTail("unterminated", pos + last_break + 1, size)
        if not body:
            return None
        line_start = pos + cut + 1  # cut == -1 at pos 0 gives 0
        last = body[cut + 1:]
        if _is_json_object(last):
            return None
        return TornTail("invalid", line_start, size)


def _sync_fd(fd: int) -> None:
    """Push a file's data to the disk. F_FULLFSYNC where the platform has it (macOS fsync alone can stay in the drive cache)."""
    full = getattr(fcntl, "F_FULLFSYNC", None) if fcntl is not None else None
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass  # this filesystem does not support it: fall through to plain fsync
    os.fsync(fd)


def _sync_dir(directory: Path) -> None:
    """fsync a folder so a new or renamed file's entry survives a reset. Skipped where folders cannot be opened (Windows)."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        full = getattr(fcntl, "F_FULLFSYNC", None) if fcntl is not None else None
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
                return
            except OSError:
                pass  # not supported on this folder: plain fsync below
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EISDIR, errno.EACCES):
                raise
    finally:
        os.close(fd)


def durable_append(path: Path, line: str) -> None:
    """Append `line` (no newline) to a .jsonl file, flushed to disk, refusing after a torn tail."""
    path = Path(path)
    data = line.encode("utf-8") + b"\n"
    with irp_lock(path.parent):
        tail = torn_tail(path)
        if tail is not None:
            raise TornTailError(path, tail)
        created = not os.path.lexists(path)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(path), flags, 0o666)
        try:
            start = os.fstat(fd).st_size
            written = 0
            try:
                while written < len(data):
                    try:
                        written += os.write(fd, data[written:])
                    except InterruptedError:
                        continue
            except BaseException:
                _roll_back(fd, start, data)
                raise
            try:
                _sync_fd(fd)
                if created:
                    _sync_dir(path.parent)
            except OSError as exc:
                raise DurabilityError(
                    f"The entry was written to {path.name} but the disk did not confirm it ({exc}). "
                    f"Check the file before trying again, or the entry may appear twice."
                ) from exc
        finally:
            os.close(fd)


def _roll_back(fd: int, start: int, data: bytes) -> None:
    """Cut a failed append back to its old length, but only if what sits after `start` is a prefix of our own
    bytes (so a writer that got in after us, without the lock, is never cut)."""
    try:
        grown = os.fstat(fd).st_size - start
        if 0 < grown <= len(data):
            if os.pread(fd, grown, start) == data[:grown]:
                os.ftruncate(fd, start)
    except (OSError, AttributeError):
        pass  # the torn-tail check will catch whatever is left


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def fix_torn_tail(irp_dir: Path, name: str = "ledger.jsonl") -> dict[str, Any]:
    """Repair a torn tail without deleting a byte.

    Whole JSON with only the newline missing: add the newline. Anything else: copy the torn bytes to
    `<stem>.torn-<UTC time>.jsonl` (never over an existing file), flush it and its folder, then truncate
    the file back to the last complete line. Returns what was done.
    """
    path = Path(irp_dir) / name
    with irp_lock(Path(irp_dir)):
        tail = torn_tail(path)
        if tail is None:
            return {"file": name, "status": "clean"}
        with open(path, "rb") as f:
            f.seek(tail.offset)
            torn = f.read()
        if tail.kind == "unterminated" and _is_json_object(torn):
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
            try:
                os.write(fd, b"\n")
                _sync_fd(fd)
            finally:
                os.close(fd)
            return {"file": name, "status": "newline-added", "bytes": 0}
        stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else name
        stamp = _utc_stamp()
        side = Path(irp_dir) / f"{stem}.torn-{stamp}.jsonl"
        n = 1
        while True:
            try:
                sfd = os.open(str(side), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                break
            except FileExistsError:
                n += 1
                side = Path(irp_dir) / f"{stem}.torn-{stamp}-{n}.jsonl"
        try:
            view = memoryview(torn)
            done = 0
            while done < len(torn):
                done += os.write(sfd, view[done:])
            _sync_fd(sfd)
        finally:
            os.close(sfd)
        _sync_dir(Path(irp_dir))
        # The bytes are safe in the side file. Only now shrink the original.
        fd = os.open(str(path), os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.ftruncate(fd, tail.offset)
            _sync_fd(fd)
        finally:
            os.close(fd)
        _ensure_ignored(Path(irp_dir), "*.torn-*.jsonl")
        return {"file": name, "status": "quarantined", "bytes": len(torn), "side_file": side.name,
                "may_hold_record": _may_hold_record(torn)}


# ── reconstructions ───────────────────────────────────────────────────────────
#
# .irp/reconstructions.jsonl holds what `irp bootstrap` guessed from git history
# and documents. It is a working file, not part of the ledger: nothing in it has
# been confirmed by a person, and nothing reads it as a decision. A line moves
# into ledger.jsonl only through `irp bootstrap --accept REC-...`.

RECONSTRUCTIONS_FILE = "reconstructions.jsonl"


def read_reconstructions(irp_dir: Path) -> list[dict[str, Any]]:
    path = irp_dir / RECONSTRUCTIONS_FILE
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append_reconstruction(irp_dir: Path, entry: dict[str, Any]) -> None:
    durable_append(irp_dir / RECONSTRUCTIONS_FILE, json.dumps(entry, ensure_ascii=False))


def next_rec_id(rows: list[dict[str, Any]], ledger: list[dict[str, Any]] | None = None) -> str:
    """Return the next REC-YYYY-MM-DD-NNN id for today.

    Same rule as next_irp_id: max + 1 over the ids that carry today's prefix, so
    gaps never cause a number to be reused.

    Pass the ledger too. A REC id that was accepted lives on in the ledger as
    `reconstructed_from`, and reconstructions.jsonl can be deleted (that is how
    guesses are discarded) or be missing in another clone. Counting only the
    file would then hand the same id out again.
    """
    prefix = f"REC-{date.today().isoformat()}-"
    seen = [str(row.get("id", "")) for row in rows]
    seen += [str(e.get("reconstructed_from", "")) for e in (ledger or [])]
    used: list[int] = []
    for row_id in seen:
        if row_id.startswith(prefix) and row_id[len(prefix):].isdigit():
            used.append(int(row_id[len(prefix):]))
    return f"{prefix}{(max(used) + 1 if used else 1):03d}"


def update_reconstruction(irp_dir: Path, rec_id: str, changes: dict[str, Any]) -> bool:
    """Merge `changes` into the line with this REC id and rewrite the file atomically.

    The new file is written beside the old one, flushed to disk, then swapped in
    with os.replace, so a crash leaves either the old file or the new one, never
    half of each. Lines that are not valid JSON are carried over untouched.
    Returns False (and writes nothing) when no line has that id.
    """
    with irp_lock(irp_dir):
        return _update_reconstruction_locked(irp_dir, rec_id, changes)


def _update_reconstruction_locked(irp_dir: Path, rec_id: str, changes: dict[str, Any]) -> bool:
    path = irp_dir / RECONSTRUCTIONS_FILE
    if not path.exists():
        return False
    out: list[str] = []
    found = False
    for raw in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            out.append(raw)
            continue
        if isinstance(row, dict) and row.get("id") == rec_id and not found:
            row.update(changes)
            found = True
            out.append(json.dumps(row, ensure_ascii=False))
        else:
            out.append(raw)
    if not found:
        return False

    fd, tmp_name = tempfile.mkstemp(dir=str(irp_dir), prefix=".reconstructions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            # mkstemp makes the file owner-only; keep the original's permissions.
            os.chmod(tmp_name, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp_name, path)
        _sync_dir(irp_dir)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


# ── project config ────────────────────────────────────────────────────────────

_CONFIG_DEFAULTS: dict[str, Any] = {
    "control_level": "advanced",
}


def read_config(irp_dir: Path) -> dict[str, Any]:
    """Read .irp/config.json. Missing keys fall back to defaults."""
    path = irp_dir / "config.json"
    if not path.exists():
        return dict(_CONFIG_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = dict(_CONFIG_DEFAULTS)
        result.update(data)
        return result
    except (json.JSONDecodeError, OSError):
        return dict(_CONFIG_DEFAULTS)


def write_config(irp_dir: Path, data: dict[str, Any]) -> None:
    """Write .irp/config.json (full replace)."""
    (irp_dir / "config.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── craft ledger ──────────────────────────────────────────────────────────────

def read_craft(irp_dir: Path) -> list[dict[str, Any]]:
    """Read .irp/craft.jsonl — individual craft knowledge entries."""
    path = irp_dir / "craft.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def append_craft_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    """Append a single entry to .irp/craft.jsonl."""
    durable_append(irp_dir / "craft.jsonl", json.dumps(entry, ensure_ascii=False))


def next_craft_id(craft_entries: list[dict[str, Any]]) -> str:
    """Return the next sequential CRAFT-YYYY-MM-DD-NNN id for today."""
    today = date.today().isoformat()
    todays = [x for x in craft_entries if str(x.get("timestamp", "")).startswith(today)]
    seq = len(todays) + 1
    return f"CRAFT-{today}-{seq:03d}"
