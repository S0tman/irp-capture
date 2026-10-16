"""The irp command finds its project, from any folder inside it.

Every test runs `irp` as a separate program in throwaway folders and repos.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date

import pytest

from project_kit import (
    CONTRADICTION,
    DECISION,
    DECISION_ID,
    commit_all,
    git,
    git_repo,
    irp_folders,
    ledger_lines,
    out,
    run_irp,
    start_project,
    state_diff,
    tree_state,
    write_empty_stray,
)

TODAY_ID_1 = f"IRP-{date.today().isoformat()}-001"
TODAY_ID_2 = f"IRP-{date.today().isoformat()}-002"
CANDIDATE = '{"what": "Keep notes in plain text", "why": "Easy to diff"}'
OTHER = dict(DECISION, id="IRP-2026-09-01-001", what="Ship weekly releases", why="Small batches")
DEMO_SEED = "IRP-2026-01-10-001"      # a decision in the built-in demo graph


def _old_ledger(folder, entry=OTHER):
    """A ledger as versions before 0.10 left one: ledger.jsonl holding entries, nothing else."""
    irp_dir = folder / ".irp"
    irp_dir.mkdir(parents=True)
    (irp_dir / "ledger.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return irp_dir


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


@pytest.fixture
def proj(root):
    """A git repo whose top folder is a project holding DECISION, with src/deep."""
    repo = git_repo(root / "proj")
    start_project(repo)
    (repo / "src" / "deep").mkdir(parents=True)
    return repo


def _project_line(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("Project:")]


# ── from any subfolder ─────────────────────────────────────────────────────────

class TestFromASubfolder:
    def test_check_finds_the_projects_conflict(self, proj):
        result = run_irp(["check", CONTRADICTION], proj / "src" / "deep")
        assert result.returncode == 10, out(result)
        assert DECISION_ID in result.stdout
        assert irp_folders(proj) == [str(proj / ".irp")]

    def test_capture_appends_to_the_project_ledger_with_the_next_id(self, root):
        repo = git_repo(root / "proj")
        start_project(repo, entries=(DECISION, dict(DECISION, id=TODAY_ID_1, what="Ship weekly")))
        deep = repo / "src" / "deep"
        deep.mkdir(parents=True)
        result = run_irp(["capture", "--stdin"], deep, input=CANDIDATE)
        assert result.returncode == 0, out(result)
        ids = [e["id"] for e in ledger_lines(repo / ".irp")]
        assert ids == [DECISION_ID, TODAY_ID_1, TODAY_ID_2]
        assert irp_folders(repo) == [str(repo / ".irp")]

    def test_why_id_finds_a_decision_from_the_project_ledger(self, proj):
        result = run_irp(["why", "--id", DECISION_ID], proj / "src" / "deep")
        assert result.returncode == 0, out(result)
        assert DECISION["what"] in result.stdout
        assert irp_folders(proj) == [str(proj / ".irp")]


# ── no false all-clear, and the no-project messages ──────────────────────────

class TestNoProject:
    @pytest.fixture
    def repo(self, root):
        repo = git_repo(root / "repo")
        (repo / "a" / "b").mkdir(parents=True)
        return repo

    def test_gate_says_no_project_and_exits_1(self, repo):
        cwd = repo / "a" / "b"
        result = run_irp(["gate", "drop the database"], cwd)
        assert result.returncode == 1
        assert json.loads(result.stdout) == {
            "verdict": "no_project", "exit_code": 1, "searched_from": str(cwd),
            "stopped_at": str(repo), "hint": f"irp init {repo}"}
        assert irp_folders(repo) == []

    def test_check_never_says_clear(self, repo):
        result = run_irp(["check", "drop the database"], repo / "a" / "b")
        assert result.returncode == 1
        assert "No conflicts" not in out(result)
        # pytest names the temp folder after this test, so take the paths out first.
        assert "clear" not in out(result).replace(str(repo.parent), "").lower()
        assert irp_folders(repo) == []

    def test_check_json_says_no_project(self, repo):
        cwd = repo / "a" / "b"
        result = run_irp(["check", "drop the database", "--json"], cwd)
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data == {"status": "no_project", "searched_from": str(cwd),
                        "stopped_at": str(repo), "hint": f"irp init {repo}"}

    def test_watch_exits_1_and_never_says_clear(self, repo):
        result = run_irp(["watch"], repo / "a" / "b", input="drop the database\n")
        assert result.returncode == 1
        assert '"clear"' not in result.stdout
        assert irp_folders(repo) == []

    def test_watch_says_no_project_in_the_same_json_as_check(self, repo):
        cwd = repo / "a" / "b"
        result = run_irp(["watch"], cwd, input="drop the database\n")
        assert result.returncode == 1
        assert result.stdout.count("\n") == 1, result.stdout
        assert json.loads(result.stdout) == {"status": "no_project", "searched_from": str(cwd),
                                             "stopped_at": str(repo), "hint": f"irp init {repo}"}
        assert irp_folders(repo) == []

    def test_watch_with_a_project_flag_that_points_at_nothing(self, repo, root):
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        result = run_irp(["watch", "--project", str(elsewhere)], repo, input="drop the database\n")
        assert result.returncode == 1
        check = run_irp(["check", "x", "--json", "--project", str(elsewhere)], repo)
        assert json.loads(result.stdout) == json.loads(check.stdout)
        assert json.loads(result.stdout)["status"] == "no_project"
        assert irp_folders(root) == []

    def test_the_message_inside_a_git_repo(self, repo):
        cwd = repo / "a" / "b"
        result = run_irp(["why"], cwd)
        assert result.returncode == 1
        text = out(result)
        assert (f"No IRP project found. Looked in {cwd} and the folders above it, "
                f"up to {repo} (the top of this git repo).") in text
        assert re.search(r"Start one:\s+irp init " + re.escape(str(repo)) + r"\n", text)
        assert re.search(r"Use another one:\s+irp --project PATH ", text)

    def test_the_message_outside_git(self, root):
        cwd = root / "plain" / "x"
        cwd.mkdir(parents=True)
        text = out(run_irp(["why"], cwd))
        assert (f"No IRP project found. Looked in {cwd} and the folders above it, "
                "up to / (the top of the disk).") in text
        assert re.search(r"Start one:\s+irp init " + re.escape(str(cwd)) + r"\n", text)

    def test_the_message_under_home_never_suggests_home(self, root, monkeypatch):
        home = root / "home"
        cwd = home / "notes"
        cwd.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        text = out(run_irp(["why"], cwd))
        assert f"up to {home} (your home folder, which is never used as a project)." in text
        assert re.search(r"Start one:\s+irp init " + re.escape(str(cwd)) + r"\n", text)
        result = run_irp(["why"], home)
        assert "irp init PATH/TO/PROJECT" in out(result)
        assert re.search(r"irp init " + re.escape(str(home)) + r"\s", out(result)) is None
        assert not (home / ".irp").exists()

    def test_a_write_says_nothing_was_written(self, repo):
        cwd = repo / "a" / "b"
        result = run_irp(["capture", "--stdin"], cwd, input=CANDIDATE)
        assert result.returncode == 1
        text = out(result)
        assert (f"Nothing was written. No IRP project found from {cwd} "
                f"(looked up to {repo}, the top of this git repo).") in text
        assert re.search(r"Start one:\s+irp init " + re.escape(str(repo)) + r"\n", text)
        assert irp_folders(repo) == []


# ── naming the project ────────────────────────────────────────────────────────

class TestNamingTheProject:
    @pytest.mark.parametrize("argv", [
        ["--project", "{p}", "why", "--id", DECISION_ID],
        ["why", "--id", DECISION_ID, "--project", "{p}"],
    ], ids=["before the command", "after the command"])
    def test_project_flag_before_or_after_the_command(self, argv, proj, root):
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        result = run_irp([a.replace("{p}", str(proj)) for a in argv], elsewhere)
        assert result.returncode == 0, out(result)
        assert DECISION["what"] in result.stdout
        assert list(elsewhere.iterdir()) == []

    def test_project_flag_after_a_sub_action(self, proj, root):
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        result = run_irp(["mod", "list", "--project", str(proj)], elsewhere)
        assert result.returncode == 0, out(result)
        assert list(elsewhere.iterdir()) == []

    def test_project_flag_beats_the_variable(self, root, monkeypatch):
        a = start_project(root / "a", entries=(dict(DECISION, id="IRP-2026-02-02-001", what="Project A rule"),))
        b = start_project(root / "b", entries=(dict(DECISION, id="IRP-2026-03-03-001", what="Project B rule"),))
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(a.parent))
        result = run_irp(["--project", str(b.parent), "why"], root)
        assert "Project B rule" in result.stdout
        assert "Project A rule" not in result.stdout

    def test_project_flag_with_no_project_names_the_path(self, proj, root):
        empty = root / "empty"
        empty.mkdir()
        result = run_irp(["--project", str(empty), "why"], proj)   # walking up would find proj
        assert result.returncode == 1
        assert (f"--project points at {empty}, but there's no IRP project there. "
                f"Start one with: irp init {empty}") in out(result)
        assert list(empty.iterdir()) == []

    def test_export_graphs_old_tag_flag_points_at_tag(self, proj):
        result = run_irp(["export", "graph", "--project", "irp-capture"], proj)
        assert result.returncode == 1
        assert "To filter by tag, use --tag NAME." in out(result)
        assert not (proj / "irp-capture").exists()

    def test_the_variable_with_no_project_names_the_path(self, proj, root, monkeypatch):
        empty = root / "empty"
        empty.mkdir()
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(empty))
        result = run_irp(["why"], proj)
        assert result.returncode == 1
        assert (f"IRP_PROJECT_ROOT points at {empty}, but there's no IRP project there. "
                f"Start one with: irp init {empty}") in out(result)
        assert list(empty.iterdir()) == []


# ── irp init ──────────────────────────────────────────────────────────────────

class TestInit:
    def test_starts_a_project(self, root):
        folder = root / "new"
        folder.mkdir()
        result = run_irp(["init"], folder)
        assert result.returncode == 0, out(result)
        assert result.stdout.splitlines()[:3] == [
            f"Started an IRP project in {folder}.",
            f"Decisions will be kept in {folder}/.irp/ledger.jsonl.",
            "Next: irp capture",
        ]
        irp_dir = folder / ".irp"
        assert sorted(p.name for p in irp_dir.iterdir()) == [
            ".gitignore", "config.json", "current.json", "ledger.jsonl"]
        assert (irp_dir / "ledger.jsonl").read_text(encoding="utf-8") == ""
        assert json.loads((irp_dir / "current.json").read_text(encoding="utf-8")) == {"version": 1, "active": []}
        assert ".lock" in (irp_dir / ".gitignore").read_text(encoding="utf-8").split()

    def test_takes_a_path(self, root):
        (root / "sub").mkdir()
        result = run_irp(["init", "sub"], root)
        assert result.returncode == 0, out(result)
        assert (root / "sub" / ".irp" / "config.json").exists()
        assert not (root / ".irp").exists()

    def test_again_changes_nothing(self, root):
        folder = root / "new"
        folder.mkdir()
        run_irp(["init"], folder)
        before = tree_state(folder)
        result = run_irp(["init"], folder)
        assert result.returncode == 0
        assert f"{folder} is already an IRP project (0 entries). Nothing changed." in result.stdout
        assert state_diff(before, tree_state(folder)) == []

    def test_inside_another_project_refuses_unless_nested(self, proj):
        inner = proj / "src"
        result = run_irp(["init"], inner)
        assert result.returncode == 1
        assert (f"{inner} is inside the IRP project at {proj}, so decisions made here already go there.\n"
                f"For a separate ledger in this folder, run: irp init --nested {inner}") in out(result)
        assert not (inner / ".irp").exists()
        result = run_irp(["init", "--nested"], inner)
        assert result.returncode == 0, out(result)
        assert (inner / ".irp" / "config.json").exists()

    def test_refuses_the_home_folder(self, root, monkeypatch):
        home = root / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        for cwd, argv in ((home, ["init"]), (root, ["init", str(home)])):
            result = run_irp(argv, cwd)
            assert result.returncode == 1
            assert ("Your home folder can't be an IRP project. Pick the project's own folder, "
                    "for example: irp init ~/my-project") in out(result)
            assert not (home / ".irp").exists()

    def test_refuses_a_dot_irp_that_is_not_irps(self, root):
        other = root / "other" / ".irp"
        other.mkdir(parents=True)
        (other / "test.txt").write_text("x", encoding="utf-8")
        before = tree_state(root)
        result = run_irp(["init"], root / "other")
        assert result.returncode == 1
        assert f"{other} already exists and IRP didn't make it. Left it alone, nothing changed." in out(result)
        assert state_diff(before, tree_state(root)) == []

    def test_adopts_an_empty_stray(self, root):
        folder = root / "old"
        irp_dir = write_empty_stray(folder)
        current = (irp_dir / "current.json").read_bytes()
        result = run_irp(["init"], folder)
        assert result.returncode == 0, out(result)
        assert (f"Started an IRP project in {folder}, using the empty .irp folder "
                "an older IRP version left here.") in result.stdout
        assert (irp_dir / "config.json").exists()
        assert (irp_dir / "current.json").read_bytes() == current

    def test_refuses_a_missing_folder(self, root):
        result = run_irp(["init", str(root / "nope")], root)
        assert result.returncode == 1
        assert (f"There's no folder at {root / 'nope'}. Create it first, "
                f"then run irp init {root / 'nope'}.") in out(result)
        assert not (root / "nope").exists()

    def test_says_when_the_folder_is_inside_a_bigger_repo(self, root):
        repo = git_repo(root / "repo")
        (repo / "app").mkdir()
        result = run_irp(["init"], repo / "app")
        assert result.returncode == 0, out(result)
        assert (f"This folder is inside the git repo at {repo}. "
                "That's fine when the repo holds more than one project.") in result.stdout

    def test_the_new_project_is_found_right_away(self, root):
        folder = root / "new"
        (folder / "src").mkdir(parents=True)
        run_irp(["init"], folder)
        check = run_irp(["check", "anything"], folder / "src")
        assert check.returncode == 0, out(check)
        capture = run_irp(["capture", "--stdin"], folder / "src", input=CANDIDATE)
        assert capture.returncode == 0, out(capture)
        assert [e["id"] for e in ledger_lines(folder / ".irp")] == [TODAY_ID_1]
        assert irp_folders(folder) == [str(folder / ".irp")]


# ── linked worktrees ─────────────────────────────────────────────────────────

@pytest.fixture
def main_and_wt(root):
    main = git_repo(root / "main")                 # .irp/ is gitignored
    (main / "app").mkdir()
    (main / "app" / "keep.txt").write_text("x\n", encoding="utf-8")
    git(["add", "-A"], main)
    git(["commit", "-q", "-m", "app", "--no-verify"], main)
    wt = root / "wt"
    git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
    return main, wt


class TestWorktrees:
    def test_check_finds_the_main_checkouts_ledger(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        result = run_irp(["check", CONTRADICTION], wt / "app")
        assert result.returncode == 10, out(result)
        assert DECISION_ID in result.stdout
        assert _project_line(result.stderr) == [f"Project: {main}"]
        assert irp_folders(wt) == []

    def test_capture_appends_to_the_main_checkouts_ledger(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        result = run_irp(["capture", "--stdin"], wt / "app", input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert [e["id"] for e in ledger_lines(main / ".irp")] == [DECISION_ID, TODAY_ID_1]
        assert irp_folders(wt) == []

    def test_init_refuses_in_a_worktree_unless_nested(self, main_and_wt):
        main, wt = main_and_wt
        result = run_irp(["init"], wt)
        assert result.returncode == 1
        text = out(result)
        assert (f"{wt} is a linked worktree of {main}. "
                "A ledger started here would go when the worktree is removed.") in text
        assert re.search(r"Start the project in the main checkout instead:\s+irp init " + re.escape(str(main)), text)
        assert re.search(r"For a ledger in this worktree anyway, run:\s+irp init --nested " + re.escape(str(wt)), text)
        assert irp_folders(wt) == []
        result = run_irp(["init", "--nested"], wt)
        assert result.returncode == 0, out(result)
        assert (wt / ".irp" / "config.json").exists()

    def test_init_in_a_worktree_subfolder_says_it_is_inside_the_worktree(self, main_and_wt):
        main, wt = main_and_wt
        result = run_irp(["init"], wt / "app")
        assert result.returncode == 1
        assert f"{wt / 'app'} is inside a linked worktree of {main}." in out(result)
        assert irp_folders(wt) == []

    def test_init_in_a_worktree_names_the_existing_project(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        result = run_irp(["init"], wt)
        assert result.returncode == 1
        assert f"Decisions made here already go to the project at {main}." in out(result)
        assert irp_folders(wt) == []

    def test_no_project_in_a_worktree_names_the_main_checkout_and_never_the_worktree(self, main_and_wt):
        main, wt = main_and_wt
        result = run_irp(["why"], wt / "app")
        assert result.returncode == 1
        text = out(result)
        assert (f"No IRP project found. Looked in {wt / 'app'} and the folders above it, "
                f"up to {wt} (a linked worktree of {main}), then in {main / 'app'} and up to {main}.") in text
        assert re.search(r"Start one in the main checkout:\s+irp init " + re.escape(str(main)) + r"\n", text)
        assert re.search(r"irp init " + re.escape(str(wt)) + r"\b", text) is None
        assert irp_folders(wt) == []

    def test_a_ledger_inside_the_worktree_names_the_main_checkouts_project(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        _old_ledger(wt)
        result = run_irp(["check", CONTRADICTION], wt)
        assert (f"Note: there's another IRP project in the main checkout, at {main}. "
                f"This command uses the one in this worktree, {wt}.") in result.stderr
        data = json.loads(run_irp(["check", CONTRADICTION, "--json"], wt).stdout)
        assert data["outer_projects"] == [str(main)]

    def test_a_capture_into_a_ledger_inside_the_worktree_says_it_goes_with_the_worktree(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        _old_ledger(wt)
        result = run_irp(["capture", "--stdin"], wt / "app", input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert (f"Note: this ledger is inside a linked worktree of {main} and goes when the worktree "
                f"is removed. To record in the main checkout instead: irp --project {main} capture") in result.stderr
        assert len(ledger_lines(main / ".irp")) == 1

    def test_a_ledger_git_tracks_inside_a_worktree_stays_with_git(self, root):
        main = git_repo(root / "main", ignore_irp=False)
        start_project(main)
        commit_all(main, "ledger")
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        result = run_irp(["capture", "--stdin"], wt, input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert "goes when the worktree is removed" not in out(result)
        # git keeps that ledger, so there's nothing to warn about.
        assert "in the main checkout" not in out(result)

    def test_an_untracked_ledger_inside_the_worktree_is_named_on_gate_and_watch(self, main_and_wt):
        main, wt = main_and_wt
        start_project(main)
        _old_ledger(wt)
        note = (f"Note: there's another IRP project in the main checkout, at {main}. "
                f"This command uses the one in this worktree, {wt}.")
        result = run_irp(["gate", CONTRADICTION], wt)
        assert note in result.stderr
        assert json.loads(result.stdout)["outer_projects"] == [str(main)]
        result = run_irp(["watch"], wt, input=CONTRADICTION + "\n")
        assert note in result.stderr
        assert json.loads(result.stdout.splitlines()[0])["outer_projects"] == [str(main)]

    def test_a_project_above_the_main_checkout_is_never_used(self, root):
        start_project(root)
        main = git_repo(root / "main")
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        result = run_irp(["check", CONTRADICTION], wt)
        assert result.returncode == 1
        assert DECISION_ID not in out(result)

    def test_a_write_in_a_worktree_never_suggests_the_worktree(self, main_and_wt):
        main, wt = main_and_wt
        result = run_irp(["capture", "--stdin"], wt / "app", input=CANDIDATE)
        assert result.returncode == 1
        assert f"irp init {main}" in out(result)
        assert re.search(r"irp init " + re.escape(str(wt)) + r"\b", out(result)) is None
        assert irp_folders(wt) == []


@pytest.fixture
def tracked_wt(root):
    """A repo that commits its ledger, and a linked worktree of it: the
    worktree has a ledger of its own that git tracks."""
    main = git_repo(root / "main", ignore_irp=False)
    start_project(main)
    commit_all(main, "ledger")
    wt = root / "wt"
    git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
    return main, wt


class TestTrackedWorktreeLedger:
    """git keeps a tracked ledger when the worktree goes, so IRP doesn't point
    at the main checkout's project in notes. JSON still lists it."""

    @pytest.mark.parametrize("argv", [
        ["why"],
        ["check", CONTRADICTION],
        ["inherit"],
        ["doctor"],
        ["capture", "--stdin"],
    ], ids=lambda argv: argv[0])
    def test_text_commands_say_nothing_about_the_main_checkout(self, tracked_wt, argv):
        main, wt = tracked_wt
        result = run_irp(argv, wt, input=CANDIDATE)
        assert result.returncode in (0, 10), out(result)
        assert "in the main checkout" not in out(result)
        assert "Note:" not in result.stderr

    def test_gate_and_watch_say_nothing_on_stderr(self, tracked_wt):
        main, wt = tracked_wt
        result = run_irp(["gate", CONTRADICTION], wt)
        assert result.stderr == ""
        result = run_irp(["watch"], wt, input=CONTRADICTION + "\nship it\n")
        assert result.stderr == ""

    def test_json_still_lists_the_main_checkouts_project(self, tracked_wt):
        main, wt = tracked_wt
        data = json.loads(run_irp(["check", CONTRADICTION, "--json"], wt).stdout)
        assert data["outer_projects"] == [str(main)]
        data = json.loads(run_irp(["why", "--json"], wt).stdout)
        assert data["outer_projects"] == [str(main)]
        data = json.loads(run_irp(["gate", CONTRADICTION], wt).stdout)
        assert data["outer_projects"] == [str(main)]
        lines = run_irp(["watch"], wt, input=CONTRADICTION + "\nship it\n").stdout.splitlines()
        assert len(lines) == 2
        assert all(json.loads(line)["outer_projects"] == [str(main)] for line in lines)

    def test_from_a_subfolder_the_project_line_stays(self, tracked_wt):
        main, wt = tracked_wt
        (wt / "src").mkdir()
        result = run_irp(["why"], wt / "src")
        assert _project_line(result.stderr) == [f"Project: {wt}"]
        assert "in the main checkout" not in result.stderr


def _main_checkout_note(main, wt):
    return (f"Note: there's another IRP project in the main checkout, at {main}. "
            f"This command uses the one in this worktree, {wt}.")


class TestMixedTrackedLedgers:
    """The main checkout's ledger and the worktree's own ledger in different
    states: what counts is whether git tracks the worktree's one."""

    def test_a_worktree_that_stops_tracking_its_ledger_hears_about_it(self, tracked_wt):
        main, wt = tracked_wt
        git(["rm", "-q", "--cached", ".irp/ledger.jsonl"], wt)
        note = _main_checkout_note(main, wt)
        assert note in run_irp(["why"], wt).stderr
        assert note in run_irp(["gate", CONTRADICTION], wt).stderr
        result = run_irp(["capture", "--stdin"], wt, input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert f"Note: this ledger is inside a linked worktree of {main} and goes" in result.stderr
        assert json.loads(run_irp(["doctor", "--json"], wt).stdout)["worktree_ledger_at_risk"] is True

    def test_a_worktree_that_tracks_its_own_ledger_stays_quiet(self, main_and_wt):
        main, wt = main_and_wt                     # the main checkout ignores .irp/
        start_project(main)
        start_project(wt, entries=(OTHER,))
        git(["add", "-f", ".irp/ledger.jsonl"], wt)
        assert run_irp(["why"], wt).stderr == ""
        assert run_irp(["gate", CONTRADICTION], wt).stderr == ""
        result = run_irp(["capture", "--stdin"], wt, input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert "goes when the worktree is removed" not in out(result)
        assert json.loads(run_irp(["doctor", "--json"], wt).stdout)["worktree_ledger_at_risk"] is False


class TestRunFromAGitHook:
    """git hooks run with GIT_DIR set to the worktree's git folder. IRP still
    asks git about the project's own ledger, not the worktree's top one."""

    def _repo(self, root, *, top_project):
        """A repo that commits a project in app/ (and, if asked, another at
        its top), and a linked worktree of it."""
        main = git_repo(root / "main", ignore_irp=False)
        if top_project:
            start_project(main)
        start_project(main / "app")
        commit_all(main, "ledgers")
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        hook_env = dict(os.environ, GIT_DIR=git(["rev-parse", "--absolute-git-dir"], wt).stdout.strip())
        return main, wt, hook_env

    @pytest.fixture
    def repo(self, root):
        return self._repo(root, top_project=True)

    def test_a_tracked_subfolder_ledger_stays_quiet(self, root):
        main, wt, hook_env = self._repo(root, top_project=False)
        result = run_irp(["gate", CONTRADICTION], wt / "app", env=hook_env)
        assert "in the main checkout" not in result.stderr, result.stderr

    def test_an_untracked_subfolder_ledger_still_gets_its_warning(self, repo):
        main, wt, hook_env = repo
        git(["rm", "-q", "--cached", "app/.irp/ledger.jsonl"], wt)
        result = run_irp(["capture", "--stdin"], wt / "app", input=CANDIDATE, env=hook_env)
        assert result.returncode == 0, out(result)
        assert _main_checkout_note(main / "app", wt / "app") in result.stderr
        assert f"Note: this ledger is inside a linked worktree of {main} and goes" in result.stderr
        doctor = json.loads(run_irp(["doctor", "--json"], wt / "app", env=hook_env).stdout)
        assert doctor["worktree_ledger_at_risk"] is True


class TestUnknownMainCheckout:
    @pytest.fixture
    def sep(self, root):
        """A repo whose git folder lives elsewhere, a project in it, and a worktree of it."""
        main = root / "main"
        (root / "store").mkdir()
        git(["init", "-q", "--separate-git-dir", str(root / "store" / "repo-git"), str(main)], root)
        (main / ".gitignore").write_text(".irp/\n", encoding="utf-8")
        commit_all(main, "start")
        start_project(main)
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "wt"], main)
        return main, wt

    def test_no_project_never_suggests_starting_one_in_the_worktree(self, sep):
        main, wt = sep
        result = run_irp(["why"], wt)
        assert result.returncode == 1
        text = out(result)
        assert f"up to {wt} (a linked worktree whose main checkout IRP couldn't find)." in text
        assert f"irp init {wt}" not in text
        data = json.loads(run_irp(["why", "--json"], wt).stdout)
        assert str(wt) not in data["hint"]
        assert irp_folders(wt) == []

    def test_init_refuses_unless_nested(self, sep):
        main, wt = sep
        result = run_irp(["init"], wt)
        assert result.returncode == 1
        text = out(result)
        assert (f"{wt} is a linked worktree and IRP couldn't find its main checkout. "
                "A ledger started here would go when the worktree is removed.") in text
        assert re.search(r"For a ledger in this worktree anyway, run:\s+irp init --nested " + re.escape(str(wt)), text)
        assert irp_folders(wt) == []
        result = run_irp(["init", "--nested"], wt)
        assert result.returncode == 0, out(result)
        assert (wt / ".irp" / "config.json").exists()


# ── which project answered ───────────────────────────────────────────────────

class TestProjectLine:
    def test_from_a_subfolder_it_goes_to_stderr(self, proj):
        result = run_irp(["why"], proj / "src" / "deep")
        assert result.returncode == 0, out(result)
        assert _project_line(result.stderr) == [f"Project: {proj}"]

    def test_nothing_extra_from_the_project_folder(self, proj):
        result = run_irp(["why"], proj)
        assert result.returncode == 0, out(result)
        assert result.stderr == ""

    def test_says_when_the_variable_chose_it(self, proj, monkeypatch):
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(proj))
        result = run_irp(["why"], proj)
        assert _project_line(result.stderr) == [f"Project: {proj} (from IRP_PROJECT_ROOT)"]

    def test_never_with_json(self, proj):
        result = run_irp(["why", "--json"], proj / "src" / "deep")
        assert result.returncode == 0, out(result)
        assert _project_line(result.stderr) == []
        json.loads(result.stdout)

    def test_names_another_project_further_up(self, proj):
        start_project(proj / "app")
        result = run_irp(["why"], proj / "app")
        assert (f"Note: there's another IRP project further up, at {proj}. "
                f"This command uses the nearer one, {proj / 'app'}.") in out(result)
        data = json.loads(run_irp(["why", "--json"], proj / "app").stdout)
        assert data["outer_projects"] == [str(proj)]

    def test_never_names_an_empty_stray_as_another_project(self, root):
        repo = git_repo(root / "repo")
        write_empty_stray(repo)
        start_project(repo / "app")
        result = run_irp(["why"], repo / "app")
        assert result.returncode == 0, out(result)
        assert "Note:" not in out(result)

    def test_a_read_never_says_it_skipped_an_empty_stray(self, proj):
        write_empty_stray(proj / "src")
        result = run_irp(["check", CONTRADICTION], proj / "src")
        assert DECISION_ID in out(result)
        assert "skipped an empty .irp folder" not in out(result)

    def test_gate_and_watch_list_an_outer_project(self, proj):
        deep = proj / "src" / "deep"
        _old_ledger(deep)                         # a ledger an older version forked off
        note = f"Note: there's another IRP project further up, at {proj}."
        result = run_irp(["gate", CONTRADICTION], deep)
        assert json.loads(result.stdout)["outer_projects"] == [str(proj)]
        assert note in result.stderr
        result = run_irp(["watch"], deep, input=CONTRADICTION + "\nship it\n")
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 2
        assert all(line["outer_projects"] == [str(proj)] for line in lines)
        assert note in result.stderr

    def test_gate_and_watch_say_nothing_extra_without_an_outer_project(self, proj):
        result = run_irp(["gate", CONTRADICTION], proj)
        assert "outer_projects" not in json.loads(result.stdout)
        assert result.stderr == ""
        result = run_irp(["watch"], proj, input=CONTRADICTION + "\n")
        assert "outer_projects" not in json.loads(result.stdout.splitlines()[0])
        assert result.stderr == ""

    @pytest.mark.parametrize("argv", [
        ["export", "decisions"],
        ["export", "context", "--target", "agents.md"],
        ["export", "graph"],
        ["export", "evidence"],
        ["craft", "export"],
        ["bootstrap", "--from", "docs", "--dry-run"],
    ], ids=lambda argv: " ".join(argv[:2]))
    def test_commands_that_name_their_project_say_it_once(self, proj, argv):
        result = run_irp(argv, proj / "src" / "deep")
        lines = [line for line in out(result).splitlines() if line.startswith("Project:")]
        assert lines == [f"Project: {proj}"], out(result)

    def test_a_capture_says_when_it_skips_an_empty_stray(self, proj):
        stray = write_empty_stray(proj / "src")
        before = tree_state(stray)
        result = run_irp(["capture", "--stdin"], proj / "src", input=CANDIDATE)
        assert result.returncode == 0, out(result)
        assert (f"Note: skipped an empty .irp folder at {proj / 'src'} "
                f"(an older IRP version left it). Writing to the project at {proj}.") in out(result)
        assert len(ledger_lines(proj / ".irp")) == 2
        assert state_diff(before, tree_state(stray)) == []


# ── output paths ──────────────────────────────────────────────────────────────

class TestOutputPaths:
    def test_a_relative_output_counts_from_where_you_stand(self, proj):
        deep = proj / "src" / "deep"
        result = run_irp(["export", "decisions", "--output", "out.md"], deep)
        assert result.returncode == 0, out(result)
        assert DECISION["what"] in (deep / "out.md").read_text(encoding="utf-8")
        assert not (proj / "out.md").exists()
        assert irp_folders(proj) == [str(proj / ".irp")]

    def test_the_default_name_lands_in_the_project_folder(self, proj):
        result = run_irp(["export", "decisions"], proj / "src" / "deep")
        assert result.returncode == 0, out(result)
        assert (proj / "DECISIONS.md").exists()
        assert not (proj / "src" / "deep" / "DECISIONS.md").exists()

    def test_output_from_a_worktree_lands_in_the_worktree(self, root):
        main = git_repo(root / "main")
        (main / "AGENTS.md").write_text("hand written\n", encoding="utf-8")
        commit_all(main, "agents")
        start_project(main)
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "feat"], main)
        (main / "AGENTS.md").write_text("hand written\nwork in progress\n", encoding="utf-8")
        result = run_irp(["export", "context", "--target", "agents.md", "--force"], wt)
        assert result.returncode == 0, out(result)
        assert f"Wrote {wt / 'AGENTS.md'}" in result.stdout
        assert DECISION["what"] in (wt / "AGENTS.md").read_text(encoding="utf-8")
        assert (main / "AGENTS.md").read_text(encoding="utf-8") == "hand written\nwork in progress\n"
        result = run_irp(["export", "decisions"], wt)
        assert result.returncode == 0, out(result)
        assert (wt / "DECISIONS.md").exists()
        assert not (main / "DECISIONS.md").exists()

    def test_output_from_a_worktree_subfolder_lands_beside_the_project(self, root):
        main = git_repo(root / "main")
        (main / "app" / "src").mkdir(parents=True)
        (main / "app" / "src" / "keep.txt").write_text("x\n", encoding="utf-8")
        commit_all(main, "app")
        start_project(main / "app")
        wt = root / "wt"
        git(["worktree", "add", "-q", str(wt), "-b", "feat"], main)
        result = run_irp(["export", "decisions"], wt / "app" / "src")
        assert result.returncode == 0, out(result)
        assert (wt / "app" / "DECISIONS.md").exists()
        assert not (main / "app" / "DECISIONS.md").exists()

    @pytest.mark.parametrize("view", [
        ["--view", "foundations"],
        ["--view", "lineage", "--seed", DEMO_SEED],
        ["--view", "impact", "--seed", DEMO_SEED],
    ], ids=["foundations", "lineage", "impact"])
    def test_export_graph_demo_lens_views_need_no_project(self, root, view):
        folder = root / "empty"
        folder.mkdir()
        result = run_irp(["export", "graph", "--demo", *view], folder)
        assert result.returncode == 0, out(result)
        assert sorted(p.name for p in folder.iterdir()) == ["GRAPH-demo.html"]

    def test_export_graph_demo_needs_no_project(self, root):
        folder = root / "empty"
        folder.mkdir()
        result = run_irp(["export", "graph", "--demo"], folder)
        assert result.returncode == 0, out(result)
        assert sorted(p.name for p in folder.iterdir()) == ["GRAPH-demo.html"]
