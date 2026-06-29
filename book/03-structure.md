# PHASE 3: Structure & Chapter Outline

## Overall Structure

```
IRP: Durable Decisions in Software Systems

PART I: Foundations (2 chapters, ~350 lines)
  CH1: The Problem & Core Abstractions
  CH2: State & Conflict Detection

PART II: Recording & Validation (2 chapters, ~300 lines)
  CH3: Capturing Intent (The Capture Command)
  CH4: Decision Validation (The Check Command)

PART III: Live Feedback & Extensibility (2-3 chapters, ~350-450 lines)
  CH5: The Figma Plugin Architecture
  CH6: Extensibility & Cross-Engine Context

EPILOGUE (1 chapter, ~150 lines)
  CH7: Patterns & Synthesis
```

**Total estimate:** ~1,100-1,300 lines (6-7 chapters)

---

## PART I: Foundations

### Chapter 1: The Problem & Core Abstractions
**Word estimate:** 170 lines

**Opening:**
- Hook: "Decisions vanish. A team chooses React. Months later, someone proposes Vue. Why wasn't the React decision visible?"
- Problem statement: Decisions exist in meetings, Slack threads, code reviews — but nowhere together. They don't transfer between tools. When systems change, the reasoning is lost.
- Why this matters: Rework multiplies cost. Onboarding is painful (new engineer doesn't know what was decided). Conflicts aren't detected until expensive.

**Body:**
1. **The Source of Truth Problem:** Three wrong answers
   - Notion: centralized, but out of sync with code/tools
   - Comments: scattered, no structure
   - Meetings: ephemeral, hard to reference
   - IRP's answer: append-only ledger as canonical record

2. **Core Abstractions**
   - The ledger: append-only log, one line = one decision
   - Sequential IDs: encode date for natural grouping
   - Current state: derived, not stored (rebuild from ledger)
   - Why separation? Enables portability

3. **Design Principle: Durability Over Friction**
   - Non-blocking checks (conflict detected but doesn't prevent)
   - No policy enforcement (teams decide)
   - Consequence: decisions are logged even when controversial

4. **Key Data Structures**
   - Decision entry: type, id, what, why, confidence, timestamp, source, tags
   - Ledger.jsonl: line-delimited, parseable even with corruption
   - Current.json: last 10 active decisions, computed from ledger

**Diagrams needed:**
- Flow: problem statement → multiple tools → IRP as arbiter
- Data model: entry structure (conceptual boxes)

**Apply This** (5 patterns):
1. Append-only audit log — solves: lossy tool transitions — adapt: append-only design to your decision domain — pitfall: don't try to update history
2. Derived state — solves: avoid dual truth — adapt: rebuild state deterministically from log — pitfall: don't cache derived state without invalidation
3. Sequential IDs — solves: human-readable, date-scoped querying — adapt: encode context (domain, date) in identifiers — pitfall: don't rely on uniqueness across time zones
4. Non-blocking validation — solves: feedback without friction — adapt: warn but don't prevent — pitfall: don't let warnings become ignored
5. Tool neutrality — solves: avoid lock-in — adapt: minimal entry format, plugin-based capture — pitfall: don't over-generalize (keep entry structure tight)

---

### Chapter 2: State & Conflict Detection
**Word estimate:** 180 lines

**Opening:**
- Why this matters: Current state is the "project bridge." When a team asks "what have we decided?", they're really asking "what's in current.json?"
- This chapter explains: how state is derived, why last-10 limit, how conflict detection works without embeddings.

**Body:**
1. **Rebuild vs. Store**
   - Why rebuild current.json from ledger: source of truth is the ledger, not current
   - Deterministic: same ledger always produces same current
   - Consequence: current.json can be deleted and reconstructed (no data loss)
   - Use case: enable shared current.json across repositories without sync headaches

2. **Last 10 Window**
   - Scope management: focus attention on recent decisions
   - Don't distract team with 2-year-old decision from previous project
   - Customizable in principle (could be last N, or last N days)
   - Current design: 10 is sweet spot (recent context, manageable to review)

3. **Conflict Detection: Keyword Overlap Heuristic**
   - Algorithm: tokenize proposal + active decisions, compute intersection
   - Stopword list: 48 words filtered (articles, common verbs, modifiers)
   - First match only: newest decision wins (most recent context)
   - Non-semantic: no embeddings, no scoring, no ML
   - Why? Lightweight, deterministic, stakeholder-testable (team understands why it matched)

4. **Conflict Example: Walkthrough**
   - Proposal: "We should use Vue for the UI framework"
   - Active decision: "Use React for core UI because team expertise"
   - Tokens overlap: {ui, framework, use}
   - Match: IRP-2026-04-10-003
   - Result: status="conflict" but proposal is still logged
   - Resolution: team reviews conflict, decides (keep React, abandon Vue; or update React decision)

5. **Status Codes & Non-Blocking**
   - Exit code 10: conflict (distinct from error=1)
   - Caller can distinguish: "Caller warned but not blocked"
   - Design: conflicts are *information*, not *policy*

**Diagrams needed:**
- State machine: ledger → rebuild → current.json
- Conflict detection flow: proposal → tokenize → overlap check
- Example conflict resolution: before/after states

**Apply This** (5 patterns):
1. Deterministic state derivation — solves: consistency without sync — adapt: define rebuild algorithm, version it — pitfall: don't add mutable fields to derived state
2. Windowing for scope — solves: focus on relevant context — adapt: choose window size based on domain — pitfall: don't lose old decisions (they live in ledger)
3. Lightweight heuristics over ML — solves: explainability, determinism — adapt: build heuristic that team understands — pitfall: don't assume heuristic is perfect (it's a warning, not a policy)
4. Stopword filtering — solves: reduce false positives — adapt: domain-specific stopwords — pitfall: don't rely on stopword list alone
5. Non-blocking warnings — solves: information without friction — adapt: warn clearly but let user decide — pitfall: don't silently ignore warnings

---

## PART II: Recording & Validation

### Chapter 3: Capturing Intent
**Word estimate:** 160 lines

**Opening:**
- The capture command is where decisions enter the ledger.
- This chapter: how does a decision get from "discussed in Slack" or "designed in Figma" to "logged in IRP"?
- Two modes: interactive (CLI) and stdin (automated bridges).

**Body:**
1. **Interactive Capture Flow**
   - User runs `irp capture`
   - Prompts: "What was decided?", "Why does it matter?", "Confidence?"
   - Generates ID, shows preview
   - Asks confirmation: "c" to confirm, "s" to skip
   - If confirmed: append to ledger, rebuild current

2. **Stdin Capture Flow**
   - External tool (Figma plugin, Slack, etc.) constructs JSON candidate
   - Pipes to `irp capture --stdin`
   - No confirmation prompt (automated)
   - Append, rebuild, return entry
   - Example: Figma bridge sends POST → bridge calls capture via stdin

3. **Sensor Architecture**
   - Sensor = external tool that observes intent and feeds IRP
   - Figma sensor: plugin observes design decisions
   - (Future) Slack sensor: observes thread discussions
   - Each sensor has a bridge (local HTTP server) that translates tool events → IRP entries
   - Core principle: sensors are independent, failures don't cascade

4. **Context Enrichment**
   - Base entry: type, what, why, confidence, timestamp
   - Figma enrichment: add page, selection, file_key to entry
   - Slack enrichment: add channel_id, thread_ts to entry
   - Design: source field indicates where decision came from
   - Use case: traces decision back to original conversation

5. **ID Generation**
   - Sequential per day: IRP-2026-04-12-001, IRP-2026-04-12-002, etc.
   - Deterministic: reading ledger computes next ID
   - Consequence: no collisions, no UUIDs needed
   - Searchability: can ask "what did we decide on 2026-04-12?"

**Diagrams needed:**
- Interactive capture flow: prompt → preview → confirm → ledger
- Sensor architecture: tool → bridge → IRP core
- Entry enrichment: base fields + source-specific context

**Apply This** (5 patterns):
1. Sensor architecture — solves: multi-tool capture without core changes — adapt: define bridge for each tool — pitfall: don't assume bridge reliability (handle bridge failures gracefully)
2. Stdin-based composition — solves: integrate with any workflow — adapt: accept JSON via pipe — pitfall: don't require confirmation for automated flows
3. Context enrichment — solves: trace decisions back to source — adapt: add source-specific metadata — pitfall: don't add so much context that ledger becomes unmanageable
4. Sequential ID generation — solves: human-readable, collision-free IDs — adapt: encode date + sequence — pitfall: don't change ID format (breaking change)
5. Interactive fallback — solves: manual entry when automation isn't available — adapt: support both stdin and interactive — pitfall: don't require manual entry for all flows (too slow)

---

### Chapter 4: Decision Validation
**Word estimate:** 140 lines

**Opening:**
- Before a new decision is captured, the team should know: does this conflict with something we already decided?
- The check command runs this validation.
- Key design: validation is non-blocking. It informs but doesn't prevent.

**Body:**
1. **Check Algorithm: Keyword Overlap**
   - User provides proposal (text)
   - Check reads current.json (active decisions)
   - Tokenizes both, removes stopwords
   - Searches for overlap in order (newest first)
   - First match: return as conflict
   - No match: return "clear"

2. **Lightweight Heuristics**
   - Why not embeddings? 
     - Deterministic (same proposal always gets same answer)
     - Explainable (can see which words matched)
     - Fast (no model inference)
   - Trade-off: may miss semantic conflicts, over-detect lexical ones
   - Acceptable: this is a *warning*, not a *blocker*

3. **Integration Points**
   - Figma plugin calls check before showing capture form
   - CLI tool can call check before capturing manually
   - Rest API exposes check for external workflows
   - Design: check runs *after* propose, *before* commit

4. **Conflict Resolution Patterns**
   - If conflict detected:
     - Team reviews matched decision
     - Options: (a) abandon new proposal, (b) withdraw old decision, (c) reconcile
     - Team updates ledger accordingly (add note, withdraw, etc.)
   - Non-blocking design: team *chooses* resolution

5. **Handling False Positives**
   - Stopword list reduces but doesn't eliminate
   - Example: "API" appears in both "use REST API" and "build gRPC API"
   - Team sees conflict, evaluates, decides it's false positive
   - Design: better to over-warn than under-warn

**Diagrams needed:**
- Check algorithm flow: proposal → tokenize → search → match or clear
- Conflict resolution loop: conflict detected → team reviews → update decision
- Example: false positive handling

**Apply This** (5 patterns):
1. Heuristic-based matching — solves: lightweight, explainable validation — adapt: choose heuristic based on domain — pitfall: don't over-trust heuristics
2. Non-blocking validation — solves: inform without friction — adapt: warn clearly, let user decide — pitfall: don't silently ignore warnings
3. Newest-first matching — solves: prioritize recent context — adapt: order by relevance — pitfall: don't assume first match is the most important
4. Stopword tuning — solves: reduce false positives — adapt: domain-specific words — pitfall: don't make stopword list too aggressive (may hide real conflicts)
5. Manual review loop — solves: catch validation errors — adapt: enable teams to override — pitfall: don't force manual review to be friction (make it fast)

---

## PART III: Live Feedback & Extensibility

### Chapter 5: The Figma Plugin Architecture
**Word estimate:** 180 lines

**Opening:**
- Figma is where design intent is born. Decisions get made in critiques, comments, selection discussions.
- The IRP Figma plugin makes those decisions capturable without leaving Figma.
- This chapter: the bridge pattern, how plugin talks to server, how server talks to core.

**Body:**
1. **Three-Layer Design**
   - Figma app layer (sandboxed, can't write filesystem)
   - Plugin main layer (manages Figma API, bridges to UI)
   - Bridge server layer (local HTTP, writes to .irp/)
   - Design: separation of concerns, each layer has one responsibility

2. **Plugin Main (code.js)**
   - Runs in Figma's restricted context
   - Embeds ui.html as iframe
   - Relays messages: get-context → Figma API → postMessage to UI
   - Notifies user on success/error
   - Minimal logic: pure relay

3. **Bridge Server (server.py)**
   - Local HTTP server on port 3002
   - Accepts POST /capture with {decision, why, context}
   - Constructs IRP entry with source="figma"
   - Appends to ledger, rebuilds current
   - Fetches Figma comments for auto-populate (if FIGMA_PAT set)
   - Handles CORS for iframe cross-origin calls

4. **Comment Auto-Populate**
   - UI calls GET /comments?file_key=...
   - Bridge queries Figma API for resolved comments
   - Returns last 10 resolved (most recent first)
   - UI populates dropdown → user can select comment as context
   - Design: feedback often appears as comments; bridge them to decisions

5. **Conflict Preview**
   - Before capture form is shown, check runs
   - If conflict: show matched decision to user
   - User can review, accept risk, or refine proposal
   - Design: decision moment is the moment of capture, not after

**Diagrams needed:**
- Three-layer architecture with message flows
- Plugin message sequence: get-context → context → capture → captured
- Comment fetch flow: file_key → Figma API → comment list
- Conflict preview UI mockup (conceptual)

**Apply This** (5 patterns):
1. Bridge pattern for sandboxed tools — solves: enable I/O in restricted environments — adapt: bridge = local proxy for tool — pitfall: don't assume bridge availability (handle gracefully)
2. Message-based relay — solves: cross-layer communication without tight coupling — adapt: use message types, version messages — pitfall: don't overly complicate message protocol
3. Real-time feedback fetch — solves: auto-populate from external source — adapt: fetch on demand, cache briefly — pitfall: don't block on slow external APIs
4. Conflict preview at decision moment — solves: catch issues early — adapt: show conflict before committing — pitfall: don't let conflict UI be too noisy
5. Source-specific enrichment — solves: trace decisions back to tool context — adapt: capture page, selection, file_key — pitfall: don't create data dependencies on tool APIs

---

### Chapter 6: Extensibility & Cross-Engine Context
**Word estimate:** 180 lines

**Opening:**
- One plugin is one tool. Real teams use many tools.
- How do decisions captured in Figma inform work in Slack? How do they inform external AI models?
- This chapter: REST API, collab.py, and the principle of context portability.

**Body:**
1. **REST API Layer**
   - GET /inherit → returns active decisions (current.json)
   - GET /why?id=... → explain specific decision
   - POST /check → validate proposal
   - Design: any HTTP client can query IRP state
   - Use case: custom tools, CI/CD pipelines, external workflows

2. **collab.py: Context Injection for AI**
   - Reads current.json, injects into LLM prompt
   - Optional topic filter: focus on positioning, architecture, etc.
   - Supports multiple AI backends: OpenAI, Anthropic, local Ollama
   - Design principle: context is injected; decisions are NOT modified
   - Use case: "Critique this design against our decisions" → runs check locally → calls external LLM with context → returns analysis

3. **Cross-Engine Context Flow**
   - Decision captured in Figma → written to ledger → available in current.json
   - Team member works in Slack → calls `irp inherit` → sees decisions → makes proposal
   - Engineer working with Claude API → calls collab.py → gets decision context → asks Claude for analysis
   - Design: single source of truth (.irp/) fed to multiple tools

4. **Conflict Detection Across Tools**
   - Figma sensor captures decision A
   - Slack sensor proposes decision B
   - Check runs: detects overlap
   - Both decisions logged (non-blocking)
   - Team resolves via git notes or new decision entry

5. **Portability Design**
   - Decisions are tool-neutral (no Figma-specific, Slack-specific fields)
   - Source field indicates origin, but decisions don't depend on tool being online
   - Consequence: if Figma plugin breaks, decisions are still accessible
   - Design: .irp/ is sovereign, tools are transient

**Diagrams needed:**
- Multi-tool context flow: Figma → IRP ← Slack ← collab.py
- REST API usage: client → /inherit → current.json
- collab.py flow: project → read current.json → format context → inject → external LLM → return

**Apply This** (5 patterns):
1. REST API for portability — solves: enable any client to access decisions — adapt: expose key endpoints (inherit, why, check) — pitfall: don't over-expose internal state
2. Context injection architecture — solves: feed decisions to external systems without modification — adapt: format context clearly, mark with metadata — pitfall: don't assume external system respects decision context
3. Topic filtering — solves: focus context on relevant area — adapt: keyword-based or structured tags — pitfall: don't filter so aggressively that context becomes useless
4. Multi-backend support — solves: avoid AI model lock-in — adapt: abstract API calls, support multiple endpoints — pitfall: don't assume all models accept same message format
5. Sovereignty via local storage — solves: avoid tool dependency — adapt: keep source of truth locally, sync selectively — pitfall: don't sync decisions to external systems (that reverses the architecture)

---

## EPILOGUE

### Chapter 7: Patterns & Synthesis
**Word estimate:** 150 lines

**Opening:**
- You've read about IRP's architecture. Now: what can you apply to *your* decisions?
- This chapter extracts principles, discusses evolution, and outlines the road ahead.

**Body:**
1. **Core Patterns (Recap & Generalization)**
   - Append-only audit logs: apply to any decision domain
   - Derived state with rebuild: avoid dual truth
   - Lightweight heuristics: explainability over sophistication
   - Non-blocking validation: inform without friction
   - Bridge architecture: integrate without coupling

2. **Decision Survivability as a Design Goal**
   - Most systems optimize for: performance, consistency, correctness
   - IRP optimizes for: *decisions surviving system change*
   - Consequence: design reflects this (append-only, portable, auditable)
   - Question for your domain: what happens to decisions when a tool dies?

3. **Tradeoffs Made in IRP**
   - Chose: ledger-as-source over database (simpler, more portable)
   - Chose: keyword overlap over embeddings (deterministic, explainable)
   - Chose: last-10 window over full history (scoped, manageable)
   - Chose: non-blocking checks over policy enforcement (team autonomy)
   - All are domain-specific: your tradeoffs may differ

4. **Evolution Roadmap (Sketch)**
   - Slack sensor: capture thread discussions natively
   - Comment threading: link decisions to supporting discussions
   - Rollback semantics: mark decision as withdrawn, keep history
   - Integration webhooks: notify external systems on decision changes
   - Encrypted ledger: for sensitive decisions (IP, strategy)

5. **Open Questions**
   - How do you handle decisions that span multiple projects?
   - How do you share decisions between teams without leaking context?
   - How do you deprecate old decisions gracefully?
   - How do you measure whether decisions are being followed?

**Diagrams needed:**
- Pattern hierarchy: core principles → design patterns → implementation details
- Tradeoff matrix: IRP's choices vs. alternatives
- Evolution sketch: Slack sensor, comment threading, etc.

**Apply This** (5 patterns):
1. Design for survivability — solves: decisions don't disappear on tool transition — adapt: log append-only, keep portable — pitfall: don't over-engineer (simplicity matters)
2. Expose rationale, not just decision — solves: future readers understand *why* — adapt: always include why/confidence fields — pitfall: don't make rationale entries so verbose they're never read
3. Separate validation from enforcement — solves: inform teams without controlling them — adapt: validate broadly, enforce narrowly — pitfall: don't ignore validation signals
4. Plan for evolution — solves: avoid accumulating technical debt — adapt: version data format, plan migrations — pitfall: don't change format mid-flight (breaking change)
5. Measure decision health — solves: know whether decisions are being followed — adapt: log decision updates, surface trends — pitfall: don't use metrics to shame teams (use them to learn)

---

## Content & Coverage Summary

### By Part

| Part | Chapters | Lines | Focus |
|------|----------|-------|-------|
| I: Foundations | 1-2 | ~350 | Problem, abstractions, state, conflict detection |
| II: Recording & Validation | 3-4 | ~300 | Capture, validation, sensor architecture |
| III: Feedback & Extensibility | 5-6 | ~360 | Figma plugin, REST API, collab.py, portability |
| Epilogue | 7 | ~150 | Patterns, synthesis, evolution, open questions |
| **TOTAL** | **7** | **~1,160** | Full arc from problem to application to future |

### By Pattern Type

- **Architectural:** append-only logs, derived state, bridge pattern, REST APIs
- **Algorithmic:** sequential IDs, keyword overlap, tokenization, stopword filtering
- **Process:** non-blocking validation, sensor architecture, context enrichment
- **Integration:** plugin design, cross-engine context, API composition

### Coverage Checklist

- [x] Problem statement (Ch1)
- [x] Core data structures (Ch1-2)
- [x] Ledger design (Ch1-2)
- [x] State derivation (Ch2)
- [x] Conflict detection (Ch2, 4)
- [x] Capture flow (Ch3)
- [x] Sensor architecture (Ch3)
- [x] ID generation (Ch3)
- [x] Validation patterns (Ch4)
- [x] Figma plugin (Ch5)
- [x] Bridge pattern (Ch5)
- [x] REST API (Ch6)
- [x] Context injection (Ch6)
- [x] Multi-tool flows (Ch6)
- [x] Pattern synthesis (Ch7)
- [x] Evolution roadmap (Ch7)
