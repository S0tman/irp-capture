from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: no advisory locking, the lock below is a no-op
    fcntl = None  # type: ignore


def ensure_irp_dir(project_root: Path) -> Path:
    irp_dir = project_root / ".irp"
    irp_dir.mkdir(exist_ok=True)

    ledger_file = irp_dir / "ledger.jsonl"
    current_file = irp_dir / "current.json"

    if not ledger_file.exists():
        ledger_file.write_text("", encoding="utf-8")

    if not current_file.exists():
        current_file.write_text(
            json.dumps({"version": 1, "active": []}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return irp_dir


def read_current(irp_dir: Path) -> dict[str, Any]:
    raw = (irp_dir / "current.json").read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else {"version": 1, "active": []}


def write_current(irp_dir: Path, data: dict[str, Any]) -> None:
    (irp_dir / "current.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def append_ledger_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    with (irp_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _ensure_lock_ignored(irp_dir: Path) -> None:
    """Keep .irp/.lock out of projects that commit .irp/: make sure .irp/.gitignore lists it. Other lines in an
    existing .irp/.gitignore are left exactly as they are; .lock is added once. Failure to write is ignored (a
    read-only folder can't hold a lock either)."""
    path = irp_dir / ".gitignore"
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if any(line.strip() in (".lock", "/.lock") for line in text.splitlines()):
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n") + ".lock\n")
        else:
            path.write_text(".lock\n", encoding="utf-8")
    except OSError:
        pass


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
    for line in path.read_text(encoding="utf-8").splitlines():
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
    with irp_lock(irp_dir):
        with (irp_dir / RECONSTRUCTIONS_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    for raw in path.read_text(encoding="utf-8").splitlines():
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            # mkstemp makes the file owner-only; keep the original's permissions.
            os.chmod(tmp_name, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp_name, path)
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
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def append_craft_entry(irp_dir: Path, entry: dict[str, Any]) -> None:
    """Append a single entry to .irp/craft.jsonl."""
    with (irp_dir / "craft.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_craft_id(craft_entries: list[dict[str, Any]]) -> str:
    """Return the next sequential CRAFT-YYYY-MM-DD-NNN id for today."""
    today = date.today().isoformat()
    todays = [x for x in craft_entries if str(x.get("timestamp", "")).startswith(today)]
    seq = len(todays) + 1
    return f"CRAFT-{today}-{seq:03d}"
