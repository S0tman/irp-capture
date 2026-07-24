"""Tests for irp guard, pre-commit hook install/run/status.

guard.run shells out to `git diff --cached`, so these tests build a real
throwaway git repo under tmp_path (no network, no external service).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, write_current  # noqa: E402
from commands.guard import run_guard, _HOOK_MARKER  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _init_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)


# ── guard install ──────────────────────────────────────────────────────────────

class TestGuardInstall:
    def test_no_git_dir_errors(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "error"

    def test_installs_hook(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "ok"
        hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
        assert hook_path.exists()
        assert _HOOK_MARKER in hook_path.read_text(encoding="utf-8")

    def test_hook_is_executable(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
        assert hook_path.stat().st_mode & 0o111

    def test_reinstall_without_force_reports_exists(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "exists"

    def test_foreign_hook_without_force_reports_conflict(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho not irp\n", encoding="utf-8")
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "conflict"

    def test_force_overwrites_foreign_hook(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho not irp\n", encoding="utf-8")
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=True))
        assert result["status"] == "ok"
        assert _HOOK_MARKER in (hooks_dir / "pre-commit").read_text(encoding="utf-8")


# ── guard run ──────────────────────────────────────────────────────────────────

class TestGuardRun:
    def test_no_staged_changes_is_clear(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "clear"

    def test_no_active_decisions_is_clear(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        (tmp_path / "file.txt").write_text("delete the authentication module now\n", encoding="utf-8")
        _git(["add", "file.txt"], tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "clear"

    def test_staged_conflict_detected(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        write_current(irp_dir, {
            "version": 1,
            "active": [{
                "id": "IRP-1",
                "what": "Do not delete the authentication module",
                "why": "Auth module is shared across all services and critical",
            }],
        })
        (tmp_path / "file.txt").write_text(
            "authentication module deletion shared services critical\n", encoding="utf-8"
        )
        _git(["add", "file.txt"], tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "conflict"
        assert result["match_id"] == "IRP-1"

    def test_low_overlap_is_warning_not_conflict(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        write_current(irp_dir, {
            "version": 1,
            "active": [{
                "id": "IRP-1",
                "what": "Do not delete the authentication module",
                "why": "Shared across services",
            }],
        })
        (tmp_path / "file.txt").write_text("authentication tweak\n", encoding="utf-8")
        _git(["add", "file.txt"], tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] in ("clear", "warning")


# ── guard status ───────────────────────────────────────────────────────────────

class TestGuardStatus:
    def test_no_git_reports_no_git(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "no_git"

    def test_not_installed_status(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "not_installed"

    def test_installed_status_after_install(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "installed"

    def test_other_hook_status(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = ensure_irp_dir(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "other_hook"


class TestGuardUnknownAction:
    def test_unknown_action_errors(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="bogus"))
        assert result["status"] == "error"
