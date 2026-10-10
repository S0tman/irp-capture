"""Roaming IRP, Cut 1 step 2.6: the TSA client and its token checks (spec v0.3 §18.4, §18a, §23).

A checkpoint SHOULD carry one RFC 3161 token over its exact header bytes. These tests hold `irp/roam/tsa.py` to
§18a: the closed `tsa.json` schema; the transport (in-process http.client over TLS, no redirects, no proxy, a
20-second deadline per TSA that includes the name lookup, a 64 KiB cap, Basic auth and client certificates);
C3 (the client key reaches `ssl` only as a PKCS#8 file under a one-time passphrase, deleted straight after);
checks 1 to 6 with their labels; and C2's failover, where a token that fails only the pin is kept aside while
the next TSA is tried. Every network test talks to a FakeTSA on 127.0.0.1 over real TLS (tests/roam_faketsa.py).
No credential may reach a log line, an alert, an exception message or a repr.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("cryptography")
pytest.importorskip("asn1crypto")
pytest.importorskip("rfc8785")

import roam_faketsa as kit  # noqa: E402
from asn1crypto import keys as akeys  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from roam_faketsa import ORG, T, CountingListener, FakeTSA, StalledResolver, TokenOptions  # noqa: E402

from irp.integrity import rfc3161  # noqa: E402
from irp.integrity.canonical import canonicalize  # noqa: E402
from irp.roam import tsa  # noqa: E402
from irp.roam.tsa import TsaConfigError, TsaEntry, TsaPin  # noqa: E402

HEADER = b'{"kind":"checkpoint","seq":1,"v":1}'
DIGEST = hashlib.sha256(HEADER).digest()
SYSTEM_PYTHON = Path("/usr/bin/python3")


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return kit.PKI(tmp_path_factory.mktemp("pki"))


@pytest.fixture(autouse=True)
def fresh_tls_notes(monkeypatch):
    # The TLS-below-1.3 note is once per process and TSA name; each test starts with none given.
    monkeypatch.setattr(tsa, "_tls_noted", set())


@pytest.fixture
def keys_dir(tmp_path):
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    (d / "keystore.bin").write_bytes(b"not a real keystore")
    return d


def pins(pki, subject_o=ORG):
    return (TsaPin("tsa-a", pki.tsa_ca.pin, subject_o),)


def check(token, pki, *, pin_list=None, now=T, created_at=None):
    return tsa.check_token(token, HEADER, pins(pki) if pin_list is None else pin_list, now=now,
                           created_at=created_at)


def entry(name, url, *, auth="none", ca=None, subject_o=ORG):
    return TsaEntry(name=name, url=url, auth=auth, ca_sha256=(ca,), subject_o=subject_o)


def run(entries, pki, keys_dir, *, creds=None, clock=lambda: T, created_at=T, timeout=5.0, resolve=None,
        factory=None, **kw):
    return tsa.stamp(HEADER, entries, creds=creds or {}, keys_dir=keys_dir, clock=clock, created_at=created_at,
                     timeout=timeout, resolver=resolve or kit.resolver(),
                     context_factory=factory or kit.context_factory(pki), **kw)


def reasons(result):
    return [(a.name, a.reason) for a in result.alerts if a.reason != "tls_below_1_3"]


# ── tsa.json ──

def good_list(pki, **over):
    e = {"name": "tsa-a", "url": "https://tsa-a.test/tsr", "auth": "basic", "ca_sha256": [pki.tsa_ca.pin],
         "subject_o": ORG}
    e.update(over)
    return {"v": 1, "tsas": [e, {"name": "tsa-b", "url": "https://tsa-b.test:8443/", "auth": "client_cert",
                                 "ca_sha256": [pki.wrong_ca.pin, pki.tsa_ca.pin]}]}


def test_constants():
    assert tsa.TSA_PATH == Path("~/.irp-roam/local/tsa.json")
    assert tsa.TSA_DEADLINE == 20
    assert tsa.MAX_REPLY == 64 * 1024
    assert tsa.MAX_TOKEN == 64 * 1024
    assert (tsa.PRESENT, tsa.UNVERIFIED, tsa.NONE) == ("PRESENT", "UNVERIFIED", "NONE")
    assert "qualified" not in tsa.LABEL_TEXT and "checked by hand" in tsa.LABEL_TEXT


def test_tsa_list_round_trip(pki):
    raw = canonicalize(good_list(pki))
    parsed = tsa.parse_tsa_list(raw)
    assert [e.name for e in parsed.entries] == ["tsa-a", "tsa-b"]
    assert parsed.entries[0] == TsaEntry("tsa-a", "https://tsa-a.test/tsr", "basic", (pki.tsa_ca.pin,), ORG)
    assert parsed.entries[1].subject_o is None
    assert parsed.digest == "sha256-" + hashlib.sha256(raw).hexdigest()
    assert tsa.encode_tsa_list(parsed.entries) == raw


@pytest.mark.parametrize("change", [
    {"v": 2}, {"v": True}, {"extra": 1}, {"tsas": []},
    {"tsas": [{"name": f"t{i}", "url": "https://t.test/", "auth": "none", "ca_sha256": ["a" * 64]}
              for i in range(5)]},
])
def test_tsa_list_top_level_rejected(pki, change):
    obj = good_list(pki)
    obj.update(change)
    with pytest.raises(TsaConfigError):
        tsa.parse_tsa_list(canonicalize(obj))


@pytest.mark.parametrize("over", [
    {"name": "TSA-A"}, {"name": ""}, {"name": "a" * 33}, {"name": "tsa_a"}, {"name": "tsa-b"},
    {"url": "http://tsa-a.test/tsr"}, {"url": "https://u:s3cret@tsa-a.test/tsr"}, {"url": "https://tsa-a.test/?q=1"},
    {"url": "https://tsa-a.test/#f"}, {"url": "https:///tsr"}, {"url": "https://tsa-a.test:0/"},
    {"url": "https://tsa-a.test:99999/"}, {"url": "https://tsa a.test/"}, {"url": "ftp://tsa-a.test/"},
    {"url": "HTTPS://tsa-a.test/"}, {"url": 5},
    {"auth": "digest"}, {"auth": None},
    {"ca_sha256": []}, {"ca_sha256": ["a" * 64] * 2}, {"ca_sha256": ["A" * 64]}, {"ca_sha256": ["a" * 63]},
    {"ca_sha256": [f"{i:064x}" for i in range(5)]}, {"ca_sha256": "a" * 64},
    {"subject_o": ""}, {"subject_o": 7}, {"subject_o": "x" * 65}, {"subject_o": "bad\nline"}, {"subject_o": None},
    {"unknown": "x"},
])
def test_tsa_entry_rejected(pki, over):
    with pytest.raises(TsaConfigError) as e:
        tsa.parse_tsa_list(canonicalize(good_list(pki, **over)))
    assert "s3cret" not in str(e.value)


BRACKETED = {"https://[::1/x": "::1", "https://[secret-host]/": "secret-host", "https://u:p@[bad]/": "[bad]",
             "https://ex]ample.org/": "ex]ample", "https://[v1.x]/": "v1.x"}


@pytest.mark.parametrize("url", sorted(BRACKETED))
def test_a_tsa_url_whose_brackets_dont_hold_an_ipv6_address_is_a_config_error(pki, url):
    """urlsplit raises ValueError on some of these (which one depends on the interpreter): each is a
    TsaConfigError on every interpreter, and no message repeats the url's host or userinfo."""
    with pytest.raises(TsaConfigError) as parsed:
        tsa.parse_tsa_list(canonicalize(good_list(pki, url=url)))
    with pytest.raises(TsaConfigError) as encoded:
        tsa.encode_tsa_list([TsaEntry("tsa-a", url, "none", (pki.tsa_ca.pin,), ORG)])
    for e in (parsed, encoded):
        assert type(e.value) is TsaConfigError
        assert BRACKETED[url] not in str(e.value) and "u:p" not in str(e.value) and url not in str(e.value)


def test_a_bracketed_ipv6_tsa_url_is_accepted(pki):
    raw = canonicalize(good_list(pki, url="https://[::1]/x"))
    assert tsa.parse_tsa_list(raw).entries[0].url == "https://[::1]/x"
    assert tsa.parse_tsa_list(canonicalize(good_list(pki, url="https://[::1]:8443/"))).entries[0].url == \
        "https://[::1]:8443/"


def test_tsa_list_must_be_exact_jcs(pki):
    raw = canonicalize(good_list(pki))
    for bad in (raw + b"\n", raw.replace(b'"v":1', b'"v": 1'), b'{"v":1,"v":1,"tsas":[]}', b"[]", b"\xff"):
        with pytest.raises(TsaConfigError):
            tsa.parse_tsa_list(bad)


def test_plain_http_only_through_the_keyword(pki):
    obj = good_list(pki, url="http://tsa-a.test/tsr", auth="none")
    with pytest.raises(TsaConfigError):
        tsa.parse_tsa_list(canonicalize(obj))
    assert tsa.parse_tsa_list(canonicalize(obj), allow_http=True).entries[0].url == "http://tsa-a.test/tsr"
    with pytest.raises(TsaConfigError):
        tsa.parse_tsa_list(canonicalize(good_list(pki, url="http://tsa-a.test/tsr", auth="basic")), allow_http=True)


def test_load_tsa_list(pki, tmp_path):
    path = tmp_path / "local" / "tsa.json"
    assert tsa.load_tsa_list(path) is None
    path.parent.mkdir()
    path.write_bytes(canonicalize(good_list(pki)))
    loaded = tsa.load_tsa_list(path)
    assert loaded is not None and loaded.digest == "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(TsaConfigError):
        tsa.load_tsa_list(path)


def test_pins_keep_their_own_subject_o(pki):
    entries = tsa.parse_tsa_list(canonicalize(good_list(pki))).entries
    assert tsa.pins_from_tsas(entries) == (
        TsaPin("tsa-a", pki.tsa_ca.pin, ORG), TsaPin("tsa-b", pki.wrong_ca.pin, None),
        TsaPin("tsa-b", pki.tsa_ca.pin, None))


# ── Token checks (check_token) ──

def test_good_token_is_present(pki):
    token = kit.build_token(DIGEST, pki)
    c = check(token, pki)
    assert c.label == tsa.PRESENT and c.failed == ()
    assert c.gen_time == T and c.policy == kit.POLICY
    assert c.pin == pins(pki)[0]
    assert c.created_at_skew is False


@pytest.mark.parametrize("opts", [
    TokenOptions(signer="pss"), TokenOptions(signer="rsa"), TokenOptions(signer="rsa", signer_hash="sha384"),
    TokenOptions(signer_hash="sha512"), TokenOptions(ess="v1"), TokenOptions(ess="both"),
    TokenOptions(ess="v2_sha512"), TokenOptions(ess="no_issuer_serial"), TokenOptions(imprint_params="null"),
    TokenOptions(pkup=(datetime(2026, 1, 1), datetime(2026, 12, 31))), TokenOptions(pkup=(None, T)),
    TokenOptions(gen_time_raw=b"20261010120000.25Z"), TokenOptions(leaf_hash="sha384"),
    TokenOptions(signature_algorithm="1.2.840.10045.2.1"), TokenOptions(signature_algorithm="ecdsa"),
    TokenOptions(signer="rsa", signature_algorithm="rsassa_pkcs1v15"),
], ids=lambda o: repr({k: v for k, v in vars(o).items() if v != getattr(TokenOptions(), k)}))
def test_token_variants_that_pass(pki, opts):
    c = check(kit.build_token(DIGEST, pki, opts), pki)
    assert c.label == tsa.PRESENT, c.failed
    assert c.gen_time == T


def test_pss_signed_leaf_under_an_rsa_ca(pki):
    token = kit.build_token(DIGEST, pki, TokenOptions(issuer="rsa", leaf_pss=True, signer="pss"))
    c = check(token, pki, pin_list=(TsaPin("tsa-r", pki.rsa_ca.pin, ORG),))
    assert c.label == tsa.PRESENT, c.failed


FAILURES = [
    ("eku_missing", TokenOptions(eku="missing"), {"check_4"}),
    ("eku_noncritical", TokenOptions(eku="noncritical"), {"check_4"}),
    ("eku_extra_purpose", TokenOptions(eku="extra"), {"check_4"}),
    ("wrong_ca", TokenOptions(issuer="wrong"), {"check_6"}),
    ("ca_missing", TokenOptions(include_ca=False), {"check_6"}),
    ("sha1_imprint", TokenOptions(imprint="sha1"), {"check_1", "imprint", "check_2"}),
    ("mislabelled_imprint", TokenOptions(imprint="mislabelled"), {"check_2"}),
    ("other_imprint", TokenOptions(imprint="other"), {"check_1", "imprint"}),
    ("signer_sha1", TokenOptions(signer_hash="sha1"), {"check_2"}),
    ("ess_missing", TokenOptions(ess="none"), {"check_5"}),
    ("ess_v2_sha1", TokenOptions(ess="v2_sha1"), {"check_5"}),
    ("ess_wrong_hash", TokenOptions(ess="wrong_hash"), {"check_5"}),
    ("ess_wrong_serial", TokenOptions(ess="wrong_serial"), {"check_5"}),
    ("ess_first_entry_other", TokenOptions(ess="second_entry_only"), {"check_5"}),
    ("dup_serial_listed_first", TokenOptions(dup_serial="other_issuer"), {"check_1"}),
    ("dup_issuer_and_serial", TokenOptions(dup_serial="same_issuer"), {"check_1"}),
    ("two_signer_infos", TokenOptions(two_signers=True), {"check_1"}),
    ("ski_sid", TokenOptions(sid="ski"), {"check_1"}),
    ("bad_signature", TokenOptions(bad_signature=True), {"check_1"}),
    ("signature_hash_not_the_digest", TokenOptions(signature_algorithm="sha384_ecdsa"), {"check_1"}),
    ("signed_with_sha1_over_a_sha256_digest", TokenOptions(signature_hash="sha1"), {"check_1"}),
    ("signed_with_sha384_over_a_sha256_digest", TokenOptions(signature_hash="sha384"), {"check_1"}),
    ("signature_algorithm_unknown", TokenOptions(signature_algorithm="1.2.3.4"), {"check_1"}),
    ("rsa_label_on_an_ec_key", TokenOptions(signature_algorithm="sha256_rsa"), {"check_1"}),
    ("two_message_digests", TokenOptions(message_digest_attrs=2), {"check_1"}),
    ("no_content_type_attr", TokenOptions(content_type_attr=0), {"check_3"}),
    ("two_content_type_attrs", TokenOptions(content_type_attr=2), {"check_3"}),
    ("subject_o_mismatch", TokenOptions(subject_o=("Another Unit",)), {"check_6"}),
    ("two_o_attributes", TokenOptions(subject_o=(ORG, ORG)), {"check_6"}),
    ("o_in_multi_valued_rdn", TokenOptions(multi_valued_o=True), {"check_6"}),
    ("leaf_expired", TokenOptions(leaf_not_after=datetime(2026, 10, 1)), {"check_6"}),
    ("leaf_not_yet_valid", TokenOptions(leaf_not_before=datetime(2026, 11, 1)), {"check_6"}),
    ("pkup_ended", TokenOptions(pkup=(datetime(2026, 1, 1), datetime(2026, 6, 1))), {"check_6"}),
    ("pkup_not_started", TokenOptions(pkup=(datetime(2026, 11, 1), None)), {"check_6"}),
    ("leaf_signed_sha1", TokenOptions(leaf_hash="sha1"), {"check_6"}),
    ("leaf_issuer_name_not_the_ca", TokenOptions(issuer_name="other"), {"check_6"}),
    ("ess_issuer_serial_other_issuer", TokenOptions(ess="wrong_issuer"), {"check_5"}),
    ("gen_time_future", TokenOptions(gen_time=T + timedelta(minutes=5, seconds=1)), {"gen_time_future"}),
    ("tst_info_not_der", TokenOptions(tst_ber=True), {"parse"}),
    ("certificate_not_der", TokenOptions(cert_ber=True), {"parse"}),
    ("gen_time_fraction_trailing_zero", TokenOptions(gen_time_raw=b"20261010120000.50Z"), {"parse"}),
]


@pytest.mark.parametrize("name,opts,expected", FAILURES, ids=[f[0] for f in FAILURES])
def test_each_failure_is_unverified(pki, name, opts, expected):
    c = check(kit.build_token(DIGEST, pki, opts), pki)
    assert c.label == tsa.UNVERIFIED
    assert set(c.failed) == expected


def test_imprint_parameters_other_than_null(pki):
    # asn1crypto re-encodes digest parameters as NULL, so the strict re-encoding already refuses these.
    c = check(kit.build_token(DIGEST, pki, TokenOptions(imprint_params="junk")), pki)
    assert c.label == tsa.UNVERIFIED and set(c.failed) & {"parse", "check_2"}


def test_econtent_type_not_tst_info(pki):
    c = check(kit.build_token(DIGEST, pki, TokenOptions(econtent_type="data")), pki)
    assert c.label == tsa.UNVERIFIED and "check_3" in c.failed


@pytest.mark.parametrize("raw", [b"20261010120000", b"20261010120000+0000", b"20261010120000.Z", b"20261310120000Z"])
def test_gen_time_must_carry_z_and_be_real(pki, raw):
    # asn1crypto rewrites some of these as it re-encodes, so the strict re-encoding may refuse them first.
    c = check(kit.build_token(DIGEST, pki, TokenOptions(gen_time_raw=raw)), pki)
    assert c.label == tsa.UNVERIFIED and set(c.failed) & {"parse", "gen_time"}
    assert c.gen_time is None


def test_gen_time_exactly_five_minutes_ahead_is_fine(pki):
    c = check(kit.build_token(DIGEST, pki, TokenOptions(gen_time=T + timedelta(minutes=5))), pki,
              now=T)
    assert c.label == tsa.PRESENT


def test_pin_and_subject_o_are_one_unit(pki):
    token = kit.build_token(DIGEST, pki)
    # Two entries pinning one CA under different subject_o: the matching pair wins.
    two = (TsaPin("tsa-a", pki.tsa_ca.pin, "Other Org"), TsaPin("tsa-b", pki.tsa_ca.pin, ORG))
    c = check(token, pki, pin_list=two)
    assert c.label == tsa.PRESENT and c.pin == two[1]
    # The CA from one pair and the O from another never combine.
    split = (TsaPin("tsa-a", pki.tsa_ca.pin, "Other Org"), TsaPin("tsa-b", pki.wrong_ca.pin, ORG))
    assert check(token, pki, pin_list=split).failed == ("check_6",)
    # Without a subject_o the O isn't looked at.
    assert check(kit.build_token(DIGEST, pki, TokenOptions(subject_o=(ORG, ORG))), pki,
                 pin_list=pins(pki, None)).label == tsa.PRESENT
    assert check(token, pki, pin_list=()).failed == ("check_6",)


def test_subject_o_is_compared_exactly(pki):
    token = kit.build_token(DIGEST, pki)
    for near in (ORG.lower(), ORG + " ", " " + ORG, ORG.replace(" ", "  ")):
        assert check(token, pki, pin_list=pins(pki, near)).label == tsa.UNVERIFIED


def test_created_at_skew(pki):
    token = kit.build_token(DIGEST, pki)
    assert check(token, pki, created_at=T - timedelta(hours=1)).created_at_skew is False
    assert check(token, pki, created_at=T + timedelta(hours=1)).created_at_skew is False
    c = check(token, pki, created_at=T - timedelta(hours=1, seconds=1))
    assert c.created_at_skew is True and c.label == tsa.PRESENT


def test_wrong_header_is_an_imprint_failure(pki):
    token = kit.build_token(DIGEST, pki)
    c = tsa.check_token(token, HEADER + b" ", pins(pki), now=T)
    assert c.label == tsa.UNVERIFIED and "imprint" in c.failed


def test_strict_der_and_size(pki):
    token = kit.build_token(DIGEST, pki)
    body = token[4:] if token[1] == 0x82 else None
    assert body is not None
    for bad in (token + b"\x00", token[:-1], kit.widen(token), b"\x30\x80" + body + b"\x00\x00", b"", b"\x00" * 10,
                token + b"\x00" * (tsa.MAX_TOKEN - len(token) + 1)):
        c = check(bad, pki)
        assert c.label == tsa.UNVERIFIED and c.failed == ("parse",)
    for wrong_type in ("text", None, 5, bytearray(b"\x30\x00")):
        assert check(wrong_type, pki).label == tsa.UNVERIFIED


@pytest.mark.parametrize("data,ok", [
    (b"\x30\x03\x02\x01\x05", True), (b"\x30\x00", True), (b"\xa0\x02\x05\x00", True),
    (b"\x30\x81\x03\x02\x01\x05", False),            # long form where the short one fits
    (b"\x30\x82\x00\x03\x02\x01\x05", False),        # a leading zero length byte
    (b"\x30\x80\x02\x01\x05\x00\x00", False),        # indefinite length
    (b"\x24\x03\x04\x01\x00", False),                  # a constructed OCTET STRING
    (b"\x10\x00", False),                                 # a primitive SEQUENCE
    (b"\x1f\x05\x00", False),                            # high-tag form for a low tag number
    (b"\x00\x00", False),                                 # end-of-contents
    (b"\x30\x03\x02\x01\x05\x00", False),             # trailing byte
    (b"\x30\x04\x02\x01\x05", False),                  # truncated
    (b"", False),
])
def test_der_re_encoding_check(data, ok):
    assert tsa._reencodes(data) is ok


# ── Strict DER anywhere (§18a: "parsed strictly with no trailing bytes and re-encoding to the same bytes") ──

def tlv(tag: int, contents: bytes) -> bytes:
    return bytes([tag]) + kit.der_length(len(contents)) + contents


@pytest.mark.parametrize("data,ok", [
    (tlv(0x02, b"\x00"), True), (tlv(0x02, b"\x00\x80"), True), (tlv(0x02, b"\xff\x7f"), True),
    (tlv(0x02, b"\x7f"), True), (tlv(0x02, b"\x80"), True),
    (tlv(0x02, b"\x00\x03"), False),                  # a non-minimal INTEGER (a redundant leading 00)
    (tlv(0x02, b"\xff\x80"), False),                  # a non-minimal negative INTEGER (a redundant leading FF)
    (tlv(0x02, b""), False),                          # an INTEGER with no contents
    (tlv(0x0A, b"\x00\x01"), False),                  # a non-minimal ENUMERATED
    (tlv(0x01, b"\xff"), True), (tlv(0x01, b"\x00"), True),
    (tlv(0x01, b"\x01"), False),                      # BOOLEAN TRUE other than FF (BER only)
    (tlv(0x01, b"\xff\xff"), False), (tlv(0x01, b""), False),
    (tlv(0x05, b""), True), (tlv(0x05, b"\x00"), False),  # NULL with contents
    (tlv(0x03, b"\x00"), True), (tlv(0x03, b"\x07\x80"), True), (tlv(0x03, b"\x00\xff"), True),
    (tlv(0x03, b""), False),                          # no unused-bits octet
    (tlv(0x03, b"\x01"), False),                      # unused bits in an empty BIT STRING
    (tlv(0x03, b"\x08\x00"), False),                  # more than 7 unused bits
    (tlv(0x03, b"\x01\x01"), False),                  # an unused bit that isn't zero
    (tlv(0x06, bytes.fromhex("551d25")), True), (tlv(0x06, bytes.fromhex("2a864886f70d")), True),
    (tlv(0x06, b"\x80\x01"), False),                  # a subidentifier with a leading 80
    (tlv(0x06, b"\x2a\x80\x86"), False),
    (tlv(0x06, b"\x81"), False),                      # the last subidentifier unfinished
    (tlv(0x06, b""), False),
    (tlv(0x17, b"261010120000Z"), True),
    (tlv(0x17, b"2610101200Z"), False),               # UTCTime without seconds
    (tlv(0x17, b"261010120000+0000"), False),         # UTCTime with an offset
    (tlv(0x18, b"20261010120000Z"), True), (tlv(0x18, b"20261010120000.25Z"), True),
    (tlv(0x18, b"20261010120000.50Z"), False),        # a trailing zero in the fraction
    (tlv(0x18, b"20261010120000.Z"), False), (tlv(0x18, b"20261010120000"), False),
    (tlv(0x18, b"202610101200Z"), False), (tlv(0x18, b"20261010120000,5Z"), False),
    (tlv(0x30, tlv(0x02, b"\x00\x03")), False),       # the same flaws inside a SEQUENCE
    (tlv(0xA0, tlv(0x01, b"\x01")), False),           # and inside an explicit tag
    (kit.nested(60), True), (kit.nested(100), False), (kit.nested(5000), False),  # nesting is bounded
    (b"\x9f\x1f\x00", True),                            # [31] in high-tag form
    (b"\x9f\x80\x1f\x00", False),                        # the same tag number with a leading 80 octet
    (b"\x0d\x02\x81\x01", True),                         # a RELATIVE-OID
    (b"\x0d\x02\x80\x01", False),                        # a RELATIVE-OID subidentifier with a leading 80
    (b"\x0d\x01\x81", False),                            # a RELATIVE-OID's last subidentifier unfinished
])
def test_der_primitive_contents_and_nesting(data, ok):
    assert tsa._reencodes(data) is ok


def test_a_clean_tstinfo_extension_is_still_present(pki):
    """The control for the TSTInfo flaws below: the same extension with nothing wrong in it."""
    c = check(kit.build_token(DIGEST, pki, TokenOptions(der_flaw=kit.CLEAN_TST_EXTENSION)), pki)
    assert c.label == tsa.PRESENT, c.failed


def test_a_clean_extra_certificate_is_still_present(pki):
    """The control for the extra-certificate flaws below: anyone holding a token can add certificates to its
    unsigned set, and a strict-DER one leaves it PRESENT."""
    token = kit.build_token(DIGEST, pki, TokenOptions(der_flaw=kit.CLEAN_EXTRA_CERT))
    assert token != kit.build_token(DIGEST, pki)
    c = check(token, pki)
    assert c.label == tsa.PRESENT, c.failed


@pytest.mark.parametrize("flaw", sorted(kit.DER_FLAWS))
def test_a_token_that_isnt_strict_der_anywhere_is_unverified(pki, flaw):
    """A non-minimal INTEGER, a non-minimal length, an indefinite length or a BER-only construct, in SignedData,
    in the TSTInfo (inside an extension value, which a re-encoding of the TSTInfo leaves as it is) or in the leaf
    certificate (in its TBSCertificate, signed again, or inside an extension value), and nesting too deep to
    parse: each is UNVERIFIED as a parse failure, never PRESENT, and never an exception."""
    token = kit.build_token(DIGEST, pki, TokenOptions(der_flaw=flaw))
    c = check(token, pki)
    assert c.label == tsa.UNVERIFIED and c.failed == ("parse",), kit.DER_FLAWS[flaw]


@pytest.mark.parametrize("flaw", sorted(kit.DER_FLAWS))
def test_a_token_that_isnt_strict_der_is_discarded_at_stamping(pki, keys_dir, flaw):
    with FakeTSA(pki, token=TokenOptions(der_flaw=flaw)) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert r.label == tsa.NONE and r.token is None
    assert reasons(r) == [("tsa-a", "parse_failed")]


def test_a_deeply_nested_token_is_unverified_never_a_crash(pki):
    for blob in (kit.nested(100), kit.nested(5000), b"\x30\x80" * 3000):
        c = check(blob, pki)
        assert c.label == tsa.UNVERIFIED and c.failed == ("parse",)


def test_checking_is_quick(pki):
    token = kit.build_token(DIGEST, pki)
    start = time.perf_counter()
    for _ in range(20):
        assert check(token, pki).label == tsa.PRESENT
    assert (time.perf_counter() - start) / 20 < 0.05


def test_exceptions_never_propagate(pki, monkeypatch):
    token = kit.build_token(DIGEST, pki)

    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(rfc3161, "verify_token", boom)
    c = check(token, pki)
    assert c.label == tsa.UNVERIFIED and "check_1" in c.failed


def test_real_world_token_fixture(pki):
    """The freetsa token kept as a test fixture (§18.4): an ESSCertID v1 token from a live TSA. Pinned on its own
    root and its leaf's O, read from the token itself, it passes every check; unpinned it is UNVERIFIED."""
    from asn1crypto import cms

    fixtures = ROOT / "tests" / "fixtures"
    token = (fixtures / "freetsa-token.tsr").read_bytes()
    message = b"irp-pr2b-test-vector-v1"
    sd = cms.ContentInfo.load(token)["content"]
    certs = [c.chosen for c in sd["certificates"]]
    serial = sd["signer_infos"][0]["sid"].chosen["serial_number"].native
    leaf = next(c for c in certs if c.serial_number == serial)
    ca = next(c for c in certs if c is not leaf)
    org = leaf.subject.native["organization_name"]
    pinned = (TsaPin("fixture", hashlib.sha256(ca.dump()).hexdigest(), org),)
    now = datetime(2026, 7, 1)
    c = tsa.check_token(token, message, pinned, now=now)
    assert c.label == tsa.PRESENT, c.failed
    assert c.gen_time == datetime(2026, 6, 30, 14, 31, 16)
    assert tsa.check_token(token, message, pins(pki), now=now).failed == ("check_6",)


# ── The transport and failover (stamp) ──

def test_stamp_present_without_auth(pki, keys_dir, monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    with FakeTSA(pki) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-a" and r.gen_time == T and r.policy == kit.POLICY
    assert r.alerts == () and r.created_at_skew is False
    assert tsa.check_token(r.token, HEADER, pins(pki), now=T).label == tsa.PRESENT
    assert r.token == a.tokens[0]  # exactly the bytes the TSA sent
    (got,) = a.requests
    assert got.body == rfc3161.build_request(DIGEST, hash_alg="sha256", cert_req=True)
    assert got.path == "/tsr"
    headers = {k.lower(): v for k, v in got.headers}
    assert headers["host"] == f"tsa-a.test:{a.port}"
    assert headers["content-type"] == "application/timestamp-query"
    assert "authorization" not in headers
    assert set(headers) <= {"host", "content-type", "content-length", "accept", "accept-encoding", "connection"}


def test_no_subprocess_is_ever_started(pki, keys_dir, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the TSA client must never start a process")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds=pki.client_creds("tsa-a"))
    assert r.label == tsa.PRESENT


def test_basic_auth(pki, keys_dir):
    creds = {"tsa-a/user": "user-1", "tsa-a/password": "pw-test-1"}
    with FakeTSA(pki, auth="basic") as a:
        r = run([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], pki, keys_dir, creds=creds)
    assert r.label == tsa.PRESENT
    headers = dict(a.requests[0].headers)
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"user-1:pw-test-1").decode()


def test_basic_auth_refused_is_no_answer(pki, keys_dir):
    creds = {"tsa-a/user": "user-1", "tsa-a/password": "wrong-pw"}
    with FakeTSA(pki, auth="basic") as a:
        r = run([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], pki, keys_dir, creds=creds)
    assert r.label == tsa.NONE and r.token is None
    (alert,) = r.alerts
    assert (alert.name, alert.reason, alert.status, alert.content_type) == ("tsa-a", "http_status", 401,
                                                                            "text/plain")


@pytest.mark.parametrize("creds,reason", [
    ({}, "missing_credential"),
    ({"tsa-a/user": "user-1"}, "missing_credential"),
    ({"tsa-a/user": "user:1", "tsa-a/password": "pw"}, "bad_credential"),
    ({"tsa-a/user": "", "tsa-a/password": "pw"}, "bad_credential"),
    ({"tsa-a/user": "user-1", "tsa-a/password": "pw\r\nX-Other: 1"}, "bad_credential"),
    ({"tsa-b/user": "user-1", "tsa-b/password": "pw"}, "missing_credential"),
])
def test_basic_credentials_missing_or_unusable(pki, keys_dir, creds, reason):
    with FakeTSA(pki, auth="basic") as a:
        r = run([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], pki, keys_dir, creds=creds)
    assert r.label == tsa.NONE and reasons(r) == [("tsa-a", reason)]
    assert a.requests == []


def test_client_certificate(pki, keys_dir):
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds=pki.client_creds("tsa-a"))
    assert r.label == tsa.PRESENT and reasons(r) == []
    assert a.requests[0].peer_cn == "client-1"
    assert "authorization" not in {k.lower() for k, _ in a.requests[0].headers}
    assert sorted(p.name for p in keys_dir.iterdir()) == ["keystore.bin"]


def test_client_certificate_traditional_rsa_key(pki, keys_dir):
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds=pki.client_creds("tsa-a", rsa_traditional=True))
    assert r.label == tsa.PRESENT
    assert sorted(p.name for p in keys_dir.iterdir()) == ["keystore.bin"]


def test_client_certificate_missing_or_unusable(pki, keys_dir):
    good = pki.client_creds("tsa-a")
    mismatched = dict(good)
    mismatched["tsa-a/key_pem"] = kit.key_pem(pki.client_rsa_key)
    cases = [({}, "missing_credential"), ({"tsa-a/cert_pem": good["tsa-a/cert_pem"]}, "missing_credential"),
             (mismatched, "client_key_failed"),
             (dict(good, **{"tsa-a/key_pem": "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"}),
              "client_key_failed"),
             (dict(good, **{"tsa-a/cert_pem": "not a certificate"}), "client_key_failed")]
    with FakeTSA(pki, auth="client_cert") as a:
        for creds, reason in cases:
            r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir, creds=creds)
            assert r.label == tsa.NONE and reasons(r) == [("tsa-a", reason)]
            assert sorted(p.name for p in keys_dir.iterdir()) == ["keystore.bin"]
    assert a.requests == []


def test_client_certificate_below_tls_13_alerts_once(pki, keys_dir):
    with FakeTSA(pki, auth="client_cert", tls_max_12=True) as a:
        e = [entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)]
        first = run(e, pki, keys_dir, creds=pki.client_creds("tsa-a"))
        second = run(e, pki, keys_dir, creds=pki.client_creds("tsa-a"))
    assert first.label == second.label == tsa.PRESENT
    assert [(x.name, x.reason) for x in first.alerts] == [("tsa-a", "tls_below_1_3")]
    assert second.alerts == ()


@pytest.mark.skipif(not ssl.HAS_TLSv1_3, reason="this ssl build has no TLS 1.3")
def test_client_certificate_over_tls_13_has_no_alert(pki, keys_dir):
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds=pki.client_creds("tsa-a"))
    assert r.label == tsa.PRESENT and r.alerts == ()
    assert a.requests[0].tls_version == "TLSv1.3"


def test_the_context_is_hardened_and_must_verify(pki, keys_dir):
    made = []

    def factory():
        ctx = kit.context_factory(pki)()
        ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        made.append(ctx)
        return ctx
    with FakeTSA(pki) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir, factory=factory)
        assert r.label == tsa.PRESENT
        assert made[0].minimum_version >= ssl.TLSVersion.TLSv1_2

        def lax():
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        with pytest.raises(TsaConfigError):
            run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir, factory=lax)


@pytest.mark.skipif(ssl.OPENSSL_VERSION.startswith("LibreSSL"),
                    reason="LibreSSL builds (Apple's /usr/bin/python3) ignore SSL_CERT_FILE")
def test_default_context_honours_ssl_cert_file(pki, keys_dir, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.tls_ca_file))
    with FakeTSA(pki) as a:
        r = tsa.stamp(HEADER, [entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], creds={}, keys_dir=keys_dir,
                      clock=lambda: T, created_at=T, timeout=5.0, resolver=kit.resolver())
    assert r.label == tsa.PRESENT


def test_tls_failures(pki, keys_dir):
    with FakeTSA(pki) as a:
        untrusted = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir,
                        factory=kit.context_factory(pki, cafile=pki.other_tls_ca_file))
        wrong_name = run([entry("tsa-z", a.url("tsa-z.test"), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert reasons(untrusted) == [("tsa-a", "tls_failed")] and untrusted.label == tsa.NONE
    assert reasons(wrong_name) == [("tsa-z", "tls_failed")]
    assert a.requests == []


def test_connection_refused(pki, keys_dir):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = run([entry("tsa-a", f"https://tsa-a.test:{port}/", ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert reasons(r) == [("tsa-a", "connect_failed")]


def test_a_local_tls_setup_failure_is_an_alert(pki, keys_dir):
    def broken():
        raise ssl.SSLError("no store")
    r = run([entry("tsa-a", "https://tsa-a.test/", ca=pki.tsa_ca.pin)], pki, keys_dir, factory=broken)
    assert r.label == tsa.NONE and reasons(r) == [("tsa-a", "tls_failed")]


def test_anything_unforeseen_fails_over(pki, keys_dir, monkeypatch):
    real = tsa._exchange
    calls = []

    def flaky(entry, *a, **k):
        calls.append(entry.name)
        if entry.name == "tsa-a":
            raise RuntimeError("unforeseen")
        return real(entry, *a, **k)
    monkeypatch.setattr(tsa, "_exchange", flaky)
    with FakeTSA(pki) as a, FakeTSA(pki) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "transport_failed")] and calls == ["tsa-a", "tsa-b"]


def test_resolver_failure(pki, keys_dir):
    def broken(host, port):
        raise socket.gaierror("no such name")
    r = run([entry("tsa-a", "https://tsa-a.test/", ca=pki.tsa_ca.pin)], pki, keys_dir, resolve=broken)
    assert reasons(r) == [("tsa-a", "resolve_failed")]


def two(a, b, pki):
    return [entry("tsa-a", a.url("tsa-a.test"), ca=pki.tsa_ca.pin),
            entry("tsa-b", b.url("tsa-b.test"), ca=pki.tsa_ca.pin)]


def test_redirect_is_never_followed(pki, keys_dir):
    with CountingListener() as target, FakeTSA(pki, behaviour="redirect") as a, FakeTSA(pki) as b:
        a.redirect_to = f"https://127.0.0.1:{target.port}/tsr"
        r = run(two(a, b, pki), pki, keys_dir)
        time.sleep(0.3)
        assert target.connections == 0 and target.received == b""
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    (alert,) = r.alerts
    assert (alert.name, alert.reason, alert.status) == ("tsa-a", "redirect", 302)


@pytest.mark.parametrize("behaviour,reason,status,ctype", [
    ("oversized_length", "too_large", 200, "application/timestamp-reply"),
    ("oversized_stream", "too_large", 200, "application/timestamp-reply"),
    ("wrong_type", "content_type", 200, "application/octet-stream"),
    ("not_granted", "not_granted", 200, "application/timestamp-reply"),
    ("granted_with_mods", "not_granted", 200, "application/timestamp-reply"),
    ("status_500", "http_status", 500, "text/plain"),
    ("trailing", "parse_failed", 200, "application/timestamp-reply"),
    ("short", "transport_failed", 200, "application/timestamp-reply"),
])
def test_no_answer_fails_over(pki, keys_dir, behaviour, reason, status, ctype):
    with FakeTSA(pki, behaviour=behaviour) as a, FakeTSA(pki) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    (alert,) = r.alerts
    assert (alert.name, alert.reason, alert.status, alert.content_type) == ("tsa-a", reason, status, ctype)


class _Reply:
    """A 200 timestamp reply of `n` zero bytes, with or without a Content-Length."""

    def __init__(self, n, length):
        self.status, self.length, self._left = 200, length, n

    def getheader(self, name):
        return "application/timestamp-reply" if name == "Content-Type" else None

    def read1(self, k):
        n = min(k, self._left)
        self._left -= n
        return b"\x00" * n


@pytest.mark.parametrize("length", [True, False])
def test_a_reply_of_exactly_64_kib_is_within_the_cap(length):
    n = tsa.MAX_REPLY
    assert len(tsa._read_reply(_Reply(n, n if length else None), time.monotonic() + 10)) == n


def test_trickling_reply_fails_over_within_the_deadline(pki, keys_dir):
    with FakeTSA(pki, behaviour="trickle") as a, FakeTSA(pki) as b:
        start = time.monotonic()
        r = run(two(a, b, pki), pki, keys_dir, timeout=1.5)
        elapsed = time.monotonic() - start
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "timeout")]
    assert elapsed < 1.5 + 2.0


def test_stalled_name_lookup_fails_over_within_the_deadline(pki, keys_dir):
    stalled = StalledResolver(stall=["tsa-a.test"])
    try:
        with FakeTSA(pki) as a, FakeTSA(pki) as b:
            start = time.monotonic()
            r = run(two(a, b, pki), pki, keys_dir, timeout=1.0, resolve=stalled)
            elapsed = time.monotonic() - start
    finally:
        stalled.release.set()
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "resolve_timeout")]
    assert 1.0 <= elapsed < 1.0 + 2.0
    assert a.requests == []


def test_plain_http_needs_the_keyword(pki, keys_dir):
    with FakeTSA(pki, tls=False) as a:
        e = [entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)]
        with pytest.raises(TsaConfigError):
            run(e, pki, keys_dir)
        r = run(e, pki, keys_dir, allow_http=True)
        assert r.label == tsa.PRESENT
        with pytest.raises(TsaConfigError):
            run([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], pki, keys_dir, allow_http=True,
                creds={"tsa-a/user": "u", "tsa-a/password": "p"})
    assert len(a.requests) == 1


def test_bad_entries_are_refused_before_any_request(pki, keys_dir):
    with FakeTSA(pki) as a:
        for e in ([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin), entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)],
                  [entry("TSA", a.url(), ca=pki.tsa_ca.pin)], [entry("tsa-a", a.url(), ca="00")],
                  [entry("tsa-a", a.url(), auth="token", ca=pki.tsa_ca.pin)]):
            with pytest.raises(TsaConfigError):
                run(e, pki, keys_dir)
    assert a.requests == []


def test_a_tsa_list_is_accepted_whole(pki, keys_dir):
    with FakeTSA(pki) as a:
        listed = tsa.parse_tsa_list(tsa.encode_tsa_list([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)]))
        r = run(listed, pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-a"


def test_empty_list_gives_none(pki, keys_dir):
    r = run([], pki, keys_dir)
    assert r.label == tsa.NONE and r.token is None and r.alerts == ()


def test_failover_to_a_tsa_behind_another_ca_uses_that_entrys_own_pin(pki, keys_dir):
    """The production shape, two TSAs behind different CAs: the default pins are every entry's, so the backup's
    token is PRESENT under its own pin."""
    with FakeTSA(pki, behaviour="status_500") as a, FakeTSA(pki) as b:
        r = run([entry("tsa-a", a.url("tsa-a.test"), ca=pki.wrong_ca.pin),
                 entry("tsa-b", b.url("tsa-b.test"), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "http_status")]


def test_first_present_wins(pki, keys_dir):
    with FakeTSA(pki) as a, FakeTSA(pki) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.tsa == "tsa-a" and len(a.requests) == 1 and b.requests == []


def test_c2_missing_eku_then_pin_failure_attaches_the_second_as_unverified(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(eku="missing")) as a, \
            FakeTSA(pki, token=TokenOptions(issuer="wrong")) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.UNVERIFIED and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "check_4"), ("tsa-b", "pin_failed")]
    assert tsa.check_token(r.token, HEADER, pins(pki), now=T).failed == ("check_6",)
    assert len(a.requests) == len(b.requests) == 1


def test_a_kept_token_gives_way_to_a_later_present_one(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(issuer="wrong")) as a, FakeTSA(pki) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "pin_failed")]


def test_the_first_kept_token_goes_out(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(issuer="wrong")) as a, \
            FakeTSA(pki, token=TokenOptions(subject_o=("Another Unit",))) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.UNVERIFIED and r.tsa == "tsa-a"
    assert reasons(r) == [("tsa-a", "pin_failed"), ("tsa-b", "pin_failed")]


@pytest.mark.parametrize("opts,reason", [
    (TokenOptions(imprint="other"), "imprint_mismatch"),
    (TokenOptions(imprint="sha1"), "imprint_mismatch"),
    (TokenOptions(imprint="mislabelled"), "check_2"),
    (TokenOptions(dup_serial="other_issuer"), "check_1"),
    (TokenOptions(two_signers=True), "check_1"),
    (TokenOptions(sid="ski"), "check_1"),
    (TokenOptions(ess="none"), "check_5"),
    (TokenOptions(ess="v2_sha1"), "check_5"),
    (TokenOptions(eku="noncritical"), "check_4"),
    (TokenOptions(content_type_attr=0), "check_3"),
    (TokenOptions(gen_time_raw=b"20261010120000"), "parse_failed"),
    (TokenOptions(tst_ber=True), "parse_failed"),
    (TokenOptions(cert_ber=True), "parse_failed"),
    (TokenOptions(eku="missing", issuer="wrong"), "check_4"),
])
def test_tokens_failing_more_than_the_pin_are_never_attached(pki, keys_dir, opts, reason):
    with FakeTSA(pki, token=opts) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert r.label == tsa.NONE and r.token is None
    assert reasons(r) == [("tsa-a", reason)]


def test_pss_token_can_be_present(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(signer="pss")) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir)
    assert r.label == tsa.PRESENT


def test_gen_time_ahead_of_the_clock_is_no_answer(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(gen_time=T + timedelta(minutes=6))) as a, FakeTSA(pki) as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b"
    assert reasons(r) == [("tsa-a", "gen_time_ahead")]


def test_gen_time_behind_the_previous_present_is_no_answer(pki, keys_dir):
    with FakeTSA(pki) as a:
        e = [entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)]
        behind = run(e, pki, keys_dir, last_present_gen_time=T + timedelta(seconds=1))
        equal = run(e, pki, keys_dir, last_present_gen_time=T)
    assert behind.label == tsa.NONE and reasons(behind) == [("tsa-a", "gen_time_behind")]
    assert equal.label == tsa.PRESENT


def test_a_kept_token_still_needs_its_gen_time_in_bounds(pki, keys_dir):
    with FakeTSA(pki, token=TokenOptions(issuer="wrong")) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir,
                last_present_gen_time=T + timedelta(minutes=1))
    assert r.label == tsa.NONE and reasons(r) == [("tsa-a", "gen_time_behind")]


def test_lines_past_the_last_present_must_be_within_an_hour_of_gen_time(pki, keys_dir):
    later = T + timedelta(minutes=50)
    with FakeTSA(pki) as a, FakeTSA(pki, token=TokenOptions(gen_time=later)) as b:
        clock = lambda: T + timedelta(hours=1)  # noqa: E731
        r = run(two(a, b, pki), pki, keys_dir, clock=clock, created_at=T + timedelta(hours=1),
                latest_line_at=T + timedelta(hours=1, seconds=1))
        exact = run(two(a, b, pki), pki, keys_dir, clock=clock, latest_line_at=T + timedelta(hours=1))
    assert r.label == tsa.PRESENT and r.tsa == "tsa-b" and r.gen_time == later
    assert reasons(r) == [("tsa-a", "line_after_gen_time")]
    assert "clock" in str(r.alerts[0])
    assert exact.tsa == "tsa-a"


def test_a_token_kept_for_the_pin_still_needs_the_lines_within_an_hour_of_gen_time(pki, keys_dir):
    """Failing the line rule is failing something besides the pin: such a token is discarded, never kept, since a
    verifier whose pins accept it later would count it PRESENT."""
    with FakeTSA(pki, token=TokenOptions(issuer="wrong")) as a:
        e = [entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)]
        late = run(e, pki, keys_dir, latest_line_at=T + timedelta(hours=1, seconds=1))
        exact = run(e, pki, keys_dir, latest_line_at=T + timedelta(hours=1))
    assert late.label == tsa.NONE and late.token is None
    assert reasons(late) == [("tsa-a", "line_after_gen_time")]
    assert exact.label == tsa.UNVERIFIED and reasons(exact) == [("tsa-a", "pin_failed")]


def test_stamp_flags_created_at_skew(pki, keys_dir):
    with FakeTSA(pki) as a:
        r = run([entry("tsa-a", a.url(), ca=pki.tsa_ca.pin)], pki, keys_dir,
                created_at=T - timedelta(hours=2))
    assert r.label == tsa.PRESENT and r.created_at_skew is True


def test_all_fail_gives_none_with_an_alert_per_tsa(pki, keys_dir):
    with FakeTSA(pki, behaviour="status_500") as a, FakeTSA(pki, behaviour="not_granted") as b:
        r = run(two(a, b, pki), pki, keys_dir)
    assert r.label == tsa.NONE and r.token is None and r.tsa is None
    assert reasons(r) == [("tsa-a", "http_status"), ("tsa-b", "not_granted")]
    for alert in r.alerts:
        assert alert.reason in tsa.REASONS
        assert str(alert).startswith("TSA " + alert.name)


# ── C3: the client key ──

def test_c3_the_key_file_is_encrypted_private_and_gone_after_the_call(pki, keys_dir):
    seen = []
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds=pki.client_creds("tsa-a"), factory=kit.context_factory(pki, seen=seen))
    assert r.label == tsa.PRESENT
    (s,) = seen
    key_path = s["key_path"]
    assert key_path.parent.parent == keys_dir and key_path.parent.name.startswith(tsa.KEY_FOLDER_PREFIX)
    assert s["key_mode"] == 0o600 and s["folder_mode"] == 0o700
    assert not key_path.exists() and not key_path.parent.exists() and not s["cert_path"].exists()
    data = s["key_bytes"]
    assert data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n")
    plain_der = pki.client_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())
    plain_pem = kit.key_pem(pki.client_key)
    assert plain_der not in data and b"BEGIN PRIVATE KEY" not in data
    for line in plain_pem.splitlines()[1:-1]:
        assert line.encode() not in data
    body = b"".join(data.splitlines()[1:-1])
    info = akeys.EncryptedPrivateKeyInfo.load(base64.b64decode(body))
    alg = info["encryption_algorithm"]
    assert alg["algorithm"].native == "pbes2"
    params = alg["parameters"]
    assert params["key_derivation_func"]["algorithm"].native == "pbkdf2"
    assert params["key_derivation_func"]["parameters"]["prf"]["algorithm"].native == "sha256"
    assert params["encryption_scheme"]["algorithm"].native == "aes256_cbc"
    password = s["password"]
    assert isinstance(password, bytes) and len(password) == 32
    loaded = serialization.load_pem_private_key(data, password=password)
    assert loaded.private_numbers() == pki.client_key.private_numbers()


def test_c3_a_key_stored_with_the_certificate_never_reaches_the_certificate_file(pki, keys_dir):
    """C3 for the certificate file too: a combined PEM stored as `<name>/cert_pem` (a vendor bundle with the key in
    it) is written back as its certificates only, so the plaintext key never touches the disk."""
    creds = pki.client_creds("tsa-a")
    cert, key = creds["tsa-a/cert_pem"], creds["tsa-a/key_pem"]
    for combined in (cert + key, key + cert):
        with tsa.client_key_files(combined, key, keys_dir) as files:
            data = files.cert.read_bytes()
            assert b"PRIVATE KEY" not in data and data.startswith(b"-----BEGIN CERTIFICATE-----\n")
            ssl.create_default_context().load_cert_chain(str(files.cert), str(files.key), password=files.password)
    seen = []
    with FakeTSA(pki, auth="client_cert") as a:
        r = run([entry("tsa-a", a.url(), auth="client_cert", ca=pki.tsa_ca.pin)], pki, keys_dir,
                creds={"tsa-a/cert_pem": cert + key, "tsa-a/key_pem": key}, factory=kit.context_factory(pki, seen=seen))
    assert r.label == tsa.PRESENT
    (s,) = seen
    assert b"PRIVATE KEY" not in s["cert_bytes"] and b"BEGIN CERTIFICATE" in s["cert_bytes"]
    assert not s["cert_path"].exists()


def test_c3_fresh_passphrase_and_salt_each_time(pki, keys_dir):
    creds = pki.client_creds("tsa-a")
    got = []
    for _ in range(2):
        with tsa.client_key_files(creds["tsa-a/cert_pem"], creds["tsa-a/key_pem"], keys_dir) as files:
            got.append((files.password, files.key.read_bytes(), files.folder))
            assert "password" not in repr(files) and files.password.hex() not in repr(files)
    assert got[0][0] != got[1][0] and got[0][1] != got[1][1] and got[0][2] != got[1][2]


def test_c3_loads_with_load_cert_chain_here_and_on_the_system_python(pki, keys_dir):
    creds = pki.client_creds("tsa-a", rsa_traditional=True)
    with tsa.client_key_files(creds["tsa-a/cert_pem"], creds["tsa-a/key_pem"], keys_dir) as files:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(str(files.cert), str(files.key),
                                                                password=files.password)
        if SYSTEM_PYTHON.exists():
            script = ("import ssl, sys; pw = bytes.fromhex(sys.stdin.read().strip()); "
                      "ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(sys.argv[1], sys.argv[2], password=pw); "
                      "print('ok', ssl.OPENSSL_VERSION)")
            out = subprocess.run([str(SYSTEM_PYTHON), "-I", "-c", script, str(files.cert), str(files.key)],
                                 input=files.password.hex(), capture_output=True, text=True, timeout=60)
            assert out.returncode == 0 and out.stdout.startswith("ok"), out.stderr[-500:]
        folder = files.folder
    assert not folder.exists()


def test_c3_the_folder_goes_even_when_loading_fails(pki, keys_dir):
    creds = pki.client_creds("tsa-a")
    with pytest.raises(RuntimeError):
        with tsa.client_key_files(creds["tsa-a/cert_pem"], creds["tsa-a/key_pem"], keys_dir) as files:
            folder = files.folder
            raise RuntimeError("load failed")
    assert not folder.exists()
    assert sorted(p.name for p in keys_dir.iterdir()) == ["keystore.bin"]


def test_c3_cleanup_removes_only_leftover_key_folders(pki, keys_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    for i in range(2):
        d = keys_dir / f"{tsa.KEY_FOLDER_PREFIX}left{i}"
        d.mkdir(mode=0o700)
        (d / "client.key").write_bytes(b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n")
    (keys_dir / f"{tsa.KEY_FOLDER_PREFIX}link").symlink_to(outside)
    (keys_dir / "other-folder").mkdir()
    assert tsa.cleanup_key_folders(keys_dir) == 3
    assert sorted(p.name for p in keys_dir.iterdir()) == ["keystore.bin", "other-folder"]
    assert (outside / "keep.txt").read_text() == "keep"
    assert tsa.cleanup_key_folders(keys_dir) == 0
    assert tsa.cleanup_key_folders(keys_dir / "missing") == 0


# ── No credential anywhere ──

def test_no_credential_in_any_log_alert_exception_or_repr(pki, keys_dir, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    password, user = "pw-secret-9f3a", "user-secret-7c1d"
    cc = pki.client_creds("tsa-c")
    secrets = [password, user, base64.b64encode(f"{user}:{password}".encode()).decode()]
    secrets += [line for line in cc["tsa-c/key_pem"].splitlines()[1:-1] if len(line) > 16]
    seen = []
    texts = []
    with FakeTSA(pki, auth="basic", user=user, password=password) as a, \
            FakeTSA(pki, auth="client_cert") as c:
        basic = {"tsa-a/user": user, "tsa-a/password": password}
        cases = [
            ([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], basic),
            ([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], {"tsa-a/user": user,
                                                                          "tsa-a/password": password + "x"}),
            ([entry("tsa-a", a.url(), auth="basic", ca=pki.tsa_ca.pin)], {"tsa-a/user": user + ":",
                                                                          "tsa-a/password": password}),
            ([entry("tsa-c", c.url("tsa-c.test"), auth="client_cert", ca=pki.tsa_ca.pin)], cc),
            ([entry("tsa-c", c.url("tsa-c.test"), auth="client_cert", ca=pki.tsa_ca.pin)],
             dict(cc, **{"tsa-c/cert_pem": kit.pem(pki.client_rsa_cert)})),
        ]
        for entries, creds in cases:
            for factory in (kit.context_factory(pki, seen=seen),
                            kit.context_factory(pki, cafile=pki.other_tls_ca_file)):
                r = run(entries, pki, keys_dir, creds=creds, factory=factory)
                texts += [repr(r), str(r)] + [str(x) for x in r.alerts] + [repr(x) for x in r.alerts]
        with pytest.raises(TsaConfigError) as e:
            run([entry("tsa-a", a.url().replace("https://", f"https://{user}:{password}@"), auth="basic",
                       ca=pki.tsa_ca.pin)], pki, keys_dir, creds=basic)
        texts.append(str(e.value))
        texts.append(repr(e.value))
    with tsa.client_key_files(cc["tsa-c/cert_pem"], cc["tsa-c/key_pem"], keys_dir) as files:
        texts.append(repr(files))
        secrets.append(files.password.hex())
    secrets += [s["password"].hex() for s in seen]
    out = capfd.readouterr()
    texts += [caplog.text, out.out, out.err]
    blob = "\n".join(texts)
    for s in secrets:
        assert s not in blob
