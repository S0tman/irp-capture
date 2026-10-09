"""irp docs — pull/push your docs folder to/from a /tmp staging area.

The docs folder comes from the IRP_DOCS_DIR environment variable. It's a
per-machine path, so there is no built-in default.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

DOCS_DIR_ENV = "IRP_DOCS_DIR"
STAGING_DIR = Path("/tmp")

KNOWN_DOCS = [
    "SPEC.md",
    "IRP-Roadmap.md",
    "IRP-Competitive-Analysis.md",
]

_UNSET_TEXT = (
    f"{DOCS_DIR_ENV} is not set, so irp docs doesn't know where your docs folder is.\n"
    "Add this line to your shell profile (e.g. ~/.zshrc), then open a new shell:\n"
    f'  export {DOCS_DIR_ENV}="/path/to/your/docs"'
)


def run_docs(project_root: Path, irp_dir: Path, args) -> dict:
    action = args.docs_action
    handlers = {"pull": _pull, "push": _push, "list": _list}
    if action not in handlers:
        return {"status": "error", "text": f"Unknown docs action: {action}"}
    docs_dir = _docs_dir()
    if docs_dir is None:
        return {"status": "error", "text": _UNSET_TEXT}
    if not docs_dir.is_dir():
        return {
            "status": "error",
            "text": f"Docs folder not found: {docs_dir} (from {DOCS_DIR_ENV})",
        }
    return handlers[action](docs_dir, args)


def _docs_dir() -> Path | None:
    raw = os.environ.get(DOCS_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _resolve_files(args) -> list[str]:
    f = getattr(args, "file", None)
    return [f] if f else KNOWN_DOCS


def _pull(docs_dir: Path, args) -> dict:
    files = _resolve_files(args)
    results, errors = [], []
    for name in files:
        src = docs_dir / name
        dst = STAGING_DIR / name
        if not src.exists():
            errors.append(f"{name}: not found in docs folder")
            continue
        shutil.copy2(src, dst)
        results.append(f"{name}: docs folder → {STAGING_DIR}")
    lines = results + [f"WARN: {e}" for e in errors]
    return {
        "status": "ok" if results else "error",
        "pulled": results,
        "errors": errors,
        "text": "\n".join(lines) if lines else "Nothing to pull.",
    }


def _push(docs_dir: Path, args) -> dict:
    files = _resolve_files(args)
    results, errors = [], []
    for name in files:
        src = STAGING_DIR / name
        dst = docs_dir / name
        if not src.exists():
            errors.append(f"{name}: not found in {STAGING_DIR}")
            continue
        shutil.copy2(src, dst)
        results.append(f"{name}: {STAGING_DIR} → docs folder")
    lines = results + [f"WARN: {e}" for e in errors]
    return {
        "status": "ok" if results else "error",
        "pushed": results,
        "errors": errors,
        "text": "\n".join(lines) if lines else "Nothing to push.",
    }


def _list(docs_dir: Path, args) -> dict:
    files = sorted(f.name for f in docs_dir.glob("*.md"))
    return {
        "status": "ok",
        "files": files,
        "text": "\n".join(files) if files else "(no .md files found)",
    }
