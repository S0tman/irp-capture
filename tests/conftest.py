"""Shared pytest fixtures and path setup for IRP tests."""
import sys
from pathlib import Path

# Make irp/core importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "irp" / "core"))
