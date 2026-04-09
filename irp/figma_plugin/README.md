# IRP Figma Plugin

Captures design decisions from Figma directly into `.irp/`.

## How it works

```
Figma sidebar (ui.html)
       │
       │ fetch() to localhost:3002
       ▼
Bridge server (bridge/server.py)
       │
       │ writes directly to .irp/ via IRP store API
       ▼
.irp/ledger.jsonl
```

The plugin sidebar has two fields: **Decision** (what was decided) and **Why** (why it won over alternatives). The current Figma page is captured automatically as context.

---

## Setup

### 1. Load the plugin in Figma

Figma → Plugins → Development → **Import plugin from manifest**

Point it at: `irp/figma_plugin/manifest.json`

### 2. Start the bridge

**Critical:** you must pass `--project-root` pointing at the project whose `.irp/` you want to write to. If you omit it, the bridge defaults to wherever it was launched from — which is almost certainly not what you want.

```bash
python3 irp/figma_plugin/bridge/server.py --project-root /path/to/your/project
```

**Example — writing to the irp-capture-v1-5 Claude skill:**
```bash
python3 /Users/jolopes/irp-capture/irp/figma_plugin/bridge/server.py \
  --project-root /Users/jolopes/.claude/skills/irp-capture-v1-5
```

**Example — writing to a local git project:**
```bash
python3 /path/to/irp-capture/irp/figma_plugin/bridge/server.py \
  --project-root /path/to/your/git/project
```

The bridge logs the resolved project root on startup:
```
[bridge] Project root: /Users/yourname/.claude/skills/irp-capture-v1-5
[bridge] IRP Figma bridge running on http://localhost:3002
```

**Always verify this line before using the plugin.**

### 3. Run the plugin in Figma

Plugins → Development → IRP Capture → Open

Fill in Decision + Why, click **Capture**. You'll see "Decision captured to IRP ✓" as a Figma toast.

---

## Verifying captures

After capturing, check the ledger from your project root:

```bash
cd /path/to/your/project
irp why
```

Or inspect directly:

```bash
cat /path/to/your/project/.irp/ledger.jsonl | tail -5
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Bridge not reachable" in plugin | Bridge not running | Start `bridge/server.py` |
| Capture succeeds but `irp check` can't see it | Wrong `--project-root` | Restart bridge with correct `--project-root` |
| Bridge starts but writes to wrong `.irp/` | `--project-root` omitted, defaulted to cwd | Always pass `--project-root` explicitly |
| Manifest error on import | Figma version mismatch | Check `manifest.json` networkAccess format |

---

## Architecture note

The bridge uses `Path(PROJECT_ROOT)` — not `Path.cwd()` — as the project root. This is intentional. The bridge can be launched from any directory as long as `--project-root` points at the correct project. This makes it safe to run as a background process or from a launch script.
