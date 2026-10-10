"""Roaming IRP, Cut 1 step 2.6, part 1: checkpoint formats and verification (spec v0.3 §18, §18a).

A checkpoint is the custodian's signed record of what the ledger and the three roam logs held when it was made:
a reader-visible header (`irp/checkpoint.json`) signed by the strand's device key, a custodian-only body
(`irp/body.json`) with the whole snapshot file, the four logs' lengths, digests and segments and the
recipients, an optional RFC 3161 token, and a MAC from the paper key. Everything the custodian keeps is sealed
in a Padmé-padded custodian container to the epoch's RK plus every active custodian and companion box, at a slot
named by HMAC(K_c[e], "irp-roam/v1/slot/custodian/<seq>"). These tests hold `irp/roam/checkpoint.py` to §18a:
the closed header and body, the slot rule and the MAC (golden vectors a stock interpreter reproduces), which
bytes are covered, the recipients, the segments, the custodian container, and `verify_checkpoint` and
`verify_chain` with one tamper case per custodian-side check. Devices logs come from tests/roam_logkit.py and
tokens from tests/roam_faketsa.py. All names and values are neutral test values.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("cryptography")
pytest.importorskip("asn1crypto")
pytest.importorskip("rfc8785")

import roam_faketsa as fk  # noqa: E402
from roam_faketsa import ORG, TokenOptions  # noqa: E402
from roam_logkit import LEDGER, NOW, DevicesKit, ReadersKit, h, ts  # noqa: E402

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import age, container, keys, rekadu, sig, tsa  # noqa: E402
from irp.roam import checkpoint as C  # noqa: E402
from irp.roam import state as S  # noqa: E402
from irp.roam.age import Identity  # noqa: E402
from irp.roam.keys import RoamLock  # noqa: E402
from irp.roam.logs import replay_devices  # noqa: E402
from irp.roam.tsa import TsaPin  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "roam"
VECTORS = json.loads((FIXTURES / "checkpoint_vectors.json").read_text())
RK = h("checkpoint tests/rk")
RK_ID = Identity(RK)
EK = {e: keys.epoch_keys(RK, e) for e in range(3)}
ENTRIES = [{"id": f"IRP-2001-01-01-{i:03d}", "type": "decision", "title": f"neutral title {i}"} for i in range(1, 4)]
HEADER_KEYS = {"v", "kind", "ledger_id", "root", "epoch", "strand", "seq", "prev", "created_at", "devices",
               "body_digest"}
BODY_KEYS = {"v", "kind", "snapshot", "logs", "recipients", "policy_digest", "tsa_policy_digest"}
READER = "rd-" + "1" * 32


def jsonl(entries) -> bytes:
    return b"".join(canonicalize(e) + b"\n" for e in entries)


def digest(data: bytes) -> str:
    return "sha256-" + hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def stdlib_hkdf(ikm: bytes, label: str) -> bytes:
    prk = hmac.new(b"irp-roam/v1", ikm, hashlib.sha256).digest()
    return hmac.new(prk, ("irp-roam/v1/" + label).encode() + b"\x01", hashlib.sha256).digest()


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return fk.PKI(tmp_path_factory.mktemp("pki"))


def pins(pki):
    return (TsaPin("tsa-a", pki.tsa_ca.pin, ORG),)


def present(pki, gen: datetime):
    """A TSA step that answers PRESENT with a token over the header, as tsa.stamp would."""
    def stamp(header: bytes):
        token = fk.build_token(hashlib.sha256(header).digest(), pki, TokenOptions(gen_time=gen))
        return tsa.StampResult(tsa.PRESENT, token, "tsa-a", gen, fk.POLICY)
    return stamp


class World:
    """A devices log with one custodian laptop and an approver (the state after init), the bytes a checkpoint
    covers, and the record a maker keeps. `make` does what make_checkpoint's step 5 and step 7 do in memory."""

    def __init__(self, label: str = "ck"):
        self.kit = DevicesKit(label)
        self.kit.genesis()
        self.strand = self.kit.custodian("laptop-1", box_label=f"{label}/laptop-1")
        self.ids = {self.strand: Identity(h(f"box/{label}/laptop-1"))}
        self.ak = self.kit.approver("hwkey-1")
        self.ledger = jsonl(ENTRIES)
        self.readers = ReadersKit(self.kit)
        self.disclosures = b""
        self.record = None
        self.made = []

    @property
    def root(self) -> str:
        return self.kit.root.kid

    def replay(self, data=None):
        return replay_devices(self.kit.data if data is None else data, ledger_id=LEDGER, root=self.root, now=NOW,
                              prefix=True)

    def rotate(self) -> str:
        label = self.kit.descs[self.strand]["label"]
        n = len(self.kit.lines)
        new = self.kit.rotate(self.strand, self.ak)
        self.ids[new] = Identity(h(f"box/{self.kit.label}/{label}/rot/{n}"))
        self.strand = new
        return new

    def companion(self, label: str = "phone-1") -> str:
        n = len(self.kit.lines)
        kid = self.kit.companion(label, via=[self.strand, self.ak])
        self.ids[kid] = Identity(h(f"box/{self.kit.label}/{label}/{n}"))
        return kid

    def recover(self, ref) -> str:
        n = len(self.kit.lines)
        active = self.replay().state().devices
        keep = [self.ak]
        new = self.kit.recovery(keep, sorted(k for k in active if k not in keep), new_label="laptop-9",
                                checkpoint_ref=ref)
        self.ids[new] = Identity(h(f"box/{self.kit.label}/laptop-9/recovery/{n}"))
        self.strand = new
        return new

    def covered(self, devices=None, ledger=None) -> C.Covered:
        return C.covered_from(ledger=self.ledger if ledger is None else ledger,
                              devices=self.kit.data if devices is None else devices,
                              readers=self.readers.data, disclosures=self.disclosures)

    def created_at(self, minutes: int = 1) -> str:
        return ts(self.kit.clock.t + timedelta(minutes=minutes))

    def make(self, *, devices=None, seq=None, prev="auto", created_at=None, stamp=None, policy=None,
             full_base=None, keep=True, ledger=None, rng=os.urandom, strand=None) -> C.Made:
        data = self.kit.data if devices is None else devices
        dev = self.replay(data)
        epoch = dev.epoch
        strand = strand or self.strand
        rec = self.record
        same = rec is not None and rec["epoch"] == epoch
        if seq is None:
            seq = rec["seq"] + 1 if same else 1
        if prev == "auto":
            prev = rec["digest"] if same and seq > 1 else C.seq1_prev(dev, epoch)
        previous = rec if (same or (rec is not None and rec["digest"] == C.seq1_prev(dev, epoch))) else None
        if full_base is None:
            full_base = rec is None or rec["strand"] != strand or rec["epoch"] != epoch
        made = C.build_checkpoint(
            ledger_id=LEDGER, covered=self.covered(devices=data, ledger=ledger), devices=dev,
            signer_seed=self.kit.keys[strand].seed, strand=strand, epoch_keys=EK[epoch], seq=seq, prev=prev,
            created_at=created_at or self.created_at(),
            previous_logs=None if previous is None else previous["logs"],
            previous_recipients=None if previous is None else previous["recipients"],
            previous_snapshot_digest=rec["snapshot_digest"] if same and seq > 1 else None,
            full_base=full_base, policy=policy, stamp=stamp, rng=rng)
        if keep:
            self.record = made.record(self.record)
            self.made.append(made)
        return made

    def verify(self, made, *, custodian=True, now=NOW, devices=None, pins=(), slot="auto", shipped=None, **kw):
        if shipped is None:
            shipped = C.open_custodian(made.container, [RK_ID]) if custodian else \
                C.Shipped(header=made.header, sig=made.sig, tsr=made.tsr)
        return C.verify_checkpoint(shipped, ledger_id=LEDGER, root=self.root,
                                   devices=self.kit.data if devices is None else devices, now=now, pins=pins,
                                   slot=(made.epoch, made.seq) if slot == "auto" else slot,
                                   epochs=EK if custodian else None, **kw)

    def verified(self, *made, custodian=True, pins=()):
        return [self.verify(m, custodian=custodian, pins=pins) for m in made]

    def chain(self, verified, *, custodian=True, now=NOW, **kw):
        return C.verify_chain(verified, ledger_id=LEDGER, root=self.root, devices=self.kit.data, now=now,
                              readers=self.readers.data if custodian else None, custodian=custodian, **kw)


def reforge(w: World, made, *, body=None, header=None, members=None, stanzas=None, fix_digest=True,
            signer=None):
    """Rebuild a custodian checkpoint around changed parts: re-signed with the strand's key and re-MACed, so only
    the check under test can fail. `body`, `header` and `members` mutate in place."""
    orig = C.open_custodian(made.container, [RK_ID])
    b = json.loads(made.body)
    if body:
        body(b)
    body_bytes = canonicalize(b)
    hd = json.loads(made.header)
    if fix_digest:
        hd["body_digest"] = digest(body_bytes)
    if header:
        header(hd)
    header_bytes = canonicalize(hd)
    kid = signer or hd["strand"]
    m = {"irp/checkpoint.json": header_bytes,
         "irp/checkpoint.sig": C.sign_header(header_bytes, w.kit.keys[kid].seed, kid),
         "irp/checkpoint.mac": C.mac_line(EK[hd["epoch"]].ka, header_bytes, body_bytes),
         "irp/body.json": body_bytes, "irp/devices.jsonl": orig.devices}
    if orig.tsr is not None:
        m["irp/checkpoint.tsr"] = orig.tsr
    if orig.policy is not None:
        m["irp/policy.json"] = orig.policy
    if members:
        members(m)
    return C.Shipped.from_members(m, stanzas=orig.stanzas if stanzas is None else stanzas)


def resnapshot(b: dict, change) -> None:
    """Change the snapshot manifest and recompute its digest, so the snapshot stays internally consistent."""
    change(b["snapshot"]["manifest"])
    b["snapshot"]["snapshot_digest"]["value"] = hashlib.sha256(canonicalize(b["snapshot"]["manifest"])).hexdigest()


# ── Golden vectors (§18.1, §18a) ──

def test_mac_golden_vector_matches_a_stdlib_one_liner():
    rk, m = bytes.fromhex(VECTORS["rk_hex"]), VECTORS["mac"]
    ka = stdlib_hkdf(rk, "custodian-mac/0")
    assert ka.hex() == m["ka_hex"]
    assert keys.custodian_mac_key(rk, 0) == ka == keys.epoch_keys(rk, 0).ka
    header, body = m["header"].encode(), m["body"].encode()
    one_liner = hmac.new(ka, b"irp-roam/v1/custodian-mac\n" + hashlib.sha256(header).hexdigest().encode() + b"\n"
                         + hashlib.sha256(body).hexdigest().encode() + b"\n", hashlib.sha256).hexdigest() + "\n"
    assert one_liner == m["line"]
    assert C.mac_line(ka, header, body) == m["line"].encode()
    C.check_mac(m["line"].encode(), ka, header, body)
    with pytest.raises(C.CheckpointError, match="MAC"):
        C.check_mac(m["line"].encode(), ka, header, body + b" ")


def test_custodian_slot_golden_vector_matches_a_stdlib_one_liner():
    rk, s = bytes.fromhex(VECTORS["rk_hex"]), VECTORS["slot"]
    kc = stdlib_hkdf(rk, "custodian-chain/0")
    assert kc.hex() == s["kc_hex"]
    assert keys.custodian_chain_key(rk, 0) == kc == keys.epoch_keys(rk, 0).kc
    assert set(s["names"]) == {"1", "2", "42"}
    for seq, name in s["names"].items():
        assert "m/" + hmac.new(kc, b"irp-roam/v1/slot/custodian/" + seq.encode(), hashlib.sha256).hexdigest() == name
        assert C.custodian_slot(kc, int(seq)) == name
        assert C.slot_name(kc, "custodian/" + seq) == name


STOCK_CHECK = r"""
import hashlib, hmac, json, sys
v = json.load(open(sys.argv[1]))
rk = bytes.fromhex(v["rk_hex"])
def hk(label):
    prk = hmac.new(b"irp-roam/v1", rk, hashlib.sha256).digest()
    return hmac.new(prk, ("irp-roam/v1/" + label).encode() + b"\x01", hashlib.sha256).digest()
m = v["mac"]
ka, kc = hk("custodian-mac/0"), hk("custodian-chain/0")
line = hmac.new(ka, b"irp-roam/v1/custodian-mac\n" + hashlib.sha256(m["header"].encode()).hexdigest().encode()
                + b"\n" + hashlib.sha256(m["body"].encode()).hexdigest().encode() + b"\n",
                hashlib.sha256).hexdigest() + "\n"
assert line == m["line"], "mac"
for seq, name in v["slot"]["names"].items():
    assert "m/" + hmac.new(kc, ("irp-roam/v1/slot/custodian/" + seq).encode(), hashlib.sha256).hexdigest() == name
assert "irp" not in sys.modules
print("ok")
"""


def test_both_golden_vectors_check_with_stock_python_from_the_paper_key_alone():
    """§18.1: the MAC is checkable by stock python from the paper key alone (and so is the slot name)."""
    out = subprocess.run([sys.executable, "-I", "-c", STOCK_CHECK, str(FIXTURES / "checkpoint_vectors.json")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# ── Names and the one slot rule ──

def test_strand8_ids_and_the_manifest_projection():
    w = World()
    made = w.make()
    s8 = w.strand[3:11]
    assert C.strand8(w.strand) == s8
    assert C.snapshot_id(w.strand, 7) == f"IRPC-{s8}-7"
    assert C.checkpoint_id(w.strand, 7) == f"ckpt-{s8}-7"
    hd = json.loads(made.header)
    proj = C.manifest_projection(made.header)
    assert proj == {"id": f"ckpt-{s8}-1", "strand": w.strand, "seq": 1, "hash": digest(made.header),
                    "signed_ts": hd["created_at"]}
    rekadu._check_checkpoint(proj)  # the Slice manifest's checkpoint field accepts it
    for bad in ("ak-" + "1" * 32, "dk-123", None):
        with pytest.raises(C.CheckpointError):
            C.strand8(bad)


def test_one_slot_rule_for_every_feed():
    k = h("slot key")

    def want(label):
        return "m/" + hmac.new(k, ("irp-roam/v1/slot/" + label).encode(), hashlib.sha256).hexdigest()

    assert C.custodian_slot(k, 3) == want("custodian/3")
    assert C.reader_slot(k, "s1", 3) == want("reader/s1/3")
    assert C.phone_slot(k, 3) == want("phone/3")
    assert C.outbox_slot(k, 3) == want("outbox/3")
    for bad in ("custodian/0", "custodian/01", "custodian/-1", "reader/S1/1", "reader/s1", "feed/1", "custodian/1/",
                "phone/1 ", "outbox/" + "9" * 17, ""):
        with pytest.raises(C.CheckpointError):
            C.slot_name(k, bad)
    for bad_seq in (0, -1, True, 1.0, 2**53, "1"):
        with pytest.raises(C.CheckpointError):
            C.custodian_slot(k, bad_seq)
    with pytest.raises(C.CheckpointError):
        C.custodian_slot(b"short", 1)
    with pytest.raises(C.CheckpointError) as exc:
        C.reader_slot(k, "BAD", 1)
    assert k.hex() not in str(exc.value)


# ── Header (§18a "Header") ──

def fields(**over):
    base = {"ledger_id": LEDGER, "root": "rt-" + "1e" * 16, "epoch": 0, "strand": "dk-" + "2d" * 16, "seq": 1,
            "prev": None, "created_at": "2026-10-10T12:00:00Z", "devices": b"x\n", "body": b"{}"}
    base.update(over)
    return base


def test_header_is_exact_jcs_with_a_closed_schema():
    data = C.build_header(**fields())
    hd = C.parse_header(data)
    assert set(hd) == HEADER_KEYS
    assert hd["v"] == 1 and hd["kind"] == "checkpoint"
    assert hd["devices"] == {"byte_length": 2, "digest": digest(b"x\n")}
    assert hd["body_digest"] == digest(b"{}")
    assert data == canonicalize(hd)
    with pytest.raises(C.CheckpointError):
        C.parse_header(json.dumps(hd).encode())  # spaces: not JCS
    with pytest.raises(C.CheckpointError):
        C.parse_header(data[:-1] + b',"extra":1}')
    mutations = [
        lambda d: d.update(extra=1), lambda d: d.pop("body_digest"), lambda d: d.update(kind="checkpoints"),
        lambda d: d.update(v=2), lambda d: d.update(v=True), lambda d: d.update(seq=0), lambda d: d.update(epoch=-1),
        lambda d: d.update(created_at="2026-02-30T00:00:00Z"), lambda d: d.update(created_at="2026-10-10 12:00:00"),
        lambda d: d.update(strand="ak-" + "2d" * 16), lambda d: d.update(devices={"byte_length": 2}),
        lambda d: d.update(prev="sha256-XYZ"), lambda d: d.update(ledger_id="ILID-x"),
        lambda d: d.update(root="rt-" + "1E" * 16), lambda d: d.update(body_digest="4b" * 32),
        lambda d: d["devices"].update(byte_length=True), lambda d: d["devices"].update(byte_length=0),
    ]
    for mutate in mutations:
        d = json.loads(data)
        mutate(d)
        with pytest.raises(C.CheckpointError):
            C.parse_header(canonicalize(d))


def test_prev_is_null_exactly_at_seq_1_of_epoch_0_or_after_a_null_checkpoint_ref():
    C.build_header(**fields(seq=2, prev=digest(b"a")))
    with pytest.raises(C.CheckpointError, match="prev"):
        C.build_header(**fields(seq=2, prev=None))
    with pytest.raises(C.CheckpointError, match="prev"):
        C.build_header(**fields(seq=1, epoch=0, prev=digest(b"a")))
    C.build_header(**fields(seq=1, epoch=1, prev=digest(b"a")))  # the checkpoint_ref of the opening line
    C.build_header(**fields(seq=1, epoch=1, prev=None))           # or null, when that line's ref is null


# ── Body (§18a "Body") ──

def test_body_holds_the_whole_snapshot_file_with_created_at_and_a_bare_hex_previous_digest():
    w = World()
    m1, m2 = w.make(), w.make()
    b1, b2 = C.parse_body(m1.body), C.parse_body(m2.body)
    assert set(b1) == BODY_KEYS and b1["kind"] == "checkpoint-body" and b1["v"] == 1
    snap = b2["snapshot"]
    assert set(snap) == {"snapshot_digest", "manifest"}
    man = snap["manifest"]
    assert man["snapshot_id"] == f"IRPC-{w.strand[3:11]}-2"
    assert man["created_at"] == json.loads(m2.header)["created_at"]
    assert man["previous_snapshot_digest"] == b1["snapshot"]["snapshot_digest"]["value"]
    assert re.fullmatch(r"[0-9a-f]{64}", man["previous_snapshot_digest"])
    assert b1["snapshot"]["manifest"]["previous_snapshot_digest"] is None
    assert snap["snapshot_digest"]["value"] == hashlib.sha256(canonicalize(man)).hexdigest()
    assert man["ledger"]["byte_digest"]["value"] == hashlib.sha256(w.ledger).hexdigest()
    assert man["ledger"]["entry_count"] == 3 and man["ledger_id"] == LEDGER
    assert set(b2["logs"]) == {"ledger", "devices", "readers", "disclosures"}
    assert b2["logs"]["ledger"]["byte_digest"] == digest(w.ledger)
    assert b2["policy_digest"] is None and b2["tsa_policy_digest"] is None
    # Reader-visible bytes carry no ledger length or entry count: only the devices prefix.
    hd = json.loads(m2.header)
    assert set(hd) == HEADER_KEYS and hd["devices"]["byte_length"] == len(w.kit.data)
    assert b"entry_count" not in m2.header and b"ledger\"" not in m2.header


def test_the_snapshot_is_build_snapshot_file_unchanged(monkeypatch):
    import irp.integrity.manifest as M

    monkeypatch.setattr(M.secrets, "token_hex", lambda n: "ab" * n)
    raw, strand = jsonl(ENTRIES), "dk-" + "2d" * 16
    snap = C.build_snapshot(ledger_id=LEDGER, strand=strand, seq=3, raw=raw, entries=ENTRIES,
                            previous_snapshot_digest="cd" * 32, created_at="2026-10-10T12:00:00Z")
    assert snap == M.build_snapshot_file(snapshot_id="IRPC-2d2d2d2d-3", ledger_id=LEDGER, raw_bytes=raw,
                                         entries=ENTRIES, previous_snapshot_digest="cd" * 32,
                                         created_at="2026-10-10T12:00:00Z")
    kw = dict(ledger_id=LEDGER, strand=strand, raw=raw, entries=ENTRIES, created_at="2026-10-10T12:00:00Z")
    with pytest.raises(C.CheckpointError):
        C.build_snapshot(seq=1, previous_snapshot_digest="cd" * 32, **kw)
    with pytest.raises(C.CheckpointError):
        C.build_snapshot(seq=2, previous_snapshot_digest=None, **kw)
    with pytest.raises(C.CheckpointError):
        C.build_snapshot(seq=2, previous_snapshot_digest="sha256-" + "cd" * 32, **kw)  # bare hex only


def test_body_is_exact_jcs_with_a_closed_schema():
    w = World()
    m = w.make()
    good = json.loads(m.body)
    C.parse_body(canonicalize(good))
    mutations = [
        lambda b: b.update(extra=1), lambda b: b.pop("tsa_policy_digest"), lambda b: b.update(v=2),
        lambda b: b["logs"].pop("disclosures"), lambda b: b["logs"].update(other=b["logs"]["ledger"]),
        lambda b: b["logs"]["ledger"].pop("append_only"), lambda b: b["logs"]["ledger"].update(append_only=1),
        lambda b: b["logs"]["devices"]["segments"][0].update(sha256="sha256-" + "0" * 64),
        lambda b: b["logs"]["devices"]["segments"][0].update(object="o/" + "A" * 64),
        lambda b: b["logs"]["devices"]["segments"][0].update(extra=1),
        lambda b: b["recipients"].reverse(), lambda b: b["recipients"].pop(),
        lambda b: b["recipients"].append(dict(b["recipients"][0])),
        lambda b: b["recipients"][0].update(recipient=b["recipients"][0]["recipient"].upper()),
        lambda b: b.update(policy_digest="nope"), lambda b: b["snapshot"].update(extra=1),
        lambda b: b["snapshot"]["snapshot_digest"].update(value="0" * 64),
        lambda b: resnapshot(b, lambda man: man.update(extra=1)),
        lambda b: resnapshot(b, lambda man: man.update(schema="irp-integrity-snapshot/9")),
        lambda b: resnapshot(b, lambda man: man.update(scope={"type": "partial"})),
    ]
    for mutate in mutations:
        b = json.loads(m.body)
        mutate(b)
        with pytest.raises(C.CheckpointError):
            C.parse_body(canonicalize(b))
    with pytest.raises(C.CheckpointError):
        C.parse_body(m.body.replace(b'"v":1', b'"v":1.0'))


# ── Which bytes a checkpoint covers (§18a "Which bytes a checkpoint covers") ──

@pytest.fixture
def lock(tmp_path):
    with RoamLock(tmp_path / "keys", exclusive=True, interactive=True) as held:
        yield held


def lay_out(tmp_path: Path, *, ledger, devices, readers=None, disclosures=None) -> dict:
    d = tmp_path / "ledgers" / LEDGER
    d.mkdir(parents=True, mode=0o700)
    paths = {"ledger_file": tmp_path / "work" / "ledger.jsonl", "devices_path": d / "devices.jsonl",
             "readers_path": d / "readers.jsonl", "disclosures_path": C.disclosures_path(d),
             "forks_dir": d / "forks"}
    paths["ledger_file"].parent.mkdir()
    paths["ledger_file"].write_bytes(ledger)
    paths["devices_path"].write_bytes(devices)
    if readers is not None:
        paths["readers_path"].write_bytes(readers)
    if disclosures is not None:
        paths["disclosures_path"].write_bytes(disclosures)
    for p in (paths["devices_path"], paths["readers_path"], paths["disclosures_path"]):
        if p.exists():
            os.chmod(p, 0o600)
    return paths


def test_a_half_written_ledger_line_is_covered_up_to_the_previous_line_and_left_unchanged(tmp_path, lock):
    w = World()
    partial = b'{"id":"IRP-2001-01-01-004","type":"deci'
    paths = lay_out(tmp_path, ledger=w.ledger + partial, devices=w.kit.data)
    before = paths["ledger_file"].read_bytes()
    st = os.stat(paths["ledger_file"])
    cov = C.read_covered(**paths, lock=lock)
    assert cov.ledger == w.ledger and cov.ledger_left == len(partial) and len(cov.entries) == 3
    assert cov.devices == w.kit.data and cov.readers == b"" and cov.disclosures == b"" and cov.torn is None
    assert paths["ledger_file"].read_bytes() == before
    after = os.stat(paths["ledger_file"])
    assert (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    made = w.make(ledger=cov.ledger)
    assert C.parse_body(made.body)["logs"]["ledger"]["byte_length"] == len(w.ledger)


def test_ledger_parse_errors_or_duplicate_ids_refuse_on_every_path(tmp_path, lock):
    one = b'{"id":"IRP-2001-01-01-001"}\n'
    bad = [one + b"{not json}\n", one + one, b"[1]\n", b"\xff\xfe\n", b'{"id":"a","id":"b"}\n']
    for i, ledger in enumerate(bad):
        with pytest.raises(C.CheckpointError):
            C.covered_from(ledger=ledger, devices=World().kit.data, readers=b"", disclosures=b"")
        paths = lay_out(tmp_path / str(i), ledger=ledger, devices=World().kit.data)
        with pytest.raises(C.CheckpointError):
            C.read_covered(**paths, lock=lock)
    with pytest.raises(C.CheckpointError, match="ledger"):
        paths = lay_out(tmp_path / "missing", ledger=one, devices=World().kit.data)
        paths["ledger_file"].unlink()
        C.read_covered(**paths, lock=lock)
    with pytest.raises(C.CheckpointError, match="whole lines"):
        C.covered_from(ledger=one[:-1], devices=World().kit.data, readers=b"", disclosures=b"")


def test_a_torn_disclosures_tail_moves_to_forks_before_it_is_covered(tmp_path, lock):
    w = World()
    line, frag = b'{"kind":"disclosure","v":1}\n', b'{"kind":"disclo'
    paths = lay_out(tmp_path, ledger=w.ledger, devices=w.kit.data, disclosures=line + frag)
    cov = C.read_covered(**paths, lock=lock)
    assert cov.disclosures == line and paths["disclosures_path"].read_bytes() == line
    torn = sorted(paths["forks_dir"].glob("disclosures.jsonl.*.torn"))
    assert len(torn) == 1 and torn[0].read_bytes() == frag and cov.torn == torn[0]
    assert stat.S_IMODE(os.stat(torn[0]).st_mode) == 0o600
    assert C.read_covered(**paths, lock=lock).torn is None
    with open(paths["disclosures_path"], "ab") as fh:
        fh.write(frag)
    C.read_covered(**paths, lock=lock)
    assert len(list(paths["forks_dir"].glob("disclosures.jsonl.*.torn"))) == 2  # never replaces an earlier one


def test_devices_and_readers_are_covered_through_their_last_newline(tmp_path, lock):
    w = World()
    w.readers.enrol(READER, [w.strand, w.ak])
    torn = b'{"body":{"v":1'
    paths = lay_out(tmp_path, ledger=w.ledger, devices=w.kit.data + torn, readers=w.readers.data + torn)
    cov = C.read_covered(**paths, lock=lock)
    assert cov.devices == w.kit.data and cov.readers == w.readers.data
    # Their writers repair torn tails; covering never touches them.
    assert paths["devices_path"].read_bytes() == w.kit.data + torn
    assert paths["readers_path"].read_bytes() == w.readers.data + torn
    paths["readers_path"].unlink()
    assert C.read_covered(**paths, lock=lock).readers == b""
    paths["devices_path"].unlink()
    with pytest.raises(C.CheckpointError, match="devices"):
        C.read_covered(**paths, lock=lock)


def test_reading_covered_bytes_needs_roam_lock_held_exclusively(tmp_path):
    w = World()
    paths = lay_out(tmp_path, ledger=w.ledger, devices=w.kit.data)
    with RoamLock(tmp_path / "keys", exclusive=False, interactive=True) as shared:
        with pytest.raises(C.CheckpointError, match="exclusive"):
            C.read_covered(**paths, lock=shared)
    with pytest.raises(C.CheckpointError, match="exclusive"):
        C.read_covered(**paths, lock=None)


def test_a_symlinked_roam_log_is_refused(tmp_path, lock):
    w = World()
    paths = lay_out(tmp_path, ledger=w.ledger, devices=w.kit.data)
    real = tmp_path / "elsewhere.jsonl"
    real.write_bytes(w.kit.data)
    paths["devices_path"].unlink()
    paths["devices_path"].symlink_to(real)
    with pytest.raises(C.CheckpointError, match="symlink"):
        C.read_covered(**paths, lock=lock)


def test_latest_line_at_reads_the_lines_past_last_present():
    w = World()
    w.readers.enrol(READER, [w.strand, w.ak])
    w.kit.approver("hwkey-2")
    newest = w.kit.clock.t
    assert C.latest_line_at(w.kit.data, w.readers.data, None) == newest
    lp = {"epoch": 0, "gen_time": ts(newest), "devices_length": len(w.kit.data),
          "readers_length": len(w.readers.data)}
    assert C.latest_line_at(w.kit.data, w.readers.data, lp) is None
    lp["devices_length"] = len(w.kit.data) - len(w.kit.lines[-1]) - 1
    assert C.latest_line_at(w.kit.data, w.readers.data, lp) == newest
    lp["devices_length"] = len(w.kit.data) + 1
    with pytest.raises(C.CheckpointRollback):
        C.latest_line_at(w.kit.data, w.readers.data, lp)


# ── Recipients (§18a "Body") ──

def test_recipients_are_rk_plus_active_custodian_and_companion_boxes_sorted_by_id():
    w = World()
    phone = w.companion()

    def rec(kid):
        return {"id": kid, "recipient": w.ids[kid].recipient().to_string()}

    got = C.recipients_for(EK[0], w.replay().state())
    rk = {"id": "rk", "recipient": RK_ID.recipient().to_string()}
    assert got == sorted([rk, rec(w.strand), rec(phone)], key=lambda r: r["id"])
    assert got[-1] == rk and all(r["id"] != w.ak for r in got)  # approvers never decrypt
    assert EK[0].rk_recipient == rk["recipient"]
    w.kit.revoke(phone, [w.strand])
    old = w.strand
    w.rotate()
    got = C.recipients_for(EK[0], w.replay().state())
    assert got == [rec(w.strand), rk]  # dk- sorts before rk
    assert old not in [r["id"] for r in got] and phone not in [r["id"] for r in got]


def test_a_revoked_companion_is_in_neither_recipients_nor_any_new_stanza():
    w = World()
    phone = w.companion()
    m1 = w.make()
    assert phone in [r["id"] for r in C.parse_body(m1.body)["recipients"]]
    assert age.decrypt(m1.container, [w.ids[phone]])  # active: the phone opens it
    w.kit.revoke(phone, [w.strand])
    m2 = w.make()
    b2 = C.parse_body(m2.body)
    assert [r["id"] for r in b2["recipients"]] == sorted([w.strand, "rk"])
    assert m2.base and m2.objects and set(m2.listed) == {oid for oid, _ in m2.objects}
    assert age.stanza_count(m2.container) == 2
    with pytest.raises(age.NoMatchError):
        age.decrypt(m2.container, [w.ids[phone]])
    for _, ct in m2.objects:
        assert age.stanza_count(ct) == 2
        with pytest.raises(age.NoMatchError):
            age.decrypt(ct, [w.ids[phone]])
        assert age.decrypt(ct, [w.ids[w.strand]])
    w.verify(m2)


# ── Segments (§18a "Segments") ──

def restore_all(made, covered: C.Covered, objects: dict, ident=RK_ID) -> None:
    body = C.parse_body(made.body)
    for name, data in covered.logs.items():
        assert C.restore_log(body["logs"][name], objects.__getitem__, [ident]) == data


def test_segments_carry_over_append_and_restore():
    w = World()
    objects = {}
    m1 = w.make()
    objects.update(m1.objects)
    b1 = C.parse_body(m1.body)
    assert m1.base and all(not e["append_only"] for e in b1["logs"].values())
    assert sorted(a.log for a in m1.alerts) == sorted(C.LOG_NAMES)  # no previous entry: loud, every log
    assert all(a.reason == "no_previous" and a.log in str(a) for a in m1.alerts)
    assert b1["logs"]["readers"] == {"byte_length": 0, "byte_digest": digest(b""), "append_only": False,
                                     "segments": []}
    restore_all(m1, w.covered(), objects)
    m2 = w.make()
    b2 = C.parse_body(m2.body)
    assert not m2.base and m2.objects == () and m2.alerts == ()
    assert all(b2["logs"][n]["append_only"] and b2["logs"][n]["segments"] == b1["logs"][n]["segments"]
               for n in C.LOG_NAMES)
    old = len(w.ledger)
    w.ledger += jsonl([{"id": "IRP-2001-01-01-004", "type": "decision", "title": "neutral title 4"}])
    m3 = w.make()
    objects.update(m3.objects)
    b3 = C.parse_body(m3.body)
    segs = b3["logs"]["ledger"]["segments"]
    assert segs[:-1] == b2["logs"]["ledger"]["segments"] and len(m3.objects) == 1
    assert segs[-1]["object"] == m3.objects[0][0] and segs[-1]["offset"] == old
    assert segs[-1]["length"] == len(w.ledger) - old
    assert segs[-1]["sha256"] == hashlib.sha256(w.ledger[old:]).hexdigest()
    assert m3.listed == C.listed_objects(b3["logs"])
    restore_all(m3, w.covered(), objects)


def test_a_pure_append_adds_one_delta_segment_per_new_mib():
    w = World()
    mib = C.SEGMENT_SIZE
    assert mib == 1 << 20
    row = b"x" * 1023 + b"\n"
    w.disclosures = row * (2 * 1024 + 512)  # 2.5 MiB
    objects = {}
    m1 = w.make()
    objects.update(m1.objects)
    segs = C.parse_body(m1.body)["logs"]["disclosures"]["segments"]
    assert [(s["offset"], s["length"]) for s in segs] == [(0, mib), (mib, mib), (2 * mib, mib // 2)]
    w.disclosures += row * (1024 + 512)    # 1.5 MiB more
    m2 = w.make()
    objects.update(m2.objects)
    segs2 = C.parse_body(m2.body)["logs"]["disclosures"]["segments"]
    assert segs2[:3] == segs
    half = mib // 2
    assert [(s["offset"], s["length"]) for s in segs2[3:]] == [(2 * mib + half, mib), (3 * mib + half, half)]
    for oid, ct in m2.objects:
        assert oid == "o/" + hashlib.sha256(ct).hexdigest() and age.stanza_count(ct) == 2
        assert list(container.unpack("segment", age.decrypt(ct, [RK_ID]))) == ["seg"]  # Padmé-padded, one member
    restore_all(m2, w.covered(), objects)


def synthetic(lines):
    data = b"".join(lines)
    segs, off = [], 0
    for i, line in enumerate(lines):
        segs.append({"object": "o/" + hashlib.sha256(b"obj%d" % i).hexdigest(), "offset": off, "length": len(line),
                     "sha256": hashlib.sha256(line).hexdigest()})
        off += len(line)
    return data, {"byte_length": len(data), "byte_digest": digest(data), "segments": segs}


def test_a_list_past_64_segments_rebases_all_four_logs():
    lines = [b"%02d\n" % i for i in range(66)]
    empty = {"byte_length": 0, "byte_digest": digest(b""), "segments": []}
    for n_prev, base in ((63, False), (64, True)):
        _, prev = synthetic(lines[:n_prev])
        covered = {"ledger": b"", "devices": b"", "readers": b"", "disclosures": b"".join(lines[:n_prev + 1])}
        previous = {"ledger": empty, "devices": empty, "readers": empty, "disclosures": prev}
        plans, alerts, rebased = C.plan_logs(covered, previous)
        assert rebased is base and not alerts
        assert all(p.append_only for p in plans.values())  # still from the prefix check
        dis = plans["disclosures"]
        if base:
            assert all(p.base and p.kept == () for p in plans.values())
            assert dis.new == ((0, len(covered["disclosures"])),)
        else:
            assert dis.count == 64 and len(dis.kept) == 63 and dis.new == ((prev["byte_length"], 3),)


def test_full_base_keeps_append_only_from_the_prefix_check():
    data = b"a\nb\n"
    prev = {"byte_length": 2, "byte_digest": digest(b"a\n"), "segments": [
        {"object": "o/" + "1" * 64, "offset": 0, "length": 2, "sha256": hashlib.sha256(b"a\n").hexdigest()}]}
    empty = {"byte_length": 0, "byte_digest": digest(b""), "segments": []}
    covered = {"ledger": data, "devices": b"", "readers": b"", "disclosures": b""}
    plans, alerts, base = C.plan_logs(covered, {"ledger": prev, "devices": empty, "readers": empty,
                                                "disclosures": empty}, full_base=True)
    assert base and not alerts and plans["ledger"].append_only and plans["ledger"].new == ((0, 4),)
    with pytest.raises(C.CheckpointError):
        C.plan_logs(covered, {"ledger": prev})  # all four previous entries or none


def test_a_rewritten_ledger_byte_gives_append_only_false_and_a_loud_alert_naming_the_ledger():
    w = World()
    m1 = w.make()
    b1 = C.parse_body(m1.body)
    w.ledger = w.ledger.replace(b"neutral title 2", b"neutral title 7")
    w.ledger += jsonl([{"id": "IRP-2001-01-01-004", "type": "decision", "title": "neutral title 4"}])
    m2 = w.make()
    b2 = C.parse_body(m2.body)
    assert b2["logs"]["ledger"]["append_only"] is False
    assert [(a.log, a.reason) for a in m2.alerts] == [("ledger", "rewritten")] and "ledger" in str(m2.alerts[0])
    assert str(m2.alerts[0]).startswith("ALERT")
    ledger_ids = {s["object"] for s in b2["logs"]["ledger"]["segments"]}
    assert b2["logs"]["ledger"]["segments"][0]["offset"] == 0
    assert not ledger_ids & {s["object"] for s in b1["logs"]["ledger"]["segments"]}  # a full base of that log
    for name in ("devices", "readers", "disclosures"):
        assert b2["logs"][name]["append_only"] and b2["logs"][name]["segments"] == b1["logs"][name]["segments"]


def test_a_ledger_rewrite_followed_by_a_rotation_still_reports_append_only_false():
    w = World()
    w.make()
    w.ledger = w.ledger.replace(b"neutral title 1", b"neutral title 8")
    w.rotate()
    m2 = w.make()  # the new strand's first checkpoint: a full base of all four logs
    b2 = C.parse_body(m2.body)
    assert m2.base and [(a.log, a.reason) for a in m2.alerts] == [("ledger", "rewritten")]
    assert b2["logs"]["ledger"]["append_only"] is False
    assert b2["logs"]["devices"]["append_only"] and b2["logs"]["readers"]["append_only"]
    assert all(e["segments"] == [] or e["segments"][0]["offset"] == 0 for e in b2["logs"].values())
    assert {s["object"] for e in b2["logs"].values() for s in e["segments"]} == {oid for oid, _ in m2.objects}
    with pytest.raises(age.NoMatchError):  # the retired box isn't an audience
        age.decrypt(m2.container, [w.ids[w.made[0].strand]])


# ── Building a checkpoint (§18a "Making a checkpoint", step 7 in memory) ──

def test_a_made_checkpoint_opens_with_rk_and_the_laptop_box_and_carries_every_member():
    w = World()
    m = w.make()
    for ident in (RK_ID, w.ids[w.strand]):
        sh = C.open_custodian(m.container, [ident])
        assert (sh.header, sh.sig, sh.mac, sh.body, sh.devices) == (m.header, m.sig, m.mac, m.body, w.kit.data)
        assert sh.tsr is None and sh.policy is None and sh.stanzas == 2
    members = container.unpack("custodian", age.decrypt(m.container, [RK_ID]))
    assert list(members) == ["irp/checkpoint.json", "irp/checkpoint.sig", "irp/checkpoint.mac", "irp/body.json",
                             "irp/devices.jsonl"]
    assert m.slot == C.custodian_slot(EK[0].kc, 1) and (m.epoch, m.seq, m.strand) == (0, 1, w.strand)
    assert m.digest == digest(m.header) and m.mac == C.mac_line(EK[0].ka, m.header, m.body)
    assert re.fullmatch(rb"[0-9a-f]{64}\n", m.mac)
    s = sig.parse_sig(m.sig)
    assert s["key_id"] == w.strand
    sig.verify("checkpoint", m.header, s, w.kit.keys[w.strand].pub)
    assert m.label == "NONE" and m.tsr is None and m.gen_time is None
    assert m.listed == C.listed_objects(C.parse_body(m.body)["logs"]) == tuple(oid for oid, _ in m.objects)
    text = repr(m)
    assert m.mac.decode().strip() not in text and b64(m.header) not in text


def test_policy_member_and_digest_travel_together():
    w = World()
    policy = canonicalize({"v": 1, "public_safe_tag": "public-safe"})
    m = w.make(policy=policy)
    assert C.parse_body(m.body)["policy_digest"] == digest(policy)
    assert C.open_custodian(m.container, [RK_ID]).policy == policy
    w.verify(m)


def test_c1_a_second_active_custodian_refuses_before_anything_is_signed():
    w = World()
    w.kit.custodian("laptop-2")
    calls = []

    def stamp(header):
        calls.append(header)
        raise AssertionError("never reached")

    def rng(n):
        calls.append(n)
        return os.urandom(n)

    with pytest.raises(C.CheckpointAlarm, match="revoke laptop-2"):
        w.make(stamp=stamp, rng=rng)
    assert calls == [] and w.record is None


def test_build_refuses_a_signer_that_isnt_the_strand_or_isnt_an_active_custodian():
    w = World()
    with pytest.raises(C.CheckpointError):
        C.build_checkpoint(ledger_id=LEDGER, covered=w.covered(), devices=w.replay(), signer_seed=h("other"),
                           strand=w.strand, epoch_keys=EK[0], seq=1, prev=None, created_at=w.created_at())
    with pytest.raises(C.CheckpointAlarm):  # an approver isn't a custodian
        C.build_checkpoint(ledger_id=LEDGER, covered=w.covered(), devices=w.replay(), signer_seed=h("other"),
                           strand=w.ak, epoch_keys=EK[0], seq=1, prev=None, created_at=w.created_at())
    old = w.strand
    w.rotate()
    with pytest.raises(C.CheckpointAlarm):
        w.make(strand=old)  # retired by the rotate line: it never signs again


def test_build_refuses_a_bad_prev_or_created_at():
    w = World()
    with pytest.raises(C.CheckpointError, match="prev"):
        w.make(prev=digest(b"x"), keep=False)
    with pytest.raises(C.CheckpointError, match="prev"):
        w.make(seq=2, prev=None, keep=False)
    with pytest.raises(C.CheckpointError, match="created_at"):
        w.make(created_at=ts(w.kit.clock.t - timedelta(seconds=1)), keep=False)
    w.readers.enrol(READER, [w.strand, w.ak])
    with pytest.raises(C.CheckpointError, match="created_at"):
        w.make(created_at=ts(w.kit.clock.t - timedelta(seconds=1)), keep=False)
    w.make(created_at=ts(w.kit.clock.t))


def test_the_record_is_what_state_json_keeps(tmp_path):
    w = World()
    m = w.make()
    rec = m.record(None)
    S.check_checkpoint(rec)
    assert (rec["epoch"], rec["seq"], rec["strand"], rec["digest"]) == (0, 1, w.strand, m.digest)
    assert sig.b64url_decode(rec["header"]) == m.header and sig.b64url_decode(rec["sig"]) == m.sig
    assert rec["label"] == "NONE" and rec["gen_time"] is None and rec["last_present"] is None
    body = C.parse_body(m.body)
    assert rec["snapshot_digest"] == body["snapshot"]["snapshot_digest"]["value"]
    assert rec["recipients"] == body["recipients"]
    assert rec["logs"] == {n: {k: v for k, v in e.items() if k != "append_only"} for n, e in body["logs"].items()}
    with RoamLock(tmp_path / "keys", exclusive=True, interactive=True) as held:
        st = S.update_state(tmp_path / "local" / "state.json", held, exclude_from_backup=lambda p: None,
                            checkpoint=rec)
    assert st.checkpoint == rec


def test_a_present_checkpoint_is_its_own_last_present_and_later_ones_carry_it(pki):
    w = World()
    gen = w.kit.clock.t + timedelta(minutes=2)
    m1 = w.make(stamp=present(pki, gen), created_at=ts(gen - timedelta(minutes=1)))
    assert m1.label == "PRESENT" and m1.gen_time == gen and m1.tsr and m1.tsa == "tsa-a"
    assert C.open_custodian(m1.container, [RK_ID]).tsr == m1.tsr
    lp = {"epoch": 0, "gen_time": ts(gen), "devices_length": len(w.kit.data), "readers_length": 0}
    assert w.record["last_present"] == lp and w.record["gen_time"] == ts(gen) and w.record["label"] == "PRESENT"
    w.make()
    assert w.record["label"] == "NONE" and w.record["last_present"] == lp and w.record["gen_time"] is None
    token = m1.tsr
    w.make(stamp=lambda header: tsa.StampResult(tsa.UNVERIFIED, token, "tsa-b", gen))
    assert w.record["label"] == "UNVERIFIED" and w.record["gen_time"] is None and w.record["last_present"] == lp
    with pytest.raises(C.CheckpointError):
        w.make(stamp=lambda header: tsa.StampResult(tsa.NONE, token), keep=False)
    with pytest.raises(C.CheckpointError):
        w.make(stamp=lambda header: tsa.StampResult(tsa.PRESENT, None), keep=False)


# ── Verifying one checkpoint (§18a "Verifying one checkpoint") ──

def test_a_made_checkpoint_verifies_on_the_custodian_side_and_the_reader_side(pki):
    w = World()
    gen = w.kit.clock.t + timedelta(minutes=2)
    m = w.make(stamp=present(pki, gen), created_at=ts(gen - timedelta(minutes=1)))
    v = w.verify(m, pins=pins(pki), cited=m.digest)
    assert (v.epoch, v.seq, v.strand, v.digest, v.label, v.gen_time) == (0, 1, w.strand, m.digest, "PRESENT", gen)
    assert v.custodian and v.body["kind"] == "checkpoint-body" and not v.created_at_skew
    assert v.prefix_idx == len(w.kit.lines) - 1 and v.cited_devices == w.kit.data
    assert v.record(None) == m.record(None)
    seen = v.seen()
    S.check_seen(seen)
    assert sig.b64url_decode(seen["devices"]) == w.kit.data and seen["digest"] == m.digest
    r = w.verify(m, custodian=False, pins=pins(pki))
    assert r.label == "PRESENT" and r.body is None and not r.custodian
    assert w.verify(m).label == "UNVERIFIED"  # no pins: check 6 fails
    m2 = w.make()
    assert w.verify(m2).label == "NONE" and w.verify(m2).gen_time is None


def test_a_slot_replay_is_alarm_on_both_sides():
    w = World()
    m1 = w.make()
    w.make()
    for custodian in (True, False):
        with pytest.raises(C.CheckpointAlarm, match="slot replay"):
            w.verify(m1, custodian=custodian, slot=(0, 2))
        with pytest.raises(C.CheckpointAlarm, match="slot replay"):
            w.verify(m1, custodian=custodian, slot=(1, 1))
    w.verify(m1, slot=None)


def test_the_digest_must_be_the_one_cited_and_the_ledger_the_pinned_one():
    w = World()
    m = w.make()
    w.verify(m, cited=m.digest)
    with pytest.raises(C.CheckpointAlarm, match="cited"):
        w.verify(m, cited=digest(b"another header"))
    with pytest.raises(C.CheckpointError, match="cited"):
        w.verify(m, custodian=False, cited=digest(b"another header"))
    with pytest.raises(C.CheckpointError, match="ledger_id"):
        C.verify_checkpoint(C.Shipped(header=m.header, sig=m.sig), ledger_id="ILID-" + "b2" * 16, root=w.root,
                            devices=w.kit.data, now=NOW)


def test_the_signature_must_name_the_strand_and_verify():
    w = World()
    m = w.make()
    other = sig.encode_sig(sig.sign("checkpoint", m.header, h("x"), sig.key_id("dk", sig.public_key(h("x")))))
    with pytest.raises(C.CheckpointError, match="strand"):
        w.verify(m, custodian=False, shipped=C.Shipped(header=m.header, sig=other))
    s = json.loads(m.sig)
    raw = bytearray(sig.b64url_decode(s["sig"]))
    raw[0] ^= 1
    s["sig"] = b64(bytes(raw))
    with pytest.raises(C.CheckpointError, match="signature"):
        w.verify(m, custodian=False, shipped=C.Shipped(header=m.header, sig=canonicalize(s)))
    with pytest.raises(C.CheckpointError, match="signature"):
        w.verify(m, custodian=False, shipped=C.Shipped(header=m.header, sig=b"{}"))
    with pytest.raises(C.CheckpointError):  # a signature over another kind of object
        w.verify(m, custodian=False, shipped=C.Shipped(
            header=m.header, sig=sig.encode_sig(sig.sign("content", m.header, w.kit.keys[w.strand].seed, w.strand))))


def test_the_signer_must_be_an_active_custodian_at_the_cited_prefix():
    w = World()
    m1 = w.make()
    before = w.kit.data
    new = w.rotate()
    hd = json.loads(m1.header)
    hd.update(strand=new, seq=2, prev=m1.digest)  # the new key, citing a prefix from before its rotate line
    header = canonicalize(hd)
    forged = C.Shipped(header=header, sig=C.sign_header(header, w.kit.keys[new].seed, new))
    assert json.loads(header)["devices"]["byte_length"] == len(before)
    with pytest.raises(C.CheckpointError, match="active custodian"):
        w.verify(m1, custodian=False, shipped=forged, slot=None)
    hd2 = dict(hd, strand=w.ak)
    header2 = canonicalize(hd2)
    with pytest.raises(C.CheckpointError):
        w.verify(m1, custodian=False, shipped=C.Shipped(header=header2, sig=m1.sig), slot=None)
    # A companion's key is a dk- too, but only a custodian signs checkpoints.
    phone = w.companion()
    hd3 = json.loads(m1.header)
    hd3.update(strand=phone, seq=2, prev=m1.digest, devices={"byte_length": len(w.kit.data),
                                                             "digest": digest(w.kit.data)})
    header3 = canonicalize(hd3)
    packed = canonicalize({"alg": "webauthn-es256", "key_id": phone, "sig": b64(b"not checked this far")})
    with pytest.raises(C.CheckpointError, match="active custodian"):
        w.verify(m1, custodian=False, shipped=C.Shipped(header=header3, sig=packed), slot=None)


def test_a_signer_revoked_later_fails_but_one_retired_later_still_verifies():
    w = World()
    m1 = w.make()
    w.rotate()
    m2 = w.make()
    w.verify(m1)  # its key was retired by a rotate line after its prefix: fine
    w.verify(m1, custodian=False)
    w.kit.revoke(m2.strand, [w.root])
    with pytest.raises(C.CheckpointAlarm, match="revoked"):
        w.verify(m2)
    w.kit.revoke(m1.strand, [w.root])  # a retired kid can still be revoked
    with pytest.raises(C.CheckpointError, match="revoked"):
        w.verify(m1, custodian=False)


def test_old_epoch_checkpoints_are_checked_against_the_prefix_before_the_line_that_closed_it():
    w = World()
    m1, m2 = w.make(), w.make()
    w.recover(ref=m1.digest)  # revokes laptop-1 at the recovery line and continues from m1
    m3 = w.make()
    assert (m3.epoch, m3.seq) == (1, 1) and json.loads(m3.header)["prev"] == m1.digest
    assert m3.slot == C.custodian_slot(EK[1].kc, 1)
    dev = w.replay()
    assert C.horizon(dev, 0) == len(w.kit.lines) - 2 and C.horizon(dev, 1) == len(w.kit.lines) - 1
    idx, body = C.opening_line(dev, 1)
    assert idx == len(w.kit.lines) - 1 and body["event"] == "recovery" and body["checkpoint_ref"] == m1.digest
    assert C.opening_line(dev, 0) is None and C.seq1_prev(dev, 0) is None and C.seq1_prev(dev, 1) == m1.digest
    for m in (m1, m2, m3):
        w.verify(m)
        w.verify(m, custodian=False)
    with pytest.raises(C.CheckpointError):
        C.horizon(dev, 2)
    forged = reforge(w, m3, header=lambda hd: hd.update(prev=m2.digest))
    with pytest.raises(C.CheckpointAlarm, match="checkpoint_ref"):
        w.verify(m3, shipped=forged)
    forged = reforge(w, m3, header=lambda hd: hd.update(prev=None))
    with pytest.raises(C.CheckpointAlarm, match="checkpoint_ref"):
        w.verify(m3, shipped=forged)
    forged = reforge(w, m3, header=lambda hd: hd.update(epoch=0, prev=None))  # the epoch at the prefix is 1
    with pytest.raises(C.CheckpointAlarm, match="epoch"):
        w.verify(m3, shipped=forged, slot=(0, 1))


def test_a_created_at_ten_minutes_in_the_verifiers_future_fails():
    w = World()
    m = w.make(created_at=ts(NOW + timedelta(minutes=10)))
    with pytest.raises(C.CheckpointAlarm, match="created_at"):
        w.verify(m)
    with pytest.raises(C.CheckpointError, match="created_at"):
        w.verify(m, custodian=False)
    w.verify(m, now=NOW + timedelta(minutes=6))  # 4 minutes ahead: within the 5-minute allowance
    w.verify(m, now=NOW + timedelta(minutes=5))  # exactly 5 minutes ahead: still within it


def test_the_devices_log_must_extend_the_cited_prefix():
    w = World()
    m = w.make()
    with pytest.raises(C.CheckpointRollback):
        w.verify(m, devices=b"".join(line + b"\n" for line in w.kit.lines[:-1]))
    fork = DevicesKit("ck")  # the same genesis and laptop, then another approver at the same idx
    fork.genesis()
    fork.custodian("laptop-1", box_label="ck/laptop-1")
    fork.approver("hwkey-9")
    assert fork.lines[:2] == w.kit.lines[:2] and fork.lines[2] != w.kit.lines[2]
    with pytest.raises(C.CheckpointFork):
        w.verify(m, devices=fork.data)
    with pytest.raises(C.CheckpointFork):
        w.verify(m, devices=fork.data, custodian=False)


def test_hostile_shipped_values_are_refused_never_crash():
    w = World()
    m = w.make()
    for header in (b"\x00", b"[]", b"{}", b'{"v":1}', None, "text", m.header + b"\n"):
        with pytest.raises(C.CheckpointError):
            w.verify(m, custodian=False, shipped=C.Shipped(header=header, sig=m.sig), slot=None)
    with pytest.raises(C.CheckpointError):
        w.verify(m, custodian=False, shipped=C.Shipped(header=m.header, sig=None))
    with pytest.raises(C.CheckpointError):
        C.open_custodian(b"not age", [RK_ID])
    with pytest.raises(C.CheckpointError):
        C.open_custodian(m.container, [Identity(h("someone else"))])
    with pytest.raises(C.CheckpointError):
        w.verify(m, shipped="not shipped")


def test_custodian_mode_needs_every_member_and_the_epoch_keys():
    w = World()
    m = w.make()
    sh = C.open_custodian(m.container, [RK_ID])
    for missing in ("mac", "body", "devices"):
        with pytest.raises(C.CheckpointAlarm):
            w.verify(m, shipped=C.Shipped(**{**{k: getattr(sh, k) for k in ("header", "sig", "tsr", "mac", "body",
                                                                             "devices", "policy", "stanzas")},
                                             missing: None}))
    with pytest.raises(C.CheckpointAlarm, match="epoch 0"):
        C.verify_checkpoint(sh, ledger_id=LEDGER, root=w.root, devices=w.kit.data, now=NOW, epochs={1: EK[1]})


# One tamper case per custodian-side check in verify_checkpoint step 6. Each is rebuilt around the change (signed
# and MACed again), so only the check named can fail.

def _flip_mac(m):
    mac = bytearray(m["irp/checkpoint.mac"])
    mac[0] = ord("0") if mac[0] != ord("0") else ord("1")
    m["irp/checkpoint.mac"] = bytes(mac)


def _other_box(w):
    return Identity(h("box/not enrolled")).recipient().to_string()


TAMPERS = {
    "mac": (dict(members=_flip_mac), "MAC"),
    "mac_format": (dict(members=lambda m: m.update({"irp/checkpoint.mac": m["irp/checkpoint.mac"][:-1]})), "MAC"),
    "body_digest": (dict(body=lambda b: b.update(tsa_policy_digest="sha256-" + "0" * 64), fix_digest=False),
                    "body_digest"),
    "body_kind": (dict(body=lambda b: b.update(kind="checkpoint-bodies")), "checkpoint-body"),
    "body_closed": (dict(body=lambda b: b.update(extra=1)), "keys"),
    "logs_devices": (dict(body=lambda b: b["logs"]["devices"].update(byte_digest="sha256-" + "0" * 64)),
                     "logs.devices"),
    "segment_ranges": (dict(body=lambda b: b["logs"]["devices"]["segments"][0].update(offset=1)), "contiguous"),
    "recipients": (None, "recipients aren't"),
    "container_stanzas": (dict(stanzas=3), "stanza"),
    "snapshot_ledger_digest": (dict(body=lambda b: b["logs"]["ledger"].update(byte_digest="sha256-" + "0" * 64)),
                               "ledger byte digest"),
    "policy_without_digest": (dict(members=lambda m: m.update({"irp/policy.json": b'{"v":1}'})), "policy"),
    "digest_without_policy": (dict(body=lambda b: b.update(policy_digest=digest(b'{"v":1}'))), "policy"),
    "policy_differs": (dict(body=lambda b: b.update(policy_digest=digest(b'{"v":1}')),
                            members=lambda m: m.update({"irp/policy.json": b'{"v":2}'})), "policy"),
    "devices_member": (dict(members=lambda m: m.update({"irp/devices.jsonl": m["irp/devices.jsonl"] + b"\n"})),
                       "irp/devices.jsonl"),
    "snapshot_id": (dict(body=lambda b: resnapshot(b, lambda man: man.update(snapshot_id="IRPC-00000000-1"))),
                    "snapshot_id"),
    "snapshot_created_at": (dict(body=lambda b: resnapshot(b, lambda man: man.update(
        created_at="2001-01-01T00:00:00Z"))), "created_at"),
    "snapshot_digest": (dict(body=lambda b: b["snapshot"]["snapshot_digest"].update(value="0" * 64)),
                        "snapshot_digest"),
    "snapshot_previous_at_seq_1": (dict(body=lambda b: resnapshot(b, lambda man: man.update(
        previous_snapshot_digest="0" * 64))), "previous_snapshot_digest"),
}


@pytest.mark.parametrize("case", sorted(TAMPERS))
def test_each_custodian_side_check_refuses_its_own_tamper(case):
    w = World()
    m = w.make()
    w.verify(m, shipped=reforge(w, m))  # the rebuild alone still verifies
    kw, match = TAMPERS[case]
    if case == "recipients":  # the same number of recipients, so only the recipients check can fail
        def swap(b):
            for r in b["recipients"]:
                if r["id"] != "rk":
                    r["recipient"] = _other_box(w)
        kw = dict(body=swap)
    shipped = reforge(w, m, **kw)
    with pytest.raises(C.CheckpointAlarm, match=match) as exc:
        w.verify(m, shipped=shipped)
    assert EK[0].ka.hex() not in str(exc.value) and EK[0].kc.hex() not in str(exc.value)


def test_each_fetched_segment_must_be_listed_named_by_its_hash_and_have_one_stanza_per_recipient():
    w = World()
    m = w.make()
    body = C.parse_body(m.body)
    objects = dict(m.objects)
    w.verify(m, segments=objects)
    seg = body["logs"]["devices"]["segments"][0]
    plain = container.pack("segment", {"seg": w.kit.data[seg["offset"]:seg["offset"] + seg["length"]]})
    targets = [age.Recipient.from_string(r["recipient"]) for r in body["recipients"]]
    extra = age.encrypt(plain, targets + [age.Recipient.from_string(_other_box(w))])
    oid = "o/" + hashlib.sha256(extra).hexdigest()
    shipped = reforge(w, m, body=lambda b: b["logs"]["devices"]["segments"][0].update(object=oid))
    w.verify(m, shipped=shipped)
    with pytest.raises(C.CheckpointAlarm, match="stanza"):
        w.verify(m, shipped=shipped, segments={oid: extra})
    with pytest.raises(C.CheckpointAlarm, match="listed"):
        w.verify(m, segments={oid: extra})
    first = next(iter(objects))
    with pytest.raises(C.CheckpointAlarm, match="hash"):
        w.verify(m, segments={first: objects[first] + b"x"})


# ── Verifying a chain (§18a "Verifying a chain") ──

def test_a_chain_verifies_and_seq_and_prev_must_link():
    w = World()
    ms = [w.make() for _ in range(3)]
    vs = w.verified(*ms)
    ch = w.chain(vs)
    assert [(v.epoch, v.seq) for v in ch.checkpoints] == [(0, 1), (0, 2), (0, 3)]
    assert ch.abandoned == () and ch.unchecked == () and ch.label(0, 1) == "NONE"
    with pytest.raises(C.CheckpointAlarm, match="contiguous"):
        w.chain([vs[0], vs[2]])
    gap = World()
    m1 = gap.make()
    m3 = gap.make(seq=3)  # prev is seq 1's digest: a gap with an intact prev link
    assert json.loads(m3.header)["prev"] == m1.digest
    with pytest.raises(C.CheckpointAlarm, match="contiguous"):
        gap.chain(gap.verified(m1, m3))
    with pytest.raises(C.CheckpointAlarm, match="order"):
        w.chain([vs[1], vs[0]])
    with pytest.raises(C.CheckpointAlarm, match="order"):
        w.chain([vs[0], vs[0]])
    m4 = w.make(prev=digest(b"not the previous header"))
    v4 = w.verify(m4)
    with pytest.raises(C.CheckpointAlarm, match="prev"):
        w.chain(vs + [v4])
    with pytest.raises(C.CheckpointError, match="prev"):
        w.chain(w.verified(*ms, m4, custodian=False), custodian=False)
    with pytest.raises(C.CheckpointError):
        w.chain([])


def test_a_later_checkpoint_whose_devices_prefix_drops_a_revoke_line_is_rejected():
    w = World()
    phone = w.companion()
    before = w.kit.data
    w.kit.revoke(phone, [w.strand])
    m1 = w.make()                   # cites the revoke line
    m2 = w.make(devices=before)     # cites a prefix without it
    v1, v2 = w.verified(m1, m2)
    with pytest.raises(C.CheckpointRollback):
        w.chain([v1, v2])
    with pytest.raises(C.CheckpointRollback):
        w.chain(w.verified(m1, m2, custodian=False), custodian=False)
    # A copy where another line stands at the revoke line's idx (the same revoke, signed by the root instead) is
    # a FORK against the newest log.
    other = World()
    other.kit.revoke(other.companion(), [other.root])
    assert other.kit.lines[:-1] == w.kit.lines[:-1] and other.kit.lines[-1] != w.kit.lines[-1]
    other.record = w.made[0].record(None)
    m3 = other.make(seq=2, prev=m1.digest)
    with pytest.raises(C.CheckpointFork):
        w.verify(m3)


def test_the_strand_changes_across_a_run_of_device_rotate_lines():
    w = World()
    m1 = w.make()
    w.rotate()      # the hook's checkpoint failed: no checkpoint on this strand
    s2 = w.rotate()
    m2 = w.make()
    assert (m2.strand, m2.seq, json.loads(m2.header)["prev"]) == (s2, 2, m1.digest)
    w.chain(w.verified(m1, m2))
    w.chain(w.verified(m1, m2, custodian=False), custodian=False)


def test_a_strand_that_arrives_any_other_way_is_refused():
    w = World()
    m1 = w.make()
    other = w.kit.custodian("laptop-2")  # enrolled by the root in the same epoch: no device_rotate
    header = C.build_header(ledger_id=LEDGER, root=w.root, epoch=0, strand=other, seq=2, prev=m1.digest,
                            created_at=w.created_at(), devices=w.kit.data, body=b"{}")
    v2 = w.verify(m1, custodian=False, slot=None,
                  shipped=C.Shipped(header=header, sig=C.sign_header(header, w.kit.keys[other].seed, other)))
    with pytest.raises(C.CheckpointAlarm, match="device_rotate"):
        w.chain([w.verify(m1, custodian=False), v2], custodian=False)
    # A device_rotate from another strand hands nothing over: the first line of the run is from the old strand.
    w = World()
    m1 = w.make()
    rotated = w.kit.rotate(w.kit.custodian("laptop-2"), w.ak)
    header = C.build_header(ledger_id=LEDGER, root=w.root, epoch=0, strand=rotated, seq=2, prev=m1.digest,
                            created_at=w.created_at(), devices=w.kit.data, body=b"{}")
    v2 = w.verify(m1, custodian=False, slot=None,
                  shipped=C.Shipped(header=header, sig=C.sign_header(header, w.kit.keys[rotated].seed, rotated)))
    with pytest.raises(C.CheckpointAlarm, match="device_rotate"):
        w.chain([w.verify(m1, custodian=False), v2], custodian=False)


def test_c1_hook_failure_then_a_second_rotation_then_revoking_the_extra_custodian_verifies_across_both_lines():
    w = World()
    m1 = w.make()
    extra = w.kit.custodian("laptop-2")
    w.rotate()
    with pytest.raises(C.CheckpointAlarm, match="revoke laptop-2"):
        w.make()  # the rotation hook's checkpoint fails (C1); the rotation itself stands
    w.rotate()
    w.kit.revoke(extra, [w.strand])
    m2 = w.make()
    assert m2.seq == 2 and json.loads(m2.header)["prev"] == m1.digest
    w.chain(w.verified(m1, m2))


def test_an_epoch_change_links_seq_1_to_checkpoint_ref_and_reports_abandoned_seqs():
    w = World()
    m1 = w.make()
    m2 = w.make(created_at=ts(NOW - timedelta(days=1)))  # later than anything after the recovery
    w.recover(ref=m1.digest)
    m3 = w.make()
    assert json.loads(m3.header)["created_at"] < json.loads(m2.header)["created_at"]
    ch = w.chain(w.verified(m1, m2, m3))
    assert ch.abandoned == ((0, 2),)
    assert all(not e["append_only"] for e in w.verify(m3).body["logs"].values())
    ch = w.chain(w.verified(m1, m2, m3, custodian=False), custodian=False)
    assert ch.abandoned == ((0, 2),)
    w2 = World("ck2")
    a, b = w2.make(), w2.make()
    w2.recover(ref=None)
    c = w2.make()
    assert json.loads(c.header)["prev"] is None
    assert w2.chain(w2.verified(a, b, c)).abandoned == ((0, 1), (0, 2))


def test_an_epoch_change_from_a_checkpoint_outside_the_run_lists_its_append_only_claims_as_unchecked():
    w = World()
    m1, m2, m3 = w.make(), w.make(), w.make()
    w.recover(ref=m3.digest)
    m4 = w.make()
    claims = {(1, 1, n) for n in C.LOG_NAMES if w.verify(m4).body["logs"][n]["append_only"]}
    assert claims
    ch = w.chain(w.verified(m1, m2, m4))  # (1, 1) continues from (0, 3), which isn't in the run
    assert claims <= set(ch.unchecked) and ch.abandoned == ((0, 1), (0, 2))


def test_last_present_and_the_gentime_order_start_again_at_a_new_epoch(pki):
    w = World()
    t = w.kit.clock.t
    m1 = w.make(stamp=present(pki, t + timedelta(minutes=10)), created_at=ts(t + timedelta(minutes=9)))
    m2 = w.make(stamp=present(pki, t + timedelta(days=3)), created_at=ts(t + timedelta(minutes=9)))
    assert w.record["last_present"]["epoch"] == 0
    w.recover(ref=m1.digest)  # (0, 2) is abandoned at the epoch change
    m3 = w.make(stamp=present(pki, t + timedelta(days=1)), created_at=ts(t + timedelta(days=1)))
    assert w.record["epoch"] == 1 and w.record["last_present"]["gen_time"] == ts(t + timedelta(days=1))
    ch = w.chain(w.verified(m1, m2, m3, pins=pins(pki)))  # genTime goes back across epochs only: fine
    assert ch.abandoned == ((0, 2),)
    w = World()
    m1 = w.make(stamp=present(pki, t + timedelta(minutes=10)), created_at=ts(t + timedelta(minutes=9)))
    w.recover(ref=m1.digest)
    w.make()  # NONE: nothing carries over from epoch 0
    assert w.record["epoch"] == 1 and w.record["last_present"] is None


def test_the_snapshot_chain_links_within_an_epoch_on_the_custodian_side():
    w = World()
    m1, m2 = w.make(), w.make()
    forged = reforge(w, m2, body=lambda b: resnapshot(b, lambda man: man.update(previous_snapshot_digest="0" * 64)))
    v1, v2 = w.verify(m1), w.verify(m2, shipped=forged)  # each alone is consistent
    with pytest.raises(C.CheckpointAlarm, match="snapshot"):
        w.chain([v1, v2])
    w.chain(w.verified(m1, m2))


def test_created_at_never_goes_backwards_along_the_chain():
    w = World()
    m1 = w.make(created_at=w.created_at(minutes=10))
    m2 = w.make(created_at=w.created_at(minutes=5))
    with pytest.raises(C.CheckpointAlarm, match="created_at"):
        w.chain(w.verified(m1, m2))


def test_a_present_gentime_regression_within_an_epoch_is_alarm(pki):
    w = World()
    t = w.kit.clock.t
    m1 = w.make(stamp=present(pki, t + timedelta(minutes=10)), created_at=ts(t + timedelta(minutes=9)))
    m2 = w.make(stamp=present(pki, t + timedelta(minutes=5)), created_at=ts(t + timedelta(minutes=9)))
    for custodian in (True, False):
        with pytest.raises(C.CheckpointAlarm, match="genTime"):
            w.chain(w.verified(m1, m2, custodian=custodian, pins=pins(pki)), custodian=custodian)


def test_the_first_present_table_and_transitive_labels(pki):
    w = World()
    m1 = w.make()
    t1 = w.kit.clock.t
    m2 = w.make(stamp=present(pki, t1 + timedelta(minutes=2)), created_at=ts(t1 + timedelta(minutes=1)))
    w.readers.enrol(READER, [w.strand, w.ak])
    w.kit.approver("hwkey-2")
    m3 = w.make()
    t3 = w.kit.clock.t
    m4 = w.make(stamp=present(pki, t3 + timedelta(minutes=2)), created_at=ts(t3 + timedelta(minutes=1)))
    ch = w.chain(w.verified(m1, m2, m3, m4, pins=pins(pki)))
    assert [(c.epoch, c.seq) for c in ch.devices_first_present] == [(0, 2)] * 3 + [(0, 4)]
    assert [(c.epoch, c.seq) for c in ch.readers_first_present] == [(0, 4)]
    assert ch.revocation_time(3) == t3 + timedelta(minutes=2)
    assert ch.label(0, 1) == "TRANSITIVE via (0, 2)" and ch.label(0, 2) == "PRESENT"
    assert ch.label(0, 3) == "TRANSITIVE via (0, 4)" and ch.label(0, 4) == "PRESENT"
    rch = w.chain(w.verified(m1, m2, m3, m4, custodian=False, pins=pins(pki)), custodian=False)
    assert rch.readers_first_present == () and rch.label(0, 1) == "TRANSITIVE via (0, 2)"
    assert w.chain(w.verified(m1, m2, m3, m4)).label(0, 1) == "NONE"  # no pins: nothing is PRESENT


def test_a_line_dated_two_hours_after_its_covering_present_gentime_fails_custodian_verification(pki):
    w = World()
    w.kit.approver("hwkey-2")
    x = w.kit.clock.t
    m = w.make(stamp=present(pki, x - timedelta(hours=2)), created_at=ts(x + timedelta(minutes=1)))
    v = w.verify(m, pins=pins(pki))
    assert v.label == "PRESENT" and v.created_at_skew
    with pytest.raises(C.CheckpointAlarm, match="1 hour"):
        w.chain([v])
    w.chain([w.verify(m, custodian=False, pins=pins(pki))], custodian=False)  # a reader doesn't apply the rule
    r = World("ck3")
    r.readers.enrol(READER, [r.strand, r.ak])
    y = r.kit.clock.t
    mr = r.make(stamp=present(pki, y - timedelta(minutes=61)), created_at=ts(y + timedelta(minutes=1)))
    with pytest.raises(C.CheckpointAlarm, match="readers line 0"):
        r.chain([r.verify(mr, pins=pins(pki))])


def test_append_only_claims_are_checked_against_their_predecessor_on_the_custodian_side():
    w = World()
    m1 = w.make()
    w.ledger = w.ledger.replace(b"neutral title 3", b"neutral title 9")
    w.ledger += jsonl([{"id": "IRP-2001-01-01-004", "type": "decision", "title": "neutral title 4"}])
    m2 = w.make()  # honest: append_only false
    forged = reforge(w, m2, body=lambda b: b["logs"]["ledger"].update(append_only=True))
    v1, v2 = w.verify(m1), w.verify(m2, shipped=forged)
    with pytest.raises(C.CheckpointFork, match="ledger"):
        w.chain([v1, v2], logs={"ledger": w.ledger})
    with pytest.raises(C.CheckpointFork, match="ledger"):
        w.chain([v1, v2], log_bytes=lambda v, name: w.ledger if (v.seq, name) == (2, "ledger") else None)
    assert w.chain([v1, v2]).unchecked == ((0, 2, "ledger"),)  # no bytes to check it with: said so
    w.chain(w.verified(m1, m2))
    # An honest append is proved by its segments alone; a rotation's base is proved by the bytes.
    w2 = World("ck4")
    a = w2.make()
    w2.ledger += jsonl([{"id": "IRP-2001-01-01-004", "type": "decision", "title": "neutral title 4"}])
    b = w2.make()
    w2.rotate()
    w2.ledger += jsonl([{"id": "IRP-2001-01-01-005", "type": "decision", "title": "neutral title 5"}])
    c = w2.make()
    assert w2.chain(w2.verified(a, b)).unchecked == ()
    assert w2.chain(w2.verified(a, b, c)).unchecked == ((0, 3, "ledger"),)
    assert w2.chain(w2.verified(a, b, c), logs={"ledger": w2.ledger}).unchecked == ()
    first = reforge(w2, a, body=lambda bd: bd["logs"]["ledger"].update(append_only=True))
    with pytest.raises(C.CheckpointAlarm, match="append_only"):
        w2.chain([w2.verify(a, shipped=first)])


def test_a_chain_is_checked_against_one_devices_log_and_custodian_mode_needs_the_readers_log():
    w = World()
    m1 = w.make()
    v1 = w.verify(m1)
    w.kit.approver("hwkey-2")
    with pytest.raises(C.CheckpointAlarm, match="another devices log"):
        w.chain([v1])
    v1 = w.verify(m1)
    with pytest.raises(C.CheckpointAlarm, match="readers"):
        C.verify_chain([v1], ledger_id=LEDGER, root=w.root, devices=w.kit.data, now=NOW, custodian=True)
    with pytest.raises(C.CheckpointAlarm, match="custodian side"):
        w.chain([w.verify(m1, custodian=False)])


def test_adoption_checks_a_candidate_against_the_stored_record_with_the_same_rules():
    """Part 2 adopts a staged checkpoint by checking it against state.json's record, which keeps the header, the
    signature, the snapshot digest and the logs but no body: the chain rules, applied from the record's side."""
    w = World()
    w.make()
    rec = w.record
    w.ledger += jsonl([{"id": "IRP-2001-01-01-004", "type": "decision", "title": "neutral title 4"}])
    m2 = w.make(keep=False)
    v2 = w.verify(m2)
    rv = C.verify_checkpoint(C.shipped_from_mark(rec), ledger_id=LEDGER, root=w.root, devices=w.kit.data, now=NOW,
                             cited=rec["digest"], slot=(rec["epoch"], rec["seq"]))
    w.chain([rv, v2], custodian=False)  # seq, prev, strand, created_at, genTime, devices prefix
    C.check_snapshot_link(rec["snapshot_digest"], v2)
    assert C.check_append_only(rec["logs"], v2) == ()  # a pure append: its segments start with the record's
    with pytest.raises(C.CheckpointAlarm, match="snapshot"):
        C.check_snapshot_link("0" * 64, v2)
    w.ledger = w.ledger.replace(b"neutral title 2", b"neutral title 6")
    w.ledger += jsonl([{"id": "IRP-2001-01-01-005", "type": "decision", "title": "neutral title 5"}])
    m3 = w.make(keep=False)
    forged = reforge(w, m3, body=lambda b: b["logs"]["ledger"].update(append_only=True))
    v3 = w.verify(m3, shipped=forged)
    assert C.check_append_only(rec["logs"], v3) == ("ledger",)  # no bytes to check the claim with
    with pytest.raises(C.CheckpointFork, match="ledger"):
        C.check_append_only(rec["logs"], v3, logs={"ledger": w.ledger})
    assert C.check_append_only(rec["logs"], w.verify(m3)) == ()  # the honest one claims nothing
    with pytest.raises(C.CheckpointAlarm):
        C.check_append_only(rec["logs"], w.verify(m3, custodian=False))
    seen = v2.seen()
    sv = C.verify_checkpoint(C.shipped_from_mark(seen), ledger_id=LEDGER, root=w.root,
                             devices=sig.b64url_decode(seen["devices"]), now=NOW, cited=seen["digest"])
    assert sv.digest == v2.digest
    for bad in ({}, {"header": "!!", "sig": rec["sig"]}, {"header": rec["header"], "sig": None}):
        with pytest.raises(C.CheckpointError):
            C.shipped_from_mark(bad)
