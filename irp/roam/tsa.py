"""The TSA client for Roaming IRP checkpoints (spec v0.3 §18.4, §18a; step 2.6).

A checkpoint SHOULD carry one RFC 3161 token over the exact bytes of its header, `irp/checkpoint.json`, so it
existed no later than the token's genTime. This module does the TSA's side of that, and nothing else:

1. The TSA list, `~/.irp-roam/local/tsa.json`: exact JCS with the closed schema `{"v":1,"tsas":[…]}`, 1 to 4
   entries in failover order, each `{name, url, auth, ca_sha256, subject_o?}`. A url is `https://` with no
   userinfo, query or fragment. Plain `http://` exists only for tests, through the `allow_http` keyword, and
   only with `auth` none. Its digest is the body's `tsa_policy_digest`. The pins are custodian policy, taken
   by hand from the national Trusted List. Credentials live in the keystore's `tsa_creds` map:
   `<name>/user` and `<name>/password`, or `<name>/cert_pem` and `<name>/key_pem`.
2. The request: the unchanged `rfc3161.build_request` over SHA-256 of the header (certReq, no nonce), sent by
   in-process `http.client` over `ssl.create_default_context()` (which honours SSL_CERT_FILE on OpenSSL
   builds), with TLS 1.2 at least. A total deadline of 20 seconds per TSA on the monotonic clock covers the
   name lookup (a worker thread joined against it and abandoned when it runs over), the connection (to the
   resolved address, with SNI and Host set to the name), the handshake and a reply read in chunks with a
   64 KiB cap. No redirect is followed and no proxy is used. Basic auth is a header built in memory. A client
   key reaches `ssl` only as a PKCS#8 file encrypted (PBES2, PBKDF2-HMAC-SHA256, AES-256-CBC) under a fresh
   random 32-byte passphrase, in a fresh 0700 folder under the keys folder, deleted as soon as
   `load_cert_chain` has read it (C3). A client certificate that travels under TLS below 1.3 (in clear) gets
   a one-time alert.
3. The token checks (`check_token`), on top of the unchanged `verify_token`: the token is the exact
   TimeStampToken DER, at most 64 KiB, with no trailing bytes and re-encoding to the same bytes, DER anywhere
   (every tag, length and primitive, inside extension values and under implicit tags too, with nesting
   bounded); then checks 1 to 6 of §18a. All pass: PRESENT. Anything else, including any exception: UNVERIFIED.
4. Failover (C2, `stamp`): TSAs are tried in order. No answer (a transport failure, a redirect, a status other
   than 200, the wrong content type, a status other than granted, a reply over the cap, a parse failure, an
   imprint that isn't ours, or a genTime more than 5 minutes after the local clock or before the previous
   PRESENT genTime) moves on. Every devices or readers line past the last PRESENT checkpoint must be dated at
   most 1 hour after a token's genTime, or that token is discarded too. A token failing check 6 alone is kept
   aside and the next TSA is still tried; any other failing token is discarded. The first PRESENT token wins;
   otherwise the first kept token goes out UNVERIFIED, or none does (NONE), and each TSA gets an alert.

Alerts carry the TSA name, the HTTP status, the content type and a fixed reason code, never a reply body or
the text of an `http.client` or `ssl` exception. Credentials, Authorization headers and key material never
reach a log line, an alert, an exception message or a repr. It never sends a TSA anything but the request,
never starts a process, never writes a plaintext client key to disk, never attaches a token that fails
anything but the pin and never calls a token "qualified".
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from irp.integrity import rfc3161
from irp.integrity.canonical import canonicalize

from . import sig
from ._deps import tsa as _deps

TSA_PATH = Path("~/.irp-roam/local/tsa.json")
TSA_LIST_KEYS = frozenset({"v", "tsas"})
TSA_ENTRY_KEYS = frozenset({"name", "url", "auth", "ca_sha256"})
TSA_ENTRY_OPTIONAL = frozenset({"subject_o"})
AUTHS = ("none", "basic", "client_cert")
MAX_TSAS = 4
MAX_PINS = 4
MAX_SUBJECT_O = 64                       # X.520 ub-organization-name
MAX_TSA_FILE = 64 * 1024
TSA_DEADLINE = 20.0                      # seconds per TSA, name lookup included
MAX_REPLY = 64 * 1024
MAX_TOKEN = 64 * 1024
GEN_TIME_AHEAD = timedelta(minutes=5)
LINE_SLACK = timedelta(hours=1)          # a covered line may be dated at most this long after genTime
SKEW_LIMIT = timedelta(hours=1)          # |genTime - created_at| above this flags created_at_skew
REQUEST_TYPE = "application/timestamp-query"
REPLY_TYPE = "application/timestamp-reply"
KEY_FOLDER_PREFIX = "tsa-key-"
MAX_DER_DEPTH = 64                       # TLV nesting a strict parse accepts (a real token needs about a dozen)
MAX_WALK_DEPTH = 128                     # parsed values deep, the token and every extension value inside it
MAX_WALK_VALUES = 20_000                 # parsed values the strict walk visits before it gives up
PBKDF2_ROUNDS = 10_000                   # the passphrase is 32 random bytes, so rounds add nothing to its strength
READ_CHUNK = 16 * 1024

PRESENT = "PRESENT"
UNVERIFIED = "UNVERIFIED"
NONE = "NONE"
LABELS = (PRESENT, UNVERIFIED, NONE)
LABEL_TEXT = "RFC 3161, issuer on the EU Trusted List (checked by hand)"

# Alert reason codes: a fixed list, and the only text an alert carries besides the name, status and type.
MISSING_CREDENTIAL = "missing_credential"
BAD_CREDENTIAL = "bad_credential"
CLIENT_KEY_FAILED = "client_key_failed"
RESOLVE_FAILED = "resolve_failed"
RESOLVE_TIMEOUT = "resolve_timeout"
CONNECT_FAILED = "connect_failed"
TLS_FAILED = "tls_failed"
TLS_BELOW_1_3 = "tls_below_1_3"
TIMEOUT = "timeout"
TRANSPORT_FAILED = "transport_failed"
REDIRECT = "redirect"
HTTP_STATUS = "http_status"
CONTENT_TYPE = "content_type"
TOO_LARGE = "too_large"
PARSE_FAILED = "parse_failed"
NOT_GRANTED = "not_granted"
IMPRINT_MISMATCH = "imprint_mismatch"
GEN_TIME_AHEAD_REASON = "gen_time_ahead"
GEN_TIME_BEHIND = "gen_time_behind"
PIN_FAILED = "pin_failed"
LINE_AFTER_GEN_TIME = "line_after_gen_time"
CHECK_1, CHECK_2, CHECK_3, CHECK_4, CHECK_5, CHECK_6 = (f"check_{i}" for i in range(1, 7))

_REASON_TEXT = {
    MISSING_CREDENTIAL: "no credential for this TSA in the keystore; skipped",
    BAD_CREDENTIAL: "the stored credential can't be used; skipped",
    CLIENT_KEY_FAILED: "the client certificate or key couldn't be loaded",
    RESOLVE_FAILED: "the name lookup failed",
    RESOLVE_TIMEOUT: "the name lookup didn't finish within the deadline",
    CONNECT_FAILED: "couldn't connect",
    TLS_FAILED: "the TLS handshake failed",
    TLS_BELOW_1_3: "TLS below 1.3: the client certificate travelled in clear",
    TIMEOUT: "no complete answer within the deadline",
    TRANSPORT_FAILED: "the connection failed during the answer",
    REDIRECT: "answered with a redirect, which is never followed",
    HTTP_STATUS: "answered with an HTTP status other than 200",
    CONTENT_TYPE: "answered with a content type other than " + REPLY_TYPE,
    TOO_LARGE: "the answer is over the 64 KiB cap",
    PARSE_FAILED: "the answer or its token doesn't parse",
    NOT_GRANTED: "the TSA didn't grant the timestamp",
    IMPRINT_MISMATCH: "the token's imprint isn't this checkpoint's",
    GEN_TIME_AHEAD_REASON: "genTime is more than 5 minutes after this laptop's clock",
    GEN_TIME_BEHIND: "genTime is before the previous PRESENT checkpoint's",
    PIN_FAILED: "the token fails only the pin check; kept aside",
    LINE_AFTER_GEN_TIME: "a log line is dated more than 1 hour after genTime: check this laptop's clock",
    CHECK_1: "the token fails check 1 (signer and signature)",
    CHECK_2: "the token fails check 2 (digest algorithms)",
    CHECK_3: "the token fails check 3 (content type)",
    CHECK_4: "the token fails check 4 (time-stamping key usage)",
    CHECK_5: "the token fails check 5 (signing certificate identifier)",
}
REASONS = frozenset(_REASON_TEXT)

# What `check_token` reports as failed: checks 1 to 6, and the token-wide failures.
F_PARSE = "parse"                        # not strict DER, too big, or not a TimeStampToken at all
F_GEN_TIME = "gen_time"                  # genTime without Z, or not a real time
F_GEN_TIME_FUTURE = "gen_time_future"    # genTime more than 5 minutes after the verifier's clock
F_IMPRINT = "imprint"                    # the imprint isn't the header's SHA-256 (part of check 1)

_NAME = re.compile(r"[a-z0-9-]{1,32}")
_PIN = re.compile(r"[0-9a-f]{64}")
_URL = re.compile(r"[\x21-\x7e]{1,2048}")
_DNS = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                  r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GEN = re.compile(rb"([0-9]{14})(?:\.[0-9]+)?Z")
_UTC_DER = re.compile(rb"[0-9]{12}Z")                         # DER's UTCTime: seconds, then Z
_GENERALIZED_DER = re.compile(rb"[0-9]{14}(?:\.[0-9]*[1-9])?Z")  # DER's GeneralizedTime: no trailing zero, Z
_TYPE_CHARS = re.compile(r"[^A-Za-z0-9!#$&^_.+\-/;= ]")

_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_ESS_V1 = "1.2.840.113549.1.9.16.2.12"
_OID_ESS_V2 = "1.2.840.113549.1.9.16.2.47"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_EKU = "2.5.29.37"
_OID_TIME_STAMPING = "1.3.6.1.5.5.7.3.8"
_OID_PKUP = "2.5.29.16"
_OID_O = "2.5.4.10"
_STRONG = frozenset({"sha256", "sha384", "sha512"})
_HASH_NAMES = {"sha1": "SHA1", "sha224": "SHA224", "sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}
_RSA_SIGS = {"rsassa_pkcs1v15": None, "sha1_rsa": "sha1", "sha224_rsa": "sha224", "sha256_rsa": "sha256",
             "sha384_rsa": "sha384", "sha512_rsa": "sha512"}
_ECDSA_SIGS = {"ecdsa": None, "1.2.840.10045.2.1": None,  # bare ECDSA or id-ecPublicKey: the digest decides
               "sha1_ecdsa": "sha1", "sha224_ecdsa": "sha224", "sha256_ecdsa": "sha256",
               "sha384_ecdsa": "sha384", "sha512_ecdsa": "sha512"}

Resolver = Callable[[str, int], Sequence[Tuple[Any, ...]]]

_tls_noted: set = set()                  # TSA names already told, in this process, that TLS was below 1.3
_tls_lock = threading.Lock()


class TsaError(Exception):
    """The TSA client can't do what was asked. Messages never carry a credential, a URL or a reply."""


class TsaConfigError(TsaError):
    """tsa.json, a TSA entry or the TLS context is outside what §18a allows."""


@dataclass(frozen=True)
class TsaEntry:
    """One TSA in failover order. `ca_sha256` holds the SHA-256 of each issuing CA certificate pinned for it."""
    name: str
    url: str
    auth: str
    ca_sha256: Tuple[str, ...]
    subject_o: Optional[str] = None


@dataclass(frozen=True)
class TsaList:
    """tsa.json as read: the entries and the exact bytes, whose digest is the body's `tsa_policy_digest`."""
    entries: Tuple[TsaEntry, ...]
    raw: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return "sha256-" + hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True)
class TsaPin:
    """One (pin, subject_o) pair, checked as a unit (check 6). A reader bundle's `tsa_pins` entry."""
    name: str
    ca_sha256: str
    subject_o: Optional[str] = None


@dataclass(frozen=True)
class TsaAlert:
    """A local alert about one TSA: its name, a fixed reason code, and the HTTP status and content type when
    there was an answer. Never a reply body, an exception's text or a credential."""
    name: str
    reason: str
    status: Optional[int] = None
    content_type: Optional[str] = None

    def __str__(self) -> str:
        parts = []
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.content_type:
            parts.append(self.content_type)
        where = f" ({', '.join(parts)})" if parts else ""
        return f"TSA {self.name}: {self.reason}{where}: {_REASON_TEXT.get(self.reason, self.reason)}"


@dataclass(frozen=True)
class TokenCheck:
    """What `check_token` found. `failed` lists `check_1` to `check_6` and the token-wide failures (`parse`,
    `gen_time`, `gen_time_future`, `imprint`); PRESENT exactly when it's empty. `pin` is the pair that
    matched in check 6. `policy` is recorded, never checked."""
    label: str
    failed: Tuple[str, ...] = ()
    gen_time: Optional[datetime] = None
    policy: Optional[str] = None
    pin: Optional[TsaPin] = None
    created_at_skew: bool = False


@dataclass(frozen=True)
class StampResult:
    """What the TSA step gives the checkpoint: the token to ship as `irp/checkpoint.tsr` (None for NONE), its
    label, which TSA gave it, genTime (as naive UTC), the policy OID, the skew flag and every alert raised."""
    label: str
    token: Optional[bytes] = field(default=None, repr=False)
    tsa: Optional[str] = None
    gen_time: Optional[datetime] = None
    policy: Optional[str] = None
    created_at_skew: bool = False
    alerts: Tuple[TsaAlert, ...] = ()


@dataclass(frozen=True)
class ClientKeyFiles:
    """The files `load_cert_chain` reads for a client certificate: the certificate chain, and the key as
    encrypted PKCS#8 under `password`, in a fresh 0700 folder. They exist only inside `client_key_files`."""
    folder: Path
    cert: Path
    key: Path
    password: bytes = field(repr=False)


# ── tsa.json ──

def _naive(t: datetime) -> datetime:
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def _check_url(url: Any, *, auth: str, allow_http: bool) -> None:
    """https with no userinfo, query or fragment, and a DNS name or an IP address as the host (in brackets only
    an IPv6 address). Errors never repeat the url (it may hold userinfo), and a url urlsplit can't take is a
    TsaConfigError on every interpreter, never a bare ValueError."""
    if not (isinstance(url, str) and _URL.fullmatch(url)):
        raise TsaConfigError("a TSA url must be printable ASCII without spaces")
    if url.startswith("http://"):
        if not allow_http:
            raise TsaConfigError("a TSA url must start with https://")
        if auth != "none":
            raise TsaConfigError("a plain http TSA (tests only) can't carry credentials: its auth must be none")
    elif not url.startswith("https://"):
        raise TsaConfigError("a TSA url must start with https://")
    if "?" in url or "#" in url:
        raise TsaConfigError("a TSA url can't carry a query or a fragment")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise TsaConfigError("a TSA url doesn't parse") from None
    if "@" in parts.netloc:
        raise TsaConfigError("a TSA url can't carry userinfo: credentials live in the keystore")
    try:
        port = parts.port
    except ValueError:
        raise TsaConfigError("a TSA url has a bad port") from None
    if port == 0:
        raise TsaConfigError("a TSA url has a bad port")
    host = parts.hostname or ""
    if "[" in parts.netloc or "]" in parts.netloc:
        # Interpreters differ on what urlsplit lets through in brackets (a name, IPvFuture): only IPv6 passes.
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise TsaConfigError("a TSA url's bracketed host must be an IPv6 address") from None
    if not _DNS.fullmatch(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise TsaConfigError("a TSA url needs a host name or address") from None
    if parts.path and not parts.path.startswith("/"):
        raise TsaConfigError("a TSA url path must start with /")


def _check_entry(e: Any, *, allow_http: bool) -> TsaEntry:
    if not isinstance(e, TsaEntry):
        raise TsaConfigError("a TSA entry must be a TsaEntry")
    if not (isinstance(e.name, str) and _NAME.fullmatch(e.name)):
        raise TsaConfigError("a TSA name must match [a-z0-9-]{1,32}")
    if e.auth not in AUTHS:
        raise TsaConfigError(f"TSA {e.name}: auth must be one of {', '.join(AUTHS)}")
    _check_url(e.url, auth=e.auth, allow_http=allow_http)
    pins = e.ca_sha256
    if not (isinstance(pins, tuple) and 1 <= len(pins) <= MAX_PINS):
        raise TsaConfigError(f"TSA {e.name}: ca_sha256 holds 1 to {MAX_PINS} pins")
    if not all(isinstance(p, str) and _PIN.fullmatch(p) for p in pins) or len(set(pins)) != len(pins):
        raise TsaConfigError(f"TSA {e.name}: each pin is 64 lowercase hex, once")
    if e.subject_o is not None and not (isinstance(e.subject_o, str) and 1 <= len(e.subject_o) <= MAX_SUBJECT_O
                                        and not _CONTROL.search(e.subject_o)):
        raise TsaConfigError(f"TSA {e.name}: subject_o must be 1 to {MAX_SUBJECT_O} characters of text")
    return e


def _check_entries(entries: Sequence[Any], *, allow_http: bool) -> Tuple[TsaEntry, ...]:
    out = tuple(_check_entry(e, allow_http=allow_http) for e in entries)
    if len(out) > MAX_TSAS:
        raise TsaConfigError(f"at most {MAX_TSAS} TSAs")
    if len({e.name for e in out}) != len(out):
        raise TsaConfigError("TSA names must be unique")
    return out


def _entry_from_json(obj: Any, *, allow_http: bool) -> TsaEntry:
    if not isinstance(obj, dict) or not TSA_ENTRY_KEYS <= set(obj) <= TSA_ENTRY_KEYS | TSA_ENTRY_OPTIONAL:
        raise TsaConfigError("a tsa.json entry has exactly name, url, auth, ca_sha256 and optionally subject_o")
    pins = obj["ca_sha256"]
    if not isinstance(pins, list):
        raise TsaConfigError("a tsa.json entry's ca_sha256 is a list")
    if "subject_o" in obj and not isinstance(obj["subject_o"], str):
        raise TsaConfigError("a tsa.json entry's subject_o, when present, is text")
    return _check_entry(TsaEntry(name=obj["name"], url=obj["url"], auth=obj["auth"], ca_sha256=tuple(pins),
                                 subject_o=obj.get("subject_o")), allow_http=allow_http)


def parse_tsa_list(data: bytes, *, allow_http: bool = False) -> TsaList:
    """Read tsa.json's exact bytes: strict JCS, closed schema, 1 to 4 entries. `allow_http` is for tests only."""
    obj = sig.load_jcs(data, "tsa.json", error=TsaConfigError)
    if not isinstance(obj, dict) or set(obj) != TSA_LIST_KEYS:
        raise TsaConfigError("tsa.json has exactly the keys v and tsas")
    if type(obj["v"]) is not int or obj["v"] != 1:
        raise TsaConfigError("tsa.json v must be 1")
    tsas = obj["tsas"]
    if not isinstance(tsas, list) or not 1 <= len(tsas) <= MAX_TSAS:
        raise TsaConfigError(f"tsa.json lists 1 to {MAX_TSAS} TSAs")
    entries = _check_entries([_entry_from_json(e, allow_http=allow_http) for e in tsas], allow_http=allow_http)
    return TsaList(entries=entries, raw=bytes(data))


def encode_tsa_list(entries: Sequence[TsaEntry], *, allow_http: bool = False) -> bytes:
    """The canonical tsa.json bytes for these entries (written by init or `tsa set`, step 2.8)."""
    checked = _check_entries(entries, allow_http=allow_http)
    if not checked:
        raise TsaConfigError(f"tsa.json lists 1 to {MAX_TSAS} TSAs")
    out = []
    for e in checked:
        item: Dict[str, Any] = {"name": e.name, "url": e.url, "auth": e.auth, "ca_sha256": list(e.ca_sha256)}
        if e.subject_o is not None:
            item["subject_o"] = e.subject_o
        out.append(item)
    return canonicalize({"v": 1, "tsas": out})


def load_tsa_list(path: Path | str = TSA_PATH, *, allow_http: bool = False) -> Optional[TsaList]:
    """tsa.json from `path` (default ~/.irp-roam/local/tsa.json), or None when there is none."""
    p = Path(path).expanduser()
    try:
        with open(p, "rb") as fh:
            data = fh.read(MAX_TSA_FILE + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TsaConfigError(f"can't read tsa.json: {exc.strerror}") from None
    if len(data) > MAX_TSA_FILE:
        raise TsaConfigError("tsa.json is too large")
    return parse_tsa_list(data, allow_http=allow_http)


def pins_from_tsas(entries: Iterable[TsaEntry]) -> Tuple[TsaPin, ...]:
    """One (pin, subject_o) pair per pin, each keeping its own entry's subject_o (§19.1 `tsa_pins`)."""
    return tuple(TsaPin(e.name, p, e.subject_o) for e in entries for p in e.ca_sha256)


# ── Token checks (check_token) ──

class _Unparsable(Exception):
    pass


def _void(d: Any, value: Any) -> bool:
    return isinstance(value, d.core.Void)


def _int_ok(c: bytes) -> bool:
    """DER's INTEGER (and ENUMERATED) contents: at least one octet, and no redundant leading 00 or FF."""
    return bool(c) and not (len(c) > 1 and ((c[0] == 0x00 and not c[1] & 0x80) or (c[0] == 0xFF and c[1] & 0x80)))


def _bits_ok(c: bytes) -> bool:
    """DER's BIT STRING contents: the unused-bits octet is 0 to 7, 0 when there are no bits, and the unused bits
    of the last octet are zero."""
    if not c or c[0] > 7:
        return False
    if len(c) == 1:
        return c[0] == 0
    return not c[-1] & ((1 << c[0]) - 1)


def _oid_ok(c: bytes) -> bool:
    """An OBJECT IDENTIFIER's (or RELATIVE-OID's) contents: every subidentifier minimal (no leading 80) and the
    last one finished."""
    if not c or c[-1] & 0x80:
        return False
    first = True
    for b in c:
        if first and b == 0x80:
            return False
        first = not b & 0x80
    return True


def _contents_ok(number: int, c: bytes) -> bool:
    """DER's rules for the contents of a universal primitive: BOOLEAN is 00 or FF, INTEGER and ENUMERATED are
    minimal, BIT STRING pads with zeros, NULL is empty, an OID's subidentifiers are minimal, UTCTime and
    GeneralizedTime carry seconds and Z (and a fraction without a trailing zero). Other types have no such rule."""
    if number == 1:
        return len(c) == 1 and c[0] in (0x00, 0xFF)
    if number in (2, 10):
        return _int_ok(c)
    if number == 3:
        return _bits_ok(c)
    if number == 5:
        return not c
    if number in (6, 13):
        return _oid_ok(c)
    if number == 23:
        return bool(_UTC_DER.fullmatch(c))
    if number == 24:
        return bool(_GENERALIZED_DER.fullmatch(c))
    return True


def _der_tlv(data: bytes, start: int, end: int, depth: int) -> int:
    """Check that the TLV at data[start:end] (and every TLV nested in it) is already in the form a DER encoder
    writes: minimal tag and definite minimal length, universal SEQUENCE and SET constructed and every other
    universal type primitive, with the DER form of its contents (`_contents_ok`). Nesting deeper than
    MAX_DER_DEPTH is refused, so the recursion stays bounded. Returns the offset just after it."""
    if depth > MAX_DER_DEPTH or start >= end:
        raise _Unparsable()
    i, tag = start + 1, data[start]
    number = tag & 0x1F
    universal = tag & 0xC0 == 0
    if number == 0x1F:
        number, first = 0, True
        while True:
            if i >= end:
                raise _Unparsable()
            b = data[i]
            i += 1
            if (first and b == 0x80) or number > 1 << 28:
                raise _Unparsable()
            first = False
            number = number << 7 | b & 0x7F
            if not b & 0x80:
                break
        if number < 31:
            raise _Unparsable()
    if (universal and number == 0) or i >= end:
        raise _Unparsable()
    first_len = data[i]
    i += 1
    if first_len < 0x80:
        length = first_len
    else:
        n = first_len & 0x7F
        if n == 0 or n > 4 or i + n > end or data[i] == 0:
            raise _Unparsable()  # indefinite, too long, or not minimal
        length = int.from_bytes(data[i:i + n], "big")
        i += n
        if length < 0x80:
            raise _Unparsable()
    stop = i + length
    if stop > end:
        raise _Unparsable()
    if tag & 0x20:
        if universal and number not in (16, 17):
            raise _Unparsable()  # a constructed string or other primitive type: BER, not DER
        while i < stop:
            i = _der_tlv(data, i, stop, depth + 1)
    elif universal and number in (16, 17):
        raise _Unparsable()
    elif universal and not _contents_ok(number, data[i:stop]):
        raise _Unparsable()  # a non-minimal INTEGER, a BOOLEAN other than 00 or FF, and the like: BER, not DER
    return stop


def _reencodes(data: bytes) -> bool:
    """True when `data` is exactly one TLV that re-encodes, TLV for TLV, to the same bytes."""
    try:
        return bool(data) and _der_tlv(data, 0, len(data), 0) == len(data)
    except (_Unparsable, IndexError):
        return False


def _strict_walk(root: Any, d: Any) -> None:
    """The schema-aware half of strictness, over a parse of the token of its own (it forces every lazy part).

    The byte check (`_reencodes`) sees every TLV of the token but not inside an OCTET STRING, and not what an
    IMPLICIT tag hides. So this walks every parsed value: an OCTET STRING or BIT STRING that carries an encoding
    (the TSTInfo, every certificate extension value, an RSA public key, a TSTInfo extension value) must itself
    pass the byte check before it's parsed and walked, and every implicitly tagged primitive gets the same
    content rules by its type (an INTEGER [2] in an authority key identifier, a GeneralizedTime [0] in a private
    key usage period, a BIT STRING [1] in a CRL distribution point's reasons, an OID [8] in a registeredID), and
    no implicitly tagged string is constructed. A named-bit list keeps no trailing zero bit, and a UTCTime or
    GeneralizedTime must be a real time. The BOOLEAN branch is reachable only through a CRL in SignedData.crls
    (an IssuingDistributionPoint [1], [2] or [4]), which FakeTSA can't attach, so it's covered by review for now.
    The walk is iterative and bounded (MAX_WALK_DEPTH, MAX_WALK_VALUES); anything outside the rules raises
    _Unparsable."""
    core = d.core
    stack: List[Tuple[Any, int]] = [(root, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if depth > MAX_WALK_DEPTH or visited > MAX_WALK_VALUES:
            raise _Unparsable()
        if value is None or isinstance(value, core.Void):
            continue
        if isinstance(value, core.Any):
            stack.append((value.parsed, depth + 1))
            continue
        if isinstance(value, core.Choice):
            stack.append((value.chosen, depth + 1))
            continue
        if isinstance(value, core.Sequence):
            for index in range(len(value)):
                stack.append((value[index], depth + 1))
            fields = getattr(value, "_field_map", None) or {}
            if "extn_id" in fields and "extn_value" in fields:
                ext = value["extn_value"]  # an extension value is DER even where it's typed as a plain OCTET STRING
                if not isinstance(ext, (core.Void, core.ParsableOctetString)) and not _reencodes(bytes(ext)):
                    raise _Unparsable()
            continue
        if isinstance(value, core.SequenceOf):
            for child in value:
                stack.append((child, depth + 1))
            continue
        if isinstance(value, core.Constructable) and getattr(value, "method", 0):
            raise _Unparsable()  # a constructed string: BER only
        c = value.contents or b""
        if isinstance(value, core.ParsableOctetString):  # ParsableOctetBitString too: it carries an encoding
            if isinstance(value, core.ParsableOctetBitString) and not _bits_ok(c):
                raise _Unparsable()
            if not _reencodes(bytes(value)):
                raise _Unparsable()
            stack.append((value.parsed, depth + 1))
            continue
        if isinstance(value, core.Boolean):
            ok = len(c) == 1 and c[0] in (0x00, 0xFF)
        elif isinstance(value, core.Integer):  # ENUMERATED too
            ok = _int_ok(c)
        elif isinstance(value, (core.BitString, core.OctetBitString, core.IntegerBitString)):
            ok = _bits_ok(c)
            if ok and isinstance(value, core.BitString) and getattr(value, "_map", None):
                # A named-bit list (KeyUsage, ReasonFlags and the like) drops trailing zero bits (X.690 11.2.2):
                # no bits at all, or the last used bit is 1.
                ok = len(c) == 1 or bool((c[-1] >> c[0]) & 1)
        elif isinstance(value, core.Null):
            ok = not c
        elif isinstance(value, core.ObjectIdentifier):  # RELATIVE-OID too
            ok = _oid_ok(c)
        elif isinstance(value, (core.UTCTime, core.GeneralizedTime)):
            # The DER form, and a real time: a month 99 matches the digits but can't be re-encoded.
            ok = bool((_UTC_DER if isinstance(value, core.UTCTime) else _GENERALIZED_DER).fullmatch(c))
            if ok:
                try:
                    value.native
                except (ValueError, TypeError, OverflowError):
                    ok = False
        else:
            ok = True
        if not ok:
            raise _Unparsable()


def _parse(token: Any, d: Any) -> Tuple[bytes, Any, Any]:
    """Strict: at most 64 KiB, no trailing bytes, and DER anywhere. The token and the TSTInfo inside it pass the
    byte check (every tag, length and universal primitive already in DER form, nesting bounded); the TSTInfo also
    re-encodes to the same bytes through asn1crypto (which rewrites a genTime or digest parameters it would encode
    differently); and the schema-aware walk (`_strict_walk`) covers what the byte check can't see: the encodings
    inside OCTET STRINGs (certificate and TSTInfo extension values) and implicitly tagged primitives. SET OF
    ordering and DEFAULT values left in aren't checked."""
    if not isinstance(token, (bytes, bytearray)):
        raise _Unparsable()
    token = bytes(token)
    if not token or len(token) > MAX_TOKEN or not _reencodes(token):
        raise _Unparsable()
    ci = d.cms.ContentInfo.load(token, strict=True)
    if ci["content_type"].dotted != _OID_SIGNED_DATA:
        raise _Unparsable()
    sd = ci["content"]
    content = sd["encap_content_info"]["content"]
    if not isinstance(content, (d.core.OctetString, d.core.ParsableOctetString)):
        raise _Unparsable()
    raw = bytes(content)
    if not _reencodes(raw) or d.tsp.TSTInfo.load(raw, strict=True).dump(force=True) != raw:
        raise _Unparsable()
    _strict_walk(d.cms.ContentInfo.load(token, strict=True), d)  # a parse of its own: the walk forces every part
    return token, sd, d.tsp.TSTInfo.load(raw, strict=True)


def _gen_time(value: Any) -> Optional[datetime]:
    """genTime as naive UTC to the second: it must carry Z; fractions are truncated."""
    m = _GEN.fullmatch(value.contents or b"")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode("ascii"), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _hash(d: Any, name: str) -> Any:
    return getattr(d.hashes, _HASH_NAMES[name])()


def _verify(d: Any, spki: Any, alg: Any, data: bytes, signature: bytes, *, digest: Optional[str],
            strong: bool) -> bool:
    """Verify one signature by its signatureAlgorithm: PKCS#1 v1.5, RSASSA-PSS with its parameters, or ECDSA.
    `digest` is the hash the signature must use when the algorithm names none (and must agree with when it
    does); `strong` limits it to SHA-256 or stronger."""
    pub = d.serialization.load_der_public_key(spki.dump())
    name = alg["algorithm"].native
    salt: Optional[int] = None
    if name == "rsassa_pss":
        params = alg["parameters"]
        h = params["hash_algorithm"]["algorithm"].native
        mgf = params["mask_gen_algorithm"]
        if (mgf["algorithm"].native != "mgf1" or mgf["parameters"]["algorithm"].native != h
                or params["trailer_field"].native != "trailer_field_bc"):
            return False
        salt = params["salt_length"].native
        rsa_key = True
    elif name in _RSA_SIGS:
        h, rsa_key = _RSA_SIGS[name] or digest, True
    elif name in _ECDSA_SIGS:
        h, rsa_key = _ECDSA_SIGS[name] or digest, False
    else:
        return False
    if h is None or h not in _HASH_NAMES or (digest is not None and h != digest) or (strong and h not in _STRONG):
        return False
    try:
        if rsa_key:
            if not isinstance(pub, d.rsa.RSAPublicKey):
                return False
            scheme: Any = d.padding.PKCS1v15() if salt is None else d.padding.PSS(
                mgf=d.padding.MGF1(_hash(d, h)), salt_length=salt)
            pub.verify(signature, data, scheme, _hash(d, h))
        else:
            if not isinstance(pub, d.ec.EllipticCurvePublicKey):
                return False
            pub.verify(signature, data, d.ec.ECDSA(_hash(d, h)))
    except d.InvalidSignature:
        return False
    return True


def _attrs(si: Any, oid: str) -> List[Any]:
    return [a for a in si["signed_attrs"] if a["type"].dotted == oid]


def _extensions(d: Any, cert: Any, oid: str) -> List[Any]:
    exts = cert["tbs_certificate"]["extensions"]
    return [] if _void(d, exts) else [e for e in exts if e["extn_id"].dotted == oid]


def _check_1(token: bytes, sd: Any, digest: bytes, d: Any) -> Tuple[Optional[Any], Optional[Any], bool, bool]:
    """Check 1: one SignerInfo with an issuerAndSerialNumber sid naming exactly one certificate (no other one
    sharing its serial), whose key verifies the SignerInfo signature; and, from the unchanged verify_token,
    the imprint and message-digest results. Returns (leaf, signer info, check 1 passed, imprint matched)."""
    tst_imprint = False
    try:
        tst_imprint = d.tsp.TSTInfo.load(bytes(sd["encap_content_info"]["content"]))[
            "message_imprint"]["hashed_message"].native == digest
    except Exception:
        pass
    try:
        sis = sd["signer_infos"]
        if len(sis) != 1:
            return None, None, False, tst_imprint
        si = sis[0]
    except Exception:
        return None, None, False, tst_imprint
    try:
        if si["sid"].name != "issuer_and_serial_number":
            return None, si, False, tst_imprint
        issuer = si["sid"].chosen["issuer"].dump()
        serial = si["sid"].chosen["serial_number"].dump()
        certs = sd["certificates"]
        if _void(d, certs) or not len(certs):
            return None, si, False, tst_imprint
        named, sharing = [], 0
        for choice in certs:
            if choice.name != "certificate":
                return None, si, False, tst_imprint
            tbs = choice.chosen["tbs_certificate"]
            if tbs["serial_number"].dump() == serial:
                sharing += 1
                if tbs["issuer"].dump() == issuer:
                    named.append(choice.chosen)
        if len(named) != 1 or sharing != 1:
            return None, si, False, tst_imprint
        leaf = named[0]
    except Exception:
        return None, si, False, tst_imprint
    try:
        attrs = si["signed_attrs"]
        if _void(d, attrs) or not len(attrs):
            return leaf, si, False, tst_imprint
        md = _attrs(si, _OID_MESSAGE_DIGEST)
        if len(md) != 1 or len(md[0]["values"]) != 1:
            return leaf, si, False, tst_imprint
        to_sign = b"\x31" + attrs.dump()[1:]  # [0] IMPLICIT becomes the SET OF that was signed
        sig_ok = _verify(d, leaf["tbs_certificate"]["subject_public_key_info"], si["signature_algorithm"],
                         to_sign, si["signature"].native, digest=si["digest_algorithm"]["algorithm"].native,
                         strong=False)
    except Exception:
        return leaf, si, False, tst_imprint
    try:
        found = rfc3161.verify_token(token, digest)
        imprint_ok, md_ok = found["imprint_ok"] is True, found["message_digest_ok"] is True
    except Exception:
        return leaf, si, False, tst_imprint
    return leaf, si, bool(sig_ok and imprint_ok and md_ok), imprint_ok


def _check_2(tst: Any, si: Any, d: Any) -> bool:
    """The imprint is exactly SHA-256 (parameters absent or NULL) with a 32-byte value; the signer digest is
    SHA-256, SHA-384 or SHA-512. Without a single SignerInfo (check 1 has failed) only the imprint is seen."""
    alg = tst["message_imprint"]["hash_algorithm"]
    params = alg["parameters"]
    if alg["algorithm"].dotted != _OID_SHA256 or params.dump() not in (b"", b"\x05\x00"):
        return False
    if len(tst["message_imprint"]["hashed_message"].native) != 32:
        return False
    return si is None or si["digest_algorithm"]["algorithm"].native in _STRONG


def _check_3(sd: Any, si: Any) -> bool:
    """eContentType is id-ct-TSTInfo, and the content-type signed attribute is present once with that value.
    Without a single SignerInfo (check 1 has failed) only eContentType is seen."""
    if sd["encap_content_info"]["content_type"].dotted != _OID_TST_INFO:
        return False
    if si is None:
        return True
    ct = _attrs(si, _OID_CONTENT_TYPE)
    return len(ct) == 1 and len(ct[0]["values"]) == 1 and ct[0]["values"][0].dotted == _OID_TST_INFO


def _check_4(leaf: Any, d: Any) -> bool:
    """A critical extended key usage of exactly id-kp-timeStamping."""
    eku = _extensions(d, leaf, _OID_EKU)
    if len(eku) != 1 or eku[0]["critical"].native is not True:
        return False
    return [p.dotted for p in eku[0]["extn_value"].parsed] == [_OID_TIME_STAMPING]


def _issuer_serial_ok(entry: Any, leaf: Any, d: Any) -> bool:
    isr = entry["issuer_serial"]
    if _void(d, isr):
        return True
    names = isr["issuer"]
    tbs = leaf["tbs_certificate"]
    return (len(names) == 1 and names[0].name == "directory_name"
            and names[0].chosen.chosen.dump() == tbs["issuer"].chosen.dump()
            and isr["serial_number"].dump() == tbs["serial_number"].dump())


def _check_5(leaf: Any, si: Any, d: Any) -> bool:
    """The first ESSCertID or ESSCertIDv2 entry hashes the leaf, and its issuerSerial matches when present.
    v2 uses SHA-256 or stronger; only v1 may use SHA-1. Each attribute present must hold exactly once."""
    leaf_der = leaf.dump()
    found = False
    for oid, v2 in ((_OID_ESS_V1, False), (_OID_ESS_V2, True)):
        attrs = _attrs(si, oid)
        if not attrs:
            continue
        if len(attrs) != 1 or len(attrs[0]["values"]) != 1:
            return False
        certs = attrs[0]["values"][0]["certs"]
        if not len(certs):
            return False
        first = certs[0]
        h = first["hash_algorithm"]["algorithm"].native if v2 else "sha1"
        if v2 and h not in _STRONG:
            return False
        if first["cert_hash"].native != hashlib.new(h, leaf_der).digest() or not _issuer_serial_ok(first, leaf, d):
            return False
        found = True
    return found


def _when(value: Any) -> Optional[datetime]:
    native = value.native
    return None if native is None else _naive(native)


def _subject_o_ok(leaf: Any, want: str) -> bool:
    """Exactly one O attribute, in a single-valued RDN, equal to `want` byte for byte (as UTF-8, with no
    normalisation, case folding or trimming)."""
    rdns = leaf["tbs_certificate"]["subject"].chosen
    hits = [(rdn, atv) for rdn in rdns for atv in rdn if atv["type"].dotted == _OID_O]
    if len(hits) != 1 or len(hits[0][0]) != 1:
        return False
    value = hits[0][1]["value"].native
    return isinstance(value, str) and value.encode("utf-8") == want.encode("utf-8")


def _check_6(leaf: Any, sd: Any, pins: Sequence[TsaPin], gen: Optional[datetime], d: Any) -> Optional[TsaPin]:
    """For some (pin, subject_o) pair, as one unit: the token holds a CA certificate whose SHA-256 is the pin,
    the leaf's issuer is that CA's subject (DER), the CA key verifies the leaf with SHA-256 or stronger, genTime
    is inside the leaf's validity and privateKeyUsagePeriod, and the leaf carries subject_o when it's set."""
    if gen is None:
        return None
    tbs = leaf["tbs_certificate"]
    validity = tbs["validity"]
    not_before, not_after = _naive(validity["not_before"].native), _naive(validity["not_after"].native)
    if not not_before <= gen <= not_after:
        return None
    pkup = _extensions(d, leaf, _OID_PKUP)
    if len(pkup) > 1:
        return None
    if pkup:
        period = pkup[0]["extn_value"].parsed
        start, end = _when(period["not_before"]), _when(period["not_after"])
        if (start is not None and gen < start) or (end is not None and gen > end):
            return None
    certs = [c.chosen for c in sd["certificates"]]
    by_pin = {hashlib.sha256(c.dump()).hexdigest(): c for c in certs}
    for p in pins:
        ca = by_pin.get(p.ca_sha256)
        if ca is None or tbs["issuer"].dump() != ca["tbs_certificate"]["subject"].dump():
            continue
        if not _verify(d, ca["tbs_certificate"]["subject_public_key_info"], leaf["signature_algorithm"],
                       tbs.dump(), leaf["signature_value"].native, digest=None, strong=True):
            continue
        if p.subject_o is not None and not _subject_o_ok(leaf, p.subject_o):
            continue
        return p
    return None


def _guard(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        return None


def _check(token: Any, digest: bytes, pins: Sequence[TsaPin], *, now: datetime,
           created_at: Optional[datetime], d: Any) -> TokenCheck:
    try:
        token, sd, tst = _parse(token, d)
    except Exception:
        return TokenCheck(UNVERIFIED, (F_PARSE,))
    failed: List[str] = []
    gen = _guard(lambda: _gen_time(tst["gen_time"]))
    policy = _guard(lambda: tst["policy"].dotted)
    leaf, si, ok_1, imprint_ok = _check_1(token, sd, digest, d)
    if not ok_1:
        failed.append(CHECK_1)
    if not imprint_ok:
        failed.append(F_IMPRINT)
    if not _guard(_check_2, tst, si, d):
        failed.append(CHECK_2)
    if not _guard(_check_3, sd, si):
        failed.append(CHECK_3)
    pin = None
    if leaf is not None:
        if not _guard(_check_4, leaf, d):
            failed.append(CHECK_4)
        if si is None or not _guard(_check_5, leaf, si, d):
            failed.append(CHECK_5)
        pin = _guard(_check_6, leaf, sd, pins, gen, d)
        if pin is None:
            failed.append(CHECK_6)
    if gen is None:
        failed.append(F_GEN_TIME)
    elif gen > now + GEN_TIME_AHEAD:
        failed.append(F_GEN_TIME_FUTURE)
    skew = bool(created_at is not None and gen is not None and abs(gen - created_at) > SKEW_LIMIT)
    return TokenCheck(PRESENT if not failed else UNVERIFIED, tuple(failed), gen, policy, pin, skew)


def check_token(token: bytes, header: bytes, pins: Iterable[TsaPin], *, now: datetime,
                created_at: Optional[datetime] = None) -> TokenCheck:
    """Check `irp/checkpoint.tsr` against the exact `irp/checkpoint.json` bytes it should stamp (§18a checks 1
    to 6). `pins` are the configured (pin, subject_o) pairs; `now` is the verifier's clock and `created_at` the
    header's, both naive UTC (aware values are converted). PRESENT when everything passes and genTime is at
    most 5 minutes after `now`; UNVERIFIED otherwise. Any exception from parsing or checking gives UNVERIFIED
    and never propagates; a missing optional dependency still raises RoamDependencyError."""
    d = _deps()
    try:
        digest = hashlib.sha256(bytes(header)).digest()
        pin_list = tuple(pins)
        now_n = _naive(now)
        created = None if created_at is None else _naive(created_at)
    except Exception:
        return TokenCheck(UNVERIFIED, (F_PARSE,))
    try:
        return _check(token, digest, pin_list, now=now_n, created_at=created, d=d)
    except Exception:
        return TokenCheck(UNVERIFIED, (F_PARSE,))


# ── C3: the client key, encrypted for its one trip through the disk ──

def _pem(label: str, der: bytes) -> bytes:
    b64 = base64.b64encode(der)
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    name = label.encode("ascii")
    return b"-----BEGIN " + name + b"-----\n" + b"\n".join(lines) + b"\n-----END " + name + b"-----\n"


def _random(rng: Callable[[int], bytes], n: int) -> bytes:
    value = rng(n)
    if not isinstance(value, bytes) or len(value) != n:
        raise TsaError("rng must return fresh random bytes of the length asked")
    return value


def _encrypt_key(key_pem: str, rng: Callable[[int], bytes], d: Any) -> Tuple[bytes, bytes]:
    """The key as PKCS#8 EncryptedPrivateKeyInfo PEM (PBES2, PBKDF2-HMAC-SHA256, AES-256-CBC) under a fresh
    random 32-byte passphrase. Only the encrypted form ever leaves memory."""
    try:
        key = d.serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
        plain = key.private_bytes(d.serialization.Encoding.DER, d.serialization.PrivateFormat.PKCS8,
                                  d.serialization.NoEncryption())
    except Exception:
        raise TsaError("the stored client key isn't a usable PEM private key") from None
    password, salt, iv = _random(rng, 32), _random(rng, 16), _random(rng, 16)
    kdf = d.PBKDF2HMAC(algorithm=d.hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ROUNDS)
    kek = kdf.derive(password)
    padder = d.sym_padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = d.Cipher(d.algorithms.AES(kek), d.modes.CBC(iv)).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    info = d.keys.EncryptedPrivateKeyInfo({
        "encryption_algorithm": {"algorithm": "pbes2", "parameters": {
            "key_derivation_func": {"algorithm": "pbkdf2", "parameters": {
                "salt": d.algos.Pbkdf2Salt(name="specified", value=salt), "iteration_count": PBKDF2_ROUNDS,
                "prf": {"algorithm": "sha256", "parameters": None}}},
            "encryption_scheme": {"algorithm": "aes256_cbc", "parameters": iv}}},
        "encrypted_data": ciphertext})
    return _pem("ENCRYPTED PRIVATE KEY", info.dump()), password


def _cert_chain(cert_pem: str, d: Any) -> bytes:
    """The certificate file's bytes: the certificates parsed out of the stored text, written back as PEM. Any
    other block in that text (a private key stored with the certificate, say) never reaches the disk."""
    try:
        certs = d.x509.load_pem_x509_certificates(cert_pem.encode("ascii"))
        if not certs:
            raise ValueError
        return b"".join(c.public_bytes(d.serialization.Encoding.PEM) for c in certs)
    except Exception:
        raise TsaError("the stored client certificate isn't PEM") from None


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _remove_folder(folder: Path) -> None:
    st = os.lstat(folder)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(folder)
    else:
        os.unlink(folder)


@contextmanager
def client_key_files(cert_pem: str, key_pem: str, keys_dir: Path | str, *,
                     rng: Callable[[int], bytes] = os.urandom) -> Iterator[ClientKeyFiles]:
    """C3: the client certificate and its key as files `ssl` can load, for the length of the `with` block.

    The key is re-encrypted in memory as PKCS#8 under a fresh random 32-byte passphrase and written 0600, with
    the certificate, into a fresh 0700 folder under `keys_dir` (~/.irp-roam/keys), which is removed in a
    `finally`. A folder a crash leaves behind holds only the encrypted key; `cleanup_key_folders` removes it."""
    d = _deps()
    if not isinstance(cert_pem, str) or not isinstance(key_pem, str):
        raise TsaError("the stored client certificate and key must be text")
    cert_bytes = _cert_chain(cert_pem, d)
    encrypted, password = _encrypt_key(key_pem, rng, d)
    base = Path(keys_dir)
    try:
        st = os.lstat(base)
    except OSError:
        raise TsaError("the keys folder is missing") from None
    if not stat.S_ISDIR(st.st_mode):
        raise TsaError("the keys folder must be a real folder, not a symlink or a file")
    folder = Path(tempfile.mkdtemp(prefix=KEY_FOLDER_PREFIX, dir=str(base)))
    try:
        os.chmod(folder, 0o700)
        files = ClientKeyFiles(folder=folder, cert=folder / "client.crt", key=folder / "client.key",
                               password=password)
        _write_new(files.key, encrypted)
        _write_new(files.cert, cert_bytes)
        yield files
    finally:
        try:
            _remove_folder(folder)
        except OSError:
            pass  # cleanup_key_folders removes it at the next checkpoint


def cleanup_key_folders(keys_dir: Path | str) -> int:
    """Remove leftover client-key folders (`tsa-key-*`) under `keys_dir`, never following a symlink. Returns how
    many it removed; raises TsaError if one can't be removed. Run at the start of every make_checkpoint."""
    base = Path(keys_dir)
    if not base.is_dir():
        return 0
    removed = 0
    stuck = []
    for name in sorted(os.listdir(base)):
        if not name.startswith(KEY_FOLDER_PREFIX):
            continue
        try:
            _remove_folder(base / name)
            removed += 1
        except OSError:
            stuck.append(name)
    if stuck:
        raise TsaError(f"couldn't remove leftover client-key folders: {', '.join(stuck)}")
    return removed


# ── The transport ──

class _NoAnswer(Exception):
    """A TSA gave no usable answer. Carries a fixed reason code, never an exception's text."""

    def __init__(self, reason: str, status: Optional[int] = None, content_type: Optional[str] = None):
        super().__init__(reason)
        self.reason, self.status, self.content_type = reason, status, content_type


class _Watchdog:
    """Shuts the connection down when the deadline passes, so no read (a trickled status line, header or body)
    can outlast it. The socket timeouts are set to the time left as well."""

    def __init__(self, deadline: float):
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self.fired = False
        self._done = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
            if self.fired:
                self._shutdown()

    def _shutdown(self) -> None:
        if self._sock is not None:
            try:
                socket.socket.shutdown(self._sock, socket.SHUT_RDWR)  # the fd itself, under any TLS layer
            except OSError:
                pass

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.fired = True
            self._shutdown()

    def stop(self) -> None:
        with self._lock:
            self._done = True
        self._timer.cancel()


class _Connection(http.client.HTTPConnection):
    """http.client over a socket this module connected (and wrapped in TLS) itself, so the name is resolved
    once under the deadline and nothing opens another connection: no proxy, no redirect, no retry."""

    def __init__(self, host: str, port: int, sock: socket.socket, default_port: int):
        super().__init__(host, port)
        self.default_port = default_port  # so Host carries the port only when it isn't the scheme's own
        self.sock = sock

    def connect(self) -> None:
        raise OSError("closed")


def _default_resolver(host: str, port: int) -> Sequence[Tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP)


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _NoAnswer(TIMEOUT)
    return left


def _resolve(resolver: Resolver, host: str, port: int, deadline: float) -> List[Tuple[Any, ...]]:
    box: Dict[str, Any] = {}

    def work() -> None:
        try:
            box["addrs"] = list(resolver(host, port))
        except Exception:
            box["failed"] = True

    worker = threading.Thread(target=work, name="irp-roam-tsa-resolve", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise _NoAnswer(RESOLVE_TIMEOUT)  # the worker is abandoned; it ends on its own
    addrs = box.get("addrs")
    if box.get("failed") or not addrs:
        raise _NoAnswer(RESOLVE_FAILED)
    for a in addrs:
        if not (isinstance(a, tuple) and len(a) == 5 and a[0] in (socket.AF_INET, socket.AF_INET6)):
            raise _NoAnswer(RESOLVE_FAILED)
    return addrs


def _connect(addrs: Sequence[Tuple[Any, ...]], deadline: float, watchdog: _Watchdog) -> socket.socket:
    for family, _type, proto, _canon, sockaddr in addrs:
        left = _left(deadline)
        s = socket.socket(family, socket.SOCK_STREAM, proto)
        watchdog.watch(s)
        try:
            s.settimeout(left)
            s.connect(sockaddr)
            return s
        except OSError:
            s.close()
    if time.monotonic() >= deadline:
        raise _NoAnswer(TIMEOUT)
    raise _NoAnswer(CONNECT_FAILED)


def _context(factory: Callable[[], ssl.SSLContext]) -> ssl.SSLContext:
    try:
        ctx = factory()
    except Exception:
        raise _NoAnswer(TLS_FAILED) from None  # the local TLS setup failed: no TSA can answer over it
    if not isinstance(ctx, ssl.SSLContext) or ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise TsaConfigError("the TLS context must verify the TSA's certificate and name")
    if ctx.minimum_version < ssl.TLSVersion.TLSv1_2:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _clean_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _TYPE_CHARS.sub("", value)[:80]


def _media_type(value: Optional[str]) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _basic(entry: TsaEntry, creds: Mapping[str, Any]) -> str:
    user, password = creds.get(entry.name + "/user"), creds.get(entry.name + "/password")
    if user is None or password is None:
        raise _NoAnswer(MISSING_CREDENTIAL)
    if (not isinstance(user, str) or not isinstance(password, str) or not user or ":" in user
            or _CONTROL.search(user) or _CONTROL.search(password)):
        raise _NoAnswer(BAD_CREDENTIAL)
    try:
        token = base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")
    except UnicodeEncodeError:
        raise _NoAnswer(BAD_CREDENTIAL) from None
    return "Basic " + token


def _note_tls(name: str) -> bool:
    with _tls_lock:
        if name in _tls_noted:
            return False
        _tls_noted.add(name)
        return True


def _read_reply(resp: http.client.HTTPResponse, deadline: float) -> bytes:
    """Status 200, the reply content type, and a body of at most 64 KiB read in chunks against the deadline.
    Each read is also bounded by the socket timeout set before the request and by the watchdog."""
    status, ctype = resp.status, _clean_type(resp.getheader("Content-Type"))
    if 300 <= status < 400:
        raise _NoAnswer(REDIRECT, status, ctype)
    if status != 200:
        raise _NoAnswer(HTTP_STATUS, status, ctype)
    if _media_type(ctype) != REPLY_TYPE:
        raise _NoAnswer(CONTENT_TYPE, status, ctype)
    expected = resp.length
    if expected is not None and expected > MAX_REPLY:
        raise _NoAnswer(TOO_LARGE, status, ctype)
    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise _NoAnswer(TIMEOUT, status, ctype)
        chunk = resp.read1(READ_CHUNK)
        if not chunk:
            break
        body += chunk
        if len(body) > MAX_REPLY:
            raise _NoAnswer(TOO_LARGE, status, ctype)
    if expected is not None and len(body) != expected:
        raise _NoAnswer(TRANSPORT_FAILED, status, ctype)
    return bytes(body)


def _exchange(entry: TsaEntry, request: bytes, headers: Dict[str, str], ctx: Optional[ssl.SSLContext],
              resolver: Resolver, deadline: float, notes: List[TsaAlert]) -> bytes:
    parts = urlsplit(entry.url)
    https = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if https else 80)
    addrs = _resolve(resolver, host, port, deadline)
    watchdog = _Watchdog(deadline)
    sock: Optional[socket.socket] = None
    status: Optional[int] = None
    ctype: Optional[str] = None
    try:
        sock = _connect(addrs, deadline, watchdog)
        if ctx is not None:
            try:
                tls = ctx.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                sock = tls
                watchdog.watch(tls)
                tls.settimeout(_left(deadline))
                tls.do_handshake()
            except _NoAnswer:
                raise
            except Exception:
                raise _NoAnswer(TIMEOUT if watchdog.fired or time.monotonic() >= deadline else TLS_FAILED) from None
            if entry.auth == "client_cert" and tls.version() != "TLSv1.3" and _note_tls(entry.name):
                notes.append(TsaAlert(entry.name, TLS_BELOW_1_3))
        conn = _Connection(host, port, sock, 443 if https else 80)
        resp: Optional[http.client.HTTPResponse] = None
        try:
            sock.settimeout(_left(deadline))
            conn.request("POST", parts.path or "/", body=request, headers=headers)
            sock.settimeout(_left(deadline))
            resp = conn.getresponse()
            status, ctype = resp.status, _clean_type(resp.getheader("Content-Type"))
            body = _read_reply(resp, deadline)
            if watchdog.fired:  # the deadline cut the connection: whatever arrived is incomplete
                raise _NoAnswer(TIMEOUT, status, ctype)
            return body
        except _NoAnswer as na:
            if watchdog.fired and na.reason != TIMEOUT:
                raise _NoAnswer(TIMEOUT, status, ctype) from None
            raise
        except Exception:
            late = watchdog.fired or time.monotonic() >= deadline
            raise _NoAnswer(TIMEOUT if late else TRANSPORT_FAILED, status, ctype) from None
        finally:
            if resp is not None:
                resp.close()
            conn.close()
    finally:
        watchdog.stop()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _ask(entry: TsaEntry, request: bytes, *, creds: Mapping[str, Any], keys_dir: Optional[Path | str],
         rng: Callable[[int], bytes], timeout: float, resolver: Resolver,
         context_factory: Callable[[], ssl.SSLContext], notes: List[TsaAlert]) -> bytes:
    """One TSA, one request, one deadline. Returns the exact TimeStampToken DER or raises _NoAnswer."""
    deadline = time.monotonic() + timeout
    https = entry.url.startswith("https://")
    headers = {"Content-Type": REQUEST_TYPE, "Accept": REPLY_TYPE, "Connection": "close"}
    if entry.auth == "basic":
        headers["Authorization"] = _basic(entry, creds)
    ctx = _context(context_factory) if https else None
    if entry.auth == "client_cert":
        cert_pem, key_pem = creds.get(entry.name + "/cert_pem"), creds.get(entry.name + "/key_pem")
        if cert_pem is None or key_pem is None:
            raise _NoAnswer(MISSING_CREDENTIAL)
        if ctx is None or keys_dir is None:
            raise _NoAnswer(CLIENT_KEY_FAILED)
        try:
            with client_key_files(cert_pem, key_pem, keys_dir, rng=rng) as files:
                ctx.load_cert_chain(str(files.cert), str(files.key), password=files.password)
        except Exception:
            raise _NoAnswer(CLIENT_KEY_FAILED) from None
    body = _exchange(entry, request, headers, ctx, resolver, deadline, notes)
    d = _deps()
    try:
        resp = _resp_spec(d).load(body, strict=True)
        granted = resp["status"]["status"].native
    except Exception:
        raise _NoAnswer(PARSE_FAILED, 200, REPLY_TYPE) from None
    if granted != "granted":
        raise _NoAnswer(NOT_GRANTED, 200, REPLY_TYPE)
    try:
        token = _token_bytes(body)
        if _void(d, resp["time_stamp_token"]) or token != resp["time_stamp_token"].dump():
            raise ValueError("no token")
        return token
    except Exception:
        raise _NoAnswer(PARSE_FAILED, 200, REPLY_TYPE) from None


def _tlv(data: bytes, start: int, end: int) -> Tuple[int, int]:
    """(where the contents start, where the TLV ends) for the definite-length TLV at data[start:end]."""
    if start + 2 > end or data[start] & 0x1F == 0x1F:
        raise _Unparsable()
    first, i = data[start + 1], start + 2
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4 or i + n > end:
            raise _Unparsable()
        length = int.from_bytes(data[i:i + n], "big")
        i += n
    if i + length > end:
        raise _Unparsable()
    return i, i + length


def _token_bytes(reply: bytes) -> bytes:
    """The token exactly as the TSA sent it: the second element of the TimeStampResp SEQUENCE, sliced from the
    reply rather than re-encoded, so check_token judges the bytes that will ship as irp/checkpoint.tsr."""
    inner, stop = _tlv(reply, 0, len(reply))
    if stop != len(reply):
        raise _Unparsable()
    _, after_status = _tlv(reply, inner, stop)
    _, after_token = _tlv(reply, after_status, stop)
    if after_token != stop:
        raise _Unparsable()
    return reply[after_status:after_token]


_RESP_SPEC: List[Any] = []


def _resp_spec(d: Any) -> Any:
    """TimeStampResp with its token optional, as RFC 3161 has it (asn1crypto's own class requires the token,
    so it can't read a refusal)."""
    if not _RESP_SPEC:
        class TimeStampResp(d.core.Sequence):
            _fields = [("status", d.tsp.PKIStatusInfo),
                       ("time_stamp_token", d.cms.ContentInfo, {"optional": True})]
        _RESP_SPEC.append(TimeStampResp)
    return _RESP_SPEC[0]


def _verdict(chk: TokenCheck, previous: Optional[datetime]) -> Optional[str]:
    """Why a token is no answer, or None when it's PRESENT or fails only the pin."""
    failed = set(chk.failed)
    if F_PARSE in failed or F_GEN_TIME in failed or chk.gen_time is None:
        return PARSE_FAILED
    if F_IMPRINT in failed:
        return IMPRINT_MISMATCH
    if F_GEN_TIME_FUTURE in failed:
        return GEN_TIME_AHEAD_REASON
    if previous is not None and chk.gen_time < previous:
        return GEN_TIME_BEHIND
    for code in (CHECK_1, CHECK_2, CHECK_3, CHECK_4, CHECK_5):
        if code in failed:
            return code
    return None


def stamp(header: bytes, tsas: TsaList | Sequence[TsaEntry], *, creds: Optional[Mapping[str, Any]],
          keys_dir: Optional[Path | str], clock: Callable[[], datetime], created_at: datetime,
          last_present_gen_time: Optional[datetime] = None, latest_line_at: Optional[datetime] = None,
          pins: Optional[Sequence[TsaPin]] = None, rng: Callable[[int], bytes] = os.urandom,
          timeout: float = TSA_DEADLINE, resolver: Optional[Resolver] = None,
          context_factory: Optional[Callable[[], ssl.SSLContext]] = None,
          allow_http: bool = False) -> StampResult:
    """The TSA step of making a checkpoint: ask each TSA in order for a token over SHA-256 of `header` (the
    exact `irp/checkpoint.json` bytes) and pick at most one, by C2.

    - `creds` is the keystore's `tsa_creds` (or None); `keys_dir` is ~/.irp-roam/keys, where a client key's
      encrypted file lives for one `load_cert_chain` call.
    - `clock` is the local clock, read after each answer for the genTime bound (+5 minutes); `created_at` is
      the header's (for `created_at_skew`); `last_present_gen_time` is `last_present.gen_time` from the record
      when it's in this epoch (a genTime before it is no answer); `latest_line_at` is the newest `at` among
      the devices and readers lines past `last_present`'s lengths (the genTime + 1 hour of any token
      attached, PRESENT or kept for the pin alone, must reach it). All are naive UTC; aware values are converted.
    - `pins` defaults to every configured (pin, subject_o) pair, as a reader with the bundle's pins sees them.
    - `timeout`, `resolver`, `context_factory` and `allow_http` exist for tests. Production keeps the 20-second
      deadline, `socket.getaddrinfo`, `ssl.create_default_context` and https only.

    Raises TsaConfigError for an entry outside §18a (before any request) or a TLS context that doesn't verify
    (before that TSA's request). Every TSA failure is an alert in the result instead, never an exception."""
    if not isinstance(header, (bytes, bytearray)):
        raise TsaError("the header must be the exact checkpoint.json bytes")
    if isinstance(tsas, TsaList):
        tsas = tsas.entries
    entries = _check_entries(tuple(tsas), allow_http=allow_http)
    pin_list = tuple(pins) if pins is not None else pins_from_tsas(entries)
    creds = creds or {}
    created = _naive(created_at)
    previous = None if last_present_gen_time is None else _naive(last_present_gen_time)
    latest = None if latest_line_at is None else _naive(latest_line_at)
    digest = hashlib.sha256(bytes(header)).digest()
    request = rfc3161.build_request(digest, hash_alg="sha256", cert_req=True)
    d = _deps()
    alerts: List[TsaAlert] = []
    kept: Optional[Tuple[str, bytes, TokenCheck]] = None
    for entry in entries:
        notes: List[TsaAlert] = []
        try:
            token = _ask(entry, request, creds=creds, keys_dir=keys_dir, rng=rng, timeout=timeout,
                         resolver=resolver or _default_resolver,
                         context_factory=context_factory or ssl.create_default_context, notes=notes)
        except _NoAnswer as na:
            alerts += notes
            alerts.append(TsaAlert(entry.name, na.reason, na.status, na.content_type))
            continue
        except TsaConfigError:
            raise
        except Exception:  # anything unforeseen is still only this TSA failing, never the checkpoint
            alerts += notes
            alerts.append(TsaAlert(entry.name, TRANSPORT_FAILED))
            continue
        alerts += notes
        chk = _check(token, digest, pin_list, now=_naive(clock()), created_at=created, d=d)
        why = _verdict(chk, previous)
        if why is not None:
            alerts.append(TsaAlert(entry.name, why, 200, REPLY_TYPE))
            continue
        # The line rule before the pin: a token kept aside must fail nothing but the pin, since a verifier whose
        # pins accept it later counts it PRESENT.
        if latest is not None and chk.gen_time is not None and latest > chk.gen_time + LINE_SLACK:
            alerts.append(TsaAlert(entry.name, LINE_AFTER_GEN_TIME, 200, REPLY_TYPE))
            continue
        if chk.failed:  # check 6 alone: kept aside, and the next TSA is still tried
            alerts.append(TsaAlert(entry.name, PIN_FAILED, 200, REPLY_TYPE))
            if kept is None:
                kept = (entry.name, token, chk)
            continue
        return StampResult(PRESENT, token, entry.name, chk.gen_time, chk.policy, chk.created_at_skew,
                           tuple(alerts))
    if kept is not None:
        name, token, chk = kept
        return StampResult(UNVERIFIED, token, name, chk.gen_time, chk.policy, chk.created_at_skew, tuple(alerts))
    return StampResult(NONE, alerts=tuple(alerts))
