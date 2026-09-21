"""Private, versioned ASGI transport between ingress and persistent execution.

The execution process alone owns the application lifespan and state. Each Unix
connection carries one authenticated ASGI scope, followed by bounded messages.
Losing ingress delivers disconnect; it never cancels an accepted application
call or retries a request whose effects may already have committed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
from contextlib import suppress
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_SCOPE_BYTES = 256 * 1024
MAX_BODY_CHUNK_BYTES = 64 * 1024
MAX_WEBSOCKET_BYTES = 4 * 1024 * 1024
MAX_HEADERS = 256
MAX_HEADER_BYTES = 128 * 1024
HANDSHAKE_TIMEOUT = 5.0


class ExecutionTransportError(RuntimeError):
    """A private connection or message failed validation (never contains data)."""


def _private_parent(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ExecutionTransportError("Execution paths must be absolute and canonical")
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ExecutionTransportError("Execution path traverses a non-directory or link")
    parent_info = path.parent.lstat()
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise ExecutionTransportError("Execution directory must be owned with mode 0700")
    return path


def _read_secret(path: Path) -> str:
    path = _private_parent(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ExecutionTransportError("Execution secret must be an owned 0600 regular file")
        value = os.read(descriptor, 65)
        if re.fullmatch(rb"[0-9a-f]{64}", value) is None:
            raise ExecutionTransportError("Invalid execution secret file")
        return value.decode("ascii")
    finally:
        os.close(descriptor)


def ensure_execution_secret(secret_path: Path) -> str:
    """Create/read one private secret; the caller creates its 0700 directory."""
    path = _private_parent(secret_path)
    # Worker and ingress are separate native services and may initialize at
    # once. O_EXCL alone exposes an empty token before the first writer fsyncs.
    # Keep this lock file: unlinking it would let later callers lock a different
    # inode. Existing malformed secrets still fail closed without regeneration.
    lock_path = path.with_name(f".{path.name}.lock")
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ExecutionTransportError("Execution initializer lock must be an owned 0600 regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = lock_path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ExecutionTransportError("Execution initializer lock changed during acquisition")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            return _read_secret(path)
        with os.fdopen(descriptor, "wb") as output:
            output.write(secrets.token_hex(32).encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _read_secret(path)
    finally:
        os.close(lock)


def _socket_identity(path: Path) -> tuple[int, int]:
    info = _private_parent(path).lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ExecutionTransportError("Execution socket must be owned with mode 0600")
    return info.st_dev, info.st_ino


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ExecutionTransportError("Duplicate private protocol field")
        result[name] = value
    return result


def _bad_constant(_value: str) -> None:
    raise ExecutionTransportError("Invalid private protocol number")


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise ExecutionTransportError("Private protocol frame exceeds its limit")
    try:
        value = json.loads(await reader.readexactly(length), object_pairs_hook=_object,
                           parse_constant=_bad_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExecutionTransportError("Invalid private protocol JSON") from exc
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != PROTOCOL_VERSION:
        raise ExecutionTransportError("Unsupported private protocol version")
    return value


async def _write_frame(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    encoded = json.dumps({"v": PROTOCOL_VERSION, **message}, ensure_ascii=True,
                         allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ExecutionTransportError("Private protocol frame exceeds its limit")
    writer.write(struct.pack("!I", len(encoded)) + encoded)
    await writer.drain()


def _fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ExecutionTransportError("Invalid private protocol fields")
    return value


def _text(value: Any, limit: int = MAX_SCOPE_BYTES) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ExecutionTransportError("Invalid private protocol text")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ExecutionTransportError("Invalid private protocol boolean")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutionTransportError("Invalid private protocol integer")
    return value


def _bytes(value: Any, *, encode: bool, limit: int) -> Any:
    if encode:
        if not isinstance(value, bytes) or len(value) > limit:
            raise ExecutionTransportError("Invalid private protocol byte field")
        return base64.b64encode(value).decode("ascii")
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise ExecutionTransportError("Invalid private protocol byte field")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ExecutionTransportError("Invalid private protocol base64") from exc
    if len(decoded) > limit or base64.b64encode(decoded).decode("ascii") != value:
        raise ExecutionTransportError("Noncanonical private protocol byte field")
    return decoded


def _headers(value: Any, *, encode: bool) -> list[Any]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_HEADERS:
        raise ExecutionTransportError("Invalid private protocol headers")
    result = []
    total = 0
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ExecutionTransportError("Invalid private protocol header pair")
        converted = [_bytes(part, encode=encode, limit=MAX_HEADER_BYTES) for part in pair]
        total += sum(len(part) for part in (pair if encode else converted))
        if total > MAX_HEADER_BYTES:
            raise ExecutionTransportError("Private protocol headers exceed their limit")
        result.append(converted if encode else tuple(converted))
    return result


def _address(value: Any, *, encode: bool) -> Any:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ExecutionTransportError("Invalid private protocol address")
    address = [_text(value[0], 1024), _integer(value[1], 0, 65535)]
    return address if encode else tuple(address)


def _scope(value: dict[str, Any], *, encode: bool) -> dict[str, Any]:
    names = {"type", "http_version", "scheme", "path", "raw_path", "query_string",
             "root_path", "headers", "client", "server", "method", "subprotocols"}
    # The gateway strips process-local state/extensions; the receiver rejects
    # attempts to smuggle those markers over the private protocol.
    if not encode:
        _fields(value, {"type", "http_version", "scheme", "path", "raw_path",
                        "query_string", "root_path", "headers", "client", "server"},
                {"method", "subprotocols"})
    result = {name: item for name, item in value.items() if name in names}
    kind = result.get("type")
    if kind not in {"http", "websocket"}:
        raise ExecutionTransportError("Unsupported ASGI scope type")
    for name, default in (("http_version", "1.1"), ("root_path", "")):
        result[name] = _text(result.get(name, default))
    for name in ("scheme", "path"):
        result[name] = _text(result.get(name))
    if result["scheme"] not in ({"http", "https"} if kind == "http" else {"ws", "wss"}):
        raise ExecutionTransportError("Invalid ASGI scheme")
    result["raw_path"] = _bytes(result.get("raw_path", result["path"].encode("utf-8")), encode=encode, limit=MAX_SCOPE_BYTES)
    result["query_string"] = _bytes(result.get("query_string", b""), encode=encode, limit=MAX_SCOPE_BYTES)
    result["headers"] = _headers(result.get("headers", []), encode=encode)
    result["client"] = _address(result.get("client"), encode=encode)
    result["server"] = _address(result.get("server"), encode=encode)
    if kind == "http":
        if "subprotocols" in result:
            raise ExecutionTransportError("WebSocket field on HTTP scope")
        result["method"] = _text(result.get("method"), 32)
    else:
        if "method" in result:
            raise ExecutionTransportError("HTTP field on WebSocket scope")
        protocols = result.get("subprotocols", [])
        if not isinstance(protocols, list) or len(protocols) > MAX_HEADERS:
            raise ExecutionTransportError("Invalid WebSocket protocol offers")
        result["subprotocols"] = [_text(item, 8192) for item in protocols]
    if encode:
        if len(json.dumps(result, ensure_ascii=True).encode("ascii")) > MAX_SCOPE_BYTES:
            raise ExecutionTransportError("ASGI scope exceeds its limit")
    else:
        result["asgi"] = {"version": "3.0", "spec_version": "2.5"}
        # FileResponse will stream bytes instead of emitting local pathsend.
        result["extensions"] = {"websocket.http.response": {}} if kind == "websocket" else {}
    return result


def _event(value: dict[str, Any], *, encode: bool, kind: str, direction: str) -> dict[str, Any]:
    _fields(value, {"type"}, {"body", "more_body", "status", "headers", "trailers",
                              "more_trailers", "bytes", "text", "code", "reason", "subprotocol"})
    event_type = value["type"]
    allowed = ({"http.request", "http.disconnect"} if kind == "http" else
               {"websocket.connect", "websocket.receive", "websocket.disconnect"}) if direction == "request" else (
               {"http.response.start", "http.response.body", "http.response.trailers"} if kind == "http" else
               {"websocket.accept", "websocket.send", "websocket.close", "websocket.http.response.start", "websocket.http.response.body"})
    if not isinstance(event_type, str) or event_type not in allowed:
        raise ExecutionTransportError("Unexpected ASGI event type")
    result: dict[str, Any] = {"type": event_type}
    if event_type in {"http.request", "http.response.body", "websocket.http.response.body"}:
        _fields(value, {"type"}, {"body", "more_body"})
        result["body"] = _bytes(value.get("body", b"" if encode else ""), encode=encode, limit=MAX_BODY_CHUNK_BYTES)
        result["more_body"] = _boolean(value.get("more_body", False))
    elif event_type in {"http.response.start", "websocket.http.response.start"}:
        _fields(value, {"type", "status"}, {"headers", "trailers"})
        result["status"] = _integer(value["status"], 100, 599)
        result["headers"] = _headers(value.get("headers", []), encode=encode)
        if "trailers" in value:
            result["trailers"] = _boolean(value["trailers"])
    elif event_type == "http.response.trailers":
        _fields(value, {"type"}, {"headers", "more_trailers"})
        result["headers"] = _headers(value.get("headers", []), encode=encode)
        result["more_trailers"] = _boolean(value.get("more_trailers", False))
    elif event_type == "websocket.accept":
        _fields(value, {"type"}, {"subprotocol", "headers"})
        result["subprotocol"] = None if value.get("subprotocol") is None else _text(value["subprotocol"], 8192)
        result["headers"] = _headers(value.get("headers", []), encode=encode)
    elif event_type in {"websocket.send", "websocket.receive"}:
        _fields(value, {"type"}, {"bytes", "text"})
        has_bytes, has_text = value.get("bytes") is not None, value.get("text") is not None
        if has_bytes == has_text:
            raise ExecutionTransportError("WebSocket message must contain bytes or text")
        if has_bytes:
            result["bytes"] = _bytes(value["bytes"], encode=encode, limit=MAX_WEBSOCKET_BYTES)
        else:
            result["text"] = _text(value["text"], MAX_WEBSOCKET_BYTES)
    elif event_type in {"websocket.close", "websocket.disconnect"}:
        _fields(value, {"type"}, {"code", "reason"})
        result["code"] = _integer(value.get("code", 1000), 1000, 4999)
        result["reason"] = _text(value.get("reason", ""), 123)
    else:
        _fields(value, {"type"})
    return result


def _chunks(message: dict[str, Any]):
    """HTTP chunks are transport boundaries, not application boundaries."""
    if message.get("type") in {"http.request", "http.response.body", "websocket.http.response.body"}:
        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise ExecutionTransportError("Invalid HTTP body")
        for offset in range(0, max(1, len(body)), MAX_BODY_CHUNK_BYTES):
            end = offset + MAX_BODY_CHUNK_BYTES
            yield {**message, "body": body[offset:end], "more_body": end < len(body) or message.get("more_body", False)}
    else:
        yield message


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


class ExecutionTransportServer:
    """Serve an existing ASGI application without entering its lifespan."""

    def __init__(self, app: Any, *, socket_path: Path, secret_path: Path,
                 max_connections: int = 256) -> None:
        self.app = app
        self.socket_path = Path(socket_path)
        self.secret_path = Path(secret_path)
        self.max_connections = _integer(max_connections, 1, 4096)
        self._server: asyncio.AbstractServer | None = None
        self._closing = False
        self._identity: tuple[int, int] | None = None
        self._secret = ""
        self._connections: set[asyncio.Task[Any]] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        if self._server is not None:
            raise ExecutionTransportError("Execution transport is already started")
        _private_parent(self.socket_path)
        self._secret = _read_secret(self.secret_path)
        self._closing = False
        # Refuse existing paths, even sockets. Stale socket retirement belongs
        # to the supervisor's independently validated process/instance lease.
        if os.path.lexists(self.socket_path):
            raise ExecutionTransportError("Execution socket path already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            self._identity = _socket_identity(self.socket_path)
            listener.listen(128)
            listener.setblocking(False)
            self._server = await asyncio.start_unix_server(self._accept, sock=listener)
        except BaseException:
            listener.close()
            self._remove_own_socket()
            raise

    def _remove_own_socket(self) -> None:
        if self._identity is None:
            return
        with suppress(FileNotFoundError, ExecutionTransportError):
            if _socket_identity(self.socket_path) == self._identity:
                self.socket_path.unlink()
        self._identity = None

    async def serve_forever(self) -> None:
        if self._server is None:
            raise ExecutionTransportError("Execution transport is not started")
        await self._server.serve_forever()

    async def close(self, *, timeout: float = 5.0) -> None:
        self._closing = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        # Explicit worker shutdown may cancel work after a bounded drain. This
        # is deliberately different from ingress EOF, which never cancels it.
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        pending: set[asyncio.Task[Any]] = set()
        if self._connections:
            _done, pending = await asyncio.wait(tuple(self._connections), timeout=max(0.0, timeout))
            for task in pending:
                task.cancel()
        # A peer that stopped reading can prevent close() from flushing its
        # buffered response indefinitely. Only explicit worker shutdown aborts
        # those remaining transports after the drain window.
        for writer in writers:
            writer.transport.abort()
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        if server is not None:
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        self._remove_own_socket()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or len(self._connections) >= self.max_connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer), name="execution-request")
        self._connections.add(task)
        self._writers.add(writer)
        task.add_done_callback(self._connections.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pump: asyncio.Task[Any] | None = None
        accepted = False
        disconnected = asyncio.Event()
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4)
        try:
            opening = await asyncio.wait_for(_read_frame(reader), HANDSHAKE_TIMEOUT)
            _fields(opening, {"v", "kind", "secret", "scope"})
            if (opening["kind"] != "open" or not isinstance(opening["secret"], str)
                    or not hmac.compare_digest(opening["secret"], self._secret)):
                raise ExecutionTransportError("Execution handshake rejected")
            if len(json.dumps(opening["scope"]).encode("utf-8")) > MAX_SCOPE_BYTES:
                raise ExecutionTransportError("ASGI scope exceeds its limit")
            scope = _scope(opening["scope"], encode=False)
            kind = scope["type"]
            disconnect = {"type": "http.disconnect"} if kind == "http" else {"type": "websocket.disconnect", "code": 1006, "reason": "Ingress disconnected"}
            await _write_frame(writer, {"kind": "ready"})
            accepted = True

            async def read_events() -> None:
                try:
                    while True:
                        frame = await _read_frame(reader)
                        _fields(frame, {"v", "kind", "event"})
                        if frame["kind"] != "event":
                            raise ExecutionTransportError("Unexpected private protocol message")
                        event = _event(frame["event"], encode=False, kind=kind, direction="request")
                        await incoming.put(event)
                        if event["type"].endswith(".disconnect"):
                            break
                except (ExecutionTransportError, asyncio.IncompleteReadError, ConnectionError, OSError):
                    pass
                # Cancellation by our own completed app's cleanup is not peer
                # disconnection: leave its bounded input drain enabled.
                disconnected.set()

            async def receive() -> dict[str, Any]:
                if not incoming.empty():
                    return incoming.get_nowait()
                if disconnected.is_set():
                    return dict(disconnect)
                message = asyncio.create_task(incoming.get())
                ended = asyncio.create_task(disconnected.wait())
                try:
                    done, _ = await asyncio.wait({message, ended}, return_when=asyncio.FIRST_COMPLETED)
                    return message.result() if message in done else dict(disconnect)
                finally:
                    for task in (message, ended):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(message, ended, return_exceptions=True)

            async def send(message: dict[str, Any]) -> None:
                for chunk in _chunks(message):
                    event = _event(chunk, encode=True, kind=kind, direction="response")
                    if disconnected.is_set():
                        return
                    try:
                        await _write_frame(writer, {"kind": "event", "event": event})
                    except (ConnectionError, OSError):
                        disconnected.set()
                        return

            pump = asyncio.create_task(read_events(), name="execution-request-reader")
            # Never race this task against reader EOF or cancel it on EOF.
            await self.app(scope, receive, send)
            if not disconnected.is_set():
                await _write_frame(writer, {"kind": "complete"})
        except (ExecutionTransportError, asyncio.IncompleteReadError, ConnectionError,
                OSError, asyncio.TimeoutError):
            pass
        except Exception:
            # App failures must not leak request data/secrets over the gateway.
            with suppress(ConnectionError, OSError):
                await _write_frame(writer, {"kind": "error"})
        finally:
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if accepted and not writer.is_closing() and not disconnected.is_set():
                # A valid app can respond without consuming its request body.
                # Closing a Unix stream with unread request bytes may reset
                # the peer and discard our response. After the terminal frame,
                # ingress closes its end; drain only bounded chunks until then.
                async def drain_ingress() -> None:
                    while await reader.read(MAX_BODY_CHUNK_BYTES):
                        pass

                with suppress(asyncio.TimeoutError, ConnectionError, OSError):
                    await asyncio.wait_for(drain_ingress(), 1.0)
            self._writers.discard(writer)
            await _close_writer(writer)


class ExecutionGateway:
    """ASGI ingress forwarding one request at a time; never retries requests."""

    def __init__(self, *, socket_path: Path, secret_path: Path,
                 connect_timeout: float = HANDSHAKE_TIMEOUT) -> None:
        self.socket_path = Path(socket_path)
        self.secret_path = Path(secret_path)
        self.connect_timeout = float(connect_timeout)
        if not 0 < self.connect_timeout <= 60:
            raise ValueError("connect_timeout must be between zero and sixty seconds")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        writer: asyncio.StreamWriter | None = None
        pump: asyncio.Task[Any] | None = None
        response_started = False
        expects_trailers = False
        try:
            secret = _read_secret(self.secret_path)
            identity = _socket_identity(self.socket_path)
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), self.connect_timeout)
            if _socket_identity(self.socket_path) != identity:
                raise ExecutionTransportError("Execution socket changed during connection")
            await _write_frame(writer, {"kind": "open", "secret": secret, "scope": _scope(scope, encode=True)})
            ready = await asyncio.wait_for(_read_frame(reader), self.connect_timeout)
            _fields(ready, {"v", "kind"})
            if ready["kind"] != "ready":
                raise ExecutionTransportError("Execution handshake failed")

            async def forward_requests() -> None:
                try:
                    while True:
                        message = await receive()
                        for chunk in _chunks(message):
                            event = _event(chunk, encode=True, kind=scope["type"], direction="request")
                            await _write_frame(writer, {"kind": "event", "event": event})
                        if message["type"].endswith(".disconnect"):
                            writer.close()
                            return
                except (ConnectionError, OSError):
                    # An early response can close the request side while its
                    # response is still buffered. Let the response reader
                    # decide whether that operation completed; never retry.
                    return
                except Exception:
                    writer.close()

            pump = asyncio.create_task(forward_requests(), name="execution-gateway-reader")
            while True:
                frame = await _read_frame(reader)
                if frame.get("kind") == "complete":
                    _fields(frame, {"v", "kind"})
                    # Application return is not proof of a complete response.
                    # A terminal ASGI response event must have arrived first.
                    raise ExecutionTransportError("Execution response ended before its final event")
                _fields(frame, {"v", "kind", "event"})
                if frame["kind"] != "event":
                    raise ExecutionTransportError("Execution response failed")
                message = _event(frame["event"], encode=False, kind=scope["type"], direction="response")
                if message["type"] == "http.response.start":
                    expects_trailers = message.get("trailers", False)
                response_started = True
                await send(message)
                if (message["type"] == "websocket.close"
                        or (message["type"] in {"http.response.body", "websocket.http.response.body"}
                            and not message["more_body"] and not expects_trailers)
                        or (message["type"] == "http.response.trailers"
                            and expects_trailers and not message["more_trailers"])):
                    # Uvicorn receive() yields http.disconnect after the final
                    # body, even when application background work continues.
                    # Finish ingress at its ASGI boundary; closing IPC informs
                    # the worker of disconnection without canceling that work.
                    return
        except (ExecutionTransportError, asyncio.IncompleteReadError, ConnectionError,
                OSError, asyncio.TimeoutError):
            if not response_started:
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 503,
                                "headers": [(b"content-type", b"text/plain"), (b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": b"Execution service unavailable"})
                else:
                    await send({"type": "websocket.close", "code": 1013, "reason": "Execution service unavailable"})
            else:
                # Do not synthesize a second response or successful ending for
                # an interrupted stream. Ingress must close this connection.
                raise ExecutionTransportError("Execution response interrupted") from None
        finally:
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if writer is not None:
                await _close_writer(writer)
