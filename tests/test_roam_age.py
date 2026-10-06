"""Roaming IRP, Cut 1 step 2.2: bech32 and age v1 (spec v0.3 §15.5).

The envelope encrypts every relay object with age v1, X25519 recipients only.
The implementation is hand-written on `cryptography` (no new runtime
dependency), so it is held to the official C2SP/CCTV age test vectors and to
interop with the stock `age` tool in both directions.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")

from irp.roam import bech32  # noqa: E402
from irp.roam.age import (  # noqa: E402
    CHUNK_SIZE,
    AgeError,
    HeaderError,
    Identity,
    MacError,
    NoMatchError,
    PayloadError,
    Recipient,
    decrypt,
    encrypt,
    generate_identity,
    stanza_count,
)

TESTKIT = ROOT / "tests" / "fixtures" / "roam" / "age-testkit"
STOCK_AGE = shutil.which("age")


def _rng(seed: bytes):
    """Deterministic randomness for reproducible tests."""
    state = {"n": 0}

    def rng(n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += hashlib.sha256(seed + state["n"].to_bytes(8, "big")).digest()
            state["n"] += 1
        return out[:n]

    return rng


# ── bech32 (BIP173) ──

BIP173_VALID = [
    "A12UEL5L",
    "a12uel5l",
    "an83characterlonghumanreadablepartthatcontainsthenumber1andtheexcludedcharactersbio1tt5tgs",
    "abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw",
    "11" + "q" * 82 + "c8247j",  # 90 characters
    "split1checkupstagehandshakeupstreamerranterredcaperred2y9e3w",
    "?1ezyfcl",
]
BIP173_INVALID = [
    "\x201nwldj5",
    "\x7f1axkwrx",
    "\x801eym55h",
    "an84characterslonghumanreadablepartthatcontainsthenumber1andtheexcludedcharactersbio1569pvx",
    "pzry9x0s0muk",
    "1pzry9x0s0muk",
    "x1b4n0q5v",
    "li1dgmt3",
    "de1lg7wt\xff",
    "A1G7SGD8",
    "10a06t8",
    "1qzzfhee",
]


@pytest.mark.parametrize("s", BIP173_VALID)
def test_bip173_valid(s):
    hrp, _ = bech32.decode_raw(s, limit=90)
    assert hrp == s[: s.rfind("1")].lower()


@pytest.mark.parametrize("s", BIP173_INVALID)
def test_bip173_invalid(s):
    with pytest.raises(bech32.Bech32Error):
        bech32.decode_raw(s, limit=90)


def test_bech32m_is_rejected():
    with pytest.raises(bech32.Bech32Error):
        bech32.decode_raw("a1lqfn3a")  # valid bech32m (BIP350), not bech32


def test_bech32_round_trip_and_tamper():
    data = bytes(range(32))
    s = bech32.encode("age", data)
    assert s.startswith("age1") and bech32.decode(s) == ("age", data)
    flipped = s[:-5] + ("q" if s[-5] != "q" else "p") + s[-4:]
    with pytest.raises(bech32.Bech32Error):
        bech32.decode(flipped)
    with pytest.raises(bech32.Bech32Error):
        bech32.decode(s[:6] + s[6:].upper())  # mixed case


# ── Keys ──

def test_identity_and_recipient_strings():
    ident = generate_identity(rng=_rng(b"k1"))
    s = ident.to_string()
    assert s.startswith("AGE-SECRET-KEY-1") and s == s.upper()
    assert Identity.from_string(s) == ident
    r = ident.recipient()
    assert r.to_string().startswith("age1")
    assert Recipient.from_string(r.to_string()) == r
    with pytest.raises(AgeError):
        Identity.from_string(r.to_string())
    with pytest.raises(AgeError):
        Recipient.from_string(s)


# ── Round trips ──

@pytest.mark.parametrize("size", [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE])
@pytest.mark.parametrize("n_recipients", [1, 3])
def test_round_trip(size, n_recipients):
    idents = [generate_identity(rng=_rng(b"id%d" % i)) for i in range(n_recipients)]
    plaintext = _rng(b"pt")(size)
    ct = encrypt(plaintext, [i.recipient() for i in idents], rng=_rng(b"enc"))
    assert stanza_count(ct) == n_recipients
    for ident in idents:
        assert decrypt(ct, [ident]) == plaintext


def test_encryption_is_deterministic_only_with_the_same_randomness():
    ident = generate_identity(rng=_rng(b"k"))
    a = encrypt(b"x", [ident.recipient()], rng=_rng(b"same"))
    b = encrypt(b"x", [ident.recipient()], rng=_rng(b"same"))
    c = encrypt(b"x", [ident.recipient()], rng=_rng(b"other"))
    assert a == b and a != c


def test_encrypt_needs_a_recipient():
    with pytest.raises(AgeError):
        encrypt(b"x", [])


# ── Malformed inputs (spec §15.5) ──

def _sample(size=10, seed=b"s"):
    ident = generate_identity(rng=_rng(seed + b"i"))
    return ident, encrypt(_rng(b"p")(size), [ident.recipient()], rng=_rng(seed + b"e"))


def _split(ct):
    i = ct.index(b"\n---")
    j = ct.index(b"\n", i + 1) + 1
    return ct[:j], ct[j:]


def test_wrong_identity_is_no_match():
    _, ct = _sample()
    with pytest.raises(NoMatchError):
        decrypt(ct, [generate_identity(rng=_rng(b"other"))])


def test_tampered_mac():
    ident, ct = _sample()
    header, payload = _split(ct)
    mac_at = header.rindex(b" ") + 1
    bad = header[:mac_at] + (b"A" if header[mac_at:mac_at + 1] != b"A" else b"B") + header[mac_at + 1:]
    with pytest.raises(MacError):
        decrypt(bad + payload, [ident])


def test_flipped_payload_bit():
    ident, ct = _sample()
    bad = ct[:-1] + bytes([ct[-1] ^ 1])
    with pytest.raises(PayloadError):
        decrypt(bad, [ident])


def test_truncated_and_dropped_chunks():
    ident, ct = _sample(size=2 * CHUNK_SIZE + 10)
    header, payload = _split(ct)
    full = CHUNK_SIZE + 16
    with pytest.raises(PayloadError):
        decrypt(ct[:-5], [ident])  # truncated last chunk
    with pytest.raises(PayloadError):
        decrypt(header + payload[: 16 + 2 * full], [ident])  # final chunk dropped: full chunk, no last flag
    with pytest.raises(PayloadError):
        decrypt(header + payload[:16] + payload[16 + full:], [ident])  # middle chunk dropped


def test_trailing_bytes_are_rejected():
    ident, ct = _sample()
    with pytest.raises(PayloadError):
        decrypt(ct + b"\x00", [ident])


def test_a_tag_after_the_final_chunk_is_rejected():
    ident, ct = _sample(size=CHUNK_SIZE)
    header, payload = _split(ct)
    # Re-encrypt with an extra empty final chunk is impossible without the key; instead feed a lone tag.
    with pytest.raises(PayloadError):
        decrypt(header + payload + b"\x00" * 16, [ident])


def test_all_zero_share_is_rejected():
    ident, ct = _sample()
    lines = ct.split(b"\n")
    i = next(n for n, l in enumerate(lines) if l.startswith(b"-> X25519 "))
    lines[i] = b"-> X25519 " + b"A" * 43  # base64 of 32 zero bytes (canonical: 'A' * 42 + 'A')
    with pytest.raises(HeaderError, match="low-order"):
        decrypt(b"\n".join(lines), [ident])


def test_non_canonical_base64_is_rejected():
    ident, ct = _sample()
    lines = ct.split(b"\n")
    i = next(n for n, l in enumerate(lines) if l.startswith(b"-> X25519 ")) + 1
    body = lines[i]
    last = body[-1:]
    # Flip the unused low bits of the last base64 character: decodes to the same bytes, but isn't canonical.
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    v = alphabet.index(last)
    lines[i] = body[:-1] + alphabet[v ^ 1: (v ^ 1) + 1]
    with pytest.raises(HeaderError):
        decrypt(b"\n".join(lines), [ident])


def test_a_full_final_body_line_is_rejected():
    ident, ct = _sample()
    lines = ct.split(b"\n")
    i = next(n for n, l in enumerate(lines) if l.startswith(b"-> X25519 ")) + 1
    lines[i] = b"A" * 64  # a 64-column body line must be followed by a shorter final line
    with pytest.raises(HeaderError, match="malformed stanza body"):
        stanza_count(b"\n".join(lines))
    with pytest.raises(HeaderError, match="malformed stanza body"):
        decrypt(b"\n".join(lines), [ident])


def test_a_non_x25519_stanza_is_rejected():
    ident, ct = _sample()
    header, payload = _split(ct)
    extra = b"-> grease-x foo\nAAAA\n"
    bad = header.replace(b"\n-> X25519", b"\n" + extra.rstrip(b"\n") + b"\n-> X25519", 1)
    with pytest.raises(HeaderError):
        decrypt(bad + payload, [ident])


def test_garbage_and_wrong_version():
    ident, _ = _sample()
    for bad in (b"", b"age-encryption.org/v2\n", b"not age at all"):
        with pytest.raises(HeaderError):
            decrypt(bad, [ident])


# ── Official C2SP/CCTV vectors ──

def _vector_files():
    return sorted(p for p in TESTKIT.iterdir() if p.is_file() and not p.name.startswith("."))


def _vectors():
    for path in _vector_files():
        raw = path.read_bytes()
        head, _, body = raw.partition(b"\n\n")
        meta: dict[str, list[str]] = {}
        for line in head.decode().splitlines():
            k, _, v = line.partition(": ")
            meta.setdefault(k, []).append(v)
        if meta.get("compressed") == ["zlib"]:
            body = zlib.decompress(body)
        yield path.name, meta, body


def _only_x25519(body: bytes) -> bool:
    header = body.split(b"\n---", 1)[0]
    stanzas = [l for l in header.split(b"\n") if l.startswith(b"->")]
    return bool(stanzas) and all(l.split(b" ")[1:2] == [b"X25519"] for l in stanzas)


EXPECT = {"no match": NoMatchError, "HMAC failure": MacError, "header failure": HeaderError,
          "payload failure": PayloadError}


@pytest.mark.parametrize("name,meta,body", list(_vectors()), ids=lambda v: v if isinstance(v, str) else "")
def test_cctv_vector(name, meta, body):
    expect = meta["expect"][0]
    try:
        identities = [Identity.from_string(s) for s in meta.get("identity", [])]
    except AgeError:
        identities = None
    supported = (identities is not None and "passphrase" not in meta and meta.get("armored") != ["yes"]
                 and _only_x25519(body))
    if not supported:
        # Outside our profile (armor, passphrase, hybrid, non-X25519 or grease stanzas): must fail closed.
        with pytest.raises(AgeError):
            decrypt(body, identities or [generate_identity(rng=_rng(b"x"))])
        return
    if expect == "success":
        assert hashlib.sha256(decrypt(body, identities)).hexdigest() == meta["payload"][0]
    else:
        with pytest.raises(EXPECT[expect]):
            decrypt(body, identities)


def test_vectors_are_present():
    assert len(_vector_files()) == 147


# ── Interop with the stock age tool, both ways ──

REQUIRE_STOCK_AGE = os.environ.get("IRP_REQUIRE_STOCK_AGE") == "1"
needs_age = pytest.mark.skipif(STOCK_AGE is None and not REQUIRE_STOCK_AGE, reason="stock age not installed")
needs_keygen = pytest.mark.skipif(shutil.which("age-keygen") is None and not REQUIRE_STOCK_AGE,
                                  reason="age-keygen not installed")


@needs_age
@pytest.mark.parametrize("size", [0, 5, CHUNK_SIZE, CHUNK_SIZE + 7])
def test_stock_age_decrypts_ours(tmp_path, size):
    ident = generate_identity()
    key = tmp_path / "key.txt"
    key.write_text(ident.to_string() + "\n")
    plaintext = os.urandom(size)
    (tmp_path / "ct.age").write_bytes(encrypt(plaintext, [ident.recipient(), generate_identity().recipient()]))
    out = subprocess.run([STOCK_AGE, "-d", "-i", str(key), str(tmp_path / "ct.age")], capture_output=True, check=True)
    assert out.stdout == plaintext


@needs_age
@pytest.mark.parametrize("size", [0, 5, CHUNK_SIZE, CHUNK_SIZE + 7])
def test_we_decrypt_stock_age(tmp_path, size):
    ident = generate_identity()
    plaintext = os.urandom(size)
    out = subprocess.run([STOCK_AGE, "-r", ident.recipient().to_string(), "-r", generate_identity().recipient().to_string()],
                         input=plaintext, capture_output=True, check=True)
    assert decrypt(out.stdout, [ident]) == plaintext


@needs_keygen
def test_stock_age_keygen_identity_parses(tmp_path):
    out = subprocess.run([shutil.which("age-keygen") or "age-keygen"], capture_output=True, text=True, check=True)
    line = next(l for l in out.stdout.splitlines() if l.startswith("AGE-SECRET-KEY-1"))
    pub = next(l.split(": ")[1] for l in out.stdout.splitlines() + out.stderr.splitlines() if "public key" in l)
    assert Identity.from_string(line).recipient().to_string() == pub


# ── Review round 1 ──

def test_decrypt_tries_every_identity():
    a, b, c = (generate_identity(rng=_rng(b"m%d" % i)) for i in range(3))
    ct = encrypt(b"hello", [b.recipient()], rng=_rng(b"e"))
    assert decrypt(ct, [a, b]) == b"hello" and decrypt(ct, [b, a]) == b"hello"
    ct3 = encrypt(b"three", [a.recipient(), b.recipient(), c.recipient()], rng=_rng(b"e3"))
    assert decrypt(ct3, [generate_identity(rng=_rng(b"u")), c]) == b"three"


LOW_ORDER = [bytes(32), (1).to_bytes(32, "little"),
             bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800")]


@pytest.mark.parametrize("point", LOW_ORDER)
def test_encrypt_refuses_a_low_order_recipient(point):
    with pytest.raises(AgeError, match="low-order"):
        encrypt(b"x", [Recipient(point)])


def test_a_genuine_empty_final_chunk_is_rejected():
    from irp.roam.age import _hkdf, _stream_nonce
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    ident = generate_identity(rng=_rng(b"ef-i"))
    ct = encrypt(b"", [ident.recipient()], rng=_rng(b"ef"))
    r = _rng(b"ef")
    file_key = r(FILE_KEY := 16)
    r(32)  # ephemeral key
    nonce = r(16)
    header, rest = _split(ct)
    assert rest[:16] == nonce
    aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    full = aead.encrypt(_stream_nonce(0, False), b"\x00" * CHUNK_SIZE, None)
    empty_final = aead.encrypt(_stream_nonce(1, True), b"", None)
    with pytest.raises(PayloadError, match="final chunk is empty"):
        decrypt(header + nonce + full + empty_final, [ident])


def test_header_without_stanzas_is_a_header_failure():
    bad = b"age-encryption.org/v1\n--- " + b"A" * 43 + b"\n" + b"\x00" * 32
    with pytest.raises(HeaderError, match="no recipient"):
        stanza_count(bad)
    with pytest.raises(HeaderError, match="no recipient"):
        decrypt(bad, [generate_identity()])


def test_base64_with_an_impossible_length_is_a_header_failure():
    ident, ct = _sample()
    lines = ct.split(b"\n")
    i = next(n for n, l in enumerate(lines) if l.startswith(b"-> X25519 "))
    for mutate in (lambda L: L.__setitem__(i, b"-> X25519 " + b"A" * 41),
                   lambda L: L.__setitem__(i + 1, b"A")):
        L = list(lines)
        mutate(L)
        with pytest.raises(HeaderError):
            decrypt(b"\n".join(L), [ident])
    header, payload = _split(ct)
    bad_mac = header[:header.rindex(b" ") + 1] + b"A" * 41 + b"\n"
    with pytest.raises(HeaderError):
        decrypt(bad_mac + payload, [ident])


def _encode_with_padding_bits(hrp, data):
    words = bech32._convertbits(list(data), 8, 5, True)
    words[-1] |= 1  # set an unused padding bit
    pm = bech32._polymod(bech32._hrp_expand(hrp) + words + [0] * 6) ^ 1
    return hrp + "1" + "".join(bech32.CHARSET[d] for d in words + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def test_key_string_validation():
    ident = generate_identity(rng=_rng(b"kv"))
    with pytest.raises(AgeError):
        Recipient.from_string(ident.recipient().to_string().upper())
    with pytest.raises(AgeError):
        Identity.from_string(ident.to_string().lower())
    with pytest.raises(AgeError):
        Recipient.from_string(bech32.encode("age", bytes(31)))
    with pytest.raises(AgeError):
        Identity.from_string(bech32.encode("age-secret-key-", bytes(33)).upper())
    with pytest.raises(AgeError):
        Recipient.from_string(_encode_with_padding_bits("age", ident.recipient().public))


@pytest.mark.parametrize("wrap", [bytearray, memoryview])
def test_bytes_like_input_is_accepted(wrap):
    ident, ct = _sample()
    assert decrypt(wrap(ct), [ident]) == _rng(b"p")(10)
    assert stanza_count(wrap(ct)) == 1


def test_str_input_is_an_age_error():
    with pytest.raises(AgeError):
        decrypt("age-encryption.org/v1", [generate_identity()])
    with pytest.raises(AgeError):
        stanza_count("age-encryption.org/v1")


def test_stanza_cap_matches_stock_age():
    from irp.roam.age import MAX_STANZAS
    assert MAX_STANZAS == 1024
    recipients = [generate_identity(rng=_rng(b"cap%d" % i)).recipient() for i in range(MAX_STANZAS + 1)]
    with pytest.raises(AgeError, match="1024"):
        encrypt(b"x", recipients)
    ident = generate_identity(rng=_rng(b"cap0"))
    ok = encrypt(b"x", recipients[:MAX_STANZAS], rng=_rng(b"c"))
    assert stanza_count(ok) == MAX_STANZAS
    header, payload = _split(ok)
    extra = header.split(b"\n---")[0].split(b"\n", 1)[1].split(b"\n-> ")[0] + b"\n"
    too_many = header.replace(b"\n---", b"\n" + extra.rstrip(b"\n") + b"\n---", 1)
    with pytest.raises(HeaderError, match="1024"):
        stanza_count(too_many)


def test_expected_stanza_count_is_checked_before_unwrapping():
    a, b = generate_identity(rng=_rng(b"x1")), generate_identity(rng=_rng(b"x2"))
    ct = encrypt(b"hi", [a.recipient(), b.recipient()], rng=_rng(b"x"))
    assert decrypt(ct, [a], expected_stanzas=2) == b"hi"
    with pytest.raises(HeaderError, match="expected 3"):
        decrypt(ct, [a], expected_stanzas=3)


GENERIC_GRAMMAR = ("stanza_", "hmac_", "header_", "version_", "empty")


@pytest.mark.parametrize("name,meta,body", [v for v in _vectors() if v[1].get("armored") != ["yes"]],
                         ids=lambda v: v if isinstance(v, str) else "")
def test_cctv_vectors_through_the_header_parser(name, meta, body):
    expect = meta["expect"][0]
    if expect == "header failure" and name.startswith(GENERIC_GRAMMAR):
        with pytest.raises(HeaderError):
            stanza_count(body)
    elif expect in ("success", "no match", "HMAC failure", "payload failure"):
        assert stanza_count(body) >= 1
