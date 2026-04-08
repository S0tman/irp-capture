"""irp bootstrap — initialise or enrich .irp/ from existing project artifacts.

Provenance contract:
  Every bootstrapped entry is marked with origin_mode and bootstrapped=True.
  Entries derived from artifacts are never presented as first-hand captures.
  If evidence is weak, the entry is skipped or written at low confidence
  with an explicit uncertainty note in the 'why' field.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from irp.core.store import append_ledger_entry, next_irp_id, read_ledger, rebuild_current, write_current

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

def _run_git_log(limit: int) -> list[dict[str, str]]:
    """Return list of {hash, date, message} from local git log."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H|%as|%s"],
            capture_output=True, text=True, timeout=10,
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
            "Derived from git commit message. Original reasoning not captured in commit. "
            "Treat as a historical signal, not a confirmed decision record."
        )
        candidates.append({
            "type": "decision",
            "what": what,
            "why": why,
            "confidence": "low",
            "tags": ["bootstrap", "git"],
            "timestamp": commit["date"],
            "source": "bootstrap",
            "origin_mode": "bootstrap_git",
            "source_ref": commit["hash"],
            "bootstrapped": True,
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

def _extract_doc_candidates(files: list[Path]) -> list[dict[str, Any]]:
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
            why = (
                f"{context_line}  " if context_line else ""
            ) + (
                f"Extracted from {filepath.name} (line {i + 1}). "
                "Original context may be incomplete. Treat as a historical signal."
            )
            why = why.strip()[:_MAX_FIELD_LEN]
            candidates.append({
                "type": "decision",
                "what": what,
                "why": why,
                "confidence": "low",
                "tags": ["bootstrap", "docs"],
                "timestamp": date.today().isoformat(),
                "source": "bootstrap",
                "origin_mode": "bootstrap_docs",
                "source_ref": str(filepath),
                "bootstrapped": True,
            })
    return candidates

# ---------------------------------------------------------------------------
# Deduplication (simple — skip if 'what' already in ledger)
# ---------------------------------------------------------------------------

def _deduplicate(candidates: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_whats = {e.get("what", "").lower() for e in ledger}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        key = c.get("what", "").lower()
        if key in existing_whats or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

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
        f"## Candidates written: {len(candidates_written)}",
        f"## Skipped (duplicate or weak): {len(skipped)}",
        "",
        "## Caveats",
        "- All bootstrapped entries are marked with `bootstrapped: true` and `origin_mode`.",
        "- Confidence is set to `low` unless the signal is unusually explicit.",
        "- 'why' fields derived from artifacts are qualified with provenance notes.",
        "- These entries reflect historical signals, not first-hand decision captures.",
        "",
        "## Entries written",
    ]
    if not candidates_written:
        lines.append("None.")
    for entry in candidates_written:
        lines += [
            f"- **{entry.get('id', 'pending')}**: {entry.get('what', '')}",
            f"  Source: `{entry.get('origin_mode', '')}` — `{entry.get('source_ref', '')}`",
        ]
    if skipped:
        lines += ["", "## Skipped entries"]
        for entry in skipped:
            lines.append(f"- (duplicate) {entry.get('what', '')[:80]}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path

# ---------------------------------------------------------------------------
# Main command runner
# ---------------------------------------------------------------------------

def run_bootstrap(project_root: Path, irp_dir: Path, args) -> dict:
    from_sources: str = getattr(args, "from_source", "all")
    scan_path_arg: str | None = getattr(args, "path", None)
    dry_run: bool = getattr(args, "dry_run", False)
    limit: int = getattr(args, "limit", 50)
    write_report: bool = getattr(args, "write_report", False)

    scan_path = Path(scan_path_arg) if scan_path_arg else project_root

    header = [
        "IRP",
        f"Project: {project_root}",
        "Command: bootstrap",
        f"Mode: {'dry-run' if dry_run else 'write'}",
        f"Sources: {from_sources}",
        "",
    ]

    ledger = read_ledger(irp_dir)
    all_candidates: list[dict[str, Any]] = []
    sources_scanned: list[str] = []

    # --- Git source ---
    if from_sources in ("git", "all"):
        commits = _run_git_log(limit)
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
            doc_candidates = _extract_doc_candidates(files)
            all_candidates.extend(doc_candidates)
        else:
            sources_scanned.append(f"{scan_path} (no .md/.txt files found)")

    # Deduplicate against existing ledger
    unique_candidates = _deduplicate(all_candidates, ledger)
    skipped = [c for c in all_candidates if c not in unique_candidates]

    # Apply limit
    unique_candidates = unique_candidates[:limit]

    if not unique_candidates:
        msg_lines = header + [
            "No bootstrap candidates found.",
            "",
            "Sources scanned:",
        ] + [f"  - {s}" for s in sources_scanned] + [
            "",
            "This may mean:",
            "  - No decision-signal language detected in commit messages or docs",
            "  - All candidates were already in the ledger (duplicates)",
            "  - Try --from git or --from docs with --path to narrow the scan",
        ]
        return {"command": "bootstrap", "status": "empty", "text": "\n".join(msg_lines)}

    # Assign IDs (need fresh ledger state as we go)
    written: list[dict[str, Any]] = []
    current_ledger = list(ledger)

    if dry_run:
        # Assign provisional IDs for display only — do not write
        temp_ledger = list(current_ledger)
        for candidate in unique_candidates:
            candidate["id"] = next_irp_id(temp_ledger)
            temp_ledger.append(candidate)
    else:
        for candidate in unique_candidates:
            candidate["id"] = next_irp_id(current_ledger)
            append_ledger_entry(irp_dir, candidate)
            current_ledger.append(candidate)
            written.append(candidate)
        # Rebuild current.json once after all writes
        write_current(irp_dir, rebuild_current(current_ledger))

    # Report
    report_path: Path | None = None
    if write_report:
        report_path = _write_report(
            irp_dir, sources_scanned, unique_candidates,
            written if not dry_run else [],
            skipped, dry_run,
        )

    # Build terminal output
    separator = "─" * 56
    output_lines = header + [
        separator,
        f"Sources scanned:",
    ] + [f"  - {s}" for s in sources_scanned] + [
        "",
        f"Candidates found:   {len(all_candidates)}",
        f"After dedup:        {len(unique_candidates)}",
        f"Skipped:            {len(skipped)}",
        "",
    ]

    if dry_run:
        output_lines.append("DRY RUN — no entries written. Candidate preview:")
        output_lines.append("")
        for c in unique_candidates:
            output_lines.append(f"  [{c['id']}] {c['what'][:80]}")
            output_lines.append(f"    origin_mode: {c['origin_mode']}  confidence: {c['confidence']}")
            output_lines.append(f"    source_ref:  {str(c.get('source_ref', ''))[:60]}")
            output_lines.append("")
    else:
        output_lines.append(f"Written to ledger:  {len(written)} entries")
        output_lines.append(f"Ledger:             .irp/ledger.jsonl  ← updated")
        output_lines.append(f"Current:            .irp/current.json  ← rebuilt")
        output_lines.append("")
        for c in written:
            output_lines.append(f"  [{c['id']}] {c['what'][:80]}")
            output_lines.append(f"    origin_mode: {c['origin_mode']}  confidence: {c['confidence']}")
            output_lines.append("")

    output_lines += [
        separator,
        "PROVENANCE NOTE",
        separator,
        "All bootstrapped entries are marked bootstrapped=True.",
        "They reflect historical signals — not first-hand decision captures.",
        "Review and prune entries that do not represent real decisions.",
        separator,
    ]

    if report_path:
        output_lines.append(f"Report written: .irp/bootstrap_reports/{report_path.name}")

    return {
        "command": "bootstrap",
        "status": "dry_run" if dry_run else "written",
        "sources_scanned": sources_scanned,
        "candidates_found": len(all_candidates),
        "candidates_written": len(written) if not dry_run else 0,
        "skipped": len(skipped),
        "entries": unique_candidates,
        "report": str(report_path) if report_path else None,
        "text": "\n".join(output_lines),
    }
