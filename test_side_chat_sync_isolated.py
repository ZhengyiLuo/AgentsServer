"""Persisted side chats across HTTP clients/restarts, without live credentials."""
import asyncio
import ast
from copy import deepcopy
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException

import side_questions as side


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "side.sqlite3"
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.factories = []
        self.closed = []
        self.notices = []
        test = self

        class Handle:
            async def ask(self, question, *, history):
                test.calls.append((question, deepcopy(history)))
                test.started.set()
                await test.release.wait()
                return {"answer": "answer: " + question, "backend": "claude", "context_note": "native"}

            async def close(self):
                test.closed.append(self)

        async def factory(session, **options):
            self.factories.append(options)
            await options["persist_state"]({"backend": "claude", "native_id": "native-context"})
            return Handle()

        self.factory = factory
        self.runtime = side.SideQuestions(native_factory=factory, storage_path=self.path,
                                           notify=lambda session, notice: self.notice(session, notice))
        self.addAsyncCleanup(self.runtime.close)
        app = FastAPI()

        def authorize(request):
            if not request.headers.get("x-agentsdock-token"):
                raise HTTPException(401, "unauthorized")

        app.include_router(side.create_side_question_router(authorize=authorize,
                           session_exists=lambda session: session == "main", runtime=self.runtime))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                       headers={"x-agentsdock-token": "owner-secret"})
        self.addAsyncCleanup(self.client.aclose)
        self.url = "/api/sessions/main/side-chat"

    async def notice(self, session, notice):
        self.notices.append((session, deepcopy(notice)))

    async def snapshot(self):
        result = await self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["cache-control"], "no-store")
        return result.json()

    async def send(self, request_id="request1", question="question", snapshot=None):
        snapshot = snapshot or await self.snapshot()
        return await self.client.post(self.url, json={"request_id": request_id, "question": question,
            "side_chat_id": snapshot["side_chat_id"], "after_request_id": snapshot["last_request_id"]})

    async def settle(self):
        tasks = tuple(self.runtime.synced.tasks.values())
        self.release.set()
        await asyncio.gather(*tasks)

    async def test_two_clients_share_ordered_answers_and_native_history(self):
        first = await self.send()
        self.assertEqual(first.status_code, 202)
        pending = first.json()
        self.assertEqual(pending["exchanges"][0]["status"], "running")
        await self.started.wait()
        self.assertEqual(await self.snapshot(), pending)
        await self.settle()
        answered = await self.snapshot()
        self.assertEqual(answered["exchanges"][0]["answer"], "answer: question")
        self.assertGreater(answered["revision"], pending["revision"])
        self.assertEqual((await self.send("request2", "follow up", answered)).status_code, 202)
        await self.settle()
        self.assertEqual(self.calls, [("question", []), ("follow up", [{"question": "question", "response": "answer: question"}])])
        self.assertEqual([item["request_id"] for item in (await self.snapshot())["exchanges"]], ["request1", "request2"])
        self.assertTrue(self.notices)
        for session, notice in self.notices:
            self.assertEqual(set(notice), {"type", "session_id", "revision"})
            self.assertEqual(notice["type"], "side_chat_updated")

    async def test_duplicate_send_is_idempotent_busy_and_stale_submissions_preserve_existing(self):
        initial = await self.snapshot()
        self.assertEqual((await self.send(snapshot=initial)).status_code, 202)
        await self.started.wait()
        self.assertEqual((await self.send(snapshot=initial)).status_code, 202)
        self.assertEqual((await self.send("different", "racing device", initial)).status_code, 409)
        self.assertEqual((await self.send("request1", "different payload", initial)).status_code, 409)
        await self.settle()
        self.assertEqual((await self.send(snapshot=initial)).status_code, 202)
        self.assertEqual((await self.send("request2", "outdated follow up", initial)).status_code, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_simultaneous_devices_do_not_overwrite_each_others_questions(self):
        initial = await self.snapshot()
        results = await asyncio.gather(self.send("device1", "first", initial), self.send("device2", "second", initial))
        self.assertEqual(sorted(result.status_code for result in results), [202, 409])
        await self.settle()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len((await self.snapshot())["exchanges"]), 1)

    async def test_accepted_request_outlives_http_client_and_reconnect_reads_result(self):
        self.assertEqual((await self.send()).status_code, 202)
        await self.started.wait()
        await self.client.aclose()
        await self.settle()
        # A fresh transport uses the same authenticated owner, not a browser id.
        async with httpx.AsyncClient(transport=self.client._transport, base_url="http://test",
                                    headers={"x-agentsdock-token": "owner-secret"}) as client:
            result = (await client.get(self.url)).json()
        self.assertEqual(result["exchanges"][0]["status"], "completed")

    async def test_restart_keeps_native_binding_history_and_does_not_resend(self):
        await self.send()
        await self.settle()
        before = await self.snapshot()
        await self.runtime.close()
        replacement = side.SyncedSideChats(self.path, native_factory=self.factory)
        self.addAsyncCleanup(replacement.close)
        owner = next(iter(self.runtime.synced.locks))[0]
        restored = await replacement.snapshot(owner, "main")
        self.assertEqual(before, restored)
        self.assertEqual(len(self.calls), 1)
        await replacement.submit(owner, "main", "request2", "after restart", before["side_chat_id"], "request1")
        await asyncio.gather(*tuple(replacement.tasks.values()))
        self.assertEqual(self.factories[-1]["persisted_state"], {"backend": "claude", "native_id": "native-context"})
        self.assertEqual(self.calls[-1][1], [{"question": "question", "response": "answer: question"}])

    async def test_crash_marks_running_interrupted_without_automatic_replay(self):
        # Simulate committed admission without starting a provider in a dead process.
        store = side.SideChatStore(self.path)
        document = store.load("owner", "main")
        exchange = {"request_id": "crashed", "question": "not resent", "status": "running", "created_at": 1, "updated_at": 1}
        document["exchanges"].append(exchange)
        store.save("owner", "main", document, receipt=exchange)
        store.database.close()
        restored = await self.runtime.synced.snapshot("owner", "main")
        self.assertEqual(restored["exchanges"][0]["status"], "interrupted")
        self.assertEqual(restored["exchanges"][0]["error"], "side_question_interrupted")
        self.assertEqual(restored["last_request_id"], "crashed")
        self.assertFalse(self.calls)
        self.assertEqual((await self.runtime.synced.submit("owner", "main", "crashed", "not resent", restored["side_chat_id"])), restored)

    async def test_clear_fences_late_answer_and_old_receipt_even_after_restart(self):
        initial = await self.snapshot()
        await self.send(snapshot=initial)
        await self.started.wait()
        cleared = (await self.client.delete(f'{self.url}/{initial["side_chat_id"]}')).json()
        self.assertNotEqual(cleared["side_chat_id"], initial["side_chat_id"])
        self.assertFalse(cleared["exchanges"])
        self.release.set()
        await asyncio.sleep(0)
        self.assertEqual(await self.snapshot(), cleared)
        self.assertEqual((await self.send(snapshot=initial)).status_code, 409)
        self.assertEqual((await self.client.delete(f'{self.url}/{initial["side_chat_id"]}')).json(), cleared)
        self.assertEqual((await self.send("fresh", snapshot=cleared)).status_code, 202)
        await self.settle()
        self.assertEqual(len((await self.snapshot())["exchanges"]), 1)
        self.assertEqual(self.calls[-1][1], [])

    async def test_stop_retains_cancelled_question_then_followup_is_usable(self):
        await self.send()
        await self.started.wait()
        stopped = await self.client.delete(f"{self.url}/requests/request1")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["exchanges"][0]["status"], "cancelled")
        self.assertEqual(stopped.json()["last_request_id"], "request1")
        self.assertTrue(self.closed)
        self.assertFalse(self.runtime.active_work_labels())
        self.assertEqual((await self.send("followup")).status_code, 202)
        await self.settle()
        self.assertEqual((await self.snapshot())["exchanges"][-1]["status"], "completed")

    async def test_stop_overtaking_post_never_starts_delayed_request(self):
        initial = await self.snapshot()
        self.assertEqual((await self.client.delete(f"{self.url}/requests/request1")).status_code, 200)
        self.assertEqual((await self.send(snapshot=initial)).status_code, 409)
        self.assertFalse(self.calls)
        self.assertEqual((await self.send("fresh", snapshot=initial)).status_code, 202)
        await self.settle()

    async def test_owner_isolation_auth_and_missing_chat_apply_to_all_new_routes(self):
        await self.send()
        async with httpx.AsyncClient(transport=self.client._transport, base_url="http://test",
                                    headers={"x-agentsdock-token": "different-owner"}) as client:
            self.assertFalse((await client.get(self.url)).json()["exchanges"])
            self.assertFalse((await client.delete(f"{self.url}/requests/request1")).json()["exchanges"])
        denied = await self.client.get(self.url, headers={"x-agentsdock-token": ""})
        self.assertEqual(denied.status_code, 401)
        missing = await self.client.get(self.url.replace("/main/", "/missing/"))
        self.assertEqual(missing.status_code, 404)
        self.assertTrue(self.runtime.active_work_labels())

    async def test_write_failure_does_not_start_provider_or_lose_existing_data(self):
        initial = await self.snapshot()
        with patch.object(self.runtime.synced.store, "save", side_effect=OSError("disk full")):
            failed = await self.send(snapshot=initial)
        self.assertEqual(failed.status_code, 503)
        self.assertFalse(self.calls)
        self.assertEqual(await self.snapshot(), initial)
        self.assertEqual((await self.send(snapshot=initial)).status_code, 202)
        await self.settle()

    async def test_provider_errors_use_localizable_codes_without_private_diagnostics(self):
        self.runtime.synced.native_factory = AsyncMock(side_effect=side.SideQuestionError(410, "private provider path or details"))
        await self.send()
        await self.settle()
        failed = (await self.snapshot())["exchanges"][-1]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "side_question_http_410")
        self.runtime.synced.native_factory = AsyncMock(side_effect=RuntimeError("private upstream exception"))
        await self.send("next")
        await self.settle()
        self.assertEqual((await self.snapshot())["exchanges"][-1]["error"], "side_question_failed")

    async def test_completed_answer_write_failure_recovers_without_resending(self):
        await self.send()
        await self.started.wait()
        with patch.object(self.runtime.synced.store, "save", side_effect=OSError("disk full")):
            await self.settle()
            self.assertEqual((await self.client.get(self.url)).status_code, 503)
        recovered = await self.snapshot()
        self.assertEqual(recovered["exchanges"][0]["answer"], "answer: question")
        self.assertEqual(recovered["exchanges"][0]["status"], "completed")
        self.assertEqual(len(self.calls), 1)

    async def test_disk_full_does_not_prevent_stopping_native_work(self):
        await self.send()
        await self.started.wait()
        with patch.object(self.runtime.synced.store, "save", side_effect=OSError("disk full")):
            self.assertEqual((await self.client.delete(f"{self.url}/requests/request1")).status_code, 503)
            self.assertFalse(self.runtime.active_work_labels())
            self.assertTrue(self.closed)
        self.assertEqual((await self.snapshot())["exchanges"][0]["status"], "cancelled")

    async def test_retirement_closes_idle_provider_without_erasing_history(self):
        await self.send()
        await self.settle()
        saved = await self.snapshot()
        self.assertFalse(self.runtime.active_work_labels())
        await self.runtime.close()
        self.assertTrue(self.closed)
        self.assertEqual(await self.snapshot(), saved)
        self.assertEqual((await self.send("after-quiesce")).status_code, 202)
        await self.settle()

    async def test_parent_deletion_stops_only_its_side_work_then_removes_saved_history(self):
        await self.send()
        await self.started.wait()
        other = await self.runtime.synced.snapshot("owner", "other-main")
        await self.runtime.close_session("main")
        self.assertFalse(self.runtime.active_work_labels())
        self.assertTrue(self.closed)
        self.runtime.delete_history("main")
        self.assertFalse((await self.snapshot())["exchanges"])
        self.assertEqual(await self.runtime.synced.snapshot("owner", "other-main"), other)

    async def test_durable_codex_side_forks_are_not_importable_as_main_chats_after_clear(self):
        initial = await self.snapshot()
        owner = next(iter(self.runtime.synced.locks))[0]
        await self.runtime.synced._provider_state((owner, "main"), initial["side_chat_id"],
            {"backend": "codex", "codex": {"thread_id": "side-thread", "path": "/native/rollout.jsonl"}})
        await self.client.delete(f'{self.url}/{initial["side_chat_id"]}')
        await self.runtime.close()
        self.assertEqual(await asyncio.to_thread(self.runtime.provider_thread_ids), {"side-thread"})
        source = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        node = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == "local_codex_session_candidates")
        root = Path(self.temporary.name)
        paths = [root / "side-thread.jsonl", root / "normal-thread.jsonl"]
        for path in paths:
            path.write_text("")
        namespace = {"CODEX_SESSIONS_ROOT": root, "SIDE_QUESTIONS": self.runtime,
            "codex_session_index_thread_names": lambda: {}, "bounded_jsonl_paths": lambda _: paths,
            "codex_transcript_meta": lambda path: (path.stem, "/workspace"), "suppress": suppress,
            "codex_transcript_preview": lambda _: "preview", "Path": Path,
            "local_session_label": lambda text, fallback: text or fallback,
            "BACKEND_CODEX": "codex", "iso_from_timestamp": str, "Any": object}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<local-history>", "exec"), namespace)
        candidates = await asyncio.to_thread(namespace["local_codex_session_candidates"], set())
        self.assertEqual([row["provider_session_id"] for row in candidates], ["normal-thread"])


class ManagedUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_retirement_joins_side_chat_cleanup(self):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "close_managed_update_provider_managers")
        side_close = AsyncMock()
        namespace = {"asyncio": asyncio,
                     "managed_update_provider_quiesce_timeout_seconds": lambda: 1,
                     "close_claude_sdk_manager": AsyncMock(),
                     "close_codex_app_server_manager": AsyncMock(),
                     "SIDE_QUESTIONS": SimpleNamespace(close=side_close)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<update-quiesce>", "exec"), namespace)
        await namespace["close_managed_update_provider_managers"]()
        side_close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
