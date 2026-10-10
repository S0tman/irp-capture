"""Rotation for Roaming IRP (spec v0.3 §14.6, §14.6a; step 2.5c).

The laptop rotates its device key (DK, signing seed plus box key) and its capability key (CK) at least every
31 days, and on demand (`irp roam rotate`, or `--now` for a suspected copy, §20.6). One interactive run:

1. takes `keys/roam.lock` exclusively, opens the devices and then the readers LogWriter (a torn tail moves to
   forks/), loads the keystore, replays both logs and checks that the live kid is an active custodian. If a
   rotation is under way it resumes instead. It runs the clock check and takes `at`, later than the last line
   of both logs, waiting up to 5 minutes for the clock to pass them and never making a time up;
2. makes the next keys in memory and gathers every signature on the `device_rotate` line: the old DK, an
   approver tap (each active approver in label order until one answers; the person can cancel) and the new
   DK. Nothing is written before this step ends;
3. saves the keystore with `pending` set;
4. runs the clock check and a replay with the line added, then appends it;
5. writes the rotation record and the old CK's probe to state.json, then saves the keystore with the new keys
   live, `pending` null and the old box key kept decrypt-only in `box_prev`. The old seeds are dropped;
6. renews every reader active at its renewal line's own `at`: a fresh identity, `expires` = min(at + 38
   days, the last approval + 90 days), each one held until its delivery is confirmed;
7. shows the relay config edit and the re-approval list, hands each identity to the delivery hook, and
   closes the record once the person confirms the config edit.

A failure after step 3, or in steps 6 and 7, is decided from the log, never from the exception: the engine
reopens both logs, reloads the keystore and looks for the line, and its message says how far the rotation got
and what to run. Pending keys are never dropped in a run whose append was attempted, since a rollback may not
have reached the disk, and never on the strength of an absent line until the devices log is synced. A crash
anywhere leaves something the next `irp roam rotate` resumes.

`--now` has one meaning: "I suspect the live keys". Plain `irp roam rotate` finishes whatever is open, keeping
its recorded flag, unless fresh keys are owed and the rotation came before the --now (then it finishes as
suspected, whatever its record says); a line that landed without its record, or a record lost with state.json,
also finishes as suspected: no overlap. `--now` always ends with fresh keys: it finishes whatever is open,
upgraded to suspected first, and once that record closes it rotates the live keys too, with a new tap,
publishing only after that fresh rotation closes. So advice to finish always says plain `irp roam rotate`; it
names `--now` only while fresh keys are still owed. Status comes from the signed log too: a key's age runs from
the line that brought it in, so editing a file can't reset it, and a keystore whose live key the log has retired
is ALARM, never "unfinished".

Hooks for later steps are injected callables with do-nothing defaults: 2.6 (adopting staged checkpoints and
the new strand's first checkpoint), 2.7 (the old-CK probe, the quarantine entries, re-minting capabilities), 1b
(delivering a reader bundle) and 2.8 (the commands, the publish refusals and a publish once the rotation
closes). It never signs with the old DK after the rotate line, never keeps the old signing seeds past step 5,
never puts a retired box in an audience, never writes a reader identity to disk, never taps on its own, never
resumes unattended and never changes K_c or K_a.

Step 2.6 (§18a) changes four things. Every engine write to state.json reloads it under the lock and replaces
only `rotation`, `probes` and `held`, so the `checkpoint`, `seen` and `epoch_start` that other writers keep
there are never lost. A fresh rotation calls `adopt(ks, lock)` after the replay and before the tap, so nothing
staged above the record is normally left sealed to the box the rotation retires; a failure there is reported
and the rotation carries on. `checkpoint(ks, rotate_idx, lock)` runs once the record closes (never while
`--now` finishes an open rotation, whose keys are the suspected ones); a failure there is reported and never
undoes the rotation, and state.json is reloaded after it. And `publish(lock)` gets the rotation's own lock.
`rotate()` and `resume_rotation()` accept a roam.lock the caller already holds exclusively (the drill), and
never release a lock they didn't take; `signing_session` has an exclusive mode and takes a held lock too. The
first write that needs `local/` creates it with its Time Machine exclusion (`exclude_from_backup`), or refuses.

Fresh keys owed (owner-approved amendment to §14.6a and §18a). A --now run first writes state.json's
`fresh_keys_owed` marker, `{since, from_idx}`, through the same reload-and-replace write, before it resumes or
rotates anything (resume with suspected=True too, even when it then refuses because nothing is unfinished:
the refusal says fresh keys are now owed). `from_idx` is the devices log's line count at that moment, so the
fresh rotation is the one whose line lands at `from_idx` or later. The write that closes that rotation's record
clears it, before the checkpoint hook runs, whichever run finishes it; closing an older rotation (the --now
finish phase, or a plain run finishing one) never does. While it's set, `unfinished`, `check_signer` and
`check_publish` refuse with "fresh keys are still owed: run irp roam rotate --now", so no checkpoint is made and
nothing is published or signed, and a plain `irp roam rotate` never starts a rotation (it would keep the
suspected CK for 7 days). A rotation from before the --now (its line below `from_idx`) finishes as suspected in
any run while it's set, plain or --now, even if the --now stopped before it wrote the upgraded record. A marker
that can't be written is said and the rotation carries on (it may still end with fresh keys); the run keeps it
in hand and writes it back with each state write and wherever it stops while fresh keys are owed, so a
state.json lost meanwhile comes back with it.
"""
from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional, Sequence, Tuple

from . import approver, sig
from . import keys as _keys
from . import logs as _logs
from . import state as _state
from .age import Identity, Recipient, generate_identity
from .keys import BoxPrev, Keystore, Pending, RoamLock
from .logs import DEVICES_KIND, READERS_KIND, DevicesLog, LogError, LogWriter, ReadersLog
from .state import REMOVE, Probe, RoamState, RotationRecord

WARN_AFTER = timedelta(days=28)
OVERDUE_AFTER = timedelta(days=31)
CK_LIFETIME = timedelta(days=38)      # a CK's not_after: the line that made it plus 38 days
OLD_CK_OVERLAP = timedelta(days=7)
RENEW_FOR = timedelta(days=38)        # rotation renewals only; enrolment keeps its 30-day default
APPROVAL_CAP = timedelta(days=90)
PROBE_DELAY = timedelta(seconds=120)
PROBE_LIFETIME = timedelta(days=90)
CLOCK_WAIT = timedelta(minutes=5)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


class RotationError(Exception):
    """A rotation can't run, or stopped. Unless the message says what landed, nothing was written."""


class RotationAlarm(RotationError):
    """The keystore, state.json and the logs disagree, or a log fails replay: ALARM, publishing stops."""


class Unfinished(RotationError):
    """A rotation is under way (`pending` set, or a rotation open). Signers refuse; only irp roam rotate runs."""


class PublishRefused(RotationError):
    """Publishing refuses (§15.6 step 0, R1). `alarm` marks the cases that raise ALARM."""

    def __init__(self, message: str, *, alarm: bool = False):
        super().__init__(message)
        self.alarm = alarm


def _parse(t: str) -> datetime:
    return datetime.strptime(t, _FMT)


def _ts(t: datetime) -> str:
    return t.strftime(_FMT)


def _now(now: datetime) -> datetime:
    """A clock reading as naive UTC in whole seconds (every line's `at` is the clock in whole seconds)."""
    return _logs._utc(now).replace(microsecond=0)


def _body(devices: DevicesLog, idx: int) -> dict:
    return _logs.parse_line(devices.lines[idx], DEVICES_KIND)[0]


@dataclass(frozen=True)
class Ledger:
    """Where one ledger's roaming files live, and its pins."""
    ledger_id: str
    root: str          # the pinned current root id
    keys_dir: Path     # ~/.irp-roam/keys (keystore, master key slots, roam.lock)
    ledger_dir: Path   # ~/.irp-roam/ledgers/<ledger_id>

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys_dir", Path(self.keys_dir))
        object.__setattr__(self, "ledger_dir", Path(self.ledger_dir))

    @property
    def devices_path(self) -> Path:
        return self.ledger_dir / "devices.jsonl"

    @property
    def readers_path(self) -> Path:
        return self.ledger_dir / "readers.jsonl"

    @property
    def forks_dir(self) -> Path:
        return self.ledger_dir / "forks"

    @property
    def home(self) -> Path:
        """~/.irp-roam: the folder that holds keys/."""
        return self.keys_dir.parent

    @property
    def local_dir(self) -> Path:
        """ledgers/<id>/local/, excluded from Time Machine (§18a)."""
        return _state.local_dir(self.ledger_dir)

    @property
    def state_path(self) -> Path:
        return _state.state_path(self.ledger_dir)

    @property
    def staging_dir(self) -> Path:
        """ledgers/<id>/staging/, excluded from Time Machine (§18a)."""
        return _state.staging_dir(self.ledger_dir)

    @property
    def tsa_path(self) -> Path:
        """~/.irp-roam/local/tsa.json (§18a)."""
        return _state.tsa_path(self.home)


# ── Status (age from the signed log) ──

@dataclass(frozen=True)
class RotationStatus:
    kid: str
    label: str
    introduced_idx: int
    introduced_at: str
    age: timedelta
    state: str  # ok, warn or overdue

    @property
    def days(self) -> int:
        """Whole days, for display only: the states compare exact seconds."""
        return self.age.days


def introduced(devices: DevicesLog, kid: str) -> Tuple[int, str]:
    """The idx and `at` of the line that brought `kid` in: `device_enrol` or `device_rotate` with that
    `device.kid`, or `recovery` with that `new_device`. A recovery that keeps a kid, a root rotation and a
    rekey never bring one in."""
    for idx, line in enumerate(devices.lines):
        body, _ = _logs.parse_line(line, DEVICES_KIND)
        event = body["event"]
        if (event in ("device_enrol", "device_rotate") and body["device"]["kid"] == kid) or \
                (event == "recovery" and body["new_device"] == kid):
            return idx, body["at"]
    raise RotationAlarm(f"ALARM: no line of the devices log brought in {kid}")


def _grade(age: timedelta) -> str:
    if age > OVERDUE_AFTER:
        return "overdue"
    if age >= WARN_AFTER:
        return "warn"
    return "ok"


def rotation_status(devices: DevicesLog, kid: str, now: datetime) -> RotationStatus:
    """How old the custodian key `kid` is, measured from the `at` of the line that introduced it: `ok` under
    28 days, `warn` from 28 days, `overdue` above 31 days (§15.6 step 0). `kid` must be an active custodian."""
    tail = devices.state()
    d = tail.devices.get(kid)
    if d is None or d.cls != "custodian":
        raise RotationAlarm(f"ALARM: {kid} isn't an active custodian in the devices log")
    idx, at = introduced(devices, kid)
    if tail.status[kid][1] != idx:  # pragma: no cover - logs.py keeps the introducing idx for an active key
        raise RotationAlarm(f"ALARM: the devices log disagrees about where {kid} came in")
    age = _now(now) - _parse(at)
    return RotationStatus(kid=kid, label=d.label, introduced_idx=idx, introduced_at=at, age=age, state=_grade(age))


def custodian_ages(devices: DevicesLog, now: datetime) -> List[RotationStatus]:
    """rotation_status for every active custodian, by label."""
    tail = devices.state()
    out = [rotation_status(devices, kid, now) for kid, d in tail.devices.items() if d.cls == "custodian"]
    return sorted(out, key=lambda s: s.label)


def old_ck_not_after(rotate_at: str, introduced_at: str, *, suspected: bool) -> str:
    """The outgoing CK's relay not_after: min(at + 7 days, its own), where its own is the `at` of the line that
    introduced the outgoing DK plus 38 days (a CK is made with its DK and printed that way). It reads
    "remove" when its own isn't after `at`, or when the rotation is suspected (no overlap). Never later than
    what the relay already holds."""
    at, own = _parse(rotate_at), _parse(introduced_at) + CK_LIFETIME
    if suspected or own <= at:
        return REMOVE
    return _ts(min(at + OLD_CK_OVERLAP, own))


OWED_AFTER_EDIT = ("once the config edit is applied, run irp roam rotate --now: the current keys are still the ones "
                   "you suspect")
OWED = "run irp roam rotate --now to start again: the current keys are still the ones you suspect"
OWED_NOW = "fresh keys are still owed: run irp roam rotate --now"


def owes_before(owed: Optional[Mapping[str, Any]], idx: int) -> bool:
    """Whether the rotation at devices line `idx` came before the --now that owes fresh keys (so its keys are
    among the suspected ones, and closing it never clears the marker)."""
    return owed is not None and idx < owed["from_idx"]


def clears_owed(owed: Optional[Mapping[str, Any]], rec: RotationRecord) -> bool:
    """Whether closing `rec` clears the marker: it's the fresh rotation, a suspected one whose line landed at
    `from_idx` or later (a plain rotation never starts while the marker is set)."""
    return owed is not None and rec.suspected and rec.rotate_idx >= owed["from_idx"]


def _finish_advice(suspected: bool) -> str:
    """How to finish a rotation under way: always plain irp roam rotate, which keeps the recorded flag. `--now`
    means only "I suspect the live keys" (it always ends with fresh keys), so it's never the way to finish."""
    if suspected:
        return "irp roam rotate (it finishes with no overlap, so reader bundles and the phone stop at once)"
    return "irp roam rotate"


def _rotate_line(devices: DevicesLog, *, old: str, new: str, box: str) -> Optional[int]:
    """The idx of a `device_rotate` with this `old`, `device.kid` and `device.box`, if one landed."""
    for idx, line in enumerate(devices.lines):
        body, _ = _logs.parse_line(line, DEVICES_KIND)
        if body["event"] == "device_rotate" and body["old"] == old and body["device"]["kid"] == new and \
                body["device"]["box"] == box:
            return idx
    return None


def _pending_absent(devices: DevicesLog, p: Pending) -> bool:
    """No line names the pending kid or the pending box."""
    return p.dk_id not in devices.state().status and Identity(p.dk_box).recipient().public not in devices.boxes


def pending_verdict(ks: Keystore, devices: DevicesLog) -> Tuple[str, Optional[int]]:
    """What the devices log says about the keystore's `pending` keys:
    ("landed", r) when the `device_rotate` from the live kid to the pending kid and box is at r and the
    pending kid is active; ("absent", None) when no line names the pending kid or box and the live kid is
    active; ("disagree", None) otherwise, which is ALARM."""
    p = ks.pending
    if p is None:
        raise RotationError("the keystore has no pending keys")
    tail = devices.state()
    hit = _rotate_line(devices, old=ks.dk_id, new=p.dk_id, box=p.dk_recipient)
    if hit is not None and tail.is_active(p.dk_id):
        return "landed", hit
    if hit is None and tail.is_active(ks.dk_id) and _pending_absent(devices, p):
        return "absent", None
    return "disagree", None


def rotation_open(devices: DevicesLog, kid: str, st: RoamState) -> Optional[int]:
    """The idx r of an open rotation: the live kid is an active custodian that came in by a `device_rotate` at
    r, and state.json has no closed record for r. None otherwise (a retired or unknown kid is never "open":
    it's ALARM, found by the live check)."""
    d = devices.state().devices.get(kid)
    if d is None or d.cls != "custodian":
        return None
    idx, _ = introduced(devices, kid)
    if _body(devices, idx)["event"] != "device_rotate":
        return None
    rec = st.rotation
    if rec is not None and rec.rotate_idx == idx and rec.closed:
        return None
    return idx


def _suspected_at(st: RoamState, r: int) -> bool:
    """Whether the rotation at r finishes as suspected: its record says so, or it has no record (a line that
    landed without one, or a record lost with state.json, is finished as suspected: no overlap), or fresh keys
    are owed and it came before the --now that owes them (its keys are among the suspected ones)."""
    rec = st.rotation
    return rec is None or rec.rotate_idx != r or rec.suspected or owes_before(st.fresh_keys_owed, r)


def unfinished(ks: Keystore, devices: DevicesLog, st: RoamState) -> Optional[str]:
    """Why the keystore can't sign yet (status shows "unfinished"), or None. The advice to finish is plain
    `irp roam rotate`, saying when the rotation finishes as suspected (no overlap). While state.json's
    `fresh_keys_owed` is set it's "fresh keys are still owed: run irp roam rotate --now", unless the fresh
    rotation itself is under way (its line landed at `from_idx` or later): then only finishing it is owed."""
    owed = st.fresh_keys_owed
    if ks.pending is not None:
        verdict, hit = pending_verdict(ks, devices)
        if verdict == "landed":
            if owes_before(owed, hit):
                return (f"{OWED_NOW} (it finishes the rotation whose line landed at devices line {hit} first, then "
                        "rotates the keys you suspect)")
            return ("a rotation is under way (its line landed but its next keys aren't live yet): finish with "
                    + _finish_advice(_suspected_at(st, hit)))
        if owed is not None:
            return (f"{OWED_NOW} (a rotation's next keys are saved but its line isn't in the log: it decides from "
                    "the log, then rotates the keys you suspect)")
        return "a rotation is under way (its next keys are saved but its line isn't in the log): finish with " \
               "irp roam rotate"
    r = rotation_open(devices, ks.dk_id, st)
    if r is not None:
        if owes_before(owed, r):
            return (f"{OWED_NOW} (it finishes the rotation at devices line {r} first, then rotates the keys you "
                    "suspect)")
        return (f"the rotation at devices line {r} isn't finished (readers to renew or deliver, or the relay "
                f"config edit to confirm): finish with {_finish_advice(_suspected_at(st, r))}")
    if owed is not None:
        return f"{OWED_NOW} (the current keys are still the ones you suspect)"
    return None


def _check_live(ks: Keystore, devices: DevicesLog) -> None:
    tail = devices.state()
    d = tail.devices.get(ks.dk_id)
    if d is None or d.cls != "custodian":
        was = tail.status.get(ks.dk_id)
        what = "the log doesn't know it" if was is None else f"{was[0]} at devices line {was[1]}"
        raise RotationAlarm(f"ALARM: the keystore's device key {ks.dk_id} isn't an active custodian ({what}); "
                            "publishing stops")
    if Identity(ks.dk_box).recipient().public != d.box:
        raise RotationAlarm("ALARM: the keystore's box key isn't the live device's box in the devices log; "
                            "publishing stops")


def check_signer(ks: Keystore, devices: DevicesLog, st: RoamState) -> None:
    """Signers check the log (§14.6a). First the live kid must be an active custodian at the tail replayed,
    with the keystore's box, or ALARM: a kid the log doesn't know, or one retired by a later rotation (a
    keystore restored from between two rotations), names where it ended. The one exception is a rotation
    whose line landed while its next keys are still pending: the live kid is retired by that very line. Then,
    with `pending` set, a rotation open, or fresh keys owed (state.json's `fresh_keys_owed`), refuse
    (Unfinished; an interactive run offers irp roam rotate, or irp roam rotate --now while fresh keys are owed)."""
    if ks.pending is not None:
        verdict, _ = pending_verdict(ks, devices)
        if verdict == "disagree":
            raise RotationAlarm("ALARM: the keystore's pending keys and the devices log disagree; publishing stops")
        if verdict == "absent":
            _check_live(ks, devices)
        raise Unfinished(unfinished(ks, devices, st))
    _check_live(ks, devices)
    why = unfinished(ks, devices, st)
    if why:
        raise Unfinished(why)


def check_publish(ks: Keystore, devices: DevicesLog, st: RoamState, now: datetime) -> frozenset:
    """The rotation checks a publish makes before it signs (enforced by 2.8). Refuses while a rotation is
    unfinished (an unattended run never resumes: "finish with irp roam rotate") or fresh keys are owed ("fresh
    keys are still owed: run irp roam rotate --now"), on any mismatch (ALARM),
    when this laptop's key is overdue (only rotation runs), and with ALARM while another active custodian is
    overdue (R1). Returns the held readers, whose feeds the publisher skips (R3)."""
    try:
        check_signer(ks, devices, st)
    except Unfinished as exc:
        raise PublishRefused(f"publishing waits: {exc}", alarm=False) from None
    except RotationAlarm as exc:
        raise PublishRefused(str(exc), alarm=True) from None
    mine = rotation_status(devices, ks.dk_id, now)
    if mine.state == "overdue":
        raise PublishRefused(f"rotation overdue: {mine.label}'s key is {mine.days} days old (more than 31), so "
                             "publishing refuses until you run irp roam rotate", alarm=False)
    for other in custodian_ages(devices, now):
        if other.kid != ks.dk_id and other.state == "overdue":
            raise PublishRefused(f"ALARM: {other.label}'s key is {other.days} days old (more than 31): rotate or "
                                 f"revoke {other.label}", alarm=True)
    return frozenset(st.held)


def drop_box_prev_if_due(ks: Keystore, now: datetime, *, companion_enrolled: bool,
                         outbox_read_through: Optional[datetime] = None) -> Keystore:
    """The first exclusive run after `box_prev.until` drops the outgoing box key once the outbox has been read
    past that time (with no companion enrolled, at `until`). Returns the keystore to save, or `ks` unchanged."""
    bp = ks.box_prev
    if bp is None:
        return ks
    until = _parse(bp.until)
    if _now(now) <= until:
        return ks
    if companion_enrolled and (outbox_read_through is None or _now(outbox_read_through) < until):
        return ks
    return replace(ks, box_prev=None)


# ── Every append is checked first ──

def append_devices_line(dw: LogWriter, line: bytes, *, ledger: Ledger, now: datetime,
                        on_write: Optional[Callable[[], None]] = None) -> DevicesLog:
    """Append to devices.jsonl only if the log replays with the line added. A bad line can't be taken out of
    an append-only log, so it never goes in. `on_write` runs once the check has passed, just before the write
    is attempted."""
    log = _logs.replay_devices(dw.read() + bytes(line) + b"\n", ledger_id=ledger.ledger_id, root=ledger.root,
                               now=now)
    if on_write is not None:
        on_write()
    dw.append(line)
    return log


def append_readers_line(rw: LogWriter, dw: LogWriter, line: bytes, *, ledger: Ledger, now: datetime) -> ReadersLog:
    """Append to readers.jsonl only if it replays with the line added, against the devices bytes the open
    devices writer holds (never an earlier replay)."""
    devices = _logs.replay_devices(dw.read(), ledger_id=ledger.ledger_id, root=ledger.root, now=now)
    log = _logs.replay_readers(rw.read() + bytes(line) + b"\n", devices, ledger_id=ledger.ledger_id, now=now)
    rw.append(line)
    return log


# ── Signing sessions ──

@dataclass(frozen=True)
class Session:
    ks: Keystore = field(repr=False)
    devices: DevicesLog = field(repr=False)
    readers: ReadersLog = field(repr=False)
    state: RoamState
    lock: RoamLock = field(repr=False)
    digest: str


def _read_logs(ledger: Ledger, lock: RoamLock, now: datetime) -> Tuple[DevicesLog, ReadersLog]:
    try:
        dev = ledger.devices_path.read_bytes()
        rd = ledger.readers_path.read_bytes() if ledger.readers_path.exists() else b""
        _logs.split_log(dev)
        _logs.split_log(rd)
    except _logs.TornLine:
        # A torn tail is repaired only under the exclusive lock, by opening the writers (it moves to forks/).
        lock.make_exclusive()
        try:
            with LogWriter(ledger.devices_path, kind=DEVICES_KIND, forks_dir=ledger.forks_dir) as dw, \
                    LogWriter(ledger.readers_path, kind=READERS_KIND, forks_dir=ledger.forks_dir) as rw:
                dev, rd = dw.read(), rw.read()
        except LogError as exc:
            raise RotationAlarm(f"ALARM: {exc}") from None
    except OSError as exc:
        raise RotationAlarm(f"ALARM: can't read the logs: {exc.strerror}") from None
    except LogError as exc:
        raise RotationAlarm(f"ALARM: {exc}") from None
    try:
        devices = _logs.replay_devices(dev, ledger_id=ledger.ledger_id, root=ledger.root, now=now)
        return devices, _logs.replay_readers(rd, devices, ledger_id=ledger.ledger_id, now=now)
    except LogError as exc:
        raise RotationAlarm(f"ALARM: {exc}") from None


def _borrowed(lock: Any, ledger: Ledger) -> RoamLock:
    """A roam.lock the caller already holds (the drill rotating, a rotation publishing): it must be held
    exclusively, on this ledger's keys folder. Whoever borrows it never releases it."""
    if not (lock is not None and getattr(lock, "held", False) and getattr(lock, "exclusive", False)):
        raise RotationError("a lock handed in must be roam.lock held exclusively; nothing was written")
    if Path(lock.keys_dir).resolve() != Path(ledger.keys_dir).resolve():
        raise RotationError("the lock handed in is roam.lock of another keys folder; nothing was written")
    return lock


@contextmanager
def signing_session(ledger: Ledger, sources: Sequence[Any], *, now: datetime, interactive: bool,
                    say: Optional[Callable[[str], None]] = None, exclusive: bool = False,
                    lock: Optional[RoamLock] = None) -> Iterator[Session]:
    """For anything that signs with DK or CK without saving the keystore or appending to a log (publish, the
    drill's checks): roam.lock from before the keystore is loaded until the caller's signatures are bound to
    the tail replayed here, shared by default. `exclusive=True` takes it exclusively (§18a: a publish, which may
    make a checkpoint, and anything that writes `seen`). `lock` is one the caller already holds exclusively
    (the in-run publish gets the rotation's): it's used as it is and never released here, and the caller holds
    no LogWriter. An unattended run (`interactive=False`) gets LockBusy instead of waiting. Refuses with
    Unfinished mid-rotation and with RotationAlarm on any mismatch."""
    with ExitStack() as stack:
        if lock is None:
            lock = stack.enter_context(RoamLock(ledger.keys_dir, exclusive=exclusive, interactive=interactive,
                                                say=say))
        else:
            lock = _borrowed(lock, ledger)
        ks, digest = _keys.load_locked(ledger.keys_dir, sources, lock)
        shared = not lock.exclusive
        devices, readers = _read_logs(ledger, lock, now)
        if shared and lock.exclusive:
            # The lock was released and taken again to repair a torn tail: another run may have saved the
            # keystore in between, so what was loaded under the shared lock is stale.
            ks, digest = _keys.load_locked(ledger.keys_dir, sources, lock)
        try:
            st = _state.load_state(ledger.state_path)
        except _state.StateError as exc:
            raise RotationAlarm(f"ALARM: {exc}") from None
        check_signer(ks, devices, st)
        yield Session(ks=ks, devices=devices, readers=readers, state=st, lock=lock, digest=digest)


# ── Config entries (§17.4) ──

@dataclass(frozen=True)
class ConfigEntries:
    """The relay config edit a rotation prints: the new issuer, the old one's not_after (or its removal) and
    the old CK's outstanding quarantines, applied in one edit over hardware-key SSH or the console."""
    new_ck: Mapping[str, str]            # {id, pub, not_after}
    old_ck: Optional[Mapping[str, str]]  # {id, not_after or "remove"}; None when the record was rebuilt
    quarantine: Tuple[Mapping[str, str], ...]
    suspected: bool

    @classmethod
    def from_record(cls, rec: RotationRecord) -> "ConfigEntries":
        return cls(new_ck=dict(rec.new_ck), old_ck=None if rec.old_ck is None else dict(rec.old_ck),
                   quarantine=tuple(dict(q) for q in rec.quarantine), suspected=rec.suspected)

    def issuers(self) -> dict:
        """The `issuers` entries to set: the new ck- with pub and not_after; the old one's new not_after."""
        out = {self.new_ck["id"]: {"pub": self.new_ck["pub"], "not_after": self.new_ck["not_after"]}}
        if self.old_ck is not None and self.old_ck["not_after"] != REMOVE:
            out[self.old_ck["id"]] = {"not_after": self.old_ck["not_after"]}
        return out

    def removals(self) -> Tuple[str, ...]:
        """The ck- entries to remove now."""
        if self.old_ck is not None and self.old_ck["not_after"] == REMOVE:
            return (self.old_ck["id"],)
        return ()

    def lines(self) -> List[str]:
        new = self.new_ck["id"]
        out = ["relay config edit (issuers and quarantine), applied in one edit:",
               f"  add issuer {new}: " + sig._canonical(self.issuers()[new]).decode()]
        if self.old_ck is None:
            out.append(f"  the old ck- couldn't be probed (its rotation record was lost): keep only {new} under "
                       "issuers and remove every other ck- now")
        elif self.old_ck["not_after"] == REMOVE:
            out.append(f"  remove {self.old_ck['id']} now")
        else:
            out.append(f"  set issuer {self.old_ck['id']} not_after: {self.old_ck['not_after']}")
        for q in self.quarantine:
            out.append("  add quarantine: " + sig._canonical(dict(q)).decode())
        return out


# ── Hooks and the result ──

def _none(*args: Any, **kw: Any) -> None:
    return None


def _empty(*args: Any, **kw: Any) -> Tuple:
    return ()


def _no(*args: Any, **kw: Any) -> bool:
    return False


@dataclass
class Hooks:
    """Fixed points where later steps plug in. The defaults do nothing: no probe, no quarantine entries, no
    delivery confirmed and no config edit confirmed (so the record stays open).

    The 2.6 hooks and the publish get the run's roam.lock, held exclusively, and never release it. `adopt` runs
    in a fresh rotation only, after the replay and before the tap, while the engine's devices and readers
    LogWriters are open, so it reads those logs as plain bytes and never opens a LogWriter (one would wait on
    the engine's flock for ever). Both writers are closed by the time `checkpoint` and `publish` run.
    `checkpoint` runs once the record has closed, except while --now finishes an open rotation. Either 2.6 hook
    may raise an Exception: it's reported (in the result's notes and through `say`) and the rotation carries on,
    or for `checkpoint` stays closed. The engine reloads state.json after each."""
    probe: Callable[..., Optional[str]] = _none            # 2.7: (old ck_seed, old ck id, nbf, exp, rng) -> token
    quarantine: Callable[[str], Sequence[Mapping[str, str]]] = _empty  # 2.7: old ck id -> config entries
    remint: Callable[[Keystore], None] = _none             # 2.7: re-mint capabilities under the new CK
    adopt: Callable[[Keystore, RoamLock], None] = _none    # 2.6: adopt staged checkpoints (the live keys)
    checkpoint: Callable[[Keystore, int, RoamLock], None] = _none  # 2.6: (new keys, rotate_idx, lock)
    deliver: Callable[[str, Identity, str], bool] = _no    # 1b: bundle on the pasteboard; True once confirmed
    show: Callable[..., None] = _none                      # (config, re-approval list, notes)
    confirm_config: Callable[[ConfigEntries], bool] = _no  # Enter once the config edit is applied
    publish: Callable[[RoamLock], None] = _none            # 2.8: publish in the same run once closed
    progress: Callable[[str], None] = _none                # each step boundary, by name


@dataclass(frozen=True)
class RotationResult:
    rotate_idx: int
    at: str
    old_kid: str
    new_kid: str
    suspected: bool
    config: ConfigEntries
    renewed: Tuple[str, ...]
    expired: Tuple[str, ...]      # active when the rotation started, expired before their renewal
    reapproval: Tuple[str, ...]   # cap earlier than at + 38 days: a reader_scope with a tap re-approves
    held: Tuple[str, ...]
    closed: bool
    resumed: Optional[str]        # None, "step 5" or "steps 6 and 7"
    notes: Tuple[str, ...]
    previous: Optional["RotationResult"] = None  # an open rotation --now upgraded and finished first


# ── The transaction ──

class _Engine:
    def __init__(self, ledger: Ledger, *, sources: Sequence[Any], rng: Callable[[int], bytes],
                 clock: Callable[[], datetime], clock_check: Callable[[datetime], None], authenticator: Any,
                 hooks: Optional[Hooks], suspected: bool, sleep: Callable[[float], None],
                 say: Optional[Callable[[str], None]], lock: Optional[RoamLock] = None,
                 exclude_from_backup: Optional[Callable[[Path], None]] = None):
        self.ledger, self.sources, self.rng, self.clock = ledger, list(sources), rng, clock
        self.clock_check, self.authenticator, self.hooks = clock_check, authenticator, hooks or Hooks()
        self.suspected, self.sleep, self.say = bool(suspected), sleep, say or _none
        self.given_lock, self.exclude = lock, exclude_from_backup
        self.lock: Optional[RoamLock] = None
        self.dw: Optional[LogWriter] = None
        self.rw: Optional[LogWriter] = None
        self.ks: Optional[Keystore] = None
        self.digest = ""
        self.devices: Optional[DevicesLog] = None
        self.readers: Optional[ReadersLog] = None
        self.st = RoamState()
        self.owed_intent: Optional[dict] = None  # the marker this --now run keeps on file until its fresh close
        self.run_notes: List[str] = []           # notes for the whole run (one or two rotations)
        self._start()

    def _start(self) -> None:
        """What one rotation (fresh or resumed) tracks for its result and its after-failure test."""
        self.notes: List[str] = []
        self.cur_suspected = self.suspected  # the rotation in hand, for the advice a failure prints
        self.assume_suspected = False        # a landed line without a record finishes as suspected
        self.upgraded = False                # --now turned a plain record suspected
        self.append_attempted = False        # the rotate line's write was attempted in this run
        self.renewing: Optional[Tuple[int, int]] = None  # (renewed, to renew) during step 6
        self.phase = "start"
        self.finishing = False       # --now is finishing an open rotation before its fresh one
        self.defer_publish = False   # no publish while keys --now suspects are live
        self.reached_step_3 = False  # the fresh rotation got as far as saving its pending keys

    # ── plumbing ──
    def _now(self) -> datetime:
        return _now(self.clock())

    def _finish(self) -> str:
        return _finish_advice(self.cur_suspected)

    def _start_again(self) -> str:
        """Nothing landed: start again the way this run was asked. A --now run still owes fresh keys."""
        return OWED if self.suspected else "run irp roam rotate to start again"

    def _writer(self, path: Path, kind: str) -> LogWriter:
        w = LogWriter(path, kind=kind, forks_dir=self.ledger.forks_dir)
        w.__enter__()
        return w

    def _close_writers(self) -> None:
        for w in (self.rw, self.dw):
            if w is not None:
                w.__exit__(None, None, None)

    def _source(self, ks: Keystore) -> Any:
        for s in self.sources:
            if s.name == ks.kek_source:
                return s
        raise RotationError(f"the keystore's master key is kept in {ks.kek_source}, which this run can't reach")

    def _load(self) -> None:
        self.ks, self.digest = _keys.load_locked(self.ledger.keys_dir, self.sources, self.lock)

    def _save(self, ks: Keystore) -> None:
        self.digest = _keys.save_locked(self.ledger.keys_dir, ks, self._source(ks), self.rng, self.lock, self.digest)
        self.ks = ks

    def _write_state(self, **changes: Any) -> None:
        """Every engine write (§18a): reload state.json under the lock and replace only `rotation`, `probes`,
        `held` and `fresh_keys_owed`, the engine's own keys, as this run holds them with `changes` applied (a
        --now run's marker comes from what it keeps in hand, so a file lost mid-run gets it back). `checkpoint`,
        `seen` and `epoch_start` stay as the file has them (a file lost mid-run comes back with them null). The
        first write after local/ went missing makes it again with its Time Machine exclusion, or refuses."""
        own = {"rotation": self.st.rotation, "probes": self.st.probes, "held": self.st.held,
               "fresh_keys_owed": self._owed()}
        own.update(changes)
        self.st = _state.update_state(self.ledger.state_path, self.lock, exclude_from_backup=self.exclude, **own)

    def _owed(self) -> Optional[Mapping[str, Any]]:
        return self.owed_intent if self.owed_intent is not None else self.st.fresh_keys_owed

    def _owe(self) -> None:
        """--now, before anything else: write the fresh-keys-owed marker. `since` stays from an earlier --now
        that still owes them; `from_idx` is the devices log's line count now, so whatever is already in the log
        (an open rotation this run finishes first) never clears it. A write that fails is said and the rotation
        carries on, since it may still end with fresh keys; the marker is kept in hand and written with the
        run's next state write."""
        owed = self.st.fresh_keys_owed
        self.owed_intent = {"since": owed["since"] if owed is not None else _ts(self._now()),
                            "from_idx": len(self.devices.lines)}
        try:
            self._write_state()
        except Exception as exc:
            note = (f"fresh keys owed couldn't be recorded in state.json ({_state._reason(exc)}); the rotation "
                    "carries on, and if it doesn't end with fresh keys, run irp roam rotate --now again before "
                    "anything publishes")
            self.run_notes.append(note)
            self.say(note)

    def _keep_owed(self) -> None:
        """Wherever a --now run stops while fresh keys are still owed: make sure state.json says so (a file lost
        or a write refused meanwhile). Best effort: the failure being reported matters more."""
        if self.owed_intent is None:
            return
        try:
            if _state.load_state(self.ledger.state_path).fresh_keys_owed != self.owed_intent:
                self._write_state()
        except Exception as exc:
            self.say(f"fresh keys owed couldn't be recorded in state.json ({_state._reason(exc)}): run irp roam "
                     "rotate --now before anything publishes")

    def _noted(self, result: RotationResult) -> RotationResult:
        """The run's own notes first, then, when fresh keys are still owed after a rotation that isn't the
        fresh one, the advice to run --now."""
        notes = tuple(self.run_notes) + result.notes
        owed = self._owed()
        if owed is not None and (result.closed or owes_before(owed, result.rotate_idx)) and \
                not any("rotate --now" in n for n in notes):
            notes += (f"{OWED_NOW} (the current keys are still the ones you suspect)",)
        return replace(result, notes=notes)

    def _reload_state(self) -> None:
        """After a hook that may write state.json (adopting or making a checkpoint writes `checkpoint`)."""
        try:
            self.st = _state.load_state(self.ledger.state_path)
        except _state.StateError as exc:
            raise RotationAlarm(f"ALARM: {exc}") from None

    def _adopt(self) -> None:
        """§18a: adopt staged checkpoints before the tap, so nothing staged above the record is normally left
        sealed to the box this rotation retires. A failure is reported and the rotation carries on (only making
        a checkpoint and publishing refuse)."""
        try:
            self.hooks.adopt(self.ks, self.lock)
        except Exception as exc:
            note = (f"adopting staged checkpoints failed, and the rotation carried on: {_state._reason(exc)}; the "
                    "next publish adopts them or refuses")
            self.notes.append(note)
            self.say(note)
        self._reload_state()

    def _checkpoint(self, r: int) -> Optional[str]:
        """§18a: once the record has closed, the new strand's first checkpoint, made under this run's lock. A
        failure is reported, never fatal: the rotation has closed, the config is shown and the bundles delivered,
        and the next publish makes it by the same rules. state.json is reloaded after it returns."""
        note = None
        try:
            self.hooks.checkpoint(self.ks, r, self.lock)
        except Exception as exc:
            note = (f"the new strand's first checkpoint wasn't made: {_state._reason(exc)}; the rotation stands "
                    "(closed), and the next publish makes it")
            self.say(note)
        self._reload_state()
        return note

    def _open(self) -> None:
        """Step 1's reads: both writers (a torn tail moves to forks/), the keystore, both logs and state.json."""
        self.dw = self._writer(self.ledger.devices_path, DEVICES_KIND)
        self.rw = self._writer(self.ledger.readers_path, READERS_KIND)
        self._load()
        now = self._now()
        try:
            self.devices = _logs.replay_devices(self.dw.read(), ledger_id=self.ledger.ledger_id,
                                                root=self.ledger.root, now=now)
            self.readers = _logs.replay_readers(self.rw.read(), self.devices, ledger_id=self.ledger.ledger_id,
                                                now=now)
        except LogError as exc:
            raise RotationAlarm(f"ALARM: {exc}") from None
        try:
            self.st = _state.load_state(self.ledger.state_path)
        except _state.StateError as exc:
            raise RotationAlarm(f"ALARM: {exc}") from None

    def _check_clock(self) -> None:
        now = self._now()
        try:
            self.clock_check(now)
        except Exception as exc:
            raise RotationError(f"the clock check failed: {exc}") from None

    def _take_at(self, after: Optional[datetime], *, strict: bool) -> datetime:
        """The clock, once it's past `after` (or at it, for a readers line). Waits up to 5 minutes, then
        refuses: a line's time is never made up."""
        waited, told = 0.0, False
        while True:
            now = self._now()
            if after is None or now > after or (not strict and now == after):
                return now
            if waited >= CLOCK_WAIT.total_seconds():
                raise RotationError(f"the clock ({_ts(now)}) is still behind the log's last line ({_ts(after)}) "
                                    "after waiting 5 minutes; refusing, since a line's time is never made up")
            if not told:
                self.say(f"waiting for the clock to pass the log's last line ({_ts(after)})")
                told = True
            self.sleep(1)
            waited += 1

    def _last_at(self) -> Optional[datetime]:
        times = [t for t in (self.dw.last_at, self.rw.last_at) if t is not None]
        return max(times) if times else None

    def _progress(self, step: str) -> None:
        self.hooks.progress(step)

    def _attempting(self) -> None:
        self.append_attempted = True

    # ── one run ──
    def run(self, *, resume_only: bool = False) -> Optional[RotationResult]:
        """Plain: finish whatever is open (keeping its recorded flag), or else rotate. --now: finish whatever is
        open (upgraded to suspected first) and, once that record closes, rotate the live keys too, with a new
        tap: --now always ends with fresh keys. Resume-only finishes and stops there."""
        if not (self.ledger.devices_path.exists() or self.ledger.devices_path.is_symlink()):
            raise RotationError(f"no devices log at {self.ledger.devices_path}; run irp roam init first")
        if self.exclude is None and not self.ledger.local_dir.is_dir():
            raise RotationError("state.json's folder (local/) is missing, and it must be excluded from Time Machine "
                                "before anything is written into it, but this run has no backup exclusion; nothing "
                                "was written")
        with ExitStack() as stack:
            if self.given_lock is not None:  # the caller's (the drill): used as it is, never released here
                self.lock = _borrowed(self.given_lock, self.ledger)
            else:
                self.lock = stack.enter_context(RoamLock(self.ledger.keys_dir, exclusive=True, interactive=True,
                                                         say=self.say))
            stack.callback(self._close_writers)
            self._open()
            try:
                return self._run(resume_only)
            except RotationError:
                if self.suspected:
                    self._keep_owed()  # the run stops while fresh keys are owed: state.json says so
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _run(self, resume_only: bool) -> Optional[RotationResult]:
        if self.suspected:
            self._owe()  # before anything else: resuming, finishing or rotating
        previous = None
        if self.ks.pending is not None or rotation_open(self.devices, self.ks.dk_id, self.st) is not None:
            self.finishing = self.defer_publish = self.suspected
            result = self._resume()
            if result is None:  # it never landed; its keys were dropped
                if resume_only:
                    return None
                self._start()
            elif not self.suspected:
                return self._noted(result)
            elif not result.closed or resume_only:
                return self._noted(replace(result, notes=result.notes + (OWED_AFTER_EDIT,)))
            else:
                previous = result
                self._close_writers()
                self._start()
                self._open()
        elif resume_only:
            _check_live(self.ks, self.devices)
            raise RotationError("nothing to resume: no rotation is under way"
                                + ("; fresh keys are now owed: run irp roam rotate --now before anything publishes"
                                   if self.suspected else ""))
        try:
            result = self._fresh()
        except RotationError as exc:
            extra = []
            if previous is not None:
                extra.append(f"the earlier rotation at devices line {previous.rotate_idx} was finished as "
                             "suspected and stands")
            if self.suspected and not self.reached_step_3:
                extra.append(OWED)  # nothing of the fresh rotation was written: the live keys are still owed
            if not extra:
                raise
            cls = RotationAlarm if isinstance(exc, RotationAlarm) else RotationError
            raise cls("; ".join([str(exc)] + extra)) from exc
        result = self._noted(result)
        return replace(result, previous=previous) if previous is not None else result

    def _fresh(self) -> RotationResult:
        ks = self.ks
        self.phase = "fresh"
        self.cur_suspected = self.suspected
        _check_live(ks, self.devices)
        if not self.suspected and self._owed() is not None:
            # A plain rotation keeps the old CK for 7 days, so it never stands in for the --now that's owed.
            raise RotationError(f"{OWED_NOW}: a plain irp roam rotate keeps the old CK for 7 days, so it never "
                                "stands in for --now; this run started no rotation")
        self._adopt()  # §18a: after the replay, before the tap
        self._check_clock()
        at = _ts(self._take_at(self._last_at(), strict=True))
        self._progress("locked")
        # Step 2: the next keys and every signature, in memory.
        new = self._new_keys(at)
        old = self.devices.state().devices[ks.dk_id]
        body = {"v": 1, "kind": DEVICES_KIND, "event": "device_rotate", "ledger_id": self.ledger.ledger_id,
                "root": self.devices.root, "idx": self.dw.next_idx, "prev": self.dw.prev, "at": at,
                "old": ks.dk_id,
                "device": {"kid": new.dk_id, "class": "custodian", "alg": "ed25519",
                           "pub": sig.b64url_encode(sig.public_key(new.dk_seed)), "box": new.dk_recipient,
                           "label": old.label, "key_scope": "device-local", "webauthn": None}}
        data = sig._canonical(body)
        sigs = [sig.sign(DEVICES_KIND, data, ks.dk_seed, ks.dk_id), self._tap(data),
                sig.sign(DEVICES_KIND, data, new.dk_seed, new.dk_id)]
        line = sig._canonical({"body": body, "sigs": sorted(sigs, key=lambda s: s["key_id"])})
        self._progress("signed")
        self.reached_step_3 = True
        try:
            self._save(replace(ks, pending=new))  # step 3
            self._progress("pending saved")
            self._check_clock()  # step 4
            self.devices = append_devices_line(self.dw, line, ledger=self.ledger, now=self._now(),
                                               on_write=self._attempting)
            self._progress("line appended")
            self._finish_keys(body["idx"])  # step 5
        except BaseException as exc:
            self._after_failure(exc)
            raise
        return self._renew_and_deliver(body["idx"], resumed=None)

    def _new_keys(self, at: str) -> Pending:
        seeds = [self.rng(32) for _ in range(3)]
        if any(not isinstance(k, bytes) or len(k) != 32 for k in seeds) or len(set(seeds)) != 3:
            raise RotationError("rng must return fresh 32-byte keys")
        ks = self.ks
        held = {ks.dk_seed, ks.dk_box, ks.ck_seed} | ({ks.box_prev.dk_box} if ks.box_prev else set())
        if held & set(seeds):
            raise RotationError("rng repeated a key the keystore already holds")
        p = Pending(dk_seed=seeds[0], dk_box=seeds[1], ck_seed=seeds[2], started_at=at)
        box = Identity(p.dk_box).recipient().public
        recipients = {Recipient.from_string(r.recipient).public for s in self.readers.states for r in s.values()}
        if not _pending_absent(self.devices, p) or box in recipients:
            raise RotationError("the new keys were seen in a log before; nothing was written, so run irp roam "
                                "rotate again")
        return p

    def _tap(self, data: bytes) -> dict:
        """An approver signature over the rotate line: each active approver in label order until one answers.
        A key that isn't present, doesn't hold its credential or gives an assertion that fails the checks
        moves on to the next; a cancel stops at once."""
        approvers = sorted((d for d in self.devices.state().devices.values() if d.cls == "approver"),
                           key=lambda d: d.label)
        if not approvers:
            raise RotationError("no active approver to tap: enrol a hardware key first; nothing was written")
        tried = []
        laptop = self.devices.state().devices[self.ks.dk_id].label
        for d in approvers:
            self.say(f"touch {d.label} and enter its PIN to approve the rotation of {laptop}")
            try:
                return approver.approve(DEVICES_KIND, data, dict(d.raw), self.authenticator)
            except approver.Cancelled:
                raise RotationError("cancelled at the hardware-key tap; nothing was written") from None
            except approver.NoCredential:
                tried.append(f"{d.label} (not present)")
            except approver.ApproverError as exc:
                tried.append(f"{d.label} ({exc})")
        raise RotationError("no approver answered: " + ", ".join(tried) + "; nothing was written")

    def _finish_keys(self, r: int) -> None:
        """Step 5: the rotation record with the old CK's probe, then the keystore with the new keys live,
        `pending` null and the old box kept decrypt-only in `box_prev`. The old signing seeds go with the old
        keystore.

        `box_prev` keeps one key. A rotation that comes before the previous one's `box_prev.until` replaces it,
        so the earlier outgoing box key is dropped early (before the old CK's not_after that R2 keeps it to);
        the result says so. That single slot is the owner's decision to make, pending."""
        ks, p = self.ks, self.ks.pending
        body = _body(self.devices, r)
        at = body["at"]
        rec = self.st.rotation if self.st.rotation is not None and self.st.rotation.rotate_idx == r else None
        # A rotation from before a --now that still owes fresh keys finishes as suspected in any run, so a plain
        # run never gives the CK that --now suspected the 7-day overlap.
        owed_here = owes_before(self._owed(), r)
        suspected = self.suspected or owed_here or self.assume_suspected or (rec is not None and rec.suspected)
        if (self.suspected or owed_here) and rec is not None and not rec.suspected:
            self.upgraded = True
            why = "--now upgraded" if self.suspected else "fresh keys are owed after --now, so it finishes as " \
                "suspected: this run upgraded"
            self.notes.append(f"{why} the unfinished rotation, so the old CK is removed now and its probe is minted "
                              "again for that time")
        self.cur_suspected = suspected
        _, old_intro = introduced(self.devices, body["old"])
        old_ck = ks.ck_id
        old_not_after = old_ck_not_after(at, old_intro, suspected=suspected)
        quarantine = tuple(_state._quarantine_entry(q) for q in self.hooks.quarantine(old_ck))
        nbf = (_parse(at) if old_not_after == REMOVE else _parse(old_not_after)) + PROBE_DELAY
        # One probe per outgoing CK, never minted twice on a resume. One minted for a different not_after (an
        # upgrade to suspected) is replaced while the old CK is still in hand.
        probes = tuple(pr for pr in self.st.probes if not (pr.iss == old_ck and pr.nbf != _ts(nbf)))
        if not any(pr.iss == old_ck for pr in probes):
            token = self.hooks.probe(ks.ck_seed, old_ck, _ts(nbf), _ts(nbf + PROBE_LIFETIME), self.rng)
            if token is not None:
                probes = probes + (Probe(iss=old_ck, nbf=_ts(nbf), token=token),)
        record = RotationRecord(
            rotate_idx=r, at=at, suspected=suspected,
            new_ck={"id": p.ck_id, "pub": sig.b64url_encode(sig.public_key(p.ck_seed)),
                    "not_after": _ts(_parse(at) + CK_LIFETIME)},
            old_ck={"id": old_ck, "not_after": old_not_after}, quarantine=quarantine, closed=False)
        self._write_state(rotation=record, probes=probes, held=())
        self._progress("record written")
        until = at if old_not_after == REMOVE else old_not_after
        if ks.box_prev is not None and _parse(at) < _parse(ks.box_prev.until):
            self.notes.append(f"box_prev held the previous rotation's outgoing box key until {ks.box_prev.until}; "
                              "this rotation's outgoing box replaced it, so that earlier key was dropped early "
                              "(box_prev keeps one key)")
        self._save(replace(ks, dk_seed=p.dk_seed, dk_box=p.dk_box, ck_seed=p.ck_seed, pending=None,
                           box_prev=BoxPrev(dk_box=ks.dk_box, until=until)))
        self._progress("keys live")

    def _renewed_since(self, r: int) -> set:
        out = set()
        for line in self.readers.lines:
            body, _ = _logs.parse_line(line, READERS_KIND)
            if body["event"] == "reader_renew" and body["devices_at"]["idx"] >= r:
                out.add(body["reader_id"])
        return out

    def _renew_and_deliver(self, r: int, *, resumed: Optional[str]) -> RotationResult:
        """Steps 6 and 7. A failure in either gets the same after-failure test as steps 3 to 5, and says how
        far the rotation got."""
        self.phase = "renewals"
        try:
            return self._steps_6_and_7(r, resumed=resumed)
        except BaseException as exc:
            self._after_failure(exc)
            raise

    def _steps_6_and_7(self, r: int, *, resumed: Optional[str]) -> RotationResult:
        ks = self.ks
        rbody = _body(self.devices, r)
        rot_at = _parse(rbody["at"])
        self.hooks.remint(ks)
        epoch = self.devices.epoch
        renewed_before = self._renewed_since(r)
        held = set(self.st.held)
        identities: List[Tuple[str, Identity, str]] = []  # in memory only
        renewed, expired, reapproval = [], [], []
        current = self.readers.states[-1] if self.readers.states else {}
        todo = []
        for rid in sorted(current):
            reader = self.readers.reader(rid)
            if reader.revoked_idx is not None or reader.epoch != epoch or _parse(reader.expires) <= rot_at:
                if rid in held:  # ended: nothing to deliver any more
                    held.discard(rid)
                    self._write_state(held=tuple(sorted(held)))
                continue
            if rid in renewed_before and rid not in held:
                continue  # renewed and delivered in an earlier run
            todo.append(rid)
        self.renewing = (0, len(todo))
        for rid in todo:
            reader = self.readers.reader(rid)
            at = self._take_at(self._last_at(), strict=False)
            if _parse(reader.expires) <= at:  # expired during the sitting: never renewed
                expired.append(rid)
                if rid in held:
                    held.discard(rid)
                    self._write_state(held=tuple(sorted(held)))
                continue
            cap = _parse(reader.approved_at) + APPROVAL_CAP
            expires = min(at + RENEW_FOR, cap)
            if cap < rot_at + RENEW_FOR:
                reapproval.append(rid)
            ident = generate_identity(self.rng)
            held.add(rid)
            self._write_state(held=tuple(sorted(held)))  # held before the line lands
            self._check_clock()
            body = {"v": 1, "kind": READERS_KIND, "event": "reader_renew", "ledger_id": self.ledger.ledger_id,
                    "root": self.devices.root, "idx": self.rw.next_idx, "prev": self.rw.prev, "at": _ts(at),
                    "devices_at": {"idx": self.dw.next_idx - 1, "line": self.dw.prev}, "reader_id": rid,
                    "recipient": ident.recipient().to_string(), "expires": _ts(expires)}
            line = sig._canonical({"body": body, "sigs": [sig.sign(READERS_KIND, sig._canonical(body), ks.dk_seed,
                                                                   ks.dk_id)]})
            self.readers = append_readers_line(self.rw, self.dw, line, ledger=self.ledger, now=self._now())
            identities.append((rid, ident, _ts(expires)))
            renewed.append(rid)
            self.renewing = (len(renewed), len(todo))
            self._progress(f"renewed {rid}")
        self._progress("renewals done")
        self._close_writers()  # both writers stay open through step 6 only
        # Step 7.
        self.phase = "step 7"
        rec = self.st.rotation
        config = ConfigEntries.from_record(rec)
        notes = list(self.notes)
        if rec.old_ck is None:
            notes.append("the old CK couldn't be probed: state.json was lost, so the rotation record was rebuilt as "
                         "suspected")
        if rec.suspected:
            notes.append("suspected copy: the old CK is removed at once, so every reader bundle and the phone stop "
                         "working at once; swap the bundles in this sitting and re-pair the phone")
        elif rec.old_ck is not None and rec.old_ck["not_after"] == REMOVE:
            notes.append("the old CK is already past its own not_after: remove it now")
        for rid in expired:
            notes.append(f"{rid} expired during the sitting and wasn't renewed; enrol it again")
        for rid in reapproval:
            notes.append(f"{rid} reaches 90 days from its last approval before the new CK's not_after: re-approve "
                         "it (a reader_scope repeating its scopes, with a dry run and a tap) while it's active")
        self.hooks.show(config, tuple(reapproval), tuple(notes))
        for rid, ident, exp in identities:
            if self.hooks.deliver(rid, ident, exp):
                held.discard(rid)
                self._write_state(held=tuple(sorted(held)))
                self._progress(f"delivered {rid}")
        identities.clear()
        for rid in sorted(held):
            notes.append(f"{rid} is held (skipped, STALE) until its new bundle is confirmed delivered: run irp roam "
                         "rotate or irp roam reader renew")
        closed = False
        if self.hooks.confirm_config(config):
            done = replace(self.st.rotation, closed=True)
            # The fresh rotation's close clears the marker in this same write, before the checkpoint hook runs;
            # closing an older rotation (the --now finish phase among them) never does.
            clear = not self.finishing and clears_owed(self._owed(), done)
            self._write_state(rotation=done, **({"fresh_keys_owed": None} if clear else {}))
            if clear:
                self.owed_intent = None
            closed = True
            self.phase = "closed"
            self._progress("closed")
            if not self.finishing:  # --now finishing an open rotation: the suspected keys sign no new checkpoint
                note = self._checkpoint(r)
                if note:
                    notes.append(note)
            if not self.defer_publish:  # a fresh suspected rotation follows: publish once, after it
                self.hooks.publish(self.lock)
        return RotationResult(rotate_idx=r, at=rbody["at"], old_kid=rbody["old"], new_kid=rbody["device"]["kid"],
                              suspected=rec.suspected, config=config, renewed=tuple(renewed), expired=tuple(expired),
                              reapproval=tuple(reapproval), held=tuple(sorted(held)), closed=closed,
                              resumed=resumed, notes=tuple(notes))

    # ── resume and abort ──
    def _resume(self) -> Optional[RotationResult]:
        """Decide from the keystore and the log (both writers open, so a torn tail is already in forks/)."""
        ks = self.ks
        if ks.pending is not None:
            verdict, hit = pending_verdict(ks, self.devices)
            if verdict == "landed":
                rec = self.st.rotation
                if rec is None or rec.rotate_idx != hit:
                    # The line landed but no record says how it was meant: fail toward no overlap.
                    self.assume_suspected = True
                    self.notes.append("the rotation line landed but its record was never written, so it's finished "
                                      "as suspected (no overlap): the old CK is removed now")
                self.say("the rotation line landed: finishing from step 5")
                self.phase = "step 5"
                try:
                    self._finish_keys(hit)
                except BaseException as exc:
                    self._after_failure(exc)
                    raise
                return self._renew_and_deliver(hit, resumed="step 5")
            if verdict == "absent":
                _check_live(ks, self.devices)
                try:
                    self.dw.sync()
                except OSError as exc:
                    raise RotationError(f"the devices log couldn't be synced ({exc.strerror}), so whether the "
                                        "earlier rotation line is gone for good isn't certain: its next keys are "
                                        "kept; run irp roam rotate again") from None
                self._save(replace(ks, pending=None))
                self.say("the earlier rotation never landed, so its keys were dropped: starting again with a new tap "
                         "and fresh keys")
                return None
            raise RotationAlarm("ALARM: the keystore and the devices log disagree: the keystore's pending keys match "
                                "no rotation line, or a line names them, or its live key isn't active; publishing "
                                "stops")
        r = rotation_open(self.devices, ks.dk_id, self.st)
        _check_live(ks, self.devices)
        rec = self.st.rotation
        if rec is None or rec.rotate_idx != r:
            # state.json was lost: rebuild the record as suspected, and hold every reader renewed since r, since
            # nobody can say whose delivery was confirmed. Both are written before anything else happens.
            body = _body(self.devices, r)
            rec = RotationRecord(rotate_idx=r, at=body["at"], suspected=True,
                                 new_ck={"id": ks.ck_id, "pub": sig.b64url_encode(sig.public_key(ks.ck_seed)),
                                         "not_after": _ts(_parse(body["at"]) + CK_LIFETIME)},
                                 old_ck=None, quarantine=(), closed=False)
            held = tuple(sorted(set(self.st.held) | self._renewed_since(r)))
            self._write_state(rotation=rec, held=held)
        elif rec.new_ck["id"] != ks.ck_id:
            raise RotationAlarm("ALARM: state.json's rotation record names a different new CK from the keystore's")
        elif (self.suspected or owes_before(self._owed(), r)) and not rec.suspected:
            # --now on an open plain rotation, or any run on one from before a --now that still owes fresh keys:
            # upgrade it before step 7 prints anything. The old CK's probe keeps the nbf it was signed with, since
            # the old CK is gone and can't sign another.
            old = None if rec.old_ck is None else {"id": rec.old_ck["id"], "not_after": REMOVE}
            self._write_state(rotation=replace(rec, suspected=True, old_ck=old))
            self.upgraded = True
            why = "--now upgraded" if self.suspected else "fresh keys are owed after --now, so it finishes as " \
                "suspected: this run upgraded"
            self.notes.append(f"{why} the open rotation, so the old CK is removed now (its probe keeps the nbf it "
                              "was signed with)")
        self.cur_suspected = self.st.rotation.suspected
        return self._renew_and_deliver(r, resumed="steps 6 and 7")

    def _after_failure(self, exc: BaseException) -> None:
        """After step 3, on any failure or interrupt: reopen both logs (a torn tail moves to forks/ and counts as
        not landed), reload the keystore and decide from them, while roam.lock is still held. A LogError from an
        append is never proof that nothing landed, since the rollback itself can fail."""
        try:
            self._close_writers()
            self.dw = self._writer(self.ledger.devices_path, DEVICES_KIND)
            self.rw = self._writer(self.ledger.readers_path, READERS_KIND)
            self._load()
            self.devices = _logs.replay_devices(self.dw.read(), ledger_id=self.ledger.ledger_id,
                                                root=self.ledger.root, now=self._now())
            outcome = self._settle()
        except RotationAlarm:
            raise
        except BaseException as inner:
            raise RotationAlarm(f"ALARM: {exc!r} stopped the rotation and the check after it failed too ({inner!r}); "
                                "run irp roam rotate to finish or clean up") from exc
        if self.finishing and self.suspected:
            outcome = f"{outcome}; {OWED_AFTER_EDIT}"
        self.say(outcome)
        if isinstance(exc, Exception):
            cls = RotationAlarm if isinstance(exc, RotationAlarm) else RotationError
            raise cls(f"{exc}; {outcome}") from exc
        # KeyboardInterrupt and the like: the caller re-raises it unchanged.

    def _settle(self) -> str:
        ks = self.ks
        st = _state.load_state(self.ledger.state_path)
        if ks.pending is None:
            if rotation_open(self.devices, ks.dk_id, st) is not None:
                if self.renewing is not None:
                    n, m = self.renewing
                    return f"the rotation is done; {n} of {m} readers renewed; finish with {self._finish()}"
                return f"the rotation is done (its new keys are live); finish the reader renewals with {self._finish()}"
            if self.phase == "closed":
                return "the rotation is finished and closed; what failed came after it"
            return f"nothing landed: {self._start_again()}"
        verdict, hit = pending_verdict(ks, self.devices)
        if verdict == "landed":
            # The flag the resume will apply: the record's, or suspected when there's none, or when fresh keys
            # are owed (the marker this run holds counts, even if state.json doesn't have it yet).
            return ("the rotation line landed, so its next keys are kept: finish with "
                    + _finish_advice(_suspected_at(st, hit) or owes_before(self._owed(), hit)))
        if verdict == "absent":
            if self.append_attempted:
                # The rollback that took the line out may not have reached the disk: only a later run, after a
                # fresh open, decides.
                if self.suspected:  # it may not have landed: the live keys may still be the suspected ones
                    run = ("run irp roam rotate --now, which decides from the log after a fresh open (the current "
                           "keys may still be the ones you suspect)")
                else:
                    run = "finish with irp roam rotate, which decides from the log after a fresh open"
                return ("the rotation line isn't in the log, but its append was attempted and the rollback may not "
                        f"have reached the disk, so its next keys are kept: {run}")
            try:
                self.dw.sync()
            except OSError as exc:
                return (f"the rotation line isn't in the log, but the log couldn't be synced ({exc.strerror}), so its "
                        "next keys are kept: the next run decides from the log after a fresh open")
            self._save(replace(ks, pending=None))
            return f"the rotation line didn't land, so its next keys were dropped: {self._start_again()}"
        raise RotationAlarm("ALARM: after a failed rotation the keystore and the devices log disagree; publishing "
                            "stops")


def _gate(interactive: bool, clock_check: Optional[Callable[[datetime], None]]) -> None:
    if not interactive:
        raise RotationError("rotation runs only in an interactive irp roam rotate (it needs a hardware-key tap); "
                            "an unattended run never rotates or resumes")
    if clock_check is None:
        raise RotationError("no clock check: rotation refuses to run without one (the relay client supplies it)")


def rotate(ledger: Ledger, *, sources: Sequence[Any], rng: Callable[[int], bytes], clock: Callable[[], datetime],
           clock_check: Optional[Callable[[datetime], None]], authenticator: Any, hooks: Optional[Hooks] = None,
           suspected: bool = False, interactive: bool = True, sleep: Callable[[float], None] = time.sleep,
           say: Optional[Callable[[str], None]] = None, lock: Optional[RoamLock] = None,
           exclude_from_backup: Optional[Callable[[Path], None]] = None) -> RotationResult:
    """`irp roam rotate` (`suspected=True` for `--now`). Plain: finishes an unfinished rotation (keeping its
    recorded flag), or else rotates; if the earlier one never landed, its keys are dropped and this run starts
    again with a new tap and fresh keys. `--now` always ends with fresh keys: an unfinished rotation is
    upgraded to suspected and finished first, and once it's closed a fresh suspected rotation follows (the
    result's `previous` is the one finished first); publishing waits for the fresh one to close.

    `lock` is a roam.lock the caller already holds exclusively (the drill): the rotation runs under it and never
    releases it. The caller holds no LogWriter, and reloads the keystore and state.json after this returns.
    `exclude_from_backup` (`state.tmutil_exclude` in production) is how a missing local/ folder is excluded from
    Time Machine before state.json is written into it; without one, a rotation that would have to create it
    refuses before anything is written."""
    _gate(interactive, clock_check)
    if lock is not None:
        _borrowed(lock, ledger)
    eng = _Engine(ledger, sources=sources, rng=rng, clock=clock, clock_check=clock_check,
                  authenticator=authenticator, hooks=hooks, suspected=suspected, sleep=sleep, say=say, lock=lock,
                  exclude_from_backup=exclude_from_backup)
    return eng.run()


def resume_rotation(ledger: Ledger, *, sources: Sequence[Any], rng: Callable[[int], bytes],
                    clock: Callable[[], datetime], clock_check: Optional[Callable[[datetime], None]],
                    authenticator: Any, hooks: Optional[Hooks] = None, suspected: bool = False,
                    interactive: bool = True, sleep: Callable[[float], None] = time.sleep,
                    say: Optional[Callable[[str], None]] = None, lock: Optional[RoamLock] = None,
                    exclude_from_backup: Optional[Callable[[Path], None]] = None) -> Optional[RotationResult]:
    """Finish an unfinished rotation and nothing more: from step 5 when the line landed, steps 6 and 7 when
    the new keys are live. With `suspected=True` it's upgraded to suspected first, but no fresh rotation
    follows here: the result says one is still owed, and no publish runs. Returns None when the earlier
    rotation never landed and its keys were dropped (the person starts again). Interactive only; refuses when
    nothing is unfinished, after the live check (a keystore the log has retired is ALARM). With
    `suspected=True` the fresh-keys-owed marker is written first, so that refusal also says fresh keys are now
    owed (run irp roam rotate --now before anything publishes): the marker stays, since --now declared the
    suspicion. `lock` and `exclude_from_backup` as for `rotate`."""
    _gate(interactive, clock_check)
    if lock is not None:
        _borrowed(lock, ledger)
    eng = _Engine(ledger, sources=sources, rng=rng, clock=clock, clock_check=clock_check,
                  authenticator=authenticator, hooks=hooks, suspected=suspected, sleep=sleep, say=say, lock=lock,
                  exclude_from_backup=exclude_from_backup)
    return eng.run(resume_only=True)
