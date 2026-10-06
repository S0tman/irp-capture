"""Rekadu: a deterministic, budgeted slice of the ledger (Roaming IRP spec v0.3 §8a, §16).

Rekadu (a Kriol-style spelling of "recado", a message left for someone who
isn't there) is the read side of the AI boundary. It was called the Context
Capsule until spec v0.3. It is built by walking ledger relationships, never by
"relevance", so the same selection and parameters give byte-identical bytes
and a stable rekadu_digest: the exact answer to "what was this reader sent?".

Selection, in order: the targets (and any records a scope rule matched); their
ancestors via rests_on / supersedes / accepts / corrects up to ancestor_depth,
read from flat fields and from a nested "relationships" object; pinned
records; and every newest replacement (supersede or correction head) of any
selected record, so a reader never acts on stale state. A replacement cycle is
an error, because it would hide which record is current.

Pruning is honest: deep ancestors are demoted to stubs first, then the deepest
stubs are dropped and counted. Targets, their direct basis, pinned records and
supersede heads are never pruned; if those alone exceed the budget, building
fails with "narrow your selection" rather than truncating silently. The budget
counts the manifest as well as the records, with a fixed allowance for the
volatile fields so they can never change what is selected.

Class 0 fails closed (Amendment A1, spec §16.3): for any reader whose surface
maps to a non-EU region, a record is Class 0 when, at any nesting level, it
carries an exposure_class that isn't a known non-zero class, a tag matching the
custodian's reviewed list (or tags we can't read), a field whose name starts
with confidential, private or secret, or a value JSON can't hold. Key names are
compared with case and punctuation removed. Class 0 records never enter the slice. A
referenced ancestor or a withheld supersede head keeps its id in "omitted"
while the record naming it is in the slice; a record a scope rule matched is
only counted. Records pending review (§16.3 #6) are held back and counted
without ids. An explicit Class 0 or pending target is an error. Unresolved
references are recorded, never dropped silently.

Canonical form is RFC 8785 JCS (irp.integrity.canonical, imported lazily, so
the base package stays dependency-free until a Rekadu is built).
"""
from __future__ import annotations

import hashlib
import heapq
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REKADU_VERSION = "0.2"
CANONICALIZATION = "RFC8785"
EDGES = ("rests_on", "supersedes", "accepts", "corrects")
HEAD_EDGES = ("supersedes", "corrects")
SELECTABLE_TYPES = (None, "decision", "contribution", "correction")
CLASS0_FIELD_PREFIXES = ("confidential", "private", "secret")
MIN_DEPTH, MAX_DEPTH = 1, 4
STUB_WHAT_MAX = 160
VOLATILE = ("generated_at", "disclosure", "rekadu_digest")
OMITTED_REASONS = ("exposure_class", "unknown_id", "type_not_in_scope", "pending_review")
DISCLOSURE_KEYS = ("disclosure_id", "reader_id", "surface", "scope", "expires", "identity_assurance")
CHECKPOINT_KEYS = ("id", "strand", "seq", "hash", "signed_ts")
# Bytes reserved in every budget for generated_at and the disclosure projection,
# so those volatile fields can never change what gets selected (or the digest).
VOLATILE_ALLOWANCE = 512
_DIGEST_PLACEHOLDER = "sha256-" + "0" * 64
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")  # fullmatch only
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}")  # fullmatch only
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LIST_SPLIT = re.compile(r"[,;]")
# exposure_class is an allowlist: only these known non-zero classes (compacted) are
# safe. Anything else, including values we can't read, counts as Class 0.
_SAFE_EXPOSURE = frozenset({"1", "2", "2a", "2b", "class1", "class2", "class2a", "class2b"})
_UNREADABLE = "\x00unreadable"
# Letters NFKD leaves alone, folded so Nordic and other Latin spellings match ASCII slugs.
_SPECIAL_LETTERS = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
                                  "ß": "ss", "ẞ": "ss", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l",
                                  "þ": "th", "Þ": "th", "ð": "d", "Ð": "d", "ı": "i"})

# Region is hard-mapped from the reader's surface, a closed list (spec §15.3,
# §16.3 #4). Only the EU sovereign runtime is "eu". It and managed browsers are
# reserved until their own spec, so Cut 1 refuses them.
_SURFACE_REGION = {
    "claude-code-cloud": "non-eu",
    "browser-ephemeral": "non-eu",
    "browser-managed": "non-eu",
    "eu-sovereign-runtime": "eu",
}
_RESERVED_SURFACES = frozenset({"browser-managed", "eu-sovereign-runtime"})


class RekaduError(ValueError):
    """The Rekadu can't be built as asked (unknown target, exposure, duplicates, budget)."""


@dataclass
class Rekadu:
    manifest: dict[str, Any]
    records: list[dict[str, Any]]
    ledger_bytes: bytes
    digest: str = field(default="")
    size: int = 0  # bytes counted against the budget (manifest frame + volatile allowance + ledger)


def region_for_surface(surface: str) -> str:
    """Map a reader surface to its region. Free-text regions are not accepted."""
    try:
        return _SURFACE_REGION[surface]
    except (KeyError, TypeError):
        known = ", ".join(sorted(_SURFACE_REGION))
        raise RekaduError(f"unknown reader surface {surface!r}; known surfaces: {known}") from None


def _canonical(obj: Any) -> bytes:
    from irp.integrity.canonical import canonicalize

    return canonicalize(obj)


# ── Reading ledger values (lenient: odd shapes are ignored, never crash) ──

def _str_list(val: Any) -> list[str]:
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [v for v in val if isinstance(v, str)]
    return []


def _refs(entry: dict[str, Any], edge: str) -> list[str]:
    nested = entry.get("relationships") if isinstance(entry.get("relationships"), dict) else {}
    return list(dict.fromkeys(_str_list(entry.get(edge)) + _str_list(nested.get(edge))))


def _parents(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return [(edge, ref) for edge in EDGES for ref in _refs(entry, edge)]


def _one_line_what(entry: dict[str, Any]) -> str:
    text = str(entry.get("what") or entry.get("title") or entry.get("decision") or "").strip()
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= STUB_WHAT_MAX else line[: STUB_WHAT_MAX - 1].rstrip() + "…"


def _sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (str(entry.get("timestamp") or ""), str(entry.get("id")))


# ── Class 0 (strict: every doubtful shape fails closed) ──

def _fold(text: Any) -> str:
    """Lowercase with accents folded away: Göteborg, Øresund and Straße become goteborg, oresund, strasse."""
    decomposed = unicodedata.normalize("NFKD", str(text).translate(_SPECIAL_LETTERS))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _compact(text: Any) -> str:
    """Folded, then only a-z and 0-9, so spellings compare equal."""
    return _NON_ALNUM.sub("", _fold(text))


def _words(text: Any) -> set[str]:
    return {w for w in _NON_ALNUM.split(_fold(text)) if w}


def _items(obj: Any):
    """Every (compacted key, value) pair at every nesting level. A container JSON
    can't hold (a set, bytes) yields an unreadable marker, which fails closed."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield _compact(k), v
            yield from _items(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _items(v)
    elif isinstance(obj, (set, frozenset, bytes, bytearray)):
        yield _UNREADABLE, obj


def _exposure_is_class0(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return True
    if isinstance(val, int):
        return str(val) not in _SAFE_EXPOSURE
    if isinstance(val, str):
        return _compact(val) not in _SAFE_EXPOSURE
    return True


def _prepare_class0_tags(class0_tags: Iterable[str]) -> tuple[tuple[frozenset[str], str], ...]:
    """Each reviewed entry as (its words, its compact form). A string may list
    several entries separated by commas or semicolons."""
    out = []
    for raw in class0_tags:
        for piece in _LIST_SPLIT.split(raw):
            if not piece.strip():
                continue
            if not _compact(piece):
                raise RekaduError(f"class0_tags entry {piece!r} has no Latin letters or digits after folding; "
                                  "write it as it appears in ledger tags")
            out.append((frozenset(_words(piece)), _compact(piece)))
    return tuple(out)


def _tag_values(entry: dict[str, Any]) -> list[str] | None:
    """Every tag string under any key named tag or tags; None if a value is unreadable."""
    out: list[str] = []
    for key, val in _items(entry):
        if key not in ("tag", "tags") or val in (None, "", [], ()):
            continue
        values = [val] if isinstance(val, str) else list(val) if isinstance(val, (list, tuple)) else None
        if values is None or not all(isinstance(v, str) for v in values):
            return None
        if any(not _compact(v) and any(unicodedata.category(ch).startswith("L") for ch in v) for v in values):
            return None  # letters in a script that folds away can't be checked against the list
        out.extend(values)
    return out


def _tags_match(tags: list[str], prepared: tuple[tuple[frozenset[str], str], ...]) -> bool:
    """A reviewed entry matches when its compact form equals a tag's (or a listed part's),
    or when all its words appear among the record's tag words. Fails closed on overlap."""
    words: set[str] = set()
    compacts: set[str] = set()
    for t in tags:
        words |= _words(t)
        compacts.add(_compact(t))
        compacts.update(_compact(p) for p in _LIST_SPLIT.split(t))
    return any(c in compacts or (w and w <= words) for w, c in prepared)


def _is_class0(entry: dict[str, Any], prepared_tags: tuple, prefixes: tuple[str, ...]) -> bool:
    """Spec §16.1 #11, read so that every doubtful shape fails closed."""
    for key, val in _items(entry):
        if key == _UNREADABLE:
            return True
        if key == "exposureclass" and _exposure_is_class0(val):
            return True
        if key.startswith(prefixes):
            return True
    tags = _tag_values(entry)
    return tags is None or _tags_match(tags, prepared_tags)


def _assert_no_class0(records: list[dict[str, Any]], class0_tags: Iterable[str], prefixes: Iterable[str],
                      class0_ids: set[str]) -> None:
    """Post-build assertion (spec §16.3 #5), run on the rendered records."""
    class0_tags = _prepare_class0_tags(class0_tags)
    prefixes = tuple(_compact(p) for p in prefixes)
    for rec in records:
        content = {k: v for k, v in rec.items() if k not in ("_rekadu_form", "superseded")}
        if rec.get("id") in class0_ids or (rec.get("_rekadu_form") == "full"
                                           and _is_class0(content, class0_tags, prefixes)):
            raise RekaduError(f"a Class 0 record ({rec.get('id')}) reached the Rekadu; refusing to build")


# ── Checking caller arguments (strict) ──

def _arg_list(name: str, val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple, set, frozenset)) and all(isinstance(v, str) for v in val):
        return list(val)
    raise RekaduError(f"{name} must be a string or a list of strings")


def _pos_int(name: str, val: Any) -> int:
    if type(val) is not int or val <= 0:
        raise RekaduError(f"{name} must be a positive integer, got {val!r}")
    return val


def _timestamp(name: str, val: Any) -> None:
    if not (isinstance(val, str) and _TIMESTAMP.fullmatch(val)):
        raise RekaduError(f"{name} must be a UTC timestamp like 2026-10-06T09:00:00Z, got {val!r}")


def _check_disclosure(disclosure: Any, surface: str, generated_at: Any) -> None:
    if generated_at is not None:
        _timestamp("generated_at", generated_at)
    if disclosure is not None:
        if not isinstance(disclosure, dict) or set(disclosure) != set(DISCLOSURE_KEYS):
            raise RekaduError(f"disclosure must be the spec projection with exactly the keys {list(DISCLOSURE_KEYS)}")
        if disclosure["surface"] != surface:
            raise RekaduError(f"disclosure surface {disclosure['surface']!r} doesn't match the reader surface {surface!r}")
        if disclosure["scope"] != "read":
            raise RekaduError("disclosure scope must be 'read' in Cut 1")
        if not (isinstance(disclosure["expires"], str) and _TIMESTAMP.fullmatch(disclosure["expires"])):
            raise RekaduError("disclosure expires must be a UTC timestamp like 2026-10-06T09:00:00Z")
        if not all(isinstance(disclosure[k], str) for k in ("disclosure_id", "reader_id", "identity_assurance")):
            raise RekaduError("disclosure ids and identity_assurance must be strings")
    if len(_canonical({"generated_at": generated_at, "disclosure": disclosure})) > VOLATILE_ALLOWANCE:
        raise RekaduError(f"generated_at and disclosure exceed the {VOLATILE_ALLOWANCE}-byte allowance")


def _check_checkpoint(ck: Any) -> None:
    if ck is None:
        return
    ok = (isinstance(ck, dict) and set(ck) == set(CHECKPOINT_KEYS)
          and isinstance(ck["id"], str) and ck["id"].startswith("ckpt-")
          and isinstance(ck["strand"], str) and ck["strand"].startswith("dk-")
          and type(ck["seq"]) is int and ck["seq"] >= 0
          and isinstance(ck["hash"], str) and _DIGEST.fullmatch(ck["hash"])
          and isinstance(ck["signed_ts"], str) and _TIMESTAMP.fullmatch(ck["signed_ts"]))
    if not ok:
        raise RekaduError("checkpoint must be None or {id: ckpt-…, strand: dk-…, seq: int >= 0, "
                          "hash: sha256-<64 hex>, signed_ts: UTC timestamp}")


def build_rekadu(
    ledger: Iterable[dict[str, Any]],
    targets: Iterable[str] = (),
    *,
    matched: Iterable[str] = (),
    pending_review: Iterable[str] = (),
    ancestor_depth: int = 2,
    pinned: Iterable[str] = (),
    token_budget: int = 8000,
    byte_budget: int = 262144,
    artefact_inline_cap: int = 32768,
    surface: str = "claude-code-cloud",
    class0_tags: Iterable[str] | str = (),
    class0_field_prefixes: Iterable[str] | str = (),
    checkpoint: dict[str, Any] | None = None,
    generated_at: str | None = None,
    disclosure: dict[str, Any] | None = None,
) -> Rekadu:
    """Build a Rekadu. `surface` must come from the reader registry (readers.jsonl);
    the publisher resolves it there, so the region is never typed in by a caller.
    `class0_field_prefixes` adds to the mandatory confidential/private/secret."""
    _pos_int("ancestor_depth", ancestor_depth)
    if not MIN_DEPTH <= ancestor_depth <= MAX_DEPTH:
        raise RekaduError(f"ancestor_depth must be {MIN_DEPTH} to {MAX_DEPTH}, got {ancestor_depth!r}")
    for name, val in (("token_budget", token_budget), ("byte_budget", byte_budget),
                      ("artefact_inline_cap", artefact_inline_cap)):
        _pos_int(name, val)
    region = region_for_surface(surface)
    if surface in _RESERVED_SURFACES:
        raise RekaduError(f"surface {surface!r} is reserved until its own spec; "
                          "Cut 1 serves claude-code-cloud and browser-ephemeral")
    _check_disclosure(disclosure, surface, generated_at)
    _check_checkpoint(checkpoint)
    restricted = region != "eu"
    raw_tags0 = _arg_list("class0_tags", class0_tags)
    tags0 = _prepare_class0_tags(raw_tags0)
    extra_prefixes = _arg_list("class0_field_prefixes", class0_field_prefixes)
    if any(not _compact(p) for p in extra_prefixes):
        raise RekaduError("class0_field_prefixes entries need Latin letters or digits after folding")
    prefixes = tuple(dict.fromkeys(_compact(p) for p in CLASS0_FIELD_PREFIXES + tuple(extra_prefixes)))
    targets = sorted(set(_arg_list("targets", targets)))
    pinned = sorted(set(_arg_list("pinned", pinned)))
    matched = sorted(set(_arg_list("matched", matched)))
    pending = set(_arg_list("pending_review", pending_review))

    entries = [e for e in ledger if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]]
    id_count = Counter(e["id"] for e in entries)
    all_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        all_by_id[e["id"]].append(e)
    selectable = [e for e in entries if e.get("type") in SELECTABLE_TYPES]
    by_id: dict[str, dict[str, Any]] = {}
    for e in selectable:
        by_id.setdefault(e["id"], e)

    def class0(rid: str) -> bool:  # fail closed across duplicates of an id
        return restricted and any(_is_class0(e, tags0, prefixes) for e in all_by_id.get(rid, ()))

    for t in targets + pinned + matched + sorted(pending):
        if t not in all_by_id:
            raise RekaduError(f"unknown record {t}")
    for t in targets + pinned + matched:
        if t not in by_id:
            raise RekaduError(f"{t} has type {all_by_id[t][0].get('type')!r}, which a Rekadu can't select")
    for t in targets + pinned:
        if class0(t):
            raise RekaduError(f"{t} is Class 0 under the exposure rules (exposure_class, reviewed tags, "
                              f"confidential fields or unreadable values) and can't go to a {region} reader")
        if t in pending:
            raise RekaduError(f"{t} is pending review; run the review dry run before naming it")

    # Omissions are counted once per id and reason. A row naming the id is shown only
    # while its referrer is in the slice in full (the referrer already names it), or,
    # for a withheld head, while the record it replaces is in the slice. Rule-matched
    # Class 0 records and records pending review are counted, never named.
    omitted_ids: dict[str, set[str]] = {reason: set() for reason in OMITTED_REASONS}
    rows: list[tuple[str, str, str, str | None, str]] = []

    def note(rid: str, reason: str, referrer: str, relationship: str | None = None, needs: str = "full") -> None:
        omitted_ids[reason].add(rid)
        rows.append((rid, reason, referrer, relationship, needs))

    def held_back(rid: str) -> bool:
        if rid in pending and not class0(rid):
            omitted_ids["pending_review"].add(rid)
            return True
        return False

    # 1. Targets, then rule-matched records.
    rel: dict[str, str] = {t: "target" for t in targets}
    depth: dict[str, int] = {t: 0 for t in targets}
    matched_kept: list[str] = []
    for m in matched:
        if m in rel:
            continue
        if class0(m):
            omitted_ids["exposure_class"].add(m)
            continue
        if held_back(m):
            continue
        rel[m], depth[m] = "target", 0
        matched_kept.append(m)

    # 2. Ancestors breadth-first, in sorted order; the shortest depth wins the label.
    frontier = sorted(rel)
    for d in range(1, ancestor_depth + 1):
        nxt = []
        for rid in frontier:
            for edge, ref in _parents(by_id[rid]):
                if ref in rel:
                    continue
                if ref not in all_by_id:
                    note(ref, "unknown_id", rid)
                elif ref not in by_id:
                    note(ref, "type_not_in_scope", rid)
                elif class0(ref):
                    note(ref, "exposure_class", rid)
                elif not held_back(ref):
                    rel[ref], depth[ref] = f"{edge}@depth{d}", d
                    nxt.append(ref)
        frontier = sorted(nxt)

    # 3. Pinned records (protected even when they are also an ancestor).
    for p in pinned:
        if p not in rel:
            rel[p], depth[p] = "pinned", 0

    # 4. Heads: every newest replacement (supersede or correction) of anything selected.
    #    Built from all copies of every id, so ledger order never matters.
    replaced_by: dict[str, set[str]] = defaultdict(set)
    for e in selectable:
        for edge in HEAD_EDGES:
            for ref in _refs(e, edge):
                if ref != e["id"]:
                    replaced_by[ref].add(e["id"])

    def on_cycle(node: str) -> bool:
        stack, seen = list(replaced_by.get(node, ())), set()
        while stack:
            x = stack.pop()
            if x == node:
                return True
            if x not in seen:
                seen.add(x)
                stack.extend(replaced_by.get(x, ()))
        return False

    heads: set[str] = set()
    withheld: set[str] = set()
    for rid in sorted(rel):
        stack, seen, terminals = [rid], {rid}, set()
        while stack:
            cur = stack.pop()
            reps = sorted(replaced_by.get(cur, ()))
            if not reps and cur != rid:
                terminals.add(cur)
            for r in reps:
                if r in seen:
                    continue
                seen.add(r)
                if class0(r) and replaced_by.get(r):
                    for successor in sorted(replaced_by[r]):
                        note(r, "exposure_class", successor)
                stack.append(r)
        cyclic = sorted(n for n in seen if on_cycle(n))
        if cyclic:
            raise RekaduError(f"supersede cycle among {', '.join(cyclic)}; correct the ledger before "
                              f"selecting {rid}, or its current version can't be shown")
        for h in sorted(terminals):
            if class0(h):
                note(h, "exposure_class", rid, f"supersede_head_of:{rid}", needs="any")
                withheld.add(rid)
            elif h in pending and h not in rel:
                omitted_ids["pending_review"].add(h)
                withheld.add(rid)
            else:
                heads.add(h)
                if h not in rel:
                    rel[h], depth[h] = "supersede_head", 0

    # 5. Unresolved references of every selected record, pinned and heads included.
    for rid in sorted(rel):
        for _, ref in _parents(by_id[rid]):
            if ref not in all_by_id:
                note(ref, "unknown_id", rid)
            elif ref not in by_id:
                note(ref, "type_not_in_scope", rid)

    for rid in rel:
        if id_count[rid] > 1:
            raise RekaduError(f"{rid} appears {id_count[rid]} times in the ledger (duplicate id); "
                              "correct the ledger before selecting it")
    class0_ids = {i for i in all_by_id if class0(i)} if restricted else set()
    if class0_ids & set(rel):
        raise RekaduError("a Class 0 record reached the selection; refusing to build")

    protected = ({r for r, label in rel.items() if label in ("target", "pinned", "supersede_head")
                  or depth.get(r) == 1} | set(pinned) | heads)
    order = _causal_order([by_id[r] for r in rel])
    forms = {r: "full" for r in rel}
    pruning: dict[str, Any] = {"applied": False, "omitted_count": 0, "reason": None}
    selection_params = {
        "targets": sorted(targets + matched_kept), "ancestor_depth": ancestor_depth, "pinned": pinned,
        "token_budget": token_budget, "byte_budget": byte_budget,
        "artefact_inline_cap": artefact_inline_cap, "reader_region": region,
    }

    def render() -> tuple[list[dict[str, Any]], bytes]:
        recs, chunks = [], []
        for e in order:
            rid = e["id"]
            if rid not in forms:
                continue
            if forms[rid] == "full":
                rec = {**e, "_rekadu_form": "full"}
            else:
                rec = {"id": rid, "what": _one_line_what(e), "relationship": rel[rid], "_rekadu_form": "stub"}
            if rid in withheld:
                rec["superseded"] = "withheld"
            try:
                chunks.append(_canonical(rec) + b"\n")
            except Exception as exc:
                raise RekaduError(f"{rid} holds a value JCS can't represent ({exc}); correct the ledger") from exc
            recs.append(rec)
        if restricted:
            _assert_no_class0(recs, raw_tags0, prefixes, class0_ids)
        return recs, b"".join(chunks)

    def manifest_for(recs: list[dict[str, Any]]) -> dict[str, Any]:
        present = {r["id"]: r["_rekadu_form"] for r in recs}
        visible: dict[tuple[str, str, str], dict[str, str]] = {}
        for rid, reason, referrer, relationship, needs in rows:
            if (present.get(referrer) == "full") if needs == "full" else (referrer in present):
                row = {"id": rid, "reason": reason}
                if relationship:
                    row["relationship"] = relationship
                visible[(rid, reason, relationship or "")] = row
        manifest_rows = []
        for r in recs:
            row = {"id": r["id"], "form": r["_rekadu_form"], "relationship_to_target": rel[r["id"]]}
            if r["id"] in withheld:
                row["superseded"] = "withheld"
            manifest_rows.append(row)
        return {
            "rekadu_version": REKADU_VERSION,
            "canonicalization": CANONICALIZATION,
            "generated_at": generated_at,
            "selection_params": selection_params,
            "checkpoint": checkpoint,
            "records": manifest_rows,
            "artefacts": [],
            "pruning": dict(pruning),
            "omitted": [visible[k] for k in sorted(visible)],
            "omitted_counts": {reason: len(ids) for reason, ids in omitted_ids.items()},
            "disclosure": disclosure,
        }

    def measure(recs: list[dict[str, Any]], data: bytes) -> int:
        stable = {k: v for k, v in manifest_for(recs).items() if k not in VOLATILE}
        frame = {**stable, "generated_at": None, "disclosure": None, "rekadu_digest": _DIGEST_PLACEHOLDER}
        return len(_canonical(frame)) + 1 + VOLATILE_ALLOWANCE + len(data)

    def fits(size: int) -> bool:
        return (size + 3) // 4 <= token_budget and size <= byte_budget

    records, data = render()
    size = measure(records, data)
    if not fits(size):
        # Step 1: demote deep ancestors (depth >= 2) to stubs.
        demote = [r for r in rel if r not in protected and depth.get(r, 0) >= 2]
        if demote:
            for r in demote:
                forms[r] = "stub"
            pruning["applied"] = True
            records, data = render()
            size = measure(records, data)
    if not fits(size):
        # Step 2: drop the deepest unprotected records first, then anything else unprotected.
        droppable = sorted((r for r in forms if r not in protected),
                           key=lambda r: (-depth.get(r, 0), _sort_key(by_id[r])))
        for r in droppable:
            del forms[r]
            pruning["applied"] = True
            pruning["omitted_count"] += 1
            pruning["reason"] = f"{pruning['omitted_count']} ancestor records omitted for size"
            records, data = render()
            size = measure(records, data)
            if fits(size):
                break
    if not fits(size):
        raise RekaduError("selection too large for the budget; narrow your selection "
                          "(fewer targets, lower ancestor_depth, or a larger budget)")

    manifest = manifest_for(records)
    stable = {k: v for k, v in manifest.items() if k not in VOLATILE}
    digest = "sha256-" + hashlib.sha256(_canonical(stable) + b"\n" + data).hexdigest()
    manifest["rekadu_digest"] = digest
    return Rekadu(manifest=manifest, records=records, ledger_bytes=data, digest=digest, size=size)


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


RETURN_TEXT = f"""IRP Rekadu (read-only), format {REKADU_VERSION}

A Rekadu is a deterministic slice of an IRP decision ledger: a message left for
whoever reads it next. manifest.json lists exactly which records are included
(full or stub), which were left out and why, and the rekadu_digest that
identifies this exact slice.

Don't commit this folder to any repository: it can hold private decisions.

Writing back is not available yet (Roaming IRP Cut 2). To record a new
decision, ask the human whose ledger this is. When proposals arrive they will
be append-only and unconfirmed until that human confirms them:
propose, don't overwrite. Cite the rekadu_digest as the context you worked from.
"""


def write_bundle(rekadu: Rekadu, out_dir: Path | str) -> Path:
    """Write the spec §8a layout into an empty folder, private to the owner (folders 0700,
    files 0600): manifest.json (JCS), ledger.jsonl, artefacts/ (reserved, empty in Cut 1)
    and IRP-RETURN.txt."""
    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        raise RekaduError(f"bundle path {out} exists and is not a folder")
    if out.exists() and any(out.iterdir()):
        raise RekaduError(f"bundle folder {out} must be empty, so no stale files ship with the slice")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)  # plaintext slice: owner only (spec §19.2), whatever the umask
    (out / "artefacts").mkdir(mode=0o700)
    os.chmod(out / "artefacts", 0o700)
    _write_private(out / "manifest.json", _canonical(rekadu.manifest) + b"\n")
    _write_private(out / "ledger.jsonl", rekadu.ledger_bytes)
    _write_private(out / "IRP-RETURN.txt", RETURN_TEXT.encode("utf-8"))
    return out


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
