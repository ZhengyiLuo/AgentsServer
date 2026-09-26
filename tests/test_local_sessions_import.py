import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

import agent_server


class LocalSessionLabelTests(unittest.TestCase):
    def test_removes_unsafe_ascii_controls_and_preserves_word_breaks(self) -> None:
        for codepoint in [*range(32), 127]:
            character = chr(codepoint)
            with self.subTest(codepoint=codepoint):
                replacement = character if character in "\t\n\r" else " " if character.isspace() else ""
                self.assertEqual(
                    agent_server.local_session_label(f"before{character}after", "Fallback"),
                    f"before{replacement}after",
                )

    def test_preserves_punctuation_unicode_and_existing_whitespace_policy(self) -> None:
        label = "  - fix: café / 中文 — 👩🏽‍💻\tpart two\r\nnext\n\n\nlast  "
        self.assertEqual(agent_server.local_session_label(label, "Fallback"), agent_server.compact_import_text(label))

    def test_sanitizes_fallback_and_preserves_empty_index_title_sentinel(self) -> None:
        self.assertEqual(agent_server.local_session_label("\x1b\x00", "\x07Fallback\x7f"), "Fallback")
        self.assertEqual(agent_server.local_session_label("\x1b", "\x07"), "Local chat")
        for value in (None, "", " \t\n", "\x1b\x00\x7f"):
            with self.subTest(value=value):
                self.assertEqual(agent_server.local_session_label(value, ""), "")

    def test_value_and_fallback_remain_within_existing_bound(self) -> None:
        text = "\x1b\x00" + "文🙂" * 200 + "\x7f"
        expected = ("文🙂" * 200)[:agent_server.MAX_LOCAL_SESSION_LABEL_CHARS]
        self.assertEqual(agent_server.local_session_label(text, "Fallback"), expected)
        self.assertEqual(agent_server.local_session_label("", text), expected)


def write_claude_transcript(
    path: Path, *, cwd: str | None, first_user_text: str,
    metadata: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if cwd is not None:
        lines.append(json.dumps({"type": "user", "cwd": cwd, "message": {"role": "user", "content": first_user_text}, **(metadata or {})}))
    else:
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": first_user_text}, **(metadata or {})}))
    path.write_text("\n".join(lines) + "\n")


class LocalClaudeSessionCandidatesTests(unittest.TestCase):
    def test_excludes_nested_and_legacy_sidechains_without_changing_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            project = root / "-work"
            main = project / "main.jsonl"
            paths = [
                main,
                project / "main" / "subagents" / "agent-child.jsonl",
                project / "main" / "subagents" / "workflows" / "run" / "agent-nested.jsonl",
                project / "agent-legacy.jsonl",
            ]
            for path in paths:
                write_claude_transcript(
                    path, cwd="/work", first_user_text="Research archived chats and subagents",
                    metadata={"isSidechain": path.name == "agent-legacy.jsonl"},
                )
            originals = {path: path.read_bytes() for path in paths}
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", root):
                candidates = agent_server.local_claude_session_candidates(set())
                # Discovery filtering must not alter explicit provider-history lookup.
                self.assertEqual(agent_server.find_claude_history("agent-legacy"), paths[-1].resolve())
            self.assertEqual([row["provider_session_id"] for row in candidates], ["main"])
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_sidechain_marker_after_a_non_message_header_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "project" / "child.jsonl"
            write_claude_transcript(transcript, cwd="/work", first_user_text="child", metadata={"isSidechain": True})
            transcript.write_text('{"type":"file-history-snapshot"}\n' + transcript.read_text())
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", root):
                self.assertEqual(agent_server.local_claude_session_candidates(set()), [])

    def test_keeps_cwd_separate_from_first_message_label(self) -> None:
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
        self.assertEqual(candidate["label"], "Fix the flaky test")

    def test_dedups_against_already_imported_provider_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-Users-georgia-code-widget" / "claude-abc123.jsonl"
            write_claude_transcript(transcript, cwd="/Users/georgia/code/widget", first_user_text="Fix the flaky test")

            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root):
                candidates = agent_server.local_claude_session_candidates({"claude-abc123"})

        self.assertEqual(candidates, [])

    def test_falls_back_to_identity_when_transcript_has_no_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            projects_root = Path(temporary) / "claude-projects"
            transcript = projects_root / "-unexpected-project-name" / "claude-xyz789.jsonl"
            write_claude_transcript(transcript, cwd=None, first_user_text="")

            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects_root):
                candidates = agent_server.local_claude_session_candidates(set())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIsNone(candidate["cwd"])
        self.assertEqual(candidate["label"], "Claude chat claude-x")

    def test_missing_projects_root_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_root = Path(temporary) / "does-not-exist"
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", missing_root):
                candidates = agent_server.local_claude_session_candidates(set())
        self.assertEqual(candidates, [])

    def test_region_reader_handles_a_small_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "small.jsonl"
            payload = b'{"type":"user","cwd":"/work"}\n'
            transcript.write_bytes(payload)

            with patch.object(
                agent_server,
                "CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES",
                64,
            ):
                regions = list(
                    agent_server.bounded_claude_transcript_regions(transcript)
                )

        self.assertEqual(regions, [payload])

    def test_region_reader_never_reads_to_eof_if_transcript_grows(self) -> None:
        class GrowingHandle:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def __enter__(self) -> "GrowingHandle":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def seek(self, _offset: int) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("transcript scan attempted an unbounded read")
                self.read_sizes.append(size)
                # The second read includes a sentinel byte, simulating growth
                # after stat without allocating beyond the fixed request size.
                return b"x" * size

        class GrowingPath:
            def __init__(self, handle: GrowingHandle) -> None:
                self.handle = handle

            def stat(self) -> Mock:
                return Mock(st_size=96)

            def open(self, mode: str) -> GrowingHandle:
                if mode != "rb":
                    raise AssertionError(f"unexpected mode: {mode}")
                return self.handle

        handle = GrowingHandle()
        with patch.object(
            agent_server,
            "CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES",
            32,
        ):
            regions = list(
                agent_server.bounded_claude_transcript_regions(
                    GrowingPath(handle)  # type: ignore[arg-type]
                )
            )

        self.assertEqual(regions, [])
        self.assertEqual(handle.read_sizes, [32, 34])


def write_codex_transcript(
    path: Path, *, session_id: str, cwd: str, first_user_text: str | None,
    metadata: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({
        "timestamp": "2026-08-23T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id, "cwd": cwd, **(metadata or {})}
    })]
    if first_user_text is not None:
        lines.append(json.dumps({
            "timestamp": "2026-08-23T00:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": first_user_text}
        }))
    path.write_text("\n".join(lines) + "\n")


class LocalCodexSessionCandidatesTests(unittest.TestCase):
    def test_excludes_all_subagent_sources_and_parent_identity(self) -> None:
        variants = [
            {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}},
            {"source": {"subagent": {"other": "worker"}}},
            {"source": {"subagent": "review"}},
            {"source": {"subAgent": {"threadSpawn": {"parentThreadId": "parent"}}}},
            *({"source": source} for source in (
                "subagent", "subagent_review", "subagent_compact", "subAgentThreadSpawn", "subAgentOther",
            )),
            {"source": "cli", "thread_source": {"subagent": {"other": "worker"}}},
            {"threadSource": "subAgent"},
            {"parent_thread_id": "parent"},
            {"parentThreadId": "parent"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            index = Path(temporary) / "session_index.jsonl"
            for number, metadata in enumerate(variants):
                with self.subTest(metadata=metadata):
                    path = root / f"rollout-child-{number}.jsonl"
                    write_codex_transcript(path, session_id=f"child-{number}", cwd="/work",
                                           first_user_text="Child task", metadata=metadata)
                    self.assertEqual(agent_server.codex_transcript_meta(path), (f"child-{number}", "/work"))
                    self.assertEqual(agent_server.codex_transcript_meta(path, exclude_subagents=True), (None, None))
            index.write_text(json.dumps({"id": "child-0", "thread_name": "Named child"}) + "\n")
            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", index,
            ):
                self.assertEqual(agent_server.local_codex_session_candidates(set()), [])
                self.assertIsNotNone(agent_server.find_codex_history("child-0"))

    def test_preserves_main_sources_legacy_metadata_and_user_forks(self) -> None:
        variants = [{}, {"forked_from_id": "original"}, {"parent_thread_id": None}]
        variants += [{"source": source} for source in ("cli", "vscode", "exec", "appServer", "unknown")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            for number, metadata in enumerate(variants):
                write_codex_transcript(root / f"rollout-main-{number}.jsonl", session_id=f"main-{number}",
                                       cwd="/work", first_user_text="Fix archived subagent history", metadata=metadata)
            with patch.object(agent_server, "CODEX_SESSIONS_ROOT", root), patch.object(
                agent_server, "CODEX_SESSION_INDEX_PATH", Path(temporary) / "missing-index",
            ):
                candidates = agent_server.local_codex_session_candidates(set())
            self.assertEqual({row["provider_session_id"] for row in candidates},
                             {f"main-{number}" for number in range(len(variants))})

    def test_excludes_archive_directory_even_with_a_broad_or_archive_scan_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = root / "sessions" / "2026" / "09" / "13" / "rollout-main.jsonl"
            archived = root / "archived_sessions" / "rollout-archived.jsonl"
            for path, session_id in ((main, "main"), (archived, "archived")):
                write_codex_transcript(path, session_id=session_id, cwd="/work", first_user_text="Hello")
            original = archived.read_bytes()
            index = root / "session_index.jsonl"
            index.write_text(json.dumps({"id": "archived", "thread_name": "Archived title"}) + "\n")
            for scan_root, expected in ((root / "sessions", ["main"]), (root, ["main"]), (archived.parent, [])):
                with self.subTest(scan_root=scan_root), patch.object(agent_server, "CODEX_SESSIONS_ROOT", scan_root), patch.object(
                    agent_server, "CODEX_SESSION_INDEX_PATH", index,
                ):
                    candidates = agent_server.local_codex_session_candidates(set())
                    self.assertEqual([row["provider_session_id"] for row in candidates], expected)
            self.assertEqual(archived.read_bytes(), original)

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
        self.assertEqual(candidate["label"], "Tell me one fun fact about narwhals.")

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
    def test_filters_before_limit_and_keeps_newest_main_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, timestamp, metadata in (
                ("old", 10, {}), ("new", 20, {}), ("child", 30, {"source": {"subagent": "review"}}),
            ):
                path = root / "sessions" / f"rollout-{name}.jsonl"
                write_codex_transcript(path, session_id=name, cwd="/work", first_user_text=name, metadata=metadata)
                os.utime(path, (timestamp, timestamp))
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", root / "missing"), patch.object(
                agent_server, "CODEX_SESSIONS_ROOT", root / "sessions",
            ), patch.object(agent_server, "CODEX_SESSION_INDEX_PATH", root / "missing-index"):
                candidates = agent_server.local_session_candidates(1, set())
            self.assertEqual([row["provider_session_id"] for row in candidates], ["new"])

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


class LocalSessionLabelsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_list_excludes_subagents_and_archives_for_both_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            codex = root / "codex"
            for name, sidechain in (("main", False), ("child", True), ("already-imported", False)):
                write_claude_transcript(projects / "project" / f"{name}.jsonl", cwd="/work",
                                        first_user_text=name, metadata={"isSidechain": sidechain})
            for name, parent in (("main", None), ("child", "main")):
                write_codex_transcript(codex / "sessions" / f"rollout-{name}.jsonl", session_id=name,
                                       cwd="/work", first_user_text=name, metadata={"parent_thread_id": parent})
            write_codex_transcript(codex / "archived_sessions" / "rollout-archived.jsonl",
                                   session_id="archived", cwd="/work", first_user_text="Old chat")
            existing = {"sess-existing": {"backend": "claude", "claude_session_id": "already-imported", "archived": True}}
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects), patch.object(
                agent_server, "CODEX_SESSIONS_ROOT", codex,
            ), patch.object(agent_server, "CODEX_SESSION_INDEX_PATH", root / "missing-index"), patch.object(
                agent_server.STORE, "sessions", existing,
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_server.app), base_url="http://test") as client:
                    response = await client.get("/api/local-sessions")
            self.assertEqual(response.status_code, 200)
            self.assertEqual({(row["backend"], row["provider_session_id"]) for row in response.json()["sessions"]},
                             {("claude", "main"), ("codex", "main")})

    async def test_http_list_sanitizes_every_label_source_without_changing_candidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="local-session-labels-") as temporary:
            root = Path(temporary)
            projects = root / "claude-projects"
            codex = root / "codex-sessions"
            indexed = "thread-indexed"
            empty_indexed = "thread-empty-title"
            preview = "thread-preview"
            claude_prompt = projects / "widget" / "claude-prompt.jsonl"
            claude_fallback = projects / "\x1b\x07" / "claude-fallback.jsonl"
            normal = projects / "normal" / "claude-normal.jsonl"
            write_claude_transcript(claude_prompt, cwd="/work/widget", first_user_text="\x1b[31mFix\x1b[0m A\x00B\t中文\nnext")
            write_claude_transcript(claude_fallback, cwd=None, first_user_text="")
            write_claude_transcript(normal, cwd="/work/normal", first_user_text="- café — 中文 👩🏽‍💻")
            for provider_id in (indexed, empty_indexed, preview):
                write_codex_transcript(
                    codex / f"{provider_id}.jsonl", session_id=provider_id, cwd="/work/codex",
                    first_user_text="From\x1b preview\x7f\vnext",
                )
            index = root / "session_index.jsonl"
            index.write_text("".join(json.dumps(entry) + "\n" for entry in (
                {"id": indexed, "thread_name": "\x1bNamed\x7f — café 中文"},
                {"id": empty_indexed, "thread_name": "\x1b\x00"},
            )), encoding="utf-8")
            paths = [claude_prompt, claude_fallback, normal, *(codex / f"{provider_id}.jsonl" for provider_id in (indexed, empty_indexed, preview))]
            for offset, path in enumerate(paths):
                os.utime(path, (1_700_000_000 + offset, 1_700_000_000 + offset))
            original_bytes = {path: path.read_bytes() for path in [*paths, index]}
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", projects), patch.object(
                agent_server, "CODEX_SESSIONS_ROOT", codex,
            ), patch.object(agent_server, "CODEX_SESSION_INDEX_PATH", index), patch.object(
                agent_server.STORE, "sessions", {},
            ), patch.object(agent_server.STORE, "_lock", asyncio.Lock()), patch.object(
                agent_server, "AGENT_TOKEN", "label-test-token",
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=agent_server.app), base_url="http://test",
                    headers={"x-agentsdock-token": "label-test-token"},
                ) as client:
                    response = await client.get("/api/local-sessions", params={"limit": 200})
            self.assertEqual(response.status_code, 200, response.text)
            candidates = response.json()["sessions"]
            self.assertEqual([candidate["provider_session_id"] for candidate in candidates], [path.stem for path in reversed(paths)])
            by_id = {candidate["provider_session_id"]: candidate for candidate in candidates}
            for candidate in candidates:
                label = candidate["label"]
                self.assertIsInstance(label, str)
                self.assertTrue(label.strip())
                self.assertLessEqual(len(label), agent_server.MAX_LOCAL_SESSION_LABEL_CHARS)
                self.assertNotRegex(label, r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
                self.assertEqual(candidate["backend"], "claude" if candidate["provider_session_id"].startswith("claude-") else "codex")
            self.assertEqual(by_id["claude-prompt"]["label"], "[31mFix[0m AB\t中文\nnext")
            self.assertEqual(by_id["claude-prompt"]["cwd"], "/work/widget")
            self.assertEqual(by_id["claude-fallback"]["label"], "Claude chat claude-f")
            self.assertIsNone(by_id["claude-fallback"]["cwd"])
            self.assertEqual(by_id["claude-normal"]["label"], "- café — 中文 👩🏽‍💻")
            self.assertEqual(by_id[indexed]["label"], "Named — café 中文")
            for provider_id in (empty_indexed, preview):
                self.assertEqual(by_id[provider_id]["label"], "From preview next")
                self.assertEqual(by_id[provider_id]["cwd"], "/work/codex")
            for path, original in original_bytes.items():
                self.assertEqual(path.read_bytes(), original)


class BulkImportSessionsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_empty_items(self) -> None:
        with self.assertRaises(agent_server.HTTPException) as raised:
            await agent_server.bulk_import_sessions(agent_server.BulkImportSessionsRequest(items=[]))
        self.assertEqual(raised.exception.status_code, 400)

    def test_schema_rejects_batches_over_the_cap(self) -> None:
        items = [
            agent_server.BulkImportSessionItem(provider_session_id=f"id-{i}", backend=agent_server.BACKEND_CLAUDE)
            for i in range(agent_server.MAX_BULK_IMPORT_ITEMS + 1)
        ]
        with self.assertRaises(ValueError):
            agent_server.BulkImportSessionsRequest(items=items)

    async def test_endpoint_defensively_rejects_batches_over_the_cap(self) -> None:
        items = [
            agent_server.BulkImportSessionItem(provider_session_id=f"id-{i}", backend=agent_server.BACKEND_CLAUDE)
            for i in range(agent_server.MAX_BULK_IMPORT_ITEMS + 1)
        ]
        request = agent_server.BulkImportSessionsRequest.model_construct(items=items)
        with self.assertRaises(agent_server.HTTPException) as raised:
            await agent_server.bulk_import_sessions(request)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn(str(agent_server.MAX_BULK_IMPORT_ITEMS), str(raised.exception.detail))

    def test_rejects_invalid_backend_and_provider_identifier(self) -> None:
        with self.assertRaises(ValueError):
            agent_server.BulkImportSessionItem(
                provider_session_id="bad-backend",
                backend="not-a-real-backend",
            )
        with self.assertRaises(ValueError):
            agent_server.BulkImportSessionItem(
                provider_session_id="../../outside.jsonl",
                backend=agent_server.BACKEND_CLAUDE,
            )

    async def test_no_messages_returns_failure_without_creating_a_chat(self) -> None:
        item = agent_server.BulkImportSessionItem(
            provider_session_id="claude-empty",
            backend=agent_server.BACKEND_CLAUDE,
        )
        candidate = {
            "provider_session_id": "claude-empty",
            "backend": agent_server.BACKEND_CLAUDE,
            "label": "Empty transcript",
            "updated_at": "2026-08-23T00:00:00Z",
            "cwd": "/work",
        }
        with patch.object(agent_server.STORE, "sessions", {}), patch.object(
            agent_server,
            "local_session_candidates",
            return_value=[candidate],
        ), patch.object(
            agent_server,
            "provider_history",
            return_value=(Path("/contained/empty.jsonl"), []),
        ), patch.object(
            agent_server.STORE,
            "create",
            AsyncMock(),
        ) as create:
            result = await agent_server.bulk_import_sessions(
                agent_server.BulkImportSessionsRequest(items=[item])
            )

        self.assertEqual(result["results"], [{
            "provider_session_id": "claude-empty",
            "backend": agent_server.BACKEND_CLAUDE,
            "session_id": None,
            "ok": False,
            "imported": 0,
            "code": "no_messages",
            "error": "The transcript contains no importable chat messages.",
        }])
        create.assert_not_awaited()

    async def test_partial_write_is_rolled_back_and_reported_truthfully(self) -> None:
        class FakeStore:
            def __init__(self) -> None:
                self._lock = asyncio.Lock()
                self.sessions: dict[str, dict[str, object]] = {}
                self.deleted: list[str] = []

            async def create(self, _req: object, *, initializing_import: bool = False):
                self.assert_initializing = initializing_import
                session = {
                    "id": "sess-staged",
                    "backend": agent_server.BACKEND_CLAUDE,
                    "session_id": "claude-one",
                    "claude_session_id": "claude-one",
                    "_fork_initializing": True,
                    "_history_import_initializing": True,
                }
                self.sessions["sess-staged"] = session
                return session

            async def delete(self, session_id: str) -> bool:
                self.deleted.append(session_id)
                return self.sessions.pop(session_id, None) is not None

            async def save(self) -> None:
                return None

        store = FakeStore()
        item = agent_server.BulkImportSessionItem(
            provider_session_id="claude-one",
            backend=agent_server.BACKEND_CLAUDE,
        )
        candidate = {
            "provider_session_id": "claude-one",
            "backend": agent_server.BACKEND_CLAUDE,
            "label": "One",
            "updated_at": "2026-08-23T00:00:00Z",
            "cwd": "/work",
        }
        with patch.object(agent_server, "STORE", store), patch.object(
            agent_server,
            "local_session_candidates",
            return_value=[candidate],
        ), patch.object(
            agent_server,
            "provider_history",
            return_value=(Path("/contained/one.jsonl"), [{"kind": "user", "text": "hello"}]),
        ), patch.object(
            agent_server,
            "append_staged_imported_history",
            AsyncMock(side_effect=OSError("disk full")),
        ):
            result = await agent_server.bulk_import_sessions(
                agent_server.BulkImportSessionsRequest(items=[item])
            )

        self.assertEqual(store.deleted, ["sess-staged"])
        self.assertEqual(store.sessions, {})
        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["session_id"], None)
        self.assertEqual(result["results"][0]["imported"], 0)
        self.assertEqual(result["results"][0]["code"], "import_failed")

    async def test_success_commits_staged_chat_and_reports_import_count(self) -> None:
        class FakeStore:
            def __init__(self) -> None:
                self._lock = asyncio.Lock()
                self.sessions: dict[str, dict[str, object]] = {}
                self.saved = 0

            async def create(self, _req: object, *, initializing_import: bool = False):
                self.assert_initializing = initializing_import
                session = {
                    "id": "sess-staged",
                    "backend": agent_server.BACKEND_CODEX,
                    "session_id": "codex-one",
                    "codex_thread_id": "codex-one",
                    "_fork_initializing": True,
                    "_history_import_initializing": True,
                }
                self.sessions["sess-staged"] = session
                return session

            async def delete(self, session_id: str) -> bool:
                return self.sessions.pop(session_id, None) is not None

            async def save(self) -> None:
                self.saved += 1

        store = FakeStore()
        item = agent_server.BulkImportSessionItem(
            provider_session_id="codex-one",
            backend=agent_server.BACKEND_CODEX,
            cwd="/work",
        )
        candidate = {
            "provider_session_id": "codex-one",
            "backend": agent_server.BACKEND_CODEX,
            "label": "One",
            "updated_at": "2026-08-23T00:00:00Z",
            "cwd": "/work",
        }
        with patch.object(agent_server, "STORE", store), patch.object(
            agent_server,
            "local_session_candidates",
            return_value=[candidate],
        ), patch.object(
            agent_server,
            "provider_history",
            return_value=(Path("/contained/one.jsonl"), [{"kind": "user", "text": "hello"}]),
        ), patch.object(
            agent_server,
            "append_staged_imported_history",
            AsyncMock(return_value={"imported": 1}),
        ):
            result = await agent_server.bulk_import_sessions(
                agent_server.BulkImportSessionsRequest(items=[item])
            )

        self.assertTrue(store.assert_initializing)
        self.assertEqual(store.saved, 1)
        self.assertNotIn("_fork_initializing", store.sessions["sess-staged"])
        self.assertNotIn("_history_import_initializing", store.sessions["sess-staged"])
        self.assertEqual(result["results"], [{
            "provider_session_id": "codex-one",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": "sess-staged",
            "ok": True,
            "imported": 1,
        }])

    async def test_duplicate_request_is_not_imported_twice(self) -> None:
        item = agent_server.BulkImportSessionItem(
            provider_session_id="claude-duplicate",
            backend=agent_server.BACKEND_CLAUDE,
        )
        candidate = {
            "provider_session_id": "claude-duplicate",
            "backend": agent_server.BACKEND_CLAUDE,
            "label": "Duplicate",
            "updated_at": "2026-08-23T00:00:00Z",
            "cwd": None,
        }
        provider_history = Mock(return_value=(Path("/contained/empty.jsonl"), []))
        with patch.object(agent_server.STORE, "sessions", {}), patch.object(
            agent_server,
            "local_session_candidates",
            return_value=[candidate],
        ), patch.object(agent_server, "provider_history", provider_history):
            result = await agent_server.bulk_import_sessions(
                agent_server.BulkImportSessionsRequest(items=[item, item])
            )

        self.assertEqual(provider_history.call_count, 1)
        self.assertEqual(result["results"][1]["code"], "duplicate_request")


class LocalTranscriptSafetyTests(unittest.TestCase):
    def test_discovery_exclusions_prune_directories_without_affecting_default_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = root / "main.jsonl"
            main.write_text("{}\n")
            for name in ("subagents", "archived_sessions"):
                directory = root / name
                directory.mkdir()
                (directory / "child.jsonl").write_text("{}\n")
            with patch.object(agent_server, "MAX_LOCAL_SESSION_SCAN_FILES", 3):
                paths = list(agent_server.bounded_jsonl_paths(
                    root, excluded_directory_names=frozenset({"subagents", "archived_sessions"}),
                ))
            self.assertEqual(paths, [main.resolve()])
            self.assertEqual(len(list(agent_server.bounded_jsonl_paths(root))), 3)

    TASK_NOTIFICATION = (
        "<task-notification>\n"
        "<task-id>workflow-42</task-id>\n"
        "<tool-use-id>tool-42</tool-use-id>\n"
        "<status>stopped</status>\n"
        "<summary>Resume the provider workflow.</summary>\n"
        "</task-notification>"
    )

    def test_direct_path_must_be_contained_and_not_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            external = base / "external.jsonl"
            external.write_text("{}\n")
            link = root / "linked.jsonl"
            link.symlink_to(external)

            self.assertIsNone(agent_server.path_if_jsonl(str(external), root))
            self.assertIsNone(agent_server.path_if_jsonl(str(link), root))

    def test_parser_rejects_oversized_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "too-wide.jsonl"
            transcript.write_text(json.dumps({"type": "user", "message": "x" * 100}) + "\n")
            with patch.object(agent_server, "MAX_LOCAL_TRANSCRIPT_LINE_BYTES", 32):
                with self.assertRaises(ValueError):
                    agent_server.parse_claude_history(transcript, None)

    def test_parser_retains_only_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "tail.jsonl"
            transcript.write_text("\n".join(
                json.dumps({"type": "user", "message": f"message-{index}"})
                for index in range(8)
            ) + "\n")
            with patch.object(agent_server, "MAX_IMPORT_MESSAGES", 3):
                items = agent_server.parse_claude_history(transcript, None)

        self.assertEqual([item["text"] for item in items], [
            "message-5",
            "message-6",
            "message-7",
        ])

    def test_claude_parser_omits_sdk_task_notification_origin(self) -> None:
        events = [
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "promptSource": "sdk",
                "queueSkipAttachments": True,
                "message": {
                    "role": "user",
                    "content": self.TASK_NOTIFICATION,
                },
            },
            {
                # Content alone is not authority to hide a real user turn.
                "type": "user",
                "message": {
                    "role": "user",
                    "content": self.TASK_NOTIFICATION,
                },
            },
            {
                "type": "assistant",
                "message": {"content": "Finished normally."},
            },
        ]

        self.assertEqual(
            agent_server.parse_claude_history_events(events, None),
            [
                {"kind": "user", "text": self.TASK_NOTIFICATION},
                {"kind": "assistant", "text": "Finished normally."},
            ],
        )

    def test_legacy_imported_task_notification_is_hidden_from_clients(self) -> None:
        imported = {
            "type": "turn_started",
            "session_id": "chat-legacy",
            "backend": agent_server.BACKEND_CLAUDE,
            "run_id": "import_legacy",
            "imported": True,
            "prompt": self.TASK_NOTIFICATION,
        }

        # Keep the empty turn boundary so pagination and the following answer
        # retain their semantic owner; the provider-only text itself is
        # removed at the shared client projection boundary.
        self.assertTrue(agent_server.is_client_visible_event(imported))
        self.assertEqual(agent_server.client_safe_event(imported)["prompt"], "")
        self.assertIsNone(agent_server.history_search_event_record(imported))
        self.assertTrue(agent_server.is_client_visible_event({
            **imported,
            "run_id": "run_human",
            "imported": False,
        }))
        self.assertTrue(agent_server.is_client_visible_event({
            **imported,
            "prompt": f"Please explain this:\n{self.TASK_NOTIFICATION}",
        }))
        self.assertTrue(agent_server.is_client_visible_event({
            **imported,
            "backend": agent_server.BACKEND_CODEX,
        }))


class StagedHistoryBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_history_uses_one_transactional_event_batch(self) -> None:
        session = {
            "id": "sess-staged",
            "backend": agent_server.BACKEND_CODEX,
            "session_id": "codex-batch",
            "codex_thread_id": "codex-batch",
        }
        items = [
            {"kind": "user", "text": "hello"},
            {"kind": "assistant", "text": "hi"},
        ]
        # marker + two messages + the terminal turn_finished that closes the
        # import run so clients never treat replayed history as live.
        append_batch = AsyncMock(return_value=4)
        with patch.object(
            agent_server,
            "append_imported_events",
            append_batch,
        ), patch.object(agent_server, "append_event", AsyncMock()) as append_live:
            result = await agent_server.append_staged_imported_history(
                session,
                Path("/contained/codex-batch.jsonl"),
                items,
            )

        self.assertEqual(result["imported"], 2)
        append_live.assert_not_awaited()
        event_specs = append_batch.await_args.args[1]
        self.assertEqual(
            [event_type for event_type, _payload in event_specs],
            ["history_imported", "turn_started", "assistant_text", "turn_finished"],
        )
        self.assertTrue(event_specs[1][1]["provider_history_sanitized"])
        self.assertTrue(event_specs[-1][1]["imported"])

    async def test_staged_history_rejects_an_incomplete_batch_write(self) -> None:
        session = {
            "id": "sess-staged",
            "backend": agent_server.BACKEND_CLAUDE,
            "session_id": "claude-batch",
            "claude_session_id": "claude-batch",
        }
        with patch.object(
            agent_server,
            "append_imported_events",
            AsyncMock(return_value=1),
        ):
            with self.assertRaises(RuntimeError):
                await agent_server.append_staged_imported_history(
                    session,
                    Path("/contained/claude-batch.jsonl"),
                    [{"kind": "user", "text": "hello"}],
                )


class StagedImportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_discards_staged_import_without_provider_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            sessions_file = state_dir / "sessions.json"
            sessions_file.write_text(json.dumps({
                "sess-staged": {
                    "id": "sess-staged",
                    "backend": agent_server.BACKEND_CODEX,
                    "session_id": "provider-source",
                    "codex_thread_id": "provider-source",
                    "_fork_initializing": True,
                    "_history_import_initializing": True,
                }
            }))
            store = agent_server.SessionStore()
            with patch.object(agent_server, "STATE_DIR", state_dir), patch.object(
                agent_server,
                "SESSIONS_FILE",
                sessions_file,
            ), patch.object(agent_server, "ensure_dirs"), patch.object(
                agent_server,
                "read_abandoned_fork_thread_ids",
                return_value=set(),
            ), patch.object(
                agent_server,
                "write_abandoned_fork_thread_ids",
            ) as write_cleanup, patch.object(
                agent_server,
                "delete_session_owned_file_records",
            ):
                await store.load()

        self.assertEqual(store.sessions, {})
        write_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
