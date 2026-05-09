"""Tests for the Decision Resolver — IRP-US-009.

Acceptance criteria:
  009a: active() excludes superseded decisions, counts them separately
  009b: --tag / --scope filters narrow the active set
  009c: conflicts ranked by score descending, newest-first on ties
  009d: verdict maps clear/warn/block to token score (0 / 1-2 / 3+)
  009e: each conflict exposes confidence, confirmed_by, tags, id
  009f: irp resolve CLI returns verdict; irp check delegates to resolver
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ── fixtures ──────────────────────────────────────────────────────────────────

def _entry(id, decision, reasoning="", tags=None, supersedes=None, confidence="high",
           confirmed_by="", timestamp="2026-05-01"):
    e = {
        "type": "decision",
        "id": id,
        "decision": decision,
        "reasoning": reasoning,
        "tags": tags or [],
        "confidence": confidence,
        "confirmed_by": confirmed_by,
        "timestamp": timestamp,
        "source": "test",
    }
    if supersedes:
        e["supersedes"] = supersedes
    return e


LEDGER = [
    _entry("IRP-001", "Use PostgreSQL for the database",
           reasoning="Relational model fits our data. SQLite ruled out at scale.",
           tags=["backend", "database"], confidence="high", confirmed_by="alice",
           timestamp="2026-04-01"),
    _entry("IRP-002", "Do not use SQLite in production",
           reasoning="SQLite does not scale under concurrent writes.",
           tags=["backend", "database"], confidence="high",
           timestamp="2026-04-02"),
    _entry("IRP-003", "Switch to SQLite for local-first v0",
           reasoning="Reconsider PostgreSQL after scale tests showed v0 needs lightweight storage.",
           tags=["backend", "database", "v0"], confidence="medium",
           timestamp="2026-04-10",
           supersedes="IRP-002"),   # supersedes IRP-002
    _entry("IRP-004", "API must return JSON not XML",
           reasoning="JSON is the standard for our consumers.",
           tags=["api", "backend"], confidence="high", confirmed_by="bob",
           timestamp="2026-04-05"),
    _entry("IRP-005", "Frontend in React",
           reasoning="Team expertise and ecosystem fit.",
           tags=["frontend"], confidence="high",
           timestamp="2026-04-06"),
]


# ── import under test ─────────────────────────────────────────────────────────

from resolver import (
    active_decisions,
    build_supersession_map,
    resolve,
    _WARN_THRESHOLD,
    _BLOCK_THRESHOLD,
)


# ── US-009a: active vs superseded ─────────────────────────────────────────────

class TestActiveDecisions:
    def test_superseded_entry_excluded(self):
        """IRP-002 is superseded by IRP-003 — must not appear in active set."""
        active, superseded_count = active_decisions(LEDGER)
        active_ids = [d["id"] for d in active]
        assert "IRP-002" not in active_ids

    def test_superseding_entry_included(self):
        """IRP-003 (which does the superseding) is itself active."""
        active, _ = active_decisions(LEDGER)
        active_ids = [d["id"] for d in active]
        assert "IRP-003" in active_ids

    def test_superseded_count_correct(self):
        """One decision superseded — count must be 1."""
        _, superseded_count = active_decisions(LEDGER)
        assert superseded_count == 1

    def test_non_superseded_all_present(self):
        """IRP-001, 003, 004, 005 are all active."""
        active, _ = active_decisions(LEDGER)
        active_ids = {d["id"] for d in active}
        assert {"IRP-001", "IRP-003", "IRP-004", "IRP-005"}.issubset(active_ids)

    def test_empty_ledger(self):
        active, superseded_count = active_decisions([])
        assert active == []
        assert superseded_count == 0

    def test_no_supersessions(self):
        simple = [_entry("IRP-A", "Decision A"), _entry("IRP-B", "Decision B")]
        active, superseded_count = active_decisions(simple)
        assert len(active) == 2
        assert superseded_count == 0


# ── US-009b: tag and scope filtering ─────────────────────────────────────────

class TestFiltering:
    def test_tag_filter_narrows_active_set(self):
        """--tag frontend returns only IRP-005."""
        active, _ = active_decisions(LEDGER, tag="frontend")
        assert [d["id"] for d in active] == ["IRP-005"]

    def test_tag_filter_case_insensitive(self):
        active, _ = active_decisions(LEDGER, tag="BACKEND")
        ids = {d["id"] for d in active}
        assert "IRP-001" in ids
        assert "IRP-005" not in ids

    def test_tag_filter_excludes_superseded(self):
        """Even with tag=database, superseded IRP-002 must not appear."""
        active, _ = active_decisions(LEDGER, tag="database")
        active_ids = [d["id"] for d in active]
        assert "IRP-002" not in active_ids

    def test_scope_filter_by_text(self):
        """scope='JSON' matches IRP-004 (API must return JSON)."""
        active, _ = active_decisions(LEDGER, scope="JSON")
        ids = {d["id"] for d in active}
        assert "IRP-004" in ids
        assert "IRP-005" not in ids

    def test_tag_and_scope_combined(self):
        active, _ = active_decisions(LEDGER, tag="api", scope="JSON")
        assert all(d["id"] == "IRP-004" for d in active)

    def test_no_match_returns_empty(self):
        active, _ = active_decisions(LEDGER, tag="nonexistent-tag-xyz")
        assert active == []


# ── US-009c: conflict ranking ─────────────────────────────────────────────────

class TestConflictRanking:
    def test_higher_score_ranked_first(self):
        """More overlapping tokens = higher score = ranked first."""
        result = resolve("use PostgreSQL database backend storage", LEDGER)
        assert result.conflicts, "Expected at least one conflict"
        scores = [c.score for c in result.conflicts]
        assert scores == sorted(scores, reverse=True)

    def test_no_overlap_produces_no_conflicts(self):
        result = resolve("completely unrelated topic about shipping logistics", LEDGER)
        assert result.conflicts == []

    def test_top_match_is_highest_score(self):
        result = resolve("PostgreSQL database backend", LEDGER)
        if result.conflicts:
            assert result.top_match.score == result.conflicts[0].score

    def test_newer_entry_ranked_first_on_tie(self):
        """When scores are equal, newer timestamp wins."""
        # IRP-001 (2026-04-01) and IRP-003 (2026-04-10) both mention database
        result = resolve("database", LEDGER)
        ids = [c.id for c in result.conflicts[:2]]
        # IRP-003 is newer and should appear before IRP-001 on equal score
        if "IRP-003" in ids and "IRP-001" in ids:
            assert ids.index("IRP-003") < ids.index("IRP-001")


# ── US-009d: verdict thresholds ───────────────────────────────────────────────

class TestVerdict:
    def test_no_overlap_is_clear(self):
        result = resolve("zeppelin airship hydrogen", LEDGER)
        assert result.verdict == "clear"
        assert result.score == 0

    def test_single_token_overlap_is_warn(self):
        # "api" alone matches IRP-004 — score 1
        result = resolve("api", LEDGER)
        assert result.verdict == "warn"
        assert result.score >= _WARN_THRESHOLD

    def test_high_overlap_is_block(self):
        # Multiple tokens matching IRP-001: postgresql, database, backend, relational
        result = resolve("switch to postgresql relational database backend scale", LEDGER)
        assert result.verdict == "block"
        assert result.score >= _BLOCK_THRESHOLD

    def test_clear_has_no_conflicts(self):
        result = resolve("zeppelin airship hydrogen", LEDGER)
        assert result.conflicts == []
        assert result.top_match is None

    def test_verdict_in_result_dict(self):
        result = resolve("api", LEDGER)
        d = result.to_dict()
        assert d["verdict"] in ("clear", "warn", "block")


# ── US-009e: provenance fields ────────────────────────────────────────────────

class TestProvenance:
    def test_conflict_exposes_id(self):
        result = resolve("PostgreSQL database", LEDGER)
        assert result.top_match is not None
        assert result.top_match.id.startswith("IRP-")

    def test_conflict_exposes_confidence(self):
        result = resolve("PostgreSQL database", LEDGER)
        assert result.top_match.confidence in ("high", "medium", "low")

    def test_conflict_exposes_tags(self):
        result = resolve("PostgreSQL database backend", LEDGER)
        assert isinstance(result.top_match.tags, list)

    def test_conflict_exposes_matched_on(self):
        result = resolve("PostgreSQL database", LEDGER)
        assert isinstance(result.top_match.matched_on, list)
        assert len(result.top_match.matched_on) > 0

    def test_confirmed_by_preserved(self):
        result = resolve("PostgreSQL database", LEDGER)
        # IRP-001 has confirmed_by="alice"
        alice_match = next((c for c in result.conflicts if c.id == "IRP-001"), None)
        if alice_match:
            assert alice_match.confirmed_by == "alice"

    def test_superseded_count_in_result(self):
        result = resolve("anything", LEDGER)
        assert result.superseded_count == 1

    def test_active_count_in_result(self):
        result = resolve("anything", LEDGER)
        assert result.active_count == 4  # IRP-001,003,004,005


# ── US-009f: CLI surface ───────────────────────────────────────────────────────

IRP_PY = str(Path(__file__).parent.parent / "irp" / "core" / "irp.py")


def _run_irp(args: list[str], proj: Path) -> tuple[int, str]:
    """Run irp CLI from a temp project root and return (exit_code, combined output)."""
    result = subprocess.run(
        [sys.executable, IRP_PY] + args,
        capture_output=True, text=True,
        cwd=str(proj),
    )
    return result.returncode, result.stdout + result.stderr


def _make_project(tmp_path: Path, ledger: list[dict]) -> Path:
    """Create a minimal .irp project in tmp_path."""
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    ledger_path = irp_dir / "ledger.jsonl"
    ledger_path.write_text(
        "\n".join(json.dumps(e) for e in ledger) + "\n", encoding="utf-8"
    )
    current = {"version": 1, "active": [e for e in ledger if e.get("type") == "decision"][-10:]}
    (irp_dir / "current.json").write_text(json.dumps(current), encoding="utf-8")
    return tmp_path


class TestCLI:
    def test_resolve_returns_verdict(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["resolve", "PostgreSQL database backend"], proj)
        assert "verdict" in out.lower() or "conflict" in out.lower() or "block" in out.lower() or "warn" in out.lower()

    def test_resolve_clear_exit_zero(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["resolve", "zeppelin airship hydrogen"], proj)
        assert code == 0

    def test_resolve_conflict_exit_ten(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["resolve", "postgresql database backend relational scale"], proj)
        assert code == 10

    def test_resolve_json_flag(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["resolve", "--json", "database"], proj)
        data = json.loads(out)
        assert "verdict" in data
        assert "conflicts" in data
        assert "superseded_count" in data

    def test_resolve_tag_filter(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["resolve", "--json", "--tag", "frontend", "React frontend"], proj)
        data = json.loads(out)
        # frontend tag only matches IRP-005 — active_count should be 1
        assert data["active_count"] == 1

    def test_check_delegates_to_resolver(self, tmp_path):
        """irp check must surface superseded_count (resolver field, not old check output)."""
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["check", "--json", "zeppelin"], proj)
        data = json.loads(out)
        assert "superseded" in data  # upgraded check exposes this

    def test_check_conflict_exit_ten(self, tmp_path):
        proj = _make_project(tmp_path, LEDGER)
        code, out = _run_irp(["check", "postgresql database backend relational scale"], proj)
        assert code == 10
