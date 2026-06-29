# IRP Technical Book: Durable Decisions in Software Systems

A comprehensive architectural analysis of the Intent Record Protocol (IRP), exploring how to make software decisions durable, portable, and conflict-aware.

## Reading Order

### Part I: Foundations
- **ch1-architecture.md** — The Problem & Core Abstractions
  - Why decisions vanish, how IRP solves it, core data structures
  - Read time: 10 minutes
  
- **ch2-state-conflicts.md** — State & Conflict Detection
  - How current.json is derived, conflict detection heuristics, non-blocking warnings
  - Read time: 10 minutes

### Part II: Recording & Validation
- **ch3-capture.md** — Capturing Intent
  - Interactive and stdin modes, sensor architecture, context enrichment
  - Read time: 10 minutes
  
- **ch4-validation.md** — Decision Validation
  - The check command, keyword overlap algorithm, conflict resolution patterns
  - Read time: 8 minutes

### Part III: Live Feedback & Extensibility
- **ch5-figma-plugin.md** — The Figma Plugin Architecture
  - Three-layer architecture, bridge pattern, comment auto-populate
  - Read time: 10 minutes
  
- **ch6-extensibility.md** — Extensibility & Cross-Engine Context
  - REST API layer, collab.py context injection, multi-tool workflows
  - Read time: 10 minutes

### Epilogue
- **ch7-epilogue.md** — Patterns & Synthesis
  - Pattern extraction, design tradeoffs, evolution roadmap, open questions
  - Read time: 8 minutes

**Total estimated read time: 45-60 minutes**

## Appendices

- **01-analysis-core.md** — Raw exploration notes on irp/core architecture
- **01-analysis-commands.md** — Raw exploration notes on command patterns
- **01-analysis-plugin.md** — Raw exploration notes on Figma plugin and bridge
- **02-positioning.md** — Audience analysis and core thesis
- **03-structure.md** — Chapter outline and content plan
- **05-review-feedback.md** — Editorial review and revision notes
- **07-audit-report.md** — Code block audit (pseudocode verification)

## Key Concepts

### The Five Core Patterns

1. **Append-Only Audit Log** — Decisions are append-only; official commands never modify them
2. **Derived State** — Current state is computed from ledger, not stored separately
3. **Lightweight Heuristics** — Simple, explainable validation (keywords, not embeddings)
4. **Non-Blocking Validation** — Warnings inform without preventing action
5. **Bridge Pattern** — External tools integrate via stateless proxies

### Architecture Principles

- **Decision Survivability** — Decisions should outlive tools and systems
- **Local-First** — Source of truth lives locally (.irp/), tools integrate remotely
- **Portability** — Decisions are tool-neutral JSONL, not vendor-locked
- **Team Autonomy** — Systems inform; teams decide

## For Different Readers

**Architects evaluating IRP:**
- Start with Ch1 (problem statement) + Ch2 (state model)
- Skim Ch3-4 (capture/validation)
- Read Ch5-6 carefully (integration, extensibility)
- Use Ch7 (patterns) to evaluate fit for your organization

**Engineers implementing IRP or similar:**
- Start with Ch1-2 (conceptual foundation)
- Read Ch3-4 carefully (implementation details)
- Read Ch5-6 (bridge pattern, API design)
- Reference appendices for architecture notes

**Teams using IRP:**
- Skim Ch1 (problem/solution overview)
- Read Ch3 (how capture works)
- Skim Ch4 (conflict detection basics)
- Reference Ch6 for REST API usage

**Researchers studying decision systems:**
- Read Ch1-2 (design space)
- Read Ch7 carefully (synthesis, open questions)
- Reference appendices for implementation details
- See ch07-epilogue.md "Open Questions" section

## Design Decisions Explained

| Choice | Explained In | Reasoning |
|--------|-------------|-----------|
| JSONL over SQLite | Ch1, Ch2 | Portability, resilience to corruption |
| Keyword overlap over embeddings | Ch2, Ch4 | Determinism, explainability, cost |
| Last 10 active decisions | Ch2 | Scope management, focus |
| Non-blocking checks | Ch2, Ch4 | Team autonomy, non-friction |
| Bridge pattern for tools | Ch5, Ch6 | Tool independence, extensibility |
| Current.json rebuild on each write | Ch2 | Consistency without sync |
| Sequential IDs (IRP-YYYY-MM-DD-NNN) | Ch3 | Determinism, human-readable, date-scoped |
| Stdin-based capture | Ch3 | Composability, automation-friendly |

## Code Examples

All code examples in this book are **pseudocode** with generic variable names. They illustrate algorithm flow and patterns, not exact implementation.

For exact implementation, see:
- `/irp/core/irp.py` — Dispatcher
- `/irp/core/store.py` — Data layer
- `/irp/core/commands/` — Command implementations
- `/irp/figma_plugin/bridge/server.py` — Bridge server

## Diagrams

The book includes ASCII-art diagrams and references to Mermaid-style flowcharts (noted as [MERMAID: description]):

- Architecture flows (ledger → current → APIs)
- Algorithm flows (tokenization → stopword removal → match)
- Message sequences (plugin → bridge → ledger)
- Decision flows (capture → validate → resolve)

## Future Enhancements (Not Covered)

These are research topics, not current implementation:

- **Multi-project decisions** — How to share decisions across repositories
- **Encrypted ledger** — Sensitive decisions (IP, strategy)
- **Team-scoped visibility** — Show decisions only to specific teams
- **Decision webhooks** — Notify external systems on decision changes
- **Ledger indexing** — Fast querying of large ledgers (1000s of decisions)
- **Rollback semantics** — Mark decisions withdrawn, keep full history

See Ch7 "Evolution Roadmap" and "Open Questions" for more.

## Terminology

- **Ledger** — Append-only JSONL file of all decisions ever made (.irp/ledger.jsonl)
- **Current state** — Derived view of last 10 active decisions (.irp/current.json)
- **Bridge** — Local HTTP server proxying tool events to IRP core
- **Sensor** — External tool that observes intent and feeds decisions (Figma, Slack, etc.)
- **Decision entry** — Single JSONL line representing one decision
- **Check** — Validation command that detects conflicts without blocking
- **Conflict** — Keyword overlap between proposal and active decision
- **Capture** — Command to record a new decision
- **collab.py** — Context injector for external AI models

## How to Use This Book

**As a reference:** Each chapter is self-contained. Jump to the section you need.

**As a narrative:** Read in order (Ch1-7) for full understanding of design decisions and their rationale.

**As a research resource:** Reference appendices (01-*analysis*.md) for detailed architecture notes.

**For implementation:** Use Ch3-5 as a guide if building similar systems. Ch7 provides pattern guidance.

## Questions?

This book attempts to explain IRP's design thoroughly. If something is unclear:

1. Check the "Apply This" section at the end of each chapter for practical guidance
2. Reference the appendices for detailed architecture notes
3. See the source code in `/irp/` — all patterns have concrete implementations

---

**Book Statistics:**
- 7 chapters + epilogue
- ~1,100 lines of markdown (equivalent to 60-80 pages)
- 5 core patterns + 35 sub-patterns (5 per chapter)
- 8 architecture diagrams (ASCII + Mermaid)
- ~10,000 words

**Last updated:** 2026-04-12
**Status:** First draft (Post-PHASE 4 Writing, Pre-PHASE 5 Review)
