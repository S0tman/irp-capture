"""Checkpoints for Roaming IRP (spec v0.3 §18, §18a; step 2.6, part 1: formats and verification).

A checkpoint is the custodian's signed record of what the ledger and the three roam logs (devices, readers,
disclosures) held when it was made. It has four parts, each exact bytes:

- the header, `irp/checkpoint.json`, exact JCS with the closed keys `v, kind, ledger_id, root, epoch, strand,
  seq, prev, created_at, devices, body_digest`. Readers see it. It cites the whole devices log as read under the
  lock, and `prev` is the previous header's digest: seq - 1 of the same epoch, or at seq 1 of a later epoch the
  `checkpoint_ref` on the root-signed line that opened it (null at seq 1 of epoch 0, or when that ref is null);
- the signature, `irp/checkpoint.sig`, the §15.2 object over SI("checkpoint", header) by the strand's device key;
- the MAC, `irp/checkpoint.mac`: 64 lowercase hex and a newline, HMAC-SHA256 under K_a[e] over the domain line
  and the hex digests of the header and the body, checkable by stock python from the paper key alone;
- the body, `irp/body.json`, custodian-only: the whole `build_snapshot_file` output (salted, so `body_digest`
  stays blinded), each log's `{byte_length, byte_digest, append_only, segments}`, the recipients, and the
  digests of policy.json and tsa.json.

Each log is covered through its last newline. The ledger is never modified: a half-written last line waits for
the next run, and a parse error or a repeated id refuses. A torn disclosures tail moves to forks/ first, the way
LogWriter repairs the other two logs. Segments are 1 MiB pieces, each a Padmé-padded `segment` container
age-encrypted to the recipients and named `o/<sha256 of the ciphertext>`: an unchanged log carries its list
over, a pure append adds delta segments, anything else is a full base. `append_only` comes only from the prefix
check, and when it's false a loud alert names the log. All four logs are rebased when a list would pass 64
segments, at the first checkpoint of a strand or an epoch, and when the recipients change, so every listed
segment opens for exactly the current audience: RK's recipient for the epoch plus every active custodian and
companion box (never an approver, a retired or revoked device, or `box_prev`). The custodian container holds the
four parts, the token when there is one, the covered devices bytes and policy.json when it exists, sealed to the
same recipients, at the slot `m/<hex(HMAC-SHA256(K_c[e], "irp-roam/v1/slot/custodian/<seq>"))>`. One slot rule
names every feed.

`build_checkpoint` assembles all of that in memory from bytes already read, with the TSA step injected;
`make_checkpoint` does the reading, the waiting, the writing and the state (below). `verify_checkpoint` checks
one checkpoint over its shipped bytes against the newest devices log,
on the reader side (header, signature, token) or, given the epoch keys, on the custodian side as well (the MAC,
the body and every member), where any failure is ALARM. `verify_chain` checks a run of verified checkpoints:
seq and `prev`, devices prefixes that only grow (ROLLBACK, FORK), strands that change only across a run of
device_rotate lines, epoch changes through `checkpoint_ref` (reporting the abandoned seqs), created_at and
genTime that never go back, and `append_only` claims; it returns the first PRESENT checkpoint covering each
devices and readers line and, on the custodian side, raises ALARM for a line dated more than an hour after it.
`check_append_only`, `check_snapshot_link` and `shipped_from_mark` apply the same pairwise rules against
state.json's record, which keeps a header, a signature and the logs but no body (adoption, compare_seen).

Part 2 makes, stages and adopts. `make_checkpoint` runs under the caller's roam.lock, held exclusively, and
never opens a RoamLock or a LogWriter. In the §18a order it trusts state.json (step 0: the record is in the
current epoch, or is the one the epoch's opening line names as checkpoint_ref, or is null with `epoch_start` at
the current epoch, a current-epoch record's own signer isn't revoked, and the devices and readers logs start
with the bytes it cites; anything else is ALARM before anything is touched), cleans staging/ and the client-key
folders, adopts what's staged above the record, stops in the rotation hook when the new strand already has its
first checkpoint, checks C1, the signer (a strand reached only by device_rotate within an epoch, and no rotation
unfinished or fresh keys owed after --now) and the clock, picks seq and prev, refuses a slot it can
see is taken (staging/, the mirror's m/<slot>, or the relay through `relay_seen`), builds in memory with the
TSA step, and writes the new segments, the sidecar, the custodian file and then state.json, each by temp file,
sync, rename and folder sync. A kill anywhere leaves files the next run adopts byte for byte or cleans.
`adopt_staged` is steps 0 to 2 alone (rotation's step 1, recovery and root_rotate before they choose
checkpoint_ref). When it raises step 0's REVOKED_SIGNER ALARM (the record's own signer is revoked), recovery
doesn't adopt: it names state.json's record digest as checkpoint_ref, and staged files above the record are left
for upload and reported as abandoned at the epoch change. A recovery never names null while an old-epoch record
remains, unless the same state write nulls the record: step 0 refuses that record for good otherwise. The
opening line's checkpoint_ref vouches for the record it names, so adopting seq 1 of the new epoch links to it
even when a separate device_revoke inside the old epoch revoked its signer. `due` is the cadence rule,
evaluated after adoption. `compare_seen` checks what the fetch
paths bring back against the local marks (ROLLBACK, FORK with a persisted proof, the epoch change, the Cut 1
foreign head) and `write_seen` moves `seen` up only. `verify_fork_proof` checks a proof from the pinned root
alone. `rotation_hooks` gives the rotation engine its `adopt` and `checkpoint` hooks.

It never signs for a second active custodian (C1), never puts a retired or revoked box or an approver in the
recipients, never modifies the ledger, never anchors anything on an UNVERIFIED token, and never puts key
material in an exception message or a repr.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import hmac
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from irp.integrity import SCHEMA_VERSION
from irp.integrity.manifest import build_snapshot_file
from irp.integrity.strict import parse_ledger_strict

from . import age, container, sig
from . import logs as _logs
from . import rotation as _rotation
from . import state as _state
from . import tsa as _tsa
from .age import Identity, Recipient
from .keys import EpochKeys, KeystoreError, _fsync_dir, _full_fsync, _private_dir
from .logs import DEVICES_KIND, READERS_KIND, DeviceState, DevicesLog, LogError, LogFork, LogRollback

HEADER_KIND = "checkpoint"
BODY_KIND = "checkpoint-body"
SIG_KIND = "checkpoint"   # the SI kind of irp/checkpoint.sig
HEADER_KEYS = frozenset({"v", "kind", "ledger_id", "root", "epoch", "strand", "seq", "prev", "created_at", "devices",
                         "body_digest"})
DEVICES_KEYS = frozenset({"byte_length", "digest"})
BODY_KEYS = frozenset({"v", "kind", "snapshot", "logs", "recipients", "policy_digest", "tsa_policy_digest"})
LOG_NAMES = ("ledger", "devices", "readers", "disclosures")
BODY_LOG_KEYS = frozenset({"byte_length", "byte_digest", "append_only", "segments"})
RECORD_LOG_KEYS = frozenset({"byte_length", "byte_digest", "segments"})  # state.json's copy has no append_only
SEGMENT_KEYS = frozenset({"object", "offset", "length", "sha256"})
RECIPIENT_KEYS = frozenset({"id", "recipient"})
SNAPSHOT_KEYS = frozenset({"snapshot_digest", "manifest"})
MANIFEST_KEYS = frozenset({"schema", "snapshot_id", "created_at", "ledger_id", "scope", "previous_snapshot_digest",
                           "snapshot_salt", "ledger", "created_by"})
SNAPSHOT_LEDGER_KEYS = frozenset({"entry_count", "head_entry_id", "byte_digest", "semantic_digest"})
SEGMENT_SIZE = 1 << 20           # 1 MiB: segments are at most this long
MAX_SEGMENTS = 64                # a list that would pass this rebases all four logs
CREATED_AT_AHEAD = timedelta(minutes=5)
LINE_SLACK = timedelta(hours=1)  # a covered line may be dated at most this long after its first PRESENT genTime
MAC_DOMAIN = b"irp-roam/v1/custodian-mac\n"
SLOT_DOMAIN = "irp-roam/v1/slot/"
DISCLOSURES_FILE = "disclosures.jsonl"
MEMBER_HEADER = "irp/checkpoint.json"
MEMBER_SIG = "irp/checkpoint.sig"
MEMBER_MAC = "irp/checkpoint.mac"
MEMBER_BODY = "irp/body.json"
MEMBER_TSR = "irp/checkpoint.tsr"
MEMBER_DEVICES = "irp/devices.jsonl"
MEMBER_POLICY = "irp/policy.json"
PRESENT, UNVERIFIED, NONE = _tsa.PRESENT, _tsa.UNVERIFIED, _tsa.NONE
LABELS = (PRESENT, UNVERIFIED, NONE)
REWRITTEN = "rewritten"          # LogAlert reasons
NO_PREVIOUS = "no_previous"
MAX_INT = 2**53 - 1

_LEDGER_ID = re.compile(r"ILID-[0-9a-f]{32}")
_ROOT = re.compile(r"rt-[0-9a-f]{32}")
_STRAND = re.compile(r"dk-[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OBJECT = re.compile(r"o/[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SEQ = r"[1-9][0-9]{0,15}"
_SLOT_LABEL = re.compile(r"(?:custodian|phone|outbox)/" + _SEQ + r"|reader/[a-z0-9][a-z0-9-]{0,31}/" + _SEQ)
_SCOPE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_MAC_LINE = re.compile(rb"[0-9a-f]{64}\n")
_FMT = "%Y-%m-%dT%H:%M:%SZ"
_HOSTILE = (TypeError, KeyError, AttributeError, IndexError, ValueError, RecursionError)


class CheckpointError(ValueError):
    """A checkpoint can't be built, read or verified as asked. Messages never carry key material."""


class CheckpointAlarm(CheckpointError):
    """ALARM: publishing stops. A slot replay, a second active custodian, and any failed check on the custodian
    side (where a reader would exit 10)."""


class CheckpointRollback(CheckpointAlarm):
    """ROLLBACK: a log bound to a later checkpoint is shorter than one bound to an earlier object."""


class CheckpointFork(CheckpointAlarm):
    """FORK: two copies of a log, or two claims about one, disagree."""


# ── Small checks ──

def _fail(message: str) -> None:
    raise CheckpointError(message)


def _keys_exact(obj: Any, keys: Iterable[str], what: str) -> dict:
    keys = frozenset(keys)
    if not isinstance(obj, dict) or set(obj) != keys:
        _fail(f"{what} must have exactly the keys {', '.join(sorted(keys))}")
    return obj


def _match(rx: re.Pattern, val: Any, what: str) -> str:
    if not (isinstance(val, str) and rx.fullmatch(val)):
        _fail(f"{what} has the wrong format")
    return val


def _int(val: Any, what: str, lo: int = 0, hi: int = MAX_INT) -> int:
    if type(val) is not int or not lo <= val <= hi:
        _fail(f"{what} must be an integer from {lo} to {hi}")
    return val


def _time(val: Any, what: str) -> datetime:
    if not (isinstance(val, str) and _TIMESTAMP.fullmatch(val)):
        _fail(f"{what} must be a UTC timestamp like 2026-10-10T12:00:00Z")
    try:
        return datetime.strptime(val, _FMT)
    except ValueError:
        _fail(f"{what} isn't a real UTC time")
    raise AssertionError  # pragma: no cover


def _utc(t: Any) -> datetime:
    """Naive UTC; aware values are converted."""
    if not isinstance(t, datetime):
        _fail("a time must be a datetime")
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def _ts(t: datetime) -> str:
    return _utc(t).strftime(_FMT)


def _bytes(val: Any, what: str) -> bytes:
    if not isinstance(val, (bytes, bytearray)):
        _fail(f"{what} must be bytes")
    return bytes(val)


def _key32(val: Any, what: str) -> bytes:
    if not isinstance(val, (bytes, bytearray)) or len(val) != 32:
        _fail(f"{what} must be 32 bytes")
    return bytes(val)


def _digest_or_null(val: Any, what: str) -> Optional[str]:
    return None if val is None else _match(_DIGEST, val, what)


def digest_of(data: bytes) -> str:
    """`sha256-` plus the hex SHA-256 of exact bytes: a header's digest, a log's byte_digest."""
    return "sha256-" + hashlib.sha256(_bytes(data, "the data")).hexdigest()


def _canonical(obj: Any, what: str) -> bytes:
    try:
        return sig._canonical(obj)
    except Exception as exc:  # integers past 2^53 - 1, lone surrogates, values JSON doesn't have
        _fail(f"{what} can't be written as exact JCS ({type(exc).__name__})")
    raise AssertionError  # pragma: no cover


# ── Names (§18a "Header", §17.1) ──

def strand8(strand: str) -> str:
    """The first 8 characters after `dk-`."""
    return _match(_STRAND, strand, "the strand")[3:11]


def snapshot_id(strand: str, seq: int) -> str:
    """The body's snapshot id, `IRPC-<strand8>-<seq>`."""
    return f"IRPC-{strand8(strand)}-{_int(seq, 'seq', 1)}"


def checkpoint_id(strand: str, seq: int) -> str:
    """The Slice manifest's checkpoint id, `ckpt-<strand8>-<seq>`."""
    return f"ckpt-{strand8(strand)}-{_int(seq, 'seq', 1)}"


def manifest_projection(header: bytes) -> Dict[str, Any]:
    """The Slice manifest's checkpoint field (§18a): `id`, `strand`, `seq`, `hash` (the header's digest) and
    `signed_ts` (the header's created_at, never genTime). Readers (1b) recompute and compare it."""
    h = parse_header(header)
    return {"id": checkpoint_id(h["strand"], h["seq"]), "strand": h["strand"], "seq": h["seq"],
            "hash": digest_of(header), "signed_ts": h["created_at"]}


def slot_name(key: bytes, label: str) -> str:
    """The one slot rule (§17.1 as amended by §18a): `m/<hex(HMAC-SHA256(K, "irp-roam/v1/slot/" + label))>`, with
    labels `custodian/<seq>` (K_c[e]), `reader/<scope_id>/<seq>` (K_r), `phone/<seq>` (K_p) and `outbox/<seq>`
    (K_o). Seq is decimal from 1 without leading zeros."""
    key = _key32(key, "a slot key")
    if not (isinstance(label, str) and _SLOT_LABEL.fullmatch(label)):
        _fail("a slot label is custodian/<seq>, reader/<scope_id>/<seq>, phone/<seq> or outbox/<seq>")
    _int(int(label.rsplit("/", 1)[1]), "a slot seq", 1)
    return "m/" + hmac.new(key, (SLOT_DOMAIN + label).encode("ascii"), hashlib.sha256).hexdigest()


def custodian_slot(kc: bytes, seq: int) -> str:
    return slot_name(kc, f"custodian/{_int(seq, 'seq', 1)}")


def reader_slot(kr: bytes, scope_id: str, seq: int) -> str:
    return slot_name(kr, f"reader/{_match(_SCOPE_ID, scope_id, 'scope_id')}/{_int(seq, 'seq', 1)}")


def phone_slot(kp: bytes, seq: int) -> str:
    return slot_name(kp, f"phone/{_int(seq, 'seq', 1)}")


def outbox_slot(ko: bytes, seq: int) -> str:
    return slot_name(ko, f"outbox/{_int(seq, 'seq', 1)}")


# ── The MAC (§18.1) ──

def mac_line(ka: bytes, header: bytes, body: bytes) -> bytes:
    """`irp/checkpoint.mac`: hex(HMAC-SHA256(K_a[e], "irp-roam/v1/custodian-mac\\n" + hex(sha256(header)) + "\\n"
    + hex(sha256(body)) + "\\n")) and one newline."""
    key = _key32(ka, "K_a")
    msg = (MAC_DOMAIN + hashlib.sha256(_bytes(header, "the header")).hexdigest().encode("ascii") + b"\n"
           + hashlib.sha256(_bytes(body, "the body")).hexdigest().encode("ascii") + b"\n")
    return hmac.new(key, msg, hashlib.sha256).hexdigest().encode("ascii") + b"\n"


def check_mac(mac: bytes, ka: bytes, header: bytes, body: bytes) -> None:
    if not isinstance(mac, (bytes, bytearray)) or not _MAC_LINE.fullmatch(bytes(mac)):
        _fail("the MAC (irp/checkpoint.mac) must be 64 lowercase hex and one newline")
    if not hmac.compare_digest(bytes(mac), mac_line(ka, header, body)):
        _fail("the MAC doesn't match the header and the body under the epoch's K_a")


# ── Header (§18a "Header") ──

def parse_header(data: bytes) -> dict:
    """Strict JCS and the closed header schema, including the format half of the prev rule: prev is set from
    seq 2 on, and null at seq 1 of epoch 0. At seq 1 of a later epoch it's the opening line's checkpoint_ref,
    which only the devices log can say (verify_checkpoint checks it)."""
    h = sig.load_jcs(_bytes(data, "the header"), "irp/checkpoint.json", error=CheckpointError)
    h = _keys_exact(h, HEADER_KEYS, "the checkpoint header")
    if type(h["v"]) is not int or h["v"] != 1:
        _fail("the header's v must be 1")
    if h["kind"] != HEADER_KIND:
        _fail("the header's kind must be checkpoint")
    _match(_LEDGER_ID, h["ledger_id"], "the header's ledger_id")
    _match(_ROOT, h["root"], "the header's root")
    _int(h["epoch"], "the header's epoch")
    _match(_STRAND, h["strand"], "the header's strand")
    _int(h["seq"], "the header's seq", 1)
    _digest_or_null(h["prev"], "the header's prev")
    _time(h["created_at"], "the header's created_at")
    d = _keys_exact(h["devices"], DEVICES_KEYS, "the header's devices")
    _int(d["byte_length"], "the header's devices byte_length", 1)  # the devices log always holds genesis
    _match(_DIGEST, d["digest"], "the header's devices digest")
    _match(_DIGEST, h["body_digest"], "the header's body_digest")
    if h["seq"] > 1 and h["prev"] is None:
        _fail("prev is set from seq 2 on: the previous checkpoint's header digest")
    if h["seq"] == 1 and h["epoch"] == 0 and h["prev"] is not None:
        _fail("prev is null at seq 1 of epoch 0")
    return h


def build_header(*, ledger_id: str, root: str, epoch: int, strand: str, seq: int, prev: Optional[str],
                 created_at: str, devices: bytes, body: bytes) -> bytes:
    """The exact header bytes. `devices` is the whole devices log as read under the lock, `body` the exact
    irp/body.json bytes."""
    devices, body = _bytes(devices, "the devices log"), _bytes(body, "the body")
    data = _canonical({"v": 1, "kind": HEADER_KIND, "ledger_id": ledger_id, "root": root, "epoch": epoch,
                       "strand": strand, "seq": seq, "prev": prev, "created_at": created_at,
                       "devices": {"byte_length": len(devices), "digest": digest_of(devices)},
                       "body_digest": digest_of(body)}, "the header")
    parse_header(data)
    return data


def sign_header(header: bytes, seed: bytes, strand: str) -> bytes:
    """`irp/checkpoint.sig`: the §15.2 object over SI("checkpoint", header), by the strand's live device key."""
    try:
        return sig.encode_sig(sig.sign(SIG_KIND, _bytes(header, "the header"), seed, strand))
    except sig.SigError as exc:
        _fail(f"can't sign the checkpoint: {exc}")
    raise AssertionError  # pragma: no cover


# ── Body (§18a "Body") ──

def build_snapshot(*, ledger_id: str, strand: str, seq: int, raw: bytes, entries: Sequence[Mapping[str, Any]],
                   previous_snapshot_digest: Optional[str], created_at: str) -> Dict[str, Any]:
    """The body's `snapshot`: the whole, unchanged `build_snapshot_file` output for the covered ledger bytes, with
    `snapshot_id` IRPC-<strand8>-<seq> and the header's created_at. `previous_snapshot_digest` is the previous
    checkpoint's `snapshot_digest.value` (bare hex) within the epoch, null at seq 1 of every epoch."""
    _int(seq, "seq", 1)
    if (seq == 1) != (previous_snapshot_digest is None):
        _fail("previous_snapshot_digest is null exactly at seq 1 of an epoch")
    if previous_snapshot_digest is not None:
        _match(_HEX64, previous_snapshot_digest, "previous_snapshot_digest (bare hex)")
    _match(_LEDGER_ID, ledger_id, "ledger_id")
    _time(created_at, "created_at")
    return build_snapshot_file(snapshot_id=snapshot_id(strand, seq), ledger_id=ledger_id,
                               raw_bytes=_bytes(raw, "the ledger bytes"), entries=list(entries),
                               previous_snapshot_digest=previous_snapshot_digest, created_at=created_at)


def _alg_value(obj: Any, what: str) -> str:
    o = _keys_exact(obj, ("alg", "value"), what)
    if o["alg"] != "sha-256":
        _fail(f"{what} alg must be sha-256")
    return _match(_HEX64, o["value"], f"{what} value")


def _check_snapshot(snap: Any, header: Optional[Mapping[str, Any]]) -> None:
    """The whole build_snapshot_file output, closed, its digest recomputed; with a header, bound to it."""
    snap = _keys_exact(snap, SNAPSHOT_KEYS, "the snapshot")
    value = _alg_value(snap["snapshot_digest"], "the snapshot_digest")
    m = _keys_exact(snap["manifest"], MANIFEST_KEYS, "the snapshot manifest")
    if m["schema"] != SCHEMA_VERSION:
        _fail(f"the snapshot schema must be {SCHEMA_VERSION}")
    if not isinstance(m["snapshot_id"], str):
        _fail("the snapshot_id must be text")
    _time(m["created_at"], "the snapshot created_at")
    _match(_LEDGER_ID, m["ledger_id"], "the snapshot ledger_id")
    if m["scope"] != {"type": "full-ledger"}:
        _fail("the snapshot scope must be the full ledger")
    if m["previous_snapshot_digest"] is not None:
        _match(_HEX64, m["previous_snapshot_digest"], "the snapshot's previous_snapshot_digest (bare hex)")
    _match(_HEX64, m["snapshot_salt"], "the snapshot salt")
    led = _keys_exact(m["ledger"], SNAPSHOT_LEDGER_KEYS, "the snapshot ledger")
    _int(led["entry_count"], "the snapshot entry_count")
    head = led["head_entry_id"]
    if head is not None and type(head) not in (str, int):
        _fail("the snapshot head_entry_id must be text, an integer or null")
    _alg_value(led["byte_digest"], "the snapshot ledger byte_digest")
    sem = _keys_exact(led["semantic_digest"], ("alg", "canon", "value"), "the snapshot semantic_digest")
    if sem["alg"] != "sha-256" or sem["canon"] != "RFC8785":
        _fail("the snapshot semantic_digest must be sha-256 over RFC8785")
    _match(_HEX64, sem["value"], "the snapshot semantic_digest value")
    by = _keys_exact(m["created_by"], ("tool", "version"), "the snapshot created_by")
    if not (isinstance(by["tool"], str) and isinstance(by["version"], str)):
        _fail("the snapshot created_by must be text")
    if hashlib.sha256(_canonical(m, "the snapshot manifest")).hexdigest() != value:
        _fail("the snapshot_digest isn't the sha256 of the JCS manifest")
    if header is not None:
        if m["snapshot_id"] != snapshot_id(header["strand"], header["seq"]):
            _fail("the snapshot_id isn't IRPC-<strand8>-<seq> of the header")
        if m["created_at"] != header["created_at"]:
            _fail("the snapshot's created_at isn't the header's")
        if m["ledger_id"] != header["ledger_id"]:
            _fail("the snapshot's ledger_id isn't the header's")
        if (header["seq"] == 1) != (m["previous_snapshot_digest"] is None):
            _fail("the snapshot's previous_snapshot_digest is null exactly at seq 1 of an epoch")


def _segments(entry: Mapping[str, Any], name: str) -> List[dict]:
    segs = entry["segments"]
    if not isinstance(segs, list):
        _fail(f"the {name} segments must be a list")
    end = 0
    for s in segs:
        s = _keys_exact(s, SEGMENT_KEYS, f"a {name} segment")
        _match(_OBJECT, s["object"], f"a {name} segment object")
        if _int(s["offset"], f"a {name} segment offset") != end:
            _fail(f"the {name} segments aren't contiguous from 0 to byte_length")
        end += _int(s["length"], f"a {name} segment length", 1, SEGMENT_SIZE)
        _match(_HEX64, s["sha256"], f"a {name} segment sha256 (bare hex)")
    if end != entry["byte_length"]:
        _fail(f"the {name} segments aren't contiguous from 0 to byte_length")
    return segs


def _log_entry(entry: Any, name: str, keys: Iterable[str]) -> dict:
    entry = _keys_exact(entry, keys, f"the {name} log entry")
    _int(entry["byte_length"], f"the {name} byte_length")
    _match(_DIGEST, entry["byte_digest"], f"the {name} byte_digest")
    if "append_only" in entry and type(entry["append_only"]) is not bool:
        _fail(f"the {name} append_only must be true or false")
    _segments(entry, name)
    return entry


def _previous_entry(entry: Any, name: str) -> dict:
    """A previous log entry: a body's (with append_only) or state.json's record copy (without)."""
    keys = BODY_LOG_KEYS if isinstance(entry, Mapping) and "append_only" in entry else RECORD_LOG_KEYS
    return _log_entry(dict(entry) if isinstance(entry, Mapping) else entry, name, keys)


def _check_recipients(val: Any) -> list:
    if not isinstance(val, list) or not val:
        _fail("recipients must be a non-empty list")
    ids, seen = [], set()
    for r in val:
        r = _keys_exact(r, RECIPIENT_KEYS, "a recipient")
        if r["id"] != "rk":
            _match(_STRAND, r["id"], "a recipient id")
        try:
            ok = isinstance(r["recipient"], str) and \
                Recipient.from_string(r["recipient"]).to_string() == r["recipient"]
        except age.AgeError:
            ok = False
        if not ok:
            _fail("a recipient must be a canonical age1… recipient")
        ids.append(r["id"])
        seen.add(r["recipient"])
    if ids != sorted(set(ids)):
        _fail("recipients must be sorted by id without duplicates")
    if "rk" not in ids:
        _fail("recipients must include rk")
    if len(seen) != len(ids):
        _fail("recipients must be distinct keys")
    return val


def parse_body(data: bytes, header: Optional[Mapping[str, Any]] = None) -> dict:
    """Strict JCS and the closed body schema: kind, the snapshot (its digest recomputed), the four logs with
    contiguous segment ranges, the recipients and the two policy digests. With the parsed header, the snapshot is
    also bound to it (snapshot_id, created_at, ledger_id, and previous_snapshot_digest null exactly at seq 1)."""
    b = sig.load_jcs(_bytes(data, "the body"), "irp/body.json", error=CheckpointError)
    b = _keys_exact(b, BODY_KEYS, "the checkpoint body")
    if type(b["v"]) is not int or b["v"] != 1:
        _fail("the body's v must be 1")
    if b["kind"] != BODY_KIND:
        _fail("the body's kind must be checkpoint-body")
    _check_snapshot(b["snapshot"], header)
    logs = _keys_exact(b["logs"], LOG_NAMES, "the body logs")
    for name in LOG_NAMES:
        _log_entry(logs[name], name, BODY_LOG_KEYS)
    _check_recipients(b["recipients"])
    _digest_or_null(b["policy_digest"], "policy_digest")
    _digest_or_null(b["tsa_policy_digest"], "tsa_policy_digest")
    return b


def build_body(*, snapshot: Mapping[str, Any], logs: Mapping[str, Mapping[str, Any]],
               recipients: Sequence[Mapping[str, str]], policy_digest: Optional[str] = None,
               tsa_policy_digest: Optional[str] = None) -> bytes:
    """The exact irp/body.json bytes, checked by the same rules a verifier applies."""
    obj = {"v": 1, "kind": BODY_KIND, "snapshot": snapshot,
           "logs": {name: dict(logs[name]) for name in LOG_NAMES},
           "recipients": [dict(r) for r in recipients], "policy_digest": policy_digest,
           "tsa_policy_digest": tsa_policy_digest}
    data = _canonical(obj, "the body")
    parse_body(data)
    return data


# ── The devices log at a cited prefix ──

def _log_bytes(devices: DevicesLog) -> bytes:
    return b"".join(line + b"\n" for line in devices.lines)


def prefix_state(devices: DevicesLog, byte_length: int) -> Tuple[int, DeviceState]:
    """The device state at a cited devices prefix: the line index is the number of newlines in the prefix minus 1,
    read off the replay of the newest log (which the prefix must start: check_extends first)."""
    _int(byte_length, "the cited devices byte_length", 1)
    data = _log_bytes(devices)
    if byte_length > len(data):
        raise CheckpointRollback("ROLLBACK: the devices log is shorter than the cited prefix")
    if data[byte_length - 1:byte_length] != b"\n":
        _fail("a cited devices byte_length must end right after a line's newline")
    idx = data.count(b"\n", 0, byte_length) - 1
    return idx, devices.state(idx)


def opening_line(devices: DevicesLog, epoch: int) -> Optional[Tuple[int, Mapping[str, Any]]]:
    """The idx and body of the root-signed `recovery` or `root_rotate` line that opened `epoch`; None for epoch 0
    (genesis) or an epoch the log hasn't reached."""
    _int(epoch, "epoch")
    if epoch == 0:
        return None
    for idx, st in enumerate(devices.states):
        if st.epoch == epoch:
            body = _logs.parse_line(devices.lines[idx], DEVICES_KIND)[0]
            if body["event"] not in ("recovery", "root_rotate") or body["epoch"] != epoch:
                _fail(f"the line that opened epoch {epoch} isn't a recovery or root_rotate line")
            return idx, body
        if st.epoch > epoch:
            break
    return None


def seq1_prev(devices: DevicesLog, epoch: int) -> Optional[str]:
    """The prev of seq 1 of `epoch`: null at epoch 0, otherwise the checkpoint_ref on the line that opened it."""
    if _int(epoch, "epoch") == 0:
        return None
    opened = opening_line(devices, epoch)
    if opened is None:
        _fail(f"no line of the devices log opens epoch {epoch}")
    return opened[1]["checkpoint_ref"]


def horizon(devices: DevicesLog, epoch: int) -> int:
    """The last line of `epoch`: the line before the one that closed it, or the tail for the current epoch. An
    earlier epoch's checkpoints are checked against the log up to here, never against the newest log."""
    _int(epoch, "epoch")
    last = None
    for idx, st in enumerate(devices.states):
        if st.epoch == epoch:
            last = idx
        elif st.epoch > epoch:
            break
    if last is None:
        _fail(f"epoch {epoch} isn't in the devices log")
    return last


def recipients_for(epoch_keys: EpochKeys, state: DeviceState) -> List[Dict[str, str]]:
    """The body's recipients, sorted by id: RK's age recipient for the epoch (from `rk_pub`) as `rk`, plus the box
    of every custodian-class or companion device active in `state`. Approvers, retired and revoked devices and
    the keystore's box_prev are never among them."""
    rk_pub = getattr(epoch_keys, "rk_pub", None)
    out = [{"id": "rk", "recipient": Recipient(_key32(rk_pub, "rk_pub")).to_string()}]
    for kid, d in state.devices.items():
        if d.cls in ("custodian", "companion"):
            if d.box is None:  # pragma: no cover - descriptors always carry a box for these classes
                _fail(f"{kid} has no box")
            out.append({"id": kid, "recipient": Recipient(d.box).to_string()})
    out.sort(key=lambda r: r["id"])
    if len({r["recipient"] for r in out}) != len(out):
        _fail("two recipients share one key")
    return out


def check_one_custodian(state: DeviceState, strand: str) -> None:
    """C1: Cut 1 has one checkpointing laptop. The strand must be an active custodian and the only one; otherwise
    ALARM naming each extra custodian to revoke, before anything is signed."""
    d = state.devices.get(strand)
    if d is None or d.cls != "custodian":
        raise CheckpointAlarm(f"ALARM: {strand} isn't an active custodian at the devices tail; nothing was signed")
    extra = sorted(x.label for kid, x in state.devices.items() if x.cls == "custodian" and kid != strand)
    if extra:
        raise CheckpointAlarm("ALARM: more than one active custodian, and Cut 1 has one checkpointing laptop: "
                              + ", ".join(f"revoke {label}" for label in extra) + "; nothing was signed")


# ── Which bytes a checkpoint covers (§18a) ──

@dataclass(frozen=True)
class Covered:
    """Each log through its last newline, read once. `entries` is the ledger's parse_ledger_strict result,
    `ledger_left` the bytes of a half-written last ledger line left for the next run, and `torn` where a torn
    disclosures tail was moved."""
    ledger: bytes = field(repr=False)
    entries: Tuple[Mapping[str, Any], ...] = field(repr=False)
    devices: bytes = field(repr=False)
    readers: bytes = field(repr=False)
    disclosures: bytes = field(repr=False)
    ledger_left: int = 0
    torn: Optional[Path] = None

    @property
    def logs(self) -> Dict[str, bytes]:
        return {"ledger": self.ledger, "devices": self.devices, "readers": self.readers,
                "disclosures": self.disclosures}


def cover(data: bytes) -> Tuple[bytes, int]:
    """The bytes through the last newline, and how many bytes after it were left."""
    data = _bytes(data, "a log")
    keep = data.rfind(b"\n") + 1
    return data[:keep], len(data) - keep


def parse_ledger(raw: bytes) -> Tuple[Mapping[str, Any], ...]:
    """The covered ledger bytes, parsed strictly. A parse error or a repeated id refuses the checkpoint."""
    try:
        text = _bytes(raw, "the ledger").decode("utf-8")
    except UnicodeDecodeError:
        _fail("the ledger isn't UTF-8; no checkpoint is made")
    parsed = parse_ledger_strict(text)
    if parsed.errors:
        first = parsed.errors[0]
        _fail(f"the ledger has {len(parsed.errors)} malformed line(s), first at line {first['line']} "
              f"({first['kind']}); no checkpoint is made")
    if parsed.duplicate_ids:
        _fail(f"the ledger repeats {len(parsed.duplicate_ids)} entry id(s); no checkpoint is made")
    return tuple(parsed.entries)


def _whole(data: bytes, what: str) -> bytes:
    data = _bytes(data, what)
    if data and not data.endswith(b"\n"):
        _fail(f"{what} must be whole lines (covered through its last newline)")
    return data


def covered_from(*, ledger: bytes, devices: bytes, readers: bytes, disclosures: bytes) -> Covered:
    """Covered bytes already in memory (Path B, tests): each must be whole lines, and the ledger must parse."""
    ledger = _whole(ledger, "the ledger")
    return Covered(ledger=ledger, entries=parse_ledger(ledger), devices=_whole(devices, "the devices log"),
                   readers=_whole(readers, "the readers log"), disclosures=_whole(disclosures, "the disclosures log"))


def disclosures_path(ledger_dir: Path | str) -> Path:
    """`ledgers/<ledger_id>/disclosures.jsonl` (§24.1)."""
    return Path(ledger_dir) / DISCLOSURES_FILE


def _read_all(fd: int) -> bytes:
    chunks, offset = [], 0
    while True:
        chunk = os.pread(fd, 1 << 16, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _open(path: Path, flags: int, what: str, *, required: bool) -> Optional[int]:
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        if required:
            _fail(f"{what} is missing ({path.name}); no checkpoint is made")
        return None
    except OSError as exc:
        if os.path.islink(path):
            _fail(f"{what} ({path.name}) is a symlink; roam logs must be real files")
        _fail(f"can't open {what} ({path.name}): {exc.strerror}")
    raise AssertionError  # pragma: no cover


def _read_file(path: Path, what: str, *, required: bool, follow: bool) -> Optional[bytes]:
    flags = os.O_RDONLY | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    fd = _open(path, flags, what, required=required)
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            _fail(f"{what} ({path.name}) must be a regular file")
        if not follow and st.st_uid != os.getuid():
            _fail(f"{what} ({path.name}) must be owned by this user")
        return _read_all(fd)
    finally:
        os.close(fd)


def _save_fragment(forks_dir: Path, name: str, offset: int, fragment: bytes, clock: Callable[[], datetime]) -> Path:
    """Keep a torn fragment the way LogWriter does: a new 0600 file each time, synced, never replacing one."""
    try:
        forks = _private_dir(Path(forks_dir))
    except KeystoreError as exc:
        _fail(f"forks/: {exc}")
    stamp = _utc(clock()).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        target = forks / f"{name}.{stamp}.{offset}{'.' + str(n) if n else ''}.torn"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(fragment)
            fh.flush()
            _full_fsync(fh.fileno())
        _fsync_dir(forks)
        return target
    _fail("too many torn fragments saved this second")
    raise AssertionError  # pragma: no cover


def _cover_disclosures(path: Path, forks_dir: Path, clock: Callable[[], datetime]) -> Tuple[bytes, Optional[Path]]:
    """The disclosures log is written only under roam.lock, so a torn tail is ours to repair: it moves to forks/
    (unique name, synced) and the file is cut back to its last newline before it's covered."""
    fd = _open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), "the disclosures log", required=False)
    if fd is None:
        return b"", None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            _fail(f"the disclosures log ({path.name}) must be a regular file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("the disclosures log is held by a writer; nothing was covered")
        data = _read_all(fd)
        torn = None
        if data and not data.endswith(b"\n"):
            keep = data.rfind(b"\n") + 1
            torn = _save_fragment(forks_dir, path.name, keep, data[keep:], clock)
            os.ftruncate(fd, keep)
            _full_fsync(fd)
            data = data[:keep]
        return data, torn
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _wall_clock() -> datetime:
    return datetime.now(timezone.utc)


def read_covered(*, ledger_file: Path | str, devices_path: Path | str, readers_path: Path | str,
                 disclosures_path: Path | str, forks_dir: Path | str, lock: Any,
                 clock: Optional[Callable[[], datetime]] = None) -> Covered:
    """Read each log once, under the caller's roam.lock held exclusively (never opening a RoamLock or a LogWriter),
    covered through its last newline. The ledger (the path set at init) is opened read-only and never modified;
    its half-written last line is left for the next run (`ledger_left`), and a parse error or a repeated id
    refuses. A missing ledger or devices log refuses; a missing readers or disclosures log is empty. Roam logs
    must be real files, never symlinks. A torn disclosures tail moves to forks/ first (`torn`); the devices and
    readers logs are only cut, since their writers repair them."""
    if not (lock is not None and getattr(lock, "held", False) and getattr(lock, "exclusive", False)):
        _fail("covered bytes are read only with roam.lock held exclusively")
    raw = _read_file(Path(ledger_file), "the ledger", required=True, follow=True)
    ledger, left = cover(raw)
    entries = parse_ledger(ledger)
    devices, _ = cover(_read_file(Path(devices_path), "the devices log", required=True, follow=False))
    readers_raw = _read_file(Path(readers_path), "the readers log", required=False, follow=False)
    readers, _ = cover(readers_raw or b"")
    disclosures, torn = _cover_disclosures(Path(disclosures_path), Path(forks_dir), clock or _wall_clock)
    return Covered(ledger=ledger, entries=entries, devices=devices, readers=readers, disclosures=disclosures,
                   ledger_left=left, torn=torn)


def _line_ats(data: bytes, kind: str, what: str) -> List[datetime]:
    try:
        return [_time(_logs.parse_line(line, kind)[0]["at"], f"a {what} line's at") for line in _logs.split_log(data)]
    except LogError as exc:
        _fail(f"the {what} log: {exc}")
    raise AssertionError  # pragma: no cover


def latest_line_at(devices: bytes, readers: bytes, last_present: Optional[Mapping[str, Any]] = None
                   ) -> Optional[datetime]:
    """The newest `at` among the devices and readers lines past `last_present`'s lengths (all lines when it's
    null): what the TSA step's genTime + 1 hour must reach for a token to count as PRESENT. None when no line
    is past them."""
    newest = None
    for name, data, kind, key in (("devices", devices, DEVICES_KIND, "devices_length"),
                                  ("readers", readers, READERS_KIND, "readers_length")):
        data = _bytes(data, f"the {name} log")
        start = 0 if last_present is None else _int(last_present[key], f"last_present {key}")
        if start > len(data):
            raise CheckpointRollback(f"ROLLBACK: the {name} log is shorter than the last PRESENT checkpoint covered")
        if start and data[start - 1:start] != b"\n":
            _fail(f"last_present's {name} length doesn't end at a line")
        for at in _line_ats(data[start:], kind, name):
            newest = at if newest is None or at > newest else newest
    return newest


# ── Segments (§18a "Segments") ──

@dataclass(frozen=True)
class LogAlert:
    """A loud local alert about one log: `append_only` is false because its previous bytes no longer hash to the
    previous digest (`rewritten`) or because no previous entry was available (`no_previous`)."""
    log: str
    reason: str

    def __str__(self) -> str:
        if self.reason == REWRITTEN:
            return (f"ALERT: the {self.log} log doesn't start with the bytes the previous checkpoint covered (it was "
                    "rewritten): append_only false, written as a full base")
        return (f"ALERT: no previous checkpoint entry to compare the {self.log} log with: append_only false, written "
                "as a full base")


@dataclass(frozen=True)
class LogPlan:
    """What one log's segment list becomes: the entries carried over (`kept`) and the plaintext ranges to seal as
    new segments (`new`), with `append_only` from the prefix check and `base` when the list starts again."""
    name: str
    byte_length: int
    byte_digest: str
    append_only: bool
    kept: Tuple[Mapping[str, Any], ...]
    new: Tuple[Tuple[int, int], ...]
    base: bool

    @property
    def count(self) -> int:
        return len(self.kept) + len(self.new)


def _chunks(start: int, end: int) -> Tuple[Tuple[int, int], ...]:
    return tuple((off, min(SEGMENT_SIZE, end - off)) for off in range(start, end, SEGMENT_SIZE))


def plan_logs(covered: Mapping[str, bytes], previous: Optional[Mapping[str, Mapping[str, Any]]] = None, *,
              full_base: bool = False) -> Tuple[Dict[str, LogPlan], Tuple[LogAlert, ...], bool]:
    """Plan each log's segments against its previous entry (the record's, or at seq 1 of a new epoch the entry the
    caller found by checkpoint_ref; None when there's none). `append_only` is true only if the previous
    byte_length bytes still hash to the previous byte_digest; otherwise it's false and a LogAlert names the log.
    The same length and digest carry the list over, a pure append adds a delta segment per new 1 MiB chunk, and
    anything else is a full base of that log. All four logs become a full base, append_only unchanged, when
    `full_base` (the first checkpoint of a strand or an epoch, or new recipients) or when a list would pass 64
    segments. Returns the plans, the alerts and whether all four were rebased."""
    if not isinstance(covered, Mapping) or set(covered) != set(LOG_NAMES):
        _fail("covered bytes are given for exactly the four logs")
    if previous is not None and (not isinstance(previous, Mapping) or set(previous) != set(LOG_NAMES)):
        _fail("previous entries are given for all four logs, or none")
    plans: Dict[str, LogPlan] = {}
    alerts: List[LogAlert] = []
    for name in LOG_NAMES:
        data = _bytes(covered[name], f"the {name} log")
        length, dg = len(data), digest_of(data)
        prev = None if previous is None else _previous_entry(previous[name], name)
        if prev is None or not (length >= prev["byte_length"] and
                                digest_of(data[:prev["byte_length"]]) == prev["byte_digest"]):
            alerts.append(LogAlert(name, NO_PREVIOUS if prev is None else REWRITTEN))
            plans[name] = LogPlan(name, length, dg, False, (), _chunks(0, length), True)
            continue
        kept = tuple(dict(s) for s in prev["segments"])
        plans[name] = LogPlan(name, length, dg, True, kept, _chunks(prev["byte_length"], length), False)
    rebase = bool(full_base) or any(p.count > MAX_SEGMENTS for p in plans.values())
    if rebase:
        plans = {name: LogPlan(name, p.byte_length, p.byte_digest, p.append_only, (), _chunks(0, p.byte_length), True)
                 for name, p in plans.items()}
    return plans, tuple(alerts), rebase


def _targets(recipients: Sequence[Mapping[str, str]]) -> List[Recipient]:
    try:
        return [Recipient.from_string(r["recipient"]) for r in recipients]
    except (age.AgeError, KeyError, TypeError):
        _fail("recipients must be {id, recipient} entries with age1… recipients")
    raise AssertionError  # pragma: no cover


def _seal(plain: bytes, targets: Sequence[Recipient], rng: Callable[[int], bytes]) -> bytes:
    try:
        return age.encrypt(plain, targets, rng=rng)
    except age.AgeError as exc:
        _fail(f"can't seal to the recipients: {exc}")
    raise AssertionError  # pragma: no cover


def seal_logs(plans: Mapping[str, LogPlan], covered: Mapping[str, bytes], recipients: Sequence[Mapping[str, str]],
              *, rng: Callable[[int], bytes] = os.urandom
              ) -> Tuple[Dict[str, Dict[str, Any]], Tuple[Tuple[str, bytes], ...]]:
    """Seal each planned range as a `segment` container (one member, `seg`, Padmé-padded) age-encrypted with a
    fresh file key to the recipients, named `o/<sha256 of the ciphertext>`. Returns the body's four log entries
    and the new objects (id, ciphertext) in write order."""
    targets = _targets(recipients)
    logs: Dict[str, Dict[str, Any]] = {}
    objects: List[Tuple[str, bytes]] = []
    for name in LOG_NAMES:
        p, data = plans[name], _bytes(covered[name], f"the {name} log")
        if len(data) != p.byte_length or digest_of(data) != p.byte_digest:
            _fail(f"the {name} bytes aren't the ones planned")
        segs = [dict(s) for s in p.kept]
        for off, n in p.new:
            piece = data[off:off + n]
            ct = _seal(container.pack("segment", {"seg": piece}), targets, rng)
            oid = "o/" + hashlib.sha256(ct).hexdigest()
            segs.append({"object": oid, "offset": off, "length": n, "sha256": hashlib.sha256(piece).hexdigest()})
            objects.append((oid, ct))
        logs[name] = {"byte_length": p.byte_length, "byte_digest": p.byte_digest, "append_only": p.append_only,
                      "segments": segs}
        _log_entry(logs[name], name, BODY_LOG_KEYS)
    return logs, tuple(objects)


def listed_objects(logs: Mapping[str, Mapping[str, Any]]) -> Tuple[str, ...]:
    """Every `o/` id the logs list, in log order, each once: what the sidecar holds."""
    out: List[str] = []
    for name in LOG_NAMES:
        for s in logs[name]["segments"]:
            if s["object"] not in out:
                out.append(s["object"])
    return tuple(out)


def open_segment(ciphertext: bytes, identities: Sequence[Identity], entry: Mapping[str, Any], *,
                 expected_stanzas: Optional[int] = None) -> bytes:
    """One segment's plaintext, checked against its entry: named by the hash of its ciphertext, opening with one of
    the identities (and the expected stanza count), one `seg` member of exactly the entry's length and sha256."""
    ct = _bytes(ciphertext, "a segment object")
    if entry["object"] != "o/" + hashlib.sha256(ct).hexdigest():
        _fail("the segment object isn't named by the hash of its ciphertext")
    try:
        members = container.unpack("segment", age.decrypt(ct, identities, expected_stanzas=expected_stanzas))
    except (age.AgeError, container.ContainerError) as exc:
        _fail(f"the segment doesn't open: {type(exc).__name__}")
    seg = members["seg"]
    if len(seg) != entry["length"] or hashlib.sha256(seg).hexdigest() != entry["sha256"]:
        _fail("the segment isn't the bytes its entry lists")
    return seg


def restore_log(entry: Mapping[str, Any], fetch: Callable[[str], bytes], identities: Sequence[Identity], *,
                expected_stanzas: Optional[int] = None, name: str = "a") -> bytes:
    """A log's covered bytes from its segments (`fetch` gives an object's ciphertext by its `o/` id), checked
    against the entry's byte_length and byte_digest."""
    entry = _previous_entry(entry, name)
    data = b"".join(open_segment(fetch(s["object"]), identities, s, expected_stanzas=expected_stanzas)
                    for s in entry["segments"])
    if len(data) != entry["byte_length"] or digest_of(data) != entry["byte_digest"]:
        _fail(f"the {name} log's segments don't add up to its byte_length and byte_digest")
    return data


# ── The custodian container (§15.4, §18a "Custodian container and slot") ──

def custodian_members(*, header: bytes, sig: bytes, mac: bytes, body: bytes, devices: bytes,
                      tsr: Optional[bytes] = None, policy: Optional[bytes] = None) -> Dict[str, bytes]:
    members = {MEMBER_HEADER: _bytes(header, "the header"), MEMBER_SIG: _bytes(sig, "the signature"),
               MEMBER_MAC: _bytes(mac, "the MAC"), MEMBER_BODY: _bytes(body, "the body"),
               MEMBER_DEVICES: _bytes(devices, "the devices bytes")}
    if tsr is not None:
        members[MEMBER_TSR] = _bytes(tsr, "the token")
    if policy is not None:
        members[MEMBER_POLICY] = _bytes(policy, "policy.json")
    return members


def seal_custodian(members: Mapping[str, bytes], recipients: Sequence[Mapping[str, str]], *,
                   rng: Callable[[int], bytes] = os.urandom) -> bytes:
    """The custodian container, Padmé-padded and age-encrypted to the recipients."""
    try:
        plain = container.pack("custodian", members)
    except container.ContainerError as exc:
        _fail(f"the custodian container: {exc}")
    return _seal(plain, _targets(recipients), rng)


@dataclass(frozen=True)
class Shipped:
    """A checkpoint's shipped bytes. A reader has the header, signature and token; the custodian side adds the
    MAC, the body, the devices bytes, policy.json and the container's stanza count."""
    header: bytes = field(repr=False)
    sig: bytes = field(repr=False)
    tsr: Optional[bytes] = field(default=None, repr=False)
    mac: Optional[bytes] = field(default=None, repr=False)
    body: Optional[bytes] = field(default=None, repr=False)
    devices: Optional[bytes] = field(default=None, repr=False)
    policy: Optional[bytes] = field(default=None, repr=False)
    stanzas: Optional[int] = None

    @classmethod
    def from_members(cls, members: Mapping[str, bytes], *, stanzas: Optional[int] = None) -> "Shipped":
        if not isinstance(members, Mapping) or MEMBER_HEADER not in members or MEMBER_SIG not in members:
            _fail("a checkpoint's members include irp/checkpoint.json and irp/checkpoint.sig")
        return cls(header=members[MEMBER_HEADER], sig=members[MEMBER_SIG], tsr=members.get(MEMBER_TSR),
                   mac=members.get(MEMBER_MAC), body=members.get(MEMBER_BODY), devices=members.get(MEMBER_DEVICES),
                   policy=members.get(MEMBER_POLICY), stanzas=stanzas)


def open_custodian(ciphertext: bytes, identities: Sequence[Identity]) -> Shipped:
    """Open a custodian object (the live box, box_prev or RK) and read its container strictly. Its stanza count
    is kept for verify_checkpoint, which compares it with the recipients."""
    ct = _bytes(ciphertext, "the custodian object")
    try:
        stanzas = age.stanza_count(ct)
        plain = age.decrypt(ct, identities)
    except age.AgeError as exc:
        _fail(f"the custodian object doesn't open: {type(exc).__name__}")
    try:
        members = container.unpack("custodian", plain)
    except container.ContainerError as exc:
        _fail(f"the custodian container: {exc}")
    return Shipped.from_members(members, stanzas=stanzas)


def shipped_from_mark(mark: Mapping[str, Any]) -> Shipped:
    """The header and signature a state.json `checkpoint` record or `seen` mark keeps (strict b64url of the exact
    bytes), as Shipped for a reader-side verify_checkpoint: adoption and compare_seen check against them."""
    if not isinstance(mark, Mapping) or "header" not in mark or "sig" not in mark:
        _fail("a record or mark carries header and sig")
    try:
        return Shipped(header=sig.b64url_decode(mark["header"]), sig=sig.b64url_decode(mark["sig"]))
    except sig.SigError as exc:
        _fail(f"a record or mark's header or sig: {exc}")
    raise AssertionError  # pragma: no cover


# ── Building a checkpoint (§18a "Making a checkpoint", step 7, in memory) ──

def checkpoint_record(header: bytes, sig_bytes: bytes, body: bytes, *, label: str, gen_time: Optional[datetime],
                      previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The `checkpoint` record state.json keeps for a made or adopted checkpoint (§18a "State"). `gen_time` is
    kept for a PRESENT checkpoint only (nothing anchors on an UNVERIFIED token), and `last_present` is its own for
    PRESENT, carried from `previous` within the same epoch otherwise, and null at a new epoch."""
    h = parse_header(header)
    b = parse_body(body, h)
    try:
        sig.parse_sig(sig_bytes)
    except sig.SigError as exc:
        _fail(f"the signature: {exc}")
    if label not in LABELS:
        _fail("a checkpoint's label is PRESENT, UNVERIFIED or NONE")
    if previous is not None:
        try:
            previous = _state.check_checkpoint(_state._plain(previous))
        except _state.StateError as exc:
            _fail(f"the previous record: {exc}")
    gen = None
    last_present = None
    if label == PRESENT:
        if not isinstance(gen_time, datetime):
            _fail("a PRESENT checkpoint has a genTime")
        gen = _ts(gen_time)
        last_present = {"epoch": h["epoch"], "gen_time": gen,
                        "devices_length": b["logs"]["devices"]["byte_length"],
                        "readers_length": b["logs"]["readers"]["byte_length"]}
    elif previous is not None and previous["epoch"] == h["epoch"] and previous["last_present"] is not None:
        last_present = dict(previous["last_present"])
    rec = {"epoch": h["epoch"], "seq": h["seq"], "strand": h["strand"], "digest": digest_of(header),
           "header": sig.b64url_encode(bytes(header)), "sig": sig.b64url_encode(bytes(sig_bytes)),
           "created_at": h["created_at"], "gen_time": gen, "label": label, "last_present": last_present,
           "snapshot_digest": b["snapshot"]["snapshot_digest"]["value"], "recipients": b["recipients"],
           "policy_digest": b["policy_digest"], "tsa_policy_digest": b["tsa_policy_digest"],
           "logs": {name: {k: b["logs"][name][k] for k in ("byte_length", "byte_digest", "segments")}
                    for name in LOG_NAMES}}
    try:
        return _state.check_checkpoint(rec)
    except _state.StateError as exc:
        _fail(f"the record: {exc}")
    raise AssertionError  # pragma: no cover


@dataclass(frozen=True)
class Made:
    """A checkpoint built in memory: every byte step 8 writes (the new segment objects, the sidecar's `listed`
    ids, the custodian ciphertext for `slot`) and what the record keeps. `alerts` are the loud local alerts for
    logs whose append_only is false, `tsa_alerts` the TSA step's."""
    epoch: int
    seq: int
    strand: str
    digest: str
    created_at: str
    label: str
    gen_time: Optional[datetime]
    created_at_skew: bool
    tsa: Optional[str]
    base: bool
    slot: str
    listed: Tuple[str, ...]
    alerts: Tuple[LogAlert, ...]
    tsa_alerts: Tuple[Any, ...]
    header: bytes = field(repr=False)
    sig: bytes = field(repr=False)
    mac: bytes = field(repr=False)
    body: bytes = field(repr=False)
    tsr: Optional[bytes] = field(repr=False)
    policy: Optional[bytes] = field(repr=False)
    objects: Tuple[Tuple[str, bytes], ...] = field(repr=False)
    container: bytes = field(repr=False)

    def record(self, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return checkpoint_record(self.header, self.sig, self.body, label=self.label, gen_time=self.gen_time,
                                 previous=previous)


def _stamp_result(result: Any) -> Tuple[str, Optional[bytes], Optional[str], Optional[datetime], bool, Tuple]:
    label = getattr(result, "label", None)
    if label not in LABELS:
        _fail("the TSA step gave an unknown label")
    token = getattr(result, "token", None)
    if (label == NONE) != (token is None):
        _fail("the TSA step gives a token with PRESENT or UNVERIFIED and none with NONE")
    if token is not None:
        token = _bytes(token, "the token")
        if not token or len(token) > _tsa.MAX_TOKEN:
            _fail("the token is empty or over 64 KiB")
    gen = getattr(result, "gen_time", None)
    if label == PRESENT:
        if not isinstance(gen, datetime):
            _fail("a PRESENT token has a genTime")
        gen = _utc(gen).replace(microsecond=0)
    else:
        gen = None  # only PRESENT may anchor anything
    return (label, token, getattr(result, "tsa", None), gen, bool(getattr(result, "created_at_skew", False)),
            tuple(getattr(result, "alerts", ()) or ()))


def build_checkpoint(*, ledger_id: str, covered: Covered, devices: DevicesLog, signer_seed: bytes, strand: str,
                     epoch_keys: EpochKeys, seq: int, prev: Optional[str], created_at: str,
                     previous_logs: Optional[Mapping[str, Mapping[str, Any]]] = None,
                     previous_recipients: Optional[Sequence[Mapping[str, str]]] = None,
                     previous_snapshot_digest: Optional[str] = None, full_base: bool = False,
                     policy: Optional[bytes] = None, tsa_policy_digest: Optional[str] = None,
                     stamp: Optional[Callable[[bytes], Any]] = None,
                     rng: Callable[[int], bytes] = os.urandom) -> Made:
    """Step 7 of making a checkpoint, in memory, in the §18a order: segments, body, header, signature, the TSA
    step, MAC, the encrypted custodian container. Nothing is written.

    - `covered` holds the bytes read under the lock and `devices` the replay of `covered.devices` (its tail gives
      the epoch, the root, the recipients and C1). `epoch_keys` is the keystore's entry for that epoch.
    - `seq` and `prev` come from step 5: seq 1 has the epoch's opening checkpoint_ref (null at epoch 0), later
      seqs the previous header's digest. `created_at` is never earlier than the last covered devices or readers
      line (step 7 waits for the clock; this only checks).
    - `previous_logs`, `previous_recipients` and `previous_snapshot_digest` come from the previous entry (the
      record, or at seq 1 of a new epoch the one checkpoint_ref names); without previous logs every log gets a
      loud alert. A full base of all four logs is written when `full_base` (the first checkpoint of a strand or an
      epoch), when the recipients differ from `previous_recipients` (or it's None), or past 64 segments.
    - `stamp(header)` is the TSA step (tsa.stamp with its keywords bound) and returns a tsa.StampResult; without
      it the checkpoint is NONE.

    Refuses with CheckpointAlarm, before anything is signed, unless the strand is the only active custodian (C1)."""
    _match(_LEDGER_ID, ledger_id, "ledger_id")
    if not isinstance(covered, Covered):
        _fail("covered must be the Covered bytes read under the lock")
    if not isinstance(devices, DevicesLog) or _log_bytes(devices) != covered.devices:
        _fail("devices must be the replay of exactly the covered devices bytes")
    for name, data in covered.logs.items():
        _whole(data, f"the covered {name} log")
    tail = devices.state()
    epoch, root = tail.epoch, tail.root
    check_one_custodian(tail, strand)
    try:
        own = sig.key_id("dk", sig.public_key(signer_seed))
    except sig.SigError:
        own = None
    if own != strand:
        _fail("the signing key isn't the strand's")
    _int(seq, "seq", 1)
    if seq == 1:
        if prev != seq1_prev(devices, epoch):
            _fail("seq 1's prev must be the checkpoint_ref of the line that opened the epoch (null at epoch 0)")
    else:
        _match(_DIGEST, prev, "prev (the previous header's digest)")
    if (seq == 1) != (previous_snapshot_digest is None):
        _fail("previous_snapshot_digest is null exactly at seq 1 of an epoch")
    created = _time(created_at, "created_at")
    last = latest_line_at(covered.devices, covered.readers, None)
    if last is not None and created < last:
        _fail("created_at is earlier than the last covered devices or readers line; wait for the clock")
    if policy is not None:
        policy = _bytes(policy, "policy.json")
    _digest_or_null(tsa_policy_digest, "tsa_policy_digest")
    entries = parse_ledger(covered.ledger)

    recipients = recipients_for(epoch_keys, tail)
    new_audience = previous_recipients is None or [dict(r) for r in previous_recipients] != recipients
    plans, alerts, base = plan_logs(covered.logs, previous_logs, full_base=bool(full_base) or new_audience)
    logs, objects = seal_logs(plans, covered.logs, recipients, rng=rng)
    snapshot = build_snapshot(ledger_id=ledger_id, strand=strand, seq=seq, raw=covered.ledger, entries=entries,
                              previous_snapshot_digest=previous_snapshot_digest, created_at=created_at)
    body = build_body(snapshot=snapshot, logs=logs, recipients=recipients,
                      policy_digest=None if policy is None else digest_of(policy),
                      tsa_policy_digest=tsa_policy_digest)
    header = build_header(ledger_id=ledger_id, root=root, epoch=epoch, strand=strand, seq=seq, prev=prev,
                          created_at=created_at, devices=covered.devices, body=body)
    parse_body(body, parse_header(header))
    sig_bytes = sign_header(header, signer_seed, strand)
    label, token, tsa_name, gen, skew, tsa_alerts = _stamp_result(stamp(header)) if stamp is not None else \
        (NONE, None, None, None, False, ())
    mac = mac_line(getattr(epoch_keys, "ka", None), header, body)
    members = custodian_members(header=header, sig=sig_bytes, mac=mac, body=body, devices=covered.devices,
                                tsr=token, policy=policy)
    sealed = seal_custodian(members, recipients, rng=rng)
    return Made(epoch=epoch, seq=seq, strand=strand, digest=digest_of(header), created_at=created_at, label=label,
                gen_time=gen, created_at_skew=skew, tsa=tsa_name, base=base,
                slot=custodian_slot(getattr(epoch_keys, "kc", None), seq), listed=listed_objects(logs),
                alerts=alerts, tsa_alerts=tsa_alerts, header=header, sig=sig_bytes, mac=mac, body=body, tsr=token,
                policy=policy, objects=objects, container=sealed)


# ── Verifying one checkpoint (§18a "Verifying one checkpoint") ──

@dataclass(frozen=True)
class Verified:
    """A checkpoint that passed verify_checkpoint. `label` is its own (PRESENT, UNVERIFIED or NONE); `gen_time` is
    set for PRESENT only. `prefix_idx` is the last devices line inside its cited prefix, `cited_devices` those
    bytes, and `newest` the digest of the devices log it was checked against. `body` and the custodian members
    are set on the custodian side only."""
    epoch: int
    seq: int
    strand: str
    digest: str
    prev: Optional[str]
    created_at: str
    devices_length: int
    devices_digest: str
    prefix_idx: int
    label: str
    gen_time: Optional[datetime]
    created_at_skew: bool
    custodian: bool
    newest: str
    token: Optional[_tsa.TokenCheck] = field(repr=False)
    header: bytes = field(repr=False)
    sig: bytes = field(repr=False)
    tsr: Optional[bytes] = field(repr=False)
    cited_devices: bytes = field(repr=False)
    body: Optional[Mapping[str, Any]] = field(repr=False)
    body_bytes: Optional[bytes] = field(repr=False)
    policy: Optional[bytes] = field(repr=False)

    def record(self, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """The state.json record for adopting this checkpoint (custodian side only)."""
        if self.body_bytes is None:
            _fail("only a checkpoint verified on the custodian side becomes the record")
        return checkpoint_record(self.header, self.sig, self.body_bytes, label=self.label, gen_time=self.gen_time,
                                 previous=previous)

    def seen(self) -> Dict[str, Any]:
        return seen_mark(self)


def seen_mark(v: Verified) -> Dict[str, Any]:
    """The `seen` high-water mark for a verified checkpoint: `{epoch, seq, strand, digest, header, sig, devices}`
    with `devices` the bytes it cited. Written only by the fetch paths, after the chain walk to the mark passes."""
    mark = {"epoch": v.epoch, "seq": v.seq, "strand": v.strand, "digest": v.digest,
            "header": sig.b64url_encode(v.header), "sig": sig.b64url_encode(v.sig),
            "devices": sig.b64url_encode(v.cited_devices)}
    try:
        return _state.check_seen(mark)
    except _state.StateError as exc:
        _fail(f"the seen mark: {exc}")
    raise AssertionError  # pragma: no cover


def _replay(devices: bytes, replay: Optional[DevicesLog], ledger_id: str, root: str, now: datetime) -> DevicesLog:
    if replay is not None:
        if not isinstance(replay, DevicesLog) or _log_bytes(replay) != devices or replay.root != root:
            _fail("the replay given isn't of these devices bytes under the pinned root")
        return replay
    try:
        return _logs.replay_devices(devices, ledger_id=ledger_id, root=root, now=now)
    except LogError as exc:
        _fail(f"the newest devices log doesn't replay from the pinned root: {exc}")
    raise AssertionError  # pragma: no cover


def _extends(newer: bytes, byte_length: int, digest: str, what: str) -> None:
    try:
        _logs.check_extends(newer, byte_length=byte_length, digest=digest)
    except LogRollback:
        raise CheckpointRollback(f"ROLLBACK: {what} is shorter than an earlier object cited") from None
    except LogFork:
        raise CheckpointFork(f"FORK: {what} doesn't start with the bytes an earlier object cited") from None
    except LogError as exc:
        raise CheckpointFork(f"FORK: {what}: {exc}") from None


def _alarmed(custodian: bool, run: Callable[[], Any]) -> Any:
    """Run a verification. On the custodian side every failure is ALARM; anywhere, hostile values that slip past a
    type check fail closed as a CheckpointError, never a crash."""
    try:
        return run()
    except CheckpointAlarm:
        raise
    except CheckpointError as exc:
        if custodian:
            raise CheckpointAlarm(f"ALARM: {exc}") from None
        raise
    except _HOSTILE as exc:
        text = f"the checkpoint is malformed ({type(exc).__name__})"
        raise (CheckpointAlarm("ALARM: " + text) if custodian else CheckpointError(text)) from None


def verify_checkpoint(shipped: Shipped, *, ledger_id: str, root: str, devices: bytes, now: datetime,
                      pins: Iterable[_tsa.TsaPin] = (), slot: Optional[Tuple[int, int]] = None,
                      cited: Optional[str] = None, epochs: Optional[Mapping[int, EpochKeys]] = None,
                      segments: Optional[Mapping[str, bytes]] = None,
                      replay: Optional[DevicesLog] = None, _named_ref: bool = False) -> Verified:
    """Verify one checkpoint over its shipped bytes (§18a steps 1 to 6).

    1. The header digest is `cited` (when given); strict JCS and the closed schema; the ledger_id is the pinned
       one; and the header's (epoch, seq) is the `slot`'s it came from (otherwise ALARM, "slot replay").
    2. `sig.key_id` is the strand, and the signature verifies over SI("checkpoint", header), under the key the
       newest devices log (replayed from the pinned `root`) enrolled for that strand, a custodian. This comes
       before anything about the devices prefix, so a header nobody signed is a bad signature, never a ROLLBACK
       or FORK verdict about the devices log.
    3. The newest devices log starts with the cited prefix (ROLLBACK, FORK). The state at the prefix gives the
       epoch and root, which must match, and the signer, an active custodian there with that same key; at the
       newest log (for an earlier epoch, the line before the one that closed it) it isn't revoked, and retired
       only after its prefix. Seq 1's prev is the opening line's checkpoint_ref.
    4. created_at isn't more than 5 minutes after `now`, the verifier's clock.
    5. The token's label under `pins` (NONE without one).
    6. With `epochs` (the keystore's, or derived from RK), the custodian side too: the MAC, body_digest, the
       closed body, logs.devices equal to the header's devices, contiguous segment ranges, recipients equal to
       the epoch's RK recipient plus the boxes active at the prefix, the container's stanza count (and each
       object in `segments`, fetched ciphertexts by id) equal to len(recipients), the snapshot's ledger byte
       digest, the policy digest and member, and the devices member exactly the cited bytes. Any failure here,
       or anywhere on this side, is ALARM.

    Raises CheckpointError (a reader exits 10); CheckpointAlarm for a slot replay and on the custodian side;
    CheckpointRollback or CheckpointFork for a devices log that doesn't extend the cited prefix.

    `_named_ref` is adoption's alone (`_Run._link`): the record it links a new epoch's seq 1 to is the checkpoint
    the epoch's root-signed opening line names as checkpoint_ref. That line vouches for it, so a separate
    device_revoke of its signer inside the old epoch (the REVOKED_SIGNER way out through recovery) is waived:
    only the revoked-at-the-horizon test, never the signature, the prefix or the signer being active there."""
    custodian = epochs is not None
    return _alarmed(custodian, lambda: _verify_one(shipped, ledger_id=ledger_id, root=root, devices=devices,
                                                   now=now, pins=pins, slot=slot, cited=cited, epochs=epochs,
                                                   segments=segments, replay=replay, named_ref=bool(_named_ref)))


def _verify_one(shipped: Any, *, ledger_id: str, root: str, devices: Any, now: datetime, pins: Iterable[Any],
                slot: Optional[Tuple[int, int]], cited: Optional[str], epochs: Optional[Mapping[int, Any]],
                segments: Optional[Mapping[str, bytes]], replay: Optional[DevicesLog],
                named_ref: bool = False) -> Verified:
    if not isinstance(shipped, Shipped):
        _fail("verify_checkpoint takes the Shipped bytes of a checkpoint")
    now_n = _utc(now)
    header = _bytes(shipped.header, "the header")
    # 1. The digest cited, the closed schema, the pinned ledger, the slot.
    d = digest_of(header)
    if cited is not None and d != cited:
        _fail("the header's digest isn't the one cited")
    h = parse_header(header)
    if h["ledger_id"] != ledger_id:
        _fail("the header's ledger_id isn't the pinned ledger")
    if slot is not None:
        se, ss = slot
        if (h["epoch"], h["seq"]) != (se, ss):
            raise CheckpointAlarm(f"ALARM: slot replay: the header says epoch {h['epoch']} seq {h['seq']}, but it "
                                  f"came from the slot for epoch {se} seq {ss}")
    # 2. The signature names the strand, and verifies under the key the newest devices log enrolled for it
    #    (a kid is enrolled once, with one key: logs.py never re-enrols a kid or reuses a key). It's checked
    #    before the devices prefix the header cites, so nothing unsigned gets a ROLLBACK or FORK verdict.
    try:
        s = sig.parse_sig(_bytes(shipped.sig, "the signature"))
    except sig.SigError as exc:
        _fail(f"the checkpoint signature: {exc}")
    if s["key_id"] != h["strand"]:
        _fail("the signature's key_id isn't the header's strand")
    devices = _bytes(devices, "the devices log")
    log = _replay(devices, replay, ledger_id, root, now_n)
    known = next((st.devices[h["strand"]] for st in log.states if h["strand"] in st.devices), None)
    if known is None or known.cls != "custodian":
        _fail("the signer isn't an active custodian at the checkpoint's devices prefix")
    try:
        sig.verify(SIG_KIND, header, s, known.pub)
    except sig.SigError as exc:
        _fail(f"bad checkpoint signature: {exc}")
    # 3. The devices log at the cited prefix.
    length, ddigest = h["devices"]["byte_length"], h["devices"]["digest"]
    _extends(devices, length, ddigest, "the newest devices log")
    idx, at_prefix = prefix_state(log, length)
    if at_prefix.epoch != h["epoch"]:
        _fail(f"the header's epoch {h['epoch']} isn't the epoch at its devices prefix ({at_prefix.epoch})")
    if at_prefix.root != h["root"]:
        _fail("the header's root isn't the root current at its devices prefix")
    signer = at_prefix.devices.get(h["strand"])
    if signer is None or signer.cls != "custodian" or signer.pub != known.pub:
        _fail("the signer isn't an active custodian at the checkpoint's devices prefix")
    status = log.state(horizon(log, h["epoch"])).status.get(h["strand"])
    if status is None or (status[0] == "revoked" and not named_ref):
        _fail("the signer is revoked in the newest devices log")
    if status[0] == "retired" and not idx < status[1]:  # pragma: no cover - an active signer retires later
        _fail("the signer was retired before the end of the checkpoint's devices prefix")
    if h["seq"] == 1:
        want = seq1_prev(log, h["epoch"])
        if h["prev"] != want:
            _fail(f"seq 1's prev isn't the checkpoint_ref on the line that opened epoch {h['epoch']}")
    # 4. Not from the future.
    created = _time(h["created_at"], "the header's created_at")
    if created > now_n + CREATED_AT_AHEAD:
        _fail("created_at is more than 5 minutes after the verifier's clock")
    # 5. The token.
    check = None
    if shipped.tsr is None:
        label, gen, skew = NONE, None, False
    else:
        check = _tsa.check_token(_bytes(shipped.tsr, "the token"), header, tuple(pins), now=now_n, created_at=created)
        label, skew = check.label, check.created_at_skew
        gen = check.gen_time if label == PRESENT else None
    cited_devices = devices[:length]
    # 6. The custodian side.
    body = None
    if epochs is not None:
        body = _custodian_checks(shipped, h, header, epochs, at_prefix, cited_devices, segments)
    return Verified(epoch=h["epoch"], seq=h["seq"], strand=h["strand"], digest=d, prev=h["prev"],
                    created_at=h["created_at"], devices_length=length, devices_digest=ddigest, prefix_idx=idx,
                    label=label, gen_time=gen, created_at_skew=skew, custodian=epochs is not None,
                    newest=digest_of(devices), token=check, header=header, sig=bytes(shipped.sig),
                    tsr=None if shipped.tsr is None else bytes(shipped.tsr), cited_devices=cited_devices, body=body,
                    body_bytes=None if body is None else bytes(shipped.body),
                    policy=None if shipped.policy is None else bytes(shipped.policy))


def _custodian_checks(shipped: Shipped, h: Mapping[str, Any], header: bytes, epochs: Mapping[int, Any],
                      at_prefix: DeviceState, cited_devices: bytes, segments: Optional[Mapping[str, bytes]]) -> dict:
    keys = epochs.get(h["epoch"]) if isinstance(epochs, Mapping) else None
    if keys is None:
        _fail(f"no keys for epoch {h['epoch']} to check the MAC and recipients with")
    if shipped.mac is None or shipped.body is None or shipped.devices is None:
        _fail("the custodian container must hold irp/checkpoint.mac, irp/body.json and irp/devices.jsonl")
    body_bytes = _bytes(shipped.body, "the body")
    check_mac(shipped.mac, keys.ka, header, body_bytes)
    if digest_of(body_bytes) != h["body_digest"]:
        _fail("the header's body_digest isn't the sha256 of irp/body.json")
    b = parse_body(body_bytes, h)
    logs = b["logs"]
    if (logs["devices"]["byte_length"], logs["devices"]["byte_digest"]) != \
            (h["devices"]["byte_length"], h["devices"]["digest"]):
        _fail("the body's logs.devices isn't the header's devices")
    want = recipients_for(keys, at_prefix)
    if b["recipients"] != want:
        _fail("the body's recipients aren't the epoch's RK recipient plus the boxes active at the devices prefix")
    if type(shipped.stanzas) is not int:
        _fail("the custodian container's stanza count is needed to check it against the recipients")
    if shipped.stanzas != len(want):
        _fail("the custodian container's stanza count isn't len(recipients)")
    listed = set(listed_objects(logs))
    for oid, ct in (segments or {}).items():
        if oid not in listed:
            _fail("a fetched segment object isn't listed in the body")
        ct = _bytes(ct, "a fetched segment object")
        if oid != "o/" + hashlib.sha256(ct).hexdigest():
            _fail("a fetched segment object isn't named by the hash of its ciphertext")
        try:
            count = age.stanza_count(ct)
        except age.AgeError:
            _fail("a fetched segment object isn't an age file")
        if count != len(want):
            _fail("a fetched segment's stanza count isn't len(recipients)")
    if "sha256-" + b["snapshot"]["manifest"]["ledger"]["byte_digest"]["value"] != logs["ledger"]["byte_digest"]:
        _fail("the snapshot's ledger byte digest isn't logs.ledger.byte_digest")
    if (b["policy_digest"] is None) != (shipped.policy is None):
        _fail("policy_digest and the irp/policy.json member are both present or both absent")
    if shipped.policy is not None and digest_of(_bytes(shipped.policy, "policy.json")) != b["policy_digest"]:
        _fail("policy_digest isn't the sha256 of irp/policy.json")
    if _bytes(shipped.devices, "irp/devices.jsonl") != cited_devices:
        _fail("irp/devices.jsonl isn't exactly the cited devices bytes")
    return b


# ── Verifying a chain (§18a "Verifying a chain") ──

@dataclass(frozen=True)
class Coverage:
    """The first PRESENT checkpoint covering a log line, and its genTime."""
    epoch: int
    seq: int
    digest: str
    gen_time: datetime


@dataclass(frozen=True)
class Chain:
    """What verify_chain found. `devices_first_present[i]` is the first PRESENT checkpoint whose devices prefix
    holds devices line i (None if none does), `readers_first_present` the same for readers lines on the custodian
    side. `transitive` maps a checkpoint that isn't PRESENT to the first later PRESENT one reaching it by prev
    links (including through a root-signed checkpoint_ref). `abandoned` lists the old-epoch checkpoints after the
    one an epoch change continued from. `unchecked` lists the (epoch, seq, log) append_only claims there were no
    bytes to check."""
    checkpoints: Tuple[Verified, ...]
    devices_first_present: Tuple[Optional[Coverage], ...]
    readers_first_present: Tuple[Optional[Coverage], ...]
    transitive: Mapping[Tuple[int, int], Tuple[int, int]]
    abandoned: Tuple[Tuple[int, int], ...]
    unchecked: Tuple[Tuple[int, int, str], ...]

    def label(self, epoch: int, seq: int) -> str:
        """PRESENT, "TRANSITIVE via (e, s)", UNVERIFIED or NONE."""
        for v in self.checkpoints:
            if (v.epoch, v.seq) == (epoch, seq):
                if v.label == PRESENT:
                    return PRESENT
                via = self.transitive.get((epoch, seq))
                return f"TRANSITIVE via ({via[0]}, {via[1]})" if via else v.label
        _fail(f"({epoch}, {seq}) isn't in the chain")
        raise AssertionError  # pragma: no cover

    def revocation_time(self, idx: int) -> Optional[datetime]:
        """§18.4's historical revocation time for devices line `idx`: the genTime of the first PRESENT checkpoint
        that holds it, or None when none does yet."""
        c = self.devices_first_present[idx]
        return None if c is None else c.gen_time


def check_snapshot_link(previous_snapshot_digest: Optional[str], v: Verified) -> None:
    """Custodian side, within an epoch: `v`'s snapshot names the previous checkpoint's snapshot_digest value (a
    body's, or the record's `snapshot_digest`); null at seq 1. ALARM otherwise."""
    if v.body is None:
        raise CheckpointAlarm("ALARM: the snapshot link is checked on a checkpoint verified on the custodian side")
    if v.body["snapshot"]["manifest"]["previous_snapshot_digest"] != previous_snapshot_digest:
        raise CheckpointAlarm(f"ALARM: the snapshot of epoch {v.epoch} seq {v.seq} doesn't name the previous "
                              "checkpoint's snapshot_digest")


def check_append_only(previous: Mapping[str, Mapping[str, Any]], v: Verified, *,
                      logs: Optional[Mapping[str, bytes]] = None,
                      log_bytes: Optional[Callable[[Verified, str], Optional[bytes]]] = None) -> Tuple[str, ...]:
    """Custodian side: each body log of `v` with append_only true extends its previous entry (`previous` is the
    predecessor's body logs, or the record's logs, which carry no append_only). A shorter log is ROLLBACK, the
    same length with other bytes or a prefix that differs is FORK. Extension is proved by equal (length, digest),
    by a segment list that starts with the previous one, or by bytes: the devices bytes `v` cited, `log_bytes(v,
    name)` (bytes restored from its segments, say), or `logs[name]` (the newest bytes) when they start with what
    `v` covered. Returns the logs whose claim had no bytes to check it with."""
    if v.body is None:
        raise CheckpointAlarm("ALARM: append_only is checked on a checkpoint verified on the custodian side")
    if not isinstance(previous, Mapping) or set(previous) != set(LOG_NAMES):
        raise CheckpointAlarm("ALARM: the previous entries are the four logs'")
    unchecked: List[str] = []
    for name in LOG_NAMES:
        eb = v.body["logs"][name]
        if not eb["append_only"]:
            continue
        try:
            ea = _previous_entry(previous[name], name)
        except CheckpointError as exc:
            raise CheckpointAlarm(f"ALARM: the previous {name} entry: {exc}") from None
        what = f"the {name} log at epoch {v.epoch} seq {v.seq} (append_only)"
        if eb["byte_length"] < ea["byte_length"]:
            raise CheckpointRollback(f"ROLLBACK: {what} is shorter than the previous checkpoint's")
        if eb["byte_length"] == ea["byte_length"]:
            if eb["byte_digest"] != ea["byte_digest"]:
                raise CheckpointFork(f"FORK: {what} has the previous checkpoint's length but other bytes")
            continue
        if eb["segments"][:len(ea["segments"])] == ea["segments"]:
            continue  # the same objects still cover the earlier bytes
        data = _bytes_for(v, name, eb, logs, log_bytes)
        if data is None:
            unchecked.append(name)
            continue
        _extends(data, ea["byte_length"], ea["byte_digest"], what)
    return tuple(unchecked)


def _bytes_for(v: Verified, name: str, entry: Mapping[str, Any], logs: Optional[Mapping[str, bytes]],
               log_bytes: Optional[Callable[[Verified, str], Optional[bytes]]]) -> Optional[bytes]:
    """The bytes `v` covered for one log, when the verifier has them."""
    if name == "devices":
        return v.cited_devices
    if log_bytes is not None:
        got = log_bytes(v, name)
        if got is not None:
            got = _bytes(got, f"the {name} bytes given")
            if len(got) != entry["byte_length"] or digest_of(got) != entry["byte_digest"]:
                raise CheckpointAlarm(f"ALARM: the {name} bytes given for epoch {v.epoch} seq {v.seq} aren't the "
                                      "ones it covered")
            return got
    data = None if logs is None else logs.get(name)
    n = entry["byte_length"]
    if data is not None:
        data = _bytes(data, f"the {name} log")
        if len(data) >= n and digest_of(data[:n]) == entry["byte_digest"]:
            return data[:n]
    return None


def verify_chain(chain: Sequence[Verified], *, ledger_id: str, root: str, devices: bytes, now: datetime,
                 replay: Optional[DevicesLog] = None, readers: Optional[bytes] = None, custodian: bool = False,
                 logs: Optional[Mapping[str, bytes]] = None,
                 log_bytes: Optional[Callable[[Verified, str], Optional[bytes]]] = None) -> Chain:
    """Verify a run of checkpoints, each already through verify_checkpoint against the same newest `devices`, in
    ascending (epoch, seq) order.

    - Within an epoch seq is contiguous and each `prev` is the previous header's digest. A new epoch starts at
      seq 1 with `prev` the opening line's checkpoint_ref; it continues from the walked checkpoint that ref names,
      and the old-epoch ones after it are reported as abandoned (not FORK).
    - Each later checkpoint's devices prefix extends the earlier one's (ROLLBACK, FORK: both ALARM).
    - The strand changes within an epoch only across a run of device_rotate lines inside the new checkpoint's
      prefix, the first from the old strand (after the old strand's last prefix) and each later one's `old` the
      previous one's `device.kid`; any other arrival is ALARM.
    - created_at never goes back along the links; genTime never goes back over PRESENT checkpoints within an
      epoch (ALARM).
    - With `custodian`, every checkpoint must have been verified on the custodian side and `readers` (the newest
      readers log) is needed: the snapshot's previous_snapshot_digest links within an epoch, and each body log
      with append_only true extends its predecessor's. That's proved by equal (length, digest), by its segment
      list starting with the predecessor's, or by bytes: the devices bytes each checkpoint cited, `log_bytes(v,
      name)` (say, restored from segments), or `logs[name]` (the newest bytes) when they start with what the
      checkpoint covered. A claim with no bytes to check it is listed in `unchecked`. A first checkpoint with a
      null prev claims no append_only at all. Then each devices and readers line dated more than 1 hour after the
      genTime of the first PRESENT checkpoint holding it is ALARM (§14.5a rule 3).

    Raises CheckpointError (CheckpointAlarm on the custodian side, and for a genTime regression, ROLLBACK, FORK
    or a strand change); returns a Chain."""
    return _alarmed(bool(custodian), lambda: _verify_chain(
        chain, ledger_id=ledger_id, root=root, devices=devices, now=now, replay=replay, readers=readers,
        custodian=bool(custodian), logs=logs, log_bytes=log_bytes))


def _strand_run(log: DevicesLog, a: Verified, b: Verified) -> None:
    cur = a.strand
    for idx in range(a.prefix_idx + 1, b.prefix_idx + 1):
        body = _logs.parse_line(log.lines[idx], DEVICES_KIND)[0]
        if body["event"] == "device_rotate" and body["old"] == cur:
            cur = body["device"]["kid"]
            if cur == b.strand:
                return
    raise CheckpointAlarm(f"ALARM: the strand changed from {a.strand} to {b.strand} at epoch {b.epoch} seq {b.seq} "
                          "without a run of device_rotate lines inside its devices prefix; a strand arrives only by "
                          "device_rotate or an epoch change")


def _no_append_claims(v: Verified) -> None:
    for name in LOG_NAMES:
        if v.body["logs"][name]["append_only"]:
            raise CheckpointAlarm(f"ALARM: epoch {v.epoch} seq {v.seq} claims the {name} log is append_only with no "
                                  "checkpoint before it")


def _verify_chain(chain: Any, *, ledger_id: str, root: str, devices: Any, now: datetime,
                  replay: Optional[DevicesLog], readers: Optional[bytes], custodian: bool,
                  logs: Optional[Mapping[str, bytes]],
                  log_bytes: Optional[Callable[[Verified, str], Optional[bytes]]]) -> Chain:
    items = tuple(chain)
    if not items:
        _fail("a chain has at least one checkpoint")
    devices = _bytes(devices, "the devices log")
    newest = digest_of(devices)
    if custodian and readers is None:
        _fail("custodian mode needs the newest readers log")
    for v in items:
        if not isinstance(v, Verified):
            _fail("a chain is a run of checkpoints that passed verify_checkpoint")
        if v.newest != newest:
            _fail("a checkpoint in the chain was verified against another devices log")
        if custodian and not v.custodian:
            _fail("custodian mode needs every checkpoint verified on the custodian side")
    now_n = _utc(now)
    log = _replay(devices, replay, ledger_id, root, now_n)
    keys = [(v.epoch, v.seq) for v in items]
    if any(x >= y for x, y in zip(keys, keys[1:])):
        _fail("the chain must be in ascending (epoch, seq) order without repeats")
    readers_b = b"" if readers is None else _bytes(readers, "the readers log")
    newest_logs = {name: _bytes(data, f"the {name} log") for name, data in (logs or {}).items()}
    if readers is not None:
        newest_logs.setdefault("readers", readers_b)
    unchecked: List[Tuple[int, int, str]] = []
    links: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    abandoned: List[Tuple[int, int]] = []
    run_start = 0
    if custodian and items[0].prev is None:
        _no_append_claims(items[0])
    for i in range(1, len(items)):
        a, b = items[i - 1], items[i]
        _extends(b.cited_devices, a.devices_length, a.devices_digest,
                 f"the devices log epoch {b.epoch} seq {b.seq} cites")
        if b.epoch == a.epoch:
            if b.seq != a.seq + 1:
                _fail(f"seq isn't contiguous within epoch {b.epoch}: {a.seq} then {b.seq}")
            if b.prev != a.digest:
                _fail(f"the prev of epoch {b.epoch} seq {b.seq} isn't the digest of seq {a.seq}")
            if b.strand != a.strand:
                _strand_run(log, a, b)
            if custodian:
                check_snapshot_link(a.body["snapshot"]["snapshot_digest"]["value"], b)
            pred: Optional[Verified] = a
        else:
            if b.seq != 1:
                _fail(f"epoch {b.epoch} starts at seq 1, not {b.seq}")
            ref = seq1_prev(log, b.epoch)
            if b.prev != ref:
                _fail(f"the prev of epoch {b.epoch} seq 1 isn't the checkpoint_ref on the line that opened it")
            run = items[run_start:i]
            pos = None
            if ref is not None:
                pos = next((k for k in range(len(run) - 1, -1, -1) if run[k].digest == ref), None)
            pred = run[pos] if pos is not None else None
            abandoned += [(x.epoch, x.seq) for x in (run[pos + 1:] if pos is not None else run)]
            run_start = i
        if pred is not None:
            links.setdefault((pred.epoch, pred.seq), []).append((b.epoch, b.seq))
            if _time(b.created_at, "created_at") < _time(pred.created_at, "created_at"):
                _fail(f"created_at goes backwards from epoch {pred.epoch} seq {pred.seq} to epoch {b.epoch} seq "
                      f"{b.seq}")
            if custodian:
                unchecked += [(b.epoch, b.seq, name) for name in check_append_only(
                    {name: pred.body["logs"][name] for name in LOG_NAMES}, b, logs=newest_logs, log_bytes=log_bytes)]
        elif custodian and b.prev is None:
            _no_append_claims(b)
        elif custodian:
            unchecked += [(b.epoch, b.seq, name) for name in LOG_NAMES if b.body["logs"][name]["append_only"]]

    last_gen: Dict[int, Verified] = {}
    for v in items:
        if v.label != PRESENT:
            continue
        before = last_gen.get(v.epoch)
        if before is not None and v.gen_time < before.gen_time:
            raise CheckpointAlarm(f"ALARM: genTime goes backwards in epoch {v.epoch}: seq {v.seq} is PRESENT before "
                                  f"seq {before.seq}")
        last_gen[v.epoch] = v

    devices_table: List[Optional[Coverage]] = [None] * len(log.lines)
    upto = 0
    for v in items:
        if v.label == PRESENT:
            cov = Coverage(v.epoch, v.seq, v.digest, v.gen_time)
            for k in range(upto, v.prefix_idx + 1):
                devices_table[k] = cov
            upto = max(upto, v.prefix_idx + 1)
    readers_table: List[Optional[Coverage]] = []
    if custodian:
        try:
            rlog = _logs.replay_readers(readers_b, log, ledger_id=ledger_id, now=now_n)
        except LogError as exc:
            _fail(f"the newest readers log doesn't replay: {exc}")
        readers_table = [None] * len(rlog.lines)
        upto = 0
        for v in items:
            if v.label != PRESENT:
                continue
            entry = v.body["logs"]["readers"]
            _extends(readers_b, entry["byte_length"], entry["byte_digest"], "the newest readers log")
            n = readers_b.count(b"\n", 0, entry["byte_length"])
            cov = Coverage(v.epoch, v.seq, v.digest, v.gen_time)
            for k in range(upto, n):
                readers_table[k] = cov
            upto = max(upto, n)
        for k, cov in enumerate(devices_table):
            if cov is not None and _time(log.state(k).at, "a devices line's at") > cov.gen_time + LINE_SLACK:
                raise CheckpointAlarm(f"ALARM: devices line {k} is dated more than 1 hour after the genTime of epoch "
                                      f"{cov.epoch} seq {cov.seq}, the first PRESENT checkpoint holding it")
        ats = _line_ats(readers_b, READERS_KIND, "readers")
        for k, cov in enumerate(readers_table):
            if cov is not None and ats[k] > cov.gen_time + LINE_SLACK:
                raise CheckpointAlarm(f"ALARM: readers line {k} is dated more than 1 hour after the genTime of epoch "
                                      f"{cov.epoch} seq {cov.seq}, the first PRESENT checkpoint holding it")

    present = {(v.epoch, v.seq) for v in items if v.label == PRESENT}
    transitive: Dict[Tuple[int, int], Tuple[int, int]] = {}
    for v in items:
        key = (v.epoch, v.seq)
        if key in present:
            continue
        best, stack, seen = None, list(links.get(key, ())), set()
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in present:
                best = x if best is None or x < best else best
                continue
            stack.extend(links.get(x, ()))
        if best is not None:
            transitive[key] = best
    return Chain(checkpoints=items, devices_first_present=tuple(devices_table),
                 readers_first_present=tuple(readers_table), transitive=transitive, abandoned=tuple(abandoned),
                 unchecked=tuple(unchecked))


# ── Part 2: making, staging and adopting checkpoints (§18a "Making a checkpoint", "Adopting", "State") ──

WRITTEN = "written"            # relay_seen answers: from 2.7 a HEAD of the slot (200 or 410 means written)
NOT_WRITTEN = "not_written"
UNKNOWN = "unknown"
RELAY_ANSWERS = (WRITTEN, NOT_WRITTEN, UNKNOWN)
OBJECTS_DIR = "o"
SIDECAR_SUFFIX = ".objects"
TMP_SUFFIX = ".tmp"
MIRROR_DIR = "mirror"          # ~/.irp-roam/mirror/{m,o}/ (§20.5)
DUE_AFTER = timedelta(hours=24)
CLOCK_WAIT = timedelta(minutes=5)
TSA_OPTIONS = frozenset({"allow_http", "timeout", "resolver", "context_factory"})  # for tests only
FORK_PROOF_KIND = "fork-proof"
FORK_PROOF_KEYS = frozenset({"v", "kind", "a", "b"})
FORK_SIDE_KEYS = frozenset({"checkpoint", "sig", "devices"})
MISSING_RECORD = "the checkpoint record is missing: rebuild it from the relay with the drill's Path B"
REVOKED_SIGNER = ("the last checkpoint's signer has been revoked; this epoch can only continue through recovery; "
                  "nothing was signed")
BEHIND = "the checkpoint record is behind"
FOREIGN = "state.json is behind, or someone else is signing"
TSA_CHANGED = ("ALERT: tsa.json isn't the one in force at the last checkpoint (tsa_policy_digest changed): compare "
               "its pins with the national Trusted List")

_STAGED_NAME = re.compile(r"custodian-(0|[1-9][0-9]{0,15})-([1-9][0-9]{0,15})")
_SIDECAR_LINE = re.compile(rb"o/[0-9a-f]{64}")


def relay_unknown(epoch: int, seq: int) -> str:
    """The 2.6 `relay_seen`: there's no relay client yet (2.7 HEADs the relay), so every slot is "unknown". That
    passes the slot check only because step 0 already trusts the record."""
    return UNKNOWN


# ── Staged names and the sidecar ──

def staged_name(epoch: int, seq: int) -> str:
    """`custodian-<epoch>-<seq>`: a staged custodian ciphertext in staging/."""
    return f"custodian-{_int(epoch, 'epoch')}-{_int(seq, 'seq', 1)}"


def sidecar_name(epoch: int, seq: int) -> str:
    """`custodian-<epoch>-<seq>.objects`: the plaintext list of the `o/` ids that checkpoint lists."""
    return staged_name(epoch, seq) + SIDECAR_SUFFIX


def sidecar_bytes(listed: Sequence[str]) -> bytes:
    """A sidecar's exact bytes: each listed `o/<64 hex>` id once, in listed order, one per line. The relay sees
    these names anyway, so the sidecar stays plaintext."""
    ids = [_match(_OBJECT, oid, "a listed object") for oid in listed]
    if len(set(ids)) != len(ids):
        _fail("a sidecar lists each object once")
    return "".join(oid + "\n" for oid in ids).encode("ascii")


def parse_sidecar(data: bytes) -> Tuple[str, ...]:
    """The ids a sidecar lists, strictly: whole lines of `o/<64 hex>`, each once."""
    data = _bytes(data, "a sidecar")
    if data and not data.endswith(b"\n"):
        _fail("a sidecar is whole lines")
    out: List[str] = []
    for line in data.split(b"\n")[:-1] if data else []:
        if not _SIDECAR_LINE.fullmatch(line):
            _fail("a sidecar holds one o/<64 hex> id per line")
        out.append(line.decode("ascii"))
    if len(set(out)) != len(out):
        _fail("a sidecar lists each object once")
    return tuple(out)


def _listdir(folder: Path) -> List[str]:
    try:
        return sorted(os.listdir(folder))
    except FileNotFoundError:
        return []


def _staging_folder(folder: Path, what: str) -> bool:
    """Whether a staging folder exists; a symlink or a file in its place is ALARM."""
    try:
        st = os.lstat(folder)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise CheckpointAlarm(f"ALARM: {what} must be a real folder, not a symlink or a file")
    return True


def _staged_file(path: Path, what: str) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise CheckpointAlarm(f"ALARM: {what} is a symlink; staged files are real files, and they stay where they are")
    if not stat.S_ISREG(st.st_mode):
        raise CheckpointAlarm(f"ALARM: {what} isn't a regular file; the files stay where they are")


def staged_checkpoints(staging: Path | str) -> Dict[Tuple[int, int], Path]:
    """The custodian files in staging/, by (epoch, seq). A name starting `custodian-` that isn't exactly
    `custodian-<epoch>-<seq>` (or its sidecar, or a temp file) is ALARM, since two names could claim one seq; so
    is a staged file that's a symlink or not a regular file."""
    staging = Path(staging)
    out: Dict[Tuple[int, int], Path] = {}
    if not _staging_folder(staging, "staging/"):
        return out
    for name in _listdir(staging):
        if not name.startswith("custodian-") or name.endswith(TMP_SUFFIX):
            continue
        base = name[:-len(SIDECAR_SUFFIX)] if name.endswith(SIDECAR_SUFFIX) else name
        m = _STAGED_NAME.fullmatch(base)
        if m is None or int(m.group(1)) > MAX_INT or int(m.group(2)) > MAX_INT:
            raise CheckpointAlarm(f"ALARM: staging/{name} isn't a canonical custodian-<epoch>-<seq> name, so two "
                                  "files could claim one seq; the files stay where they are")
        _staged_file(staging / name, f"staging/{name}")
        if base == name:
            out[(int(m.group(1)), int(m.group(2)))] = staging / name
    return out


# ── Cadence (§18a "Cadence") ──

def due(record: Optional[Mapping[str, Any]], *, epoch: int, logs: Mapping[str, bytes], now: datetime
        ) -> Optional[str]:
    """Why a checkpoint is due, or None. Due when there's no record for this epoch, when the ledger's, the devices
    log's or the readers log's covered length or digest differs from the record, or when 24 hours have passed
    since the record's created_at. The disclosures log rides along without triggering one. `logs` holds the
    covered bytes (through the last newline). Evaluated after adoption."""
    _int(epoch, "epoch")
    if record is None or record["epoch"] != epoch:
        return f"there's no checkpoint for epoch {epoch} yet"
    for name in ("ledger", "devices", "readers"):
        data = _bytes(logs[name], f"the {name} log")
        entry = record["logs"][name]
        if len(data) != entry["byte_length"] or digest_of(data) != entry["byte_digest"]:
            return f"the {name} log changed since epoch {record['epoch']} seq {record['seq']}"
    if _utc(now) - _time(record["created_at"], "the record's created_at") >= DUE_AFTER:
        return "24 hours have passed since the last checkpoint"
    return None


# ── Results ──

@dataclass(frozen=True)
class Adoption:
    """What steps 0 to 2 did: the current epoch, the record afterwards, what was adopted (in order), the staged
    files of an older epoch above the record (left for upload, abandoned at the epoch change), what step 1
    removed (paths relative to staging/), and notes."""
    epoch: int
    record: Optional[Mapping[str, Any]] = field(repr=False)
    adopted: Tuple[Tuple[int, int], ...]
    abandoned: Tuple[Tuple[int, int], ...]
    cleaned: Tuple[str, ...]
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class MakeResult:
    """What make_checkpoint did. `made` is None when nothing was made: `skipped` says why (not due, or the new
    strand already has its first checkpoint). `due` is why one was due (with `when_due`). `alerts` are the loud
    local alerts (append_only false, the TSA, tsa.json changed); `notes` the rest."""
    made: Optional[Made]
    record: Optional[Mapping[str, Any]] = field(repr=False)
    epoch: int = 0
    adopted: Tuple[Tuple[int, int], ...] = ()
    abandoned: Tuple[Tuple[int, int], ...] = ()
    cleaned: Tuple[str, ...] = ()
    alerts: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    skipped: Optional[str] = None
    due: Optional[str] = None


# ── One run under the caller's lock ──

def _check_lock(lock: Any, keys_dir: Path) -> None:
    if not (lock is not None and getattr(lock, "held", False) and getattr(lock, "exclusive", False)):
        raise CheckpointError("checkpoints are made and adopted only with roam.lock held exclusively by the caller; "
                              "nothing was done")
    try:
        same = Path(lock.keys_dir).resolve() == Path(keys_dir).resolve()
    except (AttributeError, TypeError, OSError):
        same = False
    if not same:
        raise CheckpointError("the lock handed in is roam.lock of another keys folder; nothing was done")


def _check_tsa_options(options: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    opts = dict(options or {})
    unknown = sorted(set(opts) - TSA_OPTIONS)
    if unknown:
        _fail(f"unknown TSA option(s): {', '.join(unknown)}")
    return opts


def _alarm(exc: CheckpointError, prefix: str = "") -> CheckpointAlarm:
    if isinstance(exc, CheckpointAlarm):
        return exc
    text = str(exc)
    return CheckpointAlarm(text if text.startswith("ALARM") else f"ALARM: {prefix}{text}")


class _Run:
    """Steps 0 to 2 (and, for make_checkpoint, 3 to 8) under the caller's lock. It reads logs as plain bytes and
    never opens a RoamLock or a LogWriter."""

    def __init__(self, ledger: Any, *, ks: Any, lock: Any, clock: Callable[[], datetime],
                 ledger_file: Optional[Path | str], exclude_from_backup: Optional[Callable[[Path], None]],
                 tsa_options: Optional[Mapping[str, Any]], say: Optional[Callable[[str], None]],
                 progress: Optional[Callable[[str], None]]):
        _check_lock(lock, ledger.keys_dir)
        self.ledger, self.ks, self.lock, self.clock = ledger, ks, lock, clock
        self.ledger_file = None if ledger_file is None else Path(ledger_file)
        self.exclude = exclude_from_backup
        self.tsa_options = _check_tsa_options(tsa_options)
        self.say_fn, self.progress_fn = say, progress
        self.staging = Path(ledger.staging_dir)
        self.adopted: List[Tuple[int, int]] = []
        self.abandoned: List[Tuple[int, int]] = []
        self.cleaned: List[str] = []
        self.notes: List[str] = []
        self.alerts: List[str] = []
        self.readers = b""  # the readers log as the last prefix check read it under the lock

    # ── plumbing ──
    def now(self) -> datetime:
        return _utc(self.clock()).replace(microsecond=0)

    def say(self, text: str) -> None:
        if self.say_fn is not None:
            self.say_fn(text)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.say(text)

    def alert(self, text: str) -> None:
        self.alerts.append(text)
        self.say(text)

    def progress(self, event: str) -> None:
        if self.progress_fn is not None:
            self.progress_fn(event)

    def write_record(self, record: Mapping[str, Any]) -> None:
        """Reload state.json and replace only `checkpoint`. The file step 0 trusted must still be there: one gone
        since (a restore while the TSA step or the clock wait ran) is ALARM, never a fresh state holding only the
        record, with seen, the rotation and the epoch marker lost."""
        try:
            self.st = _state.update_state(self.ledger.state_path, self.lock, exclude_from_backup=self.exclude,
                                          required=True, checkpoint=record)
        except _state.StateMissing:
            raise CheckpointAlarm("ALARM: " + MISSING_RECORD) from None
        except _state.StateError as exc:
            raise CheckpointError(f"state.json wasn't written: {exc}") from None

    def peek(self, path: Path, what: str, *, follow: bool, required: bool) -> bytes:
        """A log's bytes through its last newline, read-only (no torn-tail repair, no parse)."""
        return cover(_read_file(path, what, required=required, follow=follow) or b"")[0]

    # ── step 0: trust the state ──
    def trust(self) -> None:
        try:
            self._trust()
        except CheckpointError as exc:
            raise _alarm(exc) from None
        try:
            self.tsa_list = _tsa.load_tsa_list(self.ledger.tsa_path,
                                               allow_http=bool(self.tsa_options.get("allow_http", False)))
        except _tsa.TsaError as exc:
            raise CheckpointError(f"tsa.json: {exc}; nothing was signed") from None
        self.pins = () if self.tsa_list is None else _tsa.pins_from_tsas(self.tsa_list.entries)

    def _trust(self) -> None:
        try:
            st = _state.load_state(self.ledger.state_path, required=True)
        except _state.StateMissing:
            raise CheckpointAlarm("ALARM: " + MISSING_RECORD) from None
        except _state.StateError as exc:
            raise CheckpointAlarm(f"ALARM: state.json can't be read ({exc}); {MISSING_RECORD}") from None
        devices = self.peek(Path(self.ledger.devices_path), "the devices log", follow=False, required=True)
        try:
            log = _logs.replay_devices(devices, ledger_id=self.ledger.ledger_id, root=self.ledger.root, now=self.now())
        except LogError as exc:
            raise CheckpointAlarm(f"ALARM: the devices log doesn't replay from the pinned root: {exc}") from None
        epoch, rec = log.epoch, st.checkpoint
        if rec is not None:
            h = parse_header(sig.b64url_decode(rec["header"]))
            if (h["ledger_id"], h["epoch"], h["seq"], h["strand"], h["created_at"]) != \
                    (self.ledger.ledger_id, rec["epoch"], rec["seq"], rec["strand"], rec["created_at"]):
                raise CheckpointAlarm("ALARM: state.json's checkpoint record doesn't match its own header; "
                                      + MISSING_RECORD)
            if rec["epoch"] != epoch and not (rec["epoch"] < epoch and rec["digest"] == seq1_prev(log, epoch)):
                raise CheckpointAlarm(f"ALARM: the record is from epoch {rec['epoch']}, and the line that opened epoch "
                                      f"{epoch} doesn't name it as checkpoint_ref: {MISSING_RECORD}")
        elif st.epoch_start != epoch:
            raise CheckpointAlarm(f"ALARM: there's no checkpoint record and no marker for epoch {epoch}: "
                                  + MISSING_RECORD)
        if rec is not None and rec["epoch"] == epoch:
            # A record of this epoch whose own signer the newest devices log revokes: no chain goes on from it
            # (verify_checkpoint refuses a revoked signer at the newest log), so only recovery opens a way on.
            # Retired (rotated out) is fine. adopt_staged raises this same ALARM, so recovery doesn't adopt: it
            # names this record's digest as checkpoint_ref (never null while an old-epoch record remains, unless
            # the same state write nulls the record), and staged files above it are left for upload and reported
            # as abandoned at the epoch change. A record from the epoch before, which the opening line names as
            # checkpoint_ref, isn't checked here: that root-signed ref vouches for it, so a revoke of its signer
            # (by that line, or by a separate device_revoke inside the old epoch) is waived, here and when
            # adoption links seq 1 to it (`_named_ref`).
            was = log.state().status.get(rec["strand"])
            if was is not None and was[0] == "revoked":
                raise CheckpointAlarm(f"ALARM: {REVOKED_SIGNER} (epoch {rec['epoch']} seq {rec['seq']}, signed by "
                                      f"strand {rec['strand']}, revoked at devices line {was[1]})")
        self.st, self.devices, self.log, self.epoch = st, devices, log, epoch
        self.check_prefixes(rec)

    def check_prefixes(self, rec: Optional[Mapping[str, Any]]) -> None:
        """§14.5a's prefix check against the record: the devices and readers logs as read under the lock start
        with the bytes the record cites. A shorter log is ROLLBACK and other bytes FORK, both ALARM on the laptop.
        A log put back by a restore or a sync copy would otherwise undo a revoke, and the next checkpoint would
        seal to the revoked device and fail the laptop's own chain check. The logs run on across epochs, so a
        record from the epoch before (the one checkpoint_ref names) is checked the same way."""
        readers = self.peek(Path(self.ledger.readers_path), "the readers log", follow=False, required=False)
        self.readers = readers
        if rec is None:
            return
        where = f"epoch {rec['epoch']} seq {rec['seq']}, this laptop's record; nothing was signed"
        d = parse_header(sig.b64url_decode(rec["header"]))["devices"]
        r = rec["logs"]["readers"]
        for data, length, digest, what in ((self.devices, d["byte_length"], d["digest"], "the devices log"),
                                           (readers, r["byte_length"], r["byte_digest"], "the readers log")):
            try:
                _extends(data, length, digest, what)
            except (CheckpointRollback, CheckpointFork) as exc:
                raise type(exc)(f"{exc} ({where})") from None

    # ── step 1: clean up ──
    def clean(self) -> None:
        try:
            removed = _tsa.cleanup_key_folders(self.ledger.keys_dir)
        except _tsa.TsaError as exc:
            raise CheckpointError(f"{exc}; nothing was signed") from None
        if removed:
            self.note(f"removed {removed} leftover client-key folder(s) from an earlier TSA call")
        staging = self.staging
        staged = staged_checkpoints(staging)  # every name checked before anything is removed
        if not staged and not _staging_folder(staging, "staging/"):
            return
        odir = staging / OBJECTS_DIR
        o_names = _listdir(odir) if _staging_folder(odir, "staging/o/") else []
        for name in _listdir(staging):
            if stat.S_ISLNK(os.lstat(staging / name).st_mode):
                raise CheckpointAlarm(f"ALARM: staging/{name} is a symlink; staged files are real files, and they stay "
                                      "where they are")
        for name in o_names:
            _staged_file(odir / name, f"staging/o/{name}")
        sidecars: Dict[Tuple[int, int], Path] = {}
        for name in _listdir(staging):
            if name.startswith("custodian-") and name.endswith(SIDECAR_SUFFIX):
                m = _STAGED_NAME.fullmatch(name[:-len(SIDECAR_SUFFIX)])
                sidecars[(int(m.group(1)), int(m.group(2)))] = staging / name
        rec = self.st.checkpoint
        listed = set(listed_objects(rec["logs"])) if rec is not None else set()
        for key, path in sidecars.items():
            if key in staged:
                try:
                    listed |= set(parse_sidecar(_read_file(path, f"staging/{path.name}", required=True, follow=False)))
                except CheckpointError as exc:
                    raise CheckpointAlarm(f"ALARM: staging/{path.name} can't be read ({exc}); the files stay where "
                                          "they are") from None
        def regular(path: Path) -> bool:
            return stat.S_ISREG(os.lstat(path).st_mode)
        gone: List[Tuple[Path, str]] = []
        gone += [(staging / n, n) for n in _listdir(staging) if n.endswith(TMP_SUFFIX) and regular(staging / n)]
        gone += [(odir / n, f"{OBJECTS_DIR}/{n}") for n in o_names if n.endswith(TMP_SUFFIX)]
        gone += [(path, path.name) for key, path in sorted(sidecars.items()) if key not in staged]
        gone += [(odir / n, f"{OBJECTS_DIR}/{n}") for n in o_names
                 if _HEX64.fullmatch(n) and f"{OBJECTS_DIR}/{n}" not in listed]
        for path, rel in gone:
            os.unlink(path)
            self.cleaned.append(rel)
        if gone:
            for folder in {path.parent for path, _ in gone}:
                _fsync_dir(folder)

    # ── step 2: adopt ──
    def adopt(self) -> None:
        while True:
            rec = self.st.checkpoint
            mark = None if rec is None else (rec["epoch"], rec["seq"])
            staged = staged_checkpoints(self.staging)
            above = sorted(k for k in staged if mark is None or k > mark)
            later = [k for k in above if k[0] > self.epoch]
            if later:
                raise CheckpointAlarm(f"ALARM: staging holds epoch {later[0][0]} seq {later[0][1]}, an epoch the "
                                      "devices log hasn't reached; the files stay where they are")
            for k in above:
                if k[0] < self.epoch and mark is not None and k not in self.abandoned:
                    self.abandoned.append(k)
                    self.note(f"epoch {k[0]} seq {k[1]} was staged above the record before the epoch changed: it's "
                              "left for upload and abandoned at the epoch change")
            candidates = [k for k in above if k[0] == self.epoch]
            if not candidates:
                return
            want = (self.epoch, rec["seq"] + 1) if rec is not None and rec["epoch"] == self.epoch else (self.epoch, 1)
            if candidates[0] != want:
                raise CheckpointAlarm(f"ALARM: a gap in staging: epoch {self.epoch} seq {candidates[0][1]} is staged "
                                      f"above the record, but seq {want[1]} isn't; the files stay where they are")
            self._adopt_one(want, staged[want], rec)

    def _identities(self) -> List[Identity]:
        ids = [Identity(self.ks.dk_box)]
        if self.ks.box_prev is not None:
            ids.append(Identity(self.ks.box_prev.dk_box))
        return ids

    def _read(self, path: Path, what: str) -> bytes:
        try:
            return _read_file(path, what, required=True, follow=False)
        except CheckpointError as exc:
            raise _alarm(exc) from None

    def _adopt_one(self, key: Tuple[int, int], path: Path, rec: Optional[Mapping[str, Any]]) -> None:
        e, s = key
        where = f"epoch {e} seq {s}"
        try:
            shipped = open_custodian(self._read(path, f"staging/{path.name}"), self._identities())
        except CheckpointAlarm:
            raise
        except CheckpointError:
            raise CheckpointAlarm(f"ALARM: neither the live box nor box_prev opens the staged checkpoint at {where}; "
                                  "the files stay where they are") from None
        side = self.staging / sidecar_name(e, s)
        if not (side.is_symlink() or side.exists()):
            raise CheckpointAlarm(f"ALARM: the staged checkpoint at {where} has no sidecar; the files stay where "
                                  "they are")
        try:
            listed = parse_sidecar(self._read(side, f"staging/{side.name}"))
        except CheckpointAlarm:
            raise
        except CheckpointError as exc:
            raise CheckpointAlarm(f"ALARM: the sidecar of {where}: {exc}; the files stay where they are") from None
        in_record = set(listed_objects(rec["logs"])) if rec is not None else set()
        segments: Dict[str, bytes] = {}
        for oid in listed:
            if oid in in_record:
                continue
            seg = self.staging / oid
            if not (seg.is_symlink() or seg.exists()):
                raise CheckpointAlarm(f"ALARM: the staged checkpoint at {where} lists {oid}, which is neither in the "
                                      "record nor staged; the files stay where they are")
            segments[oid] = self._read(seg, f"staging/{oid}")
        now = self.now()
        v = verify_checkpoint(shipped, ledger_id=self.ledger.ledger_id, root=self.ledger.root, devices=self.devices,
                              now=now, pins=self.pins, slot=key, epochs=self.ks.epochs, segments=segments,
                              replay=self.log)
        if listed != listed_objects(v.body["logs"]):
            raise CheckpointAlarm(f"ALARM: the sidecar of {where} doesn't list exactly the body's segments; the files "
                                  "stay where they are")
        try:
            self._link(rec, v, segments, now)
        except CheckpointError as exc:
            raise _alarm(exc, f"the staged checkpoint at {where} doesn't link to the record: ") from None
        if v.body["tsa_policy_digest"] != (None if self.tsa_list is None else self.tsa_list.digest) and \
                TSA_CHANGED not in self.alerts:
            self.alert(TSA_CHANGED)
        new = v.record(rec)
        self.progress(f"adopt {e} {s}")
        self.write_record(new)
        self.adopted.append(key)

    def _link(self, rec: Optional[Mapping[str, Any]], v: Verified, segments: Mapping[str, bytes],
              now: datetime) -> None:
        """The candidate against the record: the verify_chain rules from the record's header, the snapshot link,
        append_only claims proved (from staged segments when needed), genTime never going back, and for a PRESENT
        candidate the custodian line rule before its genTime becomes last_present."""
        readers = self.peek(Path(self.ledger.readers_path), "the readers log", follow=False, required=False)
        if rec is None:  # the epoch's start, from its marker: nothing to link to, so nothing may claim append_only
            check_snapshot_link(None, v)
            for name in LOG_NAMES:
                if v.body["logs"][name]["append_only"]:
                    _fail(f"it claims the {name} log is append_only with no record to check that against")
            self._line_rule(None, v, readers, segments)
            return
        # A record from an earlier epoch is the one the opening line names (step 0 trusts nothing else): that
        # root-signed checkpoint_ref vouches for it, so a revoke of its signer inside the old epoch is waived.
        named = rec["epoch"] < v.epoch and rec["digest"] == seq1_prev(self.log, v.epoch)
        rv = verify_checkpoint(shipped_from_mark(rec), ledger_id=self.ledger.ledger_id, root=self.ledger.root,
                               devices=self.devices, now=now, cited=rec["digest"], slot=(rec["epoch"], rec["seq"]),
                               replay=self.log, _named_ref=named)
        verify_chain([rv, v], ledger_id=self.ledger.ledger_id, root=self.ledger.root, devices=self.devices, now=now,
                     replay=self.log)
        check_snapshot_link(rec["snapshot_digest"] if rec["epoch"] == v.epoch else None, v)
        logs = {"devices": self.devices, "readers": readers,
                "disclosures": self.peek(disclosures_path(self.ledger.ledger_dir), "the disclosures log",
                                         follow=False, required=False)}
        if self.ledger_file is not None and self.ledger_file.exists():
            logs["ledger"] = self.peek(self.ledger_file, "the ledger", follow=True, required=True)
        unchecked = check_append_only(rec["logs"], v, logs=logs, log_bytes=self._staged_log_bytes(segments))
        if unchecked:
            _fail(f"its append_only claim for the {', '.join(unchecked)} log can't be checked")
        lp = rec["last_present"]
        if v.label == PRESENT and lp is not None and rec["epoch"] == v.epoch and \
                v.gen_time < _time(lp["gen_time"], "last_present gen_time"):
            raise CheckpointAlarm(f"ALARM: genTime goes backwards in epoch {v.epoch}: seq {v.seq} is PRESENT before "
                                  "the record's last PRESENT checkpoint")
        self._line_rule(rec, v, readers, segments)

    def _line_rule(self, rec: Optional[Mapping[str, Any]], v: Verified, readers: bytes,
                   segments: Mapping[str, bytes]) -> None:
        """§14.5a devices rule 3 and §18a "Verifying a chain" on the custodian side, for a PRESENT candidate: every
        devices or readers line it covers past the record's last_present (in its epoch) is dated at most 1 hour
        after its genTime. Otherwise its token anchors nothing: ALARM, and the files stay where they are."""
        if v.label != PRESENT:
            return
        entry = v.body["logs"]["readers"]
        n = entry["byte_length"]
        covered = readers[:n] if len(readers) >= n and digest_of(readers[:n]) == entry["byte_digest"] else \
            self._staged_log_bytes(segments)(v, "readers")
        if covered is None:
            _fail("the readers bytes it covered aren't here to check its genTime against")
        lp = rec["last_present"] if rec is not None and rec["epoch"] == v.epoch else None
        latest = latest_line_at(v.cited_devices, covered, lp)
        if latest is not None and latest > v.gen_time + LINE_SLACK:
            raise CheckpointAlarm(f"ALARM: a devices or readers line epoch {v.epoch} seq {v.seq} covers is dated more "
                                  "than 1 hour after its genTime, so its token can't anchor anything")

    def _staged_log_bytes(self, segments: Mapping[str, bytes]) -> Callable[[Verified, str], Optional[bytes]]:
        """A log's bytes restored from its staged segments (None when one isn't staged any more)."""
        def restore(v: Verified, name: str) -> Optional[bytes]:
            entry = v.body["logs"][name]
            blobs: Dict[str, bytes] = {}
            for seg in entry["segments"]:
                oid = seg["object"]
                if oid in segments:
                    blobs[oid] = segments[oid]
                    continue
                path = self.staging / oid
                if not path.is_file() or path.is_symlink():
                    return None
                blobs[oid] = self._read(path, f"staging/{oid}")
            return restore_log(entry, blobs.__getitem__, self._identities(), name=name)
        return restore


def _start(ledger: Any, **kw: Any) -> _Run:
    run = _Run(ledger, **kw)
    run.trust()
    run.clean()
    run.adopt()
    return run


def adopt_staged(ledger: Any, *, ks: Any, lock: Any, clock: Callable[[], datetime],
                 ledger_file: Optional[Path | str] = None,
                 exclude_from_backup: Optional[Callable[[Path], None]] = None,
                 tsa_options: Optional[Mapping[str, Any]] = None, say: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[str], None]] = None) -> Adoption:
    """Steps 0 to 2 of making a checkpoint, alone: under the caller's roam.lock held exclusively, trust state.json
    (and that the devices and readers logs still start with the bytes its record cites: ROLLBACK, FORK), clean
    staging/ and the client-key folders, then adopt what's staged above the record until nothing is.

    Only staged files above the record are candidates: exactly record seq + 1 in the current epoch, or seq 1 of
    the current epoch when the record is from an older one (or null with the epoch's marker). A candidate opens
    with the live box, then box_prev; it must pass verify_checkpoint on the custodian side (a signer retired since
    is fine), link to the record by the verify_chain rules, the snapshot link and its append_only claims, meet the
    custodian line rule when it's PRESENT (no line it covers past last_present dated over 1 hour after its
    genTime), and its sidecar must list exactly its segments, each in the record or staged. Its body becomes the
    record, and a tsa_policy_digest other than tsa.json's raises the local alert. A gap, a name that isn't
    canonical, or a candidate neither key opens or that fails a check is ALARM, and the files stay where they
    are. A file of an older epoch above the record is never a candidate: it's left for upload and reported in
    `abandoned`. Files at or below the record are left alone.

    Recovery and root_rotate call this before they choose checkpoint_ref. When it raises step 0's REVOKED_SIGNER
    ALARM ("the last checkpoint's signer has been revoked; this epoch can only continue through recovery"),
    recovery doesn't adopt: it names state.json's record digest as checkpoint_ref, and staged files above the
    record are left for upload and reported as abandoned at the epoch change. A recovery never names null while
    an old-epoch record remains, unless the same state write nulls the record (step 0 refuses that record for
    good otherwise). Seq 1 of the new epoch, made or adopted, then links to the named record: its root-signed
    checkpoint_ref vouches for it, so the revoke of its signer is waived for that link alone.

    `ks` is the keystore loaded under `lock` (the rotation's adopt hook passes the live one). `ledger_file`, when
    given, lets an append_only claim on the ledger be checked against its newest bytes. The rotation engine's
    LogWriters may be open meanwhile: everything is read as plain bytes."""
    run = _start(ledger, ks=ks, lock=lock, clock=clock, ledger_file=ledger_file,
                 exclude_from_backup=exclude_from_backup, tsa_options=tsa_options, say=say, progress=progress)
    return Adoption(epoch=run.epoch, record=run.st.checkpoint, adopted=tuple(run.adopted),
                    abandoned=tuple(run.abandoned), cleaned=tuple(run.cleaned), notes=tuple(run.notes))


def make_checkpoint(ledger: Any, *, ledger_file: Path | str, ks: Any, lock: Any, clock: Callable[[], datetime],
                    clock_check: Optional[Callable[[datetime], None]],
                    relay_seen: Callable[[int, int], str] = relay_unknown, mirror_dir: Optional[Path | str] = None,
                    exclude_from_backup: Optional[Callable[[Path], None]] = None, policy: Optional[bytes] = None,
                    rotate_idx: Optional[int] = None, when_due: bool = False,
                    tsa_options: Optional[Mapping[str, Any]] = None, rng: Callable[[int], bytes] = os.urandom,
                    sleep: Callable[[float], None] = time.sleep, say: Optional[Callable[[str], None]] = None,
                    progress: Optional[Callable[[str], None]] = None) -> MakeResult:
    """Make at most one checkpoint (§18a "Making a checkpoint") under the caller's roam.lock, held exclusively.
    It never opens a RoamLock or a LogWriter; every log is read as plain bytes.

    0. Trust the state: state.json must exist, and its record must be in the current epoch, or be the checkpoint
       the epoch's root-signed opening line names as checkpoint_ref, or be null with `epoch_start` at the current
       epoch. Anything else is ALARM ("the checkpoint record is missing: rebuild it from the relay with the
       drill's Path B") before anything is cleaned, adopted or signed. So is a record of the current epoch whose
       own signer (its header's strand) the newest devices log revokes ("the last checkpoint's signer has been
       revoked; this epoch can only continue through recovery"; retired is fine), and a devices or readers log
       that doesn't start with the bytes the record cites (ROLLBACK, FORK), checked again after adoption.
    1. Clean up: temp files in staging/, leftover client-key folders, a sidecar whose custodian file is absent,
       and every staging/o/ object neither the record nor a remaining sidecar lists.
    2. Adopt, repeatedly, until nothing staged is above the record (`adopt_staged`).
    3. With `rotate_idx` (the rotation hook): return if the record is already the first checkpoint of the kid
       that line introduced, with a devices prefix that includes it.
    4. C1 (one active custodian) and the §14.6a signer check with no exceptions (a rotation unfinished, pending
       keys, or fresh keys owed after irp roam rotate --now refuse); within the record's epoch, a live key that
       isn't the record's strand must have reached it by a run of device_rotate lines (otherwise ALARM: a
       custodian is replaced through recovery); then
       `clock_check` (refused without one). With `when_due`, `due` is evaluated here, after adoption, and nothing
       more happens when it isn't due.
    5. Seq and prev: the record's seq + 1 and digest in the current epoch, else seq 1 and the epoch's
       checkpoint_ref.
    6. The slot check: ALARM ("the checkpoint record is behind") if staging/ or the mirror's m/<slot> holds that
       seq, or `relay_seen(epoch, seq)` says it's written ("unknown" passes only because step 0 trusts the
       record).
    7. Read the covered bytes, take created_at no earlier than the record's and the last covered devices and
       readers line (waiting up to 5 minutes for the clock, never making a time up), and build in memory with the
       TSA step (tsa.json, the keystore's tsa_creds, the record's last_present in this epoch).
    8. Write each file by temp, sync, rename and folder sync: the new segments, the sidecar, the custodian file,
       then state.json (only `checkpoint` replaced). staging/ is checked for its exclusion again first, and a
       state.json gone since step 0 is ALARM, never written afresh.

    `relay_seen` answers "written", "not_written" or "unknown" (the 2.6 default). `mirror_dir` defaults to
    ~/.irp-roam/mirror beside the keys folder. `policy` is the reviewed policy.json bytes, or None. `tsa_options`
    (allow_http, timeout, resolver, context_factory) exist for tests. `progress` is told each step 8 write
    ("write segment o/…", "rename segment o/…", the sidecar, the custodian file, "write state") and each adoption
    ("adopt <e> <seq>"). Raises CheckpointAlarm (ALARM), CheckpointError (refused); nothing is signed after a
    refusal at steps 0 to 6."""
    if clock_check is None:
        raise CheckpointError("no clock check: a checkpoint is never made without one (the relay client supplies it); "
                              "nothing was done")
    run = _start(ledger, ks=ks, lock=lock, clock=clock, ledger_file=ledger_file,
                 exclude_from_backup=exclude_from_backup, tsa_options=tsa_options, say=say, progress=progress)
    return _Maker(run, ledger_file=Path(ledger_file), clock_check=clock_check, relay_seen=relay_seen,
                  mirror_dir=Path(mirror_dir) if mirror_dir is not None else Path(ledger.home) / MIRROR_DIR,
                  policy=policy, rotate_idx=rotate_idx, when_due=bool(when_due), rng=rng, sleep=sleep).make()


class _Maker:
    """Steps 3 to 8, after _Run has trusted, cleaned and adopted."""

    def __init__(self, run: _Run, *, ledger_file: Path, clock_check: Callable[[datetime], None],
                 relay_seen: Callable[[int, int], str], mirror_dir: Path, policy: Optional[bytes],
                 rotate_idx: Optional[int], when_due: bool, rng: Callable[[int], bytes],
                 sleep: Callable[[float], None]):
        self.run, self.ledger_file, self.clock_check, self.relay_seen = run, ledger_file, clock_check, relay_seen
        self.mirror_dir, self.policy, self.rotate_idx, self.when_due = mirror_dir, policy, rotate_idx, when_due
        self.rng, self.sleep = rng, sleep
        if policy is not None:
            self.policy = _bytes(policy, "policy.json")

    def result(self, made: Optional[Made] = None, *, skipped: Optional[str] = None,
               why: Optional[str] = None) -> MakeResult:
        r = self.run
        return MakeResult(made=made, record=r.st.checkpoint, epoch=r.epoch, adopted=tuple(r.adopted),
                          abandoned=tuple(r.abandoned), cleaned=tuple(r.cleaned), alerts=tuple(r.alerts),
                          notes=tuple(r.notes), skipped=skipped, due=why)

    def make(self) -> MakeResult:
        r = self.run
        ks, log, epoch = r.ks, r.log, r.epoch
        rec = r.st.checkpoint
        r.check_prefixes(rec)  # again after adoption: the logs must still extend what the newest record cites
        # 3. The rotation hook: nothing to make when the new strand already has its first checkpoint.
        if self.rotate_idx is not None:
            done = self._hook_done(rec)
            if done is not None:
                return self.result(skipped=done)
        # 4. C1, the signer, the clock.
        check_one_custodian(log.state(), ks.dk_id)
        try:
            _rotation.check_signer(ks, log, r.st)
        except _rotation.Unfinished as exc:
            raise CheckpointError(f"no checkpoint while a rotation is unfinished or fresh keys are owed: {exc}") \
                from None
        except _rotation.RotationAlarm as exc:
            text = str(exc)
            raise CheckpointAlarm(text if text.startswith("ALARM") else f"ALARM: {text}") from None
        self._strand_arrived(rec)
        why = None
        if self.when_due:
            why = due(rec, epoch=epoch, logs=self._peek_logs(), now=r.now())
            if why is None:
                return self.result(skipped=f"not due: nothing changed since epoch {rec['epoch']} seq {rec['seq']}, "
                                           "made under 24 hours ago")
        try:
            self.clock_check(r.now())
        except Exception as exc:
            raise CheckpointError(f"the clock check failed: {_state._reason(exc)}; nothing was signed") from None
        # 5. Seq and prev.
        ref = seq1_prev(log, epoch)
        if rec is not None and rec["epoch"] == epoch:
            seq, prev = rec["seq"] + 1, rec["digest"]
        else:
            seq, prev = 1, ref
        _int(seq, "seq", 1)
        # 6. The slot check, before anything is signed.
        keys = ks.epochs.get(epoch) if isinstance(ks.epochs, Mapping) else None
        if keys is None:
            raise CheckpointError(f"the keystore has no keys for epoch {epoch}; nothing was signed")
        slot = custodian_slot(keys.kc, seq)
        self._slot_check(epoch, seq, slot)
        try:
            staging = _state.excluded_dir(r.staging, r.exclude)
        except _state.StateError as exc:
            raise CheckpointError(f"{exc}") from None
        # 7. The covered bytes, created_at, and the checkpoint in memory.
        covered = read_covered(ledger_file=self.ledger_file, devices_path=r.ledger.devices_path,
                               readers_path=r.ledger.readers_path,
                               disclosures_path=disclosures_path(r.ledger.ledger_dir), forks_dir=r.ledger.forks_dir,
                               lock=r.lock, clock=r.clock)
        if covered.devices != r.devices:
            _fail("the devices log changed under roam.lock; nothing was signed")
        if covered.readers != r.readers:
            _fail("the readers log changed under roam.lock; nothing was signed")
        if covered.torn is not None:
            r.note(f"a torn disclosures tail was moved to forks/{covered.torn.name} before it was covered")
        if covered.ledger_left:
            r.note(f"the ledger ends in a half-written line ({covered.ledger_left} bytes), left for the next run; if "
                   "this note keeps coming back, look at the ledger's last line")
        floor = latest_line_at(covered.devices, covered.readers, None)
        if rec is not None:
            t = _time(rec["created_at"], "the record's created_at")
            floor = t if floor is None or t > floor else floor
        created = self._wait_for(floor)
        made = self._build(covered, rec, epoch, seq, prev, ref, created)
        if made.slot != slot:  # pragma: no cover - the same key and seq
            _fail("the slot changed while the checkpoint was built")
        self._report(made, rec, epoch, seq)
        # 8. Write.
        self._write_all(staging, made, rec)
        return self.result(made, why=why)

    # ── step 3 ──
    def _hook_done(self, rec: Optional[Mapping[str, Any]]) -> Optional[str]:
        r = self.run
        idx = _int(self.rotate_idx, "rotate_idx")
        if idx >= len(r.log.lines):
            _fail(f"devices line {idx} doesn't exist")
        body = _logs.parse_line(r.log.lines[idx], DEVICES_KIND)[0]
        if body["event"] != "device_rotate":
            _fail(f"devices line {idx} isn't a device_rotate line; the rotation hook needs its rotate_idx")
        kid = body["device"]["kid"]
        if kid != r.ks.dk_id:
            _fail(f"the rotation at devices line {idx} isn't the live key's rotation; its strand can't sign any more")
        if rec is None or rec["strand"] != kid:
            return None
        length = parse_header(sig.b64url_decode(rec["header"]))["devices"]["byte_length"]
        if prefix_state(r.log, length)[0] < idx:
            return None
        return (f"the new strand already has its first checkpoint (epoch {rec['epoch']} seq {rec['seq']}), so the "
                "rotation hook has nothing to make")

    # ── step 4 ──
    def _strand_arrived(self, rec: Optional[Mapping[str, Any]]) -> None:
        """C1 and "Verifying a chain": within an epoch the strand changes only across a run of device_rotate lines
        after the record's devices prefix, the first from the record's strand and each later one's `old` the
        previous one's new kid, ending at the live key. A custodian replaced any other way (enrolled, then the old
        one revoked) goes through recovery, a new epoch: the next seq signed on it is a slot no verifier accepts."""
        r = self.run
        if rec is None or rec["epoch"] != r.epoch or rec["strand"] == r.ks.dk_id:
            return
        length = parse_header(sig.b64url_decode(rec["header"]))["devices"]["byte_length"]
        cur = rec["strand"]
        for line in r.log.lines[prefix_state(r.log, length)[0] + 1:]:
            body = _logs.parse_line(line, DEVICES_KIND)[0]
            if body["event"] == "device_rotate" and body["old"] == cur:
                cur = body["device"]["kid"]
                if cur == r.ks.dk_id:
                    return
        raise CheckpointAlarm(f"ALARM: the strand changed from {rec['strand']} to {r.ks.dk_id} since epoch "
                              f"{rec['epoch']} seq {rec['seq']} without a run of device_rotate lines; a custodian is "
                              "replaced within an epoch only through recovery; nothing was signed")

    def _peek_logs(self) -> Dict[str, bytes]:
        r = self.run
        return {"ledger": r.peek(self.ledger_file, "the ledger", follow=True, required=True), "devices": r.devices,
                "readers": r.peek(Path(r.ledger.readers_path), "the readers log", follow=False, required=False)}

    # ── step 6 ──
    def _slot_check(self, epoch: int, seq: int, slot: str) -> None:
        r = self.run
        where = f"epoch {epoch} seq {seq}"
        staged = r.staging / staged_name(epoch, seq)
        if staged.is_symlink() or staged.exists():
            raise CheckpointAlarm(f"ALARM: {BEHIND}: staging/ already holds {where}; nothing was signed")
        mirrored = self.mirror_dir / slot
        if mirrored.is_symlink() or mirrored.exists():
            raise CheckpointAlarm(f"ALARM: {BEHIND}: the mirror already holds the slot for {where}; nothing was signed")
        try:
            answer = self.relay_seen(epoch, seq)
        except Exception as exc:
            raise CheckpointError(f"the relay check for {where} failed ({_state._reason(exc)}); nothing was signed") \
                from None
        if answer not in RELAY_ANSWERS:
            raise CheckpointError(f"the relay check for {where} gave an answer other than written, not_written or "
                                  "unknown; nothing was signed")
        if answer == WRITTEN:
            raise CheckpointAlarm(f"ALARM: {BEHIND}: the relay says the slot for {where} is written; nothing was "
                                  "signed")

    # ── step 7 ──
    def _wait_for(self, floor: Optional[datetime]) -> datetime:
        r = self.run
        waited, told = 0.0, False
        while True:
            now = r.now()
            if floor is None or now >= floor:
                return now
            if waited >= CLOCK_WAIT.total_seconds():
                raise CheckpointError(f"the clock ({_ts(now)}) is still behind {_ts(floor)} (the record's created_at "
                                      "or the last covered line) after waiting 5 minutes; nothing was signed, since a "
                                      "time is never made up")
            if not told:
                r.say(f"waiting for the clock to reach {_ts(floor)}")
                told = True
            self.sleep(1)
            waited += 1

    def _build(self, covered: Covered, rec: Optional[Mapping[str, Any]], epoch: int, seq: int, prev: Optional[str],
               ref: Optional[str], created: datetime) -> Made:
        r = self.run
        ks = r.ks
        named = rec is not None and (rec["epoch"] == epoch or rec["digest"] == ref)
        previous = rec if named else None
        last_present = rec["last_present"] if rec is not None and rec["epoch"] == epoch else None
        stamp = None
        if r.tsa_list is not None:
            latest = latest_line_at(covered.devices, covered.readers, last_present)
            gen_floor = None if last_present is None else _time(last_present["gen_time"], "last_present gen_time")
            tsa_list, options = r.tsa_list, dict(r.tsa_options)

            def stamp(header: bytes) -> Any:
                return _tsa.stamp(header, tsa_list, creds=ks.tsa_creds, keys_dir=r.ledger.keys_dir, clock=r.clock,
                                  created_at=created, last_present_gen_time=gen_floor, latest_line_at=latest,
                                  rng=self.rng, **options)
        try:
            return build_checkpoint(
                ledger_id=r.ledger.ledger_id, covered=covered, devices=r.log, signer_seed=ks.dk_seed, strand=ks.dk_id,
                epoch_keys=ks.epochs[epoch], seq=seq, prev=prev, created_at=_ts(created),
                previous_logs=None if previous is None else previous["logs"],
                previous_recipients=None if previous is None else previous["recipients"],
                previous_snapshot_digest=rec["snapshot_digest"] if rec is not None and rec["epoch"] == epoch else None,
                full_base=rec is None or rec["strand"] != ks.dk_id or rec["epoch"] != epoch, policy=self.policy,
                tsa_policy_digest=None if r.tsa_list is None else r.tsa_list.digest, stamp=stamp, rng=self.rng)
        except _tsa.TsaError as exc:
            raise CheckpointError(f"the TSA step: {exc}; nothing was written") from None

    def _report(self, made: Made, rec: Optional[Mapping[str, Any]], epoch: int, seq: int) -> None:
        r = self.run
        for a in made.alerts:
            r.alert(str(a))
        for a in made.tsa_alerts:
            r.alert(f"ALERT: {a}")
        where = f"epoch {epoch} seq {seq}"
        if r.tsa_list is None:
            r.alert(f"ALERT: there's no tsa.json, so {where} is unwitnessed (NONE)")
        elif made.label != PRESENT:
            r.alert(f"ALERT: {where} is {made.label}: no TSA gave a token that passed every check")
        if rec is not None and rec["tsa_policy_digest"] != (None if r.tsa_list is None else r.tsa_list.digest) and \
                TSA_CHANGED not in r.alerts:
            r.alert(TSA_CHANGED)
        if made.created_at_skew:
            r.note(f"{where}: genTime and created_at are more than an hour apart (created_at_skew); genTime is "
                   "authoritative")

    # ── step 8 ──
    def _write_all(self, staging: Path, made: Made, rec: Optional[Mapping[str, Any]]) -> None:
        """Step 8. staging/ is checked again first: it may have gone while step 7 ran (the TSA step, the clock
        wait), and whichever writer creates it applies the Time Machine exclusion first or refuses. staging/o/ is
        then made inside it alone, so no parent is ever created without the exclusion."""
        r = self.run
        try:
            staging = _state.excluded_dir(staging, r.exclude)
        except _state.StateError as exc:
            raise CheckpointError(f"{exc}") from None
        odir = staging / OBJECTS_DIR
        try:
            os.mkdir(odir, 0o700)
        except FileExistsError:
            pass
        except FileNotFoundError:
            raise CheckpointError("staging/ went away while the checkpoint was written; nothing more was "
                                  "written") from None
        _staging_folder(odir, "staging/o/")
        os.chmod(odir, 0o700)
        for oid, ct in made.objects:
            self._write(staging / oid, ct, f"segment {oid}", same_ok=True)
        side = sidecar_name(made.epoch, made.seq)
        self._write(staging / side, sidecar_bytes(made.listed), f"sidecar {side}")
        name = staged_name(made.epoch, made.seq)
        self._write(staging / name, made.container, f"custodian {name}")
        r.progress("write state")
        r.write_record(made.record(rec))

    def _write(self, path: Path, data: bytes, what: str, *, same_ok: bool = False) -> None:
        """Temp file, sync, rename, folder sync. A staged file is never replaced."""
        r = self.run
        r.progress(f"write {what}")
        if path.is_symlink() or path.exists():
            if same_ok and not path.is_symlink() and path.is_file() and path.read_bytes() == data:
                return
            raise CheckpointAlarm(f"ALARM: staging already holds {path.name} with other bytes; a staged file is never "
                                  "replaced")
        tmp = path.with_name(path.name + TMP_SUFFIX)
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                _full_fsync(fh.fileno())
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        r.progress(f"rename {what}")
        os.replace(tmp, path)
        _fsync_dir(path.parent)


# ── The rotation hooks (§18a "The rotation hook") ──

class AdoptHook:
    """The rotation engine's `adopt(ks, lock)`: adopt_staged under the rotation's lock with the live keys, after
    the replay and before the tap (the engine's LogWriters are open, and nothing here opens one). `last` keeps
    the newest Adoption; a failure raises, and the engine reports it and carries on."""

    def __init__(self, ledger: Any, *, clock: Callable[[], datetime], ledger_file: Optional[Path | str] = None,
                 exclude_from_backup: Optional[Callable[[Path], None]] = None,
                 tsa_options: Optional[Mapping[str, Any]] = None, say: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[str], None]] = None):
        self.ledger, self.clock, self.ledger_file = ledger, clock, ledger_file
        self.exclude, self.tsa_options, self.say, self.progress = exclude_from_backup, tsa_options, say, progress
        self.last: Optional[Adoption] = None

    def __call__(self, ks: Any, lock: Any) -> None:
        self.last = None
        self.last = adopt_staged(self.ledger, ks=ks, lock=lock, clock=self.clock, ledger_file=self.ledger_file,
                                 exclude_from_backup=self.exclude, tsa_options=self.tsa_options, say=self.say,
                                 progress=self.progress)


class CheckpointHook:
    """The rotation engine's `checkpoint(ks, rotate_idx, lock)`: make_checkpoint with `rotate_idx`, under the
    rotation's lock, with the new live keys, after the record has closed. It adopts, returns when the new strand
    already has its first checkpoint (so running it twice for one rotate_idx gives one), and otherwise makes it:
    seq = the last seq + 1, the new DK signing, a full base of all four logs to the new recipients. It uses the
    clock check the engine was given (`clock_check` here must be that one). `last` keeps the newest MakeResult; a
    failure raises, and the engine reports it while the rotation stays closed."""

    def __init__(self, ledger: Any, *, ledger_file: Path | str, clock: Callable[[], datetime],
                 clock_check: Optional[Callable[[datetime], None]],
                 relay_seen: Callable[[int, int], str] = relay_unknown, mirror_dir: Optional[Path | str] = None,
                 exclude_from_backup: Optional[Callable[[Path], None]] = None, policy: Optional[bytes] = None,
                 tsa_options: Optional[Mapping[str, Any]] = None, rng: Callable[[int], bytes] = os.urandom,
                 sleep: Callable[[float], None] = time.sleep, say: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[str], None]] = None):
        if clock_check is None:
            raise CheckpointError("no clock check: the checkpoint hook closes over the one rotate() is given")
        self.ledger, self.clock_check = ledger, clock_check
        self.options = dict(ledger_file=ledger_file, clock=clock, clock_check=clock_check, relay_seen=relay_seen,
                            mirror_dir=mirror_dir, exclude_from_backup=exclude_from_backup, policy=policy,
                            tsa_options=tsa_options, rng=rng, sleep=sleep, say=say, progress=progress)
        self.last: Optional[MakeResult] = None

    def __call__(self, ks: Any, rotate_idx: int, lock: Any) -> None:
        self.last = None
        self.last = make_checkpoint(self.ledger, ks=ks, lock=lock, rotate_idx=rotate_idx, **self.options)


def rotation_hooks(ledger: Any, *, ledger_file: Path | str, clock: Callable[[], datetime],
                   clock_check: Optional[Callable[[datetime], None]], hooks: Optional[_rotation.Hooks] = None,
                   relay_seen: Callable[[int, int], str] = relay_unknown, mirror_dir: Optional[Path | str] = None,
                   exclude_from_backup: Optional[Callable[[Path], None]] = None, policy: Optional[bytes] = None,
                   tsa_options: Optional[Mapping[str, Any]] = None, rng: Callable[[int], bytes] = os.urandom,
                   sleep: Callable[[float], None] = time.sleep, say: Optional[Callable[[str], None]] = None,
                   progress: Optional[Callable[[str], None]] = None) -> _rotation.Hooks:
    """`hooks` (or the engine's defaults) with 2.6's `adopt` and `checkpoint` in place; every other hook is kept.
    Pass rotate() the same `clock_check` (§18a: the hook uses the clock check the engine was given)."""
    base = hooks if hooks is not None else _rotation.Hooks()
    adopt = AdoptHook(ledger, clock=clock, ledger_file=ledger_file, exclude_from_backup=exclude_from_backup,
                      tsa_options=tsa_options, say=say, progress=progress)
    checkpoint = CheckpointHook(ledger, ledger_file=ledger_file, clock=clock, clock_check=clock_check,
                                relay_seen=relay_seen, mirror_dir=mirror_dir, exclude_from_backup=exclude_from_backup,
                                policy=policy, tsa_options=tsa_options, rng=rng, sleep=sleep, say=say,
                                progress=progress)
    return dataclasses.replace(base, adopt=adopt, checkpoint=checkpoint)


# ── High-water marks (§18a "High-water marks") ──

@dataclass(frozen=True)
class SeenCheck:
    """What compare_seen accepted: the fetched head (verified), its `seen` mark (to write with write_seen), and the
    old-epoch checkpoints after an epoch's checkpoint_ref, abandoned at the epoch change (a warning, not FORK)."""
    head: Verified
    mark: Mapping[str, Any] = field(repr=False)
    abandoned: Tuple[Tuple[int, int], ...]
    fetched: Tuple[Verified, ...] = field(repr=False)


@dataclass(frozen=True)
class _Local:
    name: str  # "record" or "seen"
    epoch: int
    seq: int
    digest: str
    header: bytes = field(repr=False)
    sig: bytes = field(repr=False)
    devices: Optional[bytes] = field(repr=False)  # the devices bytes it cited, when known

    @property
    def key(self) -> Tuple[int, int]:
        return (self.epoch, self.seq)


def _locals(state: Any, devices: bytes) -> List[_Local]:
    out = []
    for name, mark in (("record", state.checkpoint), ("seen", state.seen)):
        if mark is None:
            continue
        header, sig_b = sig.b64url_decode(mark["header"]), sig.b64url_decode(mark["sig"])
        if name == "seen":
            cited: Optional[bytes] = sig.b64url_decode(mark["devices"])
        else:  # the record keeps no devices bytes: its prefix of this laptop's newest log, when it still matches
            d = parse_header(header)["devices"]
            cited = devices[:d["byte_length"]]
            if len(cited) != d["byte_length"] or digest_of(cited) != d["digest"]:
                cited = None
        out.append(_Local(name, mark["epoch"], mark["seq"], mark["digest"], header, sig_b, cited))
    return out


def compare_seen(state: Any, fetched: Sequence[Tuple[Tuple[int, int], Shipped]], *, ledger_id: str, root: str,
                 devices: bytes, now: datetime, forks_dir: Path | str, pins: Iterable[_tsa.TsaPin] = (),
                 epochs: Optional[Mapping[int, EpochKeys]] = None, rebuild: bool = False,
                 replay: Optional[DevicesLog] = None) -> SeenCheck:
    """Check what a fetch path (the drill, Path B) brought back against the local marks (§18a, §18.6). `fetched`
    is a run of ((epoch, seq) of the slot, Shipped) pairs: custodian containers opened with the keystore or RK
    (verified on the custodian side when `epochs` is given), or reader-side bytes.

    - The local mark is the higher (epoch, seq) of state.json's `checkpoint` and `seen`; at one (epoch, seq) their
      digests must match, otherwise FORK. A fetched object at a local mark's slot with other bytes is FORK.
    - A FORK writes `forks/fork-<epoch>-<seq>-<12 hex>-<12 hex>.json` (each side's devices bytes from its own
      object, or from the newest log when that starts with the bytes the side's header cites, so diverging devices
      logs still make a proof), checked with verify_fork_proof before it's written; the raised CheckpointFork
      carries it as `.proof`. A fetched side that doesn't verify is ALARM, with no proof. A FORK without a proof
      (a side's devices bytes unknown) is raised only after the fetched side passed verify_checkpoint.
    - The fetched head (the highest verified header, never a name) below the mark is ROLLBACK.
    - In Cut 1 (C1), a head above this laptop's own record is ALARM ("state.json is behind, or someone else is
      signing"), except with `rebuild` (inside the Path B rebuild step 0 points to).
    - A head above the mark must chain back to it (verify_chain, through device_rotate runs), otherwise FORK; a
      higher epoch must reach its seq 1, whose prev is the checkpoint_ref on the root-signed line that opened it,
      and the newest devices log must extend the mark's prefix (a higher epoch with no such line is ALARM). A
      local mark in an older epoch than the head is walked to it too, so the old-epoch checkpoints after
      checkpoint_ref are reported in `abandoned`.

    Everything here runs on the laptop, so every failure is ALARM (CheckpointRollback and CheckpointFork are
    kinds of it). Nothing is written but a fork proof: the caller writes `seen` with write_seen."""
    return _alarmed(True, lambda: _compare_seen(state, fetched, ledger_id=ledger_id, root=root, devices=devices,
                                                now=now, forks_dir=Path(forks_dir), pins=tuple(pins), epochs=epochs,
                                                rebuild=bool(rebuild), replay=replay))


def _compare_seen(state: Any, fetched: Any, *, ledger_id: str, root: str, devices: Any, now: datetime,
                  forks_dir: Path, pins: Tuple[Any, ...], epochs: Optional[Mapping[int, Any]], rebuild: bool,
                  replay: Optional[DevicesLog]) -> SeenCheck:
    if not isinstance(state, _state.RoamState):
        _fail("compare_seen takes the RoamState loaded from state.json")
    devices = _bytes(devices, "the devices log")
    now_n = _utc(now)
    log = _replay(devices, replay, ledger_id, root, now_n)
    marks = _locals(state, devices)
    if len(marks) == 2 and marks[0].key == marks[1].key and marks[0].digest != marks[1].digest:
        _fork(marks[0], _side(marks[1]), marks[0].key, ledger_id=ledger_id, now=now_n, pins=pins,
              forks_dir=forks_dir)
    items: List[Tuple[Tuple[int, int], Shipped, str]] = []
    for pair in fetched:
        slot, shipped = pair
        se, ss = _int(slot[0], "a fetched slot's epoch"), _int(slot[1], "a fetched slot's seq", 1)
        if not isinstance(shipped, Shipped):
            _fail("a fetched checkpoint is given as its Shipped bytes")
        h = parse_header(_bytes(shipped.header, "a fetched header"))
        if (h["epoch"], h["seq"]) != (se, ss):
            raise CheckpointAlarm(f"ALARM: slot replay: the header says epoch {h['epoch']} seq {h['seq']}, but it came "
                                  f"from the slot for epoch {se} seq {ss}")
        items.append(((se, ss), shipped, digest_of(shipped.header)))
    for mark in marks:
        for slot, shipped, d in items:
            if slot == mark.key and d != mark.digest:
                _fork(mark, _checked_side(mark, shipped, slot, ledger_id=ledger_id, root=root, devices=devices,
                                          now=now_n, pins=pins, epochs=epochs, log=log), slot,
                      ledger_id=ledger_id, now=now_n, pins=pins, forks_dir=forks_dir)
    if not items:
        _fail("nothing was fetched to compare with the marks")
    items.sort(key=lambda x: x[0])
    if any(a[0] == b[0] for a, b in zip(items, items[1:])):
        _fail("two fetched checkpoints claim one slot")
    verified = [verify_checkpoint(shipped, ledger_id=ledger_id, root=root, devices=devices, now=now_n, pins=pins,
                                  slot=slot, epochs=epochs, replay=log) for slot, shipped, _ in items]
    head = verified[-1]
    hkey = (head.epoch, head.seq)
    top = max(marks, key=lambda m: m.key) if marks else None
    if top is not None and hkey < top.key:
        raise CheckpointRollback(f"ROLLBACK: the newest checkpoint fetched is epoch {head.epoch} seq {head.seq}, below "
                                 f"this laptop's mark at epoch {top.epoch} seq {top.seq}")
    own = state.checkpoint
    if not rebuild and (own is None or hkey > (own["epoch"], own["seq"])):
        raise CheckpointAlarm(f"ALARM: {FOREIGN}: epoch {head.epoch} seq {head.seq} was fetched above this laptop's "
                              "own record, and Cut 1 has one checkpointing laptop")
    abandoned: List[Tuple[int, int]] = []
    reaches_seq1 = any(v.epoch == head.epoch and v.seq == 1 for v in verified)
    for mark in marks:
        if mark.key >= hkey:
            continue
        if mark is not top and not (mark.epoch < head.epoch and reaches_seq1):
            continue  # the walk is from the mark; a lower mark is walked only across an epoch the fetch spans
        if head.epoch > mark.epoch and opening_line(log, head.epoch) is None:
            raise CheckpointAlarm(f"ALARM: epoch {head.epoch} is fetched, but no recovery or root_rotate line opens it")
        vm = verify_checkpoint(Shipped(header=mark.header, sig=mark.sig), ledger_id=ledger_id, root=root,
                               devices=devices, now=now_n, cited=mark.digest, slot=mark.key, replay=log)
        run = [vm] + [v for v in verified if (v.epoch, v.seq) > mark.key]
        try:
            chain = verify_chain(run, ledger_id=ledger_id, root=root, devices=devices, now=now_n, replay=log)
        except CheckpointAlarm:
            raise
        except CheckpointError as exc:
            if head.epoch == mark.epoch:
                raise CheckpointFork(f"FORK: epoch {head.epoch} seq {head.seq} doesn't chain back to the {mark.name} "
                                     f"mark at seq {mark.seq}: {exc}") from None
            raise CheckpointAlarm(f"ALARM: epoch {head.epoch} seq {head.seq} doesn't chain back to the {mark.name} "
                                  f"mark at epoch {mark.epoch} seq {mark.seq} through seq 1 of its epoch: {exc}") \
                from None
        abandoned += [x for x in chain.abandoned if x not in abandoned]
    return SeenCheck(head=head, mark=seen_mark(head), abandoned=tuple(sorted(abandoned)), fetched=tuple(verified))


def write_seen(path: Path | str, lock: Any, mark: Mapping[str, Any], *,
               exclude_from_backup: Optional[Callable[[Path], None]] = None) -> _state.RoamState:
    """Write the `seen` mark compare_seen gave, under roam.lock held exclusively (only the fetch paths write it).
    It never goes down: the same mark again changes nothing, and a lower one, or another digest at the same
    (epoch, seq), is refused."""
    try:
        mark = _state.check_seen(_state._plain(mark))
        current = _state.load_state(path)
        cur = current.seen
        same = cur is not None and (cur["epoch"], cur["seq"], cur["digest"]) == \
            (mark["epoch"], mark["seq"], mark["digest"])
        if same:
            return current
        return _state.update_state(path, lock, exclude_from_backup=exclude_from_backup, seen=mark)
    except _state.StateError as exc:
        raise CheckpointError(f"seen wasn't written: {exc}") from None


# ── Fork proofs (§18a "Fork proofs") ──

@dataclass(frozen=True)
class ForkSide:
    digest: str
    strand: str
    label: str
    gen_time: Optional[datetime]
    devices_length: int


@dataclass(frozen=True)
class ForkProof:
    """A proof that passed verify_fork_proof: two checkpoints for one (epoch, seq), each with its own label."""
    epoch: int
    seq: int
    a: ForkSide
    b: ForkSide


def _side(mark: _Local) -> Dict[str, Optional[bytes]]:
    return {"checkpoint": mark.header, "sig": mark.sig, "devices": mark.devices, "tsr": None}


def _fetched_side(shipped: Shipped) -> Dict[str, Optional[bytes]]:
    """A fetched checkpoint's side of a proof: its devices bytes from its own object (the custodian container's
    or Slice's irp/devices.jsonl), cut to the length its header cites when they start with the cited bytes."""
    header = _bytes(shipped.header, "the header")
    d = parse_header(header)["devices"]
    cited = None
    if shipped.devices is not None:
        data = _bytes(shipped.devices, "irp/devices.jsonl")[:d["byte_length"]]
        if len(data) == d["byte_length"] and digest_of(data) == d["digest"]:
            cited = data
    return {"checkpoint": header, "sig": _bytes(shipped.sig, "the signature"), "devices": cited,
            "tsr": None if shipped.tsr is None else _bytes(shipped.tsr, "the token")}


def _checked_side(mark: _Local, shipped: Shipped, slot: Tuple[int, int], *, ledger_id: str, root: str,
                  devices: bytes, now: datetime, pins: Tuple[Any, ...], epochs: Optional[Mapping[int, Any]],
                  log: DevicesLog) -> Dict[str, Optional[bytes]]:
    """A fetched object at a local mark's slot, as a fork side, never taken on trust. Its devices bytes come from
    its own object, or else from the newest devices log when that starts with the bytes its header cites. When
    either side's devices bytes are still unknown (no proof can be built and checked), the fetched object must
    pass verify_checkpoint first: one that doesn't is ALARM with no proof, so whoever builds an object can't pick
    the FORK verdict (a key that equivocated) by shipping devices bytes that don't fit."""
    side = _fetched_side(shipped)
    if side["devices"] is None:
        cd = parse_header(side["checkpoint"])["devices"]
        prefix = devices[:cd["byte_length"]]
        if len(prefix) == cd["byte_length"] and digest_of(prefix) == cd["digest"]:
            side["devices"] = prefix
    if side["devices"] is None or mark.devices is None:
        try:
            verify_checkpoint(shipped, ledger_id=ledger_id, root=root, devices=devices, now=now, pins=pins,
                              slot=slot, epochs=epochs, replay=log)
        except CheckpointError as err:
            text = str(err)
            raise CheckpointAlarm(f"ALARM: a checkpoint for epoch {slot[0]} seq {slot[1]} isn't the {mark.name} "
                                  f"mark's and doesn't verify ({text[7:] if text.startswith('ALARM: ') else text}); "
                                  "no fork proof was written") from None
    return side


def _fork(mark: _Local, other: Mapping[str, Optional[bytes]], slot: Tuple[int, int], *, ledger_id: str,
          now: datetime, pins: Tuple[Any, ...], forks_dir: Path) -> None:
    """Raise FORK for two checkpoints at one slot, with a persisted proof when both sides verify on their own.
    Without one side's devices bytes there's no proof; a fetched side gets here only after it verified
    (`_checked_side`), so a FORK without a proof is never raised for an object that doesn't verify."""
    where = f"epoch {slot[0]} seq {slot[1]}"
    mine = _side(mark)
    if mine["devices"] is None or other["devices"] is None:
        exc = CheckpointFork(f"FORK: two checkpoints for {where} (no proof: one side's devices bytes aren't known)")
        exc.proof = None  # type: ignore[attr-defined]
        raise exc
    proof = build_fork_proof(mine, other)
    try:
        verify_fork_proof(proof, ledger_id, parse_header(mark.header)["root"], pins=pins, now=now)
    except CheckpointError as err:
        raise CheckpointAlarm(f"ALARM: a checkpoint for {where} isn't the {mark.name} mark's and doesn't verify "
                              f"({err}); no fork proof was written") from None
    path = write_fork_proof(forks_dir, proof)
    exc = CheckpointFork(f"FORK: two checkpoints for {where}; the proof is forks/{path.name}")
    exc.proof = path  # type: ignore[attr-defined]
    raise exc


def fork_proof_name(epoch: int, seq: int, digest_a: str, digest_b: str) -> str:
    """`fork-<epoch>-<seq>-<first 12 hex of a's digest>-<first 12 hex of b's>.json`."""
    a = _match(_DIGEST, digest_a, "a digest")[7:19]
    b = _match(_DIGEST, digest_b, "a digest")[7:19]
    return f"fork-{_int(epoch, 'epoch')}-{_int(seq, 'seq', 1)}-{a}-{b}.json"


def build_fork_proof(a: Mapping[str, Optional[bytes]], b: Mapping[str, Optional[bytes]]) -> bytes:
    """The exact proof bytes: JCS `{v:1, kind:"fork-proof", a:{checkpoint, sig, devices, tsr?}, b:{…}}`, each value
    the strict b64url of exact bytes, the sides in ascending order of their header digests."""
    sides = []
    for side in (a, b):
        out = {}
        for k in ("checkpoint", "sig", "devices"):
            out[k] = sig.b64url_encode(_bytes(side[k], f"a fork proof's {k}"))
        if side.get("tsr") is not None:
            out["tsr"] = sig.b64url_encode(_bytes(side["tsr"], "a fork proof's tsr"))
        sides.append((digest_of(_bytes(side["checkpoint"], "a header")), out))
    sides.sort(key=lambda x: x[0])
    return _canonical({"v": 1, "kind": FORK_PROOF_KIND, "a": sides[0][1], "b": sides[1][1]}, "a fork proof")


def write_fork_proof(forks_dir: Path | str, proof: bytes) -> Path:
    """Write a proof into forks/ (0600, temp file, sync, rename, folder sync) under its fork_proof_name. An
    existing proof of the same name is kept, never replaced."""
    proof = _bytes(proof, "a fork proof")
    obj = sig.load_jcs(proof, "the fork proof", error=CheckpointError)
    obj = _keys_exact(obj, FORK_PROOF_KEYS, "a fork proof")
    ha = parse_header(sig.b64url_decode(obj["a"]["checkpoint"]))
    name = fork_proof_name(ha["epoch"], ha["seq"], digest_of(sig.b64url_decode(obj["a"]["checkpoint"])),
                           digest_of(sig.b64url_decode(obj["b"]["checkpoint"])))
    try:
        folder = _private_dir(Path(forks_dir))
    except KeystoreError as exc:
        _fail(f"forks/: {exc}")
    path = folder / name
    if path.is_symlink() or path.exists():
        return path
    tmp = path.with_name(name + TMP_SUFFIX)
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(proof)
        fh.flush()
        _full_fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(folder)
    return path


def verify_fork_proof(proof: Any, ledger_id: str, root_pin: str, *, pins: Iterable[_tsa.TsaPin] = (),
                      now: Optional[datetime] = None) -> ForkProof:
    """Check a fork proof from the pinned root alone: strict JCS and the closed schema; each side's devices bytes
    are exactly what its header cites and replay from `root_pin`; both headers carry `ledger_id` and that root and
    the same epoch and seq with different bytes; each signer is a custodian active at its own prefix (so not
    revoked in it), and each signature verifies. Returns each side's label (under `pins`) and genTime (PRESENT
    only). A proof that fails any check is rejected with CheckpointError, never ALARM."""
    try:
        return _verify_fork_proof(proof, ledger_id, root_pin, tuple(pins), now)
    except CheckpointError as exc:
        text = str(exc)
        raise CheckpointError("the fork proof is rejected: " + (text[7:] if text.startswith("ALARM: ") else text)) \
            from None
    except _HOSTILE as exc:
        raise CheckpointError(f"the fork proof is rejected: it's malformed ({type(exc).__name__})") from None


def _verify_fork_proof(proof: Any, ledger_id: str, root_pin: str, pins: Tuple[Any, ...],
                       now: Optional[datetime]) -> ForkProof:
    _match(_LEDGER_ID, ledger_id, "the pinned ledger_id")
    _match(_ROOT, root_pin, "the pinned root")
    if isinstance(proof, (bytes, bytearray)):
        obj = sig.load_jcs(bytes(proof), "the fork proof", error=CheckpointError)
    elif isinstance(proof, Mapping):
        obj = dict(proof)
    else:
        _fail("a fork proof is its exact bytes")
    obj = _keys_exact(obj, FORK_PROOF_KEYS, "a fork proof")
    if type(obj["v"]) is not int or obj["v"] != 1:
        _fail("a fork proof's v must be 1")
    if obj["kind"] != FORK_PROOF_KIND:
        _fail("a fork proof's kind must be fork-proof")
    now_n = _utc(now) if now is not None else _utc(_wall_clock())
    a, ha = _fork_proof_side(obj["a"], "a", ledger_id, root_pin, pins, now_n)
    b, hb = _fork_proof_side(obj["b"], "b", ledger_id, root_pin, pins, now_n)
    if (ha["epoch"], ha["seq"]) != (hb["epoch"], hb["seq"]):
        _fail("the two sides aren't for the same epoch and seq")
    if a.digest == b.digest:
        _fail("the two sides are the same checkpoint")
    return ForkProof(epoch=ha["epoch"], seq=ha["seq"], a=a, b=b)


def _fork_proof_side(raw: Any, name: str, ledger_id: str, root_pin: str, pins: Tuple[Any, ...],
                     now: datetime) -> Tuple[ForkSide, dict]:
    if not isinstance(raw, dict) or not (FORK_SIDE_KEYS <= set(raw) <= FORK_SIDE_KEYS | {"tsr"}):
        _fail(f"side {name} must have exactly checkpoint, sig and devices, and tsr when witnessed")

    def b64(key: str) -> bytes:
        try:
            data = sig.b64url_decode(raw[key])
        except sig.SigError as exc:
            _fail(f"side {name}'s {key}: {exc}")
        if not data:
            _fail(f"side {name}'s {key} is empty")
        return data
    header, sig_b, devices = b64("checkpoint"), b64("sig"), b64("devices")
    tsr = b64("tsr") if "tsr" in raw else None
    h = parse_header(header)
    if h["ledger_id"] != ledger_id or h["root"] != root_pin:
        _fail(f"side {name}'s header doesn't carry the pinned ledger_id and root")
    if (len(devices), digest_of(devices)) != (h["devices"]["byte_length"], h["devices"]["digest"]):
        _fail(f"side {name}'s devices bytes aren't the ones its header cites")
    try:
        log = _logs.replay_devices(devices, ledger_id=ledger_id, root=root_pin, now=now)
    except LogError as exc:
        _fail(f"side {name}'s devices bytes don't replay from the pinned root: {exc}")
    tail = log.state()
    if tail.epoch != h["epoch"]:
        _fail(f"side {name}'s epoch isn't the epoch at its own devices prefix")
    signer = tail.devices.get(h["strand"])
    if signer is None or signer.cls != "custodian":
        _fail(f"side {name}'s signer isn't a custodian active at its own devices prefix")
    try:
        s = sig.parse_sig(sig_b)
        if s["key_id"] != h["strand"]:
            _fail(f"side {name}'s signature doesn't name its strand")
        sig.verify(SIG_KIND, header, s, signer.pub)
    except sig.SigError as exc:
        _fail(f"side {name}'s signature: {exc}")
    label, gen = NONE, None
    if tsr is not None:
        chk = _tsa.check_token(tsr, header, pins, now=now, created_at=_time(h["created_at"], "created_at"))
        label = chk.label
        gen = chk.gen_time if label == PRESENT else None
    return ForkSide(digest=digest_of(header), strand=h["strand"], label=label, gen_time=gen,
                    devices_length=len(devices)), h
