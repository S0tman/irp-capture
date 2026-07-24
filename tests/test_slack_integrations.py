"""Tests for irp/core/integrations/slack_capture.py and slack_post.py.

slack_capture.py's STATE_DIR is a module-level constant created at import
time from IRP_SLACK_STATE_DIR (default "./slack_capture_state"), tests
monkeypatch the already-imported module's STATE_DIR rather than relying on
env vars, since re-importing wouldn't pick up a changed env var anyway.

slack_post.py needs the `requests` package, which is not part of the dev
extras. Its tests are import-skipped when requests is absent, and all HTTP
calls are mocked when present, no real Slack call is ever made.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import irp  # noqa: E402,F401

import irp.core.integrations.slack_capture as slack_capture  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "slack_state"
    d.mkdir()
    monkeypatch.setattr(slack_capture, "STATE_DIR", d)
    return d


# ── slack_capture ──────────────────────────────────────────────────────────────

class TestThreadState:
    def test_get_state_missing_returns_empty_dict(self, state_dir):
        assert slack_capture.get_thread_state("C123", "111.222") == {}

    def test_set_then_get_roundtrip(self, state_dir):
        slack_capture.set_thread_state("C123", "111.222", {"status": "pending"})
        assert slack_capture.get_thread_state("C123", "111.222") == {"status": "pending"}

    def test_mark_thread_state_without_irp_id(self, state_dir):
        slack_capture.mark_thread_state("C123", "111.222", "ignored")
        state = slack_capture.get_thread_state("C123", "111.222")
        assert state == {"channel_id": "C123", "thread_ts": "111.222", "status": "ignored"}

    def test_mark_thread_state_with_irp_id(self, state_dir):
        slack_capture.mark_thread_state("C123", "111.222", "confirmed", irp_id="IRP-2026-01-01-001")
        state = slack_capture.get_thread_state("C123", "111.222")
        assert state["irp_id"] == "IRP-2026-01-01-001"
        assert state["status"] == "confirmed"

    def test_state_file_name_replaces_dots_in_ts(self, state_dir):
        slack_capture.set_thread_state("C123", "111.222.333", {"x": 1})
        expected = state_dir / "C123__111_222_333.json"
        assert expected.exists()

    def test_different_threads_do_not_collide(self, state_dir):
        slack_capture.set_thread_state("C123", "111.111", {"status": "a"})
        slack_capture.set_thread_state("C123", "222.222", {"status": "b"})
        assert slack_capture.get_thread_state("C123", "111.111")["status"] == "a"
        assert slack_capture.get_thread_state("C123", "222.222")["status"] == "b"


# ── slack_post (requires `requests`; mocked, never a real network call) ──────
#
# slack_post.py does `import requests` at module load time. Guarding the
# import inside a fixture (rather than at module top level) means only the
# tests that need it are skipped when requests is absent, the slack_capture
# tests above must still run.

@pytest.fixture
def slack_post_mod(monkeypatch):
    pytest.importorskip("requests", reason="requests not installed (not a dev extra)")
    import irp.core.integrations.slack_post as slack_post
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-token-for-tests")
    return slack_post


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class TestResolveBotToken:
    def test_reads_from_env(self, slack_post_mod, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
        assert slack_post_mod._resolve_bot_token() == "xoxb-from-env"

    def test_raises_when_unset_and_no_env_file(self, slack_post_mod, monkeypatch, tmp_path):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        # Point the fallback .env lookup somewhere that doesn't exist.
        monkeypatch.setattr(
            slack_post_mod, "__file__", str(tmp_path / "nonexistent" / "slack_post.py")
        )
        with pytest.raises(RuntimeError):
            slack_post_mod._resolve_bot_token()


class TestBuildCandidateBlocks:
    def test_includes_confirm_edit_ignore_buttons(self, slack_post_mod):
        blocks = slack_post_mod._build_candidate_blocks("C1", "111.222", "Ship it", "Because", "high")
        actions = next(b for b in blocks if b["type"] == "actions")
        action_ids = {el["action_id"] for el in actions["elements"]}
        assert action_ids == {"irp_confirm", "irp_edit", "irp_ignore"}

    def test_action_value_contains_channel_and_thread(self, slack_post_mod):
        import json
        blocks = slack_post_mod._build_candidate_blocks("C1", "111.222", "Ship it", "Because", "high")
        actions = next(b for b in blocks if b["type"] == "actions")
        value = json.loads(actions["elements"][0]["value"])
        assert value["channel_id"] == "C1"
        assert value["thread_ts"] == "111.222"
        assert value["what"] == "Ship it"

    def test_long_text_is_truncated(self, slack_post_mod):
        long_what = "x" * 500
        blocks = slack_post_mod._build_candidate_blocks("C1", "111.222", long_what, "", "high")
        section = blocks[1]
        field_text = section["fields"][0]["text"]
        assert len(field_text) < 500


class TestApiHelper:
    def test_api_raises_on_slack_error_payload(self, slack_post_mod, monkeypatch):
        monkeypatch.setattr(
            slack_post_mod.requests, "post",
            lambda url, headers, json, timeout: _FakeResponse({"ok": False, "error": "channel_not_found"}),
        )
        with pytest.raises(RuntimeError, match="channel_not_found"):
            slack_post_mod._api("chat.postMessage", "xoxb-fake", {"channel": "C1"})

    def test_api_returns_data_on_success(self, slack_post_mod, monkeypatch):
        monkeypatch.setattr(
            slack_post_mod.requests, "post",
            lambda url, headers, json, timeout: _FakeResponse({"ok": True, "ts": "111.222"}),
        )
        data = slack_post_mod._api("chat.postMessage", "xoxb-fake", {"channel": "C1"})
        assert data["ts"] == "111.222"


class TestPostDemoThread:
    def test_posts_each_message_then_candidate_block(self, slack_post_mod, monkeypatch):
        posted = []

        def _fake_post(endpoint, token, payload):
            posted.append((endpoint, payload))
            if "blocks" in payload:
                return {"ok": True, "ts": "999.999"}
            return {"ok": True, "ts": f"{len(posted)}00.000"}

        monkeypatch.setattr(slack_post_mod, "_resolve_bot_token", lambda: "xoxb-fake")
        monkeypatch.setattr(slack_post_mod, "_fetch_avatars", lambda user_ids, token: {})
        monkeypatch.setattr(slack_post_mod, "_api", _fake_post)
        monkeypatch.setattr(slack_post_mod.time, "sleep", lambda s: None)

        result = slack_post_mod.post_demo_thread(
            channel_id="C1",
            thread_tuples=[("Johan", "hello"), ("Sven", "world")],
            what="Ship it", why="Because", confidence="high",
        )
        assert result["channel_id"] == "C1"
        assert result["candidate_ts"] == "999.999"
        # 2 thread messages + 1 candidate block = 3 API calls
        assert len(posted) == 3
        assert posted[-1][0] == "chat.postMessage"

    def test_uses_fallback_emoji_when_avatar_missing(self, slack_post_mod, monkeypatch):
        calls = []

        def _fake_post(endpoint, token, payload):
            calls.append(payload)
            return {"ok": True, "ts": "100.000"}

        monkeypatch.setattr(slack_post_mod, "_resolve_bot_token", lambda: "xoxb-fake")
        monkeypatch.setattr(slack_post_mod, "_fetch_avatars", lambda user_ids, token: {"Johan": None})
        monkeypatch.setattr(slack_post_mod, "_api", _fake_post)
        monkeypatch.setattr(slack_post_mod.time, "sleep", lambda s: None)

        slack_post_mod.post_demo_thread(
            channel_id="C1", thread_tuples=[("Johan", "hi")],
            what="x", why="y", confidence="low",
        )
        assert calls[0]["icon_emoji"] == slack_post_mod._FALLBACK_EMOJI["Johan"]
