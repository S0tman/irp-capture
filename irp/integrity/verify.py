"""Offline snapshot verification.

Recomputes every digest from the supplied ledger and compares against the
snapshot manifest. Reports each property separately and never collapses to a
single "trusted" flag. For PR2a (no attestation), it states plainly that the
snapshot is unattested: it proves the supplied ledger matches the snapshot, not
that this was the ledger state at any past time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import SCHEMA_VERSION
from .canonical import canonicalize, digest_canonical, sha256_hex
from .errors import SnapshotFormatError
from .strict import parse_ledger_strict


def _check(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"check": name, "pass": passed, "detail": detail}


def verify_snapshot(snapshot_path: Path, ledger_path: Path) -> dict[str, Any]:
    """Verify a ledger against a snapshot file. Returns a granular result dict."""
    try:
        snapshot_file = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SnapshotFormatError(f"could not read snapshot file: {exc}")

    manifest = snapshot_file.get("manifest")
    stored = (snapshot_file.get("snapshot_digest") or {}).get("value")
    if not isinstance(manifest, dict) or not stored:
        raise SnapshotFormatError("snapshot file missing 'manifest' or 'snapshot_digest'")

    schema = manifest.get("schema")
    if schema != SCHEMA_VERSION:
        raise SnapshotFormatError(
            f"unknown snapshot schema {schema!r} (this build verifies {SCHEMA_VERSION!r})"
        )

    checks: list[dict[str, Any]] = []

    # 1. Manifest integrity: the body must hash to the stored snapshot_digest.
    recomputed = sha256_hex(canonicalize(manifest))
    checks.append(_check(
        "snapshot manifest digest",
        recomputed == stored,
        "" if recomputed == stored else "manifest has been altered",
    ))

    ledger_section = manifest.get("ledger", {})

    # Read the ledger once.
    raw = ledger_path.read_bytes() if ledger_path.exists() else b""
    decode_ok = True
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        decode_ok = False
        checks.append(_check("ledger parse", False, f"invalid UTF-8: {exc}"))

    parsed: Optional[Any] = None
    if decode_ok:
        parsed = parse_ledger_strict(text)
        checks.append(_check(
            "ledger parse",
            parsed.ok,
            "ok" if parsed.ok else f"{len(parsed.errors)} malformed line(s)",
        ))

    # 2. Byte digest (always computable from raw bytes).
    byte_expected = (ledger_section.get("byte_digest") or {}).get("value")
    checks.append(_check("byte digest", sha256_hex(raw) == byte_expected))

    # 3. Semantic digest, count, head id (need a successful parse).
    if parsed is not None:
        sem_expected = (ledger_section.get("semantic_digest") or {}).get("value")
        checks.append(_check("semantic digest", digest_canonical(parsed.entries) == sem_expected))

        count_expected = ledger_section.get("entry_count")
        checks.append(_check(
            "entry count",
            len(parsed.entries) == count_expected,
            f"expected {count_expected}, got {len(parsed.entries)}",
        ))

        head_expected = ledger_section.get("head_entry_id")
        head_actual = parsed.entries[-1].get("id") if parsed.entries else None
        checks.append(_check("head entry id", head_actual == head_expected))

    all_pass = all(c["pass"] for c in checks)

    # The honest unattested layer. PR2b flips these where it can.
    attestation = {
        "external_witness": "NOT PRESENT",
        "historical_existence": "NOT PROVEN",
        "backdating_resistance": "NOT PRESENT",
        "completeness": "NOT GUARANTEED",
        "underlying_claim_truth": "NOT ASSESSED",
    }

    return {
        "ok": all_pass,
        "checks": checks,
        "attestation": attestation,
        "snapshot_id": manifest.get("snapshot_id"),
        "ledger_id": manifest.get("ledger_id"),
    }
