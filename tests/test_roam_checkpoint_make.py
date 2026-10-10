"""Roaming IRP, Cut 1 step 2.6, part 2: making, staging and adopting checkpoints (spec v0.3 §18a).

`make_checkpoint` runs under the caller's roam.lock, held exclusively, in the §18a order: trust state.json (step
0), clean staging/ (1), adopt what's staged above the record (2), stop in the rotation hook when the new strand
already has its first checkpoint (3), check C1, the signer and the clock (4), pick seq and prev (5), check the
slot isn't taken (6), build in memory with the TSA step (7), and write segments, the sidecar, the custodian file
and then state.json, each by temp, sync, rename and folder sync (8). Adoption takes only what's above the record,
verified by the same rules a reader uses plus the custodian side, and leaves the files where they are on ALARM.
`due` is evaluated after adoption. `compare_seen` checks what the fetch paths bring back against the local marks
(ROLLBACK, FORK with a persisted proof, the epoch change, the Cut 1 foreign head), and `verify_fork_proof` checks
a proof from the pinned root alone. The rotation engine gets `adopt` and `checkpoint` hooks from
`rotation_hooks`.

These tests drive real files in a temp folder: the logs, a keystore under a plain KEK file, state.json, staging/,
a mirror folder, keys/roam.lock. The clock, the clock check, the relay's answers, Time Machine and the hardware
key are fakes; the TSA is tests/roam_faketsa.py's FakeTSA over real TLS on 127.0.0.1. A `Kill` (BaseException)
stands in for a power cut at each write. All names and values are neutral test values.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("cryptography")
pytest.importorskip("asn1crypto")
pytest.importorskip("rfc8785")

import roam_faketsa as fk  # noqa: E402
from roam_faketsa import ORG  # noqa: E402
from roam_logkit import LEDGER, DevicesKit, EdKey, ReadersKit, approver_key, h, phone_key, sign_body, ts  # noqa: E402

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import age, approver, keys, sig, tsa  # noqa: E402
from irp.roam import checkpoint as C  # noqa: E402
from irp.roam import logs as L  # noqa: E402
from irp.roam import rotation as R  # noqa: E402
from irp.roam import state as S  # noqa: E402
from irp.roam.age import Identity  # noqa: E402
from irp.roam.keys import FileKek, Keystore, Pending, RoamLock, load_keystore, load_locked, save_keystore  # noqa: E402
from irp.roam.logs import replay_devices  # noqa: E402
from irp.roam.tsa import TsaEntry, TsaPin  # noqa: E402

RK = h("make tests/rk")
RK_ID = Identity(RK)
EK = {e: keys.epoch_keys(RK, e) for e in range(3)}
ENTRIES = [{"id": f"IRP-2001-01-01-{i:03d}", "type": "decision", "title": f"neutral title {i}"} for i in range(1, 4)]
KILLS = ("write segment", "rename segment", "write sidecar", "rename sidecar", "write custodian", "rename custodian",
         "write state")


def jsonl(entries) -> bytes:
    return b"".join(canonicalize(e) + b"\n" for e in entries)


def entry(i: int) -> dict:
    return {"id": f"IRP-2001-01-01-{i:03d}", "type": "decision", "title": f"neutral title {i}"}


def rid(name: str) -> str:
    return "rd-" + hashlib.sha256(name.encode()).hexdigest()[:32]


class Kill(BaseException):
    """A power cut or kill -9: nothing may catch it and carry on."""


class SilentFile(FileKek):
    """The plain KEK file without its warning."""

    @staticmethod
    def _warn():
        pass


class FakeClock:
    """The clock every run reads. `sleep` moves it on unless it's frozen (a clock that never catches up)."""

    def __init__(self, t):
        self.t, self.frozen, self.slept = t.replace(microsecond=0), False, 0

    def __call__(self):
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        if not self.frozen:
            self.t += timedelta(seconds=seconds)


class Keyring:
    """The plugged-in hardware key: answers only for the credentials it holds, and notes each tap."""

    def __init__(self, *keys_, on_ask=None):
        self.by_cred = {k.cred_id: k for k in keys_}
        self.on_ask = on_ask

    def get_assertion(self, rp_id, client_data_hash, cred_id):
        if self.on_ask:
            self.on_ask()
        key = self.by_cred.get(cred_id)
        if key is None:
            raise approver.NoCredential()
        return key.get_assertion(rp_id, client_data_hash, cred_id)


class FakeTM:
    """`tmutil addexclusion` plus `isexcluded`: records each folder by inode (a folder exclusion moves with it)."""

    def __init__(self):
        self.calls = []

    def __call__(self, path) -> None:
        self.calls.append((Path(path), os.lstat(path).st_ino))

    def excluded(self, path) -> bool:
        return os.lstat(path).st_ino in {ino for _, ino in self.calls}


class SeedKey(EdKey):
    """A device key known only by its seed (the keystore's live key after a rotation)."""

    def __init__(self, seed: bytes):
        self.seed = seed
        self.pub = sig.public_key(seed)
        self.kid = sig.key_id("dk", self.pub)


def within(seconds: float, fn):
    """Run `fn` in a thread and fail if it hasn't finished in time (a deadlock never finishes)."""
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            out["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "it didn't finish: a deadlock"
    if "error" in out:
        raise out["error"]
    return out["value"]


class Env:
    """A ledger after `irp roam init`: genesis, laptop-1 (whose keys are in the keystore) and an approver,
    optionally some readers, the user's ledger with three entries and state.json with epoch_start 0."""

    def __init__(self, tmp_path: Path, label: str = "mk", *, readers: int = 0):
        self.tmp, self.label = tmp_path, label
        kit = DevicesKit(label)
        kit.genesis()
        self.laptop_key = EdKey(f"{label}/laptop-1/key")
        self.laptop = kit.custodian("laptop-1", key=self.laptop_key, box_label=f"{label}/laptop-1/box")
        self.ak = kit.approver("hwkey-1")
        self.kit, self.root_key = kit, kit.root
        self.rk = ReadersKit(kit)
        self.readers_made = [rid(f"{label}/reader/{i}") for i in range(readers)]
        for r in self.readers_made:
            self.rk.enrol(r, [self.laptop, self.ak])
        home = tmp_path / "home"
        self.home = home
        self.ledger = R.Ledger(ledger_id=LEDGER, root=kit.root.kid, keys_dir=home / "keys",
                               ledger_dir=home / "ledgers" / LEDGER)
        self.ledger_file = tmp_path / "work" / "ledger.jsonl"
        self.ledger_file.parent.mkdir(parents=True)
        self.ledger_file.write_bytes(jsonl(ENTRIES))
        self.n_entries = len(ENTRIES)
        self.mirror = home / "mirror"
        keys._private_dir(self.ledger.ledger_dir)
        for path, data in ((self.ledger.devices_path, kit.data), (self.ledger.readers_path, self.rk.data)):
            path.write_bytes(data)
            os.chmod(path, 0o600)
        self.source = SilentFile(self.ledger.keys_dir)
        self.ks0 = Keystore(kek_source="file", dk_seed=self.laptop_key.seed, dk_box=h(f"box/{label}/laptop-1/box"),
                            ck_seed=h(f"{label}/ck/0"), epochs={0: EK[0]})
        save_keystore(self.ledger.keys_dir, self.ks0, self.source, os.urandom)
        self.tm = FakeTM()
        self.clock = FakeClock(fk.T)
        self.events: list = []
        self.keyring = Keyring(kit.keys[self.ak], on_ask=lambda: self.events.append("tap"))
        self.answers: dict = {}
        self.relay_fail = 0
        self.said: list = []
        self.checks: list = []
        self.records: list = []
        self.delivered: list = []
        self.kill = None
        self.set_state(epoch_start=0)  # what init writes

    # ── files ──
    def lock(self, exclusive: bool = True) -> RoamLock:
        return RoamLock(self.ledger.keys_dir, exclusive=exclusive, interactive=True)

    def keystore(self) -> Keystore:
        return load_keystore(self.ledger.keys_dir, [self.source])

    def save_ks(self, ks: Keystore) -> None:
        save_keystore(self.ledger.keys_dir, ks, self.source, os.urandom)

    def state(self) -> S.RoamState:
        return S.load_state(self.ledger.state_path)

    def set_state(self, **changes):
        with self.lock() as lk:
            return S.update_state(self.ledger.state_path, lk, exclude_from_backup=self.tm, **changes)

    def devices_bytes(self) -> bytes:
        return self.ledger.devices_path.read_bytes()

    def readers_bytes(self) -> bytes:
        return self.ledger.readers_path.read_bytes()

    def devices(self):
        return replay_devices(self.devices_bytes(), ledger_id=LEDGER, root=self.ledger.root,
                              now=self.clock.t + timedelta(days=1))

    def logs_now(self) -> dict:
        disc = C.disclosures_path(self.ledger.ledger_dir)
        return {"ledger": C.cover(self.ledger_file.read_bytes())[0], "devices": self.devices_bytes(),
                "readers": self.readers_bytes(), "disclosures": disc.read_bytes() if disc.exists() else b""}

    def add_entries(self, n: int = 1) -> None:
        with open(self.ledger_file, "ab") as fh:
            for _ in range(n):
                self.n_entries += 1
                fh.write(canonicalize(entry(self.n_entries)) + b"\n")

    def staged(self) -> list:
        return sorted(C.staged_checkpoints(self.ledger.staging_dir))

    def files(self, base: Path | None = None) -> dict:
        base = base or self.home
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}

    def staged_bytes(self, e: int, s: int) -> bytes:
        return (self.ledger.staging_dir / C.staged_name(e, s)).read_bytes()

    def open(self, e: int, s: int, ident: Identity = RK_ID) -> C.Shipped:
        return C.open_custodian(self.staged_bytes(e, s), [ident])

    def verify(self, e: int, s: int, *, epochs=None, pins=(), ident: Identity = RK_ID) -> C.Verified:
        return C.verify_checkpoint(self.open(e, s, ident), ledger_id=LEDGER, root=self.ledger.root,
                                   devices=self.devices_bytes(), now=self.clock.t, pins=pins, slot=(e, s),
                                   epochs=EK if epochs is None else epochs)

    def chain(self, slots, *, epochs=None, ident: Identity = RK_ID) -> C.Chain:
        vs = [self.verify(e, s, epochs=epochs, ident=ident) for e, s in slots]
        return C.verify_chain(vs, ledger_id=LEDGER, root=self.ledger.root, devices=self.devices_bytes(),
                              now=self.clock.t, readers=self.readers_bytes(), custodian=True, logs=self.logs_now())

    # ── devices lines written by hand ──
    def later(self) -> str:
        self.clock.t += timedelta(seconds=60)
        return ts(self.clock.t)

    def line(self, event: str, fields: dict, signers, *, base: bytes | None = None) -> bytes:
        data = self.devices_bytes() if base is None else base
        lines = L.split_log(data)
        log = replay_devices(data, ledger_id=LEDGER, root=self.ledger.root, now=self.clock.t + timedelta(days=1),
                             prefix=True)
        body = {"v": 1, "kind": "devices-entry", "event": event, "ledger_id": LEDGER, "root": log.root,
                "idx": len(lines), "prev": L.line_hash(lines[-1]), "at": self.later(), **fields}
        sigs = [sign_body("devices-entry", body, k) for k in signers]
        return canonicalize({"body": body, "sigs": sorted(sigs, key=lambda s: s["key_id"])})

    def append_line(self, event: str, fields: dict, signers) -> bytes:
        line = self.line(event, fields, signers)
        with open(self.ledger.devices_path, "ab") as fh:
            fh.write(line + b"\n")
        return line

    def live_key(self) -> SeedKey:
        return SeedKey(self.keystore().dk_seed)

    def enrol_custodian(self, label: str = "laptop-2") -> EdKey:
        k = EdKey(f"{self.label}/{label}/key")
        d = k.descriptor(label, Identity(h(f"box/{self.label}/{label}")).recipient().to_string())
        self.append_line("device_enrol", {"device": d, "nonce": None}, [self.root_key, k])
        return k

    def recover(self, ref, *, marker: bool = True, label: str = "laptop-9") -> EdKey:
        """What `irp roam recover` writes on this laptop: a root-signed recovery line keeping the approver, the
        new device's keys in the keystore, then (unless killed first) the epoch_start marker."""
        k = EdKey(f"{self.label}/{label}/recovery")
        box_seed = h(f"{self.label}/{label}/recovery-box")
        desc = k.descriptor(label, Identity(box_seed).recipient().to_string())
        tail = self.devices().state()
        active = sorted([dict(tail.devices[self.ak].raw), desc], key=lambda d: d["kid"])
        revokes = sorted(kid for kid in tail.devices if kid != self.ak)
        e = tail.epoch + 1
        self.append_line("recovery", {"active": active, "revokes": revokes, "new_device": k.kid, "epoch": e,
                                      "checkpoint_ref": ref}, [self.root_key, k])
        self.save_ks(Keystore(kek_source="file", dk_seed=k.seed, dk_box=box_seed, ck_seed=h(f"{self.label}/ck/{label}"),
                              epochs={e: EK[e]}))
        if marker:
            self.set_state(epoch_start=e)
        return k

    def root_rotate(self, ref, rk2: bytes) -> None:
        new = EdKey(f"{self.label}/root/1", root=True)
        e = self.devices().epoch + 1
        self.append_line("root_rotate", {"root_pub": sig.b64url_encode(new.pub), "epoch": e, "checkpoint_ref": ref},
                         [self.root_key, new])
        self.root_key = new
        self.ledger = R.Ledger(ledger_id=LEDGER, root=new.kid, keys_dir=self.ledger.keys_dir,
                               ledger_dir=self.ledger.ledger_dir)
        ks = self.keystore()
        self.save_ks(dataclasses.replace(ks, epochs={**dict(ks.epochs), e: keys.epoch_keys(rk2, e)}))
        self.set_state(epoch_start=e)

    # ── the injected pieces ──
    def clock_check(self, now) -> None:
        self.checks.append(now)

    def relay_seen(self, epoch: int, seq: int) -> str:
        if self.relay_fail:
            self.relay_fail -= 1
            raise RuntimeError("the relay didn't answer")
        return self.answers.get((epoch, seq), C.UNKNOWN)

    def progress(self, event: str) -> None:
        self.events.append(event)
        if self.kill is not None and self.kill(event):
            raise Kill(event)

    def options(self, **kw) -> dict:
        args = dict(ledger_file=self.ledger_file, clock=self.clock, clock_check=self.clock_check,
                    relay_seen=self.relay_seen, mirror_dir=self.mirror, exclude_from_backup=self.tm,
                    sleep=self.clock.sleep, say=self.said.append, progress=self.progress)
        args.update(kw)
        return args

    def make(self, *, lock=None, ks=None, **kw) -> C.MakeResult:
        if lock is None:
            with self.lock() as lk:
                return self.make(lock=lk, ks=ks, **kw)
        ks = ks or load_locked(self.ledger.keys_dir, [self.source], lock)[0]
        res = C.make_checkpoint(self.ledger, ks=ks, lock=lock, **self.options(**kw))
        if res.made is not None:
            self.records.append(res.record)
        return res

    def publish(self, **kw) -> C.MakeResult:
        """What 2.8's publish does about checkpoints: under the exclusive lock, adopt, then make one when due."""
        return self.make(when_due=True, **kw)

    def adopt(self, **kw) -> C.Adoption:
        with self.lock() as lk:
            ks = load_locked(self.ledger.keys_dir, [self.source], lk)[0]
            args = dict(clock=self.clock, exclude_from_backup=self.tm, say=self.said.append, progress=self.progress)
            args.update(kw)
            return C.adopt_staged(self.ledger, ks=ks, lock=lk, **args)

    # ── rotation ──
    def _deliver(self, reader_id, identity, expires) -> bool:
        self.delivered.append(reader_id)
        return True

    def hooks(self, *, confirm: bool = True, publish=None, engine_progress=None, **kw) -> R.Hooks:
        base = R.Hooks(confirm_config=lambda config: confirm, deliver=self._deliver,
                       publish=publish or (lambda lock: None), progress=engine_progress or (lambda step: None))
        return C.rotation_hooks(self.ledger, hooks=base, **self.options(**kw))

    def rotate(self, *, hooks=None, suspected: bool = False, lock=None) -> R.RotationResult:
        return R.rotate(self.ledger, sources=[self.source], rng=os.urandom, clock=self.clock,
                        clock_check=self.clock_check, authenticator=self.keyring, hooks=hooks or self.hooks(),
                        suspected=suspected, sleep=self.clock.sleep, say=self.said.append, lock=lock,
                        exclude_from_backup=self.tm)

    # ── draining staging the way 2.7 will ──
    def drain(self, e: int, s: int) -> None:
        """Copy (e, s) to the mirror, then delete its staged files in reverse write order (custodian file, sidecar,
        then segments no remaining sidecar lists)."""
        st = self.ledger.staging_dir
        kc = self.keystore().epochs[e].kc
        slot = C.custodian_slot(kc, s)
        listed = C.parse_sidecar((st / C.sidecar_name(e, s)).read_bytes())
        (self.mirror / "m").mkdir(parents=True, exist_ok=True)
        (self.mirror / "o").mkdir(parents=True, exist_ok=True)
        (self.mirror / slot).write_bytes(self.staged_bytes(e, s))
        for oid in listed:
            src = st / oid
            if src.exists():
                (self.mirror / oid).write_bytes(src.read_bytes())
        (st / C.staged_name(e, s)).unlink()
        (st / C.sidecar_name(e, s)).unlink()
        still = set()
        for (e2, s2) in C.staged_checkpoints(st):
            still |= set(C.parse_sidecar((st / C.sidecar_name(e2, s2)).read_bytes()))
        for oid in listed:
            if oid not in still and (st / oid).exists():
                (st / oid).unlink()


def staging_is_clean(env: Env) -> None:
    """No temp file, no sidecar without its custodian file, and no segment that nothing lists."""
    st = env.ledger.staging_dir
    names = os.listdir(st)
    assert not [n for n in names if n.endswith(".tmp")]
    assert not [n for n in os.listdir(st / "o") if n.endswith(".tmp")]
    listed = set(C.listed_objects(env.state().checkpoint["logs"]))
    for e, s in env.staged():
        assert (st / C.sidecar_name(e, s)).exists()
        listed |= set(C.parse_sidecar((st / C.sidecar_name(e, s)).read_bytes()))
    for n in names:
        if n.endswith(".objects"):
            assert (st / n[:-len(".objects")]).exists()
    for n in os.listdir(st / "o"):
        assert "o/" + n in listed


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return fk.PKI(tmp_path_factory.mktemp("pki"))


def tsa_options(pki, **over) -> dict:
    opts = {"context_factory": fk.context_factory(pki), "resolver": fk.resolver(), "timeout": 5.0}
    opts.update(over)
    return opts


def write_tsa_json(env: Env, fake, pki, *, auth: str = "none") -> bytes:
    e = TsaEntry(name="tsa-a", url=fake.url("tsa-a.test"), auth=auth, ca_sha256=(pki.tsa_ca.pin,), subject_o=ORG)
    data = tsa.encode_tsa_list([e])
    with env.lock() as lk:
        S.write_local_file(env.ledger.tsa_path, data, lk, exclude_from_backup=env.tm)
    return data


def pins(pki):
    return (TsaPin("tsa-a", pki.tsa_ca.pin, ORG),)


# ── Locks and what make_checkpoint needs ──

def test_make_needs_roam_lock_held_exclusively_by_the_caller(tmp_path):
    env = Env(tmp_path)
    before = env.files()
    with env.lock(exclusive=False) as shared:
        ks = load_locked(env.ledger.keys_dir, [env.source], shared)[0]
        with pytest.raises(C.CheckpointError, match="exclusively"):
            C.make_checkpoint(env.ledger, ks=ks, lock=shared, **env.options())
    with pytest.raises(C.CheckpointError, match="exclusively"):
        C.make_checkpoint(env.ledger, ks=env.keystore(), lock=None, **env.options())
    with RoamLock(tmp_path / "other-keys", exclusive=True, interactive=True) as other:
        with pytest.raises(C.CheckpointError, match="keys folder"):
            C.make_checkpoint(env.ledger, ks=env.keystore(), lock=other, **env.options())
    assert env.files() == before and env.checks == []


def test_make_refuses_without_a_clock_check(tmp_path):
    env = Env(tmp_path)
    before = env.files()
    with pytest.raises(C.CheckpointError, match="clock check"):
        env.make(clock_check=None)
    assert env.files() == before


def test_make_never_opens_a_roam_lock_or_a_log_writer(tmp_path, monkeypatch):
    env = Env(tmp_path)
    with env.lock() as lk:
        ks = load_locked(env.ledger.keys_dir, [env.source], lk)[0]

        def refuse(*a, **kw):
            raise AssertionError("make_checkpoint opened a RoamLock or a LogWriter")
        monkeypatch.setattr(RoamLock, "__enter__", refuse)
        monkeypatch.setattr(L.LogWriter, "__enter__", refuse)
        res = env.make(lock=lk, ks=ks)
        env.add_entries()
        res2 = env.make(lock=lk, ks=ks)
    assert (res.made.seq, res2.made.seq) == (1, 2)


# ── The first checkpoint, and the files step 8 writes ──

def test_the_first_checkpoint_writes_segments_then_sidecar_then_custodian_file_then_the_record(tmp_path):
    env = Env(tmp_path)
    res = env.make()
    m = res.made
    assert (m.epoch, m.seq, m.strand) == (0, 1, env.laptop) and m.label == C.NONE and m.base
    assert json.loads(m.header)["prev"] is None
    st = env.ledger.staging_dir
    assert stat.S_IMODE(os.lstat(st).st_mode) == 0o700 and env.tm.excluded(st)
    assert env.staged_bytes(0, 1) == m.container
    assert (st / C.sidecar_name(0, 1)).read_bytes() == C.sidecar_bytes(m.listed) == \
        b"".join(oid.encode() + b"\n" for oid in m.listed)
    assert C.parse_sidecar(C.sidecar_bytes(m.listed)) == m.listed
    for oid, ct in m.objects:
        assert (st / oid).read_bytes() == ct
        assert stat.S_IMODE(os.lstat(st / oid).st_mode) == 0o600
    assert sorted("o/" + n for n in os.listdir(st / "o")) == sorted(m.listed)
    assert stat.S_IMODE(os.lstat(st / C.staged_name(0, 1)).st_mode) == 0o600
    rec = env.state().checkpoint
    assert rec == res.record == m.record(None)
    assert rec["digest"] == m.digest and rec["label"] == C.NONE and rec["last_present"] is None
    # Step 8's order: every segment, then the sidecar, then the custodian file, then state.json.
    renames = [e for e in env.events if e.startswith("rename ") or e == "write state"]
    kinds = [e.split()[1] if e.startswith("rename") else "state" for e in renames]
    assert kinds == ["segment"] * len(m.objects) + ["sidecar", "custodian", "state"]
    # No previous entry: every log is append_only false with a loud alert, and no tsa.json means NONE, loudly.
    assert {a.log for a in m.alerts} == set(C.LOG_NAMES) and all(a.reason == C.NO_PREVIOUS for a in m.alerts)
    assert all(str(a) in res.alerts for a in m.alerts)
    assert any("tsa.json" in a and "NONE" in a for a in res.alerts)
    assert len(env.checks) == 1
    v = env.verify(0, 1)
    assert v.digest == m.digest
    C.open_custodian(m.container, [Identity(env.ks0.dk_box)])  # the live box opens it too


def test_the_next_checkpoint_chains_and_adds_one_delta_segment(tmp_path):
    env = Env(tmp_path)
    one = env.make().made
    env.add_entries()
    res = env.make()
    two = res.made
    assert two.seq == 2 and json.loads(two.header)["prev"] == one.digest and not two.base
    assert two.alerts == () and len(two.objects) == 1
    body = env.verify(0, 2).body
    first = C.parse_body(one.body)["logs"]
    assert body["logs"]["ledger"]["segments"][:1] == first["ledger"]["segments"]
    assert {**body["logs"]["devices"], "append_only": False} == first["devices"]  # carried over unchanged
    assert all(body["logs"][n]["append_only"] for n in C.LOG_NAMES)
    assert body["snapshot"]["manifest"]["previous_snapshot_digest"] == env.records[0]["snapshot_digest"]
    env.chain([(0, 1), (0, 2)])
    staging_is_clean(env)


# ── Cadence (due), evaluated after adoption ──

def test_due_is_only_on_a_change_or_after_24_hours_and_never_on_disclosures_alone(tmp_path):
    env = Env(tmp_path)
    assert env.publish().made.seq == 1
    res = env.publish()
    assert res.made is None and res.due is None and "not due" in res.skipped
    C.disclosures_path(env.ledger.ledger_dir).write_bytes(b'{"x":1}\n')
    os.chmod(C.disclosures_path(env.ledger.ledger_dir), 0o600)
    assert env.publish().made is None
    env.add_entries()
    res = env.publish()
    assert res.made.seq == 2 and "ledger" in res.due
    assert C.parse_body(res.made.body)["logs"]["disclosures"]["byte_length"] == 8  # it rides along
    ak2 = approver_key(f"{env.label}/hwkey-2")
    env.append_line("approver_enrol", {"approver": ak2.descriptor("hwkey-2")}, [env.root_key, ak2])
    res = env.publish()
    assert res.made.seq == 3 and "devices" in res.due
    env.clock.t += timedelta(hours=23, minutes=59)
    assert env.publish().made is None
    env.clock.t += timedelta(minutes=2)
    res = env.publish()
    assert res.made.seq == 4 and "24 hours" in res.due


def test_a_readers_change_makes_a_checkpoint_due(tmp_path):
    env = Env(tmp_path)
    env.publish()
    env.rk.enrol(rid("late reader"), [env.laptop, env.ak])
    env.ledger.readers_path.write_bytes(env.rk.data)
    res = env.publish()
    assert res.made.seq == 2 and "readers" in res.due


def test_the_due_rule_on_its_own(tmp_path):
    env = Env(tmp_path)
    env.make()
    rec = env.state().checkpoint
    now = env.clock.t
    logs = env.logs_now()
    assert C.due(None, epoch=0, logs=logs, now=now) is not None
    assert C.due(rec, epoch=1, logs=logs, now=now) is not None
    assert C.due(rec, epoch=0, logs=logs, now=now) is None
    assert C.due(rec, epoch=0, logs=logs | {"disclosures": b"{}\n"}, now=now) is None
    for name in ("ledger", "devices", "readers"):
        assert name in C.due(rec, epoch=0, logs=logs | {name: logs[name] + b"{}\n"}, now=now)
    assert C.due(rec, epoch=0, logs=logs, now=now + timedelta(hours=24)) is not None


# ── Step 1: clean up ──

def test_cleanup_removes_temp_files_key_folders_orphan_sidecars_and_unlisted_segments(tmp_path):
    env = Env(tmp_path)
    one = env.make().made
    st = env.ledger.staging_dir
    junk = {st / "x.tmp": b"t", st / "o" / ("ab" * 32 + ".tmp"): b"t", st / C.sidecar_name(0, 9): b"o/" + b"cd" * 32 +
            b"\n", st / "o" / ("ef" * 32): b"orphan segment"}
    for p, data in junk.items():
        p.write_bytes(data)
    keyfolder = env.ledger.keys_dir / (tsa.KEY_FOLDER_PREFIX + "left")
    keyfolder.mkdir(mode=0o700)
    (keyfolder / "key.pem").write_bytes(b"encrypted")
    (st / "notes.txt").write_bytes(b"someone else's file")
    res = env.publish()
    assert res.made is None
    for p in junk:
        assert not p.exists()
    assert not keyfolder.exists()
    assert (st / "notes.txt").exists()
    assert set(res.cleaned) >= {"x.tmp", "o/" + "ab" * 32 + ".tmp", C.sidecar_name(0, 9), "o/" + "ef" * 32}
    for oid in one.listed:
        assert (st / oid).exists()
    staging_is_clean(env)


def test_a_symlink_in_staging_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.make()
    (env.ledger.staging_dir / C.staged_name(0, 2)).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(C.CheckpointAlarm, match="symlink"):
        env.publish()


# ── Step 2: adoption ──

def stage_and_kill(env: Env, point: str = "write state", **kw) -> None:
    """A publish that stages the next checkpoint and is killed at `point` of step 8."""
    env.kill = lambda e: e.startswith(point)
    with pytest.raises(Kill):
        env.make(**kw)
    env.kill = None


def test_a_staged_checkpoint_above_the_record_is_adopted_byte_for_byte(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    staged = env.staged_bytes(0, 2)
    assert env.state().checkpoint["seq"] == 1 and env.staged() == [(0, 1), (0, 2)]
    a = env.adopt()
    assert a.adopted == ((0, 2),) and a.abandoned == ()
    rec = env.state().checkpoint
    v = env.verify(0, 2)
    assert rec["seq"] == 2 and rec["digest"] == v.digest == C.digest_of(env.open(0, 2).header)
    assert rec == v.record(env.records[0]) and a.record == rec
    assert env.staged_bytes(0, 2) == staged
    assert env.publish().made is None  # nothing changed since what was adopted
    staging_is_clean(env)


def save_state_bytes(env: Env) -> bytes:
    return env.ledger.state_path.read_bytes()


def restore_state_bytes(env: Env, data: bytes) -> None:
    env.ledger.state_path.write_bytes(data)
    os.chmod(env.ledger.state_path, 0o600)


def test_adoption_repeats_until_nothing_staged_is_above_the_record(tmp_path):
    env = Env(tmp_path)
    env.make()
    older = save_state_bytes(env)
    for _ in range(2):
        env.add_entries()
        env.make()
    restore_state_bytes(env, older)
    a = env.adopt()
    assert a.adopted == ((0, 2), (0, 3)) and env.state().checkpoint["seq"] == 3
    assert env.state().checkpoint["digest"] == env.records[-1]["digest"]


def test_a_gap_is_alarm_and_the_files_stay_where_they_are(tmp_path):
    env = Env(tmp_path)
    env.make()
    older = save_state_bytes(env)
    for _ in range(2):
        env.add_entries()
        env.make()
    restore_state_bytes(env, older)
    env.drain(0, 2)  # seq 2 went to the relay; the record is behind it
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="gap"):
        env.publish()
    assert env.files() == before


def test_a_name_that_isnt_canonical_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    st = env.ledger.staging_dir
    (st / "custodian-0-02").write_bytes(env.staged_bytes(0, 2))
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="canonical"):
        env.publish()
    assert env.files() == before


def reseal(env: Env, e: int, s: int, recipients, *, members=None) -> None:
    """Replace a staged custodian file with its members sealed to other recipients (or changed members)."""
    sh = env.open(e, s)
    m = {C.MEMBER_HEADER: sh.header, C.MEMBER_SIG: sh.sig, C.MEMBER_MAC: sh.mac, C.MEMBER_BODY: sh.body,
         C.MEMBER_DEVICES: sh.devices}
    if members:
        members(m)
    (env.ledger.staging_dir / C.staged_name(e, s)).write_bytes(C.seal_custodian(m, recipients))


def test_a_candidate_neither_the_live_box_nor_box_prev_opens_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    other = Identity(h("someone else's box")).recipient().to_string()
    reseal(env, 0, 2, [{"id": "rk", "recipient": EK[0].rk_recipient}, {"id": "dk-" + "9" * 32, "recipient": other}])
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="opens"):
        env.publish()
    assert env.files() == before and env.state().checkpoint["seq"] == 1


@pytest.mark.parametrize("case", ["sidecar", "missing segment", "mac"])
def test_a_candidate_must_verify_and_list_exactly_its_segments(tmp_path, case):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    st = env.ledger.staging_dir
    sidecar = st / C.sidecar_name(0, 2)
    listed = C.parse_sidecar(sidecar.read_bytes())
    if case == "sidecar":
        sidecar.write_bytes(sidecar.read_bytes() + b"o/" + b"12" * 32 + b"\n")
    elif case == "missing segment":
        new = [oid for oid in listed if oid not in C.listed_objects(env.state().checkpoint["logs"])]
        (st / new[0]).unlink()
    else:
        rec = C.parse_body(env.open(0, 2).body)["recipients"]
        reseal(env, 0, 2, rec, members=lambda m: m.update({C.MEMBER_MAC: b"0" * 64 + b"\n"}))
    before = env.files()
    with pytest.raises(C.CheckpointAlarm):
        env.publish()
    assert env.files() == before and env.state().checkpoint["seq"] == 1


@pytest.mark.parametrize("case", ["omits an id the record lists", "reordered"])
def test_a_sidecar_lists_exactly_the_bodys_segment_ids_in_order(tmp_path, case):
    """2.7 uploads a file's segments from its sidecar before its slot, so a sidecar short of an id the body lists
    (one carried over from the record, say) would let that object go unuploaded."""
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    sidecar = env.ledger.staging_dir / C.sidecar_name(0, 2)
    listed = list(C.parse_sidecar(sidecar.read_bytes()))
    carried = [oid for oid in listed if oid in C.listed_objects(env.state().checkpoint["logs"])]
    ids = list(reversed(listed)) if case == "reordered" else [oid for oid in listed if oid != carried[0]]
    assert len(listed) > 1 and ids != listed
    sidecar.write_bytes(C.sidecar_bytes(ids))
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="exactly"):
        env.publish()
    assert env.files() == before and env.state().checkpoint["seq"] == 1


def restage(env: Env, e: int, s: int, *, header=None, body=None, tsr=None) -> None:
    """Rebuild a staged checkpoint around a changed header, body or token: re-signed with the laptop key, MACed
    again and sealed to its own recipients, so only the adoption rule under test can refuse it."""
    sh = env.open(e, s)
    b = json.loads(sh.body)
    if body:
        body(b)
    body_bytes = canonicalize(b)
    hd = json.loads(sh.header)
    hd["body_digest"] = C.digest_of(body_bytes)
    if header:
        header(hd)
    header_bytes = canonicalize(hd)

    def members(m):
        m.update({C.MEMBER_HEADER: header_bytes, C.MEMBER_BODY: body_bytes,
                  C.MEMBER_SIG: C.sign_header(header_bytes, env.laptop_key.seed, env.laptop),
                  C.MEMBER_MAC: C.mac_line(EK[e].ka, header_bytes, body_bytes)})
        if tsr is not None:
            m[C.MEMBER_TSR] = tsr
    reseal(env, e, s, b["recipients"], members=members)


def _no_previous_snapshot(b):
    b["snapshot"]["manifest"]["previous_snapshot_digest"] = "0" * 64
    b["snapshot"]["snapshot_digest"]["value"] = hashlib.sha256(canonicalize(b["snapshot"]["manifest"])).hexdigest()


@pytest.mark.parametrize("case,match", [("prev", "prev"), ("snapshot link", "snapshot"),
                                        ("append_only without a record", "append_only")])
def test_a_candidate_that_doesnt_link_to_the_record_is_alarm_and_stays_where_it_is(tmp_path, case, match):
    """Each candidate verifies on its own; adoption still refuses it by the link rules: prev is the record's
    digest, the snapshot names the record's snapshot_digest, and with no record (the epoch's marker) nothing may
    claim append_only."""
    env = Env(tmp_path)
    if case != "append_only without a record":
        env.make()
        env.add_entries()
    stage_and_kill(env)
    seq = 1 if case == "append_only without a record" else 2
    if case == "prev":
        restage(env, 0, 2, header=lambda hd: hd.update(prev="sha256-" + "0" * 64))
    elif case == "snapshot link":
        restage(env, 0, 2, body=_no_previous_snapshot)
    else:
        restage(env, 0, 1, body=lambda b: b["logs"]["ledger"].update(append_only=True))
    assert env.verify(0, seq).seq == seq
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match=match):
        env.publish()
    assert env.files() == before


def test_an_append_only_claim_adoption_cant_check_is_alarm(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    monkeypatch.setattr(C, "check_append_only", lambda *a, **kw: ("ledger",))
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="can't be checked"):
        env.publish()
    assert env.files() == before and env.state().checkpoint["seq"] == 1


def test_a_present_candidate_whose_gentime_goes_back_before_the_records_is_alarm(tmp_path, pki):
    env = Env(tmp_path)
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
        assert env.make(tsa_options=tsa_options(pki)).made.label == C.PRESENT
        env.add_entries()
        stage_and_kill(env, tsa_options=tsa_options(pki))
    header = env.open(0, 2).header
    earlier = fk.build_token(hashlib.sha256(header).digest(), pki,
                             fk.TokenOptions(gen_time=fk.T - timedelta(minutes=10)))
    restage(env, 0, 2, tsr=earlier)
    assert env.verify(0, 2, pins=pins(pki)).label == C.PRESENT
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="genTime"):
        env.publish(tsa_options=tsa_options(pki))
    assert env.files() == before and env.state().checkpoint["seq"] == 1


def test_a_candidate_only_box_prev_opens_is_adopted_after_a_rotation(tmp_path):
    """Staged under the old box, then a rotation whose step-1 adoption failed (reported, and the rotation carried
    on) and whose hook made nothing: the next publish opens it with box_prev and adopts it."""
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)

    def fail(ks, lock):
        raise C.CheckpointError("adoption failed here")
    r = env.rotate(hooks=dataclasses.replace(env.hooks(), adopt=fail, checkpoint=lambda ks, idx, lock: None))
    assert r.closed and env.state().checkpoint["seq"] == 1 and env.keystore().box_prev is not None
    with pytest.raises(C.CheckpointError):
        env.open(0, 2, Identity(env.keystore().dk_box))
    assert env.publish().adopted == ((0, 2),)


def test_a_staged_file_from_an_epoch_the_devices_log_hasnt_reached_is_alarm(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    st = env.ledger.staging_dir
    (st / C.staged_name(5, 1)).write_bytes(env.staged_bytes(0, 2))
    (st / C.sidecar_name(5, 1)).write_bytes((st / C.sidecar_name(0, 2)).read_bytes())
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="hasn't reached"):
        env.publish()
    assert env.files() == before


# ── Steps 3 to 6 ──

def test_c1_a_second_active_custodian_refuses_publish_before_signing_and_names_it(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.enrol_custodian("laptop-2")
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="revoke laptop-2"):
        env.publish()
    assert env.files() == before and len(env.checks) == checks


NO_RUN = "without a run of device_rotate lines"


def test_a_strand_that_arrived_without_a_run_of_device_rotate_lines_refuses_before_signing(tmp_path):
    """C1: replacing a custodian within an epoch goes through recovery, and verify_chain refuses a strand that
    arrived any other way. So a keystore whose live key took over by enrol and revoke never signs the next seq.
    With the record's own signer revoked, step 0 refuses first (REVOKED_SIGNER); with it retired, the strand rule
    does."""
    env = Env(tmp_path / "revoked")
    env.make()
    k2 = env.enrol_custodian("laptop-2")
    env.append_line("device_revoke", {"kid": env.laptop}, [env.root_key])
    env.save_ks(dataclasses.replace(env.keystore(), dk_seed=k2.seed, dk_box=h(f"box/{env.label}/laptop-2")))
    env.add_entries()
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match=REVOKED):
        env.publish()
    assert env.files() == before

    env = Env(tmp_path / "retired")
    env.make()
    k2 = env.enrol_custodian("laptop-2")
    r1 = env.rotate()  # the hook refuses (C1), so the record stays on laptop-1, now retired
    assert r1.closed and env.state().checkpoint["strand"] == env.laptop
    env.append_line("device_revoke", {"kid": r1.new_kid}, [env.root_key])
    env.save_ks(dataclasses.replace(env.keystore(), dk_seed=k2.seed, dk_box=h(f"box/{env.label}/laptop-2"),
                                    box_prev=None))
    env.add_entries()
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match=NO_RUN):
        env.publish()
    assert env.files() == before

    # laptop-2 rotating its own key hands nothing over: a run of device_rotate lines starts from the record's
    # strand, and each later line's `old` is the kid the one before brought in. Here the record's strand (laptop-1)
    # rotates to A', laptop-2 rotates K to K', and A' is revoked: K -> K' doesn't continue A's run.
    env = Env(tmp_path / "other-rotation")
    env.make()
    k2 = env.enrol_custodian("laptop-2")
    r1 = env.rotate()  # closes; its hook refuses (C1), so the record stays on laptop-1, now retired
    assert r1.closed and env.state().checkpoint["strand"] == env.laptop
    env.save_ks(dataclasses.replace(env.keystore(), dk_seed=k2.seed, dk_box=h(f"box/{env.label}/laptop-2"),
                                    box_prev=None))
    r2 = env.rotate()  # closes; its hook refuses (C1, A' is still active)
    assert r2.closed and r2.old_kid == k2.kid and env.state().checkpoint["strand"] == env.laptop
    env.append_line("device_revoke", {"kid": r1.new_kid}, [env.root_key])
    env.add_entries()
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match=NO_RUN):
        env.publish()
    assert env.files() == before


def test_make_refuses_mid_rotation_or_with_pending_keys_without_signing(tmp_path):
    env = Env(tmp_path)
    env.make()
    open_ = env.rotate(hooks=env.hooks(confirm=False))
    assert not open_.closed
    env.add_entries()
    before = env.files()
    with pytest.raises(C.CheckpointError, match="rotation") as exc:
        env.publish()
    assert not isinstance(exc.value, C.CheckpointAlarm)
    assert env.files() == before
    env2 = Env(tmp_path / "two")
    env2.make()
    ks = env2.keystore()
    env2.save_ks(dataclasses.replace(ks, pending=Pending(dk_seed=h("p/dk"), dk_box=h("p/box"), ck_seed=h("p/ck"),
                                                         started_at=ts(env2.clock.t))))
    env2.add_entries()
    before = env2.files()
    with pytest.raises(C.CheckpointError, match="rotation"):
        env2.publish()
    assert env2.files() == before


def test_a_failing_clock_check_refuses_before_signing(tmp_path):
    env = Env(tmp_path)

    def bad(now):
        raise RuntimeError("the relay's Date header is 9 minutes away")
    before = env.files()
    with pytest.raises(C.CheckpointError, match="clock check"):
        env.make(clock_check=bad)
    assert env.files() == before


def test_the_slot_check_refuses_when_the_mirror_or_the_relay_has_the_next_slot(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    slot = C.custodian_slot(EK[0].kc, 2)
    (env.mirror / "m").mkdir(parents=True)
    (env.mirror / slot).write_bytes(b"what the relay holds")
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="behind"):
        env.publish()
    assert env.files() == before
    (env.mirror / slot).unlink()
    env.answers[(0, 2)] = C.WRITTEN
    with pytest.raises(C.CheckpointAlarm, match="behind"):
        env.publish()
    env.answers[(0, 2)] = "maybe"
    with pytest.raises(C.CheckpointError, match="relay"):
        env.publish()
    env.relay_fail = 1
    with pytest.raises(C.CheckpointError, match="relay"):
        env.publish()
    env.answers[(0, 2)] = C.NOT_WRITTEN
    assert env.publish().made.seq == 2
    env.add_entries()
    assert env.publish().made.seq == 3  # "unknown" passes: step 0 already trusts the record
    assert C.relay_unknown(0, 3) == C.UNKNOWN


def test_created_at_waits_for_the_clock_and_never_makes_a_time_up(tmp_path):
    env = Env(tmp_path)
    one = env.make().made
    env.add_entries()
    env.clock.t -= timedelta(minutes=2)  # the clock went back: created_at may not go before the record's
    two = env.make().made
    assert two.created_at >= one.created_at and env.clock.slept >= 119
    env.add_entries()
    env.clock.t -= timedelta(minutes=10)
    env.clock.frozen = True
    before = env.files()
    with pytest.raises(C.CheckpointError, match="made up"):
        env.make()
    assert env.files() == before


# ── The TSA step ──

def test_a_present_token_sets_last_present_and_an_adopted_one_keeps_it(tmp_path, pki):
    env = Env(tmp_path)
    with fk.FakeTSA(pki) as fake:
        data = write_tsa_json(env, fake, pki)
        res = env.make(tsa_options=tsa_options(pki))
        m = res.made
        assert m.label == C.PRESENT and m.tsa == "tsa-a" and m.tsr is not None
        rec = env.state().checkpoint
        assert rec["label"] == C.PRESENT and rec["gen_time"] == ts(fk.T)
        assert rec["last_present"] == {"epoch": 0, "gen_time": ts(fk.T),
                                       "devices_length": len(env.devices_bytes()), "readers_length": 0}
        assert C.parse_body(m.body)["tsa_policy_digest"] == C.digest_of(data)
        assert not [a for a in res.alerts if "TSA" in a or "tsa.json" in a]
        env.add_entries()
        stage_and_kill(env, tsa_options=tsa_options(pki))
        a = env.adopt(tsa_options=tsa_options(pki))
        assert a.adopted == ((0, 2),)
        rec = env.state().checkpoint
        assert rec["label"] == C.PRESENT and rec["gen_time"] == ts(fk.T)
        assert len(fake.tokens) == 2
    assert env.verify(0, 2, pins=pins(pki)).label == C.PRESENT


def test_without_a_present_token_last_present_is_carried_and_tsa_json_changes_raise_an_alert(tmp_path, pki):
    env = Env(tmp_path)
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
        env.make(tsa_options=tsa_options(pki))
    lp = env.state().checkpoint["last_present"]
    env.ledger.tsa_path.unlink()
    env.add_entries()
    res = env.make()
    assert res.made.label == C.NONE and env.state().checkpoint["last_present"] == lp
    assert any("tsa_policy_digest" in a for a in res.alerts)
    assert any("NONE" in a for a in res.alerts)


def test_a_failing_tsa_gives_none_with_a_loud_alert_and_no_credential_anywhere(tmp_path, pki):
    env = Env(tmp_path)
    secret = "wrong-secret-pw-xyz"
    env.save_ks(dataclasses.replace(env.keystore(), tsa_creds={"tsa-a/user": "user-1", "tsa-a/password": secret}))
    with fk.FakeTSA(pki, auth="basic") as fake:
        write_tsa_json(env, fake, pki, auth="basic")
        res = env.make(tsa_options=tsa_options(pki))
    assert res.made.label == C.NONE and res.made.tsr is None
    assert any("tsa-a" in a and "http_status" in a for a in res.alerts)
    for text in [*res.alerts, *res.notes, *env.said, repr(res), repr(res.made), str(env.state())]:
        assert secret not in text and "Authorization" not in text


def test_a_broken_tsa_json_refuses_before_signing(tmp_path):
    env = Env(tmp_path)
    with env.lock() as lk:
        S.write_local_file(env.ledger.tsa_path, b'{"v":1,"tsas":[]}', lk, exclude_from_backup=env.tm)
    before = env.files()
    with pytest.raises(C.CheckpointError, match="tsa.json"):
        env.make()
    assert env.files() == before


# ── Crash gates: a kill at every write of step 8, and during the TSA call ──

@pytest.mark.parametrize("point", KILLS)
def test_a_kill_at_every_write_of_step_8_then_resume_adopts_byte_for_byte_or_cleans_orphans(tmp_path, point):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env, point)
    landed = point == "write state"
    after_kill = env.staged_bytes(0, 2) if landed else None
    res = env.publish()
    if landed:
        assert res.adopted == ((0, 2),) and res.made is None
        assert env.staged_bytes(0, 2) == after_kill
        assert env.state().checkpoint["digest"] == C.digest_of(env.open(0, 2).header)
    else:
        assert res.adopted == () and res.made.seq == 2
    assert env.staged() == [(0, 1), (0, 2)]
    staging_is_clean(env)
    env.chain([(0, 1), (0, 2)])


def test_a_kill_during_the_tsa_call_leaves_staging_unchanged_and_the_next_run_makes_it(tmp_path, pki):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)

        def killed():
            raise Kill("power cut during the TSA call")
        before = env.files()
        with pytest.raises(Kill):
            env.make(tsa_options=tsa_options(pki, context_factory=killed))
        assert env.files() == before
        left = env.ledger.keys_dir / (tsa.KEY_FOLDER_PREFIX + "after-kill")
        left.mkdir(mode=0o700)
        res = env.make(tsa_options=tsa_options(pki))
    assert res.made.seq == 2 and res.made.label == C.PRESENT and not left.exists()
    staging_is_clean(env)


# ── The rotation hook ──

def test_stage_then_kill_then_rotate_adopts_n_plus_1_before_the_tap_and_the_hook_makes_n_plus_2(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    old = env.keystore().dk_id
    r = env.rotate()
    assert r.closed
    ev = env.events
    assert ev.index("adopt 0 2") < ev.index("tap") < max(i for i, e in enumerate(ev) if e.startswith("write custodian"))
    rec = env.state().checkpoint
    assert (rec["seq"], rec["strand"]) == (3, r.new_kid) and env.staged() == [(0, 1), (0, 2), (0, 3)]
    assert env.verify(0, 2).strand == old and json.loads(env.open(0, 3).header)["prev"] == env.verify(0, 2).digest
    body = env.verify(0, 3).body
    assert all(len(body["logs"][n]["segments"]) == (1 if body["logs"][n]["byte_length"] else 0)
               for n in C.LOG_NAMES)  # a full base of all four logs
    ids = {r_["id"] for r_ in body["recipients"]}
    assert ids == {"rk", r.new_kid} and old not in ids
    env.chain([(0, 1), (0, 2), (0, 3)])


def test_the_hook_twice_for_one_rotate_idx_gives_one_checkpoint(tmp_path):
    env = Env(tmp_path)
    env.make()
    hooks = env.hooks()
    r = env.rotate(hooks=hooks)
    assert env.staged() == [(0, 1), (0, 2)]
    with env.lock() as lk:
        hooks.checkpoint(env.keystore(), r.rotate_idx, lk)
    assert hooks.checkpoint.last.made is None and "first checkpoint" in hooks.checkpoint.last.skipped
    assert env.staged() == [(0, 1), (0, 2)] and env.state().checkpoint["seq"] == 2


def test_the_hook_refuses_a_rotate_idx_that_isnt_the_live_keys_rotation(tmp_path):
    env = Env(tmp_path)
    env.make()
    hooks = env.hooks()
    first = env.rotate(hooks=hooks)
    env.rotate(hooks=hooks)
    with env.lock() as lk:
        with pytest.raises(C.CheckpointError, match="rotation"):
            hooks.checkpoint(env.keystore(), first.rotate_idx, lk)
        with pytest.raises(C.CheckpointError, match="device_rotate"):
            hooks.checkpoint(env.keystore(), 1, lk)


def test_rotate_now_over_an_open_rotation_gives_one_checkpoint_on_the_fresh_strand(tmp_path):
    env = Env(tmp_path)
    env.make()
    published = []
    open_ = env.rotate(hooks=env.hooks(confirm=False))
    assert not open_.closed and env.staged() == [(0, 1)]
    r = env.rotate(hooks=env.hooks(publish=lambda lock: published.append(env.make(when_due=True, lock=lock))),
                   suspected=True)
    assert r.closed and r.previous is not None and r.previous.new_kid == open_.new_kid
    assert env.staged() == [(0, 1), (0, 2)]
    assert env.verify(0, 2).strand == r.new_kid != open_.new_kid
    assert len(published) == 1 and published[0].made is None  # the hook's checkpoint already covers it


def test_c1_hook_failure_then_a_second_rotation_then_a_revoke_then_a_publish_verifies_across_both_lines(tmp_path):
    env = Env(tmp_path)
    env.make()
    k2 = env.enrol_custodian("laptop-2")
    r1 = env.rotate()
    r2 = env.rotate()
    for r in (r1, r2):
        assert r.closed and any("revoke laptop-2" in n for n in r.notes)
    assert env.staged() == [(0, 1)]
    env.append_line("device_revoke", {"kid": k2.kid}, [env.live_key()])
    res = env.publish()
    assert res.made.seq == 2 and res.made.strand == r2.new_kid and res.made.base
    ch = env.chain([(0, 1), (0, 2)])
    assert ch.checkpoints[1].prev == ch.checkpoints[0].digest


def test_a_hook_that_raises_leaves_the_rotation_closed_and_delivered_and_the_next_publish_makes_it(tmp_path):
    env = Env(tmp_path, readers=1)
    env.make()
    env.relay_fail = 1
    shown = []
    hooks = env.hooks()
    hooks.show = lambda config, reapproval, notes: shown.append(config)
    r = env.rotate(hooks=hooks)
    assert r.closed and env.delivered == env.readers_made and shown and shown[0].new_ck["id"] == r.config.new_ck["id"]
    assert any("first checkpoint wasn't made" in n and "relay" in n for n in r.notes)
    assert env.staged() == [(0, 1)]
    res = env.publish()
    assert res.made.seq == 2 and res.made.strand == r.new_kid and res.made.base
    env.chain([(0, 1), (0, 2)])


def test_after_rotate_the_record_is_the_new_strands_first_and_seen_is_unchanged_also_through_a_resume(tmp_path):
    for n, finish_later in enumerate((False, True)):
        env = Env(tmp_path / str(n))
        env.make()
        mark = env.verify(0, 1).seen()
        with env.lock() as lk:
            C.write_seen(env.ledger.state_path, lk, mark)
        if finish_later:
            assert not env.rotate(hooks=env.hooks(confirm=False)).closed
            assert env.state().checkpoint["seq"] == 1
        r = env.rotate()
        assert r.closed
        st = env.state()
        assert (st.checkpoint["seq"], st.checkpoint["strand"]) == (2, r.new_kid)
        assert st.seen == mark


def test_state_json_deleted_mid_rotation_closes_reports_the_alarm_and_publish_refuses(tmp_path):
    env = Env(tmp_path)
    env.make()

    def lose(step):
        if step == "signed":
            env.ledger.state_path.unlink()
    r = env.rotate(hooks=env.hooks(engine_progress=lose))
    assert r.closed and any("missing" in n and "Path B" in n for n in r.notes)
    assert env.state().checkpoint is None and env.state().epoch_start is None
    env.add_entries()
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()


def test_rotate_close_and_the_in_run_publish_and_the_drill_calling_rotate_finish_under_a_timeout(tmp_path):
    env = Env(tmp_path, readers=1)
    env.make()
    published = []
    r = within(60, lambda: env.rotate(hooks=env.hooks(
        publish=lambda lock: published.append(env.make(when_due=True, lock=lock)))))
    assert r.closed and len(published) == 1 and env.staged() == [(0, 1), (0, 2)]
    with env.lock() as drill:  # the drill holds roam.lock and rotates under it
        r2 = within(60, lambda: env.rotate(lock=drill))
        assert drill.held and drill.exclusive and r2.closed
    assert env.staged() == [(0, 1), (0, 2), (0, 3)]
    env.chain([(0, 1), (0, 2), (0, 3)])


def test_an_adopt_failure_at_a_rotations_step_1_is_reported_and_the_rotation_carries_on(tmp_path):
    env = Env(tmp_path)
    env.make()
    older = save_state_bytes(env)
    for _ in range(2):
        env.add_entries()
        env.make()
    restore_state_bytes(env, older)
    env.drain(0, 2)
    r = env.rotate()
    assert r.closed
    assert any("adopting staged checkpoints failed" in n and "gap" in n for n in r.notes)
    assert any("first checkpoint wasn't made" in n for n in r.notes)


def test_rotation_hooks_wires_both_adapters_and_keeps_the_other_hooks(tmp_path):
    env = Env(tmp_path)
    base = R.Hooks(deliver=env._deliver)
    hooks = C.rotation_hooks(env.ledger, hooks=base, **env.options())
    assert isinstance(hooks.adopt, C.AdoptHook) and isinstance(hooks.checkpoint, C.CheckpointHook)
    assert hooks.deliver == env._deliver and hooks.publish is base.publish
    assert hooks.checkpoint.clock_check == env.clock_check  # the engine's own clock check
    with pytest.raises(C.CheckpointError, match="clock check"):
        C.rotation_hooks(env.ledger, **env.options(clock_check=None))


# ── State loss ──

def test_a_deleted_state_json_or_a_null_record_without_epoch_start_refuses_before_anything(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)  # a candidate a trusted run would adopt
    (env.ledger.staging_dir / "junk.tmp").write_bytes(b"t")
    left = env.ledger.keys_dir / (tsa.KEY_FOLDER_PREFIX + "left")
    left.mkdir(mode=0o700)
    saved = save_state_bytes(env)
    env.ledger.state_path.unlink()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="missing: rebuild it from the relay with the drill's Path B"):
        env.publish()
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.adopt()
    assert env.files() == before and left.exists() and len(env.checks) == checks
    restore_state_bytes(env, saved)
    env.set_state(checkpoint=None, epoch_start=None)
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()
    assert env.files() == before
    # A null record whose marker is for an older epoch than the devices log has reached (a recovery written,
    # perhaps on another laptop, after init's marker).
    env = Env(tmp_path / "older-marker")
    env.recover(None, marker=False)
    assert env.state().epoch_start == 0 and env.devices().epoch == 1
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()
    assert env.files() == before and len(env.checks) == checks


@pytest.mark.parametrize("field,value", [("seq", 5), ("epoch", 1), ("strand", "dk-" + "0" * 32),
                                         ("created_at", "2001-01-01T00:00:00Z")])
def test_a_record_that_doesnt_match_its_own_header_refuses_before_anything(tmp_path, field, value):
    env = Env(tmp_path)
    own_chain(env, 2)
    env.set_state(checkpoint=dict(env.state().checkpoint, **{field: value}))
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="own header"):
        env.publish()
    assert env.files() == before and len(env.checks) == checks


def test_a_deleted_local_folder_refuses_and_a_deleted_staging_folder_gets_its_exclusion_again(tmp_path):
    env = Env(tmp_path)
    env.make()
    for p in sorted(env.ledger.staging_dir.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    env.ledger.staging_dir.rmdir()
    env.add_entries()
    with pytest.raises(C.CheckpointError, match="Time Machine"):
        env.make(exclude_from_backup=None)
    assert not env.ledger.staging_dir.exists()
    assert env.make().made.seq == 2 and env.tm.excluded(env.ledger.staging_dir)
    for p in env.ledger.local_dir.iterdir():
        p.unlink()
    env.ledger.local_dir.rmdir()
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.make()


def test_a_lost_record_rebuild_against_a_drained_staging_folder_refuses(tmp_path):
    env = Env(tmp_path)
    for _ in range(3):
        env.make()
        env.add_entries()
    for s in (1, 2, 3):
        env.drain(0, s)
    env.ledger.state_path.unlink()
    env.set_state(epoch_start=0)  # a rebuilt marker with no record
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="behind"):
        env.publish()
    assert env.files() == before
    for p in sorted((env.mirror / "m").iterdir()):
        p.unlink()
    env.answers[(0, 1)] = C.WRITTEN  # 2.7 HEADs the relay: 200 or 410 means written
    with pytest.raises(C.CheckpointAlarm, match="behind"):
        env.publish()


def test_an_older_state_json_with_a_newer_slot_in_the_mirror_refuses(tmp_path):
    env = Env(tmp_path)
    env.make()
    older = save_state_bytes(env)
    env.add_entries()
    env.make()
    env.drain(0, 2)
    restore_state_bytes(env, older)
    env.add_entries()
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="behind"):
        env.publish()
    assert env.files() == before


def test_an_older_record_the_opening_line_doesnt_name_refuses(tmp_path):
    env = Env(tmp_path)
    one = env.make().made
    env.add_entries()
    env.make()
    env.recover(one.digest)  # the recovery continued from seq 1; this laptop's record is seq 2
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()


def test_state_json_lost_while_a_checkpoint_is_built_is_never_recreated(tmp_path, monkeypatch):
    """Step 8 writes state.json by reloading it: a file gone since step 0 (the TSA step and the clock wait can take
    minutes) is ALARM, never a fresh state holding only the record with seen, the rotation and the marker lost."""
    env = Env(tmp_path)
    env.make()
    with env.lock() as lk:
        C.write_seen(env.ledger.state_path, lk, env.verify(0, 1).seen())
    assert env.rotate().closed
    saved = save_state_bytes(env)
    env.add_entries()
    build = C._Maker._build

    def lose(self, *a, **kw):
        made = build(self, *a, **kw)
        env.ledger.state_path.unlink()
        return made
    monkeypatch.setattr(C._Maker, "_build", lose)
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.make()
    monkeypatch.undo()
    assert not env.ledger.state_path.exists()
    assert env.staged() == [(0, 1), (0, 2), (0, 3)]  # the segments, the sidecar and the custodian file landed
    staged = env.staged_bytes(0, 3)
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()
    assert not env.ledger.state_path.exists()
    restore_state_bytes(env, saved)
    assert env.adopt().adopted == ((0, 3),) and env.staged_bytes(0, 3) == staged
    st = env.state()
    assert st.checkpoint["seq"] == 3 and st.seen is not None and st.rotation is not None and st.epoch_start == 0
    staging_is_clean(env)


def test_a_staging_folder_lost_while_a_checkpoint_is_built_gets_its_exclusion_again_or_refuses(tmp_path,
                                                                                             monkeypatch):
    """Step 8 never creates staging/ without its Time Machine exclusion, even when it went away after step 6."""
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    st = env.ledger.staging_dir
    build = C._Maker._build

    def lose(self, *a, **kw):
        made = build(self, *a, **kw)
        shutil.rmtree(st)
        return made
    monkeypatch.setattr(C._Maker, "_build", lose)
    assert env.make().made.seq == 2
    assert env.tm.excluded(st) and (st / C.staged_name(0, 2)).exists()
    assert stat.S_IMODE(os.lstat(st / "o").st_mode) == 0o700
    rec = env.state().checkpoint
    env.add_entries()
    with pytest.raises(C.CheckpointError, match="Time Machine"):
        env.make(exclude_from_backup=None)
    assert not st.exists() and env.state().checkpoint == rec


# ── Logs put back: the record's devices and readers prefixes (§14.5a prefix check, §18a) ──

def enrol_phone(env: Env, label: str = "phone-1"):
    """A companion enrolled by the laptop and the approver and countersigned by itself; its key and box."""
    k = phone_key(f"{env.label}/{label}")
    ident = Identity(h(f"{env.label}/{label}/box"))
    nonce = sig.b64url_encode(h(f"{env.label}/{label}/nonce")[:16])
    env.append_line("device_enrol", {"device": k.descriptor(label, ident.recipient().to_string()), "nonce": nonce},
                    [env.live_key(), env.kit.keys[env.ak], k])
    return k, ident


def revoked_phone_put_back(env: Env) -> bytes:
    """Seq 2 with phone-1 in recipients, then seq 3 after its revoke; returns the devices bytes from before it."""
    env.make()
    phone, _ = enrol_phone(env)
    assert phone.kid in {r["id"] for r in C.parse_body(env.make().made.body)["recipients"]}
    before_revoke = env.devices_bytes()
    env.append_line("device_revoke", {"kid": phone.kid}, [env.laptop_key])
    assert phone.kid not in {r["id"] for r in C.parse_body(env.make().made.body)["recipients"]}
    return before_revoke


def test_a_devices_log_put_back_to_before_a_revoke_refuses_before_anything_is_signed(tmp_path):
    """A single-file restore or a sync copy puts back devices.jsonl from before a revoke while state.json, in the
    excluded local/, survives: a shorter log than the record cites is ROLLBACK, so the revoked phone is never
    sealed to again and nothing is signed that the laptop's own chain check would refuse."""
    env = Env(tmp_path)
    env.ledger.devices_path.write_bytes(revoked_phone_put_back(env))
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointRollback):
        env.publish()
    with pytest.raises(C.CheckpointRollback):
        env.adopt()
    assert env.files() == before and len(env.checks) == checks and env.state().checkpoint["seq"] == 3


def test_a_devices_log_that_diverges_from_the_records_prefix_is_fork_before_anything_is_signed(tmp_path):
    env = Env(tmp_path)
    before_revoke = revoked_phone_put_back(env)
    ak2 = approver_key(f"{env.label}/hwkey-2")
    other = env.line("approver_enrol", {"approver": ak2.descriptor("hwkey-2")}, [env.root_key, ak2],
                     base=before_revoke)
    env.ledger.devices_path.write_bytes(before_revoke + other + b"\n")
    cited = json.loads(sig.b64url_decode(env.state().checkpoint["header"]))["devices"]["byte_length"]
    assert len(env.devices_bytes()) > cited  # longer than the record's prefix, with other bytes at the revoke
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointFork):
        env.publish()
    assert env.files() == before and len(env.checks) == checks


def test_a_readers_log_put_back_to_before_a_revoke_refuses_before_anything_is_signed(tmp_path):
    env = Env(tmp_path, readers=1)
    env.make()
    before_revoke = env.readers_bytes()
    env.rk.revoke(env.readers_made[0], [env.laptop, env.ak])
    env.ledger.readers_path.write_bytes(env.rk.data)
    assert env.make().made.seq == 2
    env.ledger.readers_path.write_bytes(before_revoke)
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointRollback):
        env.publish()
    assert env.files() == before and len(env.checks) == checks


def test_a_readers_log_put_back_under_a_staged_checkpoint_is_rollback_once_it_is_adopted(tmp_path):
    """The record adoption makes cites the newer readers log, so the prefix check runs again after adoption."""
    env = Env(tmp_path, readers=1)
    env.make()
    before_revoke = env.readers_bytes()
    env.rk.revoke(env.readers_made[0], [env.laptop, env.ak])
    env.ledger.readers_path.write_bytes(env.rk.data)
    stage_and_kill(env)
    env.ledger.readers_path.write_bytes(before_revoke)
    env.add_entries()
    with pytest.raises(C.CheckpointRollback, match="readers"):
        env.publish()
    assert env.state().checkpoint["seq"] == 2 and env.staged() == [(0, 1), (0, 2)]


@pytest.mark.parametrize("log", ["devices", "readers"])
def test_a_log_that_changes_between_the_prefix_check_and_the_covered_read_refuses(tmp_path, monkeypatch, log):
    """A writer that ignored roam.lock: the bytes covered must be the bytes the prefix check passed."""
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    read = C.read_covered
    path = env.ledger.devices_path if log == "devices" else env.ledger.readers_path

    def moved(**kw):
        with open(path, "ab") as fh:
            fh.write(b"{}\n")
        return read(**kw)
    monkeypatch.setattr(C, "read_covered", moved)
    with pytest.raises(C.CheckpointError, match=f"the {log} log changed under roam.lock"):
        env.publish()
    assert env.staged() == [(0, 1)] and env.state().checkpoint["seq"] == 1


def test_a_rotation_over_a_devices_log_put_back_closes_and_reports_the_hooks_alarm(tmp_path):
    env = Env(tmp_path)
    env.ledger.devices_path.write_bytes(revoked_phone_put_back(env))
    r = env.rotate()
    assert r.closed
    assert any("adopting staged checkpoints failed" in n for n in r.notes)
    assert any("first checkpoint wasn't made" in n and ("FORK" in n or "ROLLBACK" in n) for n in r.notes)
    assert env.staged() == [(0, 1), (0, 2), (0, 3)] and env.state().checkpoint["seq"] == 3


# ── Epochs ──

def test_a_kill_between_a_recovery_line_and_its_marker_still_allows_seq_1_via_checkpoint_ref(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    two = env.make().made
    k = env.recover(two.digest, marker=False)
    assert env.state().epoch_start == 0
    res = env.publish()
    m = res.made
    assert (m.epoch, m.seq, m.strand) == (1, 1, k.kid) and json.loads(m.header)["prev"] == two.digest
    assert m.alerts == ()  # a same-laptop epoch change raises no false rewrite alert
    body = env.verify(1, 1).body
    assert all(body["logs"][n]["append_only"] for n in C.LOG_NAMES)
    assert body["snapshot"]["manifest"]["previous_snapshot_digest"] is None
    ch = env.chain([(0, 2), (1, 1)])
    assert ch.abandoned == ()


def test_recovery_then_rotate_before_any_publish_gives_seq_1_and_leaves_the_old_epochs_files(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    two = env.make().made
    env.add_entries()
    stage_and_kill(env)  # (0, 3) is staged above the record and never adopted
    env.recover(two.digest)
    hooks = env.hooks()
    r = env.rotate(hooks=hooks)
    assert r.closed
    assert env.staged() == [(0, 1), (0, 2), (0, 3), (1, 1)]
    one = env.verify(1, 1)
    assert one.prev == two.digest and one.strand == r.new_kid
    assert (0, 3) in hooks.adopt.last.abandoned and (0, 3) in hooks.checkpoint.last.abandoned
    res = env.publish()
    assert res.abandoned == () and res.made is None  # reported once, at the epoch change; now below the record
    for e, s in [(0, 1), (0, 2), (0, 3)]:
        assert (env.ledger.staging_dir / C.sidecar_name(e, s)).exists()  # left for upload
    staging_is_clean(env)


def test_after_root_rotate_new_objects_carry_the_new_rk_recipient_and_not_the_old(tmp_path):
    env = Env(tmp_path)
    one = env.make().made
    rk2 = h("make tests/rk2")
    env.root_rotate(one.digest, rk2)
    res = env.publish()
    m = res.made
    assert (m.epoch, m.seq) == (1, 1) and json.loads(m.header)["root"] == env.ledger.root
    new_rk = Identity(rk2)
    rec = env.state().checkpoint["recipients"]
    assert {"id": "rk", "recipient": new_rk.recipient().to_string()} in rec
    assert EK[0].rk_recipient not in {r["recipient"] for r in rec}
    for ct in [m.container] + [ct for _, ct in m.objects]:
        age.decrypt(ct, [new_rk])
        with pytest.raises(age.AgeError):
            age.decrypt(ct, [RK_ID])
    assert m.base and len(m.objects) >= 2


def test_a_custodian_rekey_forces_a_base_the_new_box_opens(tmp_path):
    env = Env(tmp_path)
    env.make()
    new_box = h("make tests/rekeyed box")
    env.append_line("device_rekey", {"kid": env.laptop, "box": Identity(new_box).recipient().to_string(),
                                     "nonce": None}, [env.laptop_key])
    env.save_ks(dataclasses.replace(env.keystore(), dk_box=new_box))
    m = env.publish().made
    assert m.seq == 2 and m.base
    for ct in [m.container] + [ct for _, ct in m.objects]:
        age.decrypt(ct, [Identity(new_box)])
        with pytest.raises(age.AgeError):
            age.decrypt(ct, [Identity(env.ks0.dk_box)])


def test_a_ledger_rewrite_followed_by_a_rotation_gives_append_only_false_and_the_alert(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.ledger_file.write_bytes(env.ledger_file.read_bytes().replace(b"neutral title 2", b"neutral title 7"))
    hooks = env.hooks()
    env.rotate(hooks=hooks)
    res = hooks.checkpoint.last
    assert res.made.seq == 2
    assert [(a.log, a.reason) for a in res.made.alerts] == [("ledger", C.REWRITTEN)]
    assert any("ledger" in a and "rewritten" in a for a in res.alerts)
    body = env.verify(0, 2).body
    assert body["logs"]["ledger"]["append_only"] is False and body["logs"]["devices"]["append_only"] is True


# ── High-water marks ──

def own_chain(env: Env, n: int) -> None:
    for i in range(n):
        if i:
            env.add_entries()
        env.make()


def fetched(env: Env, *slots):
    return [((e, s), env.open(e, s)) for e, s in slots]


def compare(env: Env, items, **kw):
    args = dict(ledger_id=LEDGER, root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t,
                forks_dir=env.ledger.forks_dir, epochs=EK)
    args.update(kw)
    return C.compare_seen(env.state(), items, **args)


def twin(env: Env, seq: int, *, devices: bytes | None = None, title: str = "neutral twin") -> C.Made:
    """What a copied keystore could sign at (0, seq): the same logs plus one more ledger entry, chained to this
    laptop's checkpoint at seq - 1 (`devices` when the copy's devices log diverges)."""
    ks = env.keystore()
    data = env.devices_bytes() if devices is None else devices
    dev = replay_devices(data, ledger_id=LEDGER, root=env.ledger.root, now=env.clock.t)
    led = C.cover(env.ledger_file.read_bytes())[0] + canonicalize(
        {"id": "IRP-2001-02-02-001", "type": "decision", "title": title}) + b"\n"
    cov = C.covered_from(ledger=led, devices=data, readers=env.readers_bytes(), disclosures=b"")
    prev = env.records[seq - 2] if seq > 1 else None
    return C.build_checkpoint(
        ledger_id=LEDGER, covered=cov, devices=dev, signer_seed=ks.dk_seed, strand=ks.dk_id, epoch_keys=EK[0], seq=seq,
        prev=None if prev is None else prev["digest"], created_at=ts(env.clock.t),
        previous_logs=None if prev is None else prev["logs"],
        previous_recipients=None if prev is None else prev["recipients"],
        previous_snapshot_digest=None if prev is None else prev["snapshot_digest"], full_base=prev is None)


def test_own_record_n_plus_1_with_seen_n_minus_5_and_a_fetched_n_minus_3_is_rollback(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 7)  # N = 6
    with env.lock() as lk:
        C.write_seen(env.ledger.state_path, lk, env.verify(0, 1).seen())
    with pytest.raises(C.CheckpointRollback):
        compare(env, fetched(env, (0, 3)))


def test_a_relay_withholding_the_newest_slot_is_rollback(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 3)
    with pytest.raises(C.CheckpointRollback):
        compare(env, fetched(env, (0, 1), (0, 2)))


def test_a_matching_head_passes_and_seen_is_written_only_upwards(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 3)
    got = compare(env, fetched(env, (0, 1), (0, 2), (0, 3)))
    assert got.head.seq == 3 and got.abandoned == () and got.mark["seq"] == 3
    with env.lock() as lk:
        st = C.write_seen(env.ledger.state_path, lk, got.mark)
        assert st.seen == got.mark
        assert C.write_seen(env.ledger.state_path, lk, got.mark).seen == got.mark  # the same mark again: no change
        with pytest.raises(C.CheckpointError):
            C.write_seen(env.ledger.state_path, lk, env.verify(0, 1).seen())  # never lowered
        other = C.verify_checkpoint(C.open_custodian(twin(env, 3).container, [RK_ID]), ledger_id=LEDGER,
                                    root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t, slot=(0, 3))
        with pytest.raises(C.CheckpointError):
            C.write_seen(env.ledger.state_path, lk, other.seen())  # the same slot with another digest: a fork
    assert env.state().seen == got.mark
    assert compare(env, fetched(env, (0, 3))).head.seq == 3


def test_a_same_seq_fetch_with_another_digest_writes_a_proof_whose_halves_verify(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    other = twin(env, 2)
    item = [((0, 2), C.open_custodian(other.container, [RK_ID]))]
    with pytest.raises(C.CheckpointFork) as exc:
        compare(env, item)
    proofs = sorted(env.ledger.forks_dir.glob("fork-*.json"))
    assert len(proofs) == 1 and exc.value.proof == proofs[0]
    mine, theirs = env.records[1]["digest"], other.digest
    lo, hi = sorted([mine[7:19], theirs[7:19]])
    assert proofs[0].name == f"fork-0-2-{lo}-{hi}.json"
    assert stat.S_IMODE(os.lstat(proofs[0]).st_mode) == 0o600
    fp = C.verify_fork_proof(proofs[0].read_bytes(), LEDGER, env.ledger.root, now=env.clock.t)
    assert (fp.epoch, fp.seq) == (0, 2)
    assert {fp.a.digest, fp.b.digest} == {mine, theirs}
    assert fp.a.label == fp.b.label == C.NONE and fp.a.gen_time is None
    raw = json.loads(proofs[0].read_bytes())
    assert set(raw) == {"v", "kind", "a", "b"} and raw["kind"] == "fork-proof"
    assert canonicalize(raw) == proofs[0].read_bytes()
    first = proofs[0].read_bytes()
    with pytest.raises(C.CheckpointFork):
        compare(env, item)  # the same fork again: the proof is kept, never replaced
    assert sorted(env.ledger.forks_dir.glob("fork-*.json")) == proofs and proofs[0].read_bytes() == first


def test_a_proof_still_verifies_when_the_two_sides_cite_diverging_devices_logs(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    rekey = env.line("device_rekey", {"kid": env.laptop, "box": Identity(h("copy box")).recipient().to_string(),
                                      "nonce": None}, [env.laptop_key])
    diverged = env.devices_bytes() + rekey + b"\n"
    other = twin(env, 2, devices=diverged)
    with pytest.raises(C.CheckpointFork):
        compare(env, [((0, 2), C.open_custodian(other.container, [RK_ID]))])
    proof = next(env.ledger.forks_dir.glob("fork-*.json")).read_bytes()
    fp = C.verify_fork_proof(proof, LEDGER, env.ledger.root, now=env.clock.t)
    sides = {fp.a.digest: fp.a, fp.b.digest: fp.b}
    assert sides[other.digest].devices_length == len(diverged)
    assert sides[env.records[1]["digest"]].devices_length == len(env.devices_bytes())


def test_a_fabricated_proof_with_a_self_made_key_is_rejected_never_alarm(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    other = twin(env, 2)
    with pytest.raises(C.CheckpointFork):
        compare(env, [((0, 2), C.open_custodian(other.container, [RK_ID]))])
    path = next(env.ledger.forks_dir.glob("fork-*.json"))
    good = json.loads(path.read_bytes())
    honest = "a" if sig.b64url_decode(good["a"]["checkpoint"]) == sig.b64url_decode(env.records[1]["header"]) else "b"
    fake = "b" if honest == "a" else "a"

    def rejected(proof) -> None:
        with pytest.raises(C.CheckpointError) as exc:
            C.verify_fork_proof(canonicalize(proof), LEDGER, env.ledger.root, now=env.clock.t)
        assert not isinstance(exc.value, C.CheckpointAlarm)

    # A self-made key over this laptop's devices log: the signer isn't a custodian there.
    forger = SeedKey(h("a self-made key"))
    hd = json.loads(sig.b64url_decode(good[fake]["checkpoint"]))
    hd["strand"] = forger.kid
    header = canonicalize(hd)
    bad = json.loads(json.dumps(good))
    bad[fake]["checkpoint"] = sig.b64url_encode(header)
    bad[fake]["sig"] = sig.b64url_encode(C.sign_header(header, forger.seed, forger.kid))
    rejected(bad)
    # A self-made devices log from its own genesis: it doesn't replay from the pinned root.
    kit = DevicesKit("forger")
    kit.genesis()
    fkey = EdKey("forger/laptop-1/key")
    kit.custodian("laptop-1", key=fkey)
    hd = json.loads(sig.b64url_decode(good[fake]["checkpoint"]))
    hd.update(strand=fkey.kid, devices={"byte_length": len(kit.data), "digest": C.digest_of(kit.data)})
    header = canonicalize(hd)
    bad = json.loads(json.dumps(good))
    bad[fake] = {"checkpoint": sig.b64url_encode(header),
                 "sig": sig.b64url_encode(C.sign_header(header, fkey.seed, fkey.kid)),
                 "devices": sig.b64url_encode(kit.data)}
    rejected(bad)
    # The same checkpoint twice, other slots, a pin that isn't the root, junk: rejected too.
    same = json.loads(json.dumps(good))
    same[fake] = same[honest]
    rejected(same)
    for mutate in (lambda p: p.update(v=2), lambda p: p.update(kind="other"), lambda p: p[fake].update(extra="x"),
                   lambda p: p[fake].update(devices=p[honest]["devices"][:-4])):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        rejected(bad)
    with pytest.raises(C.CheckpointError):
        C.verify_fork_proof(path.read_bytes(), LEDGER, "rt-" + "0" * 32, now=env.clock.t)
    with pytest.raises(C.CheckpointError):
        C.verify_fork_proof(b"not json", LEDGER, env.ledger.root, now=env.clock.t)
    # Each side's signature is checked: an edited header under the old signature, on either side, a flipped
    # signature byte, and a key_id that names the strand's key under another prefix.
    for side in ("a", "b"):
        bad = json.loads(json.dumps(good))
        hd = json.loads(sig.b64url_decode(bad[side]["checkpoint"]))
        hd["created_at"] = "2001-01-01T00:00:00Z"
        bad[side]["checkpoint"] = sig.b64url_encode(canonicalize(hd))
        rejected(bad)

    def flip(s_):
        raw = bytearray(sig.b64url_decode(s_["sig"]))
        raw[3] ^= 1
        s_["sig"] = sig.b64url_encode(bytes(raw))
    for change in (flip, lambda s_: s_.update(key_id="ck-" + s_["key_id"][3:])):
        bad = json.loads(json.dumps(good))
        s_ = json.loads(sig.b64url_decode(bad[fake]["sig"]))
        change(s_)
        bad[fake]["sig"] = sig.b64url_encode(canonicalize(s_))
        rejected(bad)

    def resign(side, change):
        hd = json.loads(sig.b64url_decode(side["checkpoint"]))
        change(hd)
        header = canonicalize(hd)
        return dict(side, checkpoint=sig.b64url_encode(header),
                    sig=sig.b64url_encode(C.sign_header(header, env.laptop_key.seed, env.laptop)))
    # Two honest checkpoints at consecutive seqs aren't a fork.
    r0 = env.records[0]
    rejected({"v": 1, "kind": "fork-proof", "b": good[honest],
              "a": {"checkpoint": r0["header"], "sig": r0["sig"], "devices": good[honest]["devices"]}})
    # A side signed for another ledger.
    bad = json.loads(json.dumps(good))
    bad[fake] = resign(good[fake], lambda hd: hd.update(ledger_id="ILID-" + "b2" * 16))
    rejected(bad)
    # A side whose devices bytes stop short of what its header cites (an older prefix where a since-revoked
    # signer was still active, say).
    bad = json.loads(json.dumps(good))
    lines = sig.b64url_decode(good[fake]["devices"]).split(b"\n")[:-1]
    bad[fake]["devices"] = sig.b64url_encode(b"".join(line + b"\n" for line in lines[:-1]))
    rejected(bad)
    # Both sides claiming an epoch their own prefix isn't at.
    rejected({"v": 1, "kind": "fork-proof", "a": resign(good["a"], lambda hd: hd.update(epoch=1)),
              "b": resign(good["b"], lambda hd: hd.update(epoch=1))})


def test_a_fetched_head_above_the_own_record_is_alarm_except_in_a_path_b_rebuild(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    other = twin(env, 3)  # chained to this laptop's record at seq 2
    item = [((0, 3), C.open_custodian(other.container, [RK_ID]))]
    with pytest.raises(C.CheckpointAlarm, match="someone else is signing"):
        compare(env, item)
    got = compare(env, item, rebuild=True)
    assert got.head.seq == 3 and got.mark["digest"] == other.digest


def test_compare_seen_across_a_recovery_reports_abandoned_seqs_not_fork(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 3)
    got = compare(env, fetched(env, (0, 1), (0, 2), (0, 3)))
    with env.lock() as lk:
        C.write_seen(env.ledger.state_path, lk, got.mark)
    env.recover(env.records[1]["digest"])  # the recovery continued from seq 2 (the last before a suspected copy)
    env.set_state(checkpoint=None)        # so this laptop starts the new epoch from its marker
    res = env.publish()
    assert (res.made.epoch, res.made.seq) == (1, 1)
    got = compare(env, fetched(env, (1, 1)))
    assert got.head.epoch == 1 and got.abandoned == ((0, 3),)
    # Path B rebuilding a lost record: the mark is seen at (0, 3), and the head is in the next epoch.
    rebuilt = dataclasses.replace(env.state(), checkpoint=None)
    with pytest.raises(C.CheckpointAlarm, match="someone else is signing"):
        C.compare_seen(rebuilt, fetched(env, (1, 1)), ledger_id=LEDGER, root=env.ledger.root,
                       devices=env.devices_bytes(), now=env.clock.t, forks_dir=env.ledger.forks_dir, epochs=EK)
    got2 = C.compare_seen(rebuilt, fetched(env, (1, 1)), ledger_id=LEDGER, root=env.ledger.root,
                          devices=env.devices_bytes(), now=env.clock.t, forks_dir=env.ledger.forks_dir, epochs=EK,
                          rebuild=True)
    assert got2.abandoned == ((0, 3),) and got2.mark == got.mark
    with env.lock() as lk:
        assert C.write_seen(env.ledger.state_path, lk, got.mark).seen["epoch"] == 1


def test_the_record_and_seen_disagreeing_at_one_slot_is_fork(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    other = twin(env, 2)
    v = C.verify_checkpoint(C.open_custodian(other.container, [RK_ID]), ledger_id=LEDGER, root=env.ledger.root,
                            devices=env.devices_bytes(), now=env.clock.t, slot=(0, 2))
    env.set_state(seen=v.seen())
    with pytest.raises(C.CheckpointFork):
        compare(env, fetched(env, (0, 2)))
    for p in env.ledger.forks_dir.glob("fork-*.json"):
        p.unlink()
    with pytest.raises(C.CheckpointFork) as exc:  # fetching only a lower slot: the marks alone are the fork
        compare(env, fetched(env, (0, 1)))
    assert exc.value.proof is not None and list(env.ledger.forks_dir.glob("fork-0-2-*.json"))


def test_a_fetched_slot_replay_is_alarm_and_nothing_fetched_is_an_error(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    with pytest.raises(C.CheckpointAlarm, match="slot replay"):
        compare(env, [((0, 1), env.open(0, 2))])
    with pytest.raises(C.CheckpointError):
        compare(env, [])


def forged_next(env: Env, epoch: int, seq: int, rec) -> C.Shipped:
    """A checkpoint the live key signs at (epoch, seq) after `rec`, with a prev that isn't `rec`'s digest."""
    ks = env.keystore()
    data = env.devices_bytes()
    dev = replay_devices(data, ledger_id=LEDGER, root=env.ledger.root, now=env.clock.t)
    led = C.cover(env.ledger_file.read_bytes())[0] + canonicalize(
        {"id": "IRP-2001-02-02-001", "type": "decision", "title": "neutral twin"}) + b"\n"
    cov = C.covered_from(ledger=led, devices=data, readers=env.readers_bytes(), disclosures=b"")
    made = C.build_checkpoint(
        ledger_id=LEDGER, covered=cov, devices=dev, signer_seed=ks.dk_seed, strand=ks.dk_id, epoch_keys=EK[epoch],
        seq=seq, prev="sha256-" + "1" * 64, created_at=ts(env.clock.t), previous_logs=rec["logs"],
        previous_recipients=rec["recipients"], previous_snapshot_digest=rec["snapshot_digest"])
    return C.open_custodian(made.container, [RK_ID])


def test_a_path_b_head_above_the_mark_that_doesnt_chain_back_is_fork(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    with pytest.raises(C.CheckpointFork, match="chain back"):
        compare(env, [((0, 3), forged_next(env, 0, 3, env.records[1]))], rebuild=True)


def test_a_path_b_head_in_a_later_epoch_that_doesnt_chain_back_to_its_seq_1_is_alarm_not_fork(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 2)
    rec02 = env.state().checkpoint
    env.recover(rec02["digest"])
    env.add_entries()
    assert (env.publish().made.epoch, env.state().checkpoint["seq"]) == (1, 1)
    items = [((1, 1), env.open(1, 1)), ((1, 2), forged_next(env, 1, 2, env.state().checkpoint))]
    state = dataclasses.replace(env.state(), checkpoint=rec02)  # Path B, with the mark from epoch 0
    with pytest.raises(C.CheckpointAlarm, match="chain back") as exc:
        C.compare_seen(state, items, ledger_id=LEDGER, root=env.ledger.root, devices=env.devices_bytes(),
                       now=env.clock.t, forks_dir=env.ledger.forks_dir, epochs=EK, rebuild=True)
    assert not isinstance(exc.value, C.CheckpointFork)


def test_a_fetched_side_that_doesnt_verify_is_alarm_with_no_proof(tmp_path):
    """A relay serving a bad signature at the record's slot can't plant a fork proof."""
    env = Env(tmp_path)
    own_chain(env, 2)
    sh = C.open_custodian(twin(env, 2).container, [RK_ID])
    s = json.loads(sh.sig)
    raw = bytearray(sig.b64url_decode(s["sig"]))
    raw[0] ^= 1
    s["sig"] = sig.b64url_encode(bytes(raw))
    with pytest.raises(C.CheckpointAlarm) as exc:
        compare(env, [((0, 2), dataclasses.replace(sh, sig=canonicalize(s)))])
    assert not isinstance(exc.value, C.CheckpointFork)
    assert not list(env.ledger.forks_dir.glob("fork-*.json"))


# ── Smaller rules ──

def test_at_most_one_checkpoint_per_publish_even_after_adopting(tmp_path):
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    env.add_entries()  # changed again after the staged one
    res = env.publish()
    assert res.adopted == ((0, 2),) and res.made.seq == 3 and env.staged() == [(0, 1), (0, 2), (0, 3)]
    assert env.publish().made is None


def test_the_sidecar_format_is_strict():
    ids = ("o/" + "a" * 64, "o/" + "b" * 64)
    data = C.sidecar_bytes(ids)
    assert data == b"o/" + b"a" * 64 + b"\n" + b"o/" + b"b" * 64 + b"\n" and C.parse_sidecar(data) == ids
    assert C.parse_sidecar(b"") == ()
    for bad in (data[:-1], data + data[:67], b"o/" + b"A" * 64 + b"\n", b"\n", b"o/" + b"a" * 63 + b"\n"):
        with pytest.raises(C.CheckpointError):
            C.parse_sidecar(bad)
    for bad in ((ids[0], ids[0]), ("m/" + "a" * 64,)):
        with pytest.raises(C.CheckpointError):
            C.sidecar_bytes(bad)
    assert C.staged_name(0, 7) == "custodian-0-7" and C.sidecar_name(2, 1) == "custodian-2-1.objects"
    for e, s_ in ((0, 0), (-1, 1), (True, 1)):
        with pytest.raises(C.CheckpointError):
            C.staged_name(e, s_)


def test_unknown_tsa_options_are_refused(tmp_path):
    env = Env(tmp_path)
    with pytest.raises(C.CheckpointError, match="TSA option"):
        env.make(tsa_options={"proxy": "http://127.0.0.1:1"})


def test_hostile_values_given_to_compare_seen_are_alarm_never_a_crash(tmp_path):
    env = Env(tmp_path)
    own_chain(env, 1)
    for items in ([((0,), env.open(0, 1))], [((0, 1), "not shipped")], [None], [((0, 1), env.open(0, 1), 3)]):
        with pytest.raises(C.CheckpointAlarm):
            compare(env, items)
    with pytest.raises(C.CheckpointAlarm):
        C.compare_seen("not a state", fetched(env, (0, 1)), ledger_id=LEDGER, root=env.ledger.root,
                       devices=env.devices_bytes(), now=env.clock.t, forks_dir=env.ledger.forks_dir)


def test_the_tsa_step_gets_last_present_and_the_newest_covered_line(tmp_path, pki):
    """A genTime behind the record's last PRESENT in the epoch, or a covered line dated more than an hour after
    genTime, is never attached as PRESENT: make_checkpoint hands the TSA step both bounds."""
    env = Env(tmp_path)
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
        assert env.make(tsa_options=tsa_options(pki)).made.label == C.PRESENT
        lp = env.state().checkpoint["last_present"]
        env.add_entries()
        fake.token = fk.TokenOptions(gen_time=fk.T - timedelta(minutes=10))
        res = env.make(tsa_options=tsa_options(pki))
        assert res.made.label == C.NONE and any("gen_time_behind" in a for a in res.alerts)
        assert env.state().checkpoint["last_present"] == lp
        fake.token = fk.TokenOptions(gen_time=fk.T)
        env.clock.t += timedelta(hours=2)
        ak2 = approver_key(f"{env.label}/hwkey-2")
        env.append_line("approver_enrol", {"approver": ak2.descriptor("hwkey-2")}, [env.root_key, ak2])
        res = env.make(tsa_options=tsa_options(pki))
        assert res.made.label == C.NONE and any("line_after_gen_time" in a for a in res.alerts)
        fake.token = fk.TokenOptions(gen_time=env.clock.t)
        env.add_entries()
        res = env.make(tsa_options=tsa_options(pki))
        assert res.made.label == C.PRESENT
        assert env.state().checkpoint["last_present"]["devices_length"] == len(env.devices_bytes())


def test_the_tsa_steps_gentime_floor_starts_again_at_a_new_epoch(tmp_path, pki):
    """last_present is reset at a new epoch: epoch 1's seq 1 takes a token whose genTime is before epoch 0's last
    PRESENT one."""
    env = Env(tmp_path)
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
        assert env.make(tsa_options=tsa_options(pki)).made.label == C.PRESENT
        env.recover(env.records[-1]["digest"])
        fake.token = fk.TokenOptions(gen_time=fk.T - timedelta(minutes=10))
        res = env.make(tsa_options=tsa_options(pki))
    assert (res.made.epoch, res.made.seq, res.made.label) == (1, 1, C.PRESENT)
    assert env.state().checkpoint["last_present"]["gen_time"] == ts(fk.T - timedelta(minutes=10))


def lines_two_hours_after_t(env: Env, *, make: bool = True) -> None:
    """Seq 1 (NONE, unless `make` is false), then devices lines dated two hours after fk.T, the genTime of a
    default token."""
    if make:
        env.make()
    env.clock.t += timedelta(hours=2)
    k2 = env.enrol_custodian("laptop-2")
    env.append_line("device_revoke", {"kid": k2.kid}, [env.live_key()])


def test_a_token_failing_only_the_pin_still_meets_the_line_rule_so_new_pins_never_make_it_present(tmp_path, pki):
    """A token past the line rule is discarded even when it fails only the pin: kept as UNVERIFIED, it would turn
    PRESENT (and anchor last_present) as soon as tsa.json pinned its CA before the staged file was adopted."""
    env = Env(tmp_path)
    lines_two_hours_after_t(env)
    with fk.FakeTSA(pki, token=fk.TokenOptions(issuer="wrong")) as fake:
        write_tsa_json(env, fake, pki)
        stage_and_kill(env, tsa_options=tsa_options(pki))
        assert env.open(0, 2).tsr is None
        e = TsaEntry(name="tsa-a", url=fake.url("tsa-a.test"), auth="none", ca_sha256=(pki.wrong_ca.pin,),
                     subject_o=ORG)
        with env.lock() as lk:
            S.write_local_file(env.ledger.tsa_path, tsa.encode_tsa_list([e]), lk, exclude_from_backup=env.tm)
        env.said.clear()
        assert env.adopt(tsa_options=tsa_options(pki)).adopted == ((0, 2),)
    rec = env.state().checkpoint
    assert rec["label"] == C.NONE and rec["gen_time"] is None and rec["last_present"] is None
    assert env.said == [C.TSA_CHANGED]  # the pins changed since it was staged: the loud local alert


@pytest.mark.parametrize("seq", [2, 1], ids=["after a record", "from the epoch's marker"])
def test_adoption_never_anchors_on_a_token_dated_over_an_hour_before_a_line_it_covers(tmp_path, pki, seq):
    """Adopting applies the custodian line rule to a PRESENT candidate before its genTime becomes last_present."""
    env = Env(tmp_path)
    lines_two_hours_after_t(env, make=seq > 1)
    stage_and_kill(env)
    sh = env.open(0, seq)
    token = fk.build_token(hashlib.sha256(sh.header).digest(), pki)  # genTime fk.T, under the pinned CA
    reseal(env, 0, seq, C.parse_body(sh.body)["recipients"], members=lambda m: m.update({C.MEMBER_TSR: token}))
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
    assert env.verify(0, seq, pins=pins(pki)).label == C.PRESENT
    before, record = env.files(), env.state().checkpoint
    with pytest.raises(C.CheckpointAlarm, match="1 hour"):
        env.adopt(tsa_options=tsa_options(pki))
    assert env.files() == before and env.state().checkpoint == record


# ── Integration: the rotation engine with the real checkpoint hooks (the 2.6 gate audit) ──

def test_end_to_end_a_real_rotation_stamps_the_new_strands_first_checkpoint_under_a_timeout(tmp_path, pki, caplog,
                                                                                             capfd):
    """rotation.rotate() with checkpoint.rotation_hooks(), nothing faked but the clock, Time Machine and the
    hardware key: a NONE checkpoint, then tsa.json naming a Basic-auth TSA over real TLS, then a rotation whose
    hook stamps the new strand's first checkpoint PRESENT (the credentials ride through the rotated keystore) and
    whose in-run publish makes none. Both sides verify the chain, and the NONE checkpoint is TRANSITIVE."""
    caplog.set_level(logging.DEBUG)
    env = Env(tmp_path, readers=1)
    secret = "pw-e2e-only-1"
    env.save_ks(dataclasses.replace(env.keystore(), tsa_creds={"tsa-a/user": "user-1", "tsa-a/password": secret}))
    assert env.make().made.label == C.NONE  # no tsa.json yet
    opts = tsa_options(pki)
    published: list = []
    with fk.FakeTSA(pki, auth="basic", password=secret) as fake:
        data = write_tsa_json(env, fake, pki, auth="basic")
        hooks = env.hooks(tsa_options=opts, publish=lambda lock: published.append(
            env.make(when_due=True, lock=lock, tsa_options=opts)))
        r = within(60, lambda: env.rotate(hooks=hooks))
    assert r.closed and env.delivered == env.readers_made and not r.notes
    made = hooks.checkpoint.last.made
    assert (made.seq, made.strand, made.label, made.tsa, made.base) == (2, r.new_kid, C.PRESENT, "tsa-a", True)
    assert len(fake.requests) == 1 and fake.imprints == [hashlib.sha256(made.header).digest()]
    assert len(published) == 1 and published[0].made is None  # at most one checkpoint per rotation
    st = env.state()
    assert st.checkpoint["digest"] == made.digest and st.seen is None and st.rotation.closed
    assert st.checkpoint["last_present"] == {"epoch": 0, "gen_time": ts(fk.T),
                                             "devices_length": len(env.devices_bytes()),
                                             "readers_length": len(env.readers_bytes())}
    assert C.parse_body(made.body)["tsa_policy_digest"] == C.digest_of(data)
    # The custodian side, with the pins from tsa.json.
    vs = [env.verify(0, s, pins=pins(pki)) for s in (1, 2)]
    ch = C.verify_chain(vs, ledger_id=LEDGER, root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t,
                        readers=env.readers_bytes(), custodian=True, logs=env.logs_now())
    assert ch.label(0, 1) == "TRANSITIVE via (0, 2)" and ch.label(0, 2) == C.PRESENT
    # A reader: only the header, the signature and the token, with a bundle's tsa_pins.
    bundle = tsa.pins_from_tsas(tsa.parse_tsa_list(data).entries)
    rvs = []
    for s in (1, 2):
        sh = env.open(0, s)
        rvs.append(C.verify_checkpoint(C.Shipped(header=sh.header, sig=sh.sig, tsr=sh.tsr), ledger_id=LEDGER,
                                       root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t,
                                       pins=bundle, slot=(0, s)))
    rch = C.verify_chain(rvs, ledger_id=LEDGER, root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t)
    assert rvs[1].label == C.PRESENT and rch.label(0, 1) == "TRANSITIVE via (0, 2)"
    # No credential anywhere the run could have put one.
    basic = base64.b64encode(f"user-1:{secret}".encode()).decode()
    out, err = capfd.readouterr()
    for text in [*r.notes, *env.said, *hooks.checkpoint.last.alerts, *hooks.checkpoint.last.notes, repr(r),
                 repr(hooks.checkpoint.last), repr(hooks.checkpoint), repr(published), repr(env.keystore()),
                 caplog.text, out, err, str(env.state())]:
        assert secret not in text and basic not in text and "Authorization" not in text


def test_a_failing_tsa_inside_the_rotation_hook_leaks_no_credential_and_the_rotation_closes(tmp_path, pki, caplog,
                                                                                            capfd):
    """The hook's TSA step refused (a wrong password): the checkpoint goes out as NONE with a loud alert naming the
    TSA, and neither the rotation's notes, the say lines, the logs nor any repr carry the credential."""
    caplog.set_level(logging.DEBUG)
    env = Env(tmp_path)
    secret = "pw-e2e-wrong-2"
    env.save_ks(dataclasses.replace(env.keystore(), tsa_creds={"tsa-a/user": "user-1", "tsa-a/password": secret}))
    env.make()
    opts = tsa_options(pki)
    with fk.FakeTSA(pki, auth="basic", password="pw-the-tsa-wants") as fake:
        write_tsa_json(env, fake, pki, auth="basic")
        hooks = env.hooks(tsa_options=opts)
        r = within(60, lambda: env.rotate(hooks=hooks))
    res = hooks.checkpoint.last
    assert r.closed and res.made.label == C.NONE and res.made.strand == r.new_kid
    assert any("tsa-a" in a and "http_status" in a for a in res.alerts)
    assert any("tsa-a" in s for s in env.said)  # the loud alert reached the person
    basic = base64.b64encode(f"user-1:{secret}".encode()).decode()
    out, err = capfd.readouterr()
    for text in [*r.notes, *env.said, *res.alerts, *res.notes, repr(r), repr(res), repr(res.made), caplog.text,
                 out, err, str(env.state())]:
        assert secret not in text and basic not in text and "Authorization" not in text


def test_after_state_json_is_lost_mid_rotation_publish_refuses_until_recovery_or_a_path_b_rebuild(tmp_path):
    def lose_state(env: Env) -> R.RotationResult:
        def lose(step):
            if step == "signed":
                env.ledger.state_path.unlink()
        r = env.rotate(hooks=env.hooks(engine_progress=lose))
        assert r.closed and any("missing" in n for n in r.notes)
        env.add_entries()
        before = env.files()
        with pytest.raises(C.CheckpointAlarm, match="missing"):
            env.publish()
        assert env.files() == before
        return r

    # Recovery: a new epoch whose root-signed opening line names the newest checkpoint known, (0, 1).
    env = Env(tmp_path / "recovery")
    one = env.make().made
    lose_state(env)
    k = env.recover(one.digest)
    m = env.publish().made
    assert (m.epoch, m.seq, m.strand) == (1, 1, k.kid) and json.loads(m.header)["prev"] == one.digest
    env.chain([(0, 1), (1, 1)])

    # Path B (step 3) rebuilds the record and the mark from what the relay holds; the staged copy stands in here.
    env = Env(tmp_path / "path-b")
    env.make()
    r = lose_state(env)
    v = env.verify(0, 1)
    with env.lock() as lk:
        S.update_state(env.ledger.state_path, lk, exclude_from_backup=env.tm, checkpoint=v.record(None))
        C.write_seen(env.ledger.state_path, lk, v.seen(), exclude_from_backup=env.tm)
    m = env.publish().made
    assert (m.epoch, m.seq, m.strand, m.base) == (0, 2, r.new_kid, True)
    assert env.state().seen["seq"] == 1
    env.chain([(0, 1), (0, 2)])


def test_a_deleted_local_folder_makes_the_seen_and_tsa_json_writers_exclude_it_again_or_refuse(tmp_path):
    env = Env(tmp_path)
    env.make()
    mark = env.verify(0, 1).seen()
    shutil.rmtree(env.ledger.local_dir)
    with env.lock() as lk:
        with pytest.raises(C.CheckpointError, match="Time Machine"):
            C.write_seen(env.ledger.state_path, lk, mark)
        assert not env.ledger.local_dir.exists()
        C.write_seen(env.ledger.state_path, lk, mark, exclude_from_backup=env.tm)
    assert env.tm.excluded(env.ledger.local_dir) and env.state().seen == mark
    with pytest.raises(C.CheckpointAlarm, match="missing"):  # a mark alone isn't a record: step 0 still refuses
        env.make()
    home_local = env.ledger.tsa_path.parent
    with env.lock() as lk:
        S.write_local_file(env.ledger.tsa_path, b"{}", lk, exclude_from_backup=env.tm)
        shutil.rmtree(home_local)
        with pytest.raises(S.StateError, match="Time Machine"):
            S.write_local_file(env.ledger.tsa_path, b"{}", lk)
        assert not home_local.exists()
        S.write_local_file(env.ledger.tsa_path, b"{}", lk, exclude_from_backup=env.tm)
    assert env.tm.excluded(home_local)


def test_nothing_staged_or_mirrored_carries_a_record_id_the_ledger_id_or_ledger_text(tmp_path):
    """§23: no record id, ledger_id, tag or IRP- substring in ciphertext or names; every custodian object opens with
    RK and carries one stanza per recipient."""
    env = Env(tmp_path, readers=1)
    for _ in range(2):
        env.make()
        env.add_entries()
    env.rotate()
    env.drain(0, 1)
    needles = [b"IRP-", LEDGER.encode(), b"neutral title", b"decision", env.ledger.root.encode(),
               env.laptop.encode(), *(r.encode() for r in env.readers_made)]
    files = {**env.files(env.ledger.staging_dir), **env.files(env.mirror)}
    assert len([n for n in files if n.startswith("custodian-") and not n.endswith(".objects")]) == 2
    for name, data in files.items():
        for needle in needles:
            assert needle not in name.encode() and needle not in data, (name, needle)
    for (e, s) in env.staged():
        sh = env.open(e, s)
        body = C.parse_body(sh.body)
        assert sh.stanzas == len(body["recipients"]) == 2  # RK plus the one active custodian box
    for name, data in files.items():
        if name.startswith("m/"):
            assert C.open_custodian(data, [RK_ID]).stanzas == 2


def test_tsa_json_has_one_path_in_tsa_state_and_the_ledger():
    home = S.roam_home()
    ledger = R.Ledger(ledger_id=LEDGER, root="rt-" + "0" * 32, keys_dir=home / "keys",
                      ledger_dir=home / "ledgers" / LEDGER)
    assert tsa.TSA_PATH.expanduser() == S.tsa_path() == ledger.tsa_path


def test_the_slot_check_also_refuses_a_staged_file_for_the_next_seq_when_adoption_left_it(tmp_path, monkeypatch):
    """Step 6's staging half sits behind adoption (which takes or refuses anything staged above the record), so it
    is reached here by switching adoption off: the next seq's staged file alone refuses before anything is signed."""
    env = Env(tmp_path)
    env.make()
    env.add_entries()
    stage_and_kill(env)
    monkeypatch.setattr(C._Run, "adopt", lambda self: None)
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="behind: staging/ already holds epoch 0 seq 2"):
        env.publish()
    assert env.files() == before and len(env.checks) == checks + 1


HYGIENE = ("import sys; sys.path.insert(0, sys.argv[1]); "
           "import irp.roam.checkpoint, irp.roam.tsa, irp.roam.state, irp.roam.rotation, irp.roam.keys; "
           "print(sorted(m for m in ('cryptography', 'asn1crypto', 'rfc8785', 'fido2') if m in sys.modules))")


def test_the_2_6_modules_import_without_loading_any_optional_dependency():
    """§23 hygiene: base capture loads no extras. A fresh isolated interpreter imports every 2.6 module and none of
    the optional dependencies; on /usr/bin/python3 (3.9, no extras installed) that also proves the syntax."""
    import subprocess
    pythons = [sys.executable] + [p for p in ("/usr/bin/python3",) if os.path.exists(p)]
    for py in pythons:
        out = subprocess.run([py, "-I", "-c", HYGIENE, str(ROOT)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (py, out.stderr[-2000:])
        assert out.stdout.strip() == "[]", (py, out.stdout)


# ── Fresh keys owed: the --now marker (owner-approved amendment to §14.6a and §18a) ──

def no_approver(env: Env) -> None:
    """The hardware key isn't there: every approver is tried and none answers."""
    env.keyring = Keyring(on_ask=lambda: env.events.append("tap"))


def approver_back(env: Env) -> None:
    env.keyring = Keyring(env.kit.keys[env.ak], on_ask=lambda: env.events.append("tap"))


def publish_refuses_naming_now(env: Env) -> None:
    """A publish refuses at step 4 (the signer check) with nothing staged, written or signed, and check_publish
    refuses too, both naming irp roam rotate --now."""
    staged, before, checks = env.staged(), env.files(), len(env.checks)
    with pytest.raises(C.CheckpointError, match="fresh keys are still owed: run irp roam rotate --now") as exc:
        env.publish()
    assert not isinstance(exc.value, C.CheckpointAlarm)
    assert env.staged() == staged and env.files() == before and len(env.checks) == checks
    with pytest.raises(R.PublishRefused, match="rotate --now") as refused:
        R.check_publish(env.keystore(), env.devices(), env.state(), env.clock.t)
    assert not refused.value.alarm


def test_owed_a_now_over_an_open_rotation_whose_fresh_tap_fails_blocks_publish(tmp_path):
    env = Env(tmp_path)
    env.make()
    open_ = env.rotate(hooks=env.hooks(confirm=False))
    assert not open_.closed
    no_approver(env)
    with pytest.raises(R.RotationError, match="no approver answered.*stands.*rotate --now"):
        env.rotate(suspected=True)
    st = env.state()
    assert st.rotation.rotate_idx == open_.rotate_idx and st.rotation.closed and st.rotation.suspected
    assert st.fresh_keys_owed is not None and env.staged() == [(0, 1)]
    env.add_entries()  # a checkpoint is due: only the marker stops the suspected key signing it
    publish_refuses_naming_now(env)


def test_owed_a_later_successful_now_clears_it_and_the_hook_makes_the_fresh_strands_first_checkpoint(tmp_path):
    env = Env(tmp_path)
    env.make()
    open_ = env.rotate(hooks=env.hooks(confirm=False))
    no_approver(env)
    with pytest.raises(R.RotationError):
        env.rotate(suspected=True)
    env.add_entries()
    publish_refuses_naming_now(env)
    approver_back(env)
    r = env.rotate(suspected=True)
    assert r.closed and r.previous is None and r.old_kid == open_.new_kid != r.new_kid
    st = env.state()
    assert st.fresh_keys_owed is None
    assert (st.checkpoint["seq"], st.checkpoint["strand"]) == (2, r.new_kid) and env.staged() == [(0, 1), (0, 2)]
    assert env.verify(0, 2).strand == r.new_kid
    env.chain([(0, 1), (0, 2)])
    assert env.publish().made is None  # the hook's checkpoint already covers the new entries


def test_owed_a_plain_now_with_nothing_open_whose_tap_fails_also_blocks_publish(tmp_path):
    env = Env(tmp_path)
    env.make()
    no_approver(env)
    with pytest.raises(R.RotationError, match="no approver answered.*rotate --now"):
        env.rotate(suspected=True)
    assert env.state().fresh_keys_owed is not None and len(env.devices().lines) == len(L.split_log(env.kit.data))
    env.add_entries()
    publish_refuses_naming_now(env)
    approver_back(env)
    with pytest.raises(R.RotationError, match="fresh keys are still owed"):
        env.rotate()  # a plain rotation never stands in for --now
    publish_refuses_naming_now(env)
    r = env.rotate(suspected=True)
    assert r.closed and env.state().fresh_keys_owed is None and env.state().checkpoint["strand"] == r.new_kid


def test_owed_a_lost_state_json_never_lets_the_suspected_key_sign(tmp_path):
    """The marker lives in state.json, so a lost state.json loses it. The safest behaviour with only that file:
    (1) a --now run keeps its marker in hand and writes it back whenever it writes state.json, including where it
    reports that fresh keys are owed, so a file lost while it runs comes back with the marker; (2) after the run, a
    lost file stops everything that signs at step 0 (the record went with it), nothing is cleaned, adopted or
    signed, and only the drill's Path B or recovery brings the record back; --now still works on the rebuilt
    file. Path B (step 3) can't tell whether fresh keys were owed, so it never reads the missing marker as
    "nothing owed": it asks, and sets the marker again without a clear answer (state.py)."""
    env = Env(tmp_path)
    env.make()
    saved = env.state().checkpoint
    no_approver(env)
    hooks = env.hooks()
    real_adopt = hooks.adopt

    def lose_then_adopt(ks, lock):  # the fresh rotation's adopt hook runs after the marker write, before the tap
        env.ledger.state_path.unlink()
        real_adopt(ks, lock)
    hooks.adopt = lose_then_adopt
    with pytest.raises(R.RotationError, match="no approver answered.*rotate --now") as exc:
        env.rotate(hooks=hooks, suspected=True)
    st = env.state()
    assert st.fresh_keys_owed is not None  # (1) written back where the run says fresh keys are owed
    assert (st.checkpoint, st.epoch_start) == (None, None)
    env.add_entries()
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()
    assert env.files() == before and len(env.checks) == checks
    with pytest.raises(R.PublishRefused, match="rotate --now"):
        R.check_publish(env.keystore(), env.devices(), env.state(), env.clock.t)
    # (2) Lost after the run: nothing signs until the record is rebuilt.
    env.ledger.state_path.unlink()
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match="missing: rebuild it from the relay with the drill's Path B"):
        env.publish()
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.adopt()
    assert env.files() == before and len(env.checks) == checks
    approver_back(env)
    r = env.rotate(suspected=True)
    assert r.closed and env.state().fresh_keys_owed is None
    assert any("missing" in n for n in r.notes)  # the hook's step-0 ALARM, reported; the rotation stands
    with pytest.raises(C.CheckpointAlarm, match="missing"):
        env.publish()
    # Standing in for Path B's rebuild (step 3), told that a --now ran since the loss (it did, just above).
    env.set_state(checkpoint=saved, epoch_start=0, fresh_keys_owed=None)
    res = env.publish()
    assert res.made.seq == 2 and res.made.strand == r.new_kid and res.made.base


# ── Step 0 also refuses a record whose own signer has been revoked (owner-approved) ──

REVOKED = ("the last checkpoint's signer has been revoked; this epoch can only continue through recovery; nothing "
           "was signed")


def test_a_record_whose_signer_was_revoked_is_alarm_before_anything_is_cleaned_adopted_or_signed(tmp_path):
    env = Env(tmp_path)
    env.make()
    old = env.keystore().dk_id
    no_hook = R.Hooks(confirm_config=lambda config: True, deliver=env._deliver)  # the record stays on `old`
    r = env.rotate(hooks=no_hook)
    assert r.closed and env.state().checkpoint["strand"] == old
    assert env.publish().made.strand == r.new_kid  # a retired (rotated out) signer is fine
    # Again, with the retired key revoked before the next one.
    env = Env(tmp_path / "revoked")
    env.make()
    old = env.keystore().dk_id
    no_hook = R.Hooks(confirm_config=lambda config: True, deliver=env._deliver)
    env.rotate(hooks=no_hook)
    env.append_line("device_revoke", {"kid": old}, [env.live_key()])
    env.add_entries()
    (env.ledger.staging_dir / "junk.tmp").write_bytes(b"t")
    left = env.ledger.keys_dir / (tsa.KEY_FOLDER_PREFIX + "left")
    left.mkdir(mode=0o700)
    before, checks = env.files(), len(env.checks)
    with pytest.raises(C.CheckpointAlarm, match=REVOKED):
        env.publish()
    with pytest.raises(C.CheckpointAlarm, match=REVOKED):
        env.adopt()
    assert env.files() == before and left.exists() and len(env.checks) == checks
    assert env.state().checkpoint["strand"] == old and env.staged() == [(0, 1)]


def test_a_revoked_record_signer_is_reported_by_a_rotations_hooks_and_the_rotation_stands(tmp_path):
    env = Env(tmp_path)
    env.make()
    old = env.keystore().dk_id
    env.rotate(hooks=R.Hooks(confirm_config=lambda config: True, deliver=env._deliver))
    env.append_line("device_revoke", {"kid": old}, [env.live_key()])
    r = env.rotate()
    assert r.closed
    assert any("adopting staged checkpoints failed" in n and "revoked" in n for n in r.notes)
    assert any("first checkpoint wasn't made" in n and "revoked" in n for n in r.notes)
    assert env.staged() == [(0, 1)] and env.state().checkpoint["strand"] == old


# ── Review round 2, second pass ──

def revoked_record_signer(env: Env) -> str:
    """Seq 1 by laptop-1's first key, which a rotation with no checkpoint hook retired and a later line revoked:
    publish raises step 0's REVOKED_SIGNER ALARM. Returns the revoked kid."""
    env.make()
    old = env.keystore().dk_id
    r = env.rotate(hooks=R.Hooks(confirm_config=lambda config: True, deliver=env._deliver))
    assert r.closed and env.state().checkpoint["strand"] == old
    env.append_line("device_revoke", {"kid": old}, [env.live_key()])
    env.add_entries()
    with pytest.raises(C.CheckpointAlarm, match=REVOKED):
        env.publish()
    return old


@pytest.mark.parametrize("point", ("no kill",) + KILLS)
def test_after_a_revoked_record_signer_recovery_names_the_record_and_seq_1_is_made_or_adopted(tmp_path, point):
    """The way out the ALARM names: adopt_staged refuses too (recovery runs it first), so recovery doesn't adopt;
    it names state.json's record as checkpoint_ref. Then seq 1 of the new epoch is made, and a kill at any write
    of its step 8 leaves files the next publish adopts byte for byte or cleans: the record's revoked signer is
    vouched for by the root-signed checkpoint_ref, and a separate revoke inside the old epoch doesn't undo that."""
    env = Env(tmp_path)
    revoked_record_signer(env)
    before = env.files()
    with pytest.raises(C.CheckpointAlarm, match=REVOKED):
        env.adopt()
    assert env.files() == before
    rec = env.state().checkpoint
    env.recover(rec["digest"])
    if point != "no kill":
        stage_and_kill(env, point, when_due=True)
    landed = point == "write state"
    after_kill = env.staged_bytes(1, 1) if landed else None
    res = env.publish()
    if landed:
        assert res.adopted == ((1, 1),) and res.made is None
        assert env.staged_bytes(1, 1) == after_kill
        assert env.state().checkpoint["digest"] == C.digest_of(env.open(1, 1).header)
    else:
        assert res.adopted == () and (res.made.epoch, res.made.seq) == (1, 1)
    assert json.loads(env.open(1, 1).header)["prev"] == rec["digest"]
    assert (env.state().checkpoint["epoch"], env.state().checkpoint["seq"]) == (1, 1)
    assert env.publish().made is None
    assert env.staged() == [(0, 1), (1, 1)]
    staging_is_clean(env)


def test_the_named_ref_waiver_is_only_for_the_record_the_opening_line_names(tmp_path):
    """Adoption's link check waives the revoked-signer test only for the record the epoch's root-signed opening
    line names; a reader-side verify of that record still refuses its revoked signer."""
    env = Env(tmp_path)
    revoked_record_signer(env)
    rec = env.state().checkpoint
    env.recover(rec["digest"])
    with pytest.raises(C.CheckpointError, match="revoked"):
        C.verify_checkpoint(C.shipped_from_mark(rec), ledger_id=LEDGER, root=env.ledger.root,
                            devices=env.devices_bytes(), now=env.clock.t, cited=rec["digest"], slot=(0, 1))


def test_a_fetched_object_whose_devices_bytes_dont_fit_is_alarm_unless_it_verifies(tmp_path):
    """Nobody can pick the FORK verdict (a key that equivocated) by shipping devices bytes that don't fit: an
    object at the record's slot that doesn't verify is ALARM with no proof, whatever its devices member holds."""
    env = Env(tmp_path)
    own_chain(env, 2)
    rec = env.state().checkpoint
    for change in ("body_digest", "devices digest"):
        hd = json.loads(sig.b64url_decode(rec["header"]))
        if change == "body_digest":
            hd["body_digest"] = "sha256-" + "ab" * 32
        else:
            hd["devices"]["digest"] = "sha256-" + "ee" * 32
        members = C.custodian_members(header=canonicalize(hd), sig=sig.b64url_decode(rec["sig"]),
                                      mac=b"0" * 64 + b"\n", body=b"{}", devices=b"not the devices log\n")
        forged = C.open_custodian(C.seal_custodian(members, rec["recipients"]), [RK_ID])
        with pytest.raises(C.CheckpointError) as exc:
            compare(env, [((0, 2), forged)])
        assert type(exc.value) is C.CheckpointAlarm, (change, exc.value)
        assert "doesn't verify" in str(exc.value) and "no fork proof" in str(exc.value)
        assert not list(env.ledger.forks_dir.glob("fork-*.json"))
    # A genuine twin whose devices member is junk still makes a proof, from the newest log's prefix it cites.
    other = twin(env, 2)
    sh = C.open_custodian(other.container, [RK_ID])
    with pytest.raises(C.CheckpointFork) as exc:
        compare(env, [((0, 2), dataclasses.replace(sh, devices=b"junk\n"))])
    assert exc.value.proof is not None
    fp = C.verify_fork_proof(exc.value.proof.read_bytes(), LEDGER, env.ledger.root, now=env.clock.t)
    assert {fp.a.digest, fp.b.digest} == {rec["digest"], other.digest}


@pytest.mark.parametrize("change", ["longer", "other digest"])
def test_an_unsigned_header_never_gets_a_rollback_or_fork_verdict_about_the_devices_log(tmp_path, change):
    """The signature is checked before the devices prefix it cites (§18a "Verifying one checkpoint", steps 2
    and 3), so a header nobody signed is a bad signature, never ROLLBACK or FORK of the trust root."""
    env = Env(tmp_path)
    own_chain(env, 2)
    sh = env.open(0, 1)
    hd = json.loads(sh.header)
    if change == "longer":
        hd["devices"]["byte_length"] += 1000
    else:
        hd["devices"]["digest"] = "sha256-" + "ee" * 32
    junk = canonicalize({"alg": "ed25519", "key_id": hd["strand"], "sig": "A" * 86})
    header = canonicalize(hd)
    args = dict(ledger_id=LEDGER, root=env.ledger.root, devices=env.devices_bytes(), now=env.clock.t, slot=(0, 1))
    with pytest.raises(C.CheckpointError, match="bad checkpoint signature") as exc:
        C.verify_checkpoint(C.Shipped(header=header, sig=junk), **args)
    assert type(exc.value) is C.CheckpointError
    custodian = dataclasses.replace(sh, header=header, sig=junk)
    with pytest.raises(C.CheckpointError, match="bad checkpoint signature") as exc:
        C.verify_checkpoint(custodian, epochs=EK, **args)
    assert type(exc.value) is C.CheckpointAlarm
    with pytest.raises(C.CheckpointError) as exc:
        compare(env, [((0, 1), custodian), ((0, 2), env.open(0, 2))])
    assert type(exc.value) is C.CheckpointAlarm and "bad checkpoint signature" in str(exc.value)


@pytest.mark.parametrize("offset,adopted", [(3600, True), (3601, False), (5400, False)])
def test_the_custodian_line_rule_at_adoption_allows_exactly_one_hour(tmp_path, pki, offset, adopted):
    env = Env(tmp_path)
    env.make()
    env.clock.t = fk.T + timedelta(seconds=offset - 120)  # each hand-written line moves the clock on 60 s
    k2 = env.enrol_custodian("laptop-2")
    env.append_line("device_revoke", {"kid": k2.kid}, [env.live_key()])  # the last covered line: genTime + offset
    stage_and_kill(env)
    sh = env.open(0, 2)
    token = fk.build_token(hashlib.sha256(sh.header).digest(), pki)  # genTime fk.T, under the pinned CA
    reseal(env, 0, 2, C.parse_body(sh.body)["recipients"], members=lambda m: m.update({C.MEMBER_TSR: token}))
    with fk.FakeTSA(pki) as fake:
        write_tsa_json(env, fake, pki)
    assert env.verify(0, 2, pins=pins(pki)).label == C.PRESENT
    if adopted:
        assert env.adopt(tsa_options=tsa_options(pki)).adopted == ((0, 2),)
        assert env.state().checkpoint["last_present"]["gen_time"] == ts(fk.T)
        return
    before, record = env.files(), env.state().checkpoint
    with pytest.raises(C.CheckpointAlarm, match="1 hour"):
        env.adopt(tsa_options=tsa_options(pki))
    assert env.files() == before and env.state().checkpoint == record


BRACKETED = {"https://[::1/x": "::1", "https://[secret-host]/": "secret-host", "https://u:p@[bad]/": "[bad]",
             "https://ex]ample.org/": "ex]ample", "https://[v1.x]/": "v1.x"}


@pytest.mark.parametrize("url", sorted(BRACKETED))
def test_a_tsa_json_url_that_doesnt_parse_refuses_before_signing(tmp_path, url):
    """urlsplit's ValueError (which urls raise it depends on the interpreter) is the documented 'tsa.json: ...;
    nothing was signed' CheckpointError, never a bare ValueError, and it doesn't repeat the url."""
    env = Env(tmp_path)
    data = canonicalize({"v": 1, "tsas": [{"name": "tsa-a", "url": url, "auth": "none", "ca_sha256": ["ab" * 32]}]})
    with env.lock() as lk:
        S.write_local_file(env.ledger.tsa_path, data, lk, exclude_from_backup=env.tm)
    before = env.files()
    with pytest.raises(C.CheckpointError) as exc:
        env.make()
    assert type(exc.value) is C.CheckpointError and str(exc.value).startswith("tsa.json:")
    assert "nothing was signed" in str(exc.value)
    assert BRACKETED[url] not in str(exc.value) and "u:p" not in str(exc.value)
    assert env.files() == before and env.staged() == []
