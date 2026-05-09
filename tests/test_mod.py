"""Tests for irp mod — IRP-US-013.

Acceptance criteria:
  013a: supersede appends new decision with supersedes field; returns old_id + new_id
  013b: after supersede, resolver excludes old decision from active set
  013c: retire appends type=retirement event to ledger
  013d: after retire, resolver excludes retired decision from active set
  013e: both ops require --reason; missing reason → exit 1
  013f: new IRP ID is auto-generated in datestamp format
  013g: irp mod list shows recent mod events from ledger
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

IRP_PY = str(Path(__file__).parent.parent / "irp" / "core" / "irp.py")

LEDGER = [
    {
        "type": "decision", "id": "IRP-001",
        "decision": "Do not delete the authentication module",
        "reasoning": "Auth module is shared across all services.",
        "tags": ["security", "auth"], "confidence": "high",
        "timestamp": "2026-04-01", "source": "test",
    },
    {
        "type": "decision", "id": "IRP-002",
        "decision": "Use PostgreSQL for the primary database",
        "reasoning": "Relational model fits our schema.",
        "tags": ["backend", "database"], "confidence": "high",
        "timestamp": "2026-04-02", "source": "test",
    },
    {
        "type": "decision", "id": "IRP-003",
        "decision": "All API responses must be JSON",
        "reasoning": "JSON is the standard for our consumers.",
        "tags": ["api"], "confidence": "high",
        "timestamp": "2026-04-03", "source": "test",
    },
]


def _make_project(tmp_path: Path) -> Path:
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    (irp_dir / "ledger.jsonl").write_text(
        "\n".join(json.dumps(e) for e in LEDGER) + "\n", encoding="utf-8"
    )
    active = [e for e in LEDGER if e.get("type") == "decision"]
    (irp_dir / "current.json").write_text(
        json.dumps({"version": 1, "active": active}), encoding="utf-8"
    )
    return tmp_path


def _run(args: list[str], proj: Path) -> tuple[int, dict | str]:
    result = subprocess.run(
        [sys.executable, IRP_PY] + args,
        capture_output=True, text=True, cwd=str(proj),
    )
    out = result.stdout.strip()
    try:
        return result.returncode, json.loads(out)
    except json.JSONDecodeError:
        return result.returncode, out


def _read_ledger(proj: Path) -> list[dict]:
    lines = (proj / ".irp" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ── US-013a: supersede appends new decision ───────────────────────────────────

class TestSupersede:
    def test_supersede_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        assert code == 0

    def test_supersede_returns_old_id(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        assert isinstance(data, dict)
        assert data.get("old_id") == "IRP-001"

    def test_supersede_returns_new_id(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        assert "new_id" in data
        assert data["new_id"] != "IRP-001"

    def test_supersede_appends_to_ledger(self, tmp_path):
        proj = _make_project(tmp_path)
        before = len(_read_ledger(proj))
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        after = _read_ledger(proj)
        assert len(after) == before + 1

    def test_new_entry_has_supersedes_field(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        entries = _read_ledger(proj)
        new_entry = entries[-1]
        assert new_entry.get("supersedes") == "IRP-001"

    def test_new_entry_is_decision_type(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        entries = _read_ledger(proj)
        new_entry = entries[-1]
        assert new_entry.get("type") == "decision"

    def test_new_entry_has_decision_text(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Security policy updated after review",
        ], proj)
        entries = _read_ledger(proj)
        assert "split" in entries[-1]["decision"]

    def test_new_entry_has_reasoning(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth split allowed",
            "--reason", "Security policy updated after review",
        ], proj)
        entries = _read_ledger(proj)
        assert "Security policy" in entries[-1].get("reasoning", "")


# ── US-013b: resolver excludes superseded after mod ───────────────────────────

class TestSupersededExcluded:
    def test_resolve_excludes_old_after_supersede(self, tmp_path):
        proj = _make_project(tmp_path)
        # Before: IRP-001 "authentication module" is active
        _, before = _run(["gate", "delete authentication module"], proj)
        active_before = before.get("active_count", 0)

        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth module may be split but never fully deleted",
            "--reason", "Policy updated",
        ], proj)

        # After: IRP-001 is superseded — active_count may shift or top_match changes
        _, after = _run(["resolve", "--json", "authentication module"], proj)
        active_ids = [c["id"] for c in after.get("conflicts", [])]
        assert "IRP-001" not in active_ids

    def test_superseded_count_increments(self, tmp_path):
        proj = _make_project(tmp_path)
        _, before = _run(["resolve", "--json", "anything"], proj)
        sup_before = before.get("superseded_count", 0)

        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth split allowed",
            "--reason", "Policy updated",
        ], proj)

        _, after = _run(["resolve", "--json", "anything"], proj)
        assert after.get("superseded_count", 0) == sup_before + 1


# ── US-013c: retire appends retirement event ─────────────────────────────────

class TestRetire:
    def test_retire_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "retire", "IRP-002",
            "--reason", "PostgreSQL decision no longer applies — migrated to cloud-managed DB",
        ], proj)
        assert code == 0

    def test_retire_appends_event_to_ledger(self, tmp_path):
        proj = _make_project(tmp_path)
        before = len(_read_ledger(proj))
        _run([
            "mod", "retire", "IRP-002",
            "--reason", "No longer applies",
        ], proj)
        assert len(_read_ledger(proj)) == before + 1

    def test_retire_event_type(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "No longer applies"], proj)
        entries = _read_ledger(proj)
        last = entries[-1]
        assert last.get("type") == "retirement"

    def test_retire_event_has_target_id(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "No longer applies"], proj)
        entries = _read_ledger(proj)
        last = entries[-1]
        assert last.get("id") == "IRP-002"

    def test_retire_event_has_reason(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "No longer applies"], proj)
        entries = _read_ledger(proj)
        last = entries[-1]
        assert "No longer applies" in last.get("reason", "")

    def test_retire_returns_json(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["mod", "retire", "IRP-002", "--reason", "No longer applies"], proj)
        assert isinstance(data, dict)
        assert data.get("retired_id") == "IRP-002"


# ── US-013d: resolver excludes retired after mod ─────────────────────────────

class TestRetiredExcluded:
    def test_resolve_excludes_retired_decision(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "Migrated"], proj)
        _, result = _run(["resolve", "--json", "PostgreSQL database backend"], proj)
        active_ids = [c["id"] for c in result.get("conflicts", [])]
        assert "IRP-002" not in active_ids

    def test_gate_excludes_retired_decision(self, tmp_path):
        proj = _make_project(tmp_path)
        _, before = _run(["gate", "use PostgreSQL database backend"], proj)
        active_before = before.get("active_count", 0)

        _run(["mod", "retire", "IRP-002", "--reason", "Migrated"], proj)

        _, after = _run(["gate", "use PostgreSQL database backend"], proj)
        assert after.get("active_count", 0) < active_before


# ── US-013e: --reason required ────────────────────────────────────────────────

class TestReasonRequired:
    def test_supersede_without_reason_fails(self, tmp_path):
        proj = _make_project(tmp_path)
        code, _ = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Some new decision",
        ], proj)
        assert code != 0

    def test_retire_without_reason_fails(self, tmp_path):
        proj = _make_project(tmp_path)
        code, _ = _run(["mod", "retire", "IRP-001"], proj)
        assert code != 0

    def test_supersede_without_decision_fails(self, tmp_path):
        proj = _make_project(tmp_path)
        code, _ = _run([
            "mod", "supersede", "IRP-001",
            "--reason", "Policy changed",
        ], proj)
        assert code != 0


# ── US-013f: auto-generated IRP ID ───────────────────────────────────────────

class TestIdGeneration:
    def test_new_id_has_datestamp_format(self, tmp_path):
        """New ID must follow IRP-YYYY-MM-DD-NNN pattern."""
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth split allowed",
            "--reason", "Policy updated",
        ], proj)
        new_id = data.get("new_id", "")
        assert new_id.startswith("IRP-")
        parts = new_id.split("-")
        # IRP-YYYY-MM-DD-NNN → 5 parts
        assert len(parts) == 5

    def test_new_id_differs_from_superseded(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth split allowed",
            "--reason", "Policy updated",
        ], proj)
        assert data.get("new_id") != "IRP-001"


# ── US-013g: irp mod list ─────────────────────────────────────────────────────

class TestModList:
    def test_mod_list_returns_json(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "Migrated"], proj)
        code, data = _run(["mod", "list"], proj)
        assert isinstance(data, dict) or isinstance(data, list)

    def test_mod_list_includes_retirement(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["mod", "retire", "IRP-002", "--reason", "Migrated"], proj)
        code, data = _run(["mod", "list"], proj)
        events = data if isinstance(data, list) else data.get("events", [])
        types = [e.get("type") for e in events]
        assert "retirement" in types

    def test_mod_list_includes_supersession(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "mod", "supersede", "IRP-001",
            "--decision", "Auth split allowed",
            "--reason", "Policy updated",
        ], proj)
        code, data = _run(["mod", "list"], proj)
        events = data if isinstance(data, list) else data.get("events", [])
        # Supersession shows as a decision entry with supersedes field
        has_sup = any(e.get("supersedes") == "IRP-001" for e in events)
        assert has_sup

    def test_mod_list_empty_when_no_mods(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["mod", "list"], proj)
        events = data if isinstance(data, list) else data.get("events", [])
        assert events == []
