"""The test seal in conftest.py is in place for every test.

These check the seal itself: if one of its parts went missing, tests could
read or write the machine they run on without anything failing.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import conftest
from project_kit import REPO


def test_every_setting_is_in_place(irp_seal):
    assert Path.home() == irp_seal.home
    assert os.environ["GIT_CONFIG_GLOBAL"] == str(irp_seal.gitconfig)
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ["GIT_CEILING_DIRECTORIES"] == str(irp_seal.top)
    left_over = [name for name in os.environ if name.startswith(("IRP_", "IRPG_"))
                 and name not in ("IRP_TESTS_ALLOW_NETWORK", "IRP_SLACK_STATE_DIR")]
    assert left_over == []
    assert "GIT_DIR" not in os.environ and "GIT_INDEX_FILE" not in os.environ
    assert shutil.which("irp") is None
    assert Path.cwd() == irp_seal.base / "cwd"
    assert list(Path.cwd().iterdir()) == []
    assert tempfile.gettempdir() == str(irp_seal.tmp)
    assert (irp_seal.home / ".cache" / "irp" / "vendor" / conftest.GRAPH_LIBRARY).exists()


@pytest.mark.skipif(os.environ.get("IRP_TESTS_ALLOW_NETWORK") == "1", reason="network allowed for this run")
def test_the_network_is_blocked():
    with pytest.raises(OSError, match="network blocked"):
        socket.getaddrinfo("example.invalid", 80)


def test_the_scan_names_a_folder_above_that_holds_dot_irp_or_dot_git(tmp_path):
    top = tmp_path / "a" / "b"
    top.mkdir(parents=True)
    assert conftest.folders_holding_irp_or_git(top) == []
    (tmp_path / "a" / ".irp").mkdir()
    (top / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert conftest.folders_holding_irp_or_git(top) == [str(top), str(tmp_path / "a")]


def test_a_stray_above_the_temp_root_fails_every_test(tmp_path):
    planted = tmp_path / "planted"
    (planted / ".irp").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--basetemp={planted / 'bt'}",
         "tests/test_project_root.py::TestClassify::test_the_indented_default_is_the_34_byte_form"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode != 0, result.stdout
    assert "test seal" in result.stdout + result.stderr
