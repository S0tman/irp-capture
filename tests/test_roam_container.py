"""Roaming IRP, Cut 1 step 2.3: the strict ustar container (spec v0.3 §15.4).

Every relay object is a small tar archive inside the age envelope. The writer
uses stdlib tarfile with fixed, zeroed metadata and a canonical member order per
kind; the reader is our own strict parser; the archive is padded (Padmé) so
the relay learns as little as practical from sizes.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from irp.roam.container import (  # noqa: E402
    MAX_MEMBERS,
    MIN_PADDED,
    ContainerError,
    check_hashes,
    pack,
    padded_length,
    padme,
    unpack,
)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


ARTEFACT = b"artefact bytes"
ARTEFACT_NAME = f"artefacts/sha256-{_sha(ARTEFACT)}.txt"


def slice_members(tsr=True, artefact=False):
    m = {
        "irp/content.json": b'{"v":1}',
        "irp/content.sig": b"sig-c",
        "irp/disclosure.json": b'{"v":1,"d":1}',
        "irp/disclosure.sig": b"sig-d",
        "IRP-RETURN.txt": b"return text",
        "irp/checkpoint.json": b'{"ck":1}',
        "irp/checkpoint.sig": b"sig-k",
        "irp/devices.jsonl": b'{"dev":1}\n',
        "ledger.jsonl": b'{"id":"x"}\n',
        "manifest.json": b'{"m":1}',
    }
    if tsr:
        m["irp/checkpoint.tsr"] = b"tsr"
    if artefact:
        m[ARTEFACT_NAME] = ARTEFACT
    return m


def custodian_members(optional=True):
    m = {
        "irp/checkpoint.json": b'{"ck":1}',
        "irp/checkpoint.sig": b"sig",
        "irp/checkpoint.mac": b"mac",
        "irp/body.json": b'{"body":1}',
        "irp/devices.jsonl": b'{"dev":1}\n',
    }
    if optional:
        m["irp/checkpoint.tsr"] = b"tsr"
        m["irp/policy.json"] = b'{"p":1}'
    return m


SLICE_ORDER = ["irp/content.json", "irp/content.sig", "irp/disclosure.json", "irp/disclosure.sig",
               "IRP-RETURN.txt", ARTEFACT_NAME, "irp/checkpoint.json", "irp/checkpoint.sig",
               "irp/checkpoint.tsr", "irp/devices.jsonl", "ledger.jsonl", "manifest.json"]
CUSTODIAN_ORDER = ["irp/checkpoint.json", "irp/checkpoint.sig", "irp/checkpoint.mac", "irp/body.json",
                   "irp/checkpoint.tsr", "irp/devices.jsonl", "irp/policy.json"]


# ── Round trips and canonical order ──

@pytest.mark.parametrize("kind,members", [
    ("rekadu", slice_members()), ("rekadu", slice_members(tsr=False)), ("rekadu", slice_members(artefact=True)),
    ("custodian", custodian_members()), ("custodian", custodian_members(optional=False)),
    ("segment", {"seg": b"segment bytes" * 100}),
])
def test_round_trip(kind, members):
    data = pack(kind, members)
    out = unpack(kind, data)
    assert dict(out) == members


def test_member_order_is_canonical_whatever_the_input_order():
    m = slice_members(artefact=True)
    shuffled = dict(reversed(list(m.items())))
    a, b = pack("rekadu", m), pack("rekadu", shuffled)
    assert a == b
    assert list(unpack("rekadu", a)) == SLICE_ORDER
    assert list(unpack("custodian", pack("custodian", custodian_members()))) == CUSTODIAN_ORDER


def test_packing_is_deterministic():
    assert pack("segment", {"seg": b"x"}) == pack("segment", {"seg": b"x"})


def test_metadata_is_zeroed_and_plain():
    data = pack("rekadu", slice_members())
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for ti in tf.getmembers():
            assert ti.type == tarfile.REGTYPE
            assert (ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode) == (0, 0, 0, "", "", 0o644)
    assert data[257:263] == b"ustar\x00" and data[263:265] == b"00"


# ── Padmé ──

def test_padme_exact_values():
    assert padme(1000) == 1024
    assert padme(100000) == 100352
    assert padded_length(1000) == MIN_PADDED == 16384
    assert padded_length(100000) == 100352


@pytest.mark.parametrize("n", [1, 511, 512, 513, 16383, 16384, 16385, 70000, 1 << 20, (1 << 20) + 1, 9_999_999])
def test_padme_bounds(n):
    p = padded_length(n)
    assert p >= n and p % 512 == 0 and p >= MIN_PADDED
    if n > MIN_PADDED:
        assert p - n <= max(511, n // 8)  # Padmé overhead stays small (about 12% at most)


def test_packed_size_is_padded():
    for kind, members in (("rekadu", slice_members()), ("segment", {"seg": b"y" * 50_000})):
        data = pack(kind, members)
        assert len(data) % 512 == 0 and len(data) >= MIN_PADDED
        natural = sum(512 + -(-len(v) // 512) * 512 for v in members.values()) + 1024
        assert len(data) == padded_length(natural)


# ── Other readers can open it ──

def test_stdlib_tarfile_reads_it():
    data = pack("rekadu", slice_members())
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        assert tf.extractfile("ledger.jsonl").read() == b'{"id":"x"}\n'


@pytest.mark.skipif(shutil.which("tar") is None, reason="no system tar")
def test_system_tar_reads_it(tmp_path):
    f = tmp_path / "c.tar"
    f.write_bytes(pack("rekadu", slice_members()))
    out = subprocess.run(["tar", "-tf", str(f)], capture_output=True, text=True, check=True)
    assert out.stdout.split() == [n for n in SLICE_ORDER if n != ARTEFACT_NAME]


# ── The writer refuses bad input ──

@pytest.mark.parametrize("name", ["../x", "/abs", "irp/../ledger.jsonl", "café.txt", "a" * 101, "unknown.txt",
                                  "artefacts/sha256-zz.txt", "irp/", ""])
def test_pack_refuses_bad_names(name):
    m = slice_members()
    m[name] = b"x"
    with pytest.raises(ContainerError):
        pack("rekadu", m)


def test_pack_refuses_missing_required_members():
    m = slice_members()
    del m["manifest.json"]
    with pytest.raises(ContainerError, match="manifest.json"):
        pack("rekadu", m)


@pytest.mark.parametrize("kind", ["report", "control", "capsule", "nope"])
def test_pack_refuses_reserved_and_unknown_kinds(kind):
    with pytest.raises(ContainerError):
        pack(kind, {"seg": b"x"})


def test_pack_refuses_non_bytes():
    with pytest.raises(ContainerError):
        pack("segment", {"seg": "text"})


def test_pack_refuses_a_mismatched_artefact_name():
    m = slice_members()
    m[f"artefacts/sha256-{'0' * 64}.txt"] = b"not matching"
    with pytest.raises(ContainerError, match="artefact"):
        pack("rekadu", m)


# ── The reader refuses bad archives ──

def _tar(entries, fmt=tarfile.USTAR_FORMAT):
    """Build an archive with tarfile directly, bypassing our writer's checks."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for name, data, kw in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0o644, 0, 0, 0, "", ""
            for k, v in kw.items():
                setattr(ti, k, v)
            tf.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)
    raw = buf.getvalue()
    return raw + b"\x00" * (padded_length(len(raw)) - len(raw))


def _seg(**kw):
    return [("seg", b"segment", kw)]


def test_unpack_refuses_a_bad_checksum():
    data = bytearray(pack("segment", {"seg": b"x"}))
    data[0] ^= 0x01  # change the name, so the header checksum no longer matches
    with pytest.raises(ContainerError, match="checksum"):
        unpack("segment", bytes(data))


@pytest.mark.parametrize("kw", [{"type": tarfile.DIRTYPE}, {"type": tarfile.SYMTYPE, "linkname": "x"},
                                {"type": tarfile.LNKTYPE, "linkname": "x"}])
def test_unpack_refuses_links_and_directories(kw):
    with pytest.raises(ContainerError):
        unpack("segment", _tar([("seg", b"", kw)]))


@pytest.mark.parametrize("kw", [{"mtime": 1}, {"mode": 0o755}, {"uid": 1}, {"gid": 1}, {"uname": "root"},
                                {"gname": "wheel"}])
def test_unpack_refuses_non_canonical_metadata(kw):
    with pytest.raises(ContainerError):
        unpack("segment", _tar(_seg(**kw)))


def test_unpack_refuses_duplicates():
    with pytest.raises(ContainerError, match="duplicate"):
        unpack("segment", _tar([("seg", b"a", {}), ("seg", b"b", {})]))


@pytest.mark.parametrize("name", ["../seg", "/seg", "other"])
def test_unpack_refuses_names_off_the_allowlist(name):
    with pytest.raises(ContainerError):
        unpack("segment", _tar([(name, b"a", {})]))


def test_unpack_refuses_more_than_64_members():
    entries = [(n, v, {}) for n, v in slice_members().items()]
    for i in range(MAX_MEMBERS):
        blob = b"art%d" % i
        entries.append((f"artefacts/sha256-{_sha(blob)}.bin", blob, {}))
    with pytest.raises(ContainerError, match="64"):
        unpack("rekadu", _tar(entries))


def test_unpack_refuses_trailing_non_zero_bytes():
    data = bytearray(pack("segment", {"seg": b"x"}))
    data[-1] = 1
    with pytest.raises(ContainerError, match="after the end"):
        unpack("segment", bytes(data))


def test_unpack_refuses_non_zero_member_padding():
    data = bytearray(pack("segment", {"seg": b"x"}))
    data[512 + 1] = 0x41  # the byte after the 1-byte member, inside its 512-byte block
    with pytest.raises(ContainerError, match="padding"):
        unpack("segment", bytes(data))


def test_unpack_refuses_wrong_order():
    m = slice_members()
    entries = [(n, m[n], {}) for n in reversed([x for x in SLICE_ORDER if x in m])]
    with pytest.raises(ContainerError, match="order"):
        unpack("rekadu", _tar(entries))


def test_unpack_refuses_missing_required_members():
    m = slice_members()
    entries = [(n, m[n], {}) for n in SLICE_ORDER if n in m and n != "ledger.jsonl"]
    with pytest.raises(ContainerError, match="ledger.jsonl"):
        unpack("rekadu", _tar(entries))


def test_unpack_refuses_truncation_and_garbage():
    data = pack("segment", {"seg": b"x" * 600})
    for bad in (data[:700], data[:511], b"", b"\x00" * 1024, b"not a tar" * 100):
        with pytest.raises(ContainerError):
            unpack("segment", bad)


def test_unpack_refuses_a_mismatched_artefact():
    m = slice_members()
    entries = [(n, m[n], {}) for n in SLICE_ORDER if n in m]
    entries.insert(5, (f"artefacts/sha256-{'0' * 64}.txt", b"wrong", {}))
    with pytest.raises(ContainerError, match="artefact"):
        unpack("rekadu", _tar(entries))


def test_unpack_refuses_gnu_and_pax_formats():
    for fmt in (tarfile.GNU_FORMAT, tarfile.PAX_FORMAT):
        with pytest.raises(ContainerError):
            unpack("segment", _tar(_seg(), fmt=fmt) if fmt == tarfile.GNU_FORMAT else
                   _tar([("seg", b"x", {"pax_headers": {"comment": "x"}})], fmt=fmt))


# ── Sizes and hashes against the signed statement ──

def test_check_hashes_against_the_files_map():
    members = slice_members()
    files = {n: "sha256-" + _sha(members[n]) for n in ("IRP-RETURN.txt", "ledger.jsonl", "irp/devices.jsonl")}
    check_hashes(members, files)
    with pytest.raises(ContainerError, match="ledger.jsonl"):
        check_hashes({**members, "ledger.jsonl": b"tampered"}, files)
    with pytest.raises(ContainerError, match="missing"):
        check_hashes({k: v for k, v in members.items() if k != "ledger.jsonl"}, files)
    with pytest.raises(ContainerError, match="not listed"):
        check_hashes(members, files, require_all=True)


# ── Review round 1 ──

def _patch(data: bytes, offset_in_header: int, value: bytes, member: int = 0) -> bytes:
    """Overwrite header bytes of the n-th member and recompute its checksum."""
    buf = bytearray(data)
    pos = 0
    for _ in range(member):
        size = int(buf[pos + 124:pos + 136].rstrip(b"\x00 ") or b"0", 8)
        pos += 512 + -(-size // 512) * 512
    buf[pos + offset_in_header:pos + offset_in_header + len(value)] = value
    hdr = buf[pos:pos + 512]
    chk = sum(hdr[:148]) + 8 * 32 + sum(hdr[156:])
    buf[pos + 148:pos + 156] = b"%06o\x00 " % chk
    return bytes(buf)


def test_padded_length_follows_padme_above_the_floor():
    assert padded_length(70000) == 71680       # roundup512 alone would give 70144
    assert padded_length(9_999_999) == 10223616
    data = pack("segment", {"seg": b"z" * 70000})  # natural = 512 + 70144 + 1024 = 71680
    assert len(data) == 71680
    data = pack("segment", {"seg": b"z" * 80000})  # natural = 512 + 80384 + 1024 = 81920 -> 81920
    assert len(data) == 81920


@pytest.mark.parametrize("flag", [b"\x00", b"7", b"1", b"5", b"x", b"g"])
def test_unpack_refuses_any_typeflag_but_zero(flag):
    data = _patch(pack("segment", {"seg": b"x"}), 156, flag)
    with pytest.raises(ContainerError, match="regular files"):
        unpack("segment", data)


def test_unpack_refuses_off_list_names_in_otherwise_complete_archives():
    with pytest.raises(ContainerError, match="isn't allowed"):
        unpack("segment", _tar([("seg", b"a", {}), ("zzz", b"b", {})]))
    m = slice_members()
    entries = [(n, m[n], {}) for n in SLICE_ORDER if n in m] + [("zz-extra.json", b"{}", {})]
    with pytest.raises(ContainerError, match="isn't allowed"):
        unpack("rekadu", _tar(entries))


@pytest.mark.parametrize("name,msg", [("../seg", "climbs"), ("/seg", "absolute")])
def test_unpack_refuses_climbing_and_absolute_names(name, msg):
    with pytest.raises(ContainerError, match=msg):
        unpack("segment", _tar([(name, b"a", {})]))


@pytest.mark.parametrize("offset,value,msg", [
    (345, b"evil", "prefix"),               # ustar prefix: tar would read 'evil/seg'
    (4, b"X", "terminator"),                # a byte after the name's NUL ('seg\0X')
    (329, b"0000001\x00", "device"),        # devmajor
    (337, b"0000001\x00", "device"),        # devminor
])
def test_unpack_refuses_hidden_header_bytes(offset, value, msg):
    data = _patch(pack("segment", {"seg": b"x"}), offset, value)
    with pytest.raises(ContainerError, match=msg):
        unpack("segment", data)


def test_unpack_refuses_wrong_padding_length():
    data = pack("segment", {"seg": b"x"})
    natural = 512 + 512 + 1024
    with pytest.raises(ContainerError, match="Padmé"):
        unpack("segment", data[:natural])
    with pytest.raises(ContainerError, match="Padmé"):
        unpack("segment", data + b"\x00" * 512)


def _artefact_entries(n):
    out = []
    for i in range(n):
        blob = b"art%d" % i
        out.append((f"artefacts/sha256-{_sha(blob)}.bin", blob))
    return out


def test_sixty_four_members_is_fine_sixty_five_is_not():
    base = slice_members()  # 11 members
    ok = dict(base, **dict(_artefact_entries(MAX_MEMBERS - len(base))))
    assert len(unpack("rekadu", pack("rekadu", ok))) == MAX_MEMBERS
    too_many = dict(base, **dict(_artefact_entries(MAX_MEMBERS - len(base) + 1)))
    with pytest.raises(ContainerError, match="64"):
        pack("rekadu", too_many)
    entries = [(n, too_many[n], {}) for n in sorted(too_many, key=lambda n: (n not in SLICE_ORDER[:4], SLICE_ORDER.index(n) if n in SLICE_ORDER[:4] else 0, n.encode()))]
    with pytest.raises(ContainerError, match="64"):
        unpack("rekadu", _tar(entries))


@pytest.mark.parametrize("kind,members,first", [
    ("rekadu", slice_members(), ["irp/content.json", "irp/content.sig"]),
    ("custodian", custodian_members(), ["irp/checkpoint.sig", "irp/checkpoint.mac"]),
])
def test_unpack_refuses_swapped_required_members(kind, members, first):
    order = list(unpack(kind, pack(kind, members)))
    i, j = order.index(first[0]), order.index(first[1])
    order[i], order[j] = order[j], order[i]
    with pytest.raises(ContainerError, match="order"):
        unpack(kind, _tar([(n, members[n], {}) for n in order]))


def _files_map(members):
    exempt = {"irp/content.json", "irp/content.sig", "irp/disclosure.json", "irp/disclosure.sig", "manifest.json"}
    return {n: "sha256-" + _sha(v) for n, v in members.items() if n not in exempt}


def test_binding_of_a_slice_against_its_signed_files_map():
    from irp.roam.container import check_binding
    m = slice_members(tsr=False, artefact=True)
    files = _files_map(m)
    check_binding("rekadu", m, files)
    unsigned_artefact = {n: d for n, d in files.items() if not n.startswith("artefacts/")}
    with pytest.raises(ContainerError, match="artefacts/"):
        check_binding("rekadu", m, unsigned_artefact)  # a hash-named artefact still needs the signature
    with pytest.raises(ContainerError, match="irp/checkpoint.tsr"):
        check_binding("rekadu", {**m, "irp/checkpoint.tsr": b"forged token"}, files)  # unsigned tsr
    with pytest.raises(ContainerError, match="ledger.jsonl"):
        check_binding("rekadu", {**m, "ledger.jsonl": b"tampered"}, files)
    with pytest.raises(ContainerError, match="ledger.jsonl"):
        check_binding("rekadu", {**m, "ledger.jsonl": m["ledger.jsonl"] + b"\x00\x00"}, files)  # zero-extended
    with pytest.raises(ContainerError, match="segment"):
        check_binding("segment", {"seg": b"x"}, {})


@pytest.mark.parametrize("kind", [["rekadu"], None, 5])
def test_bad_kind_types_are_container_errors(kind):
    with pytest.raises(ContainerError):
        pack(kind, {"seg": b"x"})
    with pytest.raises(ContainerError):
        unpack(kind, b"\x00" * 1024)


@pytest.mark.parametrize("files", [None, ["ledger.jsonl"], {"ledger.jsonl": 5}, {5: "sha256-x"}])
def test_check_hashes_refuses_a_malformed_files_map(files):
    with pytest.raises(ContainerError):
        check_hashes(slice_members(), files)


# ── Review round 2 ──

@pytest.mark.parametrize("offset,value", [
    (100, b"644\x00\x00\x00\x00\x00"), (100, b"0000644 "), (100, b"00000644"), (100, b" 000644\x00"),
    (108, b"\x00" * 8), (124, b"1\x00" + b"\x00" * 10), (136, b"0\x00" + b"\x00" * 10),
    (329, b"0000000\x00"), (329, b" " * 8),
])
def test_numeric_fields_must_use_the_exact_tarfile_encoding(offset, value):
    data = _patch(pack("segment", {"seg": b"x"}), offset, value)
    with pytest.raises(ContainerError):
        unpack("segment", data)


def test_checksum_field_must_use_the_exact_encoding():
    data = bytearray(pack("segment", {"seg": b"x"}))
    good = bytes(data[148:156])
    digits = good[:6].lstrip(b"0")
    for variant in (digits + b"\x00" * (8 - len(digits)), b"00" + good[:6] + b"\x00"[:0], good[:6] + b"  "):
        bad = bytearray(data)
        bad[148:156] = variant[:8].ljust(8, b"\x00")
        if bytes(bad[148:156]) == good:
            continue
        with pytest.raises(ContainerError, match="checksum"):
            unpack("segment", bytes(bad))


@pytest.mark.parametrize("offset,value,msg", [(157, b"evil", "link name"), (500, b"X", "trailing"),
                                              (511, b"\x01", "trailing")])
def test_unpack_refuses_link_name_and_tail_bytes_on_a_regular_file(offset, value, msg):
    data = _patch(pack("segment", {"seg": b"x"}), offset, value)
    with pytest.raises(ContainerError, match=msg):
        unpack("segment", data)
