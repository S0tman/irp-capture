"""Hardware-key and passkey assertions for Roaming IRP (spec v0.3 §15.2, §14.5a).

Two kinds of signer sign with an ES256 WebAuthn/FIDO2 assertion instead of Ed25519:

- an approver (`fido2-es256`, `ak-`): a FIDO2 hardware key, tap plus PIN. Its rp_id is the constant
  `approver.irp-roam.invalid`, under `.invalid` (RFC 6761) so no web page can ever ask for it;
- a companion (`webauthn-es256`, `dk-`): the phone's passkey, with the phone app's rp_id and origin.

The challenge is the 32-byte signing input SI(kind, JCS(body)) from sig.py. The assertion travels as the
`sig` of a §15.2 signature object: the strict b64url of the exact JCS bytes of

    {"authenticator_data", "client_data_json", "signature"}

each a strict b64url string. The checks, in the order they run:

- authenticator data: exactly 37 bytes (rpIdHash ‖ flags ‖ signCount); rpIdHash = sha256(rp_id); UP and
  UV set; the reserved bits 0x02 and 0x20, AT (0x40) and ED (0x80) clear; BE equal to the descriptor's
  `be`; BS only with BE (and never compared to the descriptor after enrolment); signCount ignored;
- client data: at most 4096 bytes of strict UTF-8 without a BOM; duplicate keys at any depth, NaN and
  Infinity rejected; a top-level object; `type` "webauthn.get"; `challenge` exactly b64url(SI); `origin`
  the expected one; `crossOrigin` absent or the literal false; `topOrigin` absent; other members ignored;
- signature: raw r ‖ s, 64 bytes, 1 ≤ r < n and 1 ≤ s ≤ n/2 (whoever stores an assertion swaps a high s
  for n - s, so a stored line has one encoding), then ECDSA P-256/SHA-256 over
  authenticator_data ‖ sha256(client_data_json).

The public key is the fixed 91-byte DER SPKI of an uncompressed P-256 point. The fixed vectors in
tests/fixtures/roam/assertion_vectors.json come from an independent generator and serve the JS mirror too.
Real hardware keys arrive with gate 0.5, behind the `Authenticator` protocol; tests use a software one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Tuple

from . import sig
from ._deps import ec

APPROVER_RP_ID = "approver.irp-roam.invalid"
APPROVER_ORIGIN = "https://approver.irp-roam.invalid"
ES256_ALGS = ("webauthn-es256", "fido2-es256")
SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
SPKI_LEN = 91
AUTH_DATA_LEN = 37
MAX_CLIENT_DATA = 4096
PACKED_KEYS = frozenset({"authenticator_data", "client_data_json", "signature"})

UP, RFU1, UV, BE, BS, RFU2, AT, ED = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # the P-256 group order


class ApproverError(sig.SigError):
    """An assertion, its key or its client data fails the §14.5a profile."""


@dataclass(frozen=True)
class Assertion:
    authenticator_data: bytes
    client_data_json: bytes
    signature: bytes  # raw r ‖ s, low s

    @property
    def flags(self) -> int:
        return self.authenticator_data[32]

    @property
    def be(self) -> bool:
        return bool(self.flags & BE)

    @property
    def bs(self) -> bool:
        return bool(self.flags & BS)


class Authenticator(Protocol):
    """What approve() needs from a key: a CTAP2-style getAssertion over a client data hash. The hardware
    wrapper (python-fido2, user verification required) arrives with gate 0.5."""

    def get_assertion(self, rp_id: str, client_data_hash: bytes, cred_id: bytes) -> Tuple[bytes, bytes]:
        """Return (authenticator_data, DER ECDSA signature)."""


# ── Keys ──

def check_spki(spki: Any) -> bytes:
    """The fixed 91-byte DER SPKI: id-ecPublicKey, named curve P-256, an uncompressed point on the curve."""
    if not isinstance(spki, (bytes, bytearray)) or len(spki) != SPKI_LEN or not bytes(spki).startswith(SPKI_PREFIX):
        raise ApproverError("a P-256 key must be the 91-byte DER SPKI of an uncompressed point on a named curve")
    spki = bytes(spki)
    if spki[len(SPKI_PREFIX)] != 0x04:
        raise ApproverError("a P-256 key must be an uncompressed point")
    _public_key(spki)
    return spki


def _public_key(spki: bytes) -> Any:
    e = ec()
    try:
        return e.EllipticCurvePublicKey.from_encoded_point(e.SECP256R1(), spki[len(SPKI_PREFIX):])
    except ValueError:
        raise ApproverError("the P-256 point isn't on the curve") from None


# ── Client data ──

def client_data_json(si: bytes, origin: str) -> bytes:
    """The exact client data the CLI writes: {"type","challenge","origin","crossOrigin":false}, compact."""
    if not isinstance(si, (bytes, bytearray)) or len(si) != 32:
        raise ApproverError("the challenge is the 32-byte signing input")
    if not isinstance(origin, str) or not origin.isascii():
        raise ApproverError("the origin must be ASCII text")
    return ('{"type":"webauthn.get","challenge":"%s","origin":"%s","crossOrigin":false}'
            % (sig.b64url_encode(bytes(si)), origin)).encode("ascii")


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ApproverError(f"duplicate key {k!r} in client data")
        out[k] = v
    return out


def _no_constant(text: str) -> Any:
    raise ApproverError(f"{text} isn't JSON")


def parse_client_data(data: bytes, *, si: bytes, origin: str) -> dict:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_CLIENT_DATA:
        raise ApproverError(f"client data must be at most {MAX_CLIENT_DATA} bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise ApproverError("client data isn't strict UTF-8") from None
    if text.startswith("\ufeff"):
        raise ApproverError("client data starts with a byte order mark")
    try:
        cd = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except ApproverError:
        raise
    except Exception as exc:  # ValueError, and RecursionError on deep nesting in Python 3.9 to 3.11
        raise ApproverError(f"client data isn't JSON ({type(exc).__name__})") from None
    if not isinstance(cd, dict):
        raise ApproverError("client data must be a JSON object")
    if cd.get("type") != "webauthn.get" or not isinstance(cd.get("type"), str):
        raise ApproverError("client data type must be webauthn.get")
    challenge = cd.get("challenge")
    if not isinstance(challenge, str) or challenge != sig.b64url_encode(si):
        raise ApproverError("the challenge isn't this line's signing input")
    if not isinstance(cd.get("origin"), str) or cd["origin"] != origin:
        raise ApproverError("the client data origin isn't the expected one")
    if "crossOrigin" in cd and cd["crossOrigin"] is not False:
        raise ApproverError("crossOrigin must be absent or false")
    if "topOrigin" in cd:
        raise ApproverError("topOrigin must be absent")
    return cd


# ── Authenticator data and the signature ──

def _check_authenticator_data(ad: Any, *, rp_id: str | None, be: bool | None) -> int:
    if not isinstance(ad, (bytes, bytearray)) or len(ad) != AUTH_DATA_LEN:
        raise ApproverError("authenticator data must be exactly 37 bytes (no attested data, no extensions)")
    flags = ad[32]
    if rp_id is not None and bytes(ad[:32]) != hashlib.sha256(rp_id.encode("ascii")).digest():
        raise ApproverError("the rpIdHash isn't sha256 of the expected rp_id")
    if not flags & UP:
        raise ApproverError("user presence (UP) isn't set")
    if not flags & UV:
        raise ApproverError("user verification (UV) isn't set: a tap without the PIN or Face ID doesn't count")
    if flags & (RFU1 | RFU2 | AT | ED):
        raise ApproverError("reserved, AT or ED flags are set")
    if flags & BS and not flags & BE:
        raise ApproverError("BS is set without BE")
    if be is not None and bool(flags & BE) != be:
        raise ApproverError("the BE flag doesn't match the enrolled key")
    return flags


def _raw_signature(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 64:
        raise ApproverError("the signature must be raw r ‖ s, 64 bytes")
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    if not 1 <= r < N:
        raise ApproverError("signature r is out of range")
    if not 1 <= s <= N // 2:
        raise ApproverError("signature s must be between 1 and n/2 (low s)")
    return r, s


def pack(authenticator_data: bytes, client_data: bytes, der_signature: bytes) -> str:
    """Store an assertion: DER to raw r ‖ s with a high s swapped for n - s, packed as strict b64url JCS."""
    _check_authenticator_data(authenticator_data, rp_id=None, be=None)
    if not isinstance(client_data, (bytes, bytearray)) or len(client_data) > MAX_CLIENT_DATA:
        raise ApproverError(f"client data must be at most {MAX_CLIENT_DATA} bytes")
    try:
        r, s = ec().decode_dss_signature(bytes(der_signature))
    except (ValueError, TypeError):
        raise ApproverError("the authenticator didn't return a DER ECDSA signature") from None
    if s > N // 2:
        s = N - s
    if not (1 <= r < N and 1 <= s < N):
        raise ApproverError("the authenticator's signature is out of range")
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    from irp.integrity.canonical import canonicalize

    packed = canonicalize({"authenticator_data": sig.b64url_encode(bytes(authenticator_data)),
                           "client_data_json": sig.b64url_encode(bytes(client_data)),
                           "signature": sig.b64url_encode(raw)})
    return sig.b64url_encode(packed)


def unpack(text: Any) -> Assertion:
    try:
        obj = sig.load_jcs(sig.b64url_decode(text), "packed assertion", error=ApproverError)
        if not isinstance(obj, dict) or set(obj) != PACKED_KEYS:
            raise ApproverError("a packed assertion has exactly authenticator_data, client_data_json, signature")
        parts = {k: sig.b64url_decode(obj[k]) for k in PACKED_KEYS}
    except ApproverError:
        raise
    except sig.SigError as exc:
        raise ApproverError(f"packed assertion: {exc}") from None
    if len(parts["authenticator_data"]) != AUTH_DATA_LEN:
        raise ApproverError("authenticator data must be exactly 37 bytes (no attested data, no extensions)")
    if len(parts["signature"]) != 64:
        raise ApproverError("the signature must be raw r ‖ s, 64 bytes")
    if len(parts["client_data_json"]) > MAX_CLIENT_DATA:
        raise ApproverError(f"client data must be at most {MAX_CLIENT_DATA} bytes")
    return Assertion(parts["authenticator_data"], parts["client_data_json"], parts["signature"])


def verify_assertion(si: bytes, packed: Any, *, spki: bytes, rp_id: str, origin: str, be: bool) -> Assertion:
    """Every §14.5a check over one packed assertion. Returns it (so the caller can read BE and BS)."""
    a = unpack(packed)
    key = _public_key(check_spki(spki))
    _check_authenticator_data(a.authenticator_data, rp_id=rp_id, be=be)
    parse_client_data(a.client_data_json, si=si, origin=origin)
    r, s = _raw_signature(a.signature)
    e = ec()
    try:
        key.verify(e.encode_dss_signature(r, s), a.authenticator_data + hashlib.sha256(a.client_data_json).digest(),
                   e.ECDSA(e.hashes.SHA256()))
    except e.InvalidSignature:
        raise ApproverError("bad assertion signature") from None
    return a


def _expected(descriptor: Mapping[str, Any]) -> tuple[str, bytes, str, str, bool]:
    try:
        alg, kid = descriptor["alg"], descriptor["kid"]
        spki = sig.b64url_decode(descriptor["pub"])
        wa = descriptor["webauthn"]
        rp_id, origin, be = wa["rp_id"], wa["origin"], wa["be"]
    except (KeyError, TypeError, sig.SigError):
        raise ApproverError("the signer's descriptor lacks pub or webauthn values") from None
    if alg not in ES256_ALGS:
        raise ApproverError(f"alg {alg!r} isn't an assertion algorithm")
    if not (isinstance(rp_id, str) and isinstance(origin, str) and type(be) is bool):
        raise ApproverError("the descriptor's rp_id, origin or be has the wrong type")
    if alg == "fido2-es256" and (rp_id != APPROVER_RP_ID or origin != APPROVER_ORIGIN or be):
        raise ApproverError(f"an approver uses rp_id {APPROVER_RP_ID} and origin {APPROVER_ORIGIN}, never BE")
    prefix = "ak" if alg == "fido2-es256" else "dk"
    if not isinstance(kid, str) or kid != sig.key_id(prefix, spki, alg):
        raise ApproverError("the descriptor's key id doesn't match its key")
    return alg, spki, rp_id, origin, be


def verify(kind: str, data: bytes, sig_obj: Mapping[str, Any], descriptor: Mapping[str, Any]) -> Assertion:
    """Verify a signature object made by an ES256 key over the exact bytes `data` as `kind`."""
    try:
        s = sig._check_sig_obj(sig_obj)
    except ApproverError:
        raise
    except sig.SigError as exc:
        raise ApproverError(str(exc)) from None
    alg, spki, rp_id, origin, be = _expected(descriptor)
    if s["alg"] != alg:
        raise ApproverError(f"the signature's alg {s['alg']!r} isn't the key's ({alg})")
    if s["key_id"] != descriptor["kid"]:
        raise ApproverError("the signature's key id isn't the descriptor's")
    try:
        si = sig.signing_input(kind, data)
    except sig.SigError as exc:
        raise ApproverError(str(exc)) from None
    return verify_assertion(si, s["sig"], spki=spki, rp_id=rp_id, origin=origin, be=be)


def approve(kind: str, data: bytes, descriptor: Mapping[str, Any], authenticator: Authenticator) -> dict[str, str]:
    """Ask a key to sign the exact bytes `data` as `kind`, then verify the result before returning it."""
    alg, spki, rp_id, origin, be = _expected(descriptor)
    try:
        cred_id = sig.b64url_decode(descriptor["webauthn"]["cred_id"])
        si = sig.signing_input(kind, data)
    except (KeyError, TypeError, sig.SigError) as exc:
        raise ApproverError(f"can't ask for an assertion: {exc}") from None
    cdj = client_data_json(si, origin)
    try:
        authenticator_data, der = authenticator.get_assertion(rp_id, hashlib.sha256(cdj).digest(), cred_id)
    except ApproverError:
        raise
    except (TypeError, ValueError) as exc:
        raise ApproverError(f"the authenticator returned something unusable: {type(exc).__name__}") from None
    out = {"alg": alg, "key_id": descriptor["kid"], "sig": pack(authenticator_data, cdj, der)}
    verify(kind, data, out, descriptor)
    return out
