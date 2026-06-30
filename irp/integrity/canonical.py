"""Canonicalisation and digests.

Canonicalisation uses RFC 8785 (JSON Canonicalization Scheme) via the `rfc8785`
library. We do not implement JCS ourselves: correct number serialisation and
string handling are subtle, and a vetted implementation is the safer choice for
a security tool.

Important: RFC 8785 does NOT normalise Unicode. Strings are preserved as-is.
Do not add NFC/NFD normalisation here; it would make the output non-conformant
and could collapse two intentionally distinct strings.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .errors import IntegrityDependencyError


def _jcs():
    """Return the rfc8785 module, with a clear message if it is not installed."""
    try:
        import rfc8785  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via integration
        raise IntegrityDependencyError(
            "Integrity features need the RFC 8785 canonicaliser. Install with: "
            "pip install 'irp-capture[integrity]'  (or: pip install rfc8785)"
        ) from exc
    return rfc8785


def canonicalize(obj: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes for a JSON value."""
    return _jcs().dumps(obj)


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def digest_canonical(obj: Any) -> str:
    """SHA-256 hex digest over the JCS canonical form of a JSON value."""
    return sha256_hex(canonicalize(obj))
