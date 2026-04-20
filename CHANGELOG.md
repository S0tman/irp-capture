# Changelog

All notable changes to irp-capture are documented here.

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
