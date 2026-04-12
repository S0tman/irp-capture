# Claude Code Level-Up: Complete Setup (Apr 12, 2026)

## ✅ Completed

### Level 4 — Foundations
- **CLAUDE.md** (`~/.claude/CLAUDE.md`) — Global instructions with token cost rules, collaboration patterns, IRP paths
- **Pre-commit hook** (`~/.claude/settings.json`) — `irp check` runs before every git commit, warns on conflicts without blocking

### Level 5 — Skills & Orchestration

Three new skills in `~/.claude/skills/`:

#### 1. `/irp-capture-workflow`
**Purpose:** End-to-end decision recording with conflict detection and audit trail

**Usage:**
```bash
/irp-capture-workflow Poll every 30 seconds for resolved Figma comments
```

**Flow:**
1. Reads active decisions via `irp why --json`
2. Captures new decision via `irp capture --stdin`
3. Checks conflicts via `irp check`
4. Stages `.irp/` files
5. Commits with decision ID as trailer
6. Reports decision ID for future reference

**Key difference from manual capture:** Automated conflict detection + git integration. Pre-commit hook adds second layer of safety.

---

#### 2. `/competitive-research`
**Purpose:** Scan the decision-portability + anti-SaaS AI landscape

**Usage:**
```bash
/competitive-research
```

**Output:** Markdown comparison table ready for iCloud Competitive Analysis

**Targets:** Glean, Copilot, Notion AI, Cursor, Replit, MemPalace, local LLM ecosystems

**Signal focus:** Daria's ICP (agentic-native builders, local models, data leakage concerns)

Runs in **Explore subagent** (read-only, optimized for research)

---

#### 3. `/irp-ledger-summary`
**Purpose:** Analyze decision ledger, surface patterns and conflicts

**Usage:**
```bash
/irp-ledger-summary
```

**Output:**
- Activity summary (decisions by date, source, topic)
- Conflict detection
- Pattern extraction (frequency, scope shifts, unresolved questions)
- Markdown report + optional Slack post

Runs in **Explore subagent** (read-only)

---

### Level 5.5 — Scheduled Background Tasks

Three recurring tasks now running automatically:

| Task | Schedule | Purpose |
|---|---|---|
| `irp-nightly-ledger-summary` | 2:00 AM daily | Generate overnight summary of last 7 days' decisions |
| `weekly-competitive-research` | Monday 9:05 AM | Scan for positioning/feature shifts in competitor space |
| `daily-github-issues-scan` | 8:01 AM daily | Search GitHub for IRP-related feedback + feature signals |

**Note:** Times include small jitter (±90s) to balance server load.

**Next run times:** Check via `/scheduled tasks list` or in the "Scheduled" sidebar.

**Pre-approvals:** First run of each task may require tool permission approval (Bash, GitHub MCP, etc.). Subsequent runs auto-approve stored permissions.

---

## 🔄 Next: GitHub MCP Setup

GitHub MCP is not yet connected. This gives you live access to issues, PRs, and discussions in the irp-capture repo.

### Manual Setup (recommended)

1. **Install GitHub MCP** from official source:
   ```bash
   git clone https://github.com/github/github-mcp-server.git
   cd github-mcp-server
   npm install && npm run build
   ```

2. **Add to Claude Code settings** (`~/.claude/settings.json`):
   ```json
   {
     "enabledMcpjsonServers": ["github"]
   }
   ```

3. **Authenticate** — On first use, Claude Code will prompt for a GitHub token. Generate one at:
   ```
   https://github.com/settings/tokens
   ```
   Scopes needed: `repo`, `read:org`, `read:user`

4. **Test:**
   ```
   /daily-github-issues-scan
   ```
   Or ask Claude: "List open issues in irp-capture"

### Connector-based Setup (alternative)

If you have Anthropic's MCP registry connectors available, you can connect GitHub via the UI (faster but may require authentication each session).

---

## 📊 Current Architecture

```
┌─────────────────────────────────────────────────┐
│  Claude Code Session                            │
├─────────────────────────────────────────────────┤
│  CLAUDE.md (global instructions)                │
│  ~/.claude/settings.json (pre-commit hook)      │
│  ~/.claude/skills/ (3 new skills)               │
│  Scheduled tasks (3 background jobs)            │
│  [GitHub MCP] (to be connected)                 │
└─────────────────────────────────────────────────┘
                      ↓
         ┌────────────────────────┐
         │  IRP Dispatcher        │
         │  irp/core/irp.py       │
         │  - capture             │
         │  - check               │
         │  - why                 │
         └────────────────────────┘
                      ↓
         ┌────────────────────────┐
         │  .irp/ (local ledger)  │
         │  - ledger.jsonl        │
         │  - current.json        │
         └────────────────────────┘
                      ↓
   ┌─────────────────┬──────────────┐
   │                 │              │
   ↓                 ↓              ↓
Figma Plugin    collab.py    GitHub Issues
(auto-capture) (GPT bridge)  (feedback loop)
```

---

## 🎯 How to Use Each Layer

### During Development

**Capturing a design decision from Figma:**
```bash
/irp-capture-workflow Auto-populate comment text from resolved Figma comments
```
→ Decision ID: `IRP-2026-04-12-001` (reference in GitHub issues, Figma threads, etc.)

**Checking if your commit might conflict:**
Pre-commit hook fires automatically. If conflict, you see:
```
⚠️  IRP conflict: commit overlaps with active decision IRP-2026-04-11-001 (...)
Run: irp why --id IRP-2026-04-11-001
```
→ You can proceed anyway (hook doesn't block), but now you're aware.

**Collaborating with GPT:**
```bash
python3 /Users/jolopes/irp-capture/tools/collab.py -p /Users/jolopes/.claude/skills/irp-capture-v1-5 "Your question about IRP design"
```
→ GPT sees your active decisions via `.irp/current.json`, responds with context.

### Background (Automated)

- **2:00 AM:** Nightly ledger summary lands in your scheduled tasks folder
- **8:00 AM:** GitHub issues with "IRP", "decision", "portability" are scanned; new signals reported
- **Monday 9:00 AM:** Competitor landscape checked; positioning shifts flagged

Read reports in:
- `Scheduled` sidebar (task results)
- `/Users/jolopes/irp-capture/docs/ledger-summary-YYYY-MM-DD.md` (nightly)

---

## 🧠 Mental Model: Why This Stack

| Layer | What | Why |
|---|---|---|
| **CLAUDE.md + hook** | Prevention | Catch conflicting decisions before they're committed |
| **Skills** | Orchestration | Automate multi-step workflows (capture → check → commit) |
| **Scheduled tasks** | Intelligence | Discover signals (GitHub feedback, competitor moves) while you sleep |
| **GitHub MCP** | Closed loop | Link GitHub issues → IRP decisions → back to issues |

Together: **Decisions are captured, checked, persisted, researched, and connected to the feedback loop — all with minimal manual effort.**

---

## ⚠️ Known Limitations

- **Scheduled tasks are session-scoped:** They live in Claude Code and vanish if you close the session. For durable background work that survives restarts, upgrade to Cloud or Desktop scheduled tasks via `/schedule` CLI (not covered here).
- **GitHub MCP requires manual setup:** Not yet auto-connected. Follow the setup instructions above.
- **Skills run in subagents:** Explore agent (read-only). If you need to modify files during `/competitive-research` or `/irp-ledger-summary`, those runs won't write directly — you'll review and copy findings manually.

---

## 📋 Verification Checklist

- [x] CLAUDE.md exists at `~/.claude/CLAUDE.md`
- [x] Pre-commit hook in `~/.claude/settings.json`
- [x] Three skills in `~/.claude/skills/`:
  - [x] irp-capture-workflow
  - [x] competitive-research
  - [x] irp-ledger-summary
- [x] Three scheduled tasks created (check "Scheduled" sidebar)
- [ ] GitHub MCP installed and authenticated (manual step)
- [ ] First test run of each skill (to pre-approve tools)

---

## Next Level (Level 6+)

Once you're comfortable with this setup:

1. **Custom subagents** — Create a "Decision Research" agent that combines `/competitive-research` + GitHub MCP into a single workflow
2. **Hooks for skills** — Auto-run `/irp-capture-workflow` when a Figma plugin detects a resolved comment
3. **Desktop scheduled tasks** — Durable background tasks that run even when Claude Code isn't open (via `/schedule` CLI)
4. **Plugin distribution** — Package the Figma plugin + IRP skill as a plugin for reuse/sharing

---

**Status:** Level 5 complete. All systems operational.

**Time to next sync:** Nightly ledger arrives at 2:00 AM. Competitive research runs Monday 9:00 AM. GitHub scan runs daily at 8:00 AM.

**Questions?** Refer to individual skill docs: `/irp-capture-workflow`, `/competitive-research`, `/irp-ledger-summary`
