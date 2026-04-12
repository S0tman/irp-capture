# PHASE 1: Commands Analysis
Analysis of `irp/core/commands/` — Control Flow, Patterns, Integration

## Command Inventory

| Command | Purpose | Mode | Output |
|---------|---------|------|--------|
| `capture` | Record a new decision | interactive or stdin | captured entry + id |
| `check` | Test proposal against active decisions | proposal text | conflict or clear |
| `why` | Explain decision lineage | query by id or latest | entry + provenance |
| `inherit` | Show current IRP context | no args | active 10 decisions |
| `bootstrap` | Bulk ingest from git/docs/files | scan mode | audit report |
| `demo` | Generate synthetic data | scenario-based | thread + ledger entry |

## Command Patterns

### Universal Pattern
All commands return a dictionary:
```python
{
    "command": "name",
    "status": "ok|conflict|skipped|error|...",
    "text": "human-readable output",
    **payload_fields
}
```

The dispatcher then:
- Prints `text` by default
- Prints JSON (full dict) with `--json` flag
- Returns exit code: 0 (ok), 1 (error), 10 (conflict)

### Capture Command

**Modes:**
1. **Interactive:** prompts user for what/why/confidence
2. **Stdin:** reads pre-formed JSON candidate from pipe

**Flow:**
1. Read candidate (interactive or stdin)
2. Set defaults: type="decision", timestamp=today, confidence="medium"
3. Generate sequential ID
4. Display preview
5. **Non-interactive only:** ask confirmation (c=confirm, s=skip)
6. If confirmed: append to ledger, rebuild current.json
7. Return captured entry

**Key detail:** Stdin mode skips confirmation (suitable for automated flows). Interactive mode requires explicit "c" input.

**Integration point:** Figma bridge and Slack capture both feed into this via stdin mode.

### Check Command

**Purpose:** Lightweight conflict detection without semantic understanding.

**Algorithm:**
1. Read proposal text (required argument)
2. Load current.json (active decisions)
3. Tokenize proposal: split on `\W+`, lowercase, remove stopwords
4. For each active decision (newest-first):
   - Tokenize its what + why fields
   - Compute overlap with proposal tokens
   - If overlap found: return first match
5. If match: status="conflict", return matched decision + matched words
6. If no match: status="clear"

**Stopwords:** 48 common words (articles, prepositions, verbs, modifiers) filtered to reduce false positives.

**Non-blocking design:** Status "conflict" → exit code 10, not 1. Allows callers to distinguish warning from error.

**Use case:** Figma plugin calls check before capture to warn user if proposal overlaps with active decisions.

### Why Command

**Two modes:**

1. **With --id:** Lookup specific entry from ledger
   - Find entry by exact id match
   - If not found: status="not_found"
   - If found: return entry + provenance (source, channel, thread)

2. **Without --id:** Show latest active decision
   - Read current.json
   - If empty: status="empty"
   - If has entries: return latest (tail of active list) + count

**Provenance lines:** Vary by source
- Slack: includes channel_id, thread_ts
- Figma/interactive: just source label
- All: timestamp, what, why, confidence

### Inherit Command

**Purpose:** Context projection — show what systems downstream should know about.

**Flow:**
1. Load current.json
2. Return active decisions + version number
3. Output: human text or JSON (for API consumers)

**Design intent:** Downstream systems (plugins, external AI models) call inherit to understand the project's active decision context before making proposals.

### Bootstrap Command

**Purpose:** Bulk ingest from project artifacts.

**Scan modes:**
- `--from git` — parse commits, extract decision-like messages
- `--from docs` — scan markdown/doc files for decision patterns
- `--from files` — scan project structure + comments
- `--from all` — all three (default)

**Options:**
- `--dry-run` — preview candidates without writing
- `--limit N` — cap entries written (default 50)
- `--write-report` — save audit to `.irp/bootstrap_reports/<timestamp>.md`

**Output:** Report showing candidates found, written count, conflicts, gaps.

### Demo Command

**Purpose:** Generate synthetic threads for testing/demos.

**Scenarios:** product-decision, architecture, pricing, workflow, policy

**Output modes:**
1. **Local write:** `--write-thread` saves to `.irp/demo_threads/<timestamp>-<scenario>-<confidence>.md`
2. **Slack post:** `--post-to-slack CHANNEL_ID` posts thread + Ledger bot candidate block to Slack channel (no local write)

**Use case:** Populate IRP with realistic data for stakeholder demos or development testing.

## Error Handling & Exit Codes

### Exit Codes (from irp.py main)
- **0:** Command succeeded (capture completed, check clear, why ok, etc.)
- **1:** Unhandled exception (no irp_dir, bad JSON, etc.)
- **10:** Conflict detected (check command matched active decision)

### Failure Modes

**Capture:**
- Skip on user input 's' → status="skipped", exit 0
- Bad stdin JSON → ValueError, exit 1
- No write permissions → exception, exit 1

**Check:**
- Empty proposal → matches nothing, status="clear"
- No active decisions → nothing to check, status="clear"
- Match found → status="conflict", exit 10

**Why:**
- Bad --id → status="not_found", exit 0 (not error)
- Empty ledger → status="empty", exit 0

**All commands:**
- Missing .irp/ledger.jsonl → created by ensure_irp_dir
- Corrupted JSONL line → skipped in read_ledger (robust parse)

## Data Transformation Patterns

### From Candidate to Ledger Entry
```python
# Input (partial)
{"what": "...", "why": "..."}

# Enriched by capture
{
    "type": "decision",
    "what": "...",
    "why": "...",
    "confidence": "medium",          # default
    "timestamp": "2026-04-12",       # today
    "source": "stdin" or "interactive",
    "id": "IRP-2026-04-12-001",      # generated
    "tags": []
}
```

### From Ledger to Current
```python
ledger = [all entries ever]
active = [e for e in ledger if e["type"] == "decision"]
current = {"version": 1, "active": active[-10:]}
```

### Check: Text → Tokens
```python
text = "Use React for the UI"
tokens = {"use", "react", "ui"}  # after stopword filter
```

## Integration with External Systems

### Figma Bridge Flow
1. Plugin (code.js) sends `{decision, why, context}` to bridge
2. Bridge (server.py) constructs entry, calls capture via stdin
3. Capture appends to ledger, rebuilds current
4. Bridge posts 201 response back to plugin
5. Plugin notifies user ✓

### Slack Capture (future/parked)
- Would read Slack thread content
- Map thread to decision + provenance
- Call capture via stdin
- Post confirmation back to thread

### REST API (api/server.py)
- GET `/context` → inherit
- POST `/check` → check
- GET `/why?id=...` → why
- Enables remote clients to query IRP state

### collab.py (Context Projection)
1. Read current.json
2. Filter by optional --topic keyword
3. Format as markdown context block
4. Inject into LLM prompt template
5. Call external model (OpenAI, local Ollama, etc.)
6. Return raw response (collab doesn't modify .irp/)

## Summary: Commands Layer Design

Commands are thin orchestrators:
- Each handles one concern (capture, validate, explain)
- All rely on store.py for data I/O
- All return consistent {command, status, text, ...} format
- Non-blocking design (warnings don't block workflow)
- Composable via stdin (suitable for piping, automation)
- Integration points support Figma, Slack, REST, LLM context
