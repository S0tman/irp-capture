# PR2 — Deterministic Integrity Snapshots + External Timestamp Anchoring

Status: PR2a (snapshot) and PR2b (RFC 3161 attestation) shipped.
Origin: Allen Smith's trust challenge (see `TRUST.md`). Converged design (Claude + GPT double diamond, 2026-06-30).

## PR2b — RFC 3161 attestation (shipped)

`irp attest create <snapshot> [--tsa-url URL]` sends only the snapshot digest to a TSA (network) and stores a detached receipt under `.irp/integrity/receipts/`: the DER token (`<id>.tsr`) plus `<id>.receipt.json` (tsa_url, genTime, accuracy, policy, serial, token_sha256, anchored digest).

`irp attest verify <snapshot>` is offline given the token. It chains: (1) the manifest still hashes to the stored `snapshot_digest`; (2) an RFC 3161 token binds exactly that digest; (3) the token's message-digest attribute matches the TSTInfo content; (4) the TSA signature over the signed attributes is valid using the certificate embedded in the token.

Implementation notes (validated against real freetsa, fixture in `tests/fixtures/`):
- ASN.1/CMS via `asn1crypto`, signature via `cryptography`. Handles **both RSA and ECDSA** signers (freetsa signs ECDSA). signedAttrs are re-encoded as SET OF (tag 0x31) for verification; the message-digest is taken over `TSTInfo.parsed.dump()`.
- **Trust honesty:** a valid signature is reported, but certificate-path validation to a trust root is the verifier's policy and is NOT performed without configured roots (reported as `trust-root validation: NOT PERFORMED`). No trust root is ever baked in. The default `freetsa.org` is a demo provider, not an implicit trust root.
- Output flips the attestation layer to `external witness: PRESENT`, `genTime` + accuracy, while keeping `completeness: NOT GUARANTEED` and `underlying claim truth: NOT ASSESSED`.

Deferred to a later (BSL) module: qualified/eIDAS TSA integration, dual-TSA, re-timestamping, long-term revocation (CRL/OCSP) and archival. The MIT verifier here is the open, neutral evidence tool.

## Goal

Give an outside verifier two things a bare local ledger cannot:
1. Integrity of a ledger state (it has not changed since the snapshot).
2. Proof that the state existed no later than time T, witnessed externally.

That pair is the honest answer to Allen: a locally rewritten ledger can still be made internally consistent. What makes a particular state externally verifiable is that the complete snapshot manifest was witnessed outside the owner's control before the claimed cutoff time.

## Scope split

- **PR2a — Snapshot** (this PR): offline, no network. Canonicalisation, dual digests, manifest, strict reader, offline verifier. Proves only: this supplied ledger matches this supplied snapshot. Does not prove historical existence.
- **PR2b — Attestation**: RFC 3161 timestamp over the snapshot digest, detached receipt, receipt verification. This is what proves existence-by-time.

Released together as one user-facing integrity feature. If PR2a lands first, snapshots are labelled UNATTESTED.

## Licensing boundary

- **MIT (open), always:** the snapshot/receipt format, canonicalisation, basic snapshot generation, and the verifier. The verifier is neutral evidence tooling and must stay permissive so a regulator can verify without trusting or buying anything. This is `irp/integrity/`.
- **BSL (source-available):** advanced generator / managed-anchoring tooling (qualified/eIDAS TSA, dual-TSA, re-timestamping, long-term archival, enterprise key management). Separate module/package, added in PR2b+. The MIT verifier must never import BSL code.
- **Proprietary:** the hosted attestation service and the IRP Compliance product.

## Key design decisions (converged with GPT)

1. **No per-entry hash chain.** A local `previous_hash` chain proves internal consistency, not originality; the owner can recompute it. Rejected, not deferred.
2. **Canonicalisation: RFC 8785 (JCS) via the `rfc8785` library**, not a home-grown implementation. No Unicode normalisation (RFC 8785 forbids it; preserve strings as-is). Duplicate JSON keys are rejected by the strict reader before values reach the canonicaliser.
3. **Two digests, both bound inside the manifest:** `byte_digest` (SHA-256 of raw bytes, catches any change incl. formatting) and `semantic_digest` (SHA-256 of `JCS(ordered entry array)`, survives harmless reformatting).
4. **Anchor the full manifest, not a bare digest.** `snapshot_digest = SHA-256(JCS(manifest_body))` binds every field (both digests, count, head id, salt, ledger_id, schema, tool version). PR2b anchors `snapshot_digest`.
5. **`snapshot_salt`** (256-bit random, inside the manifest) blinds the digest against precomputed guessing/correlation by a TSA or observer. Named distinctly from the RFC 3161 request `nonce` (different purpose: replay/freshness).
6. **`ledger_id`** (random, stable, stored in `.irp/integrity/identity.json`) prevents cross-project replay/confusion. Not a path, repo, or customer name. Does not prove ownership.
7. **No Merkle tree.** Flat ordered array is fully verifiable at IRP scale.
8. **Strict reader prerequisite.** Malformed JSON, duplicate keys, non-object lines, invalid UTF-8 are detected and reported by line, never silently skipped (unlike `store.read_ledger`). A snapshot refuses a malformed ledger by default.
9. **TOCTOU-safe:** the ledger is read once into a single byte buffer; both digests and parsing derive from exactly those bytes. Snapshot files are written atomically (tmp + replace).
10. **Granular, honest verifier output.** Never one `trusted` flag. Reports each property and, for PR2a, states plainly: external witness NOT PRESENT, historical existence NOT PROVEN, completeness NOT GUARANTEED, underlying truth NOT ASSESSED.

## What PR2 still does not prove (write into TRUST.md)

Even a valid external timestamp proves existence-by-time, not:
- **Completeness / freshness.** An owner can present a valid older snapshot and omit later decisions. `previous_snapshot_digest` gives continuity across supplied snapshots but cannot reveal a hidden later tail.
- **Authorship.** `confirmed_by` stays metadata, not authenticated identity (a later PR).
- **Truth.** No cryptography establishes that the recorded decision is factually correct.

## File / directory layout (PR2a)

```
irp/integrity/
  __init__.py
  errors.py       exception types
  canonical.py    JCS wrapper (rfc8785) + sha256 helpers
  strict.py       strict JSONL reader (line-numbered errors, dup-key rejection)
  manifest.py     snapshot manifest builder
  snapshot.py     create_snapshot() — single read, atomic write
  verify.py       verify_snapshot() — offline, granular result

irp/core/commands/integrity.py   CLI handler (run_integrity)

.irp/integrity/
  identity.json              { "ledger_id": "ILID-..." }
  snapshots/IRPS-<date>-NNN.json
  (PR2b) receipts/, trust/
```

Snapshot file shape:
```json
{
  "snapshot_digest": { "alg": "sha-256", "value": "<sha256(JCS(manifest))>" },
  "manifest": {
    "schema": "irp-integrity-snapshot/0.1",
    "snapshot_id": "IRPS-2026-06-30-001",
    "created_at": "2026-06-30T...Z",
    "ledger_id": "ILID-...",
    "scope": { "type": "full-ledger" },
    "previous_snapshot_digest": null,
    "snapshot_salt": "<hex>",
    "ledger": {
      "entry_count": 92,
      "head_entry_id": "IRP-2026-06-29-001",
      "byte_digest": { "alg": "sha-256", "value": "..." },
      "semantic_digest": { "alg": "sha-256", "canon": "RFC8785", "value": "..." }
    },
    "created_by": { "tool": "irp-capture", "version": "0.7.0" }
  }
}
```

## CLI

```
irp integrity snapshot [--allow-malformed]      # offline, never mutates the ledger
irp integrity verify <snapshot> [--ledger PATH] # offline; exit 10 on FAIL
```
(PR2b adds `irp attest create|verify`.)

## Dependencies

Optional extra: `pip install "irp-capture[integrity]"` pulls `rfc8785`. Base `irp capture` stays zero-dependency and Python 3.9. The `rfc8785` import is lazy (only when an integrity command runs), with a clear install message if missing.

## Manifest versioning

`schema` (`irp-integrity-snapshot/0.1`) versions the manifest format independently of the decision-entry schema. Verification rejects unknown schema or downgraded algorithms rather than silently falling back.
