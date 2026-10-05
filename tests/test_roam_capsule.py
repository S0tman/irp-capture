"""Roaming IRP, Cut 1 step 1: the Context Capsule builder.

The capsule is the read side of the AI boundary (spec §8a): a deterministic,
budgeted slice of the ledger that any AI, human or script can read with only
HTTP and files. Same selection and params give byte-identical bytes, so the
capsule_digest says exactly what a reader saw.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from irp.roam.capsule import (  # noqa: E402
    CapsuleError,
    build_capsule,
    write_bundle,
)


def _d(id_, what, ts, **rel):
    return {"type": "decision", "id": id_, "what": what, "why": f"Because of {what.lower()}.",
            "timestamp": ts, **rel}


# A small lineage like the spec's worked example, plus noise.
A = _d("IRP-2026-09-08-002", "Roaming direction", "2026-09-08T10:00:00Z")
B = _d("IRP-2026-09-08-003", "Triad converged", "2026-09-08T12:00:00Z", rests_on=["IRP-2026-09-08-002"])
C = _d("IRP-2026-09-10-002", "Phone PWA trusted device", "2026-09-10T09:00:00Z", rests_on=["IRP-2026-09-08-003"])
D = _d("IRP-2026-09-01-001", "Very old root", "2026-09-01T09:00:00Z")
A_ROOTED = {**A, "rests_on": ["IRP-2026-09-01-001"]}
NOISE = _d("IRP-2026-09-09-001", "Unrelated decision", "2026-09-09T09:00:00Z")


def _ids(cap, form=None):
    return [r["id"] for r in cap.records if form is None or r["_capsule_form"] == form]


def test_worked_example_target_plus_two_ancestors():
    cap = build_capsule([A, B, C, NOISE], targets=["IRP-2026-09-10-002"])
    assert _ids(cap) == ["IRP-2026-09-08-002", "IRP-2026-09-08-003", "IRP-2026-09-10-002"]
    assert all(r["_capsule_form"] == "full" for r in cap.records)
    assert cap.manifest["pruning"]["applied"] is False
    assert "IRP-2026-09-09-001" not in _ids(cap)


def test_ancestor_depth_is_respected():
    cap = build_capsule([D, A_ROOTED, B, C], targets=["IRP-2026-09-10-002"], ancestor_depth=2)
    assert "IRP-2026-09-01-001" not in _ids(cap)
    deeper = build_capsule([D, A_ROOTED, B, C], targets=["IRP-2026-09-10-002"], ancestor_depth=3)
    assert "IRP-2026-09-01-001" in _ids(deeper)


def test_relationship_labels_in_manifest():
    cap = build_capsule([A, B, C], targets=["IRP-2026-09-10-002"])
    rel = {r["id"]: r["relationship_to_target"] for r in cap.manifest["records"]}
    assert rel["IRP-2026-09-10-002"] == "target"
    assert rel["IRP-2026-09-08-003"] == "rests_on@depth1"
    assert rel["IRP-2026-09-08-002"] == "rests_on@depth2"


def test_supersede_head_is_included_so_state_is_never_stale():
    newer = _d("IRP-2026-09-20-001", "Triad revisited", "2026-09-20T09:00:00Z", supersedes="IRP-2026-09-08-003")
    cap = build_capsule([A, B, C, newer], targets=["IRP-2026-09-10-002"])
    assert "IRP-2026-09-20-001" in _ids(cap)
    rel = {r["id"]: r["relationship_to_target"] for r in cap.manifest["records"]}
    assert rel["IRP-2026-09-20-001"] == "supersede_head"


def test_pinned_records_are_included():
    cap = build_capsule([A, B, C, NOISE], targets=["IRP-2026-09-10-002"], pinned=["IRP-2026-09-09-001"])
    rel = {r["id"]: r["relationship_to_target"] for r in cap.manifest["records"]}
    assert rel["IRP-2026-09-09-001"] == "pinned"


def test_records_are_in_causal_order():
    cap = build_capsule([C, B, A], targets=["IRP-2026-09-10-002"])  # ledger order scrambled
    assert _ids(cap) == ["IRP-2026-09-08-002", "IRP-2026-09-08-003", "IRP-2026-09-10-002"]


def test_digest_is_deterministic_and_ignores_volatile_fields():
    a = build_capsule([A, B, C], targets=["IRP-2026-09-10-002"], generated_at="2026-10-05T10:00:00Z")
    b = build_capsule([A, B, C], targets=["IRP-2026-09-10-002"], generated_at="2026-10-06T11:00:00Z",
                      disclosure={"surface": "claude-cloud", "scope": "read", "expires": "2026-10-06T13:00:00Z"})
    assert a.digest == b.digest
    assert a.digest.startswith("sha256-")
    c = build_capsule([A, B, C], targets=["IRP-2026-09-10-002"], ancestor_depth=1)
    assert c.digest != a.digest


def test_legacy_entries_get_a_readable_stub():
    legacy = {"type": "decision", "id": "IRP-2026-09-02-001", "title": "Legacy titled decision",
              "decision": "Long decision text", "timestamp": "2026-09-02T09:00:00Z"}
    child = _d("IRP-2026-09-03-001", "Child", "2026-09-03T09:00:00Z", rests_on=["IRP-2026-09-02-001"])
    grandchild = _d("IRP-2026-09-04-001", "Grandchild", "2026-09-04T09:00:00Z", rests_on=["IRP-2026-09-03-001"])
    cap = build_capsule([legacy, child, grandchild], targets=["IRP-2026-09-04-001"], token_budget=130)
    stub = next(r for r in cap.records if r["id"] == "IRP-2026-09-02-001")
    assert stub["_capsule_form"] == "stub"
    assert stub["what"] == "Legacy titled decision"


# ── Exposure classes (Amendment A1) ──

SECRET = {**_d("IRP-2026-01-05-001", "Client deal terms", "2026-01-05T09:00:00Z"), "tags": ["partner-x"]}
USES_SECRET = _d("IRP-2026-01-06-001", "Plan built on deal", "2026-01-06T09:00:00Z", rests_on=["IRP-2026-01-05-001"])


def test_class0_is_excluded_for_non_eu_readers_and_counted():
    cap = build_capsule([SECRET, USES_SECRET], targets=["IRP-2026-01-06-001"],
                        reader_region="non-eu", class0_tags=["partner-x"])
    assert "IRP-2026-01-05-001" not in _ids(cap)
    omitted = cap.manifest["omitted"]
    assert omitted == [{"id": "IRP-2026-01-05-001", "reason": "exposure_class"}]


def test_class0_is_included_for_eu_readers():
    cap = build_capsule([SECRET, USES_SECRET], targets=["IRP-2026-01-06-001"],
                        reader_region="eu", class0_tags=["partner-x"])
    assert "IRP-2026-01-05-001" in _ids(cap)


def test_explicit_exposure_field_marks_class0():
    marked = {**_d("IRP-2026-09-05-002", "Marked", "2026-09-05T10:00:00Z"), "exposure_class": "0"}
    child = _d("IRP-2026-09-06-002", "Child", "2026-09-06T10:00:00Z", rests_on=["IRP-2026-09-05-002"])
    cap = build_capsule([marked, child], targets=["IRP-2026-09-06-002"], reader_region="non-eu")
    assert "IRP-2026-09-05-002" not in _ids(cap)


def test_class0_target_for_non_eu_reader_is_an_error():
    with pytest.raises(CapsuleError, match="exposure"):
        build_capsule([SECRET], targets=["IRP-2026-01-05-001"], reader_region="non-eu",
                      class0_tags=["partner-x"])


# ── Budgets and honest pruning ──

def _chain(n):
    out = []
    for i in range(n):
        rel = {"rests_on": [f"IRP-2026-08-{i:02d}-001"]} if i else {}
        e = _d(f"IRP-2026-08-{i + 1:02d}-001", f"Step {i + 1}", f"2026-08-{i + 1:02d}T09:00:00Z", **rel)
        e["why"] = "x" * 400
        out.append(e)
    return out


def test_over_budget_demotes_deep_ancestors_to_stubs_first():
    chain = _chain(4)
    target = chain[-1]["id"]
    cap = build_capsule(chain, targets=[target], ancestor_depth=3, token_budget=350)
    forms = {r["id"]: r["_capsule_form"] for r in cap.records}
    assert forms[target] == "full"
    assert forms[chain[-2]["id"]] == "full"          # direct basis is never demoted
    assert forms[chain[0]["id"]] == "stub"           # depth 3
    assert cap.manifest["pruning"]["applied"] is True


def test_still_over_budget_drops_deepest_stubs_and_says_so():
    chain = _chain(5)
    target = chain[-1]["id"]
    cap = build_capsule(chain, targets=[target], ancestor_depth=4, token_budget=320)
    assert chain[0]["id"] not in _ids(cap)
    assert cap.manifest["pruning"]["omitted_count"] >= 1
    assert "size" in cap.manifest["pruning"]["reason"]


def test_min_set_over_budget_is_an_error_not_a_silent_truncation():
    chain = _chain(2)
    with pytest.raises(CapsuleError, match="narrow"):
        build_capsule(chain, targets=[chain[-1]["id"]], token_budget=10)


def test_unknown_target_is_an_error():
    with pytest.raises(CapsuleError, match="IRP-2099"):
        build_capsule([A], targets=["IRP-2099-01-01-001"])


# ── The bundle on disk ──

def test_bundle_has_the_spec_layout_and_a_matching_digest(tmp_path):
    cap = build_capsule([A, B, C], targets=["IRP-2026-09-10-002"])
    out = write_bundle(cap, tmp_path / "capsule")
    names = sorted(p.name for p in out.iterdir())
    assert names == ["IRP-RETURN.txt", "ledger.jsonl", "manifest.json"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["capsule_digest"] == cap.digest
    lines = (out / "ledger.jsonl").read_text().splitlines()
    assert [json.loads(l)["id"] for l in lines] == _ids(cap)
    assert "read-only" in (out / "IRP-RETURN.txt").read_text()
