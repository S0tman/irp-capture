"""The project finder: how IRP finds the project it works on.

These are unit tests of irp/core/store.py:

- find_project(start=None, project=None) -> Project
    Order: `project` (the --project flag), then IRP_PROJECT_ROOT, then walking
    up from `start` (default: the current folder). It only reads: it never
    writes and never runs git or any other program.
    Project fields: root (the folder holding .irp/), irp_dir, source
    ("--project", "IRP_PROJECT_ROOT" or "walk"), main_checkout (the main
    checkout's folder when the worktree rule found the project, else None),
    skipped_strays (folders holding an empty stray passed on the way up,
    nearest first) and outer_projects (real projects further up in the same
    repo).
- ProjectNotFound(Exception), with searched_from, stopped_at, named_by
  (None, "--project" or "IRP_PROJECT_ROOT") and main_checkout.
- classify_irp_folder(irp_dir) -> "project", "empty_stray" or "not_irp".
- init_project(folder) -> the new .irp/ folder.

Every call here also checks that the finder left the whole temp tree as it
found it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import store
from project_kit import (
    DECISION,
    DEFAULT_CURRENT_COMPACT,
    DEFAULT_CURRENT_INDENTED,
    git,
    git_repo,
    start_project,
    state_diff,
    tree_state,
    write_empty_stray,
)


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


def _find(tree: Path, start: Path | None = None, **kwargs):
    """find_project, checking it changed nothing under `tree`."""
    before = tree_state(tree)
    try:
        return store.find_project(start, **kwargs)
    finally:
        assert state_diff(before, tree_state(tree)) == [], "the project finder wrote something"


def _not_found(tree: Path, start: Path | None = None, **kwargs):
    with pytest.raises(store.ProjectNotFound) as info:
        _find(tree, start, **kwargs)
    return info.value


# ── walking up ────────────────────────────────────────────────────────────────

class TestWalkUp:
    def test_finds_the_project_from_a_nested_subfolder(self, root):
        start_project(root)
        deep = root / "src" / "deep"
        deep.mkdir(parents=True)
        found = _find(root, deep)
        assert found.root == root
        assert found.irp_dir == root / ".irp"
        assert found.source == "walk"
        assert found.main_checkout is None

    def test_uses_the_current_folder_by_default(self, root, monkeypatch):
        start_project(root)
        deep = root / "a" / "b"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        assert _find(root).root == root

    def test_no_project_in_an_empty_tree_creates_nothing(self, root):
        deep = root / "empty" / "deep"
        deep.mkdir(parents=True)
        missing = _not_found(root, deep)
        assert missing.searched_from == deep
        assert missing.stopped_at == Path("/")
        assert missing.named_by is None
        assert not (deep / ".irp").exists()

    def test_stops_at_the_git_top_folder(self, root):
        start_project(root)                       # a ledger above the repo
        repo = git_repo(root / "repo")
        (repo / "src").mkdir()
        missing = _not_found(root, repo / "src")
        assert missing.stopped_at == repo

    def test_finds_a_project_at_the_git_top_folder(self, root):
        repo = git_repo(root / "repo")
        start_project(repo)
        (repo / "src" / "deep").mkdir(parents=True)
        assert _find(root, repo / "src" / "deep").root == repo

    def test_stops_at_a_git_file(self, root):
        start_project(root)
        sub = root / "sub"
        (sub / "src").mkdir(parents=True)
        (sub / ".git").write_text("gitdir: /nonexistent/modules/sub\n", encoding="utf-8")
        missing = _not_found(root, sub / "src")
        assert missing.stopped_at == sub

    def test_never_uses_the_home_folder(self, root, monkeypatch):
        home = root / "home"
        start_project(home)                       # a ledger right in home
        deep = home / "a" / "b"
        deep.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        missing = _not_found(root, deep)
        assert missing.stopped_at == home
        assert _not_found(root, home).stopped_at == home

    def test_finds_a_project_below_home(self, root, monkeypatch):
        home = root / "home"
        start_project(home / "proj")
        (home / "proj" / "src").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        assert _find(root, home / "proj" / "src").root == home / "proj"

    def test_follows_symlinks_to_the_real_folder(self, root):
        start_project(root / "real")
        (root / "real" / "src").mkdir()
        (root / "link").symlink_to(root / "real" / "src")
        assert _find(root, root / "link").root == root / "real"


class TestStrays:
    def test_skips_an_empty_stray_and_reports_it(self, root):
        start_project(root)
        write_empty_stray(root / "src")
        (root / "src" / "deep").mkdir(parents=True)
        found = _find(root, root / "src" / "deep")
        assert found.root == root
        assert found.skipped_strays == [root / "src"]

    def test_skips_every_empty_stray_on_the_way_nearest_first(self, root):
        start_project(root)
        write_empty_stray(root / "a")
        write_empty_stray(root / "a" / "b", current=DEFAULT_CURRENT_COMPACT)
        found = _find(root, root / "a" / "b")
        assert found.root == root
        assert found.skipped_strays == [root / "a" / "b", root / "a"]

    def test_only_an_empty_stray_is_no_project(self, root):
        write_empty_stray(root / "proj")
        _not_found(root, root / "proj")

    def test_skips_a_dot_irp_that_is_not_irps(self, root):
        start_project(root)
        other = root / "src" / ".irp"
        other.mkdir(parents=True)
        (other / "test.txt").write_text("not a ledger\n", encoding="utf-8")
        found = _find(root, root / "src")
        assert found.root == root
        assert found.skipped_strays == []

    def test_uses_a_nested_ledger_holding_entries_and_reports_the_outer_one(self, root):
        repo = git_repo(root / "repo")
        start_project(repo)
        start_project(repo / "tools")
        (repo / "tools" / "x").mkdir()
        found = _find(root, repo / "tools" / "x")
        assert found.root == repo / "tools"
        assert found.outer_projects == [repo]

    def test_an_outer_empty_stray_is_never_reported_as_another_project(self, root):
        repo = git_repo(root / "repo")
        write_empty_stray(repo)
        start_project(repo / "app")
        found = _find(root, repo / "app")
        assert found.root == repo / "app"
        assert found.outer_projects == []

    def test_outer_projects_stop_at_the_git_top_folder(self, root):
        start_project(root)                       # above the repo: not the same repo
        repo = git_repo(root / "repo")
        start_project(repo / "app")
        found = _find(root, repo / "app")
        assert found.root == repo / "app"
        assert found.outer_projects == []


# ── naming the project ────────────────────────────────────────────────────────

class TestNamedProject:
    def _three(self, root):
        walk, env, flag = root / "walk", root / "env", root / "flag"
        for folder in (walk, env, flag):
            start_project(folder)
        (walk / "sub").mkdir()
        return walk, env, flag

    def test_project_flag_beats_the_variable_which_beats_walking_up(self, root, monkeypatch):
        walk, env, flag = self._three(root)
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(env))
        found = _find(root, walk / "sub", project=flag)
        assert (found.root, found.source) == (flag, "--project")
        found = _find(root, walk / "sub")
        assert (found.root, found.source) == (env, "IRP_PROJECT_ROOT")
        monkeypatch.delenv("IRP_PROJECT_ROOT")
        found = _find(root, walk / "sub")
        assert (found.root, found.source) == (walk, "walk")

    def test_a_named_path_with_no_project_raises_with_no_fallback(self, root, monkeypatch):
        start_project(root)                       # walking up would find this
        empty = root / "empty"
        empty.mkdir()
        missing = _not_found(root, root, project=empty)
        assert missing.named_by == "--project"
        assert missing.searched_from == empty
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(empty))
        missing = _not_found(root, root)
        assert missing.named_by == "IRP_PROJECT_ROOT"
        assert not (empty / ".irp").exists()

    def test_a_named_path_is_not_walked_up(self, root):
        start_project(root)
        (root / "src").mkdir()
        _not_found(root, root, project=root / "src")

    def test_a_named_empty_stray_is_no_project(self, root):
        write_empty_stray(root / "stray")
        _not_found(root, root, project=root / "stray")


# ── the three kinds of .irp/ ─────────────────────────────────────────────────

def _files(irp_dir: Path, files: dict[str, str]) -> Path:
    irp_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = irp_dir / name
        if text is None:
            path.mkdir()
        else:
            path.write_text(text, encoding="utf-8")
    return irp_dir


_ENTRY = json.dumps(DECISION) + "\n"

EMPTY_STRAYS = {
    "indented, as ensure_irp_dir wrote it": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED},
    "indented with a trailing newline": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED + "\n"},
    "compact": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_COMPACT},
    "compact with a trailing newline": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_COMPACT + "\n"},
    "compact without spaces": {"ledger.jsonl": "", "current.json": '{"version":1,"active":[]}'},
    "blank ledger lines": {"ledger.jsonl": "\n\n  \n", "current.json": DEFAULT_CURRENT_COMPACT},
    "no current.json": {"ledger.jsonl": ""},
    "blank current.json": {"ledger.jsonl": "", "current.json": "  \n"},
    "only a default current.json": {"current.json": DEFAULT_CURRENT_INDENTED},
    "with the lock files": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED,
                            ".gitignore": ".lock\n", ".lock": ""},
}

PROJECTS = {
    "a ledger entry": {"ledger.jsonl": _ENTRY, "current.json": DEFAULT_CURRENT_INDENTED},
    "legacy: only a filled-in current.json": {
        "current.json": json.dumps({"version": 1, "active": [DECISION]})},
    "started by init: config.json": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED,
                                     "config.json": '{"control_level": "advanced"}'},
    "another file": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED, "craft.jsonl": ""},
    "a subfolder": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED, "derived": None},
    "a .gitignore that isn't only .lock": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED,
                                           ".gitignore": "*\n"},
    "a .lock that isn't empty": {"ledger.jsonl": "", "current.json": DEFAULT_CURRENT_INDENTED, ".lock": "x"},
    "a current.json that isn't the default": {"ledger.jsonl": "",
                                              "current.json": '{"version": 2, "active": []}'},
    "a current.json that doesn't parse": {"ledger.jsonl": "", "current.json": "{"},
}

NOT_IRPS = {
    "only someone else's file": {"test.txt": "x"},
    "an empty folder": {},
    "only lock files": {".gitignore": ".lock\n", ".lock": ""},
}


class TestClassify:
    def test_the_indented_default_is_the_34_byte_form(self):
        assert len(DEFAULT_CURRENT_INDENTED.encode("utf-8")) == 34

    @pytest.mark.parametrize("case", sorted(EMPTY_STRAYS))
    def test_empty_stray(self, root, case):
        irp_dir = _files(root / ".irp", EMPTY_STRAYS[case])
        assert store.classify_irp_folder(irp_dir) == "empty_stray"

    @pytest.mark.parametrize("case", sorted(PROJECTS))
    def test_project(self, root, case):
        irp_dir = _files(root / ".irp", PROJECTS[case])
        assert store.classify_irp_folder(irp_dir) == "project"

    @pytest.mark.parametrize("case", sorted(NOT_IRPS))
    def test_not_irps(self, root, case):
        irp_dir = _files(root / ".irp", NOT_IRPS[case])
        assert store.classify_irp_folder(irp_dir) == "not_irp"

    def test_classify_only_reads(self, root):
        irp_dir = write_empty_stray(root)
        before = tree_state(root)
        store.classify_irp_folder(irp_dir)
        assert state_diff(before, tree_state(root)) == []


# ── init_project ──────────────────────────────────────────────────────────────

class TestInitProject:
    def test_starts_a_project_that_is_found_with_no_decisions(self, root):
        irp_dir = store.init_project(root)
        assert irp_dir == root / ".irp"
        assert (irp_dir / "ledger.jsonl").read_text(encoding="utf-8") == ""
        assert json.loads((irp_dir / "current.json").read_text(encoding="utf-8")) == {"version": 1, "active": []}
        assert json.loads((irp_dir / "config.json").read_text(encoding="utf-8")) == {"control_level": "advanced"}
        assert ".lock" in (irp_dir / ".gitignore").read_text(encoding="utf-8").split()
        assert store.classify_irp_folder(irp_dir) == "project"
        (root / "src").mkdir()
        assert _find(root, root / "src").root == root

    def test_adopts_an_empty_stray_and_keeps_its_files(self, root):
        irp_dir = write_empty_stray(root)
        current_before = (irp_dir / "current.json").read_bytes()
        store.init_project(root)
        assert (irp_dir / "current.json").read_bytes() == current_before
        assert (irp_dir / "config.json").exists()
        assert store.classify_irp_folder(irp_dir) == "project"

    def test_refuses_a_dot_irp_that_is_not_irps(self, root):
        other = root / ".irp"
        other.mkdir()
        (other / "test.txt").write_text("x", encoding="utf-8")
        before = tree_state(root)
        with pytest.raises(FileExistsError):
            store.init_project(root)
        assert state_diff(before, tree_state(root)) == []

    def test_changes_nothing_in_an_existing_project(self, root):
        start_project(root)
        store.init_project(root)                  # make sure config.json is there
        before = tree_state(root)
        store.init_project(root)
        assert state_diff(before, tree_state(root)) == []


# ── linked worktrees (the worktree rule) ────────────────────────────────────

def _main_and_worktree(root: Path, project_at: str | None = "."):
    main = git_repo(root / "main")                # .irp/ is gitignored there
    (main / "app" / "src").mkdir(parents=True)
    (main / "app" / "src" / "keep.txt").write_text("x\n", encoding="utf-8")
    git(["add", "-A"], main)
    git(["commit", "-q", "-m", "app", "--no-verify"], main)
    if project_at is not None:
        start_project(main / project_at)
    wt = root / "wt"
    git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
    return main, wt


class TestWorktrees:
    def test_a_worktree_with_no_ledger_finds_the_main_checkouts(self, root):
        main, wt = _main_and_worktree(root)
        found = _find(root, wt / "app" / "src")
        assert found.root == main
        assert found.main_checkout == main

    def test_the_same_past_an_empty_stray_at_the_worktrees_top(self, root):
        main, wt = _main_and_worktree(root)
        write_empty_stray(wt)
        found = _find(root, wt / "app" / "src")
        assert found.root == main
        assert found.skipped_strays == [wt]

    def test_walks_the_same_relative_path_in_the_main_checkout(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        found = _find(root, wt / "app" / "src")
        assert found.root == main / "app"
        assert found.main_checkout == main

    def test_a_folder_only_on_the_worktrees_branch_is_passed_over(self, root):
        main, wt = _main_and_worktree(root)
        (wt / "only-here" / "deep").mkdir(parents=True)
        assert _find(root, wt / "only-here" / "deep").root == main

    def test_a_worktree_with_its_own_ledger_uses_it(self, root):
        main, wt = _main_and_worktree(root)
        start_project(wt)
        found = _find(root, wt / "app")
        assert found.root == wt
        assert found.main_checkout is None

    def test_a_ledger_inside_a_worktree_lists_the_main_checkouts_project(self, root):
        main, wt = _main_and_worktree(root)
        start_project(wt)                         # an older version captured here
        found = _find(root, wt / "app")
        assert found.root == wt
        assert found.worktree_of == main
        assert found.outer_projects == [main]

    def test_a_nested_ledger_inside_a_worktree_lists_the_main_checkouts_projects(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        start_project(main)
        start_project(wt / "app" / "src")
        found = _find(root, wt / "app" / "src")
        assert found.root == wt / "app" / "src"
        assert found.outer_projects == [main / "app", main]

    def test_a_ledger_inside_a_worktree_with_none_in_the_main_checkout(self, root):
        main, wt = _main_and_worktree(root, project_at=None)
        start_project(wt)
        found = _find(root, wt)
        assert found.root == wt
        assert found.worktree_of == main
        assert found.outer_projects == []

    def test_a_named_ledger_inside_a_worktree_lists_the_main_checkouts_project(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        start_project(wt / "app")
        found = _find(root, root, project=wt / "app")
        assert found.root == wt / "app"
        assert found.worktree_of == main
        assert found.outer_projects == [main / "app"]

    def test_a_project_in_the_main_checkout_is_not_inside_a_worktree(self, root):
        main, wt = _main_and_worktree(root)
        found = _find(root, main / "app")
        assert found.root == main
        assert found.worktree_of is None
        assert found.outer_projects == []

    def test_a_named_worktree_path_maps_to_the_same_path_in_the_main_checkout(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        found = _find(root, root, project=wt / "app")
        assert found.root == main / "app"
        assert found.main_checkout == main

    def test_a_named_worktree_path_stays_strict(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        missing = _not_found(root, root, project=wt)   # main/ has no project, main/app isn't looked at
        assert missing.main_checkout == main

    def test_no_project_anywhere_names_the_main_checkout(self, root):
        main, wt = _main_and_worktree(root, project_at=None)
        missing = _not_found(root, wt / "app")
        assert missing.main_checkout == main

    def test_a_bare_repos_worktree_finds_no_project(self, root):
        src = git_repo(root / "src")
        outer = root / "outer"
        start_project(outer)                      # the folder holding repo.git: the wrong place
        git(["clone", "--bare", "-q", str(src), str(outer / "repo.git")], root)
        wt = outer / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], outer / "repo.git")
        missing = _not_found(root, wt)
        assert missing.main_checkout is None

    def test_a_project_above_the_main_checkout_is_never_used(self, root):
        start_project(root)                       # above the main checkout's top
        main = git_repo(root / "main")
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        missing = _not_found(root, wt)
        assert missing.main_checkout == main

    def test_a_named_worktree_path_maps_to_where_the_worktree_is(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        found = _find(root, root, project=wt / "app")
        assert found.worktree_root == wt / "app"

    def test_walking_up_in_a_worktree_maps_back_to_the_worktree(self, root):
        main, wt = _main_and_worktree(root, project_at="app")
        found = _find(root, wt / "app" / "src")
        assert found.root == main / "app"
        assert found.worktree_root == wt / "app"

    def test_a_project_found_directly_has_no_worktree_folder(self, root):
        main, wt = _main_and_worktree(root)
        assert _find(root, main / "app").worktree_root is None

    def test_a_submodule_top_never_reaches_its_parent(self, root):
        sub_src = git_repo(root / "subsrc", ignore_irp=False)
        sup = git_repo(root / "super")
        start_project(sup)
        git(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub"], sup)
        missing = _not_found(root, sup / "sub")
        assert missing.stopped_at == sup / "sub"

    def test_a_submodule_finds_no_project_and_never_reaches_its_parent(self, root):
        sub_src = git_repo(root / "subsrc", ignore_irp=False)
        sup = git_repo(root / "super")
        start_project(sup)
        git(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub"], sup)
        (sup / "sub" / "x").mkdir()
        missing = _not_found(root, sup / "sub" / "x")
        assert missing.stopped_at == sup / "sub"
        assert missing.main_checkout is None

    def test_the_finder_runs_no_programs(self, root, monkeypatch):
        main, wt = _main_and_worktree(root)

        def refuse(*args, **kwargs):
            raise AssertionError("the project finder started a program")

        monkeypatch.setattr(subprocess.Popen, "__init__", refuse)
        assert _find(root, wt / "app" / "src").root == main


# ── linked worktrees whose main checkout can't be found ──────────────────────

def _separate_git_dir_worktree(root: Path):
    """A repo whose git folder lives elsewhere (git init --separate-git-dir), and a worktree of it."""
    main = root / "main"
    (root / "store").mkdir(parents=True)
    git(["init", "-q", "--separate-git-dir", str(root / "store" / "repo-git"), str(main)], root)
    (main / ".gitignore").write_text(".irp/\n", encoding="utf-8")
    git(["add", "-A"], main)
    git(["commit", "-q", "-m", "start", "--no-verify"], main)
    start_project(main)
    wt = root / "wt"
    git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
    return main, wt


class TestUnknownMainCheckout:
    def test_is_linked_worktree(self, root):
        main, wt = _main_and_worktree(root)
        assert store.is_linked_worktree(wt) is True
        assert store.is_linked_worktree(main) is False
        sep_main, sep_wt = _separate_git_dir_worktree(root / "sep")
        assert store.is_linked_worktree(sep_wt) is True
        assert store.is_linked_worktree(sep_main) is False

    def test_a_submodule_is_not_a_linked_worktree(self, root):
        sub_src = git_repo(root / "subsrc", ignore_irp=False)
        sup = git_repo(root / "super")
        git(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "sub"], sup)
        assert store.is_linked_worktree(sup / "sub") is False

    def test_the_hint_never_names_the_worktree(self, root):
        main, wt = _separate_git_dir_worktree(root)
        missing = _not_found(root, wt)
        assert missing.main_checkout is None
        assert str(wt) not in missing.hint
        assert f"irp init {wt}" not in missing.read_message()
        assert f"irp init {wt}" not in missing.write_message()

    def test_a_named_path_in_it_never_suggests_itself(self, root):
        main, wt = _separate_git_dir_worktree(root)
        missing = _not_found(root, root, project=wt)
        assert f"irp init {wt}" not in missing.read_message()
