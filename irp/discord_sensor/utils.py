"""Utility functions for Discord Sensor."""

from datetime import datetime, timezone
import uuid

def generate_irp_id() -> str:
    """Generate IRP decision ID in format IRP-YYYY-MM-DD-NNN."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Simple counter: use first 3 chars of UUID hash
    counter = str(uuid.uuid4().int)[:3]
    return f"IRP-{today}-{counter}"

def get_timestamp() -> str:
    """Get current timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat() + "Z"

def format_decision_summary(what: str, max_length: int = 80) -> str:
    """Format decision summary, truncate if needed."""
    if len(what) <= max_length:
        return what
    return what[:max_length - 3] + "..."

def escape_discord_markdown(text: str) -> str:
    """Escape Discord markdown special characters."""
    # Escape common markdown characters while preserving emoji
    chars_to_escape = ["*", "_", "`", "~", "|"]
    for char in chars_to_escape:
        if char not in text:  # Only escape if present
            text = text.replace(char, f"\\{char}")
    return text
