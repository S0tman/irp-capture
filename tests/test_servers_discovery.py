"""Servers and sensors follow the same rules as the CLI.

The MCP server, the REST API, the Figma bridge, the Discord sensor's config
and Slack capture never create `.irp/` (or any other folder) on their own.

`mcp`, `fastapi`, `uvicorn`, `pydantic` and `dotenv` aren't in the dev
extras, so these tests run each server in a separate Python with small
stand-ins for those packages. The stand-ins only provide what the modules
touch at import time; every IRP code path is the real one. A separate
process also keeps the stand-ins out of the rest of the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

import pytest

from project_kit import (
    REPO,
    irp_folders,
    start_project,
    state_diff,
    tree_state,
)

BRIDGE = REPO / "irp" / "figma_plugin" / "bridge" / "server.py"

_STUBS = {
    "mcp/__init__.py": "",
    "mcp/server/__init__.py": "",
    "mcp/server/fastmcp.py": (
        "class FastMCP:\n"
        "    def __init__(self, name, instructions=None, **kwargs):\n"
        "        self.name = name\n"
        "    def tool(self, *args, **kwargs):\n"
        "        return lambda fn: fn\n"
        "    def run(self, *args, **kwargs):\n"
        "        pass\n"
    ),
    "fastapi/__init__.py": (
        "class HTTPException(Exception):\n"
        "    def __init__(self, status_code=500, detail=None):\n"
        "        super().__init__(detail)\n"
        "        self.status_code, self.detail = status_code, detail\n"
        "class FastAPI:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.title = kwargs.get('title')\n"
        "    def add_middleware(self, *args, **kwargs):\n"
        "        pass\n"
        "    def _route(self, *args, **kwargs):\n"
        "        return lambda fn: fn\n"
        "    get = post = put = delete = _route\n"
    ),
    "fastapi/middleware/__init__.py": "",
    "fastapi/middleware/cors.py": "class CORSMiddleware:\n    pass\n",
    "pydantic.py": (
        "class BaseModel:\n"
        "    def __init__(self, **kwargs):\n"
        "        for k, v in kwargs.items():\n"
        "            setattr(self, k, v)\n"
    ),
    "uvicorn.py": (
        "import os\n"
        "def run(app, **kwargs):\n"
        "    with open(os.environ['TEST_SERVER_MARK'], 'w') as f:\n"
        "        f.write('started')\n"
    ),
    "dotenv.py": "def load_dotenv(*args, **kwargs):\n    return False\n",
}


@pytest.fixture
def stubs(tmp_path):
    folder = tmp_path / "stubs"
    for rel, text in _STUBS.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


def _python(code: str, cwd: Path, stubs: Path, *args: str, env_extra=None, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = os.pathsep.join([str(stubs), str(REPO)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", code, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def _run_module(module: str, cwd: Path, stubs: Path, *args: str, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(stubs), str(REPO)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", module, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def _lines(result) -> list[str]:
    return [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]


# ── MCP ───────────────────────────────────────────────────────────────────────

_MCP_PROBE = r"""
import json
import irp.mcp.server as server

results = {}
def call(name, fn):
    try:
        results[name] = {"raised": None, "result": fn()}
    except BaseException as e:
        results[name] = {"raised": type(e).__name__, "message": str(e)}

call("why", lambda: server.irp_why())
call("inherit", lambda: server.irp_inherit())
call("check", lambda: server.irp_check("move the reporting service to redis"))
call("capture", lambda: server.irp_capture("Keep notes in plain text", "Easy to diff"))
print(json.dumps(results, default=str))
"""


def _mcp(cwd, stubs, **kwargs):
    result = _python(_MCP_PROBE, cwd, stubs, **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _error_text(entry: dict) -> str:
    return entry["message"] if entry["raised"] else json.dumps(entry["result"])


class TestMcp:
    def test_tools_with_no_project_create_nothing(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        before = tree_state(empty)
        _mcp(empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert state_diff(before, tree_state(empty)) == []

    def test_read_tools_with_no_project_return_a_plain_error(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        results = _mcp(empty, stubs, drop=("IRP_PROJECT_ROOT",))
        for name in ("why", "inherit", "check"):
            assert "No IRP project found" in _error_text(results[name]), name

    def test_capture_with_no_project_refuses_with_the_init_line(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        results = _mcp(empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert "irp init" in _error_text(results["capture"])
        assert "Nothing was written" in _error_text(results["capture"])
        assert irp_folders(empty) == []

    def test_walks_up_from_the_servers_folder(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        (proj / "src").mkdir()
        results = _mcp(proj / "src", stubs, drop=("IRP_PROJECT_ROOT",))
        assert results["check"]["raised"] is None
        assert results["check"]["result"]["status"] == "conflict"
        assert irp_folders(proj) == [str(proj / ".irp")]

    def test_irp_project_root_names_the_project(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        results = _mcp(elsewhere, stubs, env_extra={"IRP_PROJECT_ROOT": str(proj)})
        assert results["check"]["result"]["status"] == "conflict"
        assert irp_folders(elsewhere) == []


# ── REST API ──────────────────────────────────────────────────────────────────

_API_PROBE = r"""
import json, sys
sys.argv = ["irp-api", "--project-root", sys.argv[1]]
out = {}
try:
    import irp.api.server as server
except SystemExit as e:
    out["import_exit"] = e.code
else:
    for name in ("health", "get_decisions"):
        try:
            out[name] = getattr(server, name)()
        except BaseException as e:
            out[name] = {"raised": type(e).__name__, "message": str(e)}
print(json.dumps(out, default=str))
"""


_HEALTH_PROBE = r"""
import json, sys
sys.argv = ["uvicorn"]
import irp.api.server as server
from fastapi import HTTPException
try:
    out = {"status_code": 200, "body": server.health()}
except HTTPException as e:
    out = {"status_code": e.status_code, "detail": e.detail}
print(json.dumps(out, default=str))
"""


class TestApi:
    def test_against_an_empty_folder_creates_nothing(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        before = tree_state(empty)
        _python(_API_PROBE, root, stubs, str(empty))
        assert state_diff(before, tree_state(empty)) == []

    def test_refuses_to_start_without_a_project(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        mark = root / "started"
        result = _run_module("irp.api.server", root, stubs, "--project-root", str(empty),
                             env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert not mark.exists()
        lines = _lines(result)
        assert len(lines) == 1 and "no irp project" in lines[0].lower(), lines
        assert irp_folders(empty) == []

    def test_project_root_is_strict(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        (proj / "src").mkdir()
        mark = root / "started"
        result = _run_module("irp.api.server", root, stubs, "--project-root", str(proj / "src"),
                             env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert not mark.exists()
        assert irp_folders(proj) == [str(proj / ".irp")]

    def test_health_is_unavailable_with_no_project(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        # As uvicorn imports it: no --project-root, found from the current folder.
        result = _python(_HEALTH_PROBE, empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert data["status_code"] == 503
        assert "No IRP project found" in data["detail"]
        assert list(empty.iterdir()) == []

    def test_with_a_project_it_starts_and_health_creates_nothing(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        mark = root / "started"
        result = _run_module("irp.api.server", root, stubs, "--project-root", str(proj),
                             env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 0, result.stderr
        assert mark.exists()
        before = tree_state(proj)
        probe = _python(_API_PROBE, root, stubs, str(proj))
        data = json.loads(probe.stdout.strip().splitlines()[-1])
        assert data["health"]["project_root"] == str(proj)
        assert state_diff(before, tree_state(proj)) == []


# ── Figma bridge ──────────────────────────────────────────────────────────────

_BRIDGE_START = r"""
import http.server, os, runpy, sys

class FakeServer:
    def __init__(self, *args, **kwargs):
        with open(os.environ["TEST_SERVER_MARK"], "w") as f:
            f.write("started")
    def serve_forever(self):
        pass

http.server.HTTPServer = FakeServer
path, project = sys.argv[1], sys.argv[2]
sys.argv = [path, "--project-root", project]
runpy.run_path(path, run_name="__main__")
"""


class TestFigmaBridge:
    def test_refuses_to_start_without_a_project(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        mark = root / "started"
        result = _python(_BRIDGE_START, root, stubs, str(BRIDGE), str(empty),
                         env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert not mark.exists()
        lines = _lines(result)
        assert len(lines) == 1 and "no irp project" in lines[0].lower(), lines
        assert irp_folders(empty) == []

    def test_project_root_is_strict(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        (proj / "src").mkdir()
        mark = root / "started"
        result = _python(_BRIDGE_START, root, stubs, str(BRIDGE), str(proj / "src"),
                         env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert not mark.exists()
        assert irp_folders(proj) == [str(proj / ".irp")]

    def test_starts_with_a_project(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        mark = root / "started"
        result = _python(_BRIDGE_START, root, stubs, str(BRIDGE), str(proj),
                         env_extra={"TEST_SERVER_MARK": str(mark)})
        assert result.returncode == 0, result.stderr
        assert mark.exists()

    def test_a_capture_against_an_empty_folder_creates_nothing(self, root, monkeypatch):
        import irp.figma_plugin.bridge.server as bridge

        empty = root / "empty"
        empty.mkdir()
        monkeypatch.setattr(bridge, "PROJECT_ROOT", str(empty))
        monkeypatch.setattr(bridge, "FIGMA_PAT", "")
        server = HTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("POST", "/capture", body=json.dumps({"decision": "Add comments"}).encode(),
                         headers={"Content-Type": "application/json"})
            status = conn.getresponse().status
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert status != 200
        assert list(empty.iterdir()) == []


# ── import-time side effects (Discord, Slack) ────────────────────────────────

_IMPORT_PROBE = r"""
import importlib, json, sys
attempts = []
def hook(event, args):
    if event == "os.mkdir":
        attempts.append(str(args[0]))
        raise PermissionError("folder creation blocked by the test")
sys.addaudithook(hook)
values = {}
try:
    module = importlib.import_module(sys.argv[1])
    error = ""
    values = {k: str(v) for k, v in vars(module).items() if k.isupper() and not k.startswith("_")}
except BaseException as e:
    error = f"{type(e).__name__}: {e}"
print(json.dumps({"error": error, "mkdir": attempts, "values": values}))
"""


def _import(module: str, cwd: Path, stubs: Path, **kwargs) -> dict:
    result = _python(_IMPORT_PROBE, cwd, stubs, module, **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestImportsCreateNothing:
    def test_discord_config_with_no_project_variable(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        data = _import("irp.discord_sensor.config", empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert data["mkdir"] == []
        assert data["error"] == ""
        above_repo = str(REPO.parent / ".irp")
        assert not any(v.startswith(above_repo) for v in data["values"].values())

    def test_discord_config_with_the_project_variable(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        data = _import("irp.discord_sensor.config", root, stubs,
                       env_extra={"IRP_PROJECT_ROOT": str(empty)})
        assert data["mkdir"] == []
        assert data["error"] == ""
        assert list(empty.iterdir()) == []

    def test_discord_config_walks_up_to_the_project(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        (proj / "src").mkdir()
        data = _import("irp.discord_sensor.config", proj / "src", stubs, drop=("IRP_PROJECT_ROOT",))
        assert data["error"] == ""
        assert data["values"]["IRP_LEDGER_PATH"] == str(proj / ".irp" / "ledger.jsonl")

    def test_discord_config_with_no_project_carries_the_init_line(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        data = _import("irp.discord_sensor.config", empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert data["values"]["IRP_LEDGER_PATH"] == "None"
        problem = data["values"]["IRP_PROJECT_PROBLEM"]
        assert "No IRP project" in problem and "irp init" in problem
        assert "\n" not in problem

    def test_slack_capture(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        data = _import("irp.core.integrations.slack_capture", empty, stubs, drop=("IRP_SLACK_STATE_DIR",))
        assert data["mkdir"] == []
        assert data["error"] == ""
        assert list(empty.iterdir()) == []


# ── the Discord bot ───────────────────────────────────────────────────────────

# Stand-ins for discord.py, which isn't a dev extra: just enough for
# irp/discord_sensor/main.py to import. The bot class never runs.
_DISCORD_BOT_PROBE = r"""
import sys, types
discord = types.ModuleType("discord")
discord.Intents = type("Intents", (), {"default": staticmethod(lambda: types.SimpleNamespace())})
ext = types.ModuleType("discord.ext")
commands = types.ModuleType("discord.ext.commands")
commands.Bot = type("Bot", (), {"__init__": lambda self, *a, **k: None})
ext.commands = commands
discord.ext = ext
cogs = types.ModuleType("irp.discord_sensor.commands")
async def setup(bot, writer):
    pass
cogs.setup = setup
sys.modules.update({"discord": discord, "discord.ext": ext, "discord.ext.commands": commands,
                    "irp.discord_sensor.commands": cogs})
import irp.discord_sensor.main as bot_main
class FakeBot:
    def __init__(self, *args, **kwargs):
        pass
    def run(self, token):
        print("STARTED")
bot_main.IRPBot = FakeBot
try:
    bot_main.run_bot(token="x")
except SystemExit as e:
    print("EXIT", e.code)
"""


class TestDiscordBot:
    def test_refuses_to_start_without_a_project(self, root, stubs):
        empty = root / "empty"
        empty.mkdir()
        result = _python(_DISCORD_BOT_PROBE, empty, stubs, drop=("IRP_PROJECT_ROOT",))
        assert result.returncode == 0, result.stderr
        assert "STARTED" not in result.stdout
        assert "EXIT No IRP project" in result.stdout
        assert list(empty.iterdir()) == []

    def test_starts_with_a_project(self, root, stubs):
        proj = root / "proj"
        start_project(proj)
        result = _python(_DISCORD_BOT_PROBE, proj, stubs, drop=("IRP_PROJECT_ROOT",))
        assert result.returncode == 0, result.stderr
        assert "STARTED" in result.stdout
