import asyncio
import os
import tempfile
import threading
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import agent_server
from fastapi import HTTPException


class ServerUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    def test_agents_server_systemd_cgroup_handles_unified_and_legacy_paths(self):
        paths = (
            "/user.slice/user-1000.slice/user@1000.service/app.slice/agents-server.service",
            "/unrelated",
        )
        with patch.object(agent_server, "process_cgroup_paths", return_value=paths):
            cgroup = agent_server.agents_server_systemd_cgroup(123)

        self.assertEqual(cgroup, paths[0])
        self.assertTrue(agent_server.cgroup_is_within(f"{cgroup}/child", cgroup))
        self.assertFalse(agent_server.cgroup_is_within("/agents-server.service-old", cgroup))

    def test_managed_update_rejects_tmux_inside_service_cgroup_structurally(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        with patch.object(agent_server, "agents_server_systemd_cgroup", return_value=cgroup), \
             patch.object(agent_server, "tmux_server_pid", return_value=4242), \
             patch.object(agent_server, "process_cgroup_paths", return_value=(cgroup,)):
            with self.assertRaises(HTTPException) as raised:
                agent_server.ensure_managed_update_tmux_isolated()

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "unsafe_update_tmux_cgroup")
        self.assertTrue(raised.exception.detail["retryable"])
        self.assertIn("terminated by the restart", raised.exception.detail["message"])
        self.assertIn("login shell", raised.exception.detail["action"])

    def test_managed_update_preserves_non_systemd_and_macos_behavior(self):
        with patch.object(agent_server, "agents_server_systemd_cgroup", return_value=None), \
             patch.object(agent_server, "tmux_server_pid") as tmux_pid:
            agent_server.ensure_managed_update_tmux_isolated()

        tmux_pid.assert_not_called()

    def test_tmux_guard_does_not_false_open_when_service_appears_on_reprobe(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        server_pid = os.getpid()
        paths = {
            server_pid: (cgroup,),
            4242: ("/user.slice/user@1000.service/app.slice/updater.scope",),
        }
        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(agent_server, "agents_server_systemd_cgroup", return_value=None), \
             patch.object(agent_server, "linux_process_ids", return_value=(server_pid,)), \
             patch.object(
                 agent_server,
                 "process_cgroup_paths",
                 side_effect=lambda pid: paths[pid],
             ), \
             patch.object(agent_server, "tmux_server_pid", return_value=4242):
            verified = agent_server.ensure_managed_update_tmux_isolated()

        self.assertEqual(verified, cgroup)

    def test_service_cgroup_probe_fails_closed_on_a_stubborn_descendant(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        server_pid = os.getpid()
        paths = {
            server_pid: (cgroup,),
            4242: (cgroup,),
            5151: ("/user.slice/user@1000.service/app.slice/other.service",),
        }
        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(
                 agent_server,
                 "linux_process_ids",
                 return_value=tuple(paths),
             ), \
             patch.object(
                 agent_server,
                 "process_cgroup_paths",
                 side_effect=lambda pid: paths[pid],
             ):
            state = agent_server.managed_update_service_cgroup_state(
                service_cgroup=cgroup,
            )
            with self.assertRaises(HTTPException) as raised:
                agent_server.ensure_managed_update_service_cgroup_clear(
                    service_cgroup=cgroup,
                )

        self.assertFalse(state["safe"])
        self.assertEqual(state["unknown_descendant_count"], 1)
        self.assertNotIn("4242", str(raised.exception.detail))
        self.assertNotIn(cgroup, str(raised.exception.detail))
        self.assertEqual(
            raised.exception.detail["code"],
            "unsafe_update_service_cgroup",
        )

    def test_service_cgroup_probe_fails_closed_when_proc_is_unavailable(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(agent_server, "linux_process_ids", return_value=None):
            state = agent_server.managed_update_service_cgroup_state(
                service_cgroup=cgroup,
            )

        self.assertFalse(state["safe"])
        self.assertIsNone(state["unknown_descendant_count"])
        public = agent_server.public_managed_update_service_cgroup_state(state)
        self.assertEqual(public, {
            "safe": False,
            "unknown_descendant_count": None,
            "inspection": "process-list-unavailable",
        })

    def test_service_cgroup_probe_fails_closed_on_unreadable_live_membership(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        server_pid = os.getpid()
        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(
                 agent_server,
                 "linux_process_ids",
                 return_value=(server_pid, 4242),
             ), \
             patch.object(
                 agent_server,
                 "process_cgroup_paths",
                 side_effect=lambda pid: (cgroup,) if pid == server_pid else (),
             ), \
             patch.object(
                 agent_server,
                 "linux_process_still_exists",
                 return_value=True,
             ):
            state = agent_server.managed_update_service_cgroup_state(
                service_cgroup=cgroup,
            )

        self.assertFalse(state["safe"])
        self.assertIsNone(state["unknown_descendant_count"])
        self.assertEqual(state["inspection"], "process-cgroup-unavailable")

    def test_service_cgroup_probe_distinguishes_nonservice_from_unreadable_self_cgroup(self):
        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(agent_server, "agents_server_systemd_cgroup", return_value=None), \
             patch.object(agent_server, "process_cgroup_paths", return_value=()):
            unavailable = agent_server.managed_update_service_cgroup_state()

        self.assertEqual(
            agent_server.public_managed_update_service_cgroup_state(unavailable),
            {
                "safe": False,
                "unknown_descendant_count": None,
                "inspection": "self-cgroup-unavailable",
            },
        )

        with patch.object(agent_server.sys, "platform", "linux"), \
             patch.object(agent_server, "agents_server_systemd_cgroup", return_value=None), \
             patch.object(
                 agent_server,
                 "process_cgroup_paths",
                 return_value=("/user.slice/user@1000.service/session.scope",),
             ):
            direct = agent_server.managed_update_service_cgroup_state()

        self.assertTrue(direct["safe"])
        self.assertEqual(direct["inspection"], "not-systemd-managed")

    def test_missing_tmux_server_is_bootstrapped_in_a_user_scope(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(agent_server, "agents_server_systemd_cgroup", return_value=cgroup), \
             patch.object(agent_server, "tmux_server_pid", side_effect=[None, 4242]), \
             patch.object(agent_server.shutil, "which", return_value="/usr/bin/systemd-run"), \
             patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
             patch.object(
                 agent_server,
                 "server_update_runner_environment",
                 return_value={"XDG_RUNTIME_DIR": "/run/user/1000"},
             ), \
             patch.object(agent_server.subprocess, "run", return_value=completed) as run, \
             patch.object(
                 agent_server,
                 "process_cgroup_paths",
                 return_value=("/user.slice/user@1000.service/app.slice/agents-server-tmux.scope",),
             ), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            isolated = agent_server.bootstrap_isolated_tmux_server()

        self.assertTrue(isolated)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [
            "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
        ])
        self.assertIn("--collect", command)
        self.assertIn("/usr/bin/tmux", command)
        self.assertIn("env", run.call_args.kwargs)
        self.assertEqual(
            run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"],
            "/run/user/1000",
        )
        self.assertEqual(run_tmux.call_args_list[0].args[0], [
            "set-option", "-g", "exit-empty", "off",
        ])
        self.assertEqual(run_tmux.call_args_list[1].args[0][:3], [
            "kill-session", "-t", command[-2],
        ])

    async def test_start_cgroup_guard_runs_before_drain_or_tmux_launch(self):
        blocker = HTTPException(
            status_code=409,
            detail=agent_server.unsafe_update_tmux_detail(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            status_path = root / "status.json"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(
                     agent_server,
                     "ensure_managed_update_tmux_isolated",
                     side_effect=blocker,
                 ) as guard, \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "unsafe_update_tmux_cgroup")
        guard.assert_called_once_with()
        run_tmux.assert_not_called()
        self.assertFalse(status_path.exists())

    async def test_residual_service_descendant_reopens_admission_before_launch(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        blocker = HTTPException(
            status_code=409,
            detail={
                "code": "unsafe_update_service_cgroup",
                "message": "Managed update cannot safely start.",
                "action": "Retry.",
                "retryable": True,
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            status_path = root / "status.json"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            quiesce = AsyncMock(side_effect=blocker)
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(
                     agent_server,
                     "ensure_managed_update_tmux_isolated",
                     return_value=cgroup,
                 ), \
                 patch.object(
                     agent_server,
                     "quiesce_managed_update_service_cgroup",
                     new=quiesce,
                 ), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )
                status = agent_server.read_server_update_status()
                admission_blocker = agent_server.managed_server_update_blocker()

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "unsafe_update_service_cgroup",
        )
        self.assertEqual(status["phase"], "failed")
        self.assertIsNone(admission_blocker)
        quiesce.assert_awaited_once_with(service_cgroup=cgroup)
        run_tmux.assert_not_called()

    async def test_cancelled_prelaunch_quiesce_reopens_admission(self):
        cgroup = "/user.slice/user@1000.service/app.slice/agents-server.service"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_quiesce(*, service_cgroup):
            self.assertEqual(service_cgroup, cgroup)
            entered.set()
            await release.wait()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            status_path = root / "status.json"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(
                     agent_server,
                     "ensure_managed_update_tmux_isolated",
                     return_value=cgroup,
                 ), \
                 patch.object(
                     agent_server,
                     "quiesce_managed_update_service_cgroup",
                     side_effect=slow_quiesce,
                 ), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                task = asyncio.create_task(agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                ))
                await asyncio.wait_for(entered.wait(), timeout=1)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                status = agent_server.read_server_update_status()
                admission_blocker = agent_server.managed_server_update_blocker()

        self.assertEqual(status["phase"], "failed")
        self.assertIsNone(admission_blocker)
        run_tmux.assert_not_called()

    def test_linux_runner_environment_restores_the_user_service_bus(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "bus").touch()
            with patch.object(agent_server.sys, "platform", "linux"), \
                 patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}, clear=True):
                environment = agent_server.server_update_runner_environment()

        self.assertEqual(environment, {
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        })

    async def test_health_reports_missing_tmux_and_disables_managed_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "working_tmux_bin", return_value=None):
                response = await agent_server.health()

        capability = response["capabilities"]["tmux"]
        self.assertEqual(capability["available"], False)
        self.assertEqual(capability["required"], False)
        self.assertIn("not found", capability["message"])
        self.assertIn("Install tmux", capability["action"])
        self.assertFalse(response["managed_updates"])

    async def test_health_reports_available_tmux_and_managed_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"):
                response = await agent_server.health()

        capability = response["capabilities"]["tmux"]
        self.assertEqual(capability, {
            "available": True,
            "required": False,
            "message": "tmux is available.",
            "action": None,
        })
        self.assertTrue(response["managed_updates"])
        self.assertEqual(
            response["capabilities"]["server_updates"]["version"],
            4,
        )
        self.assertEqual(response["update_service_cgroup"], {
            "safe": True,
            "unknown_descendant_count": 0,
            "inspection": "not-systemd-managed",
        })
        self.assertEqual(
            response["capabilities"]["server_updates"]["tracks"],
            ["stable", "beta"],
        )
        self.assertEqual(
            response["capabilities"]["scheduled_jobs"],
            {
                "available": True,
                "required": False,
                "message": (
                    "Scheduled jobs support parent-chat and standalone "
                    "provider contexts."
                ),
                "action": None,
                "version": 4,
                "context_modes": ["chat", "standalone"],
                "default_context_mode": "chat",
                "features": {
                    "chat_references": True,
                    "direct_message_mentions": True,
                    "route_mentions": True,
                },
            },
        )

    async def test_health_reports_only_provisional_queue_as_update_blocking(self):
        queued = {
            "durable-chat": deque([{"queued_id": "kept", "_durable": True}]),
            "provisional-chat": deque([{"queued_id": "wait", "_durable": False}]),
        }
        with patch.object(agent_server, "QUEUED_TURNS", queued), \
             patch.object(agent_server, "RUN_NOW_TURNS", {}), \
             patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"):
            response = await agent_server.health()

        self.assertEqual(response["queued"], {
            "durable-chat": 1,
            "provisional-chat": 1,
        })
        self.assertEqual(response["update_blocking_queued_count"], 1)

    async def test_check_reports_a_signed_newer_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "1.1.0")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["track"], "stable")

    async def test_check_beta_track_discovers_and_persists_beta_release(self):
        manifest = AsyncMock(return_value={"version": "1.1.0-beta.3"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update(
                agent_server.ServerUpdateCheckRequest(track="beta"),
            )
            persisted = agent_server.read_server_update_status()

        manifest.assert_awaited_once_with("beta")
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["track"], "beta")
        self.assertEqual(status["current_track"], "stable")
        self.assertTrue(status["channel_switch"])
        self.assertEqual(persisted["track"], "beta")

    async def test_check_without_body_infers_beta_for_legacy_status(self):
        manifest = AsyncMock(return_value={"version": "1.1.0-beta.4"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update()

        manifest.assert_awaited_once_with("beta")
        self.assertEqual(status["track"], "beta")
        self.assertFalse(status["channel_switch"])

    async def test_check_stable_track_allows_beta_to_latest_stable_switch(self):
        manifest = AsyncMock(return_value={"version": "1.0.0"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update(
                agent_server.ServerUpdateCheckRequest(track="stable"),
            )

        self.assertEqual(status["phase"], "available")
        self.assertTrue(status["update_available"])
        self.assertTrue(status["channel_switch"])
        self.assertIn("Switch to stable", status["message"])

    async def test_check_does_not_offer_a_signed_older_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.0.0"})):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        self.assertIn("current", status["message"])

    async def test_check_reports_an_unpublished_release_without_failing_ipc(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(side_effect=HTTPException(status_code=404, detail="No signed AgentsServer release has been published yet."))):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "unavailable")
        self.assertFalse(status["update_available"])
        self.assertIn("No signed AgentsServer release", status["message"])

    async def test_status_keeps_a_just_started_update_active_while_tmux_appears(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "SERVER_UPDATE_START_GRACE_SECONDS", 45.0), \
             patch.object(agent_server, "server_update_status_age_seconds", return_value=44.9), \
             patch.object(agent_server, "working_tmux_bin", return_value=None):
            agent_server.write_server_update_status(
                update_id="new-update",
                phase="starting",
                target_version="1.1.0",
                message="Starting detached update.",
            )
            status = await agent_server.server_update_status()

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        self.assertNotIn("finished_at", status)

    async def test_status_normalizes_an_active_target_that_is_now_current(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(
                 agent_server,
                 "server_update_status_age_seconds",
                 return_value=46.0,
             ), \
             patch.object(agent_server, "working_tmux_bin", return_value=None):
            agent_server.write_server_update_status(
                update_id="completed-update",
                phase="restarting",
                target_version="1.1.0",
                update_available=True,
                message="Restarting updated server.",
            )
            status = await agent_server.server_update_status()

        self.assertEqual(status["phase"], "complete")
        self.assertFalse(status["update_available"])
        self.assertEqual(status["installed_version"], "1.1.0")
        self.assertIn("installed and healthy", status["message"])
        self.assertTrue(status["finished_at"])

    async def test_status_keeps_target_current_drained_while_updater_is_alive(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(
                 agent_server,
                 "SERVER_UPDATE_STATUS_FILE",
                 Path(temporary) / "status.json",
             ), \
             patch.object(
                 agent_server,
                 "server_update_is_active",
                 return_value=True,
             ), \
             patch.object(
                 agent_server,
                 "server_update_status_age_seconds",
                 return_value=120.0,
             ):
            agent_server.write_server_update_status(
                update_id="still-running",
                phase="restarting",
                target_version="1.1.0",
            )
            status = await agent_server.server_update_status()

        self.assertEqual(status["phase"], "restarting")
        self.assertTrue(agent_server.managed_server_update_blocks_work(status))

    async def test_abandoned_update_clears_its_exact_fence_before_terminal_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status_path = root / "status.json"
            hub_data = root / "hub"
            runtime = MagicMock()
            runtime.capability.return_value = {
                "available": True,
                "designated_host": True,
                "hub_id": "hub_test12345678",
                "host_server_identity": "server-test-identity",
            }
            runtime.maintenance_fence_sync.return_value = {
                "reason": "server-update",
                "operation_id": "update-exact",
                "snapshot": "snapshot_exact",
            }
            phases_at_clear: list[str] = []

            def clear(reason, operation_id, snapshot):
                phases_at_clear.append(
                    agent_server.read_server_update_status()["phase"]
                )
                self.assertEqual(reason, "server-update")
                self.assertEqual(operation_id, "update-exact")
                self.assertEqual(snapshot, hub_data / "maintenance-backups" / "snapshot_exact")
                return True

            runtime.clear_maintenance_sync.side_effect = clear
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "TEAM_HUB_DATA_DIR", hub_data), \
                 patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime), \
                 patch.object(agent_server, "server_identity", return_value="server-test-identity"), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "server_update_status_age_seconds", return_value=60.0):
                agent_server.write_server_update_status(
                    update_id="update-exact",
                    phase="downloading",
                    target_version="1.1.0",
                    team_hub_id="hub_test12345678",
                    team_hub_host_server_identity="server-test-identity",
                    team_hub_snapshot_generation="snapshot_exact",
                )
                status = await agent_server.server_update_status()

            self.assertEqual(phases_at_clear, ["downloading"])
            self.assertEqual(status["phase"], "failed")
            runtime.clear_maintenance_sync.assert_called_once()

    async def test_current_candidate_with_missing_hub_cannot_finalize_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            runtime = MagicMock()
            runtime.capability.return_value = {
                "available": False,
                "designated_host": False,
                "hub_id": None,
                "host_server_identity": None,
            }
            with patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "server_update_status_age_seconds", return_value=60.0):
                agent_server.write_server_update_status(
                    update_id="update-lost-hub",
                    phase="restarting",
                    target_version="1.1.0",
                    team_hub_id="hub_expected123456",
                    team_hub_host_server_identity="server-test-identity",
                    team_hub_snapshot_generation="snapshot_expected",
                )
                status = await agent_server.server_update_status()

            self.assertEqual(status["phase"], "restarting")
            runtime.clear_maintenance_sync.assert_not_called()

    def test_update_identity_verification_binds_exact_team_hub_transport(self):
        runtime = MagicMock()
        runtime.capability.return_value = {
            "available": True,
            "designated_host": True,
            "hub_id": "hub_test12345678",
            "host_server_identity": "server-test-identity",
            "transport": "tailscale_serve",
            "hub_url": "https://sonic.example.ts.net:8444/api/team-hub",
        }
        status = {
            "team_hub_id": "hub_test12345678",
            "team_hub_host_server_identity": "server-test-identity",
            "team_hub_snapshot_generation": "snapshot_expected",
            "team_hub_transport": "tailscale_serve",
            "team_hub_url": "https://sonic.example.ts.net:8444/api/team-hub",
        }
        with patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime), \
             patch.object(agent_server, "server_identity", return_value="server-test-identity"):
            agent_server._verify_server_update_team_hub_identity(status)
            with self.assertRaisesRegex(RuntimeError, "lost its designated Team Hub identity"):
                agent_server._verify_server_update_team_hub_identity(
                    {
                        **status,
                        "team_hub_url": (
                            "https://other.example.ts.net:8444/api/team-hub"
                        ),
                    }
                )

    def test_update_identity_verification_binds_direct_primary_and_exact_ordered_routes(self):
        direct_url = "http://100.73.184.23:7850/api/team-hub"
        serve_url = "https://sonic.example.ts.net:8444/api/team-hub"
        routes = [
            {"transport": "direct_ip", "hub_url": direct_url},
            {"transport": "tailscale_serve", "hub_url": serve_url},
        ]
        runtime = MagicMock()
        runtime.capability.return_value = {
            "available": True,
            "designated_host": True,
            "hub_id": "hub_test12345678",
            "host_server_identity": "server-test-identity",
            "transport": "direct_ip",
            "hub_url": direct_url,
            "routes": routes,
        }
        status = {
            "team_hub_id": "hub_test12345678",
            "team_hub_host_server_identity": "server-test-identity",
            "team_hub_snapshot_generation": "snapshot_expected",
            "team_hub_transport": "direct_ip",
            "team_hub_url": direct_url,
            "team_hub_direct_ip_url": direct_url,
            "team_hub_routes": routes,
        }
        with patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime), \
             patch.object(agent_server, "server_identity", return_value="server-test-identity"):
            agent_server._verify_server_update_team_hub_identity(status)
            runtime.capability.return_value = {
                **runtime.capability.return_value,
                "routes": list(reversed(routes)),
            }
            with self.assertRaisesRegex(RuntimeError, "lost its designated Team Hub identity"):
                agent_server._verify_server_update_team_hub_identity(status)
            with self.assertRaisesRegex(RuntimeError, "route binding is invalid"):
                agent_server._verify_server_update_team_hub_identity(
                    {**status, "team_hub_direct_ip_url": ""}
                )

    async def test_startup_clears_snapshot_fence_orphaned_before_status_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status_path = root / "status.json"
            hub_data = root / "hub"
            runtime = MagicMock()
            runtime.maintenance_fence_sync.return_value = {
                "reason": "server-update",
                "operation_id": "update-pre-status",
                "snapshot": "snapshot_pre_status",
            }
            with patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "TEAM_HUB_DATA_DIR", hub_data), \
                 patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime):
                agent_server.write_server_update_status(phase="current")
                status = await agent_server.reconcile_server_update_status_after_startup()

            self.assertEqual(status["phase"], "current")
            runtime.clear_maintenance_sync.assert_called_once_with(
                "server-update",
                "update-pre-status",
                hub_data / "maintenance-backups" / "snapshot_pre_status",
            )

    async def test_startup_keeps_terminal_same_operation_fence_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            runtime = MagicMock()
            runtime.maintenance_fence_sync.return_value = {
                "reason": "server-update",
                "operation_id": "update-incomplete",
                "snapshot": "snapshot_incomplete",
            }
            with patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime):
                original = agent_server.write_server_update_status(
                    update_id="update-incomplete",
                    phase="failed",
                    target_version="1.1.0",
                )
                status = await agent_server.reconcile_server_update_status_after_startup()

            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["update_id"], original["update_id"])
            runtime.clear_maintenance_sync.assert_not_called()

    async def test_check_and_start_cannot_overwrite_active_status_during_grace(self):
        manifest = AsyncMock(
            side_effect=AssertionError("active update must block release discovery")
        )
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "server_update_status_age_seconds", return_value=1.0), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            original = agent_server.write_server_update_status(
                update_id="update-active",
                phase="starting",
                target_version="1.1.0",
            )
            with self.assertRaises(HTTPException) as check_error:
                await agent_server.check_server_update()
            with self.assertRaises(HTTPException) as start_error:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0")
                )
            current = agent_server.read_server_update_status()

        self.assertEqual(check_error.exception.status_code, 409)
        self.assertEqual(start_error.exception.status_code, 409)
        self.assertEqual(current["update_id"], original["update_id"])
        self.assertEqual(current["phase"], "starting")
        manifest.assert_not_awaited()

    async def test_startup_reconciliation_completes_an_installed_orphan(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(
                 agent_server,
                 "SERVER_UPDATE_STATUS_FILE",
                 Path(temporary) / "status.json",
             ), \
             patch.object(
                 agent_server,
                 "server_update_is_active",
                 return_value=False,
             ):
            agent_server.write_server_update_status(
                update_id="orphaned-update",
                phase="restarting",
                target_version="1.1.0",
                update_available=True,
            )
            status = (
                await agent_server.reconcile_server_update_status_after_startup()
            )

        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["installed_version"], "1.1.0")
        self.assertFalse(agent_server.managed_server_update_blocks_work(status))

    async def test_startup_reconciliation_fails_an_uninstalled_orphan(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(
                 agent_server,
                 "SERVER_UPDATE_STATUS_FILE",
                 Path(temporary) / "status.json",
             ), \
             patch.object(
                 agent_server,
                 "server_update_is_active",
                 return_value=False,
             ):
            agent_server.write_server_update_status(
                update_id="orphaned-update",
                phase="downloading",
                target_version="1.1.0",
            )
            status = (
                await agent_server.reconcile_server_update_status_after_startup()
            )

        self.assertEqual(status["phase"], "failed")
        self.assertIn("detached updater exited", status["message"])
        self.assertFalse(agent_server.managed_server_update_blocks_work(status))

    async def test_startup_reconciliation_keeps_a_live_update_drained(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(
                 agent_server,
                 "SERVER_UPDATE_STATUS_FILE",
                 Path(temporary) / "status.json",
             ), \
             patch.object(
                 agent_server,
                 "server_update_is_active",
                 return_value=True,
             ):
            original = agent_server.write_server_update_status(
                update_id="live-update",
                phase="restarting",
                target_version="1.1.0",
            )
            status = (
                await agent_server.reconcile_server_update_status_after_startup()
            )

        self.assertEqual(status["phase"], "restarting")
        self.assertEqual(status["updated_at"], original["updated_at"])
        self.assertTrue(agent_server.managed_server_update_blocks_work(status))

    async def test_start_on_current_version_does_not_require_tmux(self):
        manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest), \
             patch.object(agent_server, "working_tmux_bin", return_value=None):
            status = await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.0.0"))

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        manifest.assert_not_awaited()

    async def test_start_newer_version_without_tmux_returns_actionable_503(self):
        manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest), \
             patch.object(agent_server, "working_tmux_bin", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("tmux", str(raised.exception.detail))
        self.assertIn("Install tmux", str(raised.exception.detail))
        manifest.assert_not_awaited()

    async def test_start_launches_a_detached_verified_update_without_manifest_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "signed_release_manifest", new=manifest), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0"))

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        manifest.assert_not_awaited()
        command = run_tmux.call_args.args[0]
        self.assertEqual(command[:3], ["new-session", "-d", "-s"])
        self.assertIn("--expected-version 1.1.0", command[-1])
        self.assertIn("--current-version 1.0.0", command[-1])
        self.assertIn("--track stable", command[-1])

    async def test_start_rejects_update_while_an_agent_turn_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            quiesce = AsyncMock()
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"busy-chat"}), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(
                     agent_server,
                     "quiesce_managed_update_service_cgroup",
                     new=quiesce,
                 ), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("1 active agent run", str(raised.exception.detail))
        quiesce.assert_not_awaited()
        run_tmux.assert_not_called()

    async def test_start_rejects_update_while_queued_turns_are_not_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            queued = {
                "chat": deque(
                    [
                        {"queued_id": "one", "_durable": False},
                        {"queued_id": "two", "_durable": False},
                    ]
                )
            }
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", queued), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("2 queued turns", str(raised.exception.detail))
        run_tmux.assert_not_called()

    async def test_start_preserves_durable_queued_turns_and_launches_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            queued = {
                "chat": deque([
                    {
                        "queued_id": "kept",
                        "prompt": "Keep this for later.",
                        "_durable": True,
                        "_paused_after_stop": True,
                    },
                ]),
            }
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", queued), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "active_provider_background_work_labels", return_value=[]), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(list(queued["chat"])[0]["queued_id"], "kept")
        run_tmux.assert_called_once()

    async def test_start_rejects_update_while_a_codex_subagent_is_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            status_path = root / "status.json"
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "CLAUDE_SDK_MANAGER", None), \
                 patch.object(
                     agent_server,
                     "active_codex_work_labels",
                     return_value=["Codex subagent child-thread"],
                 ), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

            self.assertFalse(status_path.exists())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Codex subagent child-thread", str(raised.exception.detail))
        run_tmux.assert_not_called()

    async def test_start_rejects_loaded_claude_background_subagent(self):
        class LoadedClaudeManager:
            @staticmethod
            def is_loaded(session_id):
                return session_id == "claude-chat"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            status_path = root / "status.json"
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server.STORE, "sessions", {
                     "claude-chat": {
                         "id": "claude-chat",
                         "backend": agent_server.BACKEND_CLAUDE,
                     },
                 }), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "CLAUDE_SDK_MANAGER", LoadedClaudeManager()), \
                 patch.object(agent_server, "active_codex_work_labels", return_value=[]), \
                 patch.object(
                     agent_server,
                     "build_claude_subagent_snapshot",
                     return_value={"active_count": 1},
                 ), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

            self.assertFalse(status_path.exists())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Claude background work in claude-chat", str(raised.exception.detail))
        run_tmux.assert_not_called()

    async def test_start_ignores_stale_claude_history_when_supervisor_is_unloaded(self):
        class UnloadedClaudeManager:
            @staticmethod
            def is_loaded(_session_id):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            snapshot = MagicMock(return_value={"active_count": 1})
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "AGENT_TOKEN", ""), \
                 patch.object(agent_server.STORE, "sessions", {
                     "claude-chat": {
                         "id": "claude-chat",
                         "backend": agent_server.BACKEND_CLAUDE,
                     },
                 }), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "CLAUDE_SDK_MANAGER", UnloadedClaudeManager()), \
                 patch.object(agent_server, "active_codex_work_labels", return_value=[]), \
                 patch.object(agent_server, "build_claude_subagent_snapshot", snapshot), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        self.assertEqual(status["phase"], "starting")
        snapshot.assert_not_called()
        run_tmux.assert_called_once()

    async def test_start_fails_closed_when_claude_load_state_cannot_be_inspected(self):
        class BrokenClaudeManager:
            @staticmethod
            def is_loaded(_session_id):
                raise RuntimeError("supervisor registry unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            status_path = root / "status.json"
            snapshot = MagicMock(return_value={"active_count": 0})
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server.STORE, "sessions", {
                     "claude-chat": {
                         "id": "claude-chat",
                         "backend": agent_server.BACKEND_CLAUDE,
                     },
                 }), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "CLAUDE_SDK_MANAGER", BrokenClaudeManager()), \
                 patch.object(agent_server, "active_codex_work_labels", return_value=[]), \
                 patch.object(agent_server, "build_claude_subagent_snapshot", snapshot), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux") as run_tmux:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )

            self.assertFalse(status_path.exists())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(
            "Claude provider state unknown in claude-chat",
            str(raised.exception.detail),
        )
        snapshot.assert_not_called()
        run_tmux.assert_not_called()

    async def test_start_allows_loaded_claude_with_terminal_subagents(self):
        class LoadedClaudeManager:
            @staticmethod
            def is_loaded(session_id):
                return session_id == "claude-chat"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "AGENT_TOKEN", ""), \
                 patch.object(agent_server.STORE, "sessions", {
                     "claude-chat": {
                         "id": "claude-chat",
                         "backend": agent_server.BACKEND_CLAUDE,
                     },
                 }), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "CLAUDE_SDK_MANAGER", LoadedClaudeManager()), \
                 patch.object(agent_server, "active_codex_work_labels", return_value=[]), \
                 patch.object(
                     agent_server,
                     "build_claude_subagent_snapshot",
                     return_value={"active_count": 0},
                 ), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        self.assertEqual(status["phase"], "starting")
        run_tmux.assert_called_once()

    async def test_update_admission_wins_race_with_new_turn_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            updater_launched = threading.Event()
            release_updater = threading.Event()

            def blocked_tmux(_args):
                updater_launched.set()
                release_updater.wait(timeout=2)

            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "AGENT_TOKEN", ""), \
                 patch.object(agent_server.STORE, "sessions", {
                     "chat": {
                         "id": "chat",
                         "backend": agent_server.BACKEND_CODEX,
                     }
                 }), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "CURRENT_TURNS", {}), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", side_effect=blocked_tmux):
                update_task = asyncio.create_task(
                    agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )
                )
                try:
                    self.assertTrue(
                        await asyncio.to_thread(updater_launched.wait, 1)
                    )
                    with self.assertRaises(HTTPException) as raised:
                        await agent_server._start_turn_locked(
                            "chat",
                            agent_server.TurnRequest(prompt="must not start"),
                        )
                finally:
                    release_updater.set()
                await update_task

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("managed update", str(raised.exception.detail))

    async def test_update_rejects_while_nonreserving_control_is_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            control_started = asyncio.Event()
            release_control = asyncio.Event()

            async def slow_ensure(*_args, **_kwargs):
                control_started.set()
                await release_control.wait()
                return "thread", "instruction-hash"

            manager = AsyncMock()
            manager.start = AsyncMock()
            maintenance: set[str] = set()
            unpin = AsyncMock()
            with ExitStack() as patches:
                patches.enter_context(
                    patch.object(agent_server, "SERVER_VERSION", "1.0.0")
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "SERVER_UPDATE_STATUS_FILE",
                        root / "status.json",
                    )
                )
                patches.enter_context(
                    patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner)
                )
                patches.enter_context(
                    patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key)
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "CODEX_TRANSPORT",
                        agent_server.CODEX_TRANSPORT_APP_SERVER,
                    )
                )
                patches.enter_context(patch.object(agent_server.STORE, "sessions", {
                     "chat": {
                         "id": "chat",
                         "backend": agent_server.BACKEND_CODEX,
                         "codex_thread_id": "thread",
                         "cwd": "/work",
                     }
                }))
                patches.enter_context(
                    patch.object(agent_server, "BUSY_SESSIONS", set())
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "SERVER_MAINTENANCE_SESSIONS",
                        maintenance,
                    )
                )
                patches.enter_context(
                    patch.object(agent_server, "CURRENT_TURNS", {})
                )
                patches.enter_context(
                    patch.object(agent_server, "QUEUED_TURNS", {})
                )
                patches.enter_context(
                    patch.object(agent_server, "RUN_NOW_TURNS", {})
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "server_update_is_active",
                        return_value=False,
                    )
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "working_tmux_bin",
                        return_value="/usr/bin/tmux",
                    )
                )
                run_tmux = patches.enter_context(
                    patch.object(agent_server, "run_tmux")
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "codex_app_server_manager",
                        AsyncMock(return_value=manager),
                    )
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "ensure_codex_app_server_thread",
                        AsyncMock(side_effect=slow_ensure),
                    )
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "unpin_codex_app_server_thread",
                        unpin,
                    )
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "acquire_codex_interactive_control_lease",
                    )
                )
                patches.enter_context(
                    patch.object(
                        agent_server,
                        "release_codex_interactive_control_lease",
                    )
                )
                control_task = asyncio.create_task(
                    agent_server.acquire_codex_control_thread(
                        "chat",
                        reserve_session=False,
                    )
                )
                await asyncio.wait_for(control_started.wait(), timeout=1)
                try:
                    self.assertEqual(maintenance, {"chat"})
                    with self.assertRaises(HTTPException) as raised:
                        await agent_server.start_server_update(
                            agent_server.ServerUpdateRequest(version="1.1.0"),
                        )
                finally:
                    release_control.set()
                manager_result, thread_id, _session = await control_task
                await agent_server.release_codex_control_thread(
                    "chat",
                    manager_result,
                    thread_id,
                    schedule_queue=False,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("active agent run", str(raised.exception.detail))
        self.assertEqual(maintenance, set())
        run_tmux.assert_not_called()
        unpin.assert_awaited_once_with(manager, "thread")

    async def test_tmux_launch_failure_reopens_admission_and_removes_credential(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "AGENT_TOKEN", "test-secret"), \
                 patch.object(agent_server, "BUSY_SESSIONS", set()), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", set()), \
                 patch.object(agent_server, "QUEUED_TURNS", {}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {}), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(
                     agent_server,
                     "run_tmux",
                     side_effect=RuntimeError("tmux failed"),
                 ):
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.start_server_update(
                        agent_server.ServerUpdateRequest(version="1.1.0"),
                    )
                status = agent_server.read_server_update_status()
                blocker = agent_server.managed_server_update_blocker()
                credentials = list(root.glob(".server-update-*.auth.json"))

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(status["phase"], "failed")
        self.assertIsNone(blocker)
        self.assertEqual(credentials, [])

    async def test_starting_update_blocks_new_interactive_and_scheduled_work(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(
                 agent_server,
                 "SERVER_UPDATE_STATUS_FILE",
                 Path(temporary) / "status.json",
             ), \
             patch.object(agent_server, "BUSY_SESSIONS", set()):
            agent_server.write_server_update_status(
                phase="starting",
                target_version="1.1.0",
            )
            interactive = await agent_server.turn_start_blocker()
            scheduled = await agent_server.scheduled_job_blocker("chat")

        self.assertIn("managed update", str(interactive))
        self.assertIn("managed update", str(scheduled))

    def test_restarted_target_version_stays_drained_until_runner_completes(self):
        self.assertTrue(
            agent_server.managed_server_update_blocks_work(
                {
                    "phase": "restarting",
                    "target_version": agent_server.SERVER_VERSION,
                }
            )
        )

    async def test_start_passes_user_service_environment_into_detached_tmux(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "server_update_runner_environment", return_value={
                     "XDG_RUNTIME_DIR": "/run/user/123",
                     "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/123/bus",
                 }), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        command = run_tmux.call_args.args[0][-1]
        self.assertIn("env XDG_RUNTIME_DIR=/run/user/123", command)
        self.assertIn(
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/123/bus",
            command,
        )
        self.assertIn("--expected-version 1.1.0", command)

    async def test_start_beta_release_passes_beta_track_to_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.1.0-beta.3",
                        track="beta",
                    )
                )

        self.assertEqual(status["track"], "beta")
        self.assertTrue(status["channel_switch"])
        self.assertIn("--track beta", run_tmux.call_args.args[0][-1])

    async def test_legacy_start_without_track_infers_installed_beta_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.2"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0-beta.3")
                )

        self.assertEqual(status["track"], "beta")
        self.assertIn("--track beta", run_tmux.call_args.args[0][-1])

    async def test_start_allows_explicit_beta_to_stable_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "working_tmux_bin", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.0.0",
                        track="stable",
                    )
                )

        self.assertEqual(status["phase"], "starting")
        self.assertTrue(status["channel_switch"])
        self.assertIn("--track stable", run_tmux.call_args.args[0][-1])

    async def test_start_rejects_version_that_does_not_match_track(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.1.0-beta.3",
                        track="stable",
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("stable track", str(raised.exception.detail))

    async def test_start_refuses_stable_to_older_stable_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            status = await agent_server.start_server_update(
                agent_server.ServerUpdateRequest(version="1.1.0", track="stable")
            )

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        run_tmux.assert_not_called()

    async def test_start_refuses_beta_to_older_beta_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0-beta.4"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            status = await agent_server.start_server_update(
                agent_server.ServerUpdateRequest(
                    version="1.2.0-beta.3",
                    track="beta",
                )
            )

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        run_tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
