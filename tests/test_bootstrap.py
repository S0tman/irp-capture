"""Tests for irp bootstrap, scanning git log and docs/files for decision
signals and recording them as *reconstructions*.

The rule these tests pin down: a guess made after the fact is never a captured
decision. `irp bootstrap` writes candidates to .irp/reconstructions.jsonl with
status "unconfirmed" and never touches ledger.jsonl or current.json. Only
`irp bootstrap --accept REC-...` (the human confirming) turns one into a normal
ledger decision.

Git operations use a real throwaway repo under tmp_path (local only, no
network). No live network is ever touched.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

import store  # noqa: E402
from store import ensure_irp_dir, read_ledger  # noqa: E402
import commands.bootstrap as bootstrap_mod  # noqa: E402
from commands.bootstrap import run_bootstrap  # noqa: E402

IRP_PY = str(REPO / "irp" / "core" / "irp.py")
TODAY = date.today().isoformat()
REC_ID_RE = re.compile(r"^REC-\d{4}-\d{2}-\d{2}-\d{3}$")


class _Args:
    def __init__(self, **kwargs):
        defaults = dict(
            from_source="all", path=None, dry_run=False, limit=50,
            write_report=False, accept=None,
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


def _git(args, cwd, env=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=env)


def _init_repo_with_commits(tmp_path, messages, when=None):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    env = None
    if when:
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    for i, msg in enumerate(messages):
        f = tmp_path / f"file{i}.txt"
        f.write_text(f"content {i}\n", encoding="utf-8")
        _git(["add", f.name], tmp_path)
        _git(["commit", "-q", "-m", msg], tmp_path, env=env)


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _recs(irp_dir):
    return _read_jsonl(irp_dir / "reconstructions.jsonl")


def _write_notes(tmp_path, lines):
    (tmp_path / "NOTES.md").write_text("# Notes\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


DOC_LINE_1 = "We decided to use feature flags for gradual rollout of new features."
DOC_LINE_2 = "We decided to keep the database schema in plain SQL migration files."
DOC_LINE_3 = "We decided to publish every release candidate to a staging index first."


def _bootstrap_docs(tmp_path, lines, **kwargs):
    irp_dir = ensure_irp_dir(tmp_path)
    _write_notes(tmp_path, lines)
    result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", **kwargs))
    return irp_dir, result


# bootstrap.py's git source (_run_git_log) reads `git log` from project_root
# (passed through as cwd), so these tests point run_bootstrap at a throwaway
# repo directly, without changing the test runner's working directory. That
# also means they genuinely exercise the cwd wiring: the process is still in
# the irp-capture checkout, so a regression to an implicit cwd would fail here.
class TestGitSource:
    def test_decision_commit_becomes_candidate(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["we decided to standardize on SQLite for local-first storage"])
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", dry_run=False))
        assert result["candidates_written"] == 1
        assert _recs(irp_dir)[0]["origin_mode"] == "bootstrap_git"

    def test_bootstrapped_flag_always_true(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for the backend"])
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert _recs(irp_dir)[0]["bootstrapped"] is True

    def test_confidence_is_always_low(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["we decided to migrate to cloud storage"])
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert _recs(irp_dir)[0]["confidence"] == "low"

    def test_commit_date_is_kept_as_estimate_not_as_the_record_date(self, tmp_path):
        _init_repo_with_commits(
            tmp_path, ["we decided to adopt PostgreSQL for the backend"],
            when="2024-03-05T10:00:00+0000",
        )
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        rec = _recs(irp_dir)[0]
        assert rec["decided_at_estimate"] == "2024-03-05"
        assert rec["recorded_at"].startswith(TODAY)
        assert "timestamp" not in rec

    def test_noise_commits_are_skipped(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["chore: bump deps", "fixup: typo"])
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert result["status"] == "empty"

    def test_commits_without_decision_language_are_skipped(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["update the login page layout"])
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert result["status"] == "empty"


class TestDocsSource:
    def test_decision_line_in_markdown_becomes_candidate(self, tmp_path):
        irp_dir, result = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        assert result["candidates_written"] == 1
        assert _recs(irp_dir)[0]["origin_mode"] == "bootstrap_docs"

    def test_docs_candidates_have_no_invented_decision_date(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec = _recs(irp_dir)[0]
        assert rec["decided_at_estimate"] is None
        assert rec["recorded_at"].startswith(TODAY)
        assert "timestamp" not in rec

    def test_headings_and_short_lines_are_skipped(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (tmp_path / "NOTES.md").write_text(
            "# We decided to use it\n\nWe decided.\n", encoding="utf-8"
        )
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "empty"

    def test_irp_internal_files_are_not_scanned(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / "internal.md").write_text(
            "we decided to use internal notes only for this ledger entry text\n", encoding="utf-8"
        )
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "empty"

    def test_no_markdown_files_reports_empty(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "empty"

    def test_long_context_line_does_not_cut_off_the_provenance_note(self, tmp_path):
        long_context = "context " * 40
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1, long_context])
        why = _recs(irp_dir)[0]["why"]
        assert "NOTES.md" in why


class TestWriteModeLeavesLedgerAlone:
    """(a) write mode appends REC lines and nothing else."""

    def test_ledger_and_current_are_untouched(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        ledger_before = (irp_dir / "ledger.jsonl").read_bytes()
        current_before = (irp_dir / "current.json").read_bytes()
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "written"
        assert (irp_dir / "ledger.jsonl").read_bytes() == ledger_before
        assert (irp_dir / "current.json").read_bytes() == current_before
        assert read_ledger(irp_dir) == []

    def test_existing_ledger_is_byte_identical_after_a_run(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        entry = {
            "type": "decision", "id": "IRP-2026-01-01-001", "what": "Use Postgres",
            "why": "Joins", "confidence": "high", "timestamp": "2026-01-01", "source": "cli",
        }
        (irp_dir / "ledger.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        (irp_dir / "current.json").write_text(
            json.dumps({"version": 1, "active": [entry]}), encoding="utf-8"
        )
        ledger_before = (irp_dir / "ledger.jsonl").read_bytes()
        current_before = (irp_dir / "current.json").read_bytes()
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert (irp_dir / "ledger.jsonl").read_bytes() == ledger_before
        assert (irp_dir / "current.json").read_bytes() == current_before

    def test_rec_lines_carry_the_candidate_fields(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec = _recs(irp_dir)[0]
        assert rec["status"] == "unconfirmed"
        assert rec["bootstrapped"] is True
        assert rec["type"] == "decision"
        assert rec["what"] == DOC_LINE_1
        assert rec["confidence"] == "low"
        assert rec["origin_mode"] == "bootstrap_docs"
        assert rec["source_ref"] == "NOTES.md"
        assert "bootstrap" in rec["tags"]
        assert REC_ID_RE.match(rec["id"])

    def test_text_says_nothing_was_added_to_the_ledger_and_how_to_accept(self, tmp_path):
        _, result = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        text = result["text"]
        assert "reconstructions.jsonl" in text
        assert "--accept" in text
        assert "nothing" in text.lower() and "ledger" in text.lower()
        # The old wording claimed entries were written to the ledger.
        assert "Written to ledger" not in text
        assert "ledger.jsonl  ← updated" not in text


class TestRecIds:
    """(b) REC-YYYY-MM-DD-NNN, numbered per day across the file."""

    def test_id_format_and_date_is_the_recording_day(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        assert REC_ID_RE.match(rec_id)
        assert rec_id.startswith(f"REC-{TODAY}-")

    def test_numbers_run_001_002_003_within_a_run(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1, DOC_LINE_2, DOC_LINE_3])
        assert [r["id"] for r in _recs(irp_dir)] == [
            f"REC-{TODAY}-001", f"REC-{TODAY}-002", f"REC-{TODAY}-003",
        ]

    def test_numbering_continues_across_runs(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert [r["id"] for r in _recs(irp_dir)] == [f"REC-{TODAY}-001", f"REC-{TODAY}-002"]

    def test_other_days_do_not_affect_todays_numbering(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        old = {"id": "REC-2020-01-01-009", "what": "An old guess", "status": "unconfirmed"}
        (irp_dir / "reconstructions.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert _recs(irp_dir)[-1]["id"] == f"REC-{TODAY}-001"

    def test_gaps_in_todays_numbers_do_not_cause_reuse(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        seeded = {"id": f"REC-{TODAY}-007", "what": "Seeded guess", "status": "unconfirmed"}
        (irp_dir / "reconstructions.jsonl").write_text(json.dumps(seeded) + "\n", encoding="utf-8")
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert _recs(irp_dir)[-1]["id"] == f"REC-{TODAY}-008"


class TestDeduplication:
    def test_duplicate_what_is_skipped_on_rerun(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        result2 = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert result2["status"] == "empty"
        assert len(_recs(irp_dir)) == 1

    def test_skips_what_already_in_the_ledger(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        entry = {
            "type": "decision", "id": "IRP-2026-01-01-001", "what": DOC_LINE_1.upper(),
            "why": "Because", "confidence": "high", "timestamp": "2026-01-01", "source": "cli",
        }
        (irp_dir / "ledger.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
        _write_notes(tmp_path, [DOC_LINE_1])
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "empty"
        assert _recs(irp_dir) == []

    def test_skips_what_already_in_reconstructions(self, tmp_path):
        """(c) A guess that was already recorded is not recorded twice."""
        irp_dir = ensure_irp_dir(tmp_path)
        existing = {"id": f"REC-{TODAY}-001", "what": DOC_LINE_1.lower(), "status": "unconfirmed"}
        (irp_dir / "reconstructions.jsonl").write_text(json.dumps(existing) + "\n", encoding="utf-8")
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["candidates_written"] == 1
        assert [r["what"] for r in _recs(irp_dir)][-1] == DOC_LINE_2
        assert len(_recs(irp_dir)) == 2

    def test_accepted_reconstructions_still_block_a_rerun(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["status"] == "empty"
        assert len(_recs(irp_dir)) == 1


class TestLimit:
    def test_limit_caps_recorded_entries(self, tmp_path, monkeypatch):
        messages = [f"we decided to adopt tool number {i} for the pipeline" for i in range(5)]
        _init_repo_with_commits(tmp_path, messages)
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", limit=2))
        assert result["candidates_written"] == 2
        assert len(_recs(irp_dir)) == 2
        assert read_ledger(irp_dir) == []


class TestDryRun:
    """(d) --dry-run previews with provisional REC ids and writes nothing."""

    def test_dry_run_writes_nothing(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        ledger_before = (irp_dir / "ledger.jsonl").read_bytes()
        current_before = (irp_dir / "current.json").read_bytes()
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", dry_run=True))
        assert result["status"] == "dry_run"
        assert result["candidates_written"] == 0
        assert not (irp_dir / "reconstructions.jsonl").exists()
        assert (irp_dir / "ledger.jsonl").read_bytes() == ledger_before
        assert (irp_dir / "current.json").read_bytes() == current_before
        assert not (irp_dir / "bootstrap_reports").exists()

    def test_dry_run_wording_does_not_overclaim(self, tmp_path):
        """main() creates .irp/ and --write-report writes a report, so 'nothing was
        written' would be false. Say what is true."""
        _write_notes(tmp_path, [DOC_LINE_1])
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", dry_run=True))
        text = result["text"]
        assert "Nothing was written" not in text
        assert "Nothing was added to the ledger or to reconstructions.jsonl" in text

    def test_dry_run_with_a_report_says_where_the_report_went(self, tmp_path):
        _write_notes(tmp_path, [DOC_LINE_1])
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(
            tmp_path, irp_dir, _Args(from_source="docs", dry_run=True, write_report=True))
        assert "Report written: .irp/bootstrap_reports/" in result["text"]
        assert "Nothing was written" not in result["text"]
        assert not (irp_dir / "reconstructions.jsonl").exists()

    def test_dry_run_accept_wording_does_not_overclaim(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id], dry_run=True))
        assert "Nothing was written" not in result["text"]
        assert "Nothing was added to the ledger or to reconstructions.jsonl" in result["text"]

    def test_dry_run_assigns_provisional_rec_ids(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", dry_run=True))
        assert result["status"] == "dry_run"
        assert result["entries"][0]["id"] == f"REC-{TODAY}-001"
        assert "REC-" in result["text"]
        assert "IRP-" not in result["entries"][0]["id"]

    def test_dry_run_ids_continue_from_existing_reconstructions(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", dry_run=True))
        assert result["entries"][0]["id"] == f"REC-{TODAY}-002"
        assert len(_recs(irp_dir)) == 1

    def test_dry_run_with_accept_previews_and_writes_nothing(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        before = (irp_dir / "reconstructions.jsonl").read_bytes()
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id], dry_run=True))
        assert result["status"] == "dry_run"
        assert read_ledger(irp_dir) == []
        assert (irp_dir / "reconstructions.jsonl").read_bytes() == before


class TestAccept:
    """(e) accepting is the human confirmation."""

    def test_accept_creates_a_confirmed_ledger_decision(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec = _recs(irp_dir)[0]
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec["id"]]))
        assert result["status"] == "accepted"

        ledger = read_ledger(irp_dir)
        assert len(ledger) == 1
        entry = ledger[0]
        assert entry["id"] == f"IRP-{TODAY}-001"
        assert entry["type"] == "decision"
        assert entry["what"] == rec["what"]
        assert entry["why"] == rec["why"]
        assert entry["confidence"] == rec["confidence"]
        assert "reconstructed" in entry["tags"]
        assert entry["source"] == "bootstrap-accepted"
        assert entry["origin_mode"] == rec["origin_mode"]
        assert entry["source_ref"] == rec["source_ref"]
        assert entry["reconstructed_from"] == rec["id"]
        assert entry["decided_at_estimate"] == rec["decided_at_estimate"]
        assert entry["timestamp"] == TODAY
        assert "bootstrapped" not in entry
        assert "status" not in entry

    def test_accept_rebuilds_current_json(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in current["active"]] == [f"IRP-{TODAY}-001"]

    def test_accept_marks_the_rec_line_accepted(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        first, second = _recs(irp_dir)
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[first["id"]]))
        recs = _recs(irp_dir)
        assert recs[0]["status"] == "accepted"
        assert recs[0]["accepted_as"] == f"IRP-{TODAY}-001"
        assert recs[0]["accepted_at"].startswith(TODAY)
        # Everything else on the line, and the other line, is left alone.
        assert recs[0]["what"] == first["what"]
        assert recs[0]["bootstrapped"] is True
        assert recs[1] == second

    def test_second_accept_of_the_same_id_writes_nothing(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        ledger_before = (irp_dir / "ledger.jsonl").read_bytes()
        recs_before = (irp_dir / "reconstructions.jsonl").read_bytes()
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        assert (irp_dir / "ledger.jsonl").read_bytes() == ledger_before
        assert (irp_dir / "reconstructions.jsonl").read_bytes() == recs_before
        assert result["accepted"] == []
        assert result["problems"][0]["rec_id"] == rec_id
        assert "already accepted" in result["text"].lower()

    def test_unknown_id_is_reported_and_writes_nothing(self, tmp_path):
        """(f) an unknown REC id."""
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        ledger_before = (irp_dir / "ledger.jsonl").read_bytes()
        recs_before = (irp_dir / "reconstructions.jsonl").read_bytes()
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=["REC-1999-01-01-001"]))
        assert (irp_dir / "ledger.jsonl").read_bytes() == ledger_before
        assert (irp_dir / "reconstructions.jsonl").read_bytes() == recs_before
        assert result["accepted"] == []
        assert result["problems"][0]["rec_id"] == "REC-1999-01-01-001"
        assert "not found" in result["text"].lower()

    def test_accept_with_no_reconstructions_file_is_a_clean_report(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=["REC-1999-01-01-001"]))
        assert result["accepted"] == []
        assert read_ledger(irp_dir) == []
        assert not (irp_dir / "reconstructions.jsonl").exists()

    def test_several_ids_in_one_call_and_a_bad_one_does_not_block_the_rest(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1, DOC_LINE_2, DOC_LINE_3])
        first, second, third = _recs(irp_dir)
        result = run_bootstrap(
            tmp_path, irp_dir,
            _Args(accept=[first["id"], "REC-1999-01-01-001", third["id"]]),
        )
        assert [a["rec_id"] for a in result["accepted"]] == [first["id"], third["id"]]
        assert [p["rec_id"] for p in result["problems"]] == ["REC-1999-01-01-001"]
        ledger = read_ledger(irp_dir)
        assert [e["id"] for e in ledger] == [f"IRP-{TODAY}-001", f"IRP-{TODAY}-002"]
        assert [e["reconstructed_from"] for e in ledger] == [first["id"], third["id"]]
        statuses = {r["id"]: r["status"] for r in _recs(irp_dir)}
        assert statuses == {
            first["id"]: "accepted", second["id"]: "unconfirmed", third["id"]: "accepted",
        }

    def test_accept_takes_the_next_id_after_every_ledger_line_including_legacy_guesses(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        legacy = {
            "type": "decision", "id": f"IRP-{TODAY}-004", "what": "An old guess",
            "why": "x", "confidence": "low", "timestamp": TODAY, "source": "bootstrap",
            "bootstrapped": True,
        }
        (irp_dir / "ledger.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        assert read_ledger(irp_dir)[-1]["id"] == f"IRP-{TODAY}-005"

    def test_accept_repairs_an_interrupted_accept_without_duplicating(self, tmp_path):
        """If the ledger append landed but the REC rewrite did not, a retry must
        not append a second decision for the same reconstruction."""
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec = _recs(irp_dir)[0]
        landed = {
            "type": "decision", "id": f"IRP-{TODAY}-001", "what": rec["what"],
            "why": rec["why"], "confidence": "low", "timestamp": TODAY,
            "source": "bootstrap-accepted", "reconstructed_from": rec["id"],
            "reconstruction_recorded_at": rec["recorded_at"],
        }
        (irp_dir / "ledger.jsonl").write_text(json.dumps(landed) + "\n", encoding="utf-8")
        # The crash also left current.json without the new decision.
        assert json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))["active"] == []
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec["id"]]))
        assert len(read_ledger(irp_dir)) == 1
        repaired = _recs(irp_dir)[0]
        assert repaired["status"] == "accepted"
        assert repaired["accepted_as"] == f"IRP-{TODAY}-001"
        assert [a["id"] for a in result["accepted"]] == [f"IRP-{TODAY}-001"]
        # The text says current.json was rebuilt, so it has to have been.
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in current["active"]] == [f"IRP-{TODAY}-001"]

    def test_rewrite_leaves_no_temp_files_and_keeps_unparseable_lines(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        path = irp_dir / "reconstructions.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        before = set(p.name for p in irp_dir.iterdir())
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        after = set(p.name for p in irp_dir.iterdir())
        assert after == before
        assert "{not json" in path.read_text(encoding="utf-8").splitlines()
        assert _read_jsonl_tolerant(path)[0]["status"] == "accepted"

    def test_rec_lines_never_leak_into_the_ledger_before_accept(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        assert read_ledger(irp_dir) == []
        assert len(_recs(irp_dir)) == 2


def _read_jsonl_tolerant(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


class TestReport:
    def test_write_report_creates_markdown_file(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", write_report=True))
        report_path = Path(result["report"])
        assert report_path.exists()
        assert "IRP Bootstrap Report" in report_path.read_text(encoding="utf-8")

    def test_report_says_nothing_was_added_to_the_ledger(self, tmp_path):
        irp_dir, result = _bootstrap_docs(tmp_path, [DOC_LINE_1], write_report=True)
        report = Path(result["report"]).read_text(encoding="utf-8")
        assert "reconstructions.jsonl" in report
        assert "--accept" in report
        assert "REC-" in report


# ── review follow-ups ──────────────────────────────────────────────────────────

class TestRecIdReuse:
    """REC ids are never reissued, and a repair only adopts a ledger entry that
    really is the same reconstruction."""

    def test_next_rec_id_counts_ids_the_ledger_already_used(self):
        ledger = [
            {"id": f"IRP-{TODAY}-001", "reconstructed_from": f"REC-{TODAY}-003"},
            {"id": f"IRP-{TODAY}-002", "reconstructed_from": "REC-2001-01-01-009"},
            {"id": f"IRP-{TODAY}-003"},
        ]
        assert store.next_rec_id([], ledger) == f"REC-{TODAY}-004"
        # With no ledger given, behaviour is unchanged.
        assert store.next_rec_id([{"id": f"REC-{TODAY}-001"}]) == f"REC-{TODAY}-002"

    def test_deleting_reconstructions_does_not_reissue_an_accepted_id(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        first = _recs(irp_dir)[0]
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[first["id"]]))
        (irp_dir / "reconstructions.jsonl").unlink()      # the only way to discard guesses

        _write_notes(tmp_path, [DOC_LINE_2])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        new = _recs(irp_dir)[0]
        assert new["what"] == DOC_LINE_2
        assert new["id"] == f"REC-{TODAY}-002"             # not -001 again

        run_bootstrap(tmp_path, irp_dir, _Args(accept=[new["id"]]))
        ledger = read_ledger(irp_dir)
        assert len(ledger) == 2
        assert ledger[1]["what"] == DOC_LINE_2
        assert ledger[1]["reconstructed_from"] == new["id"]
        assert _recs(irp_dir)[0]["accepted_as"] == ledger[1]["id"]

    def test_same_id_but_different_content_is_not_a_landed_accept(self, tmp_path):
        """Another clone (or a deleted file) can hand out the same REC id for a
        different guess. The old ledger entry must not be adopted for it."""
        irp_dir = ensure_irp_dir(tmp_path)
        rec_id = f"REC-{TODAY}-001"
        old_entry = {
            "type": "decision", "id": f"IRP-{TODAY}-001", "what": "The old guess text",
            "why": "x", "confidence": "low", "timestamp": TODAY, "source": "bootstrap-accepted",
            "reconstructed_from": rec_id, "reconstruction_recorded_at": "2026-01-01T10:00:00+00:00",
        }
        (irp_dir / "ledger.jsonl").write_text(json.dumps(old_entry) + "\n", encoding="utf-8")
        new_rec = {
            "id": rec_id, "type": "decision", "what": "A different guess in another clone",
            "why": "y", "confidence": "low", "tags": ["bootstrap"], "source": "bootstrap",
            "origin_mode": "bootstrap_docs", "source_ref": "N.md", "bootstrapped": True,
            "decided_at_estimate": None, "status": "unconfirmed",
            "recorded_at": "2026-02-02T11:00:00+00:00",
        }
        (irp_dir / "reconstructions.jsonl").write_text(json.dumps(new_rec) + "\n", encoding="utf-8")
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        ledger = read_ledger(irp_dir)
        assert [e["what"] for e in ledger] == ["The old guess text", "A different guess in another clone"]
        assert ledger[1]["id"] == f"IRP-{TODAY}-002"
        assert ledger[1]["reconstruction_recorded_at"] == "2026-02-02T11:00:00+00:00"
        assert _recs(irp_dir)[0]["accepted_as"] == f"IRP-{TODAY}-002"

    def test_accept_copies_the_recorded_at_onto_the_ledger_entry(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec = _recs(irp_dir)[0]
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec["id"]]))
        assert read_ledger(irp_dir)[0]["reconstruction_recorded_at"] == rec["recorded_at"]


@pytest.fixture
def needs_flock():
    pytest.importorskip("fcntl", reason="advisory locking needs fcntl (POSIX)")


class TestLockingAndDuplicates:
    def test_concurrent_runs_never_issue_the_same_rec_id(self, tmp_path, monkeypatch, needs_flock):
        irp_dir = ensure_irp_dir(tmp_path)
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2, DOC_LINE_3])
        real = store.next_rec_id

        def slow(*a, **k):
            result = real(*a, **k)
            time.sleep(0.05)       # widen the window between choosing an id and appending it
            return result

        monkeypatch.setattr(bootstrap_mod, "next_rec_id", slow)
        barrier = threading.Barrier(2)
        errors = []

        def work():
            try:
                barrier.wait()
                run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
            except Exception as exc:        # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        recs = _recs(irp_dir)
        ids = [r["id"] for r in recs]
        assert len(ids) == len(set(ids)), ids
        # The second run re-reads under the lock, so it finds nothing new to add.
        assert sorted(r["what"] for r in recs) == sorted([DOC_LINE_1, DOC_LINE_2, DOC_LINE_3])

    def _blocked_while_locked(self, irp_dir, action):
        """Run `action` in a thread while this thread holds the irp lock. It must
        not finish until the lock is released."""
        done = threading.Event()

        def work():
            action()
            done.set()

        with store.irp_lock(irp_dir):
            t = threading.Thread(target=work)
            t.start()
            assert not done.wait(0.4), "ran while another holder had the lock"
        t.join(timeout=10)
        assert done.is_set()

    def test_bootstrap_write_waits_for_the_lock(self, tmp_path, needs_flock):
        irp_dir = ensure_irp_dir(tmp_path)
        _write_notes(tmp_path, [DOC_LINE_1])
        self._blocked_while_locked(
            irp_dir, lambda: run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs")))
        assert len(_recs(irp_dir)) == 1

    def test_accept_waits_for_the_lock(self, tmp_path, needs_flock):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        self._blocked_while_locked(
            irp_dir, lambda: run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id])))
        assert len(read_ledger(irp_dir)) == 1

    def test_update_reconstruction_waits_for_the_lock(self, tmp_path, needs_flock):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        self._blocked_while_locked(
            irp_dir, lambda: store.update_reconstruction(irp_dir, rec_id, {"status": "x"}))
        assert _recs(irp_dir)[0]["status"] == "x"

    def test_the_lock_is_reentrant_within_one_thread(self, tmp_path, needs_flock):
        irp_dir = ensure_irp_dir(tmp_path)
        with store.irp_lock(irp_dir):
            with store.irp_lock(irp_dir):
                pass

    def test_dry_run_does_not_take_or_create_the_lock(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", dry_run=True))
        assert not (irp_dir / ".lock").exists()

    def test_accept_refuses_an_id_that_matches_more_than_one_line(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        path = irp_dir / "reconstructions.jsonl"
        line = path.read_text(encoding="utf-8")
        path.write_text(line + line, encoding="utf-8")             # same id twice
        rec_id = _recs(irp_dir)[0]["id"]
        before = path.read_bytes()
        result = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        assert result["accepted"] == []
        assert "more than one" in result["problems"][0]["reason"]
        assert read_ledger(irp_dir) == []
        assert path.read_bytes() == before


class TestSourceRefIsRelative:
    """A home folder and username must not be copied into a shared ledger."""

    def test_doc_in_a_subfolder_is_stored_relative_to_the_project(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "adr.md").write_text(DOC_LINE_1 + "\n", encoding="utf-8")
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        rec = _recs(irp_dir)[0]
        assert rec["source_ref"] == "docs/adr.md"
        assert str(tmp_path) not in json.dumps(rec)

    def test_doc_outside_the_project_falls_back_to_the_file_name(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "notes.md").write_text(DOC_LINE_1 + "\n", encoding="utf-8")
        irp_dir = ensure_irp_dir(project)
        run_bootstrap(project, irp_dir, _Args(from_source="docs", path=str(other)))
        rec = _recs(irp_dir)[0]
        assert rec["source_ref"] == "notes.md"
        assert str(tmp_path) not in json.dumps(rec)

    def _seed(self, irp_dir, source_ref):
        rec = {
            "id": f"REC-{TODAY}-001", "type": "decision", "what": "Seeded guess about caching",
            "why": "y", "confidence": "low", "tags": ["bootstrap", "docs"], "source": "bootstrap",
            "origin_mode": "bootstrap_docs", "source_ref": source_ref, "bootstrapped": True,
            "decided_at_estimate": None, "status": "unconfirmed",
            "recorded_at": "2026-02-02T11:00:00+00:00",
        }
        (irp_dir / "reconstructions.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        return rec["id"]

    def test_accept_relativizes_a_legacy_absolute_path_under_the_project(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        rec_id = self._seed(irp_dir, str(tmp_path / "docs" / "adr.md"))
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        entry = read_ledger(irp_dir)[0]
        assert entry["source_ref"] == "docs/adr.md"
        assert str(tmp_path) not in json.dumps(entry)

    def test_accept_keeps_only_the_file_name_for_an_absolute_path_outside_the_project(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        rec_id = self._seed(irp_dir, "/Users/someone/private/notes.md")
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        entry = read_ledger(irp_dir)[0]
        assert entry["source_ref"] == "notes.md"
        assert "someone" not in json.dumps(entry)

    def test_accept_leaves_a_commit_hash_alone(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        rec_id = self._seed(irp_dir, "a36f516891806c1188a5e52187d0180b74cc55f0")
        run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        assert read_ledger(irp_dir)[0]["source_ref"] == "a36f516891806c1188a5e52187d0180b74cc55f0"


def _cli(args, cwd):
    return subprocess.run(
        [sys.executable, IRP_PY] + args, cwd=cwd, capture_output=True, text=True)


class TestAcceptExitCode:
    def test_nothing_accepted_with_problems_exits_1(self, tmp_path):
        out = _cli(["bootstrap", "--accept", "REC-1999-01-01-001"], tmp_path)
        assert out.returncode == 1
        assert "not found" in out.stdout.lower()

    def test_already_accepted_exits_1(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        assert _cli(["bootstrap", "--accept", rec_id], tmp_path).returncode == 0
        again = _cli(["bootstrap", "--accept", rec_id], tmp_path)
        assert again.returncode == 1
        assert "already accepted" in again.stdout.lower()

    def test_a_successful_accept_exits_0(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        out = _cli(["bootstrap", "--accept", rec_id], tmp_path)
        assert out.returncode == 0
        assert len(read_ledger(irp_dir)) == 1

    def test_some_accepted_and_some_not_exits_0(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        rec_id = _recs(irp_dir)[0]["id"]
        out = _cli(["bootstrap", "--accept", rec_id, "REC-1999-01-01-001"], tmp_path)
        assert out.returncode == 0
        assert "not found" in out.stdout.lower()

    def test_the_scan_still_exits_0(self, tmp_path):
        _write_notes(tmp_path, [DOC_LINE_1])
        assert _cli(["bootstrap", "--from", "docs"], tmp_path).returncode == 0


class TestFileModeIsKept:
    def test_update_reconstruction_keeps_a_group_writable_file_at_0664(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        path = irp_dir / "reconstructions.jsonl"
        os.chmod(path, 0o664)
        rec_id = _recs(irp_dir)[0]["id"]
        assert store.update_reconstruction(irp_dir, rec_id, {"status": "accepted"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o664


# ── .irp/.gitignore keeps the lock out of projects that commit .irp/ ─────────

class TestLockIgnoredInCommittedIrp:
    def test_first_bootstrap_creates_irp_gitignore(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        assert (irp_dir / ".lock").exists()
        assert (irp_dir / ".gitignore").read_text(encoding="utf-8") == ".lock\n"

    def test_existing_irp_gitignore_is_kept_and_gains_lock_once(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / ".gitignore").write_text("bootstrap_reports/", encoding="utf-8")  # no final newline
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        _write_notes(tmp_path, [DOC_LINE_1, DOC_LINE_2])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert (irp_dir / ".gitignore").read_text(encoding="utf-8") == "bootstrap_reports/\n.lock\n"

    def test_a_gitignore_that_already_ignores_the_lock_is_untouched(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / ".gitignore").write_text("# mine\n.lock\n", encoding="utf-8")
        _write_notes(tmp_path, [DOC_LINE_1])
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert (irp_dir / ".gitignore").read_text(encoding="utf-8") == "# mine\n.lock\n"

    def test_dry_run_creates_neither(self, tmp_path):
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1], dry_run=True)
        assert not (irp_dir / ".lock").exists() and not (irp_dir / ".gitignore").exists()

    def test_git_ignores_the_lock_but_not_the_ledger(self, tmp_path):
        if _git(["--version"], tmp_path).returncode != 0:
            pytest.skip("git isn't available")
        _git(["init", "-q"], tmp_path)
        irp_dir, _ = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        assert _git(["check-ignore", "-q", ".irp/.lock"], tmp_path).returncode == 0
        assert _git(["check-ignore", "-q", ".irp/reconstructions.jsonl"], tmp_path).returncode == 1
        assert _git(["check-ignore", "-q", ".irp/ledger.jsonl"], tmp_path).returncode == 1


# ── round 2 ────────────────────────────────────────────────────────────────────

class TestLockOnMountsWithoutFlock:
    """Some mounts (NFS without a lock daemon, FUSE, ...) reject flock. That must
    not crash bootstrap or accept; the work simply runs unlocked."""

    @pytest.mark.parametrize("name", ["ENOTSUP", "EOPNOTSUPP", "ENOLCK", "EINVAL"])
    def test_bootstrap_and_accept_still_work(self, tmp_path, monkeypatch, needs_flock, name):
        import errno
        calls = []

        def fake_flock(fd, op):
            calls.append(op)
            if op == store.fcntl.LOCK_EX:
                raise OSError(getattr(errno, name), "flock not supported here")

        monkeypatch.setattr(store.fcntl, "flock", fake_flock)
        irp_dir, result = _bootstrap_docs(tmp_path, [DOC_LINE_1])
        assert result["status"] == "written"
        rec_id = _recs(irp_dir)[0]["id"]
        out = run_bootstrap(tmp_path, irp_dir, _Args(accept=[rec_id]))
        assert out["status"] == "accepted"
        assert len(read_ledger(irp_dir)) == 1
        # LOCK_UN is only called after a LOCK_EX that worked.
        assert store.fcntl.LOCK_UN not in calls

    def test_the_lock_file_descriptor_is_closed_on_that_path(self, tmp_path, monkeypatch, needs_flock):
        import errno
        opened = []
        real_open, real_close = os.open, os.close
        closed = []

        def spy_open(path, *a, **k):
            fd = real_open(path, *a, **k)
            if str(path).endswith(".lock"):
                opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(store.fcntl, "flock", lambda fd, op: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "no")))
        monkeypatch.setattr(store.os, "open", spy_open)
        monkeypatch.setattr(store.os, "close", spy_close)
        irp_dir = ensure_irp_dir(tmp_path)
        with store.irp_lock(irp_dir):
            pass
        assert opened and set(opened) <= set(closed)

    def test_an_unexpected_flock_error_is_not_swallowed(self, tmp_path, monkeypatch, needs_flock):
        import errno

        def fake_flock(fd, op):
            raise OSError(errno.EIO, "disk on fire")

        monkeypatch.setattr(store.fcntl, "flock", fake_flock)
        irp_dir = ensure_irp_dir(tmp_path)
        with pytest.raises(OSError):
            with store.irp_lock(irp_dir):
                pass


class TestBlankAccept:
    """--accept given, but every value blank, must not fall through to a scan."""

    @pytest.mark.parametrize("value", [[""], [" ", ""], [["", " "]], []])
    def test_blank_values_are_a_problem_not_a_scan(self, tmp_path, value):
        irp_dir = ensure_irp_dir(tmp_path)
        _write_notes(tmp_path, [DOC_LINE_1])
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs", accept=value))
        assert result["status"] == "nothing_accepted"
        assert result["accepted"] == []
        assert result["problems"] and "no reconstruction ids" in result["problems"][0]["reason"].lower()
        assert not (irp_dir / "reconstructions.jsonl").exists()
        assert read_ledger(irp_dir) == []

    def test_not_passing_accept_still_scans(self, tmp_path):
        _write_notes(tmp_path, [DOC_LINE_1])
        irp_dir = ensure_irp_dir(tmp_path)
        assert run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))["status"] == "written"

    def test_cli_accept_with_an_empty_string_exits_1_and_records_nothing(self, tmp_path):
        _write_notes(tmp_path, [DOC_LINE_1])
        out = _cli(["bootstrap", "--accept", ""], tmp_path)
        assert out.returncode == 1
        assert "no reconstruction ids" in out.stdout.lower()
        assert not (tmp_path / ".irp" / "reconstructions.jsonl").exists()
