"""Per-ledger state for Roaming IRP: `state.json` (spec v0.3 §14.6a, §18a).

`~/.irp-roam/ledgers/<ledger_id>/local/state.json` holds what the laptop keeps about a ledger that isn't secret
and isn't in a signed log. For rotation (step 2.5c) that's three things:

- `rotation`: the record of the newest rotation, `{rotate_idx, at, suspected, new_ck: {id, pub, not_after},
  old_ck: {id, not_after or "remove"}, quarantine, closed}`. A rotation is open while the live kid was
  introduced by a `device_rotate` at idx r and this file has no closed record for r. `old_ck` is null only in
  a record rebuilt after this file was lost, when the old CK is no longer known;
- `probes`: `{iss, nbf, token}` capabilities from retired CKs that the drill (and from step 3 the publisher)
  sends once `nbf` has passed, expecting a 401. A later rotation adds its own and never removes one;
- `held`: the readers whose renewed identity hasn't been confirmed delivered. The publisher skips them.

For checkpoints (step 2.6, §18a "State") three more, all null in a fresh state:

- `checkpoint`: this laptop's newest made or adopted checkpoint, `{epoch, seq, strand, digest, header, sig,
  created_at, gen_time, label, last_present, snapshot_digest, recipients, policy_digest, tsa_policy_digest,
  logs}`;
- `seen`: the high-water mark from anything fetched, `{epoch, seq, strand, digest, header, sig, devices}`. It
  never goes down;
- `epoch_start`: the epoch whose start this laptop recorded (init, recovery and root_rotate, and Path B when
  the relay has no slot for seq 1 of the current epoch).

And one for `irp roam rotate --now` (owner-approved amendment to §14.6a and §18a), null in a fresh state:

- `fresh_keys_owed`: null, or `{since, from_idx}` while fresh keys are owed. `since` is the §15.1 time the first
  --now run that owes them wrote it; `from_idx` is how many lines the devices log held when the latest --now run
  wrote it, so the fresh rotation is the one whose `device_rotate` line is at `from_idx` or later (a line already
  in the log then held the keys --now suspects). Every --now run (`rotation.rotate` or `resume_rotation` with
  `suspected=True`) writes it before anything else, through the engine's reload-and-replace write. It's cleared
  only in the write that closes the fresh rotation's record (a suspected record at `from_idx` or later), before
  the checkpoint hook runs, whichever run finishes it; finishing an older rotation never clears it, and a plain
  `irp roam rotate` never starts a rotation while it's set (its old CK would keep a 7-day overlap). While it's
  set, `rotation.unfinished`, `check_signer` and `check_publish` refuse ("fresh keys are still owed: run irp
  roam rotate --now"), so no checkpoint is made, nothing is published and nothing is signed.

  A lost state.json loses the marker with it, and nothing on disk remembers it. What keeps that safe: nothing
  signs on a lost state.json (making a checkpoint refuses at step 0 until Path B or recovery rebuilds the
  record, so no publish goes out), a --now run keeps the marker in hand and writes it back with every state
  write it makes and wherever it stops while fresh keys are owed (a file lost while it runs comes back with
  it), and a --now after the loss works on the rebuilt file. Path B (step 3), the one way back short of
  recovery, can't tell from the files whether fresh keys were owed, so it must never read the missing marker as
  "nothing owed": it asks the person, and without a clear answer (a --now run since the loss) it writes the
  marker again, so publishing waits for irp roam rotate --now (fail closed). Recovery, which makes new device
  keys, leaves it null. A second copy that survives the file (in keys/, say) would close this fully; that's
  the owner's call.

Every writer reloads the file under roam.lock held exclusively and replaces only its own keys (`update_state`):
the rotation engine writes `rotation`, `probes`, `held` and `fresh_keys_owed`; making and adopting a checkpoint
write `checkpoint`; only the fetch paths (the drill, Path B) write `seen`.

The `local/` folders (`ledgers/<id>/local/`, and `~/.irp-roam/local/` for `tsa.json`) and `staging/` are
excluded from Time Machine as folders. A folder exclusion survives the files inside it being replaced, so a
restore leaves the state missing rather than stale. Whichever writer creates one applies the exclusion first,
through an injected `exclude_from_backup` (`tmutil_exclude` in production: `tmutil addexclusion`, then
`tmutil isexcluded` to confirm), and refuses to write into it if that fails. The folder is made under a
temporary name, excluded and only then moved into place, so a crash never leaves a folder of that name without
its exclusion.

The file is exact JCS with a closed schema, written like the keystore: a 0600 temp file, fully synced, renamed
into place and the folder synced. A missing file reads as empty (no record, no probes, nothing held, no
checkpoint, no mark, no epoch marker); `load_state(..., required=True)` refuses it instead.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from . import keys as _keys
from . import sig
from .age import AgeError, Recipient

STATE_FILE = "state.json"
TSA_FILE = "tsa.json"
LOCAL_DIR = "local"
STAGING_DIR = "staging"
ROAM_HOME = ".irp-roam"
TMUTIL = "/usr/bin/tmutil"  # never whatever `tmutil` comes first on PATH
STATE_KEYS = frozenset({"rotation", "probes", "held", "checkpoint", "seen", "epoch_start", "fresh_keys_owed"})
ROTATION_KEYS = frozenset({"rotate_idx", "at", "suspected", "new_ck", "old_ck", "quarantine", "closed"})
NEW_CK_KEYS = frozenset({"id", "pub", "not_after"})
OLD_CK_KEYS = frozenset({"id", "not_after"})
PROBE_KEYS = frozenset({"iss", "nbf", "token"})
CHECKPOINT_KEYS = frozenset({"epoch", "seq", "strand", "digest", "header", "sig", "created_at", "gen_time", "label",
                             "last_present", "snapshot_digest", "recipients", "policy_digest", "tsa_policy_digest",
                             "logs"})
SEEN_KEYS = frozenset({"epoch", "seq", "strand", "digest", "header", "sig", "devices"})
LAST_PRESENT_KEYS = frozenset({"epoch", "gen_time", "devices_length", "readers_length"})
OWED_KEYS = frozenset({"since", "from_idx"})
LOG_NAMES = ("ledger", "devices", "readers", "disclosures")
LOG_KEYS = frozenset({"byte_length", "byte_digest", "segments"})
SEGMENT_KEYS = frozenset({"object", "offset", "length", "sha256"})
RECIPIENT_KEYS = frozenset({"id", "recipient"})
LABELS = ("PRESENT", "UNVERIFIED", "NONE")
REMOVE = "remove"
MAX_INT = 2**53 - 1
MAX_TOKEN = 8192

_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_CK = re.compile(r"ck-[0-9a-f]{32}")
_READER = re.compile(r"rd-[0-9a-f]{32}")
_STRAND = re.compile(r"dk-[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OBJECT = re.compile(r"o/[0-9a-f]{64}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")

ExcludeFromBackup = Callable[[Path], None]  # applies and confirms a Time Machine folder exclusion, or raises


class StateError(ValueError):
    """state.json is damaged or outside its schema, or can't be written as asked."""


class StateMissing(StateError):
    """state.json doesn't exist (never written, or a restore left it missing)."""


class BackupExclusionError(StateError):
    """A local/ or staging/ folder couldn't be excluded from Time Machine, so nothing was written into it."""


@dataclass(frozen=True)
class RotationRecord:
    rotate_idx: int
    at: str
    suspected: bool
    new_ck: Mapping[str, str]            # {id, pub, not_after}
    old_ck: Optional[Mapping[str, str]]  # {id, not_after or "remove"}; null when rebuilt after a loss
    quarantine: Tuple[Mapping[str, str], ...] = ()
    closed: bool = False


@dataclass(frozen=True)
class Probe:
    iss: str
    nbf: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class RoamState:
    rotation: Optional[RotationRecord] = None
    probes: Tuple[Probe, ...] = ()
    held: Tuple[str, ...] = ()
    checkpoint: Optional[Mapping[str, Any]] = None  # this laptop's newest made or adopted checkpoint (JSON shape)
    seen: Optional[Mapping[str, Any]] = None        # the high-water mark from anything fetched (JSON shape)
    epoch_start: Optional[int] = None               # the epoch whose start this laptop recorded
    fresh_keys_owed: Optional[Mapping[str, Any]] = None  # {since, from_idx} while --now owes fresh keys


# ── Where the files live (§18a, §24.1) ──

def roam_home() -> Path:
    """~/.irp-roam."""
    return Path.home() / ROAM_HOME


def local_dir(base: Path | str) -> Path:
    """The Time Machine-excluded `local/` folder under the roam home or a ledger folder."""
    return Path(base) / LOCAL_DIR


def state_path(ledger_dir: Path | str) -> Path:
    """`ledgers/<ledger_id>/local/state.json`."""
    return local_dir(ledger_dir) / STATE_FILE


def staging_dir(ledger_dir: Path | str) -> Path:
    """`ledgers/<ledger_id>/staging/`, also excluded from Time Machine."""
    return Path(ledger_dir) / STAGING_DIR


def tsa_path(home: Path | str | None = None) -> Path:
    """`~/.irp-roam/local/tsa.json` (or under another roam home, in tests)."""
    return local_dir(roam_home() if home is None else home) / TSA_FILE


# ── Checks ──

def _fail(message: str) -> None:
    raise StateError(message)


def _keys_exact(obj: Any, keys: frozenset, what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != keys:
        _fail(f"{what} must have exactly the keys {', '.join(sorted(keys))}")
    return obj


def _time(val: Any, what: str) -> str:
    if not (isinstance(val, str) and _TIMESTAMP.fullmatch(val)):
        _fail(f"{what} must be a UTC timestamp like 2026-10-08T09:00:00Z")
    try:
        datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail(f"{what} isn't a real UTC time")
    return val


def _match(rx: re.Pattern, val: Any, what: str) -> str:
    if not (isinstance(val, str) and rx.fullmatch(val)):
        _fail(f"{what} has the wrong format")
    return val


def _bool(val: Any, what: str) -> bool:
    if type(val) is not bool:
        _fail(f"{what} must be true or false")
    return val


def _quarantine_entry(q: Any) -> dict:
    """A §17.4 config quarantine entry: {"iss" or "sub": ..., "from": timestamp}."""
    if not isinstance(q, Mapping) or len(q) != 2 or "from" not in q or not ({"iss", "sub"} & set(q)):
        _fail("a quarantine entry is exactly {iss or sub, from}")
    who = "iss" if "iss" in q else "sub"
    _match(_PRINTABLE, q[who], f"quarantine {who}")
    if len(q[who]) > 128:
        _fail(f"quarantine {who} is too long")
    return {who: q[who], "from": _time(q["from"], "quarantine from")}


def check_record(rec: Any) -> RotationRecord:
    if not isinstance(rec, RotationRecord):
        _fail("the rotation record has the wrong type")
    if type(rec.rotate_idx) is not int or not 0 <= rec.rotate_idx <= MAX_INT:
        _fail("rotate_idx must be a line index")
    _time(rec.at, "rotation at")
    _bool(rec.suspected, "suspected")
    _bool(rec.closed, "closed")
    new = _keys_exact(dict(rec.new_ck) if isinstance(rec.new_ck, Mapping) else rec.new_ck, NEW_CK_KEYS, "new_ck")
    _match(_CK, new["id"], "new_ck id")
    try:
        pub = sig.b64url_decode(new["pub"], 32)
    except sig.SigError as exc:
        _fail(f"new_ck pub: {exc}")
    if sig.key_id("ck", pub) != new["id"]:
        _fail("new_ck id isn't the key id of its pub")
    _time(new["not_after"], "new_ck not_after")
    if rec.old_ck is not None:
        old = _keys_exact(dict(rec.old_ck) if isinstance(rec.old_ck, Mapping) else rec.old_ck, OLD_CK_KEYS,
                          "old_ck")
        _match(_CK, old["id"], "old_ck id")
        if old["id"] == new["id"]:
            _fail("old_ck and new_ck are the same key")
        if old["not_after"] != REMOVE:
            _time(old["not_after"], "old_ck not_after")
    if not isinstance(rec.quarantine, tuple):
        _fail("quarantine must be a list")
    for q in rec.quarantine:
        if _quarantine_entry(q) != dict(q):
            _fail("a quarantine entry is exactly {iss or sub, from}")
    return rec


def _int(val: Any, what: str, lo: int = 0) -> int:
    if type(val) is not int or not lo <= val <= MAX_INT:
        _fail(f"{what} must be an integer from {lo} to 2^53 - 1")
    return val


def _b64(val: Any, what: str) -> bytes:
    try:
        raw = sig.b64url_decode(val)
    except sig.SigError as exc:
        _fail(f"{what}: {exc}")
    if not raw:
        _fail(f"{what} is empty")
    return raw


def _mark(c: Any, keys: frozenset, what: str) -> dict:
    """The fields the checkpoint record and the seen mark share: (epoch, seq), the strand, and the exact header
    and .sig bytes, with `digest` the sha256 of those header bytes."""
    c = _keys_exact(c, keys, what)
    _int(c["epoch"], f"{what} epoch")
    _int(c["seq"], f"{what} seq", 1)
    _match(_STRAND, c["strand"], f"{what} strand")
    _match(_DIGEST, c["digest"], f"{what} digest")
    header = _b64(c["header"], f"{what} header")
    _b64(c["sig"], f"{what} sig")
    if "sha256-" + hashlib.sha256(header).hexdigest() != c["digest"]:
        _fail(f"{what} digest isn't the sha256 of its header bytes")
    return c


def _log_entry(c: Any, name: str) -> dict:
    c = _keys_exact(c, LOG_KEYS, f"the {name} log entry")
    length = _int(c["byte_length"], f"the {name} byte_length")
    _match(_DIGEST, c["byte_digest"], f"the {name} byte_digest")
    if not isinstance(c["segments"], list):
        _fail(f"the {name} segments must be a list")
    end = 0
    for s in c["segments"]:
        s = _keys_exact(s, SEGMENT_KEYS, f"a {name} segment")
        _match(_OBJECT, s["object"], f"a {name} segment object")
        if _int(s["offset"], f"a {name} segment offset") != end:
            _fail(f"the {name} segments must be contiguous from 0")
        end += _int(s["length"], f"a {name} segment length")
        _match(_HEX64, s["sha256"], f"a {name} segment sha256 (bare hex)")
    if end != length:
        _fail(f"the {name} segments must cover 0 to byte_length exactly")
    return c


def _recipients(val: Any) -> None:
    """Sorted by id: `rk` once, and `dk-` boxes; every recipient a distinct canonical age1… string."""
    if not isinstance(val, list):
        _fail("recipients must be a list")
    ids, recipients = [], []
    for r in val:
        r = _keys_exact(r, RECIPIENT_KEYS, "a recipient")
        if r["id"] != "rk":
            _match(_STRAND, r["id"], "a recipient id")
        try:
            ok = isinstance(r["recipient"], str) and Recipient.from_string(r["recipient"]).to_string() == r["recipient"]
        except AgeError:
            ok = False
        if not ok:
            _fail("a recipient must be an age1… recipient")
        ids.append(r["id"])
        recipients.append(r["recipient"])
    if ids != sorted(set(ids)):
        _fail("recipients must be sorted by id, without duplicates")
    if "rk" not in ids:
        _fail("recipients must include rk")
    if len(set(recipients)) != len(recipients):
        _fail("recipients must be distinct")


def check_checkpoint(rec: Any) -> dict:
    """§18a "State": the `checkpoint` record's closed schema, plus what it implies: `digest` is the sha256 of the
    header bytes, each log's segments cover it exactly from 0, a NONE label has no gen_time, `last_present` is
    from the record's own epoch, and a PRESENT checkpoint is its own `last_present`."""
    c = _mark(rec, CHECKPOINT_KEYS, "the checkpoint record")
    _time(c["created_at"], "checkpoint created_at")
    if c["gen_time"] is not None:
        _time(c["gen_time"], "checkpoint gen_time")
    if c["label"] not in LABELS:
        _fail("the checkpoint label must be PRESENT, UNVERIFIED or NONE")
    lp = c["last_present"]
    if lp is not None:
        lp = _keys_exact(lp, LAST_PRESENT_KEYS, "last_present")
        if _int(lp["epoch"], "last_present epoch") != c["epoch"]:
            _fail("last_present is reset at a new epoch, so its epoch is the record's")
        _time(lp["gen_time"], "last_present gen_time")
        _int(lp["devices_length"], "last_present devices_length")
        _int(lp["readers_length"], "last_present readers_length")
    _match(_HEX64, c["snapshot_digest"], "snapshot_digest (bare hex)")
    _recipients(c["recipients"])
    for name in ("policy_digest", "tsa_policy_digest"):
        if c[name] is not None:
            _match(_DIGEST, c[name], name)
    logs = _keys_exact(c["logs"], frozenset(LOG_NAMES), "the checkpoint logs")
    for name in LOG_NAMES:
        _log_entry(logs[name], name)
    if c["label"] == "NONE" and c["gen_time"] is not None:
        _fail("a NONE checkpoint has no gen_time")
    if c["label"] == "PRESENT":
        own = {"epoch": c["epoch"], "gen_time": c["gen_time"], "devices_length": logs["devices"]["byte_length"],
               "readers_length": logs["readers"]["byte_length"]}
        if c["gen_time"] is None or lp != own:
            _fail("a PRESENT checkpoint is its own last_present: its epoch, gen_time and devices and readers lengths")
    return c


def check_seen(mark: Any) -> dict:
    """§18a "State": the `seen` mark's closed schema; `devices` is the devices bytes that checkpoint cited."""
    c = _mark(mark, SEEN_KEYS, "the seen mark")
    _b64(c["devices"], "the seen mark devices")
    return c


def check_owed(marker: Any) -> dict:
    """The `fresh_keys_owed` marker's closed schema: exactly `{since, from_idx}`, `since` a §15.1 timestamp and
    `from_idx` a line count (an integer from 0 to 2^53 - 1)."""
    c = _keys_exact(marker, OWED_KEYS, "fresh_keys_owed")
    _time(c["since"], "fresh_keys_owed since")
    _int(c["from_idx"], "fresh_keys_owed from_idx")
    return c


def check_state(st: Any) -> RoamState:
    if not isinstance(st, RoamState):
        _fail("state has the wrong type")
    if st.rotation is not None:
        check_record(st.rotation)
    if not isinstance(st.probes, tuple):
        _fail("probes must be a list")
    for p in st.probes:
        if not isinstance(p, Probe):
            _fail("a probe has the wrong type")
        _match(_CK, p.iss, "probe iss")
        _time(p.nbf, "probe nbf")
        if not (isinstance(p.token, str) and _PRINTABLE.fullmatch(p.token) and len(p.token) <= MAX_TOKEN):
            _fail("a probe token is printable ASCII without spaces")
    if not isinstance(st.held, tuple):
        _fail("held must be a list")
    for r in st.held:
        _match(_READER, r, "a held reader id")
    if list(st.held) != sorted(set(st.held)):
        _fail("held must be sorted without duplicates")
    if st.checkpoint is not None:
        check_checkpoint(st.checkpoint)
    if st.seen is not None:
        check_seen(st.seen)
    if st.epoch_start is not None:
        _int(st.epoch_start, "epoch_start")
    if st.fresh_keys_owed is not None:
        check_owed(st.fresh_keys_owed)
    return st


# ── Reading and writing ──

def _plain(val: Any) -> Any:
    """A deep copy in JSON shape (dicts and lists), so a stored record never shares objects with its caller."""
    if isinstance(val, Mapping):
        return {k: _plain(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_plain(v) for v in val]
    return val


def _content(st: RoamState) -> dict:
    rec = st.rotation
    return {
        "rotation": None if rec is None else {
            "rotate_idx": rec.rotate_idx, "at": rec.at, "suspected": rec.suspected, "new_ck": dict(rec.new_ck),
            "old_ck": None if rec.old_ck is None else dict(rec.old_ck),
            "quarantine": [dict(q) for q in rec.quarantine], "closed": rec.closed},
        "probes": [{"iss": p.iss, "nbf": p.nbf, "token": p.token} for p in st.probes],
        "held": list(st.held),
        "checkpoint": None if st.checkpoint is None else _plain(st.checkpoint),
        "seen": None if st.seen is None else _plain(st.seen),
        "epoch_start": st.epoch_start,
        "fresh_keys_owed": None if st.fresh_keys_owed is None else _plain(st.fresh_keys_owed),
    }


def _from_content(c: Any) -> RoamState:
    c = _keys_exact(c, STATE_KEYS, "state.json")
    rec = None
    if c["rotation"] is not None:
        r = _keys_exact(c["rotation"], ROTATION_KEYS, "the rotation record")
        if not isinstance(r["quarantine"], list):
            _fail("quarantine must be a list")
        rec = RotationRecord(rotate_idx=r["rotate_idx"], at=r["at"], suspected=r["suspected"],
                             new_ck=_keys_exact(r["new_ck"], NEW_CK_KEYS, "new_ck"),
                             old_ck=None if r["old_ck"] is None else _keys_exact(r["old_ck"], OLD_CK_KEYS, "old_ck"),
                             quarantine=tuple(r["quarantine"]), closed=r["closed"])
    if not isinstance(c["probes"], list) or not isinstance(c["held"], list):
        _fail("probes and held must be lists")
    probes = tuple(Probe(**_keys_exact(p, PROBE_KEYS, "a probe")) for p in c["probes"])
    return check_state(RoamState(rotation=rec, probes=probes, held=tuple(c["held"]), checkpoint=c["checkpoint"],
                                 seen=c["seen"], epoch_start=c["epoch_start"], fresh_keys_owed=c["fresh_keys_owed"]))


def load_state(path: Path | str, *, required: bool = False) -> RoamState:
    """Read state.json. A missing file is an empty state, or StateMissing with `required` (making a checkpoint
    trusts the state only when the file exists); anything else outside the schema is StateError."""
    path = Path(path)
    if not (path.is_symlink() or path.exists()):
        if required:
            raise StateMissing(f"{path.name} is missing (never written, or a restore left it missing)")
        return RoamState()
    try:
        data = _keys._read_private(path)
    except _keys.KeystoreError as exc:
        raise StateError(str(exc)) from None
    try:
        return _from_content(sig.load_jcs(data, "state.json", error=StateError))
    except StateError:
        raise
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        raise StateError(f"state.json is malformed ({type(exc).__name__})") from None


def save_state(path: Path | str, st: RoamState, *, exclude_from_backup: Optional[ExcludeFromBackup] = None) -> None:
    """Write the whole of state.json atomically and durably (0600, fully synced, renamed into place). Its folder
    is created only with its Time Machine exclusion (`excluded_dir`). Writers use `update_state`, which reloads
    and replaces only their own keys."""
    from irp.integrity.canonical import canonicalize

    path = Path(path)
    check_state(st)
    excluded_dir(path.parent, exclude_from_backup)
    _keys._write_private(path, canonicalize(_content(st)))


def _require_exclusive(lock: Any, what: str) -> None:
    if not (lock is not None and getattr(lock, "held", False) and getattr(lock, "exclusive", False)):
        raise StateError(f"{what} only with roam.lock held exclusively")


def _never_down(old: Optional[Mapping[str, Any]], new: Optional[Mapping[str, Any]]) -> None:
    """`seen` never goes down: a lower (epoch, seq) is refused, and so is the same one with another digest (that's
    a fork, never a new mark)."""
    if old is None:
        return
    if new is None:
        _fail("seen never goes down: once set it isn't cleared")
    was, now = (old["epoch"], old["seq"]), (new["epoch"], new["seq"])
    if now < was:
        _fail(f"seen never goes down: (epoch, seq) {now} is below the mark {was}")
    if now == was and new["digest"] != old["digest"]:
        _fail(f"seen at {now} with a different digest is a fork, never a new mark")


def update_state(path: Path | str, lock: Any, *, exclude_from_backup: Optional[ExcludeFromBackup] = None,
                 required: bool = False, **changes: Any) -> RoamState:
    """The one way writers change state.json (§18a): under roam.lock held exclusively, reload the file (a missing
    one is an empty state) and replace only the keys given, leaving every other key as the file has it. `seen`
    never goes down. The folder is created only with its Time Machine exclusion. With `required` (a writer that
    trusted the file it loaded earlier, like making a checkpoint) a missing file, or a missing folder, is
    StateMissing and nothing is written, never a fresh state. Returns the state written."""
    _require_exclusive(lock, "state.json is written")
    unknown = sorted(set(changes) - STATE_KEYS)
    if unknown:
        _fail(f"unknown state.json key(s): {', '.join(unknown)}")
    path = Path(path)
    if required:
        load_state(path, required=True)
    excluded_dir(path.parent, exclude_from_backup)
    current = load_state(path, required=required)
    for name in ("checkpoint", "seen", "fresh_keys_owed"):
        if changes.get(name) is not None:
            changes[name] = _plain(changes[name])
    if "seen" in changes:
        if changes["seen"] is not None:
            check_seen(changes["seen"])
        _never_down(current.seen, changes["seen"])
    new = check_state(replace(current, **changes))
    save_state(path, new)
    return new


def write_local_file(path: Path | str, data: bytes, lock: Any, *,
                     exclude_from_backup: Optional[ExcludeFromBackup] = None) -> None:
    """Write a file into a `local/` folder (tsa.json, say) under roam.lock held exclusively: 0600, atomic and
    fully synced, its folder created only with its Time Machine exclusion."""
    path = Path(path)
    _require_exclusive(lock, f"{path.name} is written")
    excluded_dir(path.parent, exclude_from_backup)
    _keys._write_private(path, bytes(data))


# ── The Time Machine exclusion (§18a) ──

def _reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text[:300] if text else type(exc).__name__)


def _remove_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def excluded_dir(path: Path | str, exclude_from_backup: Optional[ExcludeFromBackup]) -> Path:
    """Make sure `path`, a `local/` or `staging/` folder, exists as a private (0700) folder Time Machine skips.

    An existing folder is used as it is: its exclusion was applied when it was made and survives every file
    replaced inside it. A missing one (never made, or removed by a restore) is made under a temporary name
    beside it, excluded through `exclude_from_backup` (which applies the exclusion, confirms it and raises if
    either fails) and only then moved into place, so no folder of that name ever exists without its exclusion.
    Without an exclusion, or when it fails, nothing is created and BackupExclusionError says why."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise StateError(f"{path} must be a real folder, not a symlink or a file")
    if path.is_dir():
        os.chmod(path, 0o700)
        return path
    if exclude_from_backup is None:
        raise BackupExclusionError(f"{path.name}/ is missing, and it must be excluded from Time Machine before "
                                   "anything is written into it, but this run has no backup exclusion; nothing "
                                   "was written")
    parent = _keys._private_dir(path.parent)
    tmp = parent / f".{path.name}.excluding"
    if tmp.is_symlink() or tmp.exists():
        if tmp.is_symlink() or not tmp.is_dir():
            raise StateError(f"{tmp} is in the way; remove it by hand")
        try:
            tmp.rmdir()  # left by a run that stopped before moving it into place; nothing is written into it
        except OSError:
            raise StateError(f"{tmp} is a leftover folder that isn't empty; look inside and remove it by hand") \
                from None
    tmp.mkdir(mode=0o700)
    os.chmod(tmp, 0o700)
    try:
        exclude_from_backup(tmp)
    except Exception as exc:
        _remove_dir(tmp)
        raise BackupExclusionError(f"{path.name}/ couldn't be excluded from Time Machine ({_reason(exc)}), so it "
                                   "wasn't created and nothing was written into it") from None
    except BaseException:
        _remove_dir(tmp)
        raise
    _keys._replace(tmp, path)
    return path


def tmutil_is_excluded(path: Path | str, run: Optional[_keys.Runner] = None) -> bool:
    """`tmutil isexcluded <path>` says `[Excluded]` (the init and drill checks use this too)."""
    run = run or _keys._default_run
    try:
        out = run([TMUTIL, "isexcluded", str(Path(path))], b"")
    except (_keys.KeystoreError, OSError):
        raise BackupExclusionError(f"tmutil isexcluded failed for {Path(path).name}") from None
    words = out.split()
    return bool(words) and words[0] == b"[Excluded]"


def tmutil_exclude(path: Path | str, run: Optional[_keys.Runner] = None) -> None:
    """The production `exclude_from_backup`: `tmutil addexclusion <path>` (a sticky item exclusion, which moves
    with the folder), then `tmutil isexcluded <path>` to confirm it. Raises BackupExclusionError if either
    fails."""
    run = run or _keys._default_run
    try:
        run([TMUTIL, "addexclusion", str(Path(path))], b"")
    except (_keys.KeystoreError, OSError):
        raise BackupExclusionError(f"tmutil addexclusion failed for {Path(path).name}") from None
    if not tmutil_is_excluded(path, run=run):
        raise BackupExclusionError(f"tmutil isexcluded says {Path(path).name} is still included in backups")
