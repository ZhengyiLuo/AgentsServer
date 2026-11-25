"""Independent durable side chats; no live provider, credentials or main chat."""
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

import side_questions as side


class MultipleSideChatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "side.sqlite3"
        self.calls, self.handles, self.notices = [], [], []
        self.gates, self.progress = {}, {}
        test = self

        class Handle:
            def __init__(self, marker):
                self.marker, self.closed = marker, False

            async def ask(self, question, *, history, on_progress=None):
                test.calls.append((self.marker, question, deepcopy(history)))
                test.progress[question] = on_progress
                await test.gates.setdefault(question, asyncio.Event()).wait()
                return {"answer": "answer: " + question, "backend": "codex"}

            async def close(self):
                self.closed = True

        async def factory(session, **options):
            state = options["persisted_state"] or {"backend": "codex", "codex": {"thread_id": uuid.uuid4().hex}}
            await options["persist_state"](state)
            handle = Handle(state["codex"]["thread_id"])
            self.handles.append(handle)
            return handle

        self.factory = factory
        self.runtime = side.SideQuestions(native_factory=factory, storage_path=self.path,
                                           notify=self.notify)
        self.addAsyncCleanup(self.runtime.close)
        app = FastAPI()

        def authorize(request):
            if not request.headers.get("x-agentsdock-token"):
                raise HTTPException(401, "unauthorized")

        app.include_router(side.create_side_question_router(authorize=authorize,
            session_exists=lambda session: session == "main", runtime=self.runtime))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                       headers={"x-agentsdock-token": "owner"})
        self.addAsyncCleanup(self.client.aclose)
        self.base = "/api/sessions/main/side-chat"

    async def notify(self, session, event):
        self.notices.append(deepcopy(event))

    async def get(self, chat_id=None):
        result = await self.client.get(self.base + ("s/" + chat_id if chat_id else ""))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def create(self, chat_id="second"):
        result = await self.client.post(self.base + "s", json={"side_chat_id": chat_id})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    async def send(self, snapshot, request, question):
        result = await self.client.post(self.base, json={"request_id": request, "question": question,
            "side_chat_id": snapshot["side_chat_id"], "after_request_id": snapshot["last_request_id"]})
        self.assertEqual(result.status_code, 202, result.text)
        for _ in range(100):
            if question in self.progress:
                break
            await asyncio.sleep(0)
        self.assertIn(question, self.progress)
        return result.json()

    async def finish(self, question):
        self.gates[question].set()
        for _ in range(100):
            if all(task.done() for task in self.runtime.synced.tasks.values()):
                return
            await asyncio.sleep(0)

    async def test_create_list_read_keep_default_and_owner_isolation(self):
        first = await self.get()
        second = await self.create()
        self.assertEqual(await self.create(), second)
        self.assertEqual(await self.get(), first)
        self.assertEqual(await self.get(second["side_chat_id"]), second)
        listing = await self.client.get(self.base + "s")
        self.assertEqual([chat["side_chat_id"] for chat in listing.json()["side_chats"]],
                         [first["side_chat_id"], second["side_chat_id"]])
        self.assertEqual(listing.headers["cache-control"], "no-store")
        denied = await self.client.get(self.base + "s/second", headers={"x-agentsdock-token": "other"})
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.handles, [])
        self.assertTrue(all(event.get("side_chat_id") for event in self.notices))

    async def test_concurrent_chats_keep_history_progress_and_cancellation_independent(self):
        first, second = await self.get(), await self.create()
        await self.send(first, "r1", "one")
        await self.send(second, "r2", "two")
        self.assertEqual(len(self.runtime.synced.tasks), 2)
        self.assertNotEqual(self.calls[0][0], self.calls[1][0])
        await self.progress["two"]([{"id": "tool", "kind": "tool", "text": "Two only", "status": "running"}])
        self.assertNotIn("activity", (await self.get())["exchanges"][0])
        stopped = await self.client.delete(self.base + "/requests/r1", params={"side_chat_id": first["side_chat_id"]})
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual((await self.get("second"))["exchanges"][0]["status"], "running")
        await self.finish("two")
        second = await self.get("second")
        await self.send(second, "r3", "follow-up")
        self.assertEqual(self.calls[-1][2], [{"question": "two", "response": "answer: two"}])
        self.assertEqual(self.calls[-1][0], self.calls[1][0])
        await self.finish("follow-up")
        self.assertEqual((await self.get())["exchanges"][0]["status"], "cancelled")

    async def test_clear_only_selected_chat_and_fences_late_requests_and_progress(self):
        first, second = await self.get(), await self.create()
        await self.send(first, "r1", "one")
        await self.send(second, "r2", "two")
        cleared = await self.client.delete(self.base + "/second")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        replacement = cleared.json()
        self.assertNotEqual(replacement["side_chat_id"], "second")
        self.assertEqual(replacement["conversation_id"], second["conversation_id"])
        self.assertEqual((await self.get())["exchanges"][0]["status"], "running")
        await self.progress["two"]([{"id": "late", "kind": "answer", "text": "late", "status": "running"}])
        again = await self.client.delete(self.base + "/second")
        self.assertEqual(again.json()["side_chat_id"], replacement["side_chat_id"])
        stale = await self.client.post(self.base, json={"request_id": "late", "question": "late",
                                                       "side_chat_id": "second"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((await self.get(replacement["side_chat_id"]))["exchanges"], [])
        listing = (await self.client.get(self.base + "s")).json()["side_chats"]
        self.assertEqual([chat["side_chat_id"] for chat in listing], [first["side_chat_id"], replacement["side_chat_id"]])
        await self.finish("one")

    async def test_stop_before_extra_chat_post_and_request_ids_cannot_cross_chats(self):
        first, second = await self.get(), await self.create()
        stopped = await self.client.delete(self.base + "/requests/late", params={"side_chat_id": "second"})
        self.assertEqual(stopped.json()["side_chat_id"], "second")
        late = await self.client.post(self.base, json={"request_id": "late", "question": "late", "side_chat_id": "second"})
        self.assertEqual(late.status_code, 409)
        await self.send(first, "r1", "one")
        wrong = await self.client.delete(self.base + "/requests/r1", params={"side_chat_id": "second"})
        self.assertEqual(wrong.status_code, 409)
        retry = await self.client.post(self.base, json={"request_id": "r1", "question": "one", "side_chat_id": "second"})
        self.assertEqual(retry.status_code, 409)
        await self.finish("one")
        self.assertEqual((await self.get("second"))["exchanges"], [])

    async def test_restart_retains_each_native_reference_and_history(self):
        first, second = await self.get(), await self.create()
        await self.send(first, "r1", "one")
        await self.send(second, "r2", "two")
        await self.finish("one")
        await self.finish("two")
        markers = [call[0] for call in self.calls]
        await self.runtime.close()
        second = await self.get("second")
        await self.send(second, "r3", "resumed")
        self.assertEqual(self.calls[-1][0], markers[1])
        self.assertEqual(self.calls[-1][2], [{"question": "two", "response": "answer: two"}])
        self.assertEqual((await self.get())["exchanges"][0]["answer"], "answer: one")
        self.assertEqual(self.runtime.provider_thread_ids(), set(markers))
        await self.finish("resumed")

    async def test_extra_chat_clear_retires_only_its_provider_when_storage_fails(self):
        first, second = await self.get(), await self.create()
        await self.send(first, "r1", "one")
        await self.send(second, "r2", "two")
        with patch.object(self.runtime.synced.store, "save", side_effect=OSError("disk unavailable")):
            result = await self.client.delete(self.base + "/second")
        self.assertEqual(result.status_code, 503)
        self.assertFalse(self.handles[0].closed)
        self.assertTrue(self.handles[1].closed)
        listing = (await self.client.get(self.base + "s")).json()["side_chats"]
        self.assertEqual(listing[0]["exchanges"][0]["status"], "running")
        self.assertEqual(listing[1]["exchanges"], [])
        self.assertNotEqual(listing[1]["side_chat_id"], second["side_chat_id"])
        await self.finish("one")

    async def test_startup_recovers_unfinished_extra_chat_without_resending(self):
        owner = hashlib.sha256(b"owner").hexdigest()
        store = side.SideChatStore(self.path)
        default = store.load(owner, "main")
        document = store.create(owner, "main", "second")
        exchange = {"request_id": "unfinished", "question": "accepted", "status": "running"}
        document["exchanges"].append(exchange)
        store.save(owner, "main", document, chat_key="second", receipt=exchange)
        store.close()
        restored = await self.get("second")
        self.assertEqual(restored["exchanges"][0]["status"], "interrupted")
        self.assertEqual(restored["last_request_id"], "unfinished")
        self.assertEqual((await self.get())["side_chat_id"], default["side_chat_id"])
        self.assertEqual(self.calls, [])

    async def test_existing_singleton_database_migrates_without_replacing_context(self):
        owner = hashlib.sha256(b"owner").hexdigest()
        original = {"session_id": "main", "side_chat_id": "existing", "revision": 4,
                    "exchanges": [{"request_id": "old", "question": "old question", "answer": "old answer", "status": "completed"}],
                    "last_request_id": "old", "_provider_state": {"backend": "codex", "codex": {"thread_id": "existing-native"}}}
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("CREATE TABLE chats (owner TEXT, session TEXT, document TEXT NOT NULL, PRIMARY KEY(owner, session))")
            database.execute("INSERT INTO chats VALUES(?,?,?)", (owner, "main", json.dumps(original)))
            database.commit()
        restored = await self.get()
        self.assertEqual(restored, {**{key: value for key, value in original.items() if not key.startswith("_")},
                                    "conversation_id": "default"})
        await self.create()
        await self.send(restored, "new", "after migration")
        self.assertEqual(self.calls[-1][0], "existing-native")
        self.assertEqual(self.calls[-1][2], [{"question": "old question", "response": "old answer"}])
        await self.finish("after migration")


if __name__ == "__main__":
    unittest.main()
