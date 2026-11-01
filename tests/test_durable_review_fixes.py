"""Review fixes for 0.10.1: plugin message, doctor status, atomic current.json, directory full-sync,
legacy glued damage note, whitespace tails, side-file ignore, README honesty."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from project_kit import DECISION, REPO, out, run_irp, start_project

import irp.core.store as store

LEDGER = "ledger.jsonl"


@pytest.fixture
def irp_dir(tmp_path):
    return start_project(tmp_path.resolve() / "proj", entries=(DECISION,))


# 1. Figma plugin shows the bridge's own message
class TestPluginShowsBridgeMessage:
    UI = (REPO / "irp/figma_plugin/ui.html").read_text(encoding="utf-8")
    CODE = (REPO / "irp/figma_plugin/code.js").read_text(encoding="utf-8")

    def test_ui_reads_the_error_body_of_a_refusal(self):
        assert "res.json()" in self.UI and ".error" in self.UI

    def test_unreachable_text_only_in_the_network_failure_path(self):
        # a Bridge-returned-status branch must not fall into the "not reachable" label
        assert "BridgeRefused" in self.UI or "bridgeMessage" in self.UI

    def test_code_js_forwards_the_message(self):
        assert "msg.message" in self.CODE


# 2. doctor status reflects torn craft / reconstructions
class TestDoctorStatus:
    @pytest.mark.parametrize("name", ["craft.jsonl", "reconstructions.jsonl"])
    def test_status_not_ok_with_torn_side_file(self, irp_dir, name):
        with (irp_dir / name).open("ab") as f:
            f.write(b'{"id": "x", "wha')
        data = json.loads(run_irp(["doctor", "--json"], irp_dir.parent).stdout)
        assert data["status"] == "issues_found"
        assert data["torn_tails"][0]["file"] == name

    def test_status_expression_counts_torn_files(self):
        src = (REPO / "irp/core/commands/doctor.py").read_text(encoding="utf-8")
        assert re.search(r"all_ok\s*=\s*len\(core_failed\) == 0 and not torn_files", src)

    def test_clean_is_ok(self, irp_dir):
        data = json.loads(run_irp(["doctor", "--json"], irp_dir.parent).stdout)
        assert not data["torn_tails"]


# 3. write_current: temp + fsync + replace + folder fsync
class TestWriteCurrentDurable:
    def test_replace_and_syncs(self, irp_dir, monkeypatch):
        calls = []
        # portable: on macOS F_FULLFSYNC would otherwise replace plain fsync
        monkeypatch.setattr(store, "fcntl", None)
        real_replace, real_fsync = os.replace, os.fsync
        monkeypatch.setattr(os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
        monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
        store.write_current(irp_dir, {"version": 1, "active": []})
        assert calls.count("replace") == 1
        assert calls[0] == "fsync" and calls.index("replace") > 0 and calls[-1] == "fsync"
        assert json.loads((irp_dir / "current.json").read_text()) == {"version": 1, "active": []}
        assert not [p for p in irp_dir.iterdir() if p.name.startswith("current.json.")]

    def test_failure_leaves_old_file(self, irp_dir, monkeypatch):
        before = (irp_dir / "current.json").read_bytes()
        monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError):
            store.write_current(irp_dir, {"version": 1, "active": [{"x": 1}]})
        assert (irp_dir / "current.json").read_bytes() == before
        assert not [p for p in irp_dir.iterdir() if p.name.startswith("current.json.")]


# 4. _sync_dir uses F_FULLFSYNC where available
def test_sync_dir_prefers_full_fsync(irp_dir, monkeypatch):
    seen = []

    class FakeFcntl:
        F_FULLFSYNC = 51

        @staticmethod
        def fcntl(fd, op, *a):
            seen.append(op)
            return 0

    monkeypatch.setattr(store, "fcntl", FakeFcntl)
    store._sync_dir(irp_dir)
    assert seen == [51]


def test_sync_dir_falls_back_to_fsync(irp_dir, monkeypatch):
    class FakeFcntl:
        F_FULLFSYNC = 51

        @staticmethod
        def fcntl(fd, op, *a):
            raise OSError(25, "no")

    fs = []
    monkeypatch.setattr(store, "fcntl", FakeFcntl)
    monkeypatch.setattr(os, "fsync", lambda fd: fs.append(fd))
    store._sync_dir(irp_dir)
    assert len(fs) == 1


# 5. glued legacy damage
class TestGluedLegacy:
    GLUED = b'{"type": "decision", "what": "ha' + b'{"type": "decision", "id": "IRP-2026-01-02-009", "what": "whole"}' + b"\n"

    def test_fix_tail_flags_possible_whole_record(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(self.GLUED)
        res = store.fix_torn_tail(irp_dir)
        assert res["status"] == "quarantined" and res["may_hold_record"] is True
        side = irp_dir / res["side_file"]
        assert side.read_bytes() == self.GLUED

    def test_plain_half_line_not_flagged(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(b'{"type": "decision", "wh')
        assert store.fix_torn_tail(irp_dir)["may_hold_record"] is False

    def test_doctor_text_says_so(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(self.GLUED)
        r = run_irp(["doctor", "--fix-tail"], irp_dir.parent)
        assert "whole record" in out(r)


# 6. whitespace-only tails are blank
class TestWhitespaceTail:
    @pytest.mark.parametrize("tail", [b"  ", b"\t", b"\r", b" \r"])
    def test_not_torn(self, irp_dir, tail):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(tail)
        assert store.torn_tail(irp_dir / LEDGER) is None
        store.append_ledger_entry(irp_dir, {"type": "decision", "id": "IRP-2026-01-02-010", "what": "w"})
        assert store.torn_tail(irp_dir / LEDGER) is None

    def test_whitespace_only_file(self, tmp_path):
        p = tmp_path / "x.jsonl"
        p.write_bytes(b"   ")
        assert store.torn_tail(p) is None

    def test_half_line_then_spaces_is_still_torn(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(b'{"a": 1\n{"b": 2  ')
        assert store.torn_tail(irp_dir / LEDGER) is not None


# 7. side file ignored by git; README does not over-claim
class TestIgnoreAndDocs:
    def test_side_file_is_gitignored_after_fix(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(b'{"half')
        store.fix_torn_tail(irp_dir)
        lines = (irp_dir / ".gitignore").read_text().splitlines()
        assert "*.torn-*.jsonl" in lines and ".lock" in lines

    README = (REPO / "README.md").read_text(encoding="utf-8")

    def test_readme_no_blanket_atomic_claim(self):
        note = self.README[self.README.index("Upgrading from 0.10.0 (0.10.1)"):]
        note = note[: note.index("\n---")]
        assert "can't mix" not in note
        for word in ("network", ".gitignore", "side file", "whole record"):
            assert word in note

    def test_store_comment_does_not_claim_interleave_safety(self):
        src = (REPO / "irp/core/store.py").read_text(encoding="utf-8")
        assert "cannot\n#   interleave" not in src and "even where flock is missing" not in src
