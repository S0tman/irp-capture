"""`irp integrity` — deterministic snapshots and offline verification (PR2a).

Open (MIT) tooling: the format, snapshot generation, and the verifier. The
verifier is neutral evidence tooling and stays permissively licensed.
"""
from __future__ import annotations

from pathlib import Path


def run_integrity(project_root: Path, irp_dir: Path, args) -> dict:
    action = getattr(args, "integrity_action", None)
    if action == "snapshot":
        return _do_snapshot(irp_dir, args)
    if action == "verify":
        return _do_verify(irp_dir, args)
    return {"command": "integrity", "status": "error", "text": "Unknown integrity action."}


def _do_snapshot(irp_dir: Path, args) -> dict:
    # Imported lazily so the dispatcher (and base `irp capture`) never pull the
    # optional rfc8785 dependency unless an integrity command actually runs.
    from irp.integrity.errors import IntegrityError
    from irp.integrity.snapshot import create_snapshot

    try:
        res = create_snapshot(irp_dir, allow_malformed=getattr(args, "allow_malformed", False))
    except IntegrityError as exc:
        return {
            "command": "integrity",
            "status": "error",
            "verdict": "block",
            "text": f"IRP integrity snapshot failed: {exc}",
        }

    digest = res["file"]["snapshot_digest"]["value"]
    lines = [
        "IRP integrity snapshot",
        f"Snapshot:        {res['snapshot_id']}",
        f"Entries:         {res['entry_count']}",
        f"Snapshot digest: {digest}",
        f"Written:         {res['path']}",
        "",
        "UNATTESTED. This proves internal consistency of the supplied ledger,",
        "not that this was the ledger state at any past time. Anchor it with",
        "`irp attest create` (PR2b) for an external witness.",
    ]
    if res["duplicate_ids"]:
        unique = ", ".join(sorted(set(res["duplicate_ids"])))
        lines.append(f"Note: duplicate entry ids present: {unique}")
    if res["malformed"]:
        lines.append(f"Note: snapshotted over {len(res['malformed'])} malformed line(s).")

    return {
        "command": "integrity",
        "status": "ok",
        "snapshot_id": res["snapshot_id"],
        "snapshot": res["file"],
        "text": "\n".join(lines),
    }


def _do_verify(irp_dir: Path, args) -> dict:
    from irp.integrity.errors import IntegrityError
    from irp.integrity.verify import verify_snapshot

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.exists():
        return {
            "command": "integrity",
            "status": "error",
            "verdict": "block",
            "text": f"Snapshot file not found: {snapshot_path}",
        }

    ledger_path = Path(args.ledger) if getattr(args, "ledger", None) else irp_dir / "ledger.jsonl"

    try:
        res = verify_snapshot(snapshot_path, ledger_path)
    except IntegrityError as exc:
        return {
            "command": "integrity",
            "status": "error",
            "verdict": "block",
            "text": f"IRP integrity verify failed: {exc}",
        }

    def mark(passed: bool) -> str:
        return "PASS" if passed else "FAIL"

    lines = [
        "IRP integrity verify",
        f"Snapshot:  {res['snapshot_id']}",
        f"Ledger id: {res['ledger_id']}",
        "",
    ]
    for c in res["checks"]:
        suffix = f"  ({c['detail']})" if c.get("detail") else ""
        lines.append(f"  {c['check']:<26} {mark(c['pass'])}{suffix}")

    att = res["attestation"]
    lines += [
        "",
        f"  {'external witness':<26} {att['external_witness']}",
        f"  {'historical existence':<26} {att['historical_existence']}",
        f"  {'backdating resistance':<26} {att['backdating_resistance']}",
        f"  {'completeness':<26} {att['completeness']}",
        f"  {'underlying claim truth':<26} {att['underlying_claim_truth']}",
        "",
        (
            "RESULT: PASS — the supplied ledger matches this snapshot."
            if res["ok"]
            else "RESULT: FAIL — the supplied ledger does NOT match this snapshot."
        ),
    ]

    out = {
        "command": "integrity",
        "status": "ok" if res["ok"] else "fail",
        "result": res,
        "text": "\n".join(lines),
    }
    if not res["ok"]:
        out["verdict"] = "block"  # non-zero exit for CI consumers
    return out
