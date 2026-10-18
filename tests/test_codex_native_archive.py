"""Native archive recognition must not be inferred from arbitrary error text."""
import unittest
import os
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import agent_server as server
from codex_app_server import CodexAppServerClient, CodexAppServerRequestError


class NativeArchive(unittest.IsolatedAsyncioTestCase):
    def error(self, thread="thread-native", method="thread/resume"):
        return CodexAppServerRequestError(method, {"code": -32600, "message":
            f"session {thread} is archived. Run `codex unarchive {thread}` to unarchive it first."})

    def test_exact_native_identity_and_method_only(self):
        self.assertTrue(server.codex_native_archive_rejection(self.error(), "thread-native"))
        for error, thread in [(self.error(), "other-thread"), (self.error(), ""),
                (self.error(method="turn/start"), "thread-native"),
                (RuntimeError(str(self.error())), "thread-native"),
                (CodexAppServerRequestError("thread/resume", {"message": "Login expired"}), "thread-native")]:
            self.assertFalse(server.codex_native_archive_rejection(error, thread))

    async def test_current_run_uses_normal_local_archive_not_provider_deletion(self):
        session = {"id": "chat", "backend": "codex", "session_id": "thread-native"}
        with patch.object(server.STORE, "sessions", {"chat": session}), \
                patch.object(server, "ACTIVE", {"chat": {"run_id": "run"}}), \
                patch.object(server, "DELETING_SESSIONS", set()), \
                patch.object(server, "_update_session", new_callable=AsyncMock) as archive:
            await server.archive_rejected_codex_session("chat", "thread-native", "run")
            archive.assert_awaited_once_with("chat", server.UpdateSessionRequest(archived=True), native_archive_owner=("thread-native", "run"))
            archive.reset_mock()
            for sid, tid, run in [("missing", "thread-native", "run"), ("chat", "other", "run"),
                                  ("chat", "thread-native", "stale")]:
                await server.archive_rejected_codex_session(sid, tid, run)
            archive.assert_not_awaited()
            self.assertEqual(session["session_id"], "thread-native")

    async def test_owner_is_rechecked_inside_lifecycle_lock_before_local_mutation(self):
        session = {"id": "chat", "backend": "codex", "session_id": "thread-new"}
        with patch.object(server.STORE, "sessions", {"chat": session}), \
                patch.object(server, "ACTIVE", {"chat": {"run_id": "new-run"}}), \
                patch.object(server, "public_session", side_effect=lambda row: dict(row)), \
                patch.object(server.STORE, "update", new_callable=AsyncMock) as update:
            result = await server._update_session("chat", server.UpdateSessionRequest(archived=True),
                                                   native_archive_owner=("thread-old", "old-run"))
            update.assert_not_awaited()
            self.assertEqual(result['session'], session)

    @unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_CODEX_BIN"), "optional native Codex archive contract")
    async def test_real_codex_archive_rejection_preserves_history(self):
        # Entirely disposable provider HOME; no account, inference or real chat.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".codex"
            home.mkdir(mode=0o700)
            env = {"PATH": os.environ["PATH"], "HOME": root, "CODEX_HOME": str(home)}
            client = CodexAppServerClient(os.environ["AGENTSDOCK_TEST_CODEX_BIN"], cwd=root,
                env_factory=lambda: env, lifecycle_timeout=15)
            try:
                await client.start()
                thread = await client.start_thread({"cwd": root, "ephemeral": False})
                await client.inject_items(thread, [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Disposable archive fixture, no model requested."}]}])
                await client.request("thread/archive", {"threadId": thread})
                with self.assertRaises(CodexAppServerRequestError) as rejected:
                    await client.resume_thread(thread, {"cwd": root})
                self.assertTrue(server.codex_native_archive_rejection(rejected.exception, thread))
                self.assertTrue(any((home / "archived_sessions").rglob("*.jsonl")))
            finally:
                await client.close()
