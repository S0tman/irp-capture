"""Main entry point for Discord Sensor bot."""

import discord
from discord.ext import commands
import logging
from pathlib import Path

from .config import DISCORD_BOT_TOKEN, BOT_INTENTS
from .ledger_writer import LedgerWriter
from .commands import setup as setup_cog

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class IRPBot(commands.Bot):
    """Discord bot for IRP sensor."""

    def __init__(self, ledger_writer: LedgerWriter):
        """Initialize bot.

        Args:
            ledger_writer: LedgerWriter instance
        """
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.ledger_writer = ledger_writer

    async def setup_hook(self) -> None:
        """Setup hook called before login."""
        await setup_cog(self, self.ledger_writer)
        logger.info("IRP Sensor cog loaded")

    async def on_ready(self) -> None:
        """Called when bot is ready."""
        logger.info(f"Bot logged in as {self.user}")
        logger.info(f"Ledger path: {self.ledger_writer.ledger_path}")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

def run_bot(token: str = None, irp_root: Path = None):
    """Run the Discord bot.

    Args:
        token: Discord bot token (defaults to env var)
        irp_root: Path to IRP project root
    """
    token = token or DISCORD_BOT_TOKEN

    if not token:
        raise ValueError("DISCORD_BOT_TOKEN not set")

    # Initialize ledger writer
    ledger_writer = LedgerWriter()

    # Create and run bot
    bot = IRPBot(ledger_writer)

    logger.info("Starting IRP Discord Sensor...")
    bot.run(token)

if __name__ == "__main__":
    run_bot()
