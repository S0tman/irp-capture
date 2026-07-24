"""Fills genuine gaps left by test_integrity.py, test_attest.py, and
test_evidence_attest.py:

  - irp/integrity/manifest.py: build_snapshot_file called directly
  - irp/integrity/snapshot.py: get_or_create_ledger_id persistence, integrity_dir
  - irp/integrity/verify.py: SnapshotFormatError paths, missing ledger file
  - irp/integrity/attest.py: create_attestation's success path (offline, using
    the same freetsa fixture token test_attest.py already trusts)
  - irp/integrity/errors.py: exception hierarchy
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

pytest.importorskip("rfc8785", reason="integrity extra not installed: pip install -e '.[dev]'")

from irp.integrity.canonical import canonicalize, digest_canonical, sha256_hex  # noqa: E402
from irp.integrity.manifest import build_snapshot_file  # noqa: E402
from irp.integrity.snapshot import create_snapshot, get_or_create_ledger_id, integrity_dir  # noqa: E402
from irp.integrity.verify import verify_snapshot  # noqa: E402
from irp.integrity.errors import (  # noqa: E402
    IntegrityError, IntegrityDependencyError, LedgerIntegrityError, SnapshotFormatError,
)


# ── rfc3161.build_request ──────────────────────────────────────────────────────

class TestBuildRequest:
    """build_request is exercised indirectly by request_timestamp in the
    attest tests, but never directly, cover its own contract here."""

    def test_builds_der_encoded_request(self):
        pytest.importorskip("asn1crypto")
        from irp.integrity.rfc3161 import build_request
        import hashlib

        digest = hashlib.sha256(b"hello world").digest()
        der = build_request(digest, cert_req=True)
        assert isinstance(der, bytes)
        assert len(der) > 0

    def test_request_round_trips_through_asn1crypto(self):
        pytest.importorskip("asn1crypto")
        from asn1crypto import tsp
        from irp.integrity.rfc3161 import build_request
        import hashlib

        digest = hashlib.sha256(b"round trip me").digest()
        der = build_request(digest, cert_req=True)
        parsed = tsp.TimeStampReq.load(der)
        assert parsed["message_imprint"]["hashed_message"].native == digest
        assert parsed["cert_req"].native is True

    def test_cert_req_false_is_respected(self):
        pytest.importorskip("asn1crypto")
        from asn1crypto import tsp
        from irp.integrity.rfc3161 import build_request
        import hashlib

        digest = hashlib.sha256(b"x").digest()
        der = build_request(digest, cert_req=False)
        parsed = tsp.TimeStampReq.load(der)
        assert parsed["cert_req"].native is False


# ── manifest.build_snapshot_file ───────────────────────────────────────────────

class TestBuildSnapshotFile:
    def test_manifest_carries_entry_count_and_head_id(self):
        entries = [{"id": "IRP-1"}, {"id": "IRP-2"}]
        snap = build_snapshot_file(
            snapshot_id="IRPS-test-001", ledger_id="ILID-test", raw_bytes=b'{"id":"IRP-1"}\n',
            entries=entries,
        )
        assert snap["manifest"]["ledger"]["entry_count"] == 2
        assert snap["manifest"]["ledger"]["head_entry_id"] == "IRP-2"

    def test_empty_entries_head_id_is_none(self):
        snap = build_snapshot_file(
            snapshot_id="IRPS-test-002", ledger_id="ILID-test", raw_bytes=b"", entries=[],
        )
        assert snap["manifest"]["ledger"]["head_entry_id"] is None
        assert snap["manifest"]["ledger"]["entry_count"] == 0

    def test_snapshot_digest_matches_manifest_canonical_hash(self):
        entries = [{"id": "IRP-1"}]
        snap = build_snapshot_file(
            snapshot_id="IRPS-test-003", ledger_id="ILID-test", raw_bytes=b'{"id":"IRP-1"}\n',
            entries=entries,
        )
        recomputed = sha256_hex(canonicalize(snap["manifest"]))
        assert recomputed == snap["snapshot_digest"]["value"]

    def test_previous_snapshot_digest_threaded_through(self):
        snap = build_snapshot_file(
            snapshot_id="IRPS-test-004", ledger_id="ILID-test", raw_bytes=b"", entries=[],
            previous_snapshot_digest="deadbeef",
        )
        assert snap["manifest"]["previous_snapshot_digest"] == "deadbeef"

    def test_two_snapshots_have_different_salts(self):
        a = build_snapshot_file(snapshot_id="A", ledger_id="L", raw_bytes=b"", entries=[])
        b = build_snapshot_file(snapshot_id="B", ledger_id="L", raw_bytes=b"", entries=[])
        assert a["manifest"]["snapshot_salt"] != b["manifest"]["snapshot_salt"]

    def test_explicit_created_at_is_respected(self):
        snap = build_snapshot_file(
            snapshot_id="IRPS-test-005", ledger_id="ILID-test", raw_bytes=b"", entries=[],
            created_at="2026-01-01T00:00:00Z",
        )
        assert snap["manifest"]["created_at"] == "2026-01-01T00:00:00Z"


# ── snapshot.get_or_create_ledger_id / integrity_dir ──────────────────────────

class TestLedgerId:
    def test_creates_identity_file(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        ledger_id = get_or_create_ledger_id(irp_dir)
        assert ledger_id.startswith("ILID-")
        assert (irp_dir / "integrity" / "identity.json").exists()

    def test_stable_across_calls(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        first = get_or_create_ledger_id(irp_dir)
        second = get_or_create_ledger_id(irp_dir)
        assert first == second

    def test_corrupt_identity_file_regenerates(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        integrity_dir(irp_dir)
        (irp_dir / "integrity" / "identity.json").write_text("{not json", encoding="utf-8")
        ledger_id = get_or_create_ledger_id(irp_dir)
        assert ledger_id.startswith("ILID-")

    def test_integrity_dir_creates_snapshots_subdir(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        base = integrity_dir(irp_dir)
        assert (base / "snapshots").is_dir()


# ── verify.verify_snapshot edge cases ──────────────────────────────────────────

class TestVerifySnapshotEdgeCases:
    def test_missing_ledger_file_treated_as_empty(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        res = create_snapshot(irp_dir)
        snap_path = Path(res["path"])
        missing_ledger = tmp_path / "does-not-exist.jsonl"
        out = verify_snapshot(snap_path, missing_ledger)
        assert out["ok"] is True  # empty snapshot vs missing (=> empty) ledger

    def test_bad_snapshot_json_raises_format_error(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        bad_snap = tmp_path / "bad.json"
        bad_snap.write_text("{not json", encoding="utf-8")
        with pytest.raises(SnapshotFormatError):
            verify_snapshot(bad_snap, irp_dir / "ledger.jsonl")

    def test_missing_manifest_key_raises_format_error(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        bad_snap = tmp_path / "bad.json"
        bad_snap.write_text(json.dumps({"snapshot_digest": {"value": "x"}}), encoding="utf-8")
        with pytest.raises(SnapshotFormatError):
            verify_snapshot(bad_snap, irp_dir / "ledger.jsonl")

    def test_unknown_schema_raises_format_error(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        res = create_snapshot(irp_dir)
        snap_path = Path(res["path"])
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        data["manifest"]["schema"] = "irp-integrity-snapshot/99.9"
        snap_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SnapshotFormatError):
            verify_snapshot(snap_path, irp_dir / "ledger.jsonl")

    def test_invalid_utf8_ledger_reported_not_raised(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        res = create_snapshot(irp_dir)
        snap_path = Path(res["path"])
        bad_ledger = tmp_path / "bad_ledger.jsonl"
        bad_ledger.write_bytes(b"\xff\xfe\x00bad-utf8")
        out = verify_snapshot(snap_path, bad_ledger)
        assert out["ok"] is False
        checks = {c["check"]: c["pass"] for c in out["checks"]}
        assert checks["ledger parse"] is False


# ── errors module ───────────────────────────────────────────────────────────────

class TestErrorsHierarchy:
    def test_all_errors_are_integrity_errors(self):
        assert issubclass(IntegrityDependencyError, IntegrityError)
        assert issubclass(LedgerIntegrityError, IntegrityError)
        assert issubclass(SnapshotFormatError, IntegrityError)

    def test_integrity_error_is_an_exception(self):
        assert issubclass(IntegrityError, Exception)


# ── attest.create_attestation success path (offline, mocked TSA) ─────────────

class TestCreateAttestationSuccessPath:
    """Uses the same real freetsa fixture token test_attest.py trusts, but
    exercises create_attestation's write path by monkeypatching
    request_timestamp so no network call is made."""

    def test_writes_token_and_receipt(self, tmp_path, monkeypatch):
        pytest.importorskip("asn1crypto")
        pytest.importorskip("cryptography")
        import irp.integrity.attest as attest_mod

        fixt = Path(__file__).parent / "fixtures"
        token = (fixt / "freetsa-token.tsr").read_bytes()
        meta = json.loads((fixt / "freetsa-token.meta.json").read_text())
        digest_hex = meta["digest_hex"]

        # Build a snapshot whose manifest hashes to the digest the fixture
        # token anchors, mirroring test_attest.py's TestAttestationChain approach.
        irp_dir = tmp_path / ".irp"
        (irp_dir / "integrity" / "snapshots").mkdir(parents=True)
        snap = {
            "snapshot_digest": {"alg": "sha-256", "value": digest_hex},
            "manifest": {"schema": "irp-integrity-snapshot/0.1", "snapshot_id": "IRPS-2026-01-01-001"},
        }
        snap_path = irp_dir / "integrity" / "snapshots" / "IRPS-2026-01-01-001.json"
        snap_path.write_text(json.dumps(snap), encoding="utf-8")

        monkeypatch.setattr(attest_mod, "request_timestamp", lambda digest, url, timeout=20: token)

        result = attest_mod.create_attestation(irp_dir, snap_path, tsa_url="https://example.invalid/tsr")

        token_path = Path(result["token_path"])
        receipt_path = Path(result["receipt_path"])
        assert token_path.exists()
        assert receipt_path.exists()
        assert token_path.read_bytes() == token

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["snapshot_id"] == "IRPS-2026-01-01-001"
        assert receipt["anchored_digest"]["value"] == digest_hex
        assert receipt["tsa_url"] == "https://example.invalid/tsr"

    def test_mismatched_digest_token_raises(self, tmp_path, monkeypatch):
        """If the TSA (or a compromised transport) returns a token anchoring a
        different digest than we asked for, create_attestation must refuse it."""
        pytest.importorskip("asn1crypto")
        pytest.importorskip("cryptography")
        import irp.integrity.attest as attest_mod

        fixt = Path(__file__).parent / "fixtures"
        token = (fixt / "freetsa-token.tsr").read_bytes()

        irp_dir = tmp_path / ".irp"
        (irp_dir / "integrity" / "snapshots").mkdir(parents=True)
        # Snapshot digest does NOT match the fixture token's anchored digest.
        other_digest = "0" * 64
        snap = {
            "snapshot_digest": {"alg": "sha-256", "value": other_digest},
            "manifest": {"schema": "irp-integrity-snapshot/0.1", "snapshot_id": "IRPS-2026-01-01-002"},
        }
        snap_path = irp_dir / "integrity" / "snapshots" / "IRPS-2026-01-01-002.json"
        snap_path.write_text(json.dumps(snap), encoding="utf-8")

        monkeypatch.setattr(attest_mod, "request_timestamp", lambda digest, url, timeout=20: token)

        with pytest.raises(IntegrityError):
            attest_mod.create_attestation(irp_dir, snap_path, tsa_url="https://example.invalid/tsr")
