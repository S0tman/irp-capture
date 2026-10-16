"""Tests for irp guard, pre-commit hook install/run/status.

guard.run shells out to `git diff --cached`, so these tests build a real
throwaway git repo under tmp_path (no network, no external service).

The hook tests further down install the hook through the CLI and make real
commits. The seal in conftest.py keeps any real `irp` off
PATH, so a hook only ever runs the code under test.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import write_current  # noqa: E402
from conftest import make_project  # noqa: E402
from commands.guard import run_guard, _HOOK_MARKER  # noqa: E402
from project_kit import (  # noqa: E402
    CONTRADICTION,
    DECISION_ID,
    IRP_PY,
    GUARD_NO_PROJECT,
    GUARD_NO_IRP,
    git,
    git_repo,
    irp_folders,
    out,
    run_irp,
    start_project,
    state_diff,
    tree_state,
    write_empty_stray,
)


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
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "error"

    def test_installs_hook(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "ok"
        hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
        assert hook_path.exists()
        assert _HOOK_MARKER in hook_path.read_text(encoding="utf-8")

    def test_hook_escapes_backticks_for_the_shell(self, tmp_path):
        # The shell needs \` inside double quotes to print a literal backtick.
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        hook = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
        assert 'Run \\`irp why\\` to review the conflict.' in hook

    def test_hook_is_executable(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
        assert hook_path.stat().st_mode & 0o111

    def test_reinstall_without_force_reports_exists(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "exists"

    def test_foreign_hook_without_force_reports_conflict(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho not irp\n", encoding="utf-8")
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        assert result["status"] == "conflict"

    def test_force_overwrites_foreign_hook(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
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
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "clear"

    def test_no_active_decisions_is_clear(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        (tmp_path / "file.txt").write_text("delete the authentication module now\n", encoding="utf-8")
        _git(["add", "file.txt"], tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "clear"

    def test_staged_conflict_detected(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
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
        irp_dir = make_project(tmp_path)
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
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "no_git"

    def test_not_installed_status(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "not_installed"

    def test_installed_status_after_install(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        run_guard(tmp_path, irp_dir, _Args(guard_action="install", force=False))
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "installed"

    def test_other_hook_status(self, tmp_path):
        _init_repo(tmp_path)
        irp_dir = make_project(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="status", json=False))
        assert result["status"] == "other_hook"


class TestGuardUnknownAction:
    def test_unknown_action_errors(self, tmp_path):
        irp_dir = make_project(tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="bogus"))
        assert result["status"] == "error"


# ── the v1 hook ───────────────────────────────────────────────────────────────
#
# These install the hook with the CLI, as a person would, and make real commits.

# The v0 hook as 0.9.x wrote it (comment punctuation aside). It runs whatever
# `irp` is on PATH from the repo's top folder.
V0_HOOK = """#!/bin/sh
# irp-guard-v0
# IRP Guard: checks staged changes against the decision ledger.
# Warns on conflict. Does not block commits by default.
irp guard run
GUARD_EXIT=$?
if [ "$GUARD_EXIT" -eq 10 ] && [ "${IRP_GUARD_BLOCK:-0}" = "1" ]; then
  echo ""
  echo "IRP guard: commit aborted (IRP_GUARD_BLOCK=1)."
  exit 1
fi
exit 0
"""


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


@pytest.fixture
def project_repo(root):
    """A repo whose top folder is a project holding the Postgres decision."""
    repo = git_repo(root / "proj")
    start_project(repo)
    return repo


@pytest.fixture
def irp_on_path(root):
    """An `irp` on PATH that runs the code under test and leaves a mark when called."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    mark = root / "irp-on-path-was-called"
    script = bin_dir / "irp"
    script.write_text(
        "#!/bin/sh\n"
        f'echo called >> "{mark}"\n'
        f'exec "{sys.executable}" "{IRP_PY}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return env, mark


def _hook_path(repo: Path) -> Path:
    return Path(git(["rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit"], repo).stdout.strip())


def _install(cwd: Path, *flags: str):
    return run_irp(["guard", "install", *flags], cwd)


def _stage(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text + "\n", encoding="utf-8")
    git(["add", name], folder)


def _commit(cwd: Path, message: str = "change", env=None):
    return git(["commit", "-q", "-m", message], cwd, check=False, env=env)


def _head(repo: Path) -> str:
    return git(["rev-parse", "-q", "--verify", "HEAD"], repo, check=False).stdout.strip()


def _set_recorded_entry(hook: Path, entry: str) -> None:
    text = hook.read_text(encoding="utf-8")
    text, n = re.subn(r'^IRP_ENTRY=.*$', f'IRP_ENTRY="{entry}"', text, flags=re.M)
    assert n == 1, "the hook records no IRP_ENTRY line"
    hook.write_text(text, encoding="utf-8")


class TestHookV1Template:
    def test_records_its_project_and_the_command_that_installed_it(self, project_repo):
        result = _install(project_repo)
        assert result.returncode == 0, out(result)
        hook = _hook_path(project_repo).read_text(encoding="utf-8")
        assert "# irp-guard-v1" in hook
        assert 'IRP_PROJECT="."' in hook
        assert f'IRP_PY="{sys.executable}"' in hook
        assert f'IRP_ENTRY="{IRP_PY}"' in hook
        assert "--hook-project" in hook

    def test_is_plain_posix_sh(self, project_repo):
        _install(project_repo)
        check = subprocess.run(["sh", "-n", str(_hook_path(project_repo))], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        assert _hook_path(project_repo).read_text(encoding="utf-8").startswith("#!/bin/sh\n")

    def test_records_a_project_in_a_subfolder_relative_to_the_repo_top(self, root):
        repo = git_repo(root / "mono")
        start_project(repo / "app")
        result = _install(repo / "app")
        assert result.returncode == 0, out(result)
        assert 'IRP_PROJECT="app"' in _hook_path(repo).read_text(encoding="utf-8")

    def test_installs_from_a_subfolder_of_the_project(self, project_repo):
        (project_repo / "src").mkdir()
        result = _install(project_repo / "src")
        assert result.returncode == 0, out(result)
        assert 'IRP_PROJECT="."' in _hook_path(project_repo).read_text(encoding="utf-8")
        assert irp_folders(project_repo) == [str(project_repo / ".irp")]


    def test_a_project_folder_with_shell_characters(self, root):
        repo = git_repo(root / "repo")
        odd = repo / 'a$b"c`d'
        start_project(odd)
        assert _install(odd).returncode == 0
        check = subprocess.run(["sh", "-n", str(_hook_path(repo))], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        _stage(odd, "plan.txt", CONTRADICTION)
        head = _head(repo)
        result = _commit(repo)
        assert DECISION_ID in out(result)
        assert result.returncode == 0 and _head(repo) != head


class TestHookFindsItsProject:
    def test_a_commit_from_a_subfolder_is_checked(self, project_repo):
        _install(project_repo)
        (project_repo / "src").mkdir()
        _stage(project_repo / "src", "plan.txt", CONTRADICTION)
        result = _commit(project_repo / "src")
        assert DECISION_ID in out(result)
        assert result.returncode == 0                     # warns, never blocks by default
        assert irp_folders(project_repo) == [str(project_repo / ".irp")]

    def test_a_copied_hook_in_an_unrelated_repo_prints_one_quiet_line(self, project_repo, root):
        _install(project_repo)
        other = git_repo(root / "unrelated", ignore_irp=False)
        (other / "lib").mkdir()
        hook = _hook_path(other)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(_hook_path(project_repo).read_bytes())
        hook.chmod(0o755)
        _stage(other / "lib", "a.txt", CONTRADICTION)
        head = _head(other)
        result = _commit(other / "lib")
        assert out(result).strip() == GUARD_NO_PROJECT
        assert result.returncode == 0 and _head(other) != head
        assert irp_folders(other) == []

    def test_a_project_in_a_subfolder_of_the_repo(self, root):
        repo = git_repo(root / "mono")
        start_project(repo / "app")
        assert _install(repo / "app").returncode == 0
        _stage(repo / "app", "plan.txt", CONTRADICTION)
        result = _commit(repo / "app")
        assert DECISION_ID in out(result)
        assert irp_folders(repo) == [str(repo / "app" / ".irp")]

    def test_names_the_project_when_it_is_not_the_repo_top(self, root):
        repo = git_repo(root / "mono")
        start_project(repo / "app")
        assert _install(repo / "app").returncode == 0
        _stage(repo / "app", "plan.txt", CONTRADICTION)
        assert f"Project:    {repo / 'app'}" in out(_commit(repo / "app"))
        _stage(repo / "app", "notes.txt", "tidy the readme wording")
        assert f"Checked against the project at {repo / 'app'}." in out(_commit(repo / "app"))

    def test_says_nothing_extra_when_the_project_is_the_repo_top(self, project_repo):
        _install(project_repo)
        _stage(project_repo, "notes.txt", "tidy the readme wording")
        result = _commit(project_repo)
        assert "against the project" not in out(result)
        assert "Project:" not in out(result)

    @pytest.mark.parametrize("stray_at_worktree_top", [False, True], ids=["clean", "with an old empty stray"])
    def test_a_linked_worktree_reads_the_main_checkouts_ledger(self, project_repo, root, stray_at_worktree_top):
        assert _install(project_repo).returncode == 0
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], project_repo)
        assert _hook_path(wt) == _hook_path(project_repo)  # one shared hooks folder
        if stray_at_worktree_top:
            write_empty_stray(wt)
        _stage(wt, "plan.txt", CONTRADICTION)              # staged in the worktree only
        result = _commit(wt)
        assert DECISION_ID in out(result)
        expected = [str(wt / ".irp")] if stray_at_worktree_top else []
        assert irp_folders(wt) == expected

    def test_a_linked_worktree_names_the_main_checkouts_project(self, project_repo, root):
        assert _install(project_repo).returncode == 0
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], project_repo)
        _stage(wt, "notes.txt", "tidy the readme wording")
        assert f"Checked against the project at {project_repo}." in out(_commit(wt))

    def test_a_bare_repos_worktree_finds_no_project(self, project_repo, root):
        _install(project_repo)
        outer = root / "outer"
        start_project(outer)                               # beside repo.git: the wrong place
        git(["clone", "--bare", "-q", str(project_repo), str(outer / "repo.git")], root)
        wt = outer / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], outer / "repo.git")
        hook = _hook_path(wt)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(_hook_path(project_repo).read_bytes())
        hook.chmod(0o755)
        _stage(wt, "plan.txt", CONTRADICTION)
        result = _commit(wt)
        assert out(result).strip() == GUARD_NO_PROJECT
        assert irp_folders(wt) == []

    def test_a_submodule_finds_no_project(self, project_repo, root):
        _install(project_repo)
        sub_src = git_repo(root / "subsrc", ignore_irp=False)
        git(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub"], project_repo)
        sub = project_repo / "sub"
        hook = _hook_path(sub)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(_hook_path(project_repo).read_bytes())
        hook.chmod(0o755)
        _stage(sub, "plan.txt", CONTRADICTION)
        result = _commit(sub)
        assert out(result).strip() == GUARD_NO_PROJECT
        assert irp_folders(sub) == []


class TestHookInstallPlaces:
    def test_refuses_a_hooks_folder_inside_the_work_tree(self, project_repo):
        git(["config", "core.hooksPath", ".githooks"], project_repo)
        result = _install(project_repo)
        assert result.returncode == 1
        text = out(result)
        assert (f"This repo's hooks folder is {project_repo / '.githooks'} (set by core.hooksPath), "
                "outside its .git folder.") in text
        assert "Nothing was written." in text
        assert "irp guard install --shared" in text
        assert not (project_repo / ".githooks").exists()
        assert not (project_repo / ".git" / "hooks" / "pre-commit").exists()

    def test_shared_writes_a_hook_that_records_no_paths(self, project_repo, root):
        git(["config", "core.hooksPath", ".githooks"], project_repo)
        result = _install(project_repo, "--shared")
        assert result.returncode == 0, out(result)
        hook_file = project_repo / ".githooks" / "pre-commit"
        assert f"Wrote a shared hook to {hook_file}." in out(result)
        hook = hook_file.read_text(encoding="utf-8")
        assert hook_file.stat().st_mode & 0o111
        for leak in (sys.executable, IRP_PY, str(root), "/home/", "/Users/"):
            assert leak not in hook

    def test_shared_hook_names_the_repo_top_and_ignores_irp_project_root(self, project_repo, root, irp_on_path):
        env, mark = irp_on_path
        shared = root / "global-hooks"
        git(["config", "--global", "core.hooksPath", str(shared)], project_repo)
        assert _install(project_repo, "--shared").returncode == 0
        assert "guard run --hook-project ." in (shared / "pre-commit").read_text(encoding="utf-8")
        other = git_repo(root / "unrelated", ignore_irp=False)
        _stage(other, "plan.txt", CONTRADICTION)
        head = _head(other)
        result = _commit(other, env=dict(env, IRP_PROJECT_ROOT=str(project_repo), IRP_GUARD_BLOCK="1"))
        assert mark.exists()
        assert out(result).strip() == GUARD_NO_PROJECT
        assert result.returncode == 0 and _head(other) != head
        assert irp_folders(other) == []

    def test_refuses_a_global_hooks_folder(self, project_repo, root):
        shared = root / "global-hooks"
        git(["config", "--global", "core.hooksPath", str(shared)], project_repo)
        result = _install(project_repo)
        assert result.returncode == 1
        assert f"This repo's hooks folder is {shared} (set by core.hooksPath)" in out(result)
        assert not shared.exists()
        assert not (project_repo / ".git" / "hooks" / "pre-commit").exists()

    def test_outside_git_refuses(self, root):
        folder = root / "nogit"
        start_project(folder)
        result = _install(folder)
        assert result.returncode == 1
        assert "There's no git repo here, so there's nowhere to put a commit hook." in out(result)

    def test_with_no_project_refuses(self, root):
        repo = git_repo(root / "repo")
        result = _install(repo)
        assert result.returncode == 1
        assert ("No IRP project here, so there's nothing for the hook to check. "
                f"Start one with irp init {repo}, then install the hook.") in out(result)
        assert irp_folders(repo) == []
        assert not (repo / ".git" / "hooks" / "pre-commit").exists()


class TestHookReinstall:
    def test_the_same_hook_again_is_fine(self, project_repo):
        _install(project_repo)
        before = tree_state(project_repo / ".git" / "hooks")
        result = _install(project_repo)
        assert result.returncode == 0
        assert (f"IRP guard hook already installed for this project (v1) at "
                f"{project_repo / '.git' / 'hooks' / 'pre-commit'}. Nothing changed.") in out(result)
        assert state_diff(before, tree_state(project_repo / ".git" / "hooks")) == []

    def test_says_when_the_recorded_command_differs(self, project_repo, root):
        _install(project_repo)
        _set_recorded_entry(_hook_path(project_repo), str(root / "old" / "irp.py"))
        before = tree_state(project_repo / ".git" / "hooks")
        result = _install(project_repo)
        assert result.returncode == 0
        assert f"It runs {root / 'old' / 'irp.py'}. To switch it to this one: irp guard install --force" in out(result)
        assert state_diff(before, tree_state(project_repo / ".git" / "hooks")) == []

    @pytest.mark.parametrize("existing", ["v0", "another project", "not IRP's"])
    def test_refuses_to_replace_without_force(self, root, existing):
        repo = git_repo(root / "repo")
        start_project(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        if existing == "v0":
            hook.write_text(V0_HOOK, encoding="utf-8")
        elif existing == "another project":
            start_project(repo / "app")
            assert _install(repo / "app").returncode == 0
        else:
            hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        before = hook.read_bytes()
        result = _install(repo)
        assert result.returncode == 1
        assert "--force" in out(result)
        if existing == "v0":
            assert "v0" in out(result)
        assert hook.read_bytes() == before
        assert _install(repo, "--force").returncode == 0
        assert "# irp-guard-v1" in hook.read_text(encoding="utf-8")


class TestHookRunsTheRightIrp:
    def test_falls_back_to_irp_on_path_when_the_recorded_command_is_gone(self, project_repo, root, irp_on_path):
        env, mark = irp_on_path
        _install(project_repo)
        _set_recorded_entry(_hook_path(project_repo), str(root / "gone" / "irp.py"))
        _stage(project_repo, "plan.txt", CONTRADICTION)
        result = _commit(project_repo, env=env)
        assert mark.exists()
        assert DECISION_ID in out(result)

    def test_with_neither_it_prints_m5_and_lets_the_commit_through(self, project_repo, root):
        _install(project_repo)
        _set_recorded_entry(_hook_path(project_repo), str(root / "gone" / "irp.py"))
        _stage(project_repo, "plan.txt", CONTRADICTION)
        head = _head(project_repo)
        result = _commit(project_repo)
        assert out(result).strip() == GUARD_NO_IRP
        assert result.returncode == 0 and _head(project_repo) != head

    def test_a_v0_hook_still_runs(self, project_repo, irp_on_path):
        env, mark = irp_on_path
        hook = project_repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(V0_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        _stage(project_repo, "plan.txt", CONTRADICTION)
        result = _commit(project_repo, env=env)
        assert mark.exists()
        assert DECISION_ID in out(result)
        assert result.returncode == 0


class TestHookBlocking:
    def _block_env(self):
        return dict(os.environ, IRP_GUARD_BLOCK="1")

    def test_block_stops_a_conflicting_commit(self, project_repo):
        _install(project_repo)
        _stage(project_repo, "plan.txt", CONTRADICTION)
        head = _head(project_repo)
        result = _commit(project_repo, env=self._block_env())
        assert result.returncode != 0
        assert DECISION_ID in out(result)
        assert _head(project_repo) == head

    def test_block_lets_a_clear_commit_through(self, project_repo):
        _install(project_repo)
        _stage(project_repo, "notes.txt", "tidy the readme wording")
        head = _head(project_repo)
        result = _commit(project_repo, env=self._block_env())
        assert result.returncode == 0, out(result)
        assert _head(project_repo) != head

    def test_block_lets_a_commit_through_when_the_guard_errors(self, project_repo):
        _install(project_repo)
        for name in ("ledger.jsonl", "current.json"):      # guard run reads current.json
            broken = project_repo / ".irp" / name
            broken.unlink()
            broken.mkdir()                                 # reading it now fails
        _stage(project_repo, "plan.txt", CONTRADICTION)
        head = _head(project_repo)
        result = _commit(project_repo, env=self._block_env())
        assert result.returncode == 0, out(result)
        assert _head(project_repo) != head


class TestGuardRunHookProject:
    def test_an_empty_stray_at_the_named_path_is_no_project(self, root):
        repo = git_repo(root / "repo")
        stray = write_empty_stray(repo / "app")
        before = tree_state(repo)
        result = run_irp(["guard", "run", "--hook-project", "app"], repo)
        assert result.returncode == 0
        assert out(result).strip() == GUARD_NO_PROJECT
        assert state_diff(before, tree_state(repo)) == []
        assert stray.exists()

    def test_the_named_path_is_the_only_source(self, project_repo, root, monkeypatch):
        other = git_repo(root / "other")
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(project_repo))
        _stage(other, "plan.txt", CONTRADICTION)
        result = run_irp(["guard", "run", "--hook-project", "."], other)
        assert result.returncode == 0
        assert out(result).strip() == GUARD_NO_PROJECT

    def test_a_v0_style_run_with_no_project_says_so_quietly(self, root):
        repo = git_repo(root / "repo")
        _stage(repo, "plan.txt", CONTRADICTION)
        result = run_irp(["guard", "run"], repo)
        assert result.returncode == 0
        assert out(result).strip() == GUARD_NO_PROJECT
        assert irp_folders(repo) == []

    def test_by_hand_in_a_worktree_it_checks_the_worktrees_staged_changes(self, project_repo, root):
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], project_repo)
        _stage(wt, "plan.txt", CONTRADICTION)              # staged in the worktree only
        result = run_irp(["guard", "run"], wt)
        assert result.returncode == 10, out(result)
        assert DECISION_ID in out(result)

    def test_says_so_when_it_cant_read_the_staged_changes(self, root):
        proj = root / "proj"                               # a project outside git
        start_project(proj)
        nogit = root / "nogit"
        nogit.mkdir()
        result = run_irp(["guard", "run", "--project", str(proj)], nogit)
        assert result.returncode == 0
        assert out(result).strip() == "IRP guard: couldn't read the staged changes, so nothing was checked."
        result = run_irp(["guard", "run", "--project", str(proj), "--json"], nogit)
        assert result.returncode == 0
        assert json.loads(result.stdout)["status"] == "error"

    def test_a_named_project_with_no_ledger_never_suggests_init(self, root):
        repo = git_repo(root / "repo")
        empty = root / "empty"
        empty.mkdir()
        result = run_irp(["guard", "run", "--project", str(empty)], repo)
        assert result.returncode == 0
        assert out(result).strip() == GUARD_NO_PROJECT
        assert "irp init" not in out(result)


class TestGuardStatusFindsTheHook:
    def test_from_a_linked_worktree(self, project_repo, root):
        _install(project_repo)
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], project_repo)
        before = tree_state(wt)
        result = run_irp(["guard", "status"], wt)
        assert result.returncode == 0
        assert "IRP guard hook: installed" in out(result)
        assert state_diff(before, tree_state(wt)) == []

    def test_status_calls_a_v0_hook_v0_and_suggests_force(self, project_repo):
        hook = project_repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(V0_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        result = run_irp(["guard", "status"], project_repo)
        assert result.returncode == 0
        assert ("IRP guard hook installed (older version, v0). It works, but it can't find a project "
                "outside the repo's top folder. To upgrade: irp guard install --force") in out(result)
