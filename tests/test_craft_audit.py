"""Tests for the Craft audit trail — IRP-US-010.

Acceptance criteria:
  010a: craft add writes a ledger event to ledger.jsonl automatically
  010b: --irp flag stores source_irps in both craft entry and ledger event
  010c: ledger event shape is complete and inspectable
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

IRP_PY = str(Path(__file__).parent.parent / "irp" / "core" / "irp.py")


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal empty .irp project."""
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
    (irp_dir / "current.json").write_text(
        json.dumps({"version": 1, "active": []}), encoding="utf-8"
    )
    (irp_dir / "craft.jsonl").write_text("", encoding="utf-8")
    return tmp_path


def _run(args: list[str], cwd: Path) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, IRP_PY] + args,
        capture_output=True, text=True, cwd=str(cwd),
    )
    return r.returncode, r.stdout, r.stderr


def _read_ledger(proj: Path) -> list[dict]:
    lines = (proj / ".irp" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def _read_craft(proj: Path) -> list[dict]:
    lines = (proj / ".irp" / "craft.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ── US-010a: ledger event written automatically ───────────────────────────────

class TestLedgerEventWritten:
    def test_craft_add_writes_ledger_event(self, tmp_path):
        """010a: adding a craft entry must produce a ledger event."""
        proj = _make_project(tmp_path)
        code, out, err = _run(
            ["craft", "add", "--category", "gotcha", "--what", "Always quote file paths with spaces"],
            proj,
        )
        assert code == 0, f"craft add failed: {err}"
        events = _read_ledger(proj)
        assert len(events) == 1, "Expected exactly one ledger event"

    def test_ledger_event_type_is_craft_event(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "preference", "--what", "Use --json everywhere"], proj)
        events = _read_ledger(proj)
        assert events[0]["type"] == "craft_event"

    def test_ledger_event_action_is_add(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "preference", "--what", "Use --json everywhere"], proj)
        events = _read_ledger(proj)
        assert events[0]["action"] == "add"

    def test_multiple_adds_produce_multiple_events(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "First entry"], proj)
        _run(["craft", "add", "--category", "preference", "--what", "Second entry"], proj)
        events = _read_ledger(proj)
        assert len(events) == 2

    def test_craft_list_does_not_write_ledger(self, tmp_path):
        """Read-only operations must not produce ledger events."""
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "A gotcha"], proj)
        before = len(_read_ledger(proj))
        _run(["craft", "list"], proj)
        after = len(_read_ledger(proj))
        assert after == before

    def test_craft_export_does_not_write_ledger(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "A gotcha"], proj)
        before = len(_read_ledger(proj))
        _run(["craft", "export", "--force"], proj)
        after = len(_read_ledger(proj))
        assert after == before


# ── US-010b: --irp flag stores source_irps ────────────────────────────────────

class TestSourceIrps:
    def test_irp_flag_stored_in_ledger_event(self, tmp_path):
        """010b: --irp IRP-001 must appear in ledger event source_irps."""
        proj = _make_project(tmp_path)
        _run([
            "craft", "add",
            "--category", "gotcha",
            "--what", "Always run resolver before committing",
            "--irp", "IRP-2026-05-09-005",
        ], proj)
        events = _read_ledger(proj)
        assert "source_irps" in events[0]
        assert "IRP-2026-05-09-005" in events[0]["source_irps"]

    def test_irp_flag_stored_in_craft_entry(self, tmp_path):
        """--irp must also appear in the craft.jsonl entry itself."""
        proj = _make_project(tmp_path)
        _run([
            "craft", "add",
            "--category", "gotcha",
            "--what", "Check supersession before querying",
            "--irp", "IRP-2026-05-09-005",
        ], proj)
        entries = _read_craft(proj)
        assert "source_irps" in entries[0]
        assert "IRP-2026-05-09-005" in entries[0]["source_irps"]

    def test_multiple_irp_ids(self, tmp_path):
        """Multiple --irp IDs must all be stored."""
        proj = _make_project(tmp_path)
        _run([
            "craft", "add",
            "--category", "way-of-working",
            "--what", "Run double-diamond before naming anything",
            "--irp", "IRP-2026-05-09-003", "IRP-2026-05-09-004",
        ], proj)
        events = _read_ledger(proj)
        assert "IRP-2026-05-09-003" in events[0]["source_irps"]
        assert "IRP-2026-05-09-004" in events[0]["source_irps"]

    def test_no_irp_flag_no_source_irps_key(self, tmp_path):
        """Without --irp, source_irps must be absent (not empty list)."""
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "No citation"], proj)
        events = _read_ledger(proj)
        assert "source_irps" not in events[0]


# ── US-010c: ledger event shape ───────────────────────────────────────────────

class TestEventShape:
    def test_event_has_craft_id(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "Shape test"], proj)
        event = _read_ledger(proj)[0]
        assert "craft_id" in event
        assert event["craft_id"].startswith("CRAFT-")

    def test_event_has_category(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "preference", "--what", "Shape test"], proj)
        event = _read_ledger(proj)[0]
        assert event["category"] == "preference"

    def test_event_has_what(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "The thing I know"], proj)
        event = _read_ledger(proj)[0]
        assert event["what"] == "The thing I know"

    def test_event_has_timestamp(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "Timestamp test"], proj)
        event = _read_ledger(proj)[0]
        assert "timestamp" in event
        assert "2026" in str(event["timestamp"]) or "T" in str(event["timestamp"])

    def test_event_has_contributor(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "Contributor test"], proj)
        event = _read_ledger(proj)[0]
        assert "contributor" in event
        assert event["contributor"]  # non-empty

    def test_context_included_when_provided(self, tmp_path):
        proj = _make_project(tmp_path)
        _run([
            "craft", "add",
            "--category", "gotcha",
            "--what", "Context test",
            "--context", "irp resolver",
        ], proj)
        event = _read_ledger(proj)[0]
        assert event.get("context") == "irp resolver"

    def test_context_absent_when_not_provided(self, tmp_path):
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "No context"], proj)
        event = _read_ledger(proj)[0]
        assert "context" not in event

    def test_craft_id_matches_craft_entry_id(self, tmp_path):
        """craft_id in ledger event must match id in craft.jsonl entry."""
        proj = _make_project(tmp_path)
        _run(["craft", "add", "--category", "gotcha", "--what", "ID consistency"], proj)
        event = _read_ledger(proj)[0]
        craft_entry = _read_craft(proj)[0]
        assert event["craft_id"] == craft_entry["id"]
