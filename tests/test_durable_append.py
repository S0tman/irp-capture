"""Durable ledger appends (0.10.1).

A hard reset used to leave half a line at the end of ledger.jsonl, and the next
capture wrote behind it. These tests pin the repair:

- every append flushes to disk (fsync, and the folder when the file is made)
- a torn tail makes every writer refuse, with a plain message, and changes nothing
- readers keep working on a torn tail
- `irp doctor` reports a torn tail; `irp doctor --fix-tail` moves the torn bytes
  to a side file first and only then truncates, so no byte is ever deleted
- two writers at once never interleave or tear a line

The crash tests cut the file at arbitrary byte offsets (10,000 of them) and kill
a real writer with SIGKILL, then check what the next capture does.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import random
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_kit import DECISION, REPO, ledger_lines, out, run_irp, start_project

import irp.core.store as store

LEDGER = "ledger.jsonl"
TORN_HALF = '{"type": "decision", "id": "IRP-2026-01-02-001", "what": "half'


def _entry(i: int, pad: int = 0) -> dict:
    return {"type": "decision", "id": f"IRP-2026-01-02-{i:03d}", "what": f"Decision number {i} åäö ✓",
            "why": "x" * pad, "confidence": "high", "timestamp": "2026-01-02"}


def _line(entry: dict) -> bytes:
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.fixture
def irp_dir(tmp_path):
    return start_project(tmp_path.resolve() / "proj", entries=(DECISION,))


@pytest.fixture(autouse=True)
def _drop_server_modules():
    yield
    for name in ("irp.api.server", "irp.mcp.server"):
        sys.modules.pop(name, None)


@pytest.fixture
def spy_fsync(monkeypatch):
    """Record every os.fsync as ('dir'|'file', inode) while still calling it."""
    calls: list[tuple[str, int]] = []
    real = os.fsync

    def spy(fd):
        st = os.fstat(fd)
        calls.append(("dir" if stat.S_ISDIR(st.st_mode) else "file", st.st_ino))
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return calls


def _write_torn(irp_dir: Path, name: str = LEDGER, tail: bytes = TORN_HALF.encode()) -> bytes:
    path = irp_dir / name
    with path.open("ab") as f:
        f.write(tail)
    return path.read_bytes()


# ── 1. every append path flushes to disk ─────────────────────────────────────

APPENDERS = {
    "ledger.jsonl": lambda d: store.append_ledger_entry(d, _entry(7)),
    "craft.jsonl": lambda d: store.append_craft_entry(d, {"id": "CRAFT-2026-01-02-001", "what": "x"}),
    "reconstructions.jsonl": lambda d: store.append_reconstruction(d, {"id": "REC-2026-01-02-001", "status": "unconfirmed"}),
}


class TestEveryAppendIsSynced:
    @pytest.mark.parametrize("name", sorted(APPENDERS))
    def test_file_is_fsynced(self, irp_dir, spy_fsync, name):
        APPENDERS[name](irp_dir)
        inode = (irp_dir / name).stat().st_ino
        assert ("file", inode) in spy_fsync

    @pytest.mark.parametrize("name", sorted(APPENDERS))
    def test_folder_is_fsynced_when_the_file_is_created(self, irp_dir, spy_fsync, name):
        target = irp_dir / name
        if target.exists():
            target.unlink()
        APPENDERS[name](irp_dir)
        assert ("dir", irp_dir.stat().st_ino) in spy_fsync

    def test_folder_is_not_synced_again_for_a_later_append(self, irp_dir, spy_fsync):
        store.append_ledger_entry(irp_dir, _entry(5))
        assert ("dir", irp_dir.stat().st_ino) not in spy_fsync

    def test_full_fsync_is_used_where_the_platform_has_it(self, irp_dir, monkeypatch):
        seen = []
        monkeypatch.setattr(store.fcntl, "F_FULLFSYNC", 51, raising=False)
        monkeypatch.setattr(store.fcntl, "fcntl", lambda fd, op, *a: seen.append(op) or 0)
        store.append_ledger_entry(irp_dir, _entry(5))
        assert 51 in seen

    def test_full_fsync_failure_falls_back_to_fsync(self, irp_dir, monkeypatch, spy_fsync):
        def refuse(fd, op, *a):
            raise OSError(errno.ENOTTY, "not supported here")

        monkeypatch.setattr(store.fcntl, "F_FULLFSYNC", 51, raising=False)
        monkeypatch.setattr(store.fcntl, "fcntl", refuse)
        store.append_ledger_entry(irp_dir, _entry(5))
        assert ("file", (irp_dir / LEDGER).stat().st_ino) in spy_fsync

    def test_appended_text_is_exactly_one_json_line(self, irp_dir):
        before = (irp_dir / LEDGER).read_bytes()
        store.append_ledger_entry(irp_dir, _entry(9))
        assert (irp_dir / LEDGER).read_bytes() == before + _line(_entry(9))

    def test_file_mode_matches_what_open_a_would_make(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        old = os.umask(0o022)
        try:
            store.append_ledger_entry(d, _entry(1))
        finally:
            os.umask(old)
        assert stat.S_IMODE((d / LEDGER).stat().st_mode) == 0o644

    def test_reconstruction_rewrite_syncs_the_folder_after_replace(self, irp_dir, spy_fsync):
        store.append_reconstruction(irp_dir, {"id": "REC-2026-01-02-001", "status": "unconfirmed"})
        spy_fsync.clear()
        assert store.update_reconstruction(irp_dir, "REC-2026-01-02-001", {"status": "accepted"})
        assert ("dir", irp_dir.stat().st_ino) in spy_fsync


# ── 2. fault injection ───────────────────────────────────────────────────────

class TestFaults:
    def test_partial_write_then_error_leaves_the_ledger_as_it_was(self, irp_dir, monkeypatch):
        before = (irp_dir / LEDGER).read_bytes()
        real = os.write

        def half_then_fail(fd, data):
            real(fd, bytes(data)[: len(data) // 2])
            raise OSError(errno.EIO, "disk went away")

        monkeypatch.setattr(os, "write", half_then_fail)
        with pytest.raises(OSError):
            store.append_ledger_entry(irp_dir, _entry(3))
        monkeypatch.undo()
        assert (irp_dir / LEDGER).read_bytes() == before
        assert store.torn_tail(irp_dir / LEDGER) is None

    def test_enospc_leaves_the_ledger_as_it_was(self, irp_dir, monkeypatch):
        before = (irp_dir / LEDGER).read_bytes()
        real = os.write
        calls = {"n": 0}

        def full_disk(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(fd, bytes(data)[:10])  # short write
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "write", full_disk)
        with pytest.raises(OSError) as exc:
            store.append_ledger_entry(irp_dir, _entry(3))
        assert exc.value.errno == errno.ENOSPC
        monkeypatch.undo()
        assert (irp_dir / LEDGER).read_bytes() == before

    def test_short_writes_are_completed(self, irp_dir, monkeypatch):
        real = os.write
        monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:7]))
        store.append_ledger_entry(irp_dir, _entry(4))
        monkeypatch.undo()
        assert ledger_lines(irp_dir)[-1] == _entry(4)

    def test_eintr_is_retried(self, irp_dir, monkeypatch):
        real = os.write
        calls = {"n": 0}

        def interrupted_once(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
            return real(fd, data)

        monkeypatch.setattr(os, "write", interrupted_once)
        store.append_ledger_entry(irp_dir, _entry(4))
        monkeypatch.undo()
        assert ledger_lines(irp_dir)[-1] == _entry(4)
        assert len(ledger_lines(irp_dir)) == 2

    def test_failing_fsync_raises_and_says_the_disk_did_not_confirm(self, irp_dir, monkeypatch):
        def broken(fd):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(os, "fsync", broken)
        with pytest.raises(store.DurabilityError) as exc:
            store.append_ledger_entry(irp_dir, _entry(4))
        assert "did not confirm" in str(exc.value)
        assert isinstance(exc.value, OSError)

    def test_capture_command_reports_a_failing_fsync_and_exits_nonzero(self, irp_dir, tmp_path):
        # run the CLI in a child whose fsync always fails
        code = (
            "import os, runpy, sys\n"
            "def bad(fd): raise OSError(5, 'I/O error')\n"
            "os.fsync = bad\n"
            f"sys.argv = ['irp', 'capture', '--stdin']\n"
            f"runpy.run_path({str(REPO / 'irp' / 'core' / 'irp.py')!r}, run_name='__main__')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=str(irp_dir.parent), capture_output=True, text=True,
                           input=json.dumps({"what": "x", "why": "y"}))
        assert r.returncode != 0
        assert "did not confirm" in r.stderr

    def test_failure_to_open_the_file_raises_cleanly(self, irp_dir):
        os.chmod(irp_dir / LEDGER, 0o444)
        try:
            if os.access(irp_dir / LEDGER, os.W_OK):
                pytest.skip("running as a user who ignores file modes")
            with pytest.raises(PermissionError):
                store.append_ledger_entry(irp_dir, _entry(2))
        finally:
            os.chmod(irp_dir / LEDGER, 0o644)


# ── 3. a torn tail refuses every writer, readers keep working ───────────────

class TestTornTailDetection:
    def test_clean_file_is_fine(self, irp_dir):
        assert store.torn_tail(irp_dir / LEDGER) is None

    def test_missing_and_empty_files_are_fine(self, irp_dir):
        assert store.torn_tail(irp_dir / "nope.jsonl") is None
        (irp_dir / "empty.jsonl").write_bytes(b"")
        assert store.torn_tail(irp_dir / "empty.jsonl") is None

    def test_half_line_without_newline(self, irp_dir):
        raw = _write_torn(irp_dir)
        t = store.torn_tail(irp_dir / LEDGER)
        assert t is not None and t.kind == "unterminated"
        assert raw[t.offset:] == TORN_HALF.encode()

    def test_complete_but_invalid_last_line(self, irp_dir):
        raw = _write_torn(irp_dir, tail=b'{"id": nope}\n')
        t = store.torn_tail(irp_dir / LEDGER)
        assert t is not None and t.kind == "invalid"
        assert raw[t.offset:] == b'{"id": nope}\n'

    def test_last_line_that_is_json_but_not_an_object(self, irp_dir):
        _write_torn(irp_dir, tail=b"[1, 2]\n")
        assert store.torn_tail(irp_dir / LEDGER).kind == "invalid"

    def test_cut_inside_a_multibyte_character(self, irp_dir):
        full = _line(_entry(2))
        cut = full.index("å".encode()) + 1  # one byte of a two-byte character
        _write_torn(irp_dir, tail=full[:cut])
        assert store.torn_tail(irp_dir / LEDGER).kind == "unterminated"

    def test_trailing_blank_lines_are_not_a_torn_tail(self, irp_dir):
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(b"\n\n")
        assert store.torn_tail(irp_dir / LEDGER) is None

    def test_a_bad_line_in_the_middle_is_not_a_torn_tail(self, irp_dir):
        raw = (irp_dir / LEDGER).read_bytes()
        (irp_dir / LEDGER).write_bytes(b"not json\n" + raw)
        assert store.torn_tail(irp_dir / LEDGER) is None

    def test_a_very_long_last_line_is_checked_without_reading_the_whole_file(self, irp_dir):
        store.append_ledger_entry(irp_dir, _entry(2, pad=3_000_000))
        assert store.torn_tail(irp_dir / LEDGER) is None
        with (irp_dir / LEDGER).open("ab") as f:
            f.write(_line(_entry(3, pad=3_000_000))[:-9])
        assert store.torn_tail(irp_dir / LEDGER).kind == "unterminated"


class TestWritersRefuse:
    @pytest.mark.parametrize("name", sorted(APPENDERS))
    def test_store_appenders_refuse_and_change_nothing(self, irp_dir, name):
        APPENDERS[name](irp_dir)  # make sure the file exists
        raw = _write_torn(irp_dir, name)
        with pytest.raises(store.TornTailError) as exc:
            APPENDERS[name](irp_dir)
        assert (irp_dir / name).read_bytes() == raw
        msg = str(exc.value)
        assert name in msg and "irp doctor --fix-tail" in msg
        assert isinstance(exc.value, OSError)

    def test_capture_command_refuses_with_a_plain_message(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        raw = _write_torn(d)
        r = run_irp(["capture", "--stdin"], d.parent, input=json.dumps({"what": "A", "why": "B"}))
        assert r.returncode != 0
        assert "half-written" in r.stderr and "irp doctor --fix-tail" in r.stderr
        assert "Traceback" not in r.stderr
        assert (d / LEDGER).read_bytes() == raw

    def test_capture_refuses_before_asking_questions(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        _write_torn(d)
        r = run_irp(["capture"], d.parent, input="")
        assert r.returncode != 0
        assert "What was decided" not in r.stdout + r.stderr
        assert "half-written" in r.stderr

    def test_other_cli_writers_refuse_too(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        raw = _write_torn(d)
        r = run_irp(["mod", "retire", DECISION["id"], "--reason", "test"], d.parent)
        assert r.returncode != 0 and "irp doctor --fix-tail" in r.stderr
        assert (d / LEDGER).read_bytes() == raw

    def test_read_commands_keep_working(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        _write_torn(d)
        for args in (["why"], ["why", "--id", DECISION["id"]], ["check", "use redis instead of postgres"],
                     ["find", "Postgres"], ["inherit"], ["doctor"]):
            r = run_irp(args, d.parent)
            assert r.returncode in (0, 10), (args, r.stdout, r.stderr)
            assert "Traceback" not in r.stderr, args

    def test_readers_skip_the_half_line_even_cut_inside_a_character(self, irp_dir):
        full = _line(_entry(2))
        _write_torn(irp_dir, tail=full[: full.index("å".encode()) + 1])
        assert [r["id"] for r in store.read_ledger(irp_dir)] == [DECISION["id"]]
        (irp_dir / "craft.jsonl").write_bytes(full[: full.index("å".encode()) + 1])
        assert store.read_craft(irp_dir) == []
        (irp_dir / "reconstructions.jsonl").write_bytes(full[: full.index("å".encode()) + 1])
        assert store.read_reconstructions(irp_dir) == []

    def test_discord_writer_syncs_and_refuses(self, irp_dir, spy_fsync):
        from irp.discord_sensor.ledger_writer import LedgerWriter

        w = LedgerWriter(irp_dir / LEDGER)
        w.write_decision("Use X", "Because Y", "someone")
        assert ("file", (irp_dir / LEDGER).stat().st_ino) in spy_fsync
        raw = _write_torn(irp_dir)
        with pytest.raises(IOError) as exc:
            w.write_decision("Use Z", "Because W", "someone")
        assert "irp doctor --fix-tail" in str(exc.value)
        assert (irp_dir / LEDGER).read_bytes() == raw

    def test_rest_api_answers_409_and_changes_nothing(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        pytest.importorskip("uvicorn")
        monkeypatch.setattr(sys, "argv", ["irp-api"])
        d = start_project(tmp_path.resolve() / "p")
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(d.parent))
        sys.modules.pop("irp.api.server", None)  # it reads sys.argv at import: do not leave this copy for other tests
        import irp.api.server as api

        monkeypatch.setattr(api, "_NAMED", True)
        monkeypatch.setattr(api, "PROJECT_ROOT", d.parent)
        ok = api.capture(api.CaptureRequest(what="Fine", why="because"))
        assert ok["ok"] is True
        raw = _write_torn(d)
        with pytest.raises(api.HTTPException) as exc:
            api.capture(api.CaptureRequest(what="Not fine", why="because"))
        assert exc.value.status_code == 409
        assert "irp doctor --fix-tail" in str(exc.value.detail)
        assert (d / LEDGER).read_bytes() == raw

    def test_figma_bridge_answers_409_and_changes_nothing(self, tmp_path, monkeypatch):
        from http.client import HTTPConnection
        from http.server import HTTPServer

        import irp.figma_plugin.bridge.server as bridge

        d = start_project(tmp_path.resolve() / "p")
        raw = _write_torn(d)
        monkeypatch.setattr(bridge, "PROJECT_ROOT", str(d.parent))
        monkeypatch.setattr(bridge, "FIGMA_PAT", "")
        server = HTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/capture", body=json.dumps({"decision": "Add comments"}).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            status, body = resp.status, resp.read().decode()
            conn.close()
        finally:
            server.shutdown()
            t.join(timeout=5)
        assert status == 409
        assert "irp doctor --fix-tail" in body
        assert (d / LEDGER).read_bytes() == raw

    def test_mcp_capture_surfaces_the_refusal(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp.server.fastmcp")
        d = start_project(tmp_path.resolve() / "p")
        raw = _write_torn(d)
        monkeypatch.setenv("IRP_PROJECT_ROOT", str(d.parent))
        sys.modules.pop("irp.mcp.server", None)
        import irp.mcp.server as mcp_server

        with pytest.raises(OSError) as exc:
            mcp_server.irp_capture(what="X", why="Y")
        assert "irp doctor --fix-tail" in str(exc.value)
        assert (d / LEDGER).read_bytes() == raw


# ── 4. doctor reports, doctor --fix-tail quarantines ────────────────────────

class TestDoctor:
    def test_reports_a_torn_tail_and_changes_nothing(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        raw = _write_torn(d)
        r = run_irp(["doctor"], d.parent)
        assert r.returncode == 0
        text = out(r)
        assert "half-written" in text and "irp doctor --fix-tail" in text
        assert (d / LEDGER).read_bytes() == raw
        assert not list(d.glob("ledger.torn-*"))

    def test_json_report_names_the_file_and_size(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        _write_torn(d)
        r = run_irp(["doctor", "--json"], d.parent)
        data = json.loads(r.stdout)
        assert data["torn_tails"] == [{"file": LEDGER, "kind": "unterminated", "bytes": len(TORN_HALF.encode())}]
        led = next(c for c in data["checks"] if c["label"] == "ledger.jsonl")
        assert led["ok"] is False

    def test_clean_ledger_reports_no_torn_tail(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        data = json.loads(run_irp(["doctor", "--json"], d.parent).stdout)
        assert data["torn_tails"] == []
        assert "--fix-tail" not in data["text"]

    def test_fix_tail_moves_the_bytes_aside_then_truncates(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        before = (d / LEDGER).read_bytes()
        _write_torn(d)
        r = run_irp(["doctor", "--fix-tail"], d.parent)
        assert r.returncode == 0, r.stderr
        assert (d / LEDGER).read_bytes() == before
        side = list(d.glob("ledger.torn-*.jsonl"))
        assert len(side) == 1
        assert side[0].read_bytes() == TORN_HALF.encode()
        assert "ledger.torn-" in out(r)

    def test_side_file_name_carries_the_utc_time(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        _write_torn(d)
        run_irp(["doctor", "--fix-tail"], d.parent)
        name = next(d.glob("ledger.torn-*.jsonl")).name
        stamp = name[len("ledger.torn-"):-len(".jsonl")]
        assert time.strptime(stamp[:16], "%Y%m%dT%H%M%SZ")
        assert abs(time.mktime(time.strptime(stamp[:16], "%Y%m%dT%H%M%SZ")) - (time.time() + time.timezone)) < 120

    def test_capture_works_after_fix_tail(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        _write_torn(d)
        assert run_irp(["capture", "--stdin"], d.parent, input=json.dumps({"what": "A", "why": "B"})).returncode != 0
        run_irp(["doctor", "--fix-tail"], d.parent)
        r = run_irp(["capture", "--stdin"], d.parent, input=json.dumps({"what": "A", "why": "B"}))
        assert r.returncode == 0, r.stderr
        assert [e["what"] for e in ledger_lines(d)][-1] == "A"

    def test_fix_tail_on_a_clean_ledger_does_nothing(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        before = (d / LEDGER).read_bytes()
        r = run_irp(["doctor", "--fix-tail"], d.parent)
        assert r.returncode == 0
        assert (d / LEDGER).read_bytes() == before
        assert not list(d.glob("*.torn-*"))

    def test_fix_tail_quarantines_a_complete_but_invalid_last_line(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        before = (d / LEDGER).read_bytes()
        _write_torn(d, tail=b'{"id": nope}\n')
        run_irp(["doctor", "--fix-tail"], d.parent)
        assert (d / LEDGER).read_bytes() == before
        assert next(d.glob("ledger.torn-*.jsonl")).read_bytes() == b'{"id": nope}\n'

    def test_fix_tail_only_adds_a_newline_when_the_last_line_is_whole(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        before = (d / LEDGER).read_bytes()
        (d / LEDGER).write_bytes(before + _line(_entry(2))[:-1])  # whole JSON, newline missing
        run_irp(["doctor", "--fix-tail"], d.parent)
        assert (d / LEDGER).read_bytes() == before + _line(_entry(2))
        assert not list(d.glob("ledger.torn-*"))

    def test_fix_tail_covers_craft_and_reconstructions(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        store.append_craft_entry(d, {"id": "CRAFT-2026-01-02-001"})
        store.append_reconstruction(d, {"id": "REC-2026-01-02-001", "status": "unconfirmed"})
        keep = {n: (d / n).read_bytes() for n in ("craft.jsonl", "reconstructions.jsonl")}
        for n in keep:
            _write_torn(d, n)
        run_irp(["doctor", "--fix-tail"], d.parent)
        for n, raw in keep.items():
            assert (d / n).read_bytes() == raw
            assert next(d.glob(n.replace(".jsonl", ".torn-*.jsonl"))).read_bytes() == TORN_HALF.encode()

    def test_two_fixes_in_the_same_second_keep_both_side_files(self, irp_dir, monkeypatch):
        monkeypatch.setattr(store, "_utc_stamp", lambda: "20260102T030405Z")
        _write_torn(irp_dir)
        store.fix_torn_tail(irp_dir, LEDGER)
        _write_torn(irp_dir, tail=b"{second")
        store.fix_torn_tail(irp_dir, LEDGER)
        sides = sorted(p.read_bytes() for p in irp_dir.glob("ledger.torn-*.jsonl"))
        assert sides == sorted([TORN_HALF.encode(), b"{second"])

    def test_bytes_reach_the_side_file_before_the_ledger_shrinks(self, irp_dir, monkeypatch):
        """Crash between the two steps: the torn bytes must already be safe."""
        raw = _write_torn(irp_dir)

        def boom(*a, **k):
            raise RuntimeError("power cut")

        monkeypatch.setattr(os, "ftruncate", boom)
        with pytest.raises(RuntimeError):
            store.fix_torn_tail(irp_dir, LEDGER)
        side = list(irp_dir.glob("ledger.torn-*.jsonl"))
        assert len(side) == 1 and side[0].read_bytes() == TORN_HALF.encode()
        assert (irp_dir / LEDGER).read_bytes() == raw  # nothing lost, still refusing

    def test_side_file_is_not_overwritten_and_is_private(self, irp_dir, monkeypatch):
        monkeypatch.setattr(store, "_utc_stamp", lambda: "20260102T030405Z")
        (irp_dir / "ledger.torn-20260102T030405Z.jsonl").write_bytes(b"older")
        _write_torn(irp_dir)
        store.fix_torn_tail(irp_dir, LEDGER)
        assert (irp_dir / "ledger.torn-20260102T030405Z.jsonl").read_bytes() == b"older"

    def test_side_files_do_not_confuse_readers(self, irp_dir):
        _write_torn(irp_dir)
        store.fix_torn_tail(irp_dir, LEDGER)
        assert [r["id"] for r in store.read_ledger(irp_dir)] == [DECISION["id"]]


# ── 5. concurrency ───────────────────────────────────────────────────────────

_WORKER = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import irp.core.store as store
d, tag, n, pad = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
for i in range(n):
    store.append_ledger_entry(d, {{"type": "decision", "id": f"{{tag}}-{{i}}", "what": "w", "why": "y" * pad}})
"""


class TestConcurrency:
    def test_processes_never_interleave_or_tear(self, irp_dir):
        procs = [subprocess.Popen([sys.executable, "-c", _WORKER.format(repo=str(REPO)), str(irp_dir), f"P{k}", "40", "9000"])
                 for k in range(6)]
        assert [p.wait(timeout=120) for p in procs] == [0] * 6
        raw = (irp_dir / LEDGER).read_bytes()
        assert raw.endswith(b"\n")
        rows = [json.loads(line) for line in raw.decode().splitlines()]
        ids = [r["id"] for r in rows if r["id"].startswith("P")]
        assert len(ids) == 240 and len(set(ids)) == 240
        for k in range(6):  # each writer's own order is kept
            assert [i for i in ids if i.startswith(f"P{k}-")] == [f"P{k}-{j}" for j in range(40)]

    def test_one_write_call_per_line_so_it_holds_without_the_lock(self, irp_dir, monkeypatch):
        import contextlib

        monkeypatch.setattr(store, "irp_lock", lambda d: contextlib.nullcontext())
        errors = []

        def work(k):
            try:
                for j in range(40):
                    while True:
                        try:
                            store.append_ledger_entry(irp_dir, {"type": "decision", "id": f"T{k}-{j}", "why": "y" * 6000})
                            break
                        except store.TornTailError:
                            time.sleep(0.001)  # without the lock a check can catch a neighbour mid-write: refusing is the safe side
            except Exception as e:  # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=work, args=(k,)) for k in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors
        rows = [json.loads(line) for line in (irp_dir / LEDGER).read_text(encoding="utf-8").splitlines()]
        assert len([r for r in rows if r["id"].startswith("T")]) == 240

    def test_parallel_captures_get_distinct_ids(self, tmp_path):
        d = start_project(tmp_path.resolve() / "p")
        procs = [subprocess.Popen([sys.executable, str(REPO / "irp" / "core" / "irp.py"), "capture", "--stdin"],
                                  cwd=str(d.parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(6)]
        for k, p in enumerate(procs):
            p.stdin.write(json.dumps({"what": f"W{k}", "why": "because"}))
            p.stdin.close()
        for p in procs:
            assert p.wait(timeout=120) == 0, p.stderr.read()
        ids = [e["id"] for e in ledger_lines(d)]
        assert len(ids) == 7 and len(set(ids)) == 7

    def test_reader_never_sees_more_than_a_clean_prefix_while_a_writer_appends(self, irp_dir):
        (irp_dir / LEDGER).write_bytes(b"")
        stop = threading.Event()
        seen = []

        def reader():
            while not stop.is_set():
                seen.append([r["id"] for r in store.read_ledger(irp_dir)])

        t = threading.Thread(target=reader)
        t.start()
        for i in range(150):
            store.append_ledger_entry(irp_dir, _entry(i, pad=20000))
        stop.set()
        t.join()
        full = [f"IRP-2026-01-02-{i:03d}" for i in range(150)]
        assert seen
        for ids in seen:
            assert ids == full[: len(ids)]


# ── 6. crashes ───────────────────────────────────────────────────────────────

_KILL_WRITER = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import irp.core.store as store
d = Path(sys.argv[1])
print("ready", flush=True)
i = 0
while True:
    store.append_ledger_entry(d, {{"type": "decision", "id": f"K-{{i}}", "what": "\\u00e5" * 50, "why": "y" * 400000}})
    i += 1
"""


def _after_a_crash_the_next_capture_is_right_or_refuses(d: Path) -> str:
    path = d / LEDGER
    raw = path.read_bytes()
    complete = raw[: raw.rfind(b"\n") + 1]
    parsed = [json.loads(line) for line in complete.decode("utf-8").splitlines() if line.strip()]
    assert [r["id"] for r in store.read_ledger(d)] == [r.get("id") for r in parsed]
    try:
        store.append_ledger_entry(d, _entry(500))
    except store.TornTailError:
        assert path.read_bytes() == raw
        store.fix_torn_tail(d, LEDGER)
        assert path.read_bytes().startswith(complete)
        store.append_ledger_entry(d, _entry(500))
        outcome = "repaired"
    else:
        outcome = "appended"
    final = path.read_bytes()
    assert final.endswith(_line(_entry(500)))
    for line in final.decode("utf-8").splitlines():
        json.loads(line)
    return outcome


class TestCrashes:
    def test_kill_nine_loop(self, tmp_path):
        rng = random.Random(20261031)
        outcomes = set()
        for round_no in range(12):
            d = start_project(tmp_path.resolve() / f"k{round_no}")
            p = subprocess.Popen([sys.executable, "-c", _KILL_WRITER.format(repo=str(REPO)), str(d)],
                                 stdout=subprocess.PIPE, text=True)
            assert p.stdout.readline().strip() == "ready"
            time.sleep(rng.uniform(0.02, 0.4))
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            outcomes.add(_after_a_crash_the_next_capture_is_right_or_refuses(d))
        assert outcomes <= {"appended", "repaired"}

    def test_ten_thousand_random_crash_points(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_sync_fd", lambda fd: None)  # durability is tested above; this is about bytes
        monkeypatch.setattr(store, "_sync_dir", lambda p: None)
        rng = random.Random(10_000)
        d = start_project(tmp_path.resolve() / "prop", entries=())
        path = d / LEDGER
        repaired = appended = 0
        for n in range(10_000):
            k = rng.randint(0, 4)
            lines = [_line(_entry(i, pad=rng.randint(0, 30))) for i in range(k)]
            image = b"".join(lines)
            cut = rng.randint(0, len(image))
            path.write_bytes(image[:cut])
            for side in d.glob("ledger.torn-*"):
                side.unlink()

            boundaries = {0}
            total = 0
            for ln in lines:
                total += len(ln)
                boundaries.add(total)
            # a line cut only at its line break is whole JSON, which readers still take
            whole = [i for i, b in enumerate(sorted(boundaries)[1:]) if b <= cut + 1]
            expect_ids = [f"IRP-2026-01-02-{i:03d}" for i in whole]

            assert [r["id"] for r in store.read_ledger(d)] == expect_ids, (n, cut)
            torn = store.torn_tail(path)
            assert (torn is None) == (cut in boundaries), (n, cut)

            before = path.read_bytes()
            try:
                store.append_ledger_entry(d, _entry(900))
            except store.TornTailError:
                assert path.read_bytes() == before, n
                store.fix_torn_tail(d, LEDGER)
                kept = path.read_bytes()
                side = list(d.glob("ledger.torn-*.jsonl"))
                # no byte lost: what stayed plus what moved aside is what was there (a bare newline may be added)
                moved = side[0].read_bytes() if side else b""
                assert before.startswith(kept) or kept == before + b"\n", n
                assert (before[len(kept):] == moved) or kept == before + b"\n", n
                store.append_ledger_entry(d, _entry(900))
                repaired += 1
            else:
                assert path.read_bytes() == before + _line(_entry(900)), n
                appended += 1
            rows = [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]
            assert rows[-1]["id"] == "IRP-2026-01-02-900", n
        assert repaired > 1000 and appended > 500  # both outcomes really happened


# ── 7. latency ───────────────────────────────────────────────────────────────

class TestLatency:
    def test_tail_check_does_not_grow_with_the_ledger(self, tmp_path):
        small, big = tmp_path / "small.jsonl", tmp_path / "big.jsonl"
        small.write_bytes(b"".join(_line(_entry(i, 300)) for i in range(512)))
        big.write_bytes(b"".join(_line(_entry(i, 300)) for i in range(40_000)))

        def best(p):
            runs = []
            for _ in range(30):
                t = time.perf_counter()
                store.torn_tail(p)
                runs.append(time.perf_counter() - t)
            return min(runs)

        assert best(big) < max(best(small) * 20, 0.002)

    def test_an_append_on_a_512_entry_ledger_is_quick(self, tmp_path):
        d = tmp_path / "lat"
        d.mkdir()
        (d / LEDGER).write_bytes(b"".join(_line(_entry(i, 300)) for i in range(512)))
        runs = []
        for i in range(20):
            t = time.perf_counter()
            store.append_ledger_entry(d, _entry(1000 + i, 300))
            runs.append(time.perf_counter() - t)
        assert sorted(runs)[len(runs) // 2] < 0.25  # median, generous: fsync cost depends on the disk


# ── 8. nothing bypasses the durable path ─────────────────────────────────────

def _append_opens(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        consts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        consts += [k.value.value for k in node.keywords
                   if k.arg == "mode" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)]
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name in ("open", "fdopen") and any(c.startswith("a") for c in consts):
            yield node
        if name == "open" and any(isinstance(n, ast.Attribute) and n.attr == "O_APPEND" for n in ast.walk(node)):
            yield node


class TestNoBypass:
    def test_only_the_gitignore_helper_opens_a_file_for_plain_append(self):
        offenders = []
        for py in sorted((REPO / "irp").rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            rel = py.relative_to(REPO).as_posix()
            for node in _append_opens(tree):
                enclosing = None
                for fn in ast.walk(tree):
                    if isinstance(fn, ast.FunctionDef) and fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                        enclosing = fn.name
                if (rel, enclosing) not in {("irp/core/store.py", "_ensure_lock_ignored"),
                                            ("irp/core/store.py", "durable_append"),
                                            ("irp/core/store.py", "fix_torn_tail")}:
                    offenders.append(f"{rel}:{node.lineno} in {enclosing}")
        assert offenders == []

    def test_every_line_writer_goes_through_durable_append(self):
        text = (REPO / "irp" / "core" / "store.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        for fname in ("append_ledger_entry", "append_craft_entry", "append_reconstruction"):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fname)
            calls = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in ast.walk(fn) if isinstance(c, ast.Call)}
            assert "durable_append" in calls, fname
        w = (REPO / "irp" / "discord_sensor" / "ledger_writer.py").read_text(encoding="utf-8")
        assert "durable_append" in w


# ── 9. release metadata ──────────────────────────────────────────────────────

class TestRelease:
    def test_version_is_0_10_1(self):
        import re

        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(r'^version = "0\.10\.1"$', text, re.M)

    def test_readme_carries_the_upgrade_note(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        assert "0.10.1" in text and "irp doctor --fix-tail" in text
        assert "—" not in text[text.index("0.10.1") - 200: text.index("0.10.1") + 1500]
