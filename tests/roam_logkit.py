"""Test helpers for Roaming IRP step 2.5b: a software FIDO2/WebAuthn authenticator and log builders.

Not collected by pytest (no test_ prefix). Real hardware keys come with gate 0.5; until then SoftKey makes
byte-exact assertions with deterministic ECDSA (RFC 6979), so fixed vectors can be regenerated and compared.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from irp.integrity.canonical import canonicalize
from irp.roam import approver, sig
from irp.roam.age import Identity

N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # P-256 group order
LEDGER = "ILID-" + "a1" * 16
T0 = datetime(2026, 10, 8, 9, 0, 0)
NOW = datetime(2026, 12, 31, 0, 0, 0)  # the verifier's clock in most tests
PHONE_RP = "irp.example"
PHONE_ORIGIN = "https://app.irp.example"


def h(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def line_hash(line: bytes) -> str:
    return "sha256-" + hashlib.sha256(line).hexdigest()


class SoftKey:
    """A software authenticator: P-256 key from a label, deterministic signatures, chosen flags."""

    def __init__(self, label: str, *, alg: str, rp_id: str, origin: str, be: bool = False, bs: bool = False):
        scalar = int.from_bytes(h("softkey/" + label), "big") % (N - 1) + 1
        self.priv = ec.derive_private_key(scalar, ec.SECP256R1())
        self.spki = self.priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        self.cred_id = h("cred/" + label)[:16]
        self.alg, self.rp_id, self.origin, self.be, self.bs = alg, rp_id, origin, be, bs
        self.kid = sig.key_id("ak" if alg == "fido2-es256" else "dk", self.spki, alg)
        self.flags = 0x05 | (0x08 if be else 0) | (0x10 if bs else 0)
        self.calls: list[tuple[str, bytes, bytes]] = []
        self.unchecked = False

    def der(self, message: bytes) -> bytes:
        return self.priv.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))

    # The Authenticator protocol approver.approve() calls.
    def get_assertion(self, rp_id: str, client_data_hash: bytes, cred_id: bytes) -> tuple[bytes, bytes]:
        self.calls.append((rp_id, client_data_hash, cred_id))
        ad = hashlib.sha256(rp_id.encode()).digest() + bytes([self.flags]) + (0).to_bytes(4, "big")
        return ad, self.der(ad + client_data_hash)

    def webauthn(self) -> dict[str, Any]:
        return {"rp_id": self.rp_id, "origin": self.origin, "be": self.be, "bs": self.bs,
                "cred_id": sig.b64url_encode(self.cred_id)}

    def descriptor(self, label: str, box: str | None = None) -> dict[str, Any]:
        cls = "approver" if self.alg == "fido2-es256" else "companion"
        return {"kid": self.kid, "class": cls, "alg": self.alg, "pub": sig.b64url_encode(self.spki),
                "box": box, "label": label,
                "key_scope": "account-synced" if self.be else "device-local", "webauthn": self.webauthn()}


def approver_key(label: str) -> SoftKey:
    return SoftKey(label, alg="fido2-es256", rp_id=approver.APPROVER_RP_ID, origin=approver.APPROVER_ORIGIN)


def phone_key(label: str, *, be: bool = True, bs: bool = True) -> SoftKey:
    return SoftKey(label, alg="webauthn-es256", rp_id=PHONE_RP, origin=PHONE_ORIGIN, be=be, bs=bs)


class EdKey:
    """An Ed25519 signer: a custodian device (dk-) or a root (rt-)."""

    def __init__(self, label: str, *, root: bool = False):
        self.seed = h("ed/" + label)
        self.pub = sig.public_key(self.seed)
        self.kid = sig.root_id(self.pub) if root else sig.key_id("dk", self.pub)

    def descriptor(self, label: str, box: str) -> dict[str, Any]:
        return {"kid": self.kid, "class": "custodian", "alg": "ed25519", "pub": sig.b64url_encode(self.pub),
                "box": box, "label": label, "key_scope": "device-local", "webauthn": None}


def box(label: str) -> str:
    return Identity(h("box/" + label)).recipient().to_string()


class Clock:
    def __init__(self, start: datetime = T0):
        self.t = start

    def tick(self, seconds: int = 60) -> str:
        self.t += timedelta(seconds=seconds)
        return ts(self.t)


class DevicesKit:
    """Builds a valid devices.jsonl step by step. Every helper takes `mutate` (edit the body before signing),
    `signers` (override who signs) and `extra` (raw sig objects to add) for negative tests."""

    def __init__(self, label: str = "kit", clock: Clock | None = None):
        self.label = label
        self.clock = clock or Clock()
        self.root = EdKey(label + "/root/0", root=True)
        self.first_root = self.root
        self.roots_used = 0
        self.keys: dict[str, Any] = {self.root.kid: self.root}
        self.descs: dict[str, dict[str, Any]] = {}
        self.lines: list[bytes] = []
        self.epoch = 0

    # ── plumbing ──
    @property
    def data(self) -> bytes:
        return b"".join(line + b"\n" for line in self.lines)

    def tail(self) -> dict[str, Any]:
        return {"idx": len(self.lines) - 1, "line": line_hash(self.lines[-1])}

    def body(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {"v": 1, "kind": "devices-entry", "event": event, "ledger_id": LEDGER, "root": self.root.kid,
                "idx": len(self.lines), "prev": line_hash(self.lines[-1]) if self.lines else None,
                "at": self.clock.tick(), **fields}

    def sign_with(self, kid: str, body: dict[str, Any]) -> dict[str, str]:
        return sign_body("devices-entry", body, self.keys[kid])

    def add(self, event: str, fields: dict[str, Any], signers: list[str], *, mutate: Callable | None = None,
            extra: list[dict] | None = None, raw: Callable | None = None) -> bytes:
        body = self.body(event, fields)
        if mutate:
            mutate(body)
        sigs = [self.sign_with(k, body) for k in signers] + list(extra or [])
        obj = {"body": body, "sigs": sorted(sigs, key=lambda s: s["key_id"])}
        if raw:
            raw(obj)
        line = canonicalize(obj)
        self.lines.append(line)
        return line

    # ── events ──
    def genesis(self, **kw) -> None:
        self.add("genesis", {"root_pub": sig.b64url_encode(self.root.pub), "epoch": 0}, [self.root.kid], **kw)

    def custodian(self, label: str = "laptop-1", *, signers: list[str] | None = None, key: EdKey | None = None,
                  box_label: str | None = None, **kw) -> str:
        k = key or EdKey(f"{self.label}/{label}/{len(self.lines)}")
        self.keys[k.kid] = k
        d = k.descriptor(label, box(box_label or f"{self.label}/{label}/{len(self.lines)}"))
        self.add("device_enrol", {"device": d, "nonce": None}, signers or [self.root.kid, k.kid], **kw)
        self.descs[k.kid] = d
        return k.kid

    def approver(self, label: str = "hwkey-1", *, via: list[str] | None = None, key: SoftKey | None = None,
                 **kw) -> str:
        k = key or approver_key(f"{self.label}/{label}/{len(self.lines)}")
        self.keys[k.kid] = k
        d = k.descriptor(label)
        self.add("approver_enrol", {"approver": d}, (via or [self.root.kid]) + [k.kid], **kw)
        self.descs[k.kid] = d
        return k.kid

    def companion(self, label: str = "phone-1", *, via: list[str], key: SoftKey | None = None,
                  nonce: str | None = None, **kw) -> str:
        k = key or phone_key(f"{self.label}/{label}/{len(self.lines)}")
        self.keys[k.kid] = k
        d = k.descriptor(label, box(f"{self.label}/{label}/{len(self.lines)}"))
        n = nonce if nonce is not None else sig.b64url_encode(h(f"nonce/{len(self.lines)}")[:16])
        self.add("device_enrol", {"device": d, "nonce": n}, via + [k.kid], **kw)
        self.descs[k.kid] = d
        return k.kid

    def rotate(self, old: str, approver_kid: str, **kw) -> str:
        label = self.descs[old]["label"]
        k = EdKey(f"{self.label}/{label}/rot/{len(self.lines)}")
        self.keys[k.kid] = k
        d = k.descriptor(label, box(f"{self.label}/{label}/rot/{len(self.lines)}"))
        self.add("device_rotate", {"old": old, "device": d}, [old, approver_kid, k.kid], **kw)
        self.descs[k.kid] = d
        return k.kid

    def rekey(self, kid: str, *, signers: list[str] | None = None, nonce: Any = "auto", **kw) -> None:
        companion = self.descs[kid]["class"] == "companion"
        new_box = box(f"{self.label}/rekey/{kid}/{len(self.lines)}")
        if nonce == "auto":
            nonce = sig.b64url_encode(h(f"rekey-nonce/{len(self.lines)}")[:16]) if companion else None
        self.add("device_rekey", {"kid": kid, "box": new_box, "nonce": nonce}, signers or [kid], **kw)
        self.descs[kid] = {**self.descs[kid], "box": new_box}

    def revoke(self, kid: str, signers: list[str], **kw) -> None:
        self.add("device_revoke", {"kid": kid}, signers, **kw)

    def approver_revoke(self, kid: str, signers: list[str], **kw) -> None:
        self.add("approver_revoke", {"kid": kid}, signers, **kw)

    def recovery(self, keep: list[str], revokes: list[str], new_label: str = "laptop-9",
                 checkpoint_ref: str | None = None, **kw) -> str:
        k = EdKey(f"{self.label}/{new_label}/recovery/{len(self.lines)}")
        self.keys[k.kid] = k
        d = k.descriptor(new_label, box(f"{self.label}/{new_label}/recovery/{len(self.lines)}"))
        active = sorted([self.descs[x] for x in keep] + [d], key=lambda x: x["kid"])
        self.add("recovery", {"active": active, "revokes": sorted(revokes), "new_device": k.kid,
                              "epoch": self.epoch + 1, "checkpoint_ref": checkpoint_ref},
                 [self.root.kid, k.kid], **kw)
        self.descs[k.kid] = d
        self.epoch += 1
        return k.kid

    def root_rotate(self, **kw) -> str:
        self.roots_used += 1
        new = EdKey(f"{self.label}/root/{self.roots_used}", root=True)
        self.keys[new.kid] = new
        self.add("root_rotate", {"root_pub": sig.b64url_encode(new.pub), "epoch": self.epoch + 1,
                                 "checkpoint_ref": None}, [self.root.kid, new.kid], **kw)
        self.root = new
        self.epoch += 1
        return new.kid


def sign_body(kind: str, body: dict[str, Any], key: Any) -> dict[str, str]:
    data = canonicalize(body)
    if isinstance(key, EdKey):
        return sig.sign(kind, data, key.seed, key.kid)
    if key.unchecked:  # skip approve()'s own check, to build lines a verifier must refuse
        cdj = approver.client_data_json(sig.signing_input(kind, data), key.origin)
        ad, der = key.get_assertion(key.rp_id, hashlib.sha256(cdj).digest(), key.cred_id)
        return {"alg": key.alg, "key_id": key.kid, "sig": approver.pack(ad, cdj, der)}
    return approver.approve(kind, data, key.descriptor("x-1", None), key)


def scope(scope_id: str = "s1", **rule_overrides: Any) -> dict[str, Any]:
    rule = {"ids": ["IRP-2001-10-01-001"], "tags_any": [], "types": [], "since": None, "limit": None,
            "ancestor_depth": 2, "pinned": [], "token_budget": 8000, "byte_budget": 65536}
    rule.update(rule_overrides)
    return {"scope_id": scope_id, "rule": rule,
            "rule_digest": "sha256-" + hashlib.sha256(canonicalize(rule)).hexdigest()}


class ReadersKit:
    """Builds readers.jsonl against a DevicesKit, citing its tail and sharing its clock."""

    def __init__(self, dev: DevicesKit):
        self.dev = dev
        self.lines: list[bytes] = []

    @property
    def data(self) -> bytes:
        return b"".join(line + b"\n" for line in self.lines)

    def add(self, event: str, fields: dict[str, Any], signers: list[str], *, mutate: Callable | None = None,
            devices_at: dict | None = None, extra: list[dict] | None = None) -> bytes:
        body = {"v": 1, "kind": "readers-entry", "event": event, "ledger_id": LEDGER, "root": self.dev.root.kid,
                "idx": len(self.lines), "prev": line_hash(self.lines[-1]) if self.lines else None,
                "at": self.dev.clock.tick(), "devices_at": devices_at or self.dev.tail(), **fields}
        if mutate:
            mutate(body)
        sigs = [sign_body("readers-entry", body, self.dev.keys[k]) for k in signers] + list(extra or [])
        line = canonicalize({"body": body, "sigs": sorted(sigs, key=lambda s: s["key_id"])})
        self.lines.append(line)
        return line

    def enrol(self, reader_id: str, signers: list[str], *, scopes: list[dict] | None = None, days: int = 30,
              recipient: str | None = None, reviewed: dict | None = None, **kw) -> None:
        scopes = scopes or [scope()]
        at = self.dev.clock.t + timedelta(seconds=60)
        reader = {"reader_id": reader_id, "surface": "claude-code-cloud", "region": "non-eu",
                  "viewing": "personal-device", "identity_assurance": "A0",
                  "recipient": recipient or box("reader/" + reader_id), "scopes": scopes,
                  "expires": ts(at + timedelta(days=days))}
        rv = reviewed if reviewed is not None else {s["scope_id"]: list(s["rule"]["ids"]) for s in scopes}
        self.add("reader_enrol", {"reader": reader, "dry_run_digest": "sha256-" + "d" * 64, "reviewed": rv},
                 signers, **kw)

    def rescope(self, reader_id: str, scopes: list[dict], signers: list[str], *, reviewed: dict | None = None,
                **kw) -> None:
        rv = reviewed if reviewed is not None else {s["scope_id"]: [] for s in scopes}
        self.add("reader_scope", {"reader_id": reader_id, "scopes": scopes, "dry_run_digest": "sha256-" + "e" * 64,
                                  "reviewed": rv}, signers, **kw)

    def renew(self, reader_id: str, signers: list[str], *, days: int = 30, recipient: str | None = None,
              **kw) -> None:
        at = self.dev.clock.t + timedelta(seconds=60)
        self.add("reader_renew", {"reader_id": reader_id,
                                  "recipient": recipient or box(f"reader/{reader_id}/{len(self.lines)}"),
                                  "expires": ts(at + timedelta(days=days))}, signers, **kw)

    def review(self, reader_id: str, scope_obj: dict, ids: list[str], signers: list[str], **kw) -> None:
        self.add("reader_review", {"reader_id": reader_id, "scope_id": scope_obj["scope_id"],
                                   "rule_digest": scope_obj["rule_digest"], "reviewed_ids": sorted(ids),
                                   "dry_run_digest": "sha256-" + "f" * 64}, signers, **kw)

    def revoke(self, reader_id: str, signers: list[str], **kw) -> None:
        self.add("reader_revoke", {"reader_id": reader_id}, signers, **kw)


def standard_devices(label: str = "std") -> tuple[DevicesKit, str, str]:
    """genesis, a custodian laptop and an approver enrolled by the root: the state after irp roam init."""
    kit = DevicesKit(label)
    kit.genesis()
    laptop = kit.custodian("laptop-1")
    ak = kit.approver("hwkey-1")
    return kit, laptop, ak
