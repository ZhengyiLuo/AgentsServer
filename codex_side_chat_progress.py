"""User-visible native Codex side-thread activity; never project parent/control input."""
from __future__ import annotations

import asyncio
from copy import deepcopy


class CodexSideChatProgress:
    def __init__(self, publish, *, thread_id):
        self.publish = publish
        self.thread_id = thread_id
        self.items = {}
        self.parts = {}
        self.completed = set()
        self.pending = None
        self.dirty = False

    def _item(self, identifier, kind):
        key = f"{identifier}:{kind}"
        if key not in self.items:
            self.items[key] = {"id": key, "kind": kind, "text": "", "status": "running"}
        return self.items[key]

    @staticmethod
    def _text(parts):
        if not isinstance(parts, list):
            return ""
        return "\n\n".join(part if isinstance(part, str) else str(part.get("text") or part.get("summary_text") or "")
                            for part in parts if isinstance(part, (str, dict)))

    def receive(self, packet):
        method, data = packet.get("method"), packet.get("params") or {}
        # The transport normally scopes notifications; enforce it here too so
        # descendant activity cannot become the side assistant's own answer.
        if data.get("threadId") not in (None, self.thread_id):
            return
        item = data.get("item") or {}
        identifier = str(data.get("itemId") or item.get("id") or "")
        if not identifier or identifier in self.completed:
            return
        changed = False
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta", "item/reasoning/summaryPartAdded"}:
            kind = "reasoning" if method == "item/reasoning/textDelta" else "reasoning_summary"
            field = "contentIndex" if kind == "reasoning" else "summaryIndex"
            index = data.get(field, 0)
            if not isinstance(index, int) or index < 0:
                return
            parts = self.parts.setdefault((identifier, kind), {})
            parts[index] = parts.get(index, "") + str(data.get("delta") or "")
            self._item(identifier, kind)["text"] = "\n\n".join(parts[i] for i in sorted(parts))
            changed = True
        elif method == "item/agentMessage/delta":
            # item/started carries the phase. Older runtimes may emit deltas
            # first; item/completed supplies the authoritative final phase.
            kind = "commentary" if f"{identifier}:commentary" in self.items else "answer"
            self._item(identifier, kind)["text"] += str(data.get("delta") or "")
            changed = True
        elif method in {"item/started", "item/completed"}:
            item_type = item.get("type")
            if item_type == "reasoning":
                for kind, field in (("reasoning_summary", "summary"), ("reasoning", "content")):
                    text = self._text(item.get(field))
                    if text or (identifier, kind) in self.parts:
                        projected = self._item(identifier, kind)
                        if text:
                            projected["text"] = text
                        changed = True
            elif item_type == "agentMessage":
                kind = "commentary" if item.get("phase") == "commentary" else "answer"
                other = f"{identifier}:{'answer' if kind == 'commentary' else 'commentary'}"
                if other in self.items:
                    # Keep its chronological slot when an older runtime omits phase at start.
                    self.items = {f"{identifier}:{kind}" if key == other else key:
                                  {**value, "id": f"{identifier}:{kind}", "kind": kind} if key == other else value
                                  for key, value in self.items.items()}
                projected = self._item(identifier, kind)
                if isinstance(item.get("text"), str) and (item["text"] or method == "item/completed"):
                    projected["text"] = item["text"]
                changed = True
            elif item_type in {"commandExecution", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
                               "collabToolCall", "webSearch", "fileChange", "imageView", "imageGeneration"}:
                projected = self._item(identifier, "tool")
                projected["tool"] = item_type
                text = item.get("command") if item_type == "commandExecution" else item.get("tool") or item.get("query") or item.get("path")
                if not text and item_type == "fileChange":
                    text = ", ".join(str(change.get("path") or "") for change in item.get("changes", []) if isinstance(change, dict))
                if text:
                    projected["text"] = str(text)
                changed = True
            if method == "item/completed":
                self.completed.add(identifier)
                for key, value in self.items.items():
                    if key.startswith(identifier + ":"):
                        value["status"] = "completed"
        if changed:
            self.dirty = True
            if self.pending is None:
                self.pending = asyncio.create_task(self._publish_later())

    async def _publish_later(self):
        # Coalesce token bursts; no per-token disk writes or client history reads.
        await asyncio.sleep(0.1)
        while self.dirty:
            self.dirty = False
            await self.publish(deepcopy(list(self.items.values())))
        self.pending = None

    async def flush(self):
        if self.pending is not None:
            await asyncio.shield(self.pending)
