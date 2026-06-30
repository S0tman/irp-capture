# IRP Trust Model

This document states precisely what an IRP ledger does and does not prove. IRP is a compliance and decision-lineage tool, so it holds itself to the same standard it asks of others: never claim more than the architecture can support.

## The one-sentence claim

IRP is an **append-only, local-first, human-confirmed record of decisions**. It preserves decision lineage in a form anyone can read. On its own, a local ledger is **not** independently tamper-proof and does **not** prove the time at which an entry existed.

## What "append-only" actually means

Official IRP commands add new entries and never edit or delete prior ones. Corrections, withdrawals, and changes are made by writing a **superseding** entry, not by rewriting history.

This is an **application-design property**. It describes how IRP behaves through its own commands. It is not a cryptographic guarantee about the underlying file. The ledger is a plain `.jsonl` file held by its owner. Anyone with filesystem access can, outside of IRP, edit a line, delete a line, reorder lines, change a timestamp, or replace the whole file.

## Allen's question, answered plainly

> "If the ledger lives on the user's machine, they can edit it and recompute the hashes, so what makes an entry trustworthy to an outside party, and how do you stop backdating?"

Correct. If we add a hash chain (`previous_hash` on each entry) and all of those hashes live only on the owner's machine, the owner can edit an old entry and recompute every hash forward. The result is internally consistent but is not provably the original ledger.

So:

- **A local hash chain proves internal consistency, not originality.** It catches accidental corruption. It does **not** stop a motivated owner from rewriting history.
- **A local timestamp proves only that the record contains a timestamp claim.** It does **not** independently prove when the record existed. Changing the device clock changes the claim.

The only way to make "this ledger state existed no later than time T" verifiable to an outside party is to anchor the snapshot's digest to an **external witness** (for example an RFC 3161 timestamp authority). After anchoring, the owner can still edit the local file, but they cannot produce an external anchor for the rewritten version dated to the original time. The external witness does the trust work. (This external anchoring is planned, not yet shipped. See "Roadmap" below.)

## Seven levels of trust (do not collapse them)

IRP must never reduce these to a single `trusted: true`. A verifier should be told which properties passed and which were not assessed.

| Level | Claim | Does IRP provide it today? |
|------|-------|----------------------------|
| 1 | The user asserts X happened | Yes (the user's word) |
| 2 | The user's ledger contains a record of X | Yes |
| 3 | The supplied ledger matches a deterministic snapshot digest | Planned (PR2) |
| 4 | An external witness confirms that digest existed by time T | Planned (PR2) |
| 5 | A recognised identity signed the snapshot | Not yet (optional, later) |
| 6 | The counterparty corroborated X | Out of scope for the ledger |
| 7 | X is actually, factually true | No cryptography proves this |

`confirmed_by` and `source` are **metadata assertions**, not authenticated identity. `"confirmed_by": "johan"` records a workflow fact (a human confirmed this), not a cryptographic proof of who that human was.

## What you may and may not say

**Safe to say**
- "Append-only by application design"
- "Local-first, owner-held decision lineage"
- "Inspectable: open the `.jsonl` in any editor"
- "Human-confirmed: no entry exists without a human confirming it"
- "Records what was decided, why, what was rejected, and who confirmed it"

**Do not say** (without external anchoring and explicit assumptions)
- "Immutable"
- "Tamper-proof" / "tamper-evident"
- "Cannot be modified" / "cannot be altered"
- "The ledger is the truth"
- "Satisfies" a legal requirement (use "supports")
- "Proves" the time or the identity or the underlying fact

## Roadmap toward external verifiability

The honest path to answering an outside verifier (a regulator, an auditor, a public authority):

1. **Deterministic snapshots.** Canonicalise the ledger (RFC 8785), compute a stable digest, verify offline. Proves: this supplied state has not changed since the snapshot. (Shipped: `irp integrity snapshot` / `irp integrity verify`.)
2. **External timestamp anchoring.** Submit only the digest to an RFC 3161 timestamp authority, store the detached receipt. Proves: this digest existed no later than the witnessed time. (Planned: `irp attest`.)
3. **Optional signatures and publication provenance.** Deferred until a concrete verifier requires them.

Until those ship, IRP's claim stays at levels 1 and 2: a readable, append-only, human-confirmed record of decisions, held by its owner.

## What even a valid timestamp does not prove

External anchoring proves existence-by-time. It does not prove:

- **Completeness or freshness.** An owner can present a valid, externally timestamped *older* snapshot and silently omit later decisions. A timestamp proves the snapshotted state existed by time T, not that it is the latest or complete state. Linking successive snapshots (`previous_snapshot_digest`) gives continuity across the snapshots you are shown, but it cannot reveal a hidden later tail that was never disclosed.
- **Authorship.** `confirmed_by` remains a metadata assertion, not an authenticated identity, until signatures are added.
- **Truth.** No hash, timestamp, or signature establishes that the recorded decision is factually correct.

Be explicit about this with any verifier: a snapshot, even a witnessed one, answers "did this exact state exist by this time," not "is this the whole story, and is it true."
