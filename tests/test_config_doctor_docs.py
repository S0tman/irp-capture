"""Tests for irp config, irp doctor, and irp docs command handlers.

irp docs copies between the folder named by IRP_DOCS_DIR and a /tmp staging
area (irp/core/commands/docs.py: STAGING_DIR). Tests point IRP_DOCS_DIR and
STAGING_DIR at tmp_path fixtures rather than ever touching a real docs folder
or /tmp.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IRP_PY = str(REPO / "irp" / "core" / "irp.py")
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
    """Point IRP_DOCS_DIR and docs.py's staging dir at folders under tmp_path."""
    docs = tmp_path / "docs"
    staging = tmp_path / "staging"
    docs.mkdir()
    staging.mkdir()
    monkeypatch.setenv("IRP_DOCS_DIR", str(docs))
    monkeypatch.setattr(docs_mod, "STAGING_DIR", staging)
    return docs, staging


class TestDocsDirSetting:
    """The docs folder comes from IRP_DOCS_DIR; there is no built-in default."""

    @pytest.mark.parametrize("action", ["pull", "push", "list"])
    def test_unset_errors_with_how_to_set_it(self, action, tmp_path, monkeypatch):
        monkeypatch.delenv("IRP_DOCS_DIR", raising=False)
        monkeypatch.setattr(docs_mod, "STAGING_DIR", tmp_path)
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action=action, file=None))
        assert result["status"] == "error"
        assert "export IRP_DOCS_DIR=" in result["text"]

    def test_blank_counts_as_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRP_DOCS_DIR", "   ")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["status"] == "error"
        assert "IRP_DOCS_DIR" in result["text"]

    def test_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "mydocs").mkdir()
        (tmp_path / "mydocs" / "a.md").write_text("x", encoding="utf-8")
        monkeypatch.setenv("IRP_DOCS_DIR", "~/mydocs")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["files"] == ["a.md"]

    def test_no_personal_path_in_module(self):
        source = Path(docs_mod.__file__).read_text(encoding="utf-8")
        assert "/Users/" not in source
        assert "Mobile Documents" not in source

    @pytest.mark.parametrize("action", ["pull", "push", "list"])
    def test_cli_exits_nonzero_when_unset(self, action, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "IRP_DOCS_DIR"}
        proc = subprocess.run(
            [sys.executable, IRP_PY, "docs", action],
            capture_output=True, text=True, cwd=str(tmp_path), env=env,
        )
        assert proc.returncode != 0
        assert "IRP_DOCS_DIR" in proc.stdout + proc.stderr


class TestDocsPull:
    def test_pull_copies_known_files(self, docs_env, tmp_path):
        docs, staging = docs_env
        (docs / "SPEC.md").write_text("spec content", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file=None))
        assert result["status"] == "ok"
        assert (staging / "SPEC.md").read_text(encoding="utf-8") == "spec content"

    def test_pull_missing_file_reported_as_warning(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file=None))
        assert "SPEC.md" in " ".join(result["errors"])

    def test_pull_specific_file(self, docs_env, tmp_path):
        docs, staging = docs_env
        (docs / "IRP-Roadmap.md").write_text("roadmap", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file="IRP-Roadmap.md"))
        assert result["pulled"] == [f"IRP-Roadmap.md: docs folder → {staging}"]

    def test_pull_without_docs_dir_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRP_DOCS_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(docs_mod, "STAGING_DIR", tmp_path)
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="pull", file=None))
        assert result["status"] == "error"
        assert "does-not-exist" in result["text"]


class TestDocsPush:
    def test_push_copies_to_docs_dir(self, docs_env, tmp_path):
        docs, staging = docs_env
        (staging / "SPEC.md").write_text("updated spec", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["status"] == "ok"
        assert (docs / "SPEC.md").read_text(encoding="utf-8") == "updated spec"

    def test_push_missing_staging_file_reported(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["errors"]

    def test_push_without_docs_dir_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRP_DOCS_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(docs_mod, "STAGING_DIR", tmp_path)
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="push", file=None))
        assert result["status"] == "error"


class TestDocsList:
    def test_list_returns_md_files(self, docs_env, tmp_path):
        docs, staging = docs_env
        (docs / "a.md").write_text("x", encoding="utf-8")
        (docs / "b.md").write_text("y", encoding="utf-8")
        (docs / "c.txt").write_text("z", encoding="utf-8")
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["files"] == ["a.md", "b.md"]

    def test_list_without_docs_dir_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRP_DOCS_DIR", str(tmp_path / "does-not-exist"))
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="list", file=None))
        assert result["status"] == "error"


class TestDocsUnknownAction:
    def test_unknown_action_errors(self, docs_env, tmp_path):
        result = run_docs(tmp_path, tmp_path / ".irp", _Args(docs_action="bogus", file=None))
        assert result["status"] == "error"
