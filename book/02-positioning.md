# PHASE 2: Positioning & Audience

## Primary Audience

### Tier 1: Technical Leaders
- **Role:** CTO, engineering manager, principal engineer evaluating decision-tracking systems
- **Pain:** Team proposals conflict. Decisions get lost between systems. No audit trail.
- **Buying signal:** "How do we prevent duplicate work and track why we chose this?"
- **What they need:** Proof that IRP prevents rework and surfaces conflicts

### Tier 2: Agentic-Native Builders (Daria's ICP)
- **Profile:** Solo or small-team builders running local/semi-local AI models
- **Pain:** Decisions don't survive tool transitions. Reasoning gets lost when switching between Claude, GPT, local models
- **Buying signal:** "I need my decisions to travel with my work across AI engines"
- **What they need:** A decision primitive that's tool-agnostic and portable

### Tier 3: Infrastructure/Platform Teams
- **Role:** Builders of internal developer tools, design systems, frameworks
- **Pain:** Hard to enforce architectural decisions across teams. No way to ask "what was decided?" when onboarding
- **Buying signal:** "How do we make decisions stick when there's turnover?"
- **What they need:** A transportable decision registry that survives refactoring

## Core Thesis

**IRP makes decisions durable, portable, and conflict-aware across tools and time — a foundational pattern for decision-surviving systems.**

### Three Commitments Embedded in That Thesis

1. **Durable:** Decisions are append-only once logged. No hidden history, no "let's just redo this." The trail grows by superseding, not by rewriting. (Durability is an application-design property, not an independent tamper-proof guarantee. See the [Trust model](../TRUST.md).)

2. **Portable:** Decisions live locally (.irp/) but integrate everywhere (Figma, Slack, remote AI, REST APIs). A decision captured in Figma can inform a Claude API call, which can inform a Slack post. No re-entry friction.

3. **Conflict-aware:** The check command runs lightweight detection *without* blocking. Teams see conflicts but decide how to resolve. This is the inverse of "enforce consistency at all costs."

## Why This Deserves a Book

### Problem 1: Source Code Tells Mechanism, Not Story
The IRP codebase is ~1,000 LOC. You can read it. But reading code doesn't explain:
- Why ledger-as-source-of-truth (not a database)?
- Why non-blocking conflict detection (not a policy enforcer)?
- Why bridge pattern separates tools from core?
- How to adapt these patterns to other domains?

### Problem 2: Cross-Cutting Patterns Aren't Obvious
IRP touches **5 different pattern domains:**
- **Append-only audit logs** (ledger)
- **Derived state** (current.json)
- **Conflict detection** (check algorithm)
- **Bridge architecture** (Figma plugin pattern)
- **Context portability** (collab.py, REST APIs)

A book can isolate each and explain the design intent behind it.

### Problem 3: Decision Survivability Is a Blank Space in Software
Leaders know about:
- Continuous integration (code survives merges)
- Event sourcing (state survives schema changes)
- Configuration as code (settings survive deploys)

But **decision survivability?** No pattern language exists. IRP fills that gap.

### Problem 4: Portability Requires Architectural Thinking
"Make decisions portable across tools" sounds easy. It's not. IRP solves:
- How do you represent a decision tool-neutrally?
- What's the minimal transport format?
- How do you avoid tool lock-in while staying composable?
- How do you keep .irp/ as source of truth when external systems want to own the data?

## What Readers Will Understand by the End

By finishing this book, readers should be able to:

1. **Articulate the problem:** Explain to a peer why decisions disappearing is a real architectural problem.

2. **Understand the architecture:** Trace a decision from capture (Figma plugin) → ledger → conflict check → context injection (collab.py).

3. **Apply the patterns:** Take the ledger pattern, conflict detection heuristic, or bridge architecture and adapt it to their own domain.

4. **Evaluate IRP for their context:** Know whether IRP's local-first, immutable, non-blocking design fits their team or whether they need different tradeoffs.

5. **Extend the system:** Understand why a Slack sensor, REST API, or custom plugin would fit naturally into the bridge architecture (not require core changes).

## Positioning Summary

This is **not** a tool comparison (IRP vs Notion vs Linear vs Glean). It's an **architectural case study** of how to make decisions a first-class, portable, durable primitive in software systems.

The book proves the thesis through:
- Narrative (story of why IRP was built)
- Architecture (detailed walkthroughs of each component)
- Patterns (transferable lessons for other builders)
- Design rationale (why each decision was made)

**Target length:** 2-3 parts, 5-7 chapters, ~750-1,100 lines markdown (equivalent to 60-80 pages).

**Voice:** Expert-to-expert. Assume reader understands event sourcing, eventual consistency, or similar architectural concepts. No tutorials, no marketing.
