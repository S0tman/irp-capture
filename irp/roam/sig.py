"""Signatures for relay objects (Roaming IRP spec v0.3 §15.1, §15.2, §14.2).

One signing input for everything roaming signs, with a domain label and a kind:

    SI(kind, obj) = SHA-256("irp-roam/v1" ‖ 0x00 ‖ kind ‖ 0x00 ‖ JCS(obj))

so a signature made for one kind of object can never be passed off as another.
The kinds are a closed list; the Cut 2 kinds are reserved and rejected.

Ed25519 is verified under a strict profile, so every implementation (this one,
the browser viewer, the relay) accepts exactly the same signatures: 32-byte keys
and 64-byte signatures, canonical point encodings, S < L, and A and R of prime
order L exactly (not the identity, no small-order or mixed-order points), then the
RFC 8032 equation. With those points the cofactored and cofactorless equations
agree, so a JS verifier gets the same answers from noble with zip215:false once
it also rejects A or R when `P.is0() || !P.isTorsionFree()`. The profile is held
to the C2SP/CCTV Ed25519 edge-case vectors: exactly the vectors with no flags
verify (vector 304, R = identity, is the one a torsion-only check would miss).

A signature travels as one small JCS object, `{"alg", "key_id", "sig"}` with the
signature in strict b64url: the `.sig` files of a Slice, and the entries of a
log line's `sigs` list. Hardware-key and passkey assertions (`fido2-es256`,
`webauthn-es256`) are checked by approver.py (step 2.5), never here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from ._deps import ed25519

DOMAIN = b"irp-roam/v1"
KINDS = frozenset({"content", "disclosure", "checkpoint", "devices-entry", "readers-entry", "capability", "revoke"})
RESERVED_KINDS = frozenset({"proposal", "confirmation", "control"})
ALG = "ed25519"
DELEGATED_ALGS = frozenset({"webauthn-es256", "fido2-es256"})  # approver.py, step 2.5
KEY_ID_PREFIXES = frozenset({"dk", "ck", "ak"})
SIG_KEYS = frozenset({"alg", "key_id", "sig"})

_KEY_ID = re.compile(r"(dk|ck|ak|rt)-[0-9a-f]{32}")  # fullmatch only
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class SigError(ValueError):
    """A signature, signed object or signature file fails the strict profile."""


# ── Encoding helpers (§15.1) ──

def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: Any, length: int | None = None) -> bytes:
    """Strict b64url: URL-safe alphabet, no padding, and the only encoding of its bytes."""
    if not isinstance(text, str) or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise SigError("not strict b64url (URL-safe alphabet, no padding)")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(raw) != text:
        raise SigError("not canonical b64url (non-zero unused bits)")
    if length is not None and len(raw) != length:
        raise SigError(f"b64url value must decode to {length} bytes, got {len(raw)}")
    return raw


def _canonical(obj: Any) -> bytes:
    from irp.integrity.canonical import canonicalize

    return canonicalize(obj)


def _no_floats(obj: Any) -> None:
    if isinstance(obj, float):
        raise SigError("roam signed objects never contain floats (spec §15.1)")
    if isinstance(obj, dict):
        for v in obj.values():
            _no_floats(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_floats(v)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise SigError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_float(text: str) -> Any:
    raise SigError("roam signed objects never contain floats (spec §15.1)")


def _no_constant(text: str) -> Any:
    raise SigError(f"{text} isn't JSON")


def load_jcs(data: Any, what: str, *, error: Callable[[str], Exception] = SigError) -> Any:
    """§15.1 steps 2 and 3: strict UTF-8, duplicate keys and floats rejected, and the bytes must be exactly
    the JCS form of what they parse to (so no spaces, no reordering, no escapes JCS wouldn't write)."""
    if not isinstance(data, (bytes, bytearray)):
        raise error(f"{what} must be bytes")
    try:
        text = bytes(data).decode("utf-8")
        obj = json.loads(text, object_pairs_hook=_reject_duplicates, parse_float=_no_float,
                         parse_constant=_no_constant)
        canonical = _canonical(obj)
    except SigError as exc:
        raise error(f"{what}: {exc}") from None
    except Exception as exc:  # UnicodeDecodeError, JSONDecodeError, a JCS failure on a lone surrogate, ...
        raise error(f"{what} isn't strict JSON: {type(exc).__name__}") from None
    if canonical != bytes(data):
        raise error(f"{what} isn't exact RFC 8785 (JCS) bytes")
    return obj


# ── The signing input (§15.2) ──

def check_kind(kind: Any) -> str:
    if isinstance(kind, str) and kind in RESERVED_KINDS:
        raise SigError(f"kind {kind!r} is reserved until Cut 2")
    if not (isinstance(kind, str) and kind in KINDS):
        raise SigError(f"unknown signing kind {kind!r}; accepted kinds: {', '.join(sorted(KINDS))}")
    return kind


def signing_input(kind: str, data: bytes) -> bytes:
    """SI over the exact JCS bytes that ship (verify order step 1 runs before any parsing)."""
    check_kind(kind)
    if not isinstance(data, (bytes, bytearray)):
        raise SigError("the signing input is computed over bytes")
    return hashlib.sha256(DOMAIN + b"\x00" + kind.encode("ascii") + b"\x00" + bytes(data)).digest()


def signing_input_for(kind: str, obj: Any) -> bytes:
    _no_floats(obj)
    try:
        data = _canonical(obj)
    except Exception as exc:  # integers beyond 2^53 - 1, lone surrogates, non-JSON values
        raise SigError(f"can't canonicalise the object as I-JSON: {type(exc).__name__}") from None
    return signing_input(kind, data)


# ── Key ids (§14.1, §14.2) ──

def _pub32(pub: Any) -> bytes:
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != 32:
        raise SigError("an Ed25519 public key is 32 bytes")
    return bytes(pub)


def key_id(prefix: str, pub: bytes, alg: str = ALG) -> str:
    """`dk-`, `ck-` and `ak-` ids: hex(sha256("irp-roam/v1/kid/" + alg + "/" + keybytes))[:32]."""
    if prefix not in KEY_ID_PREFIXES:
        raise SigError(f"key id prefix must be one of {', '.join(sorted(KEY_ID_PREFIXES))}")
    if alg == ALG:
        pub = _pub32(pub)
    elif alg not in DELEGATED_ALGS or not isinstance(pub, (bytes, bytearray)) or not pub:
        raise SigError(f"can't make a key id for alg {alg!r}")
    return f"{prefix}-" + hashlib.sha256(b"irp-roam/v1/kid/" + alg.encode() + b"/" + bytes(pub)).hexdigest()[:32]


def root_id(pub: bytes) -> str:
    """The root signer's id: "rt-" + hex(sha256(root_pub))[:32]."""
    return "rt-" + hashlib.sha256(_pub32(pub)).hexdigest()[:32]


def _expected_id(kid: str, pub: bytes) -> str:
    return root_id(pub) if kid.startswith("rt-") else key_id(kid[:2], pub)


# ── Keys, signing and the signature object ──

def generate_seed(rng: Callable[[int], bytes]) -> bytes:
    seed = rng(32)
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise SigError("rng must return 32 bytes")
    return seed


def public_key(seed: bytes) -> bytes:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise SigError("an Ed25519 seed is 32 bytes")
    e = ed25519()
    pub = e.Ed25519PrivateKey.from_private_bytes(bytes(seed)).public_key()
    return pub.public_bytes(e.Encoding.Raw, e.PublicFormat.Raw)


def _check_sig_obj(sig_obj: Any) -> dict[str, str]:
    if not isinstance(sig_obj, Mapping) or set(sig_obj) != SIG_KEYS:
        raise SigError(f"a signature is exactly {{{', '.join(sorted(SIG_KEYS))}}}")
    alg, kid, s = sig_obj["alg"], sig_obj["key_id"], sig_obj["sig"]
    if not (isinstance(alg, str) and (alg == ALG or alg in DELEGATED_ALGS)):
        raise SigError(f"unknown signature algorithm {alg!r}")
    if not (isinstance(kid, str) and _KEY_ID.fullmatch(kid)):
        raise SigError(f"bad signer key id {kid!r}")
    if alg == ALG:
        b64url_decode(s, 64)
    elif not isinstance(s, str):
        raise SigError("the signature must be a b64url string")
    return {"alg": alg, "key_id": kid, "sig": s}


def sign(kind: str, data: bytes, seed: bytes, kid: str) -> dict[str, str]:
    """Sign the exact bytes `data` as `kind`. `kid` must be the seed's own key id (dk-, ck-, ak- or rt-)."""
    si = signing_input(kind, data)
    pub = public_key(seed)
    if not (isinstance(kid, str) and _KEY_ID.fullmatch(kid)) or _expected_id(kid, pub) != kid:
        raise SigError("the key id doesn't belong to this signing key")
    raw = ed25519().Ed25519PrivateKey.from_private_bytes(bytes(seed)).sign(si)
    return {"alg": ALG, "key_id": kid, "sig": b64url_encode(raw)}


def encode_sig(sig_obj: Mapping[str, str]) -> bytes:
    """The `.sig` file: the signature object as exact JCS bytes, no trailing newline."""
    return _canonical(_check_sig_obj(sig_obj))


def parse_sig(data: bytes) -> dict[str, str]:
    return _check_sig_obj(load_jcs(data, "signature file"))


def verify(kind: str, data: bytes, sig_obj: Mapping[str, Any], pub: bytes) -> None:
    """Verify a signature object over the exact bytes `data` as `kind`. Raises SigError."""
    s = _check_sig_obj(sig_obj)
    if s["alg"] in DELEGATED_ALGS:
        raise SigError(f"{s['alg']} assertions are verified by the approver module (step 2.5), not here")
    if _expected_id(s["key_id"], _pub32(pub)) != s["key_id"]:
        raise SigError("the signer key id doesn't match the public key")
    verify_ed25519(pub, signing_input(kind, data), b64url_decode(s["sig"], 64))


# ── Strict Ed25519 (RFC 8032 point decoding and arithmetic, for the profile checks only) ──

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_IDENTITY = (0, 1, 1, 0)


def _decode_point(b: bytes) -> tuple[int, int, int, int] | None:
    """RFC 8032 §5.1.3, strict: y < p, x recoverable, and no "negative zero". Extended coordinates."""
    y = int.from_bytes(b, "little")
    sign_bit, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _P:
        return None
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x = u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P) % _P
    vx2 = v * x * x % _P
    if vx2 == (-u) % _P and vx2 != u:
        x = x * _SQRT_M1 % _P
    elif vx2 != u:
        return None
    if x == 0 and sign_bit:
        return None
    if x & 1 != sign_bit:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """RFC 8032 §5.1.4 addition (complete, so it also doubles)."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    q = _IDENTITY
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _is_identity(p: tuple[int, int, int, int]) -> bool:
    x, y, z, _ = p
    return x % _P == 0 and (y - z) % _P == 0


def _prime_order(p: tuple[int, int, int, int]) -> bool:
    return not _is_identity(p) and _is_identity(_mul(_L, p))


def check_ed25519_public(pub: Any) -> bytes:
    """A 32-byte canonical encoding of a point of prime order L: the §15.2 rule for A, applied at enrolment."""
    pub = _pub32(pub)
    point = _decode_point(pub)
    if point is None:
        raise SigError("the public key isn't a canonical point encoding")
    if not _prime_order(point):
        raise SigError("the public key isn't a point of prime order (small-order or mixed-order)")
    return pub


def verify_ed25519(pub: bytes, msg: bytes, signature: bytes) -> None:
    """Strict Ed25519 verification (spec §15.2). Raises SigError on anything outside the profile."""
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != 32:
        raise SigError("an Ed25519 public key is 32 bytes")
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        raise SigError("an Ed25519 signature is 64 bytes")
    if not isinstance(msg, (bytes, bytearray)):
        raise SigError("the signed message must be bytes")
    pub, signature = bytes(pub), bytes(signature)
    if int.from_bytes(signature[32:], "little") >= _L:
        raise SigError("non-canonical signature: S must be below the group order L")
    for label, enc in (("public key", pub), ("signature R", signature[:32])):
        point = _decode_point(enc)
        if point is None:
            raise SigError(f"the {label} isn't a canonical point encoding")
        if not _prime_order(point):
            raise SigError(f"the {label} isn't a point of prime order (small-order or mixed-order)")
    e = ed25519()
    try:
        e.Ed25519PublicKey.from_public_bytes(pub).verify(signature, bytes(msg))
    except e.InvalidSignature:
        raise SigError("bad signature") from None
