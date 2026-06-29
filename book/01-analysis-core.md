# PHASE 1: Core Analysis
Analysis of `irp/core/` — Architecture, State Model, Key Abstractions

## Architecture Overview

IRP is structured as a **ledger-as-source-of-truth** system with derived active state:

- **Single entry point:** `irp/core/irp.py` — dispatcher that routes commands
- **Data layer:** `irp/core/store.py` — handles ledger (JSONL) and current state (JSON)
- **Command modules:** `irp/core/commands/` — separate concerns for capture, check, why, inherit, bootstrap, demo

### Directory Organization

```
irp/core/
├── irp.py          # Dispatcher: CLI parser → command router
├── store.py        # Data layer: ledger I/O, state derivation
├── __init__.py     # Package marker
└── commands/
    ├── __init__.py
    ├── capture.py  # Record intent (interactive or stdin)
    ├── check.py    # Conflict detection via keyword overlap
    ├── why.py      # Query and explain decision lineage
    ├── inherit.py  # Show current context
    ├── bootstrap.py # Bulk ingest from git/docs/files
    └── demo.py     # Generate synthetic threads for testing
```

## State Model

### Two Files, Two Responsibilities

1. **Ledger (`.irp/ledger.jsonl`)**
   - Append-only log of all decisions ever made
   - One JSON object per line
   - Append-only once written (no updates, only appends)
   - Source of truth for historical context
   - Fields: type, id, what, why, confidence, timestamp, source, tags

2. **Current (`.irp/current.json`)**
   - Derived state computed from ledger
   - Contains only the last 10 active decisions (type="decision")
   - Rebuilt after every capture or modification
   - Used as the "project bridge" for conflict checking
   - Format: `{version: 1, active: [...]}`

### Data Structure: Decision Entry

```python
{
    "type": "decision",           # Always "decision"
    "id": "IRP-2026-04-12-001",   # Sequential: IRP-YYYY-MM-DD-NNN
    "what": "...",                # The decision (required)
    "why": "...",                 # Justification (required)
    "confidence": "high|medium|low",
    "timestamp": "2026-04-12",    # ISO date
    "source": "interactive|stdin|figma|slack|...",
    "tags": [],                   # Optional topic tags
    "source_ref": {}              # Optional: channel_id, thread_ts, etc.
}
```

## Key Abstractions

### 1. Append-Only Ledger Pattern
- Append-only history enables audit trails and conflict forensics
- JSONL format: streaming-friendly, line-delimited, no corrupt-JSON risk
- Sequential IDs encode both date and daily sequence (enables grouping)

### 2. Derived State (Current.json)
- **Why derived, not stored:** current state is a view, not truth
- **Why last 10 only:** scope management — focus on recent decisions
- **Rebuild trigger:** happens after every ledger write
- **Algorithm:** filter type="decision" from ledger, take tail -10

### 3. Non-Blocking Validation
- Check command does NOT prevent capture
- Returns status "conflict" or "clear" but always exits 0 (non-blocking)
- Design intent: inform stakeholders of overlaps without friction
- Conflict exit code: 10 (non-zero but distinct from error=1)

## Command Patterns & Entry Points

All commands follow the same signature:
```python
def run_command(project_root: Path, irp_dir: Path, args) -> dict:
    # Returns {command, status, ...payload, text}
```

### Dispatcher (irp.py)
- Builds argparse with 6 subcommands
- Each command gets dedicated argument parser
- Returns exit code: 0 (ok), 1 (error), 10 (conflict)
- Output mode: human-readable text by default, JSON with `--json` flag

### Store Module (store.py)
Key functions:
- `ensure_irp_dir()` — creates `.irp/` with ledger + current if missing
- `read_ledger()` — parse JSONL, skip malformed lines
- `read_current()` — read derived state
- `write_current()` — atomic write
- `append_ledger_entry()` — append-only write (no overwrite)
- `next_irp_id()` — generate sequential IDs per day
- `rebuild_current()` — derive state: last 10 decision-type entries

## Design Decisions & Rationale

### Why JSONL for the Ledger?
- Line-delimited: each decision is atomic
- Streaming-friendly: can append without reading whole file
- Simpler than SQLite for single-file deployments
- Parse errors don't corrupt history (skip bad lines)
- Git-friendly: diffs show line-by-line changes

### Why Current.json as Separate File?
- Enables stakeholders to fork decisions without re-computing
- Can be shared across tools (plugins, APIs, external systems)
- Rebuilds deterministically from ledger (no hidden state)
- Window of 10 keeps it small and focused

### Why Non-Blocking Check?
- IRP doesn't enforce policy; it informs
- Stakeholders decide whether to resolve conflicts
- Exit code 10 lets callers distinguish "conflict" from "error"
- No data loss — conflicts are logged, not hidden

### Why Sequential IDs?
- Encodes date for natural grouping (what happened today?)
- Human-readable (no UUIDs)
- Deterministic generation (no randomness, no collisions per day)
- Enables day-scoped queries without regex

## Integration Points

### External Systems
- **Figma plugin** → bridge/server.py → capture
- **Slack** → slack_capture.py → capture
- **REST API** → api/server.py → query ledger
- **collab.py** → reads current.json, injects context into LLM prompts

### Bootstrap System
- Scans git commits, doc files, project structure
- Generates candidate entries
- Writes in bulk (with --limit, --dry-run options)
- Produces audit report

## Summary: Core Layer Design

IRP core is intentionally minimal:
- Single dispatcher, thin command layer
- Data layer handles ledger + state derivation
- No caching, no indexing, no DSL
- All state is reconstructible from ledger
- Emphasis on auditability and portability (decisions travel)
