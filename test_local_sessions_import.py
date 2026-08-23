import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


def write_claude_transcript(path: Path, *, cwd: str | None, first_user_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if cwd is not None:
        lines.append(json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": first_user_text}}))
    else:
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": first_user_text}}))
    path.write_text("\n".join(lines) + "\n")


class LocalClaudeSessionCandidatesTests(unittest.TestCase):
    def test_reads_cwd_and_first_message_into_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-Users-georgia-code-widget" / "claude-abc123.jsonl"
            write_claude_transcript(transcript, cwd="/Users/georgia/code/widget", first_user_text="Fix the flaky test")

            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root):
                candidates = agent_server.local_claude_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["provider_session_id"], "claude-abc123")
        self.assertEqual(candidate["backend"], agent_server.BACKEND_CLAUDE)
        self.assertEqual(candidate["cwd"], "/Users/georgia/code/widget")
        self.assertIn("widget", candidate["label"])
        self.assertIn("Fix the flaky test", candidate["label"])

    def test_dedups_against_already_imported_provider_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-Users-georgia-code-widget" / "claude-abc123.jsonl"
            write_claude_transcript(transcript, cwd="/Users/georgia/code/widget", first_user_text="Fix the flaky test")

            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root):
                candidates = agent_server.local_claude_session_candidates({"claude-abc123"})

        self.assertEqual(candidates, [])

    def test_falls_back_to_folder_name_when_transcript_has_no_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-unexpected-project-name" / "claude-xyz789.jsonl"
            write_claude_transcript(transcript, cwd=None, first_user_text="")

            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root):
                candidates = agent_server.local_claude_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIsNone(candidate["cwd"])
        self.assertIn("-unexpected-project-name", candidate["label"])

    def test_missing_projects_root_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_root = Path(temporary) / "does-not-exist"
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", missing_root):
                candidates = agent_server.local_claude_session_candidates(set())
        self.assertEqual(candidates, [])


def write_codex_transcript(path: Path, *, session_id: str, cwd: str, first_user_text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({
        "timestamp": "2026-08-23T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id, "cwd": cwd}
    })]
    if first_user_text is not None:
        lines.append(json.dumps({
            "timestamp": "2026-08-23T00:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": first_user_text}
        }))
    path.write_text("\n".join(lines) + "\n")


class LocalCodexSessionCandidatesTests(unittest.TestCase):
    def test_skips_the_injected_recommended_plugins_turn_to_find_the_real_prompt(self) -> None:
        # codex CLI injects a synthetic first user-role turn carrying
        # <recommended_plugins> + <environment_context> before the real
        # prompt. Regression coverage for that turn leaking into the label.
        with tempfile.TemporaryDirectory() as temporary:
            sessions_root = Path(temporary) / "codex-sessions"
            transcript = sessions_root / "2026" / "08" / "23" / "rollout-plugins.jsonl"
            transcript.parent.mkdir(parents=True)
            lines = [
                json.dumps({
                    "type": "session_meta",
                    "payload": {"id": "session-with-plugins-turn", "cwd": "/work/octopus-facts"}
                }),
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "<recommended_plugins>\nSome plugin list...\n</recommended_plugins>"},
                            {"type": "input_text", "text": "<environment_context>\n  <cwd>/work/octopus-facts</cwd>\n</environment_context>"}
                        ]
                    }
                }),
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Tell me one fun fact about octopuses."}]
                    }
                })
            ]
            transcript.write_text("\n".join(lines) + "\n")
            missing_index = Path(temporary) / "session_index.jsonl"

            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", sessions_root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", missing_index
            ):
                candidates = agent_server.local_codex_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        label = candidates[0]["label"]
        self.assertNotIn("recommended_plugins", label)
        self.assertIn("octopuses", label)

    def test_reads_a_session_that_was_never_registered_in_the_index(self) -> None:
        # codex exec (headless) never writes session_index.jsonl; scanning the
        # transcript directly is the only reliable way to find these sessions.
        with tempfile.TemporaryDirectory() as temporary:
            sessions_root = Path(temporary) / "codex-sessions"
            transcript = sessions_root / "2026" / "08" / "23" / "rollout-01a02d6d.jsonl"
            write_codex_transcript(
                transcript,
                session_id="01a02d6d-76c4-7912-ac70-1ed02a436fe9",
                cwd="/private/tmp/codex-fun-fact",
                first_user_text="Tell me one fun fact about narwhals."
            )
            missing_index = Path(temporary) / "session_index.jsonl"

            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", sessions_root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", missing_index
            ):
                candidates = agent_server.local_codex_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["provider_session_id"], "01a02d6d-76c4-7912-ac70-1ed02a436fe9")
        self.assertEqual(candidate["backend"], agent_server.BACKEND_CODEX)
        self.assertEqual(candidate["cwd"], "/private/tmp/codex-fun-fact")
        self.assertIn("codex-fun-fact", candidate["label"])
        self.assertIn("narwhals", candidate["label"])

    def test_prefers_the_session_index_thread_name_when_one_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sessions_root = Path(temporary) / "codex-sessions"
            transcript = sessions_root / "2026" / "07" / "18" / "rollout-019f76aa.jsonl"
            write_codex_transcript(
                transcript,
                session_id="019f76aa-7880-7023-b350-cb7a24a754d8",
                cwd="/Volumes/SSD/Codes/ZenithDock",
                first_user_text="set up remote zenith dock please"
            )
            index_path = Path(temporary) / "session_index.jsonl"
            index_path.write_text(
                json.dumps({
                    "id": "019f76aa-7880-7023-b350-cb7a24a754d8",
                    "thread_name": "Set up remote Zenith Dock",
                    "updated_at": "2026-07-18T19:19:47.713052Z",
                }) + "\n"
            )

            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", sessions_root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", index_path
            ):
                candidates = agent_server.local_codex_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["label"], "Set up remote Zenith Dock")

    def test_dedups_against_already_imported_provider_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sessions_root = Path(temporary) / "codex-sessions"
            transcript = sessions_root / "2026" / "01" / "01" / "rollout-thread-1.jsonl"
            write_codex_transcript(transcript, session_id="thread-1", cwd="/work", first_user_text="hello")
            missing_index = Path(temporary) / "session_index.jsonl"

            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", sessions_root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", missing_index
            ):
                candidates = agent_server.local_codex_session_candidates({"thread-1"})

        self.assertEqual(candidates, [])

    def test_missing_sessions_root_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_root = Path(temporary) / "does-not-exist"
            missing_index = Path(temporary) / "session_index.jsonl"
            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", missing_root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", missing_index
            ):
                candidates = agent_server.local_codex_session_candidates(set())
        self.assertEqual(candidates, [])


class LocalSessionCandidatesDedupAgainstStoreTests(unittest.TestCase):
    def test_excludes_sessions_already_present_in_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-Users-georgia-code-widget" / "claude-already-imported.jsonl"
            write_claude_transcript(transcript, cwd="/Users/georgia/code/widget", first_user_text="hello")

            existing_sessions = {
                "sess_existing": {
                    "backend": agent_server.BACKEND_CLAUDE,
                    "claude_session_id": "claude-already-imported",
                }
            }

            missing_codex_sessions_root = Path(temporary) / "no-such-codex-sessions"
            missing_codex_index = Path(temporary) / "no-such-session-index.jsonl"
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root), patch.object(
                agent_server, "CODEX_SESSIONS_ROOT", missing_codex_sessions_root
            ), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", missing_codex_index
            ), patch.object(agent_server.STORE, "sessions", existing_sessions):
                candidates = agent_server.local_session_candidates(limit=200)

        self.assertEqual(candidates, [])


class BulkImportSessionsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_empty_items(self) -> None:
        with self.assertRaises(agent_server.HTTPException) as raised:
            await agent_server.bulk_import_sessions(agent_server.BulkImportSessionsRequest(items=[]))
        self.assertEqual(raised.exception.status_code, 400)

    async def test_rejects_batches_over_the_cap(self) -> None:
        items = [
            agent_server.BulkImportSessionItem(provider_session_id=f"id-{i}", backend=agent_server.BACKEND_CLAUDE)
            for i in range(agent_server.MAX_BULK_IMPORT_ITEMS + 1)
        ]
        with self.assertRaises(agent_server.HTTPException) as raised:
            await agent_server.bulk_import_sessions(agent_server.BulkImportSessionsRequest(items=items))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn(str(agent_server.MAX_BULK_IMPORT_ITEMS), str(raised.exception.detail))

    async def test_records_per_item_failure_without_aborting_the_batch(self) -> None:
        items = [
            agent_server.BulkImportSessionItem(provider_session_id="bad-backend", backend="not-a-real-backend"),
        ]
        result = await agent_server.bulk_import_sessions(agent_server.BulkImportSessionsRequest(items=items))
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["provider_session_id"], "bad-backend")


if __name__ == "__main__":
    unittest.main()
