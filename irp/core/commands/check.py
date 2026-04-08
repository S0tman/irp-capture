"""irp check — lightweight bridge conflict preview.

Heuristic: keyword/phrase overlap between the proposal and active decisions
in .irp/current.json. No semantic understanding, no scoring, no embeddings.
First matching decision only. This is a stakeholder test, not a production
conflict engine.
"""
from __future__ import annotations

import re
from pathlib import Path

from irp.core.store import read_current

_STOPWORDS = {
    # articles / prepositions / conjunctions
    "a", "an", "the", "in", "to", "of", "and", "or", "for", "at", "by",
    "on", "with", "as", "into", "from", "up", "out", "about", "per",
    # pronouns / determiners
    "it", "its", "this", "that", "we", "our", "they", "their", "i", "my",
    # common verbs with no conflict signal
    "add", "adding", "use", "using", "used", "implement", "implementing",
    "create", "make", "get", "set", "run", "update", "build", "introduce",
    "support", "allow", "enable", "provide", "include", "apply",
    # modifiers / connectives
    "will", "not", "no", "be", "is", "are", "was", "were", "have", "has",
    "had", "can", "do", "so", "but", "if", "only", "also", "all", "new",
    "any", "each", "both", "already", "now", "just", "more", "better",
    "well", "good", "clear", "simple", "local", "same",
    # too generic in this codebase to carry conflict signal
    "state", "thread", "project", "system", "single", "scale", "version",
    "v0", "approach", "management", "unnecessary", "complexity",
}

_SOURCE_LABELS = {
    "slack": "Slack thread",
    "stdin": "IRP Capture SKILL",
    "cli": "IRP Capture SKILL",
}

_DIVIDER = "\u2500" * 48


def _tokens(text: str) -> set[str]:
    words = re.split(r"[\s\W]+", text.lower())
    return {w for w in words if w and w not in _STOPWORDS}


def _source_label(raw: str) -> str:
    return _SOURCE_LABELS.get(raw, raw)


def run_check(project_root: Path, irp_dir: Path, args) -> dict:
    proposal = args.proposal.strip()
    current = read_current(irp_dir)
    active = current.get("active", [])

    proposal_tokens = _tokens(proposal)
    match = None
    matched_words = []

    for entry in reversed(active):  # newest-first: most recent decisions are most relevant
        decision_tokens = _tokens(entry.get("what", "") + " " + entry.get("why", ""))
        overlap = sorted(proposal_tokens & decision_tokens)
        if overlap:
            match = entry
            matched_words = overlap
            break  # first match only

    if match:
        lines = [
            f"Checking proposal against project bridge (.irp/current.json)...",
            "",
            "\u26a0  Potential conflict with an active project decision",
            "",
            f"  Decision:   {match.get('id', '')}",
            f"  What:       {match.get('what', '')}",
            f"  Why:        {match.get('why', '')}",
            f"  Source:     {_source_label(match.get('source', ''))}",
        ]

        if match.get("source") == "slack":
            ref = match.get("source_ref", {})
            lines.append(f"  Channel:    {ref.get('channel_id', '')}")
            lines.append(f"  Thread:     {ref.get('thread_ts', '')}")

        lines += [
            f"  Timestamp:  {match.get('timestamp', '')}",
            "",
            f"  Why surfaced: proposal overlaps with an active decision in the shared project bridge.",
            f"  Matched on:  {', '.join(matched_words)}",
            "",
            _DIVIDER,
            "Source of truth: project `.irp/current.json` (shared bridge)",
        ]

        return {
            "command": "check",
            "status": "conflict",
            "proposal": proposal,
            "match_id": match.get("id"),
            "matched_on": matched_words,
            "text": "\n".join(lines),
        }

    lines = [
        f"Checking proposal against project bridge (.irp/current.json)...",
        "",
        "\u2713  No conflicts detected against active decisions in the shared project bridge.",
        "",
        f"  Proposal:   {proposal}",
        f"  Checked:    {len(active)} active decision{'s' if len(active) != 1 else ''}",
        "",
        _DIVIDER,
        "Source of truth: project `.irp/current.json` (shared bridge)",
    ]

    return {
        "command": "check",
        "status": "clear",
        "proposal": proposal,
        "checked": len(active),
        "text": "\n".join(lines),
    }
