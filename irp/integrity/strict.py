"""Strict JSONL ledger reader.

Unlike `store.read_ledger`, which tolerantly skips malformed lines, this reader
reports every problem by 1-based line number and rejects duplicate JSON object
keys. A verifier must never be told a ledger is intact when content was skipped.
"""
from __future__ import annotations

import json
from typing import Any


class StrictReadResult:
    """Result of a strict ledger parse."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        duplicate_ids: list[str],
    ) -> None:
        self.entries = entries
        self.errors = errors            # [{"line": int, "kind": str, "detail": str}]
        self.duplicate_ids = duplicate_ids

    @property
    def ok(self) -> bool:
        return not self.errors


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key: {key!r}")
        seen.add(key)
    return dict(pairs)


def parse_ledger_strict(text: str) -> StrictReadResult:
    """Parse JSONL ledger text strictly.

    Blank lines are ignored (not errors). A non-blank line that fails to parse,
    contains duplicate keys, or is not a JSON object is recorded as an error.
    """
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except ValueError as exc:
            errors.append({"line": lineno, "kind": "malformed-json", "detail": str(exc)})
            continue
        if not isinstance(obj, dict):
            errors.append({
                "line": lineno,
                "kind": "not-an-object",
                "detail": f"expected a JSON object, got {type(obj).__name__}",
            })
            continue
        entries.append(obj)

    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for entry in entries:
        eid = entry.get("id")
        if not isinstance(eid, str):
            continue
        if eid in seen:
            duplicate_ids.append(eid)
        seen.add(eid)

    return StrictReadResult(entries, errors, duplicate_ids)
