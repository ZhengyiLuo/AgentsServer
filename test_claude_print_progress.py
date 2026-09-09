import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStream:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.chunks = deque(
            (json.dumps(record) + "\n").encode()
            for record in records
        )

    async def readline(self) -> bytes:
        return self.chunks.popleft() if self.chunks else b""

    async def read(self) -> bytes:
        return b"".join(self.chunks)


class FakeProcess:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.pid = 4242
        self.returncode = 0
        self.stdin = FakeStdin()
        self.stdout = FakeStream(records)
        self.stderr = FakeStream([])


async def wait_forever(*_args: object, **_kwargs: object) -> None:
    await asyncio.Event().wait()


class ClaudePrintProgressTests(unittest.IsolatedAsyncioTestCase):
    async def run_print(
        self,
        records: list[dict[str, object]],
        *,
        stopped: bool = False,
    ) -> tuple[AsyncMock, AsyncMock]:
        run_id = "run-print"
        process = FakeProcess(records)
        append_event = AsyncMock(return_value={})
        finalize = AsyncMock(return_value=True)
        metadata = {
            "purpose": "scheduled_job",
            "job_id": "job-1",
        }

        with tempfile.TemporaryDirectory() as cwd, patch.object(
            agent_server,
            "STOPPED_RUNS",
            {run_id} if stopped else set(),
        ), patch.dict(
            agent_server.RUN_METADATA,
            {run_id: metadata},
            clear=True,
        ), patch.multiple(
            agent_server,
            resolve_claude_resume_provider=Mock(return_value=(None, None)),
            capture_git_baseline=AsyncMock(return_value={"head": "base"}),
            build_claude_cmd=Mock(return_value=["claude", "-p"]),
            agent_runner_env=Mock(return_value={}),
            process_group_for_pid=Mock(return_value=4242),
            bind_active_turn=AsyncMock(return_value=(True, stopped)),
            watch_manifest_artifacts=wait_forever,
            append_event=append_event,
            append_active_stdout=AsyncMock(),
            mark_provider_turn_ready=AsyncMock(),
            persist_run_provider_session=AsyncMock(),
            terminate_process_tree=AsyncMock(),
            clear_active_process=AsyncMock(),
            collect_manifest=AsyncMock(),
            collect_recent_leftover_manifests=AsyncMock(),
            publish_turn_code_diff=AsyncMock(),
            record_runtime_success=Mock(),
            record_runtime_failure=Mock(),
            finalize_owned_turn_finished=finalize,
        ), patch.object(
            agent_server.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            await agent_server.run_claude_print(
                "chat-print",
                run_id,
                "Prompt",
                {
                    "id": "chat-print",
                    "backend": agent_server.BACKEND_CLAUDE,
                    "cwd": cwd,
                },
                Path(cwd) / ".manifest.json",
            )

        return append_event, finalize

    async def test_text_blocks_are_commentary_and_result_is_final_output(self) -> None:
        append_event, finalize = await self.run_print([
            {
                "type": "assistant",
                "session_id": "provider-session",
                "message": {
                    "content": [{
                        "type": "text",
                        "text": "I am checking the release.",
                    }],
                },
            },
            {
                "type": "result",
                "session_id": "provider-session",
                "result": "The release is ready.",
            },
        ])

        commentary = [
            call.args[2]
            for call in append_event.await_args_list
            if call.args[1] == "reasoning_summary"
        ]
        self.assertEqual(commentary, [{
            "run_id": "run-print",
            "text": "I am checking the release.",
            "phase": "commentary",
            "backend": agent_server.BACKEND_CLAUDE,
            "purpose": "scheduled_job",
            "job_id": "job-1",
        }])
        self.assertFalse(any(
            call.args[1] == "assistant_text"
            for call in append_event.await_args_list
        ))
        terminal = finalize.await_args.kwargs["payload"]
        self.assertEqual(terminal["result_text"], "The release is ready.")
        self.assertIs(terminal["stopped"], False)

    async def test_stopped_turn_without_result_does_not_promote_commentary(self) -> None:
        append_event, finalize = await self.run_print([
            {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "text",
                        "text": "Partial progress before Stop.",
                    }],
                },
            },
        ], stopped=True)

        self.assertTrue(any(
            call.args[1] == "reasoning_summary"
            and call.args[2].get("phase") == "commentary"
            and call.args[2].get("text") == "Partial progress before Stop."
            for call in append_event.await_args_list
        ))
        self.assertFalse(any(
            call.args[1] == "assistant_text"
            for call in append_event.await_args_list
        ))
        terminal = finalize.await_args.kwargs["payload"]
        self.assertEqual(terminal["result_text"], "")
        self.assertIs(terminal["stopped"], True)

    async def test_clean_stream_exit_without_result_is_a_failure(self) -> None:
        append_event, finalize = await self.run_print([{
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/example"},
                }],
            },
        }])

        self.assertTrue(any(
            call.args[1] == "error"
            and "before a terminal result" in call.args[2].get("message", "")
            for call in append_event.await_args_list
        ))
        terminal = finalize.await_args.kwargs["payload"]
        self.assertEqual(terminal["exit_code"], 1)
        self.assertEqual(terminal["result_text"], "")
        self.assertIs(terminal["stopped"], False)


if __name__ == "__main__":
    unittest.main()
