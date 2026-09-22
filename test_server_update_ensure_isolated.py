"""Real reconciliation functions with disposable signing keys and fake runtime.

Read/compile only allowlisted AST nodes; never import the monolithic server.
"""
from __future__ import annotations

import ast
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from typing import Any, Literal
from types import SimpleNamespace
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import BaseModel, Field

import update_runner


class ServerUpdateEnsureTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("agent_server.py")
        names = {
            "ServerUpdateTargetExpectation", "ServerUpdateRequest", "ServerUpdateEnsureRequest",
            "require_server_update_target", "require_exact_server_update_target",
            "server_update_error_detail", "public_server_update_status",
            "_start_server_update", "ensure_server_update", "ensure_server_update_endpoint",
            "advance_pending_server_update_once", "reconcile_pending_server_update_after_startup",
            "server_update_health_projection",
        }
        tree = ast.parse(source.read_text())
        nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        assert {node.name for node in nodes} == names
        for node in nodes:
            node.decorator_list = []
        cls.compiled = compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes,
        ], type_ignores=[])), str(source), "exec")

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = Ed25519PrivateKey.generate()
        self.key = self.root / "public.pem"
        self.key.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        self.status = {"phase": "idle", "track": "beta", "current_version": "1.0.4-beta.11"}
        self.write = Mock(side_effect=self.write_status)
        self.native_auth = Mock()
        self.ns = {
            "Any": Any, "Literal": Literal, "BaseModel": BaseModel, "Field": Field,
            "asyncio": asyncio, "HTTPException": HTTPException, "uuid": uuid, "re": re,
            "os": SimpleNamespace(environ={}), "Path": Path,
            "SERVER_UPDATE_OPERATION_LOCK": asyncio.Lock(), "ACTIVE_LOCK": asyncio.Lock(),
            "QUEUE_LOCK": asyncio.Lock(), "UNSAFE_HTTP_MUTATION_ADMISSION_LOCK": asyncio.Lock(),
            "SERVER_INSTANCE_ID": "instance-current", "server_identity": lambda: "server-current",
            "SERVER_VERSION": "1.0.4-beta.11", "API_CONTRACT_VERSION": 28,
            "SERVER_UPDATE_ACTIVE_PHASES": {"starting", "checking", "downloading", "verifying", "installing", "restarting"},
            "SERVER_UPDATE_PENDING_PHASE": "pending", "SERVER_UPDATE_START_GRACE_SECONDS": 45,
            "SERVER_UPDATE_PUBLIC_KEY": self.key, "SERVER_UPDATE_RUNNER": self.key,
            "verify_npm_release_envelope": update_runner.verify_npm_release_envelope,
            "version_key": update_runner.version_key, "server_release_track": update_runner.release_track,
            "server_release_transition_allowed": lambda target, current, track: update_runner.release_transition_allowed(current, target, track),
            "read_server_update_status": lambda: dict(self.status), "write_fresh_server_update_status": self.write,
            "managed_server_restart_blocks_work": lambda: False,
            "managed_update_provider_quiesce_failed": lambda: False,
            "managed_update_provider_quiesce_in_progress": lambda: False,
            "managed_server_update_is_pending": lambda status: status.get("phase") == "pending",
            "managed_server_force_update_is_pending": lambda status: False,
            "server_update_is_active": lambda status: True,
            "server_update_status_age_seconds": lambda status: 0,
            "tmux_capability": lambda: {"available": True},
            "ensure_managed_update_tmux_isolated": lambda: None,
            "prepare_provider_background_work_snapshot": AsyncMock(return_value={}),
            "server_update_active_session_ids_locked": lambda: ["busy-chat"],
            "update_blocking_queued_turn_count_locked": lambda: 0,
            "unsafe_http_mutation_count_locked": lambda: 0,
            "BUSY_SESSIONS": {"busy-chat"}, "SERVER_MAINTENANCE_SESSIONS": set(),
            "provider_background_work_labels_from_snapshot": lambda snapshot: [],
            "server_update_blocker_counts": lambda active, queued, providers, mutations: {"active": len(active)},
            "server_update_has_blockers": lambda counts: any(counts.values()),
            "update_utc_now": lambda: "2026-09-20T00:00:00Z",
            "server_update_pending_message": lambda version, counts: "Waiting for idle",
            "require_native_admin_control": self.native_auth,
            "SERVER_UPDATE_STATUS_FILE": self.root / "status.json",
            "server_update_status_lock": lambda path: nullcontext(),
            "_write_fresh_server_update_status_unlocked": Mock(side_effect=lambda **changes: changes),
        }
        exec(self.compiled, self.ns)
        for name in ("ServerUpdateTargetExpectation", "ServerUpdateRequest", "ServerUpdateEnsureRequest"):
            self.ns[name].model_rebuild(_types_namespace=self.ns)

    def write_status(self, **changes):
        self.assertTrue(self.ns["SERVER_UPDATE_OPERATION_LOCK"].locked())
        self.assertTrue(self.ns["ACTIVE_LOCK"].locked())
        self.assertTrue(self.ns["QUEUE_LOCK"].locked())
        self.assertTrue(self.ns["UNSAFE_HTTP_MUTATION_ADMISSION_LOCK"].locked())
        self.status = {"current_version": self.ns["SERVER_VERSION"], **changes}
        return dict(self.status)

    def request(self, version="1.0.4-beta.12", *, api=28):
        manifest = {"schema": 2, "version": version, "track": update_runner.release_track(version),
                    "api_contract_version": api, "distribution": "npm",
                    "npm": {"name": "@agentsdock/server", "version": version,
                            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(b"payload").digest()).decode()},
                    "archive": {"name": f"server-{version}.tgz",
                                "url": f"https://registry.npmjs.org/@agentsdock/server/-/server-{version}.tgz",
                                "sha256": hashlib.sha256(b"payload").hexdigest(), "size": 7}}
        payload = json.dumps(manifest).encode()
        return self.ns["ServerUpdateEnsureRequest"](
            expected_server_identity="server-current", expected_server_instance_id="instance-current",
            manifest_base64=base64.b64encode(payload).decode(),
            signature_base64=base64.b64encode(self.private.sign(payload)).decode(),
        )

    async def test_busy_server_queues_signed_payload_under_admission_locks(self):
        request = self.request()
        result = await self.ns["ensure_server_update"](request)
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["reconciliation"], "pending")
        self.assertTrue(result["when_idle"])
        self.assertEqual(self.status["_npm_release"]["manifest_base64"], request.manifest_base64)
        self.assertNotIn("_npm_release", result)
        self.assertEqual(result["server_identity"], "server-current")

    async def test_retry_settles_exact_failed_handoff_before_preparing_again(self):
        self.status.update(phase="failed", update_id="a" * 32,
            error_code="server_update_handoff_release_failed", retryable=True, _execution_handoff={"owned": True})
        original = dict(self.status)
        self.ns["os"].environ["AGENTS_SERVER_INSTALL_DIR"] = str(self.root)
        def settle(**changes):
            self.assertTrue(self.ns["SERVER_UPDATE_OPERATION_LOCK"].locked())
            self.status.update(changes)
            return dict(self.status)
        self.ns["_write_server_update_status_unlocked"] = Mock(side_effect=settle)
        self.ns["SERVER_ROOT"] = Path(__file__).parent
        async def prepare(status, **unused):
            self.assertIsNone(status.get("_execution_handoff"))
            self.assertIsNone(status.get("error_code"))
            return {"phase": "pending", "target_version": "1.0.4-beta.12"}, None
        self.ns["prepare_scheduled_server_update"] = prepare
        with patch("update_handoff.retry_failed_handoff", return_value={"released": True}) as cleanup:
            result = await self.ns["ensure_server_update"](self.request())
        cleanup.assert_called_once_with(self.root.resolve(), self.ns["SERVER_UPDATE_STATUS_FILE"], original)
        self.assertEqual(result["phase"], "pending")
        self.assertNotIn("_execution_handoff", result)

    async def test_failed_handoff_retry_does_not_create_another_update(self):
        self.status.update(phase="failed", update_id="a" * 32,
            error_code="server_update_handoff_release_failed", retryable=True)
        self.ns["os"].environ["AGENTS_SERVER_INSTALL_DIR"] = str(self.root)
        with patch("update_handoff.retry_failed_handoff", side_effect=RuntimeError("foreign lease")):
            with self.assertRaises(HTTPException) as error:
                await self.ns["ensure_server_update"](self.request())
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail["code"], "server_update_handoff_release_failed")
        self.write.assert_not_called()

    async def test_parallel_requests_join_one_durable_reservation(self):
        results = await asyncio.gather(*(self.ns["ensure_server_update"](self.request()) for _ in range(2)))
        self.write.assert_called_once()
        self.assertEqual(results[0]["schedule_id"], results[1]["schedule_id"])

    async def test_installed_newer_never_downgrades_or_clears_other_receipt(self):
        before = dict(self.status)
        result = await self.ns["ensure_server_update"](self.request("1.0.4-beta.10"))
        self.assertEqual(result["reconciliation"], "current")
        self.assertEqual(self.status, before)
        self.write.assert_not_called()

    async def test_same_version_split_update_joins_its_unfinished_activation(self):
        self.ns["SERVER_VERSION"] = "1.0.4-beta.12"
        self.ns["EXECUTION_MAINTENANCE"] = SimpleNamespace(worker_instance_id="owned-worker")
        self.ns["os"].environ["AGENTS_SERVER_INSTALL_DIR"] = str(self.root)
        for phase in ("pending", "installing"):
            self.status.update(phase=phase, track="beta", target_version="1.0.4-beta.12", update_id="owned-update")
            with patch("execution_update_status.current_components", return_value=False):
                result = await self.ns["ensure_server_update"](self.request())
            self.assertEqual(result["phase"], phase)
            self.assertEqual(result["update_id"], "owned-update")
            self.assertNotEqual(result["reconciliation"], "current")
        self.write.assert_not_called()

    async def test_same_version_split_update_requires_actual_component_proof(self):
        self.ns["SERVER_VERSION"] = "1.0.4-beta.12"
        self.ns["EXECUTION_MAINTENANCE"] = SimpleNamespace(worker_instance_id="owned-worker")
        self.ns["os"].environ["AGENTS_SERVER_INSTALL_DIR"] = str(self.root)
        with patch("execution_update_status.current_components", return_value=False):
            with self.assertRaises(HTTPException) as rejected:
                await self.ns["ensure_server_update"](self.request())
        self.assertEqual(rejected.exception.detail["code"], "server_update_recovery_required")
        with patch("execution_update_status.current_components", side_effect=RuntimeError("native ownership unavailable")):
            with self.assertRaises(HTTPException) as rejected:
                await self.ns["ensure_server_update"](self.request())
        self.assertEqual(rejected.exception.detail["code"], "server_update_recovery_required")
        with patch("execution_update_status.current_components", return_value=True) as proof:
            result = await self.ns["ensure_server_update"](self.request())
        self.assertEqual(result["reconciliation"], "current")
        self.assertEqual(proof.call_args.kwargs["expected_worker_instance"], "owned-worker")
        self.write.assert_not_called()

    async def test_newer_compatible_other_channel_is_satisfied_without_mutation(self):
        before = dict(self.status)
        result = await self.ns["ensure_server_update"](self.request("1.0.3"))
        self.assertEqual(result["reconciliation"], "current")
        self.assertEqual(self.status, before)
        self.write.assert_not_called()

    async def test_newer_other_channel_different_contract_is_not_satisfied(self):
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update"](self.request("1.0.3", api=29))
        self.assertEqual(raised.exception.detail["code"], "server_update_incompatible")
        self.write.assert_not_called()

    async def test_newer_installed_different_contract_is_not_satisfied(self):
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update"](self.request("1.0.4-beta.10", api=29))
        self.assertEqual(raised.exception.detail["code"], "server_update_incompatible")
        self.write.assert_not_called()

    async def test_newer_pending_or_running_release_is_joined(self):
        for phase in ("pending", "installing"):
            with self.subTest(phase=phase):
                self.status.update(phase=phase, target_version="1.0.4-beta.13", schedule_id="a" * 32)
                result = await self.ns["ensure_server_update"](self.request())
                self.assertEqual(result["target_version"], "1.0.4-beta.13")
                self.assertEqual(result["schedule_id"], "a" * 32)
        self.write.assert_not_called()

    async def test_lower_pending_is_observed_without_cancelling_its_owner(self):
        self.status.update(phase="pending", target_version="1.0.4-beta.12", schedule_id="a" * 32)
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update"](self.request("1.0.4-beta.13"))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["schedule_id"], "a" * 32)
        self.assertTrue(raised.exception.detail["retryable"])
        self.write.assert_not_called()

    async def test_signed_app_channel_survives_busy_reservation_restart_and_resume(self):
        for current, target in (("1.0.4-beta.9", "1.0.5"), ("1.0.3", "1.0.5-beta.1")):
            with self.subTest(current=current, target=target):
                self.ns["SERVER_VERSION"] = current
                self.status = {"phase": "idle", "track": update_runner.release_track(current), "current_version": current}
                self.ns["signed_release_manifest"] = AsyncMock(side_effect=AssertionError("The paired signed release needs no channel rediscovery"))
                request = self.request(target)
                result = await self.ns["ensure_server_update"](request)
                self.assertEqual(result["phase"], "pending")
                self.assertEqual(result["target_version"], target)
                self.assertEqual(result["track"], update_runner.release_track(target))
                receipt = dict(self.status)
                self.assertEqual(self.ns["reconcile_pending_server_update_after_startup"](receipt), receipt)
                resumed = await self.ns["advance_pending_server_update_once"]()
                self.assertEqual(resumed["schedule_id"], receipt["schedule_id"])
                self.assertEqual(resumed["_npm_release"], receipt["_npm_release"])
                self.ns["signed_release_manifest"].assert_not_called()

    async def test_signature_tampering_is_rejected_before_mutation(self):
        request = self.request()
        request.signature_base64 = base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update"](request)
        self.assertEqual(raised.exception.detail["code"], "server_update_descriptor_invalid")
        self.write.assert_not_called()

    async def test_legacy_forward_beta_promotion_needs_no_channel_rediscovery(self):
        self.ns["SERVER_VERSION"] = "1.0.4-beta.9"
        self.ns["signed_release_manifest"] = AsyncMock(side_effect=AssertionError("Forward promotion does not use the downgrade exception"))
        result = await self.ns["_start_server_update"](self.ns["ServerUpdateRequest"](
            version="1.0.5", track="stable", when_idle=True,
            expected_server_identity="server-current", expected_server_instance_id="instance-current",
        ))
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["target_version"], "1.0.5")
        self.ns["signed_release_manifest"].assert_not_called()

    async def test_endpoint_requires_auth_and_fresh_instance_before_verification(self):
        body = self.request()
        request = object()
        self.native_auth.side_effect = HTTPException(401, "unauthorized")
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update_endpoint"](body, request)
        self.assertEqual(raised.exception.status_code, 401)
        self.native_auth.side_effect = None
        body.expected_server_instance_id = "stale-instance"
        with self.assertRaises(HTTPException) as raised:
            await self.ns["ensure_server_update_endpoint"](body, request)
        self.assertEqual(raised.exception.detail["code"], "server_update_target_changed")
        self.write.assert_not_called()

    async def test_saved_reservation_is_reverified_on_restart_and_resume(self):
        await self.ns["ensure_server_update"](self.request())
        receipt = dict(self.status)
        self.assertEqual(self.ns["reconcile_pending_server_update_after_startup"](receipt), receipt)
        resumed = await self.ns["advance_pending_server_update_once"]()
        self.assertEqual(resumed["schedule_id"], receipt["schedule_id"])
        self.assertEqual(resumed["_npm_release"], receipt["_npm_release"])
        self.status["_npm_release"] = {**self.status["_npm_release"], "signature_base64": base64.b64encode(b"x" * 64).decode()}
        result = self.ns["reconcile_pending_server_update_after_startup"](dict(self.status))
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["error_code"], "server_update_schedule_invalid")
        self.assertNotIn("_npm_release", result)
        with self.assertRaises(HTTPException) as raised:
            await self.ns["advance_pending_server_update_once"]()
        self.assertEqual(raised.exception.status_code, 400)

    async def test_health_projection_is_bounded_and_excludes_signed_payload(self):
        self.status.update(phase="pending", schedule_id="a" * 32, updated_at="changed",
                           target_version="x" * 161, _npm_release={"manifest_base64": "private"})
        result = self.ns["server_update_health_projection"]()
        self.assertEqual(set(result), {"phase", "update_id", "schedule_id", "target_version", "updated_at"})
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["updated_at"], "changed")
        self.assertIsNone(result["target_version"])
        self.assertNotIn("_npm_release", result)
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
