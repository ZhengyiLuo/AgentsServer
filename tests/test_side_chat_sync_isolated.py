"""Persisted side chats across HTTP clients/restarts, without live credentials."""
import asyncio
import ast
from copy import deepcopy
from contextlib import suppress
import json
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException

import side_questions as side
from codex_side_question import NativeCodexSideChat


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
            async def ask(self, question, *, history, on_progress=None):
                test.calls.append((question, deepcopy(history)))
                test.progress = on_progress
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

    async def test_progress_is_visible_before_answer_persisted_and_cannot_resurrect_cancel_or_clear(self):
        await self.send()
        await self.started.wait()
        activity = [{"id": "reason:reasoning_summary", "kind": "reasoning_summary", "text": "Inspecting files", "status": "running"}]
        await self.progress(activity)
        running = await self.snapshot()
        self.assertEqual(running["exchanges"][0]["activity"], activity)
        self.assertEqual(running["exchanges"][0]["status"], "running")
        owner = hashlib.sha256(b"owner-secret").hexdigest()
        # Read storage without opening a second service (which performs startup recovery).
        with sqlite3.connect(self.path) as persisted:
            raw = persisted.execute("SELECT document FROM chats WHERE owner=? AND session=?", (owner, "main")).fetchone()[0]
            self.assertEqual(json.loads(raw)["exchanges"][0]["activity"], activity)
        cancelled = await self.client.delete(self.url + "/requests/request1")
        self.assertEqual(cancelled.status_code, 200)
        await self.progress([{**activity[0], "text": "too late"}])
        terminal = await self.snapshot()
        self.assertEqual(terminal["exchanges"][0]["status"], "cancelled")
        self.assertEqual(terminal["exchanges"][0]["activity"], activity)
        await self.runtime.synced.clear(owner, "main", terminal["side_chat_id"])
        await self.progress(activity)
        self.assertEqual((await self.snapshot())["exchanges"], [])

    async def test_progress_write_failure_keeps_native_answer_and_recovers_without_resending(self):
        await self.send()
        await self.started.wait()
        activity = [{"id": "a", "kind": "answer", "text": "Partial", "status": "running"}]
        with patch.object(self.runtime.synced.store, "save", side_effect=OSError("disk unavailable")):
            await self.progress(activity)
            await self.settle()
        restored = await self.snapshot()
        self.assertEqual(restored["exchanges"][0]["status"], "completed")
        self.assertEqual(restored["exchanges"][0]["answer"], "answer: question")
        self.assertEqual(restored["exchanges"][0]["activity"], activity)
        self.assertEqual(len(self.calls), 1)

    def use_buffered_native_adapter(self):
        """Real native item adapter and store; only provider transport is fake."""
        ready = asyncio.Event()
        natives = []
        async def factory(session, **options):
            native = NativeCodexSideChat("parent", executable="unused", model=None, env={})
            native.thread_id = "side"
            native._open = AsyncMock()
            packets = [
                {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "side", "itemId": "r", "delta": "Received partial"}},
                {"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "parent", "itemId": "parent", "delta": "Wrong parent"}},
                {"method": "item/completed", "params": {"threadId": "side", "item": {"type": "userMessage", "id": "user", "text": "Private input"}}},
                {"method": "item/completed", "params": {"threadId": "side", "item": {"type": "reasoning", "id": "hidden", "encryptedContent": "Private reasoning"}}},
            ]
            async def next_notification():
                if packets:
                    return packets.pop(0)
                ready.set()
                await asyncio.Event().wait()
            turn = SimpleNamespace(next_notification=next_notification, close=AsyncMock())
            native._client = SimpleNamespace(start_turn=AsyncMock(return_value=turn), close=AsyncMock())
            natives.append(native)
            class Handle:
                async def ask(self, question, *, history, on_progress=None):
                    return {"backend": "codex", "answer": await native.ask(question, on_progress=on_progress)}
                def snapshot_progress(self, task):
                    return native.snapshot_progress(task)
                async def close(self):
                    await native.close()
            return Handle()
        self.runtime.synced.native_factory = factory
        return ready, natives

    async def test_stop_snapshots_received_native_partials_before_coalesced_publish(self):
        ready, natives = self.use_buffered_native_adapter()
        await self.send()
        await ready.wait()
        native = natives[0]
        buffered = native._progress
        self.assertEqual((await self.snapshot())["exchanges"][0].get("activity", []), [])
        task = next(iter(self.runtime.synced.tasks.values()))
        self.assertIsNone(native.snapshot_progress(asyncio.current_task()))
        copied = native.snapshot_progress(task)
        copied[0]["text"] = "Mutation of copied snapshot"
        self.assertEqual(native.snapshot_progress(task)[0]["text"], "Received partial")
        # Another authenticated owner's same opaque request has no authority.
        await self.runtime.synced.cancel("different-owner", "main", "request1")
        self.assertFalse(task.done())
        stopped = await self.client.delete(self.url + "/requests/request1")
        self.assertEqual(stopped.status_code, 200)
        exchange = stopped.json()["exchanges"][0]
        self.assertEqual(exchange["status"], "cancelled")
        self.assertEqual([item["text"] for item in exchange["activity"]], ["Received partial"])
        self.assertIsNone(native.snapshot_progress(task))
        buffered.receive({"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "side", "itemId": "r", "delta": " Too late"}})
        await buffered.flush()
        self.assertEqual((await self.snapshot())["exchanges"][0], exchange)
        ready.clear()
        await self.send("next")
        await ready.wait()
        next_task = next(iter(self.runtime.synced.tasks.values()))
        self.assertIsNone(native.snapshot_progress(next_task))
        await self.client.delete(self.url + "/requests/request1")
        self.assertFalse(next_task.done())
        stopped_next = await self.client.delete(self.url + "/requests/next")
        self.assertEqual([item["text"] for item in stopped_next.json()["exchanges"][-1]["activity"]], ["Received partial"])

    async def _check_clear_stop_race(self, *, clear_first):
        ready, natives = self.use_buffered_native_adapter()
        initial = await self.snapshot()
        await self.send(snapshot=initial)
        await ready.wait()
        buffered = natives[0]._progress
        paths = ["requests/request1", initial["side_chat_id"]]
        results = await asyncio.gather(*(self.client.delete(self.url + "/" + path)
                                        for path in (reversed(paths) if clear_first else paths)))
        self.assertEqual([result.status_code for result in results], [200, 200])
        snapshot = await self.snapshot()
        self.assertNotEqual(snapshot["side_chat_id"], initial["side_chat_id"])
        self.assertEqual(snapshot["exchanges"], [])
        ready.clear()
        await self.send("next", snapshot=snapshot)
        await ready.wait()
        next_task = next(iter(self.runtime.synced.tasks.values()))
        buffered.receive({"method": "item/agentMessage/delta", "params": {"threadId": "side", "itemId": "old", "delta": "Old answer"}})
        await buffered.flush()
        await self.client.delete(self.url + "/" + initial["side_chat_id"])
        await self.client.delete(self.url + "/requests/request1")
        self.assertFalse(next_task.done())
        current = await self.snapshot()
        self.assertEqual([item["request_id"] for item in current["exchanges"]], ["next"])
        self.assertNotIn("Old answer", json.dumps(current))

    async def test_clear_racing_stop_discards_buffer_and_fences_next_native_request(self):
        await self._check_clear_stop_race(clear_first=False)

    async def test_stop_racing_clear_cannot_restore_deleted_native_buffer(self):
        await self._check_clear_stop_race(clear_first=True)

    async def test_stop_keeps_latest_buffer_even_when_prior_progress_cannot_be_saved(self):
        ready, natives = self.use_buffered_native_adapter()
        await self.send()
        await ready.wait()
        buffered = natives[0]._progress
        with patch.object(self.runtime.synced.store, "save", side_effect=sqlite3.OperationalError("storage unavailable")):
            await buffered.flush()
            buffered.receive({"method": "item/reasoning/summaryTextDelta", "params": {"threadId": "side", "itemId": "r", "delta": " before Stop"}})
            stopped = await self.client.delete(self.url + "/requests/request1")
            self.assertEqual(stopped.status_code, 503)
            self.assertFalse(self.runtime.active_work_labels())
            # A retry cannot claim durable success while the terminal write fails.
            self.assertEqual((await self.client.delete(self.url + "/requests/request1")).status_code, 503)
        snapshot = await self.snapshot()
        self.assertEqual(snapshot["exchanges"][0]["status"], "cancelled")
        self.assertEqual(snapshot["exchanges"][0]["activity"][0]["text"], "Received partial before Stop")

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

    async def _check_control_after_progress_write_failure(self, *, clear, recover):
        initial = await self.snapshot()
        await self.send(snapshot=initial)
        await self.started.wait()
        activity = [{"id": "partial", "kind": "reasoning_summary", "text": "Retained partial", "status": "running"}]
        key = (hashlib.sha256(b"owner-secret").hexdigest(), "main")
        store = self.runtime.synced.store
        save = store.save
        failed = True
        def controlled_save(*args, **kwargs):
            if failed:
                raise OSError("disk full")
            return save(*args, **kwargs)
        with patch.object(store, "save", side_effect=controlled_save):
            await self.progress(activity)
            self.assertIn(key, self.runtime.synced.pending_writes)
            failed = not recover
            target = initial["side_chat_id"] if clear else "requests/request1"
            result = await self.client.delete(f"{self.url}/{target}")
            self.assertEqual(result.status_code, 200 if recover else 503)
            self.assertFalse(self.runtime.active_work_labels())
            self.assertTrue(self.closed)
            pending = self.runtime.synced.pending_writes.get(key)
            if recover:
                self.assertIsNone(pending)
            elif clear:
                self.assertNotEqual(pending["side_chat_id"], initial["side_chat_id"])
                self.assertEqual(pending["exchanges"], [])
                self.assertEqual((await self.client.delete(f"{self.url}/{target}")).status_code, 503)
            else:
                self.assertEqual(pending["exchanges"][0]["status"], "cancelled")
                self.assertEqual(pending["exchanges"][0]["activity"], activity)
        recovered = await self.snapshot()
        self.assertNotIn(key, self.runtime.synced.pending_writes)
        if clear:
            self.assertEqual(recovered["exchanges"], [])
            self.assertNotEqual(recovered["side_chat_id"], initial["side_chat_id"])
            self.assertEqual((await self.send(snapshot=initial)).status_code, 409)
        else:
            self.assertEqual(recovered["exchanges"][0]["status"], "cancelled")
            self.assertEqual(recovered["exchanges"][0]["activity"], activity)
        await self.progress([{**activity[0], "text": "late update"}])
        self.assertEqual(await self.snapshot(), recovered)
        self.assertEqual(len(self.calls), 1)

    async def test_pending_progress_write_failure_cannot_prevent_stop(self):
        await self._check_control_after_progress_write_failure(clear=False, recover=False)

    async def test_pending_progress_write_failure_cannot_prevent_clear(self):
        await self._check_control_after_progress_write_failure(clear=True, recover=False)

    async def test_stop_discards_superseded_pending_progress_after_storage_recovers(self):
        await self._check_control_after_progress_write_failure(clear=False, recover=True)

    async def test_clear_discards_superseded_pending_progress_after_storage_recovers(self):
        await self._check_control_after_progress_write_failure(clear=True, recover=True)

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
        source = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
        node = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == "local_codex_session_candidates")
        root = Path(self.temporary.name)
        paths = [root / "side-thread.jsonl", root / "normal-thread.jsonl"]
        for path in paths:
            path.write_text("")
        def history_paths(scan_root, *, excluded_directory_names):
            self.assertEqual(scan_root, root)
            self.assertEqual(excluded_directory_names, frozenset({"archived_sessions"}))
            return paths

        def transcript_meta(path, *, exclude_subagents):
            self.assertTrue(exclude_subagents)
            return path.stem, "/workspace"

        namespace = {"CODEX_SESSIONS_ROOT": root, "SIDE_QUESTIONS": self.runtime,
            "codex_session_index_thread_names": lambda: {}, "bounded_jsonl_paths": history_paths,
            "codex_transcript_meta": transcript_meta, "suppress": suppress,
            "codex_transcript_preview": lambda _: "preview", "Path": Path,
            "local_session_label": lambda text, fallback: text or fallback,
            "BACKEND_CODEX": "codex", "iso_from_timestamp": str, "Any": object}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<local-history>", "exec"), namespace)
        candidates = await asyncio.to_thread(namespace["local_codex_session_candidates"], set())
        self.assertEqual([row["provider_session_id"] for row in candidates], ["normal-thread"])

    async def test_unavailable_runtime_defaults_never_block_retained_history(self):
        await self.send()
        await self.settle()
        before = await self.snapshot()
        def unavailable(session):
            raise HTTPException(409, "Custom endpoint is temporarily unavailable")
        self.runtime.synced.runtime_settings_for_session = unavailable
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual(len(before["exchanges"]), 1)

    async def test_runtime_choices_roundtrip_idempotency_defaults_and_reconnect(self):
        self.runtime.synced.runtime_settings_for_session = lambda session: {"model": "parent-model", "effort": "high"}
        initial = await self.snapshot()
        self.assertEqual((initial["model"], initial["effort"]), ("parent-model", "high"))
        self.runtime.synced.runtime_settings_for_session = lambda session: {"model": "parent-changed", "effort": "ultra"}
        self.assertEqual(await self.snapshot(), initial)
        calls = []
        async def factory(session, **options):
            settings = dict((options.get("persisted_state") or {}).get("runtime_settings") or options["initial_runtime_settings"])
            class Handle:
                async def ask(self, question, *, history, runtime_settings=None, on_progress=None):
                    settings.update(runtime_settings or {})
                    calls.append((question, deepcopy(history), dict(settings)))
                    await options["persist_state"]({"runtime_settings": dict(settings),
                        "codex": {"thread_id": "same-native-side", "path": "/native-side.jsonl"}})
                    return {"answer": "answer " + question, "backend": "codex", **settings}
                async def close(self):
                    pass
            return Handle()
        self.runtime.synced.native_factory = factory
        payload = {"request_id": "runtime-1", "question": "first", "side_chat_id": initial["side_chat_id"],
                   "model": "side-model", "effort": "low"}
        accepted = await self.client.post(self.url, json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        await self.settle()
        completed = await self.snapshot()
        self.assertEqual((completed["model"], completed["effort"]), ("side-model", "low"))
        self.assertEqual((await self.client.post(self.url, json=payload)).status_code, 202)
        self.assertEqual((await self.client.post(self.url, json={**payload, "effort": "high"})).status_code, 409)
        self.assertEqual((await self.client.post(self.url, json={**payload, "model": "other-model"})).status_code, 409)
        self.runtime.synced._expire((next(iter(self.runtime.synced.handles))[0], "main"))
        followup = {"request_id": "runtime-2", "question": "followup", "side_chat_id": initial["side_chat_id"], "after_request_id": "runtime-1"}
        self.assertEqual((await self.client.post(self.url, json=followup)).status_code, 202)
        await self.settle()
        restored = await self.snapshot()
        self.assertEqual(restored["side_chat_id"], initial["side_chat_id"])
        self.assertEqual((restored["model"], restored["effort"]), ("side-model", "low"))
        self.assertEqual(len(restored["exchanges"]), 2)
        self.assertEqual(calls[1][1], [{"question": "first", "response": "answer first"}])
        self.assertEqual(calls[1][2], {"model": "side-model", "effort": "low"})
        self.assertEqual((await self.client.post(self.url, json={**followup, "request_id": "bad-model", "model": ""})).status_code, 400)
        self.assertEqual((await self.client.post(self.url, json={**followup, "request_id": "bad-effort", "effort": 3})).status_code, 400)



class ManagedUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_retirement_joins_side_chat_cleanup(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
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
