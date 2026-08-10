import asyncio
import unittest
from unittest.mock import ANY, patch

import agent_server


class RecordingWebSocket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def accept(self) -> None:
        self.calls.append(("accept", None))

    async def close(self, code: int = 1000) -> None:
        self.calls.append(("close", code))


class ScrollDisconnectWebSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.messages = [
            {"type": "websocket.receive", "text": '{"type":"scroll","delta":-4}'},
            {"type": "websocket.disconnect"},
        ]

    async def send_json(self, _payload: dict) -> None:
        self.calls.append(("ready", None))

    async def send_bytes(self, _payload: bytes) -> None:
        self.calls.append(("data", None))

    async def receive(self) -> dict:
        return self.messages.pop(0)


class TerminalWebSocketRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def assert_rejected_after_accept(
        self,
        session_id: str,
        expected_code: int,
        *,
        authorized: bool,
        session: dict | None = None,
    ) -> None:
        websocket = RecordingWebSocket()
        sessions = {session_id: session} if session is not None else {}
        with patch.object(agent_server, "websocket_authorized", return_value=authorized), \
             patch.dict(agent_server.STORE.sessions, sessions, clear=True):
            await agent_server.session_terminal(session_id, websocket)  # type: ignore[arg-type]

        self.assertEqual(websocket.calls, [
            ("accept", None),
            ("close", expected_code),
        ])

    async def test_unauthorized_terminal_accepts_before_custom_close(self) -> None:
        await self.assert_rejected_after_accept(
            "unauthorized-terminal",
            4401,
            authorized=False,
        )

    async def test_missing_chat_terminal_accepts_before_custom_close(self) -> None:
        await self.assert_rejected_after_accept(
            "missing-terminal",
            4404,
            authorized=True,
        )

    async def test_archived_chat_terminal_accepts_before_custom_close(self) -> None:
        await self.assert_rejected_after_accept(
            "archived-terminal",
            4409,
            authorized=True,
            session={"id": "archived-terminal", "archived": True},
        )

    async def test_disconnect_exits_copy_mode_owned_by_terminal_scrolling(self) -> None:
        session_id = "scrolling-terminal"
        websocket = ScrollDisconnectWebSocket()

        async def wait_for_output(_fd: int) -> bytes:
            await asyncio.Event().wait()
            return b""

        with patch.object(agent_server, "websocket_authorized", return_value=True), \
             patch.dict(
                 agent_server.STORE.sessions,
                 {session_id: {"id": session_id, "archived": False, "cwd": "/workspace"}},
                 clear=True,
             ), \
             patch.object(
                 agent_server,
                 "spawn_terminal_client",
                 return_value=(object(), 91, "zd_scrolling-terminal"),
             ), \
             patch.object(agent_server, "read_terminal_output", side_effect=wait_for_output), \
             patch.object(agent_server, "scroll_terminal_history", return_value=True) as scroll, \
             patch.object(agent_server, "exit_terminal_auto_scroll") as exit_scroll, \
             patch.object(agent_server, "stop_terminal_client") as stop_client:
            await agent_server.session_terminal(session_id, websocket)  # type: ignore[arg-type]

        scroll.assert_called_once_with(session_id, -4, managed=False)
        exit_scroll.assert_called_once_with(session_id)
        stop_client.assert_called_once_with(ANY, 91)


if __name__ == "__main__":
    unittest.main()
