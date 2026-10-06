"""age v1 (age-encryption.org/v1), binary format, X25519 recipients only (spec v0.3 §15.5).

Hand-written on `cryptography` so roaming adds no runtime dependency and runs
the same on Python 3.9 everywhere, including the cloud runtime. It is held to
the official C2SP/CCTV age vectors and to interop with stock `age` both ways.

Stricter than general age readers, on purpose:
- Only X25519 stanzas. Any other stanza type (scrypt, hybrid, plugin, grease)
  makes the whole file a header failure. Armor isn't supported.
- Base64 must be canonical, body lines are 64 columns except a shorter final
  line, and an all-zero X25519 shared secret is refused.
- The payload is released only when every chunk authenticates through the
  final one: truncation, a missing or empty non-first final chunk, a chunk out
  of place and trailing bytes all fail, and nothing partial is returned.

Errors follow the CCTV categories: HeaderError, NoMatchError, MacError and
PayloadError, all subclasses of AgeError.
"""
from __future__ import annotations

import base64
import hmac as std_hmac
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import bech32
from ._deps import crypto

VERSION_LINE = b"age-encryption.org/v1"
X25519_LABEL = b"age-encryption.org/v1/X25519"
CHUNK_SIZE = 64 * 1024
TAG_SIZE = 16
NONCE_SIZE = 16
FILE_KEY_SIZE = 16
COLUMNS = 64
MAX_STANZAS = 1024  # stock age refuses more, so we never write or accept more
_ZERO_NONCE = b"\x00" * 12
_B64_CHARS = re.compile(rb"[A-Za-z0-9+/]*")
_RECIPIENT_HRP = "age"
_IDENTITY_HRP = "age-secret-key-"


class AgeError(ValueError):
    """An age file can't be produced or opened."""


class HeaderError(AgeError):
    """The header doesn't parse, or uses something outside our X25519-only profile."""


class NoMatchError(AgeError):
    """No identity unwraps any recipient stanza."""


class MacError(AgeError):
    """A file key was unwrapped but the header MAC doesn't match."""


class PayloadError(AgeError):
    """The payload doesn't authenticate all the way to its final chunk."""


# ── Encoding helpers ──

def _as_bytes(data: object) -> bytes:
    if isinstance(data, str):
        raise AgeError("age data must be bytes, not str")
    try:
        return bytes(data)  # type: ignore[call-overload]
    except TypeError:
        raise AgeError("age data must be bytes-like") from None


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def _unb64(s: bytes) -> bytes:
    if not _B64_CHARS.fullmatch(s) or len(s) % 4 == 1:
        raise HeaderError("invalid base64")
    data = base64.b64decode(s + b"=" * (-len(s) % 4), validate=True)
    if _b64(data) != s:
        raise HeaderError("non-canonical base64")
    return data


def _vchar(arg: bytes) -> bool:
    return bool(arg) and all(33 <= b <= 126 for b in arg)


def _hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    c = crypto()
    return c.HKDF(algorithm=c.hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)


def _hmac(key: bytes, msg: bytes) -> bytes:
    c = crypto()
    h = c.hmac.HMAC(key, c.hashes.SHA256())
    h.update(msg)
    return h.finalize()


def _stream_nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _x25519(secret: bytes, public: bytes) -> bytes:
    c = crypto()
    try:
        shared = c.X25519PrivateKey.from_private_bytes(secret).exchange(c.X25519PublicKey.from_public_bytes(public))
    except ValueError:  # OpenSSL refuses a low-order point
        shared = b"\x00" * 32
    if shared == b"\x00" * 32:
        raise HeaderError("X25519 share is a low-order point (all-zero shared secret)")
    return shared


# ── Keys ──

@dataclass(frozen=True)
class Recipient:
    public: bytes

    @classmethod
    def from_string(cls, s: str) -> "Recipient":
        if not isinstance(s, str) or s != s.lower():
            raise AgeError("an age recipient is a lowercase age1… string")
        try:
            hrp, data = bech32.decode(s)
        except bech32.Bech32Error as exc:
            raise AgeError(f"invalid age recipient: {exc}") from None
        if hrp != _RECIPIENT_HRP or len(data) != 32:
            raise AgeError("not an X25519 age recipient")
        return cls(data)

    def to_string(self) -> str:
        return bech32.encode(_RECIPIENT_HRP, self.public)


@dataclass(frozen=True)
class Identity:
    secret: bytes = field(repr=False)

    @classmethod
    def from_string(cls, s: str) -> "Identity":
        if not isinstance(s, str) or s != s.upper():
            raise AgeError("an age identity is an uppercase AGE-SECRET-KEY-1… string")
        try:
            hrp, data = bech32.decode(s)
        except bech32.Bech32Error as exc:
            raise AgeError(f"invalid age identity: {exc}") from None
        if hrp != _IDENTITY_HRP or len(data) != 32:
            raise AgeError("not an X25519 age identity")
        return cls(data)

    def to_string(self) -> str:
        return bech32.encode(_IDENTITY_HRP, self.secret).upper()

    def recipient(self) -> Recipient:
        key = crypto().X25519PrivateKey.from_private_bytes(self.secret)
        return Recipient(key.public_key().public_bytes_raw())


def generate_identity(rng: Callable[[int], bytes] = os.urandom) -> Identity:
    return Identity(rng(32))


# ── Encrypt ──

def _body_lines(body: bytes) -> bytes:
    text = _b64(body)
    lines = [text[i:i + COLUMNS] for i in range(0, len(text), COLUMNS)]
    if len(text) % COLUMNS == 0:
        lines.append(b"")  # the final line must be shorter than 64 columns
    return b"\n".join(lines) + b"\n"


def encrypt(plaintext: bytes, recipients: Sequence[Recipient], *,
            rng: Callable[[int], bytes] = os.urandom) -> bytes:
    """Encrypt to X25519 recipients. `rng` is injectable for reproducible tests only."""
    if not recipients:
        raise AgeError("encrypt needs at least one recipient")
    if len(recipients) > MAX_STANZAS:
        raise AgeError(f"at most {MAX_STANZAS} recipients, so stock age can always open the file")
    c = crypto()
    file_key = rng(FILE_KEY_SIZE)
    stanzas = []
    for r in recipients:
        if not isinstance(r, Recipient):
            raise AgeError("recipients must be Recipient objects")
        eph = rng(32)
        share = c.X25519PrivateKey.from_private_bytes(eph).public_key().public_bytes_raw()
        try:
            shared = _x25519(eph, r.public)
        except HeaderError as exc:
            raise AgeError(f"refusing recipient: {exc}") from None
        wrap_key = _hkdf(shared, share + r.public, X25519_LABEL)
        body = c.ChaCha20Poly1305(wrap_key).encrypt(_ZERO_NONCE, file_key, None)
        stanzas.append(b"-> X25519 " + _b64(share) + b"\n" + _body_lines(body))
    header = VERSION_LINE + b"\n" + b"".join(stanzas) + b"---"
    mac = _hmac(_hkdf(file_key, b"", b"header"), header)
    nonce = rng(NONCE_SIZE)
    aead = c.ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    chunks = [plaintext[i:i + CHUNK_SIZE] for i in range(0, len(plaintext), CHUNK_SIZE)] or [b""]
    payload = b"".join(aead.encrypt(_stream_nonce(i, i == len(chunks) - 1), chunk, None)
                       for i, chunk in enumerate(chunks))
    return header + b" " + _b64(mac) + b"\n" + nonce + payload


# ── Decrypt ──

def _parse_header(data: bytes) -> tuple[list[tuple[list[bytes], bytes]], bytes, bytes, int]:
    """Return (stanzas, mac, MAC-covered header bytes, payload offset)."""
    pos = 0

    def readline() -> bytes:
        nonlocal pos
        end = data.find(b"\n", pos)
        if end < 0:
            raise HeaderError("unexpected end of header")
        line = data[pos:end]
        pos = end + 1
        return line

    if readline() != VERSION_LINE:
        raise HeaderError("not an age v1 file")
    stanzas: list[tuple[list[bytes], bytes]] = []
    while True:
        line_start = pos
        line = readline()
        if line.startswith(b"---"):
            if not line.startswith(b"--- "):
                raise HeaderError("malformed MAC line")
            mac = _unb64(line[4:])
            if len(mac) != 32:
                raise HeaderError("malformed MAC")
            if not stanzas:
                raise HeaderError("no recipient stanzas")
            return stanzas, mac, data[:line_start + 3], pos
        if not line.startswith(b"-> "):
            raise HeaderError("malformed stanza line")
        args = line[3:].split(b" ")
        if not all(_vchar(a) for a in args):
            raise HeaderError("invalid stanza argument")
        body_lines = []
        while True:
            body_line = readline()
            if len(body_line) > COLUMNS or not _B64_CHARS.fullmatch(body_line):
                raise HeaderError("malformed stanza body")
            body_lines.append(body_line)
            if len(body_line) < COLUMNS:
                break
        stanzas.append((args, _unb64(b"".join(body_lines))))
        if len(stanzas) > MAX_STANZAS:
            raise HeaderError(f"more than {MAX_STANZAS} recipient stanzas (stock age refuses these too)")


def _x25519_stanzas(stanzas: list[tuple[list[bytes], bytes]]) -> list[tuple[bytes, bytes]]:
    out = []
    for args, body in stanzas:
        if args[0] != b"X25519":
            raise HeaderError(f"unsupported stanza type {args[0][:32]!r}; this profile accepts X25519 only")
        if len(args) != 2:
            raise HeaderError("X25519 stanza needs exactly one argument")
        share = _unb64(args[1])
        if len(share) != 32 or len(body) != FILE_KEY_SIZE + TAG_SIZE:
            raise HeaderError("malformed X25519 stanza")
        out.append((share, body))
    return out


def stanza_count(data: bytes) -> int:
    """Number of recipient stanzas in the header (checked against the audience, §15.5)."""
    return len(_parse_header(_as_bytes(data))[0])


def decrypt(data: bytes, identities: Sequence[Identity], *, expected_stanzas: int | None = None) -> bytes:
    """Decrypt an age v1 file, returning the plaintext only if it authenticates in full.
    `expected_stanzas` (the audience size, §15.5) is checked before any key work."""
    c = crypto()
    data = _as_bytes(data)
    stanzas, mac, covered, offset = _parse_header(data)
    if expected_stanzas is not None and len(stanzas) != expected_stanzas:
        raise HeaderError(f"header has {len(stanzas)} recipient stanzas, expected {expected_stanzas}")
    x_stanzas = _x25519_stanzas(stanzas)
    file_key = None
    for ident in identities:
        own = ident.recipient().public
        for share, body in x_stanzas:
            shared = _x25519(ident.secret, share)
            wrap_key = _hkdf(shared, share + own, X25519_LABEL)
            try:
                file_key = c.ChaCha20Poly1305(wrap_key).decrypt(_ZERO_NONCE, body, None)
            except c.InvalidTag:
                continue
            break
        if file_key is not None:
            break
    if file_key is None:
        raise NoMatchError("no identity matches a recipient stanza")
    if not std_hmac.compare_digest(_hmac(_hkdf(file_key, b"", b"header"), covered), mac):
        raise MacError("header MAC doesn't match")

    nonce = data[offset:offset + NONCE_SIZE]
    if len(nonce) < NONCE_SIZE:
        raise HeaderError("missing payload nonce")  # age counts the nonce as part of the header
    aead = c.ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    ct = data[offset + NONCE_SIZE:]
    if not ct:
        raise PayloadError("no payload chunks")
    out, counter, pos, step = [], 0, 0, CHUNK_SIZE + TAG_SIZE
    while True:
        chunk = ct[pos:pos + step]
        pos += len(chunk)
        last = pos >= len(ct)
        if len(chunk) < TAG_SIZE:
            raise PayloadError("chunk too short")
        try:
            plain = aead.decrypt(_stream_nonce(counter, last), chunk, None)
        except c.InvalidTag:
            raise PayloadError("payload chunk doesn't authenticate (tampered, truncated, "
                               "out of place or followed by extra bytes)") from None
        if last and not plain and counter > 0:
            raise PayloadError("final chunk is empty")
        out.append(plain)
        if last:
            return b"".join(out)
        counter += 1
