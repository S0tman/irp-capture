# Chapter 11b: Generating the Evidence

## The Gap Between Practice and Paper

Chapters 12 through 16 cover what the EU AI Act requires. This chapter covers how to produce the evidence that you met those requirements — specifically for organisations that have been capturing decisions and need to present that record in a form regulators and auditors can navigate.

There is a category of organisation that does everything right in practice and still fails an audit. They have human oversight. They have documented scope constraints. They have an append-only record of material decisions. But when a regulator asks for their compliance evidence, they produce a folder of internal documents, a spreadsheet of dates, and a Confluence page nobody has read since 2024. The evidence exists — it is just not structured as evidence.

The gap between practice and paper is where most Article 12, 13, and 14 findings originate. Not from organisations that ignored the obligations. From organisations that met them, but cannot show it.

`irp export evidence` closes that gap.

---

## What the Command Does

```bash
irp export evidence
```

One command. It reads `.irp/ledger.jsonl` — the append-only decision ledger — and produces `EVIDENCE.md`: a structured document that maps every captured decision to the EU AI Act articles it satisfies.

```bash
irp export evidence --demo   # try without a ledger — built-in sample data
irp export evidence --force  # overwrite an existing EVIDENCE.md
irp export evidence --json   # machine-readable output for downstream tooling
```

The `--demo` flag generates `EVIDENCE-demo.md` from a built-in sample dataset: a Nordic SME lending platform deploying a loan pre-screening AI agent, classified as high-risk under Annex III point 5(b). Ten decisions, spanning deployment approval, risk classification, escalation policy, override logging, quarterly retraining, and a Fundamental Rights Impact Assessment. Realistic enough to show an auditor, a prospect, or a compliance team exactly what the output looks like — without touching your own ledger.

---

## How Decisions Map to Articles

The mapping is deterministic. No AI inference. No LLM calls. Three heuristics:

### Article 12 — Logging and Traceability

Every decision in the ledger qualifies. The argument is structural: each IRP entry is timestamped, sequenced, append-only by design, and human-confirmed. Official IRP commands add entries and do not edit prior ones, and each entry carries the moment it was captured, not the moment it was reviewed. This is the decision trail Article 12 is asking for. One honest caveat: append-only is an application-design property, not an independent cryptographic guarantee. A local, owner-held ledger is not tamper-proof on its own. To make a snapshot externally verifiable (proof that a given ledger state existed by a given time), anchor its digest to an external timestamp authority. See the [Trust model](../TRUST.md).

The evidence package lists every decision under Article 12, with its id, date, what was decided, why, who confirmed it, and the source (CLI, Slack thread, Figma session, API call). An auditor can navigate that list. They can pick any entry and verify it against the raw ledger. The chain of custody is intact.

### Article 14 — Human Oversight Events

The `confirmed_by` field is the human oversight anchor. Every IRP entry that records a confirming human — a name, a role, a team identifier — is surfaced as a human oversight event under Article 14. These are the documented moments where a natural person approved, constrained, or governed the AI system's behaviour.

Decisions without a `confirmed_by` field are checked against a secondary heuristic: if the decision text contains oversight-related keywords (human, oversight, agent, approval, review, escalation), it is also included. This catches decisions that describe oversight mechanisms even when a specific confirmer was not recorded.

The Article 14 section is the most sensitive part of the evidence package for auditors. It is the answer to the question: "Show me where humans were in control."

### Article 13 — Transparency and Instructions for Use

Decisions that document scope, intended use, capabilities, and limitations are surfaced as transparency evidence. The keyword heuristic looks for terms like "intended use," "scope," "limitation," "capability," "constraint," "boundary," and related variants.

A decision that records "Agent scope limited to Swedish and Norwegian SME applicants with registered VAT number — no consumer credit" is an IFU-equivalent statement — the kind of documented constraint Article 13 requires deployers to understand before they use the system. The evidence package extracts these decisions and presents them as the transparency record.

---

## What the Output Looks Like

The structure is deliberate:

```markdown
## Article 12 — Logging and Traceability

Article 12 requires that high-risk AI systems maintain logs sufficient
to trace their operation. IRP provides an append-only, timestamped,
human-confirmed decision ledger...

### IRP-2026-03-10-001

**Decision:** Deploy AI agent for loan pre-screening for Nordic SME customers
**Reasoning:** Reduces manual review time from 4 days to same-day...

_Date: 2026-03-10_  _Confirmed by: `anna.lindqvist`_  _Source: cli_
```

Each decision appears with its full reasoning. The reasoning is not a summary — it is the original `why` field, captured at the moment the decision was made, preserved exactly. That is what makes it credible. An auditor reading this is not reading a post-hoc justification assembled for the evidence package. They are reading what was recorded before they asked.

The evidence summary table at the end shows coverage at a glance:

```
| Article | Requirement              | Records | Status     |
|---------|--------------------------|---------|------------|
| Art. 12 | Logging and traceability | 10      | ✅ Covered |
| Art. 14 | Human oversight evidence | 10      | ✅ Covered |
| Art. 13 | Transparency / scope     | 9       | ✅ Covered |
```

---

## What the Evidence Package Does Not Cover

The footer of every evidence package is explicit:

> **What this record does not cover:**
> - Model safety validation or bias testing
> - Legal risk classification of the AI system
> - Conformity assessment (Art. 43)
> - Training data quality evidence
> - Registration in the EU AI Act database (Art. 49)
>
> This evidence package covers decision provenance and human oversight records only.
> For full EU AI Act compliance, additional technical documentation is required.

This is not hedging. It is accurate scope. Decision provenance covers Articles 12, 13, and 14 because those articles ask "what was decided, by whom, with what human involvement, under what constraints." They do not ask "was the model safe" or "was the training data representative." Those questions require different evidence, produced by different processes.

The evidence package tells an auditor what it covers and what it does not. That honesty is itself a signal — organisations that understand their compliance scope are more credible than those that claim it is all handled.

---

## Using the Evidence Package in Practice

**Before a regulated deployment.** Run `irp export evidence` as a pre-deployment gate. If the output is thin — few decisions, no `confirmed_by` entries, no scope constraints — that is diagnostic. It tells you what governance work still needs to happen before the system goes live.

**In response to a regulatory enquiry.** When a regulator asks for your Article 12 traceability record, `EVIDENCE.md` is the starting document. It does not replace the raw ledger — but it makes the ledger navigable. Attach both.

**In procurement conversations.** If you are a provider selling to deployers in regulated industries, `irp export evidence --demo` shows the evidence package your product produces. A deployer evaluating whether your system supports their compliance obligations can see exactly what the output looks like, before they buy.

**In internal compliance reviews.** Run monthly or quarterly. Coverage changes as the deployment evolves — new decisions are captured, new oversight events are recorded. The evidence package reflects the current state of the ledger. Regenerate it when the ledger changes.

---

## The Relationship to irp guard

`irp export evidence` is retrospective — it documents what was decided. `irp guard` is prospective — it catches decisions being undone.

Together, they close the governance loop:

```
irp capture       ← decision recorded
irp export evidence ← decision presented as compliance evidence
irp guard         ← decision defended at the commit layer
```

A decision recorded but not defended is documentation. A decision recorded, evidenced, and defended is governance. The difference matters to auditors, to regulators, and to the organisation itself when something goes wrong and the question is "when did you know, and what did you do about it?"

---

## The Essentials

1. **The gap between practice and paper is where audits fail.** Organisations that meet their obligations but cannot produce structured evidence of them are indistinguishable from those that do not meet them. Evidence generation is not optional.

2. **Article 12, 13, and 14 map to the same record.** The decision ledger — timestamped, append-only, human-confirmed — satisfies the core structural requirement of all three articles. The evidence package makes that mapping explicit.

3. **The `confirmed_by` field is the Article 14 anchor.** Every IRP entry that records a human confirmation is a documented oversight event. Fill it. It is the most important field in the ledger for regulatory purposes.

4. **`--demo` shows the gap.** Run `irp export evidence --demo` before you have a populated ledger. That output is what your compliance evidence looks like when you have been capturing decisions. The gap between what it shows and what your current record produces is the governance work ahead.

5. **Regenerate regularly.** The evidence package is a projection of the ledger at a point in time. As the deployment evolves and new decisions are captured, regenerate it. The ledger is the source of truth. `EVIDENCE.md` is always a derivation, never a substitute.
