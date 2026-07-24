"""Tests for irp demo generate, synthetic thread + ledger entry generator."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

from store import ensure_irp_dir, read_ledger  # noqa: E402
from commands.demo import run_demo, run_demo_generate, render_thread, TEMPLATES  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        defaults = dict(demo_action="generate", scenario="product-decision", confidence="high",
                         write_thread=False, post_to_slack=None)
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestRunDemoGenerate:
    def test_writes_ledger_entry(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args())
        assert result["status"] == "generated"
        assert len(read_ledger(irp_dir)) == 1

    def test_entry_tagged_demo(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        run_demo(tmp_path, irp_dir, _Args())
        assert "demo" in read_ledger(irp_dir)[0]["tags"]

    def test_entry_matches_scenario_and_confidence(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        run_demo(tmp_path, irp_dir, _Args(scenario="architecture", confidence="low"))
        entry = read_ledger(irp_dir)[0]
        assert entry["scenario"] == "architecture"
        assert entry["confidence"] == "low"
        assert entry["what"] == TEMPLATES["architecture"]["low"]["what"]

    def test_unknown_scenario_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args(scenario="not-a-scenario"))
        assert result["status"] == "error"
        assert read_ledger(irp_dir) == []

    def test_unknown_confidence_is_an_error(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args(confidence="extreme"))
        assert result["status"] == "error"

    def test_write_thread_saves_markdown_file(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args(write_thread=True))
        thread_file = Path(result["thread_file"])
        assert thread_file.exists()
        assert thread_file.parent == irp_dir / "demo_threads"

    def test_no_write_thread_by_default(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args())
        assert result["thread_file"] is None
        assert not (irp_dir / "demo_threads").exists()

    def test_rebuilds_current_json(self, tmp_path):
        import json
        irp_dir = ensure_irp_dir(tmp_path)
        run_demo(tmp_path, irp_dir, _Args())
        current = json.loads((irp_dir / "current.json").read_text(encoding="utf-8"))
        assert len(current["active"]) == 1

    def test_all_scenario_confidence_pairs_generate_ledger_entries(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        for scenario in TEMPLATES:
            for confidence in ("low", "medium", "high"):
                result = run_demo(tmp_path, irp_dir, _Args(scenario=scenario, confidence=confidence))
                assert result["status"] == "generated", f"{scenario}/{confidence} failed"

    def test_unknown_demo_action_errors(self, tmp_path):
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args(demo_action="bogus"))
        assert result["status"] == "error"


class TestSlackMode:
    def test_missing_requests_dependency_reports_error(self, tmp_path, monkeypatch):
        """post_to_slack path imports slack_post, which needs `requests`.

        In this dev environment `requests` is not installed, so the import
        fails and demo generate must report the error instead of writing to
        the local ledger (per its own doc: 'do NOT write to the local ledger
        in this mode').
        """
        irp_dir = ensure_irp_dir(tmp_path)
        result = run_demo(tmp_path, irp_dir, _Args(post_to_slack="C0AMXC2E069"))
        assert result["status"] == "error"
        assert read_ledger(irp_dir) == []

    def test_slack_post_success_does_not_touch_local_ledger(self, tmp_path, monkeypatch):
        """When posting succeeds, the local ledger must stay untouched, the
        Slack Confirm button is the write path in this mode, not this call."""
        irp_dir = ensure_irp_dir(tmp_path)

        # Provide a fake slack_post module so the import inside run_demo_generate
        # succeeds without needing `requests` installed.
        import types
        fake_module = types.ModuleType("irp.core.integrations.slack_post")

        def _fake_post_demo_thread(channel_id, thread_tuples, what, why, confidence):
            return {"channel_id": channel_id, "thread_ts": "111.222", "candidate_ts": "111.333"}

        fake_module.post_demo_thread = _fake_post_demo_thread
        monkeypatch.setitem(sys.modules, "irp.core.integrations.slack_post", fake_module)

        result = run_demo(tmp_path, irp_dir, _Args(post_to_slack="C0AMXC2E069"))
        assert result["status"] == "posted_to_slack"
        assert result["slack"]["channel_id"] == "C0AMXC2E069"
        assert read_ledger(irp_dir) == []


class TestRenderThread:
    def test_includes_scenario_and_confidence(self):
        thread = [("A", "hello")]
        rendered = render_thread(thread, "architecture", "high")
        assert "architecture" in rendered
        assert "high" in rendered

    def test_includes_all_messages(self):
        thread = [("A", "first message"), ("B", "second message")]
        rendered = render_thread(thread, "policy", "low")
        assert "A: first message" in rendered
        assert "B: second message" in rendered

    def test_notes_synthetic_and_deterministic(self):
        rendered = render_thread([("A", "x")], "pricing", "medium")
        assert "synthetic" in rendered.lower()
