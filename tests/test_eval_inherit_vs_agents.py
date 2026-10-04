"""Tests for the irp_inherit vs AGENTS.md eval harness (eval/inherit-vs-agents).

Offline: no model is called. The irp arm is built by the real run_inherit,
so these tests also pin that arm to what an agent would actually receive.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "eval" / "inherit-vs-agents"
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(ROOT))

import ivsa  # noqa: E402

PROJECTS = ivsa.load_projects(EVAL_DIR / "projects.json")
TASKS = ivsa.load_tasks(EVAL_DIR / "tasks.json")


def _task(tid):
    return next(t for t in TASKS if t["id"] == tid)


# ── Task and project files ──────────────────────────────────────────────────

def test_registered_shape():
    kinds = [t["type"] for t in TASKS]
    assert len(TASKS) == 24
    assert kinds.count("superseded") == 10
    assert kinds.count("reopen") == 10
    assert kinds.count("plain") == 4


def test_four_projects():
    assert sorted(PROJECTS) == ["fjord", "hearth", "ledgerly", "riverbank"]


def test_every_task_validates_against_its_project():
    for t in TASKS:
        ivsa.validate_task(t, PROJECTS)


def test_validate_rejects_a_superseded_task_whose_pair_is_wrong():
    t = dict(_task("S01"))
    t["hinges_on"] = {"current": "IRP-2025-11-10-001", "old": "IRP-2026-02-02-001"}
    with pytest.raises(ValueError):
        ivsa.validate_task(t, PROJECTS)


def test_validate_rejects_a_reopen_task_without_recorded_alternatives():
    t = dict(_task("R01"))
    t["hinges_on"] = {"rejected_in": "IRP-2026-05-04-001"}  # has no alternatives
    with pytest.raises(ValueError):
        ivsa.validate_task(t, PROJECTS)


def test_validate_rejects_unknown_ids():
    t = dict(_task("P01"))
    t["hinges_on"] = {"rule": "IRP-1999-01-01-001"}
    with pytest.raises(ValueError):
        ivsa.validate_task(t, PROJECTS)


# ── Contexts per arm ────────────────────────────────────────────────────────

def test_none_arm_has_no_project_context():
    assert ivsa.context(PROJECTS["ledgerly"], "none") == ""


def test_agents_rules_lists_current_rules_only_without_reasons():
    ctx = ivsa.context(PROJECTS["ledgerly"], "agents-rules")
    assert "over gRPC" in ctx
    assert "over REST with JSON" not in ctx          # superseded
    assert "Why:" not in ctx and "Alternatives:" not in ctx


def test_agents_full_keeps_history_reasons_and_rejected_options():
    ctx = ivsa.context(PROJECTS["ledgerly"], "agents-full")
    assert "over REST with JSON" in ctx              # history stays
    assert "over gRPC" in ctx
    assert "Update:" in ctx                          # superseding entries are marked as updates
    assert "Alternatives: MongoDB" in ctx
    assert ctx.index("over REST with JSON") < ctx.index("over gRPC")


def test_agents_full_update_lines_do_not_name_what_they_replace():
    ctx = ivsa.context(PROJECTS["ledgerly"], "agents-full")
    assert "IRP-" not in ctx                         # no ids, no explicit pointers


def test_irp_arm_is_real_inherit_output_without_superseded_decisions():
    ctx = ivsa.context(PROJECTS["ledgerly"], "irp")
    data = json.loads(ctx)
    ids = [e["id"] for e in data["active"]]
    assert "IRP-2026-02-02-001" in ids               # gRPC, current
    assert "IRP-2025-11-10-001" not in ids           # REST, superseded
    grpc = next(e for e in data["active"] if e["id"] == "IRP-2026-02-02-001")
    assert grpc["supersedes"] == "IRP-2025-11-10-001"
    assert "alternatives" in grpc
    assert set(data) == {"project_root", "active_count", "omitted", "active"}


def test_irp_arm_matches_what_the_mcp_tool_returns():
    data = json.loads(ivsa.context(PROJECTS["fjord"], "irp"))
    assert data["active_count"] == len(data["active"]) == 8
    assert data["omitted"] == 0


def test_contexts_are_deterministic():
    for arm in ivsa.ARMS:
        assert ivsa.context(PROJECTS["hearth"], arm) == ivsa.context(PROJECTS["hearth"], arm)


# ── Prompts ─────────────────────────────────────────────────────────────────

def test_prompt_labels_the_context_source():
    t = _task("S01")
    order = ivsa.option_order(t, 0)
    assert "AGENTS.md" in ivsa.build_prompt(t, PROJECTS["ledgerly"], "agents-full", order)["user"]
    assert "irp_inherit" in ivsa.build_prompt(t, PROJECTS["ledgerly"], "irp", order)["user"]
    assert "No project context" in ivsa.build_prompt(t, PROJECTS["ledgerly"], "none", order)["user"]


def test_prompt_has_the_situation_and_lettered_options():
    t = _task("R01")
    p = ivsa.build_prompt(t, PROJECTS["ledgerly"], "none", ["ask", "adopt", "keep"])["user"]
    assert "balance endpoint is slow" in p
    assert "A. Ask the payments team" in p
    assert "C. Cache balances in Redis" in p


# ── Classifying and scoring ─────────────────────────────────────────────────

def test_ask_is_not_correct_on_superseded_but_is_on_reopen():
    s = _task("S01")
    assert ivsa.classify(s, ["ask", "current", "old"], "A") == ("ask", "ask")
    assert ivsa.classify(s, ["ask", "current", "old"], "B") == ("current", "correct")
    assert ivsa.classify(s, ["ask", "current", "old"], "C") == ("old", "wrong")
    r = _task("R01")
    assert ivsa.classify(r, ["ask", "adopt", "keep"], "A") == ("ask", "correct")
    assert ivsa.classify(r, ["ask", "adopt", "keep"], "B") == ("adopt", "wrong")
    assert ivsa.classify(r, ["ask", "adopt", "keep"], None) == (None, "invalid")


def _rows(model, arm, kind, n_correct, n=10, prefix="S"):
    rows = []
    for i in range(n):
        ok = i < n_correct
        rows.append({"model": model, "arm": arm, "type": kind, "task": f"{prefix}{i:02d}",
                     "run": 0, "outcome": "correct" if ok else "wrong"})
    return rows


def _results(sup_irp, sup_full, models=("m1", "m2", "m3")):
    rows = []
    for m in models:
        for arm, n in (("irp", sup_irp), ("agents-full", sup_full),
                       ("agents-rules", sup_full), ("none", 0)):
            rows += _rows(m, arm, "superseded", n)
            rows += _rows(m, arm, "reopen", 8, prefix="R")
    return rows


def test_superseded_accuracy_and_gap():
    s = ivsa.score(_results(9, 5), primary=("m1", "m2", "m3"))
    assert s["models"]["m1"]["superseded"]["irp"] == pytest.approx(0.9)
    assert s["models"]["m1"]["superseded_gap"] == pytest.approx(0.4)


def test_verdict_supported_when_irp_clearly_beats_agents_full():
    assert ivsa.score(_results(10, 3), primary=("m1", "m2", "m3"))["verdict"]["supersession"] == "supported"


def test_verdict_not_supported_on_a_tie():
    v = ivsa.score(_results(8, 8), primary=("m1", "m2", "m3"))["verdict"]["supersession"]
    assert v in ("killed", "inconclusive")


def test_verdict_uses_only_the_primary_models():
    rows = _results(10, 3, models=("m1", "m2", "m3")) + _results(3, 10, models=("glm",))
    s = ivsa.score(rows, primary=("m1", "m2", "m3"))
    assert s["verdict"]["supersession"] == "supported"
    assert "glm" in s["models"]


def test_reopen_adopt_rate_is_reported():
    s = ivsa.score(_results(9, 5), primary=("m1", "m2", "m3"))
    assert s["models"]["m2"]["reopen_adopt"]["irp"] == pytest.approx(0.2)


# ── Model routing ───────────────────────────────────────────────────────────

def test_glm_goes_to_grunden_with_low_reasoning():
    url, model, extra, key = ivsa.route("grunden/glm-5.3")
    assert url.startswith("https://api.grunden.ai/")
    assert model == "glm-5.3"
    assert extra == {"reasoning_effort": "low"}
    assert key == "GRUNDEN_API_KEY"


def test_openrouter_models_route_to_openrouter():
    url, model, extra, key = ivsa.route("anthropic/claude-haiku-4.5")
    assert url.startswith("https://openrouter.ai/")
    assert model == "anthropic/claude-haiku-4.5"
    assert key == "OPENROUTER_API_KEY"


# ── Freezing ────────────────────────────────────────────────────────────────

def test_freeze_covers_inputs_code_and_the_shared_scoring_code():
    h = ivsa.freeze_hashes()
    assert {"projects.json", "tasks.json", "ivsa.py", "../why-in/whyin.py",
            "../../irp/core/commands/inherit.py"} <= set(h)
