"""irp inherit returns the active decisions: superseded and retired ones are left
out, and an older active decision is never dropped just because newer ones exist.

Before 0.9.1 inherit returned the last 10 decision entries in the ledger,
whatever their status, so an agent could get a superseded decision and miss the
current one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "irp" / "core"))
sys.path.insert(0, str(ROOT))

from irp.core.commands.inherit import INHERIT_LIMIT, run_inherit  # noqa: E402
from irp.core.store import append_ledger_entry, ensure_irp_dir  # noqa: E402


def _decision(n: int, what: str | None = None, **extra) -> dict:
    return {"type": "decision", "id": f"IRP-2026-01-{n:02d}-001",
            "what": what or f"Decision {n}", "why": f"Reason {n}", **extra}


def _run(tmp_path, entries):
    irp_dir = ensure_irp_dir(tmp_path)
    for e in entries:
        append_ledger_entry(irp_dir, e)
    return run_inherit(project_root=tmp_path, irp_dir=irp_dir, args=SimpleNamespace(json=True))


def _ids(result):
    return [e["id"] for e in result["active"]]


def test_superseded_decision_is_left_out(tmp_path):
    rest = _decision(1, "Use REST for internal services")
    grpc = _decision(2, "Use gRPC for internal services", supersedes=rest["id"])
    result = _run(tmp_path, [rest, grpc])
    assert _ids(result) == [grpc["id"]]
    assert "Use REST" not in result["text"]


def test_supersedes_can_be_a_list(tmp_path):
    a, b = _decision(1), _decision(2)
    c = _decision(3, supersedes=[a["id"], b["id"]])
    assert _ids(_run(tmp_path, [a, b, c])) == [c["id"]]


def test_retired_decision_is_left_out(tmp_path):
    a, b = _decision(1), _decision(2)
    retire = {"type": "retirement", "id": a["id"], "why": "No longer relevant"}
    assert _ids(_run(tmp_path, [a, b, retire])) == [b["id"]]


def test_older_active_decision_is_kept_when_many_newer_exist(tmp_path):
    rest = _decision(1, "Use REST for internal services")
    grpc = _decision(2, "Use gRPC for internal services", supersedes=rest["id"])
    later = [_decision(n) for n in range(3, 15)]  # 12 newer decisions
    result = _run(tmp_path, [rest, grpc, *later])
    assert grpc["id"] in _ids(result)
    assert rest["id"] not in _ids(result)
    assert result["active_count"] == 13


def test_caps_at_the_most_recent_active_decisions_and_says_so(tmp_path):
    entries = [_decision(n % 28 + 1, what=f"Decision {n}") | {"id": f"IRP-2026-02-01-{n:03d}"}
               for n in range(INHERIT_LIMIT + 5)]
    result = _run(tmp_path, entries)
    assert len(result["active"]) == INHERIT_LIMIT
    assert result["active"][-1]["id"] == entries[-1]["id"]
    assert result["omitted"] == 5
    assert "5 older active decisions not shown" in result["text"]


def test_limit_is_fifty():
    assert INHERIT_LIMIT == 50


def test_order_stays_chronological(tmp_path):
    a, b, c = _decision(1), _decision(2), _decision(3)
    assert _ids(_run(tmp_path, [a, b, c])) == [a["id"], b["id"], c["id"]]


def test_text_shows_rejected_options_and_supersedes(tmp_path):
    rest = _decision(1, "Use REST")
    grpc = _decision(2, "Use gRPC", supersedes=rest["id"],
                     alternatives="GraphQL: rejected, no streaming support.")
    text = _run(tmp_path, [rest, grpc])["text"]
    assert "Rejected: GraphQL: rejected, no streaming support." in text
    assert f"Supersedes: {rest['id']}" in text


def test_reports_how_many_were_left_out_as_superseded_or_retired(tmp_path):
    a = _decision(1)
    b = _decision(2, supersedes=a["id"])
    assert _run(tmp_path, [a, b])["superseded_count"] == 1


def test_empty_ledger(tmp_path):
    result = _run(tmp_path, [])
    assert result["active_count"] == 0
    assert result["active"] == []
    assert "No active IRP context" in result["text"]
