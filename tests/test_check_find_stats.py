"""Tests for irp check, irp find, and irp stats, direct function-level tests
of the command handlers (not just the CLI plumbing already covered by
test_resolver.py / test_gate.py).
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, append_ledger_entry, write_current  # noqa: E402
from commands.check import run_check  # noqa: E402
from commands.find import run_find  # noqa: E402
from commands.stats import run_stats  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _seed_ledger(irp_dir, entries):
    for e in entries:
        append_ledger_entry(irp_dir, e)
    active = [e for e in entries if e.get("type") == "decision"]
    write_current(irp_dir, {"version": 1, "active": active})


LEDGER = [
    {"type": "decision", "id": "IRP-001", "decision": "no", "what": "Do not delete auth module",
     "why": "Auth module is shared across all services.", "reasoning": "shared services",
     "tags": ["security", "auth"], "confidence": "high", "timestamp": "2026-04-01", "source": "cli"},
    {"type": "decision", "id": "IRP-002", "what": "Use PostgreSQL", "why": "Relational fit",
     "reasoning": "Relational fit for our schema.", "tags": ["backend", "database"],
     "confidence": "high", "timestamp": "2026-04-02", "source": "slack"},
]


# ── irp check ──────────────────────────────────────────────────────────────────

class TestRunCheck:
    def test_clear_status_no_conflict(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_check(tmp_path, irp_dir, _Args(proposal="deploy zeppelin airship"))
        assert result["status"] == "clear"
        assert result["checked"] == 2

    def test_conflict_status_reports_match(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_check(tmp_path, irp_dir, _Args(proposal="delete the auth module completely"))
        assert result["status"] == "conflict"
        assert result["match_id"] == "IRP-001"

    def test_text_contains_proposal(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_check(tmp_path, irp_dir, _Args(proposal="deploy zeppelin airship"))
        assert "deploy zeppelin airship" in result["text"]

    def test_verdict_field_present_on_conflict(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_check(tmp_path, irp_dir, _Args(proposal="delete auth module security"))
        assert result["verdict"] in ("warn", "block")

    def test_empty_ledger_is_clear(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_check(tmp_path, irp_dir, _Args(proposal="anything at all"))
        assert result["status"] == "clear"
        assert result["checked"] == 0


# ── irp find ───────────────────────────────────────────────────────────────────

class TestRunFind:
    def test_finds_matching_ledger_entry(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_find(tmp_path, irp_dir, _Args(query="PostgreSQL", ledger_only=False, craft_only=False, graph=False))
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["results"][0]["id"] == "IRP-002"

    def test_no_match_returns_no_results(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_find(tmp_path, irp_dir, _Args(query="zzz_nonexistent_zzz", ledger_only=False, craft_only=False, graph=False))
        assert result["status"] == "no_results"
        assert result["count"] == 0

    def test_case_insensitive_search(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_find(tmp_path, irp_dir, _Args(query="postgresql", ledger_only=False, craft_only=False, graph=False))
        assert result["count"] == 1

    def test_ledger_only_flag_excludes_craft(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        (irp_dir / "craft.jsonl").write_text(
            json.dumps({"id": "CRAFT-1", "what": "PostgreSQL tip"}) + "\n", encoding="utf-8"
        )
        result = run_find(tmp_path, irp_dir, _Args(query="PostgreSQL", ledger_only=True, craft_only=False, graph=False))
        sources = {r["source"] for r in result["results"]}
        assert sources == {"ledger"}

    def test_craft_only_flag_excludes_ledger(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        (irp_dir / "craft.jsonl").write_text(
            json.dumps({"id": "CRAFT-1", "what": "PostgreSQL tip"}) + "\n", encoding="utf-8"
        )
        result = run_find(tmp_path, irp_dir, _Args(query="PostgreSQL", ledger_only=False, craft_only=True, graph=False))
        sources = {r["source"] for r in result["results"]}
        assert sources == {"craft"}

    def test_regex_query_matches(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_find(tmp_path, irp_dir, _Args(query="auth|Postgre", ledger_only=False, craft_only=False, graph=False))
        assert result["count"] == 2

    def test_invalid_regex_falls_back_to_literal_search(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        # "(" is an invalid regex on its own, must fall back to literal escape.
        result = run_find(tmp_path, irp_dir, _Args(query="(", ledger_only=False, craft_only=False, graph=False))
        assert result["status"] == "no_results"

    def test_hits_include_field_name(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_find(tmp_path, irp_dir, _Args(query="PostgreSQL", ledger_only=False, craft_only=False, graph=False))
        assert any(hit.startswith("what:") for hit in result["results"][0]["hits"])


# ── irp stats ──────────────────────────────────────────────────────────────────

class TestRunStats:
    def test_empty_ledger_status(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=False))
        assert result["status"] == "empty"

    def test_demo_flag_returns_sample_data(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_stats(tmp_path, irp_dir, _Args(demo=True, json=False))
        assert result["demo"] is True
        assert "sample data" in result["text"]

    def test_populated_ledger_computes_total(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=True))
        assert result["stats"]["total"] == 2

    def test_sources_are_counted(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=True))
        assert result["stats"]["sources"] == {"cli": 1, "slack": 1}

    def test_top_tags_computed(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=True))
        tags = dict(result["stats"]["top_tags"])
        assert tags.get("security") == 1
        assert tags.get("database") == 1

    def test_json_mode_has_no_text_key_when_json_requested(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=True))
        assert "text" not in result

    def test_non_json_mode_includes_text(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _seed_ledger(irp_dir, LEDGER)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=False))
        assert "text" in result
        assert "No telemetry" in result["text"]
