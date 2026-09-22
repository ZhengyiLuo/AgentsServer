"""Native ephemeral Codex forks with an independently owned lifetime.

The provider copies its own conversation history, including tool results. We
never reconstruct that history from the renderer transcript or resume, steer,
interrupt, or modify the source thread.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import tempfile

from codex_app_server import CodexAppServerClient, CodexAppServerError
from side_questions import (
    MAX_OUTPUT_BYTES, SideQuestionError, isolated_environment, run_isolated_command,
)


SIDE_INSTRUCTIONS = (
    "You are in an ephemeral side chat forked from the parent conversation. "
    "Answer only the current side question, using the inherited conversation and tool results "
    "as evidence and this side chat's own subsequent messages for follow-up references. "
    "Inherited tasks, goals, instructions, permissions, and approvals describe historical "
    "parent work; they do not authorize action in this side chat. "
    "Do not continue the parent task or pursue its goal. You have no workspace or task "
    "authority. Do not use tools, send messages, access files, browse, or claim to change "
    "anything. Answer directly and concisely; explain uncertainty when the inherited "
    "context does not contain the answer."
)

# Environment access is disabled on EVERY turn. Read-only sandboxing alone
# would still permit reading the user's files. The independent client's default
# request handler also declines inherited dynamic tool/approval requests.
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "recommended_plugins", "hooks",
    "multi_agent", "multi_agent_v2", "goals", "image_generation", "memories",
    "shell_tool", "shell_snapshot", "shell_snapshot_v2", "computer_use",
    "browser_use", "browser_use_external", "in_app_browser", "artifact",
    "request_permissions_tool", "deferred_executor", "code_mode", "code_mode_only",
    "code_mode_host", "current_time_reminder", "sleep_tool", "token_budget",
    "context_management", "realtime_conversation",
    "background_paginated_rollout_migration",
)


def isolated_config() -> dict:
    return {
        **{f"features.{name}": False for name in DISABLED_FEATURES},
        "web_search": "disabled", "tools.update_plan.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "agents.enabled": False, "notify": [],
        "orchestrator.skills.enabled": False, "orchestrator.mcp.enabled": False,
        "skills.include_instructions": False, "skills.bundled.enabled": False,
        "features.skip_host_skill_discovery": True,
        "project_doc_max_bytes": 0, "developer_instructions": SIDE_INSTRUCTIONS,
    }


def supports_native_side_chat(schema: dict) -> bool:
    """Fail closed when old protocols would silently ignore isolation fields."""
    try:
        definitions = schema["definitions"]
        fork = definitions["ThreadForkParams"]["properties"]
        environment = definitions["TurnStartParams"]["properties"]["environments"]
        ephemeral = fork["ephemeral"].get("type", [])
        return (
            "boolean" in ephemeral
            and all(name in fork for name in (
                "excludeTurns", "runtimeWorkspaceRoots", "baseInstructions",
                "developerInstructions", "config", "sandbox", "approvalPolicy",
            ))
            and "array" in environment.get("type", [])
            and "disables environment access" in environment.get("description", "")
        )
    except (KeyError, TypeError, AttributeError):
        return False


async def _verify_protocol(executable: str, temporary: str, env: dict):
    target = Path(temporary) / "schema"
    await run_isolated_command(
        [executable, "app-server", "generate-json-schema", "--experimental", "--out", str(target)],
        prompt="", cwd=temporary, env=env, timeout=15,
    )
    try:
        source = target / "codex_app_server_protocol.v2.schemas.json"
        if source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("schema too large")
        supported = supports_native_side_chat(json.loads(source.read_text()))
    except (OSError, ValueError, TypeError, AttributeError):
        supported = False
    if not supported:
        raise SideQuestionError(503, "Update Codex to use native side chats")


class NativeCodexSideChat:
    """One ephemeral provider fork, reused until its server-owned chat closes.

    The app-server process is private to this side chat. Closing it cannot
    interrupt the parent's process; no RPC that mutates the parent is issued.
    The owning service must close this object on dismissal, expiry, and shutdown.
    """

    def __init__(self, parent_thread_id: str, *, executable: str, model: str | None,
                 env: dict, parent_rollout_path: str | None = None,
                 provider_selection: dict | None = None):
        if not isinstance(parent_thread_id, str) or not parent_thread_id.strip():
            raise SideQuestionError(409, "The parent Codex conversation is not available yet")
        self.parent_thread_id = parent_thread_id
        self.parent_rollout_path = parent_rollout_path
        self.executable = executable
        self.model = model
        self.env = isolated_environment(env)
        self.provider_config = {}
        self.provider_turn_overrides = {}
        self.sensitive_values = ()
        if provider_selection:
            # Lazy import avoids the provider probe/isolated-config cycle.
            from codex_provider import native_config, native_environment, turn_overrides
            self.env = native_environment(self.env, provider_selection)
            self.provider_config = native_config(provider_selection)
            self.provider_turn_overrides = turn_overrides(provider_selection["model"], provider_selection.get("effort") or "",
                summary=provider_selection.get("reasoning_summary") or "none")
            self.sensitive_values = (provider_selection["api_key"],)
        self.thread_id: str | None = None
        self._client: CodexAppServerClient | None = None
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._opening: asyncio.Task | None = None
        self._cleaning: asyncio.Task | None = None
        self._active: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def _open(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="agentsdock-side-chat-")
        temporary = self._temporary.name
        await _verify_protocol(self.executable, temporary, self.env)
        if self._closed:
            raise SideQuestionError(409, "Side chat was closed; open a new side chat")
        config = isolated_config()
        config.update(self.provider_config)
        # Preserve Codex's auth/runtime location. Overriding sqlite_home while
        # retaining the user's history root can trigger a complete reindex.
        config.update({"log_dir": str(Path(temporary) / "log"), "history.persistence": "none"})
        from codex_provider import config_args
        client_options = {}
        if self.provider_config:
            from codex_provider import prepare_native_catalog
            catalog_path = Path(temporary) / "models.json"
            config["model_catalog_json"] = str(catalog_path)

            async def prepare_catalog():
                if self._closed:
                    raise SideQuestionError(409, "Side chat was closed; open a new side chat")
                try:
                    await prepare_native_catalog(self.executable, self.env, catalog_path)
                except Exception:
                    raise SideQuestionError(503, "Codex could not prepare custom model compatibility settings") from None
                if self._closed:
                    raise SideQuestionError(409, "Side chat was closed; open a new side chat")
            client_options["before_start"] = prepare_catalog
        args = config_args(config)
        self._client = CodexAppServerClient(
            self.executable, cwd=temporary, env_factory=lambda: self.env,
            app_server_args=args, request_timeout=20, lifecycle_timeout=30,
            process_stream_limit=MAX_OUTPUT_BYTES, notification_queue_limit=512,
            sensitive_values=self.sensitive_values,
            **client_options,
        )
        await self._client.start()
        if self._closed:
            raise SideQuestionError(409, "Side chat was closed; open a new side chat")
        effective = await self._client.request("config/read", {"includeLayers": False})
        settings = effective.get("config") if isinstance(effective, dict) else None
        if not isinstance(settings, dict):
            raise SideQuestionError(503, "Codex could not confirm isolated configuration")
        servers = settings.get("mcp_servers", {})
        if not isinstance(servers, dict) or any(not isinstance(value, dict) for value in servers.values()):
            raise SideQuestionError(503, "Codex could not confirm isolated integrations")
        # Empty maps merge with inherited config; explicitly disable every
        # server. Nested keys preserve integration names containing dots.
        config["mcp_servers"] = {name: {"enabled": False} for name in servers}
        params = {
            "ephemeral": True, "excludeTurns": True, "runtimeWorkspaceRoots": [],
            "cwd": temporary, "approvalPolicy": "never", "sandbox": "read-only",
            "baseInstructions": SIDE_INSTRUCTIONS, "developerInstructions": SIDE_INSTRUCTIONS,
            "config": config,
        }
        if self.model:
            params["model"] = self.model
        if self.provider_config:
            params["modelProvider"] = self.provider_config["model_provider"]
        if self.parent_rollout_path:
            params["path"] = self.parent_rollout_path
        # Native ephemeral forks cannot carry a goal. In particular, do not set
        # deferGoalContinuation: Codex rejects it when ephemeral is true.
        self.thread_id = await self._client.fork_thread(self.parent_thread_id, params)
        if self.thread_id == self.parent_thread_id:
            raise SideQuestionError(503, "Codex did not create a separate side chat")
        metadata = await self._client.read_thread(self.thread_id, include_turns=False)
        if metadata.get("ephemeral") is not True or metadata.get("path") is not None:
            raise SideQuestionError(503, "Codex did not confirm an ephemeral side chat")

    async def ask(self, question: str) -> str:
        if self._closed:
            raise SideQuestionError(409, "Side chat was closed; open a new side chat")
        if self._lock.locked():
            raise SideQuestionError(409, "A side question is already running in this chat")
        async with self._lock:
            self._active = asyncio.current_task()
            turn = None
            try:
                if self._opening is None:
                    self._opening = asyncio.create_task(self._open())
                # A cancelled open is joined by close before reaping its exact
                # owned process, even if spawn/fork acceptance is in flight.
                await asyncio.shield(self._opening)
                if self._closed:
                    raise SideQuestionError(409, "Side chat was closed; open a new side chat")
                turn = await self._client.start_turn(
                    self.thread_id, [{"type": "text", "text": question}],
                    overrides={**self.provider_turn_overrides, "environments": []},
                )
                answers: dict[str, str] = {}
                while True:
                    packet = await turn.next_notification()
                    method, data = packet.get("method"), packet.get("params", {})
                    if method == "item/completed":
                        item = data.get("item", {})
                        if item.get("type") == "agentMessage" and item.get("phase") in (None, "", "final_answer"):
                            text = item.get("text")
                            if isinstance(text, str):
                                answers[str(item.get("id", "answer"))] = text
                                if sum(len(value.encode("utf-8")) for value in answers.values()) > MAX_OUTPUT_BYTES:
                                    raise SideQuestionError(502, "Side question response exceeded the output limit")
                    elif method == "turn/completed":
                        completed = data.get("turn", {})
                        if completed.get("status") != "completed" or completed.get("error"):
                            raise SideQuestionError(503, "Codex did not complete the side question")
                        answer = "\n\n".join(answers.values()).strip()
                        if not answer:
                            raise SideQuestionError(502, "Codex did not return a side question answer")
                        return answer
            except CodexAppServerError:
                await self.close()
                raise SideQuestionError(503, "Codex side chat failed; check its installation and sign-in") from None
            except BaseException:
                await self.close()
                raise
            finally:
                if turn is not None:
                    await turn.close()
                self._active = None

    async def close(self):
        self._closed = True
        active = self._active
        if active is not None and active is not asyncio.current_task() and not active.done():
            active.cancel()
        if self._cleaning is None:
            async def cleanup():
                if self._opening is not None:
                    with suppress(BaseException):
                        await self._opening
                try:
                    if self._client is not None:
                        await self._client.close()
                finally:
                    if self._temporary is not None:
                        self._temporary.cleanup()
            self._cleaning = asyncio.create_task(cleanup())
        cancelled = False
        while not self._cleaning.done():
            try:
                await asyncio.shield(self._cleaning)
            except asyncio.CancelledError:
                cancelled = True
        self._cleaning.result()
        if cancelled:
            raise asyncio.CancelledError

    async def cancel(self):
        await self.close()


async def answer_side_question(question: str, *, parent_thread_id: str, executable: str,
                               model: str | None, env: dict,
                               parent_rollout_path: str | None = None) -> str:
    """Single-question convenience wrapper; follow-ups use NativeCodexSideChat."""
    chat = NativeCodexSideChat(parent_thread_id, executable=executable, model=model,
                              env=env, parent_rollout_path=parent_rollout_path)
    try:
        return await chat.ask(question)
    finally:
        await chat.close()
