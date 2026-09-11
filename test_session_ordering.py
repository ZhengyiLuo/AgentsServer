import copy
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

import agent_server


def session(
    session_id: str,
    *,
    folder: str = "General",
    order: float = 10.0,
    pinned: bool = False,
    archived: bool = False,
) -> dict:
    return {
        "id": session_id,
        "title": session_id,
        "backend": "codex",
        "folder": folder,
        "sort_order": order,
        "pinned": pinned,
        "pinned_at": "2026-07-29T00:00:00Z" if pinned else None,
        "archived": archived,
        "archived_at": "2026-07-29T00:00:00Z" if archived else None,
        "created_at": "2026-07-29T00:00:00Z",
        "updated_at": "2026-07-29T00:00:00Z",
    }


class SessionOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = agent_server.SessionStore()
        self.store.save = AsyncMock()

    async def test_cross_folder_reorder_is_atomic_and_exact(self) -> None:
        source = session("source", folder="Jobs", order=10)
        source["queued_turn_count"] = 2
        self.store.sessions = {
            "source": source,
            "first": session("first", folder="General", order=10),
            "target": session("target", folder="General", order=20),
            "last": session("last", folder="General", order=30),
        }

        result = await self.store.reorder(
            "source",
            target_id="target",
            placement="before",
            target_folder="General",
        )

        destination = [
            item["id"]
            for item in result
            if agent_server.session_section_key(item) == ("folder", "General")
        ]
        self.assertEqual(destination, ["first", "source", "target", "last"])
        self.assertEqual(source["folder"], "General")
        self.assertFalse(source["pinned"])
        self.assertFalse(source["archived"])
        self.assertEqual(source["queued_turn_count"], 2)
        self.store.save.assert_awaited_once()

    async def test_cross_folder_reorder_can_unpin_or_unarchive_source(self) -> None:
        for state in ("pinned", "archived"):
            with self.subTest(state=state):
                self.store.save.reset_mock()
                source = session(
                    "source",
                    folder="Old",
                    pinned=state == "pinned",
                    archived=state == "archived",
                )
                self.store.sessions = {
                    "source": source,
                    "target": session("target", folder="Research", order=10),
                }

                await self.store.reorder(
                    "source",
                    target_id="target",
                    placement="after",
                    target_folder="Research",
                )

                self.assertEqual(source["folder"], "Research")
                self.assertFalse(source["pinned"])
                self.assertIsNone(source["pinned_at"])
                self.assertFalse(source["archived"])
                self.assertIsNone(source["archived_at"])
                destination = [
                    item["id"]
                    for item in agent_server.sorted_sessions(
                        list(self.store.sessions.values())
                    )
                    if agent_server.session_section_key(item)
                    == ("folder", "Research")
                ]
                self.assertEqual(destination, ["target", "source"])
                self.store.save.assert_awaited_once()

    async def test_cross_folder_reorder_rejects_virtual_or_stale_target(self) -> None:
        cases = [
            (
                session("target", pinned=True),
                "General",
                400,
            ),
            (
                session("target", folder="Research"),
                "General",
                409,
            ),
        ]
        for target, requested_folder, status_code in cases:
            with self.subTest(status_code=status_code):
                self.store.save.reset_mock()
                self.store.sessions = {
                    "source": session("source", folder="Jobs"),
                    "target": target,
                }
                before = copy.deepcopy(self.store.sessions)

                with self.assertRaises(HTTPException) as raised:
                    await self.store.reorder(
                        "source",
                        target_id="target",
                        placement="before",
                        target_folder=requested_folder,
                    )

                self.assertEqual(raised.exception.status_code, status_code)
                self.assertEqual(self.store.sessions, before)
                self.store.save.assert_not_awaited()

    async def test_legacy_cross_section_reorder_remains_rejected(self) -> None:
        self.store.sessions = {
            "source": session("source", folder="Jobs"),
            "target": session("target", folder="General"),
        }
        before = copy.deepcopy(self.store.sessions)

        with self.assertRaises(HTTPException) as raised:
            await self.store.reorder(
                "source",
                target_id="target",
                placement="after",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.store.sessions, before)
        self.store.save.assert_not_awaited()

    async def test_same_folder_reorder_keeps_existing_behavior(self) -> None:
        self.store.sessions = {
            "source": session("source", folder="Jobs", order=10),
            "target": session("target", folder="Jobs", order=20),
        }

        result = await self.store.reorder(
            "source",
            target_id="target",
            placement="after",
        )

        jobs = [
            item["id"]
            for item in result
            if agent_server.session_section_key(item) == ("folder", "Jobs")
        ]
        self.assertEqual(jobs, ["target", "source"])
        self.store.save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
