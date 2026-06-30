"""Snapshot manifest construction.

The manifest body describes a ledger state. `snapshot_digest` is SHA-256 over
the JCS canonical form of the whole body, so a single timestamp over that digest
(PR2b) binds every field: both ledger digests, the count, the head id, the salt,
the ledger_id, the schema and the tool version.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from . import SCHEMA_VERSION
from .canonical import canonicalize, digest_canonical, sha256_hex

TOOL = "irp-capture"
TOOL_VERSION = "0.7.0"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_snapshot_file(
    *,
    snapshot_id: str,
    ledger_id: str,
    raw_bytes: bytes,
    entries: list[dict[str, Any]],
    previous_snapshot_digest: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build the full snapshot file object: {snapshot_digest, manifest}."""
    byte_digest = sha256_hex(raw_bytes)
    semantic_digest = digest_canonical(entries)   # JCS of the whole ordered array
    head_entry_id = entries[-1].get("id") if entries else None

    manifest: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": created_at or _utcnow_iso(),
        "ledger_id": ledger_id,
        "scope": {"type": "full-ledger"},
        "previous_snapshot_digest": previous_snapshot_digest,
        "snapshot_salt": secrets.token_hex(32),
        "ledger": {
            "entry_count": len(entries),
            "head_entry_id": head_entry_id,
            "byte_digest": {"alg": "sha-256", "value": byte_digest},
            "semantic_digest": {"alg": "sha-256", "canon": "RFC8785", "value": semantic_digest},
        },
        "created_by": {"tool": TOOL, "version": TOOL_VERSION},
    }

    snapshot_digest = sha256_hex(canonicalize(manifest))
    return {
        "snapshot_digest": {"alg": "sha-256", "value": snapshot_digest},
        "manifest": manifest,
    }
