# Discord Sensor Setup for OpenSverige

This guide walks through setting up the IRP Ledger Discord bot for the OpenSverige community.

## What It Does

The IRP Ledger bot captures decisions from Discord messages directly into your IRP ledger (`.irp/ledger.jsonl`).

**Workflow:**
1. Team makes a decision in Discord
2. Right-click the message → Apps → "Capture decision"
3. Fill in what was decided and why
4. Bot writes it to the ledger with Discord source reference
5. Decision survives all future tool changes

**Feature highlight:** Emoji-friendly (preserves emoji and GIF URLs in decisions). Community-native tone.

---

## Prerequisites

- Python 3.9+ (system or Homebrew Python recommended — avoid MacPorts if possible)
- Access to the OpenSverige Discord server (admin permissions to add bot)
- Git and GitHub (to pull the latest code)

---

## Step 1: Create Discord Bot (One-time)

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it "IRP Ledger"
3. Go to **Bot** section → **Add Bot**
4. Copy the **bot token** (you'll need this)
5. Under **OAuth2** → **URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Permissions: `Send Messages`, `Use Slash Commands`, `Manage Messages`
6. Copy the **generated URL** at the bottom
7. Open the URL in your browser to invite the bot to OpenSverige server

---

## Step 2: Clone IRP Project

```bash
git clone https://github.com/S0tman/irp-capture.git
cd irp-capture
```

Or if you already have it:
```bash
cd irp-capture
git pull origin main
```

---

## Step 3: Set Up Discord Sensor

```bash
# Navigate to sensor directory
cd irp/discord_sensor

# Create Python virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Step 4: Configure Bot Token

```bash
# Copy the example config
cp .env.example .env

# Edit .env with your values
# Use your editor to open .env and fill in:
```

**.env contents:**
```
DISCORD_BOT_TOKEN=your_bot_token_from_step_1
IRP_PROJECT_ROOT=/path/to/irp-capture
DISCORD_GUILD_ID=your_server_id_optional
```

To find your server ID:
- Right-click the OpenSverige server name
- Copy Server ID (Developer Mode must be enabled in Discord)

---

## Step 5: Run the Bot

```bash
# Make sure you're in the venv
source venv/bin/activate

# From the irp-capture directory
python -m irp.discord_sensor.main
```

You should see:
```
INFO:__main__:Starting IRP Discord Sensor...
INFO:discord.client:logging in using static token
Bot logged in as IRP Ledger
Synced N command(s)
```

The bot is now **live in OpenSverige Discord**. ✅

---

## Step 6: Test the Bot

1. Go to OpenSverige #general (or any channel)
2. Send a test message: "We decided to use Discord for decision capture"
3. Right-click that message
4. Select **Apps** → **Capture decision**
5. Modal appears with fields:
   - **What was decided?** → "Use Discord for decision capture"
   - **Why was it decided?** → "Community-native, emoji-friendly, federated with ledger"
   - **Tags** (optional) → "process, tools"
6. Submit
7. See ephemeral reply: "🎯 Locked in! Decision IRP-2026-04-12-001 captured."
8. Check the ledger:
   ```bash
   cat .irp/ledger.jsonl | tail -1 | python -m json.tool
   ```

You should see the decision with Discord source reference (message ID, channel, etc.).

---

## Keeping the Bot Running

### Development
For testing and development, run it in the foreground as shown above.

### Production
For persistent use, consider:
- **tmux/screen** — terminal multiplexer
- **systemd** — system service
- **supervisor** — process monitor
- **Docker** — containerized deployment

---

## Troubleshooting

**Bot doesn't appear in Discord after invite:**
- Check bot has `Send Messages` and `Use Application Commands` permissions
- Restart Discord app
- Ensure OAuth2 scopes included both `bot` and `applications.commands`

**"Capture decision" context menu doesn't appear:**
- Make sure bot is running (`python -m irp.discord_sensor.main`)
- Check bot has `Use Application Commands` permission
- Wait 1-2 minutes for Discord to sync commands
- Restart Discord client if needed

**SSL certificate error (macOS with MacPorts):**
- Error: `ssl.SSLCertVerificationError: certificate verify failed`
- **Solution:**
  ```bash
  # Install certifi in your virtual environment
  source venv/bin/activate
  pip install certifi
  
  # Run the bot with the certifi CA bundle
  SSL_CERT_FILE=$(python3 -c 'import certifi; print(certifi.where())') python -m irp.discord_sensor.main
  ```
- Alternative: Use system Python 3 instead of MacPorts (recommended if available)

**Ledger file not found:**
- Ensure `IRP_PROJECT_ROOT` in `.env` points to the correct directory
- The bot creates `.irp/` folder automatically
- Check file permissions

---

## How It Integrates with IRP

The Discord sensor is one of many ways to capture decisions. All sensors write to the same ledger:

```
Discord sensor → .irp/ledger.jsonl ← All other sensors
                        ↓
                   Derived state (current.json)
                        ↓
                    REST API (/inherit, /why, /check)
                        ↓
                  Your workflow (any tool)
```

The ledger is the single source of truth. Decisions survive tool changes.

---

## Next Steps

1. **Run it locally first** (this guide)
2. **Invite friends** to test in OpenSverige
3. **Capture real decisions** from actual discussions
4. **Monitor usage** — what decisions matter most?
5. **Iterate** — add GitHub sensor, Slack sensor, etc.

---

## Support

For issues or questions:
- Check [Discord Sensor README](./README.md) for detailed documentation
- Open an issue on [GitHub](https://github.com/S0tman/irp-capture/issues)
- Ask in OpenSverige Discord

---

**Built with intention for sovereign builders.** 🚀
