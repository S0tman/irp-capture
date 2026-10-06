"""Bech32 (BIP173), as age uses it for recipients (age1…) and identities (AGE-SECRET-KEY-1…).

Strict: mixed case, characters outside the charset, a bad checksum and the
bech32m variant (BIP350) are all rejected. age sets no length limit, so the
BIP173 90-character limit applies only when a caller asks for it.
"""
from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_BECH32_CONST = 1


class Bech32Error(ValueError):
    """The string isn't valid bech32."""


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> list[int]:
    acc = bits = 0
    out: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise Bech32Error("value out of range")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or (acc << (tobits - bits)) & maxv:
        raise Bech32Error("invalid padding")
    return out


def decode_raw(s: str, *, limit: int | None = None) -> tuple[str, list[int]]:
    """Return (lowercase hrp, 5-bit data words without the checksum)."""
    if not isinstance(s, str):
        raise Bech32Error("bech32 input must be a string")
    if limit is not None and len(s) > limit:
        raise Bech32Error("string too long")
    if any(ord(c) < 33 or ord(c) > 126 for c in s):
        raise Bech32Error("character out of range")
    if s.lower() != s and s.upper() != s:
        raise Bech32Error("mixed case")
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1:
        raise Bech32Error("missing separator or empty human-readable part")
    if pos + 7 > len(s):
        raise Bech32Error("checksum too short")
    hrp = s[:pos]
    try:
        data = [CHARSET.index(c) for c in s[pos + 1:]]
    except ValueError:
        raise Bech32Error("invalid data character") from None
    if _polymod(_hrp_expand(hrp) + data) != _BECH32_CONST:
        raise Bech32Error("invalid checksum (bech32m and other variants are rejected)")
    return hrp, data[:-6]


def decode(s: str, *, limit: int | None = None) -> tuple[str, bytes]:
    """Return (lowercase hrp, payload bytes)."""
    hrp, data = decode_raw(s, limit=limit)
    return hrp, bytes(_convertbits(data, 5, 8, False))


def encode(hrp: str, data: bytes) -> str:
    """Lowercase bech32 string for hrp and data (callers uppercase it when the format asks)."""
    hrp = hrp.lower()
    if not hrp or any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        raise Bech32Error("invalid human-readable part")
    words = _convertbits(list(data), 8, 5, True)
    pm = _polymod(_hrp_expand(hrp) + words + [0] * 6) ^ _BECH32_CONST
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in words + checksum)
