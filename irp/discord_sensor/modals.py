"""Modal interactions for Discord sensor."""

import discord
from discord import ui
from .config import (
    MODAL_TITLE,
    MODAL_CUSTOM_ID,
    FIELD_WHAT_CUSTOM_ID,
    FIELD_WHY_CUSTOM_ID,
    FIELD_TAGS_CUSTOM_ID,
)
from .ledger_writer import LedgerWriter

class DecisionCaptureModal(ui.Modal):
    """Modal for capturing a decision with what, why, and tags."""

    def __init__(self, message: discord.Message, ledger_writer: LedgerWriter):
        """Initialize modal.

        Args:
            message: The Discord message being captured
            ledger_writer: LedgerWriter instance for persisting decision
        """
        super().__init__(title=MODAL_TITLE, custom_id=MODAL_CUSTOM_ID)
        self.message = message
        self.ledger_writer = ledger_writer

        # Add input fields
        self.what_input = ui.TextInput(
            label="What was decided?",
            placeholder="The decision made (be concise)",
            required=True,
            max_length=500,
            custom_id=FIELD_WHAT_CUSTOM_ID,
            style=discord.TextStyle.short,
        )
        self.add_item(self.what_input)

        self.why_input = ui.TextInput(
            label="Why was it decided?",
            placeholder="The reasoning (can include GIF URLs)",
            required=True,
            max_length=1000,
            custom_id=FIELD_WHY_CUSTOM_ID,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.why_input)

        self.tags_input = ui.TextInput(
            label="Tags (optional)",
            placeholder="Comma-separated tags (e.g., architecture, infra)",
            required=False,
            max_length=200,
            custom_id=FIELD_TAGS_CUSTOM_ID,
            style=discord.TextStyle.short,
        )
        self.add_item(self.tags_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission."""
        try:
            what = self.what_input.value
            why = self.why_input.value
            tags = self.tags_input.value or ""
            username = interaction.user.name

            # Build Discord reference
            # Handle thread_id safely (message.thread only exists if message is in a thread)
            thread_id = None
            if hasattr(self.message, 'thread') and self.message.thread:
                thread_id = str(self.message.thread.id)

            discord_ref = self.ledger_writer.build_discord_ref(
                guild_id=str(self.message.guild.id),
                channel_id=str(self.message.channel.id),
                message_id=str(self.message.id),
                thread_id=thread_id,
                message_url=self.message.jump_url,
            )

            # Write to ledger
            irp_id = self.ledger_writer.write_decision(
                what=what,
                why=why,
                confirmed_by=username,
                tags=tags,
                discord_ref=discord_ref,
            )

            # Send ephemeral confirmation
            await interaction.response.send_message(
                f"🎯 **Locked in!** Decision `{irp_id}` captured.\n"
                f"**What:** {what}\n"
                f"*Read the decision reasoning in the ledger.*",
                ephemeral=True,
            )

        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to capture decision: {str(e)}",
                ephemeral=True,
            )
