"""IRP Dynamics: derived provenance analysis over the decision graph.

    Layer boundary (SPEC "IRP Dynamics", ledger IRP-2026-07-16-005):
      IRP Protocol      canonical decision records (append-only ledger)
      IRP Attestation   integrity, attribution, verification of those records
      IRP Dynamics      recomputable analysis derived from the records (here)

Invariants this module must not break:
  - DYN-I1  Everything here is recomputable from a named ledger snapshot.
            Deleting .irp/derived/ loses no evidence and no history.
  - DYN-I2  Nothing here is evidence. Nothing is written to .irp/ledger.jsonl.
  - DYN-I3  Nothing here decides. It ranks, it does not approve or confirm.
  - DYN-I4  Every artifact carries the snapshot hash it was computed from.

Design rules:
  - No new schema. The ledger record format is unchanged.
  - No dependencies. Pure-Python power iteration.
  - Deterministic. Identical input bytes give identical scores.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IRP_ID_RE = re.compile(r"\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b")

EDGE_LAYER_VERSION = "irp-dynamics-edges/0.1"
ANALYSIS_VERSION = "irp-dynamics-analysis/0.1"
EDGE_POLICY = "depends_on_only; timestamp-heuristic edges/0.1"

DEFAULT_ALPHA = 0.85
EPSILON = 1e-9
MAX_ITERATIONS = 200

WALK_RELATION = "depends_on"
STRUCTURE_VIEW = "structure"
LENS_VIEWS = ("foundations", "lineage", "impact")
SEEDED_VIEWS = ("lineage", "impact")
ALL_VIEWS = (STRUCTURE_VIEW,) + LENS_VIEWS


# ── snapshot pinning (DYN-I4) ────────────────────────────────────────────────

def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot_hash(irp_dir: Path | None, decisions: list[dict[str, Any]], demo: bool = False) -> str:
    """Pin the exact input this analysis was computed from.

    For a real ledger this is the SHA-256 of the ledger bytes on disk. For
    --demo there is no ledger, so it is the hash of the canonical sample.
    """
    if not demo and irp_dir is not None:
        path = irp_dir / "ledger.jsonl"
        if path.exists():
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return "sha256:" + hashlib.sha256(_canonical(decisions)).hexdigest()


def edge_layer_hash(edges: list[dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(edges)).hexdigest()


# ── derived typed-edge layer ─────────────────────────────────────────────────

def _timestamp(entry: dict[str, Any]) -> str | None:
    ts = entry.get("timestamp")
    return ts if isinstance(ts, str) and ts else None


def derive_typed_edges(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive typed provenance edges from `why` text plus timestamps.

    PoC heuristic (DYN-R2), conservative and deterministic:
        reference to an EARLIER decision -> depends_on (edge points at the antecedent)
        reference to a LATER decision    -> gates
        same or unparseable timestamp    -> mentions

    Why this matters: the raw exporter turns every IRP id in `why` into one
    undifferentiated edge, so a foundation that writes "gates IRP-...-002" and
    the later decision that writes "builds on IRP-...-001" produce a two-node
    cycle. A random walk circulates probability inside that cycle forever and
    inflates both members. Typing the edges and walking only `depends_on`
    (DYN-R1) leaves exactly one real edge per dependence, and the cycle cannot
    form: every walk edge points strictly backward in time, so the depends_on
    subgraph is acyclic by construction.

    Known limits (DYN-R2): backward references that are really implements,
    supersedes or rejects are all typed depends_on here, genuinely
    forward-looking dependence is mistyped, and honest timestamps are assumed.
    The durable answer is an optional structured `refs` field in the record
    format, which is a future protocol RFC and out of scope. `why` stays primary.
    """
    by_id = {d["id"]: d for d in decisions if d.get("id")}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for d in decisions:
        did = d.get("id")
        if not did:
            continue
        d_ts = _timestamp(d)
        for ref in sorted(set(IRP_ID_RE.findall(d.get("why") or ""))):
            if ref == did or ref not in by_id:
                continue
            key = (did, ref)
            if key in seen:
                continue
            seen.add(key)

            r_ts = _timestamp(by_id[ref])
            if d_ts and r_ts and r_ts < d_ts:
                relation = "depends_on"
            elif d_ts and r_ts and r_ts > d_ts:
                relation = "gates"
            else:
                relation = "mentions"

            edges.append({
                "source": did,
                "target": ref,
                "relation": relation,
                "derivation": "heuristic",
                "confidence": 1.0,
            })

    edges.sort(key=lambda e: (e["source"], e["target"]))
    return edges


def build_edge_layer(decisions: list[dict[str, Any]], snapshot: str, demo: bool = False) -> dict[str, Any]:
    edges = derive_typed_edges(decisions)
    return {
        "edge_layer_version": EDGE_LAYER_VERSION,
        "ledger_snapshot_hash": snapshot,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "derivation": "timestamp-heuristic",
        "demo": bool(demo),
        "edges": edges,
    }


def relation_counts(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in edges:
        counts[e["relation"]] = counts.get(e["relation"], 0) + 1
    return counts


# ── random walk with restart (personalized PageRank) ─────────────────────────

def personalized_pagerank(
    node_ids: list[str],
    edges: list[dict[str, Any]],
    seed: str | None = None,
    alpha: float = DEFAULT_ALPHA,
    reverse: bool = False,
) -> tuple[dict[str, float], int]:
    """Random walk with restart over the depends_on graph.

        r = alpha * P_transpose * r + (1 - alpha) * e

    Transitions are UNIFORM: P[i][j] = 1 / outdegree(i) (DYN-R4). Scores are
    deliberately NOT weighted by confidence, attestation, record age, or the
    length of `why`. Influence is not confidence: a decision recorded as
    tentative can still be structurally load-bearing. Attestation proves
    properties of the record (integrity, attribution), never its importance.

    seed=None gives the uniform teleport vector (the foundations lens).
    seed=<id> concentrates restart mass on one node (lineage and impact).
    reverse=True walks the reversed graph (impact / blast radius).

    Returns (scores, iterations). Scores are probabilities summing to 1.
    """
    nodes = list(node_ids)
    n = len(nodes)
    if n == 0:
        return {}, 0
    index = {nid: i for i, nid in enumerate(nodes)}

    out: list[list[int]] = [[] for _ in range(n)]
    for e in edges:
        if e.get("relation") != WALK_RELATION:  # DYN-R1
            continue
        s, t = e["source"], e["target"]
        if s not in index or t not in index:
            continue
        if reverse:
            s, t = t, s
        out[index[s]].append(index[t])

    tele = [0.0] * n
    if seed is None:
        for i in range(n):
            tele[i] = 1.0 / n
    else:
        if seed not in index:
            raise KeyError(seed)
        tele[index[seed]] = 1.0

    r = list(tele)
    iterations = 0
    for iterations in range(1, MAX_ITERATIONS + 1):
        nxt = [(1.0 - alpha) * tele[i] for i in range(n)]
        dangling = 0.0
        for i in range(n):
            if not out[i]:
                dangling += r[i]
                continue
            share = alpha * r[i] / len(out[i])
            for j in out[i]:
                nxt[j] += share
        if dangling:
            # Dangling mass redistributes through the active teleport vector (DYN-R5).
            for i in range(n):
                nxt[i] += alpha * dangling * tele[i]
        delta = sum(abs(nxt[i] - r[i]) for i in range(n))
        r = nxt
        if delta < EPSILON:
            break

    total = sum(r)
    if total:
        r = [v / total for v in r]
    return {nodes[i]: r[i] for i in range(n)}, iterations


def rank(scores: dict[str, float]) -> list[tuple[str, float]]:
    """Stable ordering: score descending, then id ascending (DYN-R6)."""
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# ── lens computation ─────────────────────────────────────────────────────────

def compute_lens(
    decisions: list[dict[str, Any]],
    view: str,
    seed: str | None = None,
    snapshot: str = "",
    edges: list[dict[str, Any]] | None = None,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Compute one lens and package it as a derived-analysis artifact.

    This is analysis about the record, never part of it (DYN-I2).
    """
    if view not in LENS_VIEWS:
        raise ValueError(f"not a lens view: {view}")
    if view in SEEDED_VIEWS and not seed:
        raise ValueError(f"--view {view} requires --seed")

    if edges is None:
        edges = derive_typed_edges(decisions)
    node_ids = [d["id"] for d in decisions if d.get("id")]

    if view == "foundations":
        scores, iterations = personalized_pagerank(node_ids, edges, seed=None, alpha=alpha)
    elif view == "lineage":
        scores, iterations = personalized_pagerank(node_ids, edges, seed=seed, alpha=alpha, reverse=False)
    else:  # impact
        scores, iterations = personalized_pagerank(node_ids, edges, seed=seed, alpha=alpha, reverse=True)

    ordered = rank(scores)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "method": "personalized_pagerank",
        "view": view,
        "seed": seed,
        "restart_probability": round(1.0 - alpha, 10),
        "edge_policy": EDGE_POLICY,
        "ledger_snapshot_hash": snapshot,
        "edge_layer_hash": edge_layer_hash(edges),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "convergence": {"epsilon": EPSILON, "iterations": iterations},
        "scores": {nid: score for nid, score in ordered},
    }


# ── derived-layer writers (never the ledger, DYN-I2) ─────────────────────────

def derived_dir(irp_dir: Path) -> Path:
    return irp_dir / "derived"


def write_edge_layer(irp_dir: Path, layer: dict[str, Any]) -> Path:
    path = derived_dir(irp_dir) / "graph-edges.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_analysis(irp_dir: Path, analysis: dict[str, Any]) -> Path:
    name = analysis["view"]
    if analysis.get("seed"):
        name = f"{name}-{analysis['seed']}"
    path = derived_dir(irp_dir) / "graph-analysis" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
