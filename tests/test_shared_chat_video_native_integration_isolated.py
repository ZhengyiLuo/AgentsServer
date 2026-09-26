"""AST-allowlisted native media adapters with synthetic registry state only."""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from interactive_chat_native import shared_events
from shared_chat_videos import SharedVideoUnavailable, open_shared_chat_video, shared_chat_video_descriptor


MEDIA_FUNCTIONS = frozenset({
    "shared_chat_event_videos", "project_shared_chat_event_videos", "shared_chat_video_events",
    "open_shared_chat_video_for_share", "public_chat_share_session_exists",
    "file_record_belongs_to_session", "event_establishes_session_file_origin",
    "event_files_belong_to_session", "is_client_visible_event",
})


def register_synthetic_video(root, digit="d", owner="chat-one"):
    identity = "art_" + digit * 16
    folder = root / identity
    folder.mkdir(parents=True)
    video = folder / "clip.mp4"
    video.write_bytes(b"published synthetic video")
    meta = {"id": identity, "filename": video.name, "path": str(video), "size": video.stat().st_size,
            "content_type": "video/mp4"}
    if owner is not None:
        meta["session_id"] = owner
    (folder / "meta.json").write_text(json.dumps(meta))
    return identity


def install_media_glue(namespace, tree):
    """Install only allowlisted declarations; never execute module startup."""
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in MEDIA_FUNCTIONS]
    assert {node.name for node in nodes} == MEDIA_FUNCTIONS
    constants = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES"
        for target in node.targets)]
    assert len(constants) == 1
    namespace.update(asyncio=asyncio, os=os, re=re, HTTPException=HTTPException,
        shared_events=shared_events, SharedVideoUnavailable=SharedVideoUnavailable,
        open_shared_chat_video=open_shared_chat_video, shared_chat_video_descriptor=shared_chat_video_descriptor)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *constants, *nodes], type_ignores=[])),
                 "<isolated-native-video-adapters>", "exec"), namespace)


class SharedChatVideoNativeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-shared-video-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = {}
        install_media_glue(self.native, self.tree)
        self.native.update(FILES_ROOT=self.root, AGENT_TOKEN="synthetic-native-video-secret",
            STORE=SimpleNamespace(sessions={"chat-one": {"id": "chat-one"}, "chat-two": {"id": "chat-two"}}),
            DELETING_SESSIONS=set(), DELETED_SESSION_TOMBSTONES=set())
        self.own = self.register("a", "chat-one")
        self.other = self.register("b", "chat-two")
        self.legacy = self.register("c", None)
        self.fake = {"id": "video_ZmFrZQ." + "a" * 64, "filename": "fake.mp4", "content_type": "video/mp4", "size": 1}

    def register(self, digit, owner):
        return register_synthetic_video(self.root, digit, owner)

    def event(self, kind="turn_started", **fields):
        return {"type": kind, "session_id": "chat-one", "id": "event-one", "prompt": "Visible message",
                "file_ids": [self.own], **fields}

    async def test_actual_sent_attachment_and_artifact_issue_only_owned_video(self):
        before = {path.relative_to(self.root): path.stat().st_size for path in self.root.rglob("*")}
        for event in [self.event(file_ids=[self.own, self.own, self.other, "../outside", "missing"]),
                      self.event("artifact_created", artifact={"id": self.own, "session_id": "chat-one"})]:
            with self.subTest(kind=event["type"]):
                videos = self.native["shared_chat_event_videos"]("chat-one", event)
                self.assertEqual(len(videos), 1)
                self.assertEqual(set(videos[0]), {"id", "filename", "content_type", "size"})
                opened = await self.native["open_shared_chat_video_for_share"]("chat-one", videos[0]["id"])
                try:
                    self.assertEqual(os.read(opened["file_fd"], 100), b"published synthetic video")
                    self.assertNotIn("path", opened)
                finally:
                    os.close(opened["file_fd"])
        self.assertEqual({path.relative_to(self.root): path.stat().st_size for path in self.root.rglob("*")}, before)

    def test_upload_queue_tool_workspace_and_internal_events_cannot_issue(self):
        events = [self.event(kind, artifact={"id": self.own}) for kind in
                  ["file_uploaded", "turn_queued", "tool_result", "assistant_text", "workspace_file", "turn_finished"]]
        events += [self.event(metadata_only=True), self.event(session_id="chat-two"),
                   self.event(purpose="handoff_digest"),
                   self.event(purpose="cross_chat_handoff_delivery", cross_chat_envelope_id="private-envelope"),
                   self.event("artifact_created", artifact={"id": self.own, "session_id": "chat-two"}),
                   self.event(file_ids=[self.other])]
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(self.native["shared_chat_event_videos"]("chat-one", event), [])

    def test_display_attachments_override_hidden_native_file_ids(self):
        self.assertEqual(self.native["shared_chat_event_videos"]("chat-one", self.event(display_file_ids=[])), [])
        self.assertEqual(self.native["shared_chat_event_videos"]("chat-one", self.event(display_file_ids=[self.other])), [])
        videos = self.native["shared_chat_event_videos"]("chat-one", self.event(file_ids=[self.other], display_file_ids=[self.own]))
        self.assertEqual(len(videos), 1)

    def test_only_explicit_human_goal_steer_can_share_its_attachment(self):
        for fields, expected in [({}, 0), ({"native_goal_steer": True}, 0),
                ({"provider_user_authored": True}, 0),
                ({"native_goal_steer": True, "provider_user_authored": True}, 1)]:
            with self.subTest(fields=fields):
                videos = self.native["shared_chat_event_videos"]("chat-one",
                    self.event("turn_steered", purpose="goal_followup", **fields))
                self.assertEqual(len(videos), expected)

    def test_legacy_artifact_requires_local_origin_and_not_merely_sent_id(self):
        for event, expected in [
            (self.event(file_ids=[self.legacy]), 0),
            (self.event("artifact_created", artifact={"id": self.legacy}), 1),
            (self.event("artifact_created", artifact={"id": self.legacy}, forked=True), 0),
            (self.event("artifact_created", artifact={"id": self.legacy}, original_session_id="chat-two"), 0),
        ]:
            with self.subTest(event=event):
                self.assertEqual(len(self.native["shared_chat_event_videos"]("chat-one", event)), expected)

    def test_raw_event_descriptors_are_removed_before_native_projection(self):
        for event in [self.event("assistant_text", text="Visible", shared_videos=[self.fake]),
                      self.event(file_ids=[], shared_videos=[self.fake])]:
            projected = self.native["shared_chat_video_events"]([event], "chat-one")
            self.assertEqual(len(projected), 1)
            self.assertNotIn("shared_videos", projected[0])
            self.assertEqual(event["shared_videos"], [self.fake])
        event = self.event(shared_videos=[self.fake])
        projected = self.native["shared_chat_video_events"]([event], "chat-one")[0]
        self.assertEqual(len(projected["shared_videos"]), 1)
        self.assertNotEqual(projected["shared_videos"][0]["id"], self.fake["id"])
        self.assertNotIn("file_ids", projected)

    async def test_exact_chat_signature_required_and_native_paths_are_not_downloads(self):
        handle = self.native["shared_chat_event_videos"]("chat-one", self.event())[0]["id"]
        for session, candidate in [("chat-two", handle), ("chat-one", self.other),
                                   ("chat-one", str(self.root / self.own / "clip.mp4")),
                                   ("chat-one", self.fake["id"])]:
            with self.subTest(session=session, candidate=candidate), self.assertRaises(SharedVideoUnavailable):
                await self.native["open_shared_chat_video_for_share"](session, candidate)
        for session in ["missing", "../chat-one"]:
            with self.assertRaises(HTTPException) as caught:
                await self.native["open_shared_chat_video_for_share"](session, handle)
            self.assertEqual(caught.exception.status_code, 404)
        self.native["DELETING_SESSIONS"].add("chat-one")
        with patch.dict(self.native, open_shared_chat_video=Mock()):
            opener = self.native["open_shared_chat_video"]
            with self.assertRaises(HTTPException):
                await self.native["open_shared_chat_video_for_share"]("chat-one", handle)
            opener.assert_not_called()

    async def test_cancelled_native_open_closes_descriptor_returned_later(self):
        entered, release = threading.Event(), threading.Event()
        closed = asyncio.Event()
        opened_fd = []
        def opener(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Synthetic opener was not released")
            fd = os.open(self.root / self.own / "clip.mp4", os.O_RDONLY)
            opened_fd.append(fd)
            return {"file_fd": fd}
        def close(fd):
            os.close(fd)
            closed.set()
        self.native.update(open_shared_chat_video=opener, os=SimpleNamespace(close=close))
        task = asyncio.create_task(self.native["open_shared_chat_video_for_share"]("chat-one", "synthetic-handle"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.wait_for(closed.wait(), 2)
        self.assertEqual(len(opened_fd), 1)
        with self.assertRaises(OSError):
            os.fstat(opened_fd[0])


if __name__ == "__main__":
    unittest.main()
