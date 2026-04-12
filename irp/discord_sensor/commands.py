"""Discord commands for IRP sensor."""

import discord
from discord.ext import commands
from discord import app_commands
from .ledger_writer import LedgerWriter
from .modals import DecisionCaptureModal

class IRPSensorCog(commands.Cog):
    """Cog containing IRP sensor commands."""

    def __init__(self, bot: commands.Bot, ledger_writer: LedgerWriter):
        """Initialize cog.

        Args:
            bot: Discord bot instance
            ledger_writer: LedgerWriter for persisting decisions
        """
        self.bot = bot
        self.ledger_writer = ledger_writer

    @app_commands.command(
        name="irp_capture",
        description="Capture a decision into the IRP ledger",
    )
    async def slash_capture(
        self,
        interaction: discord.Interaction,
        what: str,
        why: str,
        tags: str = None,
    ):
        """Slash command for capturing decisions.

        Args:
            interaction: Discord interaction
            what: What was decided
            why: Why was it decided
            tags: Optional tags
        """
        try:
            # Build Discord reference (from interaction channel, not a specific message)
            discord_ref = self.ledger_writer.build_discord_ref(
                guild_id=str(interaction.guild.id),
                channel_id=str(interaction.channel.id),
                message_id="",  # No specific message for slash command
                message_url=None,
            )

            # Write to ledger
            irp_id = self.ledger_writer.write_decision(
                what=what,
                why=why,
                confirmed_by=interaction.user.name,
                tags=tags or "",
                discord_ref=discord_ref,
            )

            # Send ephemeral confirmation
            await interaction.response.send_message(
                f"🎯 **Locked in!** Decision `{irp_id}` captured.\n"
                f"**What:** {what}",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to capture decision: {str(e)}",
                ephemeral=True,
            )

    @app_commands.context_menu(name="Capture decision")
    async def message_capture(
        self, interaction: discord.Interaction, message: discord.Message
    ):
        """Message context menu for capturing a decision from a message.

        Args:
            interaction: Discord interaction
            message: The message being captured
        """
        # Show modal to user
        modal = DecisionCaptureModal(message, self.ledger_writer)
        await interaction.response.send_modal(modal)

async def setup(bot: commands.Bot, ledger_writer: LedgerWriter):
    """Load the IRP Sensor cog.

    Args:
        bot: Discord bot instance
        ledger_writer: LedgerWriter instance
    """
    await bot.add_cog(IRPSensorCog(bot, ledger_writer))
