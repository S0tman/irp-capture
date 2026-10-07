"""Roaming IRP, Cut 1 step 2.4: content and disclosure statements (spec v0.3 §15.1, §15.3, §15.7).

The content statement says what's in a Slice (which files, which fingerprint,
which checkpoint); the disclosure statement says who it was for (which reader,
which surface, which scope, until when, labelled A0). Both are signed over their
exact JCS bytes, and the binding rules tie the unsigned manifest to them, so a
swapped, edited or replayed Slice fails before anyone reads it.

Fixtures use invented names and 2001 ids only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import sig  # noqa: E402
from irp.roam.age import generate_identity  # noqa: E402
from irp.roam.container import pack, unpack  # noqa: E402
from irp.roam.rekadu import RETURN_TEXT, build_rekadu, digest_for  # noqa: E402
from irp.roam.statements import (  # noqa: E402
    StatementError,
    check_manifest_binding,
    check_slot_follows,
    content_statement,
    disclosure_projection,
    disclosure_statement,
    encode_statement,
    parse_statement,
    sign_statement,
    verify_slice,
    verify_statement,
)


def _sha(b: bytes) -> str:
    return "sha256-" + hashlib.sha256(b).hexdigest()


def _seed(label: str) -> bytes:
    return hashlib.sha256(b"irp-roam test seed " + label.encode()).digest()


def _rng(label: str):
    state = {"n": 0}

    def rng(n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += hashlib.sha256(label.encode() + state["n"].to_bytes(8, "big")).digest()
            state["n"] += 1
        return out[:n]

    return rng


def _recipient(label: str) -> str:
    return generate_identity(_rng(label)).recipient().to_string()


ILID = "ILID-" + "a1" * 16
ROOT_ID = sig.root_id(sig.public_key(_seed("root")))
SEED = _seed("laptop")
PUB = sig.public_key(SEED)
DK = sig.key_id("dk", PUB)
OTHER_SEED = _seed("second laptop")
OTHER_PUB = sig.public_key(OTHER_SEED)
OTHER_DK = sig.key_id("dk", OTHER_PUB)
KEYS = {DK: PUB, OTHER_DK: OTHER_PUB}
READER = "rd-" + "b2" * 16
EPH = "eph-" + "c3" * 16
R_READER = _recipient("reader")
R_DEVICE = _recipient("laptop box")
R_DEVICE2 = _recipient("phone box")
DISCLOSURE_ID = "de-" + sig.b64url_encode(hashlib.sha256(b"disclosure").digest()[:16])
BUILT_AT = "2001-02-03T04:05:06Z"
ISSUED_AT = "2001-02-03T04:05:07Z"
EXPIRES = "2001-02-17T04:05:07Z"  # issued_at + 14 days, the standing maximum
CHECKPOINT_BYTES = canonicalize({"v": 1, "kind": "checkpoint", "seq": 42, "note": "test checkpoint"})
CHECKPOINT_SIG = b'{"alg":"ed25519","key_id":"' + DK.encode() + b'","sig":"test"}'
SCOPE = {"op": "read", "scope_id": "s1", "rule_digest": _sha(b"scope rule s1")}
SLOT = {"feed": "reader", "scope_id": "s1", "seq": 17, "prev": _sha(b"slot 16 disclosure.json")}


def _d(id_, what, ts, **rel):
    return {"type": "decision", "id": id_, "what": what, "why": f"Because of {what.lower()}.",
            "timestamp": ts, **rel}


LEDGER = [
    _d("IRP-2001-01-01-001", "Keep the record local", "2001-01-01T09:00:00Z"),
    _d("IRP-2001-01-02-001", "Pack Slices into strict tar files", "2001-01-02T09:00:00Z",
       rests_on=["IRP-2001-01-01-001"]),
    _d("IRP-2001-01-03-001", "Sign what goes in each Slice", "2001-01-03T09:00:00Z",
       rests_on=["IRP-2001-01-02-001"]),
    _d("IRP-2001-01-04-001", "Unrelated note for Göta Testbolag", "2001-01-04T09:00:00Z"),
]


def _ck_manifest(digest=None, strand=DK, seq=42):
    return {"id": "ckpt-0042", "strand": strand, "seq": seq, "hash": digest or _sha(CHECKPOINT_BYTES),
            "signed_ts": "2001-02-03T04:00:00Z"}


def _projection(**over):
    p = {"disclosure_id": DISCLOSURE_ID, "reader_id": READER, "surface": "claude-code-cloud", "scope": "read",
         "expires": EXPIRES, "identity_assurance": "A0"}
    p.update(over)
    return p


def content_kwargs(rk_digest, files, **over):
    kw = dict(ledger_id=ILID, root=ROOT_ID, epoch=0, rekadu_digest=rk_digest, built_at=BUILT_AT,
              checkpoint={"strand": DK, "seq": 42, "digest": _sha(CHECKPOINT_BYTES)}, files=files,
              signer={"alg": "ed25519", "key_id": DK})
    kw.update(over)
    return kw


def disclosure_kwargs(content_digest, rk_digest, omitted, **over):
    kw = dict(ledger_id=ILID, root=ROOT_ID, epoch=0, disclosure_id=DISCLOSURE_ID, issued_at=ISSUED_AT,
              expires=EXPIRES, content=content_digest, rekadu_digest=rk_digest, reader_id=READER,
              surface="claude-code-cloud", viewing="employer-managed",
              audience=[{"id": READER, "role": "reader", "recipient": R_READER},
                        {"id": DK, "role": "device", "recipient": R_DEVICE}],
              delivery={"slot": dict(SLOT)}, scope=dict(SCOPE),
              omitted=dict(omitted), signer={"alg": "ed25519", "key_id": DK})
    kw.update(over)
    return kw


def make_slice(*, manifest_hook=None, recompute=True, content_over=None, disclosure_over=None, tsr=True,
               ledger=LEDGER, targets=("IRP-2001-01-03-001",), checkpoint_bytes=CHECKPOINT_BYTES, projection=None,
               extra_members=None):
    """Build a complete, validly signed Slice; hooks change one thing at a time."""
    rk = build_rekadu(ledger, targets=list(targets), surface="claude-code-cloud", checkpoint=_ck_manifest(),
                      generated_at=BUILT_AT, disclosure=projection or _projection())
    manifest = copy.deepcopy(rk.manifest)
    if manifest_hook:
        manifest_hook(manifest)
        if recompute:
            manifest["rekadu_digest"] = digest_for(manifest, rk.ledger_bytes)
    members = {
        "IRP-RETURN.txt": RETURN_TEXT.encode(),
        "ledger.jsonl": rk.ledger_bytes,
        "manifest.json": canonicalize(manifest) + b"\n",
        "irp/checkpoint.json": checkpoint_bytes,
        "irp/checkpoint.sig": CHECKPOINT_SIG,
        "irp/devices.jsonl": b'{"device":"test"}\n',
    }
    if tsr:
        members["irp/checkpoint.tsr"] = b"test timestamp token"
    members.update(extra_members or {})
    files = {name: _sha(data) for name, data in members.items() if name != "manifest.json"}
    content = content_statement(**{**content_kwargs(manifest["rekadu_digest"], files), **(content_over or {})})
    c_bytes, c_sig = sign_statement(content, SEED)
    disclosure = disclosure_statement(**{**disclosure_kwargs(_sha(c_bytes), manifest["rekadu_digest"],
                                                             manifest["omitted_counts"]), **(disclosure_over or {})})
    d_bytes, d_sig = sign_statement(disclosure, SEED)
    members.update({"irp/content.json": c_bytes, "irp/content.sig": c_sig,
                    "irp/disclosure.json": d_bytes, "irp/disclosure.sig": d_sig})
    return members


def _content(members):
    return json.loads(members["irp/content.json"])


def _disclosure(members):
    return json.loads(members["irp/disclosure.json"])


# ── Round trips ──

def test_a_signed_slice_verifies_end_to_end():
    members = make_slice()
    v = verify_slice(members, KEYS)
    assert v.content["kind"] == "content" and v.disclosure["kind"] == "disclosure"
    assert v.manifest["rekadu_digest"] == v.content["rekadu_digest"] == v.disclosure["rekadu_digest"]


def test_it_also_verifies_through_the_container():
    members = make_slice()
    v = verify_slice(unpack("rekadu", pack("rekadu", members)), KEYS)
    assert v.disclosure["reader"]["reader_id"] == READER


def test_an_unwitnessed_checkpoint_has_no_tsr():
    members = make_slice(tsr=False)
    assert "irp/checkpoint.tsr" not in _content(members)["files"]
    verify_slice(members, KEYS)


def test_statements_are_exact_jcs_without_a_newline():
    members = make_slice()
    for name in ("irp/content.json", "irp/disclosure.json", "irp/content.sig", "irp/disclosure.sig"):
        data = members[name]
        assert data == canonicalize(json.loads(data)) and not data.endswith(b"\n")


def test_content_round_trip():
    members = make_slice()
    c = parse_statement(members["irp/content.json"], "content")
    assert encode_statement(c) == members["irp/content.json"]
    assert c["v"] == 1 and c["rekadu_version"] == "0.2"


def test_disclosure_round_trip_derives_region_and_assurance():
    d = parse_statement(make_slice()["irp/disclosure.json"], "disclosure")
    assert d["reader"] == {"reader_id": READER, "surface": "claude-code-cloud", "region": "non-eu",
                           "viewing": "employer-managed", "identity_assurance": "A0"}
    assert d["return"] is None
    assert encode_statement(d) == canonicalize(d)


def test_region_and_assurance_cant_be_typed_in():
    kw = disclosure_kwargs(_sha(b"c"), _sha(b"r"), {"exposure_class": 0, "unknown_id": 0, "type_not_in_scope": 0,
                                                     "pending_review": 0})
    with pytest.raises(TypeError):
        disclosure_statement(**kw, region="eu")
    with pytest.raises(TypeError):
        disclosure_statement(**kw, identity_assurance="A1")


def test_projection_matches_what_the_builder_put_in_the_manifest():
    members = make_slice()
    manifest = json.loads(members["manifest.json"])
    assert disclosure_projection(_disclosure(members)) == manifest["disclosure"]


def test_digest_for_matches_the_builder():
    rk = build_rekadu(LEDGER, targets=["IRP-2001-01-03-001"], checkpoint=_ck_manifest(), generated_at=BUILT_AT,
                      disclosure=_projection())
    assert digest_for(rk.manifest, rk.ledger_bytes) == rk.digest


# ── Strict parsing (§15.1 steps 2 to 4) ──

def _reencode(members, name, mutate):
    obj = json.loads(members[name])
    mutate(obj)
    return canonicalize(obj)


def test_wrong_kind_is_rejected():
    members = make_slice()
    with pytest.raises(StatementError):
        parse_statement(members["irp/content.json"], "disclosure")
    with pytest.raises(StatementError):
        parse_statement(members["irp/disclosure.json"], "content")
    with pytest.raises(StatementError):
        parse_statement(members["irp/content.json"], "checkpoint")


@pytest.mark.parametrize("raw", [
    lambda d: json.dumps(json.loads(d)).encode(),                 # spaces
    lambda d: d + b"\n",                                           # trailing newline
    lambda d: b'{"v":1,' + d[1:],                                  # duplicate key
    lambda d: d.replace(b'"epoch":0', b'"epoch":0.0'),             # float
    lambda d: d.replace(b'"epoch":0', b'"epoch":-0'),              # not JCS
    lambda d: b"\xef\xbb\xbf" + d,                                 # BOM
])
def test_content_bytes_must_be_exact_jcs(raw):
    data = make_slice()["irp/content.json"]
    with pytest.raises(StatementError):
        parse_statement(raw(data), "content")


CONTENT_MUTATIONS = {
    "unknown key": lambda c: c.update(extra=1),
    "missing signer": lambda c: c.pop("signer"),
    "missing files": lambda c: c.pop("files"),
    "v 2": lambda c: c.update(v=2),
    "v true": lambda c: c.update(v=True),
    "kind": lambda c: c.update(kind="disclosure"),
    "epoch negative": lambda c: c.update(epoch=-1),
    "epoch bool": lambda c: c.update(epoch=True),
    "ledger id": lambda c: c.update(ledger_id="ILID-" + "A1" * 16),
    "root": lambda c: c.update(root="rt-123"),
    "rekadu version": lambda c: c.update(rekadu_version="0.1"),
    "digest upper": lambda c: c.update(rekadu_digest=c["rekadu_digest"].upper()),
    "built_at space": lambda c: c.update(built_at="2001-02-03 04:05:06Z"),
    "built_at month": lambda c: c.update(built_at="2001-13-03T04:05:06Z"),
    "built_at offset": lambda c: c.update(built_at="2001-02-03T04:05:06+00:00"),
    "checkpoint extra": lambda c: c["checkpoint"].update(id="ckpt-1"),
    "checkpoint seq": lambda c: c["checkpoint"].update(seq=-1),
    "checkpoint strand": lambda c: c["checkpoint"].update(strand="ck-" + "0" * 32),
    "checkpoint digest format": lambda c: c["checkpoint"].update(digest="sha256-1"),
    "files missing ledger": lambda c: c["files"].pop("ledger.jsonl"),
    "files missing devices": lambda c: c["files"].pop("irp/devices.jsonl"),
    "files lists manifest": lambda c: c["files"].update({"manifest.json": _sha(b"m")}),
    "files lists a statement": lambda c: c["files"].update({"irp/content.json": _sha(b"c")}),
    "files unknown name": lambda c: c["files"].update({"notes.txt": _sha(b"n")}),
    "files bad digest": lambda c: c["files"].update({"ledger.jsonl": "sha256-xyz"}),
    "artefact hash mismatch": lambda c: c["files"].update({"artefacts/sha256-" + "1" * 64 + ".txt": _sha(b"a")}),
    "signer passkey": lambda c: c["signer"].update(alg="webauthn-es256"),
    "signer unknown alg": lambda c: c["signer"].update(alg="ed448"),
    "signer reader id": lambda c: c["signer"].update(key_id=READER),
    "signer extra": lambda c: c["signer"].update(label="laptop-1"),
}


@pytest.mark.parametrize("name", sorted(CONTENT_MUTATIONS))
def test_content_closed_schema(name):
    data = _reencode(make_slice(), "irp/content.json", CONTENT_MUTATIONS[name])
    with pytest.raises(StatementError):
        parse_statement(data, "content")


@pytest.mark.parametrize("field", ["epoch", "seq"])
def test_integers_stay_in_the_ijson_range(field):
    files = {n: _sha(b"x") for n in ("IRP-RETURN.txt", "ledger.jsonl", "irp/checkpoint.json", "irp/checkpoint.sig",
                                     "irp/devices.jsonl")}
    kw = content_kwargs(_sha(b"r"), files)
    big = 2**53
    if field == "epoch":
        kw["epoch"] = big
    else:
        kw["checkpoint"] = dict(kw["checkpoint"], seq=big)
    with pytest.raises(StatementError, match="2\\^53"):
        content_statement(**kw)
    kw["epoch"], kw["checkpoint"] = 2**53 - 1, dict(kw["checkpoint"], seq=2**53 - 1)
    content_statement(**kw)


IMPOSSIBLE_TIMESTAMPS = ["2001-02-03T24:00:00Z", "2001-02-03T04:05:60Z", "2001-02-03T04:60:00Z", "2001-02-30T00:00:00Z",
                         "2001-02-29T00:00:00Z", "2001-00-10T00:00:00Z", "0000-01-01T00:00:00Z"]


@pytest.mark.parametrize("ts", IMPOSSIBLE_TIMESTAMPS)
def test_timestamps_must_be_real_utc_times(ts):
    members = make_slice()
    with pytest.raises(StatementError, match="real UTC time"):
        parse_statement(_reencode(members, "irp/content.json", lambda c: c.update(built_at=ts)), "content")
    with pytest.raises(StatementError, match="real UTC time"):
        parse_statement(_reencode(members, "irp/disclosure.json", lambda d: d.update(issued_at=ts)), "disclosure")
    with pytest.raises(StatementError, match="real UTC time"):
        parse_statement(_reencode(members, "irp/disclosure.json", lambda d: d.update(expires=ts)), "disclosure")


def test_content_may_list_hash_named_artefacts():
    members = make_slice()
    art = b"an artefact"
    name = "artefacts/" + _sha(art) + ".txt"
    data = _reencode(members, "irp/content.json", lambda c: c["files"].update({name: _sha(art)}))
    assert name in parse_statement(data, "content")["files"]


DISCLOSURE_MUTATIONS = {
    "unknown key": lambda d: d.update(extra=1),
    "missing return": lambda d: d.pop("return"),
    "v 2": lambda d: d.update(v=2),
    "region typed eu": lambda d: d["reader"].update(region="eu"),
    "region free text": lambda d: d["reader"].update(region="Sweden"),
    "surface reserved managed": lambda d: d["reader"].update(surface="browser-managed"),
    "surface reserved eu": lambda d: d["reader"].update(surface="eu-sovereign-runtime", region="eu"),
    "surface unknown": lambda d: d["reader"].update(surface="terminal"),
    "assurance A1": lambda d: d["reader"].update(identity_assurance="A1"),
    "viewing unknown": lambda d: d["reader"].update(viewing="public"),
    "reader extra": lambda d: d["reader"].update(label="x"),
    "reader id format": lambda d: d["reader"].update(reader_id="rd-1"),
    "scope propose": lambda d: d["scope"].update(op="propose"),
    "scope extra": lambda d: d["scope"].update(limit=3),
    "scope op write": lambda d: d["scope"].update(op="write"),
    "scope_id format": lambda d: (d["scope"].update(scope_id="S1"), d["delivery"]["slot"].update(scope_id="S1")),
    "rekadu digest format": lambda d: d.update(rekadu_digest="sha256-1"),
    "scope rule digest": lambda d: d["scope"].update(rule_digest="sha256-"),
    "return not null": lambda d: d.update({"return": {"endpoint": "https://x"}}),
    "return empty": lambda d: d.update({"return": {}}),
    "disclosure id short": lambda d: d.update(disclosure_id="de-AAAA"),
    "disclosure id padded": lambda d: d.update(disclosure_id="de-" + "A" * 22 + "=="),
    "disclosure id prefix": lambda d: d.update(disclosure_id="dx-" + "A" * 22),
    "disclosure id tail bits": lambda d: d.update(disclosure_id="de-" + "A" * 21 + "B"),
    "expires before issued": lambda d: d.update(expires="2001-02-03T04:05:06Z"),
    "expires equals issued": lambda d: d.update(expires=d["issued_at"]),
    "window over 14 days": lambda d: d.update(expires="2001-02-17T04:05:08Z"),
    "content digest": lambda d: d.update(content="sha256-" + "G" * 64),
    "audience empty": lambda d: d.update(audience=[]),
    "audience reader not first": lambda d: d.update(audience=d["audience"][::-1]),
    "audience reader id": lambda d: d["audience"][0].update(id="rd-" + "0" * 32),
    "audience two readers": lambda d: d["audience"].append({"id": OTHER_DK, "role": "reader", "recipient": R_DEVICE2}),
    "audience no device": lambda d: d.update(audience=d["audience"][:1]),
    "audience duplicate device": lambda d: d["audience"].append(dict(d["audience"][1])),
    "audience duplicate recipient": lambda d: d["audience"].append(
        {"id": OTHER_DK, "role": "device", "recipient": R_DEVICE}),
    "audience unsorted devices": lambda d: d.update(audience=[d["audience"][0]] + sorted(
        [d["audience"][1], {"id": OTHER_DK, "role": "device", "recipient": R_DEVICE2}],
        key=lambda e: e["id"], reverse=True)),
    "audience unknown role": lambda d: d["audience"][1].update(role="approver"),
    "audience bad recipient": lambda d: d["audience"][1].update(recipient=R_DEVICE.upper()),
    "audience garbage recipient": lambda d: d["audience"][1].update(recipient="age1qqqq"),
    "audience extra key": lambda d: d["audience"][1].update(label="laptop-1"),
    "audience device id": lambda d: d["audience"][1].update(id="ak-" + "0" * 32),
    "delivery both": lambda d: d["delivery"].update(mailbox={"sid_sha256": "0" * 64}),
    "delivery neither": lambda d: d.update(delivery={}),
    "slot feed": lambda d: d["delivery"]["slot"].update(feed="phone"),
    "slot scope mismatch": lambda d: d["delivery"]["slot"].update(scope_id="s2"),
    "slot seq zero": lambda d: d["delivery"]["slot"].update(seq=0, prev=_sha(b"slot -1")),
    "slot seq bool": lambda d: d["delivery"]["slot"].update(seq=True, prev=None),
    "slot first with prev": lambda d: d["delivery"]["slot"].update(seq=1),
    "slot later without prev": lambda d: d["delivery"]["slot"].update(prev=None),
    "slot prev digest": lambda d: d["delivery"]["slot"].update(prev="sha256-1"),
    "slot extra": lambda d: d["delivery"]["slot"].update(name="x"),
    "omitted missing": lambda d: d["omitted"].pop("pending_review"),
    "omitted negative": lambda d: d["omitted"].update(unknown_id=-1),
    "omitted bool": lambda d: d["omitted"].update(unknown_id=False),
    "omitted extra": lambda d: d["omitted"].update(other=0),
    "signer passkey on a slot": lambda d: d["signer"].update(alg="webauthn-es256"),
    "issued_at format": lambda d: d.update(issued_at="2001-02-03T04:05:07.000Z"),
}


@pytest.mark.parametrize("name", sorted(DISCLOSURE_MUTATIONS))
def test_disclosure_closed_schema(name):
    data = _reencode(make_slice(), "irp/disclosure.json", DISCLOSURE_MUTATIONS[name])
    with pytest.raises(StatementError):
        parse_statement(data, "disclosure")


def test_scope_propose_is_reserved():
    data = _reencode(make_slice(), "irp/disclosure.json", lambda d: d["scope"].update(op="propose"))
    with pytest.raises(StatementError, match="reserved"):
        parse_statement(data, "disclosure")


def test_return_must_be_null_in_v1():
    data = _reencode(make_slice(), "irp/disclosure.json", lambda d: d.update({"return": {}}))
    with pytest.raises(StatementError, match="return"):
        parse_statement(data, "disclosure")


def test_region_must_follow_the_surface():
    data = _reencode(make_slice(), "irp/disclosure.json", lambda d: d["reader"].update(region="eu"))
    with pytest.raises(StatementError, match="region"):
        parse_statement(data, "disclosure")


def test_more_devices_in_sorted_order_are_fine():
    def add(d):
        d["audience"] = [d["audience"][0]] + sorted(
            [d["audience"][1], {"id": OTHER_DK, "role": "device", "recipient": R_DEVICE2}], key=lambda e: e["id"])
    parse_statement(_reencode(make_slice(), "irp/disclosure.json", add), "disclosure")


def test_exactly_fourteen_days_is_allowed():
    d = parse_statement(make_slice()["irp/disclosure.json"], "disclosure")
    assert (d["issued_at"], d["expires"]) == (ISSUED_AT, EXPIRES)


def test_the_first_slot_has_no_prev():
    def first(d):
        d["delivery"]["slot"].update(seq=1, prev=None)
    parse_statement(_reencode(make_slice(), "irp/disclosure.json", first), "disclosure")


# ── The browser path (§15.3): mailbox delivery, ephemeral reader, phone signer ──

def _browser(d):
    d["reader"] = {"reader_id": EPH, "surface": "browser-ephemeral", "region": "non-eu",
                   "viewing": "personal-device", "identity_assurance": "A0"}
    d["audience"] = [{"id": EPH, "role": "reader", "recipient": R_READER}]
    d["delivery"] = {"mailbox": {"sid_sha256": hashlib.sha256(b"sid").hexdigest()}}
    d["expires"] = "2001-02-03T06:05:07Z"  # issued_at + 2 hours, the browser maximum
    d["signer"] = {"alg": "webauthn-es256", "key_id": OTHER_DK}


def test_browser_disclosure_shape_parses():
    d = parse_statement(_reencode(make_slice(), "irp/disclosure.json", _browser), "disclosure")
    assert d["delivery"] == {"mailbox": {"sid_sha256": hashlib.sha256(b"sid").hexdigest()}}


BROWSER_MUTATIONS = {
    "over two hours": lambda d: d.update(expires="2001-02-03T06:05:08Z"),
    "rd reader": lambda d: (d["reader"].update(reader_id=READER), d["audience"][0].update(id=READER)),
    "devices in audience": lambda d: d["audience"].append({"id": DK, "role": "device", "recipient": R_DEVICE}),
    "ed25519 signer": lambda d: d["signer"].update(alg="ed25519"),
    "slot delivery": lambda d: d.update(delivery={"slot": dict(SLOT)}),
    "cloud surface": lambda d: d["reader"].update(surface="claude-code-cloud"),
    "sid not hex": lambda d: d["delivery"]["mailbox"].update(sid_sha256="xyz"),
    "mailbox extra": lambda d: d["delivery"]["mailbox"].update(ttl=3),
}


@pytest.mark.parametrize("name", sorted(BROWSER_MUTATIONS))
def test_browser_shape_is_closed(name):
    def mutate(d):
        _browser(d)
        BROWSER_MUTATIONS[name](d)
    with pytest.raises(StatementError):
        parse_statement(_reencode(make_slice(), "irp/disclosure.json", mutate), "disclosure")


def test_slot_delivery_on_a_browser_surface_is_rejected():
    data = _reencode(make_slice(), "irp/disclosure.json",
                     lambda d: d["reader"].update(surface="browser-ephemeral", viewing="personal-device"))
    with pytest.raises(StatementError):
        parse_statement(data, "disclosure")


# ── Signing and verifying (§15.1 step 1 first, §15.2) ──

def test_verify_statement_round_trip():
    members = make_slice()
    c = verify_statement(members["irp/content.json"], members["irp/content.sig"], KEYS, "content")
    assert c["signer"] == {"alg": "ed25519", "key_id": DK}


def test_the_signature_is_checked_over_the_shipped_bytes_first():
    members = make_slice()
    # A valid statement with one changed field: the schema would pass, so only the signature can catch it.
    edited = _reencode(members, "irp/content.json", lambda c: c.update(epoch=1))
    parse_statement(edited, "content")
    with pytest.raises(StatementError, match="signature"):
        verify_statement(edited, members["irp/content.sig"], KEYS, "content")
    # Re-serialised (not JCS) bytes fail at the signature, before any parsing.
    spaced = json.dumps(json.loads(members["irp/content.json"])).encode()
    with pytest.raises(StatementError, match="signature"):
        verify_statement(spaced, members["irp/content.sig"], KEYS, "content")


def test_statement_kind_confusion_fails():
    members = make_slice()
    d_bytes = members["irp/disclosure.json"]
    as_content = sig.encode_sig(sig.sign("content", d_bytes, SEED, DK))
    with pytest.raises(StatementError):
        verify_statement(d_bytes, as_content, KEYS, "disclosure")
    with pytest.raises(StatementError):
        verify_statement(d_bytes, members["irp/disclosure.sig"], KEYS, "content")


def test_the_named_signer_must_be_the_signing_key():
    members = make_slice()
    c = _content(members)
    c["signer"]["key_id"] = OTHER_DK
    data = canonicalize(c)
    sneaky = sig.encode_sig(sig.sign("content", data, SEED, DK))  # validly signed by DK, but names OTHER_DK
    with pytest.raises(StatementError, match="signer"):
        verify_statement(data, sneaky, KEYS, "content")


def test_an_unknown_key_fails():
    members = make_slice()
    with pytest.raises(StatementError, match="key"):
        verify_statement(members["irp/content.json"], members["irp/content.sig"], {OTHER_DK: OTHER_PUB}, "content")


def test_a_key_under_the_wrong_id_fails():
    members = make_slice()
    with pytest.raises(StatementError):
        verify_statement(members["irp/content.json"], members["irp/content.sig"], {DK: OTHER_PUB}, "content")


def test_sign_statement_refuses_a_seed_that_isnt_the_signer():
    members = make_slice()
    with pytest.raises(StatementError):
        sign_statement(_content(members), OTHER_SEED)


def test_sign_statement_refuses_an_invalid_statement():
    c = _content(make_slice())
    c["epoch"] = -1
    with pytest.raises(StatementError):
        sign_statement(c, SEED)


def test_passkey_signed_disclosures_wait_for_the_approver_module():
    members = make_slice()
    data = _reencode(members, "irp/disclosure.json", _browser)
    fake = canonicalize({"alg": "webauthn-es256", "key_id": OTHER_DK, "sig": sig.b64url_encode(b"\x01" * 64)})
    with pytest.raises(StatementError, match="approver"):
        verify_statement(data, fake, KEYS, "disclosure")


# ── Binding rules (§15.3), each broken in turn ──

def _breaks(members, match):
    with pytest.raises(StatementError, match=match):
        verify_slice(members, KEYS)


def test_binding_manifest_keys_are_exact():
    _breaks(make_slice(manifest_hook=lambda m: m.update(extra=[])), "manifest keys")
    _breaks(make_slice(manifest_hook=lambda m: m.pop("artefacts")), "manifest keys")


def test_binding_manifest_digest_must_be_the_recomputed_digest():
    def wrong(m):
        m["rekadu_digest"] = _sha(b"something else")
    members = make_slice(manifest_hook=wrong, recompute=False)
    _breaks(members, "manifest rekadu_digest")


def test_binding_content_digest_must_be_the_recomputed_digest():
    _breaks(make_slice(content_over={"rekadu_digest": _sha(b"other slice")}), "content statement's rekadu_digest")


def test_binding_disclosure_digest_must_be_the_recomputed_digest():
    _breaks(make_slice(disclosure_over={"rekadu_digest": _sha(b"other slice")}), "disclosure statement's rekadu_digest")


def test_binding_generated_at_must_be_built_at():
    _breaks(make_slice(content_over={"built_at": "2001-02-03T04:05:05Z"}), "generated_at")


@pytest.mark.parametrize("field,value", [
    ("disclosure_id", "de-" + "B" * 21 + "A"), ("reader_id", "rd-" + "0" * 32), ("expires", "2001-02-10T04:05:07Z"),
    ("identity_assurance", "A1"), ("scope", "propose"), ("surface", "browser-ephemeral"),
])
def test_binding_manifest_disclosure_must_be_the_projection(field, value):
    _breaks(_swap_manifest_disclosure(field, value), "projection")


def _swap_manifest_disclosure(field, value):
    # manifest.disclosure is volatile (outside the digest), so only the projection rule can catch an edit.
    members = make_slice()
    m = json.loads(members["manifest.json"])
    m["disclosure"][field] = value
    members["manifest.json"] = canonicalize(m) + b"\n"
    return members


def test_binding_manifest_disclosure_with_an_extra_key_fails():
    members = make_slice()
    m = json.loads(members["manifest.json"])
    m["disclosure"]["viewing"] = "employer-managed"
    members["manifest.json"] = canonicalize(m) + b"\n"
    _breaks(members, "projection")


def test_binding_manifest_checkpoint_hash_must_be_the_content_checkpoint():
    def other(m):
        m["checkpoint"]["hash"] = _sha(b"another checkpoint")
    _breaks(make_slice(manifest_hook=other), "manifest checkpoint hash")


def test_binding_content_checkpoint_must_hash_checkpoint_json():
    other = _sha(b"another checkpoint")

    def hook(m):
        m["checkpoint"]["hash"] = other
    members = make_slice(manifest_hook=hook,
                         content_over={"checkpoint": {"strand": DK, "seq": 42, "digest": other}})
    _breaks(members, "checkpoint.json")


def test_binding_checkpoint_strand_and_seq_must_agree():
    _breaks(make_slice(content_over={"checkpoint": {"strand": DK, "seq": 41, "digest": _sha(CHECKPOINT_BYTES)}}),
            "strand and seq")
    _breaks(make_slice(content_over={"checkpoint": {"strand": OTHER_DK, "seq": 42,
                                                    "digest": _sha(CHECKPOINT_BYTES)}}), "strand and seq")


def test_binding_omitted_counts_must_agree():
    omitted = {"exposure_class": 1, "unknown_id": 0, "type_not_in_scope": 0, "pending_review": 0}
    _breaks(make_slice(disclosure_over={"omitted": omitted}), "omitted")


def test_binding_reader_region_must_follow_the_surface():
    def eu(m):
        m["selection_params"]["reader_region"] = "eu"
    _breaks(make_slice(manifest_hook=eu), "reader_region")


@pytest.mark.parametrize("field,value", [("ledger_id", "ILID-" + "b2" * 16), ("root", "rt-" + "0" * 32),
                                         ("epoch", 1)])
def test_binding_content_and_disclosure_name_the_same_ledger(field, value):
    _breaks(make_slice(disclosure_over={field: value}), field)


def test_binding_disclosure_must_cite_this_content_statement():
    # A validly signed disclosure from another Slice, swapped in.
    other = make_slice(targets=("IRP-2001-01-01-001",))
    members = make_slice()
    members["irp/disclosure.json"] = other["irp/disclosure.json"]
    members["irp/disclosure.sig"] = other["irp/disclosure.sig"]
    _breaks(members, "cite")


def test_binding_manifest_must_be_exact_jcs_with_one_newline():
    members = make_slice()
    m = json.loads(members["manifest.json"])
    for bad in (canonicalize(m), canonicalize(m) + b"\n\n", json.dumps(m).encode() + b"\n"):
        members["manifest.json"] = bad
        _breaks(members, "manifest.json")


@pytest.mark.parametrize("tail", [b" ", b"}", b"\r"])
def test_binding_manifest_needs_its_one_newline(tail):
    members = make_slice()
    members["manifest.json"] = members["manifest.json"][:-1] + tail
    _breaks(members, "exact JCS plus one newline")


def test_binding_rekadu_version_must_agree():
    _breaks(make_slice(manifest_hook=lambda m: m.update(rekadu_version="0.3")), "rekadu_version")


def test_binding_compares_types_not_just_python_equality():
    # true == 1 in Python, not in JSON: a copy of a signed value must be the same JSON value.
    def seq_true(m):
        m["checkpoint"]["seq"] = True
    _breaks(make_slice(manifest_hook=seq_true,
                       content_over={"checkpoint": {"strand": DK, "seq": 1, "digest": _sha(CHECKPOINT_BYTES)}}),
            "strand and seq")

    def counts_false(m):
        m["omitted_counts"] = {k: False for k in m["omitted_counts"]}
    zero = {"exposure_class": 0, "unknown_id": 0, "type_not_in_scope": 0, "pending_review": 0}
    _breaks(make_slice(manifest_hook=counts_false, disclosure_over={"omitted": zero}), "omitted")


def test_binding_a_slice_without_checkpoint_or_disclosure_fails():
    for key in ("checkpoint", "disclosure"):
        members = make_slice()
        m = json.loads(members["manifest.json"])
        m[key] = None
        members["manifest.json"] = canonicalize(m) + b"\n"
        _breaks(members, key)


def test_check_manifest_binding_directly():
    members = make_slice()
    manifest = check_manifest_binding(
        manifest_bytes=members["manifest.json"], ledger_bytes=members["ledger.jsonl"],
        content=_content(members), disclosure=_disclosure(members),
        checkpoint_bytes=members["irp/checkpoint.json"])
    assert manifest["rekadu_version"] == "0.2"


# ── Files and members ──

def test_a_tampered_ledger_fails():
    members = make_slice()
    members["ledger.jsonl"] = members["ledger.jsonl"].replace(b"Keep", b"Drop")
    _breaks(members, "ledger.jsonl")


def test_a_listed_artefact_verifies():
    art = b"an artefact the custodian signed"
    members = make_slice(extra_members={"artefacts/" + _sha(art) + ".txt": art})
    verify_slice(unpack("rekadu", pack("rekadu", members)), KEYS)


def test_an_unsigned_artefact_fails_even_with_a_matching_name():
    payload = b"text no signature covers"
    members = make_slice()
    members["artefacts/" + _sha(payload) + ".txt"] = payload
    _breaks(members, "not listed")
    with pytest.raises(StatementError, match="not listed"):
        verify_slice(unpack("rekadu", pack("rekadu", members)), KEYS)
    members = make_slice()
    members["artefacts/sha256-" + "0" * 64 + ".md"] = payload  # name and bytes disagree, dict path
    _breaks(members, "not listed")


def test_an_unlisted_member_fails():
    members = make_slice(tsr=False)
    members["irp/checkpoint.tsr"] = b"slipped in"
    _breaks(members, "not listed")


def test_a_missing_listed_file_fails():
    members = make_slice()
    del members["irp/checkpoint.tsr"]
    _breaks(members, "missing")


@pytest.mark.parametrize("name", ["irp/content.json", "irp/content.sig", "irp/disclosure.json",
                                  "irp/disclosure.sig", "manifest.json"])
def test_missing_statement_members_fail(name):
    members = make_slice()
    del members[name]
    with pytest.raises(StatementError):
        verify_slice(members, KEYS)


def test_a_slice_signed_by_an_unlisted_key_fails():
    with pytest.raises(StatementError):
        verify_slice(make_slice(), {OTHER_DK: OTHER_PUB})


# ── Slot chain (§19.3 step 7: the delivery slot's scope, seq and prev match) ──

def _slot_disclosure(seq, prev, scope_id="s1"):
    members = make_slice(disclosure_over={"delivery": {"slot": {"feed": "reader", "scope_id": scope_id, "seq": seq,
                                                                "prev": prev}},
                                          "scope": dict(SCOPE, scope_id=scope_id)})
    return members["irp/disclosure.json"]


def test_slot_chain_follows():
    first = _slot_disclosure(1, None)
    second = _slot_disclosure(2, _sha(first))
    check_slot_follows(first, parse_statement(first, "disclosure"), parse_statement(second, "disclosure"))


def test_slot_chain_rejects_a_gap_a_wrong_prev_and_a_scope_change():
    first = _slot_disclosure(1, None)
    p = parse_statement(first, "disclosure")
    for bad in (_slot_disclosure(3, _sha(first)), _slot_disclosure(2, _sha(first + b" ")),
                _slot_disclosure(2, _sha(first), scope_id="s2")):
        with pytest.raises(StatementError):
            check_slot_follows(first, p, parse_statement(bad, "disclosure"))


def test_slot_chain_belongs_to_one_reader():
    first = _slot_disclosure(1, None)
    other = make_slice(disclosure_over={"reader_id": "rd-" + "d4" * 16, "audience": [
        {"id": "rd-" + "d4" * 16, "role": "reader", "recipient": R_READER},
        {"id": DK, "role": "device", "recipient": R_DEVICE}],
        "delivery": {"slot": {"feed": "reader", "scope_id": "s1", "seq": 2, "prev": _sha(first)}}})
    with pytest.raises(StatementError, match="one reader"):
        check_slot_follows(first, parse_statement(first, "disclosure"),
                           parse_statement(other["irp/disclosure.json"], "disclosure"))


def test_slot_chain_previous_must_match_its_bytes():
    first = _slot_disclosure(1, None)
    second = parse_statement(_slot_disclosure(2, _sha(first)), "disclosure")
    edited = dict(parse_statement(first, "disclosure"), epoch=5)
    with pytest.raises(StatementError, match="match its bytes"):
        check_slot_follows(first, edited, second)


def test_slot_chain_rejects_a_malformed_current_and_mailboxes():
    first = _slot_disclosure(1, None)
    p = parse_statement(first, "disclosure")
    second = parse_statement(_slot_disclosure(2, _sha(first)), "disclosure")
    with pytest.raises(StatementError):
        check_slot_follows(first, p, dict(second, **{"return": {}}))
    for junk in (None, "x", 5, {"kind": "content"}):
        with pytest.raises(StatementError):
            check_slot_follows(first, p, junk)
    browser = parse_statement(_reencode(make_slice(), "irp/disclosure.json", _browser), "disclosure")
    with pytest.raises(StatementError, match="reader slots"):
        check_slot_follows(first, p, browser)
    browser_bytes = _reencode(make_slice(), "irp/disclosure.json", _browser)
    with pytest.raises(StatementError, match="reader slots"):
        check_slot_follows(browser_bytes, parse_statement(browser_bytes, "disclosure"), second)


# ── Every check fails closed with a StatementError, never a crash ──

@pytest.mark.parametrize("junk", [b"", b"null", b"[]", b"{}", b'{"v":1}', b"\x00" * 10, b'{"kind":"content"}'])
def test_junk_never_crashes(junk):
    with pytest.raises(StatementError):
        parse_statement(junk, "content")
    with pytest.raises(StatementError):
        verify_statement(junk, junk, KEYS, "content")
