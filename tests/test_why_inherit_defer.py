"""Tests for irp why, irp inherit, and irp defer command handlers."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, append_ledger_entry, write_current, read_ledger  # noqa: E402
from commands.why import run_why  # noqa: E402
from commands.inherit import run_inherit  # noqa: E402
from commands.defer import run_defer  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


ENTRY_1 = {
    "id": "IRP-2026-04-01-001", "type": "decision", "what": "Use PostgreSQL",
    "why": "Relational fit", "confidence": "high", "timestamp": "2026-04-01",
    "source": "cli",
}
ENTRY_2 = {
    "id": "IRP-2026-04-02-001", "type": "decision", "what": "No SQLite in prod",
    "why": "Concurrency limits", "confidence": "high", "timestamp": "2026-04-02",
    "source": "slack", "source_ref": {"channel_id": "C123", "thread_ts": "111.222"},
}


def _seed(irp_dir, entries):
    for e in entries:
        append_ledger_entry(irp_dir, e)
    write_current(irp_dir, {"version": 1, "active": list(entries)})


# ── irp why ────────────────────────────────────────────────────────────────────

class TestRunWhy:
    def test_no_id_returns_latest_active(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1, ENTRY_2])
        result = run_why(tmp_path, irp_dir, _Args(id=None))
        assert result["latest"]["id"] == "IRP-2026-04-02-001"

    def test_no_active_context_is_empty(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=None))
        assert result["status"] == "empty"

    def test_lookup_by_id_returns_matching_entry(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1, ENTRY_2])
        result = run_why(tmp_path, irp_dir, _Args(id="IRP-2026-04-01-001"))
        assert result["status"] == "ok"
        assert result["entry"]["what"] == "Use PostgreSQL"

    def test_lookup_by_unknown_id_not_found(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1])
        result = run_why(tmp_path, irp_dir, _Args(id="IRP-nope"))
        assert result["status"] == "not_found"

    def test_slack_source_shows_channel_and_thread(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_2])
        result = run_why(tmp_path, irp_dir, _Args(id="IRP-2026-04-02-001"))
        assert "C123" in result["text"]
        assert "111.222" in result["text"]

    def test_active_count_reported(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1, ENTRY_2])
        result = run_why(tmp_path, irp_dir, _Args(id=None))
        assert result["active_count"] == 2


# ── irp inherit ────────────────────────────────────────────────────────────────

class TestRunInherit:
    def test_no_active_context(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert result["active_count"] == 0
        assert "No active IRP context" in result["text"]

    def test_lists_active_decisions(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1, ENTRY_2])
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert result["active_count"] == 2
        assert "Use PostgreSQL" in result["text"]

    def test_why_field_shown_when_present(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed(irp_dir, [ENTRY_1])
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert "Relational fit" in result["text"]

    def test_project_root_reported(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert result["project_root"] == str(tmp_path)


# ── irp defer ──────────────────────────────────────────────────────────────────

class TestRunDeferExplicitQuestion:
    def test_json_mode_returns_pending_status(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        result = run_defer(tmp_path, irp_dir, _Args(question="Should we delete old logs?", json=True))
        assert result["status"] == "pending"
        assert result["defer_question"] == "Should we delete old logs?"

    def test_json_mode_writes_no_ledger_entry(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        run_defer(tmp_path, irp_dir, _Args(question="Should we delete old logs?", json=True))
        assert read_ledger(irp_dir) == []

    def test_interactive_capture_on_answer(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "Yes, notify users first")
        result = run_defer(tmp_path, irp_dir, _Args(question="Should users be notified?", json=False))
        assert result["status"] == "ok"
        entries = read_ledger(irp_dir)
        assert entries[0]["what"] == "Yes, notify users first"

    def test_interactive_cancel_on_empty_answer(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        result = run_defer(tmp_path, irp_dir, _Args(question="Should users be notified?", json=False))
        assert result["status"] == "cancelled"
        assert read_ledger(irp_dir) == []

    def test_interactive_cancel_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)

        def _raise(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        result = run_defer(tmp_path, irp_dir, _Args(question="Should users be notified?", json=False))
        assert result["status"] == "cancelled"

    def test_captured_entry_tagged_with_defer(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "Answer text")
        run_defer(tmp_path, irp_dir, _Args(question="A question?", json=False))
        entry = read_ledger(irp_dir)[0]
        assert "defer" in entry["tags"]
        assert "resolution" in entry["tags"]


class TestRunDeferFromStdinCritique:
    def _critique(self, verdict="BLOCK", defer_question="Proceed with deletion?"):
        return {
            "verdict": verdict,
            "principle_flags": ["human_control"],
            "reasoning": "This deletes irreversible audit data.",
            "defer_question": defer_question,
        }

    def test_missing_defer_question_is_an_error(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        payload = {"verdict": "WARN", "defer_question": ""}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        result = run_defer(tmp_path, irp_dir, _Args(question=None, json=False))
        assert result["status"] == "error"

    def test_clear_verdict_from_stdin_needs_no_deferral(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        payload = self._critique(verdict="CLEAR", defer_question="")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        result = run_defer(tmp_path, irp_dir, _Args(question=None, json=False))
        assert result["status"] in ("clear", "error")

    def test_block_verdict_pending_in_json_mode(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        payload = self._critique(verdict="BLOCK")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        result = run_defer(tmp_path, irp_dir, _Args(question=None, json=True))
        assert result["status"] == "pending"
        assert result["verdict"] == "BLOCK"
        assert result["principle_flags"] == ["human_control"]

    def test_easy_control_level_warn_is_advisory_only(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        from store import write_config
        write_config(irp_dir, {"control_level": "easy"})
        payload = self._critique(verdict="WARN")
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        result = run_defer(tmp_path, irp_dir, _Args(question=None, json=False))
        assert result["status"] == "advisory"
        assert read_ledger(irp_dir) == []
