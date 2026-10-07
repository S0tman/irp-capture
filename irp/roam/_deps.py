"""Lazy imports for Roaming IRP's optional dependencies (spec v0.3 §21).

Base capture stays dependency-free on Python 3.9. Roaming uses the same pins as
the [integrity] extra and imports them only when a roaming feature runs.
"""
from __future__ import annotations

from types import SimpleNamespace


class RoamDependencyError(ImportError):
    """A roaming feature needs an optional dependency that isn't installed."""


def crypto() -> SimpleNamespace:
    """The `cryptography` primitives age v1 needs."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, hmac
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RoamDependencyError(
            "Roaming IRP needs the 'cryptography' package. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return SimpleNamespace(InvalidTag=InvalidTag, hashes=hashes, hmac=hmac, X25519PrivateKey=X25519PrivateKey,
                           X25519PublicKey=X25519PublicKey, ChaCha20Poly1305=ChaCha20Poly1305, HKDF=HKDF)


def ed25519() -> SimpleNamespace:
    """The `cryptography` primitives Ed25519 signing needs."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RoamDependencyError(
            "Roaming IRP needs the 'cryptography' package. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return SimpleNamespace(InvalidSignature=InvalidSignature, Ed25519PrivateKey=Ed25519PrivateKey,
                           Ed25519PublicKey=Ed25519PublicKey, Encoding=Encoding, PublicFormat=PublicFormat)


def aesgcm():
    """AES-256-GCM, for the keystore."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RoamDependencyError(
            "Roaming IRP needs the 'cryptography' package. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return AESGCM
