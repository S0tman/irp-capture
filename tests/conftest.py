"""Shared pytest fixtures and path setup for IRP tests.

Every test runs sealed off from the machine it runs on (the autouse `irp_seal`
fixture below):

- a fake HOME, with XDG folders and TMPDIR inside the test's temp folder
- an empty global git config (GIT_CONFIG_GLOBAL), no system git config, and
  GIT_CEILING_DIRECTORIES at the temp root, so git never looks above it
- no IRP_* or IRPG_* settings from the shell, and no git settings that would
  point git at another repo (GIT_DIR, GIT_INDEX_FILE, ...)
- no `irp` command on PATH, so a hook test only runs the code under test
- an empty current folder, so nothing looks for a project from this repo
- no network beyond loopback, for this process and (through proxy settings)
  for the programs tests start. Set IRP_TESTS_ALLOW_NETWORK=1 to run the live
  freetsa tests
- the graph library cache pre-seeded in the fake home, so nothing downloads

Before any test runs, the session checks that no folder above the temp root
holds `.irp` or `.git`. IRP walks up from where it stands, so a stray up there
would change what the tests see; if one is found, every test fails.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make irp/core importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "irp" / "core"))


# ── network: loopback only ────────────────────────────────────────────────────

_ALLOW_NETWORK = os.environ.get("IRP_TESTS_ALLOW_NETWORK") == "1"
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_DEAD_PROXY = "http://127.0.0.1:9"


def _is_loopback(host) -> bool:
    if host is None or host == "":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    name = str(host).split("%", 1)[0].strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


if not _ALLOW_NETWORK:
    _real_getaddrinfo = socket.getaddrinfo
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise socket.gaierror(socket.EAI_NONAME, f"network blocked in tests: {host}")
        return _real_getaddrinfo(host, *args, **kwargs)

    def _blocked(sock, address) -> bool:
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return False
        host = address[0] if isinstance(address, tuple) else address
        return not _is_loopback(host)

    def _guarded_connect(self, address):
        if _blocked(self, address):
            raise OSError(errno.ENETUNREACH, f"network blocked in tests: {address!r}")
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if _blocked(self, address):
            return errno.ENETUNREACH
        return _real_connect_ex(self, address)

    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex

    # Programs the tests start inherit these: urllib and requests send every
    # non-loopback request to a port where nothing listens.
    for _name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ[_name] = _DEAD_PROXY
    for _name in ("no_proxy", "NO_PROXY"):
        os.environ[_name] = "localhost,127.0.0.1,::1"


# ── the seal ──────────────────────────────────────────────────────────────────

# git settings that would point a git command at some other repo or index.
_GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE",
)

# The file name the graph export embeds. The test copy is a stub: no test
# reads the library itself.
GRAPH_LIBRARY = "3d-force-graph-v1.min.js"


def folders_holding_irp_or_git(top: Path) -> list[str]:
    """`top` and every folder above it that holds .irp or .git, nearest first."""
    return [str(d) for d in (top, *top.parents)
            if os.path.lexists(d / ".irp") or os.path.lexists(d / ".git")]


@pytest.fixture(scope="session")
def _seal_top(tmp_path_factory) -> Path:
    top = Path(tmp_path_factory.getbasetemp()).resolve()
    found = folders_holding_irp_or_git(top)
    if found:
        pytest.fail(
            "test seal: a folder at or above the temp root holds .irp or .git, "
            "so IRP or git could find it from inside a test: " + ", ".join(found),
            pytrace=False,
        )
    return top


def _path_without_irp(path_value: str) -> str:
    kept = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "irp"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


@pytest.fixture(autouse=True)
def irp_seal(_seal_top, tmp_path_factory, monkeypatch):
    """Seal one test off from the machine. Returns the folders it set up."""
    base = Path(tmp_path_factory.mktemp("seal")).resolve()
    home = base / "home"
    tmp = base / "tmp"
    cache = home / ".cache"
    for folder in (home, tmp, cache, home / ".config", home / ".local" / "share",
                   home / ".local" / "state"):
        folder.mkdir(parents=True, exist_ok=True)

    gitconfig = base / "gitconfig"
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = main\n"
        "[user]\n\tname = IRP Tests\n\temail = tests@example.invalid\n",
        encoding="utf-8",
    )

    for name in list(os.environ):
        if name.startswith(("IRP_", "IRPG_")) and name != "IRP_TESTS_ALLOW_NETWORK":
            monkeypatch.delenv(name, raising=False)
    for name in _GIT_LOCATION_VARS:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(_seal_top))
    monkeypatch.setenv("IRP_SLACK_STATE_DIR", str(base / "slack_state"))
    monkeypatch.setenv("PATH", _path_without_irp(os.environ.get("PATH", "")))

    # Start each test in an empty folder, so code that looks for a project
    # from the current folder never starts in this repo (or a checkout it's
    # a worktree of).
    cwd = base / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    # The graph library cache, where `irp export graph` keeps it.
    vendor = cache / "irp" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    (vendor / GRAPH_LIBRARY).write_text("/* graph library stub for tests */\n", encoding="utf-8")

    return SimpleNamespace(base=base, home=home, tmp=tmp, gitconfig=gitconfig, top=_seal_top)


# ── project setup for tests ───────────────────────────────────────────────────

def make_project(project_root: Path) -> Path:
    """Start an IRP project in a test folder and return its .irp/ folder."""
    from store import init_project

    return init_project(project_root)
