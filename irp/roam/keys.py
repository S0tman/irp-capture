"""Keys for Roaming IRP: derivations and the keystore (spec v0.3 §14.1, §14.2, §14.4).

Everything a custodian needs to rebuild derives from one paper key, an age
identity (`AGE-SECRET-KEY-1…`). Its raw 32-byte payload is RK, and every other
key comes out of HKDF-SHA256 with the salt `irp-roam/v1` and an info string
starting `irp-roam/v1/`:

    RS seed = HK(RK, "root-ed25519/0")                the root signer
    K_c[e]  = HK(RK, "custodian-chain/" + e)          slot naming, per epoch
    K_a[e]  = HK(RK, "custodian-mac/" + e)            the custodian checkpoint MAC
    K_r     = HK(K_c[e], "reader-chain/" + reader_id)
    K_p     = HK(K_c[e], "phone-feed/" + kid),  K_o = HK(K_c[e], "outbox/" + kid)

The laptop's working keys (the device signing seed and box key, the capability
key, the per-epoch chain and MAC keys, the TSA credentials) live in one keystore:
`keystore.bin` = a 12-byte nonce and AES-256-GCM over the JCS object
{kek_source, dk_seed, dk_box, ck_seed, epochs, tsa_creds, pending, box_prev},
with the AAD `irp-roam/v1/keystore`, in a 0700 folder as a 0600 file. `pending`
holds the next keys while a rotation is under way and `box_prev` the outgoing box
key, decrypt-only, after one (§14.6a); both are null otherwise. Its master key
(KEK) is 32 random bytes held in one of three places:

- Mode A, `keychain` (default): the login Keychain, service `irp-roam`, account
  the ledger id. It goes in through `security -i` on stdin and comes out with
  `security find-generic-password … -w`, so it never appears in a command line.
- Mode B, `passphrase`: `kek.age`, sealed by stock `age -p`. age asks for the
  passphrase on the terminal; the KEK travels on a pipe; we write the file 0600.
- `file`: `kek.bin` (0600), for tests and non-macOS only, with a loud warning.

`set_kek` re-wraps the same keystore under another mode (or the same mode, under
a fresh master key) in one step.

The derivations are pinned by golden vectors checked against an independent
stdlib HKDF, and the keystore's field mapping by an exact-content test. Saves
use two slots (`next`, then `live`), so a crash at any step leaves a keystore
that opens; the loader finishes an interrupted save. On load the keys folder
must be 0700 and every key file 0600, owned by this user, and not a symlink.

One lock file, `keys/roam.lock` (0600, flock), keeps runs apart (§14.6a). Anything
that saves the keystore or appends to a log takes it exclusively; anything else
that signs takes it shared. It's always taken before either log's own lock. The
keystore is loaded only with it held, only an exclusive holder finishes an
interrupted save, and a save refuses if keystore.bin isn't the one this run loaded.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import sig
from ._deps import aesgcm, crypto
from .age import AgeError, Identity

SALT = b"irp-roam/v1"
INFO_PREFIX = "irp-roam/v1/"
KEYSTORE_AAD = b"irp-roam/v1/keystore"
KEYSTORE_FILE = "keystore.bin"
KEYSTORE_NEXT = "keystore.bin.next"
KEYCHAIN_SERVICE = "irp-roam"
KEK_SOURCES = ("keychain", "passphrase", "file")
KEYSTORE_KEYS = frozenset({"kek_source", "dk_seed", "dk_box", "ck_seed", "epochs", "tsa_creds", "pending",
                           "box_prev"})
PENDING_KEYS = frozenset({"dk_seed", "dk_box", "ck_seed", "started_at"})
BOX_PREV_KEYS = frozenset({"dk_box", "until"})
LOCK_FILE = "roam.lock"
NONCE_LEN = 12
TAG_LEN = 16

_LABEL = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no spaces or NULs
_READER = re.compile(r"rd-[0-9a-f]{32}")
_DEVICE = re.compile(r"dk-[0-9a-f]{32}")
_LEDGER_ID = re.compile(r"ILID-[0-9a-f]{32}")
_EPOCH = re.compile(r"0|[1-9][0-9]{0,8}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

Runner = Callable[[Sequence[str], bytes], bytes]


class RoamKeyError(ValueError):
    """A key can't be derived, stored or loaded as asked."""


class KeystoreError(RoamKeyError):
    """The keystore or its master key is missing, wrong, damaged or outside the format."""


class InterruptedSave(KeystoreError):
    """The keystore opens only by finishing an interrupted save, which needs roam.lock held exclusively."""


class LockBusy(RoamKeyError):
    """Another irp roam run holds roam.lock, and this one doesn't wait (an unattended run)."""


class RoamKeyWarning(UserWarning):
    """A key is kept in a weaker place than the default (the plain KEK file)."""


# ── Derivations (§14.1) ──

def _key32(key: Any, what: str) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise RoamKeyError(f"{what} must be 32 bytes")
    return bytes(key)


def hk(key: bytes, label: str) -> bytes:
    """HK(k, s) = HKDF-SHA256(ikm=k, salt="irp-roam/v1", info="irp-roam/v1/" + s, L=32)."""
    ikm = _key32(key, "the input key")
    if not (isinstance(label, str) and _LABEL.fullmatch(label)):
        raise RoamKeyError(f"derivation label must be printable ASCII without spaces, got {label!r}")
    c = crypto()
    return c.HKDF(algorithm=c.hashes.SHA256(), length=32, salt=SALT,
                  info=(INFO_PREFIX + label).encode("ascii")).derive(ikm)


def _epoch(epoch: Any) -> str:
    if type(epoch) is not int or not 0 <= epoch <= 999_999_999:
        raise RoamKeyError(f"an epoch is a non-negative integer, got {epoch!r}")
    return str(epoch)


def rk_from_identity(identity: Any) -> bytes:
    """RK: the raw 32 bytes of the paper key's bech32 payload (unclamped)."""
    try:
        return Identity.from_string(identity).secret
    except AgeError as exc:
        raise RoamKeyError(f"the paper key must be an AGE-SECRET-KEY-1… string: {exc}") from None


def root_seed(rk: bytes) -> bytes:
    return hk(rk, "root-ed25519/0")


def root_public_key(rk: bytes) -> bytes:
    return sig.public_key(root_seed(rk))


def root_id_for(rk: bytes) -> str:
    return sig.root_id(root_public_key(rk))


def custodian_chain_key(rk: bytes, epoch: int) -> bytes:
    return hk(rk, "custodian-chain/" + _epoch(epoch))


def custodian_mac_key(rk: bytes, epoch: int) -> bytes:
    return hk(rk, "custodian-mac/" + _epoch(epoch))


def reader_chain_key(kc: bytes, reader_id: str) -> bytes:
    if not (isinstance(reader_id, str) and _READER.fullmatch(reader_id)):
        raise RoamKeyError(f"reader id must be rd- plus 32 lowercase hex, got {reader_id!r}")
    return hk(kc, "reader-chain/" + reader_id)


def _kid(kid: Any) -> str:
    if not (isinstance(kid, str) and _DEVICE.fullmatch(kid)):
        raise RoamKeyError(f"device key id must be dk- plus 32 lowercase hex, got {kid!r}")
    return kid


def phone_feed_key(kc: bytes, kid: str) -> bytes:
    return hk(kc, "phone-feed/" + _kid(kid))


def outbox_key(kc: bytes, kid: str) -> bytes:
    return hk(kc, "outbox/" + _kid(kid))


# ── The keystore (§14.4, §14.6a) ──

class _DeviceKeys:
    """The ids a set of device keys goes by (the live keys and the pending ones)."""

    @property
    def dk_id(self) -> str:
        return sig.key_id("dk", sig.public_key(self.dk_seed))

    @property
    def ck_id(self) -> str:
        return sig.key_id("ck", sig.public_key(self.ck_seed))

    @property
    def dk_recipient(self) -> str:
        return Identity(self.dk_box).recipient().to_string()


@dataclass(frozen=True)
class Pending(_DeviceKeys):
    """The next keys of a rotation under way: saved once every signature on the rotate line is gathered,
    before the line is appended (§14.6a step 3)."""
    dk_seed: bytes = field(repr=False)
    dk_box: bytes = field(repr=False)
    ck_seed: bytes = field(repr=False)
    started_at: str


@dataclass(frozen=True)
class BoxPrev:
    """The outgoing box key after a rotation: decrypt-only, never an audience entry, never a signer. `until`
    is the old CK's printed not_after. One slot: a second rotation before `until` replaces it, dropping the
    earlier outgoing box key early (an owner decision, pending; the rotate result says so)."""
    dk_box: bytes = field(repr=False)
    until: str


@dataclass(frozen=True)
class Keystore(_DeviceKeys):
    kek_source: str
    dk_seed: bytes = field(repr=False)
    dk_box: bytes = field(repr=False)
    ck_seed: bytes = field(repr=False)
    epochs: Mapping[int, tuple[bytes, bytes]] = field(repr=False)  # epoch -> (K_c, K_a)
    tsa_creds: Mapping[str, str] | None = field(default=None, repr=False)
    pending: Pending | None = field(default=None, repr=False)
    box_prev: BoxPrev | None = field(default=None, repr=False)


def new_keystore(rk: bytes, rng: Callable[[int], bytes], *, kek_source: str, epoch: int = 0,
                 tsa_creds: Mapping[str, str] | None = None) -> Keystore:
    """A fresh keystore at genesis or recovery: new device and capability keys, the epoch's keys from RK."""
    if kek_source not in KEK_SOURCES:
        raise RoamKeyError(f"kek_source must be one of {', '.join(KEK_SOURCES)}")
    keys = [rng(32) for _ in range(3)]
    if any(not isinstance(k, bytes) or len(k) != 32 for k in keys) or len(set(keys)) != 3:
        raise RoamKeyError("rng must return fresh 32-byte values")
    ks = Keystore(kek_source=kek_source, dk_seed=keys[0], dk_box=keys[1], ck_seed=keys[2],
                  epochs={epoch: (custodian_chain_key(rk, epoch), custodian_mac_key(rk, epoch))},
                  tsa_creds=dict(tsa_creds) if tsa_creds is not None else None)
    _check_keystore(ks)
    return ks


def _timestamp(val: Any, what: str) -> str:
    """A §15.1 timestamp: UTC to the second, and a real time."""
    if not (isinstance(val, str) and _TIMESTAMP.fullmatch(val)):
        raise KeystoreError(f"{what} must be a UTC timestamp like 2026-10-08T09:00:00Z")
    try:
        datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise KeystoreError(f"{what} isn't a real UTC time") from None
    return val


def _check_keystore(ks: Keystore) -> None:
    if ks.kek_source not in KEK_SOURCES:
        raise KeystoreError(f"kek_source must be one of {', '.join(KEK_SOURCES)}")
    named = [("dk_seed", ks.dk_seed), ("dk_box", ks.dk_box), ("ck_seed", ks.ck_seed)]
    if ks.pending is not None:
        if not isinstance(ks.pending, Pending):
            raise KeystoreError("pending must be null or the next keys")
        named += [("pending dk_seed", ks.pending.dk_seed), ("pending dk_box", ks.pending.dk_box),
                  ("pending ck_seed", ks.pending.ck_seed)]
        _timestamp(ks.pending.started_at, "pending started_at")
    if ks.box_prev is not None:
        if not isinstance(ks.box_prev, BoxPrev):
            raise KeystoreError("box_prev must be null or the outgoing box key")
        named.append(("box_prev dk_box", ks.box_prev.dk_box))
        _timestamp(ks.box_prev.until, "box_prev until")
    for name, key in named:
        if not isinstance(key, bytes) or len(key) != 32:
            raise KeystoreError(f"{name} must be 32 bytes")
    if len(set(k for _, k in named[:3])) != 3:
        raise KeystoreError("dk_seed, dk_box and ck_seed must be distinct keys")
    if len(set(k for _, k in named)) != len(named):
        raise KeystoreError("every key in the keystore must be distinct: the live keys, the pending keys and "
                            "the outgoing box key")
    if not isinstance(ks.epochs, Mapping) or not ks.epochs:
        raise KeystoreError("the keystore holds at least one epoch's keys")
    for e, pair in ks.epochs.items():
        if type(e) is not int or not 0 <= e <= 999_999_999:
            raise KeystoreError(f"bad epoch {e!r}")
        if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(k, bytes) and len(k) == 32
                                                                     for k in pair)):
            raise KeystoreError(f"epoch {e} must hold (K_c, K_a), two 32-byte keys")
    if ks.tsa_creds is not None and not (isinstance(ks.tsa_creds, Mapping) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in ks.tsa_creds.items())):
        raise KeystoreError("tsa_creds must be null or map names to strings")
    if ks.tsa_creds is not None:
        try:
            for k, v in ks.tsa_creds.items():
                k.encode("utf-8"), v.encode("utf-8")
        except UnicodeEncodeError:
            raise KeystoreError("tsa_creds must be valid Unicode text (no unpaired surrogates)") from None


def _content(ks: Keystore) -> dict[str, Any]:
    b = sig.b64url_encode
    p, bp = ks.pending, ks.box_prev
    return {"kek_source": ks.kek_source, "dk_seed": b(ks.dk_seed), "dk_box": b(ks.dk_box), "ck_seed": b(ks.ck_seed),
            "epochs": {str(e): {"kc": b(kc), "ka": b(ka)} for e, (kc, ka) in sorted(ks.epochs.items())},
            "tsa_creds": dict(ks.tsa_creds) if ks.tsa_creds is not None else None,
            "pending": None if p is None else {"dk_seed": b(p.dk_seed), "dk_box": b(p.dk_box),
                                               "ck_seed": b(p.ck_seed), "started_at": p.started_at},
            "box_prev": None if bp is None else {"dk_box": b(bp.dk_box), "until": bp.until}}


def _from_content(c: Any) -> Keystore:
    if not isinstance(c, dict) or set(c) != KEYSTORE_KEYS:
        raise KeystoreError(f"keystore content must have exactly the keys {', '.join(sorted(KEYSTORE_KEYS))}")

    def key(val: Any, what: str) -> bytes:
        try:
            return sig.b64url_decode(val, 32)
        except sig.SigError as exc:
            raise KeystoreError(f"keystore {what}: {exc}") from None

    epochs = c["epochs"]
    if not isinstance(epochs, dict):
        raise KeystoreError("keystore epochs must be an object")
    parsed: dict[int, tuple[bytes, bytes]] = {}
    for e, pair in epochs.items():
        if not _EPOCH.fullmatch(e):
            raise KeystoreError(f"keystore epoch {e!r} must be a decimal number without leading zeros")
        if not isinstance(pair, dict) or set(pair) != {"kc", "ka"}:
            raise KeystoreError(f"keystore epoch {e} must have exactly kc and ka")
        parsed[int(e)] = (key(pair["kc"], f"epoch {e} kc"), key(pair["ka"], f"epoch {e} ka"))
    pending = box_prev = None
    if c["pending"] is not None:
        p = c["pending"]
        if not isinstance(p, dict) or set(p) != PENDING_KEYS:
            raise KeystoreError(f"keystore pending must be null or have exactly {', '.join(sorted(PENDING_KEYS))}")
        pending = Pending(dk_seed=key(p["dk_seed"], "pending dk_seed"), dk_box=key(p["dk_box"], "pending dk_box"),
                          ck_seed=key(p["ck_seed"], "pending ck_seed"),
                          started_at=_timestamp(p["started_at"], "pending started_at"))
    if c["box_prev"] is not None:
        bp = c["box_prev"]
        if not isinstance(bp, dict) or set(bp) != BOX_PREV_KEYS:
            raise KeystoreError(f"keystore box_prev must be null or have exactly {', '.join(sorted(BOX_PREV_KEYS))}")
        box_prev = BoxPrev(dk_box=key(bp["dk_box"], "box_prev dk_box"), until=_timestamp(bp["until"], "box_prev until"))
    ks = Keystore(kek_source=c["kek_source"], dk_seed=key(c["dk_seed"], "dk_seed"),
                  dk_box=key(c["dk_box"], "dk_box"), ck_seed=key(c["ck_seed"], "ck_seed"), epochs=parsed,
                  tsa_creds=c["tsa_creds"], pending=pending, box_prev=box_prev)
    _check_keystore(ks)
    return ks


def seal_keystore(ks: Keystore, kek: bytes, rng: Callable[[int], bytes]) -> bytes:
    """nonce (12 bytes) ‖ AES-256-GCM(kek, nonce, JCS(content), AAD "irp-roam/v1/keystore")."""
    _check_keystore(ks)
    kek = _key32(kek, "the keystore master key")
    nonce = rng(NONCE_LEN)
    if not isinstance(nonce, bytes) or len(nonce) != NONCE_LEN:
        raise RoamKeyError("rng must return a 12-byte nonce")
    from irp.integrity.canonical import canonicalize

    return nonce + aesgcm()(kek).encrypt(nonce, canonicalize(_content(ks)), KEYSTORE_AAD)


def open_keystore(blob: bytes, kek: bytes) -> Keystore:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < NONCE_LEN + TAG_LEN:
        raise KeystoreError("the keystore is truncated")
    kek = _key32(kek, "the keystore master key")
    try:
        plain = aesgcm()(kek).decrypt(bytes(blob[:NONCE_LEN]), bytes(blob[NONCE_LEN:]), KEYSTORE_AAD)
    except crypto().InvalidTag:
        raise KeystoreError("the keystore doesn't open with this master key (wrong key or damaged file)") from None
    return _from_content(sig.load_jcs(plain, "keystore content", error=KeystoreError))


# ── Private files ──

def _fsync_dir(path: Path) -> None:
    """Make a rename in `path` durable (F_FULLFSYNC on macOS, where plain fsync stops at the drive cache)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        _full_fsync(fd)
    finally:
        os.close(fd)


def _full_fsync(fd: int) -> None:
    import fcntl

    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _private_dir(path: Path) -> Path:
    """Create the folder (and any missing parents) as 0700, and tighten it to 0700 if it already exists."""
    missing = [p for p in [path, *path.parents] if not p.exists()]
    for p in reversed(missing):
        p.mkdir(mode=0o700)
        os.chmod(p, 0o700)
    if path.is_symlink() or not path.is_dir():
        raise KeystoreError(f"{path} must be a real folder, not a symlink or a file")
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Write atomically and durably: a 0600 temp file in the same folder, fully synced, renamed over the target."""
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            _full_fsync(fh.fileno())
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    _replace(tmp, path)


def _replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)
    _fsync_dir(dst.parent)


def _discard(path: Path) -> None:
    """Best-effort removal of a leftover, while another error is on its way out."""
    try:
        path.unlink()
        _fsync_dir(path.parent)
    except OSError:
        pass


def _check_private(path: Path, *, folder: bool) -> None:
    """§14.4: the keys folder is 0700 and its files 0600, owned by this user, and nothing is a symlink."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise KeystoreError(f"can't read {path.name}: {exc.strerror}") from None
    if stat.S_ISLNK(st.st_mode):
        raise KeystoreError(f"{path} is a symlink; roam keys must be real files")
    if (stat.S_ISDIR(st.st_mode) if folder else stat.S_ISREG(st.st_mode)) is False:
        raise KeystoreError(f"{path} must be a {'folder' if folder else 'regular file'}")
    if st.st_uid != os.getuid():
        raise KeystoreError(f"{path} isn't owned by this user")
    if st.st_mode & 0o077:
        raise KeystoreError(f"{path} is readable by others; fix it with: chmod {'700' if folder else '600'} {path}")


def _read_private(path: Path) -> bytes:
    _check_private(path, folder=False)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise KeystoreError(f"can't read {path.name}: {exc.strerror}") from None


# ── Where the master key lives ──
#
# Each source has two slots. `live` holds the master key that opens keystore.bin. A save writes the new
# keystore and the new master key into `next` slots first, renames the keystore into place, then promotes
# the master key and clears `next`. The old master key is never destroyed before the keystore it opens has
# been replaced, so a crash at any step leaves a (keystore, master key) pair that opens.

SLOTS = ("live", "next")


def _default_run(argv: Sequence[str], data: bytes = b"") -> bytes:
    """Run a tool with `data` on stdin and return stdout. stderr is captured so it can't leak to logs, and
    a tool that prompts (age) does so on the terminal, never through these pipes."""
    import subprocess

    try:
        out = subprocess.run(list(argv), input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise KeystoreError(f"can't run {Path(argv[0]).name}: {exc.strerror}") from None
    if out.returncode != 0:
        raise KeystoreError(f"{Path(argv[0]).name} failed (exit {out.returncode})")
    return out.stdout


def _kek_from_hex(text: bytes, where: str) -> bytes:
    try:
        kek = bytes.fromhex(text.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        raise KeystoreError(f"the master key in {where} isn't 64 hex characters") from None
    if len(kek) != 32:
        raise KeystoreError(f"the master key in {where} isn't 32 bytes")
    return kek


def _slot(slot: str) -> str:
    if slot not in SLOTS:
        raise RoamKeyError(f"slot must be one of {', '.join(SLOTS)}")
    return slot


class KeychainKek:
    """Mode A: the login Keychain through /usr/bin/security. Service irp-roam, account the ledger id (the
    `next` slot uses the account `<ledger id>.next`)."""

    name = "keychain"
    TOOL = "/usr/bin/security"  # never whatever `security` comes first on PATH
    read_back = True

    def __init__(self, ledger_id: str, run: Runner | None = None):
        if not (isinstance(ledger_id, str) and _LEDGER_ID.fullmatch(ledger_id)):
            raise RoamKeyError("the Keychain account is the ledger id, ILID- plus 32 lowercase hex")
        self.ledger_id = ledger_id
        self._run = run or _default_run

    def _ids(self, slot: str) -> list[str]:
        account = self.ledger_id + (".next" if _slot(slot) == "next" else "")
        return ["-s", KEYCHAIN_SERVICE, "-a", account]

    def present(self, slot: str = "live") -> bool:
        try:
            self._run([self.TOOL, "find-generic-password", *self._ids(slot)], b"")
            return True
        except KeystoreError:
            return False

    def store(self, kek: bytes, slot: str = "live") -> None:
        kek = _key32(kek, "the master key")
        command = f"add-generic-password -U {' '.join(self._ids(slot))} -w {kek.hex()}\n"
        self._run([self.TOOL, "-i"], command.encode("ascii"))  # the secret goes on stdin, never in argv

    def load(self, slot: str = "live") -> bytes:
        return _kek_from_hex(self._run([self.TOOL, "find-generic-password", *self._ids(slot), "-w"], b""),
                             "the login Keychain")

    def remove(self, slot: str = "live") -> None:
        if self.present(slot):
            self._run([self.TOOL, "delete-generic-password", *self._ids(slot)], b"")

    def promote(self, kek: bytes) -> None:
        self.store(kek, "live")
        self.remove("next")


class _FileSlots:
    """Shared by the two file-backed sources: `<name>` is live, `<name>.next` is next."""

    filename = ""

    def __init__(self, keys_dir: Path | str):
        self.keys_dir = Path(keys_dir)

    def _path(self, slot: str) -> Path:
        return self.keys_dir / (self.filename + (".next" if _slot(slot) == "next" else ""))

    @property
    def path(self) -> Path:
        return self._path("live")

    def present(self, slot: str = "live") -> bool:
        p = self._path(slot)
        return p.is_symlink() or p.exists()

    def remove(self, slot: str = "live") -> None:
        p = self._path(slot)
        if p.is_dir() and not p.is_symlink():
            raise KeystoreError(f"{p} is a folder; remove it by hand")
        if self.present(slot):
            p.unlink()
            _fsync_dir(p.parent)

    def promote(self, kek: bytes) -> None:
        if self.present("next"):
            _replace(self._path("next"), self._path("live"))


class PassphraseKek(_FileSlots):
    """Mode B: kek.age, sealed with stock `age -p`. age asks for the passphrase on the terminal."""

    name = "passphrase"
    filename = "kek.age"
    read_back = False  # reading it back would ask for the passphrase a second time

    def __init__(self, keys_dir: Path | str, run: Runner | None = None):
        super().__init__(keys_dir)
        self._run = run or _default_run

    def store(self, kek: bytes, slot: str = "live") -> None:
        kek = _key32(kek, "the master key")
        sealed = self._run(["age", "-p"], kek.hex().encode("ascii"))  # ciphertext on stdout, we write it 0600
        if not sealed.startswith(b"age-encryption.org/v1\n"):
            raise KeystoreError("age -p didn't return an age file")
        _private_dir(self.keys_dir)
        _write_private(self._path(slot), sealed)

    def load(self, slot: str = "live") -> bytes:
        path = self._path(slot)
        _check_private(path, folder=False)
        return _kek_from_hex(self._run(["age", "-d", str(path)], b""), path.name)


class FileKek(_FileSlots):
    """For tests and non-macOS only: kek.bin, 0600, with a loud warning every time it's used."""

    name = "file"
    filename = "kek.bin"
    read_back = True

    @staticmethod
    def _warn() -> None:
        warnings.warn("the keystore master key is a plain 0600 file (kek.bin): use only for tests or on a "
                      "machine without a login Keychain", RoamKeyWarning, stacklevel=3)

    def store(self, kek: bytes, slot: str = "live") -> None:
        self._warn()
        _private_dir(self.keys_dir)
        _write_private(self._path(slot), _key32(kek, "the master key").hex().encode())

    def load(self, slot: str = "live") -> bytes:
        self._warn()
        path = self._path(slot)
        return _kek_from_hex(_read_private(path), path.name)


KekSource = Any  # KeychainKek | PassphraseKek | FileKek


_ANY = object()


def keystore_digest(keys_dir: Path | str) -> str | None:
    """sha256 of keystore.bin as it is now, or None when there's none. A run keeps the digest of what it
    loaded, so a save can tell that another run changed the keystore in between (§14.6a)."""
    path = Path(keys_dir) / KEYSTORE_FILE
    if not (path.is_symlink() or path.exists()):
        return None
    return "sha256-" + hashlib.sha256(_read_private(path)).hexdigest()


def save_keystore(keys_dir: Path | str, ks: Keystore, source: KekSource, rng: Callable[[int], bytes], *,
                  expect_digest: Any = _ANY) -> str:
    """Seal `ks` under a fresh master key held by `source`, in an order that survives a crash at any step:
    keystore.bin.next, then the master key's next slot, then rename into keystore.bin, then promote.

    With `expect_digest` (the keystore_digest this run loaded), the save refuses before writing anything if
    keystore.bin has changed since. Returns the digest of the new keystore.bin."""
    if ks.kek_source != source.name:
        raise KeystoreError(f"the keystore's kek_source is {ks.kek_source!r} but it's being saved to {source.name}")
    keys = _private_dir(Path(keys_dir))
    if expect_digest is not _ANY and keystore_digest(keys) != expect_digest:
        raise KeystoreError("keystore.bin changed since this run loaded it (another irp roam run saved it); "
                            "nothing was saved, so load it again and redo the change")
    kek = rng(32)
    if not isinstance(kek, bytes) or len(kek) != 32:
        raise RoamKeyError("rng must return a 32-byte master key")
    blob = seal_keystore(ks, kek, rng)
    _write_private(keys / KEYSTORE_NEXT, blob)
    try:
        source.store(kek, "next")
        if source.read_back:  # never swap in a keystore whose master key can't be read back
            try:
                stored = source.load("next")
            except (KeystoreError, OSError):
                stored = None
            if stored != kek:
                raise KeystoreError(f"the new master key didn't read back from {source.name}; the old keystore is "
                                    "kept")
    except BaseException:
        # A store that failed (a cancelled passphrase prompt, say) leaves keystore.bin.next sealed under a master
        # key held nowhere: take it away rather than leave an orphan beside the keystore.
        _discard(keys / KEYSTORE_NEXT)
        raise
    _replace(keys / KEYSTORE_NEXT, keys / KEYSTORE_FILE)
    source.promote(kek)
    return "sha256-" + hashlib.sha256(blob).hexdigest()


def load_keystore(keys_dir: Path | str, sources: Sequence[KekSource], *, finish: bool = True) -> Keystore:
    """Open the keystore with whichever master key fits. keystore.bin is tried with every master key first;
    keystore.bin.next only when nothing opens keystore.bin (a first save that crashed before its rename),
    and it's then rolled forward. A master key found in a `next` slot is promoted. The keystore must name the
    source that opened it, so a stray master key can't stand in for the real one.

    Rolling forward and promoting finish an interrupted save, which only an exclusive holder of roam.lock may
    do. With `finish=False` (a shared holder) they raise InterruptedSave instead and change nothing."""
    keys = Path(keys_dir)
    _check_private(keys, folder=True)
    blobs = []
    for name in (KEYSTORE_FILE, KEYSTORE_NEXT):
        path = keys / name
        if path.is_symlink() or path.exists():
            blobs.append((name, _read_private(path)))
    if not blobs:
        raise KeystoreError("no keystore here; run irp roam init")
    keks: list[tuple[Any, str, bytes]] = []
    reasons = []
    for source in sources:
        for slot in SLOTS:
            if not source.present(slot):
                continue
            try:
                keks.append((source, slot, source.load(slot)))
            except KeystoreError as exc:
                reasons.append(f"{source.name}/{slot}: {exc}")
            except OSError as exc:  # an unreadable leftover never stops the next source being tried
                reasons.append(f"{source.name}/{slot}: {exc.strerror}")
    for name, blob in blobs:
        for source, slot, kek in keks:
            try:
                ks = open_keystore(blob, kek)
            except KeystoreError:
                continue
            if ks.kek_source != source.name:
                raise KeystoreError(f"the keystore says kek_source {ks.kek_source!r} but opened with {source.name}")
            if not finish and (name == KEYSTORE_NEXT or slot == "next"):
                raise InterruptedSave("the keystore opens only by finishing an interrupted save; that needs "
                                      "roam.lock held exclusively")
            if name == KEYSTORE_NEXT:
                _replace(keys / KEYSTORE_NEXT, keys / KEYSTORE_FILE)
            if slot == "next":
                source.promote(kek)
            return ks
    reasons += [f"{source.name}/{slot}: doesn't open the keystore" for source, slot, _ in keks]
    raise KeystoreError("no master key opens the keystore" + (f" ({'; '.join(reasons)})" if reasons else ""))


def set_kek(keys_dir: Path | str, sources: Sequence[KekSource], new: KekSource,
            rng: Callable[[int], bytes], *, lock: RoamLock | None = None, say: Callable[[str], None] | None = None
            ) -> Keystore:
    """`irp roam keystore set-kek`: re-wrap the same keystore under `new` (the same mode re-wraps under a fresh
    master key), then remove every other source's master keys. Every field is carried over unchanged. Runs
    under roam.lock held exclusively: the caller's, or one taken here (waiting, and saying so, if it's busy)."""
    if lock is None:
        with RoamLock(keys_dir, exclusive=True, interactive=True, say=say) as held:
            return set_kek(keys_dir, sources, new, rng, lock=held)
    current, loaded = load_locked(keys_dir, sources, lock)
    moved = replace(current, kek_source=new.name)
    save_locked(keys_dir, moved, new, rng, lock, loaded)
    for source in sources:
        if source.name != new.name:
            for slot in SLOTS:
                source.remove(slot)
    return moved


# ── roam.lock (§14.6a) ──

class RoamLock:
    """`keys/roam.lock` (0600, flock). Exclusive for anything that saves the keystore or appends to a log;
    shared for anything else that signs with DK or CK. Always taken first, before the devices LogWriter and
    then the readers LogWriter, so it's what keeps a transaction whole when a LogWriter drops its own lock
    after a failed append. An unattended run (`interactive=False`) never waits: it gets LockBusy and skips
    the run. An interactive one waits and says what it's waiting for."""

    def __init__(self, keys_dir: Path | str, *, exclusive: bool, interactive: bool,
                 say: Callable[[str], None] | None = None):
        self.keys_dir = Path(keys_dir)
        self.exclusive, self.interactive, self.say = bool(exclusive), bool(interactive), say
        self.fd: int | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def __enter__(self) -> "RoamLock":
        import fcntl

        keys = _private_dir(self.keys_dir)
        path = keys / LOCK_FILE
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise KeystoreError(f"can't open {path}: {exc.strerror} (it must be a real file, not a symlink)") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise KeystoreError(f"{path} must be a regular file owned by this user")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        try:
            self._take(fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _take(self, mode: int) -> None:
        import fcntl

        try:
            fcntl.flock(self.fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        if not self.interactive:
            raise LockBusy("busy: another irp roam run holds keys/roam.lock; this unattended run is skipped")
        if self.say:
            self.say("waiting for another irp roam run to finish (it holds keys/roam.lock)")
        fcntl.flock(self.fd, mode)

    def make_exclusive(self) -> None:
        """Release, then take the lock exclusively (flock can't upgrade in one step, so another run may get in
        between: whatever was read under the shared lock must be read again)."""
        import fcntl

        if self.fd is None:
            raise KeystoreError("roam.lock isn't held")
        if self.exclusive:
            return
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.exclusive = True
        self._take(fcntl.LOCK_EX)

    def __exit__(self, *exc: Any) -> None:
        import fcntl

        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def load_locked(keys_dir: Path | str, sources: Sequence[KekSource], lock: RoamLock) -> tuple[Keystore, str]:
    """Load the keystore with roam.lock held, and return it with the digest of the keystore.bin it came from.
    A shared holder that finds an interrupted save takes the lock exclusively and loads again."""
    if not lock.held:
        raise KeystoreError("the keystore is loaded only with roam.lock held")
    if lock.exclusive:
        ks = load_keystore(keys_dir, sources)
    else:
        try:
            ks = load_keystore(keys_dir, sources, finish=False)
        except InterruptedSave:
            lock.make_exclusive()
            ks = load_keystore(keys_dir, sources)
    digest = keystore_digest(keys_dir)
    if digest is None:  # pragma: no cover - load_keystore has just read it
        raise KeystoreError("keystore.bin disappeared while it was being loaded")
    return ks, digest


def save_locked(keys_dir: Path | str, ks: Keystore, source: KekSource, rng: Callable[[int], bytes], lock: RoamLock,
                loaded: str) -> str:
    """Save with roam.lock held exclusively, refusing if keystore.bin isn't the one this run loaded (or last
    saved). Returns the new digest, the one the next save in this run must expect."""
    if not (lock.held and lock.exclusive):
        raise KeystoreError("saving the keystore needs roam.lock held exclusively")
    return save_keystore(keys_dir, ks, source, rng, expect_digest=loaded)
