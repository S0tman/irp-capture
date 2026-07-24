"""Tests for irp export context / export decisions, the deterministic
markdown builders in irp/core/commands/export.py.

export.py also dispatches to commands.graph (export graph) and
commands.evidence (export evidence), both exercised by their own test
modules (test_dynamics.py via commands.graph, test_evidence_attest.py). Here
we cover run_export's own logic: _derive_rule, _build_agents_md,
_build_decisions_md, and the file-writing wrappers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, append_ledger_entry  # noqa: E402
from commands.export import (  # noqa: E402
    run_export,
    _derive_rule,
    _build_agents_md,
    _build_decisions_md,
    _is_decision,
)


class _Args:
    def __init__(self, **kwargs):
        defaults = dict(
            export_action="decisions", output=None, force=False, writable=False, demo=False,
            target=None, json=False,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


DECISION_SIMPLE = {
    "id": "IRP-2026-04-01-001", "type": "decision",
    "what": "Use PostgreSQL for the primary database",
    "why": "Relational model fits our schema",
    "confidence": "high", "tags": ["backend"], "timestamp": "2026-04-01", "source": "cli",
}
DECISION_LONG = {
    "id": "IRP-2026-04-02-001", "type": "decision",
    "what": "A" * 200,  # too long to become a rule
    "why": "Some reasoning",
    "confidence": "medium", "tags": [], "timestamp": "2026-04-02", "source": "cli",
}
DECISION_EMDASH = {
    "id": "IRP-2026-04-03-001", "type": "decision",
    # Intentional em dash: this fixture exists to exercise em-dash input handling.
    "what": "Ship it — carefully",
    "why": "x", "confidence": "high", "tags": [], "timestamp": "2026-04-03", "source": "cli",
}


# ── _is_decision ───────────────────────────────────────────────────────────────

class TestIsDecision:
    def test_type_decision_counts(self):
        assert _is_decision({"type": "decision"}) is True

    def test_what_and_why_without_type_counts(self):
        assert _is_decision({"what": "x", "why": "y", "type": None}) is True

    def test_retirement_type_does_not_count(self):
        assert _is_decision({"type": "retirement", "what": "x", "why": "y"}) is False

    def test_missing_why_does_not_count(self):
        assert _is_decision({"what": "x", "type": None}) is False


# ── _derive_rule ──────────────────────────────────────────────────────────────

class TestDeriveRule:
    def test_short_clean_decision_becomes_a_rule(self):
        rule = _derive_rule({"what": "use feature flags for rollout"})
        assert rule == "Use feature flags for rollout."

    def test_too_long_returns_none(self):
        assert _derive_rule({"what": "A" * 200}) is None

    def test_multi_sentence_returns_none(self):
        assert _derive_rule({"what": "Do X. Also do Y."}) is None

    def test_em_dash_returns_none(self):
        # Intentional em dash in the fixture: it verifies an em-dash "what" is not
        # turned into a rule. Test data exercising input handling, not prose.
        assert _derive_rule({"what": "Ship it — carefully"}) is None

    def test_empty_what_returns_none(self):
        assert _derive_rule({"what": ""}) is None

    def test_strips_known_prefixes(self):
        rule = _derive_rule({"what": "Decided to use SQLite"})
        assert rule == "Use SQLite."

    def test_colon_form_locked_prefix_allowed(self):
        rule = _derive_rule({"what": "Locked: use SQLite everywhere"})
        assert rule is not None
        assert rule.startswith("Use SQLite")

    def test_arbitrary_colon_rejected(self):
        assert _derive_rule({"what": "Note: something happened"}) is None


# ── _build_agents_md ───────────────────────────────────────────────────────────

class TestBuildAgentsMd:
    def test_empty_decisions_produces_placeholder(self):
        body = _build_agents_md([])
        assert "No confirmed decisions" in body

    def test_rule_appears_under_working_constraints(self):
        body = _build_agents_md([DECISION_SIMPLE])
        assert "Use PostgreSQL for the primary database." in body
        assert "IRP-2026-04-01-001" in body

    def test_non_rule_decision_appears_under_relevant_decisions_only(self):
        body = _build_agents_md([DECISION_LONG])
        assert "No decisions in the ledger could be deterministically converted" in body
        assert "IRP-2026-04-02-001" in body  # still listed as relevant

    def test_control_level_easy_section_present(self):
        body = _build_agents_md([DECISION_SIMPLE], control_level="easy")
        assert "Agent control level: easy" in body

    def test_unknown_control_level_falls_back_to_advanced(self):
        body = _build_agents_md([DECISION_SIMPLE], control_level="bogus")
        assert "Agent control level: advanced" in body


# ── _build_decisions_md ────────────────────────────────────────────────────────

class TestBuildDecisionsMd:
    def test_empty_decisions_produces_placeholder(self):
        body = _build_decisions_md([])
        assert "No decisions in `.irp/ledger.jsonl` yet." in body

    def test_newest_first_ordering(self):
        body = _build_decisions_md([DECISION_SIMPLE, DECISION_EMDASH])
        # DECISION_EMDASH (04-03) is newer, must appear before DECISION_SIMPLE (04-01)
        assert body.index("IRP-2026-04-03-001") < body.index("IRP-2026-04-01-001")

    def test_demo_note_present_when_demo_true(self):
        body = _build_decisions_md([DECISION_SIMPLE], demo=True)
        assert "sample data" in body

    def test_what_and_why_rendered(self):
        body = _build_decisions_md([DECISION_SIMPLE])
        assert "Use PostgreSQL for the primary database" in body
        assert "Relational model fits our schema" in body


# ── run_export dispatch + file writing ────────────────────────────────────────

class TestRunExportDecisions:
    def test_writes_decisions_md_by_default(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions"))
        assert result["status"] == "ok"
        out = Path(result["output_path"])
        assert out == tmp_path / "DECISIONS.md"
        assert out.exists()
        assert "Use PostgreSQL" in out.read_text(encoding="utf-8")

    def test_refuses_overwrite_without_force(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        run_export(tmp_path, irp_dir, _Args(export_action="decisions"))
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions"))
        assert result["status"] == "exists"

    def test_force_overwrites(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        run_export(tmp_path, irp_dir, _Args(export_action="decisions"))
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions", force=True))
        assert result["status"] == "ok"

    def test_output_file_is_readonly_by_default(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions"))
        out = Path(result["output_path"])
        assert not (out.stat().st_mode & 0o222)  # not writable by anyone

    def test_writable_flag_leaves_file_writable(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions", writable=True))
        out = Path(result["output_path"])
        assert out.stat().st_mode & 0o200  # owner-writable

    def test_demo_mode_uses_sample_data_not_ledger(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)  # empty ledger
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions", demo=True))
        assert result["status"] == "ok"
        assert result["decision_count"] > 0  # sample data has entries despite empty ledger

    def test_custom_output_path(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        custom = tmp_path / "custom" / "OUT.md"
        result = run_export(tmp_path, irp_dir, _Args(export_action="decisions", output=str(custom)))
        assert Path(result["output_path"]) == custom
        assert custom.exists()


class TestRunExportContext:
    def test_agents_md_target(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        result = run_export(tmp_path, irp_dir, _Args(export_action="context", target="agents.md"))
        assert result["status"] == "ok"
        out = Path(result["output_path"])
        assert out == tmp_path / "AGENTS.md"
        assert "control_level" in result

    def test_decisions_md_target_via_context(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        append_ledger_entry(irp_dir, DECISION_SIMPLE)
        result = run_export(tmp_path, irp_dir, _Args(export_action="context", target="decisions.md"))
        assert Path(result["output_path"]) == tmp_path / "DECISIONS.md"

    def test_unsupported_target_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_export(tmp_path, irp_dir, _Args(export_action="context", target="bogus.md"))
        assert result["status"] == "error"

    def test_unknown_export_action_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_export(tmp_path, irp_dir, _Args(export_action="bogus"))
        assert result["status"] == "error"
