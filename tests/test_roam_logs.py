"""Roaming IRP, Cut 1 step 2.5b: the device and reader logs (spec v0.3 §14.3, §14.5, §14.5a).

`devices.jsonl` and `readers.jsonl` are append-only logs of signed, hash-chained JCS lines. Every verifier
replays them from the pinned root: who may sign each event, which keys are active, retired or revoked at
every line, the epoch, and which readers may be served. These tests build logs with a software
authenticator and Ed25519 keys (tests/roam_logkit.py) and check every rule in §14.5a.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import sig  # noqa: E402
from irp.roam.logs import (  # noqa: E402
    MAX_LINE,
    LogError,
    LogFork,
    LogRollback,
    LogWriter,
    TornLine,
    check_descriptor,
    check_extends,
    check_rule,
    compare_copies,
    line_hash,
    replay_devices,
    replay_readers,
    rule_digest,
    split_log,
)
from roam_logkit import (  # noqa: E402
    LEDGER,
    NOW,
    Clock,
    DevicesKit,
    EdKey,
    ReadersKit,
    approver_key,
    box,
    phone_key,
    scope,
    sign_body,
    standard_devices,
    ts,
)

READER = "rd-" + "b2" * 16
READER2 = "rd-" + "c3" * 16


def replay(kit: DevicesKit, **kw):
    return replay_devices(kit.data, ledger_id=LEDGER, root=kw.pop("root", kit.root.kid), now=kw.pop("now", NOW), **kw)


def rejects(kit: DevicesKit, match: str | None = None, **kw):
    with pytest.raises(LogError, match=match):
        replay(kit, **kw)


# ── Lines ──

def test_split_log_takes_complete_lines_only():
    assert split_log(b"") == []
    assert split_log(b'{"a":1}\n{"b":2}\n') == [b'{"a":1}', b'{"b":2}']
    with pytest.raises(TornLine):
        split_log(b'{"a":1}\n{"b":')
    for bad in (b"\n", b'{"a":1}\n\n', b'{"a":1}\r\n'):
        with pytest.raises(LogError):
            split_log(bad)


def test_line_hash_is_over_the_line_without_its_newline():
    assert line_hash(b"abc") == "sha256-" + hashlib.sha256(b"abc").hexdigest()


def test_a_minimal_log_replays():
    kit, laptop, ak = standard_devices()
    log = replay(kit)
    st = log.state()
    assert st.idx == 2 and st.epoch == 0 and st.root == kit.root.kid
    assert set(st.active) == {laptop, ak}
    assert st.status[laptop] == ("active", 1) and st.status[ak] == ("active", 2)
    assert log.state(0).active == {}


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(v=2), "v must be 1"),
    (lambda b: b.update(kind="readers-entry"), "kind"),
    (lambda b: b.update(event="device_teleport"), "unknown event"),
    (lambda b: b.update(ledger_id="ILID-" + "b1" * 16), "ledger_id"),
    (lambda b: b.update(idx=7), "idx"),
    (lambda b: b.update(idx=True), "idx"),
    (lambda b: b.update(prev="sha256-" + "0" * 64), "prev"),
    (lambda b: b.update(at="2026-10-08 09:05:00"), "at"),
    (lambda b: b.update(at="2026-02-30T09:05:00Z"), "at"),
    (lambda b: b.update(extra=1), "keys"),
    (lambda b: b.pop("prev"), "keys"),
    (lambda b: b.update(root="rt-" + "0" * 32), "root"),
])
def test_body_fields_are_checked(change, match):
    kit, laptop, ak = standard_devices()
    kit.revoke(laptop, [kit.root.kid], mutate=change)
    rejects(kit, match)


def test_at_never_goes_backwards():
    kit, laptop, ak = standard_devices()
    kit.revoke(laptop, [kit.root.kid], mutate=lambda b: b.update(at="2026-10-08T08:00:00Z"))
    rejects(kit, "backwards")


def test_at_may_repeat():
    kit, laptop, ak = standard_devices()
    same = ts(kit.clock.t)
    kit.revoke(laptop, [kit.root.kid], mutate=lambda b: b.update(at=same))
    replay(kit)


def test_at_in_the_future_is_bounded_by_the_verifier_clock():
    kit, laptop, ak = standard_devices()
    at = kit.clock.t
    replay(kit, now=at - timedelta(minutes=5))
    with pytest.raises(LogError, match="future"):
        replay(kit, now=at - timedelta(minutes=5, seconds=1))


@pytest.mark.parametrize("change,match", [
    (lambda o: o.update(extra=1), "line"),
    (lambda o: o.update(sigs=[]), "sigs"),
    (lambda o: o.update(sigs=o["sigs"][::-1]), "sorted"),
    (lambda o: o.update(sigs=o["sigs"] + [o["sigs"][0]]), "sorted"),
    (lambda o: o["sigs"][0].update(alg="webauthn-es256"), "alg"),
    (lambda o: o["sigs"][0].update(sig=o["sigs"][0]["sig"][:-2] + "AA"), "signature|b64url"),
])
def test_signature_lists_are_strict(change, match):
    kit, laptop, ak = standard_devices()
    kit.custodian("laptop-2", raw=change)
    rejects(kit, match)


def test_line_bytes_must_be_exact_jcs():
    kit, laptop, ak = standard_devices()
    kit.lines[-1] = kit.lines[-1].replace(b'{"body":', b'{ "body":', 1)
    rejects(kit, "JCS")


def test_lines_have_a_size_limit():
    kit, laptop, ak = standard_devices()
    assert MAX_LINE == 65536
    big = kit.lines[-1][:-1] + b"," + b'"x":"' + b"a" * MAX_LINE + b'"}'
    kit.lines[-1] = big
    rejects(kit, "64 KiB")


def test_a_ck_key_never_signs_a_log_line():
    kit, laptop, ak = standard_devices()
    ck = EdKey("capability")
    ck_kid = sig.key_id("ck", ck.pub)
    body_sig = {"alg": "ed25519", "key_id": ck_kid, "sig": sig.b64url_encode(b"\x00" * 64)}
    kit.revoke(laptop, [kit.root.kid], extra=[body_sig])
    rejects(kit, "ck-")


def test_body_arrays_must_be_sorted_and_unique():
    kit, laptop, ak = standard_devices()
    other = kit.custodian("laptop-2")
    kit.recovery([laptop, ak], [other], mutate=lambda b: b.update(active=b["active"][::-1]))
    rejects(kit, "sorted")


def test_strings_in_arrays_are_printable_ascii():
    kit, laptop, ak = standard_devices()
    kit.recovery([laptop, ak], ["dk-é" + "0" * 31])
    rejects(kit, "ASCII|revokes|sorted")


# ── Descriptors ──

def _desc(kind: str) -> dict:
    if kind == "custodian":
        return EdKey("d").descriptor("laptop-1", box("d"))
    if kind == "companion":
        return phone_key("d").descriptor("phone-1", box("d"))
    return approver_key("d").descriptor("hwkey-1")


@pytest.mark.parametrize("kind", ["custodian", "companion", "approver"])
def test_good_descriptors_pass(kind):
    d = check_descriptor(_desc(kind))
    assert d.kid == _desc(kind)["kid"] and d.cls == kind


DESCRIPTOR_BREAKS = {
    "custodian": {
        "extra key": lambda d: d.update(extra=1),
        "kid not of pub": lambda d: d.update(kid="dk-" + "0" * 32),
        "ak prefix": lambda d: d.update(kid="ak" + d["kid"][2:]),
        "alg": lambda d: d.update(alg="webauthn-es256"),
        "small-order pub": lambda d: d.update(pub=sig.b64url_encode(b"\x01" + b"\x00" * 31)),
        "box null": lambda d: d.update(box=None),
        "box upper": lambda d: d.update(box=d["box"].upper()),
        "box not age": lambda d: d.update(box="age1notarecipient"),
        "label": lambda d: d.update(label="Laptop 1"),
        "label digits": lambda d: d.update(label="laptop-1234"),
        "key_scope": lambda d: d.update(key_scope="account-synced"),
        "webauthn": lambda d: d.update(webauthn={}),
        "class": lambda d: d.update(**{"class": "root"}),
    },
    "companion": {
        "box null": lambda d: d.update(box=None),
        "webauthn null": lambda d: d.update(webauthn=None),
        "key_scope": lambda d: d.update(key_scope="device-local"),
        "be not bool": lambda d: d["webauthn"].update(be=1),
        "bs without be": lambda d: d["webauthn"].update(be=False, bs=True),
        "origin not under rp": lambda d: d["webauthn"].update(origin="https://evil.example"),
        "origin with port": lambda d: d["webauthn"].update(origin="https://app.irp.example:8443"),
        "origin http": lambda d: d["webauthn"].update(origin="http://app.irp.example"),
        "rp under invalid": lambda d: d["webauthn"].update(rp_id="irp-roam.invalid",
                                                            origin="https://irp-roam.invalid"),
        "cred_id short": lambda d: d["webauthn"].update(cred_id=sig.b64url_encode(b"x" * 15)),
        "cred_id long": lambda d: d["webauthn"].update(cred_id=sig.b64url_encode(b"x" * 1024)),
        "webauthn extra": lambda d: d["webauthn"].update(extra=1),
        "kid uses other alg": lambda d: d.update(kid=sig.key_id("dk", sig.b64url_decode(d["pub"]), "fido2-es256")),
        "compressed pub": lambda d: d.update(pub=sig.b64url_encode(sig.b64url_decode(d["pub"])[:60])),
    },
    "approver": {
        "box": lambda d: d.update(box=box("x")),
        "rp_id": lambda d: d["webauthn"].update(rp_id="irp.example"),
        "origin": lambda d: d["webauthn"].update(origin="https://irp.example"),
        "be true": lambda d: d["webauthn"].update(be=True),
        "key_scope": lambda d: d.update(key_scope="account-synced"),
        "dk prefix": lambda d: d.update(kid="dk" + d["kid"][2:]),
        "alg": lambda d: d.update(alg="ed25519"),
    },
}


@pytest.mark.parametrize("kind,name", [(k, n) for k, v in DESCRIPTOR_BREAKS.items() for n in v])
def test_descriptors_are_closed(kind, name):
    d = _desc(kind)
    DESCRIPTOR_BREAKS[kind][name](d)
    with pytest.raises(LogError):
        check_descriptor(d)


# ── devices.jsonl: a full history ──

def _full_history():
    kit, laptop, ak1 = standard_devices("full")
    ak2 = kit.approver("hwkey-2", via=[laptop, ak1])
    phone = kit.companion("phone-1", via=[laptop, ak1])
    kit.rekey(phone, signers=[phone, laptop])
    kit.rekey(laptop)
    laptop2 = kit.rotate(laptop, ak2)
    kit.revoke(phone, [phone])
    kit.revoke(laptop, [laptop2])  # the retired key can still be revoked
    kit.approver_revoke(ak1, [laptop2])
    new = kit.recovery([ak2], [laptop2], new_label="laptop-2")
    old_root = kit.root.kid
    new_root = kit.root_rotate()
    return kit, dict(laptop=laptop, ak1=ak1, ak2=ak2, phone=phone, laptop2=laptop2, new=new, old_root=old_root,
                     new_root=new_root)


def test_a_full_history_replays():
    kit, k = _full_history()
    log = replay(kit)
    st = log.state()
    assert st.epoch == 2 and st.root == k["new_root"]
    assert set(st.active) == {k["ak2"], k["new"]}
    assert st.status[k["laptop"]][0] == "revoked" and st.status[k["phone"]][0] == "revoked"
    assert st.status[k["ak1"]][0] == "revoked" and st.status[k["laptop2"]][0] == "revoked"
    # the history is kept per line
    rot = next(i for i, s in enumerate(log.states) if s.status.get(k["laptop"], ("",))[0] == "retired")
    assert log.state(rot).status[k["laptop"]] == ("retired", rot)
    assert log.state(rot - 1).status[k["laptop"]][0] == "active"
    assert log.state(rot).active[k["laptop2"]]["label"] == "laptop-1"


def test_rekey_updates_the_box():
    kit, laptop, ak = standard_devices()
    kit.rekey(laptop)
    assert replay(kit).state().active[laptop]["box"] == kit.descs[laptop]["box"]


def test_the_pinned_root_is_the_one_after_the_last_line():
    kit, k = _full_history()
    rejects(kit, "pinned root", root=k["old_root"])
    replay(kit, root=k["old_root"], prefix=True)


def test_line_zero_must_be_genesis():
    kit = DevicesKit("nogenesis")
    kit.custodian("laptop-1")  # a well-formed, root-signed line 0 that isn't genesis
    rejects(kit, "line 0 must be genesis")


def test_genesis_only_on_line_zero():
    kit, laptop, ak = standard_devices()
    kit.genesis()
    rejects(kit, "genesis")


def test_genesis_root_must_be_its_root_pub():
    kit = DevicesKit("g")
    kit.genesis(mutate=lambda b: b.update(root="rt-" + "1" * 32))
    rejects(kit, "genesis root", root=kit.root.kid)
    rejects(kit, "genesis root", root="rt-" + "1" * 32)


@pytest.mark.parametrize("epoch", [1, True, "0"])
def test_genesis_epoch_is_zero(epoch):
    kit = DevicesKit("g")
    kit.genesis(mutate=lambda b: b.update(epoch=epoch))
    rejects(kit)


# ── Signer sets (§14.3 as amended by §14.5a) ──

def test_custodian_enrol_needs_root_and_the_new_device():
    kit, laptop, ak = standard_devices()
    new = EdKey("x")
    kit.custodian("laptop-2", key=new, signers=[laptop, new.kid])
    rejects(kit, "signers")
    kit2, laptop2, ak2 = standard_devices()
    kit2.custodian("laptop-2", key=EdKey("y"), signers=[kit2.root.kid])
    rejects(kit2, "signers")


def test_companion_enrol_needs_custodian_approver_and_phone():
    kit, laptop, ak = standard_devices()
    kit.companion("phone-1", via=[laptop])
    rejects(kit, "signers")


def test_companion_enrol_by_root_and_phone_is_allowed():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[kit.root.kid])
    assert phone in replay(kit).state().active


def test_no_mixing_of_two_signer_sets():
    kit, laptop, ak = standard_devices()
    kit.companion("phone-1", via=[kit.root.kid, laptop, ak])
    rejects(kit, "signers")


def test_no_extra_signer():
    kit, laptop, ak = standard_devices()
    kit.revoke(laptop, [kit.root.kid, ak])
    rejects(kit, "signers")


def test_a_signature_from_a_key_with_the_wrong_role_fails():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[laptop, ak])
    kit.companion("phone-2", via=[phone, ak])
    rejects(kit, "signers")


def test_rotate_needs_old_approver_and_new():
    kit, laptop, ak = standard_devices()
    k = EdKey("r")
    kit.keys[k.kid] = k
    d = k.descriptor("laptop-1", box("r"))
    kit.add("device_rotate", {"old": laptop, "device": d}, [laptop, k.kid])
    rejects(kit, "signers")


def test_rotate_keeps_the_label_and_the_class():
    kit, laptop, ak = standard_devices()
    kit.rotate(laptop, ak, mutate=lambda b: b["device"].update(label="laptop-2"))
    rejects(kit, "label")


def test_only_a_custodian_rotates():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[laptop, ak])
    k = EdKey("r2")
    kit.keys[k.kid] = k
    kit.add("device_rotate", {"old": phone, "device": k.descriptor("phone-1", box("r2"))}, [phone, ak, k.kid])
    rejects(kit)


def test_a_retired_key_signs_nothing_new():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.rotate(laptop, ak)
    kit.companion("phone-1", via=[laptop, ak])
    rejects(kit, "signers")


def test_a_revoked_key_signs_nothing():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.custodian("laptop-2")
    kit.revoke(laptop, [laptop2])
    kit.revoke(laptop2, [laptop])
    rejects(kit, "signers")


def test_what_a_key_signed_before_it_was_revoked_stays_valid():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[laptop, ak])
    laptop2 = kit.custodian("laptop-2")
    kit.revoke(laptop, [laptop2])
    st = replay(kit).state()
    assert phone in st.active and st.status[laptop][0] == "revoked"


def test_companion_rekey_needs_a_custodian_and_a_nonce():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[laptop, ak])
    kit.rekey(phone, signers=[phone])
    rejects(kit, "signers")
    kit2, laptop2, ak2 = standard_devices("n")
    phone2 = kit2.companion("phone-1", via=[laptop2, ak2])
    kit2.rekey(phone2, signers=[phone2, laptop2], nonce=None)
    rejects(kit2, "nonce")


def test_custodian_rekey_has_no_nonce():
    kit, laptop, ak = standard_devices()
    kit.rekey(laptop, nonce=sig.b64url_encode(b"n" * 16))
    rejects(kit, "nonce")


def test_companion_enrol_needs_a_16_byte_nonce():
    kit, laptop, ak = standard_devices()
    kit.companion("phone-1", via=[laptop, ak], nonce=sig.b64url_encode(b"n" * 15))
    rejects(kit, "nonce")


def test_custodian_enrol_has_no_nonce():
    kit, laptop, ak = standard_devices()
    kit.custodian("laptop-2", mutate=lambda b: b.update(nonce=sig.b64url_encode(b"n" * 16)))
    rejects(kit, "nonce")


def test_a_device_may_revoke_itself_only_while_active():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.rotate(laptop, ak)
    kit.revoke(laptop, [laptop])
    rejects(kit, "signers")


def test_revoke_of_an_unknown_kid_is_rejected():
    kit, laptop, ak = standard_devices()
    kit.revoke("dk-" + "9" * 32, [kit.root.kid])
    rejects(kit, "unknown|isn't active")


def test_revoke_twice_is_rejected():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.custodian("laptop-2")
    kit.revoke(laptop, [laptop2])
    kit.revoke(laptop, [laptop2])
    rejects(kit)


def test_device_revoke_cant_target_an_approver_and_back():
    kit, laptop, ak = standard_devices()
    kit.revoke(ak, [laptop])
    rejects(kit)
    kit2, laptop2, ak2 = standard_devices("b")
    kit2.approver_revoke(laptop2, [kit2.root.kid])
    rejects(kit2)


def test_an_approver_cant_revoke_itself_or_others():
    kit, laptop, ak = standard_devices()
    kit.approver_revoke(ak, [ak])
    rejects(kit, "signers")


def test_approver_enrol_by_custodian_needs_another_approver():
    kit, laptop, ak = standard_devices()
    kit.approver("hwkey-2", via=[laptop])
    rejects(kit, "signers")


def test_an_approver_must_have_be_clear():
    kit, laptop, ak = standard_devices()
    k = approver_key("be")
    k.flags |= 0x08  # a synced credential presenting as an approver
    k.unchecked = True
    kit.approver("hwkey-2", key=k)
    rejects(kit)


def test_a_companion_descriptor_must_match_its_own_flags():
    kit, laptop, ak = standard_devices()
    k = phone_key("flags", be=True, bs=False)
    k.flags = 0x1D  # the passkey reports BS, the descriptor says it hasn't synced
    kit.companion("phone-1", via=[laptop, ak], key=k)
    rejects(kit, "BS|flags")


# ── Never reused ──

def test_an_enrol_of_a_kid_seen_before_is_rejected():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.custodian("laptop-2")
    kit.revoke(laptop2, [laptop])
    kit.custodian("laptop-3", key=kit.keys[laptop2])
    rejects(kit)


def test_a_pub_can_never_be_reused_even_as_a_root():
    kit, laptop, ak = standard_devices()
    same_as_root = EdKey(kit.label + "/root/0")
    assert same_as_root.pub == kit.root.pub and same_as_root.kid.startswith("dk-")
    kit.custodian("laptop-2", key=same_as_root)
    rejects(kit, "reused|never")


def test_a_box_can_never_be_reused():
    kit, laptop, ak = standard_devices("box")
    k = EdKey("box2")
    kit.keys[k.kid] = k
    kit.add("device_enrol", {"device": k.descriptor("laptop-2", kit.descs[laptop]["box"]), "nonce": None},
            [kit.root.kid, k.kid])
    rejects(kit, "box")


def test_a_rekey_can_never_reuse_a_box():
    kit, laptop, ak = standard_devices("box2")
    laptop2 = kit.custodian("laptop-2")
    kit.add("device_rekey", {"kid": laptop2, "box": kit.descs[laptop]["box"], "nonce": None}, [laptop2])
    rejects(kit, "box")


def test_labels_are_unique_among_active_devices():
    kit, laptop, ak = standard_devices()
    kit.custodian("laptop-1")
    rejects(kit, "label")
    kit2, laptop2, ak2 = standard_devices("lbl")
    laptop3 = kit2.custodian("laptop-2")
    kit2.revoke(laptop3, [laptop2])
    kit2.custodian("laptop-2")  # free again once revoked
    replay(kit2)


# ── recovery and root_rotate ──

def test_recovery_moves_the_epoch_and_replaces_the_set():
    kit, laptop, ak = standard_devices()
    phone = kit.companion("phone-1", via=[laptop, ak])
    new = kit.recovery([ak], [laptop, phone])
    st = replay(kit).state()
    assert st.epoch == 1 and set(st.active) == {ak, new}
    assert st.status[phone][0] == "revoked"


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(epoch=2), "epoch"),
    (lambda b: b.update(revokes=[]), "revokes"),
    (lambda b: next(d for d in b["active"] if d["kid"].startswith("ak-")).update(label="hwkey-9"), "identical"),
    (lambda b: b.update(checkpoint_ref="nope"), "checkpoint_ref"),
])
def test_recovery_rules(change, match):
    kit, laptop, ak = standard_devices()
    kit.recovery([ak], [laptop], mutate=change)
    rejects(kit, match)


def test_recovery_new_device_must_be_a_new_custodian():
    kit, laptop, ak = standard_devices()
    k = phone_key("rec")
    kit.keys[k.kid] = k
    d = k.descriptor("phone-1", box("rec"))
    kit.add("recovery", {"active": sorted([kit.descs[ak], d], key=lambda x: x["kid"]), "revokes": [laptop],
                         "new_device": k.kid, "epoch": 1, "checkpoint_ref": None}, [kit.root.kid, k.kid])
    rejects(kit)


def test_recovery_cant_bring_back_a_retired_key():
    kit, laptop, ak = standard_devices()
    laptop2 = kit.rotate(laptop, ak)
    kit.recovery([ak, laptop], [laptop2])
    rejects(kit)


def test_recovery_needs_root_and_new_device():
    kit, laptop, ak = standard_devices()
    k = EdKey("rec2")
    kit.keys[k.kid] = k
    d = k.descriptor("laptop-9", box("rec2"))
    kit.add("recovery", {"active": sorted([kit.descs[ak], d], key=lambda x: x["kid"]), "revokes": [laptop],
                         "new_device": k.kid, "epoch": 1, "checkpoint_ref": None}, [kit.root.kid])
    rejects(kit, "signers")


def test_root_rotate_changes_who_signs_as_root():
    kit, laptop, ak = standard_devices()
    old = kit.root
    kit.root_rotate()
    kit.custodian("laptop-2")  # signed by the new root
    replay(kit)
    kit2, laptop2, ak2 = standard_devices("rr")
    old2 = kit2.root
    new2 = kit2.root_rotate()
    kit2.root = old2  # the outgoing root tries to keep signing
    kit2.custodian("laptop-2")
    rejects(kit2, root=new2)


def test_root_rotate_carries_the_outgoing_root():
    kit, laptop, ak = standard_devices()
    kit.root_rotate(mutate=lambda b: b.update(root=sig.root_id(sig.b64url_decode(b["root_pub"]))))
    rejects(kit, "root")


def test_a_root_pub_is_never_reused():
    kit, laptop, ak = standard_devices()
    first = kit.root
    kit.root_rotate()
    kit.keys[first.kid] = first
    kit.add("root_rotate", {"root_pub": sig.b64url_encode(first.pub), "epoch": 2, "checkpoint_ref": None},
            [kit.root.kid, first.kid])
    rejects(kit, root=first.kid)


def test_root_rotate_needs_both_roots():
    kit, laptop, ak = standard_devices()
    new = EdKey("nr", root=True)
    kit.keys[new.kid] = new
    kit.add("root_rotate", {"root_pub": sig.b64url_encode(new.pub), "epoch": 1, "checkpoint_ref": None},
            [kit.root.kid])
    rejects(kit, "signers", root=new.kid)


# ── Values ──

def test_integers_are_never_booleans():
    kit, laptop, ak = standard_devices()
    kit.recovery([ak], [laptop], mutate=lambda b: b.update(epoch=True))
    rejects(kit)


# ── readers.jsonl ──

def _readers():
    kit, laptop, ak = standard_devices("rd")
    rk = ReadersKit(kit)
    return kit, rk, laptop, ak


def rreplay(kit, rk, now=None):
    now = now or max(NOW, kit.clock.t)
    return replay_readers(rk.data, replay(kit, now=now), ledger_id=LEDGER, now=now)


def rrejects(kit, rk, match=None):
    with pytest.raises(LogError, match=match):
        rreplay(kit, rk)


def test_reader_lifecycle():
    kit, rk, laptop, ak = _readers()
    s1 = scope("s1", ids=["IRP-2001-10-01-001", "IRP-2001-10-01-002"])
    rk.enrol(READER, [laptop, ak], scopes=[s1], reviewed={"s1": ["IRP-2001-10-01-001"]})
    rk.review(READER, s1, ["IRP-2001-10-01-002", "IRP-2001-10-01-009"], [laptop])
    rk.rescope(READER, [s1], [laptop, ak], reviewed={"s1": ["IRP-2001-10-01-003"]})  # same rule: union
    rk.renew(READER, [laptop])
    log = rreplay(kit, rk)
    r = log.reader(READER)
    assert r.reviewed["s1"] == {"IRP-2001-10-01-001", "IRP-2001-10-01-002", "IRP-2001-10-01-003",
                                "IRP-2001-10-01-009"}
    assert r.recipient == box(f"reader/{READER}/3")
    assert log.status(READER, now=kit.clock.t, epoch=0) == "active"
    s2 = scope("s1", ids=["IRP-2001-10-01-005"])
    rk.rescope(READER, [s2], [laptop, ak], reviewed={"s1": ["IRP-2001-10-01-005"]})  # new rule: starts again
    assert rreplay(kit, rk).reader(READER).reviewed["s1"] == {"IRP-2001-10-01-005"}
    rk.revoke(READER, [laptop])
    log = rreplay(kit, rk)
    assert log.status(READER, now=kit.clock.t, epoch=0) == "revoked"
    assert log.reader(READER, 0).revoked_idx is None


def test_reader_expiry_is_judged_by_the_caller_clock():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=30)
    log = rreplay(kit, rk)
    t = kit.clock.t
    assert log.status(READER, now=t + timedelta(days=29), epoch=0) == "active"
    assert log.status(READER, now=t + timedelta(days=31), epoch=0) == "expired"
    assert log.active_readers(now=t, epoch=0) == [READER]


def test_an_epoch_change_ends_every_reader():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    new = kit.recovery([ak], [laptop])
    rk.renew(READER, [new])
    rrejects(kit, rk, "isn't active")
    rk.lines.pop()
    log = rreplay(kit, rk)
    assert log.status(READER, now=kit.clock.t, epoch=1) == "ended"
    rk.enrol(READER2, [new, ak])
    assert rreplay(kit, rk).status(READER2, now=kit.clock.t, epoch=1) == "active"


def test_a_reader_id_is_never_enrolled_twice():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.revoke(READER, [laptop])
    rk.enrol(READER, [laptop, ak], recipient=box("again"))
    rrejects(kit, rk, "seen")


@pytest.mark.parametrize("event", ["enrol", "rescope"])
def test_enrol_and_scope_need_an_approver(event):
    kit, rk, laptop, ak = _readers()
    if event == "enrol":
        rk.enrol(READER, [laptop])
    else:
        rk.enrol(READER, [laptop, ak])
        rk.rescope(READER, [scope()], [laptop])
    rrejects(kit, rk, "signers")


@pytest.mark.parametrize("event", ["renew", "review", "revoke"])
def test_renew_review_revoke_take_a_custodian_alone(event):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    {"renew": lambda: rk.renew(READER, [laptop, ak]),
     "review": lambda: rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop, ak]),
     "revoke": lambda: rk.revoke(READER, [laptop, ak])}[event]()
    rrejects(kit, rk, "signers")


def test_a_tapless_renew_cant_revive_a_revoked_reader():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.revoke(READER, [laptop])
    rk.renew(READER, [laptop])
    rrejects(kit, rk, "isn't active")


def test_a_renew_needs_an_enrolled_reader():
    kit, rk, laptop, ak = _readers()
    rk.renew(READER, [laptop])
    rrejects(kit, rk, "isn't active")


def test_renewals_stay_within_90_days_of_the_last_approval():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=30)
    for _ in range(2):
        kit.clock.tick(29 * 86400)
        rk.renew(READER, [laptop], days=30)
    rreplay(kit, rk)
    kit.clock.tick(29 * 86400)
    rk.renew(READER, [laptop], days=30)  # would end 117 days after the enrolment tap
    rrejects(kit, rk, "90 days")


@pytest.mark.parametrize("rescope", [True, False])
def test_a_rescope_re_approves_for_another_90_days(rescope):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=30)  # day 0, until day 30
    kit.clock.tick(25 * 86400)
    rk.renew(READER, [laptop], days=30)  # day 25, until day 55
    kit.clock.tick(25 * 86400)
    if rescope:
        rk.rescope(READER, [scope()], [laptop, ak])  # day 50: a tap with the same scopes
    kit.clock.tick(4 * 86400)
    rk.renew(READER, [laptop], days=60)  # day 54, until day 114
    if rescope:
        rreplay(kit, rk)
    else:
        rrejects(kit, rk, "90 days")


@pytest.mark.parametrize("days", [91, 0])
def test_enrol_expiry_bounds(days):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=days)
    rrejects(kit, rk, "expires")


def test_a_reader_recipient_never_reuses_a_device_box():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], recipient=kit.descs[laptop]["box"])
    rrejects(kit, rk, "recipient")


def test_a_renewal_needs_a_fresh_recipient():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.renew(READER, [laptop], recipient=box("reader/" + READER))
    rrejects(kit, rk, "recipient")


@pytest.mark.parametrize("change,match", [
    (lambda r: r.update(surface="browser-managed"), "surface"),
    (lambda r: r.update(surface="browser-ephemeral"), "surface"),
    (lambda r: r.update(region="eu"), "region"),
    (lambda r: r.update(viewing="shared"), "viewing"),
    (lambda r: r.update(identity_assurance="A1"), "A0"),
    (lambda r: r.update(reader_id="rd-1"), "reader_id"),
    (lambda r: r.update(extra=1), "keys"),
    (lambda r: r.update(scopes=[]), "scope"),
])
def test_reader_fields(change, match):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], mutate=lambda b: change(b["reader"]))
    rrejects(kit, rk, match)


@pytest.mark.parametrize("rule,ok", [
    (dict(ids=["IRP-2001-10-01-001"]), True),
    (dict(ids=[], tags_any=["public-safe"]), True),
    (dict(ids=["IRP-2001-10-01-001"], tags_any=["public-safe"]), False),
    (dict(ids=[], tags_any=[]), False),
    (dict(ids=[], tags_any=["a", "b"]), False),
    (dict(pinned=["IRP-2001-10-01-009"]), False),
    (dict(ids=["IRP-2001-10-01-001", "IRP-2001-10-01-009"], pinned=["IRP-2001-10-01-009"]), True),
    (dict(types=["contribution", "decision"]), True),
    (dict(types=["decision", "contribution"]), False),
    (dict(types=["recovery"]), False),
    (dict(ancestor_depth=5), False),
    (dict(ancestor_depth=0), False),
    (dict(token_budget=0), False),
    (dict(byte_budget=True), False),
    (dict(limit=0), False),
    (dict(limit=10), True),
    (dict(since="2026-02-30T00:00:00Z"), False),
    (dict(since="2026-01-01T00:00:00Z"), True),
    (dict(ids=["IRP-2001-10-01-002", "IRP-2001-10-01-001"]), False),
    (dict(ids=["has space"]), False),
])
def test_scope_rules(rule, ok):
    s = scope(**rule)
    if ok:
        check_rule(s["rule"])
        assert rule_digest(s["rule"]) == s["rule_digest"]
    else:
        with pytest.raises(LogError):
            check_rule(s["rule"])


def test_the_writer_side_rule_check_pins_the_policy_tag():
    s = scope(ids=[], tags_any=["work"])
    check_rule(s["rule"])
    check_rule(scope(ids=[], tags_any=["public-safe"])["rule"], public_safe_tag="public-safe")
    with pytest.raises(LogError, match="public_safe_tag"):
        check_rule(s["rule"], public_safe_tag="public-safe")


def test_scope_rule_has_exactly_nine_keys():
    s = scope()
    rule = dict(s["rule"])
    rule.pop("pinned")
    with pytest.raises(LogError):
        check_rule(rule)
    with pytest.raises(LogError):
        check_rule({**s["rule"], "extra": 1})


def test_a_scope_rule_digest_must_match():
    kit, rk, laptop, ak = _readers()
    bad = {**scope(), "rule_digest": "sha256-" + "0" * 64}
    rk.enrol(READER, [laptop, ak], scopes=[bad], reviewed={"s1": []})
    rrejects(kit, rk, "rule_digest")


def test_scope_ids_are_unique_and_reviewed_matches_them():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], scopes=[scope("s1"), scope("s1")])
    rrejects(kit, rk, "sorted|scope")
    kit2, rk2, laptop2, ak2 = _readers()
    rk2.enrol(READER, [laptop2, ak2], scopes=[scope("s1")], reviewed={"s2": []})
    rrejects(kit2, rk2, "reviewed")


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(scope_id="s9"), "scope"),
    (lambda b: b.update(rule_digest="sha256-" + "0" * 64), "rule_digest"),
    (lambda b: b.update(reviewed_ids=[]), "reviewed_ids"),
])
def test_review_rules(change, match):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop], mutate=change)
    rrejects(kit, rk, match)


def test_revocation_is_final():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.revoke(READER, [laptop])
    rk.revoke(READER, [laptop])
    rrejects(kit, rk, "revoked")
    kit2, rk2, laptop2, ak2 = _readers()
    rk2.revoke(READER, [laptop2])
    rrejects(kit2, rk2, "never enrolled|isn't enrolled")


def test_an_expired_reader_can_still_be_revoked():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=1)
    kit.clock.tick(2 * 86400)
    rk.revoke(READER, [laptop])
    assert rreplay(kit, rk).status(READER, now=kit.clock.t, epoch=0) == "revoked"


# ── devices_at ──

def test_devices_at_must_name_a_real_devices_line():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], devices_at={"idx": 2, "line": "sha256-" + "0" * 64})
    rrejects(kit, rk, "devices_at")
    kit2, rk2, laptop2, ak2 = _readers()
    rk2.enrol(READER, [laptop2, ak2], devices_at={"idx": 9, "line": "sha256-" + "0" * 64})
    rrejects(kit2, rk2, "devices_at")


def test_devices_at_cites_the_tail():
    kit, rk, laptop, ak = _readers()
    old = kit.tail()
    kit.custodian("laptop-2")
    rk.enrol(READER, [laptop, ak], devices_at=old)  # a newer devices line existed when this was written
    rrejects(kit, rk, "tail")


def test_devices_at_never_goes_down():
    kit, rk, laptop, ak = _readers()
    first = kit.tail()
    rk.enrol(READER, [laptop, ak])
    kit.custodian("laptop-2")
    rk.renew(READER, [laptop])
    rk.renew(READER, [laptop], devices_at=first)
    rrejects(kit, rk, "devices_at|tail")


def test_a_retired_key_is_fenced_out_of_the_readers_log():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    before = kit.tail()
    laptop2 = kit.rotate(laptop, ak)
    rk.renew(READER, [laptop2])  # the rotation renewal cites the new tail
    rk.renew(READER, [laptop], devices_at=before)  # a copied keystore citing the old tail
    rrejects(kit, rk)


def test_readers_root_is_the_root_at_devices_at():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], mutate=lambda b: b.update(root="rt-" + "0" * 32))
    rrejects(kit, rk, "root")


def test_readers_lines_chain_and_count():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.renew(READER, [laptop], mutate=lambda b: b.update(prev="sha256-" + "1" * 64))
    rrejects(kit, rk, "prev")


def test_a_readers_line_from_the_future_is_rejected():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    with pytest.raises(LogError, match="future"):
        replay_readers(rk.data, replay(kit), ledger_id=LEDGER, now=kit.clock.t - timedelta(minutes=10))


# ── The prefix check ──

def test_compare_copies():
    kit, laptop, ak = standard_devices("p")
    older = kit.data
    kit.custodian("laptop-2")
    newer = kit.data
    compare_copies(older, newer)
    compare_copies(newer, newer)
    with pytest.raises(LogRollback):
        compare_copies(newer, older)


def test_a_different_line_at_the_same_idx_is_a_fork():
    a, laptop, ak = standard_devices("f")
    b = DevicesKit("f")
    b.genesis()
    b.lines = list(a.lines)
    a.custodian("laptop-2")
    b.custodian("laptop-3")
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, b.data)
    assert exc.value.idx == 3 and not exc.value.root_fork


def test_two_root_signed_lines_at_one_idx_are_a_root_fork():
    a, laptop, ak = standard_devices("rf")
    base = list(a.lines)
    a.recovery([ak], [laptop])
    other = DevicesKit("rf")
    other.lines, other.descs, other.keys = list(base), dict(a.descs), dict(a.keys)
    other.clock = Clock(a.clock.t)
    other.root = a.first_root
    other.root_rotate()
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, other.data, devices=replay(a))
    assert exc.value.root_fork


def test_check_extends_by_length_and_digest():
    kit, laptop, ak = standard_devices("x")
    older = kit.data
    kit.custodian("laptop-2")
    newer = kit.data
    digest = "sha256-" + hashlib.sha256(older).hexdigest()
    check_extends(newer, byte_length=len(older), digest=digest)
    check_extends(newer, byte_length=0, digest="sha256-" + hashlib.sha256(b"").hexdigest())
    with pytest.raises(LogRollback):
        check_extends(older[:-5], byte_length=len(older), digest=digest)
    with pytest.raises(LogFork):
        check_extends(newer, byte_length=len(older), digest="sha256-" + "0" * 64)
    with pytest.raises(LogError, match="line"):
        check_extends(newer, byte_length=len(older) - 1,
                      digest="sha256-" + hashlib.sha256(older[:-1]).hexdigest())
    with pytest.raises(LogError):
        check_extends(newer, byte_length=True, digest=digest)


# ── Writing ──

def test_log_writer_appends_one_line_at_a_time(tmp_path):
    kit, laptop, ak = standard_devices("w")
    path = tmp_path / "devices.jsonl"
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        for line in kit.lines:
            assert w.next_idx == split_log(path.read_bytes()).__len__()
            w.append(line)
    assert path.read_bytes() == kit.data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    replay_devices(path.read_bytes(), ledger_id=LEDGER, root=kit.root.kid, now=NOW)


def test_log_writer_refuses_a_line_that_doesnt_fit_the_tail(tmp_path):
    kit, laptop, ak = standard_devices("w2")
    path = tmp_path / "devices.jsonl"
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        w.append(kit.lines[0])
        with pytest.raises(LogError, match="tail"):
            w.append(kit.lines[2])
        with pytest.raises(LogError, match="tail"):
            w.append(kit.lines[0])
        with pytest.raises(LogError):
            w.append(b"not json")
        with pytest.raises(LogError, match="kind"):
            w.append(canonicalize({"body": {**_body_of(kit.lines[1]), "kind": "readers-entry"}, "sigs": []}))
    assert path.read_bytes() == kit.lines[0] + b"\n"


def _body_of(line: bytes) -> dict:
    import json

    return json.loads(line)["body"]


def test_log_writer_refuses_time_going_backwards(tmp_path):
    kit, laptop, ak = standard_devices("w3")
    path = tmp_path / "devices.jsonl"
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        w.append(kit.lines[0])
        w.append(kit.lines[1])
    kit2 = DevicesKit("w3")
    kit2.lines = kit.lines[:2]
    kit2.clock = Clock(kit.clock.t - timedelta(hours=1))
    kit2.keys = kit.keys
    kit2.revoke(laptop, [kit2.root.kid])
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        with pytest.raises(LogError, match="backwards"):
            w.append(kit2.lines[-1])


def test_a_torn_tail_is_moved_to_forks_and_reported(tmp_path):
    kit, laptop, ak = standard_devices("t")
    path = tmp_path / "devices.jsonl"
    path.write_bytes(kit.lines[0] + b"\n" + kit.lines[1][:40])
    os.chmod(path, 0o600)
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks") as w:
        assert w.repaired == kit.lines[1][:40]
        assert w.next_idx == 1
        w.append(kit.lines[1])
    assert path.read_bytes() == kit.lines[0] + b"\n" + kit.lines[1] + b"\n"
    saved = list((tmp_path / "forks").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == kit.lines[1][:40]
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600


def test_the_writer_holds_an_exclusive_lock(tmp_path):
    path = tmp_path / "devices.jsonl"
    with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks"):
        fd = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_the_writer_refuses_a_symlinked_log(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_bytes(b"")
    (tmp_path / "devices.jsonl").symlink_to(real)
    with pytest.raises(LogError):
        with LogWriter(tmp_path / "devices.jsonl", kind="devices-entry", forks_dir=tmp_path / "forks"):
            pass


def test_the_writer_refuses_a_corrupt_middle(tmp_path):
    kit, laptop, ak = standard_devices("c")
    path = tmp_path / "devices.jsonl"
    path.write_bytes(kit.lines[0] + b"\nnot a line\n")
    os.chmod(path, 0o600)
    with pytest.raises(LogError):
        with LogWriter(path, kind="devices-entry", forks_dir=tmp_path / "forks"):
            pass


def test_signing_helpers_cover_both_key_types():
    kit, laptop, ak = standard_devices("s")
    body = _body_of(kit.lines[-1])
    s = sign_body("devices-entry", body, kit.keys[ak])
    assert s["alg"] == "fido2-es256" and s["key_id"] == ak


# ── Review round 1 (step 2.5b): hostile types never crash, the writer after a failure, the prefix check ──

def _all_rejections_are_log_errors(fn):
    try:
        fn()
    except LogError:
        return
    raise AssertionError("expected a LogError")


@pytest.mark.parametrize("where", ["class list", "class dict", "revokes objects", "origin list", "label list"])
def test_hostile_device_values_are_log_errors(where):
    kit, laptop, ak = standard_devices("hostile")
    if where == "revokes objects":
        kit.recovery([ak], [laptop], mutate=lambda b: b.update(revokes=[{"kid": laptop}]))
    else:
        def change(b):
            d = b["device"]
            if where == "class list":
                d["class"] = ["custodian"]
            elif where == "class dict":
                d["class"] = {}
            elif where == "origin list":
                d["webauthn"] = {"rp_id": ["x"], "origin": ["y"], "be": True, "bs": True, "cred_id": "x" * 22}
            else:
                d["label"] = ["laptop-2"]
        kit.custodian("laptop-2", mutate=change)
    _all_rejections_are_log_errors(lambda: replay(kit))


@pytest.mark.parametrize("where", ["surface", "viewing", "region", "scope_id", "types", "reader_id", "ids"])
def test_hostile_reader_values_are_log_errors(where):
    kit, rk, laptop, ak = _readers()
    if where == "scope_id":
        rk.enrol(READER, [laptop, ak])
        rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop], mutate=lambda b: b.update(scope_id=[]))
    elif where == "reader_id":
        rk.enrol(READER, [laptop, ak])
        rk.renew(READER, [laptop], mutate=lambda b: b.update(reader_id={"x": 1}))
    elif where in ("types", "ids"):
        s = scope()
        s["rule"][where] = [{"kid": "x"}]
        rk.enrol(READER, [laptop, ak], scopes=[s])
    else:
        rk.enrol(READER, [laptop, ak], mutate=lambda b: b["reader"].update({where: []}))
    _all_rejections_are_log_errors(lambda: rreplay(kit, rk))


def test_deep_nesting_in_a_line_is_a_log_error():
    kit, laptop, ak = standard_devices("deep")
    kit.revoke(laptop, [kit.root.kid], mutate=lambda b: b.update(kid=[[[[[[[[[[[]]]]]]]]]]]))
    _all_rejections_are_log_errors(lambda: replay(kit))


def test_a_forged_approver_with_deep_client_data_is_rejected_cleanly():
    import base64

    kit, rk, laptop, ak = _readers()
    from irp.roam import approver as A

    def forge(o):
        s = next(x for x in o["sigs"] if x["key_id"] == ak)
        inner = {"authenticator_data": sig.b64url_encode(hashlib.sha256(A.APPROVER_RP_ID.encode()).digest() + b"\x05" + bytes(4)),
                 "client_data_json": sig.b64url_encode(b"[" * 1500 + b"]" * 1500),
                 "signature": sig.b64url_encode(b"\x01" * 64)}
        s["sig"] = sig.b64url_encode(canonicalize(inner))
    rk.enrol(READER, [laptop, ak])
    import json as _j

    obj = _j.loads(rk.lines[-1])
    forge(obj)
    rk.lines[-1] = canonicalize(obj)
    assert base64
    _all_rejections_are_log_errors(lambda: rreplay(kit, rk))


# Readers-side step rules, mirroring the devices ones

def test_readers_at_never_goes_backwards():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    back = ts(kit.clock.t - timedelta(seconds=30))
    rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop], mutate=lambda b: b.update(at=back))
    rrejects(kit, rk, "backwards")


def test_devices_at_cant_name_a_devices_line_written_after_this_one():
    kit, rk, laptop, ak = _readers()
    early = ts(kit.clock.t - timedelta(seconds=30))
    rk.enrol(READER, [laptop, ak], mutate=lambda b: b.update(at=early))
    rrejects(kit, rk, "written after")


def test_a_readers_line_at_the_same_second_as_the_next_devices_line_isnt_the_tail():
    kit, rk, laptop, ak = _readers()
    old = kit.tail()
    kit.custodian("laptop-2")
    same = ts(kit.clock.t)
    rk.enrol(READER, [laptop, ak], devices_at=old, mutate=lambda b: b.update(at=same))
    rrejects(kit, rk, "tail")


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(idx=4), "idx"),
    (lambda b: b.update(ledger_id="ILID-" + "f" * 32), "ledger_id"),
    (lambda b: b["devices_at"].update(idx=True), "devices_at"),
    (lambda b: b.update(dry_run_digest="sha256-XYZ"), "dry_run_digest"),
    (lambda b: b.update(reviewed={"s1": ["has space"]}), "record id|ASCII"),
])
def test_readers_line_fields(change, match):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], mutate=change)
    rrejects(kit, rk, match)


def test_review_needs_a_well_formed_digest_and_ids():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop], mutate=lambda b: b.update(dry_run_digest="x"))
    rrejects(kit, rk, "dry_run_digest")


@pytest.mark.parametrize("event", ["renew", "rescope", "review"])
def test_an_expired_reader_cant_be_renewed_rescoped_or_reviewed(event):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=10)
    kit.clock.tick(20 * 86400)  # expired, but still inside 90 days of the approval
    {"renew": lambda: rk.renew(READER, [laptop], days=30),
     "rescope": lambda: rk.rescope(READER, [scope()], [laptop, ak]),
     "review": lambda: rk.review(READER, scope(), ["IRP-2001-10-01-001"], [laptop])}[event]()
    rrejects(kit, rk, "isn't active")


@pytest.mark.parametrize("event", ["revoked", "ended"])
def test_review_needs_an_active_reader(event):
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak])
    signer = laptop
    if event == "revoked":
        rk.revoke(READER, [laptop])
    else:
        signer = kit.recovery([ak], [laptop])
    rk.review(READER, scope(), ["IRP-2001-10-01-001"], [signer])
    rrejects(kit, rk, "isn't active")


def test_a_phone_cant_stand_in_for_the_approver():
    kit, rk, laptop, ak = _readers()
    phone = kit.companion("phone-1", via=[laptop, ak])
    rk.enrol(READER, [laptop, phone])
    rrejects(kit, rk, "signers")
    kit2, laptop2, ak2 = standard_devices("phone-as-approver")
    phone2 = kit2.companion("phone-1", via=[laptop2, ak2])
    kit2.rotate(laptop2, phone2)
    rejects(kit2, "signers")


# Devices-side rules the first round didn't pin

def test_an_approver_cant_rekey_itself_a_box():
    kit, laptop, ak = standard_devices()
    kit.add("device_rekey", {"kid": ak, "box": box("ak-box"), "nonce": None}, [ak])
    rejects(kit, "kid has the wrong format")


def test_a_rotation_cant_bring_in_a_companion():
    kit, laptop, ak = standard_devices()
    k = phone_key("rot-phone")
    kit.keys[k.kid] = k
    kit.add("device_rotate", {"old": laptop, "device": k.descriptor("laptop-1", box("rot-phone"))}, [laptop, ak, k.kid])
    rejects(kit, "can't add a companion")


def test_labels_stay_unique_on_recovery_and_approver_enrol():
    kit, laptop, ak = standard_devices()
    kit.recovery([ak], [laptop], new_label="hwkey-1")
    rejects(kit, "label")
    kit2, laptop2, ak2 = standard_devices("lbl2")
    kit2.approver("hwkey-1", via=[laptop2, ak2])
    rejects(kit2, "label")


def test_a_companion_enrol_needs_its_nonce():
    kit, laptop, ak = standard_devices()
    kit.companion("phone-1", via=[laptop, ak], mutate=lambda b: b.update(nonce=None))
    rejects(kit, "nonce")


@pytest.mark.parametrize("change,match", [
    (lambda b: b.update(epoch=2), "epoch"),
    (lambda b: b.update(epoch=True), "epoch"),
    (lambda b: b.update(checkpoint_ref="nope"), "checkpoint_ref"),
])
def test_root_rotate_fields(change, match):
    kit, laptop, ak = standard_devices()
    new = kit.root_rotate(mutate=change)
    rejects(kit, match, root=new)


def test_v_is_never_a_boolean():
    kit, laptop, ak = standard_devices()
    kit.revoke(laptop, [kit.root.kid], mutate=lambda b: b.update(v=True))
    rejects(kit, "v must be 1")


def test_a_companion_origin_sits_under_its_rp_id_at_a_dot():
    d = phone_key("dot").descriptor("phone-1", box("dot"))
    d["webauthn"]["origin"] = "https://evilirp.example"
    with pytest.raises(LogError, match="origin"):
        check_descriptor(d)
    d["webauthn"]["origin"] = "https://irp.example"
    check_descriptor(d)


# Boundaries

def test_reader_expiry_boundaries():
    kit, rk, laptop, ak = _readers()
    rk.enrol(READER, [laptop, ak], days=90)
    log = rreplay(kit, rk)
    exp = log.reader(READER).expires
    from datetime import datetime as _dt

    at_exp = _dt.strptime(exp, "%Y-%m-%dT%H:%M:%SZ")
    assert log.status(READER, now=at_exp - timedelta(seconds=1), epoch=0) == "active"
    assert log.status(READER, now=at_exp, epoch=0) == "expired"


def test_the_64_kib_edge():
    assert split_log(b"x" * (MAX_LINE - 1) + b"\n")
    with pytest.raises(LogError, match="64 KiB"):
        split_log(b"x" * MAX_LINE + b"\n")


def test_an_empty_devices_log_is_rejected_even_as_a_prefix():
    with pytest.raises(LogError, match="empty"):
        replay_devices(b"", ledger_id=LEDGER, root="rt-" + "0" * 32, now=NOW, prefix=True)


# The prefix check

def test_compare_copies_needs_a_whole_older_copy():
    kit, laptop, ak = standard_devices("whole")
    with pytest.raises(TornLine):
        compare_copies(kit.data[:100], kit.data)


def _root_fork_pair(forge_newer: bool):
    a, laptop, ak = standard_devices("rfp")
    base = list(a.lines)
    a.recovery([ak], [laptop])
    other = DevicesKit("rfp")
    other.lines, other.descs, other.keys = list(base), dict(a.descs), dict(a.keys)
    other.clock = Clock(a.clock.t)
    other.root = a.first_root
    other.root_rotate()
    if forge_newer:
        import json as _j

        obj = _j.loads(other.lines[-1])
        obj["sigs"] = sorted([{**s, "sig": sig.b64url_encode(b"\x02" * 64)} if s["key_id"].startswith("rt-") else s
                              for s in obj["sigs"]], key=lambda s: s["key_id"])
        other.lines[-1] = canonicalize(obj)
    return a, other


def test_a_root_fork_needs_both_root_signatures_to_verify():
    a, other = _root_fork_pair(forge_newer=False)
    older = replay(a)
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, other.data, devices=older)
    assert exc.value.root_fork
    a2, forged = _root_fork_pair(forge_newer=True)
    with pytest.raises(LogFork) as exc:
        compare_copies(a2.data, forged.data, devices=replay(a2))
    assert not exc.value.root_fork  # a relay can't cry "paper key compromised" with bytes it made up
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, other.data)  # without the replayed older copy, no root fork claim
    assert not exc.value.root_fork


def test_a_root_fork_needs_both_sides_root_signed():
    a, laptop, ak = standard_devices("one-sided")
    base = list(a.lines)
    a.recovery([ak], [laptop])
    other = DevicesKit("one-sided")
    other.lines, other.descs, other.keys = list(base), dict(a.descs), dict(a.keys)
    other.clock = Clock(a.clock.t)
    other.custodian("laptop-2")
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, other.data, devices=replay(a))
    assert exc.value.idx == 3 and not exc.value.root_fork


def test_a_hostile_newer_copy_never_crashes_the_prefix_check():
    a, laptop, ak = standard_devices("hostile-prefix")
    a.recovery([ak], [laptop])
    hostile = b"\n".join(a.lines[:3]) + b"\n" + b"[" * 1500 + b"]" * 1500 + b"\n"
    _all_rejections_are_log_errors(lambda: compare_copies(a.data, hostile, devices=replay(a)))


# The writer after a failure

def _open(tmp_path, kind="devices-entry"):
    return LogWriter(tmp_path / "devices.jsonl", kind=kind, forks_dir=tmp_path / "forks")


def test_a_short_write_is_rolled_back_and_the_writer_closes(tmp_path, monkeypatch):
    import irp.roam.logs as L

    kit, laptop, ak = standard_devices("short")
    real_write = os.write
    calls = {"n": 0}

    def short(fd, data):  # patched after the first append, so its first call is the second line
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:50])
        return real_write(fd, data)
    with _open(tmp_path) as w:
        w.append(kit.lines[0])
        monkeypatch.setattr(L.os, "write", short)
        with pytest.raises(LogError, match="partly written"):
            w.append(kit.lines[1])
        with pytest.raises(LogError, match="isn't open"):
            w.append(kit.lines[1])
    monkeypatch.setattr(L.os, "write", real_write)
    assert (tmp_path / "devices.jsonl").read_bytes() == kit.lines[0] + b"\n"
    with _open(tmp_path) as w:
        w.append(kit.lines[1])
        w.append(kit.lines[2])
    replay_devices((tmp_path / "devices.jsonl").read_bytes(), ledger_id=LEDGER, root=kit.root.kid, now=NOW)


def test_an_fsync_failure_is_rolled_back_and_reported_as_a_log_error(tmp_path, monkeypatch):
    import irp.roam.logs as L

    kit, laptop, ak = standard_devices("fsync")
    real = L._full_fsync
    calls = {"n": 0}

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "Input/output error")
        return real(fd)
    monkeypatch.setattr(L, "_full_fsync", flaky)
    with _open(tmp_path) as w:
        w.append(kit.lines[0])
        with pytest.raises(LogError, match="Input/output"):
            w.append(kit.lines[1])
    monkeypatch.setattr(L, "_full_fsync", real)
    assert (tmp_path / "devices.jsonl").read_bytes() == kit.lines[0] + b"\n"


def test_two_torn_repairs_keep_both_fragments(tmp_path):
    kit, laptop, ak = standard_devices("two-torn")
    path = tmp_path / "devices.jsonl"
    for frag in (b"FRAGMENT-ONE", b"FRAGMENT-TWO"):
        path.write_bytes(kit.lines[0] + b"\n" + frag)
        os.chmod(path, 0o600)
        with _open(tmp_path):
            pass
    saved = sorted(p.read_bytes() for p in (tmp_path / "forks").iterdir())
    assert saved == [b"FRAGMENT-ONE", b"FRAGMENT-TWO"]


def test_the_writer_refuses_an_oversized_line(tmp_path):
    with _open(tmp_path) as w:
        with pytest.raises(LogError, match="64 KiB"):
            w.append(b"{" + b" " * MAX_LINE + b"}")


def test_the_writer_refuses_a_file_that_doesnt_chain(tmp_path):
    kit, laptop, ak = standard_devices("nochain")
    path = tmp_path / "devices.jsonl"
    path.write_bytes(kit.lines[0] + b"\n" + kit.lines[2] + b"\n")
    os.chmod(path, 0o600)
    with pytest.raises(LogError, match="chain"):
        with _open(tmp_path):
            pass


def test_the_writer_tightens_a_loose_log_to_0600(tmp_path):
    path = tmp_path / "devices.jsonl"
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    with _open(tmp_path):
        pass
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("error", [TypeError, KeyError, RecursionError, AttributeError])
def test_any_slip_past_the_type_checks_still_fails_closed(monkeypatch, error):
    # The backstop: whatever a future rule forgets to type-check, replay rejects the log instead of crashing.
    import irp.roam.logs as L

    kit, laptop, ak = standard_devices("backstop")
    kit.revoke(laptop, [kit.root.kid])

    def boom(self, b):
        raise error("slipped")
    monkeypatch.setattr(L._DevicesReplay, "device_revoke", boom)
    with pytest.raises(LogError, match="malformed"):
        replay(kit)

    def boom_reader(self, b, at, epoch):
        raise error("slipped")
    monkeypatch.setattr(L._ReadersReplay, "reader_enrol", boom_reader)
    kit2, rk2, laptop2, ak2 = _readers()
    rk2.enrol(READER, [laptop2, ak2])
    with pytest.raises(LogError, match="malformed"):
        replay_readers(rk2.data, replay_devices(kit2.data, ledger_id=LEDGER, root=kit2.root.kid, now=NOW),
                       ledger_id=LEDGER, now=NOW)


@pytest.mark.parametrize("edit", ["strip co-signature", "add junk signature"])
def test_a_relay_cant_fake_a_root_fork_by_editing_signatures(edit):
    # Same root-signed body, different sig list: the bytes differ, but the root signed only one event.
    import json as _j

    a, laptop, ak = standard_devices("sigedit")
    a.recovery([ak], [laptop])
    obj = _j.loads(a.lines[-1])
    if edit == "strip co-signature":
        obj["sigs"] = [s for s in obj["sigs"] if s["key_id"].startswith("rt-")]
    else:
        obj["sigs"] = sorted(obj["sigs"] + [{"alg": "ed25519", "key_id": "dk-" + "f" * 32, "sig": "A" * 86}],
                             key=lambda s: s["key_id"])
    newer = b"".join(line + b"\n" for line in a.lines[:-1]) + canonicalize(obj) + b"\n"
    with pytest.raises(LogFork) as exc:
        compare_copies(a.data, newer, devices=replay(a))
    assert exc.value.idx == 3 and not exc.value.root_fork
