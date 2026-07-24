"""Tests for tools/collab.py, the IRP-aware prompt launcher.

collab.py is a standalone script (not part of the irp package). Its pure
logic, reading/filtering IRP context from a project's .irp/ files,
minimal .env parsing, and critique JSON validation, is fully testable
without any network. The two network functions (call_model,
call_model_responses) are tested by monkeypatching urllib.request.urlopen so
no real HTTP call is ever made.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import collab  # noqa: E402


# ── read_irp_context ───────────────────────────────────────────────────────────

class TestReadIrpContext:
    def test_no_irp_dir_returns_none(self, tmp_path):
        assert collab.read_irp_context(str(tmp_path)) is None

    def test_reads_from_current_json(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "current.json").write_text(json.dumps({
            "active": [{"id": "IRP-1", "what": "Use SQLite", "why": "Simple", "confidence": "high"}]
        }), encoding="utf-8")
        ctx = collab.read_irp_context(str(tmp_path))
        assert "IRP-1" in ctx
        assert "Use SQLite" in ctx

    def test_falls_back_to_ledger_when_current_missing(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "ledger.jsonl").write_text(
            json.dumps({"id": "IRP-2", "what": "Use Postgres", "why": "Relational"}) + "\n",
            encoding="utf-8",
        )
        ctx = collab.read_irp_context(str(tmp_path))
        assert "IRP-2" in ctx

    def test_ledger_fallback_keeps_only_last_ten(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        lines = [json.dumps({"id": f"IRP-{i}", "what": f"decision {i}", "why": "x"}) for i in range(15)]
        (irp_dir / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        ctx = collab.read_irp_context(str(tmp_path))
        assert "IRP-0" not in ctx  # dropped, only last 10 kept
        assert "IRP-14" in ctx

    def test_topic_filters_to_matching_decisions(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "current.json").write_text(json.dumps({"active": [
            {"id": "IRP-1", "what": "Use SQLite for storage", "why": "simple"},
            {"id": "IRP-2", "what": "Use React for frontend", "why": "team fit"},
        ]}), encoding="utf-8")
        ctx = collab.read_irp_context(str(tmp_path), topic="frontend react")
        assert "IRP-2" in ctx
        assert "IRP-1" not in ctx

    def test_topic_with_no_matches_falls_back_to_all(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "current.json").write_text(json.dumps({"active": [
            {"id": "IRP-1", "what": "Use SQLite", "why": "simple"},
        ]}), encoding="utf-8")
        ctx = collab.read_irp_context(str(tmp_path), topic="zzz_nonexistent_zzz")
        assert "IRP-1" in ctx  # unfiltered fallback since nothing matched

    def test_corrupt_current_json_falls_back_to_ledger(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "current.json").write_text("{not json", encoding="utf-8")
        (irp_dir / "ledger.jsonl").write_text(
            json.dumps({"id": "IRP-3", "what": "fallback decision", "why": "x"}) + "\n",
            encoding="utf-8",
        )
        ctx = collab.read_irp_context(str(tmp_path))
        assert "IRP-3" in ctx

    def test_context_notes_read_only(self, tmp_path):
        irp_dir = tmp_path / ".irp"
        irp_dir.mkdir()
        (irp_dir / "current.json").write_text(json.dumps({"active": [
            {"id": "IRP-1", "what": "x", "why": "y"},
        ]}), encoding="utf-8")
        ctx = collab.read_irp_context(str(tmp_path))
        assert "Do not modify or override them" in ctx


# ── load_dotenv (collab's own minimal loader) ─────────────────────────────────

class TestLoadDotenv:
    def test_missing_file_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COLLAB_TEST_VAR", raising=False)
        collab.load_dotenv(str(tmp_path / "does-not-exist.env"))
        assert "COLLAB_TEST_VAR" not in __import__("os").environ

    def test_loads_simple_key_value(self, tmp_path, monkeypatch):
        import os
        monkeypatch.delenv("COLLAB_TEST_VAR", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("COLLAB_TEST_VAR=hello\n", encoding="utf-8")
        collab.load_dotenv(str(env_file))
        assert os.environ["COLLAB_TEST_VAR"] == "hello"
        del os.environ["COLLAB_TEST_VAR"]

    def test_does_not_override_existing_env_var(self, tmp_path, monkeypatch):
        import os
        monkeypatch.setenv("COLLAB_TEST_VAR", "original")
        env_file = tmp_path / ".env"
        env_file.write_text("COLLAB_TEST_VAR=overwritten\n", encoding="utf-8")
        collab.load_dotenv(str(env_file))
        assert os.environ["COLLAB_TEST_VAR"] == "original"

    def test_skips_comments_and_blank_lines(self, tmp_path, monkeypatch):
        import os
        monkeypatch.delenv("COLLAB_TEST_VAR2", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("# a comment\n\nCOLLAB_TEST_VAR2=value2\n", encoding="utf-8")
        collab.load_dotenv(str(env_file))
        assert os.environ["COLLAB_TEST_VAR2"] == "value2"
        del os.environ["COLLAB_TEST_VAR2"]

    def test_strips_quotes_from_value(self, tmp_path, monkeypatch):
        import os
        monkeypatch.delenv("COLLAB_TEST_VAR3", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('COLLAB_TEST_VAR3="quoted value"\n', encoding="utf-8")
        collab.load_dotenv(str(env_file))
        assert os.environ["COLLAB_TEST_VAR3"] == "quoted value"
        del os.environ["COLLAB_TEST_VAR3"]


# ── parse_critique ──────────────────────────────────────────────────────────────

class TestParseCritique:
    def test_valid_clear_verdict(self):
        raw = json.dumps({"verdict": "CLEAR", "principle_flags": [], "reasoning": "fine", "defer_question": None})
        result = collab.parse_critique(raw)
        assert result["verdict"] == "CLEAR"

    def test_strips_markdown_fences(self):
        raw = "```json\n" + json.dumps({"verdict": "WARN", "reasoning": "x"}) + "\n```"
        result = collab.parse_critique(raw)
        assert result["verdict"] == "WARN"

    def test_invalid_verdict_exits(self):
        raw = json.dumps({"verdict": "MAYBE", "reasoning": "x"})
        with pytest.raises(SystemExit):
            collab.parse_critique(raw)

    def test_invalid_json_exits(self):
        with pytest.raises(SystemExit):
            collab.parse_critique("{not json")

    def test_unknown_principle_flags_are_dropped(self):
        raw = json.dumps({
            "verdict": "BLOCK", "reasoning": "x",
            "principle_flags": ["human_control", "made_up_principle"],
        })
        result = collab.parse_critique(raw)
        assert result["principle_flags"] == ["human_control"]

    def test_missing_principle_flags_defaults_to_empty(self):
        raw = json.dumps({"verdict": "CLEAR", "reasoning": "x"})
        result = collab.parse_critique(raw)
        assert result["principle_flags"] == []


# ── call_model / call_model_responses (network mocked) ───────────────────────

class _FakeHTTPResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCallModel:
    def test_returns_message_content(self, monkeypatch):
        response_json = json.dumps({"choices": [{"message": {"content": "hello world"}}]}).encode()
        monkeypatch.setattr(collab, "urlopen", lambda req, timeout, context: _FakeHTTPResponse(response_json))
        result = collab.call_model([{"role": "user", "content": "hi"}], model="gpt-4.1", api_key="sk-fake")
        assert result == "hello world"

    def test_http_error_exits(self, monkeypatch):
        from urllib.error import HTTPError

        def _raise(req, timeout, context):
            raise HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b'{"error": "bad key"}'))

        monkeypatch.setattr(collab, "urlopen", _raise)
        with pytest.raises(SystemExit):
            collab.call_model([{"role": "user", "content": "hi"}], model="gpt-4.1", api_key="bad")

    def test_url_error_exits(self, monkeypatch):
        from urllib.error import URLError

        def _raise(req, timeout, context):
            raise URLError("no network")

        monkeypatch.setattr(collab, "urlopen", _raise)
        with pytest.raises(SystemExit):
            collab.call_model([{"role": "user", "content": "hi"}], model="gpt-4.1", api_key="sk-fake")


class TestCallModelResponses:
    def test_extracts_output_text(self, monkeypatch):
        response_json = json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "the answer"}]}]
        }).encode()
        monkeypatch.setattr(collab, "urlopen", lambda req, timeout, context: _FakeHTTPResponse(response_json))
        result = collab.call_model_responses(
            [{"role": "user", "content": "hi"}], model="o3", api_key="sk-fake"
        )
        assert result == "the answer"

    def test_falls_back_to_top_level_output_text(self, monkeypatch):
        response_json = json.dumps({"output": [], "output_text": "fallback text"}).encode()
        monkeypatch.setattr(collab, "urlopen", lambda req, timeout, context: _FakeHTTPResponse(response_json))
        result = collab.call_model_responses(
            [{"role": "user", "content": "hi"}], model="o3", api_key="sk-fake"
        )
        assert result == "fallback text"

    def test_no_extractable_text_exits(self, monkeypatch):
        response_json = json.dumps({"output": []}).encode()
        monkeypatch.setattr(collab, "urlopen", lambda req, timeout, context: _FakeHTTPResponse(response_json))
        with pytest.raises(SystemExit):
            collab.call_model_responses([{"role": "user", "content": "hi"}], model="o3", api_key="sk-fake")
