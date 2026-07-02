# IRP Trust Model

This document states precisely what an IRP ledger does and does not prove. Precision is the point. An evidence tool that overclaims is worthless the moment it meets a serious challenge, so IRP claims only what its architecture can back, and marks the rest clearly. Everything below is either verifiable today or labelled as not-yet-built. The boundaries are not a weakness to apologise for; they are what makes the claims inside them trustworthy.

## The one-sentence claim

IRP is an **append-only, local-first, human-confirmed record of decisions**. It preserves decision lineage in a form anyone can read. On its own, a local ledger is **not** independently tamper-proof and does **not** prove the time at which an entry existed.

## What "append-only" actually means

Official IRP commands add new entries and never edit or delete prior ones. Corrections, withdrawals, and changes are made by writing a **superseding** entry, not by rewriting history.

This is an **application-design property**. It describes how IRP behaves through its own commands. It is not a cryptographic guarantee about the underlying file. The ledger is a plain `.jsonl` file held by its owner. Anyone with filesystem access can, outside of IRP, edit a line, delete a line, reorder lines, change a timestamp, or replace the whole file.

## The hardest question, answered plainly

> "If the ledger lives on the user's machine, they can edit it and recompute the hashes, so what makes an entry trustworthy to an outside party, and how do you stop backdating?"

Correct. If we add a hash chain (`previous_hash` on each entry) and all of those hashes live only on the owner's machine, the owner can edit an old entry and recompute every hash forward. The result is internally consistent but is not provably the original ledger.

So:

- **A local hash chain proves internal consistency, not originality.** It catches accidental corruption. It does **not** stop a motivated owner from rewriting history.
- **A local timestamp proves only that the record contains a timestamp claim.** It does **not** independently prove when the record existed. Changing the device clock changes the claim.

The only way to make "this ledger state existed no later than time T" verifiable to an outside party is to anchor the snapshot's digest to an **external witness** (for example an RFC 3161 timestamp authority). After anchoring, the owner can still edit the local file, but they cannot produce an external anchor for the rewritten version dated to the original time. The external witness does the trust work.

**This is shipped.** `irp integrity snapshot` produces a deterministic, canonical snapshot of the ledger; `irp attest create` anchors that snapshot's digest to an external RFC 3161 timestamp authority and stores a detached receipt; `irp integrity verify` and `irp attest verify` re-check both offline. The challenge is answered concretely: a locally rewritten ledger is still internally consistent, but only the original snapshot was witnessed outside the owner's control before the claimed time, and that witness cannot be forged after the fact.

One boundary is deliberate. Verifying the TSA's signature is not the same as validating its certificate path to a trust root you accept, and that policy belongs to the verifier, not to the tool. IRP never bakes one in, and says so in plain sight: the verifier prints `trust-root validation: NOT PERFORMED`. A tool that silently chose the trust root for you would be doing you a disservice. For verifiers who do want to enforce a trust-root policy, a source-available companion, [`irp-attest-pro`](https://github.com/S0tman/irp-attest-pro), performs that validation against anchors you supply. See The open core and the source-available layer, below.

## Seven levels of trust

IRP reports these levels separately and never collapses them into a single `trusted: true`. A verifier is always told which properties passed and which were not assessed. Collapsing them is exactly how weaker evidence tools mislead; keeping them distinct is what makes IRP's output hold up in an audit.

| Level | Claim | Does IRP provide it today? |
|------|-------|----------------------------|
| 1 | The user asserts X happened | Yes (the user's word) |
| 2 | The user's ledger contains a record of X | Yes |
| 3 | The supplied ledger matches a deterministic snapshot digest | Yes (`irp integrity verify`) |
| 4 | An external witness confirms that digest existed by time T | Yes (`irp attest`, RFC 3161) |
| 5 | A recognised identity signed the snapshot | Not yet (optional, later) |
| 6 | The counterparty corroborated X | Out of scope for the ledger |
| 7 | X is actually, factually true | No cryptography proves this |

`confirmed_by` and `source` are **metadata assertions**, not authenticated identity. `"confirmed_by": "johan"` records a workflow fact (a human confirmed this), not a cryptographic proof of who that human was.

## Describing IRP accurately

This is the line between what the architecture proves and what it does not, and it doubles as a test for any owner-held decision tool: the words in the second list are false for a file that lives on the user's disk, and a serious verifier will find the gap. IRP uses the first list. External anchoring earns back the time claims, with the assumptions stated.

**Accurate**
- "Append-only by application design"
- "Local-first, owner-held decision lineage"
- "Inspectable: open the `.jsonl` in any editor"
- "Human-confirmed: no entry exists without a human confirming it"
- "Records what was decided, why, what was rejected, and who confirmed it"

**Overclaiming**
- "Immutable"
- "Tamper-proof" / "tamper-evident"
- "Cannot be modified" / "cannot be altered"
- "The ledger is the truth"
- "Satisfies" a legal requirement (IRP *supports* one; it does not *satisfy* it)
- "Proves" the time, the identity, or the underlying fact (external anchoring proves existence-by-time; nothing local does)

## What IRP can prove to an outside verifier today

For answering a regulator, an auditor, or a public authority, two capabilities are shipped:

- **Deterministic snapshots** (`irp integrity snapshot` / `irp integrity verify`). Canonicalise the ledger (RFC 8785), compute a stable digest, verify offline. Proves: the supplied ledger state has not changed since the snapshot. (Trust level 3.)
- **External timestamp anchoring** (`irp attest create` / `irp attest verify`). Submit only the digest to an RFC 3161 timestamp authority and store a detached receipt. Proves: that digest existed no later than the witnessed time. (Trust level 4.)

So for any snapshot you choose to anchor, IRP reaches **trust level 4: externally witnessed existence**. An un-anchored ledger sits at levels 1 and 2: a readable, append-only, human-confirmed record held by its owner.

## The open core and the source-available layer

Two tiers, one honest boundary, and a commitment to both.

The **open core** (`irp-capture`, MIT, open forever) proves what it can and reports what it does not, including `trust-root validation: NOT PERFORMED`. It will never pick a trust root for you. Everything a verifier needs to reach level 4 lives here, at no cost and with nothing to buy.

The **source-available layer** ([`irp-attest-pro`](https://github.com/S0tman/irp-attest-pro), BSL 1.1) picks up exactly at that boundary, for verifiers who want to go further:

- **Shipped (0.1.0): certificate-chain and trust-root validation.** Validates the timestamp's certificate chain to anchors *you* supply, confirms every certificate was valid at the time it was issued, and requires the RFC 3161 timeStamping key usage. It turns the `NOT PERFORMED` line into a reasoned `TRUSTED` or `UNTRUSTED`, never a bare boolean. This is the difference between "witnessed" and "witnessed by an authority a regulator recognises."
- **On the way:** qualified / eIDAS timestamp authorities, dual-TSA redundancy, automatic re-timestamping before certificate expiry, and revocation checking (CRL / OCSP) with long-term archival.

You can read and self-host the source-available layer under its licence; it depends on the open core and never the other way around. The discipline is identical at both tiers: the tool tells you exactly what it validated and what it did not, and leaves the trust decision with you.

## Roadmap (not yet built)

Everything below is deferred until a concrete verifier requires it. None of it is needed to reach level 4.

1. **Authenticated authorship (level 5).** Sign a snapshot with a recognised identity, so a verifier learns *who* produced it, not just *when* it existed. Until this lands, `confirmed_by` stays a metadata assertion, not proof of identity.
2. **Publication provenance.** Bind a published artifact (a PDF report, an evidence bundle) to a snapshot digest, C2PA-style, so an exported document is verifiably tied to the exact ledger state it came from.
3. **Snapshot continuity.** Chain successive snapshots via `previous_snapshot_digest` to detect gaps across the snapshots a verifier is shown. (This still cannot reveal an undisclosed later tail; see the completeness limit below.)

(The advanced attestation features once listed here, qualified / eIDAS TSAs, dual-TSA, re-timestamping, archival, and revocation, now live in the source-available layer above, where the first of them has shipped.)

## What even a valid timestamp does not prove

External anchoring proves existence-by-time. It does not prove:

- **Completeness or freshness.** An owner can present a valid, externally timestamped *older* snapshot and silently omit later decisions. A timestamp proves the snapshotted state existed by time T, not that it is the latest or complete state. Linking successive snapshots (`previous_snapshot_digest`) gives continuity across the snapshots you are shown, but it cannot reveal a hidden later tail that was never disclosed.
- **Authorship.** `confirmed_by` remains a metadata assertion, not an authenticated identity, until signatures are added.
- **Truth.** No hash, timestamp, or signature establishes that the recorded decision is factually correct.

A snapshot, even a witnessed one, answers one question precisely: did this exact state exist by this time. It does not answer whether the record is complete or whether it is true, and IRP does not pretend otherwise. Stating that boundary up front is what lets a verifier rely on everything inside it.
