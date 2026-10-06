"""Roaming IRP, Cut 1 step 2.1: the Rekadu builder (spec v0.3 §8a, §16.1, §16.3).

A Rekadu (formerly the Context Capsule) is the read side of the AI boundary: a
deterministic, budgeted slice of the ledger that any AI, human or script can
read with only HTTP and files. Same selection and params give byte-identical
bytes, so the rekadu_digest says exactly what a reader was sent.

Fixtures use neutral ids and tags only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam.rekadu import (  # noqa: E402
    REKADU_VERSION,
    RETURN_TEXT,
    RekaduError,
    build_rekadu,
    region_for_surface,
    write_bundle,
)


def _d(id_, what, ts, **rel):
    return {"type": "decision", "id": id_, "what": what, "why": f"Because of {what.lower()}.",
            "timestamp": ts, **rel}


def _ids(rk, form=None):
    return [r["id"] for r in rk.records if form is None or r["_rekadu_form"] == form]


def _rel(rk):
    return {r["id"]: r["relationship_to_target"] for r in rk.manifest["records"]}


def _tokens(rk):
    return (rk.size + 3) // 4  # the size the builder counted against the budget


DISCLOSURE = {"disclosure_id": "de-AAAAAAAAAAAAAAAAAAAAAA", "reader_id": "rd-1", "surface": "claude-code-cloud",
              "scope": "read", "expires": "2026-10-06T13:00:00Z", "identity_assurance": "A0"}

# A small lineage plus noise.
A = _d("IRP-2001-01-02-001", "Direction", "2026-01-02T10:00:00Z")
B = _d("IRP-2001-01-03-001", "Converged", "2026-01-03T12:00:00Z", rests_on=["IRP-2001-01-02-001"])
C = _d("IRP-2001-01-04-001", "Form factor", "2026-01-04T09:00:00Z", rests_on=["IRP-2001-01-03-001"])
ROOT_D = _d("IRP-2001-01-01-001", "Very old root", "2026-01-01T09:00:00Z")
A_ROOTED = {**A, "rests_on": ["IRP-2001-01-01-001"]}
NOISE = _d("IRP-2001-01-05-001", "Unrelated decision", "2026-01-05T09:00:00Z")


# ── Carried over from step 1, under the new name ──

def test_target_plus_two_ancestors():
    rk = build_rekadu([A, B, C, NOISE], targets=["IRP-2001-01-04-001"])
    assert _ids(rk) == ["IRP-2001-01-02-001", "IRP-2001-01-03-001", "IRP-2001-01-04-001"]
    assert all(r["_rekadu_form"] == "full" for r in rk.records)
    assert rk.manifest["pruning"]["applied"] is False
    assert "IRP-2001-01-05-001" not in _ids(rk)


def test_ancestor_depth_is_respected():
    rk = build_rekadu([ROOT_D, A_ROOTED, B, C], targets=["IRP-2001-01-04-001"], ancestor_depth=2)
    assert "IRP-2001-01-01-001" not in _ids(rk)
    deeper = build_rekadu([ROOT_D, A_ROOTED, B, C], targets=["IRP-2001-01-04-001"], ancestor_depth=3)
    assert "IRP-2001-01-01-001" in _ids(deeper)


def test_relationship_labels_in_manifest():
    rel = _rel(build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"]))
    assert rel == {"IRP-2001-01-04-001": "target", "IRP-2001-01-03-001": "rests_on@depth1",
                   "IRP-2001-01-02-001": "rests_on@depth2"}


def test_supersede_head_is_included_so_state_is_never_stale():
    newer = _d("IRP-2001-01-09-001", "Revisited", "2026-01-09T09:00:00Z", supersedes="IRP-2001-01-03-001")
    rk = build_rekadu([A, B, C, newer], targets=["IRP-2001-01-04-001"])
    assert _rel(rk)["IRP-2001-01-09-001"] == "supersede_head"


def test_pinned_records_are_included():
    rk = build_rekadu([A, B, C, NOISE], targets=["IRP-2001-01-04-001"], pinned=["IRP-2001-01-05-001"])
    assert _rel(rk)["IRP-2001-01-05-001"] == "pinned"


def test_records_are_in_causal_order():
    rk = build_rekadu([C, B, A], targets=["IRP-2001-01-04-001"])
    assert _ids(rk) == ["IRP-2001-01-02-001", "IRP-2001-01-03-001", "IRP-2001-01-04-001"]


def test_digest_is_deterministic_and_ignores_volatile_fields():
    a = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"], generated_at="2026-10-05T10:00:00Z")
    b = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"], generated_at="2026-10-06T11:00:00Z",
                     disclosure=DISCLOSURE)
    assert a.digest == b.digest and a.digest.startswith("sha256-")
    assert build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"], ancestor_depth=1).digest != a.digest


def test_legacy_entries_get_a_readable_stub():
    legacy = {"type": "decision", "id": "IRP-2001-01-06-001", "title": "Legacy titled decision",
              "decision": "x" * 900, "timestamp": "2026-01-06T09:00:00Z"}
    child = _d("IRP-2001-01-07-001", "Child", "2026-01-07T09:00:00Z", rests_on=["IRP-2001-01-06-001"])
    grandchild = _d("IRP-2001-01-08-001", "Grandchild", "2026-01-08T09:00:00Z", rests_on=["IRP-2001-01-07-001"])
    full = build_rekadu([legacy, child, grandchild], targets=["IRP-2001-01-08-001"])
    rk = build_rekadu([legacy, child, grandchild], targets=["IRP-2001-01-08-001"], token_budget=_tokens(full) - 100)
    stub = next(r for r in rk.records if r["id"] == "IRP-2001-01-06-001")
    assert stub["_rekadu_form"] == "stub"
    assert stub["what"] == "Legacy titled decision"


def test_unknown_target_is_an_error():
    with pytest.raises(RekaduError, match="IRP-2099"):
        build_rekadu([A], targets=["IRP-2099-01-01-001"])


# ── §16.1 #1: nested relationship edges ──

def test_nested_relationships_are_followed():
    # The shape of a real five-record lineage: a target resting (nested) on three
    # records, two of which rest (nested) on a shared root.
    t = {**_d("IRP-2001-02-05-001", "Gate cleared", "2026-02-05T12:00:00Z"),
         "relationships": {"rests_on": ["IRP-2001-02-02-001", "IRP-2001-02-03-001", "IRP-2001-02-04-001"]}}
    a = _d("IRP-2001-02-02-001", "Form factor", "2026-02-02T09:00:00Z")
    b = {**_d("IRP-2001-02-03-001", "Bundle format", "2026-02-03T09:00:00Z"),
         "relationships": {"rests_on": ["IRP-2001-02-01-001", "IRP-2001-02-02-001"]}}
    c = {**_d("IRP-2001-02-04-001", "Recovery", "2026-02-04T09:00:00Z"),
         "relationships": {"rests_on": ["IRP-2001-02-01-001", "IRP-2001-02-02-001"]}}
    root = _d("IRP-2001-02-01-001", "Converged", "2026-02-01T09:00:00Z")
    rk = build_rekadu([root, a, b, c, t], targets=["IRP-2001-02-05-001"])
    assert len(rk.records) == 5
    assert _rel(rk)["IRP-2001-02-01-001"] == "rests_on@depth2"


# ── §16.1 #2, #6, #11: Class 0 fails closed and is counted honestly ──

SECRET = {**_d("IRP-2001-03-01-001", "Partner terms", "2026-03-01T09:00:00Z"), "tags": ["partner-x"]}
USES_SECRET = _d("IRP-2001-03-02-001", "Plan built on terms", "2026-03-02T09:00:00Z", rests_on=["IRP-2001-03-01-001"])


def test_referenced_class0_ancestor_is_omitted_with_its_id():
    rk = build_rekadu([SECRET, USES_SECRET], targets=["IRP-2001-03-02-001"], class0_tags=["partner-x"])
    assert "IRP-2001-03-01-001" not in _ids(rk)
    assert {"id": "IRP-2001-03-01-001", "reason": "exposure_class"} in [
        {k: o[k] for k in ("id", "reason")} for o in rk.manifest["omitted"]]
    assert rk.manifest["omitted_counts"]["exposure_class"] == 1


def test_reserved_surfaces_are_refused_in_cut1():
    # The EU runtime and managed browsers are reserved (spec §15.3); the EU reader gets its own spec.
    for surface in ("eu-sovereign-runtime", "browser-managed"):
        with pytest.raises(RekaduError, match="reserved"):
            build_rekadu([SECRET, USES_SECRET], targets=["IRP-2001-03-02-001"],
                         surface=surface, class0_tags=["partner-x"])


def test_explicit_class0_target_is_an_error():
    with pytest.raises(RekaduError, match="exposure"):
        build_rekadu([SECRET], targets=["IRP-2001-03-01-001"], class0_tags=["partner-x"])


def test_explicit_exposure_field_marks_class0():
    marked = {**_d("IRP-2001-03-03-001", "Marked", "2026-03-03T10:00:00Z"), "exposure_class": "0"}
    child = _d("IRP-2001-03-04-001", "Child", "2026-03-04T10:00:00Z", rests_on=["IRP-2001-03-03-001"])
    rk = build_rekadu([marked, child], targets=["IRP-2001-03-04-001"])
    assert "IRP-2001-03-03-001" not in _ids(rk)


def test_confidential_fields_mark_class0_but_confidence_does_not():
    noted = {**_d("IRP-2001-03-05-001", "Noted", "2026-03-05T10:00:00Z"), "confidential_note": "x"}
    private = {**_d("IRP-2001-03-06-001", "Private", "2026-03-06T10:00:00Z"), "private_terms": "x"}
    secret = {**_d("IRP-2001-03-07-001", "Secret", "2026-03-07T10:00:00Z"), "Secret_detail": "x"}
    sure = {**_d("IRP-2001-03-08-001", "Sure", "2026-03-08T10:00:00Z"), "confidence": "high"}
    child = _d("IRP-2001-03-09-001", "Child", "2026-03-09T10:00:00Z",
               rests_on=["IRP-2001-03-05-001", "IRP-2001-03-06-001", "IRP-2001-03-07-001", "IRP-2001-03-08-001"])
    rk = build_rekadu([noted, private, secret, sure, child], targets=["IRP-2001-03-09-001"])
    assert set(_ids(rk)) == {"IRP-2001-03-08-001", "IRP-2001-03-09-001"}
    assert rk.manifest["omitted_counts"]["exposure_class"] == 3


def test_class0_supersede_head_is_withheld_and_recorded():
    head = {**_d("IRP-2001-03-10-001", "Revised partner terms", "2026-03-10T09:00:00Z",
                 supersedes="IRP-2001-01-03-001"), "tags": ["partner-x"]}
    rk = build_rekadu([A, B, C, head], targets=["IRP-2001-01-04-001"], class0_tags=["partner-x"])
    assert "IRP-2001-03-10-001" not in _ids(rk)
    assert {"id": "IRP-2001-03-10-001", "reason": "exposure_class",
            "relationship": "supersede_head_of:IRP-2001-01-03-001"} in rk.manifest["omitted"]
    superseded = next(r for r in rk.records if r["id"] == "IRP-2001-01-03-001")
    assert superseded["superseded"] == "withheld"


def test_rule_matched_class0_is_counted_without_its_id():
    rk = build_rekadu([SECRET, NOISE], matched=["IRP-2001-03-01-001", "IRP-2001-01-05-001"],
                      class0_tags=["partner-x"])
    assert _ids(rk) == ["IRP-2001-01-05-001"]
    assert rk.manifest["omitted_counts"]["exposure_class"] == 1
    assert "IRP-2001-03-01-001" not in json.dumps(rk.manifest)


# ── §16.1 #7 and §16.3 #4: region comes from the surface, never a free-text argument ──

def test_region_is_hard_mapped_from_the_surface():
    assert region_for_surface("claude-code-cloud") == "non-eu"
    assert region_for_surface("browser-ephemeral") == "non-eu"
    assert region_for_surface("browser-managed") == "non-eu"
    assert region_for_surface("eu-sovereign-runtime") == "eu"
    with pytest.raises(RekaduError, match="surface"):
        region_for_surface("eu")


def test_free_text_region_is_not_accepted():
    with pytest.raises(TypeError):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], reader_region="eu")  # type: ignore[call-arg]
    with pytest.raises(RekaduError, match="surface"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], surface="my-laptop")


def test_default_surface_fails_closed_to_non_eu():
    rk = build_rekadu([A], targets=["IRP-2001-01-02-001"])
    assert rk.manifest["selection_params"]["reader_region"] == "non-eu"


# ── §16.1 #8: duplicates ──

def test_a_selected_duplicate_id_raises():
    twin = {**A, "what": "Same id, different content"}
    with pytest.raises(RekaduError, match="duplicate"):
        build_rekadu([A, twin, B], targets=["IRP-2001-01-03-001"])


def test_an_unselected_duplicate_is_fine():
    twin = {**NOISE, "what": "Same id, different content"}
    rk = build_rekadu([A, B, NOISE, twin], targets=["IRP-2001-01-03-001"])
    assert _ids(rk) == ["IRP-2001-01-02-001", "IRP-2001-01-03-001"]


# ── §16.1 #9: unresolved references are recorded, never silently dropped ──

def test_unresolved_refs_are_recorded():
    craft = {"type": "craft_event", "id": "IRP-2001-04-01-001", "timestamp": "2026-04-01T09:00:00Z"}
    child = _d("IRP-2001-04-02-001", "Child", "2026-04-02T09:00:00Z",
               rests_on=["IRP-2099-09-09-009", "IRP-2001-04-01-001"])
    rk = build_rekadu([craft, child], targets=["IRP-2001-04-02-001"])
    omitted = {o["id"]: o["reason"] for o in rk.manifest["omitted"]}
    assert omitted == {"IRP-2099-09-09-009": "unknown_id", "IRP-2001-04-01-001": "type_not_in_scope"}
    assert rk.manifest["omitted_counts"]["unknown_id"] == 1
    assert rk.manifest["omitted_counts"]["type_not_in_scope"] == 1


def test_target_of_a_type_out_of_scope_is_an_error():
    craft = {"type": "craft_event", "id": "IRP-2001-04-01-001", "timestamp": "2026-04-01T09:00:00Z"}
    with pytest.raises(RekaduError, match="type"):
        build_rekadu([craft], targets=["IRP-2001-04-01-001"])


# ── §16.1 #10: corrections ──

def test_corrects_is_followed_like_supersedes():
    fix = {"type": "correction", "id": "IRP-2001-05-01-001", "what": "Fixes a figure",
           "timestamp": "2026-05-01T09:00:00Z", "corrects": "IRP-2001-01-03-001"}
    rk = build_rekadu([A, B, C, fix], targets=["IRP-2001-01-04-001"])
    assert _rel(rk)["IRP-2001-05-01-001"] == "supersede_head"


def test_a_correction_can_be_a_target():
    fix = {"type": "correction", "id": "IRP-2001-05-01-001", "what": "Fixes a figure",
           "timestamp": "2026-05-01T09:00:00Z", "corrects": "IRP-2001-01-03-001"}
    rk = build_rekadu([A, B, fix], targets=["IRP-2001-05-01-001"])
    assert _rel(rk)["IRP-2001-01-03-001"] == "corrects@depth1"


# ── §16.1 #3, #4, #5: budget, JCS, checkpoint ──

def test_budget_counts_the_manifest_as_well_as_the_ledger():
    rk = build_rekadu([A, B], targets=["IRP-2001-01-03-001"])
    ledger_only = (len(rk.ledger_bytes) + 3) // 4
    with pytest.raises(RekaduError, match="narrow"):
        build_rekadu([A, B], targets=["IRP-2001-01-03-001"], token_budget=ledger_only + 1)


def test_jcs_canonicalisation():
    rk = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"])
    assert rk.manifest["canonicalization"] == "RFC8785"
    lines = rk.ledger_bytes.decode("utf-8").splitlines()
    assert [l.encode("utf-8") for l in lines] == [canonicalize(r) for r in rk.records]


def test_jcs_equals_the_legacy_form_on_float_free_fixtures():
    legacy = lambda o: json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")  # noqa: E731
    for rec in (A, B, C, SECRET, {**A, "what": "Åsa's beslut, naïve café"}):
        assert canonicalize(rec) == legacy(rec)


def test_checkpoint_is_inside_the_digest():
    ck1 = {"id": "ckpt-1", "strand": "dk-a", "seq": 41, "hash": "sha256-" + "a" * 64, "signed_ts": "2026-10-05T18:00:00Z"}
    ck2 = {**ck1, "seq": 42}
    a = build_rekadu([A, B], targets=["IRP-2001-01-03-001"], checkpoint=ck1)
    b = build_rekadu([A, B], targets=["IRP-2001-01-03-001"], checkpoint=ck2)
    assert a.manifest["checkpoint"] == ck1
    assert a.digest != b.digest


def test_over_budget_demotes_deep_ancestors_to_stubs_first():
    chain = _chain(4)
    target = chain[-1]["id"]
    full = build_rekadu(chain, targets=[target], ancestor_depth=3)
    rk = build_rekadu(chain, targets=[target], ancestor_depth=3, token_budget=_tokens(full) - 50)
    forms = {r["id"]: r["_rekadu_form"] for r in rk.records}
    assert forms[target] == "full"
    assert forms[chain[-2]["id"]] == "full"          # direct basis is never demoted
    assert forms[chain[0]["id"]] == "stub"           # depth 3
    assert rk.manifest["pruning"]["applied"] is True


def test_still_over_budget_drops_deepest_and_says_so():
    chain = _chain(5)
    target = chain[-1]["id"]
    protected_only = build_rekadu(chain, targets=[target], ancestor_depth=1)
    rk = build_rekadu(chain, targets=[target], ancestor_depth=4, token_budget=_tokens(protected_only) + 40)
    assert chain[0]["id"] not in _ids(rk)
    assert rk.manifest["pruning"]["omitted_count"] >= 1
    assert "size" in rk.manifest["pruning"]["reason"]


def test_min_set_over_budget_is_an_error_not_a_silent_truncation():
    chain = _chain(2)
    with pytest.raises(RekaduError, match="narrow"):
        build_rekadu(chain, targets=[chain[-1]["id"]], token_budget=10)


def _chain(n):
    out = []
    for i in range(n):
        rel = {"rests_on": [f"IRP-2001-08-{i:02d}-001"]} if i else {}
        e = _d(f"IRP-2001-08-{i + 1:02d}-001", f"Step {i + 1}", f"2026-08-{i + 1:02d}T09:00:00Z", **rel)
        e["why"] = "x" * 400
        out.append(e)
    return out


# ── §16.1 #12: the rename is complete ──

def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_only_rekadu_names_remain():
    rk = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"])
    assert rk.manifest["rekadu_version"] == REKADU_VERSION == "0.2"
    assert rk.manifest["rekadu_digest"] == rk.digest
    assert not [k for k in _keys(rk.manifest) if "capsule" in k.lower()]
    assert not [k for k in _keys(rk.records) if "capsule" in k.lower()]
    assert "Rekadu" in RETURN_TEXT and "capsule" not in RETURN_TEXT.lower()


def test_manifest_has_the_spec_keys():
    rk = build_rekadu([A, B], targets=["IRP-2001-01-03-001"])
    assert set(rk.manifest) == {"rekadu_version", "canonicalization", "generated_at", "selection_params",
                                "checkpoint", "records", "artefacts", "pruning", "omitted", "omitted_counts",
                                "disclosure", "rekadu_digest"}
    assert set(rk.manifest["selection_params"]) == {"targets", "ancestor_depth", "pinned", "token_budget",
                                                    "byte_budget", "artefact_inline_cap", "reader_region"}
    assert set(rk.manifest["omitted_counts"]) == {"exposure_class", "unknown_id", "type_not_in_scope", "pending_review"}


# ── The bundle on disk ──

def test_bundle_has_the_spec_layout_and_a_matching_digest(tmp_path):
    rk = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"])
    out = write_bundle(rk, tmp_path / "rekadu")
    assert sorted(p.name for p in out.iterdir()) == ["IRP-RETURN.txt", "artefacts", "ledger.jsonl", "manifest.json"]
    assert not list((out / "artefacts").iterdir())
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["rekadu_digest"] == rk.digest
    assert [json.loads(l)["id"] for l in (out / "ledger.jsonl").read_text().splitlines()] == _ids(rk)
    text = (out / "IRP-RETURN.txt").read_text()
    assert "read-only" in text and f"format {REKADU_VERSION}" in text
    assert "Don't commit" in text and "propose, don't overwrite" in text
    assert (out / "manifest.json").read_bytes() == canonicalize(rk.manifest) + b"\n"


# ── Review fixes (adversarial review of step 2.1) ──

def _child_of(*ids, id_="IRP-2001-06-09-001"):
    return _d(id_, "Child", "2026-06-09T09:00:00Z", rests_on=list(ids))


@pytest.mark.parametrize("tags", ["partner-x", " Partner-X ", ["  partner-x"], ("partner-x",)])
def test_class0_tag_shapes_fail_closed(tags):
    rec = {**_d("IRP-2001-06-01-001", "Terms", "2026-06-01T09:00:00Z"), "tags": tags}
    rk = build_rekadu([rec, _child_of("IRP-2001-06-01-001")], targets=["IRP-2001-06-09-001"],
                      class0_tags=["partner-x"])
    assert "IRP-2001-06-01-001" not in _ids(rk)


def test_unreadable_tags_shape_counts_as_class0():
    rec = {**_d("IRP-2001-06-01-001", "Terms", "2026-06-01T09:00:00Z"), "tags": {"name": "anything"}}
    rk = build_rekadu([rec, _child_of("IRP-2001-06-01-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-06-01-001" not in _ids(rk)


def test_class0_tags_given_as_one_string():
    rk = build_rekadu([SECRET, USES_SECRET], targets=["IRP-2001-03-02-001"], class0_tags="partner-x")
    assert "IRP-2001-03-01-001" not in _ids(rk)


@pytest.mark.parametrize("field", [{"exposure_class": 0}, {"Exposure_Class": "0"}, {"exposure_class": " 0 "}])
def test_exposure_class_variants(field):
    rec = {**_d("IRP-2001-06-02-001", "Marked", "2026-06-02T09:00:00Z"), **field}
    rk = build_rekadu([rec, _child_of("IRP-2001-06-02-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-06-02-001" not in _ids(rk)


def test_nested_confidential_field_is_class0():
    rec = {**_d("IRP-2001-06-03-001", "Nested", "2026-06-03T09:00:00Z"), "details": {"confidential_note": "x"}}
    rk = build_rekadu([rec, _child_of("IRP-2001-06-03-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-06-03-001" not in _ids(rk)


def test_extra_prefixes_add_to_the_mandatory_ones():
    noted = {**_d("IRP-2001-06-04-001", "Noted", "2026-06-04T09:00:00Z"), "confidential_note": "x"}
    internal = {**_d("IRP-2001-06-05-001", "Internal", "2026-06-05T09:00:00Z"), "internal_memo": "x"}
    child = _child_of("IRP-2001-06-04-001", "IRP-2001-06-05-001")
    rk = build_rekadu([noted, internal, child], targets=["IRP-2001-06-09-001"], class0_field_prefixes=["internal"])
    assert _ids(rk) == ["IRP-2001-06-09-001"]


def test_disclosure_must_match_the_surface():
    with pytest.raises(RekaduError, match="surface"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], surface="browser-ephemeral", disclosure=DISCLOSURE)
    with pytest.raises(RekaduError, match="disclosure"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], disclosure={**DISCLOSURE, "extra": 1})


def _counts(rk):
    return rk.manifest["omitted_counts"]["exposure_class"]


def test_exposure_count_is_per_record_not_per_reference():
    # One Class 0 record that is both rule-matched and referenced counts once.
    rk = build_rekadu([SECRET, USES_SECRET], targets=["IRP-2001-03-02-001"], matched=["IRP-2001-03-01-001"],
                      class0_tags=["partner-x"])
    assert _counts(rk) == 1
    # One Class 0 head replacing two selected records counts once and explains both.
    x1 = _d("IRP-2001-06-11-001", "One", "2026-06-11T09:00:00Z")
    x2 = _d("IRP-2001-06-12-001", "Two", "2026-06-12T09:00:00Z", rests_on=["IRP-2001-06-11-001"])
    head = {**_d("IRP-2001-06-13-001", "Head", "2026-06-13T09:00:00Z",
                 supersedes=["IRP-2001-06-11-001", "IRP-2001-06-12-001"]), "tags": ["partner-x"]}
    rk = build_rekadu([x1, x2, head], targets=["IRP-2001-06-12-001"], class0_tags=["partner-x"])
    assert _counts(rk) == 1
    rels = {o["relationship"] for o in rk.manifest["omitted"] if o.get("relationship")}
    assert rels == {"supersede_head_of:IRP-2001-06-11-001", "supersede_head_of:IRP-2001-06-12-001"}


def test_head_row_is_written_even_when_the_head_was_already_omitted_as_an_ancestor():
    x2 = _d("IRP-2001-06-12-001", "Two", "2026-06-12T09:00:00Z")
    head = {**_d("IRP-2001-06-18-001", "Head", "2026-06-18T09:00:00Z", supersedes="IRP-2001-06-12-001"),
            "tags": ["partner-x"]}
    t = _d("IRP-2001-06-19-001", "Target", "2026-06-19T09:00:00Z", rests_on=["IRP-2001-06-12-001", "IRP-2001-06-18-001"])
    rk = build_rekadu([x2, head, t], targets=["IRP-2001-06-19-001"], class0_tags=["partner-x"])
    assert {"id": "IRP-2001-06-18-001", "reason": "exposure_class",
            "relationship": "supersede_head_of:IRP-2001-06-12-001"} in rk.manifest["omitted"]
    assert _counts(rk) == 1


def test_class0_ancestor_id_is_hidden_when_its_only_referrer_is_trimmed():
    secret = {**_d("IRP-2001-08-00-001", "Terms", "2026-08-00T09:00:00Z"), "tags": ["partner-x"]}
    chain = _chain(4)
    chain[0]["rests_on"] = ["IRP-2001-08-00-001"]
    target = chain[-1]["id"]
    full = build_rekadu([secret] + chain, targets=[target], ancestor_depth=4, class0_tags=["partner-x"])
    assert "IRP-2001-08-00-001" in [o["id"] for o in full.manifest["omitted"]]
    rk = build_rekadu([secret] + chain, targets=[target], ancestor_depth=4, class0_tags=["partner-x"],
                      token_budget=_tokens(full) - 50)
    assert next(r for r in rk.records if r["id"] == chain[0]["id"])["_rekadu_form"] == "stub"
    assert "IRP-2001-08-00-001" not in json.dumps(rk.manifest)
    assert _counts(rk) == 1


def test_class0_record_inside_a_supersede_chain_is_counted():
    s1 = {**_d("IRP-2001-06-21-001", "Middle", "2026-06-21T09:00:00Z", supersedes="IRP-2001-01-03-001"),
          "tags": ["partner-x"]}
    s2 = _d("IRP-2001-06-22-001", "Newest", "2026-06-22T09:00:00Z", supersedes="IRP-2001-06-21-001")
    rk = build_rekadu([A, B, C, s1, s2], targets=["IRP-2001-01-04-001"], class0_tags=["partner-x"])
    assert _rel(rk)["IRP-2001-06-22-001"] == "supersede_head"
    assert "IRP-2001-06-21-001" not in _ids(rk)
    assert _counts(rk) == 1


def test_pinned_record_is_never_pruned_even_when_it_is_also_a_deep_ancestor():
    chain = _chain(4)
    target = chain[-1]["id"]
    full = build_rekadu(chain, targets=[target], ancestor_depth=3, pinned=[chain[0]["id"]])
    rk = build_rekadu(chain, targets=[target], ancestor_depth=3, pinned=[chain[0]["id"]],
                      token_budget=_tokens(full) - 50)
    assert next(r for r in rk.records if r["id"] == chain[0]["id"])["_rekadu_form"] == "full"


def test_head_already_selected_as_an_ancestor_is_never_pruned():
    chain = _chain(4)
    target = chain[-1]["id"]
    # chain[1] supersedes chain[0]; chain[1] is a depth-2 ancestor of the target and the head of chain[0].
    chain[1]["supersedes"] = chain[0]["id"]
    full = build_rekadu(chain, targets=[target], ancestor_depth=3)
    rk = build_rekadu(chain, targets=[target], ancestor_depth=3, token_budget=_tokens(full) - 50)
    assert next(r for r in rk.records if r["id"] == chain[1]["id"])["_rekadu_form"] == "full"


def test_target_order_does_not_change_the_digest():
    acc = _d("IRP-2001-06-30-001", "Accepted", "2026-06-30T09:00:00Z")
    t1 = _d("IRP-2001-07-01-001", "T1", "2026-07-01T09:00:00Z", rests_on=["IRP-2001-06-30-001"])
    t2 = _d("IRP-2001-07-02-001", "T2", "2026-07-02T09:00:00Z", accepts=["IRP-2001-06-30-001"])
    a = build_rekadu([acc, t1, t2], targets=["IRP-2001-07-01-001", "IRP-2001-07-02-001"])
    b = build_rekadu([acc, t1, t2], targets=["IRP-2001-07-02-001", "IRP-2001-07-01-001"])
    assert a.digest == b.digest


def test_a_correction_never_hides_a_newer_decision():
    newer = _d("IRP-2001-07-03-001", "Replaced", "2026-07-03T09:00:00Z", supersedes="IRP-2001-01-03-001")
    fix = {"type": "correction", "id": "IRP-2001-07-04-001", "what": "Fixes a figure",
           "timestamp": "2026-07-04T09:00:00Z", "corrects": "IRP-2001-01-03-001"}
    rk = build_rekadu([A, B, C, newer, fix], targets=["IRP-2001-01-04-001"])
    rel = _rel(rk)
    assert rel["IRP-2001-07-03-001"] == "supersede_head" and rel["IRP-2001-07-04-001"] == "supersede_head"


def test_nested_supersedes_is_followed_for_heads():
    newer = {**_d("IRP-2001-07-05-001", "Replaced", "2026-07-05T09:00:00Z"),
             "relationships": {"supersedes": ["IRP-2001-01-03-001"]}}
    rk = build_rekadu([A, B, C, newer], targets=["IRP-2001-01-04-001"])
    assert _rel(rk)["IRP-2001-07-05-001"] == "supersede_head"


def test_unselected_duplicates_drive_heads_the_same_whatever_the_ledger_order():
    newer = _d("IRP-2001-07-06-001", "Replaced", "2026-07-06T09:00:00Z", supersedes="IRP-2001-01-03-001")
    dup_a = _d("IRP-2001-07-07-001", "Dup one", "2026-07-07T09:00:00Z")
    dup_b = _d("IRP-2001-07-07-001", "Dup two", "2026-07-07T09:00:00Z", supersedes="IRP-2001-07-06-001")
    for ledger in ([A, B, C, newer, dup_a, dup_b], [A, B, C, newer, dup_b, dup_a]):
        with pytest.raises(RekaduError, match="duplicate"):
            build_rekadu(ledger, targets=["IRP-2001-01-04-001"])


def test_unresolved_refs_of_pinned_records_are_recorded():
    pin = _d("IRP-2001-07-08-001", "Pinned", "2026-07-08T09:00:00Z", rests_on=["IRP-2099-01-01-001"])
    rk = build_rekadu([A, B, pin], targets=["IRP-2001-01-03-001"], pinned=["IRP-2001-07-08-001"])
    assert {"id": "IRP-2099-01-01-001", "reason": "unknown_id"} in rk.manifest["omitted"]


def test_ancestor_depth_range():
    for bad in (0, 5, -1):
        with pytest.raises(RekaduError, match="ancestor_depth"):
            build_rekadu([A], targets=["IRP-2001-01-02-001"], ancestor_depth=bad)


def test_volatile_fields_never_change_the_selection():
    chain = _chain(4)
    target = chain[-1]["id"]
    full = build_rekadu(chain, targets=[target], ancestor_depth=3)
    budget = _tokens(full)  # tight: a bigger disclosure must not tip it into pruning
    a = build_rekadu(chain, targets=[target], ancestor_depth=3, token_budget=budget)
    b = build_rekadu(chain, targets=[target], ancestor_depth=3, token_budget=budget, disclosure=DISCLOSURE,
                     generated_at="2026-10-06T09:00:00Z")
    assert a.digest == b.digest


def test_an_oversized_disclosure_is_refused():
    with pytest.raises(RekaduError, match="disclosure"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], disclosure={**DISCLOSURE, "reader_id": "rd-" + "x" * 2000})


def test_written_bundle_stays_within_the_byte_budget(tmp_path):
    chain = _chain(5)
    target = chain[-1]["id"]
    full = build_rekadu(chain, targets=[target], ancestor_depth=4)
    budget = full.size - 300
    rk = build_rekadu(chain, targets=[target], ancestor_depth=4, byte_budget=budget, token_budget=10**6,
                      disclosure=DISCLOSURE, generated_at="2026-10-06T09:00:00Z")
    out = write_bundle(rk, tmp_path / "rekadu")
    assert (out / "manifest.json").stat().st_size + (out / "ledger.jsonl").stat().st_size <= budget
    assert rk.manifest["pruning"]["applied"] is True


def test_digest_follows_the_spec_formula_and_matches_the_legacy_form():
    rk = build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"], disclosure=DISCLOSURE)
    stable = {k: v for k, v in rk.manifest.items() if k not in ("generated_at", "disclosure", "rekadu_digest")}
    import hashlib
    jcs = "sha256-" + hashlib.sha256(canonicalize(stable) + b"\n" + rk.ledger_bytes).hexdigest()
    legacy = lambda o: json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")  # noqa: E731
    legacy_ledger = b"".join(legacy(r) + b"\n" for r in rk.records)
    old = "sha256-" + hashlib.sha256(legacy(stable) + b"\n" + legacy_ledger).hexdigest()
    assert rk.digest == jcs == old


def test_checkpoint_must_have_the_spec_shape():
    with pytest.raises(RekaduError, match="checkpoint"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], checkpoint={"seq": 1})


def test_post_build_assertion_catches_a_class0_record():
    from irp.roam.rekadu import _assert_no_class0
    leaked = [{**SECRET, "_rekadu_form": "full"}]
    with pytest.raises(RekaduError, match="Class 0"):
        _assert_no_class0(leaked, {"partner-x"}, ("confidential", "private", "secret"), set())
    with pytest.raises(RekaduError, match="Class 0"):
        _assert_no_class0([{"id": "IRP-2001-03-01-001", "what": "x", "_rekadu_form": "stub"}],
                          set(), ("confidential",), {"IRP-2001-03-01-001"})


def test_importing_rekadu_does_not_load_the_jcs_library():
    import subprocess
    code = "import sys, irp.roam.rekadu; print('rfc8785' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ── Review round 2 ──

@pytest.mark.parametrize("field", [{"exposure_class": 0.0}, {"exposure_class": False}, {"exposure_class": ["0"]},
                                   {"exposure-class": "0"}, {"meta": {"exposure_class": "0"}}])
def test_more_exposure_class_shapes_fail_closed(field):
    rec = {**_d("IRP-2001-09-01-001", "Marked", "2026-09-01T09:00:00Z"), **field}
    rk = build_rekadu([rec, _child_of("IRP-2001-09-01-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-09-01-001" not in _ids(rk)


def test_a_non_zero_exposure_class_is_not_class0():
    rec = {**_d("IRP-2001-09-01-001", "Class 2a", "2026-09-01T09:00:00Z"), "exposure_class": "2a"}
    rk = build_rekadu([rec, _child_of("IRP-2001-09-01-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-09-01-001" in _ids(rk)


@pytest.mark.parametrize("field", [{"tags": "partner-x, other"}, {"tags": ["partner-x,other"]},
                                   {"tags": [["partner-x"]]}, {"tags": [{"name": "partner-x"}]},
                                   {"Tags": ["partner-x"]}, {"meta": {"tags": ["partner-x"]}}])
def test_more_tag_shapes_fail_closed(field):
    rec = {**_d("IRP-2001-09-02-001", "Terms", "2026-09-02T09:00:00Z"), **field}
    rk = build_rekadu([rec, _child_of("IRP-2001-09-02-001")], targets=["IRP-2001-06-09-001"],
                      class0_tags=["partner-x"])
    assert "IRP-2001-09-02-001" not in _ids(rk)


def test_a_supersede_cycle_is_an_error_not_hidden_currency():
    p = _d("IRP-2001-09-03-001", "P", "2026-09-03T09:00:00Z", supersedes="IRP-2001-09-04-001")
    q = _d("IRP-2001-09-04-001", "Q", "2026-09-04T09:00:00Z", supersedes="IRP-2001-09-03-001")
    t = _d("IRP-2001-09-05-001", "T", "2026-09-05T09:00:00Z")
    p["supersedes"] = ["IRP-2001-09-04-001", "IRP-2001-09-05-001"]
    with pytest.raises(RekaduError, match="cycle"):
        build_rekadu([p, q, t], targets=["IRP-2001-09-05-001"])


def test_odd_edge_values_elsewhere_in_the_ledger_are_ignored():
    odd = {**_d("IRP-2001-09-06-001", "Odd", "2026-09-06T09:00:00Z"), "corrects": True, "supersedes": 1}
    rk = build_rekadu([A, B, odd], targets=["IRP-2001-01-03-001"])
    assert _ids(rk) == ["IRP-2001-01-02-001", "IRP-2001-01-03-001"]


@pytest.mark.parametrize("kwargs", [{"class0_tags": 5}, {"targets": 7}, {"pinned": [1]}, {"matched": {"a": 1}}])
def test_bad_argument_types_raise_rekadu_error(kwargs):
    base = {"targets": ["IRP-2001-01-02-001"]}
    with pytest.raises(RekaduError):
        build_rekadu([A], **{**base, **kwargs})


@pytest.mark.parametrize("kwargs", [{"ancestor_depth": True}, {"token_budget": 10.5}, {"byte_budget": None},
                                    {"artefact_inline_cap": 0}, {"token_budget": "8000"}])
def test_numeric_parameters_must_be_positive_ints(kwargs):
    with pytest.raises(RekaduError):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], **kwargs)


def test_values_jcs_cannot_hold_raise_a_named_rekadu_error():
    big = {**_d("IRP-2001-09-07-001", "Big", "2026-09-07T09:00:00Z"), "ns": 2 ** 60}
    with pytest.raises(RekaduError, match="IRP-2001-09-07-001"):
        build_rekadu([big], targets=["IRP-2001-09-07-001"])


@pytest.mark.parametrize("disclosure", [{}, {"surface": "claude-code-cloud"},
                                        {**DISCLOSURE, "scope": "propose"},
                                        {**DISCLOSURE, "expires": "tomorrow"}])
def test_disclosure_must_be_the_full_projection(disclosure):
    with pytest.raises(RekaduError, match="disclosure"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], disclosure=disclosure)


@pytest.mark.parametrize("generated_at", ["2026-10-06", 1760000000, "2026-10-06T09:00:00+02:00"])
def test_generated_at_must_be_a_utc_timestamp(generated_at):
    with pytest.raises(RekaduError, match="generated_at"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], generated_at=generated_at)


@pytest.mark.parametrize("change", [{"seq": -1}, {"seq": True}, {"hash": "sha256-XYZ"}, {"signed_ts": "now"},
                                    {"id": "x-1"}, {"strand": "rk-1"}])
def test_checkpoint_values_follow_the_formats(change):
    ck = {"id": "ckpt-1", "strand": "dk-a", "seq": 41, "hash": "sha256-" + "a" * 64, "signed_ts": "2026-10-05T18:00:00Z"}
    with pytest.raises(RekaduError, match="checkpoint"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], checkpoint={**ck, **change})


def test_pruning_flag_is_only_set_when_something_was_pruned():
    rk = build_rekadu([A, B], targets=["IRP-2001-01-03-001"])
    tight = build_rekadu([A, B], targets=["IRP-2001-01-03-001"], byte_budget=rk.size)
    assert tight.manifest["pruning"] == {"applied": False, "omitted_count": 0, "reason": None}
    # The budget value is itself in the manifest, so compare at the same digit count.
    with pytest.raises(RekaduError, match="narrow"):
        build_rekadu([A, B], targets=["IRP-2001-01-03-001"], byte_budget=tight.size - 1)


def test_pending_review_records_are_held_back_and_counted_without_ids():
    rk = build_rekadu([A, B, C, NOISE], targets=["IRP-2001-01-04-001"], matched=["IRP-2001-01-05-001"],
                      pending_review=["IRP-2001-01-05-001", "IRP-2001-01-02-001"])
    assert "IRP-2001-01-05-001" not in json.dumps(rk.manifest)
    assert "IRP-2001-01-02-001" not in _ids(rk)
    assert rk.manifest["omitted_counts"]["pending_review"] == 2
    with pytest.raises(RekaduError, match="review"):
        build_rekadu([A, B, C], targets=["IRP-2001-01-04-001"], pending_review=["IRP-2001-01-04-001"])


def test_write_bundle_refuses_a_folder_that_is_not_empty(tmp_path):
    rk = build_rekadu([A, B], targets=["IRP-2001-01-03-001"])
    out = tmp_path / "rekadu"
    (out / "artefacts").mkdir(parents=True)
    (out / "artefacts" / "stale.bin").write_bytes(b"old")
    with pytest.raises(RekaduError, match="empty"):
        write_bundle(rk, out)


# ── Review round 3 ──

@pytest.mark.parametrize("record_tags,class0", [
    (["acme corp"], ["acme corp"]), ("acme corp", "acme corp"), (["Acme-Corp"], ["acme corp"]),
    (["acme", "corp"], ["acme corp"]), (["beta"], "alpha, beta"), (["acme.io"], ["acme.io"]),
])
def test_multi_word_and_listed_class0_tags_match(record_tags, class0):
    rec = {**_d("IRP-2001-10-01-001", "Terms", "2026-10-01T09:00:00Z"), "tags": record_tags}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-01-001")], targets=["IRP-2001-06-09-001"], class0_tags=class0)
    assert "IRP-2001-10-01-001" not in _ids(rk)


def test_unrelated_tags_still_pass():
    rec = {**_d("IRP-2001-10-02-001", "Public", "2026-10-02T09:00:00Z"), "tags": ["roadmap", "public-safe"]}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-02-001")], targets=["IRP-2001-06-09-001"],
                      class0_tags=["acme corp", "partner-x"])
    assert "IRP-2001-10-02-001" in _ids(rk)


@pytest.mark.parametrize("value", ["class-0", "0.0", "C0", "zero", "０", "00", "unknown"])
def test_unrecognised_exposure_strings_fail_closed(value):
    rec = {**_d("IRP-2001-10-03-001", "Marked", "2026-10-03T09:00:00Z"), "exposure_class": value}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-03-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-10-03-001" not in _ids(rk)


@pytest.mark.parametrize("value", ["2a", "2B", "class 2b", "1", 2, None])
def test_known_non_zero_exposure_classes_pass(value):
    rec = {**_d("IRP-2001-10-04-001", "Marked", "2026-10-04T09:00:00Z"), "exposure_class": value}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-04-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-10-04-001" in _ids(rk)


@pytest.mark.parametrize("field", [{"exposureClass": 0}, {"exposure class": "0"}, {"exposure.class": "0"},
                                   {"_secret_note": "x"}, {"PrivateTerms": "x"}, {"t": ({"confidential": 1},)}])
def test_key_spellings_and_tuples_fail_closed(field):
    rec = {**_d("IRP-2001-10-05-001", "Marked", "2026-10-05T09:00:00Z"), **field}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-05-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-10-05-001" not in _ids(rk)


def test_non_json_containers_fail_closed():
    rec = {**_d("IRP-2001-10-06-001", "Odd", "2026-10-06T09:00:00Z"), "extra": {"a", "b"}}
    rk = build_rekadu([rec, _child_of("IRP-2001-10-06-001")], targets=["IRP-2001-06-09-001"])
    assert "IRP-2001-10-06-001" not in _ids(rk)


@pytest.mark.parametrize("ts", ["2026-10-06T09:00:00Z\n", "２026-10-06T09:00:00Z"])
def test_timestamps_are_exact_ascii(ts):
    with pytest.raises(RekaduError, match="generated_at"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], generated_at=ts)


def test_write_bundle_refuses_a_file_path(tmp_path):
    rk = build_rekadu([A], targets=["IRP-2001-01-02-001"])
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(RekaduError, match="folder"):
        write_bundle(rk, f)


# ── Review round 4 ──

@pytest.mark.parametrize("reviewed,record_tag", [
    ("Åsa Exempel", "asa-exempel"), ("Göta Testbolag", "gota-testbolag"), ("Ödby Demo", "odby-demo"),
    ("Øvik Prov", "ovik-prov"), ("Musterstraße", "musterstrasse"), ("gota", "Göta"),
])
def test_diacritics_fold_so_spellings_match(reviewed, record_tag):
    rec = {**_d("IRP-2001-11-01-001", "Terms", "2026-11-01T09:00:00Z"), "tags": [record_tag]}
    rk = build_rekadu([rec, _child_of("IRP-2001-11-01-001")], targets=["IRP-2001-06-09-001"], class0_tags=[reviewed])
    assert "IRP-2001-11-01-001" not in _ids(rk)


def test_a_reviewed_entry_that_folds_to_nothing_is_refused():
    with pytest.raises(RekaduError, match="class0_tags"):
        build_rekadu([A], targets=["IRP-2001-01-02-001"], class0_tags=["Тест"])


def test_a_record_tag_that_folds_to_nothing_fails_closed():
    rec = {**_d("IRP-2001-11-02-001", "Terms", "2026-11-02T09:00:00Z"), "tags": ["тест-2026", "ok"]}
    rk = build_rekadu([rec, _child_of("IRP-2001-11-02-001")], targets=["IRP-2001-06-09-001"], class0_tags=["acme"])
    assert "IRP-2001-11-02-001" in _ids(rk)  # has ASCII letters left after folding ("2026"), so it's readable
    rec2 = {**rec, "tags": ["тест"]}
    rk2 = build_rekadu([rec2, _child_of("IRP-2001-11-02-001")], targets=["IRP-2001-06-09-001"], class0_tags=["acme"])
    assert "IRP-2001-11-02-001" not in _ids(rk2)


def test_prefixes_fold_too():
    rec = {**_d("IRP-2001-11-03-001", "Note", "2026-11-03T09:00:00Z"), "Förtrolig_anteckning": "x"}
    rk = build_rekadu([rec, _child_of("IRP-2001-11-03-001")], targets=["IRP-2001-06-09-001"],
                      class0_field_prefixes=["förtrolig"])
    assert "IRP-2001-11-03-001" not in _ids(rk)


def test_bundle_is_private_to_the_owner(tmp_path):
    import stat
    rk = build_rekadu([A, B], targets=["IRP-2001-01-03-001"])
    out = write_bundle(rk, tmp_path / "rekadu")
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "artefacts").stat().st_mode) == 0o700
    for name in ("manifest.json", "ledger.jsonl", "IRP-RETURN.txt"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600, name


# ── Review round 5 ──

@pytest.mark.parametrize("tags", [["launch", chr(0x1F680)], [chr(0x2705)], [chr(0x2014)], ["ok", chr(0x2192)]])
def test_symbol_only_tags_do_not_block(tags):
    rec = {**_d("IRP-2001-12-01-001", "Launch", "2026-12-01T09:00:00Z"), "tags": tags}
    rk = build_rekadu([rec], targets=["IRP-2001-12-01-001"], class0_tags=["acme"])
    assert _ids(rk) == ["IRP-2001-12-01-001"]
