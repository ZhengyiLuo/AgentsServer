import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

import agent_server


def http_request(method: str, revision: int | str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if revision is not None:
        value = str(revision)
        headers.append((b"if-match", value.encode("ascii")))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/api/sessions/session-1/pins",
        "raw_path": b"/api/sessions/session-1/pins",
        "query_string": b"",
        "headers": headers,
        "client": ("test", 123),
        "server": ("test", 80),
    })


def response_json(response: object) -> dict:
    return json.loads(response.body)  # type: ignore[attr-defined]


class TimelinePinsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(agent_server, "STATE_DIR", self.state_root))
        self.stack.enter_context(patch.object(agent_server, "FILES_ROOT", self.state_root / "files"))
        self.stack.enter_context(patch.object(
            agent_server.STORE,
            "sessions",
            {
                "session-1": {
                    "id": "session-1",
                    "title": "Pinned chat",
                    "cwd": str(self.root),
                    "backend": "codex",
                },
            },
        ))
        self.stack.enter_context(patch.object(
            agent_server,
            "SESSION_LIFECYCLE_LOCKS",
            {},
        ))
        self.stack.enter_context(patch.object(
            agent_server,
            "TIMELINE_PIN_LOCK_STRIPES",
            tuple(asyncio.Lock() for _ in range(8)),
        ))
        self.stack.enter_context(patch.object(
            agent_server.HUB,
            "broadcast",
            AsyncMock(),
        ))
        agent_server.ensure_dirs("session-1")

    async def asyncTearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def add_event(self, event_id: str, *, session_id: str = "session-1") -> None:
        path = agent_server.events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "seq": 1,
                "id": event_id,
                "session_id": session_id,
                "type": "assistant_message",
                "ts": "2026-08-25T12:00:00Z",
                "text": "Pinned response",
            }) + "\n")

    def add_file(self, file_id: str, *, owner: str = "session-1") -> dict:
        directory = agent_server.FILES_ROOT / file_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "report.txt"
        path.write_text("private file body", encoding="utf-8")
        metadata = {
            "id": file_id,
            "session_id": owner,
            "kind": "artifact",
            "filename": "report.txt",
            "path": str(path),
            "source_path": "/workspace/report.txt",
            "content_type": "text/plain",
        }
        (directory / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        return metadata

    def message_pin(
        self,
        event_id: str,
        *,
        created_at: int = 100,
        session_id: str = "session-1",
    ) -> agent_server.TimelinePinRequest:
        return agent_server.TimelinePinRequest.model_validate({
            "id": f"message:{event_id}",
            "sessionId": session_id,
            "kind": "message",
            "eventId": event_id,
            "title": " Assistant ",
            "subtitle": "12:00 PM",
            "body": "Part one\n\nPart two",
            "createdAt": created_at,
        })

    async def test_empty_get_and_message_put_are_durable_and_idempotent(self) -> None:
        empty_response = await agent_server.get_session_timeline_pins("session-1")
        self.assertEqual(response_json(empty_response), {
            "pins": [],
            "revision": 0,
            "updatedAt": None,
            "capabilityVersion": 1,
        })
        self.assertEqual(empty_response.headers["etag"], '"0"')

        self.add_event("evt_one")
        request = http_request("PUT")
        first = await agent_server.put_session_timeline_pin(
            request,
            "session-1",
            "message:evt_one",
            self.message_pin("evt_one"),
        )
        first_payload = response_json(first)
        self.assertEqual(first_payload["revision"], 1)
        self.assertEqual(first_payload["pins"][0]["title"], "Assistant")
        self.assertEqual(first_payload["pins"][0]["body"], "Part one\n\nPart two")
        self.assertEqual(first.headers["etag"], '"1"')
        self.assertEqual(agent_server.HUB.broadcast.await_count, 1)

        # Once the desired representation is already present, a repeated PUT
        # remains successful even if its old If-Match revision is stale.
        retry = await agent_server.put_session_timeline_pin(
            http_request("PUT", '"0"'),
            "session-1",
            "message:evt_one",
            self.message_pin("evt_one"),
        )
        self.assertEqual(response_json(retry)["revision"], 1)
        self.assertEqual(agent_server.HUB.broadcast.await_count, 1)

        stored_path = agent_server.timeline_pins_path("session-1")
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["tombstones"], {})
        self.assertEqual(stored_path.stat().st_mode & 0o777, 0o600)

    async def test_concurrent_unconditional_item_puts_are_additive(self) -> None:
        self.add_event("evt_one")
        self.add_event("evt_two")

        first, second = await asyncio.gather(
            agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "message:evt_one",
                self.message_pin("evt_one", created_at=100),
            ),
            agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "message:evt_two",
                self.message_pin("evt_two", created_at=200),
            ),
        )

        self.assertEqual(sorted([response_json(first)["revision"], response_json(second)["revision"]]), [1, 2])
        current = response_json(await agent_server.get_session_timeline_pins("session-1"))
        self.assertEqual(current["revision"], 2)
        self.assertEqual(
            [pin["id"] for pin in current["pins"]],
            ["message:evt_two", "message:evt_one"],
        )

    async def test_if_match_conflict_returns_authoritative_camel_case_state(self) -> None:
        self.add_event("evt_one")
        self.add_event("evt_two")
        await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "message:evt_one",
            self.message_pin("evt_one"),
        )

        with self.assertRaises(HTTPException) as raised:
            await agent_server.put_session_timeline_pin(
                http_request("PUT", 0),
                "session-1",
                "message:evt_two",
                self.message_pin("evt_two"),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "pin_revision_conflict")
        self.assertEqual(raised.exception.detail["revision"], 1)
        self.assertEqual(raised.exception.detail["capabilityVersion"], 1)
        self.assertEqual(raised.exception.detail["pins"][0]["id"], "message:evt_one")
        self.assertEqual(raised.exception.headers["ETag"], '"1"')

        accepted = await agent_server.put_session_timeline_pin(
            http_request("PUT", '"1"'),
            "session-1",
            "message:evt_two",
            self.message_pin("evt_two"),
        )
        self.assertEqual(response_json(accepted)["revision"], 2)

    async def test_stale_migration_is_insert_only_and_cannot_overwrite_an_active_pin(self) -> None:
        self.add_event("evt_one")
        authoritative = self.message_pin("evt_one", created_at=200)
        authoritative.body = "Newest cross-device snapshot"
        await agent_server.put_session_timeline_pin(
            http_request("PUT", 0),
            "session-1",
            "message:evt_one",
            authoritative,
        )

        stale = self.message_pin("evt_one", created_at=100)
        stale.body = "Stale local-only snapshot"
        migration = await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "message:evt_one",
            stale,
        )

        payload = response_json(migration)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["pins"][0]["createdAt"], 200)
        self.assertEqual(payload["pins"][0]["body"], "Newest cross-device snapshot")
        self.assertEqual(agent_server.HUB.broadcast.await_count, 1)

    async def test_bounded_tombstone_ledger_closes_legacy_import_without_resurrection(self) -> None:
        self.add_event("evt_one")
        with patch.object(agent_server, "MAX_TIMELINE_PIN_TOMBSTONES_PER_SESSION", 1):
            await agent_server.delete_session_timeline_pin(
                http_request("DELETE", 0),
                "session-1",
                "message:evt_deleted_one",
            )
            closed = await agent_server.delete_session_timeline_pin(
                http_request("DELETE", 1),
                "session-1",
                "message:evt_deleted_two",
            )
            self.assertEqual(response_json(closed)["revision"], 2)
            raw = json.loads(
                agent_server.timeline_pins_path("session-1").read_text(encoding="utf-8")
            )
            self.assertTrue(raw["legacy_imports_closed"])
            self.assertEqual(raw["tombstones"], {})

            blocked = await agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "message:evt_one",
                self.message_pin("evt_one"),
            )
            self.assertEqual(response_json(blocked)["revision"], 2)
            self.assertEqual(response_json(blocked)["pins"], [])

            explicit = await agent_server.put_session_timeline_pin(
                http_request("PUT", 2),
                "session-1",
                "message:evt_one",
                self.message_pin("evt_one"),
            )
            self.assertEqual(response_json(explicit)["revision"], 3)
            self.assertEqual(len(response_json(explicit)["pins"]), 1)

    async def test_delete_tombstone_blocks_stale_migration_but_explicit_repin_clears_it(self) -> None:
        self.add_event("evt_one")
        pin = self.message_pin("evt_one")
        await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "message:evt_one",
            pin,
        )
        removed = await agent_server.delete_session_timeline_pin(
            http_request("DELETE", 1),
            "session-1",
            "message:evt_one",
        )
        self.assertEqual(response_json(removed)["revision"], 2)
        self.assertEqual(response_json(removed)["pins"], [])

        stale_migration = await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "message:evt_one",
            pin,
        )
        self.assertEqual(response_json(stale_migration)["revision"], 2)
        self.assertEqual(response_json(stale_migration)["pins"], [])

        repinned = await agent_server.put_session_timeline_pin(
            http_request("PUT", 2),
            "session-1",
            "message:evt_one",
            pin,
        )
        self.assertEqual(response_json(repinned)["revision"], 3)
        self.assertEqual(len(response_json(repinned)["pins"]), 1)
        stored = json.loads(
            agent_server.timeline_pins_path("session-1").read_text(encoding="utf-8")
        )
        self.assertNotIn("message:evt_one", stored["tombstones"])

        # A delete arriving before another device's first migration still
        # establishes an idempotent durable tombstone.
        absent = await agent_server.delete_session_timeline_pin(
            http_request("DELETE", 3),
            "session-1",
            "message:evt_absent",
        )
        self.assertEqual(response_json(absent)["revision"], 4)
        repeated = await agent_server.delete_session_timeline_pin(
            http_request("DELETE", 0),
            "session-1",
            "message:evt_absent",
        )
        self.assertEqual(response_json(repeated)["revision"], 4)

    async def test_delete_requires_a_revision_precondition(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await agent_server.delete_session_timeline_pin(
                http_request("DELETE"),
                "session-1",
                "message:evt_one",
            )
        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(raised.exception.detail["code"], "pin_revision_required")
        self.assertFalse(agent_server.timeline_pins_path("session-1").exists())

    async def test_file_pin_uses_authoritative_owner_and_metadata_not_client_text(self) -> None:
        self.add_file("file_foreign", owner="another-session")
        foreign = agent_server.TimelinePinRequest.model_validate({
            "id": "file:file_foreign",
            "sessionId": "session-1",
            "kind": "file",
            "fileId": "file_foreign",
            "fileSessionId": "session-1",
            "title": "Foreign",
            "subtitle": "must not leak",
            "createdAt": 100,
        })
        with self.assertRaises(HTTPException) as raised:
            await agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "file:file_foreign",
                foreign,
            )
        self.assertEqual(raised.exception.status_code, 404)

        metadata = self.add_file("file_owned")
        owned = agent_server.TimelinePinRequest.model_validate({
            "id": "file:file_owned",
            "sessionId": "session-1",
            "kind": "file",
            "fileId": "file_owned",
            "fileSessionId": "spoofed-session",
            "filename": "spoofed.txt",
            "content_type": "text/html",
            "path": "/tmp/spoofed",
            "source_path": "/tmp/spoofed-source",
            "title": "Report",
            "subtitle": "private file body",
            "createdAt": 200,
        })
        response = await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "file:file_owned",
            owned,
        )
        saved = response_json(response)["pins"][0]
        self.assertEqual(saved["fileSessionId"], "session-1")
        self.assertEqual(saved["filename"], metadata["filename"])
        self.assertEqual(saved["path"], metadata["path"])
        self.assertEqual(saved["source_path"], metadata["source_path"])
        self.assertEqual(saved["content_type"], "text/plain")
        self.assertIsNone(saved["subtitle"])
        self.assertIsNone(saved["body"])

    async def test_tombstoned_file_migration_does_not_require_a_deleted_file(self) -> None:
        await agent_server.delete_session_timeline_pin(
            http_request("DELETE", 0),
            "session-1",
            "file:file_gone",
        )
        stale = agent_server.TimelinePinRequest.model_validate({
            "id": "file:file_gone",
            "sessionId": "session-1",
            "kind": "file",
            "fileId": "file_gone",
            "title": "Deleted artifact",
            "createdAt": 100,
        })

        response = await agent_server.put_session_timeline_pin(
            http_request("PUT"),
            "session-1",
            "file:file_gone",
            stale,
        )
        self.assertEqual(response_json(response)["revision"], 1)
        self.assertEqual(response_json(response)["pins"], [])

    async def test_route_identity_anchor_and_payload_bounds_are_enforced(self) -> None:
        self.add_event("evt_one")
        with self.assertRaises(HTTPException) as mismatch:
            await agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "message:evt_other",
                self.message_pin("evt_one"),
            )
        self.assertEqual(mismatch.exception.status_code, 409)
        self.assertEqual(mismatch.exception.detail["code"], "pin_identity_mismatch")

        with self.assertRaises(HTTPException) as missing:
            await agent_server.put_session_timeline_pin(
                http_request("PUT"),
                "session-1",
                "message:evt_missing",
                self.message_pin("evt_missing"),
            )
        self.assertEqual(missing.exception.status_code, 404)

        with self.assertRaises(ValidationError):
            agent_server.TimelinePinRequest.model_validate({
                "id": "message:evt_one",
                "sessionId": "session-1",
                "kind": "message",
                "eventId": "evt_one",
                "title": "Assistant",
                "body": "x" * (agent_server.MAX_TIMELINE_PIN_BODY_CHARS + 1),
                "createdAt": 100,
            })

    async def test_corrupt_state_is_not_silently_overwritten(self) -> None:
        path = agent_server.timeline_pins_path("session-1")
        path.write_text('{"schema_version":1,"revision":"bad"}', encoding="utf-8")
        original = path.read_bytes()

        with self.assertRaises(HTTPException) as raised:
            await agent_server.get_session_timeline_pins("session-1")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "pinned_items_unavailable")
        self.assertEqual(path.read_bytes(), original)

    async def test_state_file_cannot_inject_a_pin_from_another_session(self) -> None:
        path = agent_server.timeline_pins_path("session-1")
        foreign = self.message_pin("evt_foreign", session_id="another-session")
        path.write_text(json.dumps({
            "schema_version": 1,
            "revision": 1,
            "updated_at": "2026-08-25T12:00:00Z",
            "pins": [foreign.model_dump(by_alias=True, exclude_none=False)],
            "tombstones": {},
            "legacy_imports_closed": False,
        }), encoding="utf-8")

        with self.assertRaises(HTTPException) as raised:
            await agent_server.get_session_timeline_pins("session-1")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "pinned_items_unavailable")

    async def test_session_store_deletion_removes_pins_with_owned_session_directory(self) -> None:
        state = agent_server.empty_timeline_pin_state()
        state["revision"] = 1
        state["updated_at"] = "2026-08-25T12:00:00Z"
        agent_server.write_timeline_pin_state_sync("session-1", state)
        path = agent_server.timeline_pins_path("session-1")
        self.assertTrue(path.is_file())

        store = agent_server.SessionStore()
        store.sessions = {"session-1": {"id": "session-1"}}
        store.save = AsyncMock()
        with patch.object(agent_server, "delete_session_owned_file_records", return_value=0), \
             patch.object(agent_server, "forget_event_seq", AsyncMock()):
            deleted = await store.delete("session-1")

        self.assertTrue(deleted)
        self.assertFalse(path.exists())

    async def test_health_and_openapi_advertise_pinned_items_contract(self) -> None:
        with patch.object(agent_server, "working_tmux_bin", return_value=None):
            health = await agent_server.health()
        capability = health["capabilities"]["pinned_items"]
        # This endpoint is additive and capability-gated; legacy clients keep
        # the current global contract while opting clients inspect this key.
        self.assertEqual(health["api_contract_version"], 27)
        self.assertEqual(capability["version"], 1)
        self.assertEqual(capability["max_items_per_session"], 500)
        self.assertEqual(capability["max_body_chars"], 24_000)
        self.assertTrue(capability["supports_if_match"])
        self.assertEqual(capability["websocket_event"], "timeline_pins_changed")

        paths = agent_server.app.openapi()["paths"]
        self.assertIn("/api/sessions/{session_id}/pins", paths)
        self.assertIn("get", paths["/api/sessions/{session_id}/pins"])
        item_path = paths["/api/sessions/{session_id}/pins/{item_id}"]
        self.assertIn("put", item_path)
        self.assertIn("delete", item_path)


if __name__ == "__main__":
    unittest.main()
