"""Roaming IRP, Cut 1 step 2.5b: hardware-key and passkey assertions (spec v0.3 §14.5a, §15.2).

An approver (a FIDO2 hardware key) and a companion (a phone passkey) sign a log line or a mailbox disclosure
with an ES256 assertion: 37 bytes of authenticator data, the client data JSON and a raw low-S signature,
packed as one JCS object. Real hardware keys come with gate 0.5; until then a software authenticator
(tests/roam_logkit.py) and fixed vectors from an independent generator stand in.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature  # noqa: E402

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import sig  # noqa: E402
from irp.roam.approver import (  # noqa: E402
    APPROVER_ORIGIN,
    APPROVER_RP_ID,
    ApproverError,
    Assertion,
    approve,
    check_spki,
    client_data_json,
    pack,
    unpack,
    verify,
    verify_assertion,
)
from roam_logkit import N, PHONE_ORIGIN, PHONE_RP, SoftKey, approver_key, phone_key  # noqa: E402

VECTORS = json.loads((ROOT / "tests" / "fixtures" / "roam" / "assertion_vectors.json").read_text())["vectors"]
KIND, DATA = "devices-entry", b'{"event":"vector"}'


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── Constants and client data ──

def test_the_approver_rp_id_never_resolves():
    assert APPROVER_RP_ID == "approver.irp-roam.invalid"
    assert APPROVER_ORIGIN == "https://approver.irp-roam.invalid"


def test_client_data_json_is_the_exact_cli_bytes():
    si = sig.signing_input(KIND, DATA)
    expected = ('{"type":"webauthn.get","challenge":"%s","origin":"%s","crossOrigin":false}'
                % (sig.b64url_encode(si), APPROVER_ORIGIN)).encode()
    assert client_data_json(si, APPROVER_ORIGIN) == expected
    assert len(sig.b64url_encode(si)) == 43


@pytest.mark.parametrize("bad", [b"", b"\x00" * 31, "x" * 32, None])
def test_client_data_needs_a_32_byte_signing_input(bad):
    with pytest.raises(ApproverError):
        client_data_json(bad, APPROVER_ORIGIN)


# ── The fixed vectors ──

def _descriptor(v: dict) -> dict:
    spki = _b64d(v["spki"])
    prefix = "ak" if v["alg"] == "fido2-es256" else "dk"
    try:
        kid = sig.key_id(prefix, spki, v["alg"])
    except sig.SigError:
        kid = prefix + "-" + "0" * 32
    return {"kid": kid, "alg": v["alg"], "pub": v["spki"],
            "webauthn": {"rp_id": v["rp_id"], "origin": v["origin"], "be": v["be"], "bs": v["be"],
                         "cred_id": sig.b64url_encode(b"c" * 16)}}


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_assertion_vectors(v):
    d = _descriptor(v)
    sig_obj = {"alg": v["alg"], "key_id": d["kid"], "sig": v["sig"]}
    data = _b64d(v["data"])
    if v["valid"]:
        a = verify(v["kind"], data, sig_obj, d)
        assert isinstance(a, Assertion)
    else:
        with pytest.raises(ApproverError):
            verify(v["kind"], data, sig_obj, d)


def test_the_vectors_cover_the_spec_list():
    names = {v["name"] for v in VECTORS}
    for must in ("high s", "r is zero", "s equals n", "compressed SPKI", "extension data appended",
                 "reserved bit 0x02 set", "approver with BE set", "client data with a BOM",
                 "duplicate key in client data", "crossOrigin 0", "topOrigin present", "topOrigin null",
                 "explicit-curve SPKI", "38-byte authenticator data, ED clear", "client data nested 1500 deep"):
        assert must in names
    assert sum(v["valid"] for v in VECTORS) >= 5 and sum(not v["valid"] for v in VECTORS) >= 40


def test_approve_reproduces_the_independent_vector_byte_for_byte():
    # approve() + pack() over the same key and input give exactly the bytes the separate generator wrote.
    key = SoftKey("vectors/approver", alg="fido2-es256", rp_id=APPROVER_RP_ID, origin=APPROVER_ORIGIN)
    out = approve(KIND, DATA, key.descriptor("hwkey-1"), key)
    assert out == {"alg": "fido2-es256", "key_id": key.kid, "sig": VECTORS[0]["sig"]}
    phone = SoftKey("vectors/phone", alg="webauthn-es256", rp_id=PHONE_RP, origin=PHONE_ORIGIN, be=True, bs=True)
    assert approve(KIND, DATA, phone.descriptor("phone-1", None), phone)["sig"] == VECTORS[1]["sig"]


# ── Round trips through the software authenticator ──

def test_approver_round_trip_reports_the_flags():
    key = approver_key("rt")
    d = key.descriptor("hwkey-1")
    a = verify(KIND, DATA, approve(KIND, DATA, d, key), d)
    assert a.flags == 0x05 and not a.be and not a.bs
    assert len(a.authenticator_data) == 37 and len(a.signature) == 64


def test_phone_round_trip_reports_be_and_bs():
    key = phone_key("rt", be=True, bs=True)
    d = key.descriptor("phone-1", None)
    a = verify(KIND, DATA, approve(KIND, DATA, d, key), d)
    assert a.flags == 0x1D and a.be and a.bs


def test_approve_asks_the_authenticator_for_the_right_credential():
    key = approver_key("calls")
    d = key.descriptor("hwkey-1")
    approve(KIND, DATA, d, key)
    rp_id, cdh, cred = key.calls[-1]
    assert rp_id == APPROVER_RP_ID and cred == key.cred_id
    assert cdh == hashlib.sha256(client_data_json(sig.signing_input(KIND, DATA), APPROVER_ORIGIN)).digest()


def test_a_signature_is_bound_to_its_kind():
    key = approver_key("kind")
    d = key.descriptor("hwkey-1")
    s = approve(KIND, DATA, d, key)
    with pytest.raises(ApproverError):
        verify("readers-entry", DATA, s, d)
    with pytest.raises(ApproverError):
        verify(KIND, DATA + b" ", s, d)


def test_bs_isnt_compared_after_enrolment():
    key = phone_key("bs", be=True, bs=True)
    d = key.descriptor("phone-1", None)
    d["webauthn"]["bs"] = False  # enrolled before the passkey synced
    assert verify(KIND, DATA, approve(KIND, DATA, d, key), d).bs


def test_signer_and_descriptor_must_agree():
    key = approver_key("agree")
    d = key.descriptor("hwkey-1")
    s = approve(KIND, DATA, d, key)
    other = approver_key("other").descriptor("hwkey-2")
    with pytest.raises(ApproverError, match="key id"):
        verify(KIND, DATA, s, other)
    with pytest.raises(ApproverError, match="alg"):
        verify(KIND, DATA, {**s, "alg": "webauthn-es256"}, d)
    with pytest.raises(ApproverError):
        verify(KIND, DATA, {**s, "alg": "ed25519"}, d)
    wrong_kid = {**d, "kid": "ak-" + "0" * 32}
    with pytest.raises(ApproverError, match="key id"):
        verify(KIND, DATA, {**s, "key_id": wrong_kid["kid"]}, wrong_kid)


def test_an_approver_descriptor_must_use_the_fixed_rp_id_and_origin():
    key = SoftKey("rogue", alg="fido2-es256", rp_id=PHONE_RP, origin=PHONE_ORIGIN)
    d = key.descriptor("hwkey-1")
    with pytest.raises(ApproverError, match="approver"):
        verify(KIND, DATA, approve(KIND, DATA, d, key), d)


def test_pack_stores_low_s():
    key = approver_key("lows")
    si = sig.signing_input(KIND, DATA)
    cdj = client_data_json(si, APPROVER_ORIGIN)
    ad, der = key.get_assertion(APPROVER_RP_ID, hashlib.sha256(cdj).digest(), key.cred_id)
    r, s = decode_dss_signature(der)
    high = encode_dss_signature(r, N - s if s <= N // 2 else s)
    low = encode_dss_signature(r, s if s <= N // 2 else N - s)
    assert pack(ad, cdj, high) == pack(ad, cdj, low)
    a = unpack(pack(ad, cdj, high))
    assert int.from_bytes(a.signature[32:], "big") <= N // 2
    verify_assertion(si, pack(ad, cdj, high), spki=key.spki, rp_id=APPROVER_RP_ID, origin=APPROVER_ORIGIN, be=False)


def test_pack_refuses_what_verify_would_refuse():
    key = approver_key("refuse")
    si = sig.signing_input(KIND, DATA)
    cdj = client_data_json(si, APPROVER_ORIGIN)
    ad, der = key.get_assertion(APPROVER_RP_ID, hashlib.sha256(cdj).digest(), key.cred_id)
    for bad_ad in (ad + b"\x00", ad[:36]):
        with pytest.raises(ApproverError):
            pack(bad_ad, cdj, der)
    with pytest.raises(ApproverError):
        pack(ad, cdj, b"not der")


def test_unpack_is_strict():
    key = approver_key("unpack")
    s = approve(KIND, DATA, key.descriptor("hwkey-1"), key)["sig"]
    inner = json.loads(_b64d(s))
    for bad in (s + "=", s[:-1], "", 7, sig.b64url_encode(b"[]"),
                sig.b64url_encode(canonicalize({**inner, "authenticator_data": inner["authenticator_data"] + "A"}))):
        with pytest.raises(ApproverError):
            unpack(bad)


class _Garbage:
    def __init__(self, result):
        self.result = result

    def get_assertion(self, rp_id, client_data_hash, cred_id):
        return self.result


@pytest.mark.parametrize("result", [(b"x" * 37, b"not der"), (b"short", b""), None, (b"x" * 37,)])
def test_approve_fails_closed_on_a_bad_authenticator(result):
    d = approver_key("garbage").descriptor("hwkey-1")
    with pytest.raises(ApproverError):
        approve(KIND, DATA, d, _Garbage(result))


def test_approve_checks_its_own_output():
    # An authenticator that signs with another key: approve() verifies before returning, so nothing bad ships.
    real, other = approver_key("real"), approver_key("imposter")
    d = real.descriptor("hwkey-1")
    with pytest.raises(ApproverError):
        approve(KIND, DATA, d, other)


# ── SPKI ──

def test_spki_is_the_fixed_91_byte_form():
    key = approver_key("spki")
    assert check_spki(key.spki) == key.spki and len(key.spki) == 91
    pub = ec.derive_private_key(7, ec.SECP256R1()).public_key()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    compressed = pub.public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
    for bad in (key.spki[:-1], key.spki + b"\x00", b"\x00" * 91, key.spki[:26] + compressed,
                bytes([key.spki[0] ^ 1]) + key.spki[1:], "spki", None):
        with pytest.raises(ApproverError):
            check_spki(bad)
    not_on_curve = bytearray(key.spki)
    not_on_curve[-1] ^= 1
    with pytest.raises(ApproverError):
        check_spki(bytes(not_on_curve))


def test_ecdsa_is_sha256_over_authenticator_data_and_client_data_hash():
    key = approver_key("formula")
    s = approve(KIND, DATA, key.descriptor("hwkey-1"), key)
    a = unpack(s["sig"])
    r, s_ = int.from_bytes(a.signature[:32], "big"), int.from_bytes(a.signature[32:], "big")
    key.priv.public_key().verify(encode_dss_signature(r, s_),
                                 a.authenticator_data + hashlib.sha256(a.client_data_json).digest(),
                                 ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("depth", [1000, 1500, 3000])
def test_deep_client_data_is_a_clean_rejection(depth):
    # On Python 3.9 to 3.11 json.loads raises RecursionError here; it must come out as ApproverError.
    from irp.roam.approver import parse_client_data

    si = sig.signing_input(KIND, DATA)
    for data in (b"[" * depth + b"]" * depth, b'{"a":' * depth + b"1" + b"}" * depth):
        with pytest.raises(ApproverError):
            parse_client_data(data[:4096], si=si, origin=APPROVER_ORIGIN)
