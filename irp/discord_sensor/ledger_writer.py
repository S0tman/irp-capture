"""Ledger writer for Discord sensor decisions."""

import json
from pathlib import Path
from typing import Optional, Dict, Any
from .config import IRP_LEDGER_PATH
from .utils import generate_irp_id, get_timestamp

class LedgerWriter:
    """Writes decision entries to .irp/ledger.jsonl."""

    def __init__(self, ledger_path: Optional[Path] = None):
        """Initialize ledger writer.

        Args:
            ledger_path: Path to ledger.jsonl file (defaults to config)
        """
        self.ledger_path = ledger_path or IRP_LEDGER_PATH
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def write_decision(
        self,
        what: str,
        why: str,
        confirmed_by: str,
        tags: Optional[str] = None,
        discord_ref: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write a decision to the ledger.

        Args:
            what: What was decided
            why: Why was it decided
            confirmed_by: Discord user who confirmed
            tags: Optional comma-separated tags
            discord_ref: Discord source reference (guild_id, channel_id, message_id, etc.)

        Returns:
            IRP ID of the decision
        """
        irp_id = generate_irp_id()
        timestamp = get_timestamp()

        # Parse tags
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        tag_list.extend(["discord", "sensor"])  # Always add these

        # Build entry
        entry = {
            "id": irp_id,
            "timestamp": timestamp,
            "type": "decision",
            "what": what.strip(),
            "why": why.strip(),
            "source": "discord",
            "confirmed_by": confirmed_by,
            "context": "opensverige Discord",
            "tags": list(set(tag_list)),  # Remove duplicates
        }

        # Add Discord-specific reference if provided
        if discord_ref:
            entry["source_ref"] = discord_ref

        # Append to ledger
        try:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return irp_id
        except IOError as e:
            raise IOError(f"Failed to write to ledger: {e}")

    def build_discord_ref(
        self,
        guild_id: str,
        channel_id: str,
        message_id: str,
        thread_id: Optional[str] = None,
        message_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build Discord source reference."""
        ref = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "message_id": message_id,
            "message_url": message_url,
        }
        if thread_id:
            ref["thread_id"] = thread_id
        return ref
