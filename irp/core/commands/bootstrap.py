"""irp bootstrap: look through existing project artifacts for decisions.

Provenance contract:
  A guess made after the fact is never a captured decision.
  `irp bootstrap` records what it finds in .irp/reconstructions.jsonl, each line
  with an id like REC-YYYY-MM-DD-NNN and status "unconfirmed". It never writes to
  ledger.jsonl or current.json, so nothing it finds reaches agents, exports,
  gates or evidence.
  `irp bootstrap --accept REC-...` is the human confirmation: it appends a normal
  decision to the ledger (labelled as reconstructed, with a pointer back to the
  REC line) and marks the REC line accepted.
  Dates stay honest: the day it was recorded is `recorded_at`; the best-known day
  of the decision itself is `decided_at_estimate` (the commit date for git,
  unknown for documents).
  If evidence is weak, the entry is skipped or recorded at low confidence with
  an explicit uncertainty note in the 'why' field.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from irp.core.store import (
    RECONSTRUCTIONS_FILE,
    append_ledger_entry,
    append_reconstruction,
    irp_lock,
    next_irp_id,
    next_rec_id,
    read_ledger,
    read_reconstructions,
    rebuild_current,
    update_reconstruction,
    write_current,
)

# ---------------------------------------------------------------------------
# Decision signal heuristics
# ---------------------------------------------------------------------------

# Git commit prefixes that strongly suggest a decision was recorded
_DECISION_COMMIT_PATTERNS = re.compile(
    r"\b(decided|decision|chosen|choose|adopt|adopted|agreed|will use|standardise|standardize|"
    r"we will|we won't|we will not|locked|confirmed|rejected|drop|remove support|switch to|migrate to)\b",
    re.IGNORECASE,
)

# Commit prefixes that are almost never decision signals — skip them
_NOISE_COMMIT_PREFIXES = re.compile(
    r"^(merge|fixup|wip|bump|chore|style|fmt|format|typo|lint|revert|test|tests|ci|cd|"
    r"update changelog|update readme|add .gitignore)",
    re.IGNORECASE,
)

# Patterns that signal a decision in document text (line-level)
_DOC_DECISION_PATTERNS = re.compile(
    r"\b(we decided|decision:|we have decided|chosen to|agreed to|will use|will not use|"
    r"standardise on|standardize on|we will|we won't|we will not|confirmed:|locked:|"
    r"rejected:|adopted:|we adopt)\b",
    re.IGNORECASE,
)

# Minimum word count for a doc line to be worth extracting
_MIN_LINE_WORDS = 6

# Maximum chars for what/why fields extracted from artifacts
_MAX_FIELD_LEN = 200

# ---------------------------------------------------------------------------
# Git source
# ---------------------------------------------------------------------------

def _run_git_log(limit: int, cwd: "Path | None" = None) -> list[dict[str, str]]:
    """Return list of {hash, date, message} from git log in `cwd` (the project
    root), rather than whatever directory the process happens to be in."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H|%as|%s"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({"hash": parts[0], "date": parts[1], "message": parts[2].strip()})
        return entries
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

def _extract_git_candidates(commits: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Filter commits to decision-signal candidates, return structured entries."""
    candidates: list[dict[str, Any]] = []
    for commit in commits:
        msg = commit["message"]
        # Skip noise
        if _NOISE_COMMIT_PREFIXES.match(msg):
            continue
        if not _DECISION_COMMIT_PATTERNS.search(msg):
            continue
        # Truncate cleanly
        what = msg[:_MAX_FIELD_LEN]
        why = (
            "Reconstructed from a git commit message. "
            "The reasoning behind it was not captured at the time."
        )
        candidates.append({
            "type": "decision",
            "what": what,
            "why": why,
            "confidence": "low",
            "tags": ["bootstrap", "git"],
            "source": "bootstrap",
            "origin_mode": "bootstrap_git",
            "source_ref": commit["hash"],
            "bootstrapped": True,
            # The commit date is the best-known day of the decision, an estimate
            # and nothing more. The day it was recorded is added separately.
            "decided_at_estimate": commit["date"],
        })
    return candidates

# ---------------------------------------------------------------------------
# Docs / files source
# ---------------------------------------------------------------------------

def _scan_files(path: Path, extensions: tuple[str, ...] = (".md", ".txt")) -> list[Path]:
    """Return all files with given extensions under path, recursively."""
    found: list[Path] = []
    if path.is_file():
        return [path] if path.suffix in extensions else []
    for ext in extensions:
        found.extend(path.rglob(f"*{ext}"))
    # Skip .irp/ internals
    return [f for f in found if ".irp" not in f.parts]

def _project_relative(path: Any, project_root: Path) -> str:
    """A path as it may be stored in a ledger that gets shared.

    Relative to the project root, in posix form. A path outside the project
    falls back to the bare file name, so a home folder or username never ends up
    in the record.
    """
    p = Path(str(path))
    try:
        return p.resolve().relative_to(project_root.resolve()).as_posix()
    except (ValueError, OSError):
        return p.name


def _extract_doc_candidates(files: list[Path], project_root: Path) -> list[dict[str, Any]]:
    """Scan files for decision-signal lines, return structured entries."""
    candidates: list[dict[str, Any]] = []
    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip headings, code fences, short lines
            if stripped.startswith(("#", "```", "|", "---", ">")):
                continue
            if len(stripped.split()) < _MIN_LINE_WORDS:
                continue
            if not _DOC_DECISION_PATTERNS.search(stripped):
                continue
            # Grab a small context window (next line as potential 'why')
            context_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            context_line = context_line[:_MAX_FIELD_LEN] if context_line else ""
            what = stripped[:_MAX_FIELD_LEN]
            note = (
                f"Reconstructed from {filepath.name} (line {i + 1}). "
                "Original context may be incomplete."
            )
            # Trim the context line, not the provenance note, to fit the limit.
            room = max(0, _MAX_FIELD_LEN - len(note) - 2)
            context_line = context_line[:room].rstrip()
            why = f"{context_line}  {note}" if context_line else note
            candidates.append({
                "type": "decision",
                "what": what,
                "why": why,
                "confidence": "low",
                "tags": ["bootstrap", "docs"],
                "source": "bootstrap",
                "origin_mode": "bootstrap_docs",
                "source_ref": _project_relative(filepath, project_root),
                "bootstrapped": True,
                # A document line has no reliable decision date, and today's date
                # (when it was found) is not one.
                "decided_at_estimate": None,
            })
    return candidates

# ---------------------------------------------------------------------------
# Deduplication (simple: skip if 'what' is already in the ledger or in the
# reconstructions file)
# ---------------------------------------------------------------------------

def _deduplicate(
    candidates: list[dict[str, Any]],
    known: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split candidates into (new, skipped).

    `known` is every ledger line (legacy guesses included, so a re-run does not
    guess them again) plus every reconstruction, accepted ones too.
    """
    existing_whats = {str(e.get("what", "")).lower() for e in known}
    unique: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        key = c.get("what", "").lower()
        if key in existing_whats or key in seen:
            skipped.append(c)
            continue
        seen.add(key)
        unique.append(c)
    return unique, skipped

# ---------------------------------------------------------------------------
# Reconstruction lines
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Local time with offset. Its date part is the same day the REC id carries."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_reconstruction(candidate: dict[str, Any], rec_id: str, recorded_at: str) -> dict[str, Any]:
    """Shape a candidate as one line of reconstructions.jsonl."""
    rec: dict[str, Any] = {"id": rec_id}
    rec.update(candidate)
    rec["status"] = "unconfirmed"
    rec["recorded_at"] = recorded_at
    return rec

# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _write_report(
    irp_dir: Path,
    sources: list[str],
    candidates_found: list[dict[str, Any]],
    candidates_written: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    dry_run: bool,
) -> Path:
    reports_dir = irp_dir / "bootstrap_reports"
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    report_path = reports_dir / f"{ts}.md"

    lines = [
        "# IRP Bootstrap Report",
        f"Generated: {datetime.now().isoformat()}",
        f"Mode: {'dry-run' if dry_run else 'write'}",
        "",
        "## Sources scanned",
    ]
    for s in sources:
        lines.append(f"- {s}")
    lines += [
        "",
        f"## Candidates found: {len(candidates_found)}",
        f"## Reconstructions recorded: {len(candidates_written)}",
        f"## Skipped (duplicate or weak): {len(skipped)}",
        "",
        "## Caveats",
        f"- Nothing was added to the ledger. Reconstructions live in `.irp/{RECONSTRUCTIONS_FILE}` "
        "with status `unconfirmed` until you accept them.",
        "- Accept one with `irp bootstrap --accept REC-...`. Accepting is your confirmation that it was a decision.",
        "- Every reconstruction is marked `bootstrapped: true` and carries an `origin_mode`.",
        "- Confidence is set to `low` unless the signal is unusually explicit.",
        "- 'why' fields derived from artifacts are qualified with provenance notes.",
        "- `decided_at_estimate` is the best-known day of the decision (the commit date for git, "
        "unknown for documents). It is an estimate, not a record.",
        "- These entries reflect historical signals, not first-hand decision captures.",
        "",
        "## Reconstructions recorded",
    ]
    if not candidates_written:
        lines.append("None.")
    for entry in candidates_written:
        lines += [
            f"- **{entry.get('id', 'pending')}**: {entry.get('what', '')}",
            f"  Source: `{entry.get('origin_mode', '')}`, `{entry.get('source_ref', '')}`",
        ]
    if skipped:
        lines += ["", "## Skipped entries"]
        for entry in skipped:
            lines.append(f"- (duplicate) {entry.get('what', '')[:80]}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path

# ---------------------------------------------------------------------------
# Accepting a reconstruction (the human confirmation)
# ---------------------------------------------------------------------------

def _flatten_ids(raw: Any) -> list[str]:
    """--accept may arrive as a string, a list, or a list of lists (nargs='+'
    with action='append'). Return a flat, de-duplicated list in order."""
    flat: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            for x in item:
                walk(x)
        elif item is not None and str(item).strip():
            flat.append(str(item).strip())

    walk(raw)
    return list(dict.fromkeys(flat))


def _clean_source_ref(value: Any, project_root: Path) -> Any:
    """Old reconstructions (and hand-edited ones) can hold an absolute path.
    Make it project-relative before it reaches the shared ledger. Commit hashes
    and anything that is not an absolute path are left alone."""
    if isinstance(value, str) and os.path.isabs(value):
        return _project_relative(value, project_root)
    return value


def _ledger_entry_from_reconstruction(rec: dict[str, Any], irp_id: str, project_root: Path) -> dict[str, Any]:
    """A normal decision, as capture would write it. No `bootstrapped` flag:
    accepting it is the confirmation.

    `reconstruction_recorded_at` is copied from the REC line. With the REC id it
    identifies exactly which reconstruction this entry came from, so a retry
    after an interrupted accept can tell its own entry from one that merely
    carries a reused id.
    """
    tags = list(rec.get("tags") or [])
    if "reconstructed" not in tags:
        tags.append("reconstructed")
    return {
        "id": irp_id,
        "type": "decision",
        "what": rec.get("what", ""),
        "why": rec.get("why", ""),
        "confidence": rec.get("confidence", "low"),
        "tags": tags,
        "timestamp": date.today().isoformat(),
        "source": "bootstrap-accepted",
        "origin_mode": rec.get("origin_mode"),
        "source_ref": _clean_source_ref(rec.get("source_ref"), project_root),
        "reconstructed_from": rec.get("id"),
        "reconstruction_recorded_at": rec.get("recorded_at"),
        "decided_at_estimate": rec.get("decided_at_estimate"),
    }


def _landed_entry(ledger: list[dict[str, Any]], rec: dict[str, Any]) -> "dict[str, Any] | None":
    """The ledger entry an earlier, interrupted accept of this very REC wrote, if any.

    The REC id alone is not enough: ids can be reissued after
    reconstructions.jsonl is deleted, or differ between clones. The entry must
    also carry the same text and the same recorded_at.
    """
    recorded_at = rec.get("recorded_at")
    if not recorded_at:
        return None
    for e in ledger:
        if (
            e.get("reconstructed_from") == rec.get("id")
            and e.get("what") == rec.get("what")
            and e.get("reconstruction_recorded_at") == recorded_at
        ):
            return e
    return None


def _run_accept(project_root: Path, irp_dir: Path, rec_ids: list[str], dry_run: bool) -> dict:
    if dry_run:
        return _accept(project_root, irp_dir, rec_ids, dry_run=True)
    # Ledger append, REC rewrite and the current.json rebuild happen as one step
    # as far as other irp processes are concerned.
    with irp_lock(irp_dir):
        return _accept(project_root, irp_dir, rec_ids, dry_run=False)


def _accept(project_root: Path, irp_dir: Path, rec_ids: list[str], dry_run: bool) -> dict:
    accepted: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []

    ledger = read_ledger(irp_dir)
    all_rows = read_reconstructions(irp_dir)
    id_counts = Counter(r.get("id") for r in all_rows)
    rows = {r.get("id"): r for r in all_rows}
    # For a dry run, ids are handed out against a scratch copy of the ledger.
    scratch = list(ledger)

    for rec_id in rec_ids:
        rec = rows.get(rec_id)
        if rec is None:
            problems.append({"rec_id": rec_id, "reason": f"not found in .irp/{RECONSTRUCTIONS_FILE}"})
            continue
        if id_counts[rec_id] > 1:
            problems.append({
                "rec_id": rec_id,
                "reason": (
                    f"matches more than one line in .irp/{RECONSTRUCTIONS_FILE} "
                    f"({id_counts[rec_id]} lines). Ids must be unique, so fix the file by hand first"
                ),
            })
            continue
        status = rec.get("status", "unconfirmed")
        if status == "accepted":
            problems.append({
                "rec_id": rec_id,
                "reason": f"already accepted as {rec.get('accepted_as', 'a ledger decision')}",
            })
            continue
        if status != "unconfirmed":
            problems.append({
                "rec_id": rec_id,
                "reason": f"status is {status!r}; only unconfirmed reconstructions can be accepted",
            })
            continue
        if not str(rec.get("what", "")).strip():
            problems.append({"rec_id": rec_id, "reason": "has no 'what' text, nothing to accept"})
            continue

        if dry_run:
            irp_id = next_irp_id(scratch)
            scratch.append({"id": irp_id})
            accepted.append({"rec_id": rec_id, "id": irp_id, "what": rec.get("what", "")})
            continue

        # If an earlier accept appended the decision but was interrupted before
        # the REC line was updated, finish that job instead of appending a
        # second decision for the same reconstruction.
        ledger = read_ledger(irp_dir)
        landed = _landed_entry(ledger, rec)
        if landed is not None:
            irp_id = str(landed.get("id", ""))
        else:
            irp_id = next_irp_id(ledger)
            append_ledger_entry(irp_dir, _ledger_entry_from_reconstruction(rec, irp_id, project_root))
        update_reconstruction(irp_dir, rec_id, {
            "status": "accepted",
            "accepted_as": irp_id,
            "accepted_at": _now_iso(),
        })
        accepted.append({"rec_id": rec_id, "id": irp_id, "what": rec.get("what", "")})

    # Rebuild whenever anything was accepted or repaired: a retry of an
    # interrupted accept may find the ledger right and current.json still stale.
    if accepted and not dry_run:
        write_current(irp_dir, rebuild_current(read_ledger(irp_dir)))

    lines = [
        "IRP",
        f"Project: {project_root}",
        "Command: bootstrap --accept",
        f"Mode: {'dry-run' if dry_run else 'write'}",
        "",
    ]
    if dry_run:
        lines.append("DRY RUN. Nothing was added to the ledger or to reconstructions.jsonl. These would be accepted:")
    elif accepted:
        lines.append("Accepted. You confirmed these, so they are now ordinary decisions in the ledger:")
    if accepted:
        lines.append("")
        for a in accepted:
            lines.append(f"  {a['rec_id']} -> {a['id']}  {a['what'][:80]}")
        lines.append("")
    if problems:
        lines.append("Not accepted:")
        for pr in problems:
            lines.append(f"  {pr['rec_id']}: {pr['reason']}")
        lines.append("")
    if accepted and not dry_run:
        lines += [
            "Ledger:   .irp/ledger.jsonl  (appended)",
            "Current:  .irp/current.json  (rebuilt)",
            f"Marked accepted in .irp/{RECONSTRUCTIONS_FILE}",
        ]

    result: dict[str, Any] = {
        "command": "bootstrap",
        "status": "dry_run" if dry_run else ("accepted" if accepted else "nothing_accepted"),
        "accepted": [] if dry_run else accepted,
        "problems": problems,
        "text": "\n".join(lines).rstrip(),
    }
    if dry_run:
        result["would_accept"] = accepted
    return result

def _record(
    irp_dir: Path,
    candidates: list[dict[str, Any]],
    limit: int,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Dedupe against what is on disk now, assign REC ids, append (unless dry run).

    Returns (recorded, skipped). In write mode the caller holds the irp lock.
    """
    ledger = read_ledger(irp_dir)
    reconstructions = read_reconstructions(irp_dir)
    unique, skipped = _deduplicate(candidates, ledger + reconstructions)
    unique = unique[:limit]

    recorded_at = _now_iso()
    recorded: list[dict[str, Any]] = []
    rows = list(reconstructions)
    for candidate in unique:
        rec = _as_reconstruction(candidate, next_rec_id(rows, ledger), recorded_at)
        rows.append(rec)
        recorded.append(rec)
        if not dry_run:
            append_reconstruction(irp_dir, rec)
    return recorded, skipped


# ---------------------------------------------------------------------------
# Main command runner
# ---------------------------------------------------------------------------

def run_bootstrap(project_root: Path, irp_dir: Path, args) -> dict:
    from_sources: str = getattr(args, "from_source", "all")
    scan_path_arg: str | None = getattr(args, "path", None)
    dry_run: bool = getattr(args, "dry_run", False)
    limit: int = getattr(args, "limit", 50)
    write_report: bool = getattr(args, "write_report", False)
    accept_raw = getattr(args, "accept", None)

    # Decide on whether --accept was given at all, not on whether it held a usable
    # id: `--accept ""` must not fall through to a full scan that records guesses.
    if accept_raw is not None:
        accept_ids = _flatten_ids(accept_raw)
        if not accept_ids:
            return {
                "command": "bootstrap",
                "status": "nothing_accepted",
                "accepted": [],
                "problems": [{"rec_id": "", "reason": "no reconstruction ids were given"}],
                "text": (
                    "IRP\n"
                    f"Project: {project_root}\n"
                    "Command: bootstrap --accept\n\n"
                    "No reconstruction ids were given, so nothing was accepted and nothing was scanned.\n"
                    "Pass the id of a reconstruction, for example:\n"
                    "  irp bootstrap --accept REC-2026-10-08-001"
                ),
            }
        return _run_accept(project_root, irp_dir, accept_ids, dry_run)

    scan_path = Path(scan_path_arg) if scan_path_arg else project_root

    header = [
        "IRP",
        f"Project: {project_root}",
        "Command: bootstrap",
        f"Mode: {'dry-run' if dry_run else 'write'}",
        f"Sources: {from_sources}",
        "",
    ]

    all_candidates: list[dict[str, Any]] = []
    sources_scanned: list[str] = []

    # --- Git source ---
    if from_sources in ("git", "all"):
        commits = _run_git_log(limit, cwd=project_root)
        if commits:
            sources_scanned.append(f"git log (last {len(commits)} commits)")
            git_candidates = _extract_git_candidates(commits)
            all_candidates.extend(git_candidates)
        else:
            sources_scanned.append("git log (no commits found or not a git repo)")

    # --- Docs / files source ---
    if from_sources in ("docs", "files", "all"):
        files = _scan_files(scan_path)
        if files:
            sources_scanned.append(f"{scan_path} ({len(files)} files scanned)")
            doc_candidates = _extract_doc_candidates(files, project_root)
            all_candidates.extend(doc_candidates)
        else:
            sources_scanned.append(f"{scan_path} (no .md/.txt files found)")

    # Dedupe, give each candidate a REC id and append it. The scan above needs no
    # lock, but this step reads the ledger and reconstructions.jsonl and then
    # writes to the latter, so in write mode it runs under the lock and re-reads
    # what is on disk: a run that waited sees the other run's lines, and the two
    # can never hand out the same id. Dry run does the same on a scratch copy and
    # writes nothing.
    if dry_run:
        recorded, skipped = _record(irp_dir, all_candidates, limit, dry_run=True)
    else:
        with irp_lock(irp_dir):
            recorded, skipped = _record(irp_dir, all_candidates, limit, dry_run=False)

    if not recorded:
        msg_lines = header + [
            "No bootstrap candidates found.",
            "",
            "Sources scanned:",
        ] + [f"  - {s}" for s in sources_scanned] + [
            "",
            "This may mean:",
            "  - No decision-signal language detected in commit messages or docs",
            "  - All candidates were already in the ledger or in reconstructions.jsonl (duplicates)",
            "  - Try --from git or --from docs with --path to narrow the scan",
        ]
        return {"command": "bootstrap", "status": "empty", "text": "\n".join(msg_lines)}

    # Report
    report_path: Path | None = None
    if write_report:
        report_path = _write_report(
            irp_dir, sources_scanned, recorded,
            recorded if not dry_run else [],
            skipped, dry_run,
        )

    # Build terminal output
    separator = "─" * 56
    output_lines = header + [
        separator,
        "Sources scanned:",
    ] + [f"  - {s}" for s in sources_scanned] + [
        "",
        f"Candidates found:   {len(all_candidates)}",
        f"After dedup:        {len(recorded)}",
        f"Skipped:            {len(skipped)}",
        "",
    ]

    if dry_run:
        output_lines.append("DRY RUN. Nothing was added to the ledger or to reconstructions.jsonl.")
        output_lines.append("Preview of what a real run would record (the REC ids are provisional):")
        output_lines.append("")
    else:
        output_lines.append(f"Recorded:           {len(recorded)} reconstruction(s)")
        output_lines.append(f"File:               .irp/{RECONSTRUCTIONS_FILE}  (appended)")
        output_lines.append("Ledger:             .irp/ledger.jsonl and .irp/current.json were not touched")
        output_lines.append("")
    for c in recorded:
        output_lines.append(f"  [{c['id']}] {c['what'][:80]}")
        output_lines.append(f"    origin_mode: {c['origin_mode']}  confidence: {c['confidence']}")
        output_lines.append(f"    decided (estimate): {c.get('decided_at_estimate') or 'unknown'}")
        output_lines.append(f"    source_ref:  {str(c.get('source_ref', ''))[:60]}")
        output_lines.append("")

    output_lines += [
        separator,
        "WHAT HAPPENS NEXT",
        separator,
        "Nothing has been added to your ledger. These are guesses from git history",
        "and documents, not decisions anyone confirmed. Agents, exports, checks and",
        "evidence packages ignore them.",
        "",
        "To turn one into a real decision, read it and then accept it:",
        "  irp bootstrap --accept REC-YYYY-MM-DD-NNN    (several ids are fine)",
        "Accepting is your confirmation that it was a decision.",
        separator,
    ]

    if report_path:
        output_lines.append(f"Report written: .irp/bootstrap_reports/{report_path.name}")

    return {
        "command": "bootstrap",
        "status": "dry_run" if dry_run else "written",
        "sources_scanned": sources_scanned,
        "candidates_found": len(all_candidates),
        "candidates_written": len(recorded) if not dry_run else 0,
        "skipped": len(skipped),
        "entries": recorded,
        "report": str(report_path) if report_path else None,
        "text": "\n".join(output_lines),
    }
