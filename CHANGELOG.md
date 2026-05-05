# Changelog

All notable changes to irp-capture are documented here.

---

## [0.6.4] — 2026-05-05

### Added — `irp export evidence` · EU AI Act evidence package
### Added — `irp guard` · Pre-commit conflict detection

See [Unreleased] entries above for full detail.

---

## [0.6.3] — 2026-05-01

### Added — `irp stats` · Local activation dashboard

- New `irp stats` command — surfaces capture cadence, weekly ramp (ASCII bar chart), sensors used, top tags, days active. All local. No telemetry.
- `irp stats --demo` shows stats for the built-in 18-decision sample dataset without touching the ledger
- Empty ledger nudges to `irp stats --demo`

### Added — CLI milestone moments

- Inline messages in `irp capture` output at key activation thresholds: 1st, 10th, 25th, 50th decision
- First capture from a named sensor (Slack, MCP, Figma, VS Code, Discord, Git, API) triggers: *"IRP is now embedded in your X workflow"*

### Added — `irp export graph --demo`

- `--demo` flag generates `GRAPH-demo.html` from a built-in 18-decision / 22-edge design system dataset — no real ledger required
- Ledger is never read or modified in demo mode
- Empty-ledger nudge: if `irp export graph` is run with no decisions captured, the CLI prints a helpful message directing users to `--demo`
- Design principle locked (IRP-2026-05-01-001, corrected by IRP-2026-05-01-002): export commands and rich-output commands must ship with `--demo` + sample data; simple read commands (`why`, `check`, `inherit`) are exempt

---

## [0.6.2] — 2026-04-29

### Changed — `irp export graph` · Decision graph UX overhaul

A full interaction redesign of the `GRAPH.html` output and the `demo-graph.html` used in the IRP Book ch6.

#### Interaction model — frozen tooltip

Previous model (hover tooltip) broke down because the tooltip disappeared the moment the cursor left the node, making it impossible to click reference links inside it. Replaced with a **frozen tooltip** pattern:

- **Click a node** → tooltip freezes in place at cursor position (`position:fixed`, `pointer-events:auto`), camera flies to node
- **Click a reference pill** inside the tooltip → camera flies to the target node, tooltip dissolves, target node becomes selected
- **Click same node again or background** → dismisses tooltip and clears selection
- Eliminates the hover-then-click timing problem entirely

#### Node selection highlight

Clicked (locked) node turns `#D3D3D3` (light grey). Re-selecting via a reference link transfers the highlight to the destination node. Deselection restores confidence-based colour coding (green / amber / red).

#### Persistent node ID labels

Every node displays a persistent `IRP-NNN` label (date stripped from full id) via a CSS overlay projected with `Graph.graph2ScreenCoords()`. No Three.js dependency — zero library conflicts. Labels follow nodes through rotation and zoom.

#### Show / Hide IDs toggle

Footer right-hand side: **Hide IDs / Show IDs** link. Lets users switch between the annotated traceability view and a clean globe view, without reloading.

#### Panel removal

The right-side detail panel was removed after the frozen tooltip made it redundant. Canvas now uses full width.

#### Hebbian Theory callout — IRP Book ch6

Added a blockquote to `ch6-extensibility.md`:

> **Neurons that fire together, wire together.** — Donald Hebb, 1949

Frames IRP provenance edges through the lens of associative memory: decisions that cross-reference each other in their `why` fields wire together, forming a lineage that survives tool changes and team changes.

---

## [0.6.1] — 2026-04-29

### Added — `irp export graph` · Interactive 3D decision graph

New command: render the full decision ledger as a self-contained interactive 3D HTML file.

```bash
irp export graph                    # writes GRAPH.html in project root
irp export graph --output viz.html  # custom path
irp export graph --force            # overwrite existing
```

#### What it generates

A single `GRAPH.html` — no server, no install, open in any browser. Built on [3d-force-graph](https://github.com/vasturiano/3d-force-graph) (Three.js/WebGL). Decisions rendered as a 3D force globe with:

- **Globe layout** — nodes distribute into a sphere under 3D force simulation; clusters form naturally as cross-referenced decisions grow
- **Colour coding** — green (high confidence), amber (medium), red (low), grey (unknown)
- **Node size** — scales with confidence level
- **Animated particles** — travel along provenance edges (IRP id cross-references found in `why` fields)
- **Directional arrows** — show which decision references which
- **Click to inspect** — detail panel shows full decision: id, what, why, tags, confidence, source, and clickable cross-reference links
- **Camera fly** — clicking a node animates the camera toward it
- **Inertia** — dragging the globe has smooth deceleration (OrbitControls damping)
- **Auto-rotation** — slow clockwise idle rotation; pauses on interaction, resumes after 2 s

#### Design invariants

- No new schema. Reads `.irp/ledger.jsonl` only.
- No LLM calls. No inference. Deterministic mapping only.
- Edges derived from IRP id cross-references in `why` fields (regex only — no inference).
- Single self-contained HTML via CDN — no build step, no dependencies to install.
- `GRAPH.html` is gitignored by default (always regenerable; treat as a build artefact).

#### Obsidian graph view (same session)

`irp/integrations/obsidian.py` now converts bare IRP ids in `why` fields to `[[wikilinks]]` on write. Obsidian's built-in graph view draws provenance edges automatically — no extra configuration.

---

## [0.6.0] — 2026-04-29

### Added — `irp export context`

New command: export decision lineage as portable working-context files.

#### `irp export context --target agents.md`
- Derives single-line agent rules under **Working constraints** — each rule cites its source IRP id
- Lists every decision with full provenance under **Relevant decisions**
- Conservative rule derivation: decisions that are multi-sentence, long-form, or contain em-dash summaries are listed in full rather than forced into a misleading rule
- Fills the empty layer beneath AGENTS.md / CLAUDE.md / DESIGN.md: those files tell agents *what to do*, IRP tells them *why those rules exist*

#### `irp export context --target decisions.md`
- Human-readable decision log, newest-first
- One section per decision: IRP id · timestamp heading, bold `what`, italic `why`
- Confidence badges (🟢 high / 🟡 medium / 🔴 low), tag list, source label (slack channel, demo scenario, stdin, etc.)
- Readable by any collaborator who doesn't run IRP

#### Flags (both targets)
- `--output PATH` — override default output path (AGENTS.md or DECISIONS.md in project root)
- `--force` — overwrite existing output file
- `--writable` — leave file `-rw-r--r--`; default is `chmod 444` read-only
- `--json` — machine-readable output

#### Design invariants
- No new schema. Reads `.irp/ledger.jsonl` only.
- No LLM calls. No inference. Deterministic text transformation only.
- Provenance on every output line. Always regenerable.
- Read-only by default — editors surface "file is locked" before any save.

---

## [0.5.0] — 2026-04-26

### Added — traction stack: doctor, VS Code extension, Cursor guide, Discussion template

#### `irp doctor`
- New CLI command: checks Python version, irp-capture install, `.irp/` directory, `ledger.jsonl` integrity, `current.json`, Claude Code skill presence, and optional integrations (Obsidian, MemPalace, MCP, REST API)
- Core vs optional distinction: integration ✗ is informational, not a failure
- Supports `--json` flag
- Registered in `build_parser()` and dispatch table

#### VS Code extension (`irp/vscode_extension/`)
- Three commands: `IRP: Capture Decision` (⌘⇧I), `IRP: Show Recent Decisions`, `IRP: Doctor`
- Two-step input box (what → why), pipes JSON to `irp capture --stdin` — no shell escaping
- Status bar item (`⬡ IRP · N decisions`) — live count, click to open why panel
- Dedicated IRP output channel for `why` and `doctor` output
- Configurable `irp.executable` and `irp.projectRoot` settings
- Packaged as `irp-capture-0.5.0.vsix` (8.12 KB)
- Source field set to `vscode` for ledger provenance

#### Cursor guide (`CURSOR-GUIDE.md`)
- MCP server setup: `pip install 'irp-capture[mcp]'`, `mcp.json` config, `IRP_PROJECT_ROOT` env override
- Available tools table: `irp_capture`, `irp_why`, `irp_inherit`, `irp_check`
- Zero-install `.cursorrules` fallback for teams not running the MCP server
- README sensors table links to guide

#### GitHub traction
- PyPI `installs/month` + version badges added to README top (shields.io, signal-blue)
- GitHub Discussions badge added
- Discussion link + CTA added to Contributing section
- "Show your install" structured Discussion template (`.github/DISCUSSION_TEMPLATE/show-your-install.yml`)
- VS Code sensor row updated in README status table

#### PyPI
- v0.5.0 published to https://pypi.org/project/irp-capture/0.5.0/

---

## [book-2026-04-20b] — 2026-04-20

### Book site — ch10 GPAI provider blind spot section

#### Added
- **ch10: "The GPAI Blind Spot"** — new major section addressing the misconception that AI model service providers (sovereign cloud, API-first, open-source hosters) are outside the Act's scope
  - Explains GPAI model definition (Articles 51–55), obligations already live since Aug 2, 2025
  - Covers the four GPAI provider obligations: technical documentation, copyright policy, training data summary, EU database registration
  - Addresses the open-source exemption precisely: partial, narrow, collapses on fine-tuning or service wrapping
  - Explains the systemic risk threshold (10²⁵ FLOPs) and what additional obligations it triggers
  - Mermaid decision tree: "Are you a GPAI provider?"
  - Essentials updated with #6 on model service providers
- Synthesised from Claude + GPT collaborative review (GPT-4o via collab.py)

---

## [book-2026-04-20] — 2026-04-20

### Book site (irp-book.vercel.app) — Part 4 launch + editorial pass

#### Added
- **Part 4: A Plain-Language Guide to EU AI Act Compliance** — 9 new chapters (ch9–ch17) added to the IRP Book
  - `ch9-why-this-law-exists.md` — origin of the Act (Dutch SyRI + childcare cases), the governance gap, GDPR vs AI Act distinction, phase-in timeline
  - `ch10-who-it-applies-to.md` — Provider / Deployer / Importer role taxonomy, the modification threshold that creates new Providers
  - `ch11-the-risk-ladder.md` — four tiers, Annex III high-risk categories, self-assessment decision tree
  - `ch12-article-12-logging.md` — why system logs ≠ decision records, compliant audit trail anatomy, tamper-evidence, retention
  - `ch13-article-13-ifu.md` — Instructions for Use as a legal artefact, Provider/Deployer responsibility split, IFU as living document
  - `ch14-article-14-oversight.md` — genuine vs nominal oversight, override rate diagnostic, high-volume deployment patterns
  - `ch15-article-27-fria.md` — who must conduct a FRIA, minimum content, DPIA vs FRIA distinction, FRIA as early warning
  - `ch16-article-72-monitoring.md` — post-market monitoring as permanent function, distribution shifts, monitoring without consequence
  - `ch17-the-path-forward.md` — compliance stack by org size, three failure modes, IRP Compliance Assessment as starting point
- `book.config.ts` — Part 4 added with 9 chapter entries (numbers 9–17)

#### Strengthened (editorial pass — GPT review integration)
- **Decision accountability spine** added across all Part 4 chapters: "The AI Act is not about AI. It is about decisions."
- **IRP positioning upgraded**: all "How IRP Compliance helps" sections rewritten from "helpful tool" to "structurally required layer" — showing why existing systems fail and why manual processes break at scale
- **Impossibility gap** introduced in ch12, ch14, ch16: concrete statements that most organisations currently cannot produce the required evidence
- **Reconstruction → capture framing** added to ch12: Article 12 assumes logs are enough; IRP shifts from reconstruction after the fact to capture at decision time
- **"Integrity-verified" clarified** → "append-only record, each entry linked to previous, so gaps and modifications are detectable"
- **"Extends, not replaces" Article 12** framing added — IRP extends Article 12 from system logs to decision-level traceability
- **"The ledger is the audit trail. The audit trail is the compliance evidence."** restored as the category-defining line in ch12
- **Assessment links** made live (clickable markdown) in every chapter that references the IRP Compliance Assessment (ch12–ch17)
- Soft phrases removed: "in practice", "worth making explicit", "design tension", "A Note on…"
- Language tightened ~10% across all edited chapters

---

## [0.4.0] — 2026-04-17

### Milestone
- **MCP server** — IRP is now an MCP (Model Context Protocol) tool server. Any MCP-compatible client (Claude Code, Cursor, Windsurf, Understanding Graph, custom agents) can capture decisions, query reasoning, and check for conflicts via protocol.

### Added
- `irp/mcp/server.py` — MCP server exposing four tools: `irp_capture`, `irp_why`, `irp_inherit`, `irp_check`
- `irp-mcp` console entry point — run the server via `irp-mcp` (stdio transport)
- `pyproject.toml` — new optional extra: `[mcp]` (installs `mcp>=1.0`)
- Configurable project root via `IRP_PROJECT_ROOT` env var

### Configuration
```json
{
  "mcpServers": {
    "irp": {
      "command": "irp-mcp"
    }
  }
}
```

---

## [0.3.0] — 2026-04-15

### Milestone
- **Sovereign stack integrations** — IRP now writes decisions natively to Obsidian vaults and MemPalace palaces. No bundling, no licensing issues. IRP writes to the filesystem and ChromaDB directly. The decision layer completes the Karpathy stack: Obsidian (knowledge) + MemPalace (agent memory) + IRP (decisions).

### Added
- `irp/integrations/obsidian.py` — writes each captured decision as a `.md` file to your Obsidian vault (`{vault}/decisions/{id}.md`). No extra dependencies. Set `IRP_OBSIDIAN_VAULT=/path/to/vault` in env.
- `irp/integrations/mempalace.py` — writes each captured decision into the MemPalace `mempalace_drawers` ChromaDB collection. Agents can semantically query past IRP decisions. Set `IRP_MEMPALACE_PATH` (defaults to `~/.mempalace/palace`). Optional dependency: `pip install 'irp-capture[mempalace]'`.
- `irp/integrations/dispatch.py` — post-capture dispatcher. Fires enabled integrations silently — integration errors never break the capture flow.
- `irp capture` output now includes integration confirmation lines: `✓ obsidian: /vault/decisions/IRP-xxx.md`
- `pyproject.toml` — new optional extras: `[mempalace]` and `[sovereign]` (both install `chromadb>=0.5`)

### Book site (irp-book.vercel.app)
- **Ch6 — Sovereign Stack Integrations section** — new material covering integration dispatch, Obsidian integration, MemPalace integration, and two Mermaid architecture diagrams (REST API endpoints flow, three-destinations-one-capture architecture)
- **Ch7 — Epilogue updated** — pattern-to-problem matching expanded with sovereign stack reference
- **Mermaid diagrams now render across all chapters** — removed the broken `remark-mermaid-raw` plugin (Astro 5's content layer stripped its raw HTML output). Shiki handles `mermaid` as a native language; client-side `mermaid.js` replaces code blocks with interactive SVG diagrams.
- **Changelog page** — `CHANGELOG.md` from the repo root is now accessible at `/changelog/` on the book site
- **Sidebar + header** — added Changelog navigation link to both desktop and mobile nav

---

## [0.2.0] — 2026-04-14

### Milestone
- **Discord sensor v0 deployed in production by Opensverige community** — first zero-friction external deployment using only GitHub documentation. Community is actively capturing governance decisions.

### Added
- `irp/discord_sensor/` — Discord bot sensor with right-click context menu ("Capture decision"), structured modal (what / why / tags), and direct write to `.irp/ledger.jsonl`
- Sensor-local `.env` configuration (per sensor pattern)
- `DISCORD-SENSOR-SETUP.md` — complete step-by-step setup guide for external communities

### Fixed
- Discord sensor thread attribute error (`'Message' object has no attribute 'thread'`) — use `hasattr()` safety check
- MacPorts Python SSL certificate verification — `SSL_CERT_FILE` workaround via certifi documented in `.env.example`

### Book site (irp-book.vercel.app)
- Dark mode fully working — Tailwind v4 requires `@variant dark (&:where(.dark, .dark *))` in CSS; `darkMode: 'class'` in `tailwind.config.js` is Tailwind v3 and ignored in v4
- Header background corrected in dark mode
- Bootstrapping chapter updated: `irp bootstrap` command is live, not future
- Slack sensor marked as live (was incorrectly marked "future")
- Citation dates updated (Amol Avasare interview — April 2026)

---

## [0.1.1] — 2026-04-12

### Added
- `collab.py` — multi-engine collaboration tool (Claude + GPT with IRP context injection)
- Discord sensor v0 committed (`irp/discord_sensor/`)
- IRP book website deployed to `irp-book.vercel.app` (Astro 5.7 + Tailwind v4 + D3.js architecture diagram)

### Fixed
- `.irp/current.json` markdown code fence wrapping removed
- `.irp/ledger.jsonl` empty file initialised correctly

---

## [0.1.0] — 2026-04-10

### Added
- REST API v0 (`pip install irp-capture[api]`) — `/inherit`, `/why`, `/check` endpoints
- GitHub PR Bot — warn-only conflict detection on PR open
- Figma plugin v0 with auto-populate from resolved comments

---

## [0.0.1] — 2026-03-22

### Added
- IRP core v1.5 — single dispatcher (`irp/core/irp.py`)
- Append-only ledger (`.irp/ledger.jsonl`)
- Derived state (`.irp/current.json`)
- Commands: `capture`, `inherit`, `why`, `check`, `bootstrap`
- Slack Capture v0 — human-confirmed decisions from threads
- Git commit-msg hook — warn-only conflict surfacing
- Claude SKILL (`irp-capture-v1-5`)
