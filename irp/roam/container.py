"""Strict ustar container for relay objects (Roaming IRP spec v0.3 §15.4).

Every relay object is a small tar archive inside the age envelope. The writer
uses stdlib tarfile in USTAR format with fixed metadata (mtime 0, uid and gid 0,
empty user and group names, mode 0644, regular files only, ASCII names of at
most 100 bytes) and a canonical member order per kind: the required members
first in a fixed order, then the rest sorted bytewise. The archive is then
padded with zeros to its Padmé length, so the relay learns as little as
practical from sizes.

The reader is our own small parser, deliberately stricter than tar: it rejects
a bad header checksum, any member that isn't a plain regular file, metadata
other than the fixed values, duplicate names, names off the kind's allowlist,
`..` or absolute paths, more than 64 members, members out of canonical order,
missing required members, an artefact whose content doesn't match its name,
non-zero padding, and anything but zero bytes after the end marker. Sizes and
hashes are checked against the signed statement with `check_binding` (or the
lower-level `check_hashes`). Member bytes are trustworthy only after that binding
check: a size field can legally reach into its own zero padding, so the reader
alone can't tell "abc" from "abc" plus trailing NULs.
"""
from __future__ import annotations

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
from typing import Mapping

BLOCK = 512
MIN_PADDED = 16384
MAX_MEMBERS = 64
NAME_MAX = 100
_ZERO_BLOCK = b"\x00" * BLOCK
_ARTEFACT = re.compile(r"artefacts/sha256-([0-9a-f]{64})\.([a-z0-9]{1,18})")
_NAME_CHARS = re.compile(r"[A-Za-z0-9._/-]+")


class ContainerError(ValueError):
    """The container can't be written or read under the strict profile."""


@dataclass(frozen=True)
class _Kind:
    first: tuple[str, ...]
    rest_required: frozenset[str]
    rest_optional: frozenset[str]
    artefacts: bool = False


KINDS = {
    # A reader's Slice (format name "rekadu" in code).
    "rekadu": _Kind(
        first=("irp/content.json", "irp/content.sig", "irp/disclosure.json", "irp/disclosure.sig"),
        rest_required=frozenset({"IRP-RETURN.txt", "irp/checkpoint.json", "irp/checkpoint.sig",
                                 "irp/devices.jsonl", "ledger.jsonl", "manifest.json"}),
        rest_optional=frozenset({"irp/checkpoint.tsr"}),  # absent when the checkpoint is unwitnessed
        artefacts=True,  # reserved in Cut 1; verified when present
    ),
    "custodian": _Kind(
        first=("irp/checkpoint.json", "irp/checkpoint.sig", "irp/checkpoint.mac"),
        rest_required=frozenset({"irp/body.json", "irp/devices.jsonl"}),
        rest_optional=frozenset({"irp/checkpoint.tsr", "irp/policy.json"}),
    ),
    "segment": _Kind(first=("seg",), rest_required=frozenset(), rest_optional=frozenset()),
}
RESERVED_KINDS = frozenset({"report", "control"})  # for the browser step


def padme(n: int) -> int:
    """Padmé: round n up so only about log(log n) bits of its length are revealed."""
    if n < 0:
        raise ContainerError("length must not be negative")
    if n < 2:
        return n
    e = n.bit_length() - 1
    last_bits = e - e.bit_length()
    mask = (1 << last_bits) - 1
    return (n + mask) & ~mask


def padded_length(n: int) -> int:
    """L' = max(16384, roundup512(padme(L)))."""
    return max(MIN_PADDED, -(-padme(n) // BLOCK) * BLOCK)


def _kind(kind: str) -> _Kind:
    if not isinstance(kind, str):
        raise ContainerError("container kind must be a string")
    if kind in RESERVED_KINDS:
        raise ContainerError(f"container kind {kind!r} is reserved for the browser step")
    try:
        return KINDS[kind]
    except (KeyError, TypeError):
        raise ContainerError(f"unknown container kind {kind!r}") from None


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise ContainerError("member names must be non-empty strings")
    if len(name.encode("ascii", "replace")) > NAME_MAX or not name.isascii() or not _NAME_CHARS.fullmatch(name):
        raise ContainerError(f"member name {name!r} isn't a plain ASCII name of at most {NAME_MAX} bytes")
    parts = name.split("/")
    if name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ContainerError(f"member name {name!r} is absolute, empty-segmented or climbs with '..'")
    return name


def _allowed(spec: _Kind, name: str) -> bool:
    return (name in spec.first or name in spec.rest_required or name in spec.rest_optional
            or (spec.artefacts and _ARTEFACT.fullmatch(name) is not None))


def _check_artefact(name: str, data: bytes) -> None:
    m = _ARTEFACT.fullmatch(name)
    if m and hashlib.sha256(data).hexdigest() != m.group(1):
        raise ContainerError(f"artefact {name} doesn't match its content hash")


def _canonical_order(spec: _Kind, names) -> list[str]:
    rest = sorted((n for n in names if n not in spec.first), key=lambda n: n.encode("ascii"))
    return list(spec.first) + rest


def _check_member_set(spec: _Kind, names) -> None:
    names = set(names)
    missing = sorted((set(spec.first) | spec.rest_required) - names)
    if missing:
        raise ContainerError(f"missing required member(s): {', '.join(missing)}")


def pack(kind: str, members: Mapping[str, bytes]) -> bytes:
    """Write a canonical, padded container. Input order doesn't matter."""
    spec = _kind(kind)
    if not isinstance(members, Mapping):
        raise ContainerError("members must be a mapping of name to bytes")
    if len(members) > MAX_MEMBERS:
        raise ContainerError(f"more than {MAX_MEMBERS} members")
    for name, data in members.items():
        _check_name(name)
        if not _allowed(spec, name):
            raise ContainerError(f"member {name!r} isn't allowed in a {kind} container")
        if not isinstance(data, (bytes, bytearray)):
            raise ContainerError(f"member {name} must be bytes")
        _check_artefact(name, bytes(data))
    _check_member_set(spec, members)
    order = _canonical_order(spec, members)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in order:
            data = bytes(members[name])
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tf.addfile(info, io.BytesIO(data))
    natural = sum(BLOCK + -(-len(members[n]) // BLOCK) * BLOCK for n in order) + 2 * BLOCK
    raw = buf.getvalue()
    if any(raw[natural:]):  # tarfile pads its record with zeros; anything else is a bug
        raise ContainerError("unexpected bytes after the end marker from tarfile")
    return raw[:natural] + b"\x00" * (padded_length(natural) - natural)


# ── Reading ──

def _octal(field: bytes, digits: int, terminator: bytes, what: str) -> int:
    """Exactly the bytes tarfile writes: `digits` octal digits, then the terminator. One encoding per value,
    so a reader in another language accepts exactly the same headers and no bytes can hide in the field."""
    body, tail = field[:digits], field[digits:]
    if len(body) != digits or tail != terminator or any(c not in b"01234567" for c in body):
        raise ContainerError(f"malformed {what} field (expected {digits} octal digits and the fixed terminator)")
    return int(body, 8)


def _zero(field: bytes) -> bool:
    return not any(field)


def _parse_header(hdr: bytes) -> tuple[str, int]:
    stored = _octal(hdr[148:156], 6, b"\x00 ", "checksum")
    if sum(hdr[:148]) + 8 * 32 + sum(hdr[156:]) != stored:
        raise ContainerError("bad header checksum")
    if hdr[156:157] != b"0":
        raise ContainerError("only plain regular files are allowed (no links, directories or extended headers)")
    if hdr[257:263] != b"ustar\x00" or hdr[263:265] != b"00":
        raise ContainerError("not a strict ustar header")
    raw_name = hdr[0:100]
    end = raw_name.find(b"\x00")
    name_bytes = raw_name if end < 0 else raw_name[:end]
    if end >= 0 and any(raw_name[end:]):
        raise ContainerError("member name field has bytes after its terminator")
    try:
        name = name_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise ContainerError("member name isn't ASCII") from None
    _check_name(name)
    if (_octal(hdr[100:108], 7, b"\x00", "mode") != 0o644 or _octal(hdr[108:116], 7, b"\x00", "uid") != 0
            or _octal(hdr[116:124], 7, b"\x00", "gid") != 0 or _octal(hdr[136:148], 11, b"\x00", "mtime") != 0):
        raise ContainerError(f"member {name} has metadata other than mode 0644, uid/gid 0 and mtime 0")
    if not _zero(hdr[157:257]):
        raise ContainerError(f"member {name} has a link name")
    if not (_zero(hdr[265:297]) and _zero(hdr[297:329])):
        raise ContainerError(f"member {name} has a user or group name")
    if not _zero(hdr[329:345]):
        raise ContainerError(f"member {name} has device fields (they must be all zero bytes)")
    if not _zero(hdr[345:500]):
        raise ContainerError(f"member {name} has a prefix")
    if not _zero(hdr[500:512]):
        raise ContainerError(f"member {name} has trailing header bytes")
    return name, _octal(hdr[124:136], 11, b"\x00", "size")


def unpack(kind: str, data: bytes) -> dict[str, bytes]:
    """Read a container strictly. Returns the members in canonical order."""
    spec = _kind(kind)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContainerError("container data must be bytes")
    data = bytes(data)
    if len(data) < 2 * BLOCK or len(data) % BLOCK:
        raise ContainerError("container is truncated or isn't a whole number of 512-byte blocks")
    members: dict[str, bytes] = {}
    pos = 0
    while True:
        if pos + BLOCK > len(data):
            raise ContainerError("container is truncated (no end marker)")
        header = data[pos:pos + BLOCK]
        if header == _ZERO_BLOCK:
            if data[pos:pos + 2 * BLOCK] != _ZERO_BLOCK * 2:
                raise ContainerError("container is truncated (incomplete end marker)")
            if any(data[pos + 2 * BLOCK:]):
                raise ContainerError("non-zero bytes after the end marker")
            natural = pos + 2 * BLOCK
            break
        if len(members) >= MAX_MEMBERS:
            raise ContainerError(f"more than {MAX_MEMBERS} members")
        name, size = _parse_header(header)
        start = pos + BLOCK
        stop = start + -(-size // BLOCK) * BLOCK
        if stop > len(data):
            raise ContainerError(f"member {name} is truncated")
        if any(data[start + size:stop]):
            raise ContainerError(f"member {name} has non-zero padding")
        if name in members:
            raise ContainerError(f"duplicate member {name}")
        if not _allowed(spec, name):
            raise ContainerError(f"member {name!r} isn't allowed in a {kind} container")
        content = data[start:start + size]
        _check_artefact(name, content)
        members[name] = content
        pos = stop
    _check_member_set(spec, members)
    if list(members) != _canonical_order(spec, members):
        raise ContainerError("members are out of canonical order")
    if len(data) != padded_length(natural):
        raise ContainerError("container isn't padded to its Padmé length")
    return members


def check_hashes(members: Mapping[str, bytes], files: Mapping[str, str], *, require_all: bool = False) -> None:
    """Check members against the signed statement's files map ({name: "sha256-<hex>"})."""
    if not isinstance(files, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ContainerError("the signed files map must map names to sha256 strings")
    if not isinstance(members, Mapping) or not all(isinstance(v, (bytes, bytearray)) for v in members.values()):
        raise ContainerError("members must map names to bytes")
    for name, expected in files.items():
        if name not in members:
            raise ContainerError(f"{name} is missing but listed in the signed files map")
        if "sha256-" + hashlib.sha256(members[name]).hexdigest() != expected:
            raise ContainerError(f"{name} doesn't match its signed hash")
    if require_all:
        extra = sorted(set(members) - set(files))
        if extra:
            raise ContainerError(f"{', '.join(extra)} not listed in the signed files map")


# Members a Slice's signed files map never lists, because something else binds them: the statements are
# signed over their exact bytes, the manifest by the §15.3 binding rules, artefacts by their content-hash
# names (checked by unpack).
_SLICE_BOUND_ELSEWHERE = frozenset({"irp/content.json", "irp/content.sig", "irp/disclosure.json",
                                    "irp/disclosure.sig", "manifest.json"})


def check_binding(kind: str, members: Mapping[str, bytes], files: Mapping[str, str]) -> None:
    """The §15.3 rule for a Slice: every member not bound elsewhere is listed in the signed files map with
    a matching hash, and every listed file is present. This is also what makes member bytes trustworthy:
    unpack can't tell a member from the same bytes zero-extended into its padding, the binding can."""
    _kind(kind)
    if kind != "rekadu":
        raise ContainerError(f"a {kind} container is bound by its checkpoint, not by a Slice files map")
    check_hashes(members, files)
    unbound = sorted(n for n in members
                     if n not in files and n not in _SLICE_BOUND_ELSEWHERE and not _ARTEFACT.fullmatch(n))
    if unbound:
        raise ContainerError(f"{', '.join(unbound)} not listed in the signed files map")
