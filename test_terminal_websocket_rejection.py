import unittest
from unittest.mock import patch

import agent_server


class RecordingWebSocket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def accept(self) -> None:
        self.calls.append(("accept", None))

    async def close(self, code: int = 1000) -> None:
        self.calls.append(("close", code))


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


if __name__ == "__main__":
    unittest.main()
