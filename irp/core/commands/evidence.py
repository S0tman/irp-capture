"""irp export evidence — pluggable compliance evidence package.

Generates a structured Markdown document mapping IRP ledger entries to a
compliance framework's requirements. The EU AI Act (Art. 12/13/14) is the
default; SOC 2, GDPR, and ISO 42001 are built-in alternatives.

Usage:
    irp export evidence                            # EU AI Act (default)
    irp export evidence --framework euaiact
    irp export evidence --framework soc2
    irp export evidence --framework gdpr
    irp export evidence --framework iso42001
    irp export evidence --framework custom --config path/to/framework.json
    irp export evidence --demo                     # built-in sample data

Design rules:
  - No LLM calls. No inference. Deterministic mapping only.
  - Each framework defines sections; each section declares which decisions
    qualify via keywords, confirmed_by, or an all-qualify flag.
  - The framework schema is open — custom frameworks load from JSON.
  - --demo uses built-in sample data, never touches the ledger.
  - Output is always regenerable. The ledger remains the source of truth.
  - Does not constitute legal advice.

IRP decisions: IRP-2026-05-05-001 (evidence as pre-sales gate),
               IRP-2026-05-04-002 (regulated industry conversations).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store import read_ledger


# ── framework schema ──────────────────────────────────────────────────────────
#
# A framework is a dict with:
#   id         str           machine name
#   name       str           display name
#   sections   list[Section] ordered list of evidence sections
#   disclaimer str (opt)     override the standard legal disclaimer
#   gaps       list[str]     "What this record does not cover" bullets
#
# A Section is a dict with:
#   id                    str           machine name ("art12", "cc7.2", …)
#   name                  str           display heading
#   intro                 str           requirement explanation paragraph
#   keywords              list[str]     word fragments matched in what+why
#   all_qualify           bool (opt)    every decision qualifies
#   confirmed_by_qualifies bool (opt)   confirmed_by field makes it qualify
# ─────────────────────────────────────────────────────────────────────────────

_STANDARD_DISCLAIMER = (
    "> **Note:** This document was generated from an IRP decision ledger. "
    "IRP provides an append-only, human-confirmed record of decisions governing AI deployments. "
    "This document supports compliance assessment but does not constitute legal advice. "
    "Consult a qualified legal practitioner for advice specific to your organisation."
)

_EUAIACT_FRAMEWORK: dict[str, Any] = {
    "id": "euaiact",
    "name": "EU AI Act (Art. 12 · 13 · 14)",
    "sections": [
        {
            "id": "art12",
            "name": "Article 12 — Logging and Traceability",
            "intro": (
                "Article 12 requires that high-risk AI systems maintain logs sufficient "
                "to trace their operation. IRP provides an append-only, timestamped, "
                "human-confirmed decision ledger. Every entry below was captured at the "
                "moment the decision was made and cannot be modified after the fact. "
                "This satisfies the core logging and traceability requirement.\n\n"
                "**What this record demonstrates:**\n"
                "- Each governing decision is timestamped and sequenced\n"
                "- Each decision records who confirmed it (human oversight anchor)\n"
                "- Decisions are append-only — no entry can be retroactively altered\n"
                "- The reasoning behind each decision is preserved, not just the outcome"
            ),
            "all_qualify": True,
        },
        {
            "id": "art14",
            "name": "Article 14 — Human Oversight",
            "intro": (
                "Article 14 requires that high-risk AI systems be designed to allow "
                "effective human oversight. The following decisions document human "
                "oversight events: cases where a natural person confirmed, approved, "
                "constrained, or governed what the AI system was permitted to do. "
                "The `confirmed_by` field on each entry is the human oversight anchor."
            ),
            "confirmed_by_qualifies": True,
            "keywords": [
                "agent", "automat", "human", "oversight", "approv", "review",
                "confirm", "escalat", "intervene", "halt", "stop", "override",
                "monitor", "supervise", "control", "audit", "verify", "check",
                "sign off", "sign-off", "authoris", "authoriz", "responsible",
            ],
        },
        {
            "id": "art13",
            "name": "Article 13 — Transparency and Instructions for Use",
            "intro": (
                "Article 13 requires providers to ensure AI systems are sufficiently "
                "transparent to deployers. The following decisions document scope, "
                "intended use, capabilities, limitations, and governance constraints — "
                "the information a deployer needs to understand and correctly use the system."
            ),
            "keywords": [
                "intended use", "use case", "scope", "limitation", "capability",
                "purpose", "deploy", "user", "customer", "model", "system",
                "transparency", "ifu", "instruction", "disclosure", "inform",
                "document", "specification", "constraint", "boundary",
            ],
        },
    ],
    "gaps": [
        "Model safety validation or bias testing",
        "Legal risk classification of the AI system",
        "Conformity assessment (Art. 43)",
        "Training data quality evidence",
        "Registration in the EU AI Act database (Art. 49)",
    ],
}

_SOC2_FRAMEWORK: dict[str, Any] = {
    "id": "soc2",
    "name": "SOC 2 Type II (CC7.2 · CC6.1 · CC4.1)",
    "sections": [
        {
            "id": "cc7.2",
            "name": "CC7.2 — System Monitoring",
            "intro": (
                "CC7.2 requires that the entity monitors system components and "
                "operations for anomalies that may indicate malicious acts, natural "
                "disasters, or errors affecting the entity's ability to meet its objectives. "
                "IRP decision records demonstrate that system-level monitoring and "
                "operational constraints have been captured, documented, and confirmed."
            ),
            "all_qualify": True,
        },
        {
            "id": "cc6.1",
            "name": "CC6.1 — Logical and Physical Access Controls",
            "intro": (
                "CC6.1 requires that logical access to systems and data is managed to "
                "protect against threats from sources outside and inside the entity. "
                "The following decisions document access control decisions, permission "
                "boundaries, authentication policies, and user-level constraints."
            ),
            "keywords": [
                "access", "permission", "auth", "user", "role", "privilege",
                "restrict", "allow", "deny", "credential", "token", "key",
                "identity", "account", "login", "password", "scope",
            ],
        },
        {
            "id": "cc4.1",
            "name": "CC4.1 — Risk Assessment",
            "intro": (
                "CC4.1 requires that the entity identifies, selects, and develops "
                "risk assessment activities. The following decisions document risk "
                "identification, impact assessments, mitigation choices, and threat "
                "evaluations that governed system design and operational decisions."
            ),
            "keywords": [
                "risk", "assess", "impact", "evaluate", "threat", "mitigat",
                "vulnerability", "exposure", "hazard", "likelihood", "severity",
                "acceptable", "unacceptable", "tradeoff",
            ],
        },
    ],
    "gaps": [
        "SOC 2 Trust Service Criteria beyond CC7.2, CC6.1, CC4.1",
        "Technical security controls and penetration testing evidence",
        "Vendor management documentation",
        "Incident response procedures",
        "Employee background check and training records",
    ],
}

_GDPR_FRAMEWORK: dict[str, Any] = {
    "id": "gdpr",
    "name": "GDPR (Art. 30 · 22 · 5)",
    "sections": [
        {
            "id": "art30",
            "name": "Article 30 — Records of Processing Activities",
            "intro": (
                "Article 30 requires controllers and processors to maintain records "
                "of their processing activities. IRP decision records document the "
                "governance decisions that defined processing scope, purpose, and constraints. "
                "Every decision is timestamped, sourced, and human-confirmed — "
                "satisfying the traceability requirement of Art. 30."
            ),
            "all_qualify": True,
        },
        {
            "id": "art22",
            "name": "Article 22 — Automated Decision-Making",
            "intro": (
                "Article 22 restricts automated individual decision-making with legal "
                "or similarly significant effects. The following decisions document "
                "constraints and safeguards placed on automated or AI-driven processing "
                "that affects data subjects, and the human oversight mechanisms designed "
                "to ensure meaningful intervention rights."
            ),
            "confirmed_by_qualifies": True,
            "keywords": [
                "automat", "decision", "profil", "model", "agent", "infer",
                "predict", "classif", "score", "recommend", "individual",
                "subject", "data", "process", "algorithm",
            ],
        },
        {
            "id": "art5",
            "name": "Article 5 — Accountability and Data Principles",
            "intro": (
                "Article 5(2) requires that the controller be responsible for and able "
                "to demonstrate compliance with the GDPR data principles. The following "
                "decisions document accountability assignments, data governance choices, "
                "and principle-aligned constraints on system behaviour."
            ),
            "confirmed_by_qualifies": True,
            "keywords": [
                "account", "responsible", "owner", "govern", "lawful", "fair",
                "purpose", "minimal", "accurate", "retention", "integrity",
                "confidential", "privacy", "data protection",
            ],
        },
    ],
    "gaps": [
        "GDPR Articles beyond Art. 30, 22, and 5",
        "Data subject rights fulfilment procedures",
        "Data Processing Agreements (DPAs) with processors",
        "Data Protection Impact Assessments (DPIAs)",
        "Cross-border transfer mechanisms",
    ],
}

_ISO42001_FRAMEWORK: dict[str, Any] = {
    "id": "iso42001",
    "name": "ISO 42001 AI Management System (6.1 · 8.4 · 9.1)",
    "sections": [
        {
            "id": "6.1",
            "name": "Clause 6.1 — AI Risk Management",
            "intro": (
                "ISO 42001 Clause 6.1 requires that organisations plan actions to address "
                "AI risks and opportunities. The following decisions document risk "
                "identification, impact analysis, mitigation strategies, and the "
                "governance constraints designed to manage AI-related risks."
            ),
            "keywords": [
                "risk", "threat", "assess", "mitigat", "impact", "hazard",
                "likelihood", "severity", "control", "safeguard", "barrier",
                "prevent", "reduce", "acceptable", "residual",
            ],
        },
        {
            "id": "8.4",
            "name": "Clause 8.4 — Documentation of AI System Information",
            "intro": (
                "ISO 42001 Clause 8.4 requires that documented information about "
                "the AI system be maintained. IRP provides the append-only evidence "
                "substrate for this requirement — every decision is documented with "
                "its rationale at the time of capture."
            ),
            "all_qualify": True,
        },
        {
            "id": "9.1",
            "name": "Clause 9.1 — Monitoring, Measurement and Evaluation",
            "intro": (
                "ISO 42001 Clause 9.1 requires organisations to evaluate AI system "
                "performance, including monitoring for drift, degradation, and bias. "
                "The following decisions document monitoring choices, measurement "
                "criteria, evaluation thresholds, and performance governance constraints."
            ),
            "keywords": [
                "monitor", "metric", "measure", "evaluat", "perform", "drift",
                "degrad", "bias", "accuracy", "precision", "recall", "threshold",
                "benchmark", "baseline", "kpi", "indicator",
            ],
        },
    ],
    "gaps": [
        "ISO 42001 clauses beyond 6.1, 8.4, and 9.1",
        "AI system design specifications and architecture documentation",
        "Supplier and third-party AI system evaluations",
        "Competence and awareness records",
        "Internal audit documentation",
    ],
}

_BUILTIN_FRAMEWORKS: dict[str, dict[str, Any]] = {
    "euaiact":  _EUAIACT_FRAMEWORK,
    "soc2":     _SOC2_FRAMEWORK,
    "gdpr":     _GDPR_FRAMEWORK,
    "iso42001": _ISO42001_FRAMEWORK,
}

_DEFAULT_FRAMEWORK = "euaiact"


# ── decision → section matching ───────────────────────────────────────────────

def _matches_section(entry: dict[str, Any], section: dict[str, Any]) -> bool:
    """Return True if this entry qualifies as evidence for this section."""
    if section.get("all_qualify"):
        return True

    if section.get("confirmed_by_qualifies") and entry.get("confirmed_by"):
        return True

    keywords = section.get("keywords") or []
    if not keywords:
        return False

    text = (
        (entry.get("decision") or entry.get("what") or "") + " " +
        (entry.get("why") or "")
    ).lower()

    return any(kw in text for kw in keywords)


def _section_tags(entry: dict[str, Any], framework: dict[str, Any]) -> list[str]:
    """Return section names this entry qualifies for."""
    return [
        s["name"] for s in framework.get("sections", [])
        if _matches_section(entry, s)
    ]


# ── helpers ───────────────────────────────────────────────────────────────────

def _decision_text(entry: dict[str, Any]) -> str:
    return (entry.get("decision") or entry.get("what") or "").strip()


def _why_text(entry: dict[str, Any]) -> str:
    return (entry.get("why") or "").strip()


def _format_ts(ts: str) -> str:
    if not ts:
        return "—"
    return ts[:10] if len(ts) >= 10 else ts


def _truncate(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── document builder ──────────────────────────────────────────────────────────

def _format_entry_full(entry: dict[str, Any]) -> str:
    irp_id = entry.get("id", "unknown")
    ts = _format_ts(str(entry.get("timestamp", "")))
    decision = _decision_text(entry)
    why = _why_text(entry)
    confirmed_by = entry.get("confirmed_by", "")
    source = entry.get("source", "")
    context = entry.get("context", "")

    lines = [f"### {irp_id}", ""]

    if decision:
        lines.append(f"**Decision:** {_truncate(decision)}")
    if why:
        lines.append(f"**Reasoning:** {_truncate(why)}")

    meta = []
    if ts and ts != "—":
        meta.append(f"Date: {ts}")
    if confirmed_by:
        meta.append(f"Confirmed by: `{confirmed_by}`")
    if source:
        meta.append(f"Source: {source}")
    if context:
        meta.append(f"Context: {context}")

    if meta:
        lines.append("")
        lines.append("  ".join(f"_{m}_" for m in meta))

    lines.append("")
    return "\n".join(lines)


def _build_evidence_md(
    decisions: list[dict[str, Any]],
    framework: dict[str, Any],
    project_root: Path,
    demo: bool = False,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fw_name = framework.get("name", framework.get("id", "custom"))
    fw_id = framework.get("id", "custom")

    timestamps = [str(e.get("timestamp", ""))[:10] for e in decisions if e.get("timestamp")]
    date_range = f"{min(timestamps)} to {max(timestamps)}" if timestamps else "—"

    sources = sorted({e.get("source", "unknown") for e in decisions if e.get("source")})
    confirmed_count = sum(1 for e in decisions if e.get("confirmed_by"))

    sections = framework.get("sections", [])
    sections_covered = " · ".join(s["id"] for s in sections)
    source_note = " *(built-in sample data — does not reflect your ledger)*" if demo else ""

    disclaimer = framework.get("disclaimer") or _STANDARD_DISCLAIMER

    parts: list[str] = []

    parts.append(f"""\
# AI Agent Governance — Evidence Report
## {fw_name} — Decision Provenance Record{source_note}

| | |
|---|---|
| Generated | {generated_at} |
| Framework | {fw_name} |
| Project | {project_root} |
| Total decisions | {len(decisions)} |
| Human-confirmed | {confirmed_count} |
| Date range | {date_range} |
| Sources | {', '.join(sources) if sources else '—'} |
| Sections covered | {sections_covered} |

{disclaimer}

---
""")

    # ── per-section evidence blocks ───────────────────────────────────────────
    for section in sections:
        sid = section.get("id", "?")
        sname = section.get("name", sid)
        intro = section.get("intro", "")
        qualified = [e for e in decisions if _matches_section(e, section)]

        parts.append(f"## {sname}\n\n")
        if intro:
            parts.append(f"{intro}\n\n")
        parts.append(f"**Decisions on record: {len(qualified)}**\n\n")

        if qualified:
            for entry in qualified:
                parts.append(_format_entry_full(entry))
        else:
            parts.append(
                f"*No decisions qualified for this section. "
                f"Capture decisions relevant to {sid} to populate this section.*\n\n"
            )

        parts.append("---\n")

    # ── summary table ─────────────────────────────────────────────────────────
    table_rows = []
    for section in sections:
        sid = section.get("id", "?")
        sname = section.get("name", sid)
        qualified = [e for e in decisions if _matches_section(e, section)]
        status = "✅ Covered" if qualified else "⚠️ No decisions"
        table_rows.append(f"| {sid} | {sname} | {len(qualified)} | {status} |")

    table = "\n".join(table_rows)
    gaps = framework.get("gaps") or []
    gap_bullets = "\n".join(f"- {g}" for g in gaps)

    parts.append(f"""\
## Evidence Summary

| Section | Requirement | Records | Status |
|---|---|---|---|
{table}

""")
    if gap_bullets:
        parts.append(
            f"**What this record does not cover:**\n{gap_bullets}\n\n"
            "This evidence package covers decision provenance and human oversight records only. "
            "For full compliance certification, additional documentation is required.\n\n"
        )

    parts.append(f"""\
---

*Generated by IRP (Intent Record Protocol) — irp export evidence --framework {fw_id}*
*Regenerate: `irp export evidence --framework {fw_id} --force`*
*Source of truth: `.irp/ledger.jsonl`*
""")

    return "".join(parts)


# ── custom framework loader ───────────────────────────────────────────────────

def _load_custom_framework(config_path: Path) -> dict[str, Any] | str:
    """Load a custom framework JSON. Returns the framework dict or an error string."""
    if not config_path.exists():
        return f"Custom framework config not found: {config_path}"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in framework config: {exc}"

    # Basic validation.
    if not isinstance(data, dict):
        return "Framework config must be a JSON object."
    if not data.get("sections"):
        return "Framework config must have a 'sections' list with at least one section."
    for i, sec in enumerate(data["sections"]):
        if not sec.get("name"):
            return f"Section {i} is missing a 'name' field."

    # Fill defaults.
    data.setdefault("id", "custom")
    data.setdefault("name", data.get("id", "Custom Framework"))
    return data


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
    "why": "Art. 13 transparency requirement. Loan officers cannot make informed oversight decisions without understanding what drove the recommendation. Rejected: displaying score only.",
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
    framework_id = (getattr(args, "framework", None) or _DEFAULT_FRAMEWORK).lower()
    config_arg = getattr(args, "config", None)

    # ── 1. Resolve framework ──────────────────────────────────────────────────
    if framework_id == "custom":
        if not config_arg:
            msg = (
                "Usage: irp export evidence --framework custom --config path/to/framework.json\n"
                "The --config flag is required when using --framework custom."
            )
            return {"command": "export.evidence", "status": "error", "text": msg}

        config_path = Path(config_arg)
        if not config_path.is_absolute():
            config_path = (project_root / config_path).resolve()

        result = _load_custom_framework(config_path)
        if isinstance(result, str):
            return {"command": "export.evidence", "status": "error", "text": result}
        framework = result

    elif framework_id in _BUILTIN_FRAMEWORKS:
        framework = _BUILTIN_FRAMEWORKS[framework_id]

    else:
        valid = ", ".join(_BUILTIN_FRAMEWORKS) + ", custom"
        return {
            "command": "export.evidence",
            "status": "error",
            "text": (
                f"Unknown framework: {framework_id!r}\n"
                f"Valid values: {valid}\n"
                "For custom frameworks: irp export evidence --framework custom --config path/to/framework.json"
            ),
        }

    # ── 2. Resolve output path ────────────────────────────────────────────────
    fw_id = framework.get("id", "custom")
    if output_arg:
        output_path = Path(output_arg)
    elif demo:
        output_path = project_root / f"EVIDENCE-{fw_id}-demo.md"
    else:
        output_path = project_root / f"EVIDENCE-{fw_id}.md"

    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    # ── 3. Load decisions ─────────────────────────────────────────────────────
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
        return {"command": "export.evidence", "status": "empty", "text": nudge}

    # ── 4. Build document ─────────────────────────────────────────────────────
    body = _build_evidence_md(decisions, framework, project_root, demo=demo)

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        f"Command: export evidence --framework {fw_id}{'  [demo]' if demo else ''}",
        "",
    ]

    if output_path.exists() and not force:
        text = "\n".join(header + [
            f"Refusing to overwrite existing file: {output_path}",
            "Re-run with --force to overwrite.",
        ])
        return {
            "command": "export.evidence",
            "status": "exists",
            "output_path": str(output_path),
            "framework": fw_id,
            "text": text,
        }

    # ── 5. Write ──────────────────────────────────────────────────────────────
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

    # ── 6. Summary ────────────────────────────────────────────────────────────
    sections = framework.get("sections", [])
    section_summary = " · ".join(
        f"{s['id']}: {sum(1 for e in decisions if _matches_section(e, s))}"
        for s in sections
    )

    summary_lines = [
        f"Wrote {output_path}",
        f"Framework: {framework.get('name', fw_id)}",
        f"Decisions: {len(decisions)} total → {section_summary}",
        "Lock:    file is read-only — `chmod +w` to override",
        "",
        "Regenerate any time with:",
        f"  irp export evidence --framework {fw_id} --force{' --demo' if demo else ''}",
    ]
    if demo:
        summary_lines.extend([
            "",
            "This is sample data. Run `irp export evidence` (without --demo) "
            "to generate a report from your actual ledger.",
        ])

    text = "\n".join(header + summary_lines)

    return {
        "command": "export.evidence",
        "status": "ok",
        "output_path": str(output_path),
        "framework": fw_id,
        "decision_count": len(decisions),
        "section_counts": {
            s["id"]: sum(1 for e in decisions if _matches_section(e, s))
            for s in sections
        },
        "demo": demo,
        "text": text,
    }
