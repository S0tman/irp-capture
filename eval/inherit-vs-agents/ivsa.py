"""irp_inherit vs AGENTS.md: does IRP's structure help an agent beyond the same facts?

Each task is run with four kinds of project context:

    none          no project context
    agents-rules  an AGENTS.md listing the current rules only (how most teams write it)
    agents-full   an AGENTS.md carrying every fact IRP holds: dates, reasons, the
                  options turned down, and the history. Superseded decisions stay
                  in the file; later entries are headed "Update:" but don't name
                  what they replace
    irp           exactly what the irp_inherit MCP tool returns, built by the real
                  run_inherit from a seeded ledger: active decisions only

The comparison that matters is irp against agents-full. Scoring and the verdict
follow the pre-registration in README.md. Nothing here calls a model; run.py does.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "why-in"))
sys.path.insert(0, str(REPO))

import whyin  # noqa: E402  (shared answer parsing and paired bootstrap)
from irp.core.commands.inherit import run_inherit  # noqa: E402
from irp.core.store import append_ledger_entry, ensure_irp_dir  # noqa: E402

ARMS = ("none", "agents-rules", "agents-full", "irp")
TYPES = ("superseded", "reopen", "plain")
OPTION_KEYS = {
    "superseded": {"current", "old", "ask"},
    "reopen": {"keep", "adopt", "ask"},
    "plain": {"comply", "deviate", "ask"},
}
ACCEPT = {
    "superseded": {"current"},
    "reopen": {"keep", "ask"},
    "plain": {"comply"},
}
HINGES = {"superseded": {"current", "old"}, "reopen": {"rejected_in"}, "plain": {"rule"}}

# Registered thresholds (README.md, "Kill criteria").
GAP = 0.10
MODELS_NEEDED = 2

SYSTEM = (
    "You are an AI coding agent working in a team's repository. Use the project "
    "context you are given, if any, then pick the best option. Reply with JSON "
    'only, in this form: {"choice": "A", "why": "one short sentence"}'
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GRUNDEN_URL = "https://api.grunden.ai/v1/chat/completions"


# ── Files ───────────────────────────────────────────────────────────────────

def load_projects(path: Path | str) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {p["key"]: p for p in data["projects"]}


def load_tasks(path: Path | str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["tasks"]


def _by_id(project: dict) -> dict[str, dict]:
    return {d["id"]: d for d in project["decisions"]}


def _superseded_ids(project: dict) -> set[str]:
    return {d["supersedes"] for d in project["decisions"] if d.get("supersedes")}


def validate_task(t: dict, projects: dict[str, dict]) -> None:
    tid = t.get("id")
    if t.get("type") not in TYPES:
        raise ValueError(f"{tid}: unknown type")
    if t.get("project") not in projects:
        raise ValueError(f"{tid}: unknown project")
    if set(t["options"]) != OPTION_KEYS[t["type"]]:
        raise ValueError(f"{tid}: options don't fit type {t['type']}")
    if set(t["accept"]) != ACCEPT[t["type"]]:
        raise ValueError(f"{tid}: accept isn't the registered set")
    if set(t["hinges_on"]) != HINGES[t["type"]]:
        raise ValueError(f"{tid}: hinges_on keys don't fit type")
    p = projects[t["project"]]
    ids = _by_id(p)
    for ref in t["hinges_on"].values():
        if ref not in ids:
            raise ValueError(f"{tid}: {ref} not in project {p['key']}")
    if t["type"] == "superseded":
        cur, old = ids[t["hinges_on"]["current"]], t["hinges_on"]["old"]
        if cur.get("supersedes") != old:
            raise ValueError(f"{tid}: current doesn't supersede old")
    elif t["type"] == "reopen":
        d = ids[t["hinges_on"]["rejected_in"]]
        if not d.get("alternatives") or d["id"] in _superseded_ids(p):
            raise ValueError(f"{tid}: rejected_in must be an active decision with alternatives")
    else:
        if t["hinges_on"]["rule"] in _superseded_ids(p):
            raise ValueError(f"{tid}: rule is superseded")
    for f in ("situation",):
        if not str(t.get(f, "")).strip():
            raise ValueError(f"{tid}: empty {f}")


# ── Contexts ────────────────────────────────────────────────────────────────

def _ledger_entry(d: dict) -> dict:
    e = {"type": "decision", "id": d["id"], "timestamp": f"{d['date']}T09:00:00Z",
         "what": d["what"], "why": d["why"], "confidence": "high", "source": "cli"}
    if d.get("alternatives"):
        e["alternatives"] = d["alternatives"]
    if d.get("supersedes"):
        e["supersedes"] = d["supersedes"]
    return e


def _irp_context(project: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        irp_dir = ensure_irp_dir(Path(tmp))
        for d in project["decisions"]:
            append_ledger_entry(irp_dir, _ledger_entry(d))
        result = run_inherit(project_root=Path(f"/repo/{project['key']}"), irp_dir=irp_dir,
                             args=SimpleNamespace(json=True))
    # The same keys the irp_inherit MCP tool returns (irp/mcp/server.py).
    tool = {"project_root": result["project_root"], "active_count": result["active_count"],
            "omitted": result.get("omitted", 0), "active": result["active"]}
    return json.dumps(tool, indent=2, ensure_ascii=False)


def context(project: dict, arm: str) -> str:
    if arm == "none":
        return ""
    if arm == "irp":
        return _irp_context(project)
    gone = _superseded_ids(project)
    if arm == "agents-rules":
        rules = [f"- {d['what']}" for d in project["decisions"] if d["id"] not in gone]
        return (f"# AGENTS.md\n\n{project['name']}: rules for agents working in this "
                f"repository.\n\n" + "\n".join(rules) + "\n")
    if arm == "agents-full":
        parts = [f"# AGENTS.md\n\n{project['name']}: our decisions, oldest first.\n"]
        for d in project["decisions"]:
            head = f"## {d['date']}: " + ("Update: " if d.get("supersedes") else "") + d["what"]
            lines = [head, f"Why: {d['why']}"]
            if d.get("alternatives"):
                lines.append(f"Alternatives: {d['alternatives']}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts) + "\n"
    raise ValueError(f"unknown arm {arm!r}")


# ── Prompts and answers ─────────────────────────────────────────────────────

def option_order(t: dict, run: int) -> list[str]:
    keys = sorted(t["options"])
    random.Random(f"order:{t['id']}:{run}").shuffle(keys)
    return keys


def build_prompt(t: dict, project: dict, arm: str, order: list[str]) -> dict[str, str]:
    ctx = context(project, arm)
    if arm == "none":
        head = "No project context is available."
    elif arm == "irp":
        head = ("Result of calling the irp_inherit tool (the team's decision record):\n"
                f"```json\n{ctx}```")
    else:
        head = f"Contents of AGENTS.md in the repository:\n```markdown\n{ctx}```"
    options = "\n".join(f"{'ABC'[i]}. {t['options'][k]}" for i, k in enumerate(order))
    user = f"{head}\n\nTask: {t['situation']}\n\nOptions:\n{options}"
    return {"system": SYSTEM, "user": user}


parse_choice = whyin.parse_choice


def classify(t: dict, order: list[str], letter: str | None) -> tuple[str | None, str]:
    if letter is None:
        return None, "invalid"
    key = order["ABC".index(letter)]
    if key in t["accept"]:
        return key, "correct"
    if key == "ask":
        return key, "ask"
    return key, "wrong"


# ── Routing ─────────────────────────────────────────────────────────────────

def route(model: str) -> tuple[str, str, dict, str]:
    """(url, model id sent upstream, extra body fields, env var holding the key)."""
    if model.startswith("grunden/"):
        # As IRP Compliance runs it: GLM-5.3 has no thinking off switch; low is the floor.
        return GRUNDEN_URL, model.split("/", 1)[1], {"reasoning_effort": "low"}, "GRUNDEN_API_KEY"
    return OPENROUTER_URL, model, {}, "OPENROUTER_API_KEY"


# ── Scoring ─────────────────────────────────────────────────────────────────

def _rate(rows, hit):
    valid = [r for r in rows if r["outcome"] != "invalid"]
    return round(sum(1 for r in valid if hit(r)) / len(valid), 4) if valid else None


def _summary(rows: list[dict]) -> dict[str, Any]:
    def sel(kind, arm):
        return [r for r in rows if r["type"] == kind and r["arm"] == arm]

    correct = lambda r: r["outcome"] == "correct"  # noqa: E731
    wrong = lambda r: r["outcome"] == "wrong"      # noqa: E731
    ask = lambda r: r.get("key") == "ask"          # noqa: E731
    out = {
        "superseded": {a: _rate(sel("superseded", a), correct) for a in ARMS},
        "superseded_ask": {a: _rate(sel("superseded", a), ask) for a in ARMS},
        "reopen_adopt": {a: _rate(sel("reopen", a), wrong) for a in ARMS},
        "reopen_ask": {a: _rate(sel("reopen", a), ask) for a in ARMS},
        "plain": {a: _rate(sel("plain", a), correct) for a in ARMS},
        "invalid": sum(1 for r in rows if r["outcome"] == "invalid"),
        "calls": len(rows),
    }
    si, sf = out["superseded"]["irp"], out["superseded"]["agents-full"]
    out["superseded_gap"] = round(si - sf, 4) if si is not None and sf is not None else None
    out["superseded_gap_ci"] = whyin._gap_ci(whyin._per_task(sel("superseded", "irp"), correct),
                                             whyin._per_task(sel("superseded", "agents-full"), correct))
    ri, rf = out["reopen_adopt"]["irp"], out["reopen_adopt"]["agents-full"]
    out["reopen_adopt_gap"] = round(rf - ri, 4) if ri is not None and rf is not None else None
    out["reopen_adopt_gap_ci"] = whyin._gap_ci(whyin._per_task(sel("reopen", "agents-full"), wrong),
                                               whyin._per_task(sel("reopen", "irp"), wrong))
    gap, ci = out["superseded_gap"], out["superseded_gap_ci"]
    out["clears"] = bool(gap is not None and ci and gap >= GAP and ci[0] > 0)
    out["clearly_below"] = bool(ci and ci[1] < GAP)
    return out


def score(rows: list[dict], primary: tuple[str, ...]) -> dict[str, Any]:
    names = sorted({r["model"] for r in rows})
    models = {m: _summary([r for r in rows if r["model"] == m]) for m in names}
    prim = {m: v for m, v in models.items() if m in primary}
    clearing = sum(1 for v in prim.values() if v["clears"])
    below = sum(1 for v in prim.values() if v["clearly_below"])
    if clearing >= MODELS_NEEDED:
        verdict = "supported"
    elif below >= MODELS_NEEDED:
        verdict = "killed"
    else:
        verdict = "inconclusive"
    return {"models": models,
            "verdict": {"supersession": verdict, "primary_clearing": clearing,
                        "primary_clearly_below": below, "of": len(prim)}}


# ── Freezing ────────────────────────────────────────────────────────────────

FROZEN = ("projects.json", "tasks.json", "ivsa.py", "run.py",
          "../why-in/whyin.py", "../../irp/core/commands/inherit.py")


def freeze_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            for name in FROZEN if (HERE / name).exists()}
