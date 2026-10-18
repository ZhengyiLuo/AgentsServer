"""Opt-in large-state recovery through actual execution worker/gateway processes.

Generates about 1.2 GB in disposable storage. No native services, installed
provider executables, credentials, model calls or public update feeds are used.
Activation failure/retry and native desktop checks are recorded separately in
RECOVERY_READINESS_TESTING.md; this suite does not impersonate those boundaries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import unittest

from tests import test_execution_startup_integration as support


@unittest.skipUnless(os.environ.get("RUN_LARGE_STATE_UPDATE_TESTS") == "1",
                     "opt-in: generates approximately 1.2 GB of synthetic history")
class LargeStateUpdateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_recovery_stays_responsive_and_preserves_durable_work(self):
        fixture = support.ExecutionStartupProcessTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.shutdown)
        registry = {}
        originals = {}
        total = 0
        stamp = "2026-10-13T00:00:00Z"
        for index in range(1000):
            sid = f"stress_{index:04d}"
            registry[sid] = {
                "id": sid, "title": f"Synthetic history {index:04d}", "backend": "codex",
                "cwd": str(fixture.workspace), "created_at": stamp, "updated_at": stamp,
                "archived": index >= 800, "auto_title_enabled": False,
                "provider_jobs_access": "blocked",
            }
            path = fixture.state / "sessions" / sid / "events.jsonl"
            path.parent.mkdir(mode=0o700, parents=True)
            seq = 0
            digest = hashlib.sha256()
            with path.open("wb") as stream:
                def append(kind, **fields):
                    nonlocal seq, total
                    seq += 1
                    raw = (json.dumps({"id": f"event_{sid}_{seq}", "seq": seq,
                        "type": kind, "session_id": sid, "ts": stamp, **fields},
                        ensure_ascii=False) + "\n").encode()
                    stream.write(raw)
                    digest.update(raw)
                    total += len(raw)
                    # Match the native durable registry projection used to
                    # select chats for interrupted-run recovery on startup.
                    registry[sid]["latest_event_type"] = kind
                    registry[sid]["latest_event_seq"] = seq
                    registry[sid]["latest_event_at"] = stamp
                for turn in range(32):
                    run = f"run_{index}_{turn}"
                    append("turn_started", run_id=run, backend="codex", prompt="Mixed history: 中文 café")
                    append("tool_finished", run_id=run, tool_name="read_file",
                           output={"text": "x" * 16384, "files": ["synthetic.py", "中文.txt"]})
                    append("assistant_text", run_id=run, text="y" * 16384)
                    append("turn_finished", run_id=run, backend="codex")
                if index == 0:
                    append("tool_finished", output={"content": [{"type": "image", "data": "A" * (128 * 1024 * 1024)}]})
                if index == 1:
                    append("tool_finished", output={"values": [0] * 260001})
                if index < 64:
                    for number in range(3):
                        prompt = f"Preserve queue {index}:{number}"
                        append("turn_queued", queued_id=f"queue_{index}_{number}",
                               prompt=prompt, request_prompt=prompt, backend="codex", file_ids=[])
                if 64 <= index < 96:
                    append("turn_started", run_id=f"orphan_{index}", backend="codex",
                           prompt="Already accepted; do not replay")
            originals[sid] = path.stat().st_size, digest.digest()
        (fixture.state / "sessions.json").write_text(json.dumps(registry))
        started = time.monotonic()
        await fixture.start()
        first_health = time.monotonic() - started

        async def recovered():
            response = await fixture.client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            health = response.json()
            self.assertNotEqual(health["chat_readiness"]["state"], "failed")
            return health if health["chat_readiness"]["ready"] else None
        health = await fixture.eventually(recovered, timeout=90)
        ready_seconds = time.monotonic() - started
        self.assertEqual(len(health["queued"]), 64)
        semaphore = asyncio.Semaphore(12)
        latencies = {}

        async def request(route):
            async with semaphore:
                begin = time.monotonic()
                response = await fixture.client.get(route)
                latencies.setdefault(route, []).append(time.monotonic() - begin)
                self.assertEqual(response.status_code, 200, response.text[:200])
                return response.json()
        routes = ["/api/health", "/api/admin/update", "/api/sessions?summary=true",
                  "/api/sessions/stress_0002", "/api/sessions/stress_0002/timeline-index"]
        await asyncio.gather(*(request(routes[index % len(routes)]) for index in range(120)))
        for index in range(64):
            result = await request(f"/api/sessions/stress_{index:04d}")
            self.assertEqual([row["queued_id"] for row in result["queued_turns"]],
                             [f"queue_{index}_{number}" for number in range(3)])
        # The sealed candidate must not accept new work before activation, even
        # though both transport health and background queue recovery are ready.
        denied = await fixture.client.post("/api/sessions/stress_0002/turns",
                                           json={"prompt": "Must remain unaccepted"})
        self.assertEqual(denied.status_code, 409)
        self.assertIn("managed update", denied.json()["detail"])
        self.assertFalse((fixture.state / "admin/provider-children.json").exists())
        for index in range(64, 96):
            events = [json.loads(line) for line in
                      (fixture.state / "sessions" / f"stress_{index:04d}" / "events.jsonl").read_text().splitlines()]
            orphan = [event for event in events if event.get("run_id") == f"orphan_{index}"]
            self.assertEqual(sum(event["type"] == "turn_started" for event in orphan), 1)
            self.assertEqual(sum(event["type"] == "turn_stopped" for event in orphan), 1)

        def check_prefixes():
            for sid, (size, expected) in originals.items():
                actual = hashlib.sha256()
                with (fixture.state / "sessions" / sid / "events.jsonl").open("rb") as stream:
                    remaining = size
                    while remaining:
                        block = stream.read(min(remaining, 1024 * 1024))
                        self.assertTrue(block)
                        actual.update(block)
                        remaining -= len(block)
                self.assertEqual(actual.digest(), expected, sid)
        await asyncio.to_thread(check_prefixes)
        print(json.dumps({"large_state_acceptance": {"saved_chats": 1000,
            "history_bytes": total, "queued_messages_preserved": 192,
            "orphaned_runs_closed_without_replay": 32,
            "initial_health_seconds": round(first_health, 3),
            "chat_ready_seconds": round(ready_seconds, 3),
            "checked_read_requests": 184, "concurrency": 12,
            "latency_p95_seconds": {route: round(sorted(latencies[route])[
                math.ceil(len(latencies[route]) * .95) - 1], 3) for route in routes},
            "original_history_prefixes_preserved": 1000}}))
