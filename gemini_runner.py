"""Gemini turns and run-owned approvals using AgentsServer's durable lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import secrets
import sys
import time

from fastapi import APIRouter, HTTPException, Request

from gemini_acp_client import GeminiACPClient, GeminiProtocolError, identifier, session_models, text_content
from gemini_runtime import build_environment, GeminiConfigurationError, resolve_executable

PERMISSIONS: dict[str, dict] = {}
MODEL_CATALOG: dict[str, dict] = {}


def owns(api, session_id, run_id):
    active = api.ACTIVE.get(session_id) or {}
    return (session_id in api.STORE.sessions and session_id not in api.DELETING_SESSIONS
            and run_id not in api.STOPPED_RUNS and active.get("run_id") == run_id
            and active.get("backend") == "gemini" and not active.get("stop_requested"))


async def ask_permission(api, session_id: str, run_id: str, params: dict) -> dict:
    cancelled = {"outcome": {"outcome": "cancelled"}}
    if not owns(api, session_id, run_id) or len(PERMISSIONS) >= 128:
        return cancelled
    options = params.get("options")
    if not isinstance(options, list) or len(options) > 16:
        return cancelled
    allowed = {item["kind"]: item["optionId"] for item in options
               if isinstance(item, dict) and item.get("kind") in {"allow_once", "reject_once"}
               and isinstance(item.get("optionId"), str) and len(item["optionId"]) <= 240}
    tool = params.get("toolCall") or {}
    key = "geminireq_" + secrets.token_hex(16)
    public = {"id": key, "session_id": session_id, "thread_id": params.get("sessionId"),
              "turn_id": run_id, "item_id": identifier(tool.get("toolCallId")),
              "method": "item/tool/requestApproval", "created_at": api.now_iso(),
              "params": {"toolName": str(tool.get("title") or "Gemini tool")[:512],
                         "availableDecisions": [name for name, kind in [("accept", "allow_once"), ("decline", "reject_once")] if kind in allowed],
                         "toolInput": {"content": api.bounded_codex_interaction_value(tool.get("content") or [])}}}
    future = asyncio.get_running_loop().create_future()
    PERMISSIONS[key] = {"public": public, "future": future, "options": allowed, "run_id": run_id}
    try:
        await api.append_event(session_id, "gemini_interaction_requested", {"interaction": public, "run_id": run_id})
        choice = await asyncio.wait_for(future, 1800)
        if choice not in allowed or not owns(api, session_id, run_id):
            return cancelled
        return {"outcome": {"outcome": "selected", "optionId": allowed[choice]}}
    finally:
        PERMISSIONS.pop(key, None)
        if session_id in api.STORE.sessions:
            await api.append_event(session_id, "gemini_interaction_resolved", {"interaction": public, "run_id": run_id})


def router(api) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/sessions/{session_id}/gemini/runtime")
    async def runtime(session_id: str, request: Request):
        api.require_native_admin_control(request)
        sess = api.STORE.sessions.get(session_id)
        if not sess or sess.get("backend") != "gemini":
            raise HTTPException(404, "Gemini chat not found.")
        return {"pending_interactions": [row["public"] for row in PERMISSIONS.values()
                    if row["public"]["session_id"] == session_id and owns(api, session_id, row["run_id"])],
                "available": True}

    @routes.post("/api/sessions/{session_id}/gemini/interactions/{interaction_id}/resolve")
    async def respond(session_id: str, interaction_id: str, request: Request):
        api.require_native_admin_control(request)
        raw = bytearray()
        async def read():
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise HTTPException(413, "Approval response is too large.")
        try:
            await asyncio.wait_for(read(), 10)
            payload = json.loads(raw)
        except asyncio.TimeoutError:
            raise HTTPException(408, "Approval response timed out.") from None
        except (ValueError, RecursionError):
            raise HTTPException(400, "Invalid approval response.") from None
        if not isinstance(payload, dict) or set(payload) != {"response"} or not isinstance(payload["response"], dict):
            raise HTTPException(400, "Provide an approval response.")
        response = payload["response"]
        if set(response) != {"decision"} or not isinstance(response["decision"], str) or response["decision"] not in {"accept", "decline", "cancel"}:
            raise HTTPException(400, "Invalid approval decision.")
        async with api.session_lifecycle_lock(session_id):
            api.ensure_session_not_deleting(session_id)
            row = PERMISSIONS.get(interaction_id)
            if (not row or row["public"]["session_id"] != session_id or row["future"].done()
                    or not owns(api, session_id, row["run_id"])):
                raise HTTPException(409, "This Gemini approval is no longer active.")
            choice = {"accept": "allow_once", "decline": "reject_once", "cancel": "cancelled"}[response["decision"]]
            if choice != "cancelled" and choice not in row["options"]:
                raise HTTPException(400, "This decision is unavailable for the tool.")
            row["future"].set_result(choice)
            return {"interaction": row["public"]}

    return routes


async def run(api, session_id: str, run_id: str, prompt: str, sess: dict,
              manifest_path: Path, *, standalone_provider_context=False, **_kwargs):
    """A new owned process per turn, resuming the same durable native UUID."""
    from cursor_provider_mcp import CursorToolBroker

    text_parts: list[str] = []
    block: list[str] = []
    block_kind = ""
    block_index = 0
    char_count = 0
    tool_calls: dict[str, dict] = {}
    finished_tools: set[str] = set()
    broker = None
    error = None
    stopped = False
    owner = secrets.token_urlsafe(48)
    completed_reasoning = set()
    runtime_env = api.validate_provider_runtime_env(_kwargs.get("provider_runtime_env"))
    helper_redactor = api.opencode_helper_output_redactor(session_id, run_id, runtime_env)
    private_values = [owner]

    def redact(value):
        if isinstance(value, dict): return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list): return [redact(item) for item in value]
        if not isinstance(value, str): return value
        text = helper_redactor(value)
        for private in private_values:
            if private: text = text.replace(private, "<provider-private>")
        return text

    async def flush():
        nonlocal block, block_kind, block_index
        if not block or not await api.turn_slot_is_owned(session_id, run_id) or session_id not in api.STORE.sessions:
            return
        text = redact("".join(block))
        block = []
        item_id = f"gemini-{run_id}-{block_index}"
        block_index += 1
        payload = {"run_id": run_id, "backend": "gemini", "text": text, "item_id": item_id,
                   **api.run_event_metadata(run_id)}
        if block_kind == "text":
            text_parts.append(text)
            await api.append_event(session_id, "assistant_text", payload)
        else:
            await api.persist_reasoning_summary(session_id, {**payload, "phase": "summary"}, completed_reasoning)

    async def update(event):
        nonlocal block_kind, char_count
        if not owns(api, session_id, run_id):
            return
        kind = event["type"]
        if kind in {"text", "reasoning"}:
            if kind != block_kind:
                await flush()
                block_kind = kind
            text = redact(event.get("text") or "")
            char_count += len(text)
            if char_count > 2 * 1024 * 1024:
                raise GeminiProtocolError("Gemini output exceeded the per-turn limit.")
            block.append(text)
            if kind == "reasoning":
                await api.update_reasoning_summary_stream(session_id, run_id,
                    f"gemini-{run_id}-{block_index}", {"delta": text},
                    {**api.run_event_metadata(run_id), "backend": "gemini"})
        elif kind == "tool":
            await flush()
            call_id = event["call_id"]
            if call_id in finished_tools:
                return
            if call_id not in tool_calls:
                if len(tool_calls) + len(finished_tools) >= 4096:
                    raise GeminiProtocolError("Gemini exceeded the per-turn tool limit.")
                tool_calls[call_id] = {"id": call_id, "name": redact(event.get("title") or event.get("kind") or "Tool")[:512],
                    "input": redact(api.bounded_codex_interaction_value(event.get("input") or {}))}
                await api.append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool_calls[call_id]})
            if event.get("status") in {"completed", "failed"}:
                content = event.get("output")
                if content is None:
                    content = "\n".join(text_content(item.get("content")) for item in (event.get("content") or [])
                                        if isinstance(item, dict) and item.get("type") == "content")
                await api.append_event(session_id, "tool_finished", {"run_id": run_id,
                    "tool": {**tool_calls.pop(call_id), "output": redact(str(content))[:24000],
                             "is_error": event.get("status") == "failed"}})
                finished_tools.add(call_id)

    async def permission(params):
        return await ask_permission(api, session_id, run_id, redact(params))

    client = GeminiACPClient(on_update=update, on_permission=permission)
    try:
        cwd = str(Path(str(sess.get("cwd") or api.DEFAULT_CWD)).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise GeminiConfigurationError("The Gemini workspace must be an existing directory.")
        provider_id = None if standalone_provider_context else sess.get("gemini_session_id") or sess.get("session_id")
        if provider_id and sess.get("gemini_session_cwd") != cwd:
            raise GeminiConfigurationError("Resume Gemini in the original workspace; create a new chat to change its directory.")
        connection = api.PROVIDER_CONNECTION_STORE.for_session(sess)
        if connection: private_values.append(connection["api_key"])
        profile = api.PROVIDER_CONNECTION_STORE.root / ("gemini-runtime-" + connection["credential_id"]) if connection else None
        env = build_environment(api.runner_env(), cwd=cwd, api_home=profile,
            api_key=connection.get("api_key"), base_url=connection.get("base_url"))
        selected_skill = _kwargs.get("provider_command")
        if selected_skill is not None:
            # Recheck immediately before launching, including after queueing.
            snapshot, inventory = await api.discover_session_provider_commands(session_id, sess)
            current = inventory.resolve(selected_skill.public["id"])
            if snapshot["support"].get("available") is not True or current != selected_skill:
                raise GeminiConfigurationError("The selected Gemini skill changed. Refresh skills and select it again.")
            invocation = selected_skill.public["invocation"]
            task = prompt[len(invocation):].lstrip() if api.provider_command_prompt_matches(prompt, invocation) else prompt
            prompt = ("The user explicitly selected the installed Gemini skill "
                + json.dumps(selected_skill.native["name"]) + ". Use the native activate_skill tool to activate it, "
                "then follow its instructions for the task below. If activation is unavailable or denied, "
                "report that instead of pretending the skill ran. This selection grants no additional tool or chat permissions.\n\n"
                + task)
        executable = sess.get("_gemini_executable") or resolve_executable(env)
        if not executable:
            raise GeminiConfigurationError("Gemini CLI is not installed on the server.")
        mcp = []
        command = [str(executable), "--acp"]
        async with api.CROSS_CHAT_CAPABILITY_LOCK:
            has_authority = any(c.get("source_session_id") == session_id and c.get("source_run_id") == run_id
                                for c in api.CROSS_CHAT_CAPABILITIES.values())
        if has_authority:
            async def execute(value, key):
                try:
                    return await api.execute_provider_tool_once(session_id, run_id, value,
                        replay_key="gemini:" + key, backend="gemini", cursor_owner_token=owner)
                except api.ProviderToolError as exc:
                    return str(exc), True
            broker = CursorToolBroker(api.codex_provider_mcp_tool_definition(), execute)
            tool_env = await broker.start()
            from cursor_provider_mcp import ENV_SECRET
            private_values.append(tool_env[ENV_SECRET])
            name = "agentsdock_" + secrets.token_hex(24)
            mcp = [{"name": name, "command": sys.executable,
                    "args": [str(Path(__file__).with_name("cursor_provider_mcp.py")), "--mcp"],
                    "env": [{"name": key, "value": value} for key, value in tool_env.items()]}]
            # Preserve Gemini's native policy hierarchy. In particular, a CLI
            # --policy allow could outrank the user's existing deny rule.
            # Native permission requests go through the explicit approval UI;
            # the broker separately enforces AgentsDock's run/task authority.
            prompt += f"\nUse the run-bound AgentsDock MCP tool from {name} for authorized chat/mail operations; do not use shell helpers."
        if sess.get("model"):
            command += ["--model", str(sess["model"])]

        async def started(proc):
            bound, stop = await api.bind_active_turn(session_id, run_id, {
                "proc": proc, "run_id": run_id, "backend": "gemini", "transport": "exec",
                "pid": proc.pid, "pgid": proc.pid, "cwd": cwd, "argv": [str(executable), "--acp"],
                "started_at": time.time(), "started_at_iso": api.now_iso(),
                "gemini_mcp_owner_token": owner, "provider_turn_ready": False,
                "provider_tools_ready": asyncio.Event(),
            })
            if not bound or stop:
                raise asyncio.CancelledError()

        await client.start(command, cwd=cwd, env=env, on_started=started)
        from gemini_history import replay_snapshot
        replay = await asyncio.to_thread(replay_snapshot, env, cwd, provider_id) if provider_id else None
        opened = await client.open_session(cwd=cwd, session_id=provider_id, mcp_servers=mcp, replay=replay)
        if provider_id and await asyncio.to_thread(replay_snapshot, env, cwd, provider_id) != replay:
            raise GeminiProtocolError("Gemini history changed during resume; close the other client before retrying.")
        if not connection:
            MODEL_CATALOG["default"] = session_models(opened)
        await api.persist_run_provider_session(session_id, run_id, "gemini", client.session_id,
            cwd=cwd, standalone_provider_context=standalone_provider_context)
        await api.mark_provider_turn_ready(session_id, run_id, client.session_id)
        async with api.ACTIVE_LOCK:
            active = api.ACTIVE.get(session_id) or {}
            if active.get("run_id") == run_id:
                active["provider_tools_ready"].set()
        if not owns(api, session_id, run_id):
            raise asyncio.CancelledError()
        result = await client.prompt([{"type": "text", "text": prompt + api.session_prompt_addendum(sess)}])
        stopped = result.get("stopReason") == "cancelled"
        if result.get("stopReason") not in {"end_turn", "cancelled"}:
            raise GeminiProtocolError("Gemini stopped before completing the turn (limit or refusal).")
        if not stopped:
            api.record_runtime_success("gemini")
    except asyncio.CancelledError:
        stopped = True
    except (GeminiConfigurationError, GeminiProtocolError) as exc:
        error = str(exc)
    except Exception:
        error = "Gemini could not complete this request. Check the server's Gemini installation and selected connection."
    finally:
        stopped = stopped or run_id in api.STOPPED_RUNS
        # Flush exposed partial output even on interruption before retiring its
        # owner; do not publish raw stderr or protocol payloads.
        with suppress(Exception):
            await flush()
        async def cleanup():
            await client.close()
            if broker:
                await broker.close()
        try:
            await api.join_task_despite_caller_cancellation(asyncio.create_task(cleanup()))
        except Exception:
            # Do not release the slot or report a successful terminal event if
            # the owned provider process could not be reaped.
            if await api.turn_slot_is_owned(session_id, run_id):
                await api.append_event(session_id, "error", {"run_id": run_id, "backend": "gemini",
                    "message": "Gemini cleanup did not complete. Stop this chat before retrying."})
            return
        for call_id, tool in tool_calls.items():
            if session_id in api.STORE.sessions and await api.turn_slot_is_owned(session_id, run_id):
                await api.append_event(session_id, "tool_finished", {"run_id": run_id,
                    "tool": {**tool, "output": "Gemini tool did not complete.", "is_error": True}})
        if error and not stopped and await api.turn_slot_is_owned(session_id, run_id):
            await api.append_event(session_id, "error", {"run_id": run_id, "backend": "gemini", "message": error})
        await api.clear_active_process(session_id, expected_run_id=run_id)
        await api.finalize_owned_turn_finished(session_id, run_id, stopped=stopped, payload={
            "run_id": run_id, "backend": "gemini", "exit_code": 1 if error else 0,
            "is_error": bool(error and not stopped), "stopped": stopped,
            "result_text": "\n\n".join(text_parts), **api.run_event_metadata(run_id),
        })
