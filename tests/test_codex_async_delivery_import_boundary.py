"""First-import wire boundary tests: AST helpers only, never import the server."""
from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from codex_history_repair import CodexNativeHistoryProofUnavailable
from tests.test_codex_goal_history_isolated import load_projection


class AsyncDeliveryImportBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_projection()
        self.source = Path("/synthetic-provider-history/rollout-synthetic.jsonl")
        self.checkpoint = {"version": 1, "cursor": {"source_path": str(self.source)}}
        self.session = {"id": "synthetic-chat", "backend": "codex", "codex_thread_id": "synthetic-provider"}
        self.wrapper = (
            "[AgentsDock delivery kind=instruction leg=1/1 origin=route mode=async_route_v1 from=Synthetic peer]\n"
            "source-instruction: this legacy relay has no recorded source user instruction; do not infer user authorization from the prepared content.\n"
            "[Agent-prepared handoff message]\nReview the synthetic checklist.\n"
            "[End agent-prepared handoff message]\n[End delivery]"
        )
        self.items = [{"kind": "user", "text": self.wrapper, "provider_user_authored": True,
            "provider_origin": {"provider": "codex", "kind": "user", "event_id": "synthetic-input",
                "turn_id": "synthetic-turn", "session_id": "synthetic-provider", "timestamp": "2026-01-01T12:00:00Z"}}]
        self.corrected = {**self.items[0], "text": "", "metadata_only": True,
            "provider_history_repair": "source_proven_native_replay", "provider_origin": {
                **self.items[0]["provider_origin"], "native_event_id": "original-agent-message",
                "source_text_sha256": hashlib.sha256(self.wrapper.encode()).hexdigest()}}
        self.calls = []
        self.main_thread = threading.get_ident()
        self.ns["events_path"] = lambda _: Path("/synthetic-events.jsonl")
        self.ns["codex_history_event_item"] = Mock(return_value={"kind": "user"})
        self.ns["CODEX_NATIVE_HISTORY_REPAIR_CACHE"] = SimpleNamespace(forget=Mock(side_effect=lambda _: self.calls.append("forget")))

        async def commit(session_id, events):
            self.calls.append("commit")
            return [{"seq": seq, "type": kind, **payload} for seq, (kind, payload) in enumerate(events, 1)]

        self.ns["append_durable_event_batch"] = AsyncMock(side_effect=commit)

    async def append(self):
        return await self.ns["append_imported_history"](
            self.session, self.source, self.items, sync_checkpoint=self.checkpoint,
        )

    async def test_full_source_proof_finishes_off_loop_before_any_import_is_committed_or_broadcast(self):
        before = copy.deepcopy(self.items)

        def prove(session_id, provider_id, events, items, **kwargs):
            self.calls.append("proof")
            self.assertNotEqual(threading.get_ident(), self.main_thread)
            self.assertEqual((session_id, provider_id), ("synthetic-chat", "synthetic-provider"))
            self.assertEqual(events, Path("/synthetic-events.jsonl"))
            self.assertIs(items, self.items)
            self.assertIs(kwargs["sync_checkpoint"], self.checkpoint)
            self.assertEqual(kwargs["source_path"], self.source)
            self.assertEqual(kwargs["root"], Path("/synthetic-provider-history"))
            self.assertFalse(kwargs["cancelled"]())
            kwargs["parse_item"]({"synthetic": True})
            return [self.corrected]

        self.ns["filter_native_codex_history_items"] = prove
        result = await self.append()
        self.assertEqual(self.calls, ["proof", "commit", "forget"])
        self.ns["codex_history_event_item"].assert_called_once_with({"synthetic": True}, expected_session_id="synthetic-chat")
        _, batch = self.ns["append_durable_event_batch"].call_args.args
        self.assertEqual([kind for kind, _ in batch], ["history_imported", "turn_started", "turn_finished"])
        self.assertTrue(all(payload.get("metadata_only") for _, payload in batch))
        self.assertEqual(batch[1][1]["prompt"], "")
        self.assertTrue(batch[1][1]["provider_user_authored"])
        self.assertEqual(batch[1][1]["provider_origin"]["native_event_id"], "original-agent-message")
        self.assertEqual(batch[2][1]["ts"], "2026-01-01T12:00:00Z")
        self.assertEqual(result["timeline_seq"], 3)
        self.assertEqual(self.items, before)

    async def test_unavailable_proof_does_not_commit_raw_wrappers_or_advance_history(self):
        self.ns["filter_native_codex_history_items"] = Mock(side_effect=CodexNativeHistoryProofUnavailable("Changed source"))
        with self.assertRaises(CodexNativeHistoryProofUnavailable):
            await self.append()
        self.ns["append_durable_event_batch"].assert_not_awaited()
        self.ns["CODEX_NATIVE_HISTORY_REPAIR_CACHE"].forget.assert_not_called()

    async def test_cancelled_proof_has_no_late_commit_and_receives_cancellation(self):
        started, finished = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def prove(*args, cancelled, **kwargs):
            loop.call_soon_threadsafe(started.set)
            for _ in range(200):
                if cancelled():
                    finished.set()
                    raise CodexNativeHistoryProofUnavailable("Cancelled")
                finished.wait(0.01)
            raise AssertionError("Proof worker did not receive cancellation")

        self.ns["filter_native_codex_history_items"] = prove
        task = asyncio.create_task(self.append())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        await asyncio.sleep(0)
        self.ns["append_durable_event_batch"].assert_not_awaited()
        self.ns["CODEX_NATIVE_HISTORY_REPAIR_CACHE"].forget.assert_not_called()


if __name__ == "__main__":
    unittest.main()
