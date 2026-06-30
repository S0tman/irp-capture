"""RFC 3161 timestamp tokens: build requests, parse and verify tokens.

Uses `asn1crypto` for the ASN.1 / CMS structures and `cryptography` for the
signature verification. Both are pulled by the `[integrity]` extra.

Scope of `verify_token`: it confirms (1) the token binds the expected digest,
(2) the signed message-digest attribute matches the TSTInfo content, and (3) the
TSA's signature over the signed attributes is valid using the certificate
embedded in the token (RSA or ECDSA). It deliberately does NOT assert that the
TSA is trusted: certificate-path validation against a trust root is the
verifier's policy, supplied separately. We never bake in a trust root.
"""
from __future__ import annotations

import hashlib
import urllib.request
from typing import Any, Optional

from .errors import IntegrityDependencyError


def _deps():
    try:
        from asn1crypto import algos, core, tsp  # type: ignore
        from cryptography import x509  # type: ignore
        from cryptography.exceptions import InvalidSignature  # type: ignore
        from cryptography.hazmat.primitives import hashes  # type: ignore
        from cryptography.hazmat.primitives.asymmetric import ec, padding  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise IntegrityDependencyError(
            "Attestation needs asn1crypto and cryptography. Install with: "
            "pip install 'irp-capture[integrity]'"
        ) from exc
    return {
        "algos": algos, "core": core, "tsp": tsp, "x509": x509,
        "InvalidSignature": InvalidSignature, "hashes": hashes, "ec": ec, "padding": padding,
    }


_HASHES = {"sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512", "sha1": "SHA1"}


def _hash_obj(d, name: str):
    cls = getattr(d["hashes"], _HASHES[name])
    return cls()


def build_request(digest: bytes, *, hash_alg: str = "sha256", cert_req: bool = True) -> bytes:
    """Build a DER-encoded RFC 3161 TimeStampReq for a digest."""
    d = _deps()
    tsp, algos = d["tsp"], d["algos"]
    mi = tsp.MessageImprint({
        "hash_algorithm": algos.DigestAlgorithm({"algorithm": hash_alg}),
        "hashed_message": digest,
    })
    return tsp.TimeStampReq({"version": 1, "message_imprint": mi, "cert_req": cert_req}).dump()


def request_timestamp(digest: bytes, tsa_url: str, *, hash_alg: str = "sha256", timeout: int = 20) -> bytes:
    """POST a TimeStampReq to a TSA and return the DER TimeStampToken bytes.

    Sends only the digest. Raises on a non-granted response.
    """
    d = _deps()
    tsp = d["tsp"]
    body = build_request(digest, hash_alg=hash_alg)
    req = urllib.request.Request(
        tsa_url, data=body, headers={"Content-Type": "application/timestamp-query"}
    )
    resp_der = urllib.request.urlopen(req, timeout=timeout).read()
    resp = tsp.TimeStampResp.load(resp_der)
    status = resp["status"]["status"].native
    if status not in ("granted", "granted_with_mods"):
        info = resp["status"]["status_string"].native if resp["status"]["status_string"] else ""
        raise RuntimeError(f"TSA did not grant the timestamp (status: {status}; {info})")
    return resp["time_stamp_token"].dump()


def read_tst_info(token_der: bytes) -> dict[str, Any]:
    """Parse a TimeStampToken and return its TSTInfo fields (no verification)."""
    d = _deps()
    tsp = d["tsp"]
    token = tsp.ContentInfo.load(token_der)
    tst = token["content"]["encap_content_info"]["content"].parsed
    acc = tst["accuracy"]
    return {
        "gen_time": tst["gen_time"].native,
        "accuracy": acc.native if acc.native is not None else None,
        "policy": tst["policy"].native,
        "serial_number": tst["serial_number"].native,
        "hash_alg": tst["message_imprint"]["hash_algorithm"]["algorithm"].native,
        "hashed_message": tst["message_imprint"]["hashed_message"].native,
    }


def verify_token(token_der: bytes, expected_digest: bytes) -> dict[str, Any]:
    """Verify a TimeStampToken cryptographically against an expected digest.

    Returns a dict of granular booleans and extracted facts. Does not assert TSA
    trust-root validation (that is verifier policy).
    """
    d = _deps()
    tsp, x509, ec, padding = d["tsp"], d["x509"], d["ec"], d["padding"]

    token = tsp.ContentInfo.load(token_der)
    signed = token["content"]
    si = signed["signer_infos"][0]
    tst = signed["encap_content_info"]["content"].parsed

    dig_alg = si["digest_algorithm"]["algorithm"].native

    # 1. The token binds our digest.
    imprint = tst["message_imprint"]["hashed_message"].native
    imprint_ok = imprint == expected_digest

    # 2. The signed message-digest attribute matches the TSTInfo content.
    econtent = signed["encap_content_info"]["content"].parsed.dump()
    h = hashlib.new(dig_alg)
    h.update(econtent)
    md_attr = None
    for attr in si["signed_attrs"]:
        if attr["type"].native == "message_digest":
            md_attr = attr["values"][0].native
            break
    message_digest_ok = md_attr is not None and md_attr == h.digest()

    # 3. The TSA signature over the signed attributes is valid.
    attrs_der = si["signed_attrs"].dump()
    attrs_der = b"\x31" + attrs_der[1:]  # [0] IMPLICIT -> SET OF for signing
    serial = si["sid"].chosen["serial_number"].native
    signer_cert = None
    for c in signed["certificates"]:
        if c.chosen.serial_number == serial:
            signer_cert = c.chosen
            break
    signature_ok = False
    signer_subject = None
    if signer_cert is not None:
        cert = x509.load_der_x509_certificate(signer_cert.dump())
        signer_subject = cert.subject.rfc4514_string()
        pub = cert.public_key()
        sig = si["signature"].native
        halg = _hash_obj(d, dig_alg)
        try:
            if isinstance(pub, ec.EllipticCurvePublicKey):
                pub.verify(sig, attrs_der, ec.ECDSA(halg))
            else:
                pub.verify(sig, attrs_der, padding.PKCS1v15(), halg)
            signature_ok = True
        except d["InvalidSignature"]:
            signature_ok = False

    acc = tst["accuracy"]
    return {
        "imprint_ok": imprint_ok,
        "message_digest_ok": message_digest_ok,
        "signature_ok": signature_ok,
        "cryptographically_valid": bool(imprint_ok and message_digest_ok and signature_ok),
        "gen_time": tst["gen_time"].native,
        "accuracy": acc.native if acc.native is not None else None,
        "policy": tst["policy"].native,
        "serial_number": tst["serial_number"].native,
        "hash_alg": dig_alg,
        "signer_subject": signer_subject,
        "signer_cert_present": signer_cert is not None,
    }
