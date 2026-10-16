"""doctor and stray .irp folders.

doctor lists empty strays and nested ledgers holding entries, says where it
looked and removes nothing. It runs with no project too: its project check
fails with the `irp init` hint and it still exits 0.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from project_kit import (
    DECISION,
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

HEADER = "Stray .irp folders (older IRP versions made these when run outside a project)"
FOOTER = "An empty one can go by hand once you've checked it. IRP never removes a folder holding entries."


def _entries(n: int) -> tuple[dict, ...]:
    return tuple(dict(DECISION, id=f"IRP-2026-02-0{i}-001", what=f"Rule number {i}") for i in range(1, n + 1))


def _listed_empty(text: str, irp_dir: Path) -> bool:
    return re.search(re.escape(str(irp_dir)) + r"\s+empty, holds nothing", text) is not None


def _listed_with(text: str, irp_dir: Path, n: int) -> bool:
    return re.search(re.escape(str(irp_dir)) + rf"\s+holds {n} ledger entr(y|ies), kept", text) is not None


def _stray_section(text: str) -> str:
    start = text.find("Stray .irp folders")
    end = text.find("Looked ", start)
    return text[start:end] if start >= 0 else ""


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


class TestStrayReport:
    def test_lists_an_empty_stray_and_a_nested_ledger_with_line_counts(self, root):
        repo = git_repo(root / "a")
        start_project(repo)
        start_project(repo / "tools", entries=_entries(3))
        deep = repo / "src" / "deep"
        write_empty_stray(deep)
        result = run_irp(["doctor"], deep)
        assert result.returncode == 0, out(result)
        text = result.stdout
        assert HEADER in text
        assert _listed_empty(text, deep / ".irp")
        assert _listed_with(text, repo / "tools" / ".irp", 3)
        assert (f"Looked from here up to {repo}, one level below {repo} "
                "and in git's list of tracked files.") in text
        assert FOOTER in text

    def test_never_lists_the_project_or_a_dot_irp_that_is_not_irps(self, root):
        repo = git_repo(root / "a")
        start_project(repo)
        other = repo / "src" / ".irp"
        other.mkdir(parents=True)
        (other / "test.txt").write_text("x", encoding="utf-8")
        write_empty_stray(repo / "old")
        text = run_irp(["doctor"], repo).stdout
        assert _listed_empty(text, repo / "old" / ".irp")
        assert str(other) not in text
        assert not _listed_empty(text, repo / ".irp")
        assert not _listed_with(text, repo / ".irp", 1)
        assert re.search(re.escape(str(repo / ".irp")) + r"\s", _stray_section(text)) is None

    def test_looks_up_only_as_far_as_the_project_folder(self, root):
        repo = git_repo(root / "r")
        start_project(repo / "a" / "b")
        write_empty_stray(repo / "a")                        # above the project, below the git top
        text = run_irp(["doctor"], repo / "a" / "b").stdout
        assert str(repo / "a" / ".irp") not in text

    def test_never_reads_a_git_repo_at_the_home_folder(self, root, monkeypatch):
        home = root / "home"
        git_repo(home, ignore_irp=False)                     # a dotfiles repo, say
        other = start_project(home / "other")
        git(["add", "-f", str(other / "ledger.jsonl")], home)
        git(["commit", "-q", "-m", "other", "--no-verify"], home)
        start_project(home / "proj")
        monkeypatch.setenv("HOME", str(home))
        text = run_irp(["doctor"], home / "proj").stdout
        assert str(other) not in text
        assert "git's list of tracked files" not in text

    def test_looks_one_level_down_and_finds_tracked_ledgers_at_any_depth(self, root):
        repo = git_repo(root / "a")                          # .irp/ is gitignored
        start_project(repo)
        write_empty_stray(repo / "c")                        # one level down: found
        write_empty_stray(repo / "a1" / "b1")                # two levels down, untracked: out of sight
        tracked = start_project(repo / "x" / "y" / "z", entries=_entries(2))
        git(["add", "-f", str(tracked / "ledger.jsonl")], repo)
        text = run_irp(["doctor"], repo).stdout
        assert _listed_empty(text, repo / "c" / ".irp")
        assert _listed_with(text, repo / "x" / "y" / "z" / ".irp", 2)
        assert str(repo / "a1" / "b1" / ".irp") not in text

    def test_outside_git_the_index_check_is_skipped(self, root):
        top = root / "plain"
        start_project(top)
        write_empty_stray(top / "c")
        start_project(top / "x" / "y" / "z", entries=_entries(2))
        text = run_irp(["doctor"], top).stdout
        assert _listed_empty(text, top / "c" / ".irp")
        assert str(top / "x" / "y" / "z" / ".irp") not in text
        assert "git's list of tracked files" not in text

    def test_removes_nothing(self, root):
        repo = git_repo(root / "a")
        start_project(repo)
        start_project(repo / "tools", entries=_entries(3))
        write_empty_stray(repo / "old")
        before = tree_state(repo / "tools" / ".irp", repo / "old" / ".irp")
        run_irp(["doctor"], repo)
        run_irp(["doctor", "--fix"], repo)
        assert state_diff(before, tree_state(repo / "tools" / ".irp", repo / "old" / ".irp")) == []
        text = run_irp(["doctor"], repo).stdout
        assert _listed_empty(text, repo / "old" / ".irp")

    @pytest.mark.parametrize("cloud", ["Dropbox", "Proton Drive", "OneDrive"])
    def test_skips_the_scan_inside_a_cloud_synced_folder(self, root, cloud):
        top = root / cloud / "proj"
        start_project(top)
        write_empty_stray(top / "sub")
        text = run_irp(["doctor"], top).stdout
        assert str(top / "sub" / ".irp") not in text
        assert "cloud" in text.lower()


class TestWorktreeLedger:
    @pytest.fixture
    def main_and_wt(self, root):
        main = git_repo(root / "main")                     # .irp/ is gitignored
        start_project(main)
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        return main, wt

    def test_says_a_ledger_inside_a_worktree_goes_with_it(self, main_and_wt):
        main, wt = main_and_wt
        start_project(wt, entries=_entries(2))             # an older version captured here
        result = run_irp(["doctor"], wt)
        assert result.returncode == 0, out(result)
        assert (f"! This ledger is inside a linked worktree of {main} and git doesn't track it, "
                "so it goes when the worktree is removed.") in result.stdout
        data = json.loads(run_irp(["doctor", "--json"], wt).stdout)
        assert data["worktree_ledger_at_risk"] is True

    def test_a_tracked_ledger_inside_a_worktree_is_fine(self, root):
        main = git_repo(root / "main", ignore_irp=False)
        start_project(main)
        git(["add", "-A"], main)
        git(["commit", "-q", "-m", "ledger", "--no-verify"], main)
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        result = run_irp(["doctor"], wt)
        assert "goes when the worktree is removed" not in result.stdout
        assert json.loads(run_irp(["doctor", "--json"], wt).stdout)["worktree_ledger_at_risk"] is False

    def test_the_main_checkouts_ledger_found_from_a_worktree_is_fine(self, main_and_wt):
        main, wt = main_and_wt
        result = run_irp(["doctor"], wt)
        assert "goes when the worktree is removed" not in result.stdout


class TestNoProject:
    def test_fails_the_project_check_with_the_init_hint_and_exits_0(self, root):
        repo = git_repo(root / "repo")
        (repo / "src").mkdir()
        result = run_irp(["doctor"], repo / "src")
        assert result.returncode == 0, out(result)
        assert f"irp init {repo}" in result.stdout
        assert 'irp inherit "Project: <name>"' not in result.stdout
        assert irp_folders(repo) == []

    def test_json_reports_the_failed_project_check(self, root):
        repo = git_repo(root / "repo")
        result = run_irp(["doctor", "--json"], repo)
        assert result.returncode == 0, out(result)
        data = json.loads(result.stdout)
        assert data["status"] == "issues_found"
        failed = [c for c in data["checks"] if not c["ok"]]
        assert any(f"irp init {repo}" in c["detail"] for c in failed)
        assert irp_folders(repo) == []

    def test_still_lists_a_stray_on_the_way_up(self, root):
        repo = git_repo(root / "repo")
        deep = repo / "src" / "deep"
        write_empty_stray(deep)
        result = run_irp(["doctor"], deep)
        assert result.returncode == 0, out(result)
        assert _listed_empty(result.stdout, deep / ".irp")
