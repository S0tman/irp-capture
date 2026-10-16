"""The no-write check: commands that only read never create anything.

- Every command and sub-action from build_parser() has a class in
  irp.core.irp.COMMAND_CLASSES, and the classes are the ones listed below.
- Read, output, hook and report commands run in an empty tree with a fake home
  create nothing, in the tree or in home.
- Inside a project, read and output commands leave the project as they found
  it, apart from the file an output command was asked to write.
- With no project, write commands write nothing, print the `irp init` line and
  exit 1; every --json command prints the no-project JSON on stdout.
- The graph library cache lives under ~/.cache/irp/.
- `integrity verify --ledger FILE` and `attest verify --receipt FILE` check a
  file someone sent you with no project at all, create nothing and answer
  exactly as they would inside one.
- A source check: nothing outside `init` and `store` makes `.irp/`, and no
  code under irp/ or eval/ calls ensure_irp_dir.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_kit import (
    DECISION_ID,
    REPO,
    run_irp,
    start_project,
    state_diff,
    tree_state,
)

# What each command may do. "none" means no project needed.
EXPECTED_CLASSES = {
    "inherit": "read", "why": "read", "find": "read", "check": "read", "resolve": "read",
    "gate": "read", "watch": "read", "mod list": "read", "config get": "read",
    "craft list": "read", "integrity verify": "read", "attest verify": "read",
    "guard run": "hook",
    "export context": "output", "export decisions": "output", "export graph": "output",
    "export evidence": "output", "craft export": "output",
    "capture": "write", "mod supersede": "write", "mod retire": "write", "defer": "write",
    "craft add": "write", "demo generate": "write", "bootstrap": "write", "config set": "write",
    "integrity snapshot": "write", "attest create": "write", "guard install": "write",
    "init": "start",
    "doctor": "report", "guard status": "report",
    "docs pull": "none", "docs push": "none", "docs list": "none",
}

# How to run each command that must never write. The snapshot path doesn't
# exist in an empty tree; with no project the command stops before reading it.
NO_WRITE_ARGV = {
    "inherit": ["inherit"],
    "why": ["why"],
    "why --id": ["why", "--id", DECISION_ID],
    "find": ["find", "postgres"],
    "check": ["check", "move the reporting service to redis"],
    "resolve": ["resolve", "move the reporting service to redis"],
    "gate": ["gate", "move the reporting service to redis"],
    "watch": ["watch"],
    "mod list": ["mod", "list"],
    "config get": ["config", "get"],
    "craft list": ["craft", "list"],
    "integrity verify": ["integrity", "verify", "SNAPSHOT"],
    "attest verify": ["attest", "verify", "SNAPSHOT"],
    "guard run": ["guard", "run"],
    "guard status": ["guard", "status"],
    "doctor": ["doctor"],
    "docs list": ["docs", "list"],
    "export context": ["export", "context", "--target", "agents.md"],
    "export decisions": ["export", "decisions"],
    "export graph": ["export", "graph"],
    "export evidence": ["export", "evidence"],
    "craft export": ["craft", "export"],
}
_STDIN = {"watch": "move the reporting service to redis\n"}

READ_AND_OUTPUT = sorted(k for k, v in EXPECTED_CLASSES.items() if v in ("read", "output"))

# The file each output command writes by default, in the project folder.
OUTPUT_FILES = {
    "export context": "AGENTS.md",
    "export decisions": "DECISIONS.md",
    "export graph": "GRAPH.html",
    "export evidence": "EVIDENCE-euaiact.md",
    "craft export": "CRAFT.md",
}

WRITE_ARGV = {
    "capture": (["capture", "--stdin"], '{"what": "Keep notes in plain text", "why": "Easy to diff"}'),
    "mod supersede": (["mod", "supersede", DECISION_ID, "--decision", "Use MySQL", "--reason", "Licence"], None),
    "mod retire": (["mod", "retire", DECISION_ID, "--reason", "No longer needed"], None),
    "defer": (["defer", "Should we keep Postgres?"], ""),
    "craft add": (["craft", "add", "--category", "gotcha", "--what", "Run tests sealed"], None),
    "demo generate": (["demo", "generate", "--scenario", "pricing"], None),
    "bootstrap": (["bootstrap", "--from", "docs"], None),
    "bootstrap --accept": (["bootstrap", "--accept", "REC-1999-01-01-001"], None),
    "config set": (["config", "set", "control_level", "easy"], None),
    "integrity snapshot": (["integrity", "snapshot"], None),
    "attest create": (["attest", "create", "snapshot.json"], None),
    "guard install": (["guard", "install"], None),
}

# Commands in the read, output and write classes that take --json.
JSON_ARGV = {
    "inherit": ["inherit"], "why": ["why"], "find": ["find", "x"], "check": ["check", "x"],
    "resolve": ["resolve", "x"], "mod list": ["mod", "list"], "config get": ["config", "get"],
    "craft list": ["craft", "list"], "integrity verify": ["integrity", "verify", "snapshot.json"],
    "attest verify": ["attest", "verify", "snapshot.json"],
    "export context": ["export", "context", "--target", "agents.md"],
    "export decisions": ["export", "decisions"], "export graph": ["export", "graph"],
    "export evidence": ["export", "evidence"], "craft export": ["craft", "export"],
    "capture": ["capture", "--stdin"],
    "mod supersede": ["mod", "supersede", DECISION_ID, "--decision", "d", "--reason", "r"],
    "mod retire": ["mod", "retire", DECISION_ID, "--reason", "r"],
    "defer": ["defer", "Should we?"], "craft add": ["craft", "add", "--category", "gotcha", "--what", "x"],
    "demo generate": ["demo", "generate", "--scenario", "pricing"], "bootstrap": ["bootstrap", "--from", "docs"],
    "config set": ["config", "set", "control_level", "easy"], "integrity snapshot": ["integrity", "snapshot"],
    "attest create": ["attest", "create", "snapshot.json"], "guard install": ["guard", "install"],
}
_JSON_STDIN = {"capture": '{"what": "x", "why": "y"}', "defer": ""}


def _empty_tree(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path.resolve() / "tree"
    cwd = tree / "a" / "b"
    cwd.mkdir(parents=True)
    return tree, cwd


# ── the class table ───────────────────────────────────────────────────────────

_LIST_CLI = r"""
import argparse, json, sys
sys.path.insert(0, sys.argv[1])
import irp.core.irp as cli

def subcommands(p):
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}

paths = []
for name, sp in subcommands(cli.build_parser()).items():
    subs = subcommands(sp)
    paths += [f"{name} {s}" for s in subs] if subs else [name]
print(json.dumps({"commands": sorted(paths), "classes": getattr(cli, "COMMAND_CLASSES", None)}))
"""


@pytest.fixture(scope="module")
def cli_table():
    result = subprocess.run([sys.executable, "-c", _LIST_CLI, str(REPO)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestClassTable:
    def test_every_command_and_sub_action_has_a_class(self, cli_table):
        classes = cli_table["classes"]
        assert classes is not None, "irp.core.irp has no COMMAND_CLASSES table"
        missing = sorted(set(cli_table["commands"]) - set(classes))
        extra = sorted(set(classes) - set(cli_table["commands"]))
        assert (missing, extra) == ([], [])

    def test_the_classes_are_the_ones_in_the_spec(self, cli_table):
        assert cli_table["classes"] == EXPECTED_CLASSES

    def test_every_command_that_must_not_write_is_run_below(self):
        never_write = {k for k, v in EXPECTED_CLASSES.items() if v in ("read", "output", "hook", "report")}
        assert never_write <= set(NO_WRITE_ARGV)
        assert {k for k, v in EXPECTED_CLASSES.items() if v == "write"} <= set(WRITE_ARGV)


# ── reads never write ─────────────────────────────────────────────────────────

class TestEmptyTree:
    @pytest.mark.parametrize("name", sorted(NO_WRITE_ARGV))
    def test_creates_nothing_in_the_tree_or_in_home(self, name, tmp_path, irp_seal):
        tree, cwd = _empty_tree(tmp_path)
        argv = [str(cwd / "snapshot.json") if a == "SNAPSHOT" else a for a in NO_WRITE_ARGV[name]]
        before = tree_state(tree, irp_seal.home)
        run_irp(argv, cwd, input=_STDIN.get(name, ""))
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []

    @pytest.mark.parametrize("name", READ_AND_OUTPUT + ["why --id"])
    def test_says_there_is_no_project_and_exits_1(self, name, tmp_path):
        tree, cwd = _empty_tree(tmp_path)
        argv = [str(cwd / "snapshot.json") if a == "SNAPSHOT" else a for a in NO_WRITE_ARGV[name]]
        result = run_irp(argv, cwd, input=_STDIN.get(name, ""))
        assert result.returncode == 1, (result.stdout, result.stderr)
        text = result.stdout + result.stderr
        if name == "gate":
            assert json.loads(result.stdout)["verdict"] == "no_project"
        elif name == "watch":
            assert json.loads(result.stdout)["status"] == "no_project"
        else:
            assert f"No IRP project found. Looked in {cwd} and the folders above it" in text
            assert "clear" not in text.lower()
        assert "No conflicts" not in text


class TestInsideAProject:
    @pytest.fixture
    def project(self, tmp_path):
        from irp.integrity.snapshot import create_snapshot

        proj = tmp_path.resolve() / "proj"
        irp_dir = start_project(proj)
        snap = create_snapshot(irp_dir)
        return proj, Path(snap["path"])

    @pytest.mark.parametrize("name", READ_AND_OUTPUT + ["why --id", "guard status", "doctor"])
    def test_leaves_the_project_as_it_was(self, name, project, irp_seal):
        proj, snapshot = project
        argv = [str(snapshot) if a == "SNAPSHOT" else a for a in NO_WRITE_ARGV[name]]
        before = tree_state(proj, irp_seal.home)
        run_irp(argv, proj, input=_STDIN.get(name, ""))
        allowed = {str(proj / OUTPUT_FILES[name])} if name in OUTPUT_FILES else set()
        diff = [line for line in state_diff(before, tree_state(proj, irp_seal.home))
                if line[2:] not in allowed]
        assert diff == []


# ── writes refuse with no project ─────────────────────────────────────────────

class TestWritesRefuse:
    @pytest.mark.parametrize("name", sorted(WRITE_ARGV))
    def test_writes_nothing_prints_the_init_line_and_exits_1(self, name, tmp_path, irp_seal):
        tree, cwd = _empty_tree(tmp_path)
        argv, stdin = WRITE_ARGV[name]
        (cwd / "NOTES.md").write_text("We decided to keep notes in plain text.\n", encoding="utf-8")
        before = tree_state(tree, irp_seal.home)
        result = run_irp(argv, cwd, input=stdin if stdin is not None else "")
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert f"irp init {cwd}" in result.stdout + result.stderr

    def test_capture_from_home_refuses_and_never_suggests_home(self, irp_seal):
        home = irp_seal.home
        before = tree_state(home)
        result = run_irp(["capture", "--stdin"], home, input='{"what": "x", "why": "y"}')
        assert result.returncode == 1
        assert not (home / ".irp").exists()
        assert state_diff(before, tree_state(home)) == []
        text = result.stdout + result.stderr
        assert "irp init PATH/TO/PROJECT" in text
        assert re.search(r"irp init " + re.escape(str(home)) + r"(\s|$)", text) is None


class TestJsonNoProject:
    @pytest.mark.parametrize("name", sorted(JSON_ARGV))
    def test_prints_the_no_project_json_and_exits_1(self, name, tmp_path, irp_seal):
        tree, cwd = _empty_tree(tmp_path)
        before = tree_state(tree, irp_seal.home)
        result = run_irp(JSON_ARGV[name] + ["--json"], cwd, input=_JSON_STDIN.get(name, ""))
        assert result.returncode == 1, (result.stdout, result.stderr)
        data = json.loads(result.stdout)
        assert data["status"] == "no_project"
        assert data["searched_from"] == str(cwd)
        assert data["stopped_at"] == "/"
        assert data["hint"] == f"irp init {cwd}"
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []

    def test_gate_keeps_its_own_shape(self, tmp_path):
        tree, cwd = _empty_tree(tmp_path)
        result = run_irp(["gate", "drop the database"], cwd)
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data == {"verdict": "no_project", "exit_code": 1, "searched_from": str(cwd),
                        "stopped_at": "/", "hint": f"irp init {cwd}"}

    def test_watch_prints_the_same_no_project_json_on_one_line(self, tmp_path, irp_seal):
        # watch has no --json flag: it always answers in JSON, one line per action.
        tree, cwd = _empty_tree(tmp_path)
        before = tree_state(tree, irp_seal.home)
        result = run_irp(["watch"], cwd, input="drop the database\n")
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []
        assert result.stdout.count("\n") == 1 and result.stdout.endswith("}\n"), result.stdout
        assert result.stderr == ""
        data = json.loads(result.stdout)
        other = json.loads(run_irp(["why", "--json"], cwd).stdout)
        assert list(data) == list(other)
        assert data == other == {"status": "no_project", "searched_from": str(cwd),
                                 "stopped_at": "/", "hint": f"irp init {cwd}"}
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []

    def test_watch_reading_a_file_prints_it_too(self, tmp_path, irp_seal):
        tree, cwd = _empty_tree(tmp_path)
        (cwd / "actions.txt").write_text("drop the database\n", encoding="utf-8")
        before = tree_state(tree, irp_seal.home)
        result = run_irp(["watch", "--input", "actions.txt"], cwd)
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert json.loads(result.stdout) == json.loads(run_irp(["check", "x", "--json"], cwd).stdout)
        assert state_diff(before, tree_state(tree, irp_seal.home)) == []


# ── checking a file someone sent you ──────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"
# The flag naming the file each verify command checks, and the name it's sent as.
SENT_FILE = {"integrity verify": ("--ledger", "ledger.jsonl"), "attest verify": ("--receipt", "receipt.tsr")}
VERIFY_COMMANDS = sorted(SENT_FILE)


def _make_sent_files(name: str, maker: Path, monkeypatch) -> tuple[Path, Path]:
    """Make a snapshot and the file it's checked against in the throwaway
    project `maker`, with IRP's own code. Returns (snapshot, file)."""
    irp_dir = start_project(maker)
    if name == "integrity verify":
        pytest.importorskip("rfc8785", reason="integrity extra not installed: pip install -e '.[dev]'")
        from irp.integrity.snapshot import create_snapshot

        return Path(create_snapshot(irp_dir)["path"]), irp_dir / "ledger.jsonl"

    pytest.importorskip("asn1crypto")
    pytest.importorskip("cryptography")
    import irp.integrity.attest as attest_mod

    # The fixture token is a real timestamp over a fixed digest, so the
    # snapshot is a stub carrying that digest and the answer is NOT
    # ESTABLISHED (the stub's manifest doesn't hash to it). What matters here
    # is getting the same answer with or without a project.
    token = (FIXTURES / "freetsa-token.tsr").read_bytes()
    digest = json.loads((FIXTURES / "freetsa-token.meta.json").read_text(encoding="utf-8"))["digest_hex"]
    snapshot = irp_dir / "integrity" / "snapshots" / "IRPS-2026-01-01-001.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({
        "snapshot_digest": {"alg": "sha-256", "value": digest},
        "manifest": {"schema": "irp-integrity-snapshot/0.1", "snapshot_id": "IRPS-2026-01-01-001"},
    }), encoding="utf-8")
    # The fixture token stands in for the timestamp authority, so nothing goes on the network.
    monkeypatch.setattr(attest_mod, "request_timestamp", lambda digest, url, timeout=20: token)
    made = attest_mod.create_attestation(irp_dir, snapshot, tsa_url="https://example.invalid/tsr")
    return snapshot, Path(made["token_path"])


class TestOfflineVerify:
    """Checking a snapshot and a ledger or receipt someone sent you, with no IRP
    project set up. Every run here starts in an empty tree."""

    @pytest.fixture
    def kit(self, request, tmp_path, monkeypatch):
        """The sent files copied out to an inbox of their own, the project they
        were made in, and a project of your own to compare answers with."""
        name = request.param
        base = tmp_path.resolve()
        snapshot, sent = _make_sent_files(name, base / "maker", monkeypatch)
        inbox = base / "inbox"
        inbox.mkdir()
        shutil.copy(snapshot, inbox / "snapshot.json")
        shutil.copy(sent, inbox / SENT_FILE[name][1])
        mine = base / "mine"
        start_project(mine)
        tree, cwd = _empty_tree(tmp_path)
        return SimpleNamespace(name=name, flag=SENT_FILE[name][0], file=SENT_FILE[name][1], base=base,
                               maker=base / "maker", snapshot=snapshot, sent=sent, inbox=inbox,
                               mine=mine, cwd=cwd)

    def _argv(self, kit, snapshot, sent, as_json=False):
        return [*kit.name.split(), str(snapshot), kit.flag, str(sent)] + (["--json"] if as_json else [])

    def _run_alone(self, kit, argv, cwd, irp_seal):
        """Run with no project and check it created nothing, anywhere."""
        before = tree_state(kit.base, irp_seal.base)
        result = run_irp(argv, cwd)
        assert state_diff(before, tree_state(kit.base, irp_seal.base)) == []
        return result

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_answers_as_it_would_inside_a_project(self, kit, as_json, irp_seal):
        argv = self._argv(kit, kit.inbox / "snapshot.json", kit.inbox / kit.file, as_json)
        alone = self._run_alone(kit, argv, kit.cwd, irp_seal)
        inside = run_irp(argv, kit.mine)
        assert ((alone.returncode, alone.stdout, alone.stderr)
                == (inside.returncode, inside.stdout, inside.stderr)), alone.stderr
        assert "No IRP project found" not in alone.stdout + alone.stderr
        if kit.name == "integrity verify":
            assert alone.returncode == 0
            if as_json:
                assert json.loads(alone.stdout)["status"] == "ok"
            else:
                assert "RESULT: PASS" in alone.stdout
        elif not as_json:
            assert alone.returncode == 10
            assert "token binds same digest    PASS" in alone.stdout
            assert "TSA signature valid        PASS" in alone.stdout
            assert "RESULT: NOT ESTABLISHED" in alone.stdout
        else:
            assert alone.returncode == 10
            data = json.loads(alone.stdout)
            assert data["status"] == "fail"
            assert data["result"]["token"]["imprint_ok"] is True
            assert isinstance(data["result"]["token"]["gen_time"], str)

    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_relative_paths_count_from_where_you_stand(self, kit, irp_seal):
        argv = self._argv(kit, "snapshot.json", kit.file)
        alone = self._run_alone(kit, argv, kit.inbox, irp_seal)
        shutil.copytree(kit.inbox, kit.mine / "inbox")
        inside = run_irp(argv, kit.mine / "inbox")
        assert (alone.returncode, alone.stdout) == (inside.returncode, inside.stdout), alone.stderr
        assert alone.returncode in (0, 10)

    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_a_file_inside_another_projects_folder(self, kit, irp_seal):
        argv = self._argv(kit, kit.snapshot, kit.sent)
        alone = self._run_alone(kit, argv, kit.cwd, irp_seal)
        inside = run_irp(argv, kit.maker)
        assert (alone.returncode, alone.stdout) == (inside.returncode, inside.stdout), alone.stderr
        assert alone.returncode in (0, 10)
        assert "No IRP project found" not in alone.stdout + alone.stderr

    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_a_missing_file_gets_the_same_answer_as_inside_a_project(self, kit, irp_seal):
        argv = self._argv(kit, kit.inbox / "snapshot.json", kit.inbox / ("missing-" + kit.file))
        alone = self._run_alone(kit, argv, kit.cwd, irp_seal)
        inside = run_irp(argv, kit.mine)
        assert (alone.returncode, alone.stdout) == (inside.returncode, inside.stdout), alone.stderr
        assert "No IRP project found" not in alone.stdout + alone.stderr
        # A wrong path says so, rather than reading like a ledger that was changed.
        assert alone.returncode == 10
        missing = kit.inbox / ("missing-" + kit.file)
        noun = "Ledger file" if kit.name == "integrity verify" else "Receipt token"
        assert alone.stdout.strip() == f"{noun} not found: {missing}"

    @pytest.mark.parametrize("named", ["flag", "variable"])
    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_a_named_project_isnt_needed_when_the_file_is_given(self, kit, named, irp_seal):
        nowhere = kit.base / "nowhere"
        argv = self._argv(kit, kit.inbox / "snapshot.json", kit.inbox / kit.file)
        before = tree_state(kit.base, irp_seal.base)
        if named == "flag":
            alone = run_irp(["--project", str(nowhere), *argv], kit.cwd)
        else:
            alone = run_irp(argv, kit.cwd, env=dict(os.environ, IRP_PROJECT_ROOT=str(nowhere)))
        assert state_diff(before, tree_state(kit.base, irp_seal.base)) == []
        inside = run_irp(argv, kit.mine)
        assert ((alone.returncode, alone.stdout, alone.stderr)
                == (inside.returncode, inside.stdout, inside.stderr)), alone.stderr
        assert not nowhere.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads folders whatever their permissions")
    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_a_named_folder_it_cant_read(self, kit, irp_seal):
        locked = kit.base / "locked"
        (locked / "inner").mkdir(parents=True)
        argv = self._argv(kit, kit.inbox / "snapshot.json", kit.inbox / kit.file)
        locked.chmod(0)
        try:
            alone = run_irp(["--project", str(locked / "inner"), *argv], kit.cwd)
        finally:
            locked.chmod(0o755)
        inside = run_irp(argv, kit.mine)
        assert (alone.returncode, alone.stdout) == (inside.returncode, inside.stdout), alone.stderr
        assert "IRP error" not in alone.stderr

    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_from_a_folder_that_was_removed(self, kit, irp_seal):
        gone = kit.base / "gone"
        gone.mkdir()
        argv = self._argv(kit, kit.inbox / "snapshot.json", kit.inbox / kit.file)
        alone = subprocess.run(
            ["sh", "-c", 'cd "$1" && rmdir "$1" && shift && exec "$@"', "sh", str(gone),
             sys.executable, str(REPO / "irp" / "core" / "irp.py"), *argv],
            capture_output=True, text=True, timeout=120)
        inside = run_irp(argv, kit.mine)
        assert (alone.returncode, alone.stdout) == (inside.returncode, inside.stdout), alone.stderr
        assert "IRP error" not in alone.stderr

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    @pytest.mark.parametrize("kit", VERIFY_COMMANDS, indirect=True)
    def test_without_the_file_it_still_needs_a_project(self, kit, as_json, irp_seal):
        argv = [*kit.name.split(), str(kit.inbox / "snapshot.json")] + (["--json"] if as_json else [])
        result = self._run_alone(kit, argv, kit.cwd, irp_seal)
        assert result.returncode == 1, (result.stdout, result.stderr)
        if as_json:
            assert json.loads(result.stdout) == {"status": "no_project", "searched_from": str(kit.cwd),
                                                 "stopped_at": "/", "hint": f"irp init {kit.cwd}"}
        else:
            assert f"No IRP project found. Looked in {kit.cwd} and the folders above it" in result.stderr


# ── the graph library cache ───────────────────────────────────────────────────

class TestGraphCache:
    LIB = "3d-force-graph-v1.min.js"

    def _new_cache(self, home: Path) -> Path:
        return home / ".cache" / "irp" / "vendor" / self.LIB

    # The export reports where the library came from on its "Render:" line:
    # "inlined" when it read a cached copy, "kept CDN link" when it found none.

    def test_export_graph_reads_the_cache_under_dot_cache(self, tmp_path, irp_seal):
        assert self._new_cache(irp_seal.home).exists()   # the seal pre-seeds it
        proj = tmp_path.resolve() / "proj"
        start_project(proj)
        before = tree_state(irp_seal.home)
        result = run_irp(["export", "graph"], proj)
        assert result.returncode == 0, result.stderr
        assert "Render: 3d-force-graph inlined" in result.stdout
        assert state_diff(before, tree_state(irp_seal.home)) == []

    def test_export_graph_never_makes_dot_irp_in_home(self, tmp_path, irp_seal):
        self._new_cache(irp_seal.home).unlink()      # nothing cached, and no network
        proj = tmp_path.resolve() / "proj"
        start_project(proj)
        before = tree_state(irp_seal.home)
        run_irp(["export", "graph"], proj)
        assert not (irp_seal.home / ".irp").exists()
        new = [line for line in state_diff(before, tree_state(irp_seal.home))
               if not line[2:].startswith(str(irp_seal.home / ".cache" / "irp"))]
        assert new == []

    @pytest.mark.skipif(os.environ.get("IRP_TESTS_ALLOW_NETWORK") == "1", reason="needs the network blocked")
    def test_export_graph_offline_makes_no_cache_folder(self, tmp_path, irp_seal):
        shutil.rmtree(irp_seal.home / ".cache" / "irp")     # nothing cached, and no network
        proj = tmp_path.resolve() / "proj"
        start_project(proj)
        before = tree_state(irp_seal.home)
        result = run_irp(["export", "graph"], proj)
        assert result.returncode == 0, result.stderr
        assert state_diff(before, tree_state(irp_seal.home)) == []

    def test_export_graph_reads_the_old_place_and_never_writes_there(self, tmp_path, irp_seal):
        self._new_cache(irp_seal.home).unlink()
        old = irp_seal.home / ".irp" / "vendor" / self.LIB
        old.parent.mkdir(parents=True)
        old.write_text("/* marker-old-cache */", encoding="utf-8")
        proj = tmp_path.resolve() / "proj"
        start_project(proj)
        before = tree_state(irp_seal.home)
        result = run_irp(["export", "graph"], proj)
        assert "Render: 3d-force-graph inlined" in result.stdout
        assert state_diff(before, tree_state(irp_seal.home)) == []


# ── the source check ──────────────────────────────────────────────────────────

_ALLOWED = {"irp/core/store.py", "irp/core/commands/init.py"}
# Starts a throwaway project in a temp folder of its own.
_MAY_START_A_PROJECT = _ALLOWED | {"eval/inherit-vs-agents/ivsa.py"}


def _python_sources():
    for top in ("irp", "eval"):
        for path in sorted((REPO / top).rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            yield path


def _is_irp_literal(node) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.search(r"(^|/)\.irp(/|$)", node.value) is not None)


def _mentions(node, names: set[str], funcs: set[str]) -> bool:
    for sub in ast.walk(node):
        if _is_irp_literal(sub):
            return True
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
        if isinstance(sub, ast.Call):
            f = sub.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if fname in funcs:
                return True
    return False


def _irp_mkdirs(tree: ast.AST) -> list[int]:
    """Lines that make a folder whose path comes from a '.irp' literal."""
    names: set[str] = set()
    funcs: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in funcs:
                if any(isinstance(r, ast.Return) and r.value is not None and _mentions(r.value, names, funcs)
                       for r in ast.walk(node)):
                    funcs.add(node.name)
                    changed = True
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if _mentions(node.value, names, funcs):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        n = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
                        if n and n not in names:
                            names.add(n)
                            changed = True
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "mkdir" and _mentions(f.value, names, funcs):
            lines.append(node.lineno)
        elif (isinstance(f, ast.Attribute) and f.attr in ("makedirs", "mkdir")
              and isinstance(f.value, ast.Name) and f.value.id == "os"
              and node.args and _mentions(node.args[0], names, funcs)):
            lines.append(node.lineno)
    return lines


def _ledger_parent_mkdirs(tree: ast.AST) -> list[int]:
    """Lines that make the folder holding a ledger: `<...ledger...>.parent.mkdir(...)`."""
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mkdir"):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Attribute) and target.attr == "parent"):
            continue
        names = [n.id if isinstance(n, ast.Name) else n.attr
                 for n in ast.walk(target.value) if isinstance(n, (ast.Name, ast.Attribute))]
        if any("ledger" in name.lower() for name in names):
            lines.append(node.lineno)
    return sorted(lines)


class TestSourceCheck:
    def test_nothing_calls_ensure_irp_dir(self):
        hits = []
        for path in _python_sources():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bensure_irp_dir\b", line):
                    hits.append(f"{path.relative_to(REPO)}:{lineno}")
        assert hits == []

    def test_nothing_outside_init_and_store_makes_dot_irp(self):
        hits = []
        for path in _python_sources():
            rel = path.relative_to(REPO).as_posix()
            if rel in _ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            hits += [f"{rel}:{n}" for n in _irp_mkdirs(tree)]
        assert hits == []

    def test_nothing_else_starts_a_project(self):
        hits = []
        for path in _python_sources():
            rel = path.relative_to(REPO).as_posix()
            if rel in _MAY_START_A_PROJECT:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                    if name == "init_project":
                        hits.append(f"{rel}:{node.lineno}")
        assert hits == []

    def test_nothing_makes_the_folder_a_ledger_path_names(self):
        hits = []
        for path in _python_sources():
            rel = path.relative_to(REPO).as_posix()
            if rel in _ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            hits += [f"{rel}:{n}" for n in _ledger_parent_mkdirs(tree)]
        assert hits == []

    def test_the_ledger_check_catches_what_it_is_for(self):
        caught = _ledger_parent_mkdirs(ast.parse(
            "self.ledger_path.parent.mkdir(parents=True)\n"       # line 1
            "LEDGER.parent.mkdir()\n"                              # line 2
            "out.parent.mkdir()\n"
        ))
        assert caught == [1, 2]

    def test_the_check_catches_the_patterns_it_is_for(self):
        caught = _irp_mkdirs(ast.parse(
            "from pathlib import Path\n"
            "import os\n"
            "ROOT = Path('.')\n"
            "LEDGER = ROOT / '.irp' / 'ledger.jsonl'\n"
            "LEDGER.parent.mkdir(parents=True)\n"           # line 5
            "def cache():\n"
            "    return Path.home() / '.irp' / 'vendor'\n"
            "cache().mkdir(parents=True)\n"                 # line 8
            "os.makedirs(os.path.join('x', '.irp'))\n"      # line 9
            "(ROOT / 'out').mkdir()\n"
        ))
        assert caught == [5, 8, 9]
