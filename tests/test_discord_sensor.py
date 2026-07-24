"""Tests for irp/discord_sensor/*.

utils.py has no external dependencies and no import-time side effects, so it
gets full behavior tests. Everything else in this package (config.py,
ledger_writer.py, commands.py, modals.py, main.py) either needs `discord.py`
or `python-dotenv`, neither is part of the dev extras, and config.py also
creates a real `.irp/` directory as an import-time side effect derived from
cwd. Rather than risk polluting the repo or forcing a brittle test, those
modules get an import-skip smoke test: it proves the module is at least
syntactically valid and skips cleanly when the optional dependency is
missing, without ever executing their side effects in this environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from irp.discord_sensor import utils  # noqa: E402


# ── utils.py, pure logic, fully tested ───────────────────────────────────────

class TestGenerateIrpId:
    def test_format_has_irp_prefix_and_datestamp(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        irp_id = utils.generate_irp_id()
        assert irp_id.startswith(f"IRP-{today}-")

    def test_suffix_is_three_digits(self):
        irp_id = utils.generate_irp_id()
        suffix = irp_id.rsplit("-", 1)[-1]
        assert len(suffix) == 3
        assert suffix.isdigit()

    def test_successive_calls_are_not_required_to_be_unique_but_are_well_formed(self):
        # generate_irp_id uses a random UUID slice, not a ledger-aware counter,
        # so uniqueness isn't guaranteed, just check both are well-formed.
        a, b = utils.generate_irp_id(), utils.generate_irp_id()
        assert a.startswith("IRP-")
        assert b.startswith("IRP-")


class TestGetTimestamp:
    def test_ends_with_z_suffix(self):
        assert utils.get_timestamp().endswith("Z")

    def test_contains_iso_date_separator(self):
        assert "T" in utils.get_timestamp()


class TestFormatDecisionSummary:
    def test_short_text_unchanged(self):
        assert utils.format_decision_summary("short text") == "short text"

    def test_long_text_truncated_with_ellipsis(self):
        text = "x" * 100
        result = utils.format_decision_summary(text, max_length=20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_exact_length_boundary_not_truncated(self):
        text = "x" * 80
        assert utils.format_decision_summary(text, max_length=80) == text


class TestEscapeDiscordMarkdown:
    """escape_discord_markdown backslash-escapes Discord markdown characters
    (*, _, `, ~, |) so user text is rendered literally, not as formatting."""

    def test_plain_text_unchanged(self):
        assert utils.escape_discord_markdown("plain text") == "plain text"

    def test_escapes_asterisks(self):
        assert utils.escape_discord_markdown("*bold*") == "\\*bold\\*"

    def test_escapes_underscores(self):
        assert utils.escape_discord_markdown("_italic_") == "\\_italic\\_"

    def test_escapes_every_special_char(self):
        assert utils.escape_discord_markdown("a*b_c`d~e|f") == "a\\*b\\_c\\`d\\~e\\|f"

    def test_empty_string_unchanged(self):
        assert utils.escape_discord_markdown("") == ""


# ── other discord_sensor modules: import-skip smoke tests ───────────────────

class TestOptionalDependencySmoke:
    def test_config_module_needs_dotenv(self):
        """config.py does `from dotenv import load_dotenv` at module scope and
        also creates .irp/ under IRP_PROJECT_ROOT (or the repo root) as an
        import side effect. Skip cleanly when python-dotenv (not a dev
        extra) is absent, rather than importing it live."""
        pytest.importorskip("dotenv", reason="python-dotenv not installed (not a dev extra)")
        import irp.discord_sensor.config as config  # noqa: F401

    def test_ledger_writer_needs_dotenv_via_config(self):
        pytest.importorskip("dotenv", reason="python-dotenv not installed (not a dev extra)")
        import irp.discord_sensor.ledger_writer  # noqa: F401

    def test_commands_module_needs_discord_py(self):
        pytest.importorskip("discord", reason="discord.py not installed (not a dev extra)")
        import irp.discord_sensor.commands  # noqa: F401

    def test_modals_module_needs_discord_py(self):
        pytest.importorskip("discord", reason="discord.py not installed (not a dev extra)")
        import irp.discord_sensor.modals  # noqa: F401

    def test_main_module_needs_discord_py(self):
        pytest.importorskip("discord", reason="discord.py not installed (not a dev extra)")
        import irp.discord_sensor.main  # noqa: F401


class TestLedgerWriterLogicWithFakeConfig:
    """Exercise LedgerWriter's actual entry-building logic without needing
    python-dotenv installed, by injecting a stand-in `.config` module into
    sys.modules before import (ledger_writer.py only needs IRP_LEDGER_PATH
    from it)."""

    def test_write_decision_builds_expected_entry(self, tmp_path, monkeypatch):
        import types
        import importlib

        fake_config = types.ModuleType("irp.discord_sensor.config")
        fake_config.IRP_LEDGER_PATH = tmp_path / ".irp" / "ledger.jsonl"
        monkeypatch.setitem(sys.modules, "irp.discord_sensor.config", fake_config)
        # Force a fresh import of ledger_writer bound to our fake config.
        sys.modules.pop("irp.discord_sensor.ledger_writer", None)
        ledger_writer = importlib.import_module("irp.discord_sensor.ledger_writer")

        writer = ledger_writer.LedgerWriter()
        irp_id = writer.write_decision(
            what=" Use SQLite ", why=" Zero infra ", confirmed_by="alice",
            tags="architecture, storage",
            discord_ref=writer.build_discord_ref("G1", "C1", "M1"),
        )

        assert irp_id.startswith("IRP-")
        lines = (tmp_path / ".irp" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        import json
        entry = json.loads(lines[0])
        assert entry["what"] == "Use SQLite"  # stripped
        assert entry["why"] == "Zero infra"
        assert entry["source"] == "discord"
        assert entry["confirmed_by"] == "alice"
        assert set(entry["tags"]) == {"architecture", "storage", "discord", "sensor"}
        assert entry["source_ref"]["guild_id"] == "G1"

        # Clean up the injected fake module so it doesn't leak into other tests.
        sys.modules.pop("irp.discord_sensor.ledger_writer", None)
        sys.modules.pop("irp.discord_sensor.config", None)

    def test_build_discord_ref_includes_thread_id_when_given(self, tmp_path, monkeypatch):
        import types
        import importlib

        fake_config = types.ModuleType("irp.discord_sensor.config")
        fake_config.IRP_LEDGER_PATH = tmp_path / ".irp" / "ledger.jsonl"
        monkeypatch.setitem(sys.modules, "irp.discord_sensor.config", fake_config)
        sys.modules.pop("irp.discord_sensor.ledger_writer", None)
        ledger_writer = importlib.import_module("irp.discord_sensor.ledger_writer")

        writer = ledger_writer.LedgerWriter()
        ref = writer.build_discord_ref("G1", "C1", "M1", thread_id="T1", message_url="https://x")
        assert ref["thread_id"] == "T1"
        assert ref["message_url"] == "https://x"

        sys.modules.pop("irp.discord_sensor.ledger_writer", None)
        sys.modules.pop("irp.discord_sensor.config", None)
