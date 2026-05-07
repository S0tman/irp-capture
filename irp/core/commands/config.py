"""IRP config — read and write project-level IRP settings.

Stored in .irp/config.json. Never modifies the ledger.

Usage:
    irp config get                     # show all settings
    irp config get control_level       # show one key
    irp config set control_level easy  # set a key
    irp config set control_level medium
    irp config set control_level advanced

Supported keys:
    control_level   easy | medium | advanced (default: advanced)
                    Controls agent gate verbosity, defer vocabulary,
                    and which verdicts trigger an IRP defer prompt.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from store import read_config, write_config


# ── valid values ──────────────────────────────────────────────────────────────

_VALID_VALUES: dict[str, list[str]] = {
    "control_level": ["easy", "medium", "advanced"],
}

_KEY_DESCRIPTIONS: dict[str, str] = {
    "control_level": (
        "Controls agent gate verbosity and defer vocabulary.\n"
        "  easy     — gate only on BLOCK; plain language; minimal audit trail\n"
        "  medium   — gate on WARN + BLOCK; standard vocabulary\n"
        "  advanced — all gates; technical vocabulary; full audit trail (default)"
    ),
}


# ── public entry point ────────────────────────────────────────────────────────

def run_config(project_root: Path, irp_dir: Path, args) -> dict[str, Any]:
    config_action = getattr(args, "config_action", None)
    as_json = bool(getattr(args, "json", False))

    if config_action == "get":
        return _run_get(irp_dir, args, as_json)
    if config_action == "set":
        return _run_set(irp_dir, args, as_json)

    return {
        "command": "config",
        "status": "error",
        "text": "Unknown config action. Use: irp config get | irp config set <key> <value>",
    }


def _run_get(irp_dir: Path, args, as_json: bool) -> dict[str, Any]:
    key: str | None = getattr(args, "key", None)
    cfg = read_config(irp_dir)

    if key:
        if key not in _VALID_VALUES:
            return {
                "command": "config.get",
                "status": "error",
                "text": (
                    f"Unknown key: {key!r}\n"
                    f"Supported keys: {', '.join(_VALID_VALUES)}"
                ),
            }
        value = cfg.get(key)
        text = f"{key} = {value}"
        return {
            "command": "config.get",
            "status": "ok",
            "key": key,
            "value": value,
            "text": text,
        }

    # Show all settings.
    lines = ["IRP project config (.irp/config.json)\n"]
    for k, v in cfg.items():
        desc = _KEY_DESCRIPTIONS.get(k, "")
        lines.append(f"  {k} = {v}")
        if desc and not as_json:
            for dl in desc.splitlines():
                lines.append(f"    {dl}")
        lines.append("")

    return {
        "command": "config.get",
        "status": "ok",
        "config": cfg,
        "text": "\n".join(lines),
    }


def _run_set(irp_dir: Path, args, as_json: bool) -> dict[str, Any]:
    key: str | None = getattr(args, "key", None)
    value: str | None = getattr(args, "value", None)

    if not key or not value:
        return {
            "command": "config.set",
            "status": "error",
            "text": "Usage: irp config set <key> <value>",
        }

    if key not in _VALID_VALUES:
        return {
            "command": "config.set",
            "status": "error",
            "text": (
                f"Unknown key: {key!r}\n"
                f"Supported keys: {', '.join(_VALID_VALUES)}"
            ),
        }

    valid = _VALID_VALUES[key]
    if value not in valid:
        return {
            "command": "config.set",
            "status": "error",
            "text": (
                f"Invalid value for {key!r}: {value!r}\n"
                f"Valid values: {', '.join(valid)}"
            ),
        }

    cfg = read_config(irp_dir)
    old_value = cfg.get(key)
    cfg[key] = value
    write_config(irp_dir, cfg)

    text = f"Set {key} = {value}"
    if old_value and old_value != value:
        text += f"  (was: {old_value})"

    return {
        "command": "config.set",
        "status": "ok",
        "key": key,
        "value": value,
        "previous": old_value,
        "text": text,
    }
