"""Same-user import exclusion; synthetic homes only, never installed services."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
import server_instances as instances


class ImportFixtures:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cross-instance-import-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.registry = instances.Registry(self.home)
        self.current = instances.Instance("new-server", self.home)
        self.default = instances.Instance("default", self.home)
        self.other = instances.Instance("work", self.home)
        self.addCleanup(patch.stopall)
        patch.object(instances, "Registry", return_value=self.registry).start()

    def index(self, instance, rows, *, configured=True):
        instance.state.mkdir(parents=True, exist_ok=True)
        if configured:
            instance.config.mkdir(parents=True, exist_ok=True)
            (instance.config / "env").write_text(
                f"AGENTSDOCK_STATE_DIR={instance.state}\n"
            )
        target = instance.state / "sessions.json"
        target.write_text(json.dumps(rows))
        return target

    def keys(self):
        return instances.other_instance_provider_keys(self.current.state)


class CrossInstanceIndexTests(ImportFixtures, unittest.TestCase):
    def test_no_instances_is_read_only(self):
        self.assertEqual(self.keys(), set())
        self.assertFalse(self.registry.root.exists())

    def test_legacy_default_without_manager_registration_is_excluded(self):
        target = self.index(self.default, {"local": {"backend": "claude", "session_id": "owned"}})
        original = target.read_bytes()
        self.assertEqual(self.keys(), {("claude", "owned")})
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.registry.root.exists())

    def test_stopped_named_instance_and_archived_chat_still_count(self):
        self.index(self.other, {"local": {"backend": "codex", "codex_thread_id": "owned", "archived": True}})
        with patch.object(instances, "service_status", side_effect=AssertionError("must not query services")):
            self.assertEqual(self.keys(), {("codex", "owned")})

    def test_default_also_excludes_named_instances(self):
        self.index(self.other, {"local": {"backend": "claude", "claude_session_id": "named-owned"}})
        self.assertEqual(instances.other_instance_provider_keys(self.default.state), {("claude", "named-owned")})

    def test_current_index_is_skipped_even_if_old_or_unreadable(self):
        target = self.index(self.current, {})
        target.write_text("not json")
        self.assertEqual(self.keys(), set())

    def test_provider_namespace_and_parked_identities(self):
        self.index(self.other, {"local": {
            "backend": "claude", "session_id": "legacy-not-active",
            "claude_session_id": "same-id", "codex_thread_id": "same-id",
            "cursor_session_id": "cursor-id", "opencode_session_id": "open-id",
        }})
        self.assertEqual(self.keys(), {
            ("claude", "same-id"), ("codex", "same-id"),
            ("cursor", "cursor-id"), ("opencode", "open-id"),
        })

    def test_removed_instance_preserved_history_is_not_reserved(self):
        self.index(self.other, {"local": {"backend": "claude", "session_id": "old"}}, configured=False)
        with self.registry.locked():
            self.registry.save(self.other, "removed")
        self.assertEqual(self.keys(), set())

    def test_registered_instance_without_index_is_empty(self):
        with self.registry.locked():
            self.registry.save(self.other, "installed")
        self.assertEqual(self.keys(), set())

    def test_deletion_and_atomic_replacement_are_observed_without_restart(self):
        target = self.index(self.other, {"local": {"backend": "claude", "session_id": "owned"}})
        self.assertIn(("claude", "owned"), self.keys())
        replacement = target.with_suffix(".new")
        replacement.write_text("{}")
        replacement.replace(target)
        self.assertEqual(self.keys(), set())

    def test_malformed_index_fails_closed(self):
        target = self.index(self.other, {})
        for content in ("{", "[]", '{"bad":"not-a-session"}'):
            with self.subTest(content=content):
                target.write_text(content)
                with self.assertRaises(ValueError):
                    self.keys()

    def test_symlink_index_is_rejected_without_following_it(self):
        self.index(self.other, {})
        target = self.other.state / "sessions.json"
        outside = self.home / "private.json"
        outside.write_text("{}")
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Unsafe managed path"):
            self.keys()

    def test_named_instance_cannot_inject_another_state_path(self):
        self.index(self.other, {})
        (self.other.config / "env").write_text(f"AGENTSDOCK_STATE_DIR={self.default.state}\n")
        with self.assertRaisesRegex(ValueError, "unexpected state binding"):
            self.keys()

    def test_custom_default_state_within_owned_home_is_supported(self):
        self.index(self.default, {})
        custom = self.home / "custom-state"
        custom.mkdir()
        (custom / "sessions.json").write_text('{"chat":{"backend":"codex","session_id":"custom"}}')
        (self.default.config / "env").write_text(f"AGENTSDOCK_STATE_DIR={custom}\n")
        self.assertEqual(self.keys(), {("codex", "custom")})

    def test_custom_state_cannot_escape_home(self):
        self.index(self.default, {})
        (self.default.config / "env").write_text("AGENTSDOCK_STATE_DIR=/outside-user-home\n")
        with self.assertRaises(ValueError):
            self.keys()

    def test_index_larger_than_configuration_limit_is_supported_but_bounded(self):
        self.index(self.other, {"local": {"backend": "claude", "session_id": "owned", "padding": "x" * (1024 * 1024)}})
        self.assertEqual(self.keys(), {("claude", "owned")})
        with patch.object(instances, "MAX_IMPORT_INDEX_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "too large"):
                self.keys()

    def test_total_index_and_instance_limits_fail_closed(self):
        self.index(self.default, {"a": {"session_id": "one"}})
        self.index(self.other, {"b": {"session_id": "two"}})
        with patch.object(instances, "MAX_IMPORT_INDEX_TOTAL_BYTES", 50):
            with self.assertRaises(ValueError):
                self.keys()
        with patch.object(instances, "MAX_IMPORT_INSTANCES", 1):
            with self.assertRaises(ValueError):
                self.keys()

    def test_import_guard_rejects_concurrency_and_releases_on_exception(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with instances.history_import_lock():
                with self.assertRaisesRegex(ValueError, "Another process owns"):
                    with instances.history_import_lock():
                        self.fail("concurrent import acquired ownership")
                raise RuntimeError("synthetic")
        with instances.history_import_lock():
            pass


class CrossInstanceImportEndpointTests(ImportFixtures, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        patch.object(agent_server, "STATE_DIR", self.current.state).start()
        patch.object(agent_server.STORE, "sessions", {}).start()
        self.create = patch.object(agent_server.STORE, "create", AsyncMock()).start()
        patch.object(agent_server, "local_codex_session_candidates", return_value=[]).start()
        self.candidates = [
            {"provider_session_id": "owned", "backend": "claude", "label": "Owned", "updated_at": "2026-09-19", "cwd": None},
            {"provider_session_id": "free", "backend": "claude", "label": "Free", "updated_at": "2026-09-18", "cwd": None},
        ]
        patch.object(agent_server, "local_claude_session_candidates",
                     side_effect=lambda known: [row for row in self.candidates if row["provider_session_id"] not in known]).start()

    async def test_picker_excludes_other_server_before_applying_limit(self):
        self.index(self.default, {"local": {"backend": "claude", "claude_session_id": "owned"}})
        result = await agent_server.get_local_sessions(limit=1)
        self.assertEqual([row["provider_session_id"] for row in result["sessions"]], ["free"])

    async def test_same_identifier_on_different_backend_is_not_hidden(self):
        self.index(self.other, {"local": {"backend": "codex", "codex_thread_id": "owned"}})
        result = await agent_server.get_local_sessions(limit=10)
        self.assertEqual(len(result["sessions"]), 2)

    async def test_bulk_rechecks_ownership_after_picker_was_opened(self):
        self.assertEqual(len((await agent_server.get_local_sessions(limit=10))["sessions"]), 2)
        self.index(self.other, {"local": {"backend": "claude", "claude_session_id": "owned"}})
        req = agent_server.BulkImportSessionsRequest(items=[agent_server.BulkImportSessionItem(backend="claude", provider_session_id="owned")])
        result = await agent_server.bulk_import_sessions(req)
        self.assertEqual(result["results"][0]["code"], "already_imported")
        self.assertIn("another local", result["results"][0]["error"])
        self.create.assert_not_awaited()

    async def test_manual_resume_cannot_bypass_filter_including_import_history_false(self):
        for backend, field in instances.PROVIDER_ID_FIELDS.items():
            self.index(self.other, {"local": {"backend": backend, field: "owned"}})
            for supplied_field in (field, "provider_session_id", "session_id"):
                with self.subTest(backend=backend, supplied_field=supplied_field):
                    req = agent_server.CreateSessionRequest(**{"backend": backend, supplied_field: "owned", "import_history": False})
                    with self.assertRaises(agent_server.HTTPException) as error:
                        await agent_server.create_session(req)
                    self.assertEqual(error.exception.status_code, 409)
        self.create.assert_not_awaited()

    async def test_fresh_chat_does_not_check_other_instances(self):
        with patch.object(agent_server, "other_local_instance_provider_keys", side_effect=AssertionError("unnecessary scan")), patch.object(
            agent_server, "create_session_with_history", AsyncMock(return_value={"session": {}}),
        ) as create:
            req = agent_server.CreateSessionRequest(backend="claude")
            self.assertEqual(await agent_server.create_session(req), {"session": {}})
            create.assert_awaited_once_with(req)
        self.assertFalse(self.registry.root.exists())

    async def test_failed_resume_releases_guard_for_retry(self):
        req = agent_server.CreateSessionRequest(backend="claude", provider_session_id="free")
        with patch.object(agent_server, "create_session_with_history", AsyncMock(side_effect=RuntimeError("synthetic"))):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                await agent_server.create_session(req)
        with instances.history_import_lock():
            pass

    async def test_cancelled_resume_releases_guard_for_retry(self):
        req = agent_server.CreateSessionRequest(backend="claude", provider_session_id="free")
        with patch.object(agent_server, "create_session_with_history", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await agent_server.create_session(req)
        with instances.history_import_lock():
            pass

    async def test_concurrent_import_returns_retryable_error_without_creating(self):
        req = agent_server.CreateSessionRequest(backend="claude", provider_session_id="free")
        with instances.history_import_lock():
            with self.assertRaises(agent_server.HTTPException) as error:
                await agent_server.create_session(req)
        self.assertEqual(error.exception.status_code, 503)
        self.create.assert_not_awaited()

    async def test_unreadable_foreign_index_blocks_picker_and_import_not_new_chats(self):
        target = self.index(self.other, {})
        target.write_text("not json")
        with self.assertRaises(agent_server.HTTPException) as error:
            await agent_server.get_local_sessions(limit=10)
        self.assertEqual(error.exception.status_code, 503)
        req = agent_server.CreateSessionRequest(backend="claude", provider_session_id="free")
        with self.assertRaises(agent_server.HTTPException) as error:
            await agent_server.create_session(req)
        self.assertEqual(error.exception.status_code, 503)
        self.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
