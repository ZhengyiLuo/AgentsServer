"""Real loopback HTTP against the complete application, with disposable state.

Run with unittest from server/. The child imports the production app only after
its state/config environment is isolated. Host-wide lifespan services and model
launches are deliberately off; provider ownership is a synthetic live run and
its credential is issued by the real native capability issuer. No live helper
authority, installed config, personal chat or provider credential is used.

The scheduled checks also use the real SDK MCP server over MCP's in-memory
protocol transport, its helper subprocess, and the loopback Jobs HTTP API.
The external route check separately exercises actual locked admission and
capability issuance, including revocation between dispatch and admission.
Native runtime probes, model execution, and SDK process ownership are fixtures;
this does not claim a live Claude model or host scheduler-lifespan acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class ExternalChatHTTPIntegrationTests(unittest.TestCase):
    def test_actual_http_auth_mailbox_provenance_reply_and_revocation(self):
        with tempfile.TemporaryDirectory(prefix="external-chat-http-") as temporary:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHBOT_", "ZENITHDOCK_"))}
            env.update({
                "AGENTSDOCK_STATE_DIR": temporary,
                "AGENTS_SERVER_STATE_DIR": temporary,
                "AGENTS_SERVER_CONFIG_DIR": temporary,
                "AGENTSDOCK_AGENT_CWD": temporary,
                "AGENTSDOCK_AGENT_TOKEN": "synthetic-admin-" + "a" * 40,
                "AGENTSDOCK_TEAM_HUB_MODE": "disabled",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            })
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--isolated-child"],
                                       cwd=temporary, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr[-8000:])
            receipt = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(receipt["http"], "real-loopback-tcp")
            self.assertEqual(receipt["provider"], "native-issuer-with-synthetic-live-run")
            self.assertEqual(receipt["checks"], 22)
            self.assertEqual(receipt["scheduled_mcp"], "real-sdk-mcp-helper-http")
            self.assertEqual(receipt["scheduled_external"], "provider-http-create-update-dispatch-mailbox")


async def _scheduled_external_admission_check(s, root, peer, save_policy, job_id):
    """Keep actual locked admission/issuance; stub only native runtime work."""
    import external_chat_peers as peers

    target = peer["target_session_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    tasks, capabilities = {}, {}
    race_policy_change = False

    async def block_model(*args, **kwargs):
        entered.set()
        await release.wait()

    async def admit(chat, req, **kwargs):
        assert req.purpose == "scheduled_job"
        assert kwargs["scheduled_job_chat_references"] is True
        assert s.initial_provider_cross_chat_route_snapshot(chat, req, "chat") == []
        if race_policy_change:
            # JobStore has already accepted the old binding for dispatch.
            # Locked admission must independently reject this changed policy.
            peer["enabled"] = False
            save_policy()
        # Older saved jobs may lack the desktop mailbox capability flag;
        # scheduled admission must still issue async_route_v1 authority.
        req.client_capabilities = [flag for flag in req.client_capabilities
                                   if flag != s.ASYNC_ROUTE_V1_CLIENT_CAPABILITY]
        return await s._start_turn_locked(chat, req, **kwargs)

    model = AsyncMock(side_effect=block_model)
    runtime_patches = (
        patch.object(s, "ACTIVE", {}),
        patch.object(s, "BUSY_SESSIONS", set()),
        patch.object(s, "CURRENT_TURNS", {}),
        patch.object(s, "QUEUED_TURNS", {}),
        patch.object(s, "SERVER_MAINTENANCE_SESSIONS", set()),
        patch.object(s, "SESSION_TURN_TASKS", tasks),
        patch.object(s, "RUN_METADATA", {}),
        patch.object(s, "CROSS_CHAT_CAPABILITIES", capabilities),
        patch.object(s, "AGENT_AMBIENT_LOCAL_HANDOFFS_ENABLED", True),
        patch.object(s, "managed_server_update_admission_blocker", return_value=None),
        patch.object(s, "managed_server_update_scheduled_job_blocker", return_value=None),
        patch.object(s, "turn_start_blocker", AsyncMock(return_value=None)),
        patch.object(s, "ensure_runtime_available", AsyncMock(return_value={})),
        patch.object(s, "resolve_provider_command_selection", AsyncMock(return_value=None)),
        patch.object(s, "build_turn_provider_prompt", return_value="Synthetic scheduled provider prompt"),
        patch.object(s, "codex_manifest_path", return_value=root / "scheduled-admission-manifest.json"),
        patch.object(s, "scrub_tmux_global_secret_environment", return_value=None),
        patch.object(s, "reconcile_provider_task_exit", AsyncMock()),
        patch.object(s, "run_claude", model),
        patch.object(s, "start_turn", side_effect=admit),
    )
    with ExitStack() as stack:
        for runtime_patch in runtime_patches:
            stack.enter_context(runtime_patch)
        result = await s.JOBS.run_job(job_id)
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert result["queued"] is False
            assert len(capabilities) == 1
            capability = next(iter(capabilities.values()))
            assert capability["source_run_id"] == result["run_id"]
            assert capability["provider_route_grants"] == {peer["route_id"]: peers.route(peer)}
            assert capability["async_route_v1"] is True
            assert capability["reciprocal_mint_allowed"] is False
            assert capability["provider_direct_grants"] == {} and capability["grants"] == set()
            assert "agent_cross_chat_routes" in capability["actions"]
            assert model.await_args.kwargs["interactive_agent_sdk"] is True
            assert result["event"]["chat_references"][0]["target_pair_id"] == peer["pair_id"]
            assert result["event"]["provider_cross_chat_route_snapshot"] == []
        finally:
            release.set()
            await asyncio.gather(*list(tasks.get(target, set())))
            await s.revoke_cross_chat_capability(result["run_id"])
            s.BUSY_SESSIONS.discard(target)
            s.CURRENT_TURNS.pop(target, None)

        race_policy_change = True
        try:
            try:
                await s.JOBS.run_job(job_id)
            except s.ScheduledJobChatReferenceRepairRequired:
                pass
            else:
                raise AssertionError("policy change between dispatch and admission launched provider")
            assert model.await_count == 1 and capabilities == {}
            assert not tasks.get(target)
            assert (await s.JOBS.snapshot(job_id))["enabled"] is False
        finally:
            peer["enabled"] = True
            save_policy()


async def _scheduled_external_route_check(s, root, http):
    """Exercise durable external job bindings without a live model or scheduler."""
    import external_chat_peers as peers

    target, other = "sess_external_job_synthetic", "sess_external_job_other"
    secret = "synthetic-scheduled-secret-" + "c" * 40
    peer = {
        "peer_id": "extpeer_" + "6" * 32, "display_name": "Scheduled Duet",
        "target_session_id": target, "credential_sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "revision": "rev_" + "7" * 32, "pair_id": "pair_" + "8" * 32,
        "route_id": "route_" + "9" * 32, "inbound_route_id": "route_" + "a" * 32,
        "enabled": True,
        "policy": {"approval_id": "synthetic-scheduled-owner-approval", "approved_at": "2026-11-06T00:00:00Z",
                   "delegated_by": "Synthetic owner", "source_user_instruction":
                   "Synthetic fixture only: send scheduled synthetic updates to the paired external mailbox."},
    }
    unselected_peer = {
        **peer, "peer_id": "extpeer_" + "f" * 32, "display_name": "Unselected external peer",
        "credential_sha256": hashlib.sha256(b"synthetic-unselected-credential").hexdigest(),
        "revision": "rev_" + "0" * 32, "pair_id": "pair_" + "1" * 32,
        "route_id": "route_" + "2" * 32, "inbound_route_id": "route_" + "3" * 32,
    }
    config = root / "external-chat-peers.json"

    def save_policy():
        config.write_text(json.dumps({"version": 1, "peers": [peer, unselected_peer]}))
        config.chmod(0o600)

    save_policy()
    for chat in (target, other):
        s.ensure_dirs(chat)
        s.STORE.sessions[chat] = {
            "id": chat, "title": "Synthetic external scheduled owner", "backend": "claude",
            "cwd": str(root), "created_at": s.now_iso(), "provider_jobs_access": "full",
        }
    await s.STORE.save()
    active_runs = []

    async def activate(chat, run, routes, *, ordinary=True):
        previous = s.CURRENT_TURNS.get(chat, {}).get("run_id")
        if previous:
            await s.revoke_cross_chat_capability(previous)
        s.CURRENT_TURNS[chat] = {"run_id": run}
        s.ACTIVE[chat] = {"run_id": run, "backend": "claude", "stop_requested": False}
        s.BUSY_SESSIONS.add(chat)
        authority = await s.issue_cross_chat_capability(
            chat, run, [], actions={"jobs", "agent_cross_chat_routes"},
            provider_route_snapshot=routes, async_route_v1=True,
            reciprocal_mint_allowed=ordinary,
        )
        active_runs.append(run)
        return {"x-agentsdock-provider-capability": json.loads(authority.read_text())["provider_capability"]}

    async def request(method, path, status, **kwargs):
        response = await http.request(method, path, **kwargs)
        assert response.status_code == status, (method, path, response.status_code, response.text[:500])
        return response.json()

    def assert_saved_binding(job):
        refs = job["chat_references"]
        assert len(refs) == 1
        reference = refs[0]
        for key, expected in {
            "session_id": peer["peer_id"], "target_kind": "external_peer",
            "action": "route", "route_action": "instruction",
            "target_route_id": peer["route_id"], "target_route_revision": peer["revision"],
            "target_pair_id": peer["pair_id"], "target_paired_route_id": peer["inbound_route_id"],
        }.items():
            assert reference[key] == expected, (key, reference.get(key))
        assert reference.get("target_server_identity") is None
        assert reference.get("target_connection_id") is None
        start, end = reference["source_text_start"], reference["source_text_end"]
        assert job["prompt"][start:end] == "@Scheduled Duet"
        return refs

    def assert_no_saved_route(chat, prompt, refs):
        try:
            s.validate_scheduled_job_chat_references(chat, prompt, refs)
        except s.ScheduledJobChatReferenceRepairRequired:
            pass
        else:
            raise AssertionError("stale or other-source external scheduled binding was accepted")
        parsed = [s.ChatReference.model_validate(ref) for ref in refs]
        assert s.provider_cross_chat_route_snapshot_for_authority(
            [], parsed, source_session_id=chat, per_job_reference_routes=True,
        ) == []

    try:
        headers = await activate(target, "run_external_job_create", [peers.route(peer)])
        path = f"/api/agent/sessions/{target}/jobs"
        fields = {
            "title": "Synthetic external schedule", "prompt": "Send a synthetic update to @Scheduled Duet today.",
            "schedule_kind": "interval", "interval_seconds": 3600, "loop": True,
            "chat_routes": [{"route_id": peer["route_id"], "action": "instruction"}],
        }
        await request("POST", path, 403, headers=headers,
                      json={**fields, "chat_routes": [{"route_id": peer["route_id"], "action": "request_reply"}]})
        await request("POST", path, 409, headers=headers,
                      json={**fields, "chat_routes": [{"route_id": unselected_peer["route_id"], "action": "instruction"}]})
        # A missing exact mention must not consume the one-conversion grant.
        await request("POST", path, 400, headers=headers, json={**fields, "prompt": "Missing mention"})
        public = (await request("POST", path, 200, headers=headers, json=fields))["job"]
        job_id = public["id"]
        assert public["chat_target_count"] == 1 and public["chat_targets"][0]["display_title_snapshot"] == "Scheduled Duet"
        rendered = json.dumps(public)
        assert all(private not in rendered for private in (peer["peer_id"], peer["pair_id"], peer["revision"], peer["inbound_route_id"]))
        assert "chat_references" not in public
        refs = assert_saved_binding(await s.JOBS.snapshot(job_id))
        # Saving the schedule never contacts the external peer.
        mailbox = await request("GET", "/api/external-chat/replies", 200, headers={"Authorization": "Bearer " + secret})
        assert mailbox["messages"] == []
        # Provider-written internal reference fields cannot replace route selection.
        await request("POST", path, 403, headers=headers,
                      json={**fields, "chat_references": refs})
        await request("POST", path, 403, headers=headers, json=fields)

        headers = await activate(target, "run_external_job_update", [peers.route(peer)])
        updated_prompt = "Send a revised synthetic update to @Scheduled Duet today."
        await request("PATCH", f"{path}/{job_id}", 200, headers=headers,
                      json={"prompt": updated_prompt, "chat_routes": fields["chat_routes"]})
        job = await s.JOBS.snapshot(job_id)
        refs = assert_saved_binding(job)
        assert job["prompt"] == updated_prompt
        parsed = [s.ChatReference.model_validate(ref) for ref in refs]
        try:
            s.validate_chat_references(target, updated_prompt, parsed, [s.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY])
        except s.HTTPException:
            pass
        else:
            raise AssertionError("ordinary turn admitted server-only external scheduled reference")
        for secure_field in ("target_server_identity", "target_connection_id"):
            try:
                s.ChatReference.model_validate({**refs[0], secure_field: "forged-secure-field"})
            except ValueError:
                pass
            else:
                raise AssertionError("external reference accepted secure-peer field")
        for binding_field, prefix in (("target_pair_id", "pair_"), ("target_paired_route_id", "route_")):
            assert_no_saved_route(target, updated_prompt, [{**refs[0], binding_field: prefix + "f" * 32}])

        observed = []

        async def admit(chat, req, **kwargs):
            assert chat == target and req.purpose == "scheduled_job"
            assert kwargs["scheduled_job_chat_references"] is True
            assert kwargs["provider_context_mode"] == "chat"
            assert s.initial_provider_cross_chat_route_snapshot(chat, req, "chat") == []
            snapshot = s.provider_cross_chat_route_snapshot_for_authority(
                [], req.chat_references, source_session_id=chat, per_job_reference_routes=True,
            )
            assert snapshot == [peers.route(peer)], snapshot
            run_id = "run_external_job_dispatch_" + str(len(observed))
            scheduled_headers = await activate(chat, run_id, snapshot, ordinary=False)
            routes = await request("GET", "/api/agent/cross-chat/routes", 200, headers=scheduled_headers)
            assert [route["route_id"] for route in routes["routes"]] == [peer["route_id"]]
            handoff = await request(
                "POST", f'/api/agent/cross-chat/routes/{peer["route_id"]}/handoffs', 200,
                headers=scheduled_headers,
                json={"mode": "async_route_v1", "action": "instruction", "body": "Synthetic scheduled update",
                      "idempotency_key": "scheduled-native-update-" + str(len(observed))},
            )
            record = await s.CROSS_CHAT.get(handoff["message_id"])
            assert record["source_session_id"] == target and record["target_session_id"] == peer["peer_id"]
            # The scheduled grant can deliver, but cannot spawn another durable schedule.
            await request("POST", path, 403, headers=scheduled_headers,
                          json={**fields, "chat_routes": [{"route_id": peer["route_id"], "action": "instruction"}]})
            observed.append(handoff["message_id"])
            return {"run_id": run_id}

        with patch.object(s, "start_turn", side_effect=admit):
            await s.JOBS.run_job(job_id)
        mailbox = await request("GET", "/api/external-chat/replies", 200, headers={"Authorization": "Bearer " + secret})
        assert [message["message_id"] for message in mailbox["messages"]] == observed
        assert mailbox["messages"][0]["text"] == "Synthetic scheduled update"

        await _scheduled_external_admission_check(s, root, peer, save_policy, job_id)

        # Exact peer identity alone is insufficient: another source owns no route.
        assert_no_saved_route(other, updated_prompt, refs)
        other_headers = await activate(other, "run_external_job_other", [peers.route(peer)])
        await request("POST", f"/api/agent/sessions/{other}/jobs", 409, headers=other_headers, json=fields)

        peer["enabled"] = False
        save_policy()
        assert_no_saved_route(target, updated_prompt, refs)
        with patch.object(s, "start_turn", side_effect=AssertionError("revoked schedule admitted a turn")):
            try:
                await s.JOBS.run_job(job_id)
            except s.ScheduledJobChatReferenceRepairRequired:
                pass
            else:
                raise AssertionError("revoked schedule dispatched")
        assert (await s.JOBS.snapshot(job_id))["enabled"] is False

        # A regrant must not silently refresh an old job's binding.
        peer.update({"enabled": True, "revision": "rev_" + "b" * 32,
                     "pair_id": "pair_" + "c" * 32, "route_id": "route_" + "d" * 32,
                     "inbound_route_id": "route_" + "e" * 32})
        save_policy()
        assert_no_saved_route(target, updated_prompt, refs)
        with patch.object(s, "start_turn", side_effect=AssertionError("regrant silently refreshed old job")):
            try:
                await s.JOBS.run_job(job_id)
            except s.ScheduledJobChatReferenceRepairRequired:
                pass
            else:
                raise AssertionError("regranted old schedule dispatched")
        headers = await activate(target, "run_external_job_repair", [peers.route(peer)])
        await request("PATCH", f"{path}/{job_id}", 200, headers=headers,
                      json={"enabled": True, "chat_routes": [{"route_id": peer["route_id"], "action": "instruction"}]})
        assert_saved_binding(await s.JOBS.snapshot(job_id))
        with patch.object(s, "start_turn", side_effect=admit):
            await s.JOBS.run_job(job_id)
        assert len(observed) == 2
        mailbox = await request("GET", "/api/external-chat/replies", 200, headers={"Authorization": "Bearer " + secret})
        assert [message["message_id"] for message in mailbox["messages"]] == [observed[-1]]
        return "provider-http-create-update-dispatch-mailbox"
    finally:
        for run in active_runs:
            await s.revoke_cross_chat_capability(run)
        for chat in (target, other):
            s.ACTIVE.pop(chat, None)
            s.CURRENT_TURNS.pop(chat, None)
            s.BUSY_SESSIONS.discard(chat)


async def _scheduled_mcp_check(s, root):
    from mcp.shared.memory import create_connected_server_and_client_session

    target, run_id, job_id = "sess_scheduled_synthetic", "run_scheduled_synthetic", "job_synthetic"
    owner = "synthetic-sdk-owner"
    s.ensure_dirs(target)
    session = {"id": target, "title": "Synthetic scheduled provider", "backend": "claude",
               "cwd": str(root), "provider_jobs_access": "read_only", "claude_permission_mode": "default"}
    s.STORE.sessions[target] = session
    s.JOBS.jobs[job_id] = {
        "id": job_id, "session_id": target, "title": "Synthetic provider check",
        "prompt": "List only this disposable chat's jobs.", "backend": "claude",
        "context_mode": "chat", "schedule_kind": "interval", "interval_seconds": 3600,
        "enabled": True, "run_count": 0,
    }
    ready = asyncio.Event()
    ready.set()
    s.CURRENT_TURNS[target] = {"run_id": run_id}
    s.ACTIVE[target] = {"run_id": run_id, "backend": "claude", "transport": s.CLAUDE_TRANSPORT_AGENT_SDK,
                        "claude_sdk_owner_token": owner, "claude_permission_run_id": run_id,
                        "claude_permissions_open": True, "provider_tools_ready": ready}
    s.BUSY_SESSIONS.add(target)
    authority = await s.issue_cross_chat_capability(target, run_id, [], actions={"jobs"})
    await s.provider_authority_runtime_env(run_id, authority, target, [])
    ownership = SimpleNamespace(live=True)
    manager = SimpleNamespace(owns_active_run=lambda chat, token, run:
                              ownership.live and (chat, token, run) == (target, owner, run_id))
    observed = []

    async def sdk_protocol_bridge(chat, run, prompt, sess, manifest, **kwargs):
        # Build production unattended options and its real SDK MCP registration;
        # no Claude process starts or reads native settings/credentials.
        options, _key, _cli = s.build_claude_sdk_options(chat, sess, str(root), manifest, unattended=True)
        assert options.permission_mode == "default"
        assert options.disallowed_tools == ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]
        options._agentsdock_bind_provider_tool_owner(owner, run)
        instance = options.mcp_servers[s.CLAUDE_PROVIDER_MCP_SERVER_NAME]["instance"]
        async with create_connected_server_and_client_session(instance) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["run"]
            assert tools.tools[0].inputSchema == s.PROVIDER_TOOL_INPUT_SCHEMA
            result = await client.call_tool("run", {"helper": "jobs", "arguments": ["list"]})
            assert not result.isError, result
            payload = json.loads(result.content[0].text)
            assert [job["id"] for job in payload["jobs"]] == [job_id]
            # The scheduled grant includes no chat routes; this otherwise
            # valid helper request must remain denied.
            route_result = await client.call_tool("run", {"helper": "chats", "arguments": ["list"]})
            assert route_result.isError
            ownership.live = False
            stale = await client.call_tool("run", {"helper": "jobs", "arguments": ["list"]})
            assert stale.isError and "stale" in stale.content[0].text
        observed.append("real-sdk-mcp-helper-http")

    async def admit(chat, req, **kwargs):
        assert req.purpose == "scheduled_job" and kwargs["provider_context_mode"] == "chat"
        assert req.chat_references == []
        assert s.initial_provider_cross_chat_route_snapshot(chat, req, "chat") == []
        await s.run_claude(
            chat, run_id, req.prompt, session, root / "manifest.json",
            interactive_agent_sdk=s.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY in req.client_capabilities,
            standalone_provider_context=False,
        )
        return {"run_id": run_id}

    async def forbidden_print(*args, **kwargs):
        raise AssertionError("chat-context scheduled job selected print transport")

    try:
        with (
            patch.object(s, "CLAUDE_TRANSPORT", s.CLAUDE_TRANSPORT_AGENT_SDK),
            patch.object(s, "CLAUDE_SDK_MANAGER", manager),
            patch.object(s, "claude_sdk_cli_path", return_value=sys.executable),
            patch.object(s, "start_turn", side_effect=admit),
            patch.object(s, "run_claude_sdk", side_effect=sdk_protocol_bridge),
            patch.object(s, "run_claude_print", side_effect=forbidden_print),
        ):
            await s.JOBS.run_job(job_id)
        assert observed == ["real-sdk-mcp-helper-http"]
        return observed[0]
    finally:
        await s.revoke_cross_chat_capability(run_id)
        s.ACTIVE.pop(target, None)
        s.CURRENT_TURNS.pop(target, None)
        s.BUSY_SESSIONS.discard(target)


async def _isolated_http_check():
    import httpx
    import uvicorn
    import agent_server as s
    import external_chat_peers as peers

    root = Path(os.environ["AGENTSDOCK_STATE_DIR"])
    assert s.STATE_DIR == root and s.CONFIG_ENV_FILE.parent == root
    target = "sess_http_synthetic"
    secret = "synthetic-external-secret-" + "b" * 40
    peer = {
        "peer_id": "extpeer_" + "1" * 32, "display_name": "Synthetic Duet",
        "target_session_id": target, "credential_sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "revision": "rev_" + "2" * 32, "pair_id": "pair_" + "3" * 32,
        "route_id": "route_" + "4" * 32, "inbound_route_id": "route_" + "5" * 32,
        "enabled": True,
        "policy": {"approval_id": "synthetic-test-owner-approval", "approved_at": "2026-11-06T00:00:00Z",
                   "delegated_by": "Synthetic owner", "source_user_instruction":
                   "Synthetic test only: receive a summary request and return its synthetic reply. No personal data."},
    }
    config = root / "external-chat-peers.json"
    def save_policy():
        config.write_text(json.dumps({"version": 1, "peers": [peer]}))
        config.chmod(0o600)
    save_policy()
    s.ensure_dirs(target)
    await s.CROSS_CHAT.initialize()
    s.STORE.sessions = {target: {"id": target, "title": "Synthetic Zenith", "backend": "claude",
                                "cwd": str(root), "created_at": s.now_iso(), "provider_jobs_access": "blocked"}}
    await s.STORE.save()
    # Disable only autonomous model launch; endpoint/ledger/auth/projection are
    # production implementations. A busy synthetic run owns the native grant.
    s.schedule_chat_mailbox_wake = lambda session_id: None
    run_id = "run_http_synthetic"
    s.CURRENT_TURNS[target] = {"run_id": run_id}
    s.ACTIVE[target] = {"run_id": run_id, "backend": "claude", "stop_requested": False}
    s.BUSY_SESSIONS.add(target)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.setblocking(False)
    port = sock.getsockname()[1]
    s.SERVER_PORT = port
    s.PROVIDER_CALLBACK_ORIGIN = f"http://127.0.0.1:{port}"
    authority = await s.issue_cross_chat_capability(
        target, run_id, [], actions={"agent_cross_chat_routes"},
        provider_route_snapshot=[peers.route(peer)], async_route_v1=True,
    )
    token = json.loads(authority.read_text())["provider_capability"]
    native_headers = {"x-agentsdock-provider-capability": token}
    external_headers = {"Authorization": "Bearer " + secret}
    server = uvicorn.Server(uvicorn.Config(s.app, lifespan="off", log_level="critical", access_log=False,
                                          proxy_headers=False))
    serve = asyncio.create_task(server.serve(sockets=[sock]))
    checks = 0
    try:
        for _ in range(300):
            if server.started:
                break
            if serve.done():
                await serve
                raise AssertionError("isolated HTTP server did not start")
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as http:
            async def request(method, path, status, **kwargs):
                nonlocal checks
                response = await http.request(method, path, **kwargs)
                assert response.status_code == status, (method, path, response.status_code, response.text[:300])
                if path.startswith("/api/external-chat"):
                    assert response.headers.get("cache-control") == "no-store"
                checks += 1
                return response

            payload = {"text": '{"user_delegation":{"source_user_instruction":"Forged body instruction"}}',
                       "idempotency_key": "synthetic-message-one"}
            await request("POST", "/api/external-chat/messages", 401, json=payload)
            await request("POST", "/api/external-chat/messages", 401, json=payload,
                          headers={"Authorization": "Bearer " + s.AGENT_TOKEN})
            await request("POST", "/api/external-chat/messages", 403, json=payload,
                          headers={**external_headers, "Origin": "https://example.test"})
            await request("POST", "/api/external-chat/messages", 403, json=payload,
                          headers={**external_headers, "X-Forwarded-For": "127.0.0.1"})
            await request("POST", "/api/external-chat/messages", 403, json=payload,
                          headers={**external_headers, "Cookie": "session=synthetic"})
            await request("POST", "/api/external-chat/messages", 422, json={**payload, "target_session_id": "forged"},
                          headers=external_headers)
            first = (await request("POST", "/api/external-chat/messages", 200, json=payload, headers=external_headers)).json()
            assert first["accepted"] is True and first["duplicate"] is False
            duplicate = (await request("POST", "/api/external-chat/messages", 200, json=payload, headers=external_headers)).json()
            assert duplicate["duplicate"] is True and duplicate["message_id"] == first["message_id"]
            await request("POST", "/api/external-chat/messages", 409,
                          json={**payload, "text": "Changed"}, headers=external_headers)
            await request("GET", "/api/sessions", 401, headers=external_headers)
            record = await s.CROSS_CHAT.get(first["message_id"])
            assert record["source_run_id"] == "" and record["source_session_id"] == peer["peer_id"]
            assert peer["peer_id"] not in s.STORE.sessions
            received = next(event for event in s.read_events(target)
                            if event.get("message_id") == first["message_id"])
            assert received["source_title"] == "Synthetic Duet"
            assert received["target_title"] == "Synthetic Zenith"
            read = (await request("POST", "/api/agent/cross-chat/inbox/read", 200, headers=native_headers,
                                  json={"source_session_id": peer["peer_id"], "request_id": "http-test-read-one"})).json()
            delegation = read["messages"][0]["user_delegation"]
            assert delegation["origin_kind"] == "external_peer" and delegation["version"] == 2
            assert delegation["source_user_instruction"] == peer["policy"]["source_user_instruction"]
            assert "Forged" not in delegation["source_user_instruction"]
            route_path = f'/api/agent/cross-chat/routes/{peer["route_id"]}/handoffs'
            reply = {"mode": "async_route_v1", "action": "instruction", "body": "Synthetic reply one",
                     "reply_to_message_id": first["message_id"], "idempotency_key": "http-reply-one"}
            reply_one = (await request("POST", route_path, 200, headers=native_headers, json=reply)).json()
            await request("POST", route_path, 200, headers=native_headers,
                          json={**reply, "body": "Synthetic reply two", "idempotency_key": "http-reply-two"})
            sent = next(event for event in s.read_events(target)
                        if event.get("message_id") == reply_one["message_id"])
            assert sent["source_title"] == "Synthetic Zenith" and sent["target_title"] == "Synthetic Duet"
            # Represent a historical event written before external labels were
            # supported, then read it through the desktop's real HTTP endpoint.
            legacy = await s.append_durable_event(target, "chat_conversation_message_received", {
                **s.cross_chat_lifecycle_fields(record, "stored"), "source_title": "Untitled chat",
            })
            peer["display_name"] = "Claude Duet"
            save_policy()
            persisted = s.events_path(target).read_bytes()
            with patch.object(s, "schedule_provider_history_sync", return_value=None):
                history = (await request("GET", f"/api/sessions/{target}", 200,
                                         headers={"x-agentsdock-token": s.AGENT_TOKEN})).json()
            projected = next(event for event in history["events"] if event["id"] == legacy["id"])
            assert projected["source_title"] == "Claude Duet"
            assert projected["source_session_id"] == peer["peer_id"]
            assert projected["conversation_id"] == peer["pair_id"]
            assert legacy["source_title"] == "Untitled chat"
            assert s.events_path(target).read_bytes() == persisted
            assert all(event["target_title"] == "Claude Duet" for event in history["events"]
                       if event.get("target_session_id") == peer["peer_id"])
            page = (await request("GET", "/api/external-chat/replies?limit=1", 200, headers=external_headers)).json()
            assert page["has_more"] is True
            assert page["messages"][0]["message_id"] == reply_one["message_id"]
            second = (await request("GET", "/api/external-chat/replies", 200, headers=external_headers,
                                    params={"since": page["next_cursor"], "limit": 1})).json()
            assert second["messages"][0]["text"] == "Synthetic reply two"
            assert second["has_more"] is False
            tail = (await request("GET", "/api/external-chat/replies", 200, headers=external_headers,
                                  params={"since": second["next_cursor"]})).json()
            assert tail["messages"] == [] and tail["next_cursor"] == second["next_cursor"]
            await request("GET", "/api/external-chat/replies?since=invalid", 400, headers=external_headers)
            s.ACTIVE.pop(target)
            s.CURRENT_TURNS.pop(target)
            s.BUSY_SESSIONS.discard(target)
            await request("POST", route_path, 403, headers=native_headers,
                          json={**reply, "idempotency_key": "http-stale-run"})
            previous = s.managed_server_update_blocks_work
            s.managed_server_update_blocks_work = lambda: True
            try:
                await request("POST", "/api/external-chat/messages", 409, headers=external_headers,
                              json={**payload, "idempotency_key": "maintenance-test"})
            finally:
                s.managed_server_update_blocks_work = previous
            peer["enabled"] = False
            save_policy()
            await request("GET", "/api/external-chat/replies", 401, headers=external_headers)
            await request("POST", "/api/external-chat/messages", 401, headers=external_headers,
                          json={**payload, "idempotency_key": "revoked-message"})
        scheduled = await _scheduled_mcp_check(s, root)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as http:
            scheduled_external = await _scheduled_external_route_check(s, root, http)
        print(json.dumps({"http": "real-loopback-tcp", "checks": checks, "scheduled_mcp": scheduled,
                          "scheduled_external": scheduled_external,
                          "provider": "native-issuer-with-synthetic-live-run", "state": "disposable"}))
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve, 10)
        await s.revoke_cross_chat_capability(run_id)
        sock.close()


if __name__ == "__main__":
    if sys.argv[-1:] == ["--isolated-child"]:
        asyncio.run(_isolated_http_check())
    else:
        unittest.main()
