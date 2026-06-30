"""Attestation: anchor a snapshot to an external RFC 3161 timestamp authority.

`create_attestation` is an explicit network operation: it sends only the
snapshot digest to a TSA and stores a detached receipt. `verify_attestation`
is offline (given the stored token): it recomputes the manifest digest, then
checks that an external token binds exactly that digest and is cryptographically
valid. It reports TSA-trust honestly: a valid signature is not the same as a
validated certificate path to a trust root the verifier accepts.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .canonical import canonicalize, sha256_hex
from .errors import IntegrityError, SnapshotFormatError
from .rfc3161 import read_tst_info, request_timestamp, verify_token

DEFAULT_TSA = "https://freetsa.org/tsr"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_snapshot(snapshot_path: Path) -> tuple[dict[str, Any], str]:
    try:
        snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SnapshotFormatError(f"could not read snapshot file: {exc}")
    manifest = snap.get("manifest")
    stored = (snap.get("snapshot_digest") or {}).get("value")
    if not isinstance(manifest, dict) or not stored:
        raise SnapshotFormatError("snapshot file missing 'manifest' or 'snapshot_digest'")
    return snap, stored


def _receipts_dir(irp_dir: Path) -> Path:
    d = irp_dir / "integrity" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_attestation(
    irp_dir: Path,
    snapshot_path: Path,
    *,
    tsa_url: str = DEFAULT_TSA,
    timeout: int = 20,
) -> dict[str, Any]:
    """Anchor a snapshot's digest to a TSA and store a detached receipt."""
    snap, stored_digest = _load_snapshot(snapshot_path)
    snapshot_id = snap["manifest"].get("snapshot_id", snapshot_path.stem)
    digest_bytes = bytes.fromhex(stored_digest)

    token_der = request_timestamp(digest_bytes, tsa_url, timeout=timeout)
    info = read_tst_info(token_der)
    if info["hashed_message"] != digest_bytes:
        raise IntegrityError("TSA returned a token for a different digest")

    receipts = _receipts_dir(irp_dir)
    token_path = receipts / f"{snapshot_id}.tsr"
    token_path.write_bytes(token_der)

    receipt = {
        "schema": "irp-attestation-receipt/0.1",
        "snapshot_id": snapshot_id,
        "anchored_digest": {"alg": "sha-256", "value": stored_digest},
        "attestation_type": "rfc3161",
        "tsa_url": tsa_url,
        "token_file": token_path.name,
        "token_sha256": sha256_hex(token_der),
        "gen_time": info["gen_time"].isoformat() if hasattr(info["gen_time"], "isoformat") else str(info["gen_time"]),
        "accuracy_seconds": info["accuracy"],
        "policy": info["policy"],
        "serial_number": str(info["serial_number"]),
        "hash_alg": info["hash_alg"],
        "captured_at": _utcnow_iso(),
        "note": "An external timestamp proves existence-by-time only. It does not "
                "prove completeness, authorship, or truth. TSA trust-root validation "
                "is the verifier's policy.",
    }
    receipt_path = receipts / f"{snapshot_id}.receipt.json"
    tmp = receipt_path.with_name(receipt_path.name + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(receipt_path)

    return {
        "snapshot_id": snapshot_id,
        "token_path": str(token_path),
        "receipt_path": str(receipt_path),
        "gen_time": receipt["gen_time"],
        "accuracy_seconds": info["accuracy"],
        "tsa_url": tsa_url,
        "signer_subject": None,
    }


def find_receipt_token(irp_dir: Path, snapshot_id: str) -> Optional[Path]:
    token = irp_dir / "integrity" / "receipts" / f"{snapshot_id}.tsr"
    return token if token.exists() else None


def verify_attestation(snapshot_path: Path, token_path: Path) -> dict[str, Any]:
    """Verify an external timestamp receipt against a snapshot. Offline."""
    snap, stored_digest = _load_snapshot(snapshot_path)
    manifest = snap["manifest"]

    # Chain link 1: the manifest still hashes to the stored snapshot_digest.
    recomputed = sha256_hex(canonicalize(manifest))
    manifest_ok = recomputed == stored_digest

    # Chain link 2: an external token binds exactly that digest and is valid.
    token_der = token_path.read_bytes()
    v = verify_token(token_der, bytes.fromhex(stored_digest))

    witnessed = bool(manifest_ok and v["cryptographically_valid"])
    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "manifest_binds_digest": manifest_ok,
        "token": v,
        "externally_witnessed": witnessed,
    }
