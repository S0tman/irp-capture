"""One ledger, every agent's instruction file.

`irp export context` writes the same decision-derived context in the format
each tool reads: AGENTS.md, CLAUDE.md, a Cursor rule and GitHub Copilot's
repository instructions. Agent files carry only the active decisions: a
superseded or retired decision never reaches an agent as a current rule.
DECISIONS.md stays the full human history.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import irp  # noqa: E402,F401
from store import append_ledger_entry, ensure_irp_dir  # noqa: E402
from commands.export import AGENT_TARGETS, run_export  # noqa: E402
from irp.core.irp import build_parser  # noqa: E402


class _Args:
    def __init__(self, **kwargs):
        defaults = dict(export_action="context", output=None, force=False,
                        writable=True, demo=False, target=None, json=False)
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


REST = {"id": "IRP-2026-01-10-001", "type": "decision", "what": "Use REST for internal services",
        "why": "Simple", "timestamp": "2026-01-10", "source": "cli"}
GRPC = {"id": "IRP-2026-02-10-001", "type": "decision", "what": "Use gRPC for internal services",
        "why": "Latency", "timestamp": "2026-02-10", "source": "cli", "supersedes": REST["id"]}
PG = {"id": "IRP-2026-03-10-001", "type": "decision", "what": "Use PostgreSQL for the primary database",
      "why": "Relational fit", "timestamp": "2026-03-10", "source": "cli"}
OLD = {"id": "IRP-2026-03-20-001", "type": "decision", "what": "Deploy on Fridays",
       "why": "Quiet day", "timestamp": "2026-03-20", "source": "cli"}
RETIRE = {"type": "retirement", "id": OLD["id"], "why": "No longer applies"}


def _export(tmp_path, target, entries=(REST, GRPC, PG, OLD, RETIRE)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    irp_dir = ensure_irp_dir(tmp_path)
    for e in entries:
        append_ledger_entry(irp_dir, e)
    result = run_export(tmp_path, irp_dir, _Args(target=target))
    return result, Path(result["output_path"]).read_text(encoding="utf-8")


def _constraints(text):
    start = text.index("## Working constraints")
    return text[start:text.index("## Relevant decisions")]


def test_each_target_writes_where_its_tool_reads():
    expected = {
        "agents.md": "AGENTS.md",
        "claude.md": "CLAUDE.md",
        "cursor": ".cursor/rules/irp-decisions.mdc",
        "copilot": ".github/copilot-instructions.md",
    }
    for target, rel in expected.items():
        assert AGENT_TARGETS[target]["path"] == rel


def test_claude_target(tmp_path):
    result, text = _export(tmp_path, "claude.md")
    assert result["status"] == "ok"
    assert Path(result["output_path"]) == tmp_path / "CLAUDE.md"
    assert text.startswith("# CLAUDE.md")
    assert "irp export context --target claude.md" in text


def test_cursor_target_has_rule_front_matter(tmp_path):
    result, text = _export(tmp_path, "cursor")
    assert Path(result["output_path"]) == tmp_path / ".cursor" / "rules" / "irp-decisions.mdc"
    assert text.startswith("---\n")
    head = text.split("---\n")[1]
    assert "alwaysApply: true" in head
    assert "description:" in head


def test_copilot_target(tmp_path):
    result, text = _export(tmp_path, "copilot")
    assert Path(result["output_path"]) == tmp_path / ".github" / "copilot-instructions.md"
    assert "irp export context --target copilot" in text


def test_every_agent_target_carries_the_same_rules(tmp_path):
    rules = set()
    for i, target in enumerate(AGENT_TARGETS):
        _, text = _export(tmp_path / str(i), target)
        rules.add(_constraints(text))
    assert len(rules) == 1


def test_superseded_decision_never_reaches_an_agent_file(tmp_path):
    for i, target in enumerate(AGENT_TARGETS):
        _, text = _export(tmp_path / str(i), target)
        assert "Use gRPC for internal services" in text
        assert "Use REST for internal services" not in text


def test_retired_decision_never_reaches_an_agent_file(tmp_path):
    _, text = _export(tmp_path, "agents.md")
    assert "Deploy on Fridays" not in text


def test_agent_file_counts_active_decisions(tmp_path):
    result, text = _export(tmp_path, "agents.md")
    assert result["decision_count"] == 2
    assert "2 active decision(s)" in text


def test_decisions_md_keeps_the_full_history(tmp_path):
    _, text = _export(tmp_path, "decisions.md")
    assert "Use REST for internal services" in text
    assert "Use gRPC for internal services" in text


def test_cli_accepts_the_new_targets():
    parser = build_parser()
    for target in ("claude.md", "cursor", "copilot"):
        args = parser.parse_args(["export", "context", "--target", target])
        assert args.target == target


def test_unsupported_target_lists_every_supported_one(tmp_path):
    irp_dir = ensure_irp_dir(tmp_path)
    result = run_export(tmp_path, irp_dir, _Args(target="bogus"))
    assert result["status"] == "error"
    for target in ("agents.md", "claude.md", "cursor", "copilot", "decisions.md"):
        assert target in result["text"]
