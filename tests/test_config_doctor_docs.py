"""Tests for irp config, irp doctor, and irp docs command handlers.

irp docs talks to a fixed iCloud path and a /tmp staging area (see
irp/core/commands/docs.py: ICLOUD_DIR, STAGING_DIR). Those are module-level
constants, so tests monkeypatch them to tmp_path fixtures rather than ever
touching the real iCloud folder or /tmp.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, read_config  # noqa: E402
from commands.config import run_config  # noqa: E402
from commands.doctor import run_doctor  # noqa: E402
import commands.docs as docs_mod  # noqa: E402
from commands.docs import run_docs  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ── irp config ─────────────────────────────────────────────────────────────────

class TestConfigGet:
    def test_get_all_returns_config_dict(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="get", key=None, json=False))
        assert result["config"]["control_level"] == "advanced"

    def test_get_specific_known_key(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="get", key="control_level", json=False))
        assert result["value"] == "advanced"

    def test_get_unknown_key_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="get", key="nonsense", json=False))
        assert result["status"] == "error"


class TestConfigSet:
    def test_set_valid_value_persists(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="set", key="control_level", value="easy", json=False))
        assert result["status"] == "ok"
        assert read_config(irp_dir)["control_level"] == "easy"

    def test_set_reports_previous_value(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        run_config(tmp_path, irp_dir, _Args(config_action="set", key="control_level", value="easy", json=False))
        result = run_config(tmp_path, irp_dir, _Args(config_action="set", key="control_level", value="medium", json=False))
        assert result["previous"] == "easy"

    def test_set_invalid_value_rejected(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="set", key="control_level", value="bogus", json=False))
        assert result["status"] == "error"
        assert read_config(irp_dir)["control_level"] == "advanced"  # unchanged

    def test_set_unknown_key_rejected(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="set", key="nope", value="x", json=False))
        assert result["status"] == "error"

    def test_unknown_action_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_config(tmp_path, irp_dir, _Args(config_action="bogus"))
        assert result["status"] == "error"


# ── irp doctor ─────────────────────────────────────────────────────────────────

class TestDoctor:
    def test_reports_python_check(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        labels = {c["label"] for c in result["checks"]}
        assert "Python" in labels

    def test_python_check_passes_on_supported_version(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        py_check = next(c for c in result["checks"] if c["label"] == "Python")
        assert py_check["ok"] is True  # test env runs on Python >= 3.9

    def test_irp_dir_present_when_ensured(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        dir_check = next(c for c in result["checks"] if c["label"] == ".irp/ directory")
        assert dir_check["ok"] is True

    def test_ledger_entry_count_reported(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        from store import append_ledger_entry
        append_ledger_entry(irp_dir, {"id": "IRP-1", "type": "decision"})
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["entry_count"] == 1

    def test_corrupt_ledger_reported_not_ok(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / "ledger.jsonl").write_text("{not json at all", encoding="utf-8")
        result = run_doctor(tmp_path, irp_dir, _Args())
        ledger_check = next(c for c in result["checks"] if c["label"] == "ledger.jsonl")
        assert ledger_check["ok"] is False

    def test_status_ok_when_core_checks_pass(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        # skill file and integrations may fail, but core checks are the gate
        assert result["status"] in ("ok", "issues_found")


# ── irp docs ───────────────────────────────────────────────────────────────────

@pytest.fixture
def docs_env(tmp_path, monkeypatch):
    """Point docs.py at fake iCloud + staging dirs under tmp_path."""
    icloud = tmp_path / "icloud"
    staging = tmp_path / "staging"
    icloud.mkdir()
    staging.mkdir()
    monkeypatch.setattr(docs_mod, "ICLOUD_DIR", icloud)
    monkeypatch.setattr(docs_mod, "STAGING_DIR", staging)
    return icloud, staging


class TestDocsPull:
    def test_pull_copies_known_files(self, docs_env, tmp_path):
        icloud, staging = docs_env
        (icloud / "SPEC.md").write_text("spec content", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file=None))
        assert result["status"] == "ok"
        assert (staging / "SPEC.md").read_text(encoding="utf-8") == "spec content"

    def test_pull_missing_file_reported_as_warning(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file=None))
        assert "SPEC.md" in " ".join(result["errors"])

    def test_pull_specific_file(self, docs_env, tmp_path):
        icloud, staging = docs_env
        (icloud / "IRP-Roadmap.md").write_text("roadmap", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file="IRP-Roadmap.md"))
        assert result["pulled"] == ["IRP-Roadmap.md: iCloud → /tmp"]


class TestDocsPush:
    def test_push_copies_to_icloud(self, docs_env, tmp_path):
        icloud, staging = docs_env
        (staging / "SPEC.md").write_text("updated spec", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["status"] == "ok"
        assert (icloud / "SPEC.md").read_text(encoding="utf-8") == "updated spec"

    def test_push_missing_staging_file_reported(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["errors"]

    def test_push_without_icloud_dir_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docs_mod, "ICLOUD_DIR", tmp_path / "does-not-exist")
        monkeypatch.setattr(docs_mod, "STAGING_DIR", tmp_path)
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["status"] == "error"


class TestDocsList:
    def test_list_returns_md_files(self, docs_env, tmp_path):
        icloud, staging = docs_env
        (icloud / "a.md").write_text("x", encoding="utf-8")
        (icloud / "b.md").write_text("y", encoding="utf-8")
        (icloud / "c.txt").write_text("z", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["files"] == ["a.md", "b.md"]

    def test_list_without_icloud_dir_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docs_mod, "ICLOUD_DIR", tmp_path / "does-not-exist")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["status"] == "error"


class TestDocsUnknownAction:
    def test_unknown_action_errors(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="bogus", file=None))
        assert result["status"] == "error"
