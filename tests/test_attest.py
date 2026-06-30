"""Tests for PR2b — RFC 3161 external timestamp attestation.

The cryptographic verification is tested offline against a real freetsa token
captured as a fixture (tests/fixtures/freetsa-token.tsr). A live end-to-end test
against freetsa is included but skipped when offline.

Acceptance criteria:
  - a genuine token verifies (imprint + message-digest + signature)
  - a token for the wrong digest is rejected (imprint)
  - a tampered token is rejected (signature)
  - ECDSA signers are handled (freetsa uses ECDSA)
  - verify_attestation chains manifest -> snapshot_digest -> token
  - the receipt verifier reports honestly (trust-root NOT performed)
"""
import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

pytest.importorskip("asn1crypto")
pytest.importorskip("cryptography")

from irp.integrity.rfc3161 import read_tst_info, verify_token  # noqa: E402

FIXT = Path(__file__).parent / "fixtures"
TOKEN = (FIXT / "freetsa-token.tsr").read_bytes()
META = json.loads((FIXT / "freetsa-token.meta.json").read_text())
DIGEST = bytes.fromhex(META["digest_hex"])


def _online() -> bool:
    try:
        socket.create_connection(("freetsa.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


# ── offline token verification (fixture) ───────────────────────────────────────

class TestTokenVerification:
    def test_genuine_token_verifies(self):
        v = verify_token(TOKEN, DIGEST)
        assert v["imprint_ok"] is True
        assert v["message_digest_ok"] is True
        assert v["signature_ok"] is True
        assert v["cryptographically_valid"] is True

    def test_ecdsa_signer_handled(self):
        # freetsa signs with ECDSA; the verifier must not assume RSA.
        v = verify_token(TOKEN, DIGEST)
        assert v["signature_ok"] is True
        assert v["signer_subject"]

    def test_gen_time_and_policy_extracted(self):
        info = read_tst_info(TOKEN)
        assert info["gen_time"] is not None
        assert info["policy"]
        assert info["hashed_message"] == DIGEST

    def test_wrong_digest_rejected(self):
        v = verify_token(TOKEN, hashlib.sha256(b"not-the-anchored-data").digest())
        assert v["imprint_ok"] is False
        assert v["cryptographically_valid"] is False

    def test_tampered_token_rejected(self):
        bad = bytearray(TOKEN)
        bad[-10] ^= 0x01  # flip a byte inside the signature region
        v = verify_token(bytes(bad), DIGEST)
        assert v["cryptographically_valid"] is False


# ── verify_attestation chain (snapshot -> token) ───────────────────────────────

class TestAttestationChain:
    def _make_snapshot_for_digest(self, tmp_path, anchored_digest_hex):
        # A snapshot file whose snapshot_digest matches what the fixture token anchors.
        # We construct a manifest and set snapshot_digest to the fixture digest; for the
        # chain test we only need verify_attestation to read the digest and the token.
        irp_dir = tmp_path / ".irp"
        (irp_dir / "integrity" / "snapshots").mkdir(parents=True)
        snap = {
            "snapshot_digest": {"alg": "sha-256", "value": anchored_digest_hex},
            "manifest": {"schema": "irp-integrity-snapshot/0.1", "snapshot_id": "IRPS-test-001"},
        }
        p = irp_dir / "integrity" / "snapshots" / "IRPS-test-001.json"
        p.write_text(json.dumps(snap), encoding="utf-8")
        return p

    def test_token_binds_snapshot_digest(self, tmp_path):
        from irp.integrity.attest import verify_attestation
        snap_path = self._make_snapshot_for_digest(tmp_path, META["digest_hex"])
        token_path = tmp_path / "fixture.tsr"
        token_path.write_bytes(TOKEN)
        res = verify_attestation(snap_path, token_path)
        # The manifest here is a stub so manifest_binds_digest is False, but the
        # token must validly bind the stored snapshot_digest.
        assert res["token"]["cryptographically_valid"] is True
        assert res["token"]["imprint_ok"] is True

    def test_token_for_other_digest_not_witnessed(self, tmp_path):
        from irp.integrity.attest import verify_attestation
        other = hashlib.sha256(b"different-snapshot").hexdigest()
        snap_path = self._make_snapshot_for_digest(tmp_path, other)
        token_path = tmp_path / "fixture.tsr"
        token_path.write_bytes(TOKEN)
        res = verify_attestation(snap_path, token_path)
        assert res["token"]["imprint_ok"] is False
        assert res["externally_witnessed"] is False


# ── live end-to-end (skipped offline) ──────────────────────────────────────────

@pytest.mark.skipif(not _online(), reason="freetsa.org not reachable")
class TestLiveRoundTrip:
    def test_create_then_verify(self, tmp_path):
        # Build a real snapshot, anchor it, verify the full chain.
        sys.path.insert(0, str(REPO))
        from irp.integrity.snapshot import create_snapshot
        from irp.integrity.attest import create_attestation, verify_attestation

        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text(
            json.dumps({"id": "IRP-x-1", "type": "decision", "what": "a", "why": "b"}) + "\n",
            encoding="utf-8",
        )
        snap = create_snapshot(irp_dir)
        snap_path = Path(snap["path"])
        create_attestation(irp_dir, snap_path)
        token = irp_dir / "integrity" / "receipts" / f"{snap['snapshot_id']}.tsr"
        res = verify_attestation(snap_path, token)
        assert res["manifest_binds_digest"] is True
        assert res["token"]["cryptographically_valid"] is True
        assert res["externally_witnessed"] is True
