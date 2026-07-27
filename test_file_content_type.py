import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server
from fastapi import HTTPException


class FileContentTypeTests(unittest.TestCase):
    def test_generic_upload_type_falls_back_to_filename(self) -> None:
        self.assertEqual(
            agent_server.effective_content_type("screenshot.png", "application/octet-stream"),
            "image/png",
        )
        self.assertEqual(
            agent_server.effective_content_type("clip.mov", "binary/octet-stream; charset=binary"),
            "video/quicktime",
        )
        self.assertEqual(
            agent_server.effective_content_type("notes.txt", "text/plain"),
            "text/plain",
        )

    def test_legacy_file_records_are_normalized_without_rewriting_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_root = root / "file_legacy"
            file_root.mkdir()
            metadata_path = file_root / "meta.json"
            metadata = {
                "id": "file_legacy",
                "session_id": "session-1",
                "filename": "screenshot.png",
                "path": str(file_root / "screenshot.png"),
                "content_type": "application/octet-stream",
            }
            metadata_path.write_text(json.dumps(metadata))

            with patch.object(agent_server, "FILES_ROOT", root), patch.object(
                agent_server, "iter_session_events", return_value=iter(())
            ):
                records = agent_server.list_session_file_records("session-1")

            self.assertEqual(records[0]["content_type"], "image/png")
            self.assertEqual(json.loads(metadata_path.read_text())["content_type"], "application/octet-stream")

    def test_legacy_event_record_is_normalized_for_file_listing(self) -> None:
        event = {
            "id": "event-1",
            "seq": 10,
            "type": "file_uploaded",
            "file": {
                "id": "file_event",
                "session_id": "session-1",
                "filename": "photo.jpeg",
                "content_type": "application/octet-stream",
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            agent_server, "FILES_ROOT", Path(temporary)
        ), patch.object(agent_server, "iter_session_events", return_value=iter((event,))):
            records = agent_server.list_session_file_records("session-1")

        self.assertEqual(records[0]["content_type"], "image/jpeg")
        self.assertEqual(records[0]["event_id"], "event-1")

    def test_file_listing_rejects_an_explicit_foreign_owner_from_fork_history(self) -> None:
        event = {
            "id": "forked-artifact",
            "seq": 10,
            "session_id": "child-session",
            "type": "artifact_created",
            "forked": True,
            "artifact": {
                "id": "parent-file",
                "session_id": "parent-session",
                "filename": "parent-output.png",
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            agent_server, "FILES_ROOT", Path(temporary)
        ), patch.object(agent_server, "iter_session_events", return_value=iter((event,))):
            records = agent_server.list_session_file_records("child-session")

        self.assertEqual(records, [])

    def test_transcript_read_suppresses_foreign_file_events_but_keeps_legacy_files(self) -> None:
        events = [
            {
                "id": "foreign",
                "seq": 1,
                "session_id": "child-session",
                "type": "artifact_created",
                "artifact": {
                    "id": "parent-file",
                    "session_id": "parent-session",
                    "filename": "parent.png",
                },
            },
            {
                "id": "legacy",
                "seq": 2,
                "session_id": "child-session",
                "type": "artifact_created",
                "artifact": {"id": "legacy-file", "filename": "legacy.png"},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.read_events("child-session")

        self.assertEqual([event["id"] for event in result], ["legacy"])


class FileContentTypeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_filter_includes_legacy_generic_png(self) -> None:
        event = {
            "id": "event-1",
            "seq": 10,
            "type": "file_uploaded",
            "file": {
                "id": "file_event",
                "session_id": "session-1",
                "filename": "photo.png",
                "content_type": "application/octet-stream",
            },
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            agent_server, "FILES_ROOT", Path(temporary)
        ), patch.object(agent_server, "iter_session_events", return_value=iter((event,))), patch.object(
            agent_server.STORE, "sessions", {"session-1": {"id": "session-1"}}
        ):
            response = await agent_server.list_session_files(
                "session-1", limit=None, offset=0, content_prefix="image/"
            )

        self.assertEqual(response["total"], 1)
        self.assertEqual(response["files"][0]["content_type"], "image/png")

    async def test_file_event_lookup_rejects_an_explicit_foreign_owner(self) -> None:
        event = {
            "id": "forked-artifact",
            "seq": 10,
            "session_id": "child-session",
            "type": "artifact_created",
            "artifact": {
                "id": "parent-file",
                "session_id": "parent-session",
                "filename": "parent-output.png",
            },
        }
        with patch.object(
            agent_server, "iter_session_events", return_value=iter((event,))
        ), patch.object(
            agent_server.STORE, "sessions", {"child-session": {"id": "child-session"}}
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.get_session_file_event("child-session", "parent-file")

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
