# Discord Sensor v0

Captures decisions from Discord into the IRP ledger.

## Features

- **Message Context Menu**: Right-click any Discord message → "Capture decision"
- **Modal Confirmation**: Fill in what/why/tags with a clean modal
- **Ephemeral Confirmation**: Only you see the confirmation, no channel spam
- **Emoji-Friendly**: Preserves emoji and GIF URLs in decisions
- **Playful UX**: Celebratory tone that matches OpenSverige community culture

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application"
3. Go to "Bot" section, click "Add Bot"
4. Copy the bot token
5. Under "OAUTH2" → "URL Generator":
   - Scopes: `bot` + `applications.commands`
   - Permissions: `Send Messages`, `Use Application Commands`, `Send Messages in Threads`
6. Copy the generated URL and open it to invite bot to your server

### 3. Environment Setup

Create a `.env` file in the `discord_sensor/` directory:

```bash
cp .env.example .env
# Then edit .env with your values:
```

**.env contents:**
```
DISCORD_BOT_TOKEN=your_bot_token_here
IRP_PROJECT_ROOT=/Users/jolopes/irp-capture
DISCORD_GUILD_ID=0
```

The bot will automatically load this `.env` file on startup.

### 4. Run the Bot

```bash
python -m irp.discord_sensor.main
```

## Usage

### Via Message Context Menu (v0 Primary)

1. Right-click a Discord message
2. Select "Apps" → "Capture decision"
3. Modal opens:
   - **What was decided?** (required) - The decision in 1-2 sentences
   - **Why was it decided?** (required) - The reasoning (can include GIF URLs)
   - **Tags** (optional) - Comma-separated tags like `architecture, infra`
4. Submit
5. See ephemeral confirmation: "🎯 Locked in! Decision IRP-2026-04-12-001 captured"
6. Decision appears in `.irp/ledger.jsonl`

### Via Slash Command

```
/irp_capture what:"decision text" why:"reasoning text" tags:"optional,tags"
```

## Ledger Format

Each decision writes a JSON entry to `.irp/ledger.jsonl`:

```json
{
  "id": "IRP-2026-04-12-001",
  "timestamp": "2026-04-12T21:30:00Z",
  "type": "decision",
  "what": "Use Discord message context menu for decision capture",
  "why": "Anchors capture to concrete message, matches community interaction patterns",
  "source": "discord",
  "confirmed_by": "baltsar",
  "context": "opensverige Discord",
  "tags": ["discord", "sensor", "v0"],
  "source_ref": {
    "guild_id": "123456",
    "channel_id": "654321",
    "message_id": "987654",
    "message_url": "https://discord.com/channels/123456/654321/987654"
  }
}
```

## Design Decisions (v0)

✅ **Message context menu first** - Anchors capture to specific message
✅ **Modal for what/why/tags** - Clean, structured input without auto-inference
✅ **Ephemeral replies** - Confirmation only visible to user
✅ **Emoji-friendly** - Preserves emoji and GIF URLs as-is
✅ **Playful tone** - "🎯 Locked in!" matches OpenSverige culture
✅ **No auto-detection** - User confirms every decision (quality > volume)

## What's NOT in v0

- ❌ Automatic decision extraction
- ❌ Background scanning of channels
- ❌ Vector search or AI inference
- ❌ Dashboards or analytics
- ❌ Message reactions to captured message (save for v0.1)

## Next Steps (v0.1+)

- Add message reactions to captured messages (optional celebratory emoji)
- Slash command enhancements
- Thread-aware capture
- Batch capture from threads
- Dashboard for viewing ledger

## Development

```bash
# Run tests (when available)
python -m pytest tests/

# Check code
python -m flake8 irp/discord_sensor/
```

## Troubleshooting

**Bot doesn't respond to message context menu:**
- Ensure bot token is correct and has been invited to server
- Check bot permissions: `Send Messages`, `Use Application Commands`
- Restart bot after server invite

**"Failed to write to ledger" error:**
- Check `IRP_PROJECT_ROOT` is set correctly
- Ensure `.irp/` directory exists
- Verify write permissions on ledger file

**Ledger entries not appearing:**
- Check `.irp/ledger.jsonl` file exists
- Verify bot has write access to the file
- Check logs for write errors

## Architecture

```
discord_sensor/
├── __init__.py          # Package init
├── main.py              # Bot entry point
├── commands.py          # Command handlers (message context + slash)
├── modals.py            # Modal interactions (what/why/tags)
├── ledger_writer.py     # Write to .irp/ledger.jsonl
├── config.py            # Configuration (tokens, paths, etc.)
├── utils.py             # Helper functions
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## License

Part of the Intent Record Protocol (IRP) project.
