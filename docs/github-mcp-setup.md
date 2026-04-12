# GitHub MCP Setup — Quick Guide

GitHub MCP gives Claude direct access to your irp-capture repository: issues, pull requests, discussions, and CI status.

## 5-Minute Setup

### Step 1: Install GitHub MCP

```bash
cd /tmp
git clone https://github.com/github/github-mcp-server.git
cd github-mcp-server
npm install && npm run build
```

This creates `build/index.js` — the executable MCP server.

### Step 2: Create GitHub Token

1. Go to: https://github.com/settings/tokens/new
2. **Token name:** "Claude Code IRP"
3. **Expiration:** 90 days (or longer if you prefer)
4. **Scopes** (minimum):
   - `repo` (full control of private repositories)
   - `read:org` (read organization data)
   - `read:user` (read user profile)
5. Click "Generate token" → copy to clipboard

### Step 3: Add to Claude Code Settings

Edit or create `~/.claude/settings.json`:

```json
{
  "autoUpdatesChannel": "latest",
  "env": {
    "GITHUB_TOKEN": "ghp_xxxxx..." (paste your token here)
  },
  "mcp": [
    {
      "name": "github",
      "command": "node",
      "args": ["/tmp/github-mcp-server/build/index.js"]
    }
  ]
}
```

### Step 4: Restart Claude Code

```bash
claude
```

When Claude Code starts, it loads the GitHub MCP. On first use, it may prompt for tool permissions (Bash, etc.).

### Step 5: Test

In Claude Code, ask:

```
List open issues in irp-capture repo
```

Or invoke a scheduled task:

```
/daily-github-issues-scan
```

If it works, you'll see GitHub API calls happening.

---

## What You Get

With GitHub MCP connected:

| Capability | Example |
|---|---|
| List issues | "Show me all open issues in irp-capture" |
| Create issues | "Open an issue titled 'Add webhook signature validation' in irp-capture" |
| Search issues | "Find issues mentioning 'Figma' or 'decision'" |
| Read PR diffs | "Summarize the changes in PR #42" |
| Comment on issues | "Reply to issue #15 with feedback" |
| Check CI status | "Is the main branch green?" |
| Manage labels | "Label all decision-related issues as 'kind:decision'" |

---

## Troubleshooting

**Token invalid?**
- Check expiration at https://github.com/settings/tokens
- Regenerate if needed and update `~/.claude/settings.json`

**MCP server not starting?**
- Verify Node.js is installed: `node --version`
- Check logs: `claude --debug` or look in stderr
- Ensure `/tmp/github-mcp-server/build/index.js` exists

**Permission denied on GitHub calls?**
- Verify token scopes include `repo`, `read:org`, `read:user`
- Check token hasn't expired

**Want to move the server elsewhere?**
- Update `args` in settings.json to point to wherever you cloned it
- Example: `"args": ["/Users/jolopes/src/github-mcp-server/build/index.js"]`

---

## Optional: Add to Settings Permanently

Once working, save the GitHub token in your environment for future sessions:

```bash
# Add to ~/.zshrc or ~/.bashrc
export GITHUB_TOKEN="ghp_xxxxx..."
```

Then in `~/.claude/settings.json`, you can omit the token from `env` and it will read from the shell environment.

---

## Next: Using GitHub MCP with `/daily-github-issues-scan`

Once connected, the scheduled task `/daily-github-issues-scan` will:

1. Search GitHub for issues mentioning IRP-related keywords
2. Cross-reference with your active decisions (`irp why --json`)
3. Report new signals and feature requests
4. Flag issues that map to existing decisions

This closes the loop: user feedback → signal detection → decision context.

---

**Status:** Not yet connected. Follow the steps above to activate.
