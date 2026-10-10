"""Roaming IRP, Cut 1 step 2.5c: rotation (spec v0.3 §14.6, §14.6a).

The laptop rotates its device key (DK) and capability key (CK) at least every 31 days. One interactive
`irp roam rotate` writes a `device_rotate` line (old DK, an approver tap, new DK), swaps the keystore over
to the new keys, renews every active reader with a fresh identity, and prints the relay config edit. These
tests drive the engine against real files in a temp folder: a keystore under a plain KEK file, the two
logs, `state.json` and `keys/roam.lock`. The hardware key is a software authenticator, the clock and the
relay's clock check are fakes, and the hooks for later steps (2.6, 2.7, 1b, 2.8) are recorders.

Every §14.6a gate for 2.5c is here: a kill or an interrupt at every step boundary and at every durable
write, then resume; a second process during the tap; interleaved saves; a failure after the line lands;
a rollback that fails; a torn rotate line; a reader expiring mid-sitting; a retired DK; set_kek; the
status boundaries; a recovery that keeps the kid; the old CK's not_after; two rotations' probes; the
approvers tried in turn; and no clock check, no rotation. Plus the owner's decisions R1, R3 and R5, and the
owner-approved fresh-keys-owed marker that irp roam rotate --now writes before anything else.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import approver, sig  # noqa: E402
from irp.roam import keys as K  # noqa: E402
from irp.roam import logs as L  # noqa: E402
from irp.roam import rotation as R  # noqa: E402
from irp.roam import state as S  # noqa: E402
from irp.roam.age import Identity  # noqa: E402
from irp.roam.keys import (  # noqa: E402
    BoxPrev,
    FileKek,
    Keystore,
    KeystoreError,
    LockBusy,
    Pending,
    RoamLock,
    keystore_digest,
    load_keystore,
    load_locked,
    save_keystore,
    save_locked,
    set_kek,
)
from irp.roam.logs import LogError, LogWriter, parse_line, replay_devices, replay_readers  # noqa: E402
from roam_logkit import LEDGER, DevicesKit, EdKey, ReadersKit, h, sign_body, ts  # noqa: E402

DAY = timedelta(days=1)


class SilentFile(FileKek):
    """The plain KEK file without its warning (the warning has its own test in test_roam_keys.py)."""

    @staticmethod
    def _warn():
        pass


class Kill(BaseException):
    """A power cut or kill -9: nothing in the engine may catch it and carry on."""


class FakeClock:
    """The engine's clock. `step` moves it on by that many seconds at every reading; `sleep` moves it on by
    the time slept unless it's frozen (a clock that never catches up)."""

    def __init__(self, t: datetime, step: int = 0, frozen: bool = False):
        self.t, self.step, self.frozen, self.slept = t.replace(microsecond=0), step, frozen, 0

    def __call__(self) -> datetime:
        now = self.t
        self.t += timedelta(seconds=self.step)
        return now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        if not self.frozen:
            self.t += timedelta(seconds=seconds)


class Keyring:
    """Stands in for the plugged-in hardware keys: answers only for the credentials it holds."""

    def __init__(self, *keys, cancel: bool = False, on_ask=None):
        self.by_cred = {k.cred_id: k for k in keys}
        self.asked: list[bytes] = []
        self.cancel, self.on_ask = cancel, on_ask

    def get_assertion(self, rp_id, client_data_hash, cred_id):
        self.asked.append(cred_id)
        if self.on_ask:
            self.on_ask()
        if self.cancel:
            raise approver.Cancelled()
        key = self.by_cred.get(cred_id)
        if key is None:
            raise approver.NoCredential()
        return key.get_assertion(rp_id, client_data_hash, cred_id)


def rid(name: str) -> str:
    return "rd-" + hashlib.sha256(name.encode()).hexdigest()[:32]


def _epoch(label: str, e: int) -> K.EpochKeys:
    """A test epoch entry: K_c, K_a and RK's X25519 public key (§18a), all from labels."""
    return K.EpochKeys(kc=h(f"{label}/kc/{e}"), ka=h(f"{label}/ka/{e}"),
                       rk_pub=Identity(h(f"{label}/rk")).recipient().public)


class FakeTM:
    """Stands in for `tmutil addexclusion` plus `isexcluded`: records each folder (by inode, since a folder
    exclusion moves with the folder) and can be told to fail."""

    def __init__(self):
        self.calls: list[tuple[Path, int]] = []
        self.fail = False

    def __call__(self, path) -> None:
        self.calls.append((Path(path), os.lstat(path).st_ino))
        if self.fail:
            raise RuntimeError("tmutil isexcluded says the folder is still included")

    def excluded(self, path) -> bool:
        return os.lstat(path).st_ino in {ino for _, ino in self.calls}


def _private(path: Path, data: bytes) -> None:
    K._private_dir(path.parent)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _enrol_two(env, rk):
    env.readers_made = [rid(env.label + "/a"), rid(env.label + "/b")]
    for r in env.readers_made:
        rk.enrol(r, [env.laptop, env.aks[0]])


class Env:
    """A ledger after `irp roam init` plus enrolments: genesis, laptop-1 (whose keys are in the keystore),
    the approvers and some readers, written to disk. The engine's clock starts a day after the last line."""

    def __init__(self, tmp_path: Path, label: str = "rot", approvers=("hwkey-1",), readers=_enrol_two,
                 days_after: float = 1, step: int = 0, security=None, age=None):
        self.tmp, self.label, self.security, self.age = tmp_path, label, security, age
        kit = DevicesKit(label)
        kit.genesis()
        self.laptop_key = EdKey(f"{label}/laptop-1/key")
        self.box_label = f"{label}/laptop-1/box"
        self.laptop = kit.custodian("laptop-1", key=self.laptop_key, box_label=self.box_label)
        self.aks = [kit.approver(a) for a in approvers]
        self.kit, self.rk = kit, ReadersKit(kit)
        self.readers_made: list[str] = []
        if readers:
            readers(self, self.rk)
        self.ledger = R.Ledger(ledger_id=LEDGER, root=kit.root.kid, keys_dir=tmp_path / "keys",
                               ledger_dir=tmp_path / "ledgers" / LEDGER)
        self.write_logs()
        mode = "keychain" if security is not None else "passphrase" if age is not None else "file"
        self.ks = Keystore(kek_source=mode, dk_seed=self.laptop_key.seed,
                           dk_box=h("box/" + self.box_label),
                           ck_seed=h(label + "/ck/0"), epochs={0: _epoch(label, 0)})
        save_keystore(self.ledger.keys_dir, self.ks, self.source(), os.urandom)
        self.clock = FakeClock(kit.clock.t + timedelta(days=days_after), step=step)
        self.keyring = Keyring(*[kit.keys[a] for a in self.aks])
        self.checks: list[datetime] = []
        self.clock_fails_at: int | None = None
        self.said: list[str] = []
        self.calls: list[tuple] = []
        self.delivered: list[tuple[str, Identity, str]] = []
        self.minted: list[tuple[str, str, str]] = []
        self.refuse_delivery: set[str] = set()
        self.confirm = True
        self.on_progress = None
        self.on_adopt = None       # (ks, lock) -> None; may raise
        self.on_checkpoint = None  # (ks, rotate_idx, lock) -> None; may raise
        self.on_publish = None     # (lock) -> None
        self.tm = FakeTM()         # Time Machine exclusion: the real tmutil never runs in these tests
        self.keyring.on_ask = lambda: self.calls.append(("tap",))
        self.hooks = R.Hooks(probe=self._probe, quarantine=self._quarantine, remint=self._remint,
                             adopt=self._adopt, checkpoint=self._checkpoint, deliver=self._deliver,
                             show=self._show, confirm_config=self._confirm, publish=self._publish,
                             progress=self._progress)

    # ── files ──
    def source(self):
        if self.security is not None:  # Mode A against a fake `security`: the real Keychain is never touched
            return K.KeychainKek(LEDGER, run=self.security)
        if self.age is not None:  # Mode B against a fake `age -p`
            return K.PassphraseKek(self.ledger.keys_dir, run=self.age)
        return SilentFile(self.ledger.keys_dir)

    def write_logs(self) -> None:
        _private(self.ledger.devices_path, self.kit.data)
        _private(self.ledger.readers_path, self.rk.data)

    def devices_bytes(self) -> bytes:
        return self.ledger.devices_path.read_bytes()

    def devices(self, now=None):
        return replay_devices(self.devices_bytes(), ledger_id=LEDGER, root=self.kit.root.kid, now=now or self.clock.t)

    def readers(self, devices=None, now=None):
        devices = devices or self.devices(now)
        return replay_readers(self.ledger.readers_path.read_bytes(), devices, ledger_id=LEDGER,
                              now=now or self.clock.t)

    def keystore(self) -> Keystore:
        return load_keystore(self.ledger.keys_dir, [self.source()])

    def state(self):
        return S.load_state(self.ledger.state_path)

    def snapshot(self) -> dict:
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob("*")) if p.is_file()}

    def rotate_lines(self) -> list[dict]:
        out = []
        for line in L.split_log(self.devices_bytes()):
            body, _ = parse_line(line, "devices-entry")
            if body["event"] == "device_rotate":
                out.append(body)
        return out

    # ── the engine ──
    def rotate(self, **kw):
        args = dict(sources=[self.source()], rng=os.urandom, clock=self.clock, clock_check=self.clock_check,
                    authenticator=self.keyring, hooks=self.hooks, sleep=self.clock.sleep, say=self.said.append,
                    exclude_from_backup=self.tm)
        args.update(kw)
        return R.rotate(self.ledger, **args)

    def resume(self, **kw):
        args = dict(sources=[self.source()], rng=os.urandom, clock=self.clock, clock_check=self.clock_check,
                    authenticator=self.keyring, hooks=self.hooks, sleep=self.clock.sleep, say=self.said.append,
                    exclude_from_backup=self.tm)
        args.update(kw)
        return R.resume_rotation(self.ledger, **args)

    def clock_check(self, now: datetime) -> None:
        self.checks.append(now)
        if self.clock_fails_at is not None and len(self.checks) == self.clock_fails_at:
            raise RuntimeError("the relay's Date header is 9 minutes away")

    # ── hooks ──
    def _probe(self, ck_seed, ck_id, nbf, exp, rng):
        assert sig.key_id("ck", sig.public_key(ck_seed)) == ck_id
        self.minted.append((ck_id, nbf, exp))
        self.calls.append(("probe", ck_id))
        return f"probe.{ck_id}.{len(self.minted)}"

    def _quarantine(self, ck_id):
        self.calls.append(("quarantine", ck_id))
        return [{"iss": ck_id, "from": "2026-10-01T00:00:00Z"}]

    def _remint(self, ks):
        self.calls.append(("remint", ks.ck_id))

    def _adopt(self, ks, lock):
        assert lock.held and lock.exclusive
        self.calls.append(("adopt", ks.dk_id))
        if self.on_adopt:
            self.on_adopt(ks, lock)

    def _checkpoint(self, ks, rotate_idx, lock):
        assert lock.held and lock.exclusive
        self.calls.append(("checkpoint", rotate_idx))
        if self.on_checkpoint:
            self.on_checkpoint(ks, rotate_idx, lock)

    def _deliver(self, reader_id, identity, expires):
        self.calls.append(("deliver", reader_id))
        if reader_id in self.refuse_delivery:
            return False
        self.delivered.append((reader_id, identity, expires))
        return True

    def _show(self, config, reapproval, notes):
        self.calls.append(("show", config, tuple(reapproval), tuple(notes)))

    def _confirm(self, config):
        self.calls.append(("confirm",))
        return self.confirm

    def _publish(self, lock):
        assert lock.held and lock.exclusive
        self.calls.append(("publish",))
        if self.on_publish:
            self.on_publish(lock)

    def _progress(self, step):
        self.calls.append(("progress", step))
        if self.on_progress:
            self.on_progress(step)


def settled(env: Env, rotations: int = 1):
    """What every finished rotation leaves behind, whatever happened on the way."""
    ks = env.keystore()
    dev = env.devices()
    rd = env.readers(dev)
    st = env.state()
    rot = env.rotate_lines()
    assert len(rot) == rotations
    assert ks.pending is None
    assert R.unfinished(ks, dev, st) is None
    R.check_signer(ks, dev, st)
    assert ks.dk_id == rot[-1]["device"]["kid"] and dev.state().is_active(ks.dk_id)
    assert ks.dk_recipient == rot[-1]["device"]["box"]
    assert st.rotation.rotate_idx == rot[-1]["idx"] and st.rotation.closed
    assert st.rotation.new_ck["id"] == ks.ck_id
    assert st.held == ()
    assert ks.epochs == env.ks.epochs  # rotation never changes K_c or K_a
    # Every reader still active holds the bundle the log names: its newest delivered identity.
    for r in rd.active_readers(now=env.clock.t, epoch=dev.epoch):
        mine = [i for (x, i, _) in env.delivered if x == r]
        assert mine, r
        assert rd.reader(r).recipient == mine[-1].recipient().to_string()
    # One probe for every outgoing CK, never two.
    assert len({p.iss for p in st.probes}) == len(st.probes)
    return ks, dev, rd, st


# ── The keystore and roam.lock ──

def test_roam_lock_is_a_private_file_and_an_exclusive_holder_shuts_out_everyone(tmp_path):
    keys = tmp_path / "keys"
    with RoamLock(keys, exclusive=True, interactive=False) as lock:
        assert lock.held and lock.exclusive
        path = keys / "roam.lock"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(keys.stat().st_mode) == 0o700
        for exclusive in (True, False):
            with pytest.raises(LockBusy, match="busy"):
                with RoamLock(keys, exclusive=exclusive, interactive=False):
                    pass
    with RoamLock(keys, exclusive=True, interactive=False):
        pass  # released on exit


def test_shared_holders_share_and_keep_an_exclusive_one_out(tmp_path):
    keys = tmp_path / "keys"
    with RoamLock(keys, exclusive=False, interactive=False) as a, \
            RoamLock(keys, exclusive=False, interactive=False) as b:
        assert a.held and b.held and not a.exclusive
        with pytest.raises(LockBusy):
            with RoamLock(keys, exclusive=True, interactive=False):
                pass


def test_an_interactive_run_waits_for_the_lock_and_says_so(tmp_path):
    keys, said, got = tmp_path / "keys", [], []

    def second():
        with RoamLock(keys, exclusive=False, interactive=True, say=said.append):
            got.append(True)
    with RoamLock(keys, exclusive=True, interactive=False):
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.3)
        assert t.is_alive() and not got
        assert said and "waiting" in said[0] and "roam.lock" in said[0]
    t.join(10)
    assert got == [True]


def test_the_lock_refuses_a_symlink(tmp_path):
    keys = tmp_path / "keys"
    K._private_dir(keys)
    (tmp_path / "elsewhere").write_bytes(b"")
    (keys / "roam.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(KeystoreError):
        with RoamLock(keys, exclusive=True, interactive=False):
            pass


def _keystore(label: str) -> Keystore:
    return Keystore(kek_source="file", dk_seed=h(label + "/dk"), dk_box=h(label + "/box"), ck_seed=h(label + "/ck"),
                    epochs={0: _epoch(label, 0)})


def test_a_save_refuses_when_the_keystore_changed_since_this_process_loaded_it(tmp_path):
    keys = tmp_path / "keys"
    save_keystore(keys, _keystore("v1"), SilentFile(keys), os.urandom)
    loaded = keystore_digest(keys)
    save_keystore(keys, _keystore("v2"), SilentFile(keys), os.urandom)  # another run saved in between
    with pytest.raises(KeystoreError, match="changed since"):
        save_keystore(keys, _keystore("v3"), SilentFile(keys), os.urandom, expect_digest=loaded)
    assert load_keystore(keys, [SilentFile(keys)]) == _keystore("v2")
    fresh = keystore_digest(keys)
    assert save_keystore(keys, _keystore("v3"), SilentFile(keys), os.urandom, expect_digest=fresh) == \
        keystore_digest(keys)


def test_save_locked_needs_the_lock_held_exclusively(tmp_path):
    keys = tmp_path / "keys"
    save_keystore(keys, _keystore("v1"), SilentFile(keys), os.urandom)
    with RoamLock(keys, exclusive=False, interactive=False) as lock:
        ks, digest = load_locked(keys, [SilentFile(keys)], lock)
        with pytest.raises(KeystoreError, match="exclusive"):
            save_locked(keys, _keystore("v2"), SilentFile(keys), os.urandom, lock, digest)
    with RoamLock(keys, exclusive=True, interactive=False) as lock:
        ks, digest = load_locked(keys, [SilentFile(keys)], lock)
        save_locked(keys, _keystore("v2"), SilentFile(keys), os.urandom, lock, digest)
    assert load_keystore(keys, [SilentFile(keys)]) == _keystore("v2")


def test_two_interleaved_saves_leave_a_keystore_a_master_key_opens(tmp_path, monkeypatch):
    """Save A is paused halfway (its keystore.bin.next written, nothing renamed) while set-kek B starts. B
    waits for roam.lock, so it never writes over A's half-done save; then it loads A's result and re-wraps it."""
    keys = tmp_path / "keys"
    save_keystore(keys, _keystore("v1"), SilentFile(keys), os.urandom)
    paused, go, done = threading.Event(), threading.Event(), []
    real_write = K._write_private

    def write(path, data):
        real_write(path, data)
        if path.name == "keystore.bin.next" and not paused.is_set():
            paused.set()
            go.wait(10)
    monkeypatch.setattr(K, "_write_private", write)

    def save_a():
        with RoamLock(keys, exclusive=True, interactive=False) as lock:
            ks, digest = load_locked(keys, [SilentFile(keys)], lock)
            save_locked(keys, _keystore("v2"), SilentFile(keys), os.urandom, lock, digest)
        done.append("a")

    def switch_b():
        set_kek(keys, [SilentFile(keys)], SilentFile(keys), os.urandom)
        done.append("b")
    a = threading.Thread(target=save_a)
    a.start()
    assert paused.wait(10)
    half = (keys / "keystore.bin.next").read_bytes()
    b = threading.Thread(target=switch_b)
    b.start()
    time.sleep(0.3)
    assert b.is_alive() and done == []
    assert (keys / "keystore.bin.next").read_bytes() == half  # B hasn't touched A's half-done save
    go.set()
    a.join(10)
    b.join(10)
    assert done == ["a", "b"]
    assert load_keystore(keys, [SilentFile(keys)]) == _keystore("v2")


def _interrupted_save(keys: Path) -> None:
    """keystore.bin renamed into place but its master key still in the next slot (a crash before promote)."""
    save_keystore(keys, _keystore("v1"), SilentFile(keys), os.urandom)
    src = SilentFile(keys)
    src.promote = lambda kek: None
    save_keystore(keys, _keystore("v2"), src, os.urandom)
    assert (keys / "kek.bin.next").exists()


def test_a_shared_holder_finishes_an_interrupted_save_only_after_taking_the_lock_exclusively(tmp_path):
    keys = tmp_path / "keys"
    _interrupted_save(keys)
    with pytest.raises(K.InterruptedSave):
        load_keystore(keys, [SilentFile(keys)], finish=False)
    assert (keys / "kek.bin.next").exists()
    with RoamLock(keys, exclusive=False, interactive=False) as lock:
        ks, digest = load_locked(keys, [SilentFile(keys)], lock)
        assert lock.exclusive and ks == _keystore("v2") and digest == keystore_digest(keys)
    assert not (keys / "kek.bin.next").exists()


def test_a_shared_holder_never_finishes_a_save_while_another_holds_the_lock(tmp_path):
    keys = tmp_path / "keys"
    _interrupted_save(keys)
    before = {p.name: p.read_bytes() for p in keys.iterdir()}
    with RoamLock(keys, exclusive=False, interactive=False):  # another run, say a publish
        with pytest.raises(LockBusy):
            with RoamLock(keys, exclusive=False, interactive=False) as lock:
                load_locked(keys, [SilentFile(keys)], lock)
    assert {p.name: p.read_bytes() for p in keys.iterdir() if p.name != "roam.lock"} == before


# ── state.json ──

def _record(**kw):
    rec = dict(rotate_idx=4, at="2026-10-09T09:00:00Z", suspected=False,
               new_ck={"id": sig.key_id("ck", b"\x01" * 32), "pub": sig.b64url_encode(b"\x01" * 32),
                       "not_after": "2026-11-16T09:00:00Z"},
               old_ck={"id": "ck-" + "2" * 32, "not_after": "2026-10-16T09:00:00Z"},
               quarantine=({"iss": "ck-" + "2" * 32, "from": "2026-10-01T00:00:00Z"},), closed=False)
    rec.update(kw)
    return S.RotationRecord(**rec)


def test_state_round_trips_as_a_private_jcs_file(tmp_path):
    path = tmp_path / "ledgers" / LEDGER / "local" / "state.json"
    assert S.load_state(path) == S.RoamState()  # no file yet: nothing recorded
    st = S.RoamState(rotation=_record(), probes=(S.Probe(iss="ck-" + "2" * 32, nbf="2026-10-16T09:02:00Z",
                                                         token="probe.token"),),
                     held=(rid("x"), rid("y")))
    tm = FakeTM()
    S.save_state(path, st, exclude_from_backup=tm)
    assert tm.excluded(path.parent)
    assert S.load_state(path) == st
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = path.read_bytes()
    assert data == canonicalize(sig.load_jcs(data, "state"))
    S.save_state(path, dataclasses.replace(st, rotation=_record(old_ck=None, suspected=True, quarantine=())))
    assert S.load_state(path).rotation.old_ck is None


STATE_MUTATIONS = {
    "unknown top key": lambda c: c.update(extra=1),
    "missing held": lambda c: c.pop("held"),
    "held unsorted": lambda c: c.update(held=sorted(c["held"], reverse=True)),
    "held bad id": lambda c: c.update(held=["rd-1"]),
    "rotation extra key": lambda c: c["rotation"].update(x=1),
    "rotate_idx negative": lambda c: c["rotation"].update(rotate_idx=-1),
    "rotate_idx bool": lambda c: c["rotation"].update(rotate_idx=True),
    "at not a timestamp": lambda c: c["rotation"].update(at="yesterday"),
    "suspected not bool": lambda c: c["rotation"].update(suspected=0),
    "new_ck without pub": lambda c: c["rotation"]["new_ck"].pop("pub"),
    "new_ck not_after remove": lambda c: c["rotation"]["new_ck"].update(not_after="remove"),
    "new_ck id isn't its pub's": lambda c: c["rotation"]["new_ck"].update(id="ck-" + "1" * 32),
    "old_ck same as new_ck": lambda c: c["rotation"]["old_ck"].update(id=c["rotation"]["new_ck"]["id"]),
    "old_ck bad id": lambda c: c["rotation"]["old_ck"].update(id="dk-" + "2" * 32),
    "old_ck bad not_after": lambda c: c["rotation"]["old_ck"].update(not_after="later"),
    "quarantine both iss and sub": lambda c: c["rotation"]["quarantine"][0].update(sub="u-x"),
    "quarantine without from": lambda c: c["rotation"]["quarantine"][0].pop("from"),
    "closed not bool": lambda c: c["rotation"].update(closed="yes"),
    "probe extra key": lambda c: c["probes"][0].update(exp="x"),
    "probe bad iss": lambda c: c["probes"][0].update(iss="rt-" + "2" * 32),
    "probe token with a space": lambda c: c["probes"][0].update(token="a b"),
}


@pytest.mark.parametrize("name", sorted(STATE_MUTATIONS))
def test_state_is_a_closed_schema(tmp_path, name):
    path = tmp_path / "state.json"
    S.save_state(path, S.RoamState(rotation=_record(), probes=(S.Probe("ck-" + "2" * 32, "2026-10-16T09:02:00Z",
                                                                       "t"),), held=(rid("x"), rid("y"))))
    c = sig.load_jcs(path.read_bytes(), "state")
    STATE_MUTATIONS[name](c)
    path.write_bytes(canonicalize(c))
    with pytest.raises(S.StateError):
        S.load_state(path)


def test_state_refuses_non_jcs_bytes(tmp_path):
    path = tmp_path / "state.json"
    S.save_state(path, S.RoamState())
    path.write_bytes(b'{"checkpoint": null, "epoch_start": null, "held": [], "probes": [], "rotation": null, '
                     b'"seen": null}')
    with pytest.raises(S.StateError):
        S.load_state(path)


# ── Status (age from the signed log) ──

def _introduced(kit: DevicesKit, kid: str) -> datetime:
    for line in kit.lines:
        body, _ = parse_line(line, "devices-entry")
        dev = body.get("device") or {}
        if dev.get("kid") == kid or body.get("new_device") == kid:
            return datetime.strptime(body["at"], "%Y-%m-%dT%H:%M:%SZ")
    raise AssertionError(kid)


@pytest.mark.parametrize("age,state", [(28 * DAY - timedelta(seconds=1), "ok"), (28 * DAY, "warn"),
                                       (31 * DAY, "warn"), (31 * DAY + timedelta(seconds=1), "overdue"),
                                       (timedelta(0), "ok"), (90 * DAY, "overdue")])
def test_status_boundaries_compare_exact_seconds(age, state):
    kit = DevicesKit("status")
    kit.genesis()
    laptop = kit.custodian("laptop-1")
    dev = replay_devices(kit.data, ledger_id=LEDGER, root=kit.root.kid, now=kit.clock.t + 100 * DAY)
    s = R.rotation_status(dev, laptop, _introduced(kit, laptop) + age)
    assert s.state == state and s.age == age and s.label == "laptop-1"
    assert s.days == age.days  # whole days are for display only


def test_a_recovery_that_keeps_the_kid_a_root_rotation_and_a_rekey_never_reset_its_age():
    kit = DevicesKit("keeps")
    kit.genesis()
    laptop = kit.custodian("laptop-1")
    other = kit.custodian("laptop-2")
    kit.clock.t += 20 * DAY
    kit.rekey(laptop)
    kit.root_rotate()
    kit.recovery(keep=[laptop], revokes=[other])
    born = _introduced(kit, laptop)
    dev = replay_devices(kit.data, ledger_id=LEDGER, root=kit.root.kid, now=kit.clock.t + DAY)
    s = R.rotation_status(dev, laptop, born + 29 * DAY)
    assert s.state == "warn" and s.age == 29 * DAY  # measured from the enrolment, not the recovery


def test_a_rotation_and_a_recovery_start_a_new_key_at_age_zero():
    kit = DevicesKit("restart")
    kit.genesis()
    laptop = kit.custodian("laptop-1")
    ak = kit.approver("hwkey-1")
    kit.clock.t += 30 * DAY
    new = kit.rotate(laptop, ak)
    kit.clock.t += 2 * DAY
    nine = kit.recovery(keep=[new, ak], revokes=[])
    dev = replay_devices(kit.data, ledger_id=LEDGER, root=kit.root.kid, now=kit.clock.t + DAY)
    for kid in (new, nine):
        born = _introduced(kit, kid)
        assert R.rotation_status(dev, kid, born + DAY).age == DAY
    with pytest.raises(R.RotationAlarm):
        R.rotation_status(dev, laptop, kit.clock.t)  # retired: not an active custodian
    with pytest.raises(R.RotationAlarm):
        R.rotation_status(dev, ak, kit.clock.t)  # an approver isn't a custodian


def test_custodian_ages_lists_every_active_custodian_by_label():
    kit = DevicesKit("ages")
    kit.genesis()
    two = kit.custodian("laptop-2")
    kit.clock.t += 5 * DAY
    one = kit.custodian("laptop-1")
    kit.approver("hwkey-1")
    dev = replay_devices(kit.data, ledger_id=LEDGER, root=kit.root.kid, now=kit.clock.t + DAY)
    now = _introduced(kit, two) + 29 * DAY
    ages = R.custodian_ages(dev, now)
    assert [(a.label, a.kid, a.state) for a in ages] == [("laptop-1", one, "ok"), ("laptop-2", two, "warn")]


def test_r1_publishing_refuses_with_alarm_while_another_custodian_is_overdue():
    kit = DevicesKit("r1")
    kit.genesis()
    other = kit.custodian("laptop-2")
    kit.clock.t += 31 * DAY
    laptop_key = EdKey("r1/laptop-1/key")
    kit.custodian("laptop-1", key=laptop_key, box_label="r1/laptop-1/box")
    ks = Keystore(kek_source="file", dk_seed=laptop_key.seed, dk_box=h("box/r1/laptop-1/box"),
                  ck_seed=h("r1/ck"), epochs={0: (h("kc"), h("ka"))})
    born = _introduced(kit, other)
    dev = replay_devices(kit.data, ledger_id=LEDGER, root=kit.root.kid, now=born + 40 * DAY)
    assert R.check_publish(ks, dev, S.RoamState(), born + 31 * DAY) == frozenset()  # 31 days: warn only
    with pytest.raises(R.PublishRefused, match="rotate or revoke laptop-2") as exc:
        R.check_publish(ks, dev, S.RoamState(), born + 31 * DAY + timedelta(seconds=1))
    assert exc.value.alarm


def test_publishing_refuses_when_this_laptops_own_key_is_overdue(tmp_path):
    env = Env(tmp_path)
    dev = env.devices()
    born = _introduced(env.kit, env.laptop)
    with pytest.raises(R.PublishRefused, match="irp roam rotate") as exc:
        R.check_publish(env.ks, dev, S.RoamState(), born + 31 * DAY + timedelta(seconds=1))
    assert not exc.value.alarm
    assert R.check_publish(env.ks, dev, S.RoamState(), born + 28 * DAY) == frozenset()


# ── One rotation, end to end ──

def test_a_rotation_end_to_end(tmp_path):
    env = Env(tmp_path)
    old = env.ks
    before = env.devices()
    born = _introduced(env.kit, env.laptop)
    res = env.rotate()
    ks, dev, rd, st = settled(env)
    at = env.rotate_lines()[0]["at"]
    t_at = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
    # The line: old the live kid, the new descriptor with the same label, at later than both logs.
    body = env.rotate_lines()[0]
    assert body["old"] == old.dk_id and body["idx"] == len(before.lines)
    assert body["device"] == {"kid": ks.dk_id, "class": "custodian", "alg": "ed25519",
                              "pub": sig.b64url_encode(sig.public_key(ks.dk_seed)), "box": ks.dk_recipient,
                              "label": "laptop-1", "key_scope": "device-local", "webauthn": None}
    assert dev.state().status[old.dk_id] == ("retired", body["idx"])
    # The keystore: new keys live, the old box kept decrypt-only, the old seeds gone, K_c and K_a unchanged.
    assert ks.dk_seed not in (old.dk_seed,) and ks.ck_seed != old.ck_seed and ks.dk_box != old.dk_box
    assert ks.box_prev == BoxPrev(dk_box=old.dk_box, until=ts(t_at + 7 * DAY))
    assert old.dk_seed not in (ks.dk_seed, ks.ck_seed, ks.box_prev.dk_box)
    assert old.ck_seed not in (ks.dk_seed, ks.ck_seed, ks.box_prev.dk_box)
    # The record and the config entries (§17.4 shape).
    old_ck = sig.key_id("ck", sig.public_key(old.ck_seed))
    assert st.rotation == S.RotationRecord(
        rotate_idx=body["idx"], at=at, suspected=False,
        new_ck={"id": ks.ck_id, "pub": sig.b64url_encode(sig.public_key(ks.ck_seed)), "not_after": ts(t_at + 38 * DAY)},
        old_ck={"id": old_ck, "not_after": ts(t_at + 7 * DAY)},
        quarantine=({"iss": old_ck, "from": "2026-10-01T00:00:00Z"},), closed=True)
    assert res.config.issuers() == {ks.ck_id: {"pub": sig.b64url_encode(sig.public_key(ks.ck_seed)),
                                               "not_after": ts(t_at + 38 * DAY)},
                                    old_ck: {"not_after": ts(t_at + 7 * DAY)}}
    assert res.config.removals() == ()
    text = "\n".join(res.config.lines())
    assert ks.ck_id in text and old_ck in text and "quarantine" in text
    # The probe: the old CK, nbf = its not_after + 120 s, exp = nbf + 90 days.
    nbf = t_at + 7 * DAY + timedelta(seconds=120)
    assert env.minted == [(old_ck, ts(nbf), ts(nbf + 90 * DAY))]
    assert st.probes == (S.Probe(iss=old_ck, nbf=ts(nbf), token=f"probe.{old_ck}.1"),)
    # Every reader renewed citing the rotate line, with a fresh recipient, for 38 days.
    renews = [parse_line(x, "readers-entry")[0] for x in rd.lines if b'"reader_renew"' in x]
    assert sorted(b["reader_id"] for b in renews) == sorted(env.readers_made) == sorted(res.renewed)
    for b in renews:
        assert b["devices_at"] == {"idx": body["idx"], "line": L.line_hash(dev.lines[body["idx"]])}
        assert b["expires"] == ts(datetime.strptime(b["at"], "%Y-%m-%dT%H:%M:%SZ") + 38 * DAY)
        assert b["recipient"] not in {before.state().devices[env.laptop].raw["box"]}
    old_recipients = {replay_readers(env.rk.data, before, ledger_id=LEDGER, now=env.clock.t).reader(r).recipient
                      for r in env.readers_made}
    assert not old_recipients & {b["recipient"] for b in renews}
    assert (res.rotate_idx, res.at, res.old_kid, res.new_kid, res.suspected, res.closed, res.held) == \
        (body["idx"], at, old.dk_id, ks.dk_id, False, True, ())
    assert res.resumed is None and res.expired == () and res.reapproval == ()
    assert born < t_at


def test_the_rotate_line_is_signed_by_the_old_dk_an_approver_and_the_new_dk(tmp_path):
    env = Env(tmp_path)
    old = env.ks.dk_id
    env.rotate()
    line = L.split_log(env.devices_bytes())[-1]
    body, sigs = parse_line(line, "devices-entry")
    assert body["event"] == "device_rotate"
    assert sorted(s["key_id"] for s in sigs) == sorted([old, env.aks[0], body["device"]["kid"]])


def test_hooks_run_at_their_fixed_points(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    names = [c[0] if c[0] != "progress" else c[1] for c in env.calls]
    order = ["adopt", "locked", "tap", "signed", "pending saved", "line appended", "quarantine", "probe",
             "record written", "keys live", "remint"]
    assert names[:len(order)] == order
    rest = names[len(order):]
    assert rest.index("renewals done") > max(i for i, n in enumerate(rest) if n.startswith("renewed "))
    assert rest.index("renewals done") < rest.index("show") < rest.index("deliver")
    assert rest.index("confirm") < rest.index("closed") < rest.index("checkpoint") < rest.index("publish") == \
        len(rest) - 1
    assert names.count("adopt") == names.count("checkpoint") == names.count("publish") == 1
    remint = next(c for c in env.calls if c[0] == "remint")
    assert remint[1] == env.keystore().ck_id  # 2.7 re-mints under the new CK


def test_identities_never_reach_the_disk(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    assert env.delivered
    blob = b"".join(env.snapshot().values())
    for _, ident, _ in env.delivered:
        for form in (ident.to_string().encode(), ident.secret.hex().encode(), sig.b64url_encode(ident.secret).encode()):
            assert form not in blob


def test_rotation_is_interactive_only(tmp_path):
    env = Env(tmp_path)
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="interactive"):
        env.rotate(interactive=False)
    assert env.snapshot() == before


def test_no_clock_check_no_rotation(tmp_path):
    env = Env(tmp_path)
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="clock check"):
        env.rotate(clock_check=None)
    assert env.snapshot() == before and env.keyring.asked == []


def test_a_failed_clock_check_at_step_1_refuses_and_writes_nothing(tmp_path):
    env = Env(tmp_path)
    env.clock_fails_at = 1
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="9 minutes"):
        env.rotate()
    assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == \
        {k: v for k, v in before.items()}
    assert env.keyring.asked == []


def test_the_engine_waits_for_the_clock_to_pass_both_logs(tmp_path):
    env = Env(tmp_path)
    last = datetime.strptime(parse_line(env.rk.lines[-1], "readers-entry")[0]["at"], "%Y-%m-%dT%H:%M:%SZ")
    env.clock.t = last - timedelta(seconds=100)  # behind the readers log, within replay's 5-minute slack
    env.rotate()
    at = datetime.strptime(env.rotate_lines()[0]["at"], "%Y-%m-%dT%H:%M:%SZ")
    assert at > last and 100 <= env.clock.slept <= 300
    assert any("waiting for the clock" in m for m in env.said)


def test_the_engine_refuses_after_waiting_5_minutes_and_never_invents_a_time(tmp_path):
    env = Env(tmp_path)
    last = datetime.strptime(parse_line(env.rk.lines[-1], "readers-entry")[0]["at"], "%Y-%m-%dT%H:%M:%SZ")
    env.clock = FakeClock(last - timedelta(seconds=100), frozen=True)
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="clock"):
        env.rotate()
    assert env.clock.slept == 300 and env.rotate_lines() == []  # waits 5 minutes, not a second more
    assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == before


# ── The approver tap ──

def test_each_approver_is_tried_in_turn_in_label_order(tmp_path):
    env = Env(tmp_path, approvers=("hwkey-2", "hwkey-1"))  # enrolled out of label order
    by_label = {env.kit.descs[a]["label"]: env.kit.keys[a] for a in env.aks}
    env.keyring = Keyring(by_label["hwkey-2"])  # only the spare is plugged in
    env.rotate()
    assert env.keyring.asked == [by_label["hwkey-1"].cred_id, by_label["hwkey-2"].cred_id]
    _, sigs = parse_line(L.split_log(env.devices_bytes())[-1], "devices-entry")
    assert by_label["hwkey-2"].kid in [s["key_id"] for s in sigs]
    settled(env)


def test_no_approver_answering_refuses_and_writes_nothing(tmp_path):
    env = Env(tmp_path, approvers=("hwkey-1", "hwkey-2"))
    env.keyring = Keyring()
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="hwkey-1.*hwkey-2"):
        env.rotate()
    assert len(env.keyring.asked) == 2
    assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == before


def test_the_person_can_cancel_the_tap_and_nothing_is_written(tmp_path):
    env = Env(tmp_path, approvers=("hwkey-1", "hwkey-2"))
    env.keyring = Keyring(*[env.kit.keys[a] for a in env.aks], cancel=True)
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="cancel"):
        env.rotate()
    assert len(env.keyring.asked) == 1  # cancel stops at once; the next key isn't asked
    assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == before
    env.keyring.cancel = False
    env.rotate()
    settled(env)


def test_a_second_process_during_the_tap_gets_busy_and_changes_nothing(tmp_path):
    env = Env(tmp_path)
    seen = []

    def second_process():
        before = env.snapshot()
        for attempt in (lambda: R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False),
                        lambda: RoamLock(env.ledger.keys_dir, exclusive=True, interactive=False)):
            try:
                with attempt():
                    seen.append("got in")
            except LockBusy as exc:
                seen.append("busy" if "busy" in str(exc) else str(exc))
        try:
            R.rotate(env.ledger, sources=[env.source()], rng=os.urandom, clock=env.clock,
                     clock_check=env.clock_check, authenticator=env.keyring, interactive=False)
        except R.RotationError:
            seen.append("refused")
        seen.append(env.snapshot() == before)
    env.keyring.on_ask = second_process
    env.rotate()
    assert seen == ["busy", "busy", "refused", True]
    settled(env)


# ── Config entries and the old CK ──

@pytest.mark.parametrize("age_days,suspected,expect", [
    (10, False, "plus 7"),     # on time: at + 7 days
    (35, False, "own"),        # its own not_after (intro + 38) comes first
    (38, False, "remove"),     # its own not_after isn't after at: remove now
    (45, False, "remove"),
    (3, True, "remove"),       # suspected: no overlap
])
def test_the_old_not_after_never_passes_its_own(age_days, suspected, expect):
    intro = datetime(2026, 9, 1, 9, 0, 0)
    at = intro + timedelta(days=age_days)
    got = R.old_ck_not_after(ts(at), ts(intro), suspected=suspected)
    own = intro + 38 * DAY
    if expect == "remove":
        assert got == "remove"
    else:
        assert got == ts(min(at + 7 * DAY, own)) and datetime.strptime(got, "%Y-%m-%dT%H:%M:%SZ") <= own
        assert got == ts(at + 7 * DAY if expect == "plus 7" else own)


def _late_readers(env, rk):
    env.readers_made = [rid(env.label + "/late")]
    rk.enrol(env.readers_made[0], [env.laptop, env.aks[0]], days=60)


def test_a_rotation_after_day_31_keeps_the_old_cks_own_not_after(tmp_path):
    env = Env(tmp_path, readers=_late_readers)
    born = _introduced(env.kit, env.laptop)
    env.clock.t = born + 35 * DAY
    res = env.rotate()
    old_ck = sig.key_id("ck", sig.public_key(env.ks.ck_seed))
    assert res.config.old_ck == {"id": old_ck, "not_after": ts(born + 38 * DAY)}  # not at + 7 days
    assert res.config.issuers()[old_ck] == {"not_after": ts(born + 38 * DAY)}
    settled(env)


def test_a_rotation_past_the_old_cks_own_not_after_reads_remove(tmp_path):
    env = Env(tmp_path, readers=_late_readers, days_after=39)
    res = env.rotate()
    old_ck = sig.key_id("ck", sig.public_key(env.ks.ck_seed))
    assert res.config.old_ck == {"id": old_ck, "not_after": "remove"}
    assert res.config.removals() == (old_ck,) and old_ck not in res.config.issuers()
    assert f"remove {old_ck} now" in "\n".join(res.config.lines())
    settled(env)


def test_a_suspected_rotation_has_no_overlap_and_says_bundles_and_phone_stop(tmp_path):
    env = Env(tmp_path)
    res = env.rotate(suspected=True)
    old_ck = sig.key_id("ck", sig.public_key(env.ks.ck_seed))
    assert res.suspected and res.config.old_ck == {"id": old_ck, "not_after": "remove"}
    assert res.config.removals() == (old_ck,)
    notes = " ".join(res.notes)
    assert "reader bundle" in notes and "phone" in notes and "at once" in notes
    st = env.state()
    assert st.rotation.suspected and st.probes[0].iss == old_ck
    at = datetime.strptime(res.at, "%Y-%m-%dT%H:%M:%SZ")
    assert st.probes[0].nbf == ts(at + timedelta(seconds=120))
    settled(env)


def test_two_rotations_within_7_days_keep_both_probes(tmp_path):
    env = Env(tmp_path)
    first_ck = sig.key_id("ck", sig.public_key(env.ks.ck_seed))
    env.rotate()
    second_ck = env.keystore().ck_id
    env.clock.t += 2 * DAY
    env.rotate()
    st = env.state()
    assert [p.iss for p in st.probes] == [first_ck, second_ck]
    assert st.probes[0].token == f"probe.{first_ck}.1"
    settled(env, rotations=2)


# ── Readers: renewals (R5), held (R3) and expiry during the sitting ──

def test_r5_renewals_run_38_days_capped_at_90_days_from_the_last_approval(tmp_path):
    def readers(env, rk):
        old, new = rid("r5/old"), rid("r5/new")
        rk.enrol(old, [env.laptop, env.aks[0]])
        env.kit.clock.t += 25 * DAY
        rk.renew(old, [env.laptop])
        env.kit.clock.t += 25 * DAY
        rk.renew(old, [env.laptop])
        rk.enrol(new, [env.laptop, env.aks[0]])
        env.readers_made = [old, new]
    env = Env(tmp_path, label="r5", readers=readers, days_after=5)
    before = env.readers()
    old, new = env.readers_made
    res = env.rotate()
    rd = env.readers()
    at = datetime.strptime(res.at, "%Y-%m-%dT%H:%M:%SZ")
    for r in (old, new):
        cap = datetime.strptime(before.reader(r).approved_at, "%Y-%m-%dT%H:%M:%SZ") + 90 * DAY
        line = [parse_line(x, "readers-entry")[0] for x in rd.lines if r.encode() in x][-1]
        assert line["event"] == "reader_renew"
        r_at = datetime.strptime(line["at"], "%Y-%m-%dT%H:%M:%SZ")
        assert rd.reader(r).expires == ts(min(r_at + 38 * DAY, cap))
    assert rd.reader(old).expires == ts(datetime.strptime(before.reader(old).approved_at, "%Y-%m-%dT%H:%M:%SZ")
                                        + 90 * DAY)
    assert res.reapproval == (old,)  # its cap is earlier than at + 38 days: a tap re-approves it
    shown = next(c for c in env.calls if c[0] == "show")
    assert shown[2] == (old,)
    assert at + 38 * DAY > datetime.strptime(rd.reader(old).expires, "%Y-%m-%dT%H:%M:%SZ")
    settled(env)


def test_r3_a_renewed_reader_is_held_until_its_delivery_is_confirmed(tmp_path):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery = {b}
    res = env.rotate()
    st = env.state()
    assert res.held == (b,) and st.held == (b,) and st.rotation.closed
    ks, dev = env.keystore(), env.devices()
    assert R.check_publish(ks, dev, st, env.clock.t) == frozenset({b})  # the publisher skips b (STALE)
    # The next interactive rotate renews it again and delivers.
    env.refuse_delivery = set()
    env.clock.t += DAY
    env.rotate()
    settled(env, rotations=2)


def test_an_unconfirmed_config_edit_leaves_the_rotation_open_and_resume_redelivers_only_the_held(tmp_path):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery, env.confirm = {b}, False
    res = env.rotate()
    assert not res.closed and res.held == (b,)
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert R.unfinished(ks, dev, st) and "rotate" in R.unfinished(ks, dev, st)
    with pytest.raises(R.PublishRefused, match="finish with irp roam rotate"):
        R.check_publish(ks, dev, st, env.clock.t)  # an unattended run never resumes
    with pytest.raises(R.Unfinished):
        R.check_signer(ks, dev, st)
    env.refuse_delivery, env.confirm = set(), True
    calls = len(env.calls)
    res2 = env.rotate()
    assert res2.resumed == "steps 6 and 7" and res2.renewed == (b,) and res2.closed
    assert [c[1] for c in env.calls[calls:] if c[0] == "deliver"] == [b]
    assert not [c for c in env.calls[calls:] if c[0] == "probe"]
    settled(env)


def _expiring_readers(env, rk):
    soon, later = rid("exp/soon"), rid("exp/later")
    rk.enrol(soon, [env.laptop, env.aks[0]])
    rk.enrol(later, [env.laptop, env.aks[0]], days=40)
    env.readers_made = [soon, later]


def test_no_renewal_is_written_for_a_reader_that_expires_during_the_sitting(tmp_path):
    env = Env(tmp_path, label="exp", readers=_expiring_readers)
    soon, later = env.readers_made
    expires = datetime.strptime(env.readers().reader(soon).expires, "%Y-%m-%dT%H:%M:%SZ")
    env.clock.t = expires - timedelta(seconds=120)  # active when the rotation starts

    def slow(step):
        if step == "keys live":
            env.clock.t += timedelta(minutes=10)  # the person takes their time before the renewals
    env.on_progress = slow
    res = env.rotate()
    assert res.expired == (soon,) and res.renewed == (later,)
    rd = env.readers()
    assert not [x for x in rd.lines if b'"reader_renew"' in x and soon.encode() in x]
    assert [c[1] for c in env.calls if c[0] == "deliver"] == [later]
    settled(env)


# ── Failures, decided from the log ──

def test_a_failure_before_the_line_lands_drops_pending(tmp_path):
    env = Env(tmp_path)
    env.clock_fails_at = 2  # step 1 passes; the check before the append fails
    devices = env.devices_bytes()
    with pytest.raises(R.RotationError, match="9 minutes"):
        env.rotate()
    ks = env.keystore()
    assert ks.pending is None and ks == env.ks and env.devices_bytes() == devices
    env.clock_fails_at = None
    env.rotate()
    settled(env)


def test_a_failure_after_the_line_lands_keeps_pending(tmp_path, monkeypatch):
    env = Env(tmp_path)
    real = S.save_state

    def full_disk(path, state):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(S, "save_state", full_disk)
    with pytest.raises(R.RotationError, match="landed"):
        env.rotate()
    ks = env.keystore()
    line = env.rotate_lines()
    assert ks.pending is not None and len(line) == 1
    assert line[0]["device"]["kid"] == ks.pending.dk_id and ks.dk_id == line[0]["old"]
    monkeypatch.setattr(S, "save_state", real)
    res = env.rotate()
    assert res.resumed == "step 5" and res.new_kid == line[0]["device"]["kid"]
    settled(env)


def test_an_append_whose_rollback_fails_is_still_caught_by_the_recovery_test(tmp_path, monkeypatch):
    """The line is written, its sync fails and so does the truncate that should take it back: LogWriter
    raises "rolled back" but the line is in the file. The engine reads the log, not the exception."""
    env = Env(tmp_path)
    armed = {"on": False}
    real_sync, real_truncate = L._full_fsync, os.ftruncate

    def sync(fd):
        if armed["on"]:
            raise OSError(5, "Input/output error")
        return real_sync(fd)

    def truncate(fd, size):
        if armed["on"]:
            armed["on"] = False
            raise OSError(5, "Input/output error")
        return real_truncate(fd, size)

    def arm(step):
        if step == "pending saved":
            armed["on"] = True
    monkeypatch.setattr(L, "_full_fsync", sync)
    monkeypatch.setattr(L.os, "ftruncate", truncate)
    env.on_progress = arm
    with pytest.raises(R.RotationError, match="landed"):
        env.rotate()
    ks = env.keystore()
    rot = env.rotate_lines()
    assert len(rot) == 1 and ks.pending is not None and rot[0]["device"]["kid"] == ks.pending.dk_id
    env.on_progress = None
    res = env.rotate()
    assert res.resumed == "step 5"
    settled(env)


@pytest.mark.parametrize("handler", [True, False])
def test_a_torn_rotate_line_moves_to_forks_and_counts_as_not_landed(tmp_path, monkeypatch, handler):
    env = Env(tmp_path)
    armed = {"on": False}
    real_write = os.write

    def torn(fd, data):
        if armed["on"]:
            armed["on"] = False
            real_write(fd, data[:len(data) // 2])
            raise Kill()
        return real_write(fd, data)

    def arm(step):
        if step == "pending saved":
            armed["on"] = True
    if not handler:
        monkeypatch.setattr(R._Engine, "_after_failure", lambda self, exc: None)
    monkeypatch.setattr(L.os, "write", torn)
    env.on_progress = arm
    with pytest.raises(Kill):
        env.rotate()
    pending = env.keystore().pending
    # Either way pending is kept: an append was attempted, so only the next run, after a fresh open, decides.
    assert pending is not None
    if handler:  # the engine moved the torn tail to forks/ at once
        assert len(list((env.ledger.ledger_dir / "forks").iterdir())) == 1
    env.on_progress = None
    monkeypatch.undo()
    env.rotate()
    forks = list((env.ledger.ledger_dir / "forks").iterdir())
    assert len(forks) == 1
    fragment = forks[0].read_bytes()
    assert b'"device_rotate"' in fragment or b'"device"' in fragment
    _, dev, _, _ = settled(env)
    final = env.rotate_lines()[0]["device"]["kid"]
    assert final.encode() not in fragment  # fresh keys after a rotation that never landed
    assert pending.dk_id.encode() in fragment and pending.dk_id != final


def _run_until_killed(env: Env, k: int, exc_type=Kill):
    count = {"n": 0}
    names = []

    def at_boundary(step):
        count["n"] += 1
        names.append(step)
        if count["n"] == k:
            raise exc_type()
    env.on_progress = at_boundary
    try:
        env.rotate()
        return None
    except exc_type:
        return names[-1]
    finally:
        env.on_progress = None


@pytest.mark.parametrize("handler,exc_type", [(False, Kill), (True, KeyboardInterrupt)])
def test_a_kill_or_interrupt_at_every_step_boundary_then_resume(tmp_path, monkeypatch, handler, exc_type):
    """Boundaries include the gap between two renewals and between two deliveries. A kill skips every
    handler (the engine's after-failure test is switched off); an interrupt (Ctrl-C) runs it."""
    seen = []
    monkeypatch.setattr(L, "_full_fsync", lambda fd: None)  # ordering is under test, not durability
    monkeypatch.setattr(K, "_full_fsync", lambda fd: None)
    for k in range(1, 40):
        env = Env(tmp_path / f"b{k}")
        with monkeypatch.context() as m:
            if not handler:
                m.setattr(R._Engine, "_after_failure", lambda self, exc: None)
            name = _run_until_killed(env, k, exc_type)
        if name is None:
            break
        seen.append(name)
        env.rotate()  # the next interactive irp roam rotate resumes and finishes
        settled(env, rotations=2 if name == "closed" else 1)
    else:
        pytest.fail("the rotation never finished within 40 boundaries")
    assert seen[:5] == ["locked", "signed", "pending saved", "line appended", "record written"]
    assert [s for s in seen if s.startswith("renewed ")] and [s for s in seen if s.startswith("delivered ")]
    assert seen[-1] == "closed"


def _crash_counter(monkeypatch, sources, at: int):
    """Raise Kill before the `at`-th durable write of the whole run: keystore and state writes, renames,
    master-key stores, promotes and removes, and log appends."""
    count = {"n": 0}

    def step():
        count["n"] += 1
        if count["n"] == at:
            raise Kill()
    real_write, real_replace, real_append = K._write_private, K._replace, LogWriter.append

    def write(path, data):
        step()
        real_write(path, data)

    def replace(src, dst):
        step()
        real_replace(src, dst)

    def append(self, line):
        step()
        real_append(self, line)
    monkeypatch.setattr(K, "_write_private", write)
    monkeypatch.setattr(K, "_replace", replace)
    monkeypatch.setattr(LogWriter, "append", append)
    for src in sources:
        for name in ("store", "promote", "remove"):
            real = getattr(src, name)

            def wrapped(*a, _real=real, **kw):
                step()
                return _real(*a, **kw)
            monkeypatch.setattr(src, name, wrapped)
    return count


@pytest.mark.parametrize("handler", [False, True])
def test_a_crash_at_every_durable_write_then_resume(tmp_path, monkeypatch, handler):
    monkeypatch.setattr(K, "_full_fsync", lambda fd: None)  # ordering is under test, not durability
    monkeypatch.setattr(L, "_full_fsync", lambda fd: None)
    for at in range(1, 80):
        env = Env(tmp_path / f"c{at}")
        src = env.source()
        with monkeypatch.context() as m:
            if not handler:
                m.setattr(R._Engine, "_after_failure", lambda self, exc: None)
            count = _crash_counter(m, [src], at)
            try:
                env.rotate(sources=[src])
                finished = True
            except Kill:
                finished = False
        if finished:
            assert count["n"] < at
            settled(env)
            break
        env.rotate()
        settled(env)
    else:
        pytest.fail("the rotation never finished within 80 durable writes")


# ── Retired keys, copied keystores and lost state ──

def test_a_line_signed_by_a_retired_dk_is_refused_before_it_is_appended(tmp_path):
    env = Env(tmp_path)
    old = env.ks
    env.rotate()
    dev_bytes, rd_bytes = env.devices_bytes(), env.ledger.readers_path.read_bytes()
    with LogWriter(env.ledger.devices_path, kind="devices-entry", forks_dir=env.ledger.forks_dir) as dw, \
            LogWriter(env.ledger.readers_path, kind="readers-entry", forks_dir=env.ledger.forks_dir) as rw:
        tail = {"idx": dw.next_idx - 1, "line": dw.prev}
        at = ts(env.clock.t + timedelta(seconds=5))
        body = {"v": 1, "kind": "readers-entry", "event": "reader_renew", "ledger_id": LEDGER,
                "root": env.kit.root.kid, "idx": rw.next_idx, "prev": rw.prev, "at": at, "devices_at": tail,
                "reader_id": env.readers_made[0], "recipient": Identity(h("rogue")).recipient().to_string(),
                "expires": ts(env.clock.t + 10 * DAY)}
        line = canonicalize({"body": body, "sigs": [sig.sign("readers-entry", canonicalize(body), old.dk_seed,
                                                             old.dk_id)]})
        with pytest.raises(LogError):
            R.append_readers_line(rw, dw, line, ledger=env.ledger, now=env.clock.t + timedelta(seconds=5))
        dbody = {"v": 1, "kind": "devices-entry", "event": "device_revoke", "ledger_id": LEDGER,
                 "root": env.kit.root.kid, "idx": dw.next_idx, "prev": dw.prev, "at": at,
                 "kid": env.keystore().dk_id}
        dline = canonicalize({"body": dbody, "sigs": [sig.sign("devices-entry", canonicalize(dbody), old.dk_seed,
                                                               old.dk_id)]})
        with pytest.raises(LogError):
            R.append_devices_line(dw, dline, ledger=env.ledger, now=env.clock.t + timedelta(seconds=5))
    assert env.devices_bytes() == dev_bytes and env.ledger.readers_path.read_bytes() == rd_bytes


def test_a_copied_keystore_with_a_retired_dk_raises_alarm_and_writes_nothing(tmp_path):
    env = Env(tmp_path)
    copy = (env.ledger.keys_dir / "keystore.bin").read_bytes(), (env.ledger.keys_dir / "kek.bin").read_bytes()
    env.rotate()
    (env.ledger.keys_dir / "keystore.bin").write_bytes(copy[0])  # a Time Machine restore of the keys folder
    (env.ledger.keys_dir / "kek.bin").write_bytes(copy[1])
    before = env.snapshot()
    with pytest.raises(R.RotationAlarm):
        env.rotate()
    assert env.snapshot() == before
    with pytest.raises(R.RotationAlarm):
        R.check_signer(env.keystore(), env.devices(), env.state())
    with pytest.raises(R.PublishRefused) as exc:
        R.check_publish(env.keystore(), env.devices(), env.state(), env.clock.t)
    assert exc.value.alarm


def test_a_keystore_the_log_doesnt_know_is_alarm(tmp_path):
    env = Env(tmp_path)
    stranger = _keystore("stranger")
    with pytest.raises(R.RotationAlarm, match="isn't an active custodian"):
        R.check_signer(stranger, env.devices(), S.RoamState())
    wrong_box = dataclasses.replace(env.ks, dk_box=h("another box"))
    with pytest.raises(R.RotationAlarm, match="box"):
        R.check_signer(wrong_box, env.devices(), S.RoamState())


def test_pending_that_matches_no_line_while_the_live_key_is_gone_is_alarm(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.setattr(R._Engine, "_after_failure", lambda self, exc: None)
    assert _run_until_killed(env, 3) == "pending saved"
    monkeypatch.undo()
    env.kit.revoke(env.laptop, [env.kit.root.kid])  # the log moved on without this laptop
    _private(env.ledger.devices_path, env.kit.data)
    before = env.snapshot()
    with pytest.raises(R.RotationAlarm, match="disagree"):
        env.rotate()
    assert env.snapshot() == before


def test_a_lost_state_json_rebuilds_the_record_as_suspected(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    env.ledger.state_path.unlink()
    ks, dev = env.keystore(), env.devices()
    assert R.rotation_open(dev, ks.dk_id, S.RoamState()) == first.rotate_idx
    env.confirm = True
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and res.suspected
    assert res.config.old_ck is None and set(res.config.issuers()) == {ks.ck_id}
    assert any("couldn't be probed" in n for n in res.notes)
    assert sorted(res.renewed) == sorted(env.readers_made)  # delivery unknown: every reader again
    st = env.state()
    assert st.rotation.suspected and st.rotation.old_ck is None and st.rotation.closed
    settled(env)


def test_signing_session_holds_the_lock_shared_and_checks_the_log(tmp_path):
    env = Env(tmp_path)
    with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False) as s:
        assert s.ks == env.ks and s.lock.held and not s.lock.exclusive
        assert s.devices.state().is_active(env.ks.dk_id) and s.readers.active_readers(
            now=env.clock.t, epoch=0) == sorted(env.readers_made)
        with pytest.raises(LockBusy):
            with RoamLock(env.ledger.keys_dir, exclusive=True, interactive=False):
                pass
    env.confirm = False
    env.rotate()
    with pytest.raises(R.Unfinished, match="irp roam rotate"):
        with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False):
            pass


def test_box_prev_is_dropped_only_once_it_is_due():
    ks = dataclasses.replace(_keystore("bp"), box_prev=BoxPrev(dk_box=h("old box"), until="2026-10-16T09:00:00Z"))
    until = datetime(2026, 10, 16, 9, 0, 0)
    keep = R.drop_box_prev_if_due(ks, until, companion_enrolled=False)
    assert keep.box_prev is not None  # not after until yet
    gone = R.drop_box_prev_if_due(ks, until + timedelta(seconds=1), companion_enrolled=False)
    assert gone.box_prev is None and dataclasses.replace(gone, box_prev=ks.box_prev) == ks
    waiting = R.drop_box_prev_if_due(ks, until + DAY, companion_enrolled=True)
    assert waiting.box_prev is not None  # a phone may still post to the old box until the outbox is read
    read = R.drop_box_prev_if_due(ks, until + DAY, companion_enrolled=True, outbox_read_through=until)
    assert read.box_prev is None


def test_a_keystore_mid_rotation_never_signs(tmp_path, monkeypatch):
    """With pending set, every signer refuses and offers irp roam rotate."""
    env = Env(tmp_path)
    monkeypatch.setattr(R._Engine, "_after_failure", lambda self, exc: None)
    assert _run_until_killed(env, 3) == "pending saved"
    monkeypatch.undo()
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert ks.pending is not None
    with pytest.raises(R.Unfinished, match="irp roam rotate"):
        R.check_signer(ks, dev, st)
    with pytest.raises(R.PublishRefused):
        R.check_publish(ks, dev, st, env.clock.t)
    with pytest.raises(R.RotationError, match="interactive"):
        R.resume_rotation(env.ledger, sources=[env.source()], rng=os.urandom, clock=env.clock,
                          clock_check=env.clock_check, authenticator=env.keyring, hooks=env.hooks,
                          sleep=env.clock.sleep, say=env.said.append, interactive=False)
    assert env.resume() is None  # dropped: start again
    assert env.keystore().pending is None and env.keystore() == env.ks


def test_resume_with_nothing_unfinished_refuses(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    with pytest.raises(R.RotationError, match="nothing to resume"):
        R.resume_rotation(env.ledger, sources=[env.source()], rng=os.urandom, clock=env.clock,
                          clock_check=env.clock_check, authenticator=env.keyring, hooks=env.hooks,
                          sleep=env.clock.sleep, say=env.said.append)


def test_a_rotation_in_keychain_mode_keeps_every_key_off_the_command_line(tmp_path):
    from test_roam_keys import FakeSecurity

    sec = FakeSecurity()
    env = Env(tmp_path, security=sec)
    old = env.ks
    env.rotate()
    ks, _, _, _ = settled(env)
    assert ks.kek_source == "keychain" and not (env.ledger.keys_dir / "kek.bin").exists()
    keks = [bytes.fromhex(v) for v in sec.items.values()]
    assert list(sec.items) == [("irp-roam", LEDGER)]  # the next slot was promoted and cleared
    secrets = keks + [old.dk_seed, old.ck_seed, ks.dk_seed, ks.dk_box, ks.ck_seed, old.dk_box]
    for argv, _ in sec.calls:
        joined = " ".join(argv)
        for x in secrets:
            assert x.hex() not in joined and sig.b64url_encode(x) not in joined


# ── Review round 1 ──

def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def _seeded_key(seed: bytes) -> EdKey:
    """An Ed25519 signer for any seed (the kit's EdKey derives its seed from a label)."""
    k = EdKey("seeded")
    k.seed, k.pub = seed, sig.public_key(seed)
    k.kid = sig.key_id("dk", k.pub)
    return k


def extra_devices_line(env: Env, event: str, fields: dict, keys: list) -> int:
    """A devices line written behind the engine's back (another device, the paper key), on top of the log on
    disk, one second after the engine's clock. Returns its idx."""
    data = env.devices_bytes()
    lines = L.split_log(data)
    body = {"v": 1, "kind": "devices-entry", "event": event, "ledger_id": LEDGER, "root": env.kit.root.kid,
            "idx": len(lines), "prev": L.line_hash(lines[-1]), "at": ts(env.clock.t + timedelta(seconds=1)),
            **fields}
    sigs = sorted((sign_body("devices-entry", body, k) for k in keys), key=lambda s: s["key_id"])
    _private(env.ledger.devices_path, data + canonicalize({"body": body, "sigs": sigs}) + b"\n")
    env.clock.t += timedelta(seconds=2)
    return len(lines)


def extra_readers_line(env: Env, event: str, fields: dict, key: EdKey) -> None:
    dev = L.split_log(env.devices_bytes())
    data = env.ledger.readers_path.read_bytes()
    lines = L.split_log(data)
    body = {"v": 1, "kind": "readers-entry", "event": event, "ledger_id": LEDGER, "root": env.kit.root.kid,
            "idx": len(lines), "prev": L.line_hash(lines[-1]) if lines else None,
            "at": ts(env.clock.t + timedelta(seconds=1)),
            "devices_at": {"idx": len(dev) - 1, "line": L.line_hash(dev[-1])}, **fields}
    line = canonicalize({"body": body, "sigs": [sign_body("readers-entry", body, key)]})
    _private(env.ledger.readers_path, data + line + b"\n")
    env.clock.t += timedelta(seconds=2)


def _custodian_desc(key: EdKey, box_seed: bytes, label: str) -> dict:
    return {"kid": key.kid, "class": "custodian", "alg": "ed25519", "pub": sig.b64url_encode(key.pub),
            "box": Identity(box_seed).recipient().to_string(), "label": label, "key_scope": "device-local",
            "webauthn": None}


def _killed_at(env: Env, monkeypatch, step: str) -> None:
    """Run a rotation and kill it (no handler runs) right after the named boundary."""
    with monkeypatch.context() as m:
        m.setattr(R._Engine, "_after_failure", lambda self, exc: None)
        names = []

        def at_boundary(name):
            names.append(name)
            if name == step:
                raise Kill()
        env.on_progress = at_boundary
        with pytest.raises(Kill):
            env.rotate()
        env.on_progress = None
    assert names[-1] == step


# A: a --now rotation that fails between the line and its record stays suspected.

def test_a_suspected_rotation_that_fails_before_its_record_resumes_as_suspected(tmp_path, monkeypatch):
    env = Env(tmp_path)
    real = S.save_state

    def full_disk(path, state):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(S, "save_state", full_disk)
    with pytest.raises(R.RotationError, match="landed.*finish with irp roam rotate .it finishes with no overlap") \
            as exc:
        env.rotate(suspected=True)
    assert "--now" not in str(exc.value)  # its own line landed: fresh keys are on the way, only finishing is owed
    monkeypatch.setattr(S, "save_state", real)
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert ks.pending is not None and st.rotation is None
    assert "finish with irp roam rotate (it finishes with no overlap" in R.unfinished(ks, dev, st)
    assert "--now" not in R.unfinished(ks, dev, st)
    res = env.rotate()  # a plain run, as a person might type it
    assert res.resumed == "step 5" and res.suspected
    assert res.config.old_ck["not_after"] == "remove"
    assert any("record was never written" in n for n in res.notes)
    assert env.state().rotation.suspected
    settled(env)


def test_a_plain_rotation_whose_record_was_never_written_also_resumes_as_suspected(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "line appended")
    res = env.rotate()
    assert res.resumed == "step 5" and res.suspected and res.config.removals() == (res.config.old_ck["id"],)
    settled(env)


def test_advice_says_plain_rotate_to_finish_and_now_only_while_fresh_keys_are_owed(tmp_path):
    env = Env(tmp_path)
    env.clock_fails_at = 2  # --now, before its append: nothing landed, the live keys are still the suspected ones
    with pytest.raises(R.RotationError, match="irp roam rotate --now.*the current keys are still the ones you "
                                              "suspect"):
        env.rotate(suspected=True)
    env.checks, env.clock_fails_at = [], 3  # --now, its line landed: only finishing is owed
    with pytest.raises(R.RotationError, match="finish with irp roam rotate .it finishes with no overlap") as exc:
        env.rotate(suspected=True)
    assert "--now" not in str(exc.value)
    ks, dev, st = env.keystore(), env.devices(), env.state()
    why = R.unfinished(ks, dev, st)
    assert "finish with irp roam rotate (it finishes with no overlap" in why and "--now" not in why
    with pytest.raises(R.PublishRefused, match="no overlap") as refused:
        R.check_publish(ks, dev, st, env.clock.t)
    assert "--now" not in str(refused.value)
    env.clock_fails_at = None
    res = env.rotate()  # plain: finishes it, keeping suspected
    assert res.resumed == "steps 6 and 7" and res.suspected and res.previous is None
    settled(env)


# B: --now while a plain rotation is open upgrades it, finishes it and rotates again.

def test_now_while_a_plain_rotation_is_open_upgrades_it_then_rotates_again(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    assert not first.closed and not first.suspected
    env.confirm = True
    calls = len(env.calls)
    res = env.rotate(suspected=True)
    prev = res.previous
    assert prev is not None and prev.resumed == "steps 6 and 7" and prev.suspected and prev.closed
    assert prev.config.old_ck == {"id": first.config.old_ck["id"], "not_after": "remove"}
    assert any("reader bundle" in n and "at once" in n for n in prev.notes)
    shown = [c for c in env.calls[calls:] if c[0] == "show"]
    assert shown[0][1].old_ck["not_after"] == "remove"  # upgraded before step 7 printed anything
    assert res.suspected and res.resumed is None and len(env.rotate_lines()) == 2
    assert res.config.old_ck == {"id": first.config.new_ck["id"], "not_after": "remove"}
    assert len(env.keyring.asked) == 2  # a new tap for the fresh rotation
    run = [c[0] if c[0] != "progress" else c[1] for c in env.calls[calls:]]
    closes = [i for i, n in enumerate(run) if n == "closed"]
    assert len(closes) == 2 and run.count("publish") == 1
    assert run.index("publish") > closes[1]  # never while the suspected keys were live after the upgrade
    settled(env, rotations=2)


def test_now_upgrade_stands_when_the_new_tap_is_cancelled(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    env.confirm = True
    env.keyring.cancel = True
    calls = len(env.calls)
    with pytest.raises(R.RotationError, match="cancel.*stands.*irp roam rotate --now.*the current keys are still "
                                              "the ones you suspect"):
        env.rotate(suspected=True)
    assert ("publish",) not in env.calls[calls:]  # no publish in this run
    st = env.state()
    assert st.rotation.rotate_idx == first.rotate_idx and st.rotation.suspected and st.rotation.closed
    assert st.rotation.old_ck == {"id": first.config.old_ck["id"], "not_after": "remove"}
    assert len(env.rotate_lines()) == 1


def test_now_resuming_a_plain_rotation_at_step_5_upgrades_it_and_re_mints_the_probe(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "record written")
    old_ck = sig.key_id("ck", sig.public_key(env.ks.ck_seed))
    plain = env.state()
    assert not plain.rotation.suspected and plain.probes[0].nbf != ts(_at(plain.rotation.at) + timedelta(seconds=120))
    res = env.rotate(suspected=True)
    prev = res.previous
    assert prev.resumed == "step 5" and prev.suspected and prev.config.old_ck == {"id": old_ck, "not_after": "remove"}
    st = env.state()
    mine = [p for p in st.probes if p.iss == old_ck]
    assert len(mine) == 1 and mine[0].nbf == ts(_at(prev.at) + timedelta(seconds=120))  # re-minted, nbf follows
    assert len(env.rotate_lines()) == 2
    settled(env, rotations=2)


def test_now_on_an_already_suspected_open_rotation_finishes_it_then_rotates_again(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate(suspected=True)
    env.confirm = True
    res = env.rotate(suspected=True)  # --now always ends with fresh keys
    assert res.previous is not None and res.previous.rotate_idx == first.rotate_idx and res.previous.closed
    assert res.resumed is None and res.suspected and len(env.rotate_lines()) == 2
    settled(env, rotations=2)


def test_plain_rotate_finishes_an_open_suspected_rotation_keeping_suspected(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate(suspected=True)
    env.confirm = True
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and res.suspected and res.previous is None
    assert res.config.removals() == (res.config.old_ck["id"],) and len(env.rotate_lines()) == 1
    settled(env)


# C: a rebuilt record persists who still needs a delivery.

def test_a_rebuilt_record_persists_held_before_anything_else(tmp_path, monkeypatch):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery, env.confirm = {b}, False
    env.rotate()
    env.ledger.state_path.unlink()  # held [b] is lost with it
    real = S.save_state

    def write_then_die(path, state):
        real(path, state)
        raise Kill()
    with monkeypatch.context() as m:
        m.setattr(S, "save_state", write_then_die)
        with pytest.raises(Kill):
            env.rotate()  # dies right after the rebuilt record is written
    st = env.state()
    assert st.rotation.suspected and st.rotation.old_ck is None and set(st.held) == {a, b}
    env.refuse_delivery, env.confirm = set(), True
    env.rotate()
    settled(env)


# D: a keystore from between two rotations is ALARM, not "unfinished".

def test_a_keystore_restored_from_between_two_rotations_is_alarm_everywhere(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    copy = {n: (env.ledger.keys_dir / n).read_bytes() for n in ("keystore.bin", "kek.bin")}
    stale_kid = env.keystore().dk_id
    env.clock.t += DAY
    env.rotate()
    for n, data in copy.items():
        (env.ledger.keys_dir / n).write_bytes(data)
    ks, dev, st = env.keystore(), env.devices(), env.state()
    retired_at = dev.state().status[stale_kid][1]
    assert R.rotation_open(dev, ks.dk_id, st) is None and R.unfinished(ks, dev, st) is None
    with pytest.raises(R.RotationAlarm, match=f"retired at devices line {retired_at}"):
        R.check_signer(ks, dev, st)
    with pytest.raises(R.PublishRefused, match="retired") as exc:
        R.check_publish(ks, dev, st, env.clock.t)
    assert exc.value.alarm
    with pytest.raises(R.RotationAlarm, match="retired"):
        with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False):
            pass
    before = env.snapshot()
    with pytest.raises(R.RotationAlarm, match="retired"):
        env.rotate()
    assert env.snapshot() == before


# E: the owner's decision on box_prev, pinned and stated.

def test_a_second_rotation_before_box_prev_until_drops_the_earlier_box_key_early_and_says_so(tmp_path):
    env = Env(tmp_path)
    original_box = env.ks.dk_box
    first = env.rotate()
    assert not any("dropped early" in n for n in first.notes)
    first_box = env.keystore().dk_box
    env.clock.t += 2 * DAY
    res = env.rotate()
    ks = env.keystore()
    assert ks.box_prev.dk_box == first_box and original_box not in (ks.dk_box, ks.box_prev.dk_box)
    assert any("dropped early" in n for n in res.notes)
    assert "dropped early" in (R._Engine._finish_keys.__doc__ or "")


# F: failures in steps 6 and 7 get the after-failure test and a clear outcome.

def test_a_failure_during_the_renewals_says_the_rotation_is_done_and_how_far_it_got(tmp_path):
    env = Env(tmp_path)
    env.clock_fails_at = 4  # step 1, step 4, the first renewal pass; the second renewal fails
    with pytest.raises(R.RotationError, match="the rotation is done; 1 of 2 readers renewed; finish with irp roam "
                                              "rotate"):
        env.rotate()
    env.clock_fails_at = None
    res = env.rotate()
    assert res.resumed == "steps 6 and 7"
    settled(env)


def test_the_clock_is_checked_before_each_renewal_append(tmp_path):
    env = Env(tmp_path)
    env.clock_fails_at = 3
    with pytest.raises(R.RotationError, match="0 of 2 readers renewed"):
        env.rotate()
    assert not [x for x in L.split_log(env.ledger.readers_path.read_bytes()) if b'"reader_renew"' in x]
    env.clock_fails_at = None
    env.rotate()
    settled(env)


def test_a_failed_renewal_append_is_a_rotation_error_not_a_raw_log_error(tmp_path, monkeypatch):
    env = Env(tmp_path)
    armed = {"on": False}
    real_write = os.write

    def failing(fd, data):
        if armed["on"]:
            armed["on"] = False
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    def arm(step):
        if step == "keys live":
            armed["on"] = True
    monkeypatch.setattr(L.os, "write", failing)
    env.on_progress = arm
    with pytest.raises(R.RotationError, match="the rotation is done; 0 of 2 readers renewed") as exc:
        env.rotate()
    assert not isinstance(exc.value, LogError)
    env.on_progress = None
    monkeypatch.undo()
    env.rotate()
    settled(env)


def test_a_failure_in_step_7_says_the_rotation_is_done(tmp_path):
    env = Env(tmp_path)
    real = env._deliver

    def broken(reader_id, identity, expires):
        raise OSError(5, "the pasteboard is gone")
    env.hooks.deliver = broken
    with pytest.raises(R.RotationError, match="the rotation is done; 2 of 2 readers renewed"):
        env.rotate()
    env.hooks.deliver = real
    env.rotate()
    settled(env)


# G: a signing session that repairs a torn tail reloads the keystore.

def _torn_devices(env: Env) -> None:
    with open(env.ledger.devices_path, "ab") as fh:
        fh.write(b'{"body":{"at":"2026')


def test_a_signing_session_reloads_the_keystore_after_taking_the_lock_exclusively(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _torn_devices(env)
    changed = dataclasses.replace(env.ks, tsa_creds={"disig": "u:new"})
    real = RoamLock.make_exclusive

    def another_run_saves_in_between(self):
        real(self)
        save_keystore(env.ledger.keys_dir, changed, env.source(), os.urandom)  # e.g. a TSA credential change
    monkeypatch.setattr(RoamLock, "make_exclusive", another_run_saves_in_between)
    with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False) as s:
        assert s.lock.exclusive and s.ks == changed and s.digest == keystore_digest(env.ledger.keys_dir)
    assert len(list(env.ledger.forks_dir.iterdir())) == 1


def test_a_signing_session_wraps_a_log_error_found_while_repairing(tmp_path):
    env = Env(tmp_path)
    lines = env.kit.lines
    bad = lines[:1] + [lines[2], lines[1]] + lines[3:]  # a middle that doesn't chain
    _private(env.ledger.devices_path, b"".join(x + b"\n" for x in bad))
    _torn_devices(env)
    with pytest.raises(R.RotationAlarm):
        with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False):
            pass


# H: pending is never dropped in the run whose append failed.

def test_pending_survives_a_rollback_whose_sync_failed(tmp_path, monkeypatch):
    for came_back, now in ((False, False), (True, False), (False, True)):
        env = Env(tmp_path / f"{came_back}-{now}")
        armed = {"syncs": 0}
        written = []
        real_sync, real_write = L._full_fsync, os.write

        def sync(fd):
            if armed["syncs"]:
                armed["syncs"] -= 1
                raise OSError(5, "Input/output error")
            return real_sync(fd)

        def write(fd, data):
            if armed["syncs"]:
                written.append(bytes(data))
            return real_write(fd, data)

        def arm(step):
            if step == "pending saved":
                armed["syncs"] = 2  # the append's sync, then the rollback's
        with monkeypatch.context() as m:
            m.setattr(L, "_full_fsync", sync)
            m.setattr(L.os, "write", write)
            env.on_progress = arm
            want = "kept: run irp roam rotate --now, which decides" if now else "kept.*finish with irp roam rotate"
            with pytest.raises(R.RotationError, match=want):
                env.rotate(suspected=now)
            env.on_progress = None
        ks = env.keystore()
        assert ks.pending is not None and env.rotate_lines() == []  # truncated, but maybe not for good
        if came_back:  # the truncate never reached the disk: after a power cut the line is there
            with open(env.ledger.devices_path, "ab") as fh:
                fh.write(written[0])
            res = env.rotate()
            assert res.resumed == "step 5" and res.new_kid == ks.pending.dk_id
        else:
            res = env.rotate(suspected=now)
            assert res.resumed is None and res.new_kid != ks.pending.dk_id and res.suspected == now
        settled(env)


# I: the rules each mutant broke.

def test_pending_named_by_another_line_is_alarm_never_dropped(tmp_path, monkeypatch):
    for clash in ("kid", "box"):
        env = Env(tmp_path / clash)
        _killed_at(env, monkeypatch, "pending saved")
        p = env.keystore().pending
        key = _seeded_key(p.dk_seed if clash == "kid" else h("someone else"))
        box_seed = p.dk_box if clash == "box" else h("someone else's box")
        extra_devices_line(env, "device_enrol", {"device": _custodian_desc(key, box_seed, "laptop-2"),
                                                 "nonce": None}, [env.kit.root, key])
        before = env.snapshot()
        with pytest.raises(R.RotationAlarm, match="disagree"):
            env.rotate()
        assert env.snapshot() == before


def test_a_landed_rotation_whose_new_key_was_revoked_is_alarm(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "line appended")
    p = env.keystore().pending
    extra_devices_line(env, "device_revoke", {"kid": p.dk_id}, [env.kit.root])
    before = env.snapshot()
    with pytest.raises(R.RotationAlarm):
        env.rotate()
    assert env.snapshot() == before


def test_an_open_rotation_whose_keystore_box_isnt_the_logs_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    ks = env.keystore()
    save_keystore(env.ledger.keys_dir, dataclasses.replace(ks, dk_box=h("a box nobody enrolled")), env.source(),
                  os.urandom)
    env.confirm = True
    with pytest.raises(R.RotationAlarm, match="box"):
        env.rotate()


def test_a_record_naming_another_new_ck_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    st = env.state()
    pub = sig.public_key(h("another ck"))
    other = dataclasses.replace(st.rotation, new_ck={"id": sig.key_id("ck", pub), "pub": sig.b64url_encode(pub),
                                                     "not_after": st.rotation.new_ck["not_after"]})
    S.save_state(env.ledger.state_path, dataclasses.replace(st, rotation=other))
    with pytest.raises(R.RotationAlarm, match="new CK"):
        env.rotate()


def test_a_failure_while_the_log_moved_on_without_this_laptop_is_alarm(tmp_path):
    env = Env(tmp_path)

    def revoke_behind_its_back(step):
        if step == "pending saved":
            extra_devices_line(env, "device_revoke", {"kid": env.laptop}, [env.kit.root])
    env.on_progress = revoke_behind_its_back
    env.clock_fails_at = 2
    with pytest.raises(R.RotationAlarm):
        env.rotate()


def test_the_after_failure_test_reloads_the_keystore(tmp_path, monkeypatch):
    env = Env(tmp_path)
    src = env.source()
    real = src.promote
    armed = {"on": False}

    def promote(kek):
        if armed["on"]:
            armed["on"] = False
            raise OSError(5, "Input/output error")
        return real(kek)
    src.promote = promote

    def arm(step):
        if step == "record written":
            armed["on"] = True
    env.on_progress = arm
    with pytest.raises(R.RotationError, match="new keys are live"):
        env.rotate(sources=[src])
    env.on_progress = None
    assert env.keystore().pending is None
    env.rotate()
    settled(env)


def _revoked_and_ended(env, rk):
    revoked, ended, live = rid("ri/revoked"), rid("ri/ended"), rid("ri/live")
    rk.enrol(revoked, [env.laptop, env.aks[0]])
    rk.revoke(revoked, [env.laptop])
    rk.enrol(ended, [env.laptop, env.aks[0]])
    env.kit.recovery(keep=[env.laptop, env.aks[0]], revokes=[])  # epoch 1 ends every earlier reader
    rk.enrol(live, [env.laptop, env.aks[0]])
    env.readers_made = [live]
    env.others = (revoked, ended)


def test_revoked_and_ended_readers_are_never_renewed(tmp_path):
    env = Env(tmp_path, label="ri", readers=_revoked_and_ended)
    res = env.rotate()
    assert res.renewed == (env.readers_made[0],)
    renews = [x for x in L.split_log(env.ledger.readers_path.read_bytes()) if b'"reader_renew"' in x]
    assert len(renews) == 1
    settled(env)


def test_a_held_reader_revoked_meanwhile_leaves_held(tmp_path):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery, env.confirm = {b}, False
    env.rotate()
    assert env.state().held == (b,)
    extra_readers_line(env, "reader_revoke", {"reader_id": b}, _seeded_key(env.keystore().dk_seed))
    env.refuse_delivery, env.confirm = set(), True
    res = env.rotate()
    assert res.renewed == () and res.held == ()
    settled(env)


def test_renewals_cite_the_devices_tail_not_the_rotate_line(tmp_path, monkeypatch):
    env = Env(tmp_path)
    a, b = env.readers_made
    _killed_at(env, monkeypatch, f"renewed {a}")
    r = env.rotate_lines()[0]["idx"]
    later = env.kit.approver("hwkey-2")  # the paper key enrols a spare after the rotate line
    desc = env.kit.descs[later]
    env.kit.lines[-1:] = []
    tail = extra_devices_line(env, "approver_enrol", {"approver": desc}, [env.kit.root, env.kit.keys[later]])
    assert tail > r
    env.rotate()
    renews = [parse_line(x, "readers-entry")[0] for x in L.split_log(env.ledger.readers_path.read_bytes())
              if b'"reader_renew"' in x]
    assert renews[-1]["devices_at"]["idx"] == tail and {x["reader_id"] for x in renews[-2:]} == {a, b}
    settled(env)


def test_new_keys_never_repeat_a_logged_box_or_a_key_the_keystore_holds(tmp_path):
    env = Env(tmp_path)
    reader = env.readers_made[0]
    for name, draws in (("a reader's recipient", [os.urandom(32), h("box/reader/" + reader)]),
                        ("the live box", [os.urandom(32), env.ks.dk_box]),
                        ("the live CK, which no log shows", [os.urandom(32), os.urandom(32), env.ks.ck_seed])):
        draws = iter(draws)
        before = env.snapshot()
        with pytest.raises(R.RotationError, match="seen in a log|already holds"):
            env.rotate(rng=lambda n, draws=draws: next(draws, None) or os.urandom(n))
        assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == \
            {k: v for k, v in before.items() if not k.endswith("roam.lock")}, name
    env.rotate()
    settled(env)


def test_an_approver_whose_assertion_fails_the_checks_moves_on_to_the_next(tmp_path):
    env = Env(tmp_path, approvers=("hwkey-1", "hwkey-2"))
    by_label = {env.kit.descs[a]["label"]: env.kit.keys[a] for a in env.aks}
    by_label["hwkey-1"].flags = 0x01  # a tap without the PIN: UV clear
    try:
        env.rotate()
    finally:
        by_label["hwkey-1"].flags = 0x05
    _, sigs = parse_line(L.split_log(env.devices_bytes())[-1], "devices-entry")
    assert by_label["hwkey-2"].kid in [s["key_id"] for s in sigs]
    assert any("hwkey-2" in m for m in env.said)
    settled(env)


def test_a_torn_reader_renew_line_moves_to_forks_and_the_reader_is_renewed_again(tmp_path, monkeypatch):
    env = Env(tmp_path)
    armed = {"on": False}
    real_write = os.write

    def torn(fd, data):
        if armed["on"] and b'"reader_renew"' in data:
            armed["on"] = False
            real_write(fd, data[:len(data) // 2])
            raise Kill()
        return real_write(fd, data)

    def arm(step):
        if step == "keys live":
            armed["on"] = True
    with monkeypatch.context() as m:
        m.setattr(R._Engine, "_after_failure", lambda self, exc: None)
        m.setattr(L.os, "write", torn)
        env.on_progress = arm
        with pytest.raises(Kill):
            env.rotate()
        env.on_progress = None
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and sorted(res.renewed) == sorted(env.readers_made)
    forks = list(env.ledger.forks_dir.iterdir())
    assert len(forks) == 1 and b'"reader_renew"' in forks[0].read_bytes()
    settled(env)


def test_a_cancelled_passphrase_prompt_in_mode_b(tmp_path):
    from test_roam_keys import FakeAge

    class Cancelling(FakeAge):
        def __init__(self):
            super().__init__()
            self.cancel_at = None
            self.seals = 0

        def __call__(self, argv, data=b""):
            if argv == ["age", "-p"]:
                self.seals += 1
                if self.seals == self.cancel_at:
                    raise KeystoreError("age failed (exit 1)")  # Ctrl-C at the passphrase prompt
            return super().__call__(argv, data)

    age = Cancelling()
    env = Env(tmp_path, age=age)
    age.seals, age.cancel_at = 0, 1  # step 3's save: nothing landed, nothing to keep
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="age failed"):
        env.rotate()
    assert env.keystore() == env.ks and env.rotate_lines() == []
    assert {k: v for k, v in env.snapshot().items() if not k.endswith("roam.lock")} == before
    age.seals, age.cancel_at = 0, 2  # step 5's save: the line landed, so pending is kept
    with pytest.raises(R.RotationError, match="landed"):
        env.rotate()
    assert env.keystore().pending is not None and len(env.rotate_lines()) == 1
    age.cancel_at = None
    res = env.rotate()
    assert res.resumed == "step 5"
    settled(env)


DOUBLE_CRASHES = [(2, 3, False), (3, 8, False), (5, 4, True), (7, 2, True), (9, 9, False), (12, 5, True),
                  (15, 3, False), (18, 6, True), (22, 2, False), (25, 4, True)]


@pytest.mark.parametrize("first,second,lose_state", DOUBLE_CRASHES)
def test_a_crash_in_one_run_then_another_in_the_resume(tmp_path, monkeypatch, first, second, lose_state):
    monkeypatch.setattr(K, "_full_fsync", lambda fd: None)
    monkeypatch.setattr(L, "_full_fsync", lambda fd: None)
    env = Env(tmp_path)
    for at in (first, second):
        src = env.source()
        with monkeypatch.context() as m:
            m.setattr(R._Engine, "_after_failure", lambda self, exc: None)
            _crash_counter(m, [src], at)
            try:
                env.rotate(sources=[src])
            except Kill:
                pass
        if lose_state and at == first and env.ledger.state_path.exists():
            env.ledger.state_path.unlink()
    env.rotate()
    settled(env, rotations=len(env.rotate_lines()))



# ── Review round 2 ──

OWED = "the current keys are still the ones you suspect"


def test_now_whose_upgraded_record_isnt_confirmed_owes_a_fresh_rotation(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    calls = len(env.calls)
    res = env.rotate(suspected=True)  # the config edit still isn't confirmed
    assert not res.closed and res.suspected and len(env.rotate_lines()) == 1
    assert any("once the config edit is applied, run irp roam rotate --now: " + OWED in n for n in res.notes)
    assert ("publish",) not in env.calls[calls:]
    env.confirm = True
    res = env.rotate(suspected=True)
    assert res.previous is not None and len(env.rotate_lines()) == 2
    settled(env, rotations=2)


def test_now_failing_while_it_finishes_the_open_rotation_owes_a_fresh_rotation(tmp_path):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery, env.confirm = {b}, False
    env.rotate()
    env.refuse_delivery, env.confirm = set(), True
    env.checks, env.clock_fails_at = [], 1  # the first check of the --now run: b's renewal in the finish phase
    with pytest.raises(R.RotationError) as exc:
        env.rotate(suspected=True)
    text = str(exc.value)
    assert "finish with irp roam rotate" in text and "once the config edit is applied, run irp roam rotate --now: " \
        + OWED in text
    assert env.state().rotation.suspected  # the upgrade was written before the failure
    env.clock_fails_at = None
    res = env.rotate(suspected=True)
    assert res.previous is not None and len(env.rotate_lines()) == 2
    settled(env, rotations=2)


def test_the_fresh_rotation_whose_line_landed_is_finished_with_plain_rotate(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    env.confirm = True
    env.checks, env.clock_fails_at = [], 3  # the fresh rotation's step 1 and step 4 pass; its first renewal fails
    with pytest.raises(R.RotationError) as exc:
        env.rotate(suspected=True)
    text = str(exc.value)
    assert "finish with irp roam rotate (it finishes with no overlap" in text and "stands" in text
    assert "--now" not in text
    assert len(env.rotate_lines()) == 2
    env.clock_fails_at = None
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and res.suspected
    settled(env, rotations=2)


def test_pending_is_kept_when_the_devices_log_cant_be_synced_before_it_is_dropped(tmp_path, monkeypatch):
    def unsyncable(fd):
        raise OSError(5, "Input/output error")
    # In a resume: the line is absent, but the log can't be made durable.
    env = Env(tmp_path / "resume")
    _killed_at(env, monkeypatch, "pending saved")
    with monkeypatch.context() as m:
        m.setattr(L, "_full_fsync", unsyncable)
        with pytest.raises(R.RotationError, match="sync.*kept"):
            env.rotate()
    assert env.keystore().pending is not None
    env.rotate()
    settled(env)
    # In the after-failure test: a failure before the append, then the sync fails.
    env = Env(tmp_path / "settle")
    armed = {"on": False}
    real = L._full_fsync

    def sync(fd):
        if armed["on"]:
            raise OSError(5, "Input/output error")
        return real(fd)

    def arm(step):
        if step == "pending saved":
            armed["on"] = True
    env.clock_fails_at = 2
    with monkeypatch.context() as m:
        m.setattr(L, "_full_fsync", sync)
        env.on_progress = arm
        with pytest.raises(R.RotationError, match="sync.*kept"):
            env.rotate()
        env.on_progress = None
    assert env.keystore().pending is not None
    env.clock_fails_at = None
    env.rotate()
    settled(env)


def test_log_writer_sync_reaches_the_file_and_its_folder(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(L, "_full_fsync", lambda fd: synced.append("file"))
    monkeypatch.setattr(L, "_fsync_dir", lambda path: synced.append(("folder", path)))
    with LogWriter(tmp_path / "devices.jsonl", kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        w.sync()
    assert synced == ["file", ("folder", tmp_path)]


def test_a_landed_plain_rotation_without_its_record_advises_the_same_as_unfinished(tmp_path, monkeypatch):
    env = Env(tmp_path)
    real = S.save_state

    def full_disk(path, state):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(S, "save_state", full_disk)
    with pytest.raises(R.RotationError) as exc:
        env.rotate()  # plain, and its record never gets written
    monkeypatch.setattr(S, "save_state", real)
    advice = "finish with irp roam rotate (it finishes with no overlap, so reader bundles and the phone stop at once)"
    assert advice in str(exc.value)
    assert advice in R.unfinished(env.keystore(), env.devices(), env.state())
    res = env.rotate()
    assert res.suspected
    settled(env)


def test_resume_only_on_a_keystore_from_between_rotations_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    copy = {n: (env.ledger.keys_dir / n).read_bytes() for n in ("keystore.bin", "kek.bin")}
    env.clock.t += DAY
    env.rotate()
    for n, data in copy.items():
        (env.ledger.keys_dir / n).write_bytes(data)
    with pytest.raises(R.RotationAlarm, match="retired"):
        R.resume_rotation(env.ledger, sources=[env.source()], rng=os.urandom, clock=env.clock,
                          clock_check=env.clock_check, authenticator=env.keyring, hooks=env.hooks,
                          sleep=env.clock.sleep, say=env.said.append)


# ── Step 2.6: the adopt and checkpoint hooks, held locks and state.json's own keys (§18a) ──

def _names(env: Env, since: int = 0) -> list:
    return [c[0] if c[0] != "progress" else c[1] for c in env.calls[since:]]


def _checkpoint_rec(env: Env, seq: int, strand: str) -> dict:
    """A well-formed state.json checkpoint record (checkpoint.py makes the real ones)."""
    header = canonicalize({"v": 1, "kind": "checkpoint", "ledger_id": LEDGER, "root": env.kit.root.kid,
                           "epoch": 0, "strand": strand, "seq": seq, "prev": None,
                           "created_at": "2026-10-09T09:00:00Z",
                           "devices": {"byte_length": 10, "digest": "sha256-" + "d" * 64},
                           "body_digest": "sha256-" + "b" * 64})
    log = {"byte_length": 10, "byte_digest": "sha256-" + "e" * 64,
           "segments": [{"object": "o/" + "f" * 64, "offset": 0, "length": 10, "sha256": "a" * 64}]}
    return {"epoch": 0, "seq": seq, "strand": strand, "digest": "sha256-" + hashlib.sha256(header).hexdigest(),
            "header": sig.b64url_encode(header), "sig": sig.b64url_encode(b'{"alg":"ed25519"}'),
            "created_at": "2026-10-09T09:00:00Z", "gen_time": None, "label": "NONE", "last_present": None,
            "snapshot_digest": "c" * 64,
            "recipients": [{"id": "rk", "recipient": Identity(h("rk")).recipient().to_string()}],
            "policy_digest": None, "tsa_policy_digest": None,
            "logs": {n: dict(log) for n in ("ledger", "devices", "readers", "disclosures")}}


def _seen_mark(env: Env, seq: int) -> dict:
    rec = _checkpoint_rec(env, seq, env.ks.dk_id)
    mark = {k: rec[k] for k in ("epoch", "seq", "strand", "digest", "header", "sig")}
    mark["devices"] = sig.b64url_encode(env.devices_bytes())
    return mark


def _put_state(env: Env, **changes) -> S.RoamState:
    with RoamLock(env.ledger.keys_dir, exclusive=True, interactive=False) as lock:
        return S.update_state(env.ledger.state_path, lock, exclude_from_backup=env.tm, **changes)


def _no_log_writer_open(env: Env) -> bool:
    import fcntl

    for path in (env.ledger.devices_path, env.ledger.readers_path):
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a LogWriter holds its log's flock while open
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
    return True


def _in_thread(fn, timeout: float = 60.0):
    """Run `fn` in a thread with a deadline: a flock self-deadlock shows up as a thread still alive."""
    out: dict = {}

    def run():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            out["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "deadlocked: the run didn't finish under the timeout"
    if "error" in out:
        raise out["error"]
    return out.get("result")


def test_the_checkpoint_hook_runs_after_the_record_closes_with_the_new_keys_and_no_writer_open(tmp_path):
    env = Env(tmp_path)
    seen_by_hook = []

    def hook(ks, rotate_idx, lock):
        st = S.load_state(env.ledger.state_path)
        seen_by_hook.append((ks.dk_id, rotate_idx, st.rotation.closed, st.rotation.rotate_idx,
                             _no_log_writer_open(env), lock.held, lock.exclusive))
    env.on_checkpoint = hook
    res = env.rotate()
    assert seen_by_hook == [(res.new_kid, res.rotate_idx, True, res.rotate_idx, True, True, True)]
    names = _names(env)
    assert names.index("closed") < names.index("checkpoint") < names.index("publish")
    settled(env)


def test_a_checkpoint_hook_that_fails_leaves_the_rotation_closed_and_reports_it(tmp_path):
    env = Env(tmp_path)

    def hook(ks, rotate_idx, lock):
        raise RuntimeError("ALARM: another active custodian; revoke laptop-2")
    env.on_checkpoint = hook
    res = env.rotate()
    assert res.closed and res.held == () and sorted(d[0] for d in env.delivered) == sorted(env.readers_made)
    assert any(c[0] == "show" for c in env.calls)
    assert any("checkpoint" in n and "revoke laptop-2" in n for n in res.notes)
    assert any("revoke laptop-2" in m for m in env.said)
    assert _names(env)[-1] == "publish"  # the next publish makes the strand's first checkpoint
    settled(env)


def test_a_checkpoint_hook_interrupted_after_the_close_leaves_the_rotation_closed(tmp_path):
    env = Env(tmp_path)

    def hook(ks, rotate_idx, lock):
        raise Kill()
    env.on_checkpoint = hook
    with pytest.raises(Kill):
        env.rotate()
    assert env.state().rotation.closed
    env.on_checkpoint = None
    settled(env)
    with pytest.raises(R.RotationError, match="nothing to resume"):
        env.resume()


def test_no_checkpoint_hook_until_the_record_closes_then_exactly_once(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    assert not first.closed and "checkpoint" not in _names(env) and "publish" not in _names(env)
    env.confirm = True
    calls = len(env.calls)
    res = env.resume()
    assert res.closed and res.rotate_idx == first.rotate_idx
    assert [c for c in env.calls[calls:] if c[0] == "checkpoint"] == [("checkpoint", first.rotate_idx)]
    assert "adopt" not in _names(env, calls)  # adoption runs in a fresh rotation only
    settled(env)


def test_now_over_an_open_rotation_checkpoints_only_the_fresh_strand(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    env.confirm = True
    calls = len(env.calls)
    res = env.rotate(suspected=True)
    assert res.previous is not None and res.previous.closed
    names = _names(env, calls)
    assert [c for c in env.calls[calls:] if c[0] == "checkpoint"] == [("checkpoint", res.rotate_idx)]
    assert res.rotate_idx != first.rotate_idx
    closes = [i for i, n in enumerate(names) if n == "closed"]
    assert names.index("adopt") > closes[0]  # the fresh rotation adopts; the finish doesn't
    assert closes[1] < names.index("checkpoint") < names.index("publish")
    settled(env, rotations=2)


def test_now_resume_only_neither_checkpoints_nor_publishes(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    env.confirm = True
    calls = len(env.calls)
    res = env.resume(suspected=True)
    assert res.closed and res.suspected
    assert "checkpoint" not in _names(env, calls) and "publish" not in _names(env, calls)


def test_adopt_runs_after_the_replay_and_before_the_tap_with_the_live_keys(tmp_path):
    env = Env(tmp_path)
    got = []
    env.on_adopt = lambda ks, lock: got.append((ks.dk_id, env.keyring.asked[:], env.rotate_lines()))
    env.rotate()
    assert got == [(env.ks.dk_id, [], [])]  # the outgoing live key, before any tap or line
    names = _names(env)
    assert names.index("adopt") < names.index("tap") < names.index("signed")


def test_an_adopt_failure_is_reported_and_the_rotation_carries_on(tmp_path):
    env = Env(tmp_path)

    def adopt(ks, lock):
        raise RuntimeError("ALARM: two staged files for seq 4")
    env.on_adopt = adopt
    res = env.rotate()
    assert res.closed and any("adopt" in n and "two staged files for seq 4" in n for n in res.notes)
    assert any("two staged files for seq 4" in m for m in env.said)
    assert "checkpoint" in _names(env) and "publish" in _names(env)
    settled(env)


def test_adoption_is_not_rerun_by_a_resume(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "keys live")
    calls = len(env.calls)
    res = env.rotate()
    assert res.resumed == "steps 6 and 7"
    assert "adopt" not in _names(env, calls) and _names(env, calls).count("checkpoint") == 1
    settled(env)


def test_engine_state_writes_keep_checkpoint_seen_and_epoch_start(tmp_path):
    env = Env(tmp_path)
    mark = _seen_mark(env, 2)
    _put_state(env, checkpoint=_checkpoint_rec(env, 3, env.ks.dk_id), seen=mark, epoch_start=0)
    adopted = _checkpoint_rec(env, 4, env.ks.dk_id)

    def adopt(ks, lock):  # adoption takes a staged seq 4 and writes only `checkpoint`
        S.update_state(env.ledger.state_path, lock, checkpoint=adopted)
    made = {}

    def hook(ks, rotate_idx, lock):  # the hook makes seq 5 on the new strand
        made["rec"] = _checkpoint_rec(env, 5, ks.dk_id)
        S.update_state(env.ledger.state_path, lock, checkpoint=made["rec"])
    published = []
    env.on_adopt, env.on_checkpoint = adopt, hook
    env.on_publish = lambda lock: published.append(S.load_state(env.ledger.state_path).checkpoint)
    env.rotate()
    st = env.state()
    assert st.checkpoint == made["rec"] and st.checkpoint["strand"] == env.keystore().dk_id
    assert st.seen == mark and st.epoch_start == 0
    assert published == [made["rec"]]  # the in-run publish sees what the hook wrote
    settled(env)


def test_seen_and_the_record_survive_a_resume_where_state_json_survived(tmp_path, monkeypatch):
    env = Env(tmp_path)
    mark = _seen_mark(env, 2)
    rec = _checkpoint_rec(env, 3, env.ks.dk_id)
    _put_state(env, checkpoint=rec, seen=mark, epoch_start=0)
    _killed_at(env, monkeypatch, f"renewed {sorted(env.readers_made)[0]}")
    assert env.state().seen == mark and env.state().checkpoint == rec
    env.rotate()
    st = env.state()
    assert st.seen == mark and st.checkpoint == rec and st.epoch_start == 0  # the default hook makes nothing
    settled(env)


def _step_0(env: Env):
    """The hook's step 0, as checkpoint.py does it: no file, or a null record without the epoch marker,
    is ALARM before anything is cleaned, adopted or signed."""
    def hook(ks, rotate_idx, lock):
        st = S.load_state(env.ledger.state_path, required=True)
        if st.checkpoint is None and st.epoch_start != 0:
            raise RuntimeError("ALARM: the checkpoint record is missing: rebuild it from the relay with the "
                               "drill's Path B")
    return hook


def test_state_json_deleted_mid_rotation_closes_and_reports_the_hooks_alarm(tmp_path):
    env = Env(tmp_path)
    _put_state(env, checkpoint=_checkpoint_rec(env, 3, env.ks.dk_id), seen=_seen_mark(env, 2), epoch_start=0)
    env.on_checkpoint = _step_0(env)

    def lose(step):
        if step == "keys live":
            env.ledger.state_path.unlink()
    env.on_progress = lose
    res = env.rotate()
    env.on_progress = None
    assert res.closed and any("checkpoint record is missing" in n for n in res.notes)
    st = env.state()
    assert st.rotation.closed and st.rotation.rotate_idx == res.rotate_idx
    assert (st.checkpoint, st.seen, st.epoch_start) == (None, None, None)  # stays null until Path B or recovery
    settled(env)


def test_a_local_folder_deleted_mid_rotation_gets_its_exclusion_again(tmp_path):
    import shutil

    env = Env(tmp_path)
    _put_state(env, epoch_start=0)
    assert len(env.tm.calls) == 1

    def lose(step):
        if step == "keys live":
            shutil.rmtree(env.ledger.local_dir)
    env.on_progress = lose
    env.rotate()
    env.on_progress = None
    assert len(env.tm.calls) == 2 and env.tm.excluded(env.ledger.local_dir)
    settled(env)


def test_the_first_state_write_creates_local_with_the_exclusion(tmp_path):
    env = Env(tmp_path)
    assert not env.ledger.local_dir.exists()
    env.rotate()
    assert env.ledger.state_path.parent == env.ledger.local_dir == env.ledger.ledger_dir / "local"
    assert len(env.tm.calls) == 1 and env.tm.excluded(env.ledger.local_dir)
    settled(env)


def test_no_exclusion_and_no_local_folder_refuses_before_anything(tmp_path):
    env = Env(tmp_path)
    before = env.snapshot()
    with pytest.raises(R.RotationError, match="Time Machine"):
        env.rotate(exclude_from_backup=None)
    assert env.snapshot() == before and env.keyring.asked == [] and not env.ledger.local_dir.exists()
    env.rotate()
    settled(env)


def test_a_failed_exclusion_at_the_first_state_write_keeps_pending_and_resumes(tmp_path):
    env = Env(tmp_path)
    env.tm.fail = True
    with pytest.raises(R.RotationError, match="landed"):
        env.rotate()
    assert env.keystore().pending is not None and not env.ledger.local_dir.exists()
    env.tm.fail = False
    res = env.rotate()
    assert res.resumed == "step 5" and env.tm.excluded(env.ledger.local_dir)
    settled(env)


def test_rotate_inside_a_held_exclusive_lock_finishes_and_never_releases_it(tmp_path):
    env = Env(tmp_path)
    with RoamLock(env.ledger.keys_dir, exclusive=True, interactive=True) as lock:  # the drill holds it
        res = _in_thread(lambda: env.rotate(lock=lock), timeout=60)
        assert res.closed and lock.held and lock.exclusive
        with pytest.raises(LockBusy):  # still ours
            with RoamLock(env.ledger.keys_dir, exclusive=False, interactive=False):
                pass
        env.keyring.cancel = True
        with pytest.raises(R.RotationError, match="cancel"):
            _in_thread(lambda: env.rotate(lock=lock), timeout=60)
        assert lock.held  # a failure doesn't release it either
        env.keyring.cancel = False
    settled(env)


def test_resume_inside_a_held_exclusive_lock(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    env.confirm = True
    with RoamLock(env.ledger.keys_dir, exclusive=True, interactive=True) as lock:
        res = _in_thread(lambda: env.resume(lock=lock), timeout=60)
        assert res.closed and lock.held
    settled(env)


def test_rotate_refuses_a_lock_it_cant_use(tmp_path):
    env = Env(tmp_path)
    before = env.snapshot()
    with RoamLock(env.ledger.keys_dir, exclusive=False, interactive=False) as shared:
        with pytest.raises(R.RotationError, match="exclusively"):
            env.rotate(lock=shared)
        assert shared.held
    with pytest.raises(R.RotationError, match="exclusively"):
        env.rotate(lock=RoamLock(env.ledger.keys_dir, exclusive=True, interactive=False))  # not held
    with RoamLock(tmp_path / "other-keys", exclusive=True, interactive=False) as other:
        with pytest.raises(R.RotationError, match="another keys folder"):
            env.rotate(lock=other)
    assert {k: v for k, v in env.snapshot().items() if "roam.lock" not in k} == before
    assert env.keyring.asked == []


def test_the_in_run_publish_signs_under_the_rotations_own_lock(tmp_path):
    env = Env(tmp_path)
    signed = []

    def publish(lock):
        with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=True, exclusive=True,
                               lock=lock) as s:
            assert s.lock is lock and s.lock.exclusive
            signed.append((s.ks.dk_id, sig.sign("checkpoint", b"{}", s.ks.dk_seed, s.ks.dk_id)["key_id"]))
        assert lock.held  # the session didn't release the rotation's lock
    env.on_publish = publish
    res = _in_thread(env.rotate, timeout=60)
    assert signed == [(res.new_kid, res.new_kid)]
    settled(env)


def test_a_hook_that_reads_logs_and_writes_state_under_the_lock_doesnt_deadlock(tmp_path):
    env = Env(tmp_path)

    def hook(ks, rotate_idx, lock):
        data = env.ledger.devices_path.read_bytes() + env.ledger.readers_path.read_bytes()
        assert data
        S.update_state(env.ledger.state_path, lock, checkpoint=_checkpoint_rec(env, 1, ks.dk_id))
    env.on_checkpoint = hook
    _in_thread(env.rotate, timeout=60)
    assert env.state().checkpoint["seq"] == 1
    settled(env)


def test_signing_session_has_an_exclusive_mode(tmp_path):
    env = Env(tmp_path)
    with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False, exclusive=True) as s:
        assert s.lock.held and s.lock.exclusive and s.ks == env.ks
        with pytest.raises(LockBusy):
            with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False):
                pass
    with RoamLock(env.ledger.keys_dir, exclusive=False, interactive=False):  # released on exit
        pass


def test_signing_session_refuses_a_borrowed_lock_it_cant_use(tmp_path):
    env = Env(tmp_path)
    with RoamLock(env.ledger.keys_dir, exclusive=False, interactive=False) as shared:
        with pytest.raises(R.RotationError, match="exclusively"):
            with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False, lock=shared):
                pass
        assert shared.held


def test_the_ledger_names_the_local_and_staging_folders_and_tsa_json(tmp_path):
    env = Env(tmp_path)
    led = env.ledger
    assert led.state_path == led.ledger_dir / "local" / "state.json"
    assert led.local_dir == led.ledger_dir / "local" and led.staging_dir == led.ledger_dir / "staging"
    assert led.home == led.keys_dir.parent and led.tsa_path == tmp_path / "local" / "tsa.json"


# ── Fresh keys owed: the --now marker (owner-approved amendment to §14.6a and §18a) ──

OWED_NOW = "fresh keys are still owed: run irp roam rotate --now"


def _no_approver(env: Env) -> None:
    """The hardware key isn't there: every approver is tried and none answers."""
    env.keyring.by_cred = {}


def _approver_back(env: Env) -> None:
    env.keyring.by_cred = {env.kit.keys[a].cred_id: env.kit.keys[a] for a in env.aks}


def _writes(monkeypatch) -> list:
    """Every update_state call, in order: the keys each one replaced."""
    calls = []
    real = S.update_state

    def spy(path, lock, **kw):
        calls.append({k: v for k, v in kw.items() if k in S.STATE_KEYS})
        return real(path, lock, **kw)
    monkeypatch.setattr(S, "update_state", spy)
    return calls


def _refuses_naming_now(env: Env) -> None:
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert OWED_NOW in R.unfinished(ks, dev, st)
    with pytest.raises(R.Unfinished, match="rotate --now"):
        R.check_signer(ks, dev, st)
    with pytest.raises(R.PublishRefused, match="rotate --now") as refused:
        R.check_publish(ks, dev, st, env.clock.t)
    assert not refused.value.alarm
    with pytest.raises(R.Unfinished, match="rotate --now"):
        with R.signing_session(env.ledger, [env.source()], now=env.clock.t, interactive=False):
            pass


def test_now_writes_the_marker_before_anything_else(tmp_path, monkeypatch):
    env = Env(tmp_path)
    calls = _writes(monkeypatch)
    lines = len(L.split_log(env.devices_bytes()))
    seen_by_adopt = []
    env.on_adopt = lambda ks, lock: seen_by_adopt.append(S.load_state(env.ledger.state_path).fresh_keys_owed)
    res = env.rotate(suspected=True)
    assert calls[0] == {"rotation": None, "probes": (), "held": (),
                        "fresh_keys_owed": {"since": ts(env.clock.t), "from_idx": lines}}
    assert seen_by_adopt == [calls[0]["fresh_keys_owed"]]  # on file before the adopt hook and the tap
    assert res.closed and res.rotate_idx == lines and env.state().fresh_keys_owed is None
    settled(env)


def test_now_over_an_open_rotation_writes_the_marker_before_the_resume(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    env.confirm = True
    open_record = env.state().rotation
    calls = _writes(monkeypatch)
    res = env.rotate(suspected=True)
    assert calls[0]["fresh_keys_owed"]["from_idx"] == first.rotate_idx + 1
    assert calls[0]["rotation"] == open_record and not open_record.suspected  # before the upgrade is written
    upgrade = calls[1]
    assert upgrade["rotation"].suspected and upgrade["fresh_keys_owed"] is not None  # the upgrade keeps it
    assert res.previous.rotate_idx == first.rotate_idx and env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_now_whose_fresh_tap_fails_owes_fresh_keys_and_everything_that_signs_refuses(tmp_path):
    env = Env(tmp_path)
    lines = len(L.split_log(env.devices_bytes()))
    _no_approver(env)
    before = {k: v for k, v in env.snapshot().items() if "state.json" not in k and "roam.lock" not in k}
    with pytest.raises(R.RotationError, match="no approver answered.*rotate --now"):
        env.rotate(suspected=True)
    assert {k: v for k, v in env.snapshot().items() if "state.json" not in k and "roam.lock" not in k} == before
    assert env.state().fresh_keys_owed == {"since": ts(env.clock.t), "from_idx": lines}
    _refuses_naming_now(env)
    assert R.unfinished(env.keystore(), env.devices(), env.state()) == \
        OWED_NOW + " (the current keys are still the ones you suspect)"


def test_a_plain_rotate_never_stands_in_for_now_and_never_clears_the_marker(tmp_path):
    env = Env(tmp_path)
    _no_approver(env)
    with pytest.raises(R.RotationError):
        env.rotate(suspected=True)
    owed = env.state().fresh_keys_owed
    _approver_back(env)
    asked = len(env.keyring.asked)
    with pytest.raises(R.RotationError, match=OWED_NOW):
        env.rotate()  # plain: it would keep the suspected CK for 7 days, so it never starts while owed
    assert len(env.keyring.asked) == asked and env.rotate_lines() == [] and env.state().fresh_keys_owed == owed
    res = env.rotate(suspected=True)
    assert res.closed and res.suspected and env.state().fresh_keys_owed is None
    settled(env)


def test_the_finish_phase_never_clears_the_marker(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()
    env.confirm = True
    _no_approver(env)
    with pytest.raises(R.RotationError, match="stands.*rotate --now"):
        env.rotate(suspected=True)
    st = env.state()
    assert st.rotation.rotate_idx == first.rotate_idx and st.rotation.closed and st.rotation.suspected
    assert st.fresh_keys_owed == {"since": st.fresh_keys_owed["since"], "from_idx": first.rotate_idx + 1}
    _refuses_naming_now(env)
    _approver_back(env)
    with pytest.raises(R.RotationError, match=OWED_NOW):
        env.rotate()  # nothing is open, so a plain run would rotate: refused
    res = env.rotate(suspected=True)
    assert res.previous is None and res.closed and env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_a_plain_rotate_finishing_an_older_rotation_says_fresh_keys_are_still_owed(tmp_path):
    env = Env(tmp_path)
    a, b = env.readers_made
    env.refuse_delivery, env.confirm = {b}, False
    first = env.rotate()
    env.refuse_delivery, env.confirm = set(), True
    env.checks, env.clock_fails_at = [], 1  # the --now finish phase fails at its first clock check
    with pytest.raises(R.RotationError):
        env.rotate(suspected=True)
    env.clock_fails_at = None
    owed = env.state().fresh_keys_owed
    assert owed is not None and owed["from_idx"] == first.rotate_idx + 1
    assert OWED_NOW in R.unfinished(env.keystore(), env.devices(), env.state())
    res = env.rotate()  # plain: finishes the older rotation, which never clears the marker
    assert res.closed and res.rotate_idx == first.rotate_idx and any(OWED_NOW in n for n in res.notes)
    assert env.state().fresh_keys_owed == owed
    _refuses_naming_now(env)
    res = env.rotate(suspected=True)
    assert res.previous is None and env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_the_fresh_rotations_close_clears_the_marker_in_the_same_write_before_the_hook(tmp_path, monkeypatch):
    env = Env(tmp_path)
    calls = _writes(monkeypatch)
    at_hook = []
    env.on_checkpoint = lambda ks, idx, lock: at_hook.append(S.load_state(env.ledger.state_path).fresh_keys_owed)
    res = env.rotate(suspected=True)
    closing = [c for c in calls if c.get("rotation") is not None and c["rotation"].closed]
    assert len(closing) == 1 and closing[0]["fresh_keys_owed"] is None
    assert all(c["fresh_keys_owed"] is not None for c in calls[:calls.index(closing[0])])
    assert at_hook == [None]  # the hook makes the fresh strand's first checkpoint with the marker cleared
    assert res.closed and env.state().fresh_keys_owed is None
    settled(env)


def test_a_plain_rotate_that_finishes_the_fresh_rotation_clears_the_marker(tmp_path):
    env = Env(tmp_path)
    env.checks, env.clock_fails_at = [], 3  # --now: step 1 and step 4 pass, its first renewal fails
    with pytest.raises(R.RotationError) as exc:
        env.rotate(suspected=True)
    assert "--now" not in str(exc.value)  # the fresh line landed: only finishing is owed
    st = env.state()
    assert st.fresh_keys_owed is not None and st.rotation.suspected and not st.rotation.closed
    why = R.unfinished(env.keystore(), env.devices(), st)
    assert "finish with irp roam rotate (it finishes with no overlap" in why and "--now" not in why
    env.clock_fails_at = None
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and res.closed and env.state().fresh_keys_owed is None
    settled(env)


def test_now_again_over_an_unclosed_fresh_rotation_finishes_it_and_rotates_once_more(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate(suspected=True)
    assert not first.closed and env.state().fresh_keys_owed["from_idx"] == first.rotate_idx
    env.confirm = True
    res = env.rotate(suspected=True)  # --now always ends with keys made after it was asked
    assert res.previous.rotate_idx == first.rotate_idx and res.rotate_idx > first.rotate_idx
    assert env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_resume_only_with_now_leaves_fresh_keys_owed(tmp_path):
    env = Env(tmp_path)
    env.confirm = False
    env.rotate()
    env.confirm = True
    res = env.resume(suspected=True)
    assert res.closed and res.suspected and env.state().fresh_keys_owed is not None
    _refuses_naming_now(env)
    res = env.rotate(suspected=True)
    assert res.closed and env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_a_marker_that_cant_be_written_is_said_and_the_rotation_carries_on(tmp_path, monkeypatch):
    env = Env(tmp_path)
    real = S.save_state
    fails = {"n": 1}

    def save(path, state):
        if fails["n"]:
            fails["n"] -= 1
            raise OSError(28, "No space left on device")
        return real(path, state)
    monkeypatch.setattr(S, "save_state", save)
    res = env.rotate(suspected=True)
    assert res.closed and env.state().fresh_keys_owed is None
    assert any("fresh keys owed" in m and "No space left" in m for m in env.said)
    settled(env)


# ── Review round 2, second pass: the marker carries --now's intent to every later run ──

def _ck(seed: bytes) -> str:
    return sig.key_id("ck", sig.public_key(seed))


def test_a_plain_run_finishing_a_landed_rotation_from_before_now_finishes_it_as_suspected(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "record written")  # the plain record is on file and its keys are pending
    ck0 = _ck(env.ks.ck_seed)
    assert not env.state().rotation.suspected and env.keystore().pending is not None

    def unreadable(ck_id):
        raise RuntimeError("the quarantine list couldn't be read")
    env.hooks = dataclasses.replace(env.hooks, quarantine=unreadable)
    with pytest.raises(R.RotationError) as exc:
        env.rotate(suspected=True)  # stops before it writes the upgraded record
    text = str(exc.value)
    assert "finish with irp roam rotate (it finishes with no overlap" in text
    st = env.state()
    assert not st.rotation.suspected and st.fresh_keys_owed["from_idx"] == st.rotation.rotate_idx + 1
    env.hooks = dataclasses.replace(env.hooks, quarantine=env._quarantine)
    res = env.rotate()  # plain, as the advice says
    assert res.resumed == "step 5" and res.suspected
    assert res.config.removals() == (ck0,) and ck0 not in res.config.issuers()
    assert any("fresh keys are owed after --now, so it finishes as suspected" in n for n in res.notes)
    st = env.state()
    assert st.rotation.closed and st.rotation.suspected and st.fresh_keys_owed is not None  # r < from_idx
    mine = [p for p in st.probes if p.iss == ck0]
    assert len(mine) == 1 and mine[0].nbf == ts(_at(res.at) + timedelta(seconds=120))  # minted for the removal
    _refuses_naming_now(env)
    res = env.rotate(suspected=True)
    assert res.previous is None and env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_a_plain_run_after_a_now_killed_before_its_upgrade_finishes_the_open_rotation_as_suspected(tmp_path,
                                                                                                    monkeypatch):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate()  # open and plain, its new keys live
    env.confirm = True
    ck0 = first.config.old_ck["id"]
    real = S.update_state
    n = {"calls": 0}

    def second_write_dies(path, lock, **kw):
        n["calls"] += 1
        if n["calls"] == 2:
            raise Kill()
        return real(path, lock, **kw)
    with monkeypatch.context() as m:
        m.setattr(S, "update_state", second_write_dies)
        with pytest.raises(Kill):
            env.rotate(suspected=True)  # the marker lands, the upgraded record doesn't
    st = env.state()
    assert st.fresh_keys_owed["from_idx"] == first.rotate_idx + 1 and not st.rotation.suspected
    assert OWED_NOW in R.unfinished(env.keystore(), env.devices(), st)
    res = env.rotate()
    assert res.resumed == "steps 6 and 7" and res.rotate_idx == first.rotate_idx and res.suspected
    assert res.config.removals() == (ck0,) and ck0 not in res.config.issuers()
    shown = [c for c in env.calls if c[0] == "show"][-1][1]
    assert f"  remove {ck0} now" in shown.lines()
    assert any("fresh keys are owed after --now, so it finishes as suspected" in n for n in res.notes)
    assert env.state().fresh_keys_owed is not None
    res = env.rotate(suspected=True)
    assert env.state().fresh_keys_owed is None
    settled(env, rotations=2)


def test_a_now_run_forgets_its_marker_once_the_fresh_close_cleared_it(tmp_path):
    env = Env(tmp_path / "notes")
    res = env.rotate(suspected=True)
    assert res.closed and not any(OWED_NOW in n for n in res.notes)
    settled(env)
    env = Env(tmp_path / "publish")

    def refused(lock):
        raise R.PublishRefused("the relay refused")
    env.on_publish = refused
    with pytest.raises(R.RotationError, match="relay refused"):
        env.rotate(suspected=True)  # fails after the fresh close: what failed came after it
    st = env.state()
    assert st.rotation.closed and st.fresh_keys_owed is None
    R.check_signer(env.keystore(), env.devices(), st)
    R.check_publish(env.keystore(), env.devices(), st, env.clock.t)


def test_pending_from_before_now_whose_line_landed_names_now(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _killed_at(env, monkeypatch, "line appended")  # plain: pending set, the line landed, no record
    landed = len(L.split_log(env.devices_bytes())) - 1

    def refuse(ck_id):
        raise R.RotationError("the quarantine list couldn't be read")
    env.hooks = dataclasses.replace(env.hooks, quarantine=refuse)
    with pytest.raises(R.RotationError):
        env.rotate(suspected=True)
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert ks.pending is not None and st.fresh_keys_owed["from_idx"] == landed + 1
    assert OWED_NOW in R.unfinished(ks, dev, st)
    with pytest.raises(R.PublishRefused, match="rotate --now"):
        R.check_publish(ks, dev, st, env.clock.t)


def test_pending_whose_line_is_absent_while_fresh_keys_are_owed_names_now(tmp_path, monkeypatch):
    env = Env(tmp_path)
    armed = {"syncs": 0}
    real_sync = L._full_fsync

    def sync(fd):
        if armed["syncs"]:
            armed["syncs"] -= 1
            raise OSError(5, "Input/output error")
        return real_sync(fd)

    def arm(step):
        if step == "pending saved":
            armed["syncs"] = 2  # the append's sync, then the rollback's
    with monkeypatch.context() as m:
        m.setattr(L, "_full_fsync", sync)
        env.on_progress = arm
        with pytest.raises(R.RotationError, match="kept: run irp roam rotate --now"):
            env.rotate(suspected=True)
        env.on_progress = None
    ks, dev, st = env.keystore(), env.devices(), env.state()
    assert ks.pending is not None and env.rotate_lines() == [] and st.fresh_keys_owed is not None
    assert OWED_NOW in R.unfinished(ks, dev, st)
    with pytest.raises(R.PublishRefused, match="rotate --now"):
        R.check_publish(ks, dev, st, env.clock.t)
    res = env.rotate(suspected=True)
    assert res.resumed is None and res.suspected
    settled(env)


def test_a_stale_marker_on_file_is_replaced_by_the_runs_own_where_it_stops(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.confirm = False
    first = env.rotate(suspected=True)  # the fresh rotation stays open: its line is at from_idx
    f0 = env.state().fresh_keys_owed["from_idx"]
    assert f0 == first.rotate_idx
    env.confirm = True
    real = S.save_state
    fails = {"n": 1}

    def save(path, state, **kw):
        if fails["n"]:
            fails["n"] -= 1
            raise OSError(28, "No space left on device")
        return real(path, state, **kw)

    def unconfirmed(config):
        raise R.RotationError("the config edit couldn't be confirmed")
    monkeypatch.setattr(S, "save_state", save)
    env.hooks = dataclasses.replace(env.hooks, confirm_config=unconfirmed)
    with pytest.raises(R.RotationError):
        env.rotate(suspected=True)  # its marker write fails first, then it stops finishing the open one
    st = env.state()
    assert st.fresh_keys_owed["from_idx"] == f0 + 1
    assert OWED_NOW in R.unfinished(env.keystore(), env.devices(), st)


def test_resume_with_now_and_nothing_unfinished_says_fresh_keys_are_now_owed(tmp_path):
    env = Env(tmp_path)
    env.rotate()
    with pytest.raises(R.RotationError, match="nothing to resume.*rotate --now"):
        env.resume(suspected=True)
    assert env.state().fresh_keys_owed is not None
    _refuses_naming_now(env)
    with pytest.raises(R.RotationError) as plain:
        env.resume()
    assert "nothing to resume" in str(plain.value)
