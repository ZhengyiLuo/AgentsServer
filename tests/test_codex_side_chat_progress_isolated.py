"""Native item projection and burst coalescing; no provider, process or credentials."""
import asyncio
import unittest
from copy import deepcopy
from codex_side_chat_progress import CodexSideChatProgress


def packet(method, **params):
    return {"method": method, "params": {"threadId": "side", **params}}


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_orders_summary_parts_commentary_tool_and_final_without_duplicates_or_input(self):
        published = []
        async def publish(items): published.append(deepcopy(items))
        progress = CodexSideChatProgress(publish, thread_id="side")
        for value in [
            packet("item/started", item={"type": "userMessage", "id": "user", "text": "internal input"}),
            packet("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=0, delta="First"),
            packet("item/reasoning/summaryPartAdded", itemId="r", summaryIndex=1),
            packet("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=1, delta="Second"),
            packet("item/completed", item={"type": "reasoning", "id": "r", "summary": ["First", "Second"], "encryptedContent": "secret"}),
            packet("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=1, delta="late duplicate"),
            packet("item/started", item={"type": "agentMessage", "id": "c", "phase": "commentary", "text": ""}),
            packet("item/agentMessage/delta", itemId="c", delta="Checking files"),
            packet("item/completed", item={"type": "agentMessage", "id": "c", "phase": "commentary", "text": "Checking files"}),
            packet("item/started", item={"type": "commandExecution", "id": "tool", "command": "pwd", "aggregatedOutput": "not a progress label"}),
            packet("item/completed", item={"type": "commandExecution", "id": "tool", "command": "pwd"}),
            packet("item/agentMessage/delta", itemId="answer", delta="Answer"),
            packet("item/completed", item={"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": "Answer"}),
            packet("item/agentMessage/delta", threadId="parent", itemId="other", delta="wrong thread"),
        ]: progress.receive(value)
        await progress.flush()
        self.assertEqual(len(published), 1)
        self.assertEqual([item["kind"] for item in published[0]], ["reasoning_summary", "commentary", "tool", "answer"])
        self.assertEqual([item["text"] for item in published[0]], ["First\n\nSecond", "Checking files", "pwd", "Answer"])
        self.assertTrue(all(item['status'] == 'completed' for item in published[0]))

    async def test_streams_while_native_turn_is_waiting_and_keeps_exposed_reasoning_separate(self):
        published = []
        async def publish(items): published.append(items)
        progress = CodexSideChatProgress(publish, thread_id="side")
        progress.receive(packet("item/reasoning/textDelta", itemId="r", contentIndex=0, delta="Exposed reasoning"))
        progress.receive(packet("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=0, delta="Summary"))
        await asyncio.sleep(.15)
        self.assertEqual([x['text'] for x in published[-1]], ['Exposed reasoning', 'Summary'])
        progress.receive(packet("item/completed", item={"type": "reasoning", "id": "r", "content": ["Exposed reasoning"], "summary": ["Summary"]}))
        await progress.flush()
        self.assertEqual(len(published[-1]), 2)
        self.assertTrue(all(x['status'] == 'completed' for x in published[-1]))

    async def test_corrects_late_message_phase_in_place_and_preserves_partial_text(self):
        result = []
        async def publish(items): result[:] = items
        progress = CodexSideChatProgress(publish, thread_id="side")
        progress.receive(packet("item/agentMessage/delta", itemId="c", delta="Partial commentary"))
        progress.receive(packet("item/completed", item={"type": "agentMessage", "id": "c", "phase": "commentary", "text": "Full commentary"}))
        progress.receive(packet("item/agentMessage/delta", itemId="a", delta="Partial answer"))
        await progress.flush()
        self.assertEqual([x['kind'] for x in result], ['commentary', 'answer'])
        self.assertEqual(result[-1]['text'], 'Partial answer')
