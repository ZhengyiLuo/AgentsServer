import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import agent_server


class ProviderJobsAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_sessions = agent_server.STORE.sessions
        self.original_current_turns = agent_server.CURRENT_TURNS
        self.original_authority_root = agent_server.CROSS_CHAT_AUTHORITY_ROOT
        self.original_agent_token = agent_server.AGENT_TOKEN
        self.original_jobs = agent_server.JOBS.jobs
        self.original_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        agent_server.STORE.sessions = {
            "source": {
                "id": "source",
                "title": "Source",
                "backend": "codex",
                "provider_jobs_access": "full",
            },
        }
        agent_server.CURRENT_TURNS = {}
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.root / "authority"
        agent_server.CROSS_CHAT_CAPABILITIES.clear()
        agent_server.AGENT_TOKEN = "test-admin-token"
        agent_server.JOBS.jobs = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}

    async def asyncTearDown(self) -> None:
        agent_server.CROSS_CHAT_CAPABILITIES.clear()
        agent_server.STORE.sessions = self.original_sessions
        agent_server.CURRENT_TURNS = self.original_current_turns
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.original_authority_root
        agent_server.AGENT_TOKEN = self.original_agent_token
        agent_server.JOBS.jobs = self.original_jobs
        agent_server.SESSION_LIFECYCLE_LOCKS = self.original_lifecycle_locks
        self.temporary.cleanup()

    @staticmethod
    def provider_request(token: str, method: str = "GET") -> Request:
        return Request({
            "type": "http",
            "method": method,
            "path": "/api/agent/sessions/source/jobs",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode("utf-8")),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 7850),
            "client": ("127.0.0.1", 41000),
        })

    async def issue(self, mode: str, run_id: str) -> tuple[str, dict]:
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = mode
        authority_path = await agent_server.issue_cross_chat_capability(
            "source",
            run_id,
            [],
        )
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
        token = payload["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode("utf-8")).hexdigest()
        return token, agent_server.CROSS_CHAT_CAPABILITIES[token_hash]

    async def test_session_policy_defaults_persists_updates_and_validates(self) -> None:
        store = agent_server.SessionStore()
        sessions_path = self.root / "sessions.json"
        with (
            patch.object(agent_server, "SESSIONS_FILE", sessions_path),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            created = await store.create(
                agent_server.CreateSessionRequest(title="Policy chat"),
            )
            self.assertEqual(created["provider_jobs_access"], "full")
            persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted[created["id"]]["provider_jobs_access"],
                "full",
            )

            updated = await store.update(
                created["id"],
                {"provider_jobs_access": "read_only"},
            )
            self.assertEqual(updated["provider_jobs_access"], "read_only")
            persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted[created["id"]]["provider_jobs_access"],
                "read_only",
            )

            with self.assertRaises(HTTPException) as raised:
                await store.update(
                    created["id"],
                    {"provider_jobs_access": "unexpected"},
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            agent_server.effective_provider_jobs_access({}),
            "full",
        )
        self.assertEqual(
            agent_server.public_session({"id": "legacy"})["provider_jobs_access"],
            "full",
        )
        with self.assertRaises(ValueError):
            agent_server.UpdateSessionRequest(provider_jobs_access="unexpected")

    async def test_legacy_session_load_migrates_default_policy_durably(self) -> None:
        sessions_path = self.root / "legacy-sessions.json"
        sessions_path.write_text(json.dumps({
            "legacy": {
                "id": "legacy",
                "title": "Legacy",
                "backend": "claude",
                "cwd": "/tmp",
            },
        }), encoding="utf-8")
        store = agent_server.SessionStore()
        with (
            patch.object(agent_server, "SESSIONS_FILE", sessions_path),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "read_abandoned_fork_thread_ids",
                return_value=set(),
            ),
            patch.object(agent_server, "rebuild_codex_subagent_indexes"),
        ):
            await store.load()

        self.assertEqual(
            store.sessions["legacy"]["provider_jobs_access"],
            "full",
        )
        persisted = json.loads(sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["legacy"]["provider_jobs_access"],
            "full",
        )

    async def test_failed_policy_save_restores_previous_authorization(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "source": {
                "id": "source",
                "backend": "codex",
                "provider_jobs_access": "blocked",
            },
        }
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=OSError("disk unavailable")),
        ):
            with self.assertRaises(OSError):
                await store.update(
                    "source",
                    {"provider_jobs_access": "full"},
                )
        self.assertEqual(
            store.sessions["source"]["provider_jobs_access"],
            "blocked",
        )

    def test_standalone_scheduled_context_inherits_provider_jobs_policy(self) -> None:
        isolated = agent_server.standalone_provider_session({
            "id": "source",
            "backend": "codex",
            "provider_jobs_access": "read_only",
            "codex_thread_id": "thread-parent",
        })
        self.assertEqual(isolated["provider_jobs_access"], "read_only")
        self.assertIsNone(isolated["codex_thread_id"])

    async def test_health_advertises_versioned_policy_contract(self) -> None:
        with patch.object(
            agent_server,
            "tmux_capability",
            return_value={
                "available": False,
                "required": False,
                "message": "tmux unavailable",
                "action": None,
            },
        ):
            response = await agent_server.health()
        self.assertEqual(response["api_contract_version"], 12)
        self.assertEqual(
            response["capabilities"]["provider_jobs_access_control_v1"],
            {
                "available": True,
                "version": 1,
                "modes": ["full", "read_only", "blocked"],
                "default": "full",
            },
        )

    async def test_capability_issuance_is_bounded_by_policy(self) -> None:
        _full_token, full = await self.issue("full", "run_full")
        self.assertIn("jobs", full["actions"])
        self.assertEqual(full["provider_jobs_access"], "full")

        _read_token, read_only = await self.issue("read_only", "run_read")
        self.assertIn("jobs", read_only["actions"])
        self.assertEqual(read_only["provider_jobs_access"], "read_only")

        _blocked_token, blocked = await self.issue("blocked", "run_blocked")
        self.assertNotIn("jobs", blocked["actions"])
        self.assertEqual(blocked["provider_jobs_access"], "blocked")

    async def test_policy_tightening_is_immediate_during_live_turn(self) -> None:
        token, _capability = await self.issue("full", "run_live")
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_live"}}
        request = self.provider_request(token)

        await agent_server.authorize_provider_jobs_operation(
            request,
            session_id="source",
            operation="write",
        )

        async def update(_session_id: str, policy_patch: dict) -> dict:
            agent_server.STORE.sessions["source"].update(policy_patch)
            return agent_server.STORE.sessions["source"]

        with patch.object(
            agent_server.STORE,
            "update",
            AsyncMock(side_effect=update),
        ):
            response = await agent_server.update_session(
                "source",
                agent_server.UpdateSessionRequest(
                    provider_jobs_access="read_only",
                ),
            )
            self.assertEqual(
                response["session"]["provider_jobs_access"],
                "read_only",
            )
            await agent_server.authorize_provider_jobs_operation(
                request,
                session_id="source",
                operation="read",
            )
            with self.assertRaises(HTTPException) as raised:
                await agent_server.authorize_provider_jobs_operation(
                    request,
                    session_id="source",
                    operation="write",
                )
            self.assertEqual(raised.exception.status_code, 403)
            self.assertIn("read-only", str(raised.exception.detail))

            await agent_server.update_session(
                "source",
                agent_server.UpdateSessionRequest(
                    provider_jobs_access="blocked",
                ),
            )
            with self.assertRaises(HTTPException) as raised:
                await agent_server.authorize_provider_jobs_operation(
                    request,
                    session_id="source",
                    operation="read",
                )
            self.assertEqual(raised.exception.status_code, 403)
            self.assertIn("blocked", str(raised.exception.detail))

    async def test_issue_time_read_only_ceiling_cannot_be_loosened_mid_turn(self) -> None:
        token, _capability = await self.issue("read_only", "run_read_live")
        agent_server.CURRENT_TURNS = {
            "source": {"run_id": "run_read_live"},
        }
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = "full"
        request = self.provider_request(token)
        await agent_server.authorize_provider_jobs_operation(
            request,
            session_id="source",
            operation="read",
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.authorize_provider_jobs_operation(
                request,
                session_id="source",
                operation="write",
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_policy_patch_serializes_with_live_agent_job_mutation(self) -> None:
        token, _capability = await self.issue("full", "run_policy_race")
        agent_server.CURRENT_TURNS = {
            "source": {"run_id": "run_policy_race"},
        }
        request = self.provider_request(token, method="POST")
        update_entered = agent_server.asyncio.Event()
        allow_update = agent_server.asyncio.Event()

        async def gated_update(_session_id: str, policy_patch: dict) -> dict:
            update_entered.set()
            await allow_update.wait()
            agent_server.STORE.sessions["source"].update(policy_patch)
            return agent_server.STORE.sessions["source"]

        create = AsyncMock(return_value={"job": {}})
        with (
            patch.object(
                agent_server.STORE,
                "update",
                AsyncMock(side_effect=gated_update),
            ),
            patch.object(agent_server, "create_session_job", create),
        ):
            update_task = agent_server.asyncio.create_task(
                agent_server.update_session(
                    "source",
                    agent_server.UpdateSessionRequest(
                        provider_jobs_access="blocked",
                    ),
                )
            )
            await update_entered.wait()
            create_task = agent_server.asyncio.create_task(
                agent_server.create_agent_session_job(
                    request,
                    "source",
                    agent_server.CreateScopedJobRequest(
                        title="Must not race",
                        prompt="Run",
                        interval_seconds=60,
                    ),
                )
            )
            await agent_server.asyncio.sleep(0)
            self.assertFalse(create_task.done())
            allow_update.set()
            await update_task
            with self.assertRaises(HTTPException) as raised:
                await create_task

        self.assertEqual(raised.exception.status_code, 403)
        create.assert_not_awaited()

    async def test_every_agent_jobs_route_declares_read_or_write_policy(self) -> None:
        request = self.provider_request("unused")
        authorize = AsyncMock(return_value={})
        with (
            patch.object(
                agent_server,
                "authorize_provider_jobs_operation",
                authorize,
            ),
            patch.object(
                agent_server,
                "list_session_jobs",
                AsyncMock(return_value={"jobs": []}),
            ),
            patch.object(
                agent_server,
                "get_session_job_runs",
                AsyncMock(return_value={"runs": []}),
            ),
            patch.object(
                agent_server,
                "create_session_job",
                AsyncMock(return_value={"job": {}}),
            ),
            patch.object(
                agent_server,
                "update_session_job",
                AsyncMock(return_value={"job": {}}),
            ),
            patch.object(
                agent_server,
                "delete_session_job",
                AsyncMock(return_value={"ok": True}),
            ),
        ):
            await agent_server.list_agent_session_jobs(request, "source")
            await agent_server.get_agent_session_job_runs(
                request,
                "source",
                "job_1",
                before_seq=None,
                limit=20,
            )
            await agent_server.create_agent_session_job(
                request,
                "source",
                agent_server.CreateScopedJobRequest(
                    title="Create",
                    prompt="Run",
                    interval_seconds=60,
                ),
            )
            await agent_server.update_agent_session_job(
                request,
                "source",
                "job_1",
                agent_server.UpdateJobRequest(enabled=False),
            )
            await agent_server.delete_agent_session_job(
                request,
                "source",
                "job_1",
            )

        self.assertEqual(
            [call.kwargs["operation"] for call in authorize.await_args_list],
            ["read", "read", "write", "write", "write"],
        )

    async def test_human_jobs_routes_ignore_provider_policy(self) -> None:
        agent_server.STORE.sessions["source"]["provider_jobs_access"] = "blocked"
        job = {
            "id": "job_human",
            "session_id": "source",
            "title": "Human-created",
        }
        with patch.object(
            agent_server.JOBS,
            "create",
            AsyncMock(return_value=job),
        ) as create:
            response = await agent_server.create_session_job(
                "source",
                agent_server.CreateScopedJobRequest(
                    title="Human-created",
                    prompt="Run",
                    interval_seconds=60,
                ),
            )
        self.assertEqual(response["job"]["id"], "job_human")
        create.assert_awaited_once()

    def test_provider_prompt_cannot_overstate_read_only_or_blocked_access(self) -> None:
        authority = self.root / "authority.json"
        read_only = agent_server.cross_chat_provider_authority_block(
            [],
            authority,
            "source",
            {"jobs", "publish"},
            "read_only",
        )
        self.assertIn("Jobs access is read-only", read_only)
        self.assertIn("Jobs list", read_only)
        self.assertIn("Jobs detail", read_only)
        self.assertIn("Jobs run status", read_only)
        self.assertNotIn("Jobs (full access)", read_only)
        self.assertNotIn(" COMMAND`", read_only)

        blocked = agent_server.cross_chat_provider_authority_block(
            [],
            authority,
            "source",
            {"publish"},
            "blocked",
        )
        self.assertIn("Jobs access is blocked", blocked)
        self.assertNotIn("$AGENTSDOCK_JOBS_CLI", blocked)


if __name__ == "__main__":
    unittest.main()
