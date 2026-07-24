"""Tests for irp/core/commands/capture.py, run_capture and its helpers.

Covers the stdin path (non-interactive, what tools/agents actually use),
defaulting behaviour, milestone messages, and the confirm-token gate on the
interactive path. Integration dispatch is monkeypatched so no real Obsidian/
MemPalace/Slack call is ever made.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# capture.py does `from irp.core.store import ...` (absolute). Cache the real
# `irp` package first so that resolves to the package, not irp/core/irp.py
# (see the shadowing note at the top of irp/core/irp.py).
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, read_ledger  # noqa: E402
from commands.capture import (  # noqa: E402
    run_capture,
    read_candidate_from_stdin,
    confirm_token,
    _milestone_lines,
)


class _Args:
    def __init__(self, stdin=True, json=False):
        self.stdin = stdin
        self.json = json


def _no_integrations(monkeypatch):
    """Prevent capture.py from touching real integrations during tests."""
    import commands.capture as capture_mod
    monkeypatch.setattr(capture_mod._dispatch, "run", lambda decision, project_root: [])


# ── read_candidate_from_stdin ─────────────────────────────────────────────────

class TestReadCandidateFromStdin:
    def test_parses_valid_json_object(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"what": "x", "why": "y"}'))
        candidate = read_candidate_from_stdin()
        assert candidate == {"what": "x", "why": "y"}

    def test_empty_stdin_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        with pytest.raises(ValueError):
            read_candidate_from_stdin()

    def test_non_object_json_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("[1, 2, 3]"))
        with pytest.raises(ValueError):
            read_candidate_from_stdin()

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
        with pytest.raises(json.JSONDecodeError):
            read_candidate_from_stdin()


# ── confirm_token ──────────────────────────────────────────────────────────────

class TestConfirmToken:
    def test_confirm_accepts_c(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "c")
        assert confirm_token() is True

    def test_confirm_rejects_anything_else(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "s")
        assert confirm_token() is False

    def test_confirm_rejects_empty(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        assert confirm_token() is False

    def test_confirm_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "C")
        assert confirm_token() is True


# ── run_capture (stdin path) ──────────────────────────────────────────────────

class TestRunCaptureStdin:
    def test_writes_ledger_entry(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "Use X", "why": "Because Y"})))
        args = _Args(stdin=True)
        result = run_capture(tmp_path, irp_dir, args)
        assert result["status"] == "captured"
        entries = read_ledger(irp_dir)
        assert len(entries) == 1
        assert entries[0]["what"] == "Use X"

    def test_assigns_sequential_id(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["entry"]["id"].startswith("IRP-")
        assert result["entry"]["id"].endswith("-001")

    def test_defaults_type_to_decision(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["entry"]["type"] == "decision"

    def test_defaults_confidence_to_medium(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["entry"]["confidence"] == "medium"

    def test_defaults_source_to_stdin(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["entry"]["source"] == "stdin"

    def test_explicit_fields_are_not_overwritten(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        payload = {"what": "a", "why": "b", "confidence": "high", "source": "custom", "tags": ["x"]}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["entry"]["confidence"] == "high"
        assert result["entry"]["source"] == "custom"
        assert result["entry"]["tags"] == ["x"]

    def test_rebuilds_current_json(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        run_capture(tmp_path, irp_dir, _Args(stdin=True))
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert len(current["active"]) == 1

    def test_second_capture_gets_next_sequence_number(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        run_capture(tmp_path, irp_dir, _Args(stdin=True))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "c", "why": "d"})))
        result2 = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result2["entry"]["id"].endswith("-002")

    def test_integration_results_surfaced(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        import commands.capture as capture_mod
        monkeypatch.setattr(
            capture_mod._dispatch, "run",
            lambda decision, project_root: [{"integration": "obsidian", "status": "ok", "path": "/x.md"}],
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert result["integrations"][0]["status"] == "ok"
        assert "obsidian" in result["text"]

    def test_integration_error_surfaced_in_text(self, tmp_path, monkeypatch):
        irp_dir = ensure_irp_dir(tmp_path)
        import commands.capture as capture_mod
        monkeypatch.setattr(
            capture_mod._dispatch, "run",
            lambda decision, project_root: [{"integration": "mempalace", "status": "error", "error": "boom"}],
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"what": "a", "why": "b"})))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=True))
        assert "boom" in result["text"]


# ── run_capture (interactive path) ────────────────────────────────────────────

class TestRunCaptureInteractive:
    def test_skip_when_not_confirmed(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        answers = iter(["What was decided?", "Because", "high", "s"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=False))
        assert result["status"] == "skipped"
        assert read_ledger(irp_dir) == []

    def test_captures_when_confirmed(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        answers = iter(["Ship the thing", "Users asked for it", "high", "c"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=False))
        assert result["status"] == "captured"
        assert read_ledger(irp_dir)[0]["what"] == "Ship the thing"

    def test_interactive_source_is_interactive(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        answers = iter(["a", "b", "medium", "c"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=False))
        assert result["entry"]["source"] == "interactive"

    def test_invalid_confidence_falls_back_to_medium(self, tmp_path, monkeypatch):
        _no_integrations(monkeypatch)
        irp_dir = ensure_irp_dir(tmp_path)
        answers = iter(["a", "b", "not-a-level", "c"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        result = run_capture(tmp_path, irp_dir, _Args(stdin=False))
        assert result["entry"]["confidence"] == "medium"


# ── milestone messages ─────────────────────────────────────────────────────────

class TestMilestoneLines:
    def test_no_milestone_lines_below_threshold(self):
        ledger = [{"type": "decision", "id": f"IRP-{i}", "source": "cli"} for i in range(3)]
        new_entry = ledger[-1]
        assert _milestone_lines(ledger, new_entry) == []

    def test_milestone_at_first_decision(self):
        entry = {"type": "decision", "id": "IRP-1", "source": "cli"}
        lines = _milestone_lines([entry], entry)
        assert any("First decision" in line for line in lines)

    def test_milestone_at_ten(self):
        ledger = [{"type": "decision", "id": f"IRP-{i}", "source": "cli"} for i in range(10)]
        lines = _milestone_lines(ledger, ledger[-1])
        assert any("10 decisions" in line for line in lines)

    def test_first_sensor_use_flagged(self):
        ledger = [
            {"type": "decision", "id": "IRP-1", "source": "cli"},
            {"type": "decision", "id": "IRP-2", "source": "slack"},
        ]
        lines = _milestone_lines(ledger, ledger[-1])
        assert any("Slack" in line for line in lines)

    def test_repeat_sensor_use_not_flagged_again(self):
        ledger = [
            {"type": "decision", "id": "IRP-1", "source": "slack"},
            {"type": "decision", "id": "IRP-2", "source": "slack"},
        ]
        lines = _milestone_lines(ledger, ledger[-1])
        assert not any("First capture from" in line for line in lines)
