# Changelog

All notable changes to irp-capture are documented here.

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
