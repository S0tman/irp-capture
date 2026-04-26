# Using IRP with Cursor

IRP works with Cursor in two ways: as an **MCP server** (recommended — full tool integration) or as a **`.cursorrules` snippet** (zero-install fallback).

---

## Option A — MCP Server (recommended)

The MCP server exposes IRP as four tools Cursor's agent can call directly:
`irp_capture`, `irp_why`, `irp_inherit`, `irp_check`.

### 1. Install

```bash
pip install 'irp-capture[mcp]'
```

Verify:

```bash
irp-mcp --help
```

### 2. Configure Cursor

Open **Cursor → Settings → MCP** (or edit `~/.cursor/mcp.json` directly):

```json
{
  "mcpServers": {
    "irp": {
      "command": "irp-mcp"
    }
  }
}
```

If your project root is not the directory you open in Cursor, set it explicitly:

```json
{
  "mcpServers": {
    "irp": {
      "command": "irp-mcp",
      "env": {
        "IRP_PROJECT_ROOT": "/absolute/path/to/your/project"
      }
    }
  }
}
```

Restart Cursor. You should see **irp** appear in the MCP tools list.

### 3. Use it

In Cursor's agent chat, you can now say things like:

> "Capture the decision to use Postgres — we rejected Redis because the query patterns require joins."

> "What decisions have we made about the authentication layer?"

> "Check whether switching to MongoDB conflicts with any active decisions."

The agent calls the corresponding IRP tool. The ledger is written to `.irp/ledger.jsonl` in your project root.

### Available tools

| Tool | What it does |
|---|---|
| `irp_capture` | Record a confirmed decision — `what`, `why`, optional `confidence` + `tags` |
| `irp_why` | Show recent decisions, or look up a specific one by ID |
| `irp_inherit` | Return the current project context (active decisions summary) |
| `irp_check` | Check a proposal against active decisions for conflicts |

---

## Option B — `.cursorrules` snippet (zero-install)

If you want Cursor to follow IRP capture discipline without installing the MCP server, add this to your project's `.cursorrules` file:

```
## Decision capture (IRP)

When a significant decision is made during this session — an architectural choice,
a rejected alternative, a scope call, or a constraint accepted — capture it before
moving on.

Format:
  Decision: [what was decided]
  Why: [why it was decided, and what was rejected]

Run: irp capture "Decision: [what]" --why "[why]"

If irp is not installed: write the decision and reasoning as a comment block
at the top of the relevant file, or append to DECISIONS.md in the project root.

Do not wait until the end of the session. Capture at the moment of decision.
```

This gives Cursor's agent the habit without any server setup. The trade-off: it relies on the agent following the instructions rather than having structured tool access.

---

## Verifying your setup

```bash
irp doctor
```

This checks your Python version, irp-capture install, ledger health, and which integrations are active.

---

## Next steps

- **Claude Code**: IRP also works as a Claude Code skill — see `SKILL.md`
- **Obsidian sync**: Set `IRP_OBSIDIAN_VAULT=/path/to/vault` to write every decision as a markdown file into your vault
- **MemPalace**: `pip install 'irp-capture[mempalace]'` to write decisions into your agent's memory collections
- **REST API**: `pip install 'irp-capture[api]'` for custom integrations via HTTP

Full documentation: [irp-book.vercel.app](https://irp-book.vercel.app)
