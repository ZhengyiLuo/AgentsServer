"""Run-scoped Cursor CLI MCP transport; never edits native login or permissions.

Native print-mode sessions, login and cwd remain unchanged. A private per-run
configuration adds only the exact internal MCP tool permission, retaining
native permissions and explicit denies. A bounded loopback broker keeps
helper authority in AgentsServer and expires when the run ends.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import itertools
import sys
import tempfile
import subprocess
from typing import Any

PLUGIN_NAME = "agentsdock-internal-9f3a2c71"
MCP_SERVER_KEY = "provider"
MCP_NAME = "plugin-" + PLUGIN_NAME + "-" + MCP_SERVER_KEY
MCP_ALLOW = "Mcp(" + MCP_NAME + ":run)"
MAX_MESSAGE = 2 * 1024 * 1024
ENV_PORT = "AGENTSDOCK_CURSOR_TOOL_PORT"
ENV_SECRET = "AGENTSDOCK_CURSOR_TOOL_SECRET"


class CursorToolBroker:
    """One run's authenticated IPC endpoint, with bounded in-flight work."""

    def __init__(self, definition: dict, execute: Any):
        self.definition = definition
        self.execute = execute
        self.secret = secrets.token_urlsafe(48)
        self.server = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False

    async def start(self) -> dict[str, str]:
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, limit=MAX_MESSAGE + 1,
        )
        return {ENV_PORT: str(self.server.sockets[0].getsockname()[1]),
                ENV_SECRET: self.secret}

    async def close(self) -> None:
        self.closed = True
        if self.server:
            self.server.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()

    async def handle(self, reader, writer) -> None:
        task = asyncio.current_task()
        if self.closed or len(self.tasks) >= 8:
            writer.close()
            return
        self.tasks.add(task)
        response = None
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
                raise ValueError("invalid provider IPC frame")
            request = json.loads(line)
            if (not isinstance(request, dict) or self.closed
                    or not isinstance(request.get("secret"), str)
                    or not hmac.compare_digest(request["secret"], self.secret)):
                raise ValueError("provider IPC is forbidden")
            if request.get("method") == "definition":
                result = self.definition
            elif request.get("method") == "call":
                key = request.get("key")
                if not isinstance(key, str) or not 1 <= len(key) <= 160:
                    raise ValueError("invalid provider call identity")
                # The callback rechecks the actual live process/run/capability.
                text, error = await self.execute(request.get("arguments"), key)
                result = {"content": [{"type": "text", "text": text}], "isError": error}
            else:
                raise ValueError("unsupported provider IPC method")
            response = {"result": result}
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never reflect bearer material or an untrusted request in errors.
            response = {"error": "Provider tool unavailable or authorization expired."}
        finally:
            if response is not None and not self.closed:
                data = (json.dumps(response) + "\n").encode()
                if len(data) <= MAX_MESSAGE:
                    with contextlib.suppress(Exception):
                        writer.write(data)
                        await asyncio.wait_for(writer.drain(), 5)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 5)
            self.tasks.discard(task)


def broker_request(method: str, **payload) -> Any:
    request = {"method": method, "secret": os.environ[ENV_SECRET], **payload}
    data = (json.dumps(request) + "\n").encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("provider request exceeds limit")
    with socket.create_connection(("127.0.0.1", int(os.environ[ENV_PORT])), timeout=5) as conn:
        conn.settimeout(240)
        conn.sendall(data)
        with conn.makefile("rb") as stream:
            line = stream.readline(MAX_MESSAGE + 1)
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            raise ValueError("invalid provider response")
        response = json.loads(line)
        if "error" in response:
            raise ValueError(response["error"])
        return response["result"]


def mcp_main() -> None:
    """Native Cursor launches this stdio MCP, with a private per-run env."""
    connection_id = secrets.token_hex(16)
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            raise ValueError("MCP frame exceeds limit")
        message = json.loads(line)
        if "id" not in message:
            continue
        method, params = message.get("method"), message.get("params") or {}
        error = None
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": MCP_NAME, "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [broker_request("definition")]}
            elif method == "tools/call" and params.get("name") == "run":
                result = broker_request("call", arguments=params.get("arguments"),
                                        key=connection_id + ":" + str(message["id"]))
            else:
                error = {"code": -32601, "message": "Method not found"}
        except Exception:
            result = {"content": [{"type": "text", "text":
                      "Provider tool unavailable or authorization expired. Do not resend an accepted message."}],
                      "isError": True}
        response = {"jsonrpc": "2.0", "id": message["id"],
                    **({"error": error} if error else {"result": result})}
        print(json.dumps(response), flush=True)



def read_config(path: Path) -> dict:
    # A repository-controlled FIFO/device must not hang configuration startup.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MESSAGE:
            raise ValueError("Cursor configuration must be a bounded regular file")
        data = source.read(MAX_MESSAGE + 1)
    if len(data) > MAX_MESSAGE:
        raise ValueError("Cursor configuration exceeds limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Cursor configuration must be an object")
    return value


def config_root(env: dict[str, str]) -> Path:
    home = Path(env.get("HOME") or Path.home())
    explicit = env.get("CURSOR_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) / "cursor" if xdg else home / ".cursor").resolve()


def permission_snapshot(base: dict, projects: list[dict]) -> dict:
    """Match native replacement of allow lists; retain every explicit deny.

    Project permission lists override the global lists in Cursor. Keeping
    the union of explicit denies is deliberately conservative: a later empty
    deny list must not remove an earlier user prohibition to admit our tool.
    """
    result = dict(base)
    permissions = {"allow": ["Shell(ls)"], "deny": []}
    denies = []
    for config in [base, *projects]:
        supplied = config.get("permissions", {})
        if not isinstance(supplied, dict) or set(supplied) - {"allow", "deny"}:
            raise ValueError("Unsupported Cursor permission configuration")
        for key in ("allow", "deny"):
            if key not in supplied:
                continue
            values = supplied[key]
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError("Invalid Cursor permission list")
            permissions[key] = list(values)
        denies.extend(permissions["deny"])
    permissions["deny"] = list(dict.fromkeys(denies))
    permissions["allow"] = list(dict.fromkeys([*permissions["allow"], MCP_ALLOW]))
    result["permissions"] = permissions
    result.setdefault("version", 1)
    result.setdefault("editor", {"vimMode": False})
    return result


class CursorRuntimeProfile:
    """Ephemeral config/plugin; never writes native config, auth or history."""

    def __init__(self, env: dict[str, str], cwd: str):
        self.temp = None
        self.path = None
        self.env = env
        self.cwd = Path(cwd).resolve()

    def prepare(self) -> tuple[dict[str, str], list[str]]:
        original = config_root(self.env)
        base = read_config(original / "cli-config.json")
        try:
            git = subprocess.run(["git", "-C", str(self.cwd), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=5, env=self.env)
        except FileNotFoundError:
            git = None
        project_root = Path(git.stdout.strip()).resolve() if git and git.returncode == 0 else self.cwd
        try:
            relative = self.cwd.relative_to(project_root)
        except ValueError:
            raise ValueError("Cursor workspace root does not contain cwd") from None
        roots = [project_root]
        for part in relative.parts:
            roots.append(roots[-1] / part)
        projects = []
        for root in roots:
            config = read_config(root / ".cursor" / "cli.json")
            if set(config) - {"permissions"}:
                raise ValueError("Unsupported Cursor project configuration; internal tools were not enabled")
            projects.append(config)
        merged = permission_snapshot(base, projects)
        self.temp = tempfile.TemporaryDirectory(prefix="agentsdock-cursor-")
        self.path = Path(self.temp.name)
        self.path.chmod(0o700)
        config_dir = self.path / "config"
        config_dir.mkdir(mode=0o700)
        # Preserve native sibling configuration (plugins, rules, MCP settings).
        # Credential stores and CLI history use the unchanged HOME/data roots.
        if original.is_dir():
            children = list(itertools.islice(original.iterdir(), 1025))
            if len(children) > 1024:
                raise ValueError("Cursor configuration directory exceeds limit")
            for child in children:
                if child.name != "cli-config.json":
                    (config_dir / child.name).symlink_to(child, target_is_directory=child.is_dir())
        config_file = config_dir / "cli-config.json"
        config_file.write_text(json.dumps(merged), encoding="utf-8")
        config_file.chmod(0o600)
        plugin = self.path / PLUGIN_NAME
        (plugin / ".cursor-plugin").mkdir(parents=True, mode=0o700)
        (plugin / ".cursor-plugin" / "plugin.json").write_text(json.dumps({"name": PLUGIN_NAME, "version": "1.0.0"}))
        (plugin / "mcp.json").write_text(json.dumps({"mcpServers": {MCP_SERVER_KEY: {
            "command": sys.executable, "args": [str(Path(__file__).resolve()), "--mcp"],
            "env": {key: self.env[key] for key in (ENV_PORT, ENV_SECRET)}}}}))
        (plugin / "mcp.json").chmod(0o600)
        return {"CURSOR_CONFIG_DIR": str(config_dir)}, ["--disable-project-configs", "--plugin-dir", str(plugin)]

    def close(self) -> None:
        if self.temp:
            self.temp.cleanup()


if __name__ == "__main__":
    if sys.argv[1:] != ["--mcp"]:
        raise SystemExit("Only the run-bound MCP transport may invoke this module")
    mcp_main()
