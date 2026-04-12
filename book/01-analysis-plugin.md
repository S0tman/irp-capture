# PHASE 1: Plugin & Bridge Analysis
Analysis of `irp/figma_plugin/` and `tools/collab.py` — Architecture, Bridge Pattern, Cross-Engine Context

## Figma Plugin Architecture

### Three-Layer Design

```
┌─────────────────────────────────────────┐
│ Figma App (Sandbox)                     │
│  - Cannot access external network       │
│  - Can only talk to plugin context      │
└─────────────────────────────────────────┘
                    ↕
         (figma.ui.postMessage)
                    ↕
┌─────────────────────────────────────────┐
│ Plugin Main (code.js)                   │
│  - Runs in restricted plugin context    │
│  - Bridges Figma API to UI iframe       │
│  - Manages file/page/selection context  │
└─────────────────────────────────────────┘
                    ↕
         (fetch, HTTP CORS)
                    ↕
┌─────────────────────────────────────────┐
│ Bridge Server (bridge/server.py)        │
│  - Local HTTP (port 3002)               │
│  - Proxies to IRP ledger                │
│  - Fetches Figma comments via API       │
└─────────────────────────────────────────┘
                    ↕
┌─────────────────────────────────────────┐
│ IRP Core (irp/core/)                    │
│  - Appends to ledger.jsonl              │
│  - Rebuilds current.json                │
└─────────────────────────────────────────┘
```

### Plugin Main (code.js)

**Key functions:**
- `figma.showUI(__html__, {...})` — embed ui.html iframe
- `figma.ui.onmessage` — receive messages from iframe
- `figma.ui.postMessage` — send context to iframe

**Message types:**
- `get-context` → fetches page name, selection, file key
- `context` (response) → {page, selection, fileKey}
- `captured` → notify user on successful IRP write
- `error` → show error notification if bridge unreachable

**Design:** Plugin acts as a dumb relay. Real logic (form, submission, API calls) lives in the iframe (ui.html).

### UI Layer (ui.html)

*Not fully shown in analysis, but inferred from bridge/code.js:*

**Expected responsibilities:**
- Form fields: "What was decided?", "Why does it matter?"
- Context display: current page, selection, file key
- Comment auto-populate (if FIGMA_PAT enabled)
- Submit → POST /capture to bridge server
- Handle conflict preview (via /check endpoint)

### Bridge Server (bridge/server.py)

**HTTP Endpoints:**

#### `GET /comments?file_key=...`
- Fetches resolved comments from Figma file
- Requires FIGMA_PAT env variable
- Returns last 10 resolved comments (most recent first)
- CORS enabled for iframe requests
- Falls back to empty list if FIGMA_PAT not set

#### `POST /capture`
- Receives {decision, why, context}
- Constructs full IRP entry with Figma context
- Calls irp.core directly (no subprocess)
- Appends to ledger, rebuilds current.json
- Returns 201 on success

**Context enrichment:**
```python
entry = {
    "type": "decision",
    "what": decision,
    "why": why,
    "confidence": "medium",
    "timestamp": date.today().isoformat(),
    "source": "figma",
    "context": {
        "page": context.get("page"),
        "selection": context.get("selection"),
        "file_key": context.get("file_key")
    }
}
```

**Project root handling:**
- Accepts `--project-root` CLI argument
- Default: cwd
- Ensures .irp/ exists at that root
- Critical for multi-project setups

**CORS headers:**
```python
def _cors_headers(self):
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
```

**SSL/HTTPS handling:**
- Bridge runs on plain HTTP (3002)
- Figma iframe can call it directly (localhost, no CORS)
- For remote access, use ngrok or reverse proxy

## Bridge Pattern: Design Rationale

### Why a Separate Bridge Server?

1. **Sandbox isolation:** Figma plugin cannot directly write filesystem
2. **Context injection:** Bridge runs in project directory, has access to .irp/
3. **Real-time feedback:** Can fetch comments, check conflicts, preview impact
4. **Extensibility:** Future sensors (Slack, Pencil.dev, etc.) use same bridge pattern

### Polling for Resolved Comments

**Feature:** Auto-populate "why" field from recently resolved Figma comments

**Flow:**
1. UI fetches comments via `GET /comments?file_key=...`
2. Bridge calls Figma API (requires FIGMA_PAT)
3. Filters to resolved-only, orders by resolve_at (newest first)
4. Returns up to 10 comments
5. UI populates dropdown / text field

**Design intent:** Feedback often appears as comments. This bridges the gap between design discussion (Figma comments) and decision capture (IRP).

## collab.py: Context Injection for Remote AI

### Purpose
IRP is local-first. But decisions should inform remote AI models. collab.py is a disposable bridge that injects IRP context into LLM prompts.

**Philosophy:**
- Automation prepares context. Cannot decide, hide, or infer.
- collab.py does NOT modify .irp/
- Does NOT maintain state
- Transport (API) is commodity. Protocol (.irp/) is sovereign.

### Usage Patterns

```bash
# Basic: inject IRP context automatically
python3 tools/collab.py "Critique this approach: ..."

# Filter context to a topic
python3 tools/collab.py --topic "positioning" "What's unclear?"

# Pipe from stdin
echo "Review this design" | python3 tools/collab.py

# Override model
python3 tools/collab.py --model gpt-4o "..."

# Skip IRP context entirely
python3 tools/collab.py --no-irp "What's the weather?"

# Use local Ollama for full sovereignty
COLLAB_API_BASE=http://localhost:11434/v1 python3 tools/collab.py --model llama3 "..."
```

### Context Reading (read_irp_context)

**Input:** project_root, optional topic filter

**Output:** Formatted markdown block of active decisions:

```markdown
# IRP Context
## Active Decisions (last 10)

IRP-2026-04-12-001
What: Use React for core UI
Why: Team expertise, ecosystem maturity
Confidence: high
Timestamp: 2026-04-12
Source: Figma

...
```

**Topic filtering:**
- If --topic "positioning" provided
- Filter to decisions whose what/why contain any topic keyword
- Use case: focus LLM on positioning decisions only

### API Integration

**Support for multiple backends:**
- Default: OpenAI (gpt-4o)
- Can specify any model: `--model gpt-4o-mini`, `--model claude-3-sonnet`
- API base configurable: `COLLAB_API_BASE=https://api.anthropic.com/v1`
- Local support: Ollama via `http://localhost:11434/v1`

**Environment:**
- `OPENAI_API_KEY` — primary API credential
- `COLLAB_API_BASE` — override endpoint (default: https://api.openai.com/v1)
- `COLLAB_MODEL` — override model (default: gpt-4o)
- `.env` auto-loaded from tools/ directory

### Message Format

**System prompt included** (inferred from code):
```
"You are an expert evaluator of software decisions. 
Review the context and user prompt carefully. 
Be direct and opinionated."
```

**User message format:**
```
IRP Context:
[active decisions in markdown]

---

User Query:
[user's actual question]
```

## Cross-Engine Context Architecture

### Design Intent
Decisions should be portable. IRP doesn't force a single tool. Instead:
1. Decisions live in local .irp/ (source of truth)
2. Plugins/bridges inject context into different tools (Figma, Slack, external AI)
3. Each tool can propose changes, but .irp/ remains the arbiter

### Integration Points

| Tool | Component | Flow |
|------|-----------|------|
| Figma | Plugin + Bridge | Draft decision in Figma → POST /capture → append ledger |
| Slack | slack_capture.py | Thread discussion → message_ref → capture as decision |
| AI models | collab.py | Local decisions → prompt injection → external reasoning |
| REST API | api/server.py | HTTP clients → query /inherit, /check, /why |

### Conflict Detection Across Engines

**Scenario:** Designer proposes feature A in Figma while engineer proposes conflicting feature B in Slack thread.

**Resolution:**
1. Figma captures decision A → check command runs
2. check finds no conflicts, A is written
3. Slack captures decision B → check runs
4. check finds A in active decisions (keyword overlap)
5. Slack thread gets marked "conflict" but decision still written (non-blocking)
6. Team resolves manually; whichever decision is withdrawn is logged in a note

### Sovereignty & Portability

**Local-first principle:**
- All decisions stored locally (.irp/) first
- Bridge servers are stateless (no decision logic)
- External tools (Figma, Slack) cannot lock decisions
- .irp/ is the only source of truth

**Consequence:** If Figma plugin is broken, decisions can still be captured via CLI or other sensors. If Slack integration goes offline, Figma still works.

## Summary: Plugin & Bridge Design

Figma plugin architecture demonstrates the **bridge pattern for decision capturing**:
1. External tool (Figma) = source of intent
2. Local bridge = context gateway
3. IRP core = decision ledger
4. collab.py = context exporter to remote AI

This enables:
- Multi-tool decision capture (Figma, Slack, etc.) without lock-in
- Real-time conflict detection across sources
- Decision portability (local storage, remote reasoning)
- Composability (each bridge is independent, failures don't cascade)
