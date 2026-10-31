"""Concurrent runtime reads share bounded work without starting provider CLIs."""
from __future__ import annotations

import ast
import base64
import json
import asyncio
import copy
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from pathlib import Path
import tempfile
from types import SimpleNamespace
import threading
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
import httpx
import codex_auth

SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"
NAMES = {
    "runtime_catalog", "runtime_catalog_scope", "file_signature", "refresh_runtime_catalog", "discover_runtime_catalog",
    "discover_runtime_catalog_within_budget", "runtime_catalog_budget_expired",
    "runtime_diagnostic", "runtime_diagnostic_budget_fallback",
    "probe_runtime_diagnostic", "store_runtime_diagnostic",
}
NODES = [node for node in ast.parse(SOURCE.read_text()).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
assert {node.name for node in NODES} == NAMES
for node in NODES:
    node.decorator_list = []
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *NODES], type_ignores=[])), str(SOURCE), "exec")


class RuntimeCatalogConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ns = {
            "Any": Any, "asyncio": asyncio, "copy": copy, "time": time, "Path": Path,
            "codex_auth": codex_auth, "DEFAULT_CWD": str(self.root), "existing_cwd": lambda cwd: cwd,
            "codex_app_server_env": lambda: {"CODEX_HOME": str(self.root / "native")},
            "CODEX_PROVIDER_STORE": SimpleNamespace(root=self.root / "codex"),
            "PROVIDER_CONNECTION_STORE": SimpleNamespace(root=self.root / "providers"),
            "Future": Future, "ThreadPoolExecutor": ThreadPoolExecutor,
            "copy_context": copy_context,
            "RUNTIME_CATALOG_DEADLINE": ContextVar("catalog_deadline", default=None),
            "RUNTIME_CATALOG_BUDGET_SECONDS": 25.0,
            "RUNTIME_DIAGNOSTIC_TTL_SECONDS": 60.0,
            "RUNTIME_DIAGNOSTICS_LOCK": threading.RLock(),
            "RUNTIME_DIAGNOSTICS": {}, "RUNTIME_DIAGNOSTIC_GENERATIONS": {},
            "RUNTIME_DIAGNOSTIC_PROBES": {}, "RUNTIME_CATALOG_TASKS": {}, "RUNTIME_DIAGNOSTIC_REVISION": 0,
            "RuntimeCatalogBudgetExpired": type("RuntimeCatalogBudgetExpired", (RuntimeError,), {}),
            "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude",
            "VALID_BACKENDS": {"codex", "claude", "cursor", "opencode"},
            "runtime_display_name": str, "now_iso": lambda: "synthetic-now",
            "runtime_diagnostic_payload": lambda backend, status, **fields: {"backend": backend, "status": status, **fields},
            "refresh_codex_app_server_binary": AsyncMock(),
            "refresh_codex_app_server_login": AsyncMock(),
        }
        exec(CODE, self.ns)
        self.catalog_requests_ready = 0

        async def to_thread(func, *args, **kwargs):
            result = await asyncio.to_thread(func, *args, **kwargs)
            if func is self.ns["runtime_catalog_scope"] and asyncio.current_task() not in {
                pending[1] for pending in self.ns["RUNTIME_CATALOG_TASKS"].values()
            }:
                self.catalog_requests_ready += 1
            return result

        self.ns["asyncio"] = SimpleNamespace(create_task=asyncio.create_task, current_task=asyncio.current_task,
                                              shield=asyncio.shield, to_thread=to_thread)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.tasks = []

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        pending = [entry[1] for entry in self.ns["RUNTIME_CATALOG_TASKS"].values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.assertFalse(self.ns["RUNTIME_DIAGNOSTIC_PROBES"])
        self.assertFalse(self.ns["RUNTIME_CATALOG_TASKS"])

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def started(self, event):
        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(event.wait, 5), 6))

    async def catalog_waiters(self, count):
        # Scope reads run off-thread. Keep the synthetic probe blocked until
        # every intended overlapping request has actually reached the join.
        async with asyncio.timeout(5):
            while self.catalog_requests_ready < count:
                await asyncio.sleep(0)

    def block(self):
        if not self.release.wait(5):
            raise AssertionError("test did not release synthetic probe")

    @staticmethod
    def diagnostic(backend="codex", status="ready"):
        return {"backend": backend, "status": status, "installed": True,
                "authenticated": status == "ready", "checked_at_epoch": time.time()}

    async def test_concurrent_http_reads_share_one_provider_pipeline(self):
        entered = threading.Event()
        calls = []
        lock = threading.Lock()

        def pipeline(backend, *, force_runtime_probe):
            with lock:
                calls.append((backend, force_runtime_probe))
                if len(calls) == len(self.ns["VALID_BACKENDS"]):
                    entered.set()
            self.block()
            return {"models": [{"value": backend}], "custom_provider": {"configured": backend == "codex"}}

        self.ns["discover_runtime_backend_catalog"] = pipeline
        app = FastAPI()
        app.get("/api/runtime/catalog")(self.ns["runtime_catalog"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            requests = [self.task(client.get("/api/runtime/catalog")) for _ in range(24)]
            await self.started(entered)
            await self.catalog_waiters(24)
            self.assertEqual(sorted(calls), [(backend, False) for backend in sorted(self.ns["VALID_BACKENDS"])])
            self.release.set()
            responses = await asyncio.gather(*requests)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertTrue(all(response.json() == responses[0].json() for response in responses))
        self.assertEqual(sorted(calls), [(backend, False) for backend in sorted(self.ns["VALID_BACKENDS"])])
        self.ns["refresh_codex_app_server_binary"].assert_not_awaited()
        self.ns["refresh_codex_app_server_login"].assert_not_awaited()

    async def test_cancelled_request_leaves_one_forced_pipeline_for_later_waiters(self):
        entered = threading.Event()
        calls = []

        def discover(*, force_runtime_probe):
            calls.append(force_runtime_probe)
            entered.set()
            self.block()
            return {"backends": {"codex": {"models": [{"value": "synthetic"}]}}}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        first = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        shared = self.ns["RUNTIME_CATALOG_TASKS"][True][1]
        self.assertFalse(shared.done())
        later = [self.task(self.ns["runtime_catalog"](refresh=bool(index % 2))) for index in range(10)]
        await self.catalog_waiters(11)
        self.assertEqual(calls, [True])
        self.release.set()
        results = await asyncio.gather(*later)
        self.ns["refresh_codex_app_server_binary"].assert_awaited_once_with(force=True)
        self.ns["refresh_codex_app_server_login"].assert_awaited_once_with(source="catalog_refresh")
        results[0]["backends"]["codex"]["models"][0]["value"] = "changed by one caller"
        self.assertEqual(results[1]["backends"]["codex"]["models"][0]["value"], "synthetic")

    async def test_cancellation_during_refresh_preflight_does_not_start_another_probe(self):
        entered, release_preflight = asyncio.Event(), asyncio.Event()

        async def preflight(**_kwargs):
            entered.set()
            await release_preflight.wait()

        self.ns["refresh_codex_app_server_binary"].side_effect = preflight
        self.ns["discover_runtime_catalog"] = Mock(return_value={"backends": {}})
        first = self.task(self.ns["runtime_catalog"](refresh=True))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        later = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.catalog_waiters(2)
        release_preflight.set()
        await later
        self.ns["refresh_codex_app_server_binary"].assert_awaited_once()
        self.ns["discover_runtime_catalog"].assert_called_once_with(force_runtime_probe=True)

    async def test_force_does_not_reuse_passive_cache_or_wait_out_its_budget(self):
        entered = {False: threading.Event(), True: threading.Event()}
        calls, deadlines = [], {}

        def discover(*, force_runtime_probe):
            calls.append(force_runtime_probe)
            deadlines[force_runtime_probe] = self.ns["RUNTIME_CATALOG_DEADLINE"].get()
            entered[force_runtime_probe].set()
            self.block()
            return {"forced": force_runtime_probe}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        passive = self.task(self.ns["runtime_catalog"]())
        await self.started(entered[False])
        forced = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[True])
        joining = [self.task(self.ns["runtime_catalog"](refresh=mode)) for mode in (False, True)]
        await self.catalog_waiters(4)
        self.assertEqual(calls, [False, True])
        self.assertTrue(all(24 < deadline - time.monotonic() <= 25 for deadline in deadlines.values()))
        self.release.set()
        self.assertEqual(await passive, {"forced": False})
        self.assertEqual(await forced, {"forced": True})
        self.assertEqual(await asyncio.gather(*joining), [{"forced": True}] * 2)

    async def test_catalog_failure_clears_shared_task_and_next_request_retries(self):
        entered = threading.Event()
        calls = []

        def discover(**_kwargs):
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                self.block()
                raise ValueError("synthetic discovery failure")
            return {"backends": {}}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        first = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered)
        second = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.catalog_waiters(2)
        self.release.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(value, ValueError) for value in failures))
        self.assertFalse(self.ns["RUNTIME_CATALOG_TASKS"])
        self.assertEqual(await self.ns["runtime_catalog"](refresh=True), {"backends": {}})
        self.assertEqual(len(calls), 2)

    async def test_auth_change_replaces_stale_catalog_without_old_cleanup_removing_new(self):
        entered = [threading.Event(), threading.Event()]
        release_new = threading.Event()
        self.addCleanup(release_new.set)
        calls = []
        self.ns["store_runtime_diagnostic"](self.diagnostic(status="unauthenticated"))

        def discover(**_kwargs):
            index = len(calls)
            calls.append(True)
            diagnostic = dict(self.ns["RUNTIME_DIAGNOSTICS"]["codex"])
            entered[index].set()
            if not (self.release if index == 0 else release_new).wait(5):
                raise AssertionError("catalog not released")
            return {"backends": {"codex": {"diagnostic": diagnostic, "available": diagnostic["status"] == "ready"}}}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        old = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[0])
        self.ns["store_runtime_diagnostic"](self.diagnostic())
        new = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[1])
        new_owner = self.ns["RUNTIME_CATALOG_TASKS"][True][1]
        self.release.set()
        self.assertFalse((await old)["backends"]["codex"]["available"])
        self.assertIs(self.ns["RUNTIME_CATALOG_TASKS"][True][1], new_owner)
        joining = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.catalog_waiters(3)
        release_new.set()
        self.assertTrue((await new)["backends"]["codex"]["available"])
        self.assertEqual((await joining)["backends"]["codex"]["diagnostic"]["status"], "ready")
        self.assertEqual(len(calls), 2)

    async def test_preflight_revision_and_probe_publication_keep_existing_catalog_shared(self):
        entered = threading.Event()
        calls = []

        async def preflight(**_kwargs):
            self.ns["store_runtime_diagnostic"](self.diagnostic())

        def discover(**_kwargs):
            calls.append(True)
            # Ordinary probe publication advances its generation, but does not
            # invalidate the catalog that is currently producing that result.
            self.ns["store_runtime_diagnostic"](self.diagnostic(), from_probe=True)
            entered.set()
            self.block()
            return {"backends": {}}

        self.ns["refresh_codex_app_server_login"].side_effect = preflight
        self.ns["discover_runtime_catalog_within_budget"] = discover
        first = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered)
        others = [self.task(self.ns["runtime_catalog"](refresh=mode)) for mode in (False, True)]
        await self.catalog_waiters(3)
        self.release.set()
        await asyncio.gather(first, *others)
        self.assertEqual(calls, [True])
        self.ns["refresh_codex_app_server_login"].assert_awaited_once()

    async def test_saved_endpoint_change_cannot_join_previous_catalog_metadata(self):
        settings = self.ns["CODEX_PROVIDER_STORE"].root / "settings.json"
        settings.parent.mkdir()
        settings.write_text("first endpoint")
        entered = [threading.Event(), threading.Event()]
        calls = []

        def discover(**_kwargs):
            selected = settings.read_text()
            index = len(calls)
            calls.append(selected)
            entered[index].set()
            self.block()
            return {"backends": {"codex": {"custom_provider": {"selected": selected}},
                                  "gemini": {"available": False, "native_credentials_present": False}}}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        first = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[0])
        settings.write_text("second saved endpoint")
        second = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[1])
        self.release.set()
        old, new = await asyncio.gather(first, second)
        self.assertEqual(old["backends"]["codex"]["custom_provider"]["selected"], "first endpoint")
        self.assertEqual(new["backends"]["codex"]["custom_provider"]["selected"], "second saved endpoint")
        self.assertEqual(new["backends"]["gemini"], {"available": False, "native_credentials_present": False})
        self.assertEqual(len(calls), 2)

    async def test_external_native_sign_in_cannot_join_a_pre_login_catalog(self):
        entered = [threading.Event(), threading.Event()]
        native = self.root / "native"
        native.mkdir()
        calls = []

        def discover(**_kwargs):
            status = "ready" if (native / "auth.json").exists() else "unauthenticated"
            index = len(calls)
            calls.append(status)
            entered[index].set()
            self.block()
            return {"status": status}

        self.ns["discover_runtime_catalog_within_budget"] = discover
        old = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[0])
        (native / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "synthetic-fixture-key"}))
        self.assertEqual(self.ns["RUNTIME_DIAGNOSTIC_REVISION"], 0)
        new = self.task(self.ns["runtime_catalog"](refresh=True))
        await self.started(entered[1])
        self.release.set()
        self.assertEqual(await old, {"status": "unauthenticated"})
        self.assertEqual(await new, {"status": "ready"})
        self.assertEqual(calls, ["unauthenticated", "ready"])

    def test_native_token_rotation_does_not_invalidate_shared_scope(self):
        native = self.root / "native"
        native.mkdir()
        claims = {"sub": "synthetic-user", "auth_time": 100}

        def write(access):
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            (native / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
                "id_token": "fixture." + encoded + ".fixture", "account_id": "synthetic-account",
                "access_token": access, "refresh_token": access}}))

        write("first-token")
        before = self.ns["runtime_catalog_scope"]()
        write("rotated-token")
        self.assertEqual(self.ns["runtime_catalog_scope"](), before)
        claims["auth_time"] = 200
        write("new-login-token")
        self.assertNotEqual(self.ns["runtime_catalog_scope"](), before)

    async def test_concurrent_diagnostics_share_probe_but_not_other_backend(self):
        entered = threading.Event()
        calls = []
        lock = threading.Lock()

        def probe(backend):
            with lock:
                calls.append(backend)
                if set(calls) == {"codex", "claude"}:
                    entered.set()
            self.block()
            return self.diagnostic(backend)

        self.ns["probe_runtime"] = probe
        requests = [self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True)) for _ in range(12)]
        requests.append(self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "claude", force=True)))
        await self.started(entered)
        # All submitted calls have reached their pending future before release.
        await asyncio.sleep(0.05)
        self.assertCountEqual(calls, ["codex", "claude"])
        self.release.set()
        results = await asyncio.gather(*requests)
        self.assertTrue(all(value["status"] == "ready" for value in results))
        self.assertCountEqual(calls, ["codex", "claude"])
        self.assertEqual(self.ns["RUNTIME_DIAGNOSTIC_GENERATIONS"], {"codex": 1, "claude": 1})

    async def test_auth_generation_change_does_not_join_or_publish_an_older_probe(self):
        entered = threading.Event()
        calls = []

        def probe(backend):
            calls.append(backend)
            if len(calls) == 1:
                entered.set()
                self.block()
                return self.diagnostic(backend, "unauthenticated")
            return self.diagnostic(backend, "ready")

        self.ns["probe_runtime"] = probe
        old = self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True))
        await self.started(entered)
        self.ns["store_runtime_diagnostic"](self.diagnostic())
        new = await asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True)
        self.assertEqual(new["status"], "ready")
        self.assertEqual(len(calls), 2)
        self.release.set()
        self.assertEqual((await old)["status"], "ready")
        self.assertEqual(self.ns["RUNTIME_DIAGNOSTICS"]["codex"]["status"], "ready")

    async def test_waiter_deadline_does_not_cancel_probe_or_refresh_old_cache(self):
        entered = threading.Event()
        previous = {**self.diagnostic(), "checked_at_epoch": 0.0}
        self.ns["store_runtime_diagnostic"](previous)

        def probe(backend):
            entered.set()
            self.block()
            return self.diagnostic(backend)

        self.ns["probe_runtime"] = Mock(side_effect=probe)
        owner = self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True))
        await self.started(entered)

        def waiter():
            token = self.ns["RUNTIME_CATALOG_DEADLINE"].set(time.monotonic() + 0.03)
            try:
                return self.ns["runtime_diagnostic"]("codex", force=True)
            finally:
                self.ns["RUNTIME_CATALOG_DEADLINE"].reset(token)

        self.assertEqual(await asyncio.to_thread(waiter), previous)
        self.assertEqual(self.ns["RUNTIME_DIAGNOSTICS"]["codex"], previous)
        self.assertFalse(owner.done())
        self.release.set()
        self.assertGreater((await owner)["checked_at_epoch"], 0)
        self.ns["probe_runtime"].assert_called_once_with("codex")

    async def test_probe_failure_clears_key_and_retry_preserves_exception(self):
        entered = threading.Event()
        calls = []

        def probe(backend):
            calls.append(backend)
            if len(calls) == 1:
                entered.set()
                self.block()
                raise TimeoutError("synthetic owner failure")
            return self.diagnostic(backend)

        self.ns["probe_runtime"] = probe
        first = self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True))
        await self.started(entered)
        second = self.task(asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True))
        await asyncio.sleep(0.05)
        self.release.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(value, TimeoutError) and str(value) == "synthetic owner failure" for value in failures))
        self.assertFalse(self.ns["RUNTIME_DIAGNOSTIC_PROBES"])
        self.assertEqual((await asyncio.to_thread(self.ns["runtime_diagnostic"], "codex", force=True))["status"], "ready")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
