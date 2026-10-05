"""Context Capsule: a deterministic, budgeted slice of the ledger (Roaming IRP spec §8a).

The capsule is the read side of the AI boundary. It is built by walking ledger
relationships, never by "relevance", so the same selection and parameters give
byte-identical bytes and a stable capsule_digest: the exact answer to "what did
this reader see?".

Selection, in order: the targets; their ancestors via rests_on / supersedes /
accepts up to ancestor_depth; pinned records; and the supersede head of any
selected record that was later replaced, so a reader never acts on stale state.

Pruning is honest: deep ancestors are demoted to stubs first, then the deepest
stubs are dropped and counted. Targets, their direct basis, pinned records and
supersede heads are never pruned; if those alone exceed the budget, building
fails with "narrow your selection" rather than truncating silently.

Exposure classes (Amendment A1): an entry is Class 0 when it carries
exposure_class "0" or a tag on the custodian's Class-0 list. Class 0 entries are
left out for non-EU readers and listed in the manifest as omitted.
"""
from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CAPSULE_VERSION = "0.1"
EDGES = ("rests_on", "supersedes", "accepts")
STUB_WHAT_MAX = 160
VOLATILE = ("generated_at", "disclosure", "capsule_digest")


class CapsuleError(ValueError):
    """The capsule can't be built as asked (unknown target, exposure, budget)."""


@dataclass
class Capsule:
    manifest: dict[str, Any]
    records: list[dict[str, Any]]
    ledger_bytes: bytes
    digest: str = field(default="")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _refs(entry: dict[str, Any], edge: str) -> list[str]:
    val = entry.get(edge)
    if not val:
        return []
    return [val] if isinstance(val, str) else [v for v in val if isinstance(v, str)]


def _parents(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return [(edge, ref) for edge in EDGES for ref in _refs(entry, edge)]


def _one_line_what(entry: dict[str, Any]) -> str:
    text = entry.get("what") or entry.get("title") or entry.get("decision") or ""
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line if len(line) <= STUB_WHAT_MAX else line[: STUB_WHAT_MAX - 1].rstrip() + "…"


def _sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("timestamp") or ""), str(entry.get("id")))


def _is_class0(entry: dict[str, Any], class0_tags: set[str]) -> bool:
    if str(entry.get("exposure_class", "")) == "0":
        return True
    tags = {str(t).lower() for t in entry.get("tags") or []}
    return bool(tags & class0_tags)


def build_capsule(
    ledger: Iterable[dict[str, Any]],
    targets: list[str],
    *,
    ancestor_depth: int = 2,
    pinned: Iterable[str] = (),
    token_budget: int = 8000,
    byte_budget: int = 262144,
    artefact_inline_cap: int = 32768,
    reader_region: str = "non-eu",
    class0_tags: Iterable[str] = (),
    generated_at: str | None = None,
    disclosure: dict[str, Any] | None = None,
) -> Capsule:
    by_id = {e["id"]: e for e in ledger if isinstance(e, dict) and e.get("id")
             and e.get("type") in (None, "decision", "contribution")}
    class0 = {t.lower() for t in class0_tags}
    restricted = reader_region != "eu"
    pinned = list(dict.fromkeys(pinned))

    for t in list(targets) + pinned:
        if t not in by_id:
            raise CapsuleError(f"unknown record {t}")
        if restricted and _is_class0(by_id[t], class0):
            raise CapsuleError(f"{t} is Class 0 by exposure class and can't go to a {reader_region} reader")

    # 1-2. Targets, then ancestors breadth-first; shortest depth wins the label.
    rel: dict[str, str] = {t: "target" for t in targets}
    depth: dict[str, int] = {t: 0 for t in targets}
    omitted: list[dict[str, str]] = []
    frontier = list(targets)
    for d in range(1, ancestor_depth + 1):
        nxt = []
        for rid in frontier:
            for edge, ref in _parents(by_id[rid]):
                if ref in rel or ref not in by_id or any(o["id"] == ref for o in omitted):
                    continue
                if restricted and _is_class0(by_id[ref], class0):
                    omitted.append({"id": ref, "reason": "exposure_class"})
                    continue
                rel[ref], depth[ref] = f"{edge}@depth{d}", d
                nxt.append(ref)
        frontier = nxt

    # 3. Pinned records.
    for p in pinned:
        if p not in rel:
            rel[p], depth[p] = "pinned", 0

    # 4. Supersede heads: the latest replacement of anything selected.
    superseded_by: dict[str, list[dict[str, Any]]] = {}
    for e in by_id.values():
        for ref in _refs(e, "supersedes"):
            superseded_by.setdefault(ref, []).append(e)
    for rid in list(rel):
        cur = rid
        while superseded_by.get(cur):
            cur = max(superseded_by[cur], key=_sort_key)["id"]
        if cur != rid and cur not in rel and not (restricted and _is_class0(by_id[cur], class0)):
            rel[cur], depth[cur] = "supersede_head", 0

    protected = {r for r, label in rel.items()
                 if label in ("target", "pinned", "supersede_head") or depth.get(r) == 1}

    # Causal order: parents before children, ties by (timestamp, id).
    order = _causal_order([by_id[r] for r in rel])

    forms = {r: "full" for r in rel}
    pruning = {"applied": False, "omitted_count": 0, "reason": None}

    def render() -> tuple[list[dict[str, Any]], bytes]:
        recs = []
        for e in order:
            rid = e["id"]
            if rid not in forms:
                continue
            if forms[rid] == "full":
                recs.append({**e, "_capsule_form": "full"})
            else:
                recs.append({"id": rid, "what": _one_line_what(e), "relationship": rel[rid],
                             "_capsule_form": "stub"})
        data = "".join(_canonical(r) + "\n" for r in recs).encode("utf-8")
        return recs, data

    def fits(data: bytes) -> bool:
        return (len(data) + 3) // 4 <= token_budget and len(data) <= byte_budget

    records, data = render()
    if not fits(data):
        # Step 1: demote deep ancestors (depth >= 2) to stubs.
        for r in rel:
            if r not in protected and depth.get(r, 0) >= 2:
                forms[r] = "stub"
        pruning["applied"] = True
        records, data = render()
    if not fits(data):
        # Step 2: drop the deepest unprotected records first, then anything else unprotected.
        droppable = sorted((r for r in forms if r not in protected),
                           key=lambda r: (-depth.get(r, 0), _sort_key(by_id[r])))
        for r in droppable:
            del forms[r]
            pruning["omitted_count"] += 1
            records, data = render()
            if fits(data):
                break
        pruning["reason"] = f"{pruning['omitted_count']} ancestor records omitted for size"
    if not fits(data):
        raise CapsuleError("selection too large for the budget; narrow your selection "
                           "(fewer targets, lower ancestor_depth, or a larger budget)")

    manifest: dict[str, Any] = {
        "capsule_version": CAPSULE_VERSION,
        "generated_at": generated_at,
        "selection_params": {
            "targets": list(targets), "ancestor_depth": ancestor_depth, "pinned": pinned,
            "token_budget": token_budget, "byte_budget": byte_budget,
            "artefact_inline_cap": artefact_inline_cap, "reader_region": reader_region,
        },
        "checkpoint": None,
        "records": [{"id": r["id"], "form": r["_capsule_form"], "relationship_to_target": rel[r["id"]]}
                    for r in records],
        "artefacts": [],
        "pruning": pruning,
        "omitted": omitted,
        "disclosure": disclosure,
    }
    stable = {k: v for k, v in manifest.items() if k not in VOLATILE}
    digest = "sha256-" + hashlib.sha256(_canonical(stable).encode("utf-8") + b"\n" + data).hexdigest()
    manifest["capsule_digest"] = digest
    return Capsule(manifest=manifest, records=records, ledger_bytes=data, digest=digest)


def _causal_order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {e["id"] for e in entries}
    by_id = {e["id"]: e for e in entries}
    indeg = {i: 0 for i in ids}
    children: dict[str, list[str]] = {i: [] for i in ids}
    for e in entries:
        for _, ref in _parents(e):
            if ref in ids and ref != e["id"]:
                indeg[e["id"]] += 1
                children[ref].append(e["id"])
    heap = [(_sort_key(by_id[i]), i) for i in ids if indeg[i] == 0]
    heapq.heapify(heap)
    out = []
    while heap:
        _, i = heapq.heappop(heap)
        out.append(by_id[i])
        for c in children[i]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(heap, (_sort_key(by_id[c]), c))
    # A cycle would leave records out; append them in (timestamp, id) order.
    seen = {e["id"] for e in out}
    out += sorted((e for e in entries if e["id"] not in seen), key=_sort_key)
    return out


RETURN_TEXT = """IRP Context Capsule (read-only)

This capsule is a deterministic slice of an IRP decision ledger. manifest.json
lists exactly which records are included (full or stub), which were omitted and
why, and the capsule_digest that identifies this exact slice.

Writing back is not available yet (Roaming IRP Cut 2). To record a new decision,
ask the human whose ledger this is. When proposals arrive they will be
append-only and unconfirmed until that human confirms them: propose, don't
overwrite. Cite the capsule_digest as the context you worked from.
"""


def write_bundle(capsule: Capsule, out_dir: Path | str) -> Path:
    """Write the spec §8a layout: manifest.json, ledger.jsonl, IRP-RETURN.txt."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(capsule.manifest, indent=2, sort_keys=True,
                                                  ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "ledger.jsonl").write_bytes(capsule.ledger_bytes)
    (out / "IRP-RETURN.txt").write_text(RETURN_TEXT, encoding="utf-8")
    return out
