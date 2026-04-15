# irp-capture

## Intent Record Protocol

**IRP is a decision ledger that records why things were done.**

Confirm a decision → it is written to a local file → it stays forever.

```bash
# Before starting something new
irp why

# IRP-2026-04-08-001  Decision: Use Postgres for the reporting service
# Why: Redis rejected — query patterns require joins
#
# → No need to reopen this debate
```

---

Three sprints ago your team made a hard call on the architecture.
Everyone was in the room. It was the right decision, for the right reasons.
Last week, someone reopened the debate.
Because the reasoning was not written down anywhere.

This is not a memory problem. It is a meaning or decision, reasoning traceability problem.

---

## Who this is for

IRP is designed for teams making decisions together:

- Engineering teams choosing between approaches
- Design and product teams approving creative direction
- Pre-sales and delivery teams aligning on scope

If you are working alone, this may feel unnecessary.
If you are working in a team, it becomes obvious quickly.

---

## The reasoning gap

AI tools are everywhere in your workflow now.
Prompts, reviews, approvals, feedback loops.
Good reasoning happens constantly. Almost none of it is preserved.

Not because it was not captured somewhere.
But because nothing was designed to capture *why*.

Six months later:

- A new engineer asks why the architecture was designed this way.
- An audit asks which human approved this creative direction.
- A product decision gets relitigated because no one remembers the reasoning.

Every new AI session starts from zero.
Every new team member re-learns what was already decided.
Every tool has logs of *what happened*. None of them record *why it mattered*.

> Storing everything is easy. Knowing what mattered is not.

---

## Before / After

**Without IRP**

```
New engineer joins
→ asks why the system works this way
→ team debates again
→ decision gets re-made
```

**With IRP**

```
New engineer joins
→ runs `irp why`
→ sees the decision and the reasoning
→ moves forward without reopening it
```

---

## What IRP does

IRP is an append-only decision ledger that lives alongside your work.

Think of it as a ship's log.
Every course correction. Every judgment call. Every hard decision.
Not because the ocean would remember but because the next watch
needs to know why the ship is where it is.

When a human confirms a decision, IRP records it.
That is the entire model.

No AI inference. No automatic capture. No vector search.
A human confirms a decision. IRP records it.
The ledger is a plain text file. It never changes an existing entry.

```
.irp/
  ledger.jsonl     ← append-only canonical record
  current.json     ← last 10 decisions, derived from ledger
```

Every entry looks like this:

```json
{
  "id": "IRP-2026-04-08-001",
  "timestamp": "2026-04-08T14:32:11Z",
  "decision": "Use Postgres for the reporting service",
  "why": "Redis was considered but rejected. The query patterns require joins that do not map cleanly to key-value. Team aligned on this in the April 8 architecture review.",
  "source": "cli",
  "confirmed_by": "johan",
  "context": "Backend infrastructure Q2 2026"
}
```

Open the file. Read it. No tooling required.

---

## The human confirmation invariant

IRP does not decide what matters. You do.

Every entry in the ledger was confirmed by a human.
This is not a design limitation. It is the point.

An AI can produce a hundred options.
Only one was chosen, and someone chose it for a reason.
That reason is what IRP captures.

This makes the ledger auditable, defensible, and trustworthy
in a way that ambient memory systems cannot be.

---

## What survives

When systems change, most context is lost.

You move to a new tool.
A team member leaves.
The platform updates.
Infrastructure changes.
An AI service goes down.

Most knowledge disappears with it.

IRP ensures one thing survives:

- What was decided
- Why it was decided

The `.irp/` directory travels with your work. It is not trapped in a tool or a service.
It is a plain text file. It lives where your code lives.

New team member joins?
They run `irp why` and see what was already decided.

New system, new tool, new team?
The ledger travels.
The reasoning survives.

This is not about memory. It is about meaning traveling across time, tools, and teams.

---

## Under the hood

| Component | Choice | Why |
|---|---|---|
| Storage | Append-only `.jsonl` flat file | Human-readable, no database required |
| Retrieval | Deterministic — read the ledger | No embeddings, no similarity guessing |
| Capture | Human-confirmed via sensor | No AI decides what matters |
| Format | Plain JSON per line | Works in any editor, any language |
| Dependencies | Python 3.9+, no cloud | Runs entirely on your machine |

No ChromaDB. No vector index. No model dependency.
The ledger is the truth. Not an approximation of it.

---

## Get started

```bash
pip install irp-capture
```

```bash
# Set the context for your project
irp inherit "Project: My project — backend API, Q2 2026"

# Capture a decision
irp capture "Decision: Use Postgres for the reporting service" \
  --why "Redis considered but rejected — query patterns require joins"

# Review recent decisions
irp why
```

```
# Output:
# IRP-2026-04-08-001
# Decision: Use Postgres for the reporting service
# Why: Redis considered but rejected — query patterns require joins
#
# → No need to reopen this debate
```

The ledger is created at `.irp/ledger.jsonl` in your working directory.
That file is yours. It does not leave your machine unless you choose.

---

## Sensors

Sensors are optional. The ledger is the system.

IRP captures decisions from the tools your team already uses.
Nothing changes about how you work.

| Sensor | How it captures |
|---|---|
| **Claude Code skill** | Type `capture` inside any Claude session. Captures the decision the moment it is made, without leaving your workflow. |
| **CLI** | `irp capture` — capture any decision directly from the terminal |
| **Discord** | Right-click any message → "Capture decision" → fill in what/why/tags. Modal writes directly to ledger. Community-native, emoji-friendly. Setup: see `DISCORD-SENSOR-SETUP.md`. |
| **Slack** | Resolve a thread with a decision. The bot writes it to the ledger. |
| **Figma** | Resolve a design comment. The plugin captures the decision. Start the bridge with `--project-root /path/to/project` — see `irp/figma_plugin/README.md`. |
| **Git hook** | Capture architecture decisions at commit time |
| **PR bot** | Add to any GitHub repo — warns on PRs that conflict with active decisions. See setup below. |
| **REST API** | Build custom integrations via HTTP. `pip install irp-capture[api]` — see `irp/api/README.md`. |
| **More coming** | VS Code, Python SDK |

No tool talks to another. Everything talks to the ledger.

---

## Integrations

Integrations are optional sync targets. After each `irp capture`, IRP can write the decision to your knowledge base and agent memory — automatically.

### Obsidian

Writes each decision as a `.md` file to your Obsidian vault. No extra dependencies needed — Obsidian vaults are plain directories.

```bash
# Set in your shell or project .env
export IRP_OBSIDIAN_VAULT="/Users/you/Notes"
```

Each decision lands at `{vault}/decisions/IRP-YYYY-MM-DD-NNN.md` with YAML frontmatter:

```markdown
---
id: IRP-2026-04-15-001
type: decision
timestamp: 2026-04-15
confidence: high
tags: [architecture]
source: cli
---

# Use Postgres for the reporting service

## Why it matters

Redis considered but rejected — query patterns require joins.
```

### MemPalace

Writes each decision into the MemPalace `mempalace_drawers` ChromaDB collection. Your agents can now semantically query past decisions alongside their other memories.

```bash
pip install 'irp-capture[mempalace]'

# Optional — defaults to ~/.mempalace/palace
export IRP_MEMPALACE_PATH="/Users/you/.mempalace/palace"
```

If MemPalace is not installed or the palace directory does not exist, IRP skips silently. No error. No friction.

### The sovereign stack

```
Obsidian vault     ← your knowledge
MemPalace palace   ← your agent's memory
IRP ledger         ← your decisions

All three. All local. No SaaS required.
```

---

## Use with Claude Code

If you clone this repo or have `SKILL.md` in your project root,
Claude Code discovers the IRP skill automatically.

**Option A — Clone the repo (skill is included):**

```bash
git clone https://github.com/S0tman/irp-capture.git
cd irp-capture
# Claude Code now has the IRP skill
```

**Option B — Add the skill to an existing project:**

```bash
pip install irp-capture
curl -O https://raw.githubusercontent.com/S0tman/irp-capture/main/SKILL.md
# Claude Code now has the IRP skill
```

Once active, type `capture` inside any Claude session to record a decision.

---

## Daily workflow

**The highest-signal moment**

When you are working with an AI assistant and a decision crystallises,
that is when the reasoning is sharpest.
The Claude Code skill lets you capture it without switching context.
Type `capture`. The ledger is updated. You continue working.

> The Claude skill captures decisions made *with* AI.
> The ledger records decisions made *by* humans.

**When to capture**

Capture when a decision is made, not when you remember to.
The sensors are designed to trigger at the natural confirmation moment —
a Slack thread resolved, a Figma comment approved, a commit pushed.

For decisions made in conversation or in a document, use the CLI:

```bash
irp capture "Decision: [what was decided]" --why "[why it was decided]"
```

**What makes a good entry**

A good decision entry answers two questions:

1. What did we choose?
2. What did we reject, and why?

The second question is the valuable one.
Anyone can find what you chose. The rejected alternatives are what gets lost.

**Review before starting something new**

Before beginning a new phase, sprint, or project:

```bash
irp why
```

This shows the last 10 confirmed decisions.
Takes 30 seconds. Prevents relitigating what was already decided.

---

## Common mistakes

**Capturing too much**
IRP is not a log. Do not capture every small choice.
Capture decisions that would be hard to explain six months later.
That is the right threshold.

**Skipping the why**
The decision text without the reasoning is just a log entry.
The reasoning is what makes it worth keeping.
If you cannot write one sentence of why, the decision may not be ready to capture yet.

**Expecting search**
The ledger is intentionally small.
It is meant to be read, not queried.
`irp why` shows your last 10 confirmed decisions in order.
Read it the way you would read a ship's log.

**Waiting until the end of a project**
The value of IRP compounds over time.
A decision captured the day it was made is worth ten decisions captured a month later.
Start capturing from day one, even if the entries are simple.

---

## Quick reference

| Task | Command |
|---|---|
| Capture inside a Claude session | `capture` (Claude Code skill) |
| Set project context | `irp inherit "Project: [name and context]"` |
| Capture a decision | `irp capture "Decision: [what]" --why "[why]"` |
| Review recent decisions | `irp why` |
| Review specific decision | `irp why --id IRP-2026-04-08-001` |
| Capture from stdin | `irp capture --stdin` |
| JSON output | Add `--json` to any command |

---

## Add the PR bot to your project

Copy `.github/workflows/irp-pr-check.yml` to your repo:

```yaml
name: IRP PR Check
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  pull-requests: write
jobs:
  irp-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - uses: S0tman/irp-capture/.github/actions/irp-check@main
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

The bot posts a warning comment when a PR title or description overlaps with an active decision in `.irp/`. Silently passes if no `.irp/` is found. Warn-only — never blocks a merge.

---

## Why not a memory tool?

Memory stores what happened.

Decisions explain why it mattered.

IRP focuses on the second.

---

## Design principles

- **Local-first.** The ledger lives on your machine. No cloud required.
- **Append-only.** Entries are never edited or deleted. The record is immutable.
- **Human-confirmed.** No entry exists without a human confirming it.
- **Model-agnostic.** Works with Claude, GPT, Gemini, or no AI at all.
- **Tool-agnostic.** Any sensor can write to the same substrate.
- **Plain text.** The ledger is a `.jsonl` file. Open it in any editor.

---

## Status

| Component | Status |
|---|---|
| Core CLI | Available |
| Claude Code skill | Available |
| Slack sensor | Available |
| Discord sensor | Live — v0 |
| Figma plugin | Live — v0 |
| Git hook | Live — warn mode (enforce coming) |
| PR bot | Live — warn-only |
| pip package | Live — v0.3.0 |
| REST API | Live — v0 (`pip install irp-capture[api]`) |
| Obsidian integration | Live — v0 (set `IRP_OBSIDIAN_VAULT`) |
| MemPalace integration | Live — v0 (`pip install irp-capture[mempalace]`) |

---

## Contributing

IRP is used to capture real decisions across CLI and Slack workflows.
It is early. If you are building in this space and the substrate
model resonates, open an issue or reach out.

The sensor pattern is open. If you want to write a sensor
for a tool your team uses, the format is simple and documented.

---

*IRP does not tell you what to decide.
It makes sure you remember why you did.*
