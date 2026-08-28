import asyncio
import json
import os
import signal
import subprocess
import tempfile
import unittest
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import Response

import agent_server


TOKEN = "restart_test_token_abcdefghijklmnopqrstuvwxyz"
SERVER_IDENTITY = "server-identity-test"
SERVER_INSTANCE_ID = "a" * 32


def tmux_restart_state(
    *,
    in_service_cgroup: bool = False,
    cgroup_unknown: bool = False,
    pid: int | None = None,
    paths: tuple[str, ...] = (),
    service_cgroup: str | None = None,
    inspection: str = "not-running",
) -> dict[str, object]:
    return {
        "tmux_server_in_service_cgroup": in_service_cgroup,
        "tmux_server_cgroup_unknown": cgroup_unknown,
        "_tmux_server_pid": pid,
        "_tmux_server_cgroup_paths": paths,
        "_server_service_cgroup": service_cgroup,
        "_tmux_server_inspection": inspection,
    }


def http_request(
    method: str = "GET",
    path: str = "/api/admin/restart",
    *,
    token: str | None = TOKEN,
    query: str = "",
    extra_headers: dict[str, str] | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"x-agentsdock-token", token.encode()))
    headers.extend(
        (name.lower().encode(), value.encode())
        for name, value in (extra_headers or {}).items()
    )
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "client": (client_host, 54321),
        "server": ("127.0.0.1", 7850),
    })


def restart_body(
    request_id: uuid.UUID | None = None,
    *,
    server_identity: str = SERVER_IDENTITY,
    server_instance_id: str = SERVER_INSTANCE_ID,
    force: bool = False,
    blocker_revision: str | None = None,
) -> agent_server.ServerRestartRequest:
    fields = {
        "request_id": request_id or uuid.uuid4(),
        "expected_server_identity": server_identity,
        "expected_server_instance_id": server_instance_id,
        "confirmed": True,
    }
    if force:
        fields.update({
            "force": True,
            "force_confirmed": True,
            "expected_blocker_revision": (
                blocker_revision
                or agent_server.server_restart_blocker_snapshot_locked()[
                    "revision"
                ]
            ),
        })
    return agent_server.ServerRestartRequest(
        **fields,
    )


@contextmanager
def restart_environment(root: Path):
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            agent_server,
            "SERVER_RESTART_STATUS_FILE",
            root / "admin" / "server-restart.json",
        ))
        stack.enter_context(patch.object(
            agent_server,
            "SERVER_UPDATE_STATUS_FILE",
            root / "admin" / "server-update.json",
        ))
        stack.enter_context(patch.object(agent_server, "AGENT_TOKEN", TOKEN))
        stack.enter_context(patch.object(
            agent_server,
            "SERVER_INSTANCE_ID",
            SERVER_INSTANCE_ID,
        ))
        stack.enter_context(patch.object(
            agent_server,
            "server_identity",
            return_value=SERVER_IDENTITY,
        ))
        stack.enter_context(patch.object(
            agent_server,
            "detect_managed_server_service_kind",
            return_value="systemd-user",
        ))
        stack.enter_context(patch.object(agent_server, "BUSY_SESSIONS", set()))
        stack.enter_context(patch.object(
            agent_server,
            "SERVER_MAINTENANCE_SESSIONS",
            set(),
        ))
        stack.enter_context(patch.object(agent_server, "DELETING_SESSIONS", set()))
        stack.enter_context(patch.object(
            agent_server,
            "CODEX_GOALS_RECONFIGURING",
            False,
        ))
        stack.enter_context(patch.object(agent_server, "QUEUED_TURNS", {}))
        stack.enter_context(patch.object(agent_server, "RUN_NOW_TURNS", {}))
        stack.enter_context(patch.object(
            agent_server,
            "active_provider_background_work_labels",
            return_value=[],
        ))
        stack.enter_context(patch.object(
            agent_server,
            "server_restart_tmux_cgroup_state",
            return_value=tmux_restart_state(),
        ))
        stack.enter_context(patch.object(
            agent_server,
            "UNSAFE_HTTP_MUTATIONS_IN_FLIGHT",
            0,
        ))
        yield


class ServerRestartStateTests(unittest.TestCase):
    def test_private_journal_is_mode_0600_and_public_status_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                private = agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                    raw_path="/private/server/path",
                    pid=4242,
                    message="x" * 800,
                )
                public = agent_server.public_server_restart_status(private)
                mode = os.stat(agent_server.SERVER_RESTART_STATUS_FILE).st_mode & 0o777

        self.assertEqual(mode, 0o600)
        self.assertEqual(len(public["message"]), 500)
        self.assertNotIn("_source_instance_id", public)
        self.assertNotIn("raw_path", public)
        self.assertNotIn("pid", public)
        self.assertFalse(public["forced"])
        self.assertEqual(public["server_identity"], SERVER_IDENTITY)
        self.assertEqual(public["server_instance_id"], SERVER_INSTANCE_ID)

    def test_forced_public_audit_is_bounded_and_does_not_expose_private_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                private = agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                    _forced=True,
                    _forced_work_snapshot={
                        "active_count": 10_000_001,
                        "restart_blocking_queued_count": -2,
                        "provider_background_count": "invalid",
                        "mutation_count": True,
                        "deleting_session_count": 3,
                        "codex_goals_reconfiguring": True,
                        "tmux_server_in_service_cgroup": True,
                        "tmux_server_cgroup_unknown": False,
                    },
                )
                public = agent_server.public_server_restart_status(private)

        self.assertTrue(public["forced"])
        self.assertEqual(public["interrupted_work"], {
            "codex_goals_reconfiguring": True,
            "tmux_server_in_service_cgroup": True,
            "tmux_server_cgroup_unknown": False,
            "active_count": 1_000_000,
            "restart_blocking_queued_count": 0,
            "provider_background_count": 0,
            "server_maintenance_count": 0,
            "mutation_count": 0,
            "deleting_session_count": 3,
        })
        self.assertNotIn("_forced", public)
        self.assertNotIn("_forced_work_snapshot", public)

    def test_startup_reconciliation_completes_only_after_a_new_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.write_server_restart_status(
                    phase="signaling",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id="b" * 32,
                    requested_at="2026-08-19T00:00:00Z",
                )
                status = agent_server.reconcile_server_restart_status_after_startup()

        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["message"], "AgentsServer restarted successfully.")
        self.assertTrue(status["completed_at"])

    def test_startup_reconciliation_keeps_same_instance_fenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                original = agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                    requested_at="2026-08-19T00:00:00Z",
                )
                status = agent_server.reconcile_server_restart_status_after_startup()

        self.assertEqual(status, original)
        self.assertTrue(agent_server.managed_server_restart_blocks_work(status))

    def test_incomplete_active_restart_record_fails_closed_then_unfences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                )
                status = agent_server.reconcile_server_restart_status_after_startup()

        self.assertEqual(status["phase"], "failed")
        self.assertFalse(agent_server.managed_server_restart_blocks_work(status))

    def test_signal_worker_marks_signaling_and_sends_sigterm_to_self(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(agent_server.os, "getpid", return_value=321), \
                 patch.object(agent_server.os, "kill") as kill:
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                agent_server.signal_managed_server_restart(request_id)
                status = agent_server.read_server_restart_status()

        kill.assert_called_once_with(321, signal.SIGTERM)
        self.assertEqual(status["phase"], "signaling")

    def test_signal_failure_records_failed_and_reopens_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(agent_server.os, "kill", side_effect=OSError("denied")):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                agent_server.signal_managed_server_restart(request_id)
                status = agent_server.read_server_restart_status()

        self.assertEqual(status["phase"], "failed")
        self.assertFalse(agent_server.managed_server_restart_blocks_work(status))
        self.assertNotIn("denied", status["message"])

    def test_forced_signal_arms_hard_kill_watchdog_before_sigterm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            watchdog = MagicMock()
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(agent_server.os, "getpid", return_value=321), \
                 patch.object(agent_server.os, "kill") as kill, \
                 patch.object(agent_server.threading, "Thread", return_value=watchdog) as thread:
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                    _forced=True,
                )
                agent_server.signal_managed_server_restart(request_id)

        thread.assert_called_once_with(
            target=agent_server.force_kill_managed_server_after_deadline,
            args=(request_id, 321),
            daemon=True,
            name="agents-server-force-restart",
        )
        watchdog.start.assert_called_once_with()
        kill.assert_called_once_with(321, signal.SIGTERM)

    def test_force_kill_watchdog_rechecks_request_instance_and_force_fences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(agent_server.os, "kill") as kill:
                agent_server.write_server_restart_status(
                    phase="signaling",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                    _forced=True,
                )
                agent_server.force_kill_managed_server_after_deadline(
                    request_id,
                    321,
                )
                agent_server.write_server_restart_status(
                    phase="signaling",
                    request_id="different-request",
                    _source_instance_id=SERVER_INSTANCE_ID,
                    _forced=True,
                )
                agent_server.force_kill_managed_server_after_deadline(
                    request_id,
                    321,
                )

        kill.assert_called_once_with(321, signal.SIGKILL)

    def test_duplicate_signal_workers_claim_only_one_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(agent_server.os, "getpid", return_value=321), \
                 patch.object(agent_server.os, "kill") as kill:
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                agent_server.signal_managed_server_restart(request_id)
                agent_server.signal_managed_server_restart(request_id)
                status = agent_server.read_server_restart_status()

        kill.assert_called_once_with(321, signal.SIGTERM)
        self.assertEqual(status["phase"], "signaling")

    def test_stale_same_process_acceptance_fails_and_reopens_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "server_restart_status_age_seconds",
                return_value=agent_server.SERVER_RESTART_ACCEPTED_STALE_SECONDS,
            ):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                blocked = agent_server.managed_server_restart_blocks_work()
                status = agent_server.read_server_restart_status()

        self.assertFalse(blocked)
        self.assertEqual(status["phase"], "failed")
        self.assertIn("could not begin", status["message"])

    def test_public_status_poll_reconciles_a_stale_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "server_restart_status_age_seconds",
                return_value=agent_server.SERVER_RESTART_ACCEPTED_STALE_SECONDS,
            ):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                status = agent_server.public_server_restart_status()

        self.assertEqual(status["phase"], "failed")
        self.assertIn("could not begin", status["message"])


class ManagedServerProofTests(unittest.TestCase):
    def test_restart_tmux_cgroup_probe_is_read_only_and_fail_closed(self):
        service_cgroup = (
            "/user.slice/user-1000.slice/user@1000.service/"
            "app.slice/agents-server.service"
        )
        probe = subprocess.CompletedProcess(
            ["tmux", "list-sessions"],
            0,
            stdout="4242\n",
            stderr="",
        )
        with patch.object(agent_server.sys, "platform", "linux"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
            return_value=service_cgroup,
        ), patch.object(
            agent_server,
            "run_tmux",
            return_value=probe,
        ) as run_tmux, patch.object(
            agent_server,
            "process_cgroup_paths",
            return_value=(service_cgroup,),
        ):
            inside = agent_server.server_restart_tmux_cgroup_state()
        self.assertTrue(inside["tmux_server_in_service_cgroup"])
        self.assertFalse(inside["tmux_server_cgroup_unknown"])
        self.assertEqual(inside["_tmux_server_pid"], 4242)
        run_tmux.assert_called_once_with(
            ["list-sessions", "-F", "#{pid}"],
            check=False,
        )

        with patch.object(agent_server.sys, "platform", "linux"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
            return_value=service_cgroup,
        ), patch.object(
            agent_server,
            "run_tmux",
            return_value=probe,
        ), patch.object(
            agent_server,
            "process_cgroup_paths",
            return_value=(),
        ):
            unknown = agent_server.server_restart_tmux_cgroup_state()
        self.assertFalse(unknown["tmux_server_in_service_cgroup"])
        self.assertTrue(unknown["tmux_server_cgroup_unknown"])

        with patch.object(agent_server.sys, "platform", "linux"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
            return_value=None,
        ), patch.object(
            agent_server,
            "run_tmux",
            return_value=probe,
        ), patch.object(agent_server, "process_cgroup_paths") as daemon_paths:
            server_unknown = agent_server.server_restart_tmux_cgroup_state()
        self.assertFalse(server_unknown["tmux_server_in_service_cgroup"])
        self.assertTrue(server_unknown["tmux_server_cgroup_unknown"])
        self.assertEqual(
            server_unknown["_tmux_server_inspection"],
            "server-cgroup-unavailable",
        )
        daemon_paths.assert_not_called()

        failed_probe = subprocess.CompletedProcess(
            ["tmux", "list-sessions"],
            1,
            stdout="",
            stderr="server lookup failed",
        )
        with patch.object(agent_server.sys, "platform", "linux"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
            return_value=service_cgroup,
        ), patch.object(
            agent_server,
            "run_tmux",
            return_value=failed_probe,
        ), patch.object(
            agent_server.Path,
            "lstat",
            return_value=MagicMock(st_mode=0),
        ), patch.object(
            agent_server.stat,
            "S_ISSOCK",
            return_value=True,
        ):
            lookup_failed = agent_server.server_restart_tmux_cgroup_state()
        self.assertFalse(lookup_failed["tmux_server_in_service_cgroup"])
        self.assertTrue(lookup_failed["tmux_server_cgroup_unknown"])
        self.assertEqual(
            lookup_failed["_tmux_server_inspection"],
            "lookup-failed-with-socket",
        )

        with patch.object(agent_server.sys, "platform", "linux"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
        ) as absent_service, patch.object(
            agent_server,
            "run_tmux",
            return_value=failed_probe,
        ), patch.object(
            agent_server.Path,
            "lstat",
            side_effect=FileNotFoundError,
        ):
            absent = agent_server.server_restart_tmux_cgroup_state()
        self.assertFalse(absent["tmux_server_in_service_cgroup"])
        self.assertFalse(absent["tmux_server_cgroup_unknown"])
        absent_service.assert_not_called()

        with patch.object(agent_server.sys, "platform", "darwin"), patch.object(
            agent_server,
            "agents_server_systemd_cgroup",
        ) as unmanaged_cgroup, patch.object(
            agent_server,
            "run_tmux",
        ) as unmanaged_probe:
            unmanaged = agent_server.server_restart_tmux_cgroup_state()
        self.assertFalse(unmanaged["tmux_server_in_service_cgroup"])
        self.assertFalse(unmanaged["tmux_server_cgroup_unknown"])
        unmanaged_cgroup.assert_not_called()
        unmanaged_probe.assert_not_called()

    def test_linux_requires_current_release_tree_and_exact_service_cgroup(self):
        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary)
            release = install_root / "releases" / "0.1.25-beta.1"
            release.mkdir(parents=True)
            (install_root / "current").symlink_to(release, target_is_directory=True)
            with patch.dict(
                os.environ,
                {"AGENTS_SERVER_INSTALL_DIR": str(install_root)},
                clear=False,
            ), patch.object(agent_server, "SERVER_ROOT", release.resolve()), \
                 patch.object(agent_server.sys, "platform", "linux"), \
                 patch.object(
                     agent_server,
                     "agents_server_systemd_cgroup",
                     return_value=(
                         "/user.slice/user-1000.slice/user@1000.service/"
                         "app.slice/agents-server.service"
                     ),
                 ):
                self.assertEqual(
                    agent_server.detect_managed_server_service_kind(),
                    "systemd-user",
                )
                with patch.object(
                    agent_server,
                    "agents_server_systemd_cgroup",
                    return_value=None,
                ):
                    self.assertIsNone(
                        agent_server.detect_managed_server_service_kind()
                    )

    def test_release_tree_must_resolve_through_installer_current_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            install_root = Path(temporary)
            current = install_root / "current"
            current.mkdir()
            with patch.dict(
                os.environ,
                {"AGENTS_SERVER_INSTALL_DIR": str(install_root)},
                clear=False,
            ), patch.object(agent_server, "SERVER_ROOT", install_root / "other"):
                self.assertFalse(agent_server.official_server_release_tree())

    def test_macos_launchctl_must_report_only_the_exact_serving_pid(self):
        completed = MagicMock(
            returncode=0,
            stdout="state = running\n\tpid = 4242\n",
        )
        with patch.object(agent_server.sys, "platform", "darwin"), \
             patch.object(agent_server.Path, "is_file", return_value=True), \
             patch.object(agent_server.os, "getuid", return_value=501), \
             patch.object(agent_server.os, "getpid", return_value=4242), \
             patch.object(
                 agent_server.subprocess,
                 "run",
                 return_value=completed,
             ) as run:
            self.assertTrue(agent_server.macos_launchd_owns_current_process())

        self.assertEqual(run.call_args.args[0], [
            "/bin/launchctl",
            "print",
            "gui/501/com.agentsdock.server",
        ])

        completed.stdout = "state = running\n\tpid = 9999\n"
        with patch.object(agent_server.sys, "platform", "darwin"), \
             patch.object(agent_server.Path, "is_file", return_value=True), \
             patch.object(agent_server.os, "getpid", return_value=4242), \
             patch.object(agent_server.subprocess, "run", return_value=completed):
            self.assertFalse(agent_server.macos_launchd_owns_current_process())

    def test_capability_requires_both_authentication_and_managed_ownership(self):
        with patch.object(agent_server, "AGENT_TOKEN", ""), \
             patch.object(agent_server, "managed_server_service_kind") as managed:
            capability = agent_server.server_restart_capability()
        self.assertFalse(capability["available"])
        managed.assert_not_called()

        with patch.object(agent_server, "AGENT_TOKEN", TOKEN), \
             patch.object(agent_server, "managed_server_service_kind", return_value=None):
            self.assertFalse(agent_server.server_restart_capability()["available"])

        with patch.object(agent_server, "AGENT_TOKEN", TOKEN), \
             patch.object(agent_server, "managed_server_service_kind", return_value="launch-agent"):
            capability = agent_server.server_restart_capability()
        self.assertTrue(capability["available"])
        self.assertEqual(capability["version"], 2)
        self.assertTrue(capability["force_restart"])
        self.assertTrue(capability["force_confirmation_required"])


class ServerRestartEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_exposes_boot_instance_and_restart_capability(self):
        capability = {
            "available": True,
            "required": False,
            "version": 2,
            "force_restart": True,
            "force_confirmation_required": True,
            "message": "available",
            "action": None,
        }
        capability_builder = MagicMock(return_value=capability)
        with patch.object(agent_server, "SERVER_INSTANCE_ID", SERVER_INSTANCE_ID), \
             patch.object(agent_server, "server_restart_capability", capability_builder), \
             patch.object(
                 agent_server,
                 "server_restart_tmux_cgroup_state",
                 return_value=tmux_restart_state(),
             ):
            health = await agent_server.health()

        self.assertEqual(health["server_instance_id"], SERVER_INSTANCE_ID)
        self.assertEqual(health["capabilities"]["server_restart"], capability)
        self.assertEqual(health["api_contract_version"], 20)
        snapshot = capability_builder.call_args.args[0]
        self.assertEqual(snapshot["version"], 2)
        self.assertRegex(snapshot["revision"], r"^[0-9a-f]{64}$")
        self.assertFalse(snapshot["has_blockers"])

    async def test_authenticated_get_exposes_bounded_private_blocker_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"private-chat-id"}), \
                 patch.object(
                     agent_server,
                     "active_provider_background_work_labels",
                     return_value=["private provider label"],
                 ):
                first = await agent_server.server_restart_status_endpoint(
                    http_request()
                )
                second = await agent_server.server_restart_status_endpoint(
                    http_request()
                )

        snapshot = first["blocker_snapshot"]
        self.assertEqual(snapshot["version"], 2)
        self.assertEqual(snapshot["revision"], second["blocker_snapshot"]["revision"])
        self.assertEqual(snapshot["active_count"], 1)
        self.assertEqual(snapshot["provider_background_count"], 1)
        self.assertTrue(snapshot["has_forceable_blockers"])
        self.assertFalse(snapshot["has_safety_blockers"])
        self.assertNotIn("private-chat-id", json.dumps(first))
        self.assertNotIn("private provider label", json.dumps(first))

    async def test_restart_routes_require_configured_header_auth_not_query_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.require_server_restart_control(http_request())
                query_only = http_request(token=None, query=f"token={TOKEN}")
                with self.assertRaises(HTTPException) as unauthorized:
                    agent_server.require_server_restart_control(query_only)
                self.assertEqual(unauthorized.exception.status_code, 401)
                with patch.object(
                    agent_server,
                    "detect_managed_server_service_kind",
                    return_value=None,
                ), self.assertRaises(HTTPException) as unmanaged:
                    agent_server.require_server_restart_control(http_request())
                self.assertEqual(unmanaged.exception.status_code, 503)
                self.assertEqual(
                    unmanaged.exception.detail["code"],
                    "server_restart_unmanaged",
                )

            with patch.object(agent_server, "AGENT_TOKEN", ""):
                with self.assertRaises(HTTPException) as unavailable:
                    agent_server.require_server_restart_control(
                        http_request(token=None)
                    )
                self.assertEqual(unavailable.exception.status_code, 503)

    async def test_restart_routes_reject_browser_ambient_authority_headers(self):
        forbidden_headers = (
            {"origin": "https://attacker.example"},
            {"sec-fetch-site": "cross-site"},
            {"sec-fetch-site": "same-origin"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                for headers in forbidden_headers:
                    with self.subTest(headers=headers), self.assertRaises(
                        HTTPException
                    ) as raised:
                        agent_server.require_server_restart_control(
                            http_request(extra_headers=headers)
                        )
                    self.assertEqual(raised.exception.status_code, 403)
                    self.assertEqual(raised.exception.detail, "forbidden")

                agent_server.require_server_restart_control(
                    http_request(extra_headers={"sec-fetch-site": "none"})
                )

    async def test_restart_post_transport_is_checked_before_route_parsing(self):
        called = False

        async def call_next(_request: Request) -> Response:
            nonlocal called
            called = True
            return Response("parsed", status_code=202)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                response = await agent_server.require_agent_token(
                    http_request(method="POST"),
                    call_next,
                )
                self.assertEqual(response.status_code, 415)
                self.assertFalse(called)

                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={"content-type": "application/json"},
                    ),
                    call_next,
                )
                self.assertEqual(response.status_code, 411)
                self.assertFalse(called)

                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={
                            "content-type": "application/json",
                            "content-length": "256",
                            "transfer-encoding": "chunked",
                        },
                    ),
                    call_next,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(called)

                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={
                            "content-type": "application/json",
                            "content-length": "256",
                            "transfer-encoding": "",
                        },
                    ),
                    call_next,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(called)

                duplicate_length = http_request(
                    method="POST",
                    extra_headers={
                        "content-type": "application/json",
                        "content-length": "256",
                    },
                )
                duplicate_length.scope["headers"].append(
                    (b"content-length", b"256")
                )
                response = await agent_server.require_agent_token(
                    duplicate_length,
                    call_next,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(called)

                for invalid_length in ("+10", "1_0"):
                    with self.subTest(content_length=invalid_length):
                        response = await agent_server.require_agent_token(
                            http_request(
                                method="POST",
                                extra_headers={
                                    "content-type": "application/json",
                                    "content-length": invalid_length,
                                },
                            ),
                            call_next,
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertFalse(called)

                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={
                            "content-type": "application/json",
                            "content-length": str(
                                agent_server.SERVER_RESTART_MAX_BODY_BYTES + 1
                            ),
                        },
                    ),
                    call_next,
                )
                self.assertEqual(response.status_code, 413)
                self.assertFalse(called)

                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={
                            "content-type": "application/json; charset=utf-8",
                            "content-length": "256",
                            "sec-fetch-site": "none",
                        },
                    ),
                    call_next,
                )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(called)

    async def test_restart_header_auth_is_checked_before_route_parsing(self):
        called = False

        async def call_next(_request: Request) -> Response:
            nonlocal called
            called = True
            return Response("parsed", status_code=202)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        token=None,
                        query=f"token={TOKEN}",
                        extra_headers={"content-type": "application/json"},
                    ),
                    call_next,
                )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(called)
        self.assertEqual(
            json.loads(response.body)["detail"]["code"],
            "server_restart_unauthorized",
        )

    async def test_post_accepts_idle_server_and_leaves_durable_queue_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = deque([{"queued_id": "kept", "_durable": True}])
            with restart_environment(root), \
                 patch.object(agent_server, "QUEUED_TURNS", {"chat": queued}):
                body = restart_body()
                tasks = BackgroundTasks()
                status = await agent_server.restart_server_endpoint(
                    body,
                    http_request(method="POST"),
                    tasks,
                )
                private = agent_server.read_server_restart_status()

        self.assertEqual(status["phase"], "accepted")
        self.assertEqual(status["request_id"], str(body.request_id))
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(list(queued)[0]["queued_id"], "kept")
        self.assertEqual(private["_source_instance_id"], SERVER_INSTANCE_ID)

    async def test_same_accepted_request_reattaches_one_recovery_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = uuid.uuid4()
            with restart_environment(root):
                first_tasks = BackgroundTasks()
                first = await agent_server.restart_server_endpoint(
                    restart_body(request_id),
                    http_request(method="POST"),
                    first_tasks,
                )
                replay_tasks = BackgroundTasks()
                replay = await agent_server.restart_server_endpoint(
                    restart_body(request_id),
                    http_request(method="POST"),
                    replay_tasks,
                )

        self.assertEqual(replay, first)
        self.assertEqual(len(first_tasks.tasks), 1)
        self.assertEqual(len(replay_tasks.tasks), 1)

    async def test_same_request_id_rejects_changed_target_or_force_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = uuid.uuid4()
            with restart_environment(root):
                await agent_server.restart_server_endpoint(
                    restart_body(request_id),
                    http_request(method="POST"),
                    BackgroundTasks(),
                )
                changed_bodies = (
                    restart_body(
                        request_id,
                        server_instance_id="changed-instance",
                    ),
                    restart_body(request_id, force=True),
                )
                for body in changed_bodies:
                    with self.subTest(body=body), self.assertRaises(
                        HTTPException
                    ) as raised:
                        await agent_server.restart_server_endpoint(
                            body,
                            http_request(method="POST"),
                            BackgroundTasks(),
                        )
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(
                        raised.exception.detail["code"],
                        "server_restart_request_changed",
                    )

    async def test_remote_agent_helper_is_rejected_before_mutation_admission(self):
        called = False

        async def call_next(_request: Request) -> Response:
            nonlocal called
            called = True
            return Response("unsafe")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        path="/api/agent/cross-chat/handoffs",
                        token=None,
                        client_host="203.0.113.10",
                    ),
                    call_next,
                )
                in_flight = agent_server.UNSAFE_HTTP_MUTATIONS_IN_FLIGHT

        self.assertEqual(response.status_code, 403)
        self.assertFalse(called)
        self.assertEqual(in_flight, 0)

    async def test_different_request_is_rejected_while_restart_is_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                await agent_server.restart_server_endpoint(
                    restart_body(),
                    http_request(method="POST"),
                    BackgroundTasks(),
                )
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "server_restart_in_progress",
        )

    async def test_different_request_can_start_after_stale_acceptance_reconciles(self):
        def status_age(status: dict[str, object]) -> float:
            return (
                agent_server.SERVER_RESTART_ACCEPTED_STALE_SECONDS
                if status.get("phase") == "accepted"
                else agent_server.SERVER_RESTART_COOLDOWN_SECONDS + 1
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "server_restart_status_age_seconds",
                side_effect=status_age,
            ):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                tasks = BackgroundTasks()
                status = await agent_server.restart_server_endpoint(
                    restart_body(),
                    http_request(method="POST"),
                    tasks,
                )

        self.assertEqual(status["phase"], "accepted")
        self.assertEqual(len(tasks.tasks), 1)

    async def test_recent_terminal_restart_enforces_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(
                     agent_server,
                     "server_restart_status_age_seconds",
                     return_value=2,
                 ):
                agent_server.write_server_restart_status(
                    phase="complete",
                    request_id=str(uuid.uuid4()),
                    requested_at="2026-08-19T00:00:00Z",
                )
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "server_restart_cooldown")
        self.assertGreater(raised.exception.detail["retry_after_seconds"], 0)

    async def test_expected_identity_and_instance_are_both_authoritative(self):
        for force in (False, True):
            for body in (
                restart_body(
                    server_identity="different-server",
                    force=force,
                ),
                restart_body(
                    server_instance_id="different-instance",
                    force=force,
                ),
            ):
                with self.subTest(
                    force=force,
                    body=body,
                ), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    with restart_environment(root):
                        with self.assertRaises(HTTPException) as raised:
                            await agent_server.restart_server_endpoint(
                                body,
                                http_request(method="POST"),
                                BackgroundTasks(),
                            )
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(
                        raised.exception.detail["code"],
                        "server_restart_target_changed",
                    )

    async def test_update_active_blocks_restart(self):
        for force in (False, True):
            with self.subTest(force=force), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with restart_environment(root):
                    agent_server.write_server_update_status(
                        phase="installing",
                        update_id="update-1",
                    )
                    with self.assertRaises(HTTPException) as raised:
                        await agent_server.restart_server_endpoint(
                            restart_body(force=force),
                            http_request(method="POST"),
                            BackgroundTasks(),
                        )

                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(
                    raised.exception.detail["code"],
                    "server_update_in_progress",
                )

    async def test_active_queue_and_provider_work_are_forceable_blockers(self):
        scenarios = (
            ({"BUSY_SESSIONS": {"chat"}}, "active_count"),
            ({
                "QUEUED_TURNS": {
                    "chat": deque([{"queued_id": "unsafe", "_durable": False}])
                }
            }, "restart_blocking_queued_count"),
        )
        for changes, count_name in scenarios:
            with self.subTest(count_name=count_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with restart_environment(root), ExitStack() as stack:
                    for name, value in changes.items():
                        stack.enter_context(patch.object(agent_server, name, value))
                    with self.assertRaises(HTTPException) as raised:
                        await agent_server.restart_server_endpoint(
                            restart_body(),
                            http_request(method="POST"),
                            BackgroundTasks(),
                        )
                self.assertEqual(raised.exception.detail["code"], "server_restart_busy")
                self.assertEqual(raised.exception.detail[count_name], 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "active_provider_background_work_labels",
                return_value=["private provider label"],
            ):
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )
        self.assertEqual(raised.exception.detail["provider_background_count"], 1)
        self.assertNotIn("private provider label", json.dumps(raised.exception.detail))

    async def test_active_codex_label_is_not_double_counted_as_provider_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"chat"}), \
                 patch.object(
                     agent_server,
                     "active_provider_background_work_labels",
                     return_value=[
                         "active chat chat",
                         "private provider background",
                     ],
                 ):
                snapshot = agent_server.server_restart_blocker_snapshot_locked()

        self.assertEqual(snapshot["active_count"], 1)
        self.assertEqual(snapshot["provider_background_count"], 1)

    def test_blocker_revision_binds_same_session_run_and_prebind_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.BUSY_SESSIONS.add("private-chat-id")
                agent_server.CURRENT_TURNS["private-chat-id"] = {
                    "run_id": "private-run-a",
                    "_server_restart_admission_id": "private-admission-a",
                }
                first_run = agent_server.server_restart_blocker_snapshot_locked()

                agent_server.CURRENT_TURNS["private-chat-id"]["run_id"] = (
                    "private-run-b"
                )
                replacement_run = (
                    agent_server.server_restart_blocker_snapshot_locked()
                )

                agent_server.CURRENT_TURNS["private-chat-id"] = {
                    "run_id": None,
                    "_server_restart_admission_id": "private-admission-b",
                }
                first_prebind = (
                    agent_server.server_restart_blocker_snapshot_locked()
                )
                agent_server.CURRENT_TURNS["private-chat-id"] = {
                    "run_id": None,
                    "_server_restart_admission_id": "private-admission-c",
                }
                replacement_prebind = (
                    agent_server.server_restart_blocker_snapshot_locked()
                )

                agent_server.CURRENT_TURNS["private-chat-id"] = {
                    "run_id": None,
                    "codex_control_reservation_id": "private-control-a",
                }
                first_control = (
                    agent_server.server_restart_blocker_snapshot_locked()
                )
                agent_server.CURRENT_TURNS["private-chat-id"] = {
                    "run_id": None,
                    "codex_control_reservation_id": "private-control-b",
                }
                replacement_control = (
                    agent_server.server_restart_blocker_snapshot_locked()
                )

        snapshots = (
            first_run,
            replacement_run,
            first_prebind,
            replacement_prebind,
            first_control,
            replacement_control,
        )
        self.assertTrue(all(snapshot["active_count"] == 1 for snapshot in snapshots))
        self.assertNotEqual(first_run["revision"], replacement_run["revision"])
        self.assertNotEqual(
            first_prebind["revision"],
            replacement_prebind["revision"],
        )
        self.assertNotEqual(
            first_control["revision"],
            replacement_control["revision"],
        )
        public_json = json.dumps(snapshots)
        for private_value in (
            "private-chat-id",
            "private-run-a",
            "private-run-b",
            "private-admission-a",
            "private-admission-b",
            "private-admission-c",
            "private-control-a",
            "private-control-b",
        ):
            self.assertNotIn(private_value, public_json)

    async def test_safety_critical_work_blocks_normal_and_force_restart(self):
        scenarios = (
            ({"SERVER_MAINTENANCE_SESSIONS": {"chat"}}, "server_maintenance_count"),
            ({"DELETING_SESSIONS": {"chat"}}, "deleting_session_count"),
            ({"CODEX_GOALS_RECONFIGURING": True}, "codex_goals_reconfiguring"),
            ({"UNSAFE_HTTP_MUTATIONS_IN_FLIGHT": 1}, "mutation_count"),
        )
        for changes, count_name in scenarios:
            for force in (False, True):
                with self.subTest(
                    count_name=count_name,
                    force=force,
                ), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    with restart_environment(root), ExitStack() as stack:
                        for name, value in changes.items():
                            stack.enter_context(patch.object(agent_server, name, value))
                        with self.assertRaises(HTTPException) as raised:
                            await agent_server.restart_server_endpoint(
                                restart_body(force=force),
                                http_request(method="POST"),
                                BackgroundTasks(),
                            )
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(
                        raised.exception.detail["code"],
                        "server_restart_unsafe_busy",
                    )
                    expected = True if count_name == "codex_goals_reconfiguring" else 1
                    self.assertEqual(raised.exception.detail[count_name], expected)

    async def test_force_restart_accepts_busy_server_and_audits_interrupted_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = deque([
                {"queued_id": "unsafe-1", "_durable": False},
                {"queued_id": "durable", "_durable": True},
            ])
            run_now = {"queued_id": "unsafe-2", "_update_transitioning": True}
            with restart_environment(root), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"chat-1", "chat-2"}), \
                 patch.object(agent_server, "QUEUED_TURNS", {"chat-1": queued}), \
                 patch.object(agent_server, "RUN_NOW_TURNS", {"chat-5": run_now}), \
                 patch.object(
                     agent_server,
                     "active_provider_background_work_labels",
                     return_value=["private provider label"],
                 ):
                tasks = BackgroundTasks()
                status = await agent_server.restart_server_endpoint(
                    restart_body(force=True),
                    http_request(method="POST"),
                    tasks,
                )
                private = agent_server.read_server_restart_status()

        self.assertEqual(status["phase"], "accepted")
        self.assertTrue(status["forced"])
        self.assertEqual(status["interrupted_work"], {
            "codex_goals_reconfiguring": False,
            "tmux_server_in_service_cgroup": False,
            "tmux_server_cgroup_unknown": False,
            "active_count": 2,
            "restart_blocking_queued_count": 2,
            "provider_background_count": 1,
            "server_maintenance_count": 0,
            "mutation_count": 0,
            "deleting_session_count": 0,
        })
        self.assertEqual(len(tasks.tasks), 1)
        self.assertTrue(private["_forced"])
        self.assertEqual(
            private["_forced_work_snapshot"],
            status["interrupted_work"],
        )
        self.assertNotIn("private provider label", json.dumps(status))

    async def test_force_restart_rejects_stale_blocker_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                confirmed = agent_server.server_restart_blocker_snapshot_locked()
                body = restart_body(
                    force=True,
                    blocker_revision=confirmed["revision"],
                )
                agent_server.BUSY_SESSIONS.add("newly-started-chat")
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        body,
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "server_restart_blockers_changed",
        )
        refreshed = raised.exception.detail["blocker_snapshot"]
        self.assertNotEqual(refreshed["revision"], confirmed["revision"])
        self.assertEqual(refreshed["active_count"], 1)
        self.assertNotIn("newly-started-chat", json.dumps(raised.exception.detail))

    async def test_tmux_cgroup_risk_requires_force_and_is_privately_revision_bound(self):
        service_cgroup = (
            "/user.slice/user-1000.slice/user@1000.service/"
            "app.slice/agents-server.service"
        )
        scenarios = (
            tmux_restart_state(
                in_service_cgroup=True,
                pid=4242,
                paths=(service_cgroup,),
                service_cgroup=service_cgroup,
                inspection="verified",
            ),
            tmux_restart_state(
                cgroup_unknown=True,
                pid=4242,
                service_cgroup=service_cgroup,
                inspection="cgroup-unavailable",
            ),
        )
        for state in scenarios:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with restart_environment(root), patch.object(
                    agent_server,
                    "server_restart_tmux_cgroup_state",
                    return_value=state,
                ):
                    snapshot = agent_server.server_restart_blocker_snapshot_locked()
                    public_json = json.dumps(snapshot)
                    self.assertNotIn("4242", public_json)
                    self.assertNotIn(service_cgroup, public_json)
                    with self.assertRaises(HTTPException) as safe:
                        await agent_server.restart_server_endpoint(
                            restart_body(),
                            http_request(method="POST"),
                            BackgroundTasks(),
                        )
                    self.assertEqual(
                        safe.exception.detail["code"],
                        "server_restart_busy",
                    )
                    forced = await agent_server.restart_server_endpoint(
                        restart_body(
                            force=True,
                            blocker_revision=snapshot["revision"],
                        ),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )
                self.assertTrue(forced["forced"])
                interrupted = forced["interrupted_work"]
                self.assertEqual(
                    interrupted["tmux_server_in_service_cgroup"],
                    state["tmux_server_in_service_cgroup"],
                )
                self.assertEqual(
                    interrupted["tmux_server_cgroup_unknown"],
                    state["tmux_server_cgroup_unknown"],
                )

    async def test_tmux_private_pid_change_invalidates_force_confirmation(self):
        service_cgroup = "/user.slice/agents-server.service"
        confirmed_state = tmux_restart_state(
            in_service_cgroup=True,
            pid=4242,
            paths=(service_cgroup,),
            service_cgroup=service_cgroup,
            inspection="verified",
        )
        changed_state = {**confirmed_state, "_tmux_server_pid": 4343}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "server_restart_tmux_cgroup_state",
                return_value=confirmed_state,
            ):
                confirmed = agent_server.server_restart_blocker_snapshot_locked()
                body = restart_body(
                    force=True,
                    blocker_revision=confirmed["revision"],
                )
                with patch.object(
                    agent_server,
                    "server_restart_tmux_cgroup_state",
                    return_value=changed_state,
                ), self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        body,
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )

        self.assertEqual(
            raised.exception.detail["code"],
            "server_restart_blockers_changed",
        )
        self.assertTrue(
            raised.exception.detail["blocker_snapshot"][
                "tmux_server_in_service_cgroup"
            ]
        )
        self.assertNotIn("4343", json.dumps(raised.exception.detail))

    async def test_force_restart_audits_provider_only_background_blockers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "active_provider_background_work_labels",
                return_value=["private provider label", "second private label"],
            ):
                status = await agent_server.restart_server_endpoint(
                    restart_body(force=True),
                    http_request(method="POST"),
                    BackgroundTasks(),
                )

        self.assertEqual(
            status["interrupted_work"]["provider_background_count"],
            2,
        )
        self.assertNotIn("private provider label", json.dumps(status))

    async def test_force_restart_preserves_recent_terminal_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), patch.object(
                agent_server,
                "server_restart_status_age_seconds",
                return_value=2,
            ):
                agent_server.write_server_restart_status(
                    phase="failed",
                    request_id=str(uuid.uuid4()),
                    failed_at="2026-08-19T00:00:00Z",
                )
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(force=True),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "server_restart_cooldown",
        )

    async def test_existing_http_mutation_blocks_restart_until_it_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def call_next(_request: Request) -> Response:
                entered.set()
                await release.wait()
                return Response("ok")

            with restart_environment(root):
                mutation_task = asyncio.create_task(
                    agent_server.require_agent_token(
                        http_request(method="PATCH", path="/api/sessions/chat"),
                        call_next,
                    )
                )
                await entered.wait()
                self.assertEqual(
                    agent_server.UNSAFE_HTTP_MUTATIONS_IN_FLIGHT,
                    1,
                )
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )
                release.set()
                response = await mutation_task

        self.assertEqual(response.status_code, 200)
        self.assertEqual(raised.exception.detail["mutation_count"], 1)
        self.assertEqual(agent_server.UNSAFE_HTTP_MUTATIONS_IN_FLIGHT, 0)

    async def test_new_http_mutations_are_rejected_after_restart_drain(self):
        called = False

        async def call_next(_request: Request) -> Response:
            nonlocal called
            called = True
            return Response("unsafe")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                response = await agent_server.require_agent_token(
                    http_request(method="POST", path="/api/sessions"),
                    call_next,
                )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(called)
        self.assertEqual(json.loads(response.body)["detail"]["code"], "server_restarting")

    async def test_restart_fence_blocks_new_interactive_and_scheduled_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=str(uuid.uuid4()),
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                turn_blocker = await agent_server.turn_start_blocker()
                job_blocker = await agent_server.scheduled_job_blocker("chat")
                with self.assertRaises(HTTPException) as update_check:
                    await agent_server.check_server_update()

        self.assertEqual(turn_blocker, "AgentsServer is restarting")
        self.assertEqual(job_blocker, "AgentsServer is restarting")
        self.assertEqual(update_check.exception.status_code, 409)

    async def test_restart_route_itself_is_not_counted_as_an_unsafe_mutation(self):
        observed_count = -1

        async def call_next(_request: Request) -> Response:
            nonlocal observed_count
            observed_count = agent_server.UNSAFE_HTTP_MUTATIONS_IN_FLIGHT
            return Response("accepted", status_code=202)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root):
                response = await agent_server.require_agent_token(
                    http_request(
                        method="POST",
                        extra_headers={
                            "content-type": "application/json",
                            "content-length": "256",
                        },
                    ),
                    call_next,
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed_count, 0)

    async def test_signal_failure_immediately_reopens_http_mutation_admission(self):
        called = False

        async def call_next(_request: Request) -> Response:
            nonlocal called
            called = True
            return Response("ok")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_id = str(uuid.uuid4())
            with restart_environment(root), \
                 patch.object(agent_server.time, "sleep"), \
                 patch.object(
                     agent_server.os,
                     "kill",
                     side_effect=OSError("denied"),
                 ):
                agent_server.write_server_restart_status(
                    phase="accepted",
                    request_id=request_id,
                    _source_instance_id=SERVER_INSTANCE_ID,
                )
                agent_server.signal_managed_server_restart(request_id)
                response = await agent_server.require_agent_token(
                    http_request(method="PATCH", path="/api/sessions/chat"),
                    call_next,
                )
                status = agent_server.read_server_restart_status()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(called)
        self.assertEqual(status["phase"], "failed")

    def test_post_route_declares_202_and_confirmations_are_literal_true(self):
        route = next(
            route
            for route in agent_server.app.routes
            if getattr(route, "path", None) == "/api/admin/restart"
            and "POST" in getattr(route, "methods", set())
        )
        self.assertEqual(route.status_code, 202)
        for invalid_confirmation in (False, 0, 1, 1.0, "true"):
            with self.subTest(confirmed=invalid_confirmation), self.assertRaises(
                Exception
            ):
                agent_server.ServerRestartRequest(
                    request_id=uuid.uuid4(),
                    expected_server_identity=SERVER_IDENTITY,
                    expected_server_instance_id=SERVER_INSTANCE_ID,
                    confirmed=invalid_confirmation,
                )
        forced = restart_body(force=True)
        self.assertTrue(forced.force)
        self.assertTrue(forced.force_confirmed)
        self.assertRegex(
            forced.expected_blocker_revision or "",
            r"^[0-9a-f]{64}$",
        )
        revision = "b" * 64
        for changes in (
            {"force": True},
            {"force_confirmed": True},
            {"force": False, "force_confirmed": True},
            {
                "force": True,
                "force_confirmed": False,
                "expected_blocker_revision": revision,
            },
            {
                "force": 1,
                "force_confirmed": True,
                "expected_blocker_revision": revision,
            },
            {
                "force": True,
                "force_confirmed": "true",
                "expected_blocker_revision": revision,
            },
            {
                "force": True,
                "force_confirmed": True,
            },
            {"expected_blocker_revision": revision},
            {
                "force": True,
                "force_confirmed": True,
                "expected_blocker_revision": "B" * 64,
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                agent_server.ServerRestartRequest(
                    request_id=uuid.uuid4(),
                    expected_server_identity=SERVER_IDENTITY,
                    expected_server_instance_id=SERVER_INSTANCE_ID,
                    confirmed=True,
                    **changes,
                )
        with self.assertRaises(Exception):
            agent_server.ServerRestartRequest(
                request_id=uuid.uuid4(),
                expected_server_identity=SERVER_IDENTITY,
                expected_server_instance_id=SERVER_INSTANCE_ID,
                confirmed=True,
                unexpected=True,
            )


if __name__ == "__main__":
    unittest.main()
