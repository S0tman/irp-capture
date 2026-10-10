"""Roaming IRP, Cut 1 step 2.6: state.json's checkpoint fields and the Time Machine-excluded folders
(spec v0.3 §14.4, §14.6a, §18a "What this changes elsewhere" and "State").

`state.json` moves to `ledgers/<id>/local/state.json` and `tsa.json` to `~/.irp-roam/local/tsa.json`. These
`local/` folders and `staging/` are excluded from Time Machine as folders, so a restore leaves the state missing
rather than stale. Whichever writer creates one applies the exclusion first, through an injected
`exclude_from_backup` (a fake here: the real `tmutil` never runs in these tests), and refuses to write into it
if that fails. `state.json` gains three closed fields, `checkpoint`, `seen` and `epoch_start`, and every writer
reloads it under the exclusive lock and replaces only its own keys. `seen` never goes down. An owner-approved
amendment adds a fourth, `fresh_keys_owed` (null, or `{since, from_idx}` while irp roam rotate --now owes fresh
keys), with its own closed schema; a writer that doesn't name it keeps it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import sig  # noqa: E402
from irp.roam import state as S  # noqa: E402
from irp.roam.age import Identity  # noqa: E402
from irp.roam.keys import RoamLock  # noqa: E402

LEDGER = "ILID-" + "a1" * 16
STRAND = "dk-" + "3c" * 16
STRAND_2 = "dk-" + "4d" * 16


def h(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def digest(data: bytes) -> str:
    return "sha256-" + hashlib.sha256(data).hexdigest()


class FakeTM:
    """Stands in for `tmutil addexclusion` plus `isexcluded`. Records the folder it was given, its inode and
    what was in it at that moment. A folder exclusion is sticky (it moves with the folder), so the inode is what
    counts. The real tmutil never runs in these tests."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[Path, int, list[str]]] = []
        self.fail = fail

    def __call__(self, path) -> None:
        path = Path(path)
        st = os.lstat(path)
        assert stat.S_ISDIR(st.st_mode)
        self.calls.append((path, st.st_ino, sorted(os.listdir(path))))
        if self.fail:
            raise RuntimeError("tmutil isexcluded says the folder is still included")

    def excluded(self, path) -> bool:
        return not self.fail and os.lstat(path).st_ino in {ino for _, ino, _ in self.calls}


def _header(epoch: int = 0, seq: int = 3, strand: str = STRAND) -> bytes:
    return canonicalize({"v": 1, "kind": "checkpoint", "ledger_id": LEDGER, "root": "rt-" + "5e" * 16,
                         "epoch": epoch, "strand": strand, "seq": seq, "prev": None,
                         "created_at": "2026-10-09T09:00:00Z",
                         "devices": {"byte_length": 1234, "digest": digest(b"devices")},
                         "body_digest": digest(b"body")})


def _log(n: int, chunks: int = 1) -> dict:
    segs, off = [], 0
    for i in range(chunks):
        length = n // chunks if i < chunks - 1 else n - off
        segs.append({"object": "o/" + hashlib.sha256(b"seg %d %d" % (n, i)).hexdigest(), "offset": off,
                     "length": length, "sha256": hashlib.sha256(b"plain %d %d" % (n, i)).hexdigest()})
        off += length
    return {"byte_length": n, "byte_digest": digest(b"log %d" % n), "segments": segs}


def _recipients() -> list:
    out = [{"id": STRAND, "recipient": Identity(h("box")).recipient().to_string()},
           {"id": "rk", "recipient": Identity(h("rk")).recipient().to_string()}]
    return sorted(out, key=lambda r: r["id"])


def checkpoint(epoch: int = 0, seq: int = 3, strand: str = STRAND, label: str = "PRESENT", **kw) -> dict:
    header = _header(epoch, seq, strand)
    logs = {"ledger": _log(5000, 2), "devices": _log(1234), "readers": _log(900), "disclosures": _log(0)}
    gen = "2026-10-09T09:00:05Z" if label != "NONE" else None
    rec = {"epoch": epoch, "seq": seq, "strand": strand, "digest": digest(header),
           "header": sig.b64url_encode(header),
           "sig": sig.b64url_encode(canonicalize({"alg": "ed25519", "key_id": strand,
                                                   "sig": sig.b64url_encode(b"\x07" * 64)})),
           "created_at": "2026-10-09T09:00:00Z", "gen_time": gen, "label": label,
           "last_present": {"epoch": epoch, "gen_time": "2026-10-09T09:00:05Z", "devices_length": 1234,
                            "readers_length": 900} if label == "PRESENT" else None,
           "snapshot_digest": hashlib.sha256(b"snapshot").hexdigest(), "recipients": _recipients(),
           "policy_digest": None, "tsa_policy_digest": digest(b"tsa.json"), "logs": logs}
    rec.update(kw)
    return rec


def seen(epoch: int = 0, seq: int = 3, strand: str = STRAND) -> dict:
    header = _header(epoch, seq, strand)
    return {"epoch": epoch, "seq": seq, "strand": strand, "digest": digest(header),
            "header": sig.b64url_encode(header), "sig": sig.b64url_encode(b'{"alg":"ed25519"}'),
            "devices": sig.b64url_encode(b'{"body":{},"sigs":[]}\n')}


@pytest.fixture()
def home(tmp_path):
    return tmp_path


def _lock(home: Path, *, exclusive: bool = True) -> RoamLock:
    return RoamLock(home / "keys", exclusive=exclusive, interactive=False)


def _state_path(home: Path) -> Path:
    return S.state_path(home / "ledgers" / LEDGER)


# ── Where the files live ──

def test_state_and_tsa_json_live_in_local_folders(tmp_path):
    ledger_dir = tmp_path / "ledgers" / LEDGER
    assert S.state_path(ledger_dir) == ledger_dir / "local" / "state.json"
    assert S.staging_dir(ledger_dir) == ledger_dir / "staging"
    assert S.tsa_path(tmp_path) == tmp_path / "local" / "tsa.json"
    assert S.roam_home() == Path.home() / ".irp-roam"
    assert S.tsa_path() == Path.home() / ".irp-roam" / "local" / "tsa.json"


def test_a_fresh_state_has_every_new_field_null(home):
    st = S.load_state(_state_path(home))
    assert (st.checkpoint, st.seen, st.epoch_start) == (None, None, None)
    with pytest.raises(S.StateMissing, match="missing"):
        S.load_state(_state_path(home), required=True)


# ── The closed schema ──

def test_every_field_round_trips_as_exact_jcs(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        st = S.update_state(path, lock, exclude_from_backup=tm, checkpoint=checkpoint(), seen=seen(seq=2),
                            epoch_start=0, fresh_keys_owed=OWED)
    assert S.load_state(path) == st
    assert st.checkpoint == checkpoint() and st.seen == seen(seq=2) and st.epoch_start == 0
    assert st.fresh_keys_owed == OWED
    data = path.read_bytes()
    assert data == canonicalize(sig.load_jcs(data, "state"))
    assert set(sig.load_jcs(data, "state")) == {"rotation", "probes", "held", "checkpoint", "seen", "epoch_start",
                                                "fresh_keys_owed"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("label", ["PRESENT", "UNVERIFIED", "NONE"])
def test_each_label_is_accepted(home, label):
    rec = checkpoint(label=label)
    if label == "UNVERIFIED":  # carried over from the newest PRESENT one in the epoch
        rec["last_present"] = {"epoch": 0, "gen_time": "2026-10-08T09:00:00Z", "devices_length": 1000,
                               "readers_length": 800}
    st = S.RoamState(checkpoint=rec)
    path = home / "state.json"
    S.save_state(path, st)
    assert S.load_state(path).checkpoint == rec


def _mutate(c, *path_and_value):
    *keys, last, value = path_and_value
    for k in keys:
        c = c[k]
    if value is _POP:
        c.pop(last)
    else:
        c[last] = value


_POP = object()

CHECKPOINT_MUTATIONS = {
    "extra key": ("x", 1),
    "missing logs": ("logs", _POP),
    "epoch negative": ("epoch", -1),
    "epoch bool": ("epoch", False),
    "epoch too big": ("epoch", 2**53),
    "seq zero": ("seq", 0),
    "seq text": ("seq", "3"),
    "strand not dk": ("strand", "ck-" + "3c" * 16),
    "strand short": ("strand", "dk-3c"),
    "digest bare hex": ("digest", hashlib.sha256(b"x").hexdigest()),
    "digest not the header's": ("digest", digest(b"another header")),
    "header padded": ("header", sig.b64url_encode(b"abcd") + "=="),
    "header empty": ("header", ""),
    "sig not b64url": ("sig", "a+b/"),
    "created_at with offset": ("created_at", "2026-10-09T09:00:00+00:00"),
    "created_at not real": ("created_at", "2026-02-30T09:00:00Z"),
    "gen_time not a time": ("gen_time", "soon"),
    "label lowercase": ("label", "present"),
    "label qualified": ("label", "QUALIFIED"),
    "present without gen_time": ("gen_time", None),
    "present without last_present": ("last_present", None),
    "last_present extra key": ("last_present", "x", 1),
    "last_present other epoch": ("last_present", "epoch", 1),
    "last_present not this gen_time": ("last_present", "gen_time", "2026-10-09T09:00:06Z"),
    "last_present devices length off": ("last_present", "devices_length", 1233),
    "last_present readers length negative": ("last_present", "readers_length", -1),
    "snapshot_digest prefixed": ("snapshot_digest", digest(b"snapshot")),
    "snapshot_digest upper": ("snapshot_digest", hashlib.sha256(b"snapshot").hexdigest().upper()),
    "recipients unsorted": ("recipients", list(reversed(_recipients()))),
    "recipients without rk": ("recipients", [r for r in _recipients() if r["id"] != "rk"]),
    "recipients duplicate id": ("recipients", _recipients() + [_recipients()[0]]),
    "recipient bad id": ("recipients", 0, "id", "rd-" + "1" * 32),
    "recipient not age1": ("recipients", 0, "recipient", "AGE-SECRET-KEY-1QQQ"),
    "recipient extra key": ("recipients", 0, "role", "device"),
    "policy_digest bare": ("policy_digest", hashlib.sha256(b"p").hexdigest()),
    "tsa_policy_digest number": ("tsa_policy_digest", 7),
    "logs missing disclosures": ("logs", "disclosures", _POP),
    "logs extra log": ("logs", "outbox", _log(3)),
    "log with append_only": ("logs", "ledger", "append_only", True),
    "log byte_digest bare": ("logs", "devices", "byte_digest", hashlib.sha256(b"d").hexdigest()),
    "log byte_length bool": ("logs", "readers", "byte_length", True),
    "segments not contiguous": ("logs", "ledger", "segments", 1, "offset", 2501),
    "segments short of the length": ("logs", "disclosures", "byte_length", 1),
    "segment object not o/": ("logs", "ledger", "segments", 0, "object", "m/" + "0" * 64),
    "segment sha256 prefixed": ("logs", "ledger", "segments", 0, "sha256", digest(b"plain")),
    "segment extra key": ("logs", "ledger", "segments", 0, "kind", "delta"),
    "segment negative length": ("logs", "readers", "segments", 0, "length", -1),
}


@pytest.mark.parametrize("name", sorted(CHECKPOINT_MUTATIONS))
def test_the_checkpoint_record_is_a_closed_schema(home, name):
    path = home / "state.json"
    S.save_state(path, S.RoamState(checkpoint=checkpoint()))
    c = sig.load_jcs(path.read_bytes(), "state")
    _mutate(c["checkpoint"], *CHECKPOINT_MUTATIONS[name])
    with pytest.raises(S.StateError):
        S.check_checkpoint(c["checkpoint"])
    if name == "epoch too big":  # JCS can't write it; hand-write the bytes
        data = path.read_bytes().replace(b'"epoch":0,"gen_time"', b'"epoch":9007199254740992,"gen_time"')
        assert data != path.read_bytes()
    else:
        data = canonicalize(c)
    path.write_bytes(data)
    with pytest.raises(S.StateError):
        S.load_state(path)


def test_a_none_label_carries_no_gen_time(home):
    with pytest.raises(S.StateError):
        S.check_checkpoint(checkpoint(label="NONE", gen_time="2026-10-09T09:00:05Z"))


@pytest.mark.parametrize("label", ["UNVERIFIED", "NONE"])
def test_a_carried_last_present_is_from_the_records_own_epoch(home, label):
    """A record that isn't PRESENT carries last_present from the newest PRESENT one in its own epoch only: it's
    reset at a new epoch, so one from another epoch is refused."""
    lp = {"epoch": 1, "gen_time": "2026-10-08T09:00:00Z", "devices_length": 1000, "readers_length": 800}
    S.check_checkpoint(checkpoint(epoch=1, label=label, last_present=lp))
    with pytest.raises(S.StateError, match="epoch"):
        S.check_checkpoint(checkpoint(epoch=1, label=label, last_present=dict(lp, epoch=0)))


SEEN_MUTATIONS = {
    "extra key": ("x", 1),
    "missing devices": ("devices", _POP),
    "devices empty": ("devices", ""),
    "devices padded": ("devices", sig.b64url_encode(b"ab") + "="),
    "digest not the header's": ("digest", digest(b"x")),
    "seq zero": ("seq", 0),
    "strand bad": ("strand", "dk-" + "G" * 32),
}


@pytest.mark.parametrize("name", sorted(SEEN_MUTATIONS))
def test_the_seen_mark_is_a_closed_schema(home, name):
    path = home / "state.json"
    S.save_state(path, S.RoamState(seen=seen()))
    c = sig.load_jcs(path.read_bytes(), "state")
    _mutate(c["seen"], *SEEN_MUTATIONS[name])
    path.write_bytes(canonicalize(c))
    with pytest.raises(S.StateError):
        S.load_state(path)


@pytest.mark.parametrize("value", [-1, True, "0", 2**53, 1.0])
def test_epoch_start_is_null_or_an_epoch(home, value):
    path = home / "state.json"
    S.save_state(path, S.RoamState(epoch_start=4))
    assert S.load_state(path).epoch_start == 4
    with pytest.raises(S.StateError):
        S.save_state(path, S.RoamState(epoch_start=value))
    if isinstance(value, float) or value == 2**53:
        data = path.read_bytes().replace(b'"epoch_start":4', b'"epoch_start":' + str(value).encode())
    else:
        c = sig.load_jcs(path.read_bytes(), "state")
        c["epoch_start"] = value
        data = canonicalize(c)
    path.write_bytes(data)
    with pytest.raises(S.StateError):
        S.load_state(path)


# ── fresh_keys_owed: the --now marker (owner-approved amendment to §14.6a and §18a) ──

OWED = {"since": "2026-10-09T09:00:00Z", "from_idx": 7}
OWED_BAD = {
    "extra key": dict(OWED, extra=1),
    "missing since": {"from_idx": 7},
    "missing from_idx": {"since": OWED["since"]},
    "since not a timestamp": dict(OWED, since="yesterday"),
    "since not a real time": dict(OWED, since="2026-13-01T00:00:00Z"),
    "from_idx negative": dict(OWED, from_idx=-1),
    "from_idx bool": dict(OWED, from_idx=True),
    "from_idx past 2^53 - 1": dict(OWED, from_idx=2**53),
    "from_idx text": dict(OWED, from_idx="7"),
    "true": True,
    "false": False,
    "a list": [OWED],
    "text": "owed",
}


def test_fresh_keys_owed_is_null_by_default_and_round_trips(home):
    assert S.RoamState().fresh_keys_owed is None
    assert S.load_state(_state_path(home)).fresh_keys_owed is None  # a missing file is an empty state
    path = home / "state.json"
    S.save_state(path, S.RoamState(fresh_keys_owed=OWED))
    assert S.load_state(path).fresh_keys_owed == OWED
    data = path.read_bytes()
    assert data == canonicalize(sig.load_jcs(data, "state")) and sig.load_jcs(data, "state")["fresh_keys_owed"] == OWED


@pytest.mark.parametrize("name", sorted(OWED_BAD))
def test_fresh_keys_owed_is_a_closed_schema(home, name):
    path = home / "state.json"
    with pytest.raises(S.StateError):
        S.save_state(path, S.RoamState(fresh_keys_owed=OWED_BAD[name]))
    S.save_state(path, S.RoamState(fresh_keys_owed=OWED))
    if name == "from_idx past 2^53 - 1":  # JCS itself can't write it, so the bytes are edited
        data = path.read_bytes().replace(b'"from_idx":7', b'"from_idx":%d' % 2**53)
    else:
        c = sig.load_jcs(path.read_bytes(), "state")
        c["fresh_keys_owed"] = OWED_BAD[name]
        data = canonicalize(c)
    path.write_bytes(data)
    with pytest.raises(S.StateError):
        S.load_state(path)


def test_a_writer_that_doesnt_name_the_marker_keeps_it(home):
    path, tm = _state_path(home), FakeTM()
    owed = dict(OWED)
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, fresh_keys_owed=owed)
        owed["from_idx"] = 99  # the caller's dict is copied, never shared
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        S.update_state(path, lock, exclude_from_backup=tm, checkpoint=checkpoint())
        st = S.update_state(path, lock, exclude_from_backup=tm, seen=seen(seq=2))
        assert st.fresh_keys_owed == OWED and S.load_state(path).fresh_keys_owed == OWED
        st = S.update_state(path, lock, exclude_from_backup=tm, fresh_keys_owed=None)
    assert st.fresh_keys_owed is None and S.load_state(path) == st and st.checkpoint == checkpoint()


def test_a_state_json_from_before_step_2_6_is_refused(home):
    path = home / "state.json"
    path.write_bytes(canonicalize({"rotation": None, "probes": [], "held": []}))
    os.chmod(path, 0o600)
    with pytest.raises(S.StateError):
        S.load_state(path)


# ── Every writer reloads and replaces only its own keys ──

def test_update_needs_roam_lock_held_exclusively(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home, exclusive=False) as shared:
        with pytest.raises(S.StateError, match="exclusively"):
            S.update_state(path, shared, exclude_from_backup=tm, epoch_start=0)
    unheld = _lock(home)
    with pytest.raises(S.StateError, match="exclusively"):
        S.update_state(path, unheld, exclude_from_backup=tm, epoch_start=0)
    assert not path.parent.exists() and tm.calls == []


def test_update_refuses_a_key_that_isnt_in_the_schema(home):
    with _lock(home) as lock:
        with pytest.raises(S.StateError, match="unknown"):
            S.update_state(_state_path(home), lock, exclude_from_backup=FakeTM(), seq=4)


def test_each_writer_replaces_only_its_own_keys(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        stale = S.load_state(path)  # a writer that loaded before the others wrote
        S.update_state(path, lock, exclude_from_backup=tm, checkpoint=checkpoint())  # making
        S.update_state(path, lock, exclude_from_backup=tm, seen=seen(seq=2))  # a fetch path
        rid = "rd-" + "ab" * 16
        st = S.update_state(path, lock, exclude_from_backup=tm, held=stale.held + (rid,))  # the rotation engine
    assert st == S.load_state(path)
    assert st.checkpoint == checkpoint() and st.seen == seen(seq=2) and st.epoch_start == 0 and st.held == (rid,)


def test_update_over_a_missing_file_starts_from_an_empty_state(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        st = S.update_state(path, lock, exclude_from_backup=tm, held=("rd-" + "ab" * 16,))
    assert (st.rotation, st.probes, st.checkpoint, st.seen, st.epoch_start) == (None, (), None, None, None)


def test_a_required_update_never_writes_a_fresh_state_or_remakes_a_missing_folder(home):
    """A writer that trusted the file it loaded earlier (making a checkpoint) refuses a file or a local/ folder
    gone since, instead of writing a fresh state with every other key lost."""
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        with pytest.raises(S.StateMissing):
            S.update_state(path, lock, exclude_from_backup=tm, required=True, checkpoint=checkpoint())
        assert not path.parent.exists() and tm.calls == []
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        st = S.update_state(path, lock, exclude_from_backup=tm, required=True, checkpoint=checkpoint())
        assert st.checkpoint == checkpoint() and st.epoch_start == 0
        path.unlink()
        with pytest.raises(S.StateMissing):
            S.update_state(path, lock, exclude_from_backup=tm, required=True, checkpoint=checkpoint())
        assert not path.exists()


def test_seen_never_goes_down(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, seen=seen(epoch=1, seq=5))
        before = path.read_bytes()
        for lower in (seen(epoch=1, seq=4), seen(epoch=0, seq=9)):
            with pytest.raises(S.StateError, match="never goes down"):
                S.update_state(path, lock, exclude_from_backup=tm, seen=lower)
        with pytest.raises(S.StateError, match="never goes down"):
            S.update_state(path, lock, exclude_from_backup=tm, seen=None)
        with pytest.raises(S.StateError, match="different digest"):
            S.update_state(path, lock, exclude_from_backup=tm, seen=seen(epoch=1, seq=5, strand=STRAND_2))
        assert path.read_bytes() == before
        S.update_state(path, lock, exclude_from_backup=tm, seen=seen(epoch=1, seq=5))  # the same mark again
        S.update_state(path, lock, exclude_from_backup=tm, seen=seen(epoch=1, seq=6, strand=STRAND_2))
        assert S.update_state(path, lock, exclude_from_backup=tm, seen=seen(epoch=2, seq=1)).seen == seen(2, 1)


def test_the_returned_state_is_independent_of_the_callers_objects(home):
    path, rec = _state_path(home), checkpoint()
    with _lock(home) as lock:
        st = S.update_state(path, lock, exclude_from_backup=FakeTM(), checkpoint=rec)
    rec["seq"] = 99
    rec["logs"]["ledger"]["segments"].clear()
    assert st.checkpoint == checkpoint() == S.load_state(path).checkpoint


# ── The Time Machine exclusion ──

def test_the_writer_that_creates_local_excludes_it_before_writing(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
    assert len(tm.calls) == 1
    excluded_as, ino, contents = tm.calls[0]
    assert contents == []  # nothing was in it yet
    assert os.lstat(path.parent).st_ino == ino and tm.excluded(path.parent)
    assert excluded_as.parent == path.parent.parent  # excluded under a temporary name, then moved into place
    assert sorted(p.name for p in path.parent.parent.iterdir()) == ["local"]
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=1)
    assert len(tm.calls) == 1  # the folder exists: its exclusion stands


def test_a_failed_exclusion_refuses_and_leaves_no_folder(home):
    path, tm = _state_path(home), FakeTM(fail=True)
    with _lock(home) as lock:
        with pytest.raises(S.BackupExclusionError, match="Time Machine"):
            S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
    assert len(tm.calls) == 1
    assert not path.parent.exists()
    assert list(path.parent.parent.iterdir()) == []  # no temporary folder left either


def test_no_exclusion_given_refuses_to_create_the_folder(home):
    path = _state_path(home)
    with _lock(home) as lock:
        with pytest.raises(S.BackupExclusionError, match="excluded from Time Machine"):
            S.update_state(path, lock, epoch_start=0)
        with pytest.raises(S.BackupExclusionError):
            S.save_state(path, S.RoamState())
    assert not path.parent.exists()


def test_a_deleted_local_folder_gets_its_exclusion_again(home):
    import shutil

    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        shutil.rmtree(path.parent)  # a restore that removed it
        with pytest.raises(S.BackupExclusionError):
            S.update_state(path, lock, epoch_start=1)  # a writer without the exclusion refuses
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=1)
    assert len(tm.calls) == 2 and tm.excluded(path.parent)
    assert S.load_state(path).epoch_start == 1


def test_a_time_machine_style_file_replacement_keeps_the_folder_exclusion(home):
    path, tm = _state_path(home), FakeTM()
    with _lock(home) as lock:
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        older = home / "older-state.json"
        older.write_bytes(path.read_bytes())
        S.update_state(path, lock, exclude_from_backup=tm, checkpoint=checkpoint())
        os.replace(older, path)  # the file is replaced; the folder (and its exclusion) stays
        os.chmod(path, 0o600)
        assert tm.excluded(path.parent)
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=2)
    assert len(tm.calls) == 1 and tm.excluded(path.parent)


def test_a_leftover_from_a_crash_before_the_move_is_cleaned_and_excluded_again(home, monkeypatch):
    from irp.roam import keys as K

    path, tm = _state_path(home), FakeTM()

    class Kill(BaseException):
        pass

    def die(src, dst):
        raise Kill()
    with _lock(home) as lock:
        with monkeypatch.context() as m:
            m.setattr(K, "_replace", die)
            with pytest.raises(Kill):
                S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
        assert not path.parent.exists()
        S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
    assert len(tm.calls) == 2 and tm.excluded(path.parent)
    assert sorted(p.name for p in path.parent.parent.iterdir()) == ["local"]


def test_a_symlinked_local_folder_is_refused(home):
    path, tm = _state_path(home), FakeTM()
    (home / "elsewhere").mkdir()
    path.parent.parent.mkdir(parents=True)
    path.parent.symlink_to(home / "elsewhere")
    with _lock(home) as lock:
        with pytest.raises(S.StateError, match="symlink"):
            S.update_state(path, lock, exclude_from_backup=tm, epoch_start=0)
    assert tm.calls == [] and list((home / "elsewhere").iterdir()) == []


def test_excluded_dir_serves_staging_and_the_home_local_folder(home):
    tm = FakeTM()
    staging = S.excluded_dir(S.staging_dir(home / "ledgers" / LEDGER), tm)
    local = S.excluded_dir(S.tsa_path(home).parent, tm)
    assert staging.is_dir() and local.is_dir() and tm.excluded(staging) and tm.excluded(local)
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700 == stat.S_IMODE(local.stat().st_mode)


def test_write_local_file_writes_privately_into_an_excluded_folder(home):
    tm, target = FakeTM(), S.tsa_path(home)
    with _lock(home) as lock:
        S.write_local_file(target, b'{"tsas":[],"v":1}', lock, exclude_from_backup=tm)
    assert target.read_bytes() == b'{"tsas":[],"v":1}' and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert tm.excluded(target.parent)
    with _lock(home, exclusive=False) as shared:
        with pytest.raises(S.StateError, match="exclusively"):
            S.write_local_file(target, b"{}", shared, exclude_from_backup=tm)


# ── The production exclusion, against a fake tmutil ──

class FakeTmutil:
    def __init__(self, answer: bytes = b"[Excluded]    {path}\n", fail_on: str | None = None):
        self.calls: list[tuple[list[str], bytes]] = []
        self.answer, self.fail_on = answer, fail_on

    def __call__(self, argv, data=b""):
        from irp.roam.keys import KeystoreError

        self.calls.append((list(argv), data))
        if self.fail_on == argv[1]:
            raise KeystoreError("tmutil failed (exit 1)")
        if argv[1] == "isexcluded":
            return self.answer.replace(b"{path}", argv[2].encode())
        return b""


def test_tmutil_adds_the_exclusion_then_confirms_it(tmp_path):
    run = FakeTmutil()
    S.tmutil_exclude(tmp_path / "local", run=run)
    assert run.calls == [(["/usr/bin/tmutil", "addexclusion", str(tmp_path / "local")], b""),
                         (["/usr/bin/tmutil", "isexcluded", str(tmp_path / "local")], b"")]
    assert S.tmutil_is_excluded(tmp_path / "local", run=run)


@pytest.mark.parametrize("run", [FakeTmutil(answer=b"[Included]    {path}\n"), FakeTmutil(answer=b""),
                                 FakeTmutil(fail_on="addexclusion"), FakeTmutil(fail_on="isexcluded")])
def test_tmutil_failures_refuse(tmp_path, run):
    with pytest.raises(S.BackupExclusionError):
        S.tmutil_exclude(tmp_path / "local", run=run)


def test_the_state_dataclass_keeps_its_old_defaults():
    st = S.RoamState()
    assert dataclasses.asdict(st) == {"rotation": None, "probes": (), "held": (), "checkpoint": None,
                                      "seen": None, "epoch_start": None, "fresh_keys_owed": None}
