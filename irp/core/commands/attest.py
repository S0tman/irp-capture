"""`irp attest` — external timestamp anchoring and verification (PR2b).

`create` is the basic generator (a single RFC 3161 call to a user-supplied TSA),
`verify` is the MIT verifier. Advanced/managed anchoring (qualified/eIDAS TSA,
dual-TSA, re-timestamping, archival) is the BSL layer, added separately.
"""
from __future__ import annotations

from pathlib import Path


def run_attest(project_root: Path, irp_dir: Path, args) -> dict:
    action = getattr(args, "attest_action", None)
    if action == "create":
        return _do_create(irp_dir, args)
    if action == "verify":
        return _do_verify(irp_dir, args)
    return {"command": "attest", "status": "error", "text": "Unknown attest action."}


def _do_create(irp_dir: Path, args) -> dict:
    from irp.integrity.attest import DEFAULT_TSA, create_attestation
    from irp.integrity.errors import IntegrityError

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.exists():
        return {"command": "attest", "status": "error", "verdict": "block",
                "text": f"Snapshot file not found: {snapshot_path}"}

    tsa_url = getattr(args, "tsa_url", None) or DEFAULT_TSA
    try:
        res = create_attestation(irp_dir, snapshot_path, tsa_url=tsa_url)
    except (IntegrityError, OSError, RuntimeError) as exc:
        return {"command": "attest", "status": "error", "verdict": "block",
                "text": f"IRP attest create failed: {exc}"}

    acc = res["accuracy_seconds"]
    lines = [
        "IRP attest create",
        f"Snapshot:  {res['snapshot_id']}",
        f"TSA:       {res['tsa_url']}",
        f"genTime:   {res['gen_time']}" + (f"  (accuracy +/- {acc}s)" if acc else "  (accuracy unspecified)"),
        f"Receipt:   {res['receipt_path']}",
        f"Token:     {res['token_path']}",
        "",
        "The snapshot digest is now externally witnessed: this exact state existed",
        "no later than genTime. This still does not prove completeness or truth, and",
        "TSA trust-root validation is your policy. Verify with `irp attest verify`.",
    ]
    return {"command": "attest", "status": "ok", "snapshot_id": res["snapshot_id"], "text": "\n".join(lines)}


def _do_verify(irp_dir: Path, args) -> dict:
    from irp.integrity.attest import find_receipt_token, verify_attestation
    from irp.integrity.errors import IntegrityError

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.exists():
        return {"command": "attest", "status": "error", "verdict": "block",
                "text": f"Snapshot file not found: {snapshot_path}"}

    if getattr(args, "receipt", None):
        token_path = Path(args.receipt)
    else:
        # Auto-locate by snapshot id.
        import json
        try:
            sid = json.loads(snapshot_path.read_text(encoding="utf-8"))["manifest"]["snapshot_id"]
        except (ValueError, OSError, KeyError):
            sid = snapshot_path.stem
        found = find_receipt_token(irp_dir, sid)
        if not found:
            return {"command": "attest", "status": "error", "verdict": "block",
                    "text": f"No receipt found for {sid}. Pass --receipt <path> or run `irp attest create` first."}
        token_path = found

    if not token_path.exists():
        return {"command": "attest", "status": "error", "verdict": "block",
                "text": f"Receipt token not found: {token_path}"}

    try:
        res = verify_attestation(snapshot_path, token_path)
    except (IntegrityError, OSError) as exc:
        return {"command": "attest", "status": "error", "verdict": "block",
                "text": f"IRP attest verify failed: {exc}"}

    t = res["token"]

    def mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    acc = t["accuracy"]
    lines = [
        "IRP attest verify",
        f"Snapshot:  {res['snapshot_id']}",
        "",
        f"  manifest binds digest      {mark(res['manifest_binds_digest'])}",
        f"  token binds same digest    {mark(t['imprint_ok'])}",
        f"  token content consistent   {mark(t['message_digest_ok'])}",
        f"  TSA signature valid        {mark(t['signature_ok'])}",
        "",
        f"  external witness           {'PRESENT' if res['externally_witnessed'] else 'NOT ESTABLISHED'}",
        f"  witnessed by               {t['signer_subject'] or '(unknown)'}",
        f"  genTime                    {t['gen_time']}" + (f"  (accuracy +/- {acc}s)" if acc else "  (accuracy unspecified)"),
        f"  TSA policy                 {t['policy']}",
        f"  trust-root validation      NOT PERFORMED (verifier policy; no roots configured)",
        f"  completeness               NOT GUARANTEED",
        f"  underlying claim truth     NOT ASSESSED",
        "",
        (
            "RESULT: WITNESSED — this snapshot existed no later than the TSA's genTime,"
            "\n        subject to trusting the TSA. Not a proof of completeness or truth."
            if res["externally_witnessed"]
            else "RESULT: NOT ESTABLISHED — the timestamp does not validly bind this snapshot."
        ),
    ]
    out = {"command": "attest", "status": "ok" if res["externally_witnessed"] else "fail",
           "result": res, "text": "\n".join(lines)}
    if not res["externally_witnessed"]:
        out["verdict"] = "block"
    return out
