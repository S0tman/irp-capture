"""Roaming IRP, Cut 1 step 2.4: the signing recipe and strict Ed25519 (spec v0.3 §15.1, §15.2, §15.7).

One signing input with a domain label and a kind, so a signature made for one
kind of thing can never be passed off as another. Ed25519 is verified under a
strict profile (canonical encodings, S < L, points of prime order only), so the
Python publisher, the browser viewer and the relay all accept exactly the same
signatures. The profile is held to the C2SP/CCTV Ed25519 edge-case vectors.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.roam import sig  # noqa: E402
from irp.roam.sig import (  # noqa: E402
    DOMAIN,
    KINDS,
    RESERVED_KINDS,
    SigError,
    b64url_decode,
    b64url_encode,
    encode_sig,
    key_id,
    load_jcs,
    parse_sig,
    public_key,
    root_id,
    sign,
    signing_input,
    signing_input_for,
    verify,
    verify_ed25519,
)

VECTORS = ROOT / "tests" / "fixtures" / "roam" / "ed25519vectors.json"
L = 2**252 + 27742317777372353535851937790883648493


def _seed(label: str) -> bytes:
    return hashlib.sha256(b"irp-roam test seed " + label.encode()).digest()


SEED = _seed("laptop")
PUB = public_key(SEED)
KID = key_id("dk", PUB)
OTHER_SEED = _seed("other")
OTHER_PUB = public_key(OTHER_SEED)
DATA = b'{"a":1,"b":[true,null,"x"]}'
GOLDEN_SIG = "tGCAk8_dS7ZkVdaQwMilSBuKoliW4p9cwb9RHtsZmfL7GGIUPr3QZmSorDgRkIZCxY_si7ufTUT5Uj0eFfxLBQ"


def _jcs(obj) -> bytes:
    import rfc8785

    return rfc8785.dumps(obj)


# ── The signing input (§15.2) ──

def test_signing_input_golden_bytes():
    # SHA-256("irp-roam/v1" ‖ 0x00 ‖ kind ‖ 0x00 ‖ JCS(obj)), pinned so the viewer and relay can cross-check.
    assert DOMAIN == b"irp-roam/v1"
    assert signing_input("content", DATA).hex() == "ce546054067e0f14162075d74bfbddf398c626101da1f7ed293a355726ebceb0"
    assert signing_input("content", DATA) == hashlib.sha256(b"irp-roam/v1\x00content\x00" + DATA).digest()


def test_signing_input_for_an_object_is_over_its_jcs_bytes():
    obj = {"b": [True, None, "x"], "a": 1}
    assert signing_input_for("content", obj) == signing_input("content", DATA)


def test_every_accepted_kind_gives_a_different_input():
    assert KINDS == frozenset({"content", "disclosure", "checkpoint", "devices-entry", "readers-entry",
                               "capability", "revoke"})
    inputs = {signing_input(k, DATA) for k in KINDS}
    assert len(inputs) == len(KINDS)


@pytest.mark.parametrize("kind", ["proposal", "confirmation", "control"])
def test_reserved_cut2_kinds_are_rejected(kind):
    assert kind in RESERVED_KINDS
    with pytest.raises(SigError, match="reserved"):
        signing_input(kind, DATA)
    with pytest.raises(SigError, match="reserved"):
        sign(kind, DATA, SEED, KID)


@pytest.mark.parametrize("kind", ["", "Content", "content ", "con\x00tent", "receipt", None, b"content", 1])
def test_unknown_kinds_are_rejected(kind):
    with pytest.raises(SigError):
        signing_input(kind, DATA)


def test_signing_input_needs_bytes():
    with pytest.raises(SigError):
        signing_input("content", DATA.decode())


def test_out_of_range_integers_are_sig_errors():
    # I-JSON range only (spec §15.1): JCS can't carry integers beyond 2^53 - 1 exactly.
    with pytest.raises(SigError):
        signing_input_for("content", {"a": 2**53})
    signing_input_for("content", {"a": 2**53 - 1})


def test_floats_are_never_signed():
    # §15.1: no floats in roam statements; JCS number formatting is a classic cross-language trap.
    with pytest.raises(SigError, match="float"):
        signing_input_for("content", {"a": 1.5})
    with pytest.raises(SigError, match="float"):
        signing_input_for("content", {"a": [{"b": 2.0}]})


# ── Key ids (§14.2) ──

def test_key_id_golden():
    expected = "dk-" + hashlib.sha256(b"irp-roam/v1/kid/ed25519/" + PUB).hexdigest()[:32]
    assert key_id("dk", PUB) == expected
    assert key_id("ck", PUB) == "ck-" + expected[3:]
    assert root_id(PUB) == "rt-" + hashlib.sha256(PUB).hexdigest()[:32]


@pytest.mark.parametrize("prefix", ["rt", "rd", "DK", "", "dk-"])
def test_key_id_prefix_is_closed(prefix):
    with pytest.raises(SigError):
        key_id(prefix, PUB)


@pytest.mark.parametrize("pub", [b"", b"\x01" * 31, b"\x01" * 33, "ab" * 32])
def test_ed25519_key_ids_need_32_bytes(pub):
    with pytest.raises(SigError):
        key_id("dk", pub)
    with pytest.raises(SigError):
        root_id(pub)


# ── Sign and verify ──

def test_sign_and_verify_round_trip():
    s = sign("content", DATA, SEED, KID)
    assert set(s) == {"alg", "key_id", "sig"}
    assert s["alg"] == "ed25519" and s["key_id"] == KID
    assert len(b64url_decode(s["sig"])) == 64
    verify("content", DATA, s, PUB)


def test_signing_is_deterministic():
    assert sign("content", DATA, SEED, KID) == sign("content", DATA, SEED, KID)


def test_root_signatures_use_the_root_id():
    rid = root_id(PUB)
    s = sign("devices-entry", DATA, SEED, rid)
    verify("devices-entry", DATA, s, PUB)


def test_kind_confusion_fails():
    s = sign("content", DATA, SEED, KID)
    for other in KINDS - {"content"}:
        with pytest.raises(SigError):
            verify(other, DATA, s, PUB)


def test_a_changed_byte_fails():
    s = sign("content", DATA, SEED, KID)
    with pytest.raises(SigError):
        verify("content", DATA.replace(b"1", b"2"), s, PUB)
    with pytest.raises(SigError):
        verify("content", DATA + b" ", s, PUB)


def test_the_wrong_key_fails():
    s = sign("content", DATA, SEED, KID)
    with pytest.raises(SigError):
        verify("content", DATA, s, OTHER_PUB)


def test_sign_refuses_a_key_id_that_isnt_the_seeds():
    with pytest.raises(SigError, match="key id"):
        sign("content", DATA, SEED, key_id("dk", OTHER_PUB))
    with pytest.raises(SigError, match="key id"):
        sign("content", DATA, SEED, "dk-" + "0" * 32)


def test_verify_refuses_a_key_id_that_isnt_the_public_keys():
    s = sign("content", DATA, SEED, KID)
    relabelled = dict(s, key_id=key_id("dk", OTHER_PUB))
    with pytest.raises(SigError, match="key id"):
        verify("content", DATA, relabelled, PUB)
    as_ck = dict(s, key_id=key_id("ck", PUB))  # the same key under another role still binds its own id
    verify("content", DATA, as_ck, PUB)


@pytest.mark.parametrize("alg", ["webauthn-es256", "fido2-es256"])
def test_hardware_key_and_passkey_assertions_are_not_verified_here(alg):
    s = dict(sign("content", DATA, SEED, KID), alg=alg)
    with pytest.raises(SigError, match="approver"):
        verify("content", DATA, s, PUB)


@pytest.mark.parametrize("alg", ["ed448", "Ed25519", "EdDSA", "", None, "es256"])
def test_unknown_algorithms_are_rejected(alg):
    s = dict(sign("content", DATA, SEED, KID), alg=alg)
    with pytest.raises(SigError):
        verify("content", DATA, s, PUB)


# ── The .sig file: one small JCS object {alg, key_id, sig} ──

def test_sig_file_round_trip():
    s = sign("content", DATA, SEED, KID)
    data = encode_sig(s)
    assert data == _jcs(s) and not data.endswith(b"\n")
    assert parse_sig(data) == s


@pytest.mark.parametrize("mutate", [
    lambda s: _jcs({**s, "kind": "content"}),                       # unknown key
    lambda s: _jcs({k: v for k, v in s.items() if k != "key_id"}),  # missing key
    lambda s: json.dumps(s).encode(),                                # not JCS (spaces)
    lambda s: _jcs(s) + b"\n",                                       # trailing newline
    lambda s: b'{"alg":"ed25519","alg":"ed25519",' + _jcs(s)[17:],   # duplicate key
    lambda s: _jcs({**s, "key_id": s["key_id"].upper()}),            # key id not lowercase hex
    lambda s: _jcs({**s, "key_id": "rd-" + "0" * 32}),               # not a signing key id
    lambda s: _jcs({**s, "sig": s["sig"] + "=="}),                   # padded b64url
    lambda s: _jcs({**s, "sig": s["sig"][:-2]}),                     # 63 bytes
    lambda s: _jcs({**s, "sig": b64url_encode(b"\x00" * 65)}),       # 65 bytes
    lambda s: _jcs({**s, "sig": s["sig"].replace("-", "+").replace("_", "/") + "+/"}),  # standard alphabet
    lambda s: _jcs({**s, "sig": 7}),                                 # not a string
    lambda s: _jcs([s]),                                             # not an object
    lambda s: b"\xff" + _jcs(s),                                     # not UTF-8
])
def test_sig_file_parser_is_strict(mutate):
    s = sign("content", DATA, SEED, KID)
    with pytest.raises(SigError):
        parse_sig(mutate(s))


def test_duplicate_key_sig_file_is_otherwise_well_formed():
    # Guard for the duplicate-key case above: the only flaw is the repeated key.
    s = sign("content", DATA, SEED, KID)
    dup = b'{"alg":"ed25519","alg":"ed25519",' + _jcs(s)[17:]
    assert json.loads(dup) == s


# ── b64url (§15.1: strict, no padding) ──

@pytest.mark.parametrize("n", [0, 1, 2, 3, 16, 32, 64])
def test_b64url_round_trip(n):
    raw = bytes(range(n))
    assert b64url_decode(b64url_encode(raw)) == raw
    assert "=" not in b64url_encode(raw)


@pytest.mark.parametrize("text", ["AB=", "AB==", "A", "AB C", "AB+C", "AB/C", "AB\n", "ABC=",
                                  "AF", "AB",  # one byte with non-zero unused bits ('AA' is the only form of 0x00)
                                  ])
def test_b64url_rejects_non_canonical_text(text):
    with pytest.raises(SigError):
        b64url_decode(text)


def test_b64url_length_check():
    with pytest.raises(SigError):
        b64url_decode(b64url_encode(b"\x00" * 15), length=16)
    assert b64url_decode(b64url_encode(b"\x00" * 16), length=16) == b"\x00" * 16


# ── Strict JCS loading (§15.1 verify order steps 2 and 3) ──

def test_load_jcs_accepts_exact_jcs_only():
    assert load_jcs(DATA, "test") == {"a": 1, "b": [True, None, "x"]}
    for bad in [b'{"b":[true,null,"x"],"a":1}', b'{"a": 1,"b":[true,null,"x"]}', b'{"a":1,"a":1}',
                b'{"a":1.5}', b'{"a":1e2}', b'{"a":NaN}', b'{"a":Infinity}', DATA + b"\n", b"\xef\xbb\xbf" + DATA,
                b'{"a":"\\u00e9"}', b'{"a":"\\ud800"}', b"", b"nul"]:
        with pytest.raises(SigError):
            load_jcs(bad, "test")


def test_load_jcs_accepts_raw_utf8_strings():
    obj = {"name": "Göta Testbolag"}
    assert load_jcs(_jcs(obj), "test") == obj


# ── Strict Ed25519 (§15.2) ──

RFC8032 = [  # RFC 8032 §7.1, TEST 1 and TEST 2
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


@pytest.mark.parametrize("seed,pub,msg,sig_hex", RFC8032)
def test_rfc8032_vectors(seed, pub, msg, sig_hex):
    assert public_key(bytes.fromhex(seed)).hex() == pub
    verify_ed25519(bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig_hex))
    with pytest.raises(SigError):
        verify_ed25519(bytes.fromhex(pub), bytes.fromhex(msg) + b"!", bytes.fromhex(sig_hex))


def _with_s(sig64: bytes, s: int) -> bytes:
    return sig64[:32] + s.to_bytes(32, "little")


@pytest.mark.parametrize("seed,pub,msg,sig_hex", RFC8032)
def test_non_canonical_s_is_rejected(seed, pub, msg, sig_hex):
    good = bytes.fromhex(sig_hex)
    s = int.from_bytes(good[32:], "little")
    for bad_s in (s + L, L, 2**256 - 1, s | (1 << 255)):
        if bad_s >= 2**256:
            continue
        with pytest.raises(SigError, match="S"):
            verify_ed25519(bytes.fromhex(pub), bytes.fromhex(msg), _with_s(good, bad_s))


@pytest.mark.parametrize("pub_len,sig_len", [(31, 64), (33, 64), (32, 63), (32, 65), (0, 0)])
def test_wrong_lengths_are_rejected(pub_len, sig_len):
    with pytest.raises(SigError):
        verify_ed25519(b"\x01" * pub_len, b"m", b"\x01" * sig_len)


IDENTITY = (1).to_bytes(32, "little")  # the neutral point, order 1


def _openssl_accepts(pub: bytes, msg: bytes, s: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(s, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def test_the_identity_key_is_rejected():
    # Under A = identity, R = identity and S = 0 satisfy the equation for every message (a universal
    # forgery that OpenSSL accepts); only the profile's order check stops it.
    forged = IDENTITY + bytes(32)
    assert _openssl_accepts(IDENTITY, b"any message", forged)
    with pytest.raises(SigError, match="public key isn't a point of prime order"):
        verify_ed25519(IDENTITY, b"any message", forged)


def test_an_identity_r_is_rejected():
    # A key holder can sign with R = identity (r = 0, S = k*a). OpenSSL and a torsion-only check accept it;
    # the profile requires order exactly L, so every implementation must reject it.
    h = hashlib.sha512(SEED).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    msg = signing_input("content", DATA)
    k = int.from_bytes(hashlib.sha512(IDENTITY + PUB + msg).digest(), "little") % L
    forged = IDENTITY + (k * a % L).to_bytes(32, "little")
    assert _openssl_accepts(PUB, msg, forged)
    with pytest.raises(SigError, match="signature R isn't a point of prime order"):
        verify_ed25519(PUB, msg, forged)
    with pytest.raises(SigError):
        verify("content", DATA, {"alg": "ed25519", "key_id": KID, "sig": b64url_encode(forged)}, PUB)


def test_golden_signature():
    # Ed25519 is deterministic, so this pins the whole recipe (SI, then Ed25519 over the 32-byte SI) for
    # the viewer and relay to cross-check, independently of sign().
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    si = hashlib.sha256(b"irp-roam/v1\x00content\x00" + DATA).digest()
    expected = Ed25519PrivateKey.from_private_bytes(SEED).sign(si)
    s = sign("content", DATA, SEED, KID)
    assert b64url_decode(s["sig"]) == expected
    assert s["sig"] == GOLDEN_SIG
    verify_ed25519(PUB, si, expected)


def _cctv():
    return json.loads(VECTORS.read_text())


def test_cctv_vector_file_is_the_pinned_copy():
    assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == \
        "b38e84caf3e7e89170ff520292dbeae421b0a794c27408ce5ce973018fe3d7f9"
    assert (VECTORS.parent / "ed25519vectors-LICENSE.txt").read_text().startswith("Copyright 2019 Google LLC")


def test_cctv_edge_vectors_only_flagless_signatures_verify():
    # The roaming profile accepts a vector exactly when it has no edge-case flags: no small-order or
    # mixed-order points, no non-canonical encodings, nothing that depends on the verification formula.
    accepted, rejected = [], []
    for v in _cctv():
        pub, msg, s = bytes.fromhex(v["key"]), v["msg"].encode(), bytes.fromhex(v["sig"])
        try:
            verify_ed25519(pub, msg, s)
            accepted.append(v["number"])
        except SigError:
            rejected.append(v["number"])
    expected = sorted(v["number"] for v in _cctv() if not v["flags"])
    assert sorted(accepted) == expected
    assert len(accepted) + len(rejected) == 914


def test_cctv_vectors_304_and_305_by_name():
    # #304 has R = identity: the one vector a torsion-only JS check would accept. #305 is the only clean one.
    by_number = {v["number"]: v for v in _cctv()}
    v304, v305 = by_number[304], by_number[305]
    assert v304["flags"] == ["low_order_R"] and v304["sig"][:64] == IDENTITY.hex()
    with pytest.raises(SigError, match="R isn't a point of prime order"):
        verify_ed25519(bytes.fromhex(v304["key"]), v304["msg"].encode(), bytes.fromhex(v304["sig"]))
    verify_ed25519(bytes.fromhex(v305["key"]), v305["msg"].encode(), bytes.fromhex(v305["sig"]))


def test_cctv_flag_families_are_each_rejected():
    seen = set()
    for v in _cctv():
        for flag in v["flags"] or []:
            if flag in seen:
                continue
            with pytest.raises(SigError):
                verify_ed25519(bytes.fromhex(v["key"]), v["msg"].encode(), bytes.fromhex(v["sig"]))
            seen.add(flag)
    assert seen == {"low_order_A", "low_order_R", "non_canonical_A", "non_canonical_R", "low_order_component_A",
                    "low_order_component_R", "low_order_residue", "reencoded_k"}


def test_our_profile_is_stricter_than_openssl_alone():
    # Plain OpenSSL accepts some of these (for example low_order_A); the profile must not depend on that.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    openssl_accepts = 0
    for v in _cctv():
        if not v["flags"]:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(v["key"])).verify(bytes.fromhex(v["sig"]),
                                                                              v["msg"].encode())
            openssl_accepts += 1
        except (InvalidSignature, ValueError):
            pass
    assert openssl_accepts > 0


# ── Dependencies stay lazy ──

def test_importing_sig_loads_no_optional_dependency():
    code = ("import sys; sys.path.insert(0, %r); import irp.roam.sig; "
            "print(any(m.split('.')[0] in ('cryptography', 'rfc8785') for m in sys.modules))" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_signatures_never_carry_the_seed():
    s = sign("content", DATA, SEED, KID)
    assert SEED.hex() not in repr(s) and SEED.hex() not in encode_sig(s).decode()
    assert b64url_encode(SEED) not in encode_sig(s).decode()
    assert sig.__name__ == "irp.roam.sig"
