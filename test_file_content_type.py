import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


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


if __name__ == "__main__":
    unittest.main()
