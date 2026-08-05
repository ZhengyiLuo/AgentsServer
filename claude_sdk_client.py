"""Per-chat ownership and lifecycle for Claude Agent SDK clients.

The Claude Agent SDK is stateful: ``connect()``, ``query()``, message
consumption, ``interrupt()``, and ``disconnect()`` must stay on the same async
runtime.  This module keeps the SDK object behind a chat-scoped actor so HTTP
handlers never touch it directly.  The actor owns one permanent
``receive_messages()`` consumer and projects each provider response into a
bounded-lifetime :class:`ClaudeSDKRunHandle`.

The SDK dependency is intentionally optional at import time.  AgentsServer can
therefore retain its ``claude -p`` fallback when the package is unavailable.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable


CLAUDE_AGENT_SDK_MIN_VERSION = "0.2.130"


class ClaudeSDKSupervisorError(RuntimeError):
    """Base error for the chat-scoped SDK transport."""

    safe_to_fallback = False
    safe_to_requeue = False
    delivery_uncertain = False


class ClaudeSDKUnavailable(ClaudeSDKSupervisorError):
    """The SDK could not be constructed or connected before prompt delivery."""

    safe_to_fallback = True
    safe_to_requeue = True


class ClaudeSDKQueryError(ClaudeSDKSupervisorError):
    """A query failed at a boundary where delivery cannot be disproved."""

    delivery_uncertain = True


class ClaudeSDKLoopError(ClaudeSDKSupervisorError):
    """A manager, supervisor, or run handle crossed event loops."""


class ClaudeSDKSupervisorClosed(ClaudeSDKSupervisorError):
    """The chat-scoped SDK supervisor has been closed."""


class ClaudeSDKRunActive(ClaudeSDKSupervisorError):
    """The chat already owns an unfinished SDK response."""

    safe_to_requeue = True


class ClaudeSDKConfigurationConflict(ClaudeSDKSupervisorError):
    """A connected chat cannot be reconfigured while its run is active."""

    safe_to_requeue = True


@runtime_checkable
class ClaudeSDKClientProtocol(Protocol):
    async def connect(self) -> None: ...

    async def query(self, prompt: str, **kwargs: Any) -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[
    [Any],
    ClaudeSDKClientProtocol | Awaitable[ClaudeSDKClientProtocol],
]
ResultPredicate = Callable[[Any], bool]
InterruptCallback = Callable[[str], Awaitable[bool]]
SupervisorReadyCallback = Callable[[str], Awaitable[None]]


def default_is_result_message(message: Any) -> bool:
    """Recognize SDK ResultMessage objects and JSON-shaped test adapters."""

    if type(message).__name__ == "ResultMessage":
        return True
    return isinstance(message, dict) and str(message.get("type") or "") == "result"


def default_claude_sdk_client_factory(options: Any) -> ClaudeSDKClientProtocol:
    """Construct the real client without making the SDK a hard import dependency."""

    try:
        from claude_agent_sdk import ClaudeSDKClient
    except (ImportError, ModuleNotFoundError) as exc:
        raise ClaudeSDKUnavailable(
            "claude-agent-sdk is not installed; use the claude -p fallback"
        ) from exc
    return ClaudeSDKClient(options=options)


def create_claude_agent_options(**kwargs: Any) -> Any:
    """Construct ``ClaudeAgentOptions`` behind the same optional-import fence."""

    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except (ImportError, ModuleNotFoundError) as exc:
        raise ClaudeSDKUnavailable(
            "claude-agent-sdk is not installed; use the claude -p fallback"
        ) from exc
    return ClaudeAgentOptions(**kwargs)


def bind_permission_owner(options: Any, ownership_token: str) -> None:
    """Bind an AgentServer callback closure to one supervisor generation."""

    callback = (
        options.get("can_use_tool")
        if isinstance(options, dict)
        else getattr(options, "can_use_tool", None)
    )
    binder = getattr(callback, "_agentsdock_bind_owner", None)
    if callable(binder):
        binder(ownership_token)


_RUN_END = object()


@dataclass(frozen=True)
class _RunFailure:
    error: BaseException


class ClaudeSDKRunHandle:
    """An accepted query and its ordered stream of SDK messages.

    ``start_run()`` returns only after ``ClaudeSDKClient.query()`` succeeds.
    Consequently, receiving this handle is the transport's accepted boundary;
    failures raised before a handle is returned are classified by exception
    type for safe fallback decisions.
    """

    def __init__(
        self,
        chat_id: str,
        run_id: str,
        loop: asyncio.AbstractEventLoop,
        interrupt_callback: InterruptCallback,
    ) -> None:
        self.chat_id = chat_id
        self.run_id = run_id
        self._loop = loop
        self._interrupt_callback = interrupt_callback
        self._messages: asyncio.Queue[Any] = asyncio.Queue()
        self._terminal: asyncio.Future[Any] = loop.create_future()
        self.accepted_at: float | None = None

    def _check_loop(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ClaudeSDKLoopError("Claude SDK run handles require an event loop") from exc
        if running is not self._loop:
            raise ClaudeSDKLoopError(
                "Claude SDK run handle used from a different event loop"
            )

    @property
    def done(self) -> bool:
        return self._terminal.done()

    @property
    def accepted(self) -> bool:
        return self.accepted_at is not None

    async def wait_result(self) -> Any:
        """Return the terminal ResultMessage, or raise the terminal transport error."""

        self._check_loop()
        return await asyncio.shield(self._terminal)

    async def interrupt(self) -> bool:
        """Interrupt only this accepted run, returning false once it is no longer active."""

        self._check_loop()
        return await self._interrupt_callback(self.run_id)

    def __aiter__(self) -> ClaudeSDKRunHandle:
        return self

    async def __anext__(self) -> Any:
        self._check_loop()
        value = await self._messages.get()
        if value is _RUN_END:
            raise StopAsyncIteration
        if isinstance(value, _RunFailure):
            raise value.error
        return value

    def _mark_accepted(self) -> None:
        if self.accepted_at is None:
            self.accepted_at = time.monotonic()

    def _deliver(self, message: Any) -> None:
        if not self.done:
            self._messages.put_nowait(message)

    def _finish(self, terminal: Any) -> None:
        if self.done:
            return
        self._terminal.set_result(terminal)
        self._messages.put_nowait(_RUN_END)

    def _fail(self, error: BaseException) -> None:
        if self.done:
            return
        self._terminal.set_exception(error)
        # Retrieving the failure from the iterator and from wait_result() are
        # independent supported consumption modes. Marking the Future's
        # exception observed avoids noisy warnings when a caller uses only the
        # iterator.
        self._terminal.exception()
        self._messages.put_nowait(_RunFailure(error))
        self._messages.put_nowait(_RUN_END)


@dataclass(frozen=True)
class ClaudeSDKSupervisorSnapshot:
    chat_id: str
    configuration_key: str
    connected: bool
    active_run_id: str | None
    generation: int
    closed: bool
    last_used_at: float


@dataclass
class _StartRun:
    prompt: str
    run_id: str
    query_session_id: str | None
    on_supervisor_ready: SupervisorReadyCallback | None
    response: asyncio.Future[ClaudeSDKRunHandle]


@dataclass
class _Interrupt:
    run_id: str | None
    response: asyncio.Future[bool]


@dataclass
class _Close:
    response: asyncio.Future[None]


@dataclass(frozen=True)
class _ReceivedMessage:
    generation: int
    message: Any


@dataclass(frozen=True)
class _ReceiverStopped:
    generation: int
    error: BaseException | None


class ClaudeSDKSupervisor:
    """One lazy, restartable Claude SDK actor for one AgentsDock chat."""

    def __init__(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
        client_factory: ClientFactory = default_claude_sdk_client_factory,
        is_result_message: ResultPredicate = default_is_result_message,
        disconnect_timeout_seconds: float = 2.0,
    ) -> None:
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        self.chat_id = clean_chat_id
        self.options = options
        self.configuration_key = str(configuration_key)
        self.ownership_token = f"claudeowner_{uuid.uuid4().hex}"
        bind_permission_owner(self.options, self.ownership_token)
        self._client_factory = client_factory
        self._is_result_message = is_result_message
        if disconnect_timeout_seconds <= 0:
            raise ValueError("disconnect_timeout_seconds must be positive")
        self._disconnect_timeout_seconds = float(disconnect_timeout_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._commands: asyncio.Queue[Any] | None = None
        self._actor_task: asyncio.Task[None] | None = None
        self._client: ClaudeSDKClientProtocol | None = None
        self._connecting_client: ClaudeSDKClientProtocol | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._active_run: ClaudeSDKRunHandle | None = None
        self._generation = 0
        self._closed = False
        self._connected = False
        self._last_used_at = time.monotonic()
        self._inflight_response: asyncio.Future[Any] | None = None

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ClaudeSDKLoopError("Claude SDK supervisors require an event loop") from exc
        if self._loop is None:
            self._loop = loop
            self._commands = asyncio.Queue()
        elif loop is not self._loop:
            raise ClaudeSDKLoopError(
                f"Claude SDK supervisor for {self.chat_id} used from a different event loop"
            )
        return loop

    def _ensure_actor(self) -> asyncio.AbstractEventLoop:
        loop = self._bind_loop()
        if self._closed:
            raise ClaudeSDKSupervisorClosed(
                f"Claude SDK supervisor for {self.chat_id} is closed"
            )
        if self._actor_task is None:
            self._actor_task = loop.create_task(
                self._actor_loop(),
                name=f"claude-sdk:{self.chat_id}",
            )
        return loop

    @property
    def active_run_id(self) -> str | None:
        active = self._active_run
        return active.run_id if active is not None and not active.done else None

    @property
    def is_active(self) -> bool:
        return self.active_run_id is not None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_used_at(self) -> float:
        return self._last_used_at

    def snapshot(self) -> ClaudeSDKSupervisorSnapshot:
        return ClaudeSDKSupervisorSnapshot(
            chat_id=self.chat_id,
            configuration_key=self.configuration_key,
            connected=self._connected,
            active_run_id=self.active_run_id,
            generation=self._generation,
            closed=self._closed,
            last_used_at=self._last_used_at,
        )

    async def start_run(
        self,
        prompt: str,
        *,
        run_id: str,
        query_session_id: str | None = None,
        on_supervisor_ready: SupervisorReadyCallback | None = None,
    ) -> ClaudeSDKRunHandle:
        """Submit one prompt and return after the SDK accepts ``query()``."""

        if not str(run_id or "").strip():
            raise ValueError("run_id is required")
        loop = self._ensure_actor()
        response: asyncio.Future[ClaudeSDKRunHandle] = loop.create_future()
        assert self._commands is not None
        await self._commands.put(
            _StartRun(
                prompt=str(prompt),
                run_id=str(run_id),
                query_session_id=(
                    str(query_session_id)
                    if query_session_id is not None
                    else None
                ),
                on_supervisor_ready=on_supervisor_ready,
                response=response,
            )
        )
        return await asyncio.shield(response)

    async def interrupt(self, *, run_id: str | None = None) -> bool:
        """Ask the SDK to interrupt this chat's active response."""

        loop = self._ensure_actor()
        response: asyncio.Future[bool] = loop.create_future()
        assert self._commands is not None
        await self._commands.put(_Interrupt(run_id=run_id, response=response))
        return await asyncio.shield(response)

    async def close(self) -> None:
        """Interrupt any active run and disconnect this chat's SDK process."""

        loop = self._bind_loop()
        if self._closed:
            task = self._actor_task
            if task is not None and not task.done():
                await asyncio.shield(task)
            return
        if self._actor_task is None:
            self._closed = True
            return
        response: asyncio.Future[None] = loop.create_future()
        assert self._commands is not None
        await self._commands.put(_Close(response=response))
        await asyncio.shield(response)
        task = self._actor_task
        if task is not None:
            await asyncio.shield(task)

    async def abort(self) -> None:
        """Cancel this chat-owned actor even when an SDK call is wedged."""

        self._bind_loop()
        if self._closed:
            task = self._actor_task
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            return
        self._closed = True
        task = self._actor_task
        if task is None:
            return
        actor_finished = await self._cancel_task_bounded(task)
        if actor_finished:
            return
        # A third-party SDK await may ignore task cancellation. Detach its
        # process resources directly and return after the bounded disconnect;
        # the stale actor no longer owns manager-visible state and cannot
        # block a later reconnect forever.
        self._fail_active(
            ClaudeSDKSupervisorClosed(
                f"Claude SDK supervisor for {self.chat_id} was aborted"
            )
        )
        client = self._client
        connecting_client = self._connecting_client
        receiver = self._receiver_task
        self._client = None
        self._connecting_client = None
        self._receiver_task = None
        self._connected = False
        response = self._inflight_response
        if response is not None and not response.done():
            response.set_exception(
                ClaudeSDKSupervisorClosed(
                    f"Claude SDK supervisor for {self.chat_id} was aborted"
                )
            )
            response.exception()
        if client is not None:
            await self._disconnect_client(client)
        if connecting_client is not None and connecting_client is not client:
            await self._disconnect_client(connecting_client)
        if receiver is not None and receiver is not task:
            await self._cancel_task_bounded(receiver)

    async def _disconnect_client(self, client: ClaudeSDKClientProtocol) -> None:
        """Best-effort bounded disconnect for cancellation/error cleanup."""

        task = asyncio.create_task(client.disconnect())
        done, _pending = await asyncio.wait(
            {task},
            timeout=self._disconnect_timeout_seconds,
        )
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
            return
        task.cancel()

        def consume_result(completed: asyncio.Task[Any]) -> None:
            if not completed.cancelled():
                with suppress(BaseException):
                    completed.exception()

        # A cancellation-hostile third-party disconnect must not block Stop
        # or deletion. Consume its eventual result without awaiting it here.
        task.add_done_callback(consume_result)

    async def _cancel_task_bounded(self, task: asyncio.Task[Any]) -> bool:
        """Cancel one SDK-owned task without allowing teardown to wedge."""

        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return True
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=self._disconnect_timeout_seconds,
        )
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
            return True

        def consume_result(completed: asyncio.Task[Any]) -> None:
            if not completed.cancelled():
                with suppress(BaseException):
                    completed.exception()

        task.add_done_callback(consume_result)
        return False

    async def _new_client(self) -> ClaudeSDKClientProtocol:
        client: ClaudeSDKClientProtocol | None = None
        try:
            candidate = self._client_factory(self.options)
            client = await candidate if inspect.isawaitable(candidate) else candidate
            self._connecting_client = client
            await client.connect()
            if self._closed or self._connecting_client is not client:
                await self._disconnect_client(client)
                raise ClaudeSDKSupervisorClosed(
                    f"Claude SDK supervisor for {self.chat_id} closed while connecting"
                )
        except asyncio.CancelledError:
            if client is not None:
                await self._disconnect_client(client)
            raise
        except (ClaudeSDKUnavailable, ClaudeSDKSupervisorClosed):
            raise
        except Exception as exc:
            if client is not None:
                await self._disconnect_client(client)
            raise ClaudeSDKUnavailable(
                f"Claude SDK could not connect for chat {self.chat_id}: {exc}"
            ) from exc
        finally:
            if self._connecting_client is client:
                self._connecting_client = None
        self._generation += 1
        self._client = client
        self._connected = True
        self._last_used_at = time.monotonic()
        generation = self._generation
        self._receiver_task = asyncio.create_task(
            self._receive_loop(client, generation),
            name=f"claude-sdk-recv:{self.chat_id}:{generation}",
        )
        return client

    async def _ensure_client(self) -> ClaudeSDKClientProtocol:
        client = self._client
        if client is None or not self._connected:
            client = await self._new_client()
        return client

    async def _receive_loop(
        self,
        client: ClaudeSDKClientProtocol,
        generation: int,
    ) -> None:
        error: BaseException | None = None
        try:
            async for message in client.receive_messages():
                commands = self._commands
                if commands is None:
                    return
                await commands.put(
                    _ReceivedMessage(generation=generation, message=message)
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc
        finally:
            commands = self._commands
            if commands is not None:
                await commands.put(
                    _ReceiverStopped(generation=generation, error=error)
                )

    async def _disconnect_current_client(self) -> None:
        client = self._client
        receiver = self._receiver_task
        self._client = None
        self._receiver_task = None
        self._connected = False
        if client is not None:
            await self._disconnect_client(client)
        if receiver is not None and receiver is not asyncio.current_task():
            await self._cancel_task_bounded(receiver)

    def _fail_active(self, error: BaseException) -> None:
        active = self._active_run
        self._active_run = None
        if active is not None:
            active._fail(error)

    async def _handle_start(self, command: _StartRun) -> None:
        if self._active_run is not None and not self._active_run.done:
            if not command.response.done():
                command.response.set_exception(
                    ClaudeSDKRunActive(
                        f"Claude SDK chat {self.chat_id} already has active run "
                        f"{self._active_run.run_id}"
                    )
                )
            return
        self._active_run = None
        try:
            client = await self._ensure_client()
        except Exception as exc:
            if not command.response.done():
                command.response.set_exception(exc)
            return

        assert self._loop is not None
        async def interrupt_this_run(run_id: str) -> bool:
            return await self.interrupt(run_id=run_id)

        handle = ClaudeSDKRunHandle(
            self.chat_id,
            command.run_id,
            self._loop,
            interrupt_this_run,
        )
        self._active_run = handle
        self._last_used_at = time.monotonic()
        if command.on_supervisor_ready is not None:
            try:
                # Run the admission fence after connect and after publishing
                # _active_run, at the last event-loop boundary before query.
                # This prevents Stop/Delete from winning during a slow SDK
                # connection and then having the prompt delivered afterward.
                await command.on_supervisor_ready(self.ownership_token)
            except BaseException as exc:
                self._active_run = None
                handle._fail(
                    exc
                    if isinstance(exc, Exception)
                    else ClaudeSDKSupervisorClosed(
                        f"Claude SDK query admission was cancelled for chat {self.chat_id}"
                    )
                )
                if not command.response.done():
                    command.response.set_exception(exc)
                return
        try:
            if self._closed:
                raise ClaudeSDKSupervisorClosed(
                    f"Claude SDK supervisor for {self.chat_id} closed before query"
                )
            if command.query_session_id is None:
                await client.query(command.prompt)
            else:
                await client.query(
                    command.prompt,
                    session_id=command.query_session_id,
                )
        except ClaudeSDKSupervisorClosed as exc:
            self._active_run = None
            handle._fail(exc)
            if not command.response.done():
                command.response.set_exception(exc)
            await self._disconnect_current_client()
            return
        except Exception as exc:
            error = ClaudeSDKQueryError(
                f"Claude SDK query delivery is uncertain for chat {self.chat_id}: {exc}"
            )
            self._active_run = None
            handle._fail(error)
            if not command.response.done():
                command.response.set_exception(error)
            # A query failure can leave protocol framing ambiguous. Retire only
            # this chat's process; the next run may resume its persisted Claude
            # session through a fresh client.
            await self._disconnect_current_client()
            return
        handle._mark_accepted()
        if not command.response.done():
            command.response.set_result(handle)

    async def _handle_interrupt(self, command: _Interrupt) -> None:
        active = self._active_run
        if (
            active is None
            or active.done
            or (command.run_id is not None and active.run_id != command.run_id)
        ):
            if not command.response.done():
                command.response.set_result(False)
            return
        client = self._client
        if client is None or not self._connected:
            if not command.response.done():
                command.response.set_exception(
                    ClaudeSDKSupervisorError(
                        f"Claude SDK client for {self.chat_id} is not connected"
                    )
                )
            return
        try:
            await client.interrupt()
        except Exception as exc:
            if not command.response.done():
                command.response.set_exception(
                    ClaudeSDKSupervisorError(
                        f"Claude SDK interrupt failed for chat {self.chat_id}: {exc}"
                    )
                )
            return
        self._last_used_at = time.monotonic()
        if not command.response.done():
            command.response.set_result(True)

    async def _handle_received(self, command: _ReceivedMessage) -> None:
        if command.generation != self._generation:
            return
        active = self._active_run
        if active is None or active.done:
            return
        active._deliver(command.message)
        self._last_used_at = time.monotonic()
        if self._is_result_message(command.message):
            active._finish(command.message)
            self._active_run = None

    async def _handle_receiver_stopped(self, command: _ReceiverStopped) -> None:
        if command.generation != self._generation:
            return
        error = ClaudeSDKSupervisorError(
            f"Claude SDK message stream stopped for chat {self.chat_id}"
            + (f": {command.error}" if command.error is not None else "")
        )
        self._fail_active(error)
        await self._disconnect_current_client()

    async def _handle_close(self, command: _Close) -> None:
        active = self._active_run
        client = self._client
        if active is not None and not active.done and client is not None:
            with suppress(Exception):
                await client.interrupt()
        self._fail_active(
            ClaudeSDKSupervisorClosed(
                f"Claude SDK supervisor for {self.chat_id} was closed"
            )
        )
        await self._disconnect_current_client()
        self._closed = True
        if not command.response.done():
            command.response.set_result(None)

    async def _actor_loop(self) -> None:
        assert self._commands is not None
        command: Any | None = None
        try:
            while not self._closed:
                command = await self._commands.get()
                response = getattr(command, "response", None)
                self._inflight_response = (
                    response if isinstance(response, asyncio.Future) else None
                )
                if isinstance(command, _StartRun):
                    await self._handle_start(command)
                elif isinstance(command, _Interrupt):
                    await self._handle_interrupt(command)
                elif isinstance(command, _ReceivedMessage):
                    await self._handle_received(command)
                elif isinstance(command, _ReceiverStopped):
                    await self._handle_receiver_stopped(command)
                elif isinstance(command, _Close):
                    await self._handle_close(command)
                self._inflight_response = None
                command = None
        except asyncio.CancelledError:
            response = getattr(command, "response", None)
            if isinstance(response, asyncio.Future) and not response.done():
                response.set_exception(
                    ClaudeSDKSupervisorClosed(
                        f"Claude SDK supervisor for {self.chat_id} was aborted"
                    )
                )
                # A caller canceled by the same Stop request may no longer be
                # waiting on this command Future. Mark the exception observed
                # while preserving it for any remaining live waiter.
                response.exception()
            self._fail_active(
                ClaudeSDKSupervisorClosed(
                    f"Claude SDK supervisor for {self.chat_id} was closed"
                )
            )
            raise
        finally:
            await self._disconnect_current_client()
            self._closed = True
            while not self._commands.empty():
                command = self._commands.get_nowait()
                response = getattr(command, "response", None)
                if isinstance(response, asyncio.Future) and not response.done():
                    response.set_exception(
                        ClaudeSDKSupervisorClosed(
                            f"Claude SDK supervisor for {self.chat_id} is closed"
                        )
                    )


class ClaudeSDKSupervisorManager:
    """Lazy, bounded registry of independent chat-scoped supervisors."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = default_claude_sdk_client_factory,
        is_result_message: ResultPredicate = default_is_result_message,
        max_clients: int = 12,
        idle_ttl_seconds: float | None = 15 * 60,
        disconnect_timeout_seconds: float = 2.0,
    ) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        if idle_ttl_seconds is not None and idle_ttl_seconds < 0:
            raise ValueError("idle_ttl_seconds must be non-negative or None")
        self._client_factory = client_factory
        self._is_result_message = is_result_message
        self._max_clients = int(max_clients)
        self._idle_ttl_seconds = idle_ttl_seconds
        if disconnect_timeout_seconds <= 0:
            raise ValueError("disconnect_timeout_seconds must be positive")
        self._disconnect_timeout_seconds = float(disconnect_timeout_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._supervisors: OrderedDict[str, ClaudeSDKSupervisor] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._evicting: dict[str, asyncio.Task[None]] = {}
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ClaudeSDKLoopError("Claude SDK manager requires an event loop") from exc
        if self._loop is None:
            self._loop = loop
            self._lock = asyncio.Lock()
        elif loop is not self._loop:
            raise ClaudeSDKLoopError("Claude SDK manager used from a different event loop")
        if self._closed:
            raise ClaudeSDKSupervisorClosed("Claude SDK manager is closed")
        if (
            self._reaper_task is None
            and self._idle_ttl_seconds is not None
            and self._idle_ttl_seconds > 0
        ):
            self._reaper_task = loop.create_task(
                self._reaper_loop(),
                name="claude-sdk-idle-reaper",
            )
        return loop

    async def _reaper_loop(self) -> None:
        interval = min(
            60.0,
            max(1.0, float(self._idle_ttl_seconds or 1.0) / 2.0),
        )
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                await self.evict_idle()
        except (asyncio.CancelledError, ClaudeSDKSupervisorClosed):
            return

    async def _wait_for_eviction(self, chat_id: str) -> None:
        """Serialize replacement behind complete teardown of the old client."""

        assert self._lock is not None
        while True:
            async with self._lock:
                task = self._evicting.get(chat_id)
            if task is None:
                return
            await asyncio.shield(task)
            async with self._lock:
                if self._evicting.get(chat_id) is task and task.done():
                    self._evicting.pop(chat_id, None)

    async def _get_locked(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> tuple[ClaudeSDKSupervisor, ClaudeSDKSupervisor | None]:
        old_to_close: ClaudeSDKSupervisor | None = None
        supervisor = self._supervisors.get(chat_id)
        if supervisor is not None and supervisor.configuration_key != configuration_key:
            if supervisor.is_active or self._pins.get(chat_id, 0):
                raise ClaudeSDKConfigurationConflict(
                    f"Claude SDK chat {chat_id} is active with another configuration"
                )
            self._supervisors.pop(chat_id, None)
            old_to_close = supervisor
            supervisor = None
        if supervisor is None:
            supervisor = ClaudeSDKSupervisor(
                chat_id,
                options=options,
                configuration_key=configuration_key,
                client_factory=self._client_factory,
                is_result_message=self._is_result_message,
                disconnect_timeout_seconds=self._disconnect_timeout_seconds,
            )
            self._supervisors[chat_id] = supervisor
        else:
            # A disconnected actor has no live provider state. Refresh its
            # lazy reconnect options so a newly persisted resume/fork binding
            # is honored after a stream failure without changing the stable
            # process-configuration key used by healthy persistent clients.
            if not supervisor.connected and not supervisor.is_active:
                supervisor.options = options
                bind_permission_owner(
                    supervisor.options,
                    supervisor.ownership_token,
                )
            self._supervisors.move_to_end(chat_id)
        return supervisor, old_to_close

    async def get(
        self,
        chat_id: str,
        *,
        options: Any,
        configuration_key: str,
    ) -> ClaudeSDKSupervisor:
        """Return the chat supervisor, replacing an idle stale configuration."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        await self._wait_for_eviction(clean_chat_id)
        async with self._lock:
            supervisor, old_to_close = await self._get_locked(
                clean_chat_id,
                options=options,
                configuration_key=str(configuration_key),
            )
        if old_to_close is not None:
            await old_to_close.close()
        await self.evict_idle(exclude={clean_chat_id})
        return supervisor

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: Any,
        configuration_key: str,
        query_session_id: str | None = None,
        on_supervisor_ready: SupervisorReadyCallback | None = None,
    ) -> ClaudeSDKRunHandle:
        """Pin a chat through query acceptance, then return its run handle."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        await self._wait_for_eviction(clean_chat_id)
        old_to_close: ClaudeSDKSupervisor | None = None
        async with self._lock:
            supervisor, old_to_close = await self._get_locked(
                clean_chat_id,
                options=options,
                configuration_key=str(configuration_key),
            )
            self._pins[clean_chat_id] = self._pins.get(clean_chat_id, 0) + 1
        if old_to_close is not None:
            await old_to_close.close()
        try:
            return await supervisor.start_run(
                prompt,
                run_id=run_id,
                query_session_id=query_session_id,
                on_supervisor_ready=on_supervisor_ready,
            )
        finally:
            async with self._lock:
                count = self._pins.get(clean_chat_id, 0) - 1
                if count > 0:
                    self._pins[clean_chat_id] = count
                else:
                    self._pins.pop(clean_chat_id, None)
                if self._supervisors.get(clean_chat_id) is supervisor:
                    self._supervisors.move_to_end(clean_chat_id)
            await self.evict_idle(exclude={clean_chat_id})

    def owns_active_run(
        self,
        chat_id: str,
        ownership_token: str,
        run_id: str,
    ) -> bool:
        """Return whether the current registry owner is running this query."""

        supervisor = self._supervisors.get(str(chat_id))
        return bool(
            supervisor is not None
            and not supervisor.closed
            and supervisor.ownership_token == str(ownership_token)
            and supervisor.active_run_id == str(run_id)
        )

    async def interrupt(self, chat_id: str, *, run_id: str | None = None) -> bool:
        self._bind_loop()
        assert self._lock is not None
        async with self._lock:
            supervisor = self._supervisors.get(str(chat_id))
        if supervisor is None:
            return False
        return await supervisor.interrupt(run_id=run_id)

    def is_loaded(self, chat_id: str) -> bool:
        """Return whether this manager currently owns a connected SDK client."""

        supervisor = self._supervisors.get(str(chat_id))
        return bool(
            supervisor is not None
            and supervisor.connected
            and not supervisor.closed
        )

    async def evict(self, chat_id: str, *, force: bool = False) -> bool:
        """Disconnect one idle chat, or an active chat only when ``force`` is true."""

        self._bind_loop()
        assert self._lock is not None
        clean_chat_id = str(chat_id)
        async with self._lock:
            existing_close = self._evicting.get(clean_chat_id)
            supervisor = self._supervisors.get(clean_chat_id)
            if existing_close is not None:
                close_task = existing_close
            elif supervisor is None:
                return False
            else:
                if not force and (
                    supervisor.is_active or self._pins.get(clean_chat_id, 0)
                ):
                    return False
                self._supervisors.pop(clean_chat_id, None)
                self._pins.pop(clean_chat_id, None)
                close_task = asyncio.create_task(
                    supervisor.abort() if force else supervisor.close(),
                    name=f"claude-sdk-evict:{clean_chat_id}",
                )
                self._evicting[clean_chat_id] = close_task
        try:
            await asyncio.shield(close_task)
        finally:
            if close_task.done():
                async with self._lock:
                    if self._evicting.get(clean_chat_id) is close_task:
                        self._evicting.pop(clean_chat_id, None)
        return True

    async def evict_idle(self, *, exclude: set[str] | None = None) -> list[str]:
        """Apply TTL and LRU limits without ever evicting an active/pinned chat."""

        self._bind_loop()
        assert self._lock is not None
        excluded = {str(value) for value in (exclude or set())}
        now = time.monotonic()
        selected_ids: set[str] = set()
        async with self._lock:
            idle_candidates = [
                (chat_id, supervisor)
                for chat_id, supervisor in self._supervisors.items()
                if (
                    chat_id not in excluded
                    and not supervisor.is_active
                    and not self._pins.get(chat_id, 0)
                )
            ]
            idle_candidates.sort(key=lambda item: item[1].last_used_at)
            if self._idle_ttl_seconds is not None:
                for chat_id, supervisor in idle_candidates:
                    if now - supervisor.last_used_at >= self._idle_ttl_seconds:
                        selected_ids.add(chat_id)
            overflow = max(0, len(self._supervisors) - self._max_clients)
            if overflow:
                for chat_id, _supervisor in idle_candidates:
                    if len(selected_ids) >= overflow:
                        break
                    selected_ids.add(chat_id)
        if selected_ids:
            await asyncio.gather(
                *(self.evict(chat_id) for chat_id in selected_ids),
                return_exceptions=False,
            )
        return list(selected_ids)

    async def close_all(self) -> None:
        """Disconnect every per-chat process; used by AgentsServer shutdown."""

        try:
            self._bind_loop()
        except ClaudeSDKSupervisorClosed:
            return
        assert self._lock is not None
        async with self._lock:
            supervisors = list(self._supervisors.values())
            eviction_tasks = list(self._evicting.values())
            self._supervisors.clear()
            self._pins.clear()
            self._closed = True
            reaper_task = self._reaper_task
            self._reaper_task = None
        if reaper_task is not None and reaper_task is not asyncio.current_task():
            reaper_task.cancel()
            await asyncio.gather(reaper_task, return_exceptions=True)
        if supervisors:
            await asyncio.gather(
                *(supervisor.abort() for supervisor in supervisors),
                return_exceptions=False,
            )
        if eviction_tasks:
            await asyncio.gather(*eviction_tasks, return_exceptions=False)
        async with self._lock:
            self._evicting.clear()

    def snapshots(self) -> list[ClaudeSDKSupervisorSnapshot]:
        return [supervisor.snapshot() for supervisor in self._supervisors.values()]
