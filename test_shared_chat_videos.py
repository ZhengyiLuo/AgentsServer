"""Synthetic registry fixtures; never import the server runtime."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from shared_chat_videos import (SharedVideoUnavailable, normalize_shared_chat_videos,
                              open_shared_chat_video, shared_chat_video_descriptor)


class SharedVideoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = "art_" + "a" * 16
        self.folder = self.root / self.identity
        self.folder.mkdir()
        self.video = self.folder / "example.mp4"
        self.video.write_bytes(b"synthetic-video-bytes")
        self.meta = {"id": self.identity, "session_id": "chat-one", "filename": self.video.name,
            "path": str(self.video), "size": self.video.stat().st_size, "content_type": "video/mp4"}
        self.save_meta()
        self.secret = "synthetic-admin-secret"

    def save_meta(self):
        (self.folder / "meta.json").write_text(json.dumps(self.meta))

    def descriptor(self, **kwargs):
        return shared_chat_video_descriptor(self.root, self.secret, "chat-one", self.identity, **kwargs)

    def open(self, identity):
        return open_shared_chat_video(self.root, self.secret, "chat-one", identity)

    def test_descriptor_is_path_free_and_open_is_pinned_without_copying(self):
        before = set(self.root.rglob("*"))
        descriptor = self.descriptor()
        self.assertEqual(set(descriptor), {"id", "filename", "content_type", "size"})
        self.assertNotIn(str(self.root), json.dumps(descriptor))
        self.assertNotIn(self.secret, json.dumps(descriptor))
        self.assertEqual(self.descriptor(), descriptor)
        opened = self.open(descriptor["id"])
        try:
            self.assertEqual(os.read(opened["file_fd"], 100), b"synthetic-video-bytes")
            self.assertEqual(set(opened), {"file_fd", "filename", "content_type", "size", "file_revision"})
        finally:
            os.close(opened["file_fd"])
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_tampering_wrong_chat_and_secret_never_open(self):
        identity = self.descriptor()["id"]
        for secret, chat, value in ((self.secret, "other-chat", identity),
                ("changed-key", "chat-one", identity), ("", "chat-one", identity),
                (self.secret, "chat-one", identity[:-1] + ("a" if identity[-1] != "a" else "b")),
                (self.secret, "chat-one", "../../private")):
            with self.subTest(chat=chat), self.assertRaisesRegex(SharedVideoUnavailable, "^Shared video is unavailable$"):
                open_shared_chat_video(self.root, secret, chat, value)

    def test_modified_replaced_or_deleted_video_never_returns_new_bytes(self):
        identity = self.descriptor()["id"]
        original = self.video.stat()
        self.video.write_bytes(b"replacement-content!")
        os.utime(self.video, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(SharedVideoUnavailable): self.open(identity)
        self.video.unlink()
        self.video.write_bytes(b"synthetic-video-bytes")
        os.utime(self.video, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(SharedVideoUnavailable): self.open(identity)
        self.video.unlink()
        with self.assertRaises(SharedVideoUnavailable): self.open(identity)

    def test_metadata_identity_owner_name_type_size_and_path_are_checked(self):
        original = dict(self.meta)
        for fields in ({"id": "art_" + "b" * 16}, {"session_id": "other-chat"},
                {"filename": "../example.mp4"}, {"filename": "example\n.mp4"},
                {"path": str(self.root / "unrelated.mp4")}, {"content_type": "text/html"},
                {"size": 2}, {"size": True}):
            self.meta = {**original, **fields}; self.save_meta()
            with self.subTest(fields=fields), self.assertRaises(SharedVideoUnavailable): self.descriptor()

    def test_symlinks_hardlinks_and_non_regular_files_are_rejected(self):
        other = self.root / "outside.mp4"
        self.video.rename(other)
        self.video.symlink_to(other)
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()
        self.video.unlink(); os.link(other, self.video)
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()
        self.video.unlink(); self.video.mkdir()
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()

    def test_registry_and_metadata_symlinks_are_rejected(self):
        target = self.root / "elsewhere"
        self.folder.rename(target)
        self.folder.symlink_to(target, target_is_directory=True)
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()
        self.folder.unlink(); target.rename(self.folder)
        meta = self.folder / "meta.json"
        meta.rename(self.folder / "saved-meta.json")
        meta.symlink_to(self.folder / "saved-meta.json")
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()

    def test_ownerless_legacy_needs_proven_event_before_signing(self):
        self.meta.pop("session_id"); self.save_meta()
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()
        descriptor = self.descriptor(legacy_owner=True)
        opened = self.open(descriptor["id"]); os.close(opened["file_fd"])
        self.meta["session_id"] = "other-chat"; self.save_meta()
        with self.assertRaises(SharedVideoUnavailable): self.open(descriptor["id"])

    def test_octet_stream_mp4_is_normalized_without_exposing_other_types(self):
        self.meta["content_type"] = "application/octet-stream"; self.save_meta()
        self.assertEqual(self.descriptor()["content_type"], "video/mp4")
        self.video.rename(self.folder / "example.html")
        self.video = self.folder / "example.html"
        self.meta.update(filename=self.video.name, path=str(self.video), content_type="video/mp4")
        self.save_meta()
        with self.assertRaises(SharedVideoUnavailable): self.descriptor()

    def test_descriptor_validation_denies_other_media_paths_duplicates_and_oversize(self):
        descriptor = self.descriptor()
        for value in (None, {}, [descriptor, descriptor], [{**descriptor, "path": "/private"}],
                [{**descriptor, "filename": "../private.mp4"}], [{**descriptor, "content_type": "text/html"}],
                [{**descriptor, "content_type": []}], [{**descriptor, "size": -1}],
                [{**descriptor, "id": "https://foreign.example/video.mp4"}]):
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_shared_chat_videos(value)


if __name__ == "__main__":
    unittest.main()
