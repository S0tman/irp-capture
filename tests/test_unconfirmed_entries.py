"""Legacy bootstrap guesses in a ledger are never treated as decisions.

Before reconstructions.jsonl existed, `irp bootstrap` appended guesses straight
to ledger.jsonl with `bootstrapped: true`. Those lines cannot be removed (the
ledger is append-only), so every reader has to cope with them:

  * anything that speaks to an agent or an auditor, or enforces decisions,
    skips them;
  * human views (why, find, doctor) may show them, but label them
    "unconfirmed (bootstrap guess)";
  * ids still see every line, so a new capture never reuses a guess's number.

The fixture below is a ledger written by the old bootstrap: one real decision,
one legacy guess, and a current.json that (as the old rebuild_current did)
contains both.
"""
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import types
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
import irp  # noqa: E402,F401

import store  # noqa: E402
from store import ensure_irp_dir, next_irp_id, read_ledger, rebuild_current, write_current  # noqa: E402
from commands.check import run_check  # noqa: E402
from commands.doctor import run_doctor  # noqa: E402
from commands.evidence import run_export_evidence, _build_evidence_md, _EUAIACT_FRAMEWORK  # noqa: E402
from commands.export import run_export  # noqa: E402
from commands.find import run_find  # noqa: E402
from commands.gate import run_gate  # noqa: E402
from commands.guard import run_guard  # noqa: E402
from commands.inherit import run_inherit  # noqa: E402
from commands.mod import run_mod  # noqa: E402
from commands.resolve import run_resolve  # noqa: E402
from commands.stats import run_stats  # noqa: E402
from commands.watch import run_watch  # noqa: E402
from commands.why import run_why  # noqa: E402
from commands.defer import run_defer  # noqa: E402
import commands.find as find_mod  # noqa: E402
from commands.capture import _milestone_lines  # noqa: E402
from irp.core.compact import compact_entry  # noqa: E402

LABEL = "unconfirmed (bootstrap guess)"
TODAY = date.today().isoformat()

CONFIRMED = {
    "type": "decision", "id": "IRP-2026-04-01-001",
    "what": "Use PostgreSQL for the primary database",
    "why": "Relational model fits our schema and the joins we need.",
    "confidence": "high", "tags": ["backend"], "timestamp": "2026-04-01", "source": "cli",
}
GUESS = {
    "type": "decision", "id": "IRP-2026-04-02-001",
    "what": "Adopt zeppelin airship delivery for hardware shipping",
    "why": "Derived from git commit message. Original reasoning not captured in commit.",
    "confidence": "low", "tags": ["bootstrap", "git"], "timestamp": "2026-04-02",
    "source": "bootstrap", "origin_mode": "bootstrap_git", "source_ref": "abc123",
    "bootstrapped": True,
}


class _Args:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _write_ledger(irp_dir, entries):
    (irp_dir / "ledger.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def _legacy_project(tmp_path, entries=None):
    """A project whose ledger and current.json were written by the old bootstrap."""
    irp_dir = ensure_irp_dir(tmp_path)
    entries = [CONFIRMED, GUESS] if entries is None else entries
    _write_ledger(irp_dir, entries)
    write_current(irp_dir, {"version": 1, "active": [e for e in entries if e.get("type") == "decision"]})
    return irp_dir


# ── the store helpers ──────────────────────────────────────────────────────────

class TestStoreHelpers:
    def test_is_unconfirmed_only_for_bootstrapped_true(self):
        assert store.is_unconfirmed({"bootstrapped": True}) is True
        assert store.is_unconfirmed({"bootstrapped": False}) is False
        assert store.is_unconfirmed({"bootstrapped": "true"}) is False
        assert store.is_unconfirmed({}) is False
        assert store.is_unconfirmed(CONFIRMED) is False
        assert store.is_unconfirmed(GUESS) is True

    def test_confirmed_only_keeps_order_and_drops_guesses(self):
        rows = [CONFIRMED, GUESS, {"id": "x", "type": "retirement"}]
        assert store.confirmed_only(rows) == [CONFIRMED, {"id": "x", "type": "retirement"}]

    def test_read_ledger_still_returns_every_line(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        assert [e["id"] for e in read_ledger(irp_dir)] == [CONFIRMED["id"], GUESS["id"]]

    def test_rebuild_current_excludes_guesses(self):
        current = rebuild_current([CONFIRMED, GUESS])
        assert [e["id"] for e in current["active"]] == [CONFIRMED["id"]]

    def test_rebuild_current_keeps_the_last_ten_confirmed(self):
        confirmed = [
            dict(CONFIRMED, id=f"IRP-2026-05-01-{i:03d}", what=f"Decision number {i}")
            for i in range(1, 12)
        ]
        current = rebuild_current(confirmed + [GUESS])
        assert [e["id"] for e in current["active"]] == [c["id"] for c in confirmed[-10:]]

    def test_next_irp_id_still_counts_a_guess_id(self):
        guess_today = dict(GUESS, id=f"IRP-{TODAY}-003")
        assert next_irp_id([guess_today]) == f"IRP-{TODAY}-004"


# ── agent-facing and enforcing readers skip guesses ────────────────────────────

class TestAgentFacingReaders:
    def test_inherit_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert [e["id"] for e in result["active"]] == [CONFIRMED["id"]]
        assert result["active_count"] == 1
        assert "zeppelin" not in result["text"]

    def test_inherit_with_only_guesses_reports_no_context(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [GUESS])
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert result["active"] == []
        assert "No active IRP context found" in result["text"]

    def test_inherit_current_json_fallback_also_drops_guesses(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)  # empty ledger, so inherit falls back to current.json
        write_current(irp_dir, {"version": 1, "active": [CONFIRMED, GUESS]})
        result = run_inherit(tmp_path, irp_dir, _Args())
        assert [e["id"] for e in result["active"]] == [CONFIRMED["id"]]

    def test_gate_does_not_match_a_guess(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_gate(tmp_path, irp_dir, _Args(query="zeppelin airship delivery hardware shipping"))
        assert result["verdict"] == "clear"
        assert result["top_match"] is None
        assert result["active_count"] == 1

    def test_gate_still_matches_the_real_decision(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_gate(tmp_path, irp_dir, _Args(query="replace PostgreSQL primary database"))
        assert result["verdict"] in ("warn", "block")
        assert result["top_match"]["id"] == CONFIRMED["id"]

    def test_check_does_not_match_a_guess(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_check(tmp_path, irp_dir, _Args(proposal="zeppelin airship delivery hardware shipping"))
        assert result["status"] == "clear"
        assert result["checked"] == 1

    def test_resolve_does_not_match_a_guess(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_resolve(tmp_path, irp_dir, _Args(
            query="zeppelin airship delivery hardware shipping", tag=None, scope=None, top=3))
        assert result["verdict"] == "clear"
        assert result["active_count"] == 1
        assert "zeppelin" not in result["text"].split("Query:")[1].split("\n", 1)[1]

    def test_watch_does_not_match_a_guess(self, tmp_path, capsys):
        irp_dir = _legacy_project(tmp_path)
        actions = tmp_path / "actions.txt"
        actions.write_text("zeppelin airship delivery hardware shipping\n", encoding="utf-8")
        run_watch(tmp_path, irp_dir, _Args(input=str(actions), tag=None, scope=None, strict=False))
        line = json.loads(capsys.readouterr().out.strip().splitlines()[0])
        assert line["verdict"] == "clear"
        assert line["top_match"] is None

    def test_guard_ignores_a_guess_still_sitting_in_current_json(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        irp_dir = _legacy_project(tmp_path, [GUESS])
        (tmp_path / "ship.txt").write_text(
            "zeppelin airship delivery hardware shipping\n", encoding="utf-8")
        subprocess.run(["git", "add", "ship.txt"], cwd=tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["status"] == "clear"
        assert result.get("match_id") is None

    def test_guard_still_catches_the_real_decision(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        irp_dir = _legacy_project(tmp_path)
        (tmp_path / "db.txt").write_text(
            "replace PostgreSQL primary database relational schema joins\n", encoding="utf-8")
        subprocess.run(["git", "add", "db.txt"], cwd=tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["match_id"] == CONFIRMED["id"]


class TestExports:
    def test_agents_md_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="agents.md", output=None, force=False, writable=False))
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "PostgreSQL" in body
        assert "zeppelin" not in body
        assert result["decision_count"] == 1
        assert result["unconfirmed_excluded"] == 1
        assert "unconfirmed" in result["text"].lower()

    def test_claude_md_target_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="claude.md", output=None, force=False, writable=False))
        assert "zeppelin" not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    def test_decisions_md_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_export(tmp_path, irp_dir, _Args(
            export_action="decisions", output=None, force=False, writable=False, demo=False))
        body = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
        assert "PostgreSQL" in body
        assert "zeppelin" not in body
        assert result["decision_count"] == 1
        assert result["unconfirmed_excluded"] == 1

    def test_context_decisions_md_target_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="decisions.md", output=None, force=False, writable=False))
        assert "zeppelin" not in (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")

    def test_no_note_when_nothing_was_excluded(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED])
        result = run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="agents.md", output=None, force=False, writable=False))
        assert result["unconfirmed_excluded"] == 0
        assert "unconfirmed" not in result["text"].lower()

    def test_graph_leaves_guesses_out(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        out = tmp_path / "graph.html"
        result = run_export(tmp_path, irp_dir, _Args(
            export_action="graph", output=str(out), force=True))
        assert result["decision_count"] == 1
        assert "zeppelin" not in out.read_text(encoding="utf-8")
        assert result["unconfirmed_excluded"] == 1
        assert "unconfirmed" in result["text"].lower()


def _evidence_args(out):
    return argparse.Namespace(
        demo=False, output=str(out), force=True, json=False,
        framework="euaiact", config=None, attest=False, tsa_url=None,
    )


class TestEvidence:
    def test_package_leaves_guesses_out_and_says_how_many_and_why(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        out = tmp_path / "EVIDENCE.md"
        result = run_export_evidence(tmp_path, irp_dir, _evidence_args(out))
        body = out.read_text(encoding="utf-8")
        assert "zeppelin" not in body
        assert "PostgreSQL" in body
        assert "| Total decisions | 1 |" in body
        assert "Left out: 1 ledger entry" in body
        assert "bootstrap" in body.split("Left out:")[1].split("\n")[0]
        assert "Left out: 1" in result["text"]

    def test_the_note_uses_the_plural_for_several(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [
            CONFIRMED, GUESS, dict(GUESS, id="IRP-2026-04-03-001", what="Another guessed thing here"),
        ])
        out = tmp_path / "EVIDENCE.md"
        run_export_evidence(tmp_path, irp_dir, _evidence_args(out))
        assert "Left out: 2 ledger entries" in out.read_text(encoding="utf-8")

    def test_no_note_when_nothing_was_excluded(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED])
        out = tmp_path / "EVIDENCE.md"
        result = run_export_evidence(tmp_path, irp_dir, _evidence_args(out))
        assert "Left out" not in out.read_text(encoding="utf-8")
        assert "Left out" not in result["text"]

    def test_a_ledger_of_only_guesses_is_treated_as_empty(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [GUESS])
        out = tmp_path / "EVIDENCE.md"
        result = run_export_evidence(tmp_path, irp_dir, _evidence_args(out))
        assert result["status"] == "empty"
        assert not out.exists()

    def test_an_attested_package_says_the_timestamp_covers_the_whole_ledger_file(self):
        attestation = {
            "snapshot_id": "snap", "gen_time": "2026-10-08T00:00:00Z", "tsa_url": "https://tsa.invalid",
            "accuracy_seconds": 1, "entry_count": 2,
        }
        body = _build_evidence_md(
            [CONFIRMED], _EUAIACT_FRAMEWORK, Path("/p"), attestation=attestation, unconfirmed_excluded=1)
        assert "Left out: 1 ledger entry" in body
        assert "whole ledger file" in body


# ── human views show guesses, labelled ─────────────────────────────────────────

class TestHumanViews:
    def test_why_by_id_labels_a_guess(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=GUESS["id"]))
        assert result["status"] == "unconfirmed"
        assert result["unconfirmed"] is True
        assert LABEL in result["text"]
        assert result["entry"]["id"] == GUESS["id"]

    def test_why_by_id_does_not_label_a_real_decision(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=CONFIRMED["id"]))
        assert LABEL not in result["text"]
        assert result["status"] == "ok"
        assert "unconfirmed" not in result

    def test_why_for_agents_treats_a_guess_as_not_found(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=GUESS["id"], confirmed_only=True))
        assert result["status"] == "not_found"
        assert "zeppelin" not in result["text"]
        assert "entry" not in result

    def test_why_latest_skips_a_guess_in_a_stale_current_json(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)  # current.json ends with the guess
        result = run_why(tmp_path, irp_dir, _Args(id=None))
        assert result["latest"]["id"] == CONFIRMED["id"]
        assert result["active_count"] == 1

    def test_find_labels_a_guess(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_find(tmp_path, irp_dir, _Args(query="zeppelin", craft_only=False, ledger_only=False, graph=False))
        assert result["count"] == 1
        assert LABEL in result["text"]
        assert result["results"][0]["unconfirmed"] is True

    def test_find_does_not_label_a_real_decision(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_find(tmp_path, irp_dir, _Args(query="PostgreSQL", craft_only=False, ledger_only=False, graph=False))
        assert LABEL not in result["text"]
        assert result["results"][0]["unconfirmed"] is False

    def test_stats_does_not_count_a_guess_and_says_so(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=False))
        assert result["stats"]["total"] == 1
        assert result["stats"]["unconfirmed_not_counted"] == 1
        assert LABEL in result["text"]

    def test_stats_with_only_guesses_is_empty_but_mentions_them(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [GUESS])
        result = run_stats(tmp_path, irp_dir, _Args(demo=False, json=False))
        assert result["status"] == "empty"
        assert LABEL in result["text"]

    def test_doctor_reports_how_many_and_how_to_deal_with_them(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["unconfirmed_count"] == 1
        text = result["text"]
        assert "unconfirmed" in text
        assert GUESS["id"] in text
        assert "irp mod retire" in text
        assert "irp mod supersede" in text

    def test_doctor_is_quiet_when_there_are_none(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED])
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["unconfirmed_count"] == 0
        assert "unconfirmed" not in result["text"]

    def test_doctor_stops_counting_a_guess_once_it_is_retired(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        out = run_mod(tmp_path, irp_dir, _Args(
            mod_action="retire", target_id=GUESS["id"], reason="bootstrap guess, not a decision"))
        assert out["retired_id"] == GUESS["id"]
        assert run_doctor(tmp_path, irp_dir, _Args())["unconfirmed_count"] == 0

    def test_doctor_stops_counting_a_guess_once_it_is_superseded(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        run_mod(tmp_path, irp_dir, _Args(
            mod_action="supersede", target_id=GUESS["id"],
            decision="We ship hardware by courier", reason="Confirmed by the team", confidence="high"))
        assert run_doctor(tmp_path, irp_dir, _Args())["unconfirmed_count"] == 0

    def test_doctor_counts_reconstructions_waiting_for_review(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        rows = [
            {"id": "REC-2026-10-08-001", "what": "a", "status": "unconfirmed"},
            {"id": "REC-2026-10-08-002", "what": "b", "status": "accepted"},
            {"id": "REC-2026-10-08-003", "what": "c", "status": "unconfirmed"},
        ]
        (irp_dir / "reconstructions.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["reconstructions_pending"] == 2
        assert "--accept" in result["text"]

    def test_mod_can_still_retire_a_guess(self, tmp_path):
        """Retirement is how a person says 'that was never a decision'."""
        irp_dir = _legacy_project(tmp_path)
        run_mod(tmp_path, irp_dir, _Args(mod_action="retire", target_id=GUESS["id"], reason="not real"))
        assert read_ledger(irp_dir)[-1]["type"] == "retirement"
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in current["active"]] == [CONFIRMED["id"]]


# ── MCP server, REST API and tools/collab.py ───────────────────────────────────
#
# Neither `mcp` nor `fastapi` is installed in the dev environment (see
# test_entrypoints_smoke.py), so these tests give the servers minimal stand-ins
# for the framework objects and call the real tool and route functions.

def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _import_with_stubs(monkeypatch, name, stubs):
    for mod_name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, mod_name, mod)
    saved = sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = importlib.import_module(parent_name)
    saved_attr = getattr(parent, attr, None)
    module = importlib.import_module(name)

    def restore():
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved
        if saved_attr is not None:
            setattr(parent, attr, saved_attr)
        elif hasattr(parent, attr):
            delattr(parent, attr)

    return module, restore


@pytest.fixture
def mcp_server(monkeypatch, tmp_path):
    class FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self):
            return lambda fn: fn

        def run(self):
            pass

    stubs = {
        "mcp": _stub_module("mcp"),
        "mcp.server": _stub_module("mcp.server"),
        "mcp.server.fastmcp": _stub_module("mcp.server.fastmcp", FastMCP=FastMCP),
    }
    module, restore = _import_with_stubs(monkeypatch, "irp.mcp.server", stubs)
    monkeypatch.setenv("IRP_PROJECT_ROOT", str(tmp_path))
    try:
        yield module
    finally:
        restore()


@pytest.fixture
def api_server(monkeypatch, tmp_path):
    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class FastAPI:
        def __init__(self, *a, **k):
            self.title = k.get("title")

        def add_middleware(self, *a, **k):
            pass

        def _route(self, *a, **k):
            return lambda fn: fn

        get = post = _route

    stubs = {
        "fastapi": _stub_module("fastapi", FastAPI=FastAPI, HTTPException=HTTPException),
        "fastapi.middleware": _stub_module("fastapi.middleware"),
        "fastapi.middleware.cors": _stub_module("fastapi.middleware.cors", CORSMiddleware=object),
        "pydantic": _stub_module("pydantic", BaseModel=object),
        "uvicorn": _stub_module("uvicorn"),
    }
    monkeypatch.setattr(sys, "argv", ["irp-api", "--project-root", str(tmp_path)])
    module, restore = _import_with_stubs(monkeypatch, "irp.api.server", stubs)
    try:
        yield module
    finally:
        restore()


class TestMcpServer:
    def test_irp_inherit_leaves_guesses_out(self, mcp_server, tmp_path):
        _legacy_project(tmp_path)
        for full in (False, True):
            result = mcp_server.irp_inherit(full=full)
            assert [e["id"] for e in result["active"]] == [CONFIRMED["id"]]
            assert "zeppelin" not in json.dumps(result)

    def test_irp_why_treats_a_guess_as_not_found(self, mcp_server, tmp_path):
        _legacy_project(tmp_path)
        result = mcp_server.irp_why(id=GUESS["id"])
        assert result["status"] == "not_found"
        assert result["entry"] is None
        assert "zeppelin" not in json.dumps(result)

    def test_irp_why_still_finds_a_real_decision(self, mcp_server, tmp_path):
        _legacy_project(tmp_path)
        assert mcp_server.irp_why(id=CONFIRMED["id"])["entry"]["id"] == CONFIRMED["id"]

    def test_irp_why_latest_is_the_last_confirmed_decision(self, mcp_server, tmp_path):
        _legacy_project(tmp_path)
        assert mcp_server.irp_why()["latest"]["id"] == CONFIRMED["id"]

    def test_irp_check_does_not_match_a_guess(self, mcp_server, tmp_path):
        _legacy_project(tmp_path)
        result = mcp_server.irp_check("zeppelin airship delivery hardware shipping")
        assert result["status"] == "clear"
        assert result["checked"] == 1

    def test_irp_capture_does_not_reuse_a_guess_id(self, mcp_server, tmp_path):
        guess_today = dict(GUESS, id=f"IRP-{TODAY}-001")
        irp_dir = _legacy_project(tmp_path, [guess_today])
        result = mcp_server.irp_capture(what="Ship by courier", why="It is cheaper")
        assert result["id"] == f"IRP-{TODAY}-002"
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in current["active"]] == [f"IRP-{TODAY}-002"]


class TestRestApi:
    def test_decisions_endpoint_leaves_guesses_out(self, api_server, tmp_path):
        _legacy_project(tmp_path)
        result = api_server.get_decisions()
        assert [d["id"] for d in result["decisions"]] == [CONFIRMED["id"]]
        assert result["count"] == 1

    def test_single_decision_endpoint_does_not_serve_a_guess(self, api_server, tmp_path):
        _legacy_project(tmp_path)
        with pytest.raises(Exception) as exc:
            api_server.get_decision(GUESS["id"])
        assert getattr(exc.value, "status_code", None) == 404
        assert "zeppelin" not in str(getattr(exc.value, "detail", ""))

    def test_single_decision_endpoint_still_serves_a_real_decision(self, api_server, tmp_path):
        _legacy_project(tmp_path)
        assert api_server.get_decision(CONFIRMED["id"])["id"] == CONFIRMED["id"]

    def test_check_endpoint_does_not_match_a_guess(self, api_server, tmp_path):
        _legacy_project(tmp_path)
        req = types.SimpleNamespace(proposal="zeppelin airship delivery hardware shipping")
        assert api_server.check(req)["status"] == "clear"


class TestCollab:
    def test_collab_context_skips_guesses_in_current_json(self, tmp_path):
        import collab
        _legacy_project(tmp_path)
        ctx = collab.read_irp_context(str(tmp_path))
        assert "PostgreSQL" in ctx
        assert "zeppelin" not in ctx

    def test_collab_context_skips_guesses_in_the_ledger_fallback(self, tmp_path):
        import collab
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        _write_ledger(irp_dir, [CONFIRMED, GUESS])
        ctx = collab.read_irp_context(str(tmp_path))
        assert "PostgreSQL" in ctx
        assert "zeppelin" not in ctx


# ── review follow-ups ──────────────────────────────────────────────────────────

# Up to v0.7.0 a bootstrap guess and a same-day capture could be given the same
# IRP id. A guess first, then the real decision, under one id.
SHARED = dict(GUESS, id=CONFIRMED["id"])


def _shared_project(tmp_path):
    return _legacy_project(tmp_path, [SHARED, CONFIRMED])


class TestSharedIds:
    def test_store_finds_ids_shared_by_a_guess_and_a_confirmed_decision(self):
        retirement = {"type": "retirement", "id": GUESS["id"], "reason": "r"}
        # A retirement event shares its target's id by design. That is not a collision.
        assert store.shared_unconfirmed_ids([SHARED, CONFIRMED]) == {CONFIRMED["id"]}
        assert store.shared_unconfirmed_ids([GUESS, retirement]) == set()
        assert store.shared_unconfirmed_ids([CONFIRMED, GUESS]) == set()

    def test_doctor_lists_a_shared_id_once_and_does_not_say_to_retire_it(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        text = result["text"]
        assert result["shared_ids"] == [CONFIRMED["id"]]
        assert text.count(CONFIRMED["id"]) == 1
        assert "irp mod retire" not in text
        assert "irp mod supersede" not in text
        assert "both" in text and "confirmed decision" in text
        assert "ignore" in text
        # The way forward is named, and it says what it changes.
        assert "--shared-id-ok" in text
        assert "changes the confirmed decision" in text

    def test_doctor_keeps_the_retire_advice_for_guesses_with_their_own_id(self, tmp_path):
        other = dict(GUESS, id="IRP-2026-04-05-001", what="Another guess about hosting")
        irp_dir = _legacy_project(tmp_path, [SHARED, CONFIRMED, other])
        result = run_doctor(tmp_path, irp_dir, _Args())
        text = result["text"]
        assert result["shared_ids"] == [CONFIRMED["id"]]
        assert result["unconfirmed_ids"] == [other["id"]]
        assert result["unconfirmed_count"] == 2
        assert text.count(CONFIRMED["id"]) == 1
        assert text.count(other["id"]) == 1
        assert "irp mod retire" in text

    def test_mod_retire_refuses_a_shared_id(self, tmp_path, capsys):
        irp_dir = _shared_project(tmp_path)
        before = (irp_dir / "ledger.jsonl").read_bytes()
        with pytest.raises(SystemExit) as exc:
            run_mod(tmp_path, irp_dir, _Args(mod_action="retire", target_id=CONFIRMED["id"], reason="x"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "both a bootstrap guess and a confirmed decision" in err
        assert "--shared-id-ok" in err
        assert (irp_dir / "ledger.jsonl").read_bytes() == before

    def test_mod_supersede_refuses_a_shared_id(self, tmp_path, capsys):
        irp_dir = _shared_project(tmp_path)
        before = (irp_dir / "ledger.jsonl").read_bytes()
        with pytest.raises(SystemExit) as exc:
            run_mod(tmp_path, irp_dir, _Args(
                mod_action="supersede", target_id=CONFIRMED["id"], decision="d", reason="r"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "both a bootstrap guess and a confirmed decision" in err
        assert "--shared-id-ok" in err
        assert (irp_dir / "ledger.jsonl").read_bytes() == before

    def test_retire_with_the_flag_acts_on_the_confirmed_decision(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        assert [e["id"] for e in run_inherit(tmp_path, irp_dir, _Args())["active"]] == [CONFIRMED["id"]]
        out = run_mod(tmp_path, irp_dir, _Args(
            mod_action="retire", target_id=CONFIRMED["id"], reason="no longer true", shared_id_ok=True))
        assert out["retired_id"] == CONFIRMED["id"]
        assert read_ledger(irp_dir)[-1]["type"] == "retirement"
        assert run_inherit(tmp_path, irp_dir, _Args())["active"] == []

    def test_supersede_with_the_flag_acts_on_the_confirmed_decision(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        out = run_mod(tmp_path, irp_dir, _Args(
            mod_action="supersede", target_id=CONFIRMED["id"], decision="Use MySQL instead",
            reason="Licence change", confidence="high", shared_id_ok=True))
        active = run_inherit(tmp_path, irp_dir, _Args())["active"]
        assert [e["id"] for e in active] == [out["new_id"]]
        assert active[0]["what"] == "Use MySQL instead"

    def test_the_flag_is_wired_into_the_command_line(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        irp_py = str(REPO / "irp" / "core" / "irp.py")
        refused = subprocess.run(
            [sys.executable, irp_py, "mod", "retire", CONFIRMED["id"], "--reason", "x"],
            cwd=tmp_path, capture_output=True, text=True)
        assert refused.returncode == 1
        assert "--shared-id-ok" in refused.stderr
        allowed = subprocess.run(
            [sys.executable, irp_py, "mod", "retire", CONFIRMED["id"], "--reason", "x", "--shared-id-ok"],
            cwd=tmp_path, capture_output=True, text=True)
        assert allowed.returncode == 0
        assert read_ledger(irp_dir)[-1]["type"] == "retirement"
        sup = subprocess.run(
            [sys.executable, irp_py, "mod", "supersede", CONFIRMED["id"], "--decision", "d",
             "--reason", "r", "--shared-id-ok"], cwd=tmp_path, capture_output=True, text=True)
        assert sup.returncode == 0

    def test_mod_still_works_for_an_ordinary_id(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        out = run_mod(tmp_path, irp_dir, _Args(mod_action="retire", target_id=CONFIRMED["id"], reason="x"))
        assert out["retired_id"] == CONFIRMED["id"]

    def test_why_by_id_prefers_the_confirmed_entry(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=CONFIRMED["id"]))
        assert result["status"] == "ok"
        assert result["entry"]["what"] == CONFIRMED["what"]
        assert LABEL not in result["text"]

    def test_why_by_id_for_agents_prefers_the_confirmed_entry(self, tmp_path):
        irp_dir = _shared_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=CONFIRMED["id"], confirmed_only=True))
        assert result["entry"]["what"] == CONFIRMED["what"]

    def test_why_falls_back_to_the_guess_only_when_nothing_else_matches(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [SHARED])
        result = run_why(tmp_path, irp_dir, _Args(id=SHARED["id"]))
        assert result["status"] == "unconfirmed"

    def test_api_serves_the_confirmed_entry_for_a_shared_id(self, api_server, tmp_path):
        _shared_project(tmp_path)
        assert api_server.get_decision(CONFIRMED["id"])["what"] == CONFIRMED["what"]


class TestSupersedeWritesWhatAndWhy:
    """Every agent-facing reader reads what/why, so supersede has to write them."""

    NEW_WHAT = "We ship hardware by courier"
    NEW_WHY = "The team confirmed the airship idea was never real"

    def _supersede(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        out = run_mod(tmp_path, irp_dir, _Args(
            mod_action="supersede", target_id=GUESS["id"],
            decision=self.NEW_WHAT, reason=self.NEW_WHY, confidence="high"))
        return irp_dir, out["new_id"]

    def test_the_new_entry_has_what_and_why_and_keeps_decision_and_reasoning(self, tmp_path):
        irp_dir, new_id = self._supersede(tmp_path)
        entry = next(e for e in read_ledger(irp_dir) if e["id"] == new_id)
        assert entry["what"] == self.NEW_WHAT and entry["why"] == self.NEW_WHY
        assert entry["decision"] == self.NEW_WHAT and entry["reasoning"] == self.NEW_WHY
        assert entry["supersedes"] == GUESS["id"]

    def test_agent_views_show_the_text(self, tmp_path):
        irp_dir, new_id = self._supersede(tmp_path)
        inherit = run_inherit(tmp_path, irp_dir, _Args())
        item = next(e for e in inherit["active"] if e["id"] == new_id)
        assert compact_entry(item) == {
            "id": new_id, "date": TODAY, "what": self.NEW_WHAT, "why": self.NEW_WHY,
            "supersedes": GUESS["id"],
        }
        assert self.NEW_WHAT in inherit["text"] and self.NEW_WHY in inherit["text"]
        assert self.NEW_WHAT in run_why(tmp_path, irp_dir, _Args(id=new_id))["text"]

    def test_export_and_current_json_show_the_text(self, tmp_path):
        irp_dir, new_id = self._supersede(tmp_path)
        run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="agents.md", output=None, force=False, writable=False))
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert self.NEW_WHAT in body and self.NEW_WHY in body
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        item = next(e for e in current["active"] if e["id"] == new_id)
        assert item["what"] == self.NEW_WHAT and item["why"] == self.NEW_WHY

    def test_guard_matches_a_superseding_decision_by_its_text(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        irp_dir, new_id = self._supersede(tmp_path)
        (tmp_path / "ship.txt").write_text("hardware courier shipping\n", encoding="utf-8")
        subprocess.run(["git", "add", "ship.txt"], cwd=tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["match_id"] == new_id


def _read(path):
    return (REPO / path).read_text(encoding="utf-8")


class TestHumanConfirmedWording:
    """The wording must stay true for ledgers that hold lines an older bootstrap guessed."""

    def test_the_absolute_claims_are_gone(self):
        readme, trust = _read("README.md"), _read("TRUST.md")
        assert "Every entry in the ledger was confirmed by a human" not in readme
        assert "No entry exists without a human confirming it" not in readme
        assert "no entry exists without a human confirming it" not in trust

    def test_the_narrower_claim_is_there(self):
        readme, trust = _read("README.md"), _read("TRUST.md")
        assert "Every decision IRP presents was confirmed by a human" in readme
        assert "every decision IRP presents was confirmed by a human" in trust
        assert "—" not in "".join(
            line for line in readme.splitlines() if "presents was confirmed" in line)


class TestBookWording:
    def test_the_book_no_longer_calls_bootstrap_a_bulk_ingest(self):
        for name in ("book/01-analysis-commands.md", "book/01-analysis-core.md"):
            assert "Bulk ingest" not in _read(name), name
        assert "unconfirmed reconstructions" in _read("book/01-analysis-commands.md")
        assert "unconfirmed reconstructions" in _read("book/01-analysis-core.md")

    def test_ch6_does_not_say_every_ledger_decision_qualifies(self):
        text = _read("book/ch6-extensibility.md")
        assert "Every decision in the ledger qualifies" not in text
        assert "Every decision IRP presents qualifies" in text
        assert "leaves them out" in text

    def test_no_em_dash_in_the_lines_changed(self):
        for name, needle in (
            ("book/ch6-extensibility.md", "Every decision IRP presents qualifies"),
            ("book/01-analysis-commands.md", "unconfirmed reconstructions"),
            ("book/01-analysis-core.md", "unconfirmed reconstructions"),
        ):
            for line in _read(name).splitlines():
                if needle in line:
                    # (the Article 12 heading earlier on the ch6 line has an old em dash)
                    assert "—" not in line[line.index(needle):], (name, line)


class TestWhyJson:
    def test_a_guess_is_not_status_ok_in_json_output(self, tmp_path):
        _legacy_project(tmp_path)
        out = subprocess.run(
            [sys.executable, str(REPO / "irp" / "core" / "irp.py"), "why", "--id", GUESS["id"], "--json"],
            cwd=tmp_path, capture_output=True, text=True)
        data = json.loads(out.stdout)
        assert data["status"] == "unconfirmed"
        assert data["unconfirmed"] is True
        assert out.returncode == 0

    def test_a_real_decision_stays_ok(self, tmp_path):
        _legacy_project(tmp_path)
        out = subprocess.run(
            [sys.executable, str(REPO / "irp" / "core" / "irp.py"), "why", "--id", CONFIRMED["id"], "--json"],
            cwd=tmp_path, capture_output=True, text=True)
        assert json.loads(out.stdout)["status"] == "ok"


# A stale current.json: left by an old bootstrap, so its window is full of guesses.
def _stale_project(tmp_path, ledger=None):
    ledger = [CONFIRMED, GUESS] if ledger is None else ledger
    irp_dir = ensure_irp_dir(tmp_path)
    _write_ledger(irp_dir, ledger)
    guesses = [dict(GUESS, id=f"IRP-2026-05-01-{i:03d}", what=f"Guessed thing number {i}") for i in range(1, 11)]
    write_current(irp_dir, {"version": 1, "active": guesses})        # all guesses, real one pushed out
    return irp_dir


class TestStaleCurrentJson:
    def test_store_recomputes_from_the_ledger_when_current_holds_guesses(self, tmp_path):
        irp_dir = _stale_project(tmp_path)
        active, stale = store.read_active(irp_dir)
        assert stale is True
        assert [e["id"] for e in active] == [CONFIRMED["id"]]

    def test_store_trusts_a_clean_current_json(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED])
        active, stale = store.read_active(irp_dir)
        assert stale is False
        assert [e["id"] for e in active] == [CONFIRMED["id"]]

    def test_guard_checks_the_real_decisions_when_current_is_all_guesses(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        irp_dir = _stale_project(tmp_path)
        (tmp_path / "db.txt").write_text(
            "replace PostgreSQL primary database relational schema joins\n", encoding="utf-8")
        subprocess.run(["git", "add", "db.txt"], cwd=tmp_path)
        result = run_guard(tmp_path, irp_dir, _Args(guard_action="run", json=False))
        assert result["match_id"] == CONFIRMED["id"]
        assert "no active decisions" not in result["text"]

    def test_why_without_an_id_finds_the_real_latest_decision(self, tmp_path):
        irp_dir = _stale_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=None))
        assert result["status"] == "ok"
        assert result["latest"]["id"] == CONFIRMED["id"]

    def test_the_api_lists_the_real_decisions(self, api_server, tmp_path):
        _stale_project(tmp_path)
        result = api_server.get_decisions()
        assert [d["id"] for d in result["decisions"]] == [CONFIRMED["id"]]

    def test_doctor_reports_a_stale_current_json_and_how_to_fix_it(self, tmp_path):
        irp_dir = _stale_project(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["current_stale"] is True
        assert "irp doctor --fix" in result["text"]
        # Reporting does not change anything.
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert len(current["active"]) == 10

    def test_doctor_fix_rewrites_current_json_from_the_ledger(self, tmp_path):
        irp_dir = _stale_project(tmp_path)
        result = run_doctor(tmp_path, irp_dir, _Args(fix=True))
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in current["active"]] == [CONFIRMED["id"]]
        assert result["current_stale"] is False
        assert result["fixed"] == ["current.json"]

    def test_doctor_does_not_complain_about_a_clean_current_json(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED])
        result = run_doctor(tmp_path, irp_dir, _Args())
        assert result["current_stale"] is False
        assert "doctor --fix" not in result["text"]

    def test_inherit_without_a_ledger_still_uses_current_json(self, tmp_path):
        """A project that has only current.json keeps working (nothing to recompute from)."""
        irp_dir = ensure_irp_dir(tmp_path)
        write_current(irp_dir, {"version": 1, "active": [CONFIRMED, GUESS]})
        assert [e["id"] for e in run_inherit(tmp_path, irp_dir, _Args())["active"]] == [CONFIRMED["id"]]


# ── tests that pin down behaviour a mutation could silently remove ─────────────

class TestPinned:
    def test_defer_json_leaves_a_guess_out_of_relevant_decisions(self, tmp_path):
        irp_dir = _legacy_project(tmp_path)
        result = run_defer(tmp_path, irp_dir, _Args(
            question="Should zeppelin airship delivery replace PostgreSQL primary database?", json=True))
        assert result["status"] == "pending"
        assert result["relevant_decisions"] == [CONFIRMED["id"]]

    def test_find_graph_html_leaves_a_guess_out(self, tmp_path, monkeypatch):
        irp_dir = _legacy_project(tmp_path)
        opened = []
        monkeypatch.setattr(find_mod.subprocess, "Popen", lambda *a, **k: opened.append(a))
        monkeypatch.setattr(find_mod.tempfile, "gettempdir", lambda: str(tmp_path))
        result = run_find(tmp_path, irp_dir, _Args(
            query="z[e]ppelin|PostgreSQL", craft_only=False, ledger_only=False, graph=True))
        assert result["count"] == 2                       # the list still shows the guess, labelled
        html = Path(result["graph_path"]).read_text(encoding="utf-8")
        assert "PostgreSQL" in html
        assert "zeppelin" not in html
        assert opened                                      # the browser opener was stubbed, not run

    def test_capture_milestones_ignore_guesses(self):
        guesses = [dict(GUESS, id=f"IRP-2026-05-01-{i:03d}", what=f"Guess {i}") for i in range(1, 10)]
        new = dict(CONFIRMED, id="IRP-2026-06-01-001", what="First real one")
        lines = _milestone_lines(guesses + [new], new)
        assert any("First decision captured" in line for line in lines)
        assert not any("10 decisions" in line for line in lines)

    def test_capture_sensor_milestone_ignores_a_guess_from_the_same_sensor(self):
        guess = dict(GUESS, source="slack")
        new = dict(CONFIRMED, id="IRP-2026-06-01-001", source="slack")
        lines = _milestone_lines([guess, new], new)
        assert any("First capture from Slack" in line for line in lines)

    def test_a_guess_that_claims_to_supersede_does_not_remove_a_real_decision_from_exports(self, tmp_path):
        rogue = dict(GUESS, supersedes=CONFIRMED["id"])
        irp_dir = _legacy_project(tmp_path, [CONFIRMED, rogue])
        run_export(tmp_path, irp_dir, _Args(
            export_action="context", target="agents.md", output=None, force=False, writable=False))
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "PostgreSQL" in body
        assert "zeppelin" not in body

    def test_a_guess_that_claims_to_supersede_does_not_remove_a_real_decision_from_inherit(self, tmp_path):
        rogue = dict(GUESS, supersedes=CONFIRMED["id"])
        irp_dir = _legacy_project(tmp_path, [CONFIRMED, rogue])
        assert [e["id"] for e in run_inherit(tmp_path, irp_dir, _Args())["active"]] == [CONFIRMED["id"]]


# ── round 2 ────────────────────────────────────────────────────────────────────

RETIREMENT_OF_GUESS = {
    "type": "retirement", "id": GUESS["id"], "reason": "bootstrap guess, not a decision",
    "timestamp": "2026-04-03", "source": "irp mod",
}
RETIREMENT_OF_REAL = {
    "type": "retirement", "id": CONFIRMED["id"], "reason": "no longer true",
    "timestamp": "2026-04-04", "source": "irp mod",
}


def _retired_guess_project(tmp_path):
    return _legacy_project(tmp_path, [CONFIRMED, GUESS, RETIREMENT_OF_GUESS])


class TestRetiredGuessLookup:
    """A retirement event shares its target's id and is not bootstrapped, so a
    lookup that takes 'any confirmed row' returns it for a retired guess."""

    def test_store_picks_decision_rows_only(self):
        confirmed, guesses = store.decision_rows_for_id([GUESS, RETIREMENT_OF_GUESS], GUESS["id"])
        assert confirmed == []
        assert guesses == [GUESS]
        confirmed, guesses = store.decision_rows_for_id(
            [CONFIRMED, RETIREMENT_OF_REAL], CONFIRMED["id"])
        assert confirmed == [CONFIRMED] and guesses == []

    def test_why_shows_the_retired_guess_labelled_not_the_retirement(self, tmp_path):
        irp_dir = _retired_guess_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=GUESS["id"]))
        assert result["status"] == "unconfirmed"
        assert result["unconfirmed"] is True
        assert result["entry"]["type"] == "decision"
        assert result["entry"]["what"] == GUESS["what"]
        assert LABEL in result["text"]

    def test_why_for_agents_says_not_found_for_a_retired_guess(self, tmp_path):
        irp_dir = _retired_guess_project(tmp_path)
        result = run_why(tmp_path, irp_dir, _Args(id=GUESS["id"], confirmed_only=True))
        assert result["status"] == "not_found"
        assert "entry" not in result

    def test_cli_text_and_json_for_a_retired_guess(self, tmp_path):
        _retired_guess_project(tmp_path)
        irp_py = str(REPO / "irp" / "core" / "irp.py")
        as_json = subprocess.run(
            [sys.executable, irp_py, "why", "--id", GUESS["id"], "--json"],
            cwd=tmp_path, capture_output=True, text=True)
        data = json.loads(as_json.stdout)
        assert data["status"] == "unconfirmed" and data["unconfirmed"] is True
        assert data["entry"]["type"] == "decision"
        as_text = subprocess.run(
            [sys.executable, irp_py, "why", "--id", GUESS["id"]],
            cwd=tmp_path, capture_output=True, text=True)
        assert LABEL in as_text.stdout
        assert "zeppelin" in as_text.stdout

    def test_why_still_shows_a_retired_real_decision(self, tmp_path):
        irp_dir = _legacy_project(tmp_path, [CONFIRMED, RETIREMENT_OF_REAL])
        result = run_why(tmp_path, irp_dir, _Args(id=CONFIRMED["id"]))
        assert result["status"] == "ok"
        assert result["entry"]["type"] == "decision"

    def test_mcp_irp_why_is_not_found_for_a_retired_guess(self, mcp_server, tmp_path):
        _retired_guess_project(tmp_path)
        result = mcp_server.irp_why(id=GUESS["id"])
        assert result["status"] == "not_found"
        assert result["entry"] is None

    def test_api_answers_404_for_a_retired_guess(self, api_server, tmp_path):
        _retired_guess_project(tmp_path)
        with pytest.raises(Exception) as exc:
            api_server.get_decision(GUESS["id"])
        assert getattr(exc.value, "status_code", None) == 404
        assert "unconfirmed" in str(getattr(exc.value, "detail", ""))

    def test_api_still_serves_a_retired_real_decision(self, api_server, tmp_path):
        _legacy_project(tmp_path, [CONFIRMED, RETIREMENT_OF_REAL])
        assert api_server.get_decision(CONFIRMED["id"])["type"] == "decision"


class TestDoctorCurrentJsonCount:
    def _count_detail(self, result):
        return next(c for c in result["checks"] if c["label"] == "current.json")["detail"]

    def test_counts_the_active_list_of_the_dict_format(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        write_current(irp_dir, {"version": 1, "active": [CONFIRMED, dict(CONFIRMED, id="IRP-2026-04-09-001")]})
        assert self._count_detail(run_doctor(tmp_path, irp_dir, _Args())) == "2 active decisions"

    def test_zero_and_one(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        assert self._count_detail(run_doctor(tmp_path, irp_dir, _Args())) == "0 active decisions"
        write_current(irp_dir, {"version": 1, "active": [CONFIRMED]})
        assert self._count_detail(run_doctor(tmp_path, irp_dir, _Args())) == "1 active decision"

    def test_the_list_format_still_counts_its_length(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        (irp_dir / "current.json").write_text(json.dumps([CONFIRMED, CONFIRMED, CONFIRMED]), encoding="utf-8")
        assert self._count_detail(run_doctor(tmp_path, irp_dir, _Args())) == "3 active decisions"

    def test_guesses_left_in_a_stale_file_are_not_counted_as_active(self, tmp_path):
        irp_dir = _stale_project(tmp_path)
        assert self._count_detail(run_doctor(tmp_path, irp_dir, _Args())) == "0 active decisions"
