"""Every module under irp/ compiles without a warning.

An invalid escape sequence such as "\\`" in a normal string is a SyntaxWarning
on Python 3.12+ (a DeprecationWarning before that), printed on every command
run. Treating both as errors here catches a new one on any supported Python.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SOURCES = sorted((REPO / "irp").rglob("*.py"))


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(REPO)))
def test_compiles_without_warnings(path):
    source = path.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        warnings.simplefilter("error", DeprecationWarning)
        compile(source, str(path), "exec")
