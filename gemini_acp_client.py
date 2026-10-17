"""Bounded Gemini CLI ACP transport; authentication is NOT performed here.

Each owner starts its own process and supplies an explicit cwd/environment.
In particular, catalog refresh must never call ACP authenticate: Gemini 0.64
persists the selected method and can clear the user's cached credentials.
Protocol baseline: google-gemini/gemini-cli v0.64.0, ACP protocol version 1.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_REQUESTS = 32
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class GeminiProtocolError(RuntimeError):
    pass


class GeminiRPCError(GeminiProtocolError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def safe_rpc_error(error: dict) -> str:
    """Classify known failures without echoing gateway text or credentials."""
    message = str(error.get("message", "")).lower()
    if "privileged approval modes in an untrusted folder" in message:
        return "Gemini cannot enable this permission mode in an untrusted workspace. Review folder trust in native Gemini or choose Default; no prompt was sent."
    if "invalid or unavailable mode" in message:
        return "This Gemini permission mode is not enabled in the workspace. Choose an available native mode; no prompt was sent."
    if "no longer supported" in message and "code assist for individuals" in message:
        return ("Google no longer supports this Gemini CLI connection for Code Assist for individuals. "
                "Use a Gemini Developer API key in My Agents. "
                "Signing in to the same personal account again will not resolve this restriction.")
    if error.get("code") == 402 or "prepayment credits are depleted" in message:
        return "Gemini API project credits are depleted. Add credits or check billing in Google AI Studio, then retry."
    if "no longer available" in message or "modelnotfound" in message:
        return "This Gemini model is unavailable for the selected account. Choose another current model in the chat's model menu."
    if "quota" in message or "resource_exhausted" in message or "rate limit" in message:
        return "Gemini's selected account or API project has reached its quota. Check its billing/quota or retry later."
    if error.get("code") == -32000 and ("auth" in message or "api key" in message):
        return "Gemini API authentication is required. Configure its API key in My Agents, then retry."
    return "Gemini rejected the request. Check the selected account, model and workspace."


def identifier(value: Any, *, session: bool = False) -> str:
    pattern = SESSION_ID if session else IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise GeminiProtocolError("Gemini returned an invalid session/tool identifier.")
    return value


def text_content(value: Any) -> str:
    """Only visible text, never decode opaque/encrypted/provider-private data."""
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "text" and isinstance(value.get("text"), str):
        return value["text"]
    if value.get("type") == "resource" and isinstance(value.get("resource"), dict):
        text = value["resource"].get("text")
        return text if isinstance(text, str) else ""
    return ""


def normalize_update(params: Any, session_id: str) -> dict | None:
    if not isinstance(params, dict) or params.get("sessionId") != session_id:
        raise GeminiProtocolError("Gemini update belongs to a different session.")
    update = params.get("update")
    if not isinstance(update, dict):
        raise GeminiProtocolError("Gemini returned an invalid session update.")
    kind = update.get("sessionUpdate")
    if kind in {"agent_message_chunk", "agent_thought_chunk"}:
        return {"type": "text" if kind == "agent_message_chunk" else "reasoning",
                "text": text_content(update.get("content"))}
    if kind in {"tool_call", "tool_call_update"}:
        call_id = identifier(update.get("toolCallId"))
        status = update.get("status")
        if status is not None and status not in {"pending", "in_progress", "completed", "failed"}:
            raise GeminiProtocolError("Gemini returned an invalid tool status.")
        return {"type": "tool", "call_id": call_id, "status": status,
                "title": update.get("title"), "kind": update.get("kind"),
                "input": update.get("rawInput"), "output": update.get("rawOutput"),
                "content": update.get("content"), "locations": update.get("locations")}
    # user_message_chunk includes historical replay and provider reinjections.
    # It is never evidence of a new user-authored AgentsDock turn.
    if kind == "user_message_chunk":
        return None
    if kind == "plan":
        return {"type": "plan", "entries": update.get("entries")}
    if kind == "usage_update":
        return {"type": "usage", "usage": update}
    if kind == "current_mode_update":
        mode = update.get("currentModeId")
        if mode not in {"default", "autoEdit", "yolo", "plan"}:
            raise GeminiProtocolError("Gemini reported an unknown permission mode.")
        return {"type": "permission_mode", "mode": mode}
    # Model/mode/command notifications aren't conversation text.
    return None


def session_models(result: dict) -> dict:
    value = result.get("models")
    if not isinstance(value, dict):
        return {"models": [], "default_model": ""}
    options = value.get("availableModels")
    if not isinstance(options, list) or len(options) > 512:
        raise GeminiProtocolError("Gemini returned an invalid model catalog.")
    models = []
    seen = set()
    for option in options:
        if not isinstance(option, dict):
            raise GeminiProtocolError("Gemini returned an invalid model option.")
        model = option.get("modelId")
        name = option.get("name")
        if (not isinstance(model, str) or not model or len(model) > 256
                or any(ord(c) < 33 or ord(c) == 127 for c in model)
                or not isinstance(name, str) or len(name) > 256
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise GeminiProtocolError("Gemini returned an invalid model option.")
        if model not in seen:
            models.append({"value": model, "label": name or model})
            seen.add(model)
    current = value.get("currentModelId")
    return {"models": models, "default_model": current if current in seen else ""}


class GeminiACPClient:
    """One process owner. Requests, permission replies and Stop share one pipe."""

    def __init__(self, *, on_update: Callable[[dict], Awaitable[None]],
                 on_permission: Callable[[dict], Awaitable[dict]] | None = None):
        self.on_update = on_update
        self.on_permission = on_permission
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.replaying = False
        self.replay_expected = deque()
        self.replay_done = asyncio.Event()
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.reader: asyncio.Task | None = None
        self.stderr: asyncio.Task | None = None
        self.permissions: dict[Any, asyncio.Task] = {}
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.cleanup_complete = False
        self.close_lock = asyncio.Lock()
        self.guard_ready = asyncio.Event()
        self.guard_ready_verified = False
        self.failure: Exception | None = None
        self.capabilities: dict = {}
        self.requested_mode: str | None = None

    async def start(self, command: list[str], *, cwd: str, env: dict[str, str],
                    on_started: Callable[[asyncio.subprocess.Process], Awaitable[None]] | None = None) -> dict:
        if self.process is not None or self.closed:
            raise GeminiProtocolError("Gemini client cannot be started twice.")
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("cursor_process_guard.py")),
            "--verify-cleanup", "--ready-notice", "--parent-pid", str(os.getpid()), "--", *command,
            cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE_BYTES, start_new_session=True,
        ))
        cancelled = False
        while True:
            try:
                # Stop can arrive while asyncio is constructing the transport.
                # Retain its process handle before propagating cancellation so
                # the runner's finally block can verify owned-tree cleanup.
                self.process = await asyncio.shield(spawning)
                break
            except asyncio.CancelledError:
                if spawning.cancelled():
                    raise
                cancelled = True
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._drain_stderr())
        if cancelled:
            raise asyncio.CancelledError()
        if on_started:
            await on_started(self.process)
        result = await self.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "agentsdock", "version": "1"},
            "clientCapabilities": {},  # Do not advertise an unimplemented FS/terminal proxy.
        })
        if result.get("protocolVersion") != 1:
            raise GeminiProtocolError("Unsupported Gemini ACP protocol version.")
        self.capabilities = result.get("agentCapabilities") or {}
        return result

    async def open_session(self, *, cwd: str, session_id: str | None = None,
                           mcp_servers: list[dict] | None = None,
                           replay: list[tuple[str, str]] | None = None) -> dict:
        if self.session_id is not None:
            raise GeminiProtocolError("This Gemini process already owns a session.")
        params: dict = {"cwd": cwd, "mcpServers": mcp_servers or []}
        if session_id:
            if self.capabilities.get("loadSession") is not True:
                raise GeminiProtocolError("This Gemini CLI cannot resume sessions.")
            self.session_id = identifier(session_id, session=True)
            if replay is None:
                raise GeminiProtocolError("Gemini resume requires a verified native replay snapshot.")
            self.replay_expected = deque(replay)
            self.replay_done.clear()
            if not replay:
                self.replay_done.set()
            self.replaying = True
            try:
                result = await self.request("session/load", {**params, "sessionId": self.session_id})
                await asyncio.wait_for(self.replay_done.wait(), 45)
                if self.failure:
                    raise self.failure
            finally:
                self.replaying = False
        else:
            result = await self.request("session/new", params)
            self.session_id = identifier(result.get("sessionId"), session=True)
        return result

    async def prompt(self, blocks: list[dict], *, timeout: float = 3600) -> dict:
        if not self.session_id:
            raise GeminiProtocolError("Gemini session has not been opened.")
        return await self.request("session/prompt", {
            "sessionId": self.session_id, "prompt": blocks,
        }, timeout=timeout)

    async def cancel(self) -> None:
        if self.session_id and not self.closed:
            await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                              "params": {"sessionId": self.session_id}})

    async def request(self, method: str, params: dict, *, timeout: float = 45) -> dict:
        if self.closed or self.failure or len(self.pending) >= MAX_REQUESTS:
            raise GeminiProtocolError("Gemini connection is unavailable.") from self.failure
        # Calling authenticate is an explicit account mutation, never an ordinary
        # admission/recheck step. A dedicated isolated API profile is required.
        if method == "authenticate":
            raise GeminiProtocolError("Authentication mutation is not allowed on this transport.")
        if method == "session/set_mode":
            if self.requested_mode is not None or params.get("modeId") not in {"default", "autoEdit", "yolo", "plan"}:
                raise GeminiProtocolError("Invalid or overlapping Gemini mode change.")
            self.requested_mode = params["modeId"]
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)
            if method == "session/set_mode":
                self.requested_mode = None
            if not future.done():
                future.cancel()

    async def _send(self, payload: dict) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_LINE_BYTES:
            raise GeminiProtocolError("Gemini request exceeds the protocol size limit.")
        async with self.write_lock:
            if self.closed or not self.process or not self.process.stdin:
                raise GeminiProtocolError("Gemini connection is closed.")
            self.process.stdin.write(encoded)
            await asyncio.wait_for(self.process.stdin.drain(), 10)

    async def _read(self) -> None:
        count = total = 0
        try:
            assert self.process and self.process.stdout
            while raw := await self.process.stdout.readline():
                count += 1
                total += len(raw)
                if len(raw) > MAX_LINE_BYTES or total > MAX_STREAM_BYTES or count > MAX_EVENTS:
                    raise GeminiProtocolError("Gemini exceeded the bounded protocol stream limit.")
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise GeminiProtocolError("Gemini emitted invalid JSON-RPC.") from None
                if not isinstance(event, dict) or event.get("jsonrpc") != "2.0":
                    raise GeminiProtocolError("Gemini emitted an invalid JSON-RPC envelope.")
                method = event.get("method")
                if method is not None:
                    if not isinstance(method, str):
                        raise GeminiProtocolError("Gemini emitted an invalid method.")
                    if "id" in event:
                        key = event["id"]
                        if not (type(key) is int or isinstance(key, str) and 0 < len(key) < 256):
                            raise GeminiProtocolError("Gemini emitted an invalid request identifier.")
                        if key in self.permissions or len(self.permissions) >= MAX_REQUESTS:
                            raise GeminiProtocolError("Gemini exceeded the outstanding request limit.")
                        task = asyncio.create_task(self._answer(key, method, event.get("params")))
                        self.permissions[key] = task
                        task.add_done_callback(lambda _task, key=key: self.permissions.pop(key, None))
                    elif method == "session/update":
                        params = event.get("params")
                        # Command notifications may arrive immediately after new;
                        # no model is prompted until open_session has completed.
                        if self.session_id is not None:
                            update = normalize_update(params, self.session_id)
                            # Pinned CLI emits its set_mode acknowledgement as
                            # text rather than ACP current_mode_update. Correlate
                            # only the exact notice during our own RPC, never
                            # suppress matching model replies or history replay.
                            if (self.requested_mode is not None and not self.replaying
                                    and update == {"type": "text", "text": f"[MODE_UPDATE] {self.requested_mode}"}):
                                update = {"type": "permission_mode", "mode": self.requested_mode}
                            if self.replaying and params["update"].get("sessionUpdate") in {
                                "user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"
                            }:
                                from gemini_history import fingerprint
                                if not self.replay_expected or fingerprint(params["update"]) != self.replay_expected.popleft():
                                    raise GeminiProtocolError("Gemini history replay changed; refusing to mix it with a new turn.")
                                if not self.replay_expected:
                                    self.replay_done.set()
                            if update is not None and not self.replaying:
                                await self.on_update(update)
                    continue
                key = event.get("id")
                if type(key) is not int:
                    raise GeminiProtocolError("Gemini emitted an invalid response identifier.")
                future = self.pending.get(key)
                if future is None or future.done():
                    continue  # A late response to a timed-out/cancelled request.
                error = event.get("error")
                if isinstance(error, dict):
                    code = error.get("code")
                    # Do not include raw RPC messages: gateways can echo keys.
                    future.set_exception(GeminiRPCError(code if type(code) is int else -32000, safe_rpc_error(error)))
                elif isinstance(event.get("result"), dict):
                    future.set_result(event["result"])
                else:
                    raise GeminiProtocolError("Gemini emitted an invalid response.")
            raise GeminiProtocolError("Gemini disconnected before the request completed.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc if isinstance(exc, GeminiProtocolError) else GeminiProtocolError("Gemini protocol connection failed.")
            self.replay_done.set()
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(self.failure)

    async def _answer(self, key: Any, method: str, params: Any) -> None:
        try:
            if method != "session/request_permission":
                await self._send({"jsonrpc": "2.0", "id": key,
                                  "error": {"code": -32601, "message": "Unsupported client method"}})
                return
            result = {"outcome": {"outcome": "cancelled"}}
            if (not self.replaying and self.session_id and isinstance(params, dict)
                    and params.get("sessionId") == self.session_id and self.on_permission):
                result = await self.on_permission(params)
            await self._send({"jsonrpc": "2.0", "id": key, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Permission callbacks must fail closed, never silently auto-approve.
            with suppress(Exception):
                await self._send({"jsonrpc": "2.0", "id": key,
                                  "result": {"outcome": {"outcome": "cancelled"}}})

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        from cursor_process_guard import READY_NOTICE
        try:
            self.guard_ready_verified = await self.process.stderr.readline() == READY_NOTICE
        finally:
            self.guard_ready.set()
        # Native stderr can include credentials, browser URLs and prompts. Drain
        # without retaining or publishing it; use structured, sanitized errors.
        while await self.process.stderr.read(16 * 1024):
            pass

    async def close(self) -> None:
        async with self.close_lock:
            await self._close_owned()

    async def _close_owned(self) -> None:
        if self.cleanup_complete:
            return
        self.closed = True
        if self.process:
            # Never TERM the guard before its handlers are installed. An early
            # Stop used to kill the wrapper with -SIGTERM and permanently fence
            # the chat, with no trustworthy evidence about child cleanup.
            try:
                await asyncio.wait_for(self.guard_ready.wait(), 5)
            except asyncio.TimeoutError:
                raise GeminiProtocolError("Gemini runtime cleanup has not completed.") from None
            if not self.guard_ready_verified:
                raise GeminiProtocolError("Gemini runtime cleanup could not be verified.")
        tasks = [task for task in (self.reader, self.stderr, *self.permissions.values()) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(GeminiProtocolError("Gemini connection closed."))
        proc = self.process
        if proc:
            if proc.stdin:
                proc.stdin.close()
            # The guard owns the CLI's separate group, tracks detached children
            # and survives an abrupt server death. Give it time to verify exit;
            # killing the guard itself is NOT evidence that tools have stopped.
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                raise GeminiProtocolError("Gemini runtime cleanup has not completed.") from None
            if proc.returncode not in {0, 71, 143}:
                raise GeminiProtocolError("Gemini runtime cleanup could not be verified.")
        self.cleanup_complete = True
