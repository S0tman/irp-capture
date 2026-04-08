"""
Slack posting utilities for `irp demo generate --post-to-slack`.

Posts a synthetic thread to a Slack channel using username/avatar overrides
(one message per participant), then immediately posts the Ledger bot's
Confirm/Edit/Ignore candidate block in the same thread.

When the user clicks Confirm in Slack, the existing action_handler in
irp/slack_capture/ handles the write to .irp/ledger.jsonl — the normal path.

Design notes:
- Uses requests directly (available via slack-bolt's dependency chain)
- Bot token read from SLACK_BOT_TOKEN env var or irp/slack_capture/.env
- Avatars fetched at runtime via users.info so they stay current
- Block structure duplicated here intentionally (consistent with irp_writer.py
  pattern — cross-package imports between core and slack_capture are fragile)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Demo workspace participant → Slack user ID mapping
# ---------------------------------------------------------------------------
DEMO_PARTICIPANT_IDS: dict[str, str] = {
    "Johan": "U0ANDJ87V41",
    "Nate":  "U0AMYRNNQSJ",
    "Sven":  "U0ANDR369SM",
}

# Fallback emoji per participant if avatar fetch fails
_FALLBACK_EMOJI: dict[str, str] = {
    "Johan": ":bust_in_silhouette:",
    "Nate":  ":bust_in_silhouette:",
    "Sven":  ":bust_in_silhouette:",
}

# Small pause between posted messages — makes the thread feel natural
_MESSAGE_DELAY_S = 0.35


# ---------------------------------------------------------------------------
# Auth + HTTP
# ---------------------------------------------------------------------------

def _resolve_bot_token() -> str:
    """Return SLACK_BOT_TOKEN from env or irp/slack_capture/.env."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if token:
        return token
    # Fallback: read from the slack_capture .env file
    env_path = Path(__file__).resolve().parents[3] / "irp" / "slack_capture" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "SLACK_BOT_TOKEN not found. Set the env var or check irp/slack_capture/.env."
    )


def _api(endpoint: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to a Slack API endpoint, raise on error."""
    resp = requests.post(
        f"https://slack.com/api/{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"Slack API error [{endpoint}]: {data.get('error', 'unknown')}"
        )
    return data


# ---------------------------------------------------------------------------
# Avatar resolution
# ---------------------------------------------------------------------------

def _fetch_avatars(user_ids: dict[str, str], token: str) -> dict[str, str | None]:
    """Return {name: avatar_url} for each participant, None on failure.

    users.info is a GET-style endpoint — params must be sent as query string,
    not JSON body.
    """
    result: dict[str, str | None] = {}
    for name, uid in user_ids.items():
        try:
            resp = requests.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {token}"},
                params={"user": uid},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                result[name] = None
                continue
            profile = data.get("user", {}).get("profile", {})
            result[name] = (
                profile.get("image_72")
                or profile.get("image_48")
                or profile.get("image_32")
                or None
            )
        except Exception:
            result[name] = None
    return result


# ---------------------------------------------------------------------------
# Candidate block builder (self-contained — see design note above)
# ---------------------------------------------------------------------------

def _build_candidate_blocks(
    channel_id: str,
    thread_ts: str,
    what: str,
    why: str,
    confidence: str,
) -> list[dict[str, Any]]:
    """Build the Confirm/Edit/Ignore Block Kit payload.

    Matches the format expected by action_handler.py exactly:
    action_value keys: channel_id, thread_ts, what, why,
                       decision_type, decision_confidence
    """
    def _trunc(text: str, n: int = 200) -> str:
        return text if len(text) <= n else text[: n - 3] + "..."

    action_value = json.dumps({
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "what": _trunc(what),
        "why": _trunc(why),
        "decision_type": "decision",
        "decision_confidence": confidence,
    })

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*IRP detected a likely durable decision in this thread.*",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*What:*\n{_trunc(what) or '_Not provided_'}"},
                {"type": "mrkdwn", "text": f"*Why:*\n{_trunc(why) or '_Not provided_'}"},
                {"type": "mrkdwn", "text": f"*Type:*\ndecision"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{confidence}"},
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Source:* Slack thread  \u2022  "
                        f"*Channel:* `{channel_id}`  \u2022  "
                        f"*Thread:* `{thread_ts}`  \u2022  "
                        f"*IRP ID:* pending"
                    ),
                }
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Project bridge:* If confirmed, this decision will be written to "
                    "the shared project `.irp/` and will be available to all sensors "
                    "(Slack + IRP Capture SKILL)."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "style": "primary",
                    "action_id": "irp_confirm",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": "irp_edit",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Ignore"},
                    "style": "danger",
                    "action_id": "irp_ignore",
                    "value": action_value,
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def post_demo_thread(
    channel_id: str,
    thread_tuples: list[tuple[str, str]],
    what: str,
    why: str,
    confidence: str,
) -> dict[str, str]:
    """Post the synthetic thread to Slack, then the Ledger bot candidate block.

    Args:
        channel_id:    Target Slack channel ID (e.g. C0AMXC2E069)
        thread_tuples: List of (speaker_name, message_text) pairs
        what:          Decision text for the candidate block
        why:           Reasoning text for the candidate block
        confidence:    low | medium | high

    Returns:
        {"channel_id": ..., "thread_ts": ..., "candidate_ts": ...}
    """
    token = _resolve_bot_token()
    avatars = _fetch_avatars(DEMO_PARTICIPANT_IDS, token)

    root_ts: str | None = None

    # Post each message in sequence
    for i, (speaker, message) in enumerate(thread_tuples):
        payload: dict[str, Any] = {
            "channel": channel_id,
            "username": speaker,
            "text": message,
        }
        avatar_url = avatars.get(speaker)
        if avatar_url:
            payload["icon_url"] = avatar_url
        else:
            payload["icon_emoji"] = _FALLBACK_EMOJI.get(speaker, ":bust_in_silhouette:")

        if root_ts is not None:
            payload["thread_ts"] = root_ts

        result = _api("chat.postMessage", token, payload)

        if root_ts is None:
            root_ts = result["ts"]

        if i < len(thread_tuples) - 1:
            time.sleep(_MESSAGE_DELAY_S)

    assert root_ts is not None  # always set after first message

    # Post the candidate block as the Ledger bot
    blocks = _build_candidate_blocks(channel_id, root_ts, what, why, confidence)
    candidate_result = _api("chat.postMessage", token, {
        "channel": channel_id,
        "thread_ts": root_ts,
        "blocks": blocks,
        "text": "IRP detected a likely durable decision in this thread.",
    })

    return {
        "channel_id": channel_id,
        "thread_ts": root_ts,
        "candidate_ts": candidate_result["ts"],
    }
