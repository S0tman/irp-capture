"""Tracked files must not carry a real macOS home path.

Examples use placeholders (/Users/you, /Users/yourname, /Users/someone,
/path/to/...). Anything else under /Users/ is someone's real username leaking
into a public repo and the PyPI package.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_PLACEHOLDERS = ("you", "yourname", "someone")
_HOME_PATH = re.compile(
    r"/Users/(?!(?:%s)(?![\w.-]))[\w.-]+" % "|".join(_PLACEHOLDERS)
)


def _tracked_files() -> list[Path]:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [REPO / p for p in out.decode("utf-8").split("\0") if p]


def test_no_real_home_paths_in_tracked_files():
    hits = []
    for path in _tracked_files():
        if path == Path(__file__).resolve():
            continue  # the pattern tests below use made-up names on purpose
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        for lineno, line in enumerate(text.splitlines(), 1):
            if _HOME_PATH.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not hits, "real home paths found (use a placeholder):\n" + "\n".join(hits)


@pytest.mark.parametrize("line, leaks", [
    ("IRP_PROJECT_ROOT=/Users/alice/proj", True),
    ("export IRP_OBSIDIAN_VAULT=\"/Users/you/Notes\"", False),
    ("[bridge] Project root: /Users/yourname/.claude", False),
    ("seed(\"/Users/someone/private/notes.md\")", False),
    ("cd /Users/younger/x", True),
    ("assert \"/Users/\" not in source", False),
])
def test_pattern(line, leaks):
    assert bool(_HOME_PATH.search(line)) is leaks
