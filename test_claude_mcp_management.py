import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import agent_server
from claude_sdk_client import ClaudeSDKControlTimeout


RAW_MCP_STATUS = {
    "mcpServers": [
        {
            "name": "dayone",
            "status": "failed",
            "error": "Bearer top-secret-token at https://private.example/path?key=secret",
            "scope": "user",
            "serverInfo": {
                "name": "https://private.example/server",
                "version": "Bearer top-secret-token",
            },
            "config": {
                "command": "/private/bin/dayone",
                "env": {"PRIVATE_TOKEN": "top-secret-token"},
                "headers": {"Authorization": "Bearer top-secret-token"},
            },
            "tools": [
                {
                    "name": "private-tool",
                    "description": "top-secret-description",
                }
            ],
        },
        {
            "name": "calendar",
            "status": "connected",
            "scope": "project",
            "serverInfo": {"name": "calendar-server", "version": "1.2.3"},
            "tools": [{"name": "events"}, {"name": "availability"}],
        },
        {
            "name": "future",
            "status": "future-provider-state",
            "scope": "secret-scope",
        },
    ]
}


class FakeMCPManager:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, object]] = []
        self.mutate_calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.error: Exception | None = None

    async def get_mcp_status(self, session_id: str, **kwargs: object):
        self.get_calls.append({"session_id": session_id, **kwargs})
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return RAW_MCP_STATUS, "claudemcp_exact-owner-generation"

    async def mutate_mcp_server(self, session_id: str, **kwargs: object):
        self.mutate_calls.append({"session_id": session_id, **kwargs})
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return RAW_MCP_STATUS, "claudemcp_exact-owner-generation"


class ClaudeMCPManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "claude-mcp-chat"
        self.session = {
            "id": self.session_id,
            "title": "MCP chat",
            "backend": agent_server.BACKEND_CLAUDE,
            "cwd": "/tmp",
        }
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_tasks = agent_server.SESSION_TURN_TASKS
        self.previous_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        self.previous_maintenance = agent_server.SERVER_MAINTENANCE_SESSIONS
        self.previous_deleting = agent_server.DELETING_SESSIONS
        self.previous_deleted = agent_server.DELETED_SESSION_TOMBSTONES
        agent_server.STORE.sessions = {self.session_id: self.session}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.CURRENT_TURNS = {}
        agent_server.SESSION_TURN_TASKS = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
        agent_server.SERVER_MAINTENANCE_SESSIONS = set()
        agent_server.DELETING_SESSIONS = set()
        agent_server.DELETED_SESSION_TOMBSTONES = set()

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.SESSION_TURN_TASKS = self.previous_tasks
        agent_server.SESSION_LIFECYCLE_LOCKS = self.previous_lifecycle_locks
        agent_server.SERVER_MAINTENANCE_SESSIONS = self.previous_maintenance
        agent_server.DELETING_SESSIONS = self.previous_deleting
        agent_server.DELETED_SESSION_TOMBSTONES = self.previous_deleted

    def mcp_patches(self, manager: FakeMCPManager):
        return (
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AGENT_SDK),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=True),
            patch.object(
                agent_server,
                "build_claude_sdk_options",
                return_value=({"cwd": "/tmp"}, "profile-a", "/usr/bin/claude"),
            ),
            patch.object(
                agent_server,
                "claude_sdk_manager",
                AsyncMock(return_value=manager),
            ),
            patch.object(agent_server, "managed_server_update_blocker", return_value=None),
        )

    async def call_with_patches(
        self,
        manager: FakeMCPManager,
        request: agent_server.ClaudeMCPControlRequest | None = None,
    ) -> dict[str, object]:
        patches = self.mcp_patches(manager)
        for selected in patches:
            selected.start()
        try:
            return await agent_server.manage_claude_mcp(self.session_id, request)
        finally:
            for selected in reversed(patches):
                selected.stop()

    async def test_get_returns_v1_allowlisted_snapshot_without_provider_secrets(self) -> None:
        manager = FakeMCPManager()

        response = await self.call_with_patches(manager)

        self.assertEqual(response["version"], 1)
        self.assertTrue(response["available"])
        self.assertEqual(response["transport"], "agent-sdk")
        self.assertEqual(response["generation"], "claudemcp_exact-owner-generation")
        self.assertTrue(response["session_loaded"])
        self.assertFalse(response["truncated"])
        self.assertIsNone(response["reason"])
        self.assertIsNone(response["action"])
        servers = {item["name"]: item for item in response["servers"]}
        self.assertEqual(
            servers["dayone"]["error"],
            "Claude could not connect to this MCP server.",
        )
        self.assertEqual(servers["dayone"]["tool_count"], 1)
        self.assertIsNone(servers["dayone"]["server_info"])
        self.assertEqual(servers["calendar"]["server_info"], {
            "name": "calendar-server",
            "version": "1.2.3",
        })
        self.assertEqual(servers["future"]["status"], "unknown")
        self.assertIsNone(servers["future"]["scope"])
        encoded = json.dumps(response)
        for secret in (
            "top-secret-token",
            "private.example",
            "/private/bin/dayone",
            "Authorization",
            "PRIVATE_TOKEN",
            "private-tool",
            "top-secret-description",
            "secret-scope",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(manager.get_calls, [{
            "session_id": self.session_id,
            "options": {"cwd": "/tmp"},
            "configuration_key": "profile-a",
        }])
        self.assertNotIn(self.session_id, agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_post_binds_action_to_required_exact_generation(self) -> None:
        manager = FakeMCPManager()
        request = agent_server.ClaudeMCPControlRequest(
            version=1,
            action="disable",
            server_name="calendar",
            expected_generation="claudemcp_exact-owner-generation",
        )

        response = await self.call_with_patches(manager, request)

        self.assertEqual(response["action"], {
            "type": "disable",
            "server_name": "calendar",
        })
        self.assertEqual(manager.mutate_calls, [{
            "session_id": self.session_id,
            "action": "disable",
            "server_name": "calendar",
            "expected_generation": "claudemcp_exact-owner-generation",
            "options": {"cwd": "/tmp"},
            "configuration_key": "profile-a",
        }])

    async def test_reconnect_all_is_one_server_side_control(self) -> None:
        manager = FakeMCPManager()
        request = agent_server.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            expected_generation="claudemcp_exact-owner-generation",
        )

        response = await self.call_with_patches(manager, request)

        self.assertEqual(response["action"], {
            "type": "reconnect_all",
            "server_name": None,
        })
        self.assertEqual(len(manager.mutate_calls), 1)
        self.assertIsNone(manager.mutate_calls[0]["server_name"])

    async def test_active_and_starting_turns_reject_status_before_sdk_access(self) -> None:
        for active_kind in ("busy", "active", "current", "starting"):
            with self.subTest(active_kind=active_kind):
                agent_server.ACTIVE.clear()
                agent_server.BUSY_SESSIONS.clear()
                agent_server.CURRENT_TURNS.clear()
                agent_server.SESSION_TURN_TASKS.clear()
                task: asyncio.Task[None] | None = None
                if active_kind == "busy":
                    agent_server.BUSY_SESSIONS.add(self.session_id)
                elif active_kind == "active":
                    agent_server.ACTIVE[self.session_id] = {"run_id": "run-1"}
                elif active_kind == "current":
                    agent_server.CURRENT_TURNS[self.session_id] = {"run_id": None}
                else:
                    task = asyncio.create_task(asyncio.Event().wait())
                    agent_server.SESSION_TURN_TASKS[self.session_id] = {task}
                manager = FakeMCPManager()
                try:
                    with self.assertRaises(agent_server.HTTPException) as raised:
                        await self.call_with_patches(manager)
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(
                        raised.exception.detail["code"],
                        "claude_mcp_turn_active",
                    )
                    self.assertFalse(manager.get_calls)
                finally:
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_operation_reserves_maintenance_until_bounded_sdk_call_settles(self) -> None:
        manager = FakeMCPManager()
        manager.release = asyncio.Event()
        patches = self.mcp_patches(manager)
        for selected in patches:
            selected.start()
        try:
            operation = asyncio.create_task(
                agent_server.manage_claude_mcp(self.session_id)
            )
            await asyncio.wait_for(manager.started.wait(), 1)
            self.assertIn(
                self.session_id,
                agent_server.SERVER_MAINTENANCE_SESSIONS,
            )
            # Turn admission and managed-update admission both inspect this
            # exact set under ACTIVE_LOCK before reserving work/restart.
            self.assertTrue(
                agent_server.BUSY_SESSIONS
                | agent_server.SERVER_MAINTENANCE_SESSIONS
            )
            manager.release.set()
            await asyncio.wait_for(operation, 1)
            self.assertNotIn(
                self.session_id,
                agent_server.SERVER_MAINTENANCE_SESSIONS,
            )
        finally:
            for selected in reversed(patches):
                selected.stop()

    async def test_print_and_missing_sdk_status_are_non_mutating_capability_responses(self) -> None:
        for transport, sdk_available, reason_code in (
            (agent_server.CLAUDE_TRANSPORT_PRINT, True, "claude_mcp_print_transport"),
            (agent_server.CLAUDE_TRANSPORT_AGENT_SDK, False, "claude_mcp_sdk_unavailable"),
        ):
            with self.subTest(transport=transport, sdk_available=sdk_available):
                with patch.object(agent_server, "CLAUDE_TRANSPORT", transport), patch.object(
                    agent_server,
                    "claude_sdk_dependency_available",
                    return_value=sdk_available,
                ):
                    response = await agent_server.manage_claude_mcp(self.session_id)
                self.assertFalse(response["available"])
                self.assertEqual(response["transport"], "print")
                self.assertIsNone(response["generation"])
                self.assertEqual(response["servers"], [])
                self.assertFalse(response["truncated"])
                self.assertEqual(response["reason"]["code"], reason_code)

    def test_projection_deduplicates_sorts_and_caps_server_rows(self) -> None:
        raw = {
            "mcpServers": [
                {"name": f"server-{index:03d}", "status": "connected"}
                for index in range(104, -1, -1)
            ]
            + [
                {
                    "name": "server-104",
                    "status": "failed",
                    "error": "duplicate must not win",
                }
            ]
        }

        response = agent_server.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        names = [item["name"] for item in response["servers"]]
        self.assertEqual(len(names), agent_server.CLAUDE_MCP_MAX_SERVERS)
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(names, sorted(names, key=lambda name: (name.casefold(), name)))
        self.assertTrue(response["truncated"])
        server_104 = next(
            item for item in response["servers"] if item["name"] == "server-104"
        )
        self.assertEqual(server_104["status"], "connected")

    def test_projection_rejects_spoofed_names_and_accepts_normal_unicode(self) -> None:
        accepted_names = [
            "Caf\u00e9",
            "join\u200cer",
            "join\u200der",
            "\u65e5\u5386",
            "\u062a\u0642\u0648\u064a\u0645",
        ]
        rejected_names = [
            " calendar ",
            "spoof\u202e",
            "control\u0085",
            "hidden\u200b",
            "Cafe\u0301",
        ]
        raw = {
            "mcpServers": [
                {"name": name, "status": "connected"}
                for name in accepted_names + rejected_names
            ]
        }

        response = agent_server.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        self.assertEqual(
            {item["name"] for item in response["servers"]},
            set(accepted_names),
        )
        self.assertTrue(response["truncated"])
        for value in rejected_names:
            self.assertNotIn(value, {item["name"] for item in response["servers"]})

        for metadata in rejected_names:
            with self.subTest(metadata=metadata):
                self.assertIsNone(
                    agent_server.claude_mcp_display_metadata(metadata, 240)
                )
        for metadata in accepted_names:
            with self.subTest(metadata=metadata):
                self.assertEqual(
                    agent_server.claude_mcp_display_metadata(metadata, 240),
                    metadata,
                )

    def test_projection_marks_duplicates_incomplete_and_never_clamps_tool_count(self) -> None:
        raw = {
            "mcpServers": [
                {
                    "name": "calendar",
                    "status": "connected",
                    "tools": [None] * (agent_server.CLAUDE_MCP_MAX_TOOL_COUNT + 1),
                },
                {"name": "calendar", "status": "failed"},
            ]
        }

        response = agent_server.claude_mcp_snapshot(
            raw,
            generation="claudemcp_generation",
        )

        self.assertTrue(response["truncated"])
        self.assertEqual(len(response["servers"]), 1)
        self.assertEqual(response["servers"][0]["status"], "connected")
        self.assertIsNone(response["servers"][0]["tool_count"])

    async def test_noncanonical_request_names_are_rejected_before_manager_control(self) -> None:
        manager = FakeMCPManager()
        for server_name in (
            " calendar ",
            "spoof\u202e",
            "control\u0085",
            "hidden\u200b",
            "Cafe\u0301",
        ):
            with self.subTest(server_name=server_name):
                request = agent_server.ClaudeMCPControlRequest(
                    version=1,
                    action="disable",
                    server_name=server_name,
                    expected_generation="generation",
                )
                with self.assertRaises(agent_server.HTTPException) as raised:
                    await self.call_with_patches(manager, request)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(
                    raised.exception.detail["code"],
                    "claude_mcp_invalid_request",
                )

        reconnect_all = agent_server.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            server_name="",
            expected_generation="generation",
        )
        with self.assertRaises(agent_server.HTTPException) as raised:
            await self.call_with_patches(manager, reconnect_all)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(manager.mutate_calls)

    async def test_mutation_unavailable_wrong_backend_and_invalid_shape_fail_closed(self) -> None:
        request = agent_server.ClaudeMCPControlRequest(
            version=1,
            action="enable",
            server_name="calendar",
            expected_generation="generation",
        )
        with patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT), patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ):
            with self.assertRaises(agent_server.HTTPException) as unavailable:
                await agent_server.manage_claude_mcp(self.session_id, request)
        self.assertEqual(unavailable.exception.status_code, 409)
        self.assertEqual(unavailable.exception.detail["code"], "claude_mcp_unavailable")

        self.session["backend"] = agent_server.BACKEND_CODEX
        with self.assertRaises(agent_server.HTTPException) as wrong_backend:
            await agent_server.manage_claude_mcp(self.session_id)
        self.assertEqual(wrong_backend.exception.status_code, 409)
        self.assertEqual(wrong_backend.exception.detail["code"], "claude_mcp_wrong_backend")
        self.session["backend"] = agent_server.BACKEND_CLAUDE

        manager = FakeMCPManager()
        invalid = agent_server.ClaudeMCPControlRequest(
            version=1,
            action="reconnect_all",
            server_name="calendar",
            expected_generation="generation",
        )
        with self.assertRaises(agent_server.HTTPException) as invalid_request:
            await self.call_with_patches(manager, invalid)
        self.assertEqual(invalid_request.exception.status_code, 400)
        self.assertFalse(manager.mutate_calls)

    async def test_sdk_failure_response_never_echoes_raw_exception(self) -> None:
        manager = FakeMCPManager()
        manager.error = RuntimeError(
            "Authorization: Bearer private-token https://private.example"
        )

        with self.assertRaises(agent_server.HTTPException) as raised:
            await self.call_with_patches(manager)

        self.assertEqual(raised.exception.status_code, 503)
        encoded = json.dumps(raised.exception.detail)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("private.example", encoded)
        self.assertEqual(
            raised.exception.detail["code"],
            "claude_mcp_connection_failed",
        )

    async def test_timeout_is_generic_and_maintenance_is_released(self) -> None:
        manager = FakeMCPManager()
        manager.error = ClaudeSDKControlTimeout("secret timeout detail")

        with self.assertRaises(agent_server.HTTPException) as raised:
            await self.call_with_patches(manager)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail["code"],
            "claude_mcp_connection_failed",
        )
        self.assertNotIn("secret", json.dumps(raised.exception.detail))
        self.assertNotIn(self.session_id, agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_routes_are_standard_authenticated_api_not_agent_helpers(self) -> None:
        path = f"/api/sessions/{self.session_id}/claude/mcp"
        self.assertFalse(agent_server.is_agent_helper_route("GET", path))
        self.assertFalse(agent_server.is_agent_helper_route("POST", path))
        route_methods = {
            method
            for route in agent_server.app.routes
            if getattr(route, "path", None) == "/api/sessions/{session_id}/claude/mcp"
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(route_methods, {"GET", "POST"})

    async def test_health_and_runtime_advertise_capability_version(self) -> None:
        with patch.object(agent_server, "claude_sdk_dependency_available", return_value=True), patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            runtime = await agent_server.claude_runtime_snapshot(self.session_id)
            health = await agent_server.health()

        self.assertTrue(runtime["features"]["mcp_management"])
        capability = health["capabilities"]["claude_controls"]
        self.assertEqual(capability["version"], 3)
        self.assertTrue(capability["features"]["mcp_management"])
        self.assertEqual(health["api_contract_version"], 23)
        local_import = health["capabilities"]["local_session_import_v1"]
        self.assertTrue(local_import["available"])
        self.assertEqual(local_import["version"], 1)
        self.assertEqual(
            local_import["max_batch_items"],
            agent_server.MAX_BULK_IMPORT_ITEMS,
        )
        self.assertEqual(
            local_import["max_list_items"],
            agent_server.MAX_LOCAL_SESSION_LIST_ITEMS,
        )


if __name__ == "__main__":
    unittest.main()
