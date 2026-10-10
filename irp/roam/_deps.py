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


def ec() -> SimpleNamespace:
    """ECDSA P-256, for hardware-key and passkey assertions."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.ec import (
            ECDSA,
            SECP256R1,
            EllipticCurvePublicKey,
        )
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RoamDependencyError(
            "Roaming IRP needs the 'cryptography' package. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return SimpleNamespace(InvalidSignature=InvalidSignature, hashes=hashes, ECDSA=ECDSA, SECP256R1=SECP256R1,
                           EllipticCurvePublicKey=EllipticCurvePublicKey,
                           decode_dss_signature=decode_dss_signature, encode_dss_signature=encode_dss_signature)


def tsa() -> SimpleNamespace:
    """`asn1crypto` and the `cryptography` pieces the TSA client and its token checks need (step 2.6)."""
    try:
        from asn1crypto import algos, cms, core, keys, tsp
        from asn1crypto import x509 as asn1_x509
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives import padding as sym_padding
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RoamDependencyError(
            "Roaming IRP's TSA client needs the 'asn1crypto' and 'cryptography' packages. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return SimpleNamespace(algos=algos, cms=cms, core=core, keys=keys, tsp=tsp, asn1_x509=asn1_x509, x509=x509,
                           InvalidSignature=InvalidSignature, hashes=hashes, serialization=serialization,
                           sym_padding=sym_padding, ec=ec, padding=padding, rsa=rsa, Cipher=Cipher,
                           algorithms=algorithms, modes=modes, PBKDF2HMAC=PBKDF2HMAC)
