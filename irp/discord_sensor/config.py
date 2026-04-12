"""Configuration for Discord Sensor."""

import os
from pathlib import Path

# Discord Bot Configuration
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))  # Optional: for slash commands sync

# IRP Ledger Configuration
IRP_ROOT = Path(os.getenv("IRP_PROJECT_ROOT", ".")).resolve()
IRP_LEDGER_PATH = IRP_ROOT / ".irp" / "ledger.jsonl"
IRP_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)

# Bot Configuration
BOT_INTENTS = [
    "message_content",
    "guilds",
    "guild_messages",
]

# Modal Configuration
MODAL_TITLE = "Capture Decision"
MODAL_CUSTOM_ID = "decision_capture_modal"
FIELD_WHAT_CUSTOM_ID = "what_field"
FIELD_WHY_CUSTOM_ID = "why_field"
FIELD_TAGS_CUSTOM_ID = "tags_field"

# Playful response emojis
CELEBRATORY_EMOJIS = ["🎯", "🚀", "⚡", "✨"]
