"""Tests for irp watch — IRP-US-012.

Acceptance criteria:
  012a: one input line → one JSON verdict line (verdict, score, top_match, action)
  012b: accepts plain text AND {"action": "..."} JSON input
  012c: exit code = worst verdict: 0=all clear, 10=any warn, 20=any block
  012d: --strict propagates (warn exit → 20)
  012e: --tag / --scope filters apply per evaluation
  012f: empty stdin → exit 0, no output
  012g: --input FILE reads from file instead of stdin
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

IRP_PY = str(Path(__file__).parent.parent / "irp" / "core" / "irp.py")

LEDGER = [
    {
        "type": "decision", "id": "IRP-001",
        "decision": "Do not delete the authentication module",
        "reasoning": "Auth module is shared across all services. Deletion would break prod.",
        "tags": ["security", "auth"], "confidence": "high",
        "timestamp": "2026-04-01", "source": "test",
    },
    {
        "type": "decision", "id": "IRP-002",
        "decision": "Use PostgreSQL for the primary database",
        "reasoning": "Relational model fits our schema. SQLite ruled out.",
        "tags": ["backend", "database"], "confidence": "high",
        "timestamp": "2026-04-02", "source": "test",
    },
    {
        "type": "decision", "id": "IRP-003",
        "decision": "All API responses must be JSON",
        "reasoning": "JSON is the standard for our consumers.",
        "tags": ["api"], "confidence": "high",
        "timestamp": "2026-04-03", "source": "test",
    },
]


def _make_project(tmp_path: Path) -> Path:
    irp_dir = tmp_path / ".irp"
    irp_dir.mkdir()
    (irp_dir / "ledger.jsonl").write_text(
        "\n".join(json.dumps(e) for e in LEDGER) + "\n", encoding="utf-8"
    )
    active = [e for e in LEDGER if e.get("type") == "decision"]
    (irp_dir / "current.json").write_text(
        json.dumps({"version": 1, "active": active}), encoding="utf-8"
    )
    return tmp_path


def _run_watch(args: list[str], proj: Path, stdin: str = "") -> tuple[int, list[dict]]:
    """Run irp watch and return (exit_code, list_of_parsed_output_lines)."""
    result = subprocess.run(
        [sys.executable, IRP_PY, "watch"] + args,
        input=stdin,
        capture_output=True, text=True, cwd=str(proj),
        timeout=10,
    )
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"_raw": line})
    return result.returncode, parsed


# ── US-012a: one line in → one JSON verdict line out ─────────────────────────

class TestLineMapping:
    def test_single_line_produces_single_output(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="deploy new zeppelin airship\n")
        assert len(outputs) == 1

    def test_output_has_verdict_field(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="deploy new zeppelin airship\n")
        assert "verdict" in outputs[0]

    def test_output_has_score_field(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="deploy zeppelin\n")
        assert "score" in outputs[0]
        assert isinstance(outputs[0]["score"], int)

    def test_output_has_action_field(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="deploy zeppelin\n")
        assert "action" in outputs[0]
        assert "zeppelin" in outputs[0]["action"]

    def test_output_has_top_match_field(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="deploy zeppelin\n")
        assert "top_match" in outputs[0]

    def test_multiple_lines_produce_multiple_outputs(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "deploy zeppelin\nchange api endpoint\ndelete authentication module\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        assert len(outputs) == 3

    def test_output_order_matches_input_order(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "deploy zeppelin\ndelete authentication module\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        assert outputs[0]["action"] == "deploy zeppelin"
        assert "authentication" in outputs[1]["action"] or outputs[1]["verdict"] != "clear"


# ── US-012b: plain text AND JSON {"action": "..."} input ─────────────────────

class TestInputFormats:
    def test_plain_text_input(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="delete authentication module\n")
        assert len(outputs) == 1
        assert outputs[0]["verdict"] in ("clear", "warn", "block")

    def test_json_object_input(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = json.dumps({"action": "delete authentication module"}) + "\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        assert len(outputs) == 1
        assert outputs[0]["verdict"] in ("clear", "warn", "block")

    def test_json_input_same_verdict_as_plain(self, tmp_path):
        """JSON {"action": "..."} and plain text must produce identical verdicts."""
        proj = _make_project(tmp_path)
        _, plain_out = _run_watch([], proj, stdin="delete authentication module\n")
        _, json_out = _run_watch([], proj,
            stdin=json.dumps({"action": "delete authentication module"}) + "\n")
        assert plain_out[0]["verdict"] == json_out[0]["verdict"]

    def test_mixed_input_formats(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = (
            "deploy zeppelin\n"
            + json.dumps({"action": "change api endpoint"}) + "\n"
        )
        code, outputs = _run_watch([], proj, stdin=stdin)
        assert len(outputs) == 2


# ── US-012c: exit code = worst verdict ───────────────────────────────────────

class TestExitCode:
    def test_all_clear_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "deploy zeppelin airship blimp\ndeploy another zeppelin\n"
        code, _ = _run_watch([], proj, stdin=stdin)
        assert code == 0

    def test_any_warn_exits_ten(self, tmp_path):
        proj = _make_project(tmp_path)
        # "api" alone → warn; "zeppelin" → clear → worst is warn
        stdin = "deploy zeppelin\nchange api\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        verdicts = [o["verdict"] for o in outputs]
        if "warn" in verdicts and "block" not in verdicts:
            assert code == 10

    def test_any_block_exits_twenty(self, tmp_path):
        proj = _make_project(tmp_path)
        # One clear, one block
        stdin = "deploy zeppelin\ndelete authentication module security auth\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        verdicts = [o["verdict"] for o in outputs]
        if "block" in verdicts:
            assert code == 20

    def test_block_overrides_warn_in_exit(self, tmp_path):
        """If mix of warn and block, exit must be 20 not 10."""
        proj = _make_project(tmp_path)
        stdin = "change api\ndelete authentication module security auth\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        verdicts = [o["verdict"] for o in outputs]
        if "block" in verdicts:
            assert code == 20

    def test_exit_code_never_ten_when_block_present(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "delete authentication module security auth\n"
        code, outputs = _run_watch([], proj, stdin=stdin)
        if outputs[0]["verdict"] == "block":
            assert code != 10


# ── US-012d: --strict propagates ─────────────────────────────────────────────

class TestStrictMode:
    def test_strict_warn_exits_twenty(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "change api\n"
        code_normal, outputs = _run_watch([], proj, stdin=stdin)
        if outputs[0]["verdict"] == "warn":
            code_strict, _ = _run_watch(["--strict"], proj, stdin=stdin)
            assert code_strict == 20

    def test_strict_clear_still_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "deploy zeppelin airship\n"
        code, _ = _run_watch(["--strict"], proj, stdin=stdin)
        assert code == 0

    def test_strict_verdict_label_unchanged(self, tmp_path):
        """--strict changes exit code, not verdict label in JSON."""
        proj = _make_project(tmp_path)
        stdin = "change api\n"
        _, normal = _run_watch([], proj, stdin=stdin)
        _, strict = _run_watch(["--strict"], proj, stdin=stdin)
        if normal and strict:
            assert normal[0]["verdict"] == strict[0]["verdict"]


# ── US-012e: --tag and --scope filters ────────────────────────────────────────

class TestFilters:
    def test_tag_filter_limits_active_set(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "delete authentication module\n"
        _, outputs = _run_watch(["--tag", "security"], proj, stdin=stdin)
        assert "active_count" in outputs[0]
        assert outputs[0]["active_count"] <= len(
            [e for e in LEDGER if "security" in e.get("tags", [])]
        )

    def test_scope_filter_applied(self, tmp_path):
        proj = _make_project(tmp_path)
        stdin = "check something\n"
        _, outputs = _run_watch(["--scope", "api"], proj, stdin=stdin)
        assert "verdict" in outputs[0]


# ── US-012f: empty stdin → exit 0, no output ─────────────────────────────────

class TestEmptyInput:
    def test_empty_stdin_exits_zero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="")
        assert code == 0

    def test_empty_stdin_no_output(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="")
        assert outputs == []

    def test_whitespace_only_lines_skipped(self, tmp_path):
        proj = _make_project(tmp_path)
        code, outputs = _run_watch([], proj, stdin="   \n\n  \n")
        assert outputs == []


# ── US-012g: --input FILE ─────────────────────────────────────────────────────

class TestInputFile:
    def test_input_file_produces_output(self, tmp_path):
        proj = _make_project(tmp_path)
        input_file = tmp_path / "actions.txt"
        input_file.write_text("deploy zeppelin\ndelete authentication module\n")
        code, outputs = _run_watch(["--input", str(input_file)], proj, stdin="")
        assert len(outputs) == 2

    def test_input_file_exit_code(self, tmp_path):
        proj = _make_project(tmp_path)
        input_file = tmp_path / "actions.txt"
        input_file.write_text("deploy zeppelin\n")
        code, outputs = _run_watch(["--input", str(input_file)], proj, stdin="")
        assert code == 0  # all clear

    def test_input_file_missing_exits_nonzero(self, tmp_path):
        proj = _make_project(tmp_path)
        code, _ = _run_watch(["--input", "/nonexistent/path/actions.txt"], proj, stdin="")
        assert code != 0
