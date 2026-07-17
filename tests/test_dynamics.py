"""IRP Dynamics: derived typed-edge layer and provenance lens walks.

Covers the properties the SPEC makes normative:
  DYN-R1  only depends_on participates in the walk
  DYN-R2  timestamp heuristic, and the two-node cycle it removes
  DYN-R4  uniform transitions (not weighted by confidence/attestation)
  DYN-R5  dangling mass redistributes through the teleport vector
  DYN-R6  determinism, normalisation, stable ordering
  DYN-I2  analysis never mutates the decisions it reads
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "irp" / "core"))

import dynamics as dyn  # noqa: E402
from commands.graph import _SAMPLE_DECISIONS  # noqa: E402

FOUNDATION = "IRP-2026-01-10-001"   # shared design token system
FIGMA_VARS = "IRP-2026-01-15-002"   # builds on FOUNDATION; FOUNDATION says it "gates" this
RADIX = "IRP-2026-02-01-004"        # builds on FOUNDATION; FOUNDATION says it "gates" this
QUARTERLY = "IRP-2026-04-25-018"    # quarterly review cycle
VERSIONING = "IRP-2026-04-22-017"   # semantic versioning


def _edges():
    return dyn.derive_typed_edges(_SAMPLE_DECISIONS)


def _ids():
    return [d["id"] for d in _SAMPLE_DECISIONS]


def _rel(edges, source, target):
    for e in edges:
        if e["source"] == source and e["target"] == target:
            return e["relation"]
    return None


# ── typed edge derivation ────────────────────────────────────────────────────

def test_forward_reference_is_gates_backward_is_depends_on():
    edges = _edges()
    # The foundation names two later decisions it "gates": forward refs.
    assert _rel(edges, FOUNDATION, FIGMA_VARS) == "gates"
    assert _rel(edges, FOUNDATION, RADIX) == "gates"
    # Those later decisions cite the foundation as an antecedent: backward refs.
    assert _rel(edges, FIGMA_VARS, FOUNDATION) == "depends_on"
    assert _rel(edges, RADIX, FOUNDATION) == "depends_on"


def test_walk_graph_has_no_two_node_cycle():
    """The raw exporter produces 001 -> 002 and 002 -> 001. The typed walk must not."""
    edges = _edges()
    walk = {(e["source"], e["target"]) for e in edges if e["relation"] == dyn.WALK_RELATION}
    for a, b in list(walk):
        assert (b, a) not in walk, f"two-node cycle survived in the walk graph: {a} <-> {b}"


def test_depends_on_subgraph_is_acyclic_by_construction():
    """Every walk edge points strictly backward in time, so no cycle of any length."""
    ts = {d["id"]: d["timestamp"] for d in _SAMPLE_DECISIONS}
    for e in _edges():
        if e["relation"] == dyn.WALK_RELATION:
            assert ts[e["target"]] < ts[e["source"]]


def test_self_and_unknown_references_are_dropped():
    decisions = [
        {"id": "IRP-2026-01-01-001", "timestamp": "2026-01-01T00:00:00Z",
         "why": "cites itself IRP-2026-01-01-001 and a ghost IRP-2099-01-01-999"},
    ]
    assert dyn.derive_typed_edges(decisions) == []


def test_same_timestamp_is_mentions_and_is_excluded_from_the_walk():
    decisions = [
        {"id": "IRP-2026-01-01-001", "timestamp": "2026-01-01T00:00:00Z", "why": "x"},
        {"id": "IRP-2026-01-01-002", "timestamp": "2026-01-01T00:00:00Z",
         "why": "see IRP-2026-01-01-001"},
    ]
    edges = dyn.derive_typed_edges(decisions)
    assert _rel(edges, "IRP-2026-01-01-002", "IRP-2026-01-01-001") == "mentions"
    scores, _ = dyn.personalized_pagerank([d["id"] for d in decisions], edges)
    # No walk edges at all: mass stays uniform.
    assert scores["IRP-2026-01-01-001"] == scores["IRP-2026-01-01-002"]


def test_derivation_is_deterministic_and_sorted():
    assert _edges() == _edges()
    edges = _edges()
    assert edges == sorted(edges, key=lambda e: (e["source"], e["target"]))


# ── the walk ─────────────────────────────────────────────────────────────────

def test_scores_are_a_probability_distribution():
    scores, _ = dyn.personalized_pagerank(_ids(), _edges())
    assert abs(sum(scores.values()) - 1.0) < 1e-9
    assert all(v >= 0 for v in scores.values())


def test_walk_ignores_non_depends_on_relations():
    """Retyping every edge to depends_on must change the result: proof gates are excluded."""
    edges = _edges()
    typed, _ = dyn.personalized_pagerank(_ids(), edges)
    raw = [dict(e, relation=dyn.WALK_RELATION) for e in edges]
    untyped, _ = dyn.personalized_pagerank(_ids(), raw)
    assert typed != untyped
    # The cycle members are inflated by the raw graph.
    assert untyped[FIGMA_VARS] > typed[FIGMA_VARS]
    assert untyped[FOUNDATION] > typed[FOUNDATION]


def test_foundations_surface_the_real_foundations():
    scores, _ = dyn.personalized_pagerank(_ids(), _edges())
    top3 = [nid for nid, _ in dyn.rank(scores)[:3]]
    assert set(top3) == {FOUNDATION, RADIX, FIGMA_VARS}
    assert top3[0] == FOUNDATION


def test_lineage_is_confined_to_actual_antecedents():
    scores, _ = dyn.personalized_pagerank(_ids(), _edges(), seed=QUARTERLY)
    assert scores[QUARTERLY] > 0
    for nid in (VERSIONING, RADIX, FOUNDATION):
        assert scores[nid] > 0, f"{nid} is an antecedent of the seed and should carry mass"
    # A decision on an unrelated branch is not reachable walking backward.
    assert scores["IRP-2026-04-01-012"] == 0


def test_impact_is_the_reverse_of_lineage():
    """Seeded at the foundation, the walk reaches descendants, not antecedents."""
    scores, _ = dyn.personalized_pagerank(_ids(), _edges(), seed=FOUNDATION, reverse=True)
    assert scores[RADIX] > 0
    assert scores[QUARTERLY] > 0
    reached = sum(1 for nid, v in scores.items() if v > 1e-9 and nid != FOUNDATION)
    assert reached >= 10


def test_uniform_transitions_ignore_confidence():
    """DYN-R4: flipping confidence must not move a single score."""
    baseline, _ = dyn.personalized_pagerank(_ids(), _edges())
    flipped = [dict(d, confidence="low") for d in _SAMPLE_DECISIONS]
    scores, _ = dyn.personalized_pagerank(
        [d["id"] for d in flipped], dyn.derive_typed_edges(flipped)
    )
    assert scores == baseline


def test_dangling_node_mass_is_conserved():
    """DYN-R5: a node with no outgoing walk edge must not leak probability."""
    decisions = [
        {"id": "IRP-2026-01-01-001", "timestamp": "2026-01-01T00:00:00Z", "why": "root, cites nobody"},
        {"id": "IRP-2026-01-02-002", "timestamp": "2026-01-02T00:00:00Z", "why": "builds on IRP-2026-01-01-001"},
    ]
    edges = dyn.derive_typed_edges(decisions)
    scores, _ = dyn.personalized_pagerank([d["id"] for d in decisions], edges)
    assert abs(sum(scores.values()) - 1.0) < 1e-9
    assert scores["IRP-2026-01-01-001"] > scores["IRP-2026-01-02-002"]


def test_rank_is_stable_on_ties():
    tied = {"IRP-2026-01-01-002": 0.5, "IRP-2026-01-01-001": 0.5}
    assert [nid for nid, _ in dyn.rank(tied)] == ["IRP-2026-01-01-001", "IRP-2026-01-01-002"]


def test_unknown_seed_raises():
    try:
        dyn.personalized_pagerank(_ids(), _edges(), seed="IRP-2099-01-01-999")
    except KeyError:
        return
    raise AssertionError("expected KeyError for a seed outside the graph")


# ── lens packaging and layer boundary ────────────────────────────────────────

def test_compute_lens_pins_the_snapshot_and_reports_convergence():
    snap = dyn.snapshot_hash(None, _SAMPLE_DECISIONS, demo=True)
    lens = dyn.compute_lens(_SAMPLE_DECISIONS, "foundations", snapshot=snap)
    assert lens["method"] == "personalized_pagerank"
    assert lens["ledger_snapshot_hash"] == snap
    assert lens["edge_policy"] == dyn.EDGE_POLICY
    assert lens["restart_probability"] == 0.15
    assert 0 < lens["convergence"]["iterations"] <= dyn.MAX_ITERATIONS
    assert list(lens["scores"])[0] == FOUNDATION  # already ranked


def test_seeded_lens_requires_a_seed():
    for view in dyn.SEEDED_VIEWS:
        try:
            dyn.compute_lens(_SAMPLE_DECISIONS, view)
        except ValueError:
            continue
        raise AssertionError(f"{view} must require a seed")


def test_analysis_does_not_mutate_the_decisions_it_reads():
    """DYN-I2: Dynamics reads the record, it never writes to it."""
    import copy
    before = copy.deepcopy(_SAMPLE_DECISIONS)
    dyn.compute_lens(_SAMPLE_DECISIONS, "impact", seed=FOUNDATION,
                     snapshot=dyn.snapshot_hash(None, _SAMPLE_DECISIONS, demo=True))
    assert _SAMPLE_DECISIONS == before


def test_snapshot_hash_changes_when_the_input_changes():
    a = dyn.snapshot_hash(None, _SAMPLE_DECISIONS, demo=True)
    mutated = [dict(d) for d in _SAMPLE_DECISIONS]
    mutated[0] = dict(mutated[0], what="something else")
    b = dyn.snapshot_hash(None, mutated, demo=True)
    assert a != b and a.startswith("sha256:")
