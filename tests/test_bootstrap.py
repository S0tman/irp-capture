"""Tests for irp bootstrap, scanning git log and docs/files for decision
signals and writing (or previewing) candidate ledger entries.

Git operations use a real throwaway repo under tmp_path (local only, no
network). No live network is ever touched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, read_ledger  # noqa: E402
from commands.bootstrap import run_bootstrap  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        defaults = dict(from_source="all", path=None, dry_run=False, limit=50, write_report=False)
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _init_repo_with_commits(tmp_path, messages):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    for i, msg in enumerate(messages):
        f = tmp_path / f"file{i}.txt"
        f.write_text(f"content {i}\n", encoding="utf-8")
        _git(["add", f.name], tmp_path)
        _git(["commit", "-q", "-m", msg], tmp_path)


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
        assert read_ledger(irp_dir)[0]["origin_mode"] == "bootstrap_git"

    def test_bootstrapped_flag_always_true(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for the backend"])
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert read_ledger(irp_dir)[0]["bootstrapped"] is True

    def test_confidence_is_always_low(self, tmp_path):
        _init_repo_with_commits(tmp_path, ["we decided to migrate to cloud storage"])
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert read_ledger(irp_dir)[0]["confidence"] == "low"

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

    def test_dry_run_does_not_write_ledger(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", dry_run=True))
        assert result["status"] == "dry_run"
        assert read_ledger(irp_dir) == []

    def test_dry_run_still_assigns_preview_ids(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", dry_run=True))
        assert result["entries"][0]["id"].startswith("IRP-")


class TestDocsSource:
    def test_decision_line_in_markdown_becomes_candidate(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (tmp_path / "NOTES.md").write_text(
            "# Notes\n\nWe decided to use feature flags for gradual rollout of new features.\n",
            encoding="utf-8",
        )
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="docs"))
        assert result["candidates_written"] == 1
        assert read_ledger(irp_dir)[0]["origin_mode"] == "bootstrap_docs"

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


class TestDeduplication:
    def test_duplicate_what_is_skipped_on_rerun(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        result2 = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git"))
        assert result2["status"] == "empty"
        assert len(read_ledger(irp_dir)) == 1


class TestLimit:
    def test_limit_caps_written_entries(self, tmp_path, monkeypatch):
        messages = [f"we decided to adopt tool number {i} for the pipeline" for i in range(5)]
        _init_repo_with_commits(tmp_path, messages)
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", limit=2))
        assert result["candidates_written"] == 2
        assert len(read_ledger(irp_dir)) == 2


class TestReport:
    def test_write_report_creates_markdown_file(self, tmp_path, monkeypatch):
        _init_repo_with_commits(tmp_path, ["we decided to adopt PostgreSQL for storage"])
        monkeypatch.chdir(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_bootstrap(tmp_path, irp_dir, _Args(from_source="git", write_report=True))
        report_path = Path(result["report"])
        assert report_path.exists()
        assert "IRP Bootstrap Report" in report_path.read_text(encoding="utf-8")
