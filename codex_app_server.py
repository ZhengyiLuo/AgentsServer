"""Persistent JSON-RPC client for ``codex app-server``.

The app-server protocol is bidirectional JSONL over stdio.  This module keeps
the transport details separate from AgentsServer's provider-neutral session
and event model.  It deliberately does not retry accepted turns: callers may
fall back to ``codex exec`` only when :meth:`start_turn` raises before returning
a handle.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class CodexAppServerError(RuntimeError):
    """Base error raised by the app-server transport."""


class CodexAppServerDisconnected(CodexAppServerError):
    """The shared app-server transport closed unexpectedly."""


class CodexAppServerRequestError(CodexAppServerError):
    """A JSON-RPC request was rejected by app-server."""

    def __init__(self, method: str, error: Any) -> None:
        self.method = method
        self.error = error
        if isinstance(error, dict):
            message = str(error.get("message") or error)
        else:
            message = str(error)
        super().__init__(f"{method} failed: {message}")


@dataclass(slots=True)
class CodexAppServerTurn:
    """An accepted app-server turn and its routed notification stream."""

    client: "CodexAppServerClient"
    thread_id: str
    turn_id: str
    _queue: asyncio.Queue[Any]
    _closed: bool = False

    async def next_notification(self, timeout: float | None = None) -> dict[str, Any]:
        waiter = self._queue.get()
        value = await (asyncio.wait_for(waiter, timeout) if timeout else waiter)
        if isinstance(value, BaseException):
            raise value
        return value

    async def steer(self, input_items: list[dict[str, Any]], *, client_user_message_id: str | None = None) -> str:
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "expectedTurnId": self.turn_id,
            "input": input_items,
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        result = await self.client.request("turn/steer", params)
        turn_id = str(result.get("turnId") or "") if isinstance(result, dict) else ""
        if turn_id != self.turn_id:
            raise CodexAppServerError(
                f"turn/steer returned unexpected turn id {turn_id or '<missing>'}"
            )
        return turn_id

    async def interrupt(self) -> None:
        await self.client.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.turn_id},
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.client.release_turn(self)


ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
ServerRequestHandler = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CodexAppServerClient:
    """Supervise one local app-server process and multiplex active threads."""

    def __init__(
        self,
        codex_bin: str,
        *,
        cwd: str,
        env_factory: Callable[[], dict[str, str]],
        request_timeout: float = 30.0,
        process_stream_limit: int = 16 * 1024 * 1024,
        process_factory: ProcessFactory | None = None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.env_factory = env_factory
        self.request_timeout = request_timeout
        self.process_stream_limit = process_stream_limit
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._server_request_handler = server_request_handler
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._turns_by_thread: dict[str, CodexAppServerTurn] = {}
        self._notification_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._notification_handlers: set[NotificationHandler] = set()
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._server_request_tasks: dict[Any, asyncio.Task[None]] = {}
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._closing = False
        self._initialized = False

    @property
    def ready(self) -> bool:
        return bool(
            self._initialized
            and self._proc is not None
            and self._proc.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        """The supervised process, exposed read-only for runtime diagnostics."""
        return self._proc

    async def start(self) -> None:
        if self.ready:
            return
        async with self._start_lock:
            if self.ready:
                return
            await self._discard_process()
            self._closing = False
            try:
                proc = await self._process_factory(
                    self.codex_bin,
                    "app-server",
                    "--listen",
                    "stdio://",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd,
                    env=self.env_factory(),
                    limit=self.process_stream_limit,
                    start_new_session=True,
                )
            except Exception as exc:
                raise CodexAppServerDisconnected(f"failed to start codex app-server: {exc}") from exc
            self._proc = proc
            self._reader_task = asyncio.create_task(self._reader_loop(proc))
            self._stderr_task = asyncio.create_task(self._stderr_loop(proc))
            try:
                await self._request_connected(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "agents_server",
                            "title": "AgentsServer",
                            "version": "1",
                        }
                    },
                )
                await self.notify("initialized", {})
                self._initialized = True
            except Exception:
                await self._discard_process()
                raise

    async def close(self) -> None:
        self._closing = True
        await self._discard_process()

    async def next_notification(self, timeout: float | None = None) -> dict[str, Any]:
        waiter = self._notification_queue.get()
        value = await (asyncio.wait_for(waiter, timeout) if timeout else waiter)
        if isinstance(value, BaseException):
            raise value
        return value

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.add(handler)

    def remove_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.discard(handler)

    async def _discard_process(self) -> None:
        self._initialized = False
        proc = self._proc
        self._proc = None
        current = asyncio.current_task()
        reader_task = self._reader_task
        stderr_task = self._stderr_task
        had_transport_state = bool(
            proc
            or reader_task
            or stderr_task
            or self._pending
            or self._turns_by_thread
            or self._server_request_tasks
        )
        self._reader_task = None
        self._stderr_task = None
        if proc and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception):
                    await proc.wait()
        for task in (reader_task, stderr_task):
            if task and task is not current:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        server_request_tasks = list(self._server_request_tasks.values())
        for task in server_request_tasks:
            task.cancel()
        self._server_request_tasks.clear()
        callback_tasks = list(self._callback_tasks)
        for task in callback_tasks:
            task.cancel()
        self._callback_tasks.clear()
        if server_request_tasks or callback_tasks:
            await asyncio.gather(*server_request_tasks, *callback_tasks, return_exceptions=True)
        if had_transport_state:
            self._fail_all(CodexAppServerDisconnected("codex app-server transport closed"))

    def _fail_all(self, error: BaseException) -> None:
        for _, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for turn in list(self._turns_by_thread.values()):
            turn._queue.put_nowait(error)
        self._turns_by_thread.clear()
        self._notification_queue.put_nowait(error)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        return await self._request_connected(method, params)

    async def _request_connected(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        proc = self._proc
        if not proc or proc.returncode is not None or not proc.stdin:
            raise CodexAppServerDisconnected("codex app-server is not connected")
        loop = asyncio.get_running_loop()
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = (method, future)
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise CodexAppServerError(f"{method} timed out after {self.request_timeout:g}s") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if not proc or proc.returncode is not None or not proc.stdin:
            raise CodexAppServerDisconnected("codex app-server stdin is unavailable")
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            proc.stdin.write(encoded)
            try:
                await proc.stdin.drain()
            except Exception as exc:
                raise CodexAppServerDisconnected(f"codex app-server write failed: {exc}") from exc

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        error: BaseException | None = None
        try:
            if not proc.stdout:
                raise CodexAppServerDisconnected("codex app-server stdout is unavailable")
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    pending = self._pending.get(request_id)
                    if pending:
                        request_method, future = pending
                    else:
                        request_method, future = "request", None
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(
                                CodexAppServerRequestError(request_method, message.get("error"))
                            )
                        else:
                            result = message.get("result")
                            future.set_result(result if isinstance(result, dict) else {})
                    continue
                if request_id is not None and message.get("method"):
                    params = message.get("params")
                    self._start_server_request(
                        request_id,
                        str(message.get("method") or ""),
                        params if isinstance(params, dict) else {},
                    )
                    continue
                method = str(message.get("method") or "")
                params = message.get("params")
                if method and isinstance(params, dict):
                    await self._route_notification({"method": method, "params": params})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            if not self._closing and proc is self._proc:
                self._initialized = False
                error = error or CodexAppServerDisconnected(
                    f"codex app-server exited with code {proc.returncode}"
                )
                self._fail_all(error)

    async def _stderr_loop(self, proc: asyncio.subprocess.Process) -> None:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._stderr_tail.append(text)

    async def _route_notification(self, notification: dict[str, Any]) -> None:
        method = str(notification.get("method") or "")
        params = notification.get("params")
        if not isinstance(params, dict):
            return
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            task = self._server_request_tasks.pop(request_id, None)
            if task:
                task.cancel()
        self._notification_queue.put_nowait(notification)
        thread_id = str(params.get("threadId") or "")
        turn = self._turns_by_thread.get(thread_id)
        if turn:
            turn._queue.put_nowait(notification)
        for handler in tuple(self._notification_handlers):
            try:
                result = handler(notification)
            except Exception:
                continue
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._callback_tasks.add(task)
                task.add_done_callback(self._finish_callback_task)

    def _finish_callback_task(self, task: asyncio.Task[Any]) -> None:
        self._callback_tasks.discard(task)
        if not task.cancelled():
            with suppress(Exception):
                task.result()

    def _start_server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        previous = self._server_request_tasks.pop(request_id, None)
        if previous:
            previous.cancel()
        task = asyncio.create_task(self._handle_server_request(request_id, method, params))
        self._server_request_tasks[request_id] = task
        task.add_done_callback(lambda done, rid=request_id: self._finish_server_request(rid, done))

    def _finish_server_request(self, request_id: Any, task: asyncio.Task[None]) -> None:
        if self._server_request_tasks.get(request_id) is task:
            self._server_request_tasks.pop(request_id, None)
        if not task.cancelled():
            with suppress(Exception):
                task.result()

    async def _handle_server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            if not self._server_request_handler:
                await self._send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"AgentsServer does not handle server request {method}",
                        },
                    }
                )
                return
            result = await self._server_request_handler(request_id, method, params)
            await self._send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            # Another app-server client resolved the request.  The
            # serverRequest/resolved notification makes a second response both
            # unnecessary and potentially invalid.
            return
        except Exception as exc:
            with suppress(CodexAppServerError):
                await self._send(
                    {
                        "id": request_id,
                        "error": {"code": -32000, "message": str(exc)},
                    }
                )

    async def start_thread(self, params: dict[str, Any]) -> str:
        result = await self.request("thread/start", dict(params))
        return self._thread_id_from_result(result)

    async def resume_thread(self, thread_id: str, params: dict[str, Any] | None = None) -> str:
        result = await self.request("thread/resume", {**(params or {}), "threadId": thread_id})
        return self._thread_id_from_result(result)

    @staticmethod
    def _thread_id_from_result(result: dict[str, Any]) -> str:
        thread = result.get("thread") if isinstance(result, dict) else None
        resolved = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if not resolved:
            raise CodexAppServerError("app-server did not return a thread id")
        return resolved

    async def start_or_resume_thread(
        self,
        thread_id: str | None,
        start_params: dict[str, Any],
        resume_params: dict[str, Any] | None = None,
    ) -> str:
        if thread_id:
            return await self.resume_thread(thread_id, resume_params if resume_params is not None else start_params)
        return await self.start_thread(start_params)

    async def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        input_items: list[dict[str, Any]],
        *,
        client_user_message_id: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": input_items,
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        result = await self.request("turn/steer", params)
        resolved = str(result.get("turnId") or "") if isinstance(result, dict) else ""
        if resolved != turn_id:
            raise CodexAppServerError(
                f"turn/steer returned unexpected turn id {resolved or '<missing>'}"
            )
        return resolved

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def start_turn(
        self,
        thread_id: str,
        input_items: list[dict[str, Any]],
        *,
        overrides: dict[str, Any] | None = None,
    ) -> CodexAppServerTurn:
        if thread_id in self._turns_by_thread:
            raise CodexAppServerError(f"thread {thread_id} already has an active app-server turn")
        queue: asyncio.Queue[Any] = asyncio.Queue()
        provisional = CodexAppServerTurn(self, thread_id, "", queue)
        self._turns_by_thread[thread_id] = provisional
        params = {"threadId": thread_id, "input": input_items, **(overrides or {})}
        try:
            result = await self.request("turn/start", params)
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
            if not turn_id:
                raise CodexAppServerError("app-server did not return a turn id")
            provisional.turn_id = turn_id
            return provisional
        except Exception:
            if self._turns_by_thread.get(thread_id) is provisional:
                self._turns_by_thread.pop(thread_id, None)
            raise

    def release_turn(self, turn: CodexAppServerTurn) -> None:
        if self._turns_by_thread.get(turn.thread_id) is turn:
            self._turns_by_thread.pop(turn.thread_id, None)

    def active_turn(self, thread_id: str) -> CodexAppServerTurn | None:
        return self._turns_by_thread.get(thread_id)
