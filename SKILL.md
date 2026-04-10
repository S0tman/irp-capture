---
name: irp-capture
description: Capture, recall, and check durable decision lineage inside any project using IRP (Intent Record Protocol).
---

# IRP Capture — Claude Code Skill

IRP preserves durable human decisions, rationale, and reasoning lineage inside a local project.

## When to use this skill

Use `capture` when a decision crystallises during a Claude session. The reasoning is sharpest at the moment of decision — capture it before moving on.

Use `irp why` before starting new work. It shows what was already decided so nothing gets relitigated.

Use `irp check` before proposing something new. It catches conflicts with active decisions.

## What IRP captures

- A decided outcome (what)
- The reason it was decided (why)
- When and how it was decided (timestamp, source)

## What IRP does NOT capture

- Temporary brainstorming
- Speculative or unresolved thinking
- Task lists or status updates
- Filler summaries
- Duplicate restatements with no new meaning

Think of IRP as a camera, not a notebook. Capture only what should survive session boundaries and matter later.

---

## Substrate

The canonical IRP state lives in the project root:

```
.irp/
├── ledger.jsonl     ← append-only source of truth
└── current.json     ← derived active state (last 10 decisions)
```

These are plain text files. They travel with the repo. No service required.

---

## Commands

### Capture a decision

```bash
irp capture "Decision: Use Postgres for the reporting service" \
  --why "Redis rejected — query patterns require joins"
```

### Review active decisions

```bash
irp why
```

Shows the last 10 confirmed decisions with reasoning.

### Review a specific decision

```bash
irp why --id IRP-2026-04-08-001
```

### Show full project context

```bash
irp inherit
```

Lists all active decisions — use this to understand the current state of the project before making changes.

### Check a proposal for conflicts

```bash
irp check "Add a SQLite database for reporting"
```

Returns whether the proposal conflicts with any active decision. Use this before proposing architectural changes.

### JSON output

Add `--json` to any command for machine-readable output:

```bash
irp why --json
irp check --json "Add a SQLite database"
```

---

## Installation

```bash
pip install irp-capture
```

The `irp` CLI is immediately available after install. No configuration needed.

---

## Decision quality guide

A good decision entry answers two questions:

1. **What did we choose?**
2. **What did we reject, and why?**

The second question is the valuable one. Anyone can find what you chose. The rejected alternatives are what gets lost.

**Threshold:** Capture decisions that would be hard to explain six months later. If you cannot write one sentence of why, the decision may not be ready to capture yet.
