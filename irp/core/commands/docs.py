"""irp docs — pull/push iCloud strategic docs to/from /tmp staging area."""
from __future__ import annotations

import shutil
from pathlib import Path

ICLOUD_DIR = Path("/Users/jolopes/Library/Mobile Documents/com~apple~CloudDocs/irp docs")
STAGING_DIR = Path("/tmp")

KNOWN_DOCS = [
    "SPEC.md",
    "IRP-Roadmap.md",
    "IRP-Competitive-Analysis.md",
]


def run_docs(project_root: Path, irp_dir: Path, args) -> dict:
    action = args.docs_action
    if action == "pull":
        return _pull(args)
    if action == "push":
        return _push(args)
    if action == "list":
        return _list(args)
    return {"status": "error", "text": f"Unknown docs action: {action}"}


def _resolve_files(args) -> list[str]:
    f = getattr(args, "file", None)
    return [f] if f else KNOWN_DOCS


def _pull(args) -> dict:
    files = _resolve_files(args)
    results, errors = [], []
    for name in files:
        src = ICLOUD_DIR / name
        dst = STAGING_DIR / name
        if not src.exists():
            errors.append(f"{name}: not found in iCloud")
            continue
        shutil.copy2(src, dst)
        results.append(f"{name}: iCloud → /tmp")
    lines = results + [f"WARN: {e}" for e in errors]
    return {
        "status": "ok" if results else "error",
        "pulled": results,
        "errors": errors,
        "text": "\n".join(lines) if lines else "Nothing to pull.",
    }


def _push(args) -> dict:
    files = _resolve_files(args)
    results, errors = [], []
    if not ICLOUD_DIR.exists():
        return {"status": "error", "text": f"iCloud dir not found: {ICLOUD_DIR}"}
    for name in files:
        src = STAGING_DIR / name
        dst = ICLOUD_DIR / name
        if not src.exists():
            errors.append(f"{name}: not found in /tmp")
            continue
        shutil.copy2(src, dst)
        results.append(f"{name}: /tmp → iCloud")
    lines = results + [f"WARN: {e}" for e in errors]
    return {
        "status": "ok" if results else "error",
        "pushed": results,
        "errors": errors,
        "text": "\n".join(lines) if lines else "Nothing to push.",
    }


def _list(args) -> dict:
    if not ICLOUD_DIR.exists():
        return {"status": "error", "text": f"iCloud dir not found: {ICLOUD_DIR}"}
    files = sorted(f.name for f in ICLOUD_DIR.glob("*.md"))
    return {
        "status": "ok",
        "files": files,
        "text": "\n".join(files) if files else "(no .md files found)",
    }
