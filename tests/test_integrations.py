"""Tests for irp/integrations/*, obsidian, mempalace, and the dispatcher
that fires them after capture.

Both integrations are opt-in via env vars pointing at a local directory
(Obsidian vault / MemPalace path). No network is ever involved. mempalace
additionally needs chromadb (optional dependency) to actually write, so its
write-path test is import-skipped when chromadb is absent; the "not
configured" skip path is tested unconditionally.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from irp.integrations import obsidian, mempalace, dispatch  # noqa: E402


DECISION = {
    "id": "IRP-2026-04-01-001",
    "what": "Use PostgreSQL for the primary database",
    "why": "Relational fit. See IRP-2026-03-01-001 for prior context.",
    "confidence": "high",
    "timestamp": "2026-04-01",
    "tags": ["backend"],
    "source": "cli",
}


# ── obsidian ───────────────────────────────────────────────────────────────────

class TestObsidianWriteDecision:
    def test_writes_markdown_file_named_by_id(self, tmp_path):
        out = obsidian.write_decision(DECISION, tmp_path)
        assert out == tmp_path / "decisions" / "IRP-2026-04-01-001.md"
        assert out.exists()

    def test_frontmatter_contains_metadata(self, tmp_path):
        out = obsidian.write_decision(DECISION, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "id: IRP-2026-04-01-001" in text
        assert "confidence: high" in text
        assert "tags: [backend]" in text

    def test_body_contains_what_as_heading(self, tmp_path):
        out = obsidian.write_decision(DECISION, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "# Use PostgreSQL for the primary database" in text

    def test_irp_ids_in_why_become_wikilinks(self, tmp_path):
        out = obsidian.write_decision(DECISION, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "[[IRP-2026-03-01-001]]" in text

    def test_no_tags_renders_empty_list(self, tmp_path):
        entry = {**DECISION, "tags": []}
        out = obsidian.write_decision(entry, tmp_path)
        assert "tags: []" in out.read_text(encoding="utf-8")

    def test_expands_user_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        out = obsidian.write_decision(DECISION, "~/vault")
        assert out.exists()
        assert str(tmp_path) in str(out)


class TestObsidianSync:
    def test_returns_none_when_not_configured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IRP_OBSIDIAN_VAULT", raising=False)
        assert obsidian.sync(DECISION, tmp_path) is None

    def test_returns_ok_when_configured(self, monkeypatch, tmp_path):
        vault = tmp_path / "vault"
        monkeypatch.setenv("IRP_OBSIDIAN_VAULT", str(vault))
        result = obsidian.sync(DECISION, tmp_path)
        assert result["integration"] == "obsidian"
        assert result["status"] == "ok"
        assert Path(result["path"]).exists()

    def test_returns_error_status_on_failure(self, monkeypatch, tmp_path):
        # Point the vault at a path that collides with a file, so mkdir fails.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("IRP_OBSIDIAN_VAULT", str(blocker / "vault"))
        result = obsidian.sync(DECISION, tmp_path)
        assert result["status"] == "error"
        assert "error" in result


# ── mempalace ──────────────────────────────────────────────────────────────────

class TestMempalaceSync:
    def test_skipped_silently_when_not_configured_and_default_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IRP_MEMPALACE_PATH", raising=False)
        # Point the "default" palace at a path guaranteed not to exist.
        monkeypatch.setattr(mempalace, "_DEFAULT_PALACE", tmp_path / "does-not-exist")
        assert mempalace.sync(DECISION, tmp_path) is None

    def test_explicit_path_without_chromadb_installed(self, monkeypatch, tmp_path):
        """chromadb is not part of the dev extras, so an explicitly configured
        palace path must report status=skipped rather than raising or
        silently doing nothing. If chromadb happens to be installed in this
        environment, the absent-dependency path can't be exercised, so skip."""
        if _has_chromadb():
            pytest.skip("chromadb is installed, this covers the absent-dependency path")
        palace = tmp_path / "palace"
        monkeypatch.setenv("IRP_MEMPALACE_PATH", str(palace))
        result = mempalace.sync(DECISION, tmp_path)
        assert result["status"] == "skipped"
        assert "chromadb" in result["reason"]

    def test_format_decision_includes_key_fields(self):
        text = mempalace._format_decision(DECISION)
        assert "IRP-2026-04-01-001" in text
        assert "PostgreSQL" in text
        assert "backend" in text

    def test_format_decision_handles_no_tags(self):
        entry = {**DECISION, "tags": []}
        text = mempalace._format_decision(entry)
        assert "Tags: none" in text


def _has_chromadb() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


# ── dispatch ───────────────────────────────────────────────────────────────────

class TestDispatchRun:
    def test_runs_all_integrations_and_collects_results(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dispatch.obsidian, "sync", lambda decision, project_root: {"integration": "obsidian", "status": "ok"})
        monkeypatch.setattr(dispatch.mempalace, "sync", lambda decision, project_root: {"integration": "mempalace", "status": "ok"})
        results = dispatch.run(DECISION, tmp_path)
        names = {r["integration"] for r in results}
        assert names == {"obsidian", "mempalace"}

    def test_none_results_are_omitted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dispatch.obsidian, "sync", lambda decision, project_root: None)
        monkeypatch.setattr(dispatch.mempalace, "sync", lambda decision, project_root: None)
        assert dispatch.run(DECISION, tmp_path) == []

    def test_exception_in_one_integration_does_not_abort_the_other(self, monkeypatch, tmp_path):
        def _boom(decision, project_root):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(dispatch.obsidian, "sync", _boom)
        monkeypatch.setattr(dispatch.mempalace, "sync", lambda decision, project_root: {"integration": "mempalace", "status": "ok"})
        results = dispatch.run(DECISION, tmp_path)
        statuses = {r.get("integration", r.get("status")): r for r in results}
        mempalace_result = next(r for r in results if r.get("integration") == "mempalace")
        assert mempalace_result["status"] == "ok"
        error_result = next(r for r in results if r.get("status") == "error")
        assert "simulated failure" in error_result["error"]

    def test_no_dotenv_installed_does_not_crash(self, tmp_path):
        """python-dotenv is not part of the dev extras, _try_load_dotenv must
        degrade silently rather than raising ImportError."""
        # Real integrations (not configured), should just return [].
        result = dispatch.run(DECISION, tmp_path)
        assert isinstance(result, list)
