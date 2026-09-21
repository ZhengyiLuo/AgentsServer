"""Independent execution owner and replaceable public ASGI gateway.

The worker owns the application's lifespan, provider transports and state. The
gateway owns only public client connections. In particular, losing the gateway
must never run the application's shutdown handlers or stop its provider children.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import importlib
import json
import logging
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any
import uuid

import uvicorn

from execution_transport import (
    ExecutionGateway,
    ExecutionTransportServer,
    ensure_execution_secret,
)


PROTOCOL_VERSION = 1
MAX_HEALTH_BYTES = 1024 * 1024


class ProcessLease:
    """One owner per role, independent of PIDs reused after a crash."""

    def __init__(self, runtime_dir: Path, role: str) -> None:
        if role not in {"worker", "gateway"}:
            raise ValueError("Unknown execution service role")
        self.directory = runtime_dir
        self.role = role
        self.fd: int | None = None
        self.instance_id = uuid.uuid4().hex

    def __enter__(self) -> "ProcessLease":
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        ensure_execution_secret(self.directory / "control.token")
        path = self.directory / f"{self.role}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Execution lease is not an owned regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Execution lease must be owner-only")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(fd, False)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def publish(self, **values: Any) -> None:
        if self.fd is None:
            raise RuntimeError("Execution lease is not held")
        receipt = {
            "schema": 1,
            "protocol": PROTOCOL_VERSION,
            "role": self.role,
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "started_at": time.time(),
            **values,
        }
        temporary = self.directory / f".{self.role}.{self.instance_id}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f"{self.role}.json")
        finally:
            temporary.unlink(missing_ok=True)

    def __exit__(self, *unused: Any) -> None:
        # The lock remains held while removing this process's receipt. Leave the
        # lock inode in place so a waiting process cannot acquire another inode.
        receipt_path = self.directory / f"{self.role}.json"
        try:
            if not receipt_path.is_symlink():
                receipt = json.loads(receipt_path.read_text())
                if isinstance(receipt, dict) and receipt.get("instance_id") == self.instance_id:
                    receipt_path.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


class ComponentHealth:
    """Add component identity without relabeling an old engine as upgraded."""

    def __init__(self, app: Any, field: str, identity: dict[str, Any]) -> None:
        self.app = app
        self.field = field
        self.identity = dict(identity)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/health":
            await self.app(scope, receive, send)
            return
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []
        count = 0
        passthrough = False

        async def component_send(message: dict[str, Any]) -> None:
            nonlocal start, count, passthrough
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                content_types = [v for k, v in headers if k.lower() == b"content-type"]
                encoded = any(k.lower() == b"content-encoding" for k, _ in headers)
                if message["status"] != 200 or encoded or not any(
                    v.lower().startswith(b"application/json") for v in content_types
                ):
                    passthrough = True
                    await send(message)
                else:
                    start = dict(message)
                return
            if passthrough or start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunk = message.get("body", b"")
            chunks.append(chunk)
            count += len(chunk)
            if count > MAX_HEALTH_BYTES:
                passthrough = True
                await send(start)
                await send({**message, "body": b"".join(chunks)})
                chunks.clear()
                return
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            try:
                document = json.loads(body)
                if isinstance(document, dict):
                    document[self.field] = self.identity
                    body = json.dumps(document, separators=(",", ":")).encode()
            except (ValueError, UnicodeError):
                pass
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("ascii")))
            await send({**start, "headers": headers})
            await send({**message, "body": body})

        await self.app(scope, receive, component_send)


class WorkerLifespan:
    """Start IPC after the application's one startup and stop it before teardown."""

    def __init__(self, app: Any, transport: ExecutionTransportServer, ready: Any) -> None:
        self.app = app
        self.transport = transport
        self.ready = ready
        self.started = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def lifecycle_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "lifespan.shutdown" and self.started:
                await self.transport.close()
                self.started = False
            return message

        async def lifecycle_send(message: dict[str, Any]) -> None:
            if message["type"] == "lifespan.startup.complete":
                await self.transport.start()
                self.started = True
                self.ready()
            await send(message)

        try:
            await self.app(scope, lifecycle_receive, lifecycle_send)
        finally:
            if self.started:
                await self.transport.close()
                self.started = False


def load_application(reference: str) -> tuple[Any, Any]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute or ":" in attribute:
        raise ValueError("Application must be an importable module:attribute")
    module = importlib.import_module(module_name)
    return module, getattr(module, attribute)


def source_version() -> str:
    return Path(__file__).with_name("VERSION").read_text().strip()


def remove_stale_worker_socket(path: Path) -> None:
    """Recover a crashed listener only while the caller holds the worker lease."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
        raise ValueError("Refusing to replace an unexpected execution socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError as error:
        if error.errno != errno.ECONNREFUSED:
            raise RuntimeError("Execution listener ownership is ambiguous") from error
    else:
        raise RuntimeError("An execution listener is still running")
    finally:
        probe.close()
    after = path.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("Execution socket changed during recovery")
    path.unlink()


async def serve_worker(args: argparse.Namespace, lease: ProcessLease) -> None:
    module, app = load_application(args.application)
    if args.application == "agent_server:app":
        expected_runtime = module.STATE_DIR.expanduser().resolve() / "execution"
        if args.runtime_dir != expected_runtime:
            raise ValueError("Worker runtime directory must be its state directory's execution folder")
    remove_stale_worker_socket(args.runtime_dir / "worker.socket")
    callback_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    callback_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    callback_socket.bind(("127.0.0.1", args.callback_port))
    callback_socket.listen(128)
    callback_socket.setblocking(False)
    callback_port = callback_socket.getsockname()[1]
    callback_origin = f"http://127.0.0.1:{callback_port}"
    maintenance = None
    if args.application == "agent_server:app":
        # Public addresses still describe the gateway, including Team Hub URLs.
        # Only provider callbacks use the worker-owned stable private listener.
        module.SERVER_BIND_ADDRESS = args.bind
        module.SERVER_PORT = args.port
        module.PROVIDER_CALLBACK_ORIGIN = callback_origin
        module.configure_server_logging(module.STATE_DIR)
        from execution_install import pending_worker_operation
        from execution_maintenance import ExecutionMaintenance

        maintenance = ExecutionMaintenance(
            module.SERVER_ADMIN_ROOT / "execution-maintenance.json", lease.instance_id,
        )
        configured_install = os.environ.get("AGENTS_SERVER_INSTALL_DIR")
        if configured_install:
            operation = pending_worker_operation(
                Path(configured_install).expanduser().resolve(),
                Path(__file__).resolve().parent,
            )
            if operation is not None:
                maintenance.hold_for_startup(operation)
        # Set before app startup so recovered queues and scheduled work cannot
        # start in a candidate that has not passed activation health checks.
        module.EXECUTION_MAINTENANCE = maintenance
    version = str(getattr(module, "SERVER_VERSION", source_version()))
    identity = {
        "protocol": PROTOCOL_VERSION,
        "instance_id": lease.instance_id,
        "pid": os.getpid(),
        "version": version,
        "worker_upgrade_policy": "when_idle",
        "rolling_worker_upgrade": False,
    }
    health_app = ComponentHealth(app, "execution_service", identity)
    transport = ExecutionTransportServer(
        health_app,
        socket_path=args.runtime_dir / "worker.socket",
        secret_path=args.runtime_dir / "control.token",
    )
    lifecycle_app = WorkerLifespan(
        health_app,
        transport,
        lambda: lease.publish(
            version=version,
            release_root=str(Path(__file__).resolve().parent),
            callback_origin=callback_origin,
            public_bind=args.bind,
            public_port=args.port,
        ),
    )
    callback_app = lifecycle_app
    if maintenance is not None:
        from execution_control import ExecutionControl

        callback_app = ExecutionControl(
            lifecycle_app,
            token=ensure_execution_secret(args.runtime_dir / "control.token"),
            worker_instance_id=lease.instance_id,
            status_callback=module.execution_maintenance_control,
            maintenance_callback=module.execution_maintenance_control,
        )
    server = uvicorn.Server(uvicorn.Config(
        callback_app, host="127.0.0.1", port=callback_port,
        lifespan="on", proxy_headers=False, server_header=False,
        log_level="info", timeout_graceful_shutdown=20,
        **({"log_config": None} if args.application == "agent_server:app" else {}),
    ))
    try:
        await server.serve(sockets=[callback_socket])
    finally:
        callback_socket.close()


async def serve_gateway(args: argparse.Namespace, lease: ProcessLease) -> None:
    gateway = ExecutionGateway(
        socket_path=args.runtime_dir / "worker.socket",
        secret_path=args.runtime_dir / "control.token",
    )
    identity = {
        "protocol": PROTOCOL_VERSION,
        "instance_id": lease.instance_id,
        "pid": os.getpid(),
        "version": source_version(),
        "restart_preserves_execution": True,
    }
    app = ComponentHealth(gateway, "gateway", identity)
    lease.publish(
        version=source_version(), release_root=str(Path(__file__).resolve().parent),
        public_bind=args.bind, public_port=args.port,
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host=args.bind, port=args.port,
        lifespan="off", proxy_headers=False, server_header=False,
        # Only client attachments belong to this process. There is no reason
        # to delay replacement while an execution-owned request keeps running.
        timeout_graceful_shutdown=2, log_level="warning", access_log=False,
    ))
    await server.serve()


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentsServer execution service")
    parser.add_argument("role", choices=["worker", "gateway"])
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7850)
    parser.add_argument("--callback-port", type=int, default=0)
    parser.add_argument("--application", default="agent_server:app")
    args = parser.parse_args()
    if not args.runtime_dir.is_absolute():
        parser.error("--runtime-dir must be absolute")
    if not 0 <= args.callback_port <= 65535 or not 1 <= args.port <= 65535:
        parser.error("Invalid listener port")
    if args.role == "gateway" and args.application != "agent_server:app":
        parser.error("Only workers load applications")
    try:
        with ProcessLease(args.runtime_dir, args.role) as lease:
            asyncio.run(serve_worker(args, lease) if args.role == "worker" else serve_gateway(args, lease))
    except BlockingIOError:
        logging.error("Another %s already owns this execution directory", args.role)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
