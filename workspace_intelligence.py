"""Native, on-demand language servers owned by individual editor connections."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import signal
from typing import Any, Callable

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect


WEBSOCKET_PROTOCOL = "agentsdock-intelligence-v1"
LANGUAGES = {
    "python": {
        "binary": "pyright-langserver",
        "local": (".venv/bin/pyright-langserver", "node_modules/.bin/pyright-langserver"),
        "install_hint": "Install Pyright in this project's environment or make pyright-langserver available on the server PATH.",
    },
    "typescript": {
        "binary": "typescript-language-server",
        "local": ("node_modules/.bin/typescript-language-server",),
        "install_hint": "Install typescript-language-server with a TypeScript version that provides tsserver.js (TypeScript 6 is supported) in this project or on the server PATH.",
    },
}


class IntelligenceError(Exception):
    """A bridge failure that the editor can present without losing its buffer."""


def language_command(root: Path, language: str, environment: dict[str, str]) -> tuple[str, ...] | None:
    specification = LANGUAGES.get(language)
    if specification is None:
        raise HTTPException(404, "Unsupported intelligence language.")
    for relative in specification["local"]:
        candidate = root / relative
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return (str(candidate), "--stdio")
    # Resolve relative PATH components against the selected workspace, never
    # against the server process's unrelated working directory.
    search_path = os.pathsep.join(
        str(Path(entry) if Path(entry).is_absolute() else root / entry)
        for entry in environment.get("PATH", os.defpath).split(os.pathsep)
    )
    installed = shutil.which(specification["binary"], path=search_path)
    return (installed, "--stdio") if installed else None


def rpc_object(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
            or not (isinstance(value.get("method"), str) or "id" in value)):
        raise IntelligenceError("Expected a JSON-RPC 2.0 object.")
    return value


async def read_message(stream: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read UTF-8 LSP content using its byte length, including split frames."""
    try:
        headers = await stream.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise IntelligenceError("The language server closed an incomplete message.") from exc
    lengths = []
    for line in headers[:-4].split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if not separator:
            raise IntelligenceError("The language server sent an invalid message header.")
        if name.lower() == b"content-length":
            lengths.append(value.strip())
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise IntelligenceError("The language server sent an invalid Content-Length.")
    try:
        body = await stream.readexactly(int(lengths[0]))
        return rpc_object(json.loads(body.decode("utf-8")))
    except (asyncio.IncompleteReadError, UnicodeError, ValueError) as exc:
        raise IntelligenceError("The language server sent an incomplete or invalid JSON message.") from exc


async def write_message(stream: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    await stream.drain()


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Reap only the process group created for this connection, including tsserver."""
    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(65536):
                pass

    # A paused full pipe can otherwise keep asyncio's Process.wait pending
    # after the child has exited. The regular pumps have already been joined.
    drains = [asyncio.create_task(discard(stream)) for stream in (process.stdout, process.stderr)]
    if process.stdin is not None:
        process.stdin.close()
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass
    finally:
        # The wrapper can exit before its children. Its private process group
        # remains ours until all children exit; no agent process shares it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS can report EPERM for a group whose already-exited members
            # are awaiting reaping. Do not mask a live parent's signal failure.
            if process.returncode is None:
                raise
        await process.wait()
        await asyncio.gather(*drains, return_exceptions=True)


class WorkspaceIntelligence:
    def __init__(self, *, workspace_root: Callable, environment: Callable):
        self.workspace_root = workspace_root
        self.environment = environment
        self.connections: set[asyncio.Task] = set()
        self.closing = False

    def checked_root(self, session_id: str, expected: tuple[Path, int, int] | None = None) -> tuple[Path, int, int]:
        _, root = self.workspace_root(session_id)
        info = root.stat()
        selected = (root, info.st_dev, info.st_ino)
        if expected is not None and selected != expected:
            raise HTTPException(409, "This chat's working directory changed. Reopen the editor intelligence connection.")
        return selected

    def status(self, session_id: str) -> dict[str, Any]:
        selected = self.checked_root(session_id)
        root = selected[0]
        environment = self.environment()
        languages = []
        for language, specification in LANGUAGES.items():
            available = language_command(root, language, environment) is not None
            languages.append({"id": language, "available": available, **({} if available else {
                "message": f"{specification['binary']} is not installed for this workspace.",
                "install_hint": specification["install_hint"],
            })})
        self.checked_root(session_id, selected)
        return {"root": str(root), "root_uri": root.as_uri(), "languages": languages}

    async def shutdown(self) -> None:
        self.closing = True
        owners = tuple(self.connections)
        for owner in owners:
            owner.cancel()
        await asyncio.gather(*owners, return_exceptions=True)

    async def serve(self, session_id: str, language: str, ws: WebSocket,
                    *, authorize: Callable, protocols: Callable) -> None:
        offered = protocols(ws)
        await ws.accept(subprotocol=WEBSOCKET_PROTOCOL if WEBSOCKET_PROTOCOL in offered else None)
        if not authorize(ws):
            await ws.close(code=4401)
            return
        if WEBSOCKET_PROTOCOL not in offered:
            await ws.close(code=4406)
            return
        if ws.query_params:
            await ws.close(code=4400)
            return
        if self.closing:
            await ws.close(code=1012)
            return

        owner = asyncio.current_task()
        assert owner is not None
        self.connections.add(owner)
        spawn: asyncio.Task | None = None
        pumps: set[asyncio.Task] = set()
        close_code = 1000
        error: str | None = None
        try:
            selected = await asyncio.to_thread(self.checked_root, session_id)
            environment = await asyncio.to_thread(self.environment)
            command = await asyncio.to_thread(language_command, selected[0], language, environment)
            if command is None:
                raise HTTPException(503, LANGUAGES[language]["install_hint"])
            await asyncio.to_thread(self.checked_root, session_id, selected)
            # Shield creation so disconnect/cancellation cannot lose ownership
            # between OS process creation and delivery of its Process handle.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=selected[0], env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            ))
            process = await asyncio.shield(spawn)
            await asyncio.to_thread(self.checked_root, session_id, selected)
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None

            async def from_client() -> None:
                while True:
                    frame = await ws.receive()
                    if frame["type"] == "websocket.disconnect":
                        return
                    raw = frame.get("text")
                    if raw is None:
                        raise IntelligenceError("Intelligence messages must be JSON text.")
                    message = rpc_object(json.loads(raw))
                    await asyncio.to_thread(self.checked_root, session_id, selected)
                    if message.get("method") == "initialize":
                        params = message.get("params")
                        if not isinstance(params, dict):
                            raise IntelligenceError("Initialize requires an object of parameters.")
                        options = params.get("initializationOptions")
                        tsserver = options.get("tsserver") if isinstance(options, dict) else None
                        semantic_only = (language == "typescript" and isinstance(tsserver, dict)
                                         and tsserver.get("useSyntaxServer") == "never")
                        # The server owns workspace and process identity. Do not
                        # accept executable/configuration paths from the client.
                        params = {key: value for key, value in params.items()
                                  if key not in {"initializationOptions", "processId", "rootUri", "rootPath", "workspaceFolders"}}
                        if semantic_only:
                            # Symbol search needs the semantic project's initial
                            # load, not the fast syntax server's partial view.
                            params["initializationOptions"] = {"tsserver": {"useSyntaxServer": "never"}}
                        params.update(processId=None, rootUri=selected[0].as_uri(), rootPath=str(selected[0]),
                                      workspaceFolders=[{"uri": selected[0].as_uri(), "name": selected[0].name}])
                        message = {**message, "params": params}
                    await write_message(process.stdin, message)

            async def from_server() -> None:
                while True:
                    message = await read_message(process.stdout)
                    if message is None:
                        code = await process.wait()
                        if code:
                            raise IntelligenceError(f"The language server exited with status {code}.")
                        return
                    await asyncio.to_thread(self.checked_root, session_id, selected)
                    await ws.send_json(message)

            async def drain_errors() -> None:
                # Language server logs can be large and contain workspace text.
                # Drain without retaining them or mixing them into JSON-RPC.
                while await process.stderr.read(65536):
                    pass

            incoming = asyncio.create_task(from_client())
            outgoing = asyncio.create_task(from_server())
            errors = asyncio.create_task(drain_errors())
            pumps.update((incoming, outgoing, errors))
            done, _ = await asyncio.wait((incoming, outgoing), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except HTTPException as exc:
            close_code = 4404 if exc.status_code == 404 else 4409 if exc.status_code == 409 else 1011
            error = exc.detail.get("message", str(exc.detail)) if isinstance(exc.detail, dict) else str(exc.detail)
        except (IntelligenceError, OSError, ValueError, asyncio.LimitOverrunError) as exc:
            close_code = 1011
            error = str(exc) or "The language server connection failed."
        except asyncio.CancelledError:
            close_code = 1001
            raise
        finally:
            async def cleanup() -> None:
                for task in pumps:
                    task.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                if spawn is not None:
                    with suppress(Exception):
                        process = await spawn
                        await stop_process(process)
                with suppress(Exception):
                    if error:
                        await ws.send_json({"type": "intelligence_error", "message": error})
                    await ws.close(code=close_code)

            cleanup_task = asyncio.create_task(cleanup())
            try:
                # Repeated lifecycle cancellation must still reap our process.
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                cleanup_task.result()
            finally:
                self.connections.discard(owner)


def register_workspace_intelligence_routes(app, *, authorize: Callable,
                                           authorize_websocket: Callable, protocols: Callable,
                                           workspace_root: Callable, environment: Callable) -> WorkspaceIntelligence:
    runtime = WorkspaceIntelligence(workspace_root=workspace_root, environment=environment)

    @app.get("/api/sessions/{session_id}/workspace/intelligence")
    async def intelligence_status(session_id: str, request: Request) -> dict[str, Any]:
        authorize(request)
        return await asyncio.to_thread(runtime.status, session_id)

    @app.websocket("/api/sessions/{session_id}/workspace/intelligence/{language}")
    async def intelligence_socket(session_id: str, language: str, ws: WebSocket) -> None:
        await runtime.serve(session_id, language, ws, authorize=authorize_websocket, protocols=protocols)

    return runtime
