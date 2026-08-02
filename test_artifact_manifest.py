import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class ArtifactManifestContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_manifest_accepts_paths_and_titled_video_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated"
            generated.mkdir()
            report = generated / "report.txt"
            report.write_text("ready\n")
            video = generated / "preview.mov"
            video.write_bytes(b"preview-video")
            manifest = root / "manifests" / "current.json"
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({
                "files": [
                    str(report.resolve()),
                    {
                        "path": str(video.resolve()),
                        "title": "Demo",
                        "text": "Optional note",
                    },
                ],
            }))
            append_event = AsyncMock()

            with (
                patch.object(agent_server, "FILES_ROOT", root / "published"),
                patch.object(agent_server, "append_event", append_event),
            ):
                await agent_server.collect_manifest(
                    "sess-artifacts",
                    "run-artifacts",
                    manifest,
                    final=True,
                )

            self.assertFalse(manifest.exists())
            self.assertEqual(append_event.await_count, 2)
            records = {
                call.args[2]["artifact"]["filename"]: call.args[2]["artifact"]
                for call in append_event.await_args_list
            }
            self.assertEqual(records["report.txt"]["content_type"], "text/plain")
            self.assertEqual(records["preview.mov"]["content_type"], "video/quicktime")
            self.assertEqual(records["preview.mov"]["title"], "Demo")
            self.assertEqual(records["preview.mov"]["text"], "Optional note")
            self.assertEqual(records["preview.mov"]["source_path"], str(video.resolve()))
            self.assertTrue(Path(records["preview.mov"]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
