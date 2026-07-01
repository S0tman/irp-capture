"""Tests for `irp export evidence --attest` — external RFC 3161 attestation of a
compliance evidence package.

Design under test (SPEC 2026-07-01):
  - --attest anchors a snapshot of the ledger to a TSA and adds an
    "External timestamp" block to the report.
  - It fails closed: if the TSA is unreachable, nothing is written and no
    package claims to be attested.
  - --attest cannot be combined with --demo (sample data is not in the ledger).
  - The default path is unchanged: no network, no attestation block.

Live end-to-end coverage uses real freetsa and is skipped when offline. The
fail-closed test monkeypatches the timestamp call so it needs no network.
"""
import argparse
import json
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# conftest already put irp/core on sys.path for dispatcher-style imports. Insert
# REPO *ahead* of it and cache the `irp` package, so later `import irp.integrity.*`
# resolves the package rather than irp/core/irp.py (which shadows the name).
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from commands.evidence import run_export_evidence  # noqa: E402


def _online() -> bool:
    try:
        socket.create_connection(("freetsa.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        demo=False, output=None, force=False, json=False,
        framework="euaiact", config=None, attest=False, tsa_url=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _make_ledger(tmp_path: Path) -> Path:
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    lines = [
        {"id": "IRP-2026-01-01-001", "type": "decision",
         "what": "Agent restricted to pre-screening; human confirms every recommendation",
         "why": "Art. 14 human oversight anchor", "confirmed_by": "compliance.officer",
         "timestamp": "2026-01-01"},
        {"id": "IRP-2026-01-02-001", "type": "decision",
         "what": "System classified high-risk under Annex III 5(b)",
         "why": "Creditworthiness assessment of natural persons", "confirmed_by": "legal.team",
         "timestamp": "2026-01-02"},
    ]
    (irp_dir / "ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8"
    )
    return irp_dir


# ── guards (no network, no crypto deps) ────────────────────────────────────────

class TestGuards:
    def test_attest_demo_is_rejected(self, tmp_path):
        irp_dir = _make_ledger(tmp_path)
        res = run_export_evidence(tmp_path, irp_dir, _args(attest=True, demo=True))
        assert res["status"] == "error"
        assert res.get("verdict") == "block"
        assert "--demo" in res["text"]
        # Nothing written.
        assert not list(tmp_path.glob("EVIDENCE-*.md"))

    def test_default_has_no_attestation_and_no_block(self, tmp_path):
        irp_dir = _make_ledger(tmp_path)
        res = run_export_evidence(tmp_path, irp_dir, _args())
        assert res["status"] == "ok"
        assert res["attested"] is False
        body = Path(res["output_path"]).read_text(encoding="utf-8")
        assert "## External timestamp" not in body


# ── fail-closed (crypto deps, but no network — timestamp call is patched) ───────

class TestFailClosed:
    def test_unreachable_tsa_writes_nothing(self, tmp_path, monkeypatch):
        pytest.importorskip("asn1crypto")
        pytest.importorskip("cryptography")
        import irp.integrity.attest as attest_mod

        def _boom(*a, **k):
            raise OSError("simulated: TSA unreachable")

        # create_attestation looks up request_timestamp in its own namespace.
        monkeypatch.setattr(attest_mod, "request_timestamp", _boom)

        irp_dir = _make_ledger(tmp_path)
        res = run_export_evidence(tmp_path, irp_dir, _args(attest=True))

        assert res["status"] == "error"
        assert res.get("verdict") == "block"
        assert "fail-closed" in res["text"].lower()
        # The package must not exist: no artifact may claim a witness it never got.
        assert not (tmp_path / "EVIDENCE-euaiact.md").exists()


# ── live end-to-end against real freetsa (skipped offline) ─────────────────────

@pytest.mark.skipif(not _online(), reason="freetsa.org not reachable")
class TestLive:
    def test_attested_package_is_witnessed_and_verifies(self, tmp_path):
        pytest.importorskip("asn1crypto")
        irp_dir = _make_ledger(tmp_path)
        res = run_export_evidence(tmp_path, irp_dir, _args(attest=True))

        assert res["status"] == "ok"
        assert res["attested"] is True
        att = res["attestation"]

        body = Path(res["output_path"]).read_text(encoding="utf-8")
        assert "## External timestamp" in body
        assert att["snapshot_id"] in body
        assert att["gen_time"] in body
        assert "irp attest verify" in body

        # The receipt and token exist where the report says to verify them.
        snap_path = Path(att["snapshot_path"])
        assert snap_path.exists()
        token = irp_dir / "integrity" / "receipts" / f"{att['snapshot_id']}.tsr"
        assert token.exists()

        # Independently verify the chain: manifest -> snapshot_digest -> token.
        from irp.integrity.attest import verify_attestation
        v = verify_attestation(snap_path, token)
        assert v["manifest_binds_digest"] is True
        assert v["token"]["cryptographically_valid"] is True
        assert v["externally_witnessed"] is True

    def test_tampered_snapshot_breaks_the_witness(self, tmp_path):
        pytest.importorskip("asn1crypto")
        irp_dir = _make_ledger(tmp_path)
        res = run_export_evidence(tmp_path, irp_dir, _args(attest=True))
        att = res["attestation"]
        snap_path = Path(att["snapshot_path"])
        token = irp_dir / "integrity" / "receipts" / f"{att['snapshot_id']}.tsr"

        # Alter the witnessed manifest: the timestamp no longer binds it.
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        snap["manifest"]["ledger"]["entry_count"] += 1
        snap_path.write_text(json.dumps(snap, indent=2), encoding="utf-8")

        from irp.integrity.attest import verify_attestation
        v = verify_attestation(snap_path, token)
        assert v["manifest_binds_digest"] is False
        assert v["externally_witnessed"] is False
