"""Per-ledger state for Roaming IRP: `state.json` (spec v0.3 §14.6a).

`~/.irp-roam/ledgers/<ledger_id>/state.json` holds what the laptop keeps about a ledger that isn't secret and
isn't in a signed log. For rotation (step 2.5c) that's three things:

- `rotation`: the record of the newest rotation, `{rotate_idx, at, suspected, new_ck: {id, pub, not_after},
  old_ck: {id, not_after or "remove"}, quarantine, closed}`. A rotation is open while the live kid was
  introduced by a `device_rotate` at idx r and this file has no closed record for r. `old_ck` is null only in
  a record rebuilt after this file was lost, when the old CK is no longer known;
- `probes`: `{iss, nbf, token}` capabilities from retired CKs that the drill (and from step 3 the publisher)
  sends once `nbf` has passed, expecting a 401. A later rotation adds its own and never removes one;
- `held`: the readers whose renewed identity hasn't been confirmed delivered. The publisher skips them.

The file is exact JCS with a closed schema, written like the keystore: a 0600 temp file, fully synced, renamed
into place and the folder synced. A missing file reads as empty (no record, no probes, nothing held).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from . import keys as _keys
from . import sig

STATE_FILE = "state.json"
STATE_KEYS = frozenset({"rotation", "probes", "held"})
ROTATION_KEYS = frozenset({"rotate_idx", "at", "suspected", "new_ck", "old_ck", "quarantine", "closed"})
NEW_CK_KEYS = frozenset({"id", "pub", "not_after"})
OLD_CK_KEYS = frozenset({"id", "not_after"})
PROBE_KEYS = frozenset({"iss", "nbf", "token"})
REMOVE = "remove"
MAX_INT = 2**53 - 1
MAX_TOKEN = 8192

_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_CK = re.compile(r"ck-[0-9a-f]{32}")
_READER = re.compile(r"rd-[0-9a-f]{32}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")


class StateError(ValueError):
    """state.json is damaged or outside its schema."""


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
    return st


# ── Reading and writing ──

def _content(st: RoamState) -> dict:
    rec = st.rotation
    return {
        "rotation": None if rec is None else {
            "rotate_idx": rec.rotate_idx, "at": rec.at, "suspected": rec.suspected, "new_ck": dict(rec.new_ck),
            "old_ck": None if rec.old_ck is None else dict(rec.old_ck),
            "quarantine": [dict(q) for q in rec.quarantine], "closed": rec.closed},
        "probes": [{"iss": p.iss, "nbf": p.nbf, "token": p.token} for p in st.probes],
        "held": list(st.held),
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
    return check_state(RoamState(rotation=rec, probes=probes, held=tuple(c["held"])))


def load_state(path: Path | str) -> RoamState:
    """Read state.json. A missing file is an empty state; anything else outside the schema is StateError."""
    path = Path(path)
    if not (path.is_symlink() or path.exists()):
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


def save_state(path: Path | str, st: RoamState) -> None:
    """Write state.json atomically and durably (0600, fully synced, renamed into place)."""
    from irp.integrity.canonical import canonicalize

    path = Path(path)
    check_state(st)
    _keys._private_dir(path.parent)
    _keys._write_private(path, canonicalize(_content(st)))
