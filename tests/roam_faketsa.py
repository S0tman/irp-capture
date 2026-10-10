"""FakeTSA: a test kit for the Roaming IRP TSA client (spec v0.3 §18a, §23; step 2.6).

Not collected by pytest (no test_ prefix). It has three parts:

- `PKI`: test certificate authorities and keys, made with `cryptography`. A TLS CA issues the FakeTSA's server
  certificate (names `tsa-a.test`, `tsa-b.test`, `tsa-c.test`, `localhost`, and 127.0.0.1) and a client CA
  issues the client certificates; a pinned TSA CA and an unpinned "wrong" CA issue the time-stamping leaves.
  Every certificate is strict enough for Python 3.13's `VERIFY_X509_STRICT`.
- `build_token(digest, pki, opts)`: an RFC 3161 TimeStampToken built with `asn1crypto`, with a switch in
  `TokenOptions` for every failure §18a and §23 name (EKU missing, non-critical or widened, the wrong CA, a
  SHA-1 or mislabelled imprint, ESSCertID missing, v1 or v2 over SHA-1, a duplicate-serial certificate listed
  first, two SignerInfos, a subjectKeyIdentifier sid, RSASSA-PSS, the CA left out, subject O mismatches,
  genTime ahead, behind or without Z, privateKeyUsagePeriod, and non-DER encodings: `DER_FLAWS` puts one
  encoding DER forbids (a non-minimal INTEGER, length, tag number or subidentifier, an indefinite length, a
  BER-only construct, a named-bit list keeping a trailing zero, a time that isn't a time, nesting too deep or
  too many values) in SignedData, in a TSTInfo extension value, in the leaf certificate (signed again) or in an
  extra certificate in the unsigned certificate set, so the rest of the token still passes).
- `FakeTSA`: an HTTPS server on 127.0.0.1 (or plain HTTP for the test-only path) answering TimeStampReqs with
  those tokens, with Basic auth, a required client certificate, a 302 to a second listener, a trickling reply,
  an oversized reply, a wrong content type, a non-granted or granted-with-mods status and trailing bytes.
  `StalledResolver` is a name lookup that never returns in time; `resolver()` maps every test name to 127.0.0.1.

The client trusts the server through an injected SSL context factory (or SSL_CERT_FILE); production keeps
`ssl.create_default_context()`. All names and values here are neutral test values.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from asn1crypto import algos, cms, core, tsp
from asn1crypto import x509 as ax509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

T = datetime(2026, 10, 10, 12, 0, 0)          # genTime of every default token, and the tests' clock
ORG = "Example TSA Unit"                       # the leaf's O by default
POLICY = "1.2.3.4.5"
NAMES = ("tsa-a.test", "tsa-b.test", "tsa-c.test", "localhost")
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_HASHES = {"sha1": hashes.SHA1, "sha256": hashes.SHA256, "sha384": hashes.SHA384, "sha512": hashes.SHA512}
_RSA_CACHE: Dict[str, Any] = {}


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc)


def ec_key(label: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(hashlib.sha256(("faketsa/" + label).encode()).digest(), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def rsa_key(label: str) -> rsa.RSAPrivateKey:
    if label not in _RSA_CACHE:
        _RSA_CACHE[label] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _RSA_CACHE[label]


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def key_pem(key: Any, fmt: str = "pkcs8") -> str:
    form = serialization.PrivateFormat.PKCS8 if fmt == "pkcs8" else serialization.PrivateFormat.TraditionalOpenSSL
    return key.private_bytes(serialization.Encoding.PEM, form, serialization.NoEncryption()).decode("ascii")


def pin(cert: x509.Certificate) -> str:
    return hashlib.sha256(der(cert)).hexdigest()


def _name(cn: str, orgs: Sequence[str] = (), *, multi_valued: bool = False) -> x509.Name:
    if multi_valued:
        rdn = x509.RelativeDistinguishedName([x509.NameAttribute(NameOID.ORGANIZATION_NAME, orgs[0]),
                                              x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        return x509.Name([rdn])
    attrs = [x509.NameAttribute(NameOID.ORGANIZATION_NAME, o) for o in orgs]
    return x509.Name(attrs + [x509.NameAttribute(NameOID.COMMON_NAME, cn)])


class CA:
    """A self-signed test CA (EC P-256 unless given another key)."""

    def __init__(self, label: str, key: Any = None):
        self.label = label
        self.key = key or ec_key("ca/" + label)
        self.name = _name("Test CA " + label, ["Example Trust Services"])
        ski = x509.SubjectKeyIdentifier.from_public_key(self.key.public_key())
        self.cert = (x509.CertificateBuilder().subject_name(self.name).issuer_name(self.name)
                     .public_key(self.key.public_key()).serial_number(1000 + len(label))
                     .not_valid_before(datetime(2025, 1, 1)).not_valid_after(datetime(2031, 1, 1))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                                  key_encipherment=False, data_encipherment=False,
                                                  key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                  encipher_only=False, decipher_only=False), critical=True)
                     .add_extension(ski, critical=False)
                     .sign(self.key, hashes.SHA256()))

    @property
    def pin(self) -> str:
        return pin(self.cert)

    def issue(self, subject: x509.Name, public_key: Any, *, serial: int, extensions: Sequence[Tuple[Any, bool]],
              not_before: datetime = datetime(2026, 1, 1), not_after: datetime = datetime(2027, 12, 31),
              hash_name: str = "sha256", pss: bool = False,
              issuer_name: Optional[x509.Name] = None) -> x509.Certificate:
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name or self.name)
             .public_key(public_key)
             .serial_number(serial).not_valid_before(not_before).not_valid_after(not_after)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False))
        for ext, critical in extensions:
            b = b.add_extension(ext, critical=critical)
        if pss:
            return b.sign(self.key, _HASHES[hash_name](), rsa_padding=padding.PSS(
                mgf=padding.MGF1(_HASHES[hash_name]()), salt_length=32))
        if hash_name == "sha1":
            return self._resign_sha1(b.sign(self.key, hashes.SHA256()))
        return b.sign(self.key, _HASHES[hash_name]())

    def _resign_sha1(self, cert: x509.Certificate) -> x509.Certificate:
        """The same certificate signed with SHA-1, which `cryptography`'s builder no longer makes."""
        a = ax509.Certificate.load(der(cert))
        rsa_ca = isinstance(self.key, rsa.RSAPrivateKey)
        alg = {"algorithm": "sha1_rsa" if rsa_ca else "sha1_ecdsa"}
        tbs = a["tbs_certificate"]
        tbs["signature"] = alg
        tbs_der = tbs.dump(force=True)
        if rsa_ca:
            value = self.key.sign(tbs_der, padding.PKCS1v15(), hashes.SHA1())
        else:
            value = self.key.sign(tbs_der, ec.ECDSA(hashes.SHA1()))
        out = ax509.Certificate({"tbs_certificate": ax509.TbsCertificate.load(tbs_der),
                                 "signature_algorithm": alg, "signature_value": value})
        return x509.load_der_x509_certificate(out.dump())


def _leaf_usage() -> x509.KeyUsage:
    return x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                         encipher_only=False, decipher_only=False)


class PKI:
    """Every test CA, key and certificate, with the PEM files the TLS server and client load."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.tls_ca = CA("tls")
        self.client_ca = CA("client")
        self.tsa_ca = CA("tsa")
        self.wrong_ca = CA("wrong")
        self.rsa_ca = CA("tsa-rsa", key=rsa_key("ca/tsa-rsa"))
        self.other_tls_ca = CA("other-tls")
        server_key = ec_key("server")
        san = x509.SubjectAlternativeName([x509.DNSName(n) for n in NAMES]
                                          + [x509.IPAddress(IPv4Address("127.0.0.1"))])
        self.server_cert = self.tls_ca.issue(
            _name("tsa-a.test"), server_key.public_key(), serial=11,
            extensions=[(x509.BasicConstraints(ca=False, path_length=None), True), (_leaf_usage(), True),
                        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False), (san, False)])
        self.server_cert_file = self._write("server.crt", pem(self.server_cert))
        self.server_key_file = self._write("server.key", key_pem(server_key))
        self.tls_ca_file = self._write("tls-ca.crt", pem(self.tls_ca.cert))
        self.other_tls_ca_file = self._write("other-tls-ca.crt", pem(self.other_tls_ca.cert))
        self.client_ca_file = self._write("client-ca.crt", pem(self.client_ca.cert))
        self.client_key = ec_key("client")
        self.client_cert = self.client_cert_for(self.client_key, serial=21)
        self.client_rsa_key = rsa_key("client-rsa")
        self.client_rsa_cert = self.client_cert_for(self.client_rsa_key, serial=22)
        self._leaves: Dict[Tuple[Any, ...], Tuple[Any, x509.Certificate]] = {}

    def _write(self, name: str, text: str) -> Path:
        path = self.folder / name
        path.write_text(text)
        return path

    def client_cert_for(self, key: Any, *, serial: int) -> x509.Certificate:
        return self.client_ca.issue(
            _name("client-1"), key.public_key(), serial=serial,
            extensions=[(x509.BasicConstraints(ca=False, path_length=None), True), (_leaf_usage(), True),
                        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)])

    def client_creds(self, name: str, *, rsa_traditional: bool = False) -> Dict[str, str]:
        """What the keystore's tsa_creds holds for a client_cert TSA: the certificate and a plain PEM key."""
        if rsa_traditional:
            return {name + "/cert_pem": pem(self.client_rsa_cert),
                    name + "/key_pem": key_pem(self.client_rsa_key, "traditional")}
        return {name + "/cert_pem": pem(self.client_cert), name + "/key_pem": key_pem(self.client_key)}

    def leaf(self, opts: "TokenOptions") -> Tuple[Any, x509.Certificate, CA]:
        """The time-stamping leaf (key, certificate, issuing CA) for these options, cached."""
        issuer = {"pinned": self.tsa_ca, "wrong": self.wrong_ca, "rsa": self.rsa_ca}[opts.issuer]
        rsa_signer = opts.signer in ("rsa", "pss")
        k = (opts.issuer, rsa_signer, opts.eku, tuple(opts.subject_o), opts.multi_valued_o, opts.leaf_not_before,
             opts.leaf_not_after, opts.pkup, opts.leaf_hash, opts.leaf_pss, opts.serial, opts.issuer_name)
        if k not in self._leaves:
            key = rsa_key("tsu") if rsa_signer else ec_key("tsu")
            exts: List[Tuple[Any, bool]] = [(_leaf_usage(), True)]
            if opts.eku == "critical":
                exts.append((x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), True))
            elif opts.eku == "noncritical":
                exts.append((x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), False))
            elif opts.eku == "extra":
                exts.append((x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING,
                                                    ExtendedKeyUsageOID.CLIENT_AUTH]), True))
            if opts.pkup is not None:
                nb, na = opts.pkup
                period: Dict[str, Any] = {}
                if nb is not None:
                    period["not_before"] = _utc(nb)
                if na is not None:
                    period["not_after"] = _utc(na)
                value = ax509.PrivateKeyUsagePeriod(period).dump()
                exts.append((x509.UnrecognizedExtension(ObjectIdentifier("2.5.29.16"), value), False))
            subject = _name("Test TSU", list(opts.subject_o), multi_valued=opts.multi_valued_o)
            other = _name("Another Test CA", ["Example Trust Services"]) if opts.issuer_name == "other" else None
            cert = issuer.issue(subject, key.public_key(), serial=opts.serial, extensions=exts,
                                not_before=opts.leaf_not_before, not_after=opts.leaf_not_after,
                                hash_name=opts.leaf_hash, pss=opts.leaf_pss, issuer_name=other)
            self._leaves[k] = (key, cert, issuer)
        return self._leaves[k]


@dataclass
class TokenOptions:
    """One switch per failure §18a and §23 name. The defaults make a token that is PRESENT under PKI.tsa_ca."""
    signer: str = "ec"                    # "ec" | "rsa" (PKCS#1 v1.5) | "pss" (RSASSA-PSS)
    signer_hash: str = "sha256"           # the SignerInfo digest algorithm, and the signature's hash
    signature_hash: Optional[str] = None  # sign with this hash instead (and label the signature so)
    eku: str = "critical"                 # "critical" | "missing" | "noncritical" | "extra"
    issuer: str = "pinned"                # "pinned" (tsa_ca) | "wrong" (wrong_ca) | "rsa" (rsa_ca)
    issuer_name: str = "ca"               # "ca" | "other": the leaf names another issuer, signed by the same key
    include_ca: bool = True               # put the issuing CA certificate in the token
    imprint: str = "ours"                 # "ours" | "sha1" | "mislabelled" | "other"
    imprint_params: str = "absent"        # "absent" | "null" | "junk"
    ess: str = "v2"                       # "v2" | "v1" | "none" | "both" | "v2_sha1" | "v2_sha512" | "wrong_hash"
                                          # | "wrong_serial" | "wrong_issuer" | "no_issuer_serial"
                                          # | "second_entry_only"
    dup_serial: Optional[str] = None      # None | "other_issuer" | "same_issuer": listed before the leaf
    two_signers: bool = False
    sid: str = "issuer_serial"            # "issuer_serial" | "ski"
    subject_o: Sequence[str] = (ORG,)     # one O attribute per entry, each in its own RDN
    multi_valued_o: bool = False          # the first O and the CN share one RDN
    gen_time: Optional[datetime] = None   # default T
    gen_time_raw: Optional[bytes] = None  # exact GeneralizedTime text, e.g. b"20261010120000" (no Z)
    leaf_not_before: datetime = datetime(2026, 1, 1)
    leaf_not_after: datetime = datetime(2027, 12, 31)
    pkup: Optional[Tuple[Optional[datetime], Optional[datetime]]] = None
    leaf_hash: str = "sha256"             # the hash the CA signs the leaf with
    leaf_pss: bool = False                # the CA signs the leaf with RSASSA-PSS (issuer "rsa" only)
    serial: int = 4242
    econtent_type: str = "tst_info"
    content_type_attr: int = 1            # how many content-type signed attributes
    message_digest_attrs: int = 1
    bad_signature: bool = False
    signature_algorithm: Optional[str] = None  # relabel the SignerInfo signatureAlgorithm (signature unchanged)
    tst_ber: bool = False                 # TSTInfo with a non-minimal length (signed as is)
    cert_ber: bool = False                # the leaf certificate with a non-minimal outer length
    der_flaw: Optional[str] = None        # one encoding DER forbids, somewhere the rest of the token is fine:
                                          # see DER_FLAWS (the TSTInfo, the leaf certificate, SignedData)
    policy: str = POLICY


# Each flaw puts one encoding that BER allows and DER forbids into an otherwise good token. The TSTInfo flaws sit
# in the value of a TSTInfo extension (signed as is); the leaf certificate flaws sit in its TBSCertificate or in
# an extension value, and the issuing CA signs the flawed TBSCertificate again, so its signature still verifies.
DER_FLAWS = {
    "signed_data_integer": "SignedData.version 3 as 02 02 00 03 (a non-minimal INTEGER)",
    "tst_ext_integer": "a non-minimal INTEGER inside a TSTInfo extension value",
    "tst_ext_length": "a non-minimal length inside a TSTInfo extension value",
    "tst_ext_indefinite": "an indefinite length inside a TSTInfo extension value",
    "tst_ext_constructed": "a constructed OCTET STRING (BER only) inside a TSTInfo extension value",
    "cert_integer": "the leaf's TBSCertificate version 2 as 02 02 00 02 (a non-minimal INTEGER)",
    "cert_boolean": "the leaf's extended key usage marked critical by 01 01 01 (BER only: DER says FF)",
    "cert_ext_length": "a non-minimal length inside the leaf's subject key identifier value",
    "cert_ext_indefinite": "an indefinite length inside the leaf's extended key usage value",
    "cert_ext_constructed": "a constructed OCTET STRING (BER only) inside the leaf's subject key identifier value",
    "cert_ext_implicit_integer": "a non-minimal [2] IMPLICIT INTEGER inside the leaf's authority key identifier",
    "cert_ext_deep": "200 nested SEQUENCEs as the leaf's subject key identifier value",
    "tst_ext_hightag": "a [31] tag in high-tag form with a leading 80 octet inside a TSTInfo extension value",
    "tst_ext_relative_oid": "a RELATIVE-OID with a non-minimal subidentifier inside a TSTInfo extension value",
    "cert_pkup_fraction": "a privateKeyUsagePeriod [0] IMPLICIT GeneralizedTime with a trailing-zero fraction "
                          "(.0Z) in the leaf, in place of its subject key identifier",
    "cert_crldp_reasons": "a CRL distribution point's reasons [1] IMPLICIT BIT STRING with a padding bit set, in "
                          "the leaf, in place of its subject key identifier",
    "cert_regid": "a subjectAltName registeredID [8] IMPLICIT OID with a non-minimal subidentifier, in the leaf, "
                  "in place of its subject key identifier",
    "cert_many_values": "an unknown leaf extension whose value is a SEQUENCE of 20001 NULLs (valid DER, more values "
                        "than the strict walk visits)",
    "cert_named_bits": "an extra CA certificate in the unsigned certificate set whose KeyUsage keeps a trailing "
                       "zero bit (03 02 00 06; X.690 11.2.2 says 03 02 01 06)",
    "cert_time_invalid": "an extra CA certificate in the unsigned certificate set whose notBefore UTCTime is "
                         "month 99",
}
CLEAN_TST_EXTENSION = "tst_ext_clean"     # the TSTInfo extension the tst_ext_* flaws use, without a flaw
CLEAN_EXTRA_CERT = "cert_extra_clean"     # an extra certificate the cert_named_bits and cert_time_invalid flaws use
EXTRA_CERT_FLAWS = frozenset({"cert_named_bits", "cert_time_invalid", CLEAN_EXTRA_CERT})
TST_EXTENSION_OID = "1.2.3.4.5.6.7"
_OID_EKU = bytes.fromhex("551d25")
_OID_SKI = bytes.fromhex("551d0e")
_OID_AKI = bytes.fromhex("551d23")
_OID_KU = bytes.fromhex("551d0f")
_OID_PKUP = bytes.fromhex("551d10")
_OID_CRLDP = bytes.fromhex("551d1f")
_OID_SAN = bytes.fromhex("551d11")
_OID_UNKNOWN = bytes.fromhex("2a03040506")


class Tlv:
    """One TLV of an encoding as a tree, so a test can re-encode one part in a form DER forbids. `kids` is None
    for a primitive; `form` is "der" (minimal definite length), "long" (one length octet more than DER uses) or
    "indefinite" (constructed only)."""

    def __init__(self, ident: bytes, kids: Optional[List["Tlv"]] = None, data: bytes = b"", form: str = "der"):
        self.ident, self.kids, self.data, self.form = ident, kids, data, form


def _tlv_one(data: bytes, i: int) -> Tuple[Tlv, int]:
    start = i
    tag = data[i]
    i += 1
    if tag & 0x1F == 0x1F:
        while data[i] & 0x80:
            i += 1
        i += 1
    ident = data[start:i]
    first = data[i]
    i += 1
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        length = int.from_bytes(data[i:i + n], "big")
        i += n
    stop = i + length
    if tag & 0x20:
        kids = []
        while i < stop:
            kid, i = _tlv_one(data, i)
            kids.append(kid)
        return Tlv(ident, kids), stop
    return Tlv(ident, data=data[i:stop]), stop


def tlv_parse(data: bytes) -> Tlv:
    """A DER encoding as a Tlv tree (test input only: it trusts the bytes)."""
    node, end = _tlv_one(data, 0)
    assert end == len(data)
    return node


def der_length(n: int, form: str = "der") -> bytes:
    body = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    if form == "long":
        return b"\x81" + bytes([n]) if n < 0x80 else bytes([0x80 | (len(body) + 1)]) + b"\x00" + body
    return bytes([n]) if n < 0x80 else bytes([0x80 | len(body)]) + body


def tlv_dump(node: Tlv) -> bytes:
    contents = node.data if node.kids is None else b"".join(tlv_dump(k) for k in node.kids)
    if node.form == "indefinite":
        assert node.kids is not None
        return node.ident + b"\x80" + contents + b"\x00\x00"
    return node.ident + der_length(len(contents), node.form) + contents


def nested(depth: int, inner: bytes = b"\x05\x00") -> bytes:
    """`depth` SEQUENCEs, one inside the other, around `inner` (definite lengths throughout)."""
    out = inner
    for _ in range(depth):
        out = b"\x30" + der_length(len(out)) + out
    return out


def _non_minimal(node: Tlv) -> None:
    node.data = (b"\xff" if node.data[0] & 0x80 else b"\x00") + node.data


def _constructed(node: Tlv) -> Tlv:
    half = max(1, len(node.data) // 2)
    return Tlv(bytes([node.ident[0] | 0x20]),
               [Tlv(b"\x04", data=node.data[:half]), Tlv(b"\x04", data=node.data[half:])])


def _tst_extension_value(flaw: str) -> bytes:
    """SEQUENCE { INTEGER 5, OCTET STRING "neutral value" }, with the TSTInfo flaw in it (none for the clean one)."""
    value = Tlv(b"\x30", [Tlv(b"\x02", data=b"\x05"), Tlv(b"\x04", data=b"neutral value")])
    if flaw == "tst_ext_integer":
        _non_minimal(value.kids[0])
    elif flaw == "tst_ext_length":
        value.form = "long"
    elif flaw == "tst_ext_indefinite":
        value.form = "indefinite"
    elif flaw == "tst_ext_constructed":
        value.kids[1] = _constructed(value.kids[1])
    elif flaw == "tst_ext_hightag":
        return bytes.fromhex("30049f801f00")  # SEQUENCE { [31] with its tag number as 80 1f }
    elif flaw == "tst_ext_relative_oid":
        return bytes.fromhex("30040d028001")  # SEQUENCE { RELATIVE-OID with a leading 80 subidentifier }
    return tlv_dump(value)


def _flawed_leaf(leaf_der: bytes, issuer: "CA", flaw: str) -> bytes:
    """The leaf with one flaw, its TBSCertificate signed again by the issuing CA."""
    cert = tlv_parse(leaf_der)
    tbs = cert.kids[0]
    extensions = tbs.kids[-1].kids[0]  # [3] EXPLICIT Extensions

    def ext(oid: bytes) -> Tlv:
        return next(e for e in extensions.kids if e.kids[0].data == oid)

    def inner(e: Tlv) -> Tlv:
        return tlv_parse(e.kids[-1].data)
    if flaw == "cert_integer":
        _non_minimal(tbs.kids[0].kids[0])
    elif flaw == "cert_boolean":
        critical = ext(_OID_EKU).kids[1]
        assert critical.ident == b"\x01" and critical.data == b"\xff"
        critical.data = b"\x01"
    elif flaw == "cert_ext_length":
        value = inner(ext(_OID_SKI))
        value.form = "long"
        ext(_OID_SKI).kids[-1].data = tlv_dump(value)
    elif flaw == "cert_ext_indefinite":
        value = inner(ext(_OID_EKU))
        value.form = "indefinite"
        ext(_OID_EKU).kids[-1].data = tlv_dump(value)
    elif flaw == "cert_ext_constructed":
        ext(_OID_SKI).kids[-1].data = tlv_dump(_constructed(inner(ext(_OID_SKI))))
    elif flaw == "cert_ext_implicit_integer":
        value = inner(ext(_OID_AKI))
        value.kids.append(Tlv(b"\x82", data=b"\x00\x03\xe8"))  # authorityCertSerialNumber 1000, one byte too long
        ext(_OID_AKI).kids[-1].data = tlv_dump(value)
    elif flaw == "cert_ext_deep":
        ext(_OID_SKI).kids[-1].data = nested(200)
    elif flaw in ("cert_pkup_fraction", "cert_crldp_reasons", "cert_regid", "cert_many_values"):
        oid, value = {
            "cert_pkup_fraction": (_OID_PKUP, b"\x30\x13\x80\x11" + b"20260101000000.0Z"),
            "cert_crldp_reasons": (_OID_CRLDP, bytes.fromhex("3006300481020781")),
            "cert_regid": (_OID_SAN, bytes.fromhex("300588032a8003")),
            "cert_many_values": (_OID_UNKNOWN, b"\x30" + der_length(2 * 20001) + b"\x05\x00" * 20001),
        }[flaw]
        e = ext(_OID_SKI)
        e.kids[0].data = oid
        e.kids[-1].data = value
    else:
        raise ValueError(f"unknown certificate flaw {flaw}")
    tbs_der = tlv_dump(tbs)
    if isinstance(issuer.key, rsa.RSAPrivateKey):
        signature = issuer.key.sign(tbs_der, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = issuer.key.sign(tbs_der, ec.ECDSA(hashes.SHA256()))
    cert.kids[2] = Tlv(b"\x03", data=b"\x00" + signature)
    return tlv_dump(cert)


def _extra_cert(ca_der: bytes, flaw: str) -> bytes:
    """A copy of another CA's certificate for the token's certificate set, which nothing signs, with one flaw (none
    for the clean one). It isn't signed again: nothing verifies it, so only the strict parse can see the flaw."""
    cert = tlv_parse(ca_der)
    tbs = cert.kids[0]
    if flaw == "cert_named_bits":
        extensions = tbs.kids[-1].kids[0]
        usage = next(e for e in extensions.kids if e.kids[0].data == _OID_KU)
        value = tlv_parse(usage.kids[-1].data)
        assert value.ident == b"\x03" and value.data == b"\x01\x06"  # keyCertSign and cRLSign, DER
        value.data = b"\x00\x06"
        usage.kids[-1].data = tlv_dump(value)
    elif flaw == "cert_time_invalid":
        not_before = tbs.kids[4].kids[0]  # Validity's notBefore
        assert not_before.ident == b"\x17" and len(not_before.data) == 13
        not_before.data = b"259901000000Z"
    return tlv_dump(cert)


def widen(encoded: bytes) -> bytes:
    """The same TLV with its outer length in a non-minimal long form (one leading zero byte)."""
    first = encoded[1]
    if first < 0x80:
        return encoded[:1] + b"\x81" + encoded[1:]
    n = first & 0x7F
    return encoded[:1] + bytes([0x80 | (n + 1), 0]) + encoded[2:]


def _gen_time_value(opts: TokenOptions) -> core.GeneralizedTime:
    if opts.gen_time_raw is not None:
        raw = opts.gen_time_raw
        return core.GeneralizedTime.load(b"\x18" + bytes([len(raw)]) + raw)
    return core.GeneralizedTime(_utc(opts.gen_time or T))


def _hash(name: str, data: bytes) -> bytes:
    return hashlib.new(name, data).digest()


def _sign(key: Any, data: bytes, opts: TokenOptions) -> Tuple[Dict[str, Any], bytes]:
    name = opts.signature_hash or opts.signer_hash
    h = _HASHES[name]()
    if opts.signer == "pss":
        params = algos.RSASSAPSSParams({
            "hash_algorithm": {"algorithm": name},
            "mask_gen_algorithm": {"algorithm": "mgf1", "parameters": {"algorithm": name}},
            "salt_length": 32, "trailer_field": "trailer_field_bc"})
        sig = key.sign(data, padding.PSS(mgf=padding.MGF1(_HASHES[name]()), salt_length=32), h)
        return {"algorithm": "rsassa_pss", "parameters": params}, sig
    if opts.signer == "rsa":
        return {"algorithm": name + "_rsa"}, key.sign(data, padding.PKCS1v15(), h)
    return {"algorithm": name + "_ecdsa"}, key.sign(data, ec.ECDSA(h))


def _issuer_serial(cert: x509.Certificate, serial: int) -> Dict[str, Any]:
    issuer = ax509.Certificate.load(der(cert))["tbs_certificate"]["issuer"]
    return {"issuer": ax509.GeneralNames([ax509.GeneralName(name="directory_name", value=issuer)]),
            "serial_number": serial}


def _ess_attrs(leaf_der: bytes, leaf: x509.Certificate, other_der: bytes, opts: TokenOptions) -> List[Any]:
    mode = opts.ess
    if mode == "none":
        return []
    isr = _issuer_serial(leaf, opts.serial)
    out: List[Any] = []
    if mode in ("v1", "both"):
        out.append({"type": "signing_certificate", "values": [tsp.SigningCertificate({"certs": [
            tsp.ESSCertID({"cert_hash": _hash("sha1", leaf_der), "issuer_serial": isr})]})]})
    if mode == "v1":
        return out
    hash_name = {"v2_sha1": "sha1", "v2_sha512": "sha512"}.get(mode, "sha256")
    entry: Dict[str, Any] = {"cert_hash": _hash(hash_name, leaf_der), "issuer_serial": isr}
    if hash_name != "sha256":
        entry["hash_algorithm"] = {"algorithm": hash_name}
    if mode == "wrong_hash":
        entry["cert_hash"] = _hash("sha256", other_der)
    elif mode == "wrong_serial":
        entry["issuer_serial"] = _issuer_serial(leaf, opts.serial + 1)
    elif mode == "wrong_issuer":  # the right serial under another CA's name
        other = ax509.Certificate.load(other_der)["tbs_certificate"]["subject"]
        entry["issuer_serial"] = {"issuer": ax509.GeneralNames([ax509.GeneralName(name="directory_name",
                                                                                 value=other)]),
                                  "serial_number": opts.serial}
    elif mode == "no_issuer_serial":
        del entry["issuer_serial"]
    entries = [tsp.ESSCertIDv2(entry)]
    if mode == "second_entry_only":
        entries = [tsp.ESSCertIDv2({"cert_hash": _hash("sha256", other_der)}), tsp.ESSCertIDv2(entry)]
    out.append({"type": "signing_certificate_v2", "values": [tsp.SigningCertificateV2({"certs": entries})]})
    return out


def build_token(digest: bytes, pki: PKI, opts: Optional[TokenOptions] = None) -> bytes:
    """A TimeStampToken (CMS ContentInfo DER) over a SHA-256 digest, shaped by `opts`."""
    opts = opts or TokenOptions()
    flaw = opts.der_flaw
    if flaw is not None and flaw not in DER_FLAWS and flaw not in (CLEAN_TST_EXTENSION, CLEAN_EXTRA_CERT):
        raise ValueError(f"unknown DER flaw {flaw}")
    key, leaf, issuer = pki.leaf(opts)
    leaf_der = der(leaf)
    if flaw is not None and flaw.startswith("cert_") and flaw not in EXTRA_CERT_FLAWS:
        leaf_der = _flawed_leaf(leaf_der, issuer, flaw)
    if opts.imprint == "ours":
        imprint_alg, value = "sha256", digest
    elif opts.imprint == "sha1":
        imprint_alg, value = "sha1", _hash("sha1", digest)
    elif opts.imprint == "mislabelled":
        imprint_alg, value = "sha384", digest
    else:
        imprint_alg, value = "sha256", _hash("sha256", b"some other checkpoint")
    hash_alg = algos.DigestAlgorithm({"algorithm": imprint_alg})
    if opts.imprint_params == "absent":
        hash_alg = algos.DigestAlgorithm.load(b"\x30" + bytes([len(hash_alg["algorithm"].dump())])
                                              + hash_alg["algorithm"].dump())
    elif opts.imprint_params == "junk":
        oid = hash_alg["algorithm"].dump()
        body = oid + b"\x04\x01\x00"
        hash_alg = algos.DigestAlgorithm.load(b"\x30" + bytes([len(body)]) + body)
    fields: Dict[str, Any] = {"version": "v1", "policy": opts.policy,
                              "message_imprint": tsp.MessageImprint({"hash_algorithm": hash_alg,
                                                                     "hashed_message": value}),
                              "serial_number": 777, "gen_time": _gen_time_value(opts)}
    if flaw is not None and flaw.startswith("tst_ext_"):
        fields["extensions"] = [{"extn_id": TST_EXTENSION_OID, "critical": False,
                                 "extn_value": _tst_extension_value(flaw)}]
    tst = tsp.TSTInfo(fields)
    econtent = tst.dump()
    if opts.tst_ber:
        econtent = widen(econtent)
    attrs: List[Any] = []
    for _ in range(opts.content_type_attr):
        attrs.append({"type": "content_type", "values": [opts.econtent_type]})
    for _ in range(opts.message_digest_attrs):
        attrs.append({"type": "message_digest", "values": [_hash(opts.signer_hash, econtent)]})
    other_der = der(pki.wrong_ca.cert)
    attrs += _ess_attrs(leaf_der, leaf, other_der, opts)
    signed_attrs = cms.CMSAttributes(attrs)
    alg, signature = _sign(key, signed_attrs.dump(), opts)
    if opts.signature_algorithm is not None:
        alg = {"algorithm": opts.signature_algorithm}
    if opts.bad_signature:
        signature = signature[:-1] + bytes([signature[-1] ^ 1])
    leaf_ax = ax509.Certificate.load(leaf_der)
    if opts.sid == "ski":
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key()).digest
        sid = cms.SignerIdentifier(name="subject_key_identifier", value=ski)
        version = "v3"
    else:
        sid = cms.SignerIdentifier(name="issuer_and_serial_number", value={
            "issuer": leaf_ax["tbs_certificate"]["issuer"], "serial_number": opts.serial})
        version = "v1"
    si = cms.SignerInfo({"version": version, "sid": sid, "digest_algorithm": {"algorithm": opts.signer_hash},
                         "signed_attrs": signed_attrs, "signature_algorithm": alg, "signature": signature})
    certs: List[bytes] = []
    if opts.dup_serial is not None:
        dup_ca = pki.wrong_ca if opts.dup_serial == "other_issuer" else issuer
        dup = dup_ca.issue(_name("Test TSU twin", [ORG]), ec_key("twin").public_key(), serial=opts.serial,
                           extensions=[(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), True)])
        certs.append(der(dup))
    certs.append(widen(leaf_der) if opts.cert_ber else leaf_der)
    if opts.include_ca:
        certs.append(der(issuer.cert))
    if flaw in EXTRA_CERT_FLAWS:
        certs.append(_extra_cert(der(pki.wrong_ca.cert), flaw))
    sd = cms.SignedData({
        "version": "v3", "digest_algorithms": [{"algorithm": opts.signer_hash}],
        "encap_content_info": {"content_type": opts.econtent_type, "content": core.ParsableOctetString(econtent)},
        "certificates": [cms.CertificateChoices(name="certificate", value=ax509.Certificate.load(c)) for c in certs],
        "signer_infos": [si, si] if opts.two_signers else [si]})
    token = cms.ContentInfo({"content_type": "signed_data", "content": sd}).dump()
    if flaw == "signed_data_integer":
        root = tlv_parse(token)
        version = root.kids[1].kids[0].kids[0]  # ContentInfo [0] EXPLICIT SignedData, its version
        assert version.ident == b"\x02" and version.data == b"\x03"
        _non_minimal(version)
        token = tlv_dump(root)
    return token


def reply(token: Optional[bytes], status: str = "granted") -> bytes:
    """A TimeStampResp carrying `token` (or none, for a refusal)."""
    if token is None:  # asn1crypto won't dump a TimeStampResp without its token, so the SEQUENCE is made here
        info = tsp.PKIStatusInfo({"status": status}).dump()
        return b"\x30" + bytes([len(info)]) + info
    return tsp.TimeStampResp({"status": {"status": status}, "time_stamp_token": cms.ContentInfo.load(token)}).dump()


# ── The server ──

@dataclass
class Received:
    path: str
    headers: List[Tuple[str, str]]
    body: bytes
    peer_cn: Optional[str]
    tls_version: Optional[str]


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: "_Server"

    def setup(self) -> None:
        self.request.settimeout(15)
        fake = self.server.fake
        if fake.tls:
            self.request = fake.server_context.wrap_socket(self.request, server_side=True)
        super().setup()

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.request.close()  # socketserver only closes the raw socket the TLS layer took over

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's own name
        self.server.fake.log.append(format % args)

    def _send(self, status: int, body: bytes, ctype: str = "application/timestamp-reply",
              length: Optional[int] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server's own name
        fake = self.server.fake
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        peer_cn = version = None
        if fake.tls:
            cert = self.request.getpeercert()
            if cert:
                for rdn in cert.get("subject", ()):
                    for k, v in rdn:
                        if k == "commonName":
                            peer_cn = v
            version = self.request.version()
        fake.requests.append(Received(self.path, list(self.headers.items()), body, peer_cn, version))
        if fake.auth == "basic":
            want = "Basic " + base64.b64encode((fake.user + ":" + fake.password).encode()).decode()
            if self.headers.get("Authorization") != want:
                self._send(401, b"no", "text/plain")
                return
        b = fake.behaviour
        if b == "redirect":
            self.send_response(302)
            self.send_header("Location", fake.redirect_to or "https://127.0.0.1:9/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if b == "status_500":
            self._send(500, b"busy", "text/plain")
            return
        if b == "trickle":
            for ch in b"HTTP/1.0 200 OK\r\nContent-Type: application/timestamp-reply\r\n" * 50:
                try:
                    self.wfile.write(bytes([ch]))
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(fake.trickle_gap)
            return
        if b == "oversized_length":
            self._send(200, b"\x00" * 70000)
            return
        if b == "oversized_stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.end_headers()
            try:
                for _ in range(70):
                    self.wfile.write(b"\x00" * 1000)
            except OSError:
                pass
            return
        req = tsp.TimeStampReq.load(body)
        fake.imprints.append(req["message_imprint"]["hashed_message"].native)
        if b == "not_granted":
            self._send(200, reply(None, "rejection"))
            return
        token = build_token(req["message_imprint"]["hashed_message"].native, fake.pki, fake.token)
        fake.tokens.append(token)
        out = reply(token, "granted_with_mods" if b == "granted_with_mods" else "granted")
        if b == "trailing":
            out += b"\x00\x00"
        if b == "wrong_type":
            self._send(200, out, "application/octet-stream")
            return
        if b == "short":
            self._send(200, out[:-10], length=len(out))
            return
        self._send(200, out)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    fake: "FakeTSA"

    def server_bind(self) -> None:
        # HTTPServer.server_bind would look the address up with getfqdn; nothing here leaves 127.0.0.1.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        self.fake.errors += 1


class FakeTSA:
    """An RFC 3161 server on 127.0.0.1 over real TLS (or plain HTTP with tls=False)."""

    def __init__(self, pki: PKI, *, auth: str = "none", tls: bool = True, tls_max_12: bool = False,
                 token: Optional[TokenOptions] = None, behaviour: str = "ok", user: str = "user-1",
                 password: str = "pw-test-1"):
        self.pki, self.auth, self.tls = pki, auth, tls
        self.token = token or TokenOptions()
        self.behaviour = behaviour
        self.user, self.password = user, password
        self.redirect_to: Optional[str] = None
        self.trickle_gap = 0.05
        self.requests: List[Received] = []
        self.imprints: List[bytes] = []
        self.tokens: List[bytes] = []
        self.log: List[str] = []
        self.errors = 0
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(pki.server_cert_file), str(pki.server_key_file))
        if auth == "client_cert":
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(str(pki.client_ca_file))
        if tls_max_12:
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self.server_context = ctx
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "FakeTSA":
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def url(self, host: str = "tsa-a.test", path: str = "/tsr") -> str:
        return ("https" if self.tls else "http") + f"://{host}:{self.port}{path}"


class CountingListener:
    """A TCP listener that only counts what reaches it (the target of a redirect that must never be followed)."""

    def __init__(self) -> None:
        self.connections = 0
        self.received = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.settimeout(0.5)
            try:
                self.received += conn.recv(65536)
            except OSError:
                pass
            conn.close()

    def __enter__(self) -> "CountingListener":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(2)
        self._sock.close()

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]


def resolver(calls: Optional[List[str]] = None) -> Callable[[str, int], List[Tuple[Any, ...]]]:
    """A name lookup that sends every test name to 127.0.0.1, recording what it was asked."""
    def resolve(host: str, port: int) -> List[Tuple[Any, ...]]:
        if calls is not None:
            calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
    return resolve


class StalledResolver:
    """A name lookup that doesn't return until released (or 60 seconds pass): for every name, or only for the
    names in `stall`, the others going straight to 127.0.0.1."""

    def __init__(self, stall: Optional[Sequence[str]] = None) -> None:
        self.release = threading.Event()
        self.stall = None if stall is None else set(stall)
        self.calls: List[str] = []

    def __call__(self, host: str, port: int) -> List[Tuple[Any, ...]]:
        self.calls.append(host)
        if self.stall is None or host in self.stall:
            self.release.wait(60)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]


class RecordingContext(ssl.SSLContext):
    """An SSLContext that records what load_cert_chain was given: the key file's mode, folder mode and bytes, and
    the certificate file's bytes."""

    seen: List[Dict[str, Any]]

    def load_cert_chain(self, certfile: Any, keyfile: Any = None, password: Any = None) -> None:
        import os
        import stat as _stat

        kpath = Path(keyfile)
        self.seen.append({"key_path": kpath, "cert_path": Path(certfile), "key_bytes": kpath.read_bytes(),
                          "cert_bytes": Path(certfile).read_bytes(),
                          "key_mode": _stat.S_IMODE(os.lstat(kpath).st_mode),
                          "folder_mode": _stat.S_IMODE(os.lstat(kpath.parent).st_mode),
                          "password": password})
        super().load_cert_chain(certfile, keyfile, password=password)


def context_factory(pki: PKI, *, cafile: Optional[Path] = None,
                    seen: Optional[List[Dict[str, Any]]] = None) -> Callable[[], ssl.SSLContext]:
    """What the tests inject in place of ssl.create_default_context: the same settings, trusting the test CA."""
    def make() -> ssl.SSLContext:
        if seen is None:
            return ssl.create_default_context(cafile=str(cafile or pki.tls_ca_file))
        ctx = RecordingContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.seen = seen
        ctx.load_verify_locations(cafile=str(cafile or pki.tls_ca_file))
        return ctx
    return make
