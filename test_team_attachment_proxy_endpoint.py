"""Local AgentsServer bridge checks for cached Team attachment media."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import agent_server
from agentsdock_team_hub.secure_peer import AttachmentFileLease, ProxyResponse


class TeamAttachmentProxyEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_runtime = agent_server.SECURE_PEER_RUNTIME
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection_id = str(uuid.uuid4())
        self.team_id = "team_binary_test"
        self.attachment_id = "tatt_binary_test"
        self.url = (
            f"/api/team-hub-secure/{self.connection_id}/v1/teams/{self.team_id}/"
            f"network/attachments/{self.attachment_id}/content"
        )

    async def asyncTearDown(self) -> None:
        agent_server.SECURE_PEER_RUNTIME = self.original_runtime

    @staticmethod
    def request(method: str, url: str, headers: dict[str, str]) -> Mock:
        request = Mock()
        request.method = method
        request.scope = {"headers": []}
        request.headers = headers
        request.url.path = url
        request.url.query = ""
        return request

    async def test_cached_get_preserves_range_semantics(self) -> None:
        payload = b"range-capable-video"
        path = Path(self.temporary.name) / "sample.mp4"
        path.write_bytes(payload)
        runtime = Mock()
        runtime.open_cached_team_attachment.return_value = (
            {
                "id": self.attachment_id,
                "file_name": "sample.mp4",
                "media_type": "video/mp4",
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            os.open(path, os.O_RDONLY),
        )
        agent_server.SECURE_PEER_RUNTIME = runtime
        request = self.request("GET", self.url, {"range": "bytes=2-7"})
        with patch.object(agent_server, "require_secure_peer_control"):
            response = await agent_server.secure_peer_hub_proxy_endpoint(
                self.connection_id,
                self.url.split(f"/{self.connection_id}/", 1)[1],
                request,
            )
        body = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual((response.status_code, body), (206, payload[2:8]))
        self.assertEqual(
            response.headers["content-range"], f"bytes 2-7/{len(payload)}"
        )

    async def test_put_uses_binary_runtime_without_json_reencoding(self) -> None:
        runtime = Mock()
        runtime.proxy_team_attachment_chunk.return_value = ProxyResponse(
            200,
            (("content-type", "application/json"),),
            b'{"attachment":{"state":"ready"}}',
        )
        agent_server.SECURE_PEER_RUNTIME = runtime
        request = self.request(
            "PUT",
            self.url,
            {
                "content-type": "application/octet-stream",
                "content-range": "bytes 0-2/3",
            },
        )
        request.body = AsyncMock(return_value=b"\x00\x01\x02")
        with patch.object(agent_server, "require_secure_peer_control"):
            response = await agent_server.secure_peer_hub_proxy_endpoint(
                self.connection_id,
                self.url.split(f"/{self.connection_id}/", 1)[1],
                request,
            )
        self.assertEqual(response.body, b'{"attachment":{"state":"ready"}}')
        runtime.proxy_team_attachment_chunk.assert_called_once_with(
            self.connection_id,
            self.team_id,
            self.attachment_id,
            content_range="bytes 0-2/3",
            body=b"\x00\x01\x02",
        )

    async def test_local_binary_framing_is_bounded(self) -> None:
        request = self.request("PUT", self.url, {})
        request.scope["headers"] = [
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(agent_server.TEAM_ATTACHMENT_CHUNK_BYTES + 1).encode()),
            (
                b"content-range",
                (
                    f"bytes 0-{agent_server.TEAM_ATTACHMENT_CHUNK_BYTES}/"
                    f"{agent_server.TEAM_ATTACHMENT_CHUNK_BYTES + 1}"
                ).encode(),
            ),
        ]
        self.assertEqual(
            agent_server.secure_peer_attachment_put_transport_error(request),
            (413, "attachment upload chunk size is invalid"),
        )
        request = self.request("GET", self.url, {})
        request.scope["headers"] = [(b"transfer-encoding", b"chunked")]
        self.assertEqual(
            agent_server.secure_peer_attachment_read_transport_error(request),
            (400, "attachment downloads do not accept transfer encoding"),
        )

    async def test_cancelled_cache_open_releases_late_lease(self) -> None:
        path = Path(self.temporary.name) / "late.bin"
        path.write_bytes(b"late")
        descriptor = os.open(path, os.O_RDONLY)
        opened = threading.Event()
        proceed = threading.Event()
        closed = threading.Event()

        def release() -> None:
            os.close(descriptor)
            closed.set()

        def open_late(*_args):
            opened.set()
            proceed.wait(timeout=2)
            return (
                {
                    "id": self.attachment_id,
                    "file_name": "late.bin",
                    "media_type": "application/octet-stream",
                    "byte_size": 4,
                    "sha256": hashlib.sha256(b"late").hexdigest(),
                },
                AttachmentFileLease(descriptor, release),
            )

        runtime = Mock()
        runtime.open_cached_team_attachment.side_effect = open_late
        agent_server.SECURE_PEER_RUNTIME = runtime
        request = self.request("GET", self.url, {})
        with patch.object(agent_server, "require_secure_peer_control"):
            task = asyncio.create_task(
                agent_server.secure_peer_hub_proxy_endpoint(
                    self.connection_id,
                    self.url.split(f"/{self.connection_id}/", 1)[1],
                    request,
                )
            )
            self.assertTrue(await asyncio.to_thread(opened.wait, 1))
            task.cancel()
            proceed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(await asyncio.to_thread(closed.wait, 1))


if __name__ == "__main__":
    unittest.main()
