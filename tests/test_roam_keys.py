"""Roaming IRP, Cut 1 step 2.5a: derivations and the keystore (spec v0.3 §14.1, §14.2, §14.4).

Everything derives from one paper key (an age identity) with HKDF and the
`irp-roam/v1/` prefix, so the root signer and the custodian chain and MAC keys
can always be rebuilt from the sheet. The laptop's working keys live in one
AES-256-GCM keystore whose master key (KEK) sits in the login Keychain (Mode A),
under a stock `age -p` passphrase (Mode B), or, for tests and non-macOS only, in
a 0600 file with a loud warning. No secret ever appears in a command line.

The real login Keychain is never touched by these tests: Mode A runs against a
fake `security` that records every call.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pty
import select
import shutil
import stat
import sys
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("cryptography")
pytest.importorskip("rfc8785")

from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import sig  # noqa: E402
from irp.roam.age import Identity  # noqa: E402
from irp.roam.keys import (  # noqa: E402
    KEYSTORE_AAD,
    FileKek,
    KeychainKek,
    Keystore,
    KeystoreError,
    PassphraseKek,
    RoamKeyError,
    RoamKeyWarning,
    custodian_chain_key,
    custodian_mac_key,
    hk,
    load_keystore,
    new_keystore,
    open_keystore,
    outbox_key,
    phone_feed_key,
    reader_chain_key,
    rk_from_identity,
    root_id_for,
    root_public_key,
    root_seed,
    save_keystore,
    seal_keystore,
    set_kek,
)

STOCK_AGE = shutil.which("age")
REQUIRE_STOCK_AGE = os.environ.get("IRP_REQUIRE_STOCK_AGE") == "1"

RK = bytes.fromhex("9d2c16dd2cda2eabcb7e063858d1498a7b57c991a21f7db3838ba11150db1306")  # sha256("irp-roam test paper key")
GOLDEN = {
    "RS": "b6481f1007c90cf39e1756cd5eb6128bb1d203364b0446dc2f4a83ae9ed34cca",
    "ROOT_PUB": "59723b0dff2a66c27e88dafb8dcdf1a07525dcd3e06150f1076271ec75276c92",
    "ROOT_ID": "rt-62a55b4eb5e369f8a1e0c42fd3ee362e",
    "KC0": "a43283d14ed229918c785170b63e02a15771befcafc0403eeef00b019d0d0134",
    "KA0": "4638bde7e240f7d34bd4e45f13a9e8d5f157615ab15c187050d5d0d630f1a911",
    "KC1": "cb8fe37a37f8372a1e6f71e6b58e4117c8669f4f2ebfba1747f869da52b2d577",
    "KR": "d3f5835122231fa0eda7afeabdedf10edfc6a74b2b4b25b4eabfa0fe71d3753e",
    "KP": "95c7f01efe6c2d837800b7d9874860d5aeffa42bcbffc7b9c7cc811fb7c6a2e2",
    "KO": "c0031d219d1ff4200a571335f49abeedf0ad54986629144022cc7df813924523",
}
READER = "rd-" + "b2" * 16
KID = "dk-" + "c3" * 16
LEDGER_ID = "ILID-" + "a1" * 16


def _rng(label: str):
    state = {"n": 0}

    def rng(n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += hashlib.sha256(label.encode() + state["n"].to_bytes(8, "big")).digest()
            state["n"] += 1
        return out[:n]

    return rng


def _stdlib_hkdf(ikm: bytes, label: str) -> bytes:
    """RFC 5869 with hmac only: an independent check of hk(), as tools/roam_ref.py will need."""
    prk = hmac.new(b"irp-roam/v1", ikm, hashlib.sha256).digest()
    return hmac.new(prk, b"irp-roam/v1/" + label.encode() + b"\x01", hashlib.sha256).digest()


# ── Derivations (§14.1) ──

def test_hk_is_rfc5869_with_the_roam_salt_and_prefix():
    for label in ("root-ed25519/0", "custodian-chain/0", "custodian-mac/7", "reader-chain/" + READER):
        assert hk(RK, label) == _stdlib_hkdf(RK, label)


def test_golden_derivations():
    assert root_seed(RK).hex() == GOLDEN["RS"]
    assert root_public_key(RK).hex() == GOLDEN["ROOT_PUB"]
    assert root_id_for(RK) == GOLDEN["ROOT_ID"] == sig.root_id(root_public_key(RK))
    assert custodian_chain_key(RK, 0).hex() == GOLDEN["KC0"]
    assert custodian_mac_key(RK, 0).hex() == GOLDEN["KA0"]
    assert custodian_chain_key(RK, 1).hex() == GOLDEN["KC1"]
    kc0 = custodian_chain_key(RK, 0)
    assert reader_chain_key(kc0, READER).hex() == GOLDEN["KR"]
    assert phone_feed_key(kc0, KID).hex() == GOLDEN["KP"]
    assert outbox_key(kc0, KID).hex() == GOLDEN["KO"]


def test_the_paper_key_is_the_raw_identity_payload():
    paper = Identity(RK).to_string()
    assert paper.startswith("AGE-SECRET-KEY-1")
    assert rk_from_identity(paper) == RK
    for bad in (paper.lower(), Identity(RK).recipient().to_string(), "AGE-SECRET-KEY-1", 42, None):
        with pytest.raises(RoamKeyError):
            rk_from_identity(bad)


def test_the_root_signer_can_sign_genesis():
    s = sig.sign("devices-entry", b'{"event":"genesis"}', root_seed(RK), root_id_for(RK))
    sig.verify("devices-entry", b'{"event":"genesis"}', s, root_public_key(RK))


def test_keys_are_distinct_across_epochs_ids_and_purposes():
    kc0 = custodian_chain_key(RK, 0)
    values = [root_seed(RK), kc0, custodian_mac_key(RK, 0), custodian_chain_key(RK, 1), custodian_mac_key(RK, 1),
              reader_chain_key(kc0, READER), reader_chain_key(kc0, "rd-" + "d4" * 16), phone_feed_key(kc0, KID),
              outbox_key(kc0, KID)]
    assert len(set(values)) == len(values)
    assert all(len(v) == 32 for v in values)


@pytest.mark.parametrize("call", [
    lambda: custodian_chain_key(RK, -1),
    lambda: custodian_chain_key(RK, True),
    lambda: custodian_chain_key(RK, "0"),
    lambda: custodian_mac_key(RK, 1.0),
    lambda: custodian_chain_key(RK[:31], 0),
    lambda: custodian_chain_key(RK.hex(), 0),
    lambda: reader_chain_key(RK, "rd-1"),
    lambda: reader_chain_key(RK, "dk-" + "c3" * 16),
    lambda: phone_feed_key(RK, "rd-" + "b2" * 16),
    lambda: outbox_key(RK, "dk-" + "C3" * 16),
    lambda: hk(RK, "no\x00nul"),
    lambda: hk(RK, "é"),
    lambda: root_seed(b""),
])
def test_derivation_inputs_are_checked(call):
    with pytest.raises(RoamKeyError):
        call()


# ── The keystore (§14.4) ──

def _ks(label="ks", source="file", tsa=None) -> Keystore:
    return new_keystore(RK, _rng(label), kek_source=source, tsa_creds=tsa)


def test_new_keystore_derives_the_epoch_keys_and_makes_fresh_device_keys():
    ks = _ks()
    assert ks.epochs == {0: (custodian_chain_key(RK, 0), custodian_mac_key(RK, 0))}
    assert len({ks.dk_seed, ks.dk_box, ks.ck_seed}) == 3
    assert all(len(k) == 32 for k in (ks.dk_seed, ks.dk_box, ks.ck_seed))
    assert ks.dk_id == sig.key_id("dk", sig.public_key(ks.dk_seed))
    assert ks.ck_id == sig.key_id("ck", sig.public_key(ks.ck_seed))
    assert ks.dk_recipient == Identity(ks.dk_box).recipient().to_string()
    assert ks.kek_source == "file" and ks.tsa_creds is None


def test_seal_and_open_round_trip():
    ks = _ks(tsa={"disig": "user:secret-token"})
    kek = _rng("kek")(32)
    blob = seal_keystore(ks, kek, _rng("nonce"))
    assert open_keystore(blob, kek) == ks
    assert len(blob) > 12 + 16


def test_the_wrong_kek_fails():
    blob = seal_keystore(_ks(), _rng("kek")(32), _rng("nonce"))
    with pytest.raises(KeystoreError):
        open_keystore(blob, _rng("other kek")(32))


@pytest.mark.parametrize("where", [0, 11, 12, 40, -17, -1])
def test_any_changed_byte_fails(where):
    kek = _rng("kek")(32)
    blob = bytearray(seal_keystore(_ks(), kek, _rng("nonce")))
    blob[where] ^= 0x01
    with pytest.raises(KeystoreError):
        open_keystore(bytes(blob), kek)


@pytest.mark.parametrize("cut", [0, 11, 12, 28, -1])
def test_a_truncated_keystore_fails(cut):
    kek = _rng("kek")(32)
    blob = seal_keystore(_ks(), kek, _rng("nonce"))
    with pytest.raises(KeystoreError):
        open_keystore(blob[:cut] if cut >= 0 else blob[:cut], kek)


def test_each_seal_uses_a_fresh_nonce():
    kek = _rng("kek")(32)
    a = seal_keystore(_ks(), kek, os.urandom)
    b = seal_keystore(_ks(), kek, os.urandom)
    assert a[:12] != b[:12] and a != b


def _raw_seal(payload: bytes, kek: bytes, aad: bytes = KEYSTORE_AAD) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = _rng("raw nonce")(12)
    return nonce + AESGCM(kek).encrypt(nonce, payload, aad)


def test_the_aad_is_bound():
    kek = _rng("kek")(32)
    blob = seal_keystore(_ks(), kek, _rng("nonce"))
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = AESGCM(kek).decrypt(blob[:12], blob[12:], KEYSTORE_AAD)
    assert KEYSTORE_AAD == b"irp-roam/v1/keystore"
    open_keystore(_raw_seal(payload, kek), kek)
    with pytest.raises(KeystoreError):
        open_keystore(_raw_seal(payload, kek, b"irp-roam/v2/keystore"), kek)


def _content(ks: Keystore) -> dict:
    kek = _rng("kek")(32)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = seal_keystore(ks, kek, _rng("nonce"))
    return json.loads(AESGCM(kek).decrypt(blob[:12], blob[12:], KEYSTORE_AAD))


def test_the_sealed_content_is_the_spec_object():
    c = _content(_ks())
    assert set(c) == {"kek_source", "dk_seed", "dk_box", "ck_seed", "epochs", "tsa_creds"}
    assert set(c["epochs"]) == {"0"} and set(c["epochs"]["0"]) == {"kc", "ka"}
    assert sig.b64url_decode(c["epochs"]["0"]["kc"], 32) == custodian_chain_key(RK, 0)


CONTENT_MUTATIONS = {
    "unknown key": lambda c: c.update(extra=1),
    "missing ck_seed": lambda c: c.pop("ck_seed"),
    "kek_source unknown": lambda c: c.update(kek_source="touch-id"),
    "dk_seed short": lambda c: c.update(dk_seed=sig.b64url_encode(b"\x01" * 31)),
    "dk_box padded": lambda c: c.update(dk_box=c["dk_box"] + "="),
    "ck_seed not text": lambda c: c.update(ck_seed=7),
    "epochs empty": lambda c: c.update(epochs={}),
    "epoch leading zero": lambda c: c.update(epochs={"00": c["epochs"]["0"]}),
    "epoch negative": lambda c: c.update(epochs={"-1": c["epochs"]["0"]}),
    "epoch extra key": lambda c: c["epochs"]["0"].update(kr="x"),
    "epoch missing ka": lambda c: c["epochs"]["0"].pop("ka"),
    "tsa_creds list": lambda c: c.update(tsa_creds=["x"]),
    "tsa_creds non-text value": lambda c: c.update(tsa_creds={"disig": 5}),
    "same seed twice": lambda c: c.update(ck_seed=c["dk_seed"]),
}


@pytest.mark.parametrize("name", sorted(CONTENT_MUTATIONS))
def test_the_sealed_content_is_a_closed_schema(name):
    c = _content(_ks())
    CONTENT_MUTATIONS[name](c)
    kek = _rng("kek")(32)
    with pytest.raises(KeystoreError):
        open_keystore(_raw_seal(canonicalize(c), kek), kek)


def test_non_jcs_content_is_rejected():
    c = _content(_ks())
    kek = _rng("kek")(32)
    with pytest.raises(KeystoreError):
        open_keystore(_raw_seal(json.dumps(c).encode(), kek), kek)


def test_repr_never_shows_a_secret():
    ks = _ks(tsa={"disig": "user:secret-token"})
    text = repr(ks) + str(ks)
    for secret in (ks.dk_seed, ks.dk_box, ks.ck_seed, *ks.epochs[0]):
        assert secret.hex() not in text and sig.b64url_encode(secret) not in text and repr(secret) not in text
    assert "secret-token" not in text
    for name in ("dk_seed=", "dk_box=", "ck_seed=", "epochs=", "tsa_creds="):
        assert name not in text


# ── Saving, loading and the three KEK sources ──

def _opts(tokens):
    out, i = {}, 0
    while i < len(tokens):
        if tokens[i] in ("-s", "-a", "-w") and i + 1 < len(tokens):
            out[tokens[i]] = tokens[i + 1]
            i += 2
        else:
            out[tokens[i]] = True
            i += 1
    return out


class FakeSecurity:
    """Stands in for /usr/bin/security. Records every call; never touches the real Keychain."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv, data=b""):
        self.calls.append((list(argv), data))
        assert argv[0] == "/usr/bin/security"  # never whatever `security` comes first on PATH
        if argv[1:] == ["-i"]:
            for line in data.decode().splitlines():
                parts = line.split()
                if parts and parts[0] == "add-generic-password":
                    o = _opts(parts[1:])
                    if (o["-s"], o["-a"]) in self.items and o.get("-U") is not True:
                        raise KeystoreError("security: The specified item already exists in the keychain.")
                    self.items[(o["-s"], o["-a"])] = o["-w"]
            return b""
        o = _opts(argv[2:])
        key = (o.get("-s"), o.get("-a"))
        if argv[1] == "find-generic-password":
            if key not in self.items:
                raise KeystoreError("security: the specified item could not be found in the keychain")
            return (self.items[key] + "\n").encode() if o.get("-w") is True else b"found"
        if argv[1] == "delete-generic-password":
            self.items.pop(key, None)
            return b""
        raise AssertionError(f"unexpected security call {argv}")


def _assert_no_secret_in_argv(calls, secrets):
    for argv, _ in calls:
        joined = " ".join(argv)
        for s in secrets:
            for form in (s.hex(), s.hex().upper(), sig.b64url_encode(s)):
                assert form not in joined


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_file_mode_round_trip_with_a_loud_warning(tmp_path):
    keys = tmp_path / "keys"
    ks = _ks(source="file")
    with pytest.warns(RoamKeyWarning):
        save_keystore(keys, ks, FileKek(keys), _rng("save"))
    assert _mode(keys) == 0o700 and _mode(keys / "keystore.bin") == 0o600 and _mode(keys / "kek.bin") == 0o600
    with pytest.warns(RoamKeyWarning):
        assert load_keystore(keys, [FileKek(keys)]) == ks


def test_files_are_private_whatever_the_umask(tmp_path):
    old = os.umask(0)
    try:
        keys = tmp_path / "keys"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RoamKeyWarning)
            save_keystore(keys, _ks(), FileKek(keys), _rng("save"))
    finally:
        os.umask(old)
    assert _mode(keys) == 0o700
    assert all(_mode(p) == 0o600 for p in keys.iterdir())


def test_keychain_mode_round_trip_without_a_secret_in_argv(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    ks = _ks(source="keychain")
    kc = KeychainKek(LEDGER_ID, run=sec)
    save_keystore(keys, ks, kc, _rng("save"))
    assert load_keystore(keys, [kc]) == ks
    assert sec.items and list(sec.items) == [("irp-roam", LEDGER_ID)]
    kek = bytes.fromhex(sec.items[("irp-roam", LEDGER_ID)])
    assert len(kek) == 32
    _assert_no_secret_in_argv(sec.calls, [kek])
    adds = [c for c in sec.calls if c[0] == ["/usr/bin/security", "-i"]]
    assert adds and kek.hex().encode() in adds[0][1]  # the secret travels on stdin only
    assert ["/usr/bin/security", "find-generic-password", "-s", "irp-roam", "-a", LEDGER_ID, "-w"] in \
        [c[0] for c in sec.calls]
    assert not (keys / "kek.bin").exists() and not (keys / "kek.age").exists()


def test_keychain_mode_without_its_item_fails(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    save_keystore(keys, _ks(source="keychain"), KeychainKek(LEDGER_ID, run=sec), _rng("save"))
    sec.items.clear()
    with pytest.raises(KeystoreError):
        load_keystore(keys, [KeychainKek(LEDGER_ID, run=sec)])


def test_keychain_needs_a_ledger_id():
    for bad in ("ILID-1", "", None, "ILID-" + "A1" * 16):
        with pytest.raises(RoamKeyError):
            KeychainKek(bad, run=FakeSecurity())


class FakeAge:
    """A stand-in for stock `age -p` that records argv; the stock round trip is tested separately."""

    def __init__(self, passphrase=b"correct horse"):
        self.passphrase = passphrase
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv, data=b""):
        self.calls.append((list(argv), data))
        if argv == ["age", "-p"]:
            return b"age-encryption.org/v1\nFAKE:" + self.passphrase + b":" + data
        if argv[:2] == ["age", "-d"] and len(argv) == 3:
            blob = Path(argv[2]).read_bytes()
            head, pw, body = blob.split(b":", 2)
            if pw != self.passphrase:
                raise KeystoreError("age: incorrect passphrase")
            return body
        raise AssertionError(f"unexpected age call {argv}")


def test_passphrase_mode_logic_with_a_fake_age(tmp_path):
    keys, age = tmp_path / "keys", FakeAge()
    ks = _ks(source="passphrase")
    save_keystore(keys, ks, PassphraseKek(keys, run=age), _rng("save"))
    assert _mode(keys / "kek.age") == 0o600
    assert load_keystore(keys, [PassphraseKek(keys, run=age)]) == ks
    kek = bytes.fromhex(age.calls[0][1].decode())
    _assert_no_secret_in_argv(age.calls, [kek])
    age.passphrase = b"wrong"
    with pytest.raises(KeystoreError):
        load_keystore(keys, [PassphraseKek(keys, run=age)])


def _age_with_tty(passphrase: bytes):
    """A runner for stock age: stdin and stdout are pipes, and a pseudo-terminal answers the passphrase prompt,
    as a person typing at the Air would."""

    def run(argv, data=b""):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        pid, master = pty.fork()
        if pid == 0:  # pragma: no cover - child
            os.dup2(in_r, 0)
            os.dup2(out_w, 1)
            for fd in (in_r, in_w, out_r, out_w):
                os.close(fd)
            os.execvp(argv[0], argv)
        os.close(in_r)
        os.close(out_w)
        os.write(in_w, data)
        os.close(in_w)
        term, out, answered, deadline = b"", b"", 0, time.time() + 30
        while True:
            if time.time() > deadline:  # never let a stuck child hang the suite
                os.kill(pid, 9)
                break
            ready, _, _ = select.select([master, out_r], [], [], 0.2)
            if master in ready:
                try:
                    term += os.read(master, 1024)
                except OSError:
                    pass
                if term.count(b"assphrase") > answered:
                    answered = term.count(b"assphrase")
                    os.write(master, passphrase + b"\n")
            if out_r in ready:
                chunk = os.read(out_r, 65536)
                if not chunk:
                    break
                out += chunk
        _, status = os.waitpid(pid, 0)
        os.close(master)
        os.close(out_r)
        if not os.WIFEXITED(status):
            pytest.fail(f"{argv[0]} was killed or timed out; terminal said {term[-200:]!r}")
        if os.WEXITSTATUS(status) != 0:
            raise KeystoreError("age failed")
        return out

    return run


@pytest.fixture
def stock_age():
    if not STOCK_AGE:
        if REQUIRE_STOCK_AGE:
            pytest.fail("IRP_REQUIRE_STOCK_AGE=1 but stock age isn't installed")
        pytest.skip("stock age isn't installed")
    return STOCK_AGE


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_passphrase_mode_with_stock_age(tmp_path, stock_age):
    keys = tmp_path / "keys"
    ks = _ks(source="passphrase")
    save_keystore(keys, ks, PassphraseKek(keys, run=_age_with_tty(b"correct horse")), _rng("save"))
    blob = (keys / "kek.age").read_bytes()
    assert blob.startswith(b"age-encryption.org/v1\n-> scrypt ")
    assert _mode(keys / "kek.age") == 0o600
    assert load_keystore(keys, [PassphraseKek(keys, run=_age_with_tty(b"correct horse"))]) == ks
    with pytest.raises(KeystoreError):
        load_keystore(keys, [PassphraseKek(keys, run=_age_with_tty(b"wrong horse"))])
    # The sheet-check path: stock `age -d` alone gives back the KEK as hex.
    kek_hex = _age_with_tty(b"correct horse")(["age", "-d", str(keys / "kek.age")])
    assert len(bytes.fromhex(kek_hex.decode())) == 32


# ── Switching modes (set-kek) ──

def _sources(keys, sec, age):
    return [PassphraseKek(keys, run=age), FileKek(keys), KeychainKek(LEDGER_ID, run=sec)]


def test_set_kek_rewraps_the_same_keystore(tmp_path):
    keys, sec, age = tmp_path / "keys", FakeSecurity(), FakeAge()
    ks = _ks(source="keychain")
    save_keystore(keys, ks, KeychainKek(LEDGER_ID, run=sec), _rng("save"))
    for new in (PassphraseKek(keys, run=age), FileKek(keys), KeychainKek(LEDGER_ID, run=sec)):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RoamKeyWarning)
            after = set_kek(keys, _sources(keys, sec, age), new, _rng("switch " + new.name))
            loaded = load_keystore(keys, _sources(keys, sec, age))
        assert after.kek_source == new.name == loaded.kek_source
        assert (loaded.dk_seed, loaded.dk_box, loaded.ck_seed, loaded.epochs) == \
            (ks.dk_seed, ks.dk_box, ks.ck_seed, ks.epochs)
        present = [s.name for s in _sources(keys, sec, age) if s.present()]
        assert present == [new.name]  # the old master key is gone
    _assert_no_secret_in_argv(sec.calls + age.calls, [bytes.fromhex(v) for v in sec.items.values()])


def test_a_switch_interrupted_after_storing_the_new_kek_still_opens(tmp_path):
    keys, sec, age = tmp_path / "keys", FakeSecurity(), FakeAge()
    ks = _ks(source="keychain")
    save_keystore(keys, ks, KeychainKek(LEDGER_ID, run=sec), _rng("save"))
    PassphraseKek(keys, run=age).store(_rng("orphan kek")(32))  # crash before the keystore was re-sealed
    assert load_keystore(keys, _sources(keys, sec, age)).kek_source == "keychain"


def test_the_inner_kek_source_must_match_the_source_that_opened_it(tmp_path):
    # A keystore that says "keychain" but opens with the file key (save_keystore refuses to write one).
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    kek = _rng("kek")(32)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        FileKek(keys).store(kek)
        (keys / "keystore.bin").write_bytes(seal_keystore(_ks(source="keychain"), kek, _rng("nonce")))
        os.chmod(keys / "keystore.bin", 0o600)
        with pytest.raises(KeystoreError, match="the keystore says kek_source 'keychain' but opened with file"):
            load_keystore(keys, [FileKek(keys)])


def test_save_refuses_a_source_that_doesnt_match_the_keystore(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    with pytest.raises(KeystoreError, match="kek_source"):
        save_keystore(keys, _ks(source="file"), KeychainKek(LEDGER_ID, run=sec), _rng("save"))


def test_load_with_nothing_present_fails(tmp_path):
    keys, sec, age = tmp_path / "keys", FakeSecurity(), FakeAge()
    with pytest.raises(KeystoreError):
        load_keystore(keys, _sources(keys, sec, age))


def test_the_kek_comes_from_the_rng(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    ks = _ks(source="keychain")
    drawn = []

    def recording(n):
        out = os.urandom(n)
        drawn.append(out)
        return out
    save_keystore(keys, ks, KeychainKek(LEDGER_ID, run=sec), recording)
    kek = bytes.fromhex(sec.items[("irp-roam", LEDGER_ID)])
    assert kek == drawn[0] and len(kek) == 32
    save_keystore(keys, ks, KeychainKek(LEDGER_ID, run=sec), os.urandom)
    assert bytes.fromhex(sec.items[("irp-roam", LEDGER_ID)]) != kek


# ── Review round 1 (step 2.5a): content mapping, permissions, the shipped runner, crash safety ──

def test_the_sealed_content_maps_each_field_exactly():
    outs = [bytes([i]) * 32 for i in (1, 2, 3)]
    it = iter(outs)
    ks = new_keystore(RK, lambda n: next(it), kek_source="keychain", tsa_creds={"disig": "u:p"})
    b = sig.b64url_encode
    expected = {"kek_source": "keychain", "dk_seed": b(outs[0]), "dk_box": b(outs[1]), "ck_seed": b(outs[2]),
                "epochs": {"0": {"kc": b(custodian_chain_key(RK, 0)), "ka": b(custodian_mac_key(RK, 0))}},
                "tsa_creds": {"disig": "u:p"}}
    assert _content(ks) == expected


def test_text_values_must_be_strict_unicode():
    with pytest.raises(RoamKeyError):
        new_keystore(RK, _rng("x"), kek_source="file", tsa_creds={"globaltrust_password": "pa\udcffss"})


def test_keychain_re_save_overwrites_its_item(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    kc = KeychainKek(LEDGER_ID, run=sec)
    ks = _ks(source="keychain")
    save_keystore(keys, ks, kc, os.urandom)
    save_keystore(keys, ks, kc, os.urandom)  # needs `add-generic-password -U`
    assert load_keystore(keys, [kc]) == ks
    assert list(sec.items) == [("irp-roam", LEDGER_ID)]


def test_an_existing_loose_keys_folder_is_tightened(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o755)
    os.chmod(keys, 0o755)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        save_keystore(keys, _ks(), FileKek(keys), _rng("save"))
    assert _mode(keys) == 0o700


def test_missing_parent_folders_are_created_private(tmp_path):
    old = os.umask(0)
    try:
        keys = tmp_path / "home" / ".irp-roam" / "keys"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RoamKeyWarning)
            save_keystore(keys, _ks(), FileKek(keys), _rng("save"))
    finally:
        os.umask(old)
    assert _mode(tmp_path / "home") == 0o700 and _mode(tmp_path / "home" / ".irp-roam") == 0o700


def _saved_file_mode(tmp_path):
    keys = tmp_path / "keys"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        save_keystore(keys, _ks(), FileKek(keys), _rng("save"))
    return keys


@pytest.mark.parametrize("loosen,mode", [("kek file", 0o644), ("kek file", 0o640), ("keystore file", 0o644),
                                         ("keystore file", 0o604), ("keys folder", 0o755), ("keys folder", 0o750)])
def test_load_refuses_loose_permissions(tmp_path, loosen, mode):
    keys = _saved_file_mode(tmp_path)
    target = {"kek file": keys / "kek.bin", "keystore file": keys / "keystore.bin", "keys folder": keys}[loosen]
    os.chmod(target, mode)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        with pytest.raises(KeystoreError, match="is readable by others; fix it with: chmod"):
            load_keystore(keys, [FileKek(keys)])


def test_load_refuses_a_loose_kek_age(tmp_path):
    keys, age = tmp_path / "keys", FakeAge()
    save_keystore(keys, _ks(source="passphrase"), PassphraseKek(keys, run=age), _rng("save"))
    os.chmod(keys / "kek.age", 0o640)
    with pytest.raises(KeystoreError, match="no master key opens the keystore .*kek.age is readable by others"):
        load_keystore(keys, [PassphraseKek(keys, run=age)])


def test_load_refuses_files_owned_by_someone_else(tmp_path, monkeypatch):
    keys = _saved_file_mode(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(keys).st_uid + 1)
    with pytest.raises(KeystoreError, match="isn't owned by this user"):
        load_keystore(keys, [FileKek(keys)])


def test_load_refuses_a_symlinked_keystore(tmp_path):
    keys = _saved_file_mode(tmp_path)
    real = tmp_path / "elsewhere.bin"
    (keys / "keystore.bin").rename(real)
    (keys / "keystore.bin").symlink_to(real)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        with pytest.raises(KeystoreError, match="is a symlink; roam keys must be real files"):
            load_keystore(keys, [FileKek(keys)])


def test_an_unreadable_leftover_master_key_is_skipped(tmp_path):
    keys, sec = tmp_path / "keys", FakeSecurity()
    ks = _ks(source="keychain")
    save_keystore(keys, ks, KeychainKek(LEDGER_ID, run=sec), _rng("save"))
    (keys / "kek.bin").mkdir(mode=0o700)  # a leftover that can't be read as a file
    stale = keys / "kek.age"
    stale.write_bytes(b"age-encryption.org/v1\nstale")
    os.chmod(stale, 0o000)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RoamKeyWarning)
            assert load_keystore(keys, _sources(keys, sec, FakeAge())) == ks
    finally:
        os.chmod(stale, 0o600)


class Crash(Exception):
    pass


def _crashing(monkeypatch, sources, at: int):
    """Count every durable step of a save (file writes, renames and master-key stores, promotes and removes)
    and raise Crash at step `at`, as a power cut or Ctrl-C would."""
    import irp.roam.keys as K

    count = {"n": 0}

    def step():
        count["n"] += 1
        if count["n"] == at:
            raise Crash()

    real_write, real_replace = K._write_private, K._replace

    def write(path, data):
        step()
        real_write(path, data)

    def replace(src, dst):
        step()
        real_replace(src, dst)

    monkeypatch.setattr(K, "_write_private", write)
    monkeypatch.setattr(K, "_replace", replace)
    for src in sources:
        for name in ("store", "promote", "remove"):
            real = getattr(src, name)

            def wrapped(*a, _real=real, **k):
                step()
                return _real(*a, **k)
            monkeypatch.setattr(src, name, wrapped)
    return count


SWITCHES = [("file", "file"), ("keychain", "keychain"), ("passphrase", "passphrase"), ("keychain", "passphrase"),
            ("passphrase", "file"), ("file", "keychain")]


@pytest.mark.parametrize("old,new", SWITCHES)
def test_a_crash_at_any_step_of_a_switch_leaves_a_keystore_that_opens(tmp_path, monkeypatch, old, new):
    import irp.roam.keys as K

    monkeypatch.setattr(K, "_full_fsync", lambda fd: None)  # ordering is under test, not durability
    sec, age = FakeSecurity(), FakeAge()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RoamKeyWarning)
        for at in range(1, 30):
            keys = tmp_path / f"keys-{at}"
            fresh = {s.name: s for s in _sources(keys, sec, age)}
            ks = _ks(source=old)
            save_keystore(keys, ks, fresh[old], os.urandom)
            with monkeypatch.context() as m:
                crashing = {s.name: s for s in _sources(keys, sec, age)}
                count = _crashing(m, crashing.values(), at)
                try:
                    set_kek(keys, list(crashing.values()), crashing[new], os.urandom)
                    finished = True
                except Crash:
                    finished = False
            after = load_keystore(keys, _sources(keys, sec, age))
            assert (after.dk_seed, after.ck_seed, after.epochs) == (ks.dk_seed, ks.ck_seed, ks.epochs)
            assert after.kek_source in (old, new)
            again = load_keystore(keys, _sources(keys, sec, age))  # stable once settled
            assert again == after
            sec.items.clear()
            if finished:
                assert count["n"] < at
                break
        else:
            pytest.fail("the switch never finished within 30 steps")


def _stand_in(tmp_path: Path, name: str, exit_code: int = 0) -> Path:
    """An executable that records its argv and stdin, chatters on stderr, and exits with `exit_code`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / f"{name}.log"
    script = bin_dir / name
    script.write_text(f"""#!{sys.executable}
import json, sys
data = sys.stdin.buffer.read()
open({str(log)!r}, "w").write(json.dumps({{"argv": sys.argv[1:], "stdin": data.hex()}}))
sys.stderr.write("STDERR-NOISE " + data.decode("ascii", "replace"))
sys.stdout.write("OUT")
sys.exit({exit_code})
""")
    os.chmod(script, 0o700)
    return script


def test_the_shipped_runner_feeds_stdin_and_keeps_stderr(tmp_path, capfd):
    from irp.roam.keys import _default_run

    tool = _stand_in(tmp_path, "tool")
    secret = b"6b" * 32
    assert _default_run([str(tool), "-x"], secret) == b"OUT"
    rec = json.loads((tmp_path / "tool.log").read_text())
    assert rec == {"argv": ["-x"], "stdin": secret.hex()}
    err = capfd.readouterr().err
    assert "STDERR-NOISE" not in err


def test_the_shipped_runner_fails_closed_without_echoing_data(tmp_path):
    from irp.roam.keys import _default_run

    tool = _stand_in(tmp_path, "failing", exit_code=3)
    with pytest.raises(KeystoreError) as exc:
        _default_run([str(tool)], b"6b" * 32)
    assert "6b6b" not in str(exc.value) and "STDERR-NOISE" not in str(exc.value)
    with pytest.raises(KeystoreError):
        _default_run([str(tmp_path / "no-such-tool")], b"")


def test_mode_a_runs_the_absolute_security_path():
    calls = []
    kc = KeychainKek(LEDGER_ID, run=lambda argv, data=b"": calls.append(list(argv)) or b"ab" * 32)
    kc.store(b"\x01" * 32)
    kc.load()
    kc.present()
    kc.remove()
    assert calls and all(c[0] == "/usr/bin/security" for c in calls)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_passphrase_mode_through_the_shipped_runner(tmp_path, stock_age):
    # A child Python with the pseudo-terminal as its terminal runs PassphraseKek with the real runner, as
    # `irp roam keystore set-kek passphrase` would in Terminal; we answer age's prompts.
    keys = tmp_path / "keys"
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
            "import hashlib, os\n"
            "from irp.roam.keys import new_keystore, save_keystore, load_keystore, PassphraseKek\n"
            f"rk = bytes.fromhex({RK.hex()!r})\n"
            "ks = new_keystore(rk, os.urandom, kek_source='passphrase')\n"
            f"save_keystore({str(keys)!r}, ks, PassphraseKek({str(keys)!r}), os.urandom)\n"
            f"assert load_keystore({str(keys)!r}, [PassphraseKek({str(keys)!r})]) == ks\n"
            "sys.stdout.write('ROUND-TRIP-OK')\n")
    out = _age_with_tty(b"correct horse")([sys.executable, "-c", code])
    assert b"ROUND-TRIP-OK" in out
    assert (keys / "kek.age").read_bytes().startswith(b"age-encryption.org/v1\n-> scrypt ")


class _SilentFile(FileKek):
    """FileKek without the warning, for the long crash runs."""

    @staticmethod
    def _warn():
        pass


def _version(ks):
    return int(ks.tsa_creds["v"])


@pytest.mark.parametrize("mode", ["file", "keychain", "passphrase"])
@pytest.mark.parametrize("order", ["as given", "reversed"])
def test_crashes_through_init_and_two_rotations_never_lose_or_roll_back(tmp_path, monkeypatch, mode, order):
    """Exhaustive over three operations, each crashed at every step: init (v1), a re-seal with new content (v2)
    and another (v3). After each one, a load must give the newest version written or the one before, never an
    older one, and keep giving it. A crashed init may leave nothing that opens; a retried init then works."""
    import irp.roam.keys as K

    monkeypatch.setattr(K, "_full_fsync", lambda fd: None)
    def sources(keys, sec, age):
        srcs = [PassphraseKek(keys, run=age), _SilentFile(keys), KeychainKek(LEDGER_ID, run=sec)]
        return srcs if order == "as given" else srcs[::-1]

    def run(keys, sec, age, version, crash_at):
        with monkeypatch.context() as m:
            srcs = sources(keys, sec, age)
            count = _crashing(m, srcs, crash_at)
            target = next(x for x in srcs if x.name == mode)
            ks = new_keystore(RK, os.urandom, kek_source=mode, tsa_creds={"v": str(version)})
            try:
                save_keystore(keys, ks, target, os.urandom)
                return True, count["n"]
            except Crash:
                return False, count["n"]

    def load(keys, sec, age):
        return load_keystore(keys, sources(keys, sec, age))

    case = 0
    for k1 in range(1, 8):
        for k2 in range(1, 8):
            for k3 in range(1, 8):
                case += 1
                keys, sec, age = tmp_path / f"k{case}", FakeSecurity(), FakeAge()
                done, _ = run(keys, sec, age, 1, k1)
                try:
                    have = _version(load(keys, sec, age))
                except KeystoreError:
                    assert not done
                    assert run(keys, sec, age, 1, 0)[0]
                    have = _version(load(keys, sec, age))
                assert have == 1
                for version, k in ((2, k2), (3, k3)):
                    run(keys, sec, age, version, k)
                    now = _version(load(keys, sec, age))
                    assert now in (have, version), (k1, k2, k3, have, now)
                    assert _version(load(keys, sec, age)) == now
                    have = now


def test_a_master_key_that_doesnt_read_back_keeps_the_old_keystore(tmp_path):
    class Forgetful(FakeSecurity):
        """`security -i` exits 0 but the .next item never lands (the case spec §14.4 marks [verify])."""

        def __call__(self, argv, data=b""):
            if argv[1:] == ["-i"] and b".next" in data:
                self.calls.append((list(argv), data))
                return b""
            return super().__call__(argv, data)

    keys, sec = tmp_path / "keys", Forgetful()
    kc = KeychainKek(LEDGER_ID, run=sec)
    old = _ks(source="keychain", tsa={"v": "1"})
    sec_ok = FakeSecurity()
    save_keystore(keys, old, KeychainKek(LEDGER_ID, run=sec_ok), os.urandom)
    sec.items = sec_ok.items
    with pytest.raises(KeystoreError, match="didn't read back"):
        save_keystore(keys, _ks(source="keychain", tsa={"v": "2"}), kc, os.urandom)
    assert load_keystore(keys, [KeychainKek(LEDGER_ID, run=sec_ok)]) == old
