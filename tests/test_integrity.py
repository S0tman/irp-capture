"""Tests for PR2a — deterministic integrity snapshots + offline verify.

Acceptance criteria:
  - snapshot is deterministic across harmless reformatting (semantic digest)
  - byte digest catches formatting-only change; semantic digest does not
  - edit / delete / insert / reorder are all detected on verify
  - strict reader reports malformed lines instead of silently skipping
  - duplicate JSON keys are rejected
  - snapshotting never mutates the ledger
  - the manifest digest binds the whole manifest (tamper detection)
  - verify is honest: unattested layer reported as NOT PROVEN / NOT PRESENT
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # make `irp.integrity` importable

from irp.integrity.canonical import canonicalize, digest_canonical
from irp.integrity.snapshot import create_snapshot
from irp.integrity.strict import parse_ledger_strict
from irp.integrity.verify import verify_snapshot


# ── fixtures ───────────────────────────────────────────────────────────────────

ENTRY_1 = {"id": "IRP-2026-06-30-001", "type": "decision", "what": "Use JCS", "why": "interop"}
ENTRY_2 = {"id": "IRP-2026-06-30-002", "type": "decision", "what": "No hash chain", "why": "owner can recompute"}


def _make_ledger(tmp_path: Path, lines: list[str]) -> Path:
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    (irp_dir / "ledger.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return irp_dir


def _ledger_jsonl(*entries: dict) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in entries]


# ── canonicalisation ───────────────────────────────────────────────────────────

class TestCanonicalisation:
    def test_key_order_does_not_change_digest(self):
        a = {"b": 1, "a": 2, "c": [3, 2, 1]}
        b = {"c": [3, 2, 1], "a": 2, "b": 1}
        assert digest_canonical(a) == digest_canonical(b)

    def test_canonical_output_is_sorted_bytes(self):
        assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_unicode_is_preserved_not_normalised(self):
        # NFC and NFD forms of "é" must NOT collapse (RFC 8785 forbids normalisation).
        nfc = {"x": "é"}        # é as single code point
        nfd = {"x": "é"}       # e + combining acute
        assert digest_canonical(nfc) != digest_canonical(nfd)


# ── strict reader ──────────────────────────────────────────────────────────────

class TestStrictReader:
    def test_blank_lines_ignored(self):
        text = json.dumps(ENTRY_1) + "\n\n   \n" + json.dumps(ENTRY_2) + "\n"
        res = parse_ledger_strict(text)
        assert res.ok and len(res.entries) == 2

    def test_malformed_line_reported_not_skipped(self):
        text = json.dumps(ENTRY_1) + "\n{not json\n" + json.dumps(ENTRY_2) + "\n"
        res = parse_ledger_strict(text)
        assert not res.ok
        assert res.errors[0]["line"] == 2
        assert res.errors[0]["kind"] == "malformed-json"

    def test_duplicate_keys_rejected(self):
        res = parse_ledger_strict('{"id": "a", "id": "b"}\n')
        assert not res.ok and res.errors[0]["kind"] == "malformed-json"

    def test_duplicate_ids_flagged(self):
        text = "\n".join(_ledger_jsonl(ENTRY_1, ENTRY_1)) + "\n"
        res = parse_ledger_strict(text)
        assert res.ok and res.duplicate_ids == ["IRP-2026-06-30-001"]


# ── snapshot + verify roundtrip ─────────────────────────────────────────────────

class TestSnapshotVerify:
    def test_roundtrip_passes(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        res = create_snapshot(irp_dir)
        snap_path = Path(res["path"])
        out = verify_snapshot(snap_path, irp_dir / "ledger.jsonl")
        assert out["ok"] is True
        assert all(c["pass"] for c in out["checks"])

    def test_snapshot_does_not_mutate_ledger(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        before = (irp_dir / "ledger.jsonl").read_bytes()
        create_snapshot(irp_dir)
        assert (irp_dir / "ledger.jsonl").read_bytes() == before

    def test_empty_ledger_snapshots_and_verifies(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, [])
        res = create_snapshot(irp_dir)
        assert res["entry_count"] == 0
        out = verify_snapshot(Path(res["path"]), irp_dir / "ledger.jsonl")
        assert out["ok"] is True

    def test_reformat_only_byte_fails_semantic_passes(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        res = create_snapshot(irp_dir)
        # Rewrite the ledger single-line but with reordered keys + extra spaces
        # (same meaning, different bytes).
        reformatted = [
            json.dumps(
                {"why": "interop", "what": "Use JCS", "type": "decision", "id": "IRP-2026-06-30-001"},
                separators=(", ", ": "),
            ),
            json.dumps(ENTRY_2),
        ]
        (irp_dir / "ledger.jsonl").write_text("\n".join(reformatted) + "\n", encoding="utf-8")
        out = verify_snapshot(Path(res["path"]), irp_dir / "ledger.jsonl")
        checks = {c["check"]: c["pass"] for c in out["checks"]}
        assert checks["byte digest"] is False
        assert checks["semantic digest"] is True

    @pytest.mark.parametrize("mutation", ["edit", "delete", "insert", "reorder"])
    def test_semantic_changes_detected(self, tmp_path, mutation):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        res = create_snapshot(irp_dir)
        ledger = irp_dir / "ledger.jsonl"

        if mutation == "edit":
            new = _ledger_jsonl({**ENTRY_1, "why": "CHANGED"}, ENTRY_2)
        elif mutation == "delete":
            new = _ledger_jsonl(ENTRY_1)
        elif mutation == "insert":
            extra = {"id": "IRP-2026-06-30-003", "type": "decision", "what": "x", "why": "y"}
            new = _ledger_jsonl(ENTRY_1, ENTRY_2, extra)
        else:  # reorder
            new = _ledger_jsonl(ENTRY_2, ENTRY_1)

        ledger.write_text("\n".join(new) + "\n", encoding="utf-8")
        out = verify_snapshot(Path(res["path"]), ledger)
        assert out["ok"] is False
        assert any(c["check"] == "semantic digest" and not c["pass"] for c in out["checks"])

    def test_manifest_tamper_detected(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        res = create_snapshot(irp_dir)
        snap_path = Path(res["path"])
        # Alter a manifest field without recomputing snapshot_digest.
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        data["manifest"]["ledger"]["entry_count"] = 999
        snap_path.write_text(json.dumps(data), encoding="utf-8")
        out = verify_snapshot(snap_path, irp_dir / "ledger.jsonl")
        assert out["ok"] is False
        assert any(c["check"] == "snapshot manifest digest" and not c["pass"] for c in out["checks"])

    def test_verify_is_honest_about_attestation(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1))
        res = create_snapshot(irp_dir)
        out = verify_snapshot(Path(res["path"]), irp_dir / "ledger.jsonl")
        att = out["attestation"]
        assert att["historical_existence"] == "NOT PROVEN"
        assert att["external_witness"] == "NOT PRESENT"
        assert att["completeness"] == "NOT GUARANTEED"


class TestMalformedLedger:
    def test_refuses_malformed_by_default(self, tmp_path):
        from irp.integrity.errors import LedgerIntegrityError
        irp_dir = _make_ledger(tmp_path, [json.dumps(ENTRY_1), "{garbage"])
        with pytest.raises(LedgerIntegrityError):
            create_snapshot(irp_dir)

    def test_allow_malformed_snapshots_anyway(self, tmp_path):
        irp_dir = _make_ledger(tmp_path, [json.dumps(ENTRY_1), "{garbage"])
        res = create_snapshot(irp_dir, allow_malformed=True)
        assert res["entry_count"] == 1 and len(res["malformed"]) == 1


# ── end-to-end via the CLI (module mode) ───────────────────────────────────────

class TestCLI:
    def _run(self, args, cwd):
        return subprocess.run(
            [sys.executable, "-m", "irp.core.irp"] + args,
            capture_output=True, text=True, cwd=str(cwd), env={"PYTHONPATH": str(REPO)},
        )

    def test_snapshot_then_verify(self, tmp_path):
        _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        snap = self._run(["integrity", "snapshot"], tmp_path)
        assert snap.returncode == 0, snap.stderr
        assert "UNATTESTED" in snap.stdout

        snaps = list((tmp_path / ".irp" / "integrity" / "snapshots").glob("IRPS-*.json"))
        assert len(snaps) == 1

        ok = self._run(["integrity", "verify", str(snaps[0])], tmp_path)
        assert ok.returncode == 0, ok.stderr
        assert "RESULT: PASS" in ok.stdout

    def test_verify_fails_exit_10_after_tamper(self, tmp_path):
        _make_ledger(tmp_path, _ledger_jsonl(ENTRY_1, ENTRY_2))
        self._run(["integrity", "snapshot"], tmp_path)
        snaps = list((tmp_path / ".irp" / "integrity" / "snapshots").glob("IRPS-*.json"))
        # Tamper with the ledger after snapshot.
        (tmp_path / ".irp" / "ledger.jsonl").write_text(
            "\n".join(_ledger_jsonl({**ENTRY_1, "why": "TAMPERED"}, ENTRY_2)) + "\n",
            encoding="utf-8",
        )
        res = self._run(["integrity", "verify", str(snaps[0])], tmp_path)
        assert res.returncode == 10, (res.returncode, res.stdout, res.stderr)
        assert "RESULT: FAIL" in res.stdout
