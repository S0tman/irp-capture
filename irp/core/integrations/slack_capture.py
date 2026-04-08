import json
import os
from pathlib import Path

STATE_DIR = Path(os.getenv("IRP_SLACK_STATE_DIR", "./slack_capture_state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

def _state_file(channel_id: str, thread_ts: str) -> Path:
    safe_name = f"{channel_id}__{thread_ts.replace('.', '_')}.json"
    return STATE_DIR / safe_name

def get_thread_state(channel_id: str, thread_ts: str) -> dict:
    path = _state_file(channel_id, thread_ts)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

def set_thread_state(channel_id: str, thread_ts: str, state: dict) -> None:
    path = _state_file(channel_id, thread_ts)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")

def mark_thread_state(channel_id: str, thread_ts: str, status: str, irp_id: str | None = None) -> None:
    state = {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "status": status,
    }
    if irp_id:
        state["irp_id"] = irp_id
    set_thread_state(channel_id, thread_ts, state)