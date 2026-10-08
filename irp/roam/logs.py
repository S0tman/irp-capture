"""The device and reader logs of Roaming IRP (spec v0.3 §14.3, §14.5, §14.5a).

`devices.jsonl` records who holds which key: the root, custodian laptops, companion phones and approver
hardware keys. `readers.jsonl` records which reader runtimes may be served, with what scope and until
when. Both are append-only runs of lines, each the exact JCS bytes of {"body": …, "sigs": […]} plus one
newline, hash-chained through `prev`. Every verifier (the laptop, the phone, a reader) replays them from
the pinned root, so a relay or a copied keystore can't add a device, revive a reader or roll the root
without the keys §14.3 asks for. Replay lives here rather than in keys.py, so a reader can check the
logs without any keystore code.

What replay checks, line by line (§14.5a):
- the line: at most 64 KiB, exact JCS, a closed body per event, `idx` and `prev` chained, `ledger_id`
  and `root` pinned, `at` never backwards and never more than 5 minutes ahead of the verifier's clock,
  arrays sorted without duplicates and strings in them printable ASCII;
- the signers: exactly one of the event's allowed sets, judged on the state before the line, each
  signature over SI(kind, JCS(body)); a key the event adds countersigns its own line;
- the event's own rules: descriptors closed and bound to their keys, never-reused keys, boxes and
  recipients, unique labels, nonces, retirements, revocations, the recovery set and the epoch.

The output is the state after every line: the root, the epoch, the active descriptors and each key's
status (active, retired or revoked, with the idx), and for readers their scopes, recipient, expiry and
reviewed records. The prefix check compares two copies (ROLLBACK, FORK, root fork), and LogWriter
appends under an exclusive lock with a full sync, moving a torn final line to forks/.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import approver, sig
from .age import AgeError, Recipient
from .keys import _fsync_dir, _full_fsync, _private_dir
from .rekadu import region_for_surface
from .statements import IDENTITY_ASSURANCE, RESERVED_SURFACES, SLOT_SURFACE, VIEWING

DEVICES_KIND = "devices-entry"
READERS_KIND = "readers-entry"
MAX_LINE = 65536  # bytes, newline included
FUTURE_SLACK = timedelta(minutes=5)
READER_MAX = timedelta(days=90)
MAX_INT = 2**53 - 1

COMMON_KEYS = frozenset({"v", "kind", "event", "ledger_id", "root", "idx", "prev", "at"})
DEVICE_EVENTS: Mapping[str, frozenset] = MappingProxyType({
    "genesis": frozenset({"root_pub", "epoch"}),
    "device_enrol": frozenset({"device", "nonce"}),
    "device_rotate": frozenset({"old", "device"}),
    "device_rekey": frozenset({"kid", "box", "nonce"}),
    "device_revoke": frozenset({"kid"}),
    "approver_enrol": frozenset({"approver"}),
    "approver_revoke": frozenset({"kid"}),
    "recovery": frozenset({"active", "revokes", "new_device", "epoch", "checkpoint_ref"}),
    "root_rotate": frozenset({"root_pub", "epoch", "checkpoint_ref"}),
})
READER_EVENTS: Mapping[str, frozenset] = MappingProxyType({
    "reader_enrol": frozenset({"reader", "dry_run_digest", "reviewed"}),
    "reader_scope": frozenset({"reader_id", "scopes", "dry_run_digest", "reviewed"}),
    "reader_renew": frozenset({"reader_id", "recipient", "expires"}),
    "reader_review": frozenset({"reader_id", "scope_id", "rule_digest", "reviewed_ids", "dry_run_digest"}),
    "reader_revoke": frozenset({"reader_id"}),
})
DESCRIPTOR_KEYS = frozenset({"kid", "class", "alg", "pub", "box", "label", "key_scope", "webauthn"})
WEBAUTHN_KEYS = frozenset({"rp_id", "origin", "be", "bs", "cred_id"})
READER_KEYS = frozenset({"reader_id", "surface", "region", "viewing", "identity_assurance", "recipient", "scopes",
                         "expires"})
SCOPE_KEYS = frozenset({"scope_id", "rule", "rule_digest"})
RULE_KEYS = frozenset({"ids", "tags_any", "types", "since", "limit", "ancestor_depth", "pinned", "token_budget",
                       "byte_budget"})
RULE_TYPES = frozenset({"decision", "contribution", "correction"})
CLASS_ALG = MappingProxyType({"custodian": "ed25519", "companion": "webauthn-es256", "approver": "fido2-es256"})
CLASS_PREFIX = MappingProxyType({"custodian": "dk", "companion": "dk", "approver": "ak"})

_LEDGER_ID = re.compile(r"ILID-[0-9a-f]{32}")
_ROOT = re.compile(r"rt-[0-9a-f]{32}")
_DK = re.compile(r"dk-[0-9a-f]{32}")
_AK = re.compile(r"ak-[0-9a-f]{32}")
_READER = re.compile(r"rd-[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_LABEL = re.compile(r"[a-z]+-[0-9]{1,3}")
_SCOPE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class LogError(ValueError):
    """A log line or a whole log breaks a §14.5a rule. Publishing stops; a reader exits 10."""


class TornLine(LogError):
    """The log ends in a fragment without its newline (a write cut short)."""


class LogRollback(LogError):
    """A copy bound to a later object is shorter than one bound to an earlier object."""


class LogFork(LogError):
    """Two copies differ at the same idx. A root fork means the paper key is in two hands."""

    def __init__(self, message: str, idx: int | None = None, root_fork: bool = False):
        super().__init__(message)
        self.idx = idx
        self.root_fork = root_fork


# ── Small checks ──

def _fail(message: str) -> None:
    raise LogError(message)


def _match(rx: re.Pattern, val: Any, what: str) -> str:
    if not (isinstance(val, str) and rx.fullmatch(val)):
        _fail(f"{what} has the wrong format: {val!r}")
    return val


def _int(val: Any, what: str, lo: int = 0, hi: int = MAX_INT) -> int:
    if type(val) is not int or not lo <= val <= hi:
        _fail(f"{what} must be an integer from {lo} to {hi}, got {val!r}")
    return val


def _time(val: Any, what: str) -> datetime:
    if not (isinstance(val, str) and _TIMESTAMP.fullmatch(val)):
        _fail(f"{what} must be a UTC timestamp like 2026-10-08T09:00:00Z, got {val!r}")
    try:
        return datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail(f"{what} isn't a real UTC time: {val!r}")
    raise AssertionError  # pragma: no cover


def _utc(now: datetime) -> datetime:
    if not isinstance(now, datetime):
        _fail("the verifier's clock must be a datetime")
    return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now


def _keys(obj: Any, keys: Iterable[str], what: str) -> dict:
    keys = frozenset(keys)
    if not isinstance(obj, dict) or set(obj) != keys:
        _fail(f"{what} must have exactly the keys {', '.join(sorted(keys))}")
    return obj


def _b64(val: Any, what: str, length: int | None = None) -> bytes:
    try:
        return sig.b64url_decode(val, length)
    except sig.SigError as exc:
        _fail(f"{what}: {exc}")
    raise AssertionError  # pragma: no cover


def _digest_or_null(val: Any, what: str) -> None:
    if val is not None:
        _match(_DIGEST, val, what)


def _recipient(val: Any, what: str) -> bytes:
    try:
        return Recipient.from_string(val).public
    except AgeError as exc:
        _fail(f"{what} isn't an age1 recipient: {exc}")
    raise AssertionError  # pragma: no cover


def line_hash(line: bytes) -> str:
    """The `prev` of the next line: sha256 over a line's bytes without its newline."""
    return "sha256-" + hashlib.sha256(line).hexdigest()


def split_log(data: bytes) -> list[bytes]:
    """A log is empty or a run of complete lines, each ending in `\\n`. A final fragment is never skipped."""
    if not isinstance(data, (bytes, bytearray)):
        _fail("a log is bytes")
    data = bytes(data)
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise TornLine("the log ends in a line without its newline (a torn write)")
    lines = data[:-1].split(b"\n")
    for i, line in enumerate(lines):
        if not line:
            _fail(f"line {i} is blank")
        if b"\r" in line:
            _fail(f"line {i} has a carriage return (LF only)")
        if len(line) + 1 > MAX_LINE:
            _fail(f"line {i} is over 64 KiB")
    return lines


def _check_arrays(obj: Any, where: str) -> None:
    """Every array is sorted ascending with no duplicates: strings by value (printable ASCII only), objects by
    `kid` or `scope_id`. In ASCII, byte order, code point order and JS's default sort all agree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_arrays(v, f"{where}.{k}")
        return
    if not isinstance(obj, list):
        return
    if all(isinstance(x, str) for x in obj):
        for x in obj:
            if not _PRINTABLE.fullmatch(x):
                _fail(f"{where} holds a string that isn't printable ASCII without spaces")
        order = list(obj)
    elif all(isinstance(x, dict) for x in obj):
        key = "kid" if all("kid" in x for x in obj) else "scope_id" if all("scope_id" in x for x in obj) else None
        if key is None or not all(isinstance(x[key], str) for x in obj):
            _fail(f"{where} holds objects without a kid or scope_id to sort by")
        order = [x[key] for x in obj]
        for x in obj:
            _check_arrays(x, where)
    else:
        _fail(f"{where} must hold only strings or only objects")
    if any(a >= b for a, b in zip(order, order[1:])):
        _fail(f"{where} must be sorted ascending without duplicates")


def parse_line(line: bytes, kind: str) -> tuple[dict, list]:
    """The format checks of one line: size, exact JCS, the closed body of its event and the sig list."""
    if not isinstance(line, (bytes, bytearray)):
        _fail("a log line is bytes")
    if len(line) + 1 > MAX_LINE:
        _fail("a log line is over 64 KiB")
    obj = sig.load_jcs(bytes(line), "log line", error=LogError)
    if not isinstance(obj, dict) or set(obj) != {"body", "sigs"}:
        _fail("a log line is exactly {body, sigs}")
    body, sigs = obj["body"], obj["sigs"]
    if not isinstance(body, dict):
        _fail("the line body must be an object")
    if body.get("kind") != kind:
        _fail(f"kind must be {kind} in this log, got {body.get('kind')!r}")
    events = DEVICE_EVENTS if kind == DEVICES_KIND else READER_EVENTS
    event = body.get("event")
    if not isinstance(event, str) or event not in events:
        _fail(f"unknown event {event!r} in {kind}")
    expected = COMMON_KEYS | events[event] | ({"devices_at"} if kind == READERS_KIND else set())
    if set(body) != expected:
        _fail(f"the {event} body must have exactly the keys {', '.join(sorted(expected))}")
    if type(body["v"]) is not int or body["v"] != 1:
        _fail("v must be 1")
    _match(_LEDGER_ID, body["ledger_id"], "ledger_id")
    _match(_ROOT, body["root"], "root")
    _int(body["idx"], "idx")
    _digest_or_null(body["prev"], "prev")
    _time(body["at"], "at")
    if kind == READERS_KIND:
        da = _keys(body["devices_at"], ("idx", "line"), "devices_at")
        _int(da["idx"], "devices_at idx")
        _match(_DIGEST, da["line"], "devices_at line")
    _check_arrays(body, "body")

    if not isinstance(sigs, list) or not sigs:
        _fail("sigs must be a non-empty list")
    checked = []
    for s in sigs:
        try:
            s = sig._check_sig_obj(s)
        except sig.SigError as exc:
            _fail(f"a signature in sigs: {exc}")
        kid, alg = s["key_id"], s["alg"]
        if kid.startswith("ck-"):
            _fail("ck- keys never sign log lines (the relay config is CK's only authority)")
        allowed = {"rt": ("ed25519",), "dk": ("ed25519", "webauthn-es256"), "ak": ("fido2-es256",)}[kid[:2]]
        if alg not in allowed:
            _fail(f"{kid}: alg {alg} isn't allowed for a {kid[:2]}- key")
        checked.append(s)
    ids = [s["key_id"] for s in checked]
    if any(a >= b for a, b in zip(ids, ids[1:])):
        _fail("sigs must be sorted by key_id, each key at most once")
    return body, checked


# ── Descriptors ──

@dataclass(frozen=True)
class Device:
    kid: str
    cls: str
    alg: str
    pub: bytes = field(repr=False)
    box: bytes | None = field(repr=False)
    label: str
    be: bool
    bs: bool
    raw: Mapping[str, Any] = field(repr=False, compare=False)


def _dns(name: Any, what: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 253 or "." not in name:
        _fail(f"{what} must be a lowercase DNS name")
    for part in name.split("."):
        if not _DNS_LABEL.fullmatch(part):
            _fail(f"{what} must be a lowercase DNS name")
    return name


def check_descriptor(d: Any) -> Device:
    """A §14.5a descriptor: closed, its kid bound to its key, its box, label, scope and webauthn values set
    by its class."""
    d = _keys(d, DESCRIPTOR_KEYS, "a descriptor")
    cls = d["class"]
    if not isinstance(cls, str) or cls not in CLASS_ALG:
        _fail(f"descriptor class must be custodian, companion or approver, got {cls!r}")
    alg = CLASS_ALG[cls]
    if d["alg"] != alg:
        _fail(f"a {cls} signs with {alg}, not {d['alg']!r}")
    pub = _b64(d["pub"], "descriptor pub")
    try:
        if cls == "custodian":
            sig.check_ed25519_public(pub)
        else:
            approver.check_spki(pub)
        kid = sig.key_id(CLASS_PREFIX[cls], pub, alg)
    except sig.SigError as exc:
        _fail(f"descriptor pub: {exc}")
    if d["kid"] != kid:
        _fail("the descriptor kid isn't the key id of its pub")
    box = None if cls == "approver" else _recipient(d["box"], "descriptor box")
    if cls == "approver" and d["box"] is not None:
        _fail("an approver has no box: it approves and never decrypts")
    _match(_LABEL, d["label"], "label")
    wa = d["webauthn"]
    be = bs = False
    if cls == "custodian":
        if wa is not None:
            _fail("a custodian descriptor has webauthn null")
    else:
        wa = _keys(wa, WEBAUTHN_KEYS, "descriptor webauthn")
        be, bs = wa["be"], wa["bs"]
        if type(be) is not bool or type(bs) is not bool:
            _fail("be and bs are JSON booleans")
        if bs and not be:
            _fail("bs true needs be true")
        cred = _b64(wa["cred_id"], "cred_id")
        if not 16 <= len(cred) <= 1023:
            _fail("cred_id must be 16 to 1023 bytes")
        if cls == "approver":
            if wa["rp_id"] != approver.APPROVER_RP_ID or wa["origin"] != approver.APPROVER_ORIGIN or be:
                _fail(f"an approver uses rp_id {approver.APPROVER_RP_ID}, origin {approver.APPROVER_ORIGIN} "
                      "and be false")
        else:
            rp_id = _dns(wa["rp_id"], "rp_id")
            if rp_id == "invalid" or rp_id.endswith(".invalid"):
                _fail("a companion rp_id can't sit under .invalid")
            origin = wa["origin"]
            host = origin[len("https://"):] if isinstance(origin, str) and origin.startswith("https://") else None
            if host is None or (_dns(host, "origin host") != rp_id and not host.endswith("." + rp_id)):
                _fail("a companion origin is https:// plus its rp_id or a host under it, with no port or path")
    key_scope = "account-synced" if be else "device-local"
    if d["key_scope"] != key_scope:
        _fail(f"key_scope must be {key_scope} for this {cls}")
    return Device(kid=kid, cls=cls, alg=alg, pub=pub, box=box, label=d["label"], be=be, bs=bs, raw=d)


# ── Scope rules (§16.2, §16.3 #6) ──

def rule_digest(rule: Mapping[str, Any]) -> str:
    return "sha256-" + hashlib.sha256(sig._canonical(rule)).hexdigest()


def check_rule(rule: Any, *, public_safe_tag: str | None = None) -> None:
    """The nine-key §16.2 rule under the public-safe rule every v1 (non-EU) reader gets: exactly one of
    ids and tags_any, at most one tag (the policy's public_safe_tag when the writer knows it), pinned within
    ids, and the rest only narrowing. Replay passes no tag: a reader doesn't hold policy.json."""
    rule = _keys(rule, RULE_KEYS, "a scope rule")
    for name, rx in (("ids", _RECORD_ID), ("tags_any", _TAG), ("pinned", _RECORD_ID)):
        vals = rule[name]
        if not isinstance(vals, list):
            _fail(f"rule {name} must be a list")
        for v in vals:
            _match(rx, v, f"rule {name} entry")
        if any(a >= b for a, b in zip(vals, vals[1:])):
            _fail(f"rule {name} must be sorted without duplicates")
    types = rule["types"]
    if not isinstance(types, list) or any(not isinstance(t, str) or t not in RULE_TYPES for t in types) or \
            any(a >= b for a, b in zip(types, types[1:])):
        _fail(f"rule types must be a sorted list from {', '.join(sorted(RULE_TYPES))}")
    if rule["since"] is not None:
        _time(rule["since"], "rule since")
    if rule["limit"] is not None:
        _int(rule["limit"], "rule limit", 1)
    _int(rule["ancestor_depth"], "rule ancestor_depth", 1, 4)
    _int(rule["token_budget"], "rule token_budget", 1)
    _int(rule["byte_budget"], "rule byte_budget", 1)
    ids, tags = rule["ids"], rule["tags_any"]
    if bool(ids) == bool(tags):
        _fail("a public-safe rule uses exactly one of ids and tags_any")
    if len(tags) > 1:
        _fail("a public-safe rule has at most one tag")
    if tags and public_safe_tag is not None and tags != [public_safe_tag]:
        _fail("tags_any must be exactly [public_safe_tag] from policy.json")
    if not set(rule["pinned"]) <= set(ids):
        _fail("pinned must be within ids")


def _check_scopes(scopes: Any, what: str) -> dict[str, dict]:
    if not isinstance(scopes, list) or not scopes:
        _fail(f"{what} must be a non-empty list of scopes")
    out: dict[str, dict] = {}
    for s in scopes:
        s = _keys(s, SCOPE_KEYS, "a scope")
        sid = _match(_SCOPE_ID, s["scope_id"], "scope_id")
        check_rule(s["rule"])
        if s["rule_digest"] != rule_digest(s["rule"]):
            _fail(f"scope {sid}: rule_digest isn't sha256 of its JCS rule")
        if sid in out:
            _fail(f"scope_id {sid} appears twice")
        out[sid] = s
    return out


def _check_reviewed(reviewed: Any, scope_ids: Iterable[str]) -> dict[str, frozenset]:
    scope_ids = set(scope_ids)
    if not isinstance(reviewed, dict) or set(reviewed) != scope_ids:
        _fail("reviewed must map exactly the line's scope ids to their reviewed records")
    out = {}
    for sid, ids in reviewed.items():
        if not isinstance(ids, list):
            _fail("reviewed values are lists of record ids")
        for i in ids:
            _match(_RECORD_ID, i, "a reviewed record id")
        out[sid] = frozenset(ids)
    return out


# ── Signer sets ──

class _Role:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return self.name


CUSTODIAN = _Role("an active custodian")
APPROVER = _Role("an active approver")
Pattern = Sequence[Any]  # fixed key ids (str) and the CUSTODIAN / APPROVER roles


def _match_signers(ids: Sequence[str], patterns: Sequence[Pattern], devices: Mapping[str, Device], event: str) -> None:
    """The line's signer ids must equal exactly one allowed set: no extra signer, no mixing of two sets."""
    have = set(ids)
    for pat in patterns:
        fixed = [p for p in pat if isinstance(p, str)]
        if len(set(fixed)) != len(fixed) or not set(fixed) <= have:
            continue
        rest = have - set(fixed)
        need_c, need_a = sum(p is CUSTODIAN for p in pat), sum(p is APPROVER for p in pat)
        custodians = [i for i in rest if i in devices and devices[i].cls == "custodian"]
        approvers = [i for i in rest if i in devices and devices[i].cls == "approver"]
        if len(rest) == need_c + need_a and len(custodians) == need_c and len(approvers) == need_a:
            return
    allowed = " or ".join("{" + ", ".join(str(p) for p in pat) + "}" for pat in patterns)
    _fail(f"signers {sorted(have)} don't match an allowed set for {event}: {allowed}")


def _verify_sigs(kind: str, body: dict, sigs: Sequence[dict], keys: Mapping[str, Any]) -> dict[str, approver.Assertion]:
    """Each signature verifies over SI(kind, JCS(body)) with the key it names. `keys` maps an id to a Device
    or, for a root, to its 32-byte Ed25519 public key. Returns the ES256 assertions by key id."""
    data = sig._canonical(body)
    assertions = {}
    for s in sigs:
        kid = s["key_id"]
        key = keys.get(kid)
        if key is None:
            _fail(f"no key for signer {kid} at this line")
        try:
            if isinstance(key, bytes):
                if s["alg"] != "ed25519":
                    _fail(f"{kid}: a root signs with ed25519")
                sig.verify(kind, data, s, key)
            elif s["alg"] != key.alg:
                _fail(f"{kid}: alg {s['alg']} isn't the key's ({key.alg})")
            elif key.alg == "ed25519":
                sig.verify(kind, data, s, key.pub)
            else:
                assertions[kid] = approver.verify(kind, data, s, key.raw)
        except sig.SigError as exc:
            _fail(f"bad signature from {kid}: {exc}")
    return assertions


# ── devices.jsonl ──

@dataclass(frozen=True)
class DeviceState:
    """The state after one line of devices.jsonl."""
    idx: int
    at: str
    root: str
    root_pub: bytes = field(repr=False)
    epoch: int
    devices: Mapping[str, Device] = field(repr=False)  # the active set
    status: Mapping[str, tuple] = field(repr=False)    # kid -> ("active" | "retired" | "revoked", idx)

    @property
    def active(self) -> dict[str, dict]:
        return {kid: dict(d.raw) for kid, d in self.devices.items()}

    def is_active(self, kid: str) -> bool:
        return kid in self.devices


@dataclass(frozen=True)
class DevicesLog:
    lines: tuple
    hashes: tuple
    states: tuple
    boxes: frozenset = field(repr=False)  # every box key ever enrolled, for reader recipient uniqueness

    def state(self, idx: int = -1) -> DeviceState:
        return self.states[idx]

    @property
    def root(self) -> str:
        return self.states[-1].root

    @property
    def epoch(self) -> int:
        return self.states[-1].epoch


@dataclass
class _Plan:
    patterns: list
    new_keys: dict = field(default_factory=dict)  # id -> Device or root pub, for keys the line adds
    apply: Callable[[], None] = lambda: None
    enrolled: Device | None = None  # an ES256 key whose own flags must match its descriptor


class _DevicesReplay:
    def __init__(self, ledger_id: str, now: datetime):
        self.ledger_id, self.now = ledger_id, now
        self.root: str | None = None
        self.root_pub = b""
        self.epoch = 0
        self.devices: dict[str, Device] = {}
        self.status: dict[str, tuple] = {}
        self.seen_keys: set[bytes] = set()
        self.seen_boxes: set[bytes] = set()
        self.hashes: list[str] = []
        self.states: list[DeviceState] = []
        self.last_at: datetime | None = None
        self.idx = -1

    # ── helpers ──
    def _new(self, raw: Any, classes: Iterable[str]) -> Device:
        d = check_descriptor(raw)
        if d.cls not in classes:
            _fail(f"this event can't add a {d.cls}")
        if d.kid in self.status:
            _fail(f"{d.kid} was enrolled before; an enrolment always takes a new key")
        if d.pub in self.seen_keys:
            _fail("this public key was used before; keys are never reused")
        if d.box is not None and d.box in self.seen_boxes:
            _fail("this box was used before; boxes are never reused")
        return d

    def _labels_unique(self, after: Iterable[Device]) -> None:
        labels = [d.label for d in after]
        if len(labels) != len(set(labels)):
            _fail("a label must be unique among the active devices")

    def _active(self, kid: Any, rx: re.Pattern, what: str) -> Device:
        _match(rx, kid, what)
        if kid not in self.status:
            _fail(f"{kid} is unknown")
        if kid not in self.devices:
            _fail(f"{kid} isn't active")
        return self.devices[kid]

    def _enrol(self, d: Device) -> None:
        self.devices[d.kid] = d
        self.status[d.kid] = ("active", self.idx)
        self.seen_keys.add(d.pub)
        if d.box is not None:
            self.seen_boxes.add(d.box)

    def _nonce(self, val: Any, companion: bool) -> None:
        if companion:
            if val is None:
                _fail("a companion enrolment or rekey carries the ceremony nonce")
            _b64(val, "nonce", 16)
        elif val is not None:
            _fail("a custodian enrolment or rekey has nonce null")

    # ── events ──
    def genesis(self, b: dict) -> _Plan:
        _int(b["epoch"], "epoch", 0, 0)
        root_pub = _b64(b["root_pub"], "root_pub", 32)
        try:
            sig.check_ed25519_public(root_pub)
        except sig.SigError as exc:
            _fail(f"root_pub: {exc}")
        rid = sig.root_id(root_pub)
        if b["root"] != rid:
            _fail("genesis root must be the rt- id of its root_pub")

        def apply() -> None:
            self.root, self.root_pub, self.epoch = rid, root_pub, 0
            self.seen_keys.add(root_pub)
        return _Plan([[rid]], {rid: root_pub}, apply)

    def device_enrol(self, b: dict) -> _Plan:
        d = self._new(b["device"], ("custodian", "companion"))
        companion = d.cls == "companion"
        self._nonce(b["nonce"], companion)
        self._labels_unique([*self.devices.values(), d])
        patterns = [[CUSTODIAN, APPROVER, d.kid], [self.root, d.kid]] if companion else [[self.root, d.kid]]
        return _Plan(patterns, {d.kid: d}, lambda: self._enrol(d), d if companion else None)

    def device_rotate(self, b: dict) -> _Plan:
        old = self._active(b["old"], _DK, "old")
        if old.cls != "custodian":
            _fail("only a custodian rotates")
        d = self._new(b["device"], ("custodian",))
        if d.label != old.label:
            _fail("a rotation keeps the device's label")

        def apply() -> None:
            del self.devices[old.kid]
            self.status[old.kid] = ("retired", self.idx)
            self._enrol(d)
        return _Plan([[old.kid, APPROVER, d.kid]], {d.kid: d}, apply)

    def device_rekey(self, b: dict) -> _Plan:
        d = self._active(b["kid"], _DK, "kid")
        new_box = _recipient(b["box"], "box")
        if new_box in self.seen_boxes:
            _fail("this box was used before; boxes are never reused")
        companion = d.cls == "companion"
        self._nonce(b["nonce"], companion)

        def apply() -> None:
            raw = {**d.raw, "box": b["box"]}
            self.devices[d.kid] = replace(d, box=new_box, raw=MappingProxyType(raw))
            self.seen_boxes.add(new_box)
        return _Plan([[d.kid, CUSTODIAN]] if companion else [[d.kid]], {}, apply)

    def device_revoke(self, b: dict) -> _Plan:
        kid = _match(_DK, b["kid"], "kid")
        state = self.status.get(kid, ("unknown",))[0]
        if state == "unknown":
            _fail(f"{kid} is unknown")
        if state == "revoked":
            _fail(f"{kid} is already revoked")
        patterns: list = [[self.root], [CUSTODIAN]] + ([[kid]] if state == "active" else [])

        def apply() -> None:
            self.devices.pop(kid, None)
            self.status[kid] = ("revoked", self.idx)
        return _Plan(patterns, {}, apply)

    def approver_enrol(self, b: dict) -> _Plan:
        d = self._new(b["approver"], ("approver",))
        self._labels_unique([*self.devices.values(), d])
        return _Plan([[self.root, d.kid], [CUSTODIAN, APPROVER, d.kid]], {d.kid: d}, lambda: self._enrol(d), d)

    def approver_revoke(self, b: dict) -> _Plan:
        d = self._active(b["kid"], _AK, "kid")

        def apply() -> None:
            del self.devices[d.kid]
            self.status[d.kid] = ("revoked", self.idx)
        return _Plan([[self.root], [CUSTODIAN]], {}, apply)

    def recovery(self, b: dict) -> _Plan:
        _int(b["epoch"], "epoch")
        if b["epoch"] != self.epoch + 1:
            _fail("a recovery moves to the current epoch plus 1")
        _digest_or_null(b["checkpoint_ref"], "checkpoint_ref")
        new_kid = _match(_DK, b["new_device"], "new_device")
        if not isinstance(b["active"], list):
            _fail("active must be a list of descriptors")
        kept: dict[str, Device] = {}
        new: Device | None = None
        for raw in b["active"]:
            kid = raw.get("kid") if isinstance(raw, dict) else None
            if not isinstance(kid, str):
                _fail("every entry in active is a descriptor with a kid")
            if kid == new_kid:
                new = self._new(raw, ("custodian",))
            elif kid in self.devices:
                if sig._canonical(raw) != sig._canonical(dict(self.devices[kid].raw)):
                    _fail(f"{kid} in active must be byte-identical to its current descriptor")
                kept[kid] = self.devices[kid]
            else:
                _fail(f"{kid!r} in active must be active before the recovery")
        if new is None:
            _fail("new_device must be in active")
        revokes = b["revokes"]
        if not isinstance(revokes, list) or not all(isinstance(k, str) and (_DK.fullmatch(k) or _AK.fullmatch(k))
                                                    for k in revokes):
            _fail("revokes must be a list of dk- and ak- key ids")
        if set(revokes) != set(self.devices) - set(kept):
            _fail("revokes must be exactly the active kids the recovery leaves out")
        self._labels_unique([*kept.values(), new])

        def apply() -> None:
            for kid in revokes:
                self.devices.pop(kid)
                self.status[kid] = ("revoked", self.idx)
            self._enrol(new)
            self.epoch += 1
        return _Plan([[self.root, new_kid]], {new_kid: new}, apply)

    def root_rotate(self, b: dict) -> _Plan:
        _int(b["epoch"], "epoch")
        if b["epoch"] != self.epoch + 1:
            _fail("a root rotation moves to the current epoch plus 1")
        _digest_or_null(b["checkpoint_ref"], "checkpoint_ref")
        root_pub = _b64(b["root_pub"], "root_pub", 32)
        try:
            sig.check_ed25519_public(root_pub)
        except sig.SigError as exc:
            _fail(f"root_pub: {exc}")
        if root_pub in self.seen_keys:
            _fail("this root key was used before; keys are never reused")
        nid = sig.root_id(root_pub)

        def apply() -> None:
            self.root, self.root_pub = nid, root_pub
            self.seen_keys.add(root_pub)
            self.epoch += 1
        return _Plan([[self.root, nid]], {nid: root_pub}, apply)

    # ── one line ──
    def step(self, idx: int, line: bytes) -> None:
        body, sigs = parse_line(line, DEVICES_KIND)
        if body["idx"] != idx:
            _fail(f"line {idx} says idx {body['idx']}")
        if body["prev"] != (self.hashes[-1] if self.hashes else None):
            _fail(f"line {idx}: prev doesn't match the previous line")
        if body["ledger_id"] != self.ledger_id:
            _fail(f"line {idx}: ledger_id isn't the pinned ledger")
        at = _time(body["at"], "at")
        if self.last_at is not None and at < self.last_at:
            _fail(f"line {idx}: at goes backwards")
        if at > self.now + FUTURE_SLACK:
            _fail(f"line {idx}: at is in the future (more than 5 minutes past the verifier's clock)")
        event = body["event"]
        if (idx == 0) != (event == "genesis"):
            _fail("line 0 must be genesis, and genesis only on line 0")
        if idx > 0 and body["root"] != self.root:
            _fail(f"line {idx}: root must be the root current before the line")
        self.idx = idx
        plan = getattr(self, event)(body)
        _match_signers([s["key_id"] for s in sigs], plan.patterns, self.devices, event)
        keys: dict[str, Any] = {**self.devices, **plan.new_keys}
        if self.root is not None:
            keys[self.root] = self.root_pub
        assertions = _verify_sigs(DEVICES_KIND, body, sigs, keys)
        if plan.enrolled is not None:
            a = assertions.get(plan.enrolled.kid)
            if a is None or a.be != plan.enrolled.be or a.bs != plan.enrolled.bs:
                _fail("the new key's BE and BS flags don't match its descriptor")
        plan.apply()
        self.hashes.append(line_hash(line))
        self.last_at = at
        self.states.append(DeviceState(idx=idx, at=body["at"], root=self.root, root_pub=self.root_pub,
                                       epoch=self.epoch, devices=MappingProxyType(dict(self.devices)),
                                       status=MappingProxyType(dict(self.status))))


def _guarded(step: Callable[[int, bytes], None], idx: int, line: bytes) -> None:
    """Fail closed: hostile bytes that slip past a type check still reject the log, never crash the verifier."""
    try:
        step(idx, line)
    except LogError:
        raise
    except (TypeError, KeyError, AttributeError, IndexError, ValueError, RecursionError) as exc:
        raise LogError(f"line {idx} is malformed ({type(exc).__name__})") from None


def replay_devices(data: bytes, *, ledger_id: str, root: str, now: datetime, prefix: bool = False) -> DevicesLog:
    """Replay devices.jsonl from genesis. `root` is the pinned current root: after the last line the current
    root must equal it, unless this is a replay of a prefix (then the prefix check vouches instead)."""
    _match(_LEDGER_ID, ledger_id, "pinned ledger_id")
    _match(_ROOT, root, "pinned root")
    lines = split_log(data)
    if not lines:
        _fail("the devices log is empty (it starts with genesis)")
    r = _DevicesReplay(ledger_id, _utc(now))
    for idx, line in enumerate(lines):
        _guarded(r.step, idx, line)
    if not prefix and r.root != root:
        _fail("the current root after the last line isn't the pinned root")
    return DevicesLog(lines=tuple(lines), hashes=tuple(r.hashes), states=tuple(r.states),
                      boxes=frozenset(r.seen_boxes))


# ── readers.jsonl ──

@dataclass(frozen=True)
class ReaderState:
    reader_id: str
    surface: str
    region: str
    viewing: str
    identity_assurance: str
    recipient: str
    scopes: Mapping[str, Mapping[str, Any]] = field(repr=False)
    expires: str
    epoch: int
    enrolled_idx: int
    revoked_idx: int | None
    approved_at: str
    reviewed: Mapping[str, frozenset] = field(repr=False)

    def status(self, *, now: datetime, epoch: int) -> str:
        if self.revoked_idx is not None:
            return "revoked"
        if self.epoch != epoch:
            return "ended"
        if _time(self.expires, "expires") <= _utc(now):
            return "expired"
        return "active"


@dataclass(frozen=True)
class ReadersLog:
    lines: tuple
    states: tuple  # after each line: Mapping reader_id -> ReaderState

    def reader(self, reader_id: str, idx: int = -1) -> ReaderState | None:
        if not self.states:
            return None
        return self.states[idx].get(reader_id)

    def status(self, reader_id: str, *, now: datetime, epoch: int, idx: int = -1) -> str | None:
        r = self.reader(reader_id, idx)
        return None if r is None else r.status(now=now, epoch=epoch)

    def active_readers(self, *, now: datetime, epoch: int) -> list[str]:
        if not self.states:
            return []
        return sorted(rid for rid, r in self.states[-1].items() if r.status(now=now, epoch=epoch) == "active")


class _ReadersReplay:
    def __init__(self, devices: DevicesLog, ledger_id: str, now: datetime):
        self.dev, self.ledger_id, self.now = devices, ledger_id, now
        self.readers: dict[str, ReaderState] = {}
        self.recipients: set[bytes] = set()
        self.hashes: list[str] = []
        self.states: list[Mapping] = []
        self.last_at: datetime | None = None
        self.last_devices_at = -1
        self.idx = -1

    def _fresh(self, val: Any) -> bytes:
        key = _recipient(val, "recipient")
        if key in self.dev.boxes or key in self.recipients:
            _fail("this recipient was used before (a device box or an earlier reader); recipients are never reused")
        return key

    def _reader(self, rid: Any, at: datetime, epoch: int) -> ReaderState:
        _match(_READER, rid, "reader_id")
        r = self.readers.get(rid)
        if r is None or r.revoked_idx is not None or r.epoch != epoch or _time(r.expires, "expires") <= at:
            _fail(f"reader {rid} isn't active (enrolled in this epoch, not revoked, not expired)")
        return r

    @staticmethod
    def _expires(val: Any, at: datetime) -> datetime:
        exp = _time(val, "expires")
        if exp <= at or exp - at > READER_MAX:
            _fail("expires must be after at and at most 90 days later")
        return exp

    def reader_enrol(self, b: dict, at: datetime, epoch: int) -> tuple[list, Callable]:
        r = _keys(b["reader"], READER_KEYS, "the reader")
        rid = _match(_READER, r["reader_id"], "reader_id")
        if rid in self.readers:
            _fail(f"reader {rid} was seen before; a reader id is enrolled once")
        for name in ("surface", "region", "viewing", "identity_assurance"):
            if not isinstance(r[name], str):
                _fail(f"reader {name} must be text")
        if r["surface"] in RESERVED_SURFACES or r["surface"] != SLOT_SURFACE:
            _fail(f"surface must be {SLOT_SURFACE} for an enrolled reader in Cut 1, got {r['surface']!r}")
        if r["region"] != region_for_surface(r["surface"]):
            _fail("region is derived from the surface, never typed in")
        if r["viewing"] not in VIEWING:
            _fail(f"unknown viewing label {r['viewing']!r}")
        if r["identity_assurance"] != IDENTITY_ASSURANCE:
            _fail(f"identity_assurance must be {IDENTITY_ASSURANCE} in v1")
        key = self._fresh(r["recipient"])
        scopes = _check_scopes(r["scopes"], "reader scopes")
        self._expires(r["expires"], at)
        _match(_DIGEST, b["dry_run_digest"], "dry_run_digest")
        reviewed = _check_reviewed(b["reviewed"], scopes)

        def apply() -> None:
            self.recipients.add(key)
            self.readers[rid] = ReaderState(
                reader_id=rid, surface=r["surface"], region=r["region"], viewing=r["viewing"],
                identity_assurance=r["identity_assurance"], recipient=r["recipient"],
                scopes=MappingProxyType(scopes), expires=r["expires"], epoch=epoch, enrolled_idx=self.idx,
                revoked_idx=None, approved_at=b["at"], reviewed=MappingProxyType(reviewed))
        return [[CUSTODIAN, APPROVER]], apply

    def reader_scope(self, b: dict, at: datetime, epoch: int) -> tuple[list, Callable]:
        r = self._reader(b["reader_id"], at, epoch)
        scopes = _check_scopes(b["scopes"], "scopes")
        _match(_DIGEST, b["dry_run_digest"], "dry_run_digest")
        listed = _check_reviewed(b["reviewed"], scopes)
        reviewed = {}
        for sid, s in scopes.items():
            same = sid in r.scopes and r.scopes[sid]["rule_digest"] == s["rule_digest"]
            reviewed[sid] = (r.reviewed[sid] | listed[sid]) if same else listed[sid]

        def apply() -> None:
            self.readers[r.reader_id] = replace(r, scopes=MappingProxyType(scopes),
                                                reviewed=MappingProxyType(reviewed), approved_at=b["at"])
        return [[CUSTODIAN, APPROVER]], apply

    def reader_renew(self, b: dict, at: datetime, epoch: int) -> tuple[list, Callable]:
        r = self._reader(b["reader_id"], at, epoch)
        key = self._fresh(b["recipient"])
        exp = self._expires(b["expires"], at)
        if exp - _time(r.approved_at, "approved_at") > READER_MAX:
            _fail("a tapless renewal can't run past 90 days after the reader's last approver-signed line")

        def apply() -> None:
            self.recipients.add(key)
            self.readers[r.reader_id] = replace(r, recipient=b["recipient"], expires=b["expires"])
        return [[CUSTODIAN]], apply

    def reader_review(self, b: dict, at: datetime, epoch: int) -> tuple[list, Callable]:
        r = self._reader(b["reader_id"], at, epoch)
        sid = _match(_SCOPE_ID, b["scope_id"], "scope_id")
        if sid not in r.scopes:
            _fail(f"scope_id {sid!r} isn't one of the reader's current scopes")
        if b["rule_digest"] != r.scopes[sid]["rule_digest"]:
            _fail("rule_digest isn't the scope's current one")
        ids = b["reviewed_ids"]
        if not isinstance(ids, list) or not ids:
            _fail("reviewed_ids must be a non-empty list")
        for i in ids:
            _match(_RECORD_ID, i, "a reviewed record id")
        _match(_DIGEST, b["dry_run_digest"], "dry_run_digest")

        def apply() -> None:
            reviewed = {**r.reviewed, sid: r.reviewed[sid] | frozenset(ids)}
            self.readers[r.reader_id] = replace(r, reviewed=MappingProxyType(reviewed))
        return [[CUSTODIAN]], apply

    def reader_revoke(self, b: dict, at: datetime, epoch: int) -> tuple[list, Callable]:
        rid = _match(_READER, b["reader_id"], "reader_id")
        r = self.readers.get(rid)
        if r is None:
            _fail(f"reader {rid} was never enrolled")
        if r.revoked_idx is not None:
            _fail(f"reader {rid} is already revoked; revocation is final")

        def apply() -> None:
            self.readers[rid] = replace(r, revoked_idx=self.idx)
        return [[CUSTODIAN]], apply

    def step(self, idx: int, line: bytes) -> None:
        body, sigs = parse_line(line, READERS_KIND)
        if body["idx"] != idx:
            _fail(f"line {idx} says idx {body['idx']}")
        if body["prev"] != (self.hashes[-1] if self.hashes else None):
            _fail(f"line {idx}: prev doesn't match the previous line")
        if body["ledger_id"] != self.ledger_id:
            _fail(f"line {idx}: ledger_id isn't the pinned ledger")
        at = _time(body["at"], "at")
        if self.last_at is not None and at < self.last_at:
            _fail(f"line {idx}: at goes backwards")
        if at > self.now + FUTURE_SLACK:
            _fail(f"line {idx}: at is in the future (more than 5 minutes past the verifier's clock)")
        da = body["devices_at"]
        di = da["idx"]
        if di >= len(self.dev.lines) or self.dev.hashes[di] != da["line"]:
            _fail(f"line {idx}: devices_at doesn't name a line of the devices log")
        if di < self.last_devices_at:
            _fail(f"line {idx}: devices_at goes down")
        dstate = self.dev.state(di)
        if _time(dstate.at, "devices at") > at:
            _fail(f"line {idx}: devices_at names a devices line written after this one")
        if di + 1 < len(self.dev.lines) and _time(self.dev.state(di + 1).at, "devices at") <= at:
            _fail(f"line {idx}: devices_at isn't the devices tail when this line was written")
        if body["root"] != dstate.root:
            _fail(f"line {idx}: root isn't the root current at devices_at")
        self.idx = idx
        patterns, apply = getattr(self, body["event"])(body, at, dstate.epoch)
        _match_signers([s["key_id"] for s in sigs], patterns, dstate.devices, body["event"])
        _verify_sigs(READERS_KIND, body, sigs, dstate.devices)
        apply()
        self.hashes.append(line_hash(line))
        self.last_at = at
        self.last_devices_at = di
        self.states.append(MappingProxyType(dict(self.readers)))


def replay_readers(data: bytes, devices: DevicesLog, *, ledger_id: str, now: datetime) -> ReadersLog:
    """Replay readers.jsonl against a replayed devices log (each line names the devices line it was signed
    against). An empty readers log is valid."""
    _match(_LEDGER_ID, ledger_id, "pinned ledger_id")
    lines = split_log(data)
    r = _ReadersReplay(devices, ledger_id, _utc(now))
    for idx, line in enumerate(lines):
        _guarded(r.step, idx, line)
    return ReadersLog(lines=tuple(lines), states=tuple(r.states))


# ── The prefix check (§18.6, §19.3 step 6) ──

def _root_signed(line: bytes, devices: DevicesLog, idx: int) -> bool:
    """A recovery or root_rotate line at idx whose rt- signature verifies with the root current before it."""
    try:
        if idx < 1 or idx > len(devices.states):
            return False
        before = devices.state(idx - 1)
        body, sigs = parse_line(line, DEVICES_KIND)
        if body["event"] not in ("recovery", "root_rotate") or body["idx"] != idx:
            return False
        s = next((x for x in sigs if x["key_id"] == before.root), None)
        if s is None:
            return False
        sig.verify(DEVICES_KIND, sig._canonical(body), s, before.root_pub)
        return True
    except Exception:  # anything that doesn't verify cleanly isn't evidence of a root fork
        return False


def _body_bytes(line: bytes) -> bytes | None:
    try:
        return sig._canonical(parse_line(line, DEVICES_KIND)[0])
    except Exception:
        return None


def compare_copies(older: bytes, newer: bytes, *, devices: DevicesLog | None = None) -> None:
    """`newer` is bound to a later object than `older`, so it must start with older's exact bytes. A shorter
    copy is ROLLBACK; two different lines at one idx are a FORK. It's named a root fork only when `devices`
    (the replay of the older copy) is given, both lines are recovery or root_rotate lines whose root
    signature verifies with the root current before that idx, and the two signed bodies differ, so a relay
    can't fake one by editing signatures."""
    old_lines = split_log(older)
    if bytes(newer).startswith(bytes(older)):
        return
    new_lines = split_log(newer)
    for i, line in enumerate(old_lines):
        if i >= len(new_lines):
            raise LogRollback(f"the later copy stops at line {len(new_lines)}; the earlier one has {len(old_lines)}")
        if new_lines[i] != line:
            # Two root-signed lines that sign the SAME body are one event with edited signatures (a relay
            # stripping or adding a co-signature), not the paper key in two hands.
            root_fork = devices is not None and _root_signed(line, devices, i) and \
                _root_signed(new_lines[i], devices, i) and _body_bytes(line) != _body_bytes(new_lines[i])
            raise LogFork(f"the copies differ at line {i}" + (": a root fork" if root_fork else ""), i, root_fork)
    raise LogFork("the copies differ")  # pragma: no cover - equal lines mean one is a prefix of the other


def check_extends(newer: bytes, *, byte_length: Any, digest: Any) -> None:
    """The prefix check against a cited (byte_length, digest): a checkpoint's devices, a body's logs, or a
    session's high-water mark. The cited length must end right after a line's newline."""
    _int(byte_length, "byte_length")
    _match(_DIGEST, digest, "digest")
    newer = bytes(newer)
    if byte_length > len(newer):
        raise LogRollback("the log is shorter than the length an earlier object cited")
    if byte_length and newer[byte_length - 1:byte_length] != b"\n":
        _fail("a cited byte_length must end right after a line's newline")
    if "sha256-" + hashlib.sha256(newer[:byte_length]).hexdigest() != digest:
        raise LogFork("the log doesn't start with the bytes an earlier object cited")


# ── Writing ──

class LogWriter:
    """Append lines to a log under an exclusive lock. The caller builds a body from `next_idx`, `prev` and the
    tail's time, gathers every signature, and appends while still holding the lock. Each line goes in one
    O_APPEND write followed by a full sync. A torn final line found on opening is moved to forks/ and
    reported in `repaired`; the operation that wrote it is then redone."""

    def __init__(self, path: Path | str, *, kind: str, forks_dir: Path | str):
        if kind not in (DEVICES_KIND, READERS_KIND):
            _fail(f"unknown log kind {kind!r}")
        self.path, self.kind, self.forks_dir = Path(path), kind, Path(forks_dir)
        self.fd: int | None = None
        self.repaired: bytes | None = None
        self.next_idx = 0
        self.prev: str | None = None
        self.last_at: datetime | None = None

    def __enter__(self) -> "LogWriter":
        _private_dir(self.path.parent)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise LogError(f"can't open {self.path.name}: {exc.strerror}") from None
        self.fd = fd
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                _fail(f"{self.path.name} must be a regular file owned by this user")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._read_all()
            if data and not data.endswith(b"\n"):
                keep = data.rfind(b"\n") + 1
                self.repaired = data[keep:]
                self._save_fragment(keep, self.repaired)
                os.ftruncate(fd, keep)
                _full_fsync(fd)
                data = data[:keep]
            for i, line in enumerate(split_log(data)):
                body, _ = parse_line(line, self.kind)
                if body["idx"] != i or body["prev"] != self.prev:
                    _fail(f"{self.path.name} line {i} doesn't chain; replay the log before writing")
                self.prev = line_hash(line)
                self.last_at = _time(body["at"], "at")
                self.next_idx = i + 1
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _save_fragment(self, offset: int, fragment: bytes) -> None:
        """Keep every torn fragment: a new 0600 file each time, never replacing an earlier one."""
        forks = _private_dir(self.forks_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for n in range(1000):
            target = forks / f"{self.path.name}.{stamp}.{offset}{'.' + str(n) if n else ''}.torn"
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as fh:
                fh.write(fragment)
                fh.flush()
                _full_fsync(fh.fileno())
            _fsync_dir(forks)
            return
        _fail("too many torn fragments saved this second")

    def _read_all(self) -> bytes:
        chunks, offset = [], 0
        while True:
            chunk = os.pread(self.fd, 1 << 16, offset)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            offset += len(chunk)

    def append(self, line: bytes) -> None:
        if self.fd is None:
            _fail("the log isn't open")
        body, _ = parse_line(line, self.kind)
        if body["idx"] != self.next_idx or body["prev"] != self.prev:
            _fail("the line doesn't fit the log tail (idx or prev); rebuild it and gather the signatures again")
        at = _time(body["at"], "at")
        if self.last_at is not None and at < self.last_at:
            _fail("at goes backwards; wait for the clock to pass the previous line")
        data = bytes(line) + b"\n"
        size = os.fstat(self.fd).st_size
        try:
            written = os.write(self.fd, data)
            if written != len(data):
                raise LogError("the line was only partly written")
            _full_fsync(self.fd)
        except (OSError, LogError) as exc:
            # Put the file back as it was and close the writer: a retry starts from a fresh open.
            try:
                os.ftruncate(self.fd, size)
                _full_fsync(self.fd)
            except OSError:
                pass
            self.__exit__(None, None, None)
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise LogError(f"append failed and was rolled back ({reason}); reopen the log and redo it") from None
        self.prev, self.last_at, self.next_idx = line_hash(bytes(line)), at, self.next_idx + 1

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
