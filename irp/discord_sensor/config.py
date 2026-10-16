"""Configuration for Discord Sensor."""

import os
from pathlib import Path
from dotenv import load_dotenv

from irp.core.store import ProjectNotFound, find_project

# Load .env from discord_sensor directory
DISCORD_SENSOR_DIR = Path(__file__).parent
ENV_FILE = DISCORD_SENSOR_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Discord Bot Configuration
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))  # Optional: for slash commands sync

# IRP Ledger Configuration
# IRP_PROJECT_ROOT names the project; otherwise it's found by walking up from
# the folder the sensor starts in. Nothing is created here. With no project,
# IRP_ROOT and IRP_LEDGER_PATH are None and the bot refuses to start.
def _find_root():
    try:
        return find_project().root, ""
    except ProjectNotFound as exc:
        return None, (f"No IRP project at {exc.searched_from}, so the Discord sensor didn't start. "
                      f"Start one with: {exc.hint}")


IRP_ROOT, IRP_PROJECT_PROBLEM = _find_root()
IRP_LEDGER_PATH = IRP_ROOT / ".irp" / "ledger.jsonl" if IRP_ROOT else None

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
