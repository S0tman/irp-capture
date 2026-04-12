# Codebase-to-Book Estimation: IRP-Capture

## Executive Summary

Running the "analyze-codebase-to-book" prompt on irp-capture would produce a **5-7 chapter technical book** in approximately **3-4 hours** (with parallel agents), comparable to a **60-100 page professional publication**.

---

## IRP-Capture Code Metrics

### Repository Size
- **Total files:** 25 (18 Python, 1 JavaScript, 6 Markdown)
- **Lines of code:** 2,397 LOC
- **Complexity:** Medium (well-structured, modular)
- **Reference level:** ~13% the size of Claude Code from Source (1,884 files)

### Subsystems Identified (Book Chapters)

| Subsystem | LOC | Purpose | Book Potential |
|---|---|---|---|
| **IRP Core (irp/core/)** | 1,724 | Dispatcher + data layer | Ch1-2: Architecture + State Management |
| **Commands (capture/check/why)** | 1,164 | CLI command implementations | Ch3: Decision Capture & Validation |
| **Integrations (Slack)** | 325 | External service connectors | Ch4: Extending IRP (Optional advanced chapter) |
| **Figma Plugin** | 245 | Browser-based UI + bridge | Ch5: Live Feedback Loop & UI |
| **REST API** | 183 | HTTP interface | Ch6: Exposing IRP as a Service (Optional) |
| **Tools (collab.py)** | 245 | Multi-engine collaboration | Ch7: Cross-System Decision Context |

---

## Phase-by-Phase Time Estimate

### Phase 1: Exploration
**Purpose:** Read every file, document architecture/patterns/decisions  
**Agents:** 3-4 parallel (Core, Commands, Figma, Integrations)  
**Time:** **30-45 minutes**

Each agent reads:
- Core agent: irp.py, store.py, __init__.py (~500 LOC) → architecture, state model, flow
- Commands agent: capture.py, check.py, why.py, inherit.py (~1,164 LOC) → patterns, integration points
- Plugin agent: code.js, bridge/server.py (~245 LOC) → UI patterns, real-time architecture
- Integrations agent: Slack integration + collab.py (~570 LOC) → extension patterns

**Expected output:** 4 research docs (2-3 pages each) = ~12 pages of raw analysis

---

### Phase 2: Audience & Positioning
**Purpose:** Define core thesis and book value  
**Agents:** 1  
**Time:** **15-20 minutes**

**Core Thesis:** "IRP makes decisions durable, portable, and conflict-aware across tools and time"

**Audience:** 
- Primary: Builders of AI-native tools and agents (Daria's ICP)
- Secondary: Technical leads evaluating decision-tracking systems

**Why it's a book (not just docs):**
1. **Narrative:** How IRP evolved from "capture intent" → "check conflicts" → "cross-engine visibility"
2. **Patterns:** Ledger-as-source-of-truth, conflict detection via keywords, decision inheritance
3. **Design rationale:** Why JSONL over databases, why local-first, why Figma + bridge architecture
4. **Transferable lessons:** Decision survivability, immutable audit trails, sensor architecture

---

### Phase 3: Structure
**Purpose:** Outline chapters and parts  
**Agents:** 1  
**Time:** **20-30 minutes**

**Expected Book Structure:**

```
Part I: Foundations (2 chapters)
├─ Ch1: Architecture & Core Abstractions
│   • What is IRP? Problem statement (decisions disappearing)
│   • Core data model: ledger, current, decision ID
│   • Why JSONL? Why local-first?
│   └─ Apply This: 5 patterns (immutable logs, derived state, etc)
│
└─ Ch2: State & Conflict Detection
    • How current.json works (derived active state)
    • Keyword-based conflict detection
    • Why conflict detection matters (avoid decision thrashing)
    └─ Apply This: 5 patterns (keyword matching, state synthesis, etc)

Part II: Recording & Validation (2 chapters)
├─ Ch3: Capturing Intent (the capture command)
│   • Life of a decision: intent → ledger entry → current.json update
│   • Why stdin-based capture? (composability with external sensors)
│   • What the capture loop does step-by-step
│   └─ Apply This: 5 patterns (sensor design, stdin composition, etc)
│
└─ Ch4: Decision Validation (the check command)
    • Why explicit validation? (catch conflicts early)
    • How check works: proposal vs. active decisions
    • Designing for non-blocking warnings
    └─ Apply This: 5 patterns (validation patterns, safety without friction, etc)

Part III: Live Feedback (1-2 chapters)
├─ Ch5: The Figma Plugin Architecture
│   • Why Figma? (source of design intent)
│   • Bridge pattern: plugin ↔ backend ↔ IRP
│   • Live polling for resolved comments (the 30-second refresh)
│   • Auto-populate flow: resolved comment → decision context
│   └─ Apply This: 5 patterns (bridge servers, polling patterns, plugin UX, etc)
│
└─ [Ch6 optional: REST API & collab.py]
    • Extending IRP beyond CLI
    • collab.py: making IRP context portable to other AI engines
    • Bridging local decision context to GPT (via collab.py)
    └─ Apply This: 5 patterns (context sharing, API design, etc)
```

**Potential book size:** 5-7 chapters, 2-3 parts, ~150-250 lines per chapter average = **750-1,750 lines of output**

---

### Phase 4: Writing
**Purpose:** Write chapters from scratch (narrative, not restructured analysis)  
**Agents:** 4-5 parallel (one per chapter cluster)  
**Time:** **60-90 minutes**

Each writing agent produces 150-250 lines per chapter × 5-7 chapters = ~1,000-1,750 lines total

**Content per chapter:**
- Opening: "What problem does this layer solve?" (2-3 paragraphs)
- Body: Mix of prose + pseudocode + diagrams (20-30 lines of diagrams, 3-5 code blocks, rest prose)
- Deep Dive (optional): Implementation detail
- Apply This: 5 transferable patterns (1 paragraph each)

**Expected first draft:** 1,200-1,800 lines

---

### Phase 5: Editorial Review
**Purpose:** Identify gaps, weak sections, missing diagrams  
**Agents:** 2 (split book in half)  
**Time:** **30-45 minutes**

Each reviewer checks:
- Opening hooks + backward references
- Flow and repetition
- Missing diagrams
- Gaps (would a reader be confused?)
- 5-10 specific rewrite suggestions per section

**Expected feedback:** 300-400 lines of actionable notes

---

### Phase 6: Revision
**Purpose:** Apply feedback, add diagrams, deduplicate, cut bloat  
**Agents:** 1  
**Time:** **45-60 minutes**

- Structural fixes (split long chapters, fix broken references)
- Add 5-8 Mermaid diagrams (data flow, state machines, architecture)
- Deduplicate repeated explanations
- Tighten Apply This sections for consistency
- Cut reference-manual content

**Expected cuts:** 30-40% reduction (typical for first drafts)  
**Expected final:** 700-1,000 lines

---

### Phase 7: Source Code Audit
**Purpose:** Verify code blocks are pseudocode, not exact copies  
**Agents:** 1  
**Time:** **20-30 minutes**

- Scan all code blocks against original source
- Verify variable names are generic (not `current_json`, use `activeState`)
- Confirm no exact function implementations remain
- Add "Illustrative" comments where needed

**Expected findings:** 0-2 blocks needing replacement (IRP codebase is already pseudocode-friendly)

---

## Total Time Estimate

| Phase | Agents | Duration | Type |
|---|---|---|---|
| 1: Exploration | 3-4 | 30-45 min | Parallel |
| 2: Positioning | 1 | 15-20 min | Sequential |
| 3: Structure | 1 | 20-30 min | Sequential |
| 4: Writing | 4-5 | 60-90 min | Parallel |
| 5: Review | 2 | 30-45 min | Parallel |
| 6: Revision | 1 | 45-60 min | Sequential |
| 7: Audit | 1 | 20-30 min | Sequential |
| **TOTAL** | **13-14** | **3-4 hours** | **Mixed** |

**Actual wall-clock time with parallelization:** 3-4 hours  
**Max agents running simultaneously:** 5 (during Phase 4)

---

## Output Expectations

### Raw Numbers
- **First draft:** 1,200-1,800 lines (Phase 4)
- **After feedback:** 700-1,000 lines (Phase 6)
- **Final markdown:** 750-1,100 lines with diagrams
- **Equivalent pages:** 50-80 pages (assuming 10 lines per page)
- **Diagrams:** 5-8 Mermaid charts (architecture, state machines, data flow)

### Chapters
| Chapter | Est. Lines | Focus |
|---|---|---|
| 1. Architecture | 150-200 | System design, JSONL choice, why local-first |
| 2. State & Conflicts | 150-200 | current.json derivation, conflict detection algorithm |
| 3. Capture | 120-170 | Recording intent, stdin-based sensors, audit trail |
| 4. Validation | 120-170 | The check command, non-blocking warnings |
| 5. Live Feedback (Figma) | 150-200 | Plugin architecture, bridge pattern, polling |
| 6. Context Portability | 100-150 | collab.py, cross-engine visibility (optional) |
| 7. Patterns & Future | 100-130 | Synthesis, transferable lessons, roadmap (epilogue) |
| **TOTAL** | **900-1,220** | **~60-80 pages** |

---

## Model & Cost Implications

### Recommended Model Mix

| Phase | Agent Count | Model | Rationale |
|---|---|---|---|
| 1: Exploration | 3-4 | Sonnet | Read heavy, parallel analysis |
| 2-3: Planning | 2 | Haiku | Lightweight structure/positioning |
| 4: Writing | 4-5 | **Opus** | Narrative quality, pattern synthesis |
| 5: Review | 2 | Sonnet | Critical reading, detailed feedback |
| 6: Revision | 1 | **Opus** | Complex restructuring, consistency |
| 7: Audit | 1 | Haiku | Pattern matching against source |

### Token Budget Estimate

- **Phase 1:** Each exploration agent reads ~500-1,000 LOC → ~15K-25K tokens per agent
- **Phase 4:** Each writing agent produces ~250-350 lines → ~8K-12K tokens per agent
- **Phase 5:** Each review agent reads + feedback → ~10K-15K tokens per agent
- **Phases 2,3,6,7:** Smaller → ~3K-5K tokens each

**Estimated total:** 80K-150K tokens (vs Claude Code's ~500K+ for 1,884 files)

---

## Recommendations for IRP

### ✅ **Best Use Case: YES, run this prompt**

**Why:** 
1. **IRP is architecturally interesting.** The ledger-as-source-of-truth, conflict detection, and decision portability are novel patterns worth explaining.
2. **Audience exists.** Your (Johan's) ICP (agentic-native builders) would find a book like this valuable.
3. **Timeline is reasonable.** 3-4 hours is one deep work session or overnight with background agents.
4. **Codebase is stable.** At 2.4K LOC, it's a good "snapshot in time" for a book.

### 🎯 **Ideal Execution Model**

1. **Phase 1 (Exploration):** Run 3 agents in parallel, save analysis to `.reference/analysis-notes/`
2. **Phase 2-3 (Structure):** Review outline, tweak chapter list if needed
3. **Phase 4 (Writing):** Launch 4-5 agents in parallel, monitor quality
4. **Phase 5-7 (Review/Revision/Audit):** Run sequentially (faster, easier to control)

**Use the `/schedule` skill** to run this as a background task:
```bash
/schedule "Analyze irp-capture codebase and produce a technical book using the analyze-codebase-to-book prompt"
```

### 💡 **Optional Enhancements**

- **Add a Prologue** (Phase 2): Interview Daria or another user about "what problem does IRP solve?"
- **Include git history** in Phase 1: Analyze commit messages for design decisions
- **Add case study**: Show how a real team used IRP (e.g., the Figma plugin development)

---

## Risk Factors

| Risk | Impact | Mitigation |
|---|---|---|
| **Figma plugin complexity underestimated** | +30 min | Have Phase 1 agent spend extra time on bridge/server |
| **Conflict detection algorithm needs deep dive** | +20 min | Add "Deep Dive: Keyword Matching" section |
| **Tool overload** (too many subsystems) | +45 min | Merge REST API + collab.py into one "Extensibility" chapter |
| **First draft exceeds 1,800 lines** | +30 min revision | Aggressive cutting in Phase 6 |
| **Diagrams need multiple iterations** | +20-30 min | Plan for 2 rounds of diagram feedback |

---

## Timeline Comparison

| System | Files | LOC | Chapters | Time | Pages |
|---|---|---|---|---|---|
| **Claude Code from Source** | 1,884 | ~50K+ | 18 | 6 hours | ~400 |
| **IRP-Capture (est.)** | 25 | 2.4K | 5-7 | 3-4 hours | 60-80 |
| **Ratio** | ~1:75 | ~1:21 | ~1:3 | ~1:2 | ~1:5 |

IRP is ~75x smaller in file count but *proportionally* produces useful output quickly due to coherent architecture.

---

## Conclusion

**Running the prompt: RECOMMENDED**

**Estimated time:** 3-4 hours (with background agents)  
**Expected output:** 5-7 chapter technical book (~750-1,100 lines, 60-80 pages equivalent)  
**Value:** Solidifies your positioning, creates a reference for your ICP, documents architecture for future contributors

**Best time to run:** After current level-up is stabilized. Could be triggered as a Level 6 task.

---

**Next steps:**
1. Review this estimate with actual prompt test on 1-2 subsystems
2. Decide on optional chapters (REST API? collab.py integration details?)
3. Plan a "Phase 0" that includes Daria interview or git history analysis
4. Schedule the run as a `/schedule` background task when ready

---

*Estimate generated: 2026-04-12*  
*Assumptions: Opus for writing, parallel agents, no major refactoring between now and run date*
