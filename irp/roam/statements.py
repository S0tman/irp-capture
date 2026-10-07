"""Content and disclosure statements for a Slice (Roaming IRP spec v0.3 §15.1, §15.3, §15.7).

The content statement (`irp/content.json`) says what's in a Slice: which files,
which fingerprint (`rekadu_digest`) and which checkpoint. It doesn't depend on
the recipient. The disclosure statement (`irp/disclosure.json`) says who it was
for: which reader, on which surface, under which scope, until when, labelled A0
("user-declared, not attested"). Both are signed over their exact JCS bytes.

The manifest isn't signed. The §15.3 binding rules authenticate every field of
it against the two statements and the checkpoint bytes, so a swapped, edited or
replayed Slice fails before anyone reads it.

Verify order (§15.1): the signature over the shipped bytes first, then a strict
parse (duplicate keys rejected), then JCS equality, then the closed schema.
Nothing is negotiated (§15.7): unknown versions, kinds, algorithms, surfaces and
keys are rejected. The region is derived from the surface, never typed in.

Formats the spec gives only by example are closed here: reader ids are `rd-` (or
`eph-` on the browser path) plus 32 lowercase hex, scope ids are short lowercase
tokens, `viewing` is `employer-managed` or `personal-device`, a mailbox
`sid_sha256` is bare lowercase hex, integers stay within 0 to 2^53 - 1 (I-JSON),
and reader slots count from 1 with a null `prev` on the first.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from . import sig
from .age import AgeError, Recipient
from .container import _ARTEFACT, ContainerError, check_binding
from .rekadu import DISCLOSURE_KEYS as PROJECTION_KEYS
from .rekadu import MANIFEST_KEYS, OMITTED_REASONS, REKADU_VERSION, RekaduError, digest_for, region_for_surface

STATEMENT_KINDS = ("content", "disclosure")
CONTENT_KEYS = frozenset({"v", "kind", "ledger_id", "root", "epoch", "rekadu_version", "rekadu_digest", "built_at",
                          "checkpoint", "files", "signer"})
DISCLOSURE_STATEMENT_KEYS = frozenset({"v", "kind", "ledger_id", "root", "epoch", "disclosure_id", "issued_at",
                                       "expires", "content", "rekadu_digest", "reader", "audience", "delivery",
                                       "scope", "omitted", "return", "signer"})
READER_KEYS = frozenset({"reader_id", "surface", "region", "viewing", "identity_assurance"})
FILES_REQUIRED = frozenset({"IRP-RETURN.txt", "ledger.jsonl", "irp/checkpoint.json", "irp/checkpoint.sig",
                            "irp/devices.jsonl"})
FILES_OPTIONAL = frozenset({"irp/checkpoint.tsr"})  # absent when the checkpoint is unwitnessed
SLICE_STATEMENT_MEMBERS = ("irp/content.json", "irp/content.sig", "irp/disclosure.json", "irp/disclosure.sig",
                           "manifest.json")
SLOT_SURFACE = "claude-code-cloud"       # delivered to a reader slot, signed by the laptop (ed25519)
MAILBOX_SURFACE = "browser-ephemeral"    # delivered to a mailbox, signed by the phone (webauthn-es256)
RESERVED_SURFACES = frozenset({"browser-managed", "eu-sovereign-runtime"})
VIEWING = frozenset({"employer-managed", "personal-device"})
IDENTITY_ASSURANCE = "A0"
FEEDS = frozenset({"reader"})
MAX_INT = 2**53 - 1  # I-JSON: JCS carries integers exactly only up to here
STANDING_MAX = timedelta(days=14)  # §16.5: a standing disclosure lives at most issued_at + 14 days
BROWSER_MAX = timedelta(hours=2)   # §16.5: a browser disclosure lives 2 hours or less

_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")  # fullmatch only
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_LEDGER_ID = re.compile(r"ILID-[0-9a-f]{32}")
_ROOT = re.compile(r"rt-[0-9a-f]{32}")
_DEVICE = re.compile(r"dk-[0-9a-f]{32}")
_READER = re.compile(r"rd-[0-9a-f]{32}")
_EPHEMERAL = re.compile(r"eph-[0-9a-f]{32}")
_SCOPE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class StatementError(ValueError):
    """A statement, signature or binding rule fails. A reader stops with no plaintext written."""


@dataclass(frozen=True)
class VerifiedSlice:
    content: dict[str, Any]
    disclosure: dict[str, Any]
    manifest: dict[str, Any]


def _sha(data: bytes) -> str:
    return "sha256-" + hashlib.sha256(data).hexdigest()


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON values, so true never passes for 1 (Python's == says it does)."""
    from irp.integrity.canonical import canonicalize

    try:
        return canonicalize(a) == canonicalize(b)
    except Exception:
        return False


# ── Field checks ──

def _keys(obj: Any, keys: frozenset[str] | tuple[str, ...], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise StatementError(f"{what} must have exactly the keys {', '.join(sorted(keys))}")
    return obj


def _match(pattern: re.Pattern[str], val: Any, what: str) -> str:
    if not (isinstance(val, str) and pattern.fullmatch(val)):
        raise StatementError(f"{what} has the wrong format: {val!r}")
    return val


def _int(val: Any, what: str, minimum: int) -> int:
    if type(val) is not int or not minimum <= val <= MAX_INT:
        raise StatementError(f"{what} must be an integer from {minimum} to 2^53 - 1, got {val!r}")
    return val


def _time(val: Any, what: str) -> datetime:
    _match(_TIMESTAMP, val, what)
    try:
        return datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise StatementError(f"{what} isn't a real UTC time: {val!r}") from None


def _common(stmt: dict[str, Any], kind: str) -> None:
    if type(stmt["v"]) is not int or stmt["v"] != 1:
        raise StatementError(f"unsupported statement version {stmt['v']!r}; v1 only")
    if stmt["kind"] != kind:
        raise StatementError(f"not a {kind} statement")
    _match(_LEDGER_ID, stmt["ledger_id"], "ledger_id")
    _match(_ROOT, stmt["root"], "root")
    _int(stmt["epoch"], "epoch", 0)


def _signer(val: Any, algs: tuple[str, ...], what: str) -> None:
    _keys(val, ("alg", "key_id"), f"{what} signer")
    if not (isinstance(val["alg"], str) and val["alg"] in algs):
        raise StatementError(f"{what} must be signed with {' or '.join(algs)}, got {val['alg']!r}")
    _match(_DEVICE, val["key_id"], f"{what} signer key_id")


def _check_files(files: Any) -> None:
    if not isinstance(files, dict):
        raise StatementError("files must map member names to sha256 digests")
    for name, digest in files.items():
        _match(_DIGEST, digest, f"files[{name!r}]")
        artefact = _ARTEFACT.fullmatch(name)
        if artefact:
            if digest != "sha256-" + artefact.group(1):
                raise StatementError(f"{name} must hash to the digest in its name")
        elif name not in FILES_REQUIRED and name not in FILES_OPTIONAL:
            raise StatementError(f"{name} can't be listed in a content statement's files map")
    missing = sorted(FILES_REQUIRED - set(files))
    if missing:
        raise StatementError(f"files map is missing {', '.join(missing)}")


def _check_content(c: dict[str, Any]) -> None:
    _keys(c, CONTENT_KEYS, "content statement")
    _common(c, "content")
    if c["rekadu_version"] != REKADU_VERSION:
        raise StatementError(f"unsupported rekadu_version {c['rekadu_version']!r}; expected {REKADU_VERSION}")
    _match(_DIGEST, c["rekadu_digest"], "rekadu_digest")
    _time(c["built_at"], "built_at")
    ck = _keys(c["checkpoint"], ("strand", "seq", "digest"), "content checkpoint")
    _match(_DEVICE, ck["strand"], "checkpoint strand")
    _int(ck["seq"], "checkpoint seq", 0)
    _match(_DIGEST, ck["digest"], "checkpoint digest")
    _check_files(c["files"])
    _signer(c["signer"], (sig.ALG,), "a content statement")


def _check_audience(audience: Any, reader_id: str, mailbox: bool) -> None:
    if not isinstance(audience, list) or not audience:
        raise StatementError("audience must be a non-empty list")
    for entry in audience:
        _keys(entry, ("id", "role", "recipient"), "audience entry")
        if entry["role"] not in ("reader", "device"):
            raise StatementError(f"unknown audience role {entry['role']!r}")
        try:
            Recipient.from_string(entry["recipient"])
        except AgeError as exc:
            raise StatementError(f"audience recipient: {exc}") from None
    if audience[0]["role"] != "reader" or audience[0]["id"] != reader_id:
        raise StatementError("the first audience entry must be this disclosure's reader")
    devices = audience[1:]
    if any(e["role"] != "device" for e in devices):
        raise StatementError("a disclosure has exactly one reader in its audience")
    ids = [_match(_DEVICE, e["id"], "audience device id") for e in devices]
    if ids != sorted(set(ids)):
        raise StatementError("audience devices must be sorted by id, without duplicates")
    recipients = [e["recipient"] for e in audience]
    if len(set(recipients)) != len(recipients):
        raise StatementError("audience recipients must be distinct")
    if mailbox and devices:
        raise StatementError("a mailbox delivery is encrypted to the viewer key only")
    if not mailbox and not devices:
        raise StatementError("a reader slot is also encrypted to the active devices")


def _check_delivery(delivery: Any, scope_id: str, mailbox: bool) -> None:
    if mailbox:
        box = _keys(_keys(delivery, ("mailbox",), "a browser delivery")["mailbox"], ("sid_sha256",), "mailbox")
        _match(_HEX64, box["sid_sha256"], "mailbox sid_sha256")
        return
    slot = _keys(_keys(delivery, ("slot",), "a reader delivery")["slot"], ("feed", "scope_id", "seq", "prev"), "slot")
    if slot["feed"] not in FEEDS:
        raise StatementError(f"unknown slot feed {slot['feed']!r}")
    if slot["scope_id"] != scope_id:
        raise StatementError("the delivery slot's scope_id must be the disclosure's scope_id")
    seq = _int(slot["seq"], "slot seq", 1)
    if seq == 1:
        if slot["prev"] is not None:
            raise StatementError("the first slot (seq 1) has a null prev")
    else:
        _match(_DIGEST, slot["prev"], "slot prev")


def _check_disclosure(d: dict[str, Any]) -> None:
    _keys(d, DISCLOSURE_STATEMENT_KEYS, "disclosure statement")
    _common(d, "disclosure")
    did = d["disclosure_id"]
    if not (isinstance(did, str) and did.startswith("de-")):
        raise StatementError("disclosure_id must be de-<b64url of 16 bytes>")
    try:
        sig.b64url_decode(did[3:], 16)
    except sig.SigError as exc:
        raise StatementError(f"disclosure_id: {exc}") from None
    issued, expires = _time(d["issued_at"], "issued_at"), _time(d["expires"], "expires")
    _match(_DIGEST, d["content"], "content")
    _match(_DIGEST, d["rekadu_digest"], "rekadu_digest")

    reader = _keys(d["reader"], READER_KEYS, "reader")
    surface = reader["surface"]
    if surface in RESERVED_SURFACES:
        raise StatementError(f"surface {surface!r} is reserved and refused in Cut 1")
    if surface not in (SLOT_SURFACE, MAILBOX_SURFACE):
        raise StatementError(f"unknown reader surface {surface!r}")
    if reader["region"] != region_for_surface(surface):
        raise StatementError(f"region {reader['region']!r} isn't the region of {surface} (it's derived, not typed in)")
    if reader["viewing"] not in VIEWING:
        raise StatementError(f"unknown viewing label {reader['viewing']!r}")
    if reader["identity_assurance"] != IDENTITY_ASSURANCE:
        raise StatementError(f"identity_assurance must be {IDENTITY_ASSURANCE} in v1 (user-declared, not attested)")
    mailbox = surface == MAILBOX_SURFACE
    _match(_EPHEMERAL if mailbox else _READER, reader["reader_id"], "reader_id")

    if expires <= issued:
        raise StatementError("expires must be after issued_at")
    if expires - issued > (BROWSER_MAX if mailbox else STANDING_MAX):
        raise StatementError("the disclosure window is longer than its surface allows (§16.5)")

    _check_audience(d["audience"], reader["reader_id"], mailbox)
    scope = _keys(d["scope"], ("op", "scope_id", "rule_digest"), "scope")
    if scope["op"] == "propose":
        raise StatementError("scope op 'propose' is reserved until Cut 2")
    if scope["op"] != "read":
        raise StatementError(f"unknown scope op {scope['op']!r}")
    _match(_SCOPE_ID, scope["scope_id"], "scope_id")
    _match(_DIGEST, scope["rule_digest"], "rule_digest")
    _check_delivery(d["delivery"], scope["scope_id"], mailbox)

    omitted = _keys(d["omitted"], OMITTED_REASONS, "omitted")
    for reason in OMITTED_REASONS:
        _int(omitted[reason], f"omitted {reason}", 0)
    if d["return"] is not None:
        raise StatementError("return must be null in v1")
    _signer(d["signer"], ("webauthn-es256",) if mailbox else (sig.ALG,), "a disclosure statement")


def check_statement(stmt: Any) -> None:
    """The closed schema for a content or disclosure statement (§15.1 step 4)."""
    if not isinstance(stmt, dict) or stmt.get("kind") not in STATEMENT_KINDS:
        raise StatementError(f"a statement's kind must be one of {', '.join(STATEMENT_KINDS)}")
    try:
        (_check_content if stmt["kind"] == "content" else _check_disclosure)(stmt)
    except StatementError:
        raise
    except (TypeError, KeyError, AttributeError, ValueError) as exc:  # odd shapes fail closed, never crash
        raise StatementError(f"malformed {stmt['kind']} statement ({type(exc).__name__})") from None


# ── Building, encoding and parsing ──

def content_statement(*, ledger_id: str, root: str, epoch: int, rekadu_digest: str, built_at: str,
                      checkpoint: Mapping[str, Any], files: Mapping[str, str],
                      signer: Mapping[str, str]) -> dict[str, Any]:
    stmt = {"v": 1, "kind": "content", "ledger_id": ledger_id, "root": root, "epoch": epoch,
            "rekadu_version": REKADU_VERSION, "rekadu_digest": rekadu_digest, "built_at": built_at,
            "checkpoint": dict(checkpoint), "files": dict(files), "signer": dict(signer)}
    check_statement(stmt)
    return stmt


def disclosure_statement(*, ledger_id: str, root: str, epoch: int, disclosure_id: str, issued_at: str,
                         expires: str, content: str, rekadu_digest: str, reader_id: str, surface: str,
                         viewing: str, audience: list[Mapping[str, str]], delivery: Mapping[str, Any],
                         scope: Mapping[str, Any], omitted: Mapping[str, int],
                         signer: Mapping[str, str]) -> dict[str, Any]:
    """The region and identity_assurance are filled in here, from the surface and the v1 rule (A0)."""
    try:
        region = region_for_surface(surface)
    except RekaduError as exc:
        raise StatementError(str(exc)) from None
    stmt = {"v": 1, "kind": "disclosure", "ledger_id": ledger_id, "root": root, "epoch": epoch,
            "disclosure_id": disclosure_id, "issued_at": issued_at, "expires": expires, "content": content,
            "rekadu_digest": rekadu_digest,
            "reader": {"reader_id": reader_id, "surface": surface, "region": region, "viewing": viewing,
                       "identity_assurance": IDENTITY_ASSURANCE},
            "audience": copy.deepcopy(list(audience)), "delivery": copy.deepcopy(dict(delivery)),
            "scope": dict(scope), "omitted": dict(omitted), "return": None, "signer": dict(signer)}
    check_statement(stmt)
    return stmt


def encode_statement(stmt: Mapping[str, Any]) -> bytes:
    """The exact bytes that ship: JCS, no trailing newline."""
    check_statement(stmt)
    from irp.integrity.canonical import canonicalize

    try:
        return canonicalize(stmt)
    except Exception as exc:  # the schema should make this unreachable; fail closed if it doesn't
        raise StatementError(f"can't encode the statement as JCS: {type(exc).__name__}") from None


def parse_statement(data: bytes, kind: str) -> dict[str, Any]:
    """§15.1 steps 2 to 4 for a statement of the expected kind. Call after the signature check."""
    if kind not in STATEMENT_KINDS:
        raise StatementError(f"unknown statement kind {kind!r}")
    obj = sig.load_jcs(data, f"{kind} statement", error=StatementError)
    if not isinstance(obj, dict) or obj.get("kind") != kind:
        raise StatementError(f"not a {kind} statement")
    check_statement(obj)
    return obj


# ── Signing and verifying ──

def sign_statement(stmt: Mapping[str, Any], seed: bytes) -> tuple[bytes, bytes]:
    """Sign a statement with the laptop's Ed25519 seed. Returns (statement bytes, .sig file bytes)."""
    data = encode_statement(stmt)
    signer = stmt["signer"]
    if signer["alg"] != sig.ALG:
        raise StatementError(f"{signer['alg']} statements are signed on the phone, not here")
    try:
        sig_obj = sig.sign(stmt["kind"], data, seed, signer["key_id"])
    except sig.SigError as exc:
        raise StatementError(f"can't sign the {stmt['kind']} statement: {exc}") from None
    return data, sig.encode_sig(sig_obj)


def verify_statement(data: bytes, sig_data: bytes, keys: Mapping[str, bytes], kind: str) -> dict[str, Any]:
    """Verify in spec order: the signature over the shipped bytes, then strict parse, JCS equality and the
    closed schema; then the statement's named signer must be the key that signed it. `keys` maps the
    signing key ids the caller trusts (from the replayed device log) to their public keys."""
    if kind not in STATEMENT_KINDS:
        raise StatementError(f"unknown statement kind {kind!r}")
    try:
        sig_obj = sig.parse_sig(sig_data)
    except sig.SigError as exc:
        raise StatementError(f"{kind} signature file: {exc}") from None
    pub = keys.get(sig_obj["key_id"]) if isinstance(keys, Mapping) else None
    if pub is None:
        raise StatementError(f"the {kind} statement is signed by an unknown key {sig_obj['key_id']}")
    try:
        sig.verify(kind, data, sig_obj, pub)
    except sig.SigError as exc:
        raise StatementError(f"{kind} signature: {exc}") from None
    stmt = parse_statement(data, kind)
    if stmt["signer"] != {"alg": sig_obj["alg"], "key_id": sig_obj["key_id"]}:
        raise StatementError(f"the {kind} statement names a different signer than the key that signed it")
    return stmt


# ── Binding rules (§15.3) ──

def disclosure_projection(disclosure: Mapping[str, Any]) -> dict[str, Any]:
    """What `manifest.disclosure` must equal: {disclosure_id, reader_id, surface, scope, expires,
    identity_assurance}."""
    reader = disclosure["reader"]
    projection = {"disclosure_id": disclosure["disclosure_id"], "reader_id": reader["reader_id"],
                  "surface": reader["surface"], "scope": disclosure["scope"]["op"], "expires": disclosure["expires"],
                  "identity_assurance": reader["identity_assurance"]}
    return {k: projection[k] for k in PROJECTION_KEYS}


def check_manifest_binding(*, manifest_bytes: bytes, ledger_bytes: bytes, content: Mapping[str, Any],
                           disclosure: Mapping[str, Any], checkpoint_bytes: bytes) -> dict[str, Any]:
    """Authenticate every manifest field against the verified statements and the checkpoint bytes.

    Spec rules: the manifest keys are exact; the recomputed digest equals the manifest's, the content
    statement's and the disclosure statement's; generated_at equals built_at; manifest.disclosure is the
    projection; the checkpoint hash, the content checkpoint digest and sha256(irp/checkpoint.json) agree.
    Also checked, so no copy of a signed value can disagree with it: the checkpoint strand and seq, the
    omitted counts, the reader region and the rekadu_version."""
    if not isinstance(manifest_bytes, (bytes, bytearray)) or not manifest_bytes.endswith(b"\n"):
        raise StatementError("manifest.json must be exact JCS plus one newline")
    manifest = sig.load_jcs(bytes(manifest_bytes[:-1]), "manifest.json", error=StatementError)
    if not isinstance(manifest, dict) or set(manifest) != set(MANIFEST_KEYS):
        raise StatementError(f"manifest keys must be exactly {', '.join(MANIFEST_KEYS)}")
    ck, projected = manifest["checkpoint"], manifest["disclosure"]
    if not isinstance(ck, dict):
        raise StatementError("a published Slice's manifest must carry its checkpoint")
    if not isinstance(projected, dict):
        raise StatementError("a published Slice's manifest must carry its disclosure projection")
    if not isinstance(ledger_bytes, (bytes, bytearray)) or not isinstance(checkpoint_bytes, (bytes, bytearray)):
        raise StatementError("ledger.jsonl and irp/checkpoint.json must be bytes")

    digest = digest_for(manifest, bytes(ledger_bytes))
    if not _same(manifest["rekadu_digest"], digest):
        raise StatementError("manifest rekadu_digest isn't the digest recomputed from the manifest and ledger")
    if not _same(content["rekadu_digest"], digest):
        raise StatementError("the content statement's rekadu_digest isn't the recomputed digest")
    if not _same(disclosure["rekadu_digest"], digest):
        raise StatementError("the disclosure statement's rekadu_digest isn't the recomputed digest")
    if not _same(manifest["rekadu_version"], content["rekadu_version"]):
        raise StatementError("manifest rekadu_version must equal the content statement's")
    if not _same(manifest["generated_at"], content["built_at"]):
        raise StatementError("manifest generated_at must equal the content statement's built_at")
    if not _same(projected, disclosure_projection(disclosure)):
        raise StatementError("manifest disclosure isn't the projection of the disclosure statement")

    content_ck = content["checkpoint"]
    if content_ck["digest"] != _sha(bytes(checkpoint_bytes)):
        raise StatementError("the content statement's checkpoint digest doesn't hash irp/checkpoint.json")
    if not _same(ck.get("hash"), content_ck["digest"]):
        raise StatementError("manifest checkpoint hash must equal the content statement's checkpoint digest")
    if not _same([ck.get("strand"), ck.get("seq")], [content_ck["strand"], content_ck["seq"]]):
        raise StatementError("manifest checkpoint strand and seq must match the content statement's checkpoint")
    if not _same(manifest["omitted_counts"], disclosure["omitted"]):
        raise StatementError("the disclosure's omitted counts must equal manifest omitted_counts")
    params = manifest["selection_params"]
    if not isinstance(params, dict) or not _same(params.get("reader_region"), disclosure["reader"]["region"]):
        raise StatementError("manifest reader_region must be the region of the disclosure's surface")
    return manifest


def verify_slice(members: Mapping[str, bytes], keys: Mapping[str, bytes]) -> VerifiedSlice:
    """Verify an unpacked Slice: both statements signed by keys in `keys`, the disclosure citing this content
    statement, every member bound by the signed files map (container.check_binding), and the manifest bound
    by §15.3. Device-log replay (is the signer active?) and reader checks (is it mine, is it in its window,
    does its slot follow the last one?) sit on top of this."""
    if not isinstance(members, Mapping):
        raise StatementError("members must map names to bytes")
    for name in SLICE_STATEMENT_MEMBERS:
        if name not in members:
            raise StatementError(f"{name} is missing")
    content = verify_statement(members["irp/content.json"], members["irp/content.sig"], keys, "content")
    disclosure = verify_statement(members["irp/disclosure.json"], members["irp/disclosure.sig"], keys,
                                  "disclosure")
    if disclosure["content"] != _sha(members["irp/content.json"]):
        raise StatementError("the disclosure statement doesn't cite this content statement (content hash)")
    for field in ("ledger_id", "root", "epoch"):
        if not _same(content[field], disclosure[field]):
            raise StatementError(f"{field} differs between the content and disclosure statements")
    try:
        check_binding("rekadu", members, content["files"])
    except ContainerError as exc:
        raise StatementError(f"files: {exc}") from None
    manifest = check_manifest_binding(
        manifest_bytes=members["manifest.json"], ledger_bytes=members["ledger.jsonl"], content=content,
        disclosure=disclosure, checkpoint_bytes=members["irp/checkpoint.json"])
    return VerifiedSlice(content=content, disclosure=disclosure, manifest=manifest)


def check_slot_follows(previous_bytes: bytes, previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    """Slot n follows slot n-1: same reader, feed and scope, the next seq, and `prev` is the hash of slot
    n-1's disclosure.json bytes (§15.3, §19.3 step 7)."""
    if not _same(parse_statement(previous_bytes, "disclosure"), previous):
        raise StatementError("the previous disclosure doesn't match its bytes")
    if not isinstance(current, Mapping) or current.get("kind") != "disclosure":
        raise StatementError("the current slot must be a disclosure statement")
    check_statement(dict(current))
    p, c = previous["delivery"].get("slot"), current["delivery"].get("slot")
    if p is None or c is None:
        raise StatementError("slot chains apply to reader slots only")
    if previous["reader"]["reader_id"] != current["reader"]["reader_id"]:
        raise StatementError("a slot chain belongs to one reader")
    if (p["feed"], p["scope_id"]) != (c["feed"], c["scope_id"]):
        raise StatementError("a slot chain stays in one feed and scope")
    if c["seq"] != p["seq"] + 1:
        raise StatementError(f"slot {c['seq']} doesn't follow slot {p['seq']}")
    if c["prev"] != _sha(bytes(previous_bytes)):
        raise StatementError("slot prev isn't the hash of the previous slot's disclosure.json")
