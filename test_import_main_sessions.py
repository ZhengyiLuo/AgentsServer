"""Import filters backported from main #108; synthetic transcripts only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import agent_server


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


class ImportMainSessionFilters(unittest.IsolatedAsyncioTestCase):
    def test_unrelated_sidechain_records_do_not_hide_main_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "project" / "main.jsonl"
            write_claude_transcript(path, cwd="/work", first_user_text="Main prompt")
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "custom-title", "isSidechain": True, "customTitle": "Child metadata"}) + "\n")
                stream.write(json.dumps({"type": "user", "sessionId": "different-child", "isSidechain": True}) + "\n")
                stream.write('[' * 2000 + ']' * 2000 + '\n')
            with patch.object(agent_server, "CLAUDE_PROJECTS_ROOT", root):
                self.assertEqual([row["provider_session_id"] for row in agent_server.local_claude_session_candidates(set())], ["main"])

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
                    response = await client.get("/api/local-sessions", headers={"X-AgentsDock-Token": agent_server.AGENT_TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual({(row["backend"], row["provider_session_id"]) for row in response.json()["sessions"]},
                             {("claude", "main"), ("codex", "main")})

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
