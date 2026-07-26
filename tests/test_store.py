"""Tests for irp/core/store.py, the ledger, current.json, config, and craft
persistence primitives every command handler is built on.

No CLI here: these are direct unit tests of the pure I/O helpers, using
tmp_path so nothing touches a real project.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from store import (
    ensure_irp_dir,
    read_current,
    write_current,
    append_ledger_entry,
    next_irp_id,
    rebuild_current,
    read_ledger,
    read_config,
    write_config,
    read_craft,
    append_craft_entry,
    next_craft_id,
)


# ── ensure_irp_dir ────────────────────────────────────────────────────────────

class TestEnsureIrpDir:
    def test_creates_irp_directory(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        assert irp_dir == tmp_path / ".irp"
        assert irp_dir.is_dir()

    def test_creates_empty_ledger_file(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        assert (irp_dir / "ledger.jsonl").exists()
        assert (irp_dir / "ledger.jsonl").read_text(encoding="utf-8") == ""

    def test_creates_current_json_with_defaults(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        data = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert data == {"version": 1, "active": []}

    def test_idempotent_does_not_overwrite_existing_ledger(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / "ledger.jsonl").write_text('{"id": "IRP-1"}\n', encoding="utf-8")
        # Calling again must not wipe existing content.
        ensure_irp_dir(tmp_path)
        assert (irp_dir / "ledger.jsonl").read_text(encoding="utf-8") == '{"id": "IRP-1"}\n'

    def test_idempotent_does_not_overwrite_existing_current(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        custom = {"version": 1, "active": [{"id": "IRP-1"}]}
        write_current(irp_dir, custom)
        ensure_irp_dir(tmp_path)
        assert read_current(irp_dir) == custom


# ── read_current / write_current ─────────────────────────────────────────────

class TestCurrent:
    def test_read_current_missing_file_defaults(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        # write_current always creates the file; simulate a blank one directly.
        (irp_dir / "current.json").write_text("", encoding="utf-8")
        assert read_current(irp_dir) == {"version": 1, "active": []}

    def test_write_then_read_roundtrip(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        data = {"version": 1, "active": [{"id": "IRP-1", "what": "x"}]}
        write_current(irp_dir, data)
        assert read_current(irp_dir) == data

    def test_write_current_is_pretty_printed_utf8(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        write_current(irp_dir, {"version": 1, "active": [{"id": "IRP-é"}]})
        text = (irp_dir / "current.json").read_text(encoding="utf-8")
        assert "\n" in text  # indent=2 produces multiple lines
        assert "é" in text  # ensure_ascii=False preserves unicode


# ── append_ledger_entry / read_ledger ────────────────────────────────────────

class TestLedger:
    def test_append_then_read_roundtrip(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        append_ledger_entry(irp_dir, {"id": "IRP-1", "what": "a"})
        append_ledger_entry(irp_dir, {"id": "IRP-2", "what": "b"})
        entries = read_ledger(irp_dir)
        assert [e["id"] for e in entries] == ["IRP-1", "IRP-2"]

    def test_read_ledger_missing_file_returns_empty_list(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        assert read_ledger(irp_dir) == []

    def test_read_ledger_skips_blank_lines(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text(
            '{"id": "IRP-1"}\n\n   \n{"id": "IRP-2"}\n', encoding="utf-8"
        )
        entries = read_ledger(irp_dir)
        assert len(entries) == 2

    def test_read_ledger_skips_malformed_lines_tolerantly(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text(
            '{"id": "IRP-1"}\n{not json\n{"id": "IRP-2"}\n', encoding="utf-8"
        )
        entries = read_ledger(irp_dir)
        assert [e["id"] for e in entries] == ["IRP-1", "IRP-2"]

    def test_append_preserves_unicode(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        append_ledger_entry(irp_dir, {"id": "IRP-1", "what": "café"})
        raw = (irp_dir / "ledger.jsonl").read_text(encoding="utf-8")
        assert "café" in raw


# ── next_irp_id ───────────────────────────────────────────────────────────────

class TestNextIrpId:
    def test_first_id_of_the_day_is_001(self):
        assert next_irp_id([]).endswith("-001")

    def test_id_format_has_datestamp(self):
        from datetime import date
        today = date.today().isoformat()
        assert next_irp_id([]) == f"IRP-{today}-001"

    def test_sequence_increments_with_todays_entries(self):
        from datetime import date
        today = date.today().isoformat()
        ledger = [
            {"id": f"IRP-{today}-001", "timestamp": today},
            {"id": f"IRP-{today}-002", "timestamp": today},
        ]
        assert next_irp_id(ledger) == f"IRP-{today}-003"

    def test_entries_from_other_days_do_not_count(self):
        from datetime import date
        today = date.today().isoformat()
        ledger = [{"id": "IRP-2020-01-01-001", "timestamp": "2020-01-01"}]
        assert next_irp_id(ledger) == f"IRP-{today}-001"

    def test_missing_timestamp_field_does_not_count(self):
        from datetime import date
        today = date.today().isoformat()
        ledger = [{"id": "IRP-x"}]  # no timestamp
        assert next_irp_id(ledger) == f"IRP-{today}-001"

    def test_backdated_timestamp_still_increments_the_sequence(self):
        """
        Regression: the sequence used to be counted from entries whose
        `timestamp` was today, while the id was named from today's date. A
        record captured today but carrying a backdated timestamp, the date the
        decision was actually taken, therefore never incremented the counter,
        and the next capture reused its number. Seeding one project's ledger
        with true decision dates produced fourteen records sharing one id.
        """
        from datetime import date
        today = date.today().isoformat()
        ledger = [
            {"id": f"IRP-{today}-001", "timestamp": "2026-07-23"},
            {"id": f"IRP-{today}-002", "timestamp": "2026-07-25"},
        ]
        assert next_irp_id(ledger) == f"IRP-{today}-003"

    def test_sequence_survives_a_gap(self):
        """max + 1, not count + 1, so a hand-removed entry cannot cause a collision."""
        from datetime import date
        today = date.today().isoformat()
        ledger = [
            {"id": f"IRP-{today}-001", "timestamp": today},
            {"id": f"IRP-{today}-007", "timestamp": today},
        ]
        assert next_irp_id(ledger) == f"IRP-{today}-008"

    def test_malformed_ids_with_todays_prefix_are_ignored(self):
        from datetime import date
        today = date.today().isoformat()
        ledger = [{"id": f"IRP-{today}-abc", "timestamp": today}]
        assert next_irp_id(ledger) == f"IRP-{today}-001"


# ── rebuild_current ───────────────────────────────────────────────────────────

class TestRebuildCurrent:
    def test_keeps_only_decision_type_entries(self):
        ledger = [
            {"type": "decision", "id": "IRP-1"},
            {"type": "retirement", "id": "IRP-2"},
            {"type": "decision", "id": "IRP-3"},
        ]
        current = rebuild_current(ledger)
        assert [e["id"] for e in current["active"]] == ["IRP-1", "IRP-3"]

    def test_keeps_last_ten_only(self):
        ledger = [{"type": "decision", "id": f"IRP-{i}"} for i in range(15)]
        current = rebuild_current(ledger)
        assert len(current["active"]) == 10
        assert current["active"][0]["id"] == "IRP-5"
        assert current["active"][-1]["id"] == "IRP-14"

    def test_empty_ledger_gives_empty_active(self):
        assert rebuild_current([]) == {"version": 1, "active": []}

    def test_version_field_present(self):
        assert rebuild_current([])["version"] == 1


# ── project config ─────────────────────────────────────────────────────────────

class TestConfig:
    def test_read_config_missing_file_returns_defaults(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        assert read_config(irp_dir) == {"control_level": "advanced"}

    def test_write_then_read_roundtrip(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        write_config(irp_dir, {"control_level": "easy"})
        assert read_config(irp_dir) == {"control_level": "easy"}

    def test_read_config_merges_missing_keys_with_defaults(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "config.json").write_text(json.dumps({"custom_key": "x"}), encoding="utf-8")
        cfg = read_config(irp_dir)
        assert cfg["control_level"] == "advanced"
        assert cfg["custom_key"] == "x"

    def test_read_config_corrupt_json_falls_back_to_defaults(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "config.json").write_text("{not json", encoding="utf-8")
        assert read_config(irp_dir) == {"control_level": "advanced"}


# ── craft ledger ──────────────────────────────────────────────────────────────

class TestCraft:
    def test_append_then_read_roundtrip(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        append_craft_entry(irp_dir, {"id": "CRAFT-1", "what": "a gotcha"})
        entries = read_craft(irp_dir)
        assert len(entries) == 1
        assert entries[0]["what"] == "a gotcha"

    def test_read_craft_missing_file_returns_empty_list(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        assert read_craft(irp_dir) == []

    def test_read_craft_skips_malformed_lines(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "craft.jsonl").write_text(
            '{"id": "CRAFT-1"}\n{bad\n{"id": "CRAFT-2"}\n', encoding="utf-8"
        )
        entries = read_craft(irp_dir)
        assert [e["id"] for e in entries] == ["CRAFT-1", "CRAFT-2"]

    def test_next_craft_id_format(self):
        from datetime import date
        today = date.today().isoformat()
        assert next_craft_id([]) == f"CRAFT-{today}-001"

    def test_next_craft_id_increments(self):
        from datetime import date
        today = date.today().isoformat()
        entries = [{"id": f"CRAFT-{today}-001", "timestamp": today}]
        assert next_craft_id(entries) == f"CRAFT-{today}-002"

    def test_next_craft_id_ignores_other_days(self):
        from datetime import date
        today = date.today().isoformat()
        entries = [{"id": "CRAFT-2020-01-01-001", "timestamp": "2020-01-01"}]
        assert next_craft_id(entries) == f"CRAFT-{today}-001"
