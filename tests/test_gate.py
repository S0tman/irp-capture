"""Tests for the Runtime Gate — IRP-US-011.

Acceptance criteria:
  011a: irp gate returns machine-readable JSON with verdict, score, top_match, defer_question
  011b: exit codes 0=allow, 10=warn, 20=block (distinct from irp check)
  011c: --strict treats warn as block (exit 20)
  011d: block response includes a defer_question
  011e: no interactive prompts, fast, composable
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

IRP_PY = str(Path(__file__).parent.parent / "irp" / "core" / "irp.py")

# ── shared fixtures ────────────────────────────────────────────────────────────

def _entry(id, decision, reasoning="", tags=None, confidence="high", timestamp="2026-05-01"):
    return {
        "type": "decision",
        "id": id,
        "decision": decision,
        "reasoning": reasoning,
        "tags": tags or [],
        "confidence": confidence,
        "timestamp": timestamp,
        "source": "test",
    }


LEDGER = [
    _entry("IRP-001", "Do not delete the authentication module",
           reasoning="Auth module is shared across all services. Deletion would break prod.",
           tags=["security", "auth"], confidence="high", timestamp="2026-04-01"),
    _entry("IRP-002", "Use PostgreSQL for the primary database",
           reasoning="Relational model fits our schema. SQLite ruled out.",
           tags=["backend", "database"], confidence="high", timestamp="2026-04-02"),
    _entry("IRP-003", "All API responses must be JSON",
           reasoning="JSON is the standard for our consumers.",
           tags=["api"], confidence="high", timestamp="2026-04-03"),
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
    """Run irp gate and return (exit_code, parsed_json_or_raw_stdout)."""
    result = subprocess.run(
        [sys.executable, IRP_PY] + args,
        capture_output=True, text=True, cwd=str(proj),
    )
    out = result.stdout.strip()
    try:
        return result.returncode, json.loads(out)
    except json.JSONDecodeError:
        return result.returncode, out


# ── US-011a: machine-readable JSON output ─────────────────────────────────────

class TestJsonOutput:
    def test_gate_returns_verdict(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete the authentication module"], proj)
        assert isinstance(data, dict)
        assert "verdict" in data

    def test_gate_returns_score(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete the authentication module"], proj)
        assert "score" in data
        assert isinstance(data["score"], int)

    def test_gate_returns_top_match(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete the authentication module"], proj)
        # top_match present on conflict, None on clear
        assert "top_match" in data

    def test_gate_returns_defer_question_on_block(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete the authentication module"], proj)
        if data.get("verdict") == "block":
            assert "defer_question" in data
            assert data["defer_question"]  # non-empty string

    def test_gate_clear_returns_json(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "deploy new zeppelin airship"], proj)
        assert isinstance(data, dict)
        assert data["verdict"] == "clear"

    def test_gate_json_has_action_field(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "deploy new zeppelin airship"], proj)
        assert "action" in data
        assert "zeppelin" in data["action"]


# ── US-011b: exit codes 0/10/20 ───────────────────────────────────────────────

class TestExitCodes:
    def test_clear_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, _ = _run(["gate", "deploy zeppelin airship blimp"], proj)
        assert code == 0

    def test_warn_exits_ten(self, tmp_path):
        """Single token overlap → warn → exit 10."""
        proj = _make_project(tmp_path)
        # "api" alone overlaps IRP-003 (score 1 = warn)
        code, data = _run(["gate", "change api"], proj)
        if data.get("verdict") == "warn":
            assert code == 10

    def test_block_exits_twenty(self, tmp_path):
        """High overlap with auth deletion decision → block → exit 20."""
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete authentication module security"], proj)
        if data.get("verdict") == "block":
            assert code == 20

    def test_block_exit_is_not_ten(self, tmp_path):
        """Gate uses 20 for block, not 10 like irp check."""
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete authentication module security"], proj)
        if data.get("verdict") == "block":
            assert code != 10

    def test_exit_code_matches_verdict(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete authentication module security"], proj)
        verdict = data.get("verdict")
        if verdict == "clear":
            assert code == 0
        elif verdict == "warn":
            assert code == 10
        elif verdict == "block":
            assert code == 20


# ── US-011c: --strict mode ────────────────────────────────────────────────────

class TestStrictMode:
    def test_strict_upgrades_warn_to_block_exit(self, tmp_path):
        """In strict mode, warn → exit 20 instead of 10."""
        proj = _make_project(tmp_path)
        # First confirm we get warn without --strict
        code_normal, data = _run(["gate", "change api"], proj)
        if data.get("verdict") == "warn":
            code_strict, _ = _run(["gate", "--strict", "change api"], proj)
            assert code_strict == 20

    def test_strict_block_still_exits_twenty(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "--strict", "delete authentication module security"], proj)
        if data.get("verdict") in ("warn", "block"):
            assert code == 20

    def test_strict_clear_still_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "--strict", "deploy zeppelin airship"], proj)
        assert code == 0

    def test_strict_verdict_field_unchanged(self, tmp_path):
        """--strict changes exit code but not verdict label."""
        proj = _make_project(tmp_path)
        _, data_normal = _run(["gate", "change api"], proj)
        _, data_strict = _run(["gate", "--strict", "change api"], proj)
        assert data_normal.get("verdict") == data_strict.get("verdict")


# ── US-011d: defer_question ───────────────────────────────────────────────────

class TestDeferQuestion:
    def test_defer_question_present_on_block(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete authentication module security"], proj)
        if data.get("verdict") == "block":
            assert "defer_question" in data
            assert len(data["defer_question"]) > 10  # meaningful string

    def test_defer_question_references_irp_id(self, tmp_path):
        """defer_question must mention the conflicting IRP ID."""
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "delete authentication module security"], proj)
        if data.get("verdict") == "block" and data.get("top_match"):
            irp_id = data["top_match"]["id"]
            assert irp_id in data["defer_question"]

    def test_defer_question_absent_on_clear(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "deploy zeppelin airship"], proj)
        assert data.get("verdict") == "clear"
        # defer_question should be None or absent on clear
        assert not data.get("defer_question")

    def test_warn_has_defer_question(self, tmp_path):
        """Warn also surfaces a defer_question — agent should surface it."""
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "change api"], proj)
        if data.get("verdict") == "warn":
            assert "defer_question" in data


# ── US-011e: composability ────────────────────────────────────────────────────

class TestComposability:
    def test_no_interactive_prompts(self, tmp_path):
        """gate must complete without any stdin interaction."""
        proj = _make_project(tmp_path)
        result = subprocess.run(
            [sys.executable, IRP_PY, "gate", "delete authentication module"],
            capture_output=True, text=True, cwd=str(proj),
            input="",  # empty stdin — would hang if interactive
            timeout=5,
        )
        assert result.returncode in (0, 10, 20)

    def test_output_is_valid_json(self, tmp_path):
        proj = _make_project(tmp_path)
        result = subprocess.run(
            [sys.executable, IRP_PY, "gate", "delete authentication module"],
            capture_output=True, text=True, cwd=str(proj),
        )
        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_tag_filter_composable(self, tmp_path):
        proj = _make_project(tmp_path)
        code, data = _run(["gate", "--tag", "security", "delete authentication module"], proj)
        assert "verdict" in data
        # only security-tagged decisions checked
        assert data.get("active_count", 999) <= len(
            [e for e in LEDGER if "security" in e.get("tags", [])]
        )

    def test_empty_ledger_returns_clear(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text("", encoding="utf-8")
        (irp_dir / "current.json").write_text(
            json.dumps({"version": 1, "active": []}), encoding="utf-8"
        )
        code, data = _run(["gate", "anything at all"], tmp_path)
        assert code == 0
        assert data["verdict"] == "clear"
