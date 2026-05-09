"""IRP Decision Resolver — the control plane query engine.

Answers for any query:
  - Which decisions are active (not superseded)?
  - Which apply to this scope (tag / context)?
  - Which conflict with a proposed action?
  - What is the verdict: clear / warn / block?

This module is pure logic — no CLI, no I/O, no argparse.
All commands that need conflict detection (check, guard, resolve) import from here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── stopwords ────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "a", "an", "the", "in", "to", "of", "and", "or", "for", "at", "by",
    "on", "with", "as", "into", "from", "up", "out", "about", "per",
    "it", "its", "this", "that", "we", "our", "they", "their", "i", "my",
    "add", "adding", "use", "using", "used", "implement", "implementing",
    "create", "make", "get", "set", "run", "update", "build", "introduce",
    "support", "allow", "enable", "provide", "include", "apply",
    "will", "not", "no", "be", "is", "are", "was", "were", "have", "has",
    "had", "can", "do", "so", "but", "if", "only", "also", "all", "new",
    "any", "each", "both", "already", "now", "just", "more", "better",
    "well", "good", "clear", "simple", "local", "same",
    "state", "thread", "project", "system", "single", "scale", "version",
    "v0", "approach", "management", "unnecessary", "complexity",
}

# Verdict thresholds (token overlap score)
_WARN_THRESHOLD = 1
_BLOCK_THRESHOLD = 3


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class ConflictMatch:
    id: str
    decision: str
    reasoning: str
    score: int
    matched_on: list[str]
    confidence: str
    confirmed_by: str
    tags: list[str]
    timestamp: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision": self.decision,
            "reasoning": self.reasoning,
            "score": self.score,
            "matched_on": self.matched_on,
            "confidence": self.confidence,
            "confirmed_by": self.confirmed_by,
            "tags": self.tags,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass
class ResolverResult:
    query: str
    verdict: str                          # "clear" | "warn" | "block"
    score: int                            # highest individual conflict score
    active_count: int
    superseded_count: int
    conflicts: list[ConflictMatch] = field(default_factory=list)
    top_match: ConflictMatch | None = None
    tag_filter: str | None = None
    scope_filter: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "verdict": self.verdict,
            "score": self.score,
            "active_count": self.active_count,
            "superseded_count": self.superseded_count,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "top_match": self.top_match.to_dict() if self.top_match else None,
            "tag_filter": self.tag_filter,
            "scope_filter": self.scope_filter,
        }


# ── internal helpers ─────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    words = re.split(r"[\s\W]+", text.lower())
    return {w for w in words if w and len(w) > 1 and w not in _STOPWORDS}


def _decision_text(entry: dict[str, Any]) -> str:
    """Concatenate all text fields worth matching against."""
    parts = [
        entry.get("decision", ""),
        entry.get("what", ""),       # legacy field name
        entry.get("reasoning", ""),
        entry.get("why", ""),        # legacy field name
        " ".join(entry.get("tags", [])),
    ]
    return " ".join(p for p in parts if p)


def _entry_to_conflict(entry: dict[str, Any], score: int, matched_on: list[str]) -> ConflictMatch:
    return ConflictMatch(
        id=entry.get("id", ""),
        decision=entry.get("decision", entry.get("what", "")),
        reasoning=entry.get("reasoning", entry.get("why", "")),
        score=score,
        matched_on=matched_on,
        confidence=entry.get("confidence", "medium"),
        confirmed_by=entry.get("confirmed_by", ""),
        tags=entry.get("tags", []),
        timestamp=str(entry.get("timestamp", "")),
        source=entry.get("source", ""),
    )


# ── supersession ─────────────────────────────────────────────────────────────

def build_supersession_map(ledger: list[dict[str, Any]]) -> set[str]:
    """Return the set of IRP IDs that have been superseded by a later entry."""
    superseded: set[str] = set()
    for entry in ledger:
        ref = entry.get("supersedes")
        if ref:
            if isinstance(ref, str):
                superseded.add(ref)
            elif isinstance(ref, list):
                superseded.update(ref)
    return superseded


def active_decisions(
    ledger: list[dict[str, Any]],
    tag: str | None = None,
    scope: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Return (active_entries, superseded_count).

    active_entries: decisions not superseded, optionally filtered by tag/scope.
    superseded_count: total number of superseded decisions (before tag filter).
    """
    superseded = build_supersession_map(ledger)
    decisions = [e for e in ledger if e.get("type") == "decision"]
    superseded_count = sum(1 for d in decisions if d.get("id", "") in superseded)
    active = [d for d in decisions if d.get("id", "") not in superseded]

    if tag:
        tag_lower = tag.lower()
        active = [
            d for d in active
            if any(tag_lower in t.lower() for t in d.get("tags", []))
        ]

    if scope:
        scope_lower = scope.lower()
        active = [
            d for d in active
            if scope_lower in _decision_text(d).lower()
        ]

    return active, superseded_count


# ── core resolver ─────────────────────────────────────────────────────────────

def resolve(
    query: str,
    ledger: list[dict[str, Any]],
    tag: str | None = None,
    scope: str | None = None,
) -> ResolverResult:
    """
    Resolve a query against the active decision ledger.

    Returns a ResolverResult with verdict, conflicts ranked by score, and provenance.
    """
    query = query.strip()
    query_tokens = _tokens(query)

    active, superseded_count = active_decisions(ledger, tag=tag, scope=scope)

    conflicts: list[ConflictMatch] = []

    for entry in reversed(active):  # newest-first
        entry_tokens = _tokens(_decision_text(entry))
        overlap = sorted(query_tokens & entry_tokens)
        if not overlap:
            continue
        score = len(overlap)
        conflicts.append(_entry_to_conflict(entry, score, overlap))

    # sort by score descending, then timestamp descending (newest first on ties)
    conflicts.sort(key=lambda c: (c.score, c.timestamp), reverse=True)

    top = conflicts[0] if conflicts else None
    max_score = top.score if top else 0

    if max_score >= _BLOCK_THRESHOLD:
        verdict = "block"
    elif max_score >= _WARN_THRESHOLD:
        verdict = "warn"
    else:
        verdict = "clear"

    return ResolverResult(
        query=query,
        verdict=verdict,
        score=max_score,
        active_count=len(active),
        superseded_count=superseded_count,
        conflicts=conflicts,
        top_match=top,
        tag_filter=tag,
        scope_filter=scope,
    )
