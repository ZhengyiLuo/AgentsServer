"""Native ephemeral Codex forks with an independently owned lifetime.

The provider copies its own conversation history, including tool results. We
never reconstruct that history from the renderer transcript or resume, steer,
interrupt, or modify the source thread.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import suppress
import json
from pathlib import Path
import tempfile

from codex_app_server import CodexAppServerClient, CodexAppServerError, decline_server_request
from side_questions import (
    MAX_OUTPUT_BYTES, SideQuestionError, isolated_environment, run_isolated_command,
)


SIDE_INSTRUCTIONS = """Answer questions and explore in this separate side chat without disrupting the main conversation.

Treat inherited messages, tasks, plans, tool calls and approvals as reference material, not current instructions or permission. Follow only requests made in this side chat; do not resume unfinished parent work.

Use the thread's existing permissions and available tools, including external tools, to read or search files and run checks that leave repo-tracked files unchanged. Do not create, contact or control subagents.

Change files, git state, configuration, permissions or other workspace state only when explicitly requested here. Request escalation only when such an explicit mutation requires it. Keep authorized changes limited to the request and preserve ongoing parent work."""

# These restrictions remain for title generation and endpoint probes. Side
# conversations use side_chat_config instead, with ordinary native tools.
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


def side_chat_config(parent_config: dict | None = None) -> dict:
    """Keep native tools, without inheriting a main run's helper transport."""
    from claude_sdk_client import CLAUDE_PROVIDER_MCP_SERVER_NAME

    config = deepcopy(parent_config or {})
    name = CLAUDE_PROVIDER_MCP_SERVER_NAME
    prefix = f"mcp_servers.{name}"
    for key in tuple(config):
        if key == prefix or key.startswith(prefix + "."):
            config.pop(key)
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(name, None)
    config.update({
        "features.multi_agent": False, "features.multi_agent_v2": False,
        "agents.enabled": False,
        f"{prefix}.enabled": False, f"{prefix}.required": False,
        # Codex validates transport shape even for a disabled server. Never
        # copy the parent's authenticated helper URL/headers into this child.
        f"{prefix}.url": "http://127.0.0.1:0",
    })
    return config


def supports_native_side_chat(schema: dict) -> bool:
    """Require the fork and permission fields used by native side chats."""
    try:
        definitions = schema["definitions"]
        fork = definitions["ThreadForkParams"]["properties"]
        ephemeral = fork["ephemeral"].get("type", [])
        return (
            "boolean" in ephemeral
            and all(name in fork for name in (
                "excludeTurns", "runtimeWorkspaceRoots", "cwd",
                "developerInstructions", "config", "sandbox", "approvalPolicy",
                "permissions", "approvalsReviewer",
            ))
        )
    except (KeyError, TypeError, AttributeError):
        return False


def _supports_empty_turn_environments(schema: dict) -> bool:
    try:
        environment = schema["definitions"]["TurnStartParams"]["properties"]["environments"]
        return ("array" in environment.get("type", [])
                and "disables environment access" in environment.get("description", ""))
    except (KeyError, TypeError, AttributeError):
        return False


async def _verify_protocol(executable: str, temporary: str, env: dict, *, require_empty_environments=False):
    target = Path(temporary) / "schema"
    await run_isolated_command(
        [executable, "app-server", "generate-json-schema", "--experimental", "--out", str(target)],
        prompt="", cwd=temporary, env=env, timeout=15,
    )
    try:
        source = target / "codex_app_server_protocol.v2.schemas.json"
        if source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("schema too large")
        schema = json.loads(source.read_text())
        supported = supports_native_side_chat(schema)
        if require_empty_environments:
            supported = supported and _supports_empty_turn_environments(schema)
    except (OSError, ValueError, TypeError, AttributeError):
        supported = False
    if not supported:
        raise SideQuestionError(503, "Update Codex to use native side chats")


async def _verify_isolated_protocol(executable: str, temporary: str, env: dict):
    # Endpoint probes still use empty environments; Side chat does not.
    await _verify_protocol(executable, temporary, env, require_empty_environments=True)


class NativeCodexSideChat:
    """One ephemeral provider fork, reused until its server-owned chat closes.

    The app-server process is private to this side chat. Closing it cannot
    interrupt the parent's process; no RPC that mutates the parent is issued.
    The owning service must close this object on dismissal, expiry, and shutdown.
    """

    def __init__(self, parent_thread_id: str, *, executable: str, model: str | None,
                 env: dict, parent_rollout_path: str | None = None,
                 provider_selection: dict | None = None, cwd: str | None = None,
                 fork_overrides: dict | None = None, turn_overrides: dict | None = None,
                 server_request_handler=None):
        if not isinstance(parent_thread_id, str) or not parent_thread_id.strip():
            raise SideQuestionError(409, "The parent Codex conversation is not available yet")
        self.parent_thread_id = parent_thread_id
        self.parent_rollout_path = parent_rollout_path
        self.executable = executable
        self.model = model
        self.cwd = cwd
        self.fork_overrides = deepcopy(fork_overrides or {})
        self.turn_overrides = deepcopy(turn_overrides or {})
        self.server_request_handler = server_request_handler
        self.env = isolated_environment(env)
        self.provider_config = {}
        self.provider_turn_overrides = {}
        self.sensitive_values = ()
        self.protected_env_keys = ()
        if provider_selection:
            # Lazy import avoids the provider probe/isolated-config cycle.
            from codex_provider import ENV_KEY, native_config, native_environment, turn_overrides as provider_turn_overrides
            self.env = native_environment(self.env, provider_selection)
            self.provider_config = native_config(provider_selection)
            self.provider_turn_overrides = provider_turn_overrides(provider_selection["model"], provider_selection.get("effort") or "",
                summary=provider_selection.get("reasoning_summary") or "none")
            self.sensitive_values = (provider_selection["api_key"],)
            self.protected_env_keys = (ENV_KEY,)
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

    async def _handle_server_request(self, request_id, method, params):
        # The callback can present approvals through the existing UI, but only
        # for this exact child. Never route a parent's inherited tool request.
        if (self.server_request_handler is not None and not self._closed
                and self._active is not None and self.thread_id is not None
                and params.get("threadId") == self.thread_id):
            return await self.server_request_handler(request_id, method, params)
        return await decline_server_request(request_id, method, params)

    async def _open(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="agentsdock-side-chat-")
        temporary = self._temporary.name
        await _verify_protocol(self.executable, temporary, self.env)
        if self._closed:
            raise SideQuestionError(409, "Side chat was closed; open a new side chat")
        config = side_chat_config(self.fork_overrides.get("config"))
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
            self.executable, cwd=self.cwd or temporary, env_factory=lambda: self.env,
            app_server_args=args,
            process_stream_limit=MAX_OUTPUT_BYTES, notification_queue_limit=512,
            sensitive_values=self.sensitive_values,
            protected_env_keys=self.protected_env_keys,
            server_request_handler=self._handle_server_request,
            **client_options,
        )
        await self._client.start()
        if self._closed:
            raise SideQuestionError(409, "Side chat was closed; open a new side chat")
        inherited_instructions = self.fork_overrides.get("developerInstructions") or ""
        params = {
            **self.fork_overrides,
            "ephemeral": True, "excludeTurns": True,
            "developerInstructions": "\n\n".join(value for value in (inherited_instructions, SIDE_INSTRUCTIONS) if value),
            "config": config,
        }
        if self.cwd:
            params["cwd"] = self.cwd
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
                    overrides={**self.turn_overrides, **self.provider_turn_overrides},
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
                               parent_rollout_path: str | None = None,
                               cwd: str | None = None, fork_overrides: dict | None = None,
                               turn_overrides: dict | None = None, server_request_handler=None) -> str:
    """Single-question convenience wrapper; follow-ups use NativeCodexSideChat."""
    chat = NativeCodexSideChat(parent_thread_id, executable=executable, model=model,
                              env=env, parent_rollout_path=parent_rollout_path,
                              cwd=cwd, fork_overrides=fork_overrides, turn_overrides=turn_overrides,
                              server_request_handler=server_request_handler)
    try:
        return await chat.ask(question)
    finally:
        await chat.close()
