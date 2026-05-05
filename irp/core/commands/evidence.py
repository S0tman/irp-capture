"""irp export evidence — EU AI Act decision provenance evidence package.

Generates a structured Markdown document mapping IRP ledger entries to EU AI Act
Articles 12 (logging), 13 (transparency), and 14 (human oversight).

Design rules:
  - No LLM calls. No inference. Deterministic mapping only.
  - All decisions qualify as Article 12 evidence (append-only, timestamped, human-confirmed).
  - Article 14 evidence: every decision with a confirmed_by field is a human oversight event.
  - Article 13 evidence: decisions about scope, use, limitations (keyword heuristic).
  - --demo flag: uses built-in sample data, never touches the ledger.
  - Output is always regenerable. The ledger remains the source of truth.
  - Does not constitute legal advice.

IRP decision: IRP-2026-05-04-002 (irp export evidence as pre-sales gate for
regulated industry conversations).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from irp.core.store import read_ledger

# ── article keyword heuristics ────────────────────────────────────────────────

# Article 13 — transparency / IFU: decisions about what the system does,
# its scope, intended use, limitations, and how deployers should use it.
_ART13_KEYWORDS = {
    "intended use", "use case", "scope", "limitation", "capability",
    "purpose", "deploy", "user", "customer", "model", "system",
    "transparency", "ifu", "instruction", "disclosure", "inform",
    "document", "specification", "constraint", "boundary",
}

# Article 14 — human oversight: decisions about agent control, approval
# chains, escalation, human review, and intervention rights.
_ART14_KEYWORDS = {
    "agent", "automat", "human", "oversight", "approv", "review",
    "confirm", "escalat", "intervene", "halt", "stop", "override",
    "monitor", "supervise", "control", "audit", "verify", "check",
    "sign off", "sign-off", "authoris", "authoriz", "responsible",
}

def _article_tags(entry: dict[str, Any]) -> list[str]:
    """Return which EU AI Act articles this decision is evidence for."""
    text = (
        (entry.get("decision") or entry.get("what") or "") + " " +
        (entry.get("why") or "")
    ).lower()

    tags = ["Art. 12"]  # every decision qualifies

    if entry.get("confirmed_by"):
        tags.append("Art. 14")
    else:
        for kw in _ART14_KEYWORDS:
            if kw in text:
                tags.append("Art. 14")
                break

    for kw in _ART13_KEYWORDS:
        if kw in text:
            tags.append("Art. 13")
            break

    return tags

def _decision_text(entry: dict[str, Any]) -> str:
    return (entry.get("decision") or entry.get("what") or "").strip()

def _why_text(entry: dict[str, Any]) -> str:
    return (entry.get("why") or "").strip()

def _format_ts(ts: str) -> str:
    if not ts:
        return "—"
    return ts[:10] if len(ts) >= 10 else ts

# ── document builder ──────────────────────────────────────────────────────────

_DISCLAIMER = (
    "> **Note:** This document was generated from an IRP decision ledger. "
    "IRP provides an append-only, human-confirmed record of decisions governing AI deployments. "
    "This document supports compliance assessment but does not constitute legal advice. "
    "Consult a qualified legal practitioner for advice specific to your organisation."
)

_ART12_INTRO = """\
Article 12 requires that high-risk AI systems maintain logs sufficient to trace \
their operation. IRP provides an append-only, timestamped, human-confirmed decision \
ledger. Every entry below was captured at the moment the decision was made and cannot \
be modified after the fact. This satisfies the core logging and traceability requirement.

**What this record demonstrates:**
- Each governing decision is timestamped and sequenced
- Each decision records who confirmed it (human oversight anchor)
- Decisions are append-only — no entry can be retroactively altered
- The reasoning behind each decision is preserved, not just the outcome\
"""

_ART14_INTRO = """\
Article 14 requires that high-risk AI systems be designed to allow effective \
human oversight. The following decisions document human oversight events: cases \
where a natural person confirmed, approved, constrained, or governed what the \
AI system was permitted to do. The `confirmed_by` field on each entry is the \
human oversight anchor.\
"""

_ART13_INTRO = """\
Article 13 requires providers to ensure AI systems are sufficiently transparent \
to deployers. The following decisions document scope, intended use, capabilities, \
limitations, and governance constraints — the information a deployer needs to \
understand and correctly use the system.\
"""

def _format_entry_full(entry: dict[str, Any], show_articles: bool = True) -> str:
    irp_id = entry.get("id", "unknown")
    ts = _format_ts(str(entry.get("timestamp", "")))
    decision = _decision_text(entry)
    why = _why_text(entry)
    confirmed_by = entry.get("confirmed_by", "")
    source = entry.get("source", "")
    context = entry.get("context", "")
    articles = _article_tags(entry)

    lines = [f"### {irp_id}"]
    lines.append("")

    if decision:
        lines.append(f"**Decision:** {decision}")
    if why:
        lines.append(f"**Reasoning:** {why}")

    meta = []
    if ts and ts != "—":
        meta.append(f"Date: {ts}")
    if confirmed_by:
        meta.append(f"Confirmed by: `{confirmed_by}`")
    if source:
        meta.append(f"Source: {source}")
    if context:
        meta.append(f"Context: {context}")
    if show_articles:
        meta.append(f"Evidence for: {' · '.join(articles)}")

    if meta:
        lines.append("")
        lines.append("  ".join(f"_{m}_" for m in meta))

    lines.append("")
    return "\n".join(lines)

def _build_evidence_md(
    decisions: list[dict[str, Any]],
    project_root: Path,
    demo: bool = False,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Date range
    timestamps = [str(e.get("timestamp", ""))[:10] for e in decisions if e.get("timestamp")]
    date_range = f"{min(timestamps)} to {max(timestamps)}" if timestamps else "—"

    # Source summary
    sources = sorted({e.get("source", "unknown") for e in decisions if e.get("source")})
    confirmed_count = sum(1 for e in decisions if e.get("confirmed_by"))

    # Article buckets
    art12 = decisions  # all
    art14 = [e for e in decisions if "Art. 14" in _article_tags(e)]
    art13 = [e for e in decisions if "Art. 13" in _article_tags(e)]

    source_note = " *(built-in sample data — does not reflect your ledger)*" if demo else ""

    parts: list[str] = []

    parts.append(f"""\
# AI Agent Governance — Evidence Report
## EU AI Act Decision Provenance Record{source_note}

| | |
|---|---|
| Generated | {generated_at} |
| Project | {project_root} |
| Total decisions | {len(decisions)} |
| Human-confirmed | {confirmed_count} |
| Date range | {date_range} |
| Sources | {', '.join(sources) if sources else '—'} |
| Articles covered | Art. 12 · Art. 13 · Art. 14 |

{_DISCLAIMER}

---
""")

    # ── Art. 12 ──────────────────────────────────────────────────────────────
    parts.append(f"""\
## Article 12 — Logging and Traceability

{_ART12_INTRO}

**Decisions on record: {len(art12)}**

""")
    for entry in art12:
        parts.append(_format_entry_full(entry, show_articles=False))

    parts.append("---\n")

    # ── Art. 14 ──────────────────────────────────────────────────────────────
    parts.append(f"""\
## Article 14 — Human Oversight

{_ART14_INTRO}

**Human oversight events on record: {len(art14)}**

""")
    if art14:
        for entry in art14:
            parts.append(_format_entry_full(entry, show_articles=False))
    else:
        parts.append(
            "*No decisions in the ledger contain explicit human oversight evidence. "
            "Capture decisions with `confirmed_by` set to populate this section.*\n\n"
        )

    parts.append("---\n")

    # ── Art. 13 ──────────────────────────────────────────────────────────────
    parts.append(f"""\
## Article 13 — Transparency and Instructions for Use

{_ART13_INTRO}

**Transparency decisions on record: {len(art13)}**

""")
    if art13:
        for entry in art13:
            parts.append(_format_entry_full(entry, show_articles=False))
    else:
        parts.append(
            "*No decisions in the ledger were identified as scope or transparency decisions. "
            "Capture decisions about intended use, system limitations, or deployment constraints "
            "to populate this section.*\n\n"
        )

    parts.append("---\n")

    # ── summary table ─────────────────────────────────────────────────────────
    parts.append(f"""\
## Evidence Summary

| Article | Requirement | Records | Status |
|---|---|---|---|
| Art. 12 | Logging and traceability | {len(art12)} | {'✅ Covered' if art12 else '⚠️ No decisions'} |
| Art. 14 | Human oversight evidence | {len(art14)} | {'✅ Covered' if art14 else '⚠️ Add confirmed_by'} |
| Art. 13 | Transparency / scope | {len(art13)} | {'✅ Covered' if art13 else '⚠️ Capture scope decisions'} |

**What this record does not cover:**
- Model safety validation or bias testing
- Legal risk classification of the AI system
- Conformity assessment (Art. 43)
- Training data quality evidence
- Registration in the EU AI Act database (Art. 49)

This evidence package covers decision provenance and human oversight records only.
For full EU AI Act compliance, additional technical documentation is required.

---

*Generated by IRP (Intent Record Protocol) — irp export evidence*
*Regenerate: `irp export evidence --force`*
*Source of truth: `.irp/ledger.jsonl`*
""")

    return "".join(parts)

# ── sample data ───────────────────────────────────────────────────────────────

_SAMPLE_DECISIONS_JSON = """[
  {
    "id": "IRP-2026-03-10-001",
    "timestamp": "2026-03-10",
    "decision": "Deploy AI agent for loan pre-screening for Nordic SME customers",
    "why": "Reduces manual review time from 4 days to same-day. Agent operates in pre-screening phase only. Final credit decisions remain with licensed loan officers.",
    "confirmed_by": "anna.lindqvist",
    "source": "cli",
    "context": "Nordic SME lending platform — Q1 2026"
  },
  {
    "id": "IRP-2026-03-10-002",
    "timestamp": "2026-03-10",
    "decision": "Agent classified as high-risk under EU AI Act Annex III point 5(b) — creditworthiness assessment",
    "why": "System evaluates natural persons for credit eligibility. Annex III 5(b) applies explicitly. Full Art. 9-15 obligations in scope. Legal reviewed and confirmed.",
    "confirmed_by": "legal.team",
    "source": "cli",
    "context": "Nordic SME lending platform — EU AI Act classification"
  },
  {
    "id": "IRP-2026-03-12-001",
    "timestamp": "2026-03-12",
    "decision": "All agent recommendations require human loan officer review before any customer communication",
    "why": "Art. 14 human oversight requirement. Agent output is a recommendation input to the officer, not a final decision. No automated customer-facing output permitted.",
    "confirmed_by": "compliance.officer",
    "source": "cli",
    "context": "Nordic SME lending platform — oversight design"
  },
  {
    "id": "IRP-2026-03-15-001",
    "timestamp": "2026-03-15",
    "decision": "Escalation required for any loan application exceeding 500,000 SEK — senior credit manager must confirm",
    "why": "Risk threshold above which automated pre-screening is insufficient. High-value decisions require senior human oversight. Tested against 18 months of historical decisions.",
    "confirmed_by": "risk.committee",
    "source": "cli",
    "context": "Nordic SME lending platform — escalation policy"
  },
  {
    "id": "IRP-2026-03-18-001",
    "timestamp": "2026-03-18",
    "decision": "Agent must display confidence score and top three contributing factors on every recommendation",
    "why": "Art. 13 transparency requirement. Loan officers cannot make informed oversight decisions without understanding what drove the recommendation. Rejected: displaying score only (insufficient for meaningful review).",
    "confirmed_by": "product.lead",
    "source": "cli",
    "context": "Nordic SME lending platform — transparency design"
  },
  {
    "id": "IRP-2026-03-20-001",
    "timestamp": "2026-03-20",
    "decision": "Agent scope limited to Swedish and Norwegian SME applicants with registered VAT number — no consumer credit",
    "why": "Intended use boundary. Consumer credit requires additional regulatory compliance outside current scope. Prevents scope creep into higher-risk categories.",
    "confirmed_by": "cto",
    "source": "cli",
    "context": "Nordic SME lending platform — scope definition"
  },
  {
    "id": "IRP-2026-04-01-001",
    "timestamp": "2026-04-01",
    "decision": "Model retrained quarterly — previous version archived with full decision log and performance metrics",
    "why": "Art. 72 post-market monitoring requirement. Version history enables audit trail. Retraining trigger: drift exceeding 3% on precision metric over 30-day rolling window.",
    "confirmed_by": "ml.lead",
    "source": "cli",
    "context": "Nordic SME lending platform — model governance"
  },
  {
    "id": "IRP-2026-04-05-001",
    "timestamp": "2026-04-05",
    "decision": "Agent cannot access applicant data predating 2024-01-01",
    "why": "Data quality limitation — pre-2024 records incomplete and not representative of current SME risk profile. Using older data degrades model performance and introduces unvalidated bias risk.",
    "confirmed_by": "data.governance",
    "source": "cli",
    "context": "Nordic SME lending platform — data constraints"
  },
  {
    "id": "IRP-2026-04-10-001",
    "timestamp": "2026-04-10",
    "decision": "Override log required for every case where loan officer departs from agent recommendation",
    "why": "Art. 14 oversight record. Override reason must be documented before file is closed. Enables post-market monitoring of agent accuracy and systematic bias detection.",
    "confirmed_by": "compliance.officer",
    "source": "cli",
    "context": "Nordic SME lending platform — oversight records"
  },
  {
    "id": "IRP-2026-04-15-001",
    "timestamp": "2026-04-15",
    "decision": "Fundamental Rights Impact Assessment completed and filed — updated on any material system change",
    "why": "Art. 27 FRIA obligation for high-risk AI deployers. Assessment covers discrimination risk across protected characteristics. Filed with legal. Trigger for update: model retraining or scope change.",
    "confirmed_by": "legal.team",
    "source": "cli",
    "context": "Nordic SME lending platform — FRIA"
  }
]"""

def _load_sample_decisions() -> list[dict[str, Any]]:
    return json.loads(_SAMPLE_DECISIONS_JSON)

# ── public entry point ────────────────────────────────────────────────────────

def run_export_evidence(project_root: Path, irp_dir: Path, args) -> dict:
    demo = bool(getattr(args, "demo", False))
    output_arg = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))
    as_json = bool(getattr(args, "json", False))

    # Resolve output path
    default_name = "EVIDENCE-demo.md" if demo else "EVIDENCE.md"
    output_path = Path(output_arg) if output_arg else (project_root / default_name)
    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    # Load decisions
    if demo:
        decisions = _load_sample_decisions()
    else:
        ledger = read_ledger(irp_dir)
        decisions = [
            row for row in ledger
            if row.get("type") == "decision" or (row.get("decision") or row.get("what"))
        ]

    if not decisions and not demo:
        nudge = (
            "No decisions in the ledger yet.\n"
            "Capture one with `irp capture`, or run `irp export evidence --demo` "
            "to see a sample evidence package for a regulated AI deployment."
        )
        if as_json:
            return {"command": "export.evidence", "status": "empty", "text": nudge}
        return {"command": "export.evidence", "status": "empty", "text": nudge}

    # Build document
    body = _build_evidence_md(decisions, project_root, demo=demo)

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        f"Command: export evidence{'  [demo]' if demo else ''}",
        "",
    ]

    # Check existing
    if output_path.exists() and not force:
        text = "\n".join(header + [
            f"Refusing to overwrite existing file: {output_path}",
            "Re-run with --force to overwrite.",
        ])
        return {
            "command": "export.evidence",
            "status": "exists",
            "output_path": str(output_path),
            "text": text,
        }

    # Write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        try:
            output_path.chmod(0o644)
        except OSError:
            pass

    output_path.write_text(body, encoding="utf-8")

    try:
        output_path.chmod(0o444)
    except OSError:
        pass

    art14_count = sum(1 for e in decisions if "Art. 14" in _article_tags(e))
    art13_count = sum(1 for e in decisions if "Art. 13" in _article_tags(e))

    summary_lines = [
        f"Wrote {output_path}",
        f"Decisions: {len(decisions)} total · {art14_count} Art. 14 · {art13_count} Art. 13",
        "Lock:    file is read-only — `chmod +w` to override",
        "",
        "Regenerate any time with:",
        f"  irp export evidence --force{' --demo' if demo else ''}",
    ]
    if demo:
        summary_lines.append("")
        summary_lines.append(
            "This is sample data. Run `irp export evidence` (without --demo) "
            "to generate a report from your actual ledger."
        )

    text = "\n".join(header + summary_lines)

    result = {
        "command": "export.evidence",
        "status": "ok",
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "art12_count": len(decisions),
        "art14_count": art14_count,
        "art13_count": art13_count,
        "demo": demo,
        "text": text,
    }

    if as_json:
        return result

    return result
