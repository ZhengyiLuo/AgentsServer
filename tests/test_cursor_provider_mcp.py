"""Cursor native config, narrow MCP permission, IPC bounds and run revocation."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
import os
from unittest.mock import AsyncMock, patch

from cursor_provider_mcp import (
    CursorToolBroker, CursorRuntimeProfile, MCP_NAME, MCP_ALLOW, ENV_PORT, ENV_SECRET,
    MAX_MESSAGE, permission_snapshot, config_root, read_config,
)


class ProfileTests(unittest.TestCase):
    def test_permissions_preserve_allow_scope_and_all_denies(self):
        base = {"permissions": {"allow": ["Shell(ls)"], "deny": ["Mcp(*:*)", "Shell(rm)"]}, "notifications": False}
        project = {"permissions": {"allow": ["Read(src/**)"], "deny": ["Write(.env)"]}}
        result = permission_snapshot(base, [project])
        self.assertEqual(result["permissions"]["allow"], ["Read(src/**)", MCP_ALLOW])
        self.assertEqual(result["permissions"]["deny"], ["Mcp(*:*)", "Shell(rm)", "Write(.env)"])
        self.assertFalse(result["notifications"])
        self.assertEqual(base["permissions"]["allow"], ["Shell(ls)"])
        self.assertEqual(permission_snapshot({}, [])["permissions"]["allow"], ["Shell(ls)", MCP_ALLOW])

    def test_malformed_permissions_fail_closed(self):
        for value in (None, "all", {"allow": "*"}, {"deny": [1]}, {"unknown": []}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                permission_snapshot({"permissions": value}, [])

    def test_profile_never_mutates_native_files_and_removes_temporary_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            config = home / ".cursor"
            config.mkdir(parents=True)
            original = '{"permissions":{"allow":["Shell(ls)"],"deny":["Shell(rm)"]}}'
            (config / "cli-config.json").write_text(original)
            (config / "mcp.json").write_text('{"mcpServers":{}}')
            ((config / "projects").resolve()).mkdir()
            cwd = root / "workspace"
            (cwd / ".cursor").mkdir(parents=True)
            (cwd / ".cursor/cli.json").write_text('{"permissions":{"deny":["Mcp(*:danger)"]}}')
            env = {"HOME": str(home), "PATH": os.defpath, ENV_PORT: "1", ENV_SECRET: "synthetic"}
            profile = CursorRuntimeProfile(env, str(cwd))
            try:
                injected, flags = profile.prepare()
                overlay = Path(injected["CURSOR_CONFIG_DIR"])
                permissions = json.loads((overlay / "cli-config.json").read_text())["permissions"]
                self.assertIn(MCP_ALLOW, permissions["allow"])
                self.assertEqual(permissions["deny"], ["Shell(rm)", "Mcp(*:danger)"])
                self.assertEqual((config / "cli-config.json").read_text(), original)
                self.assertEqual((overlay / "projects").resolve(), (config / "projects").resolve())
                self.assertEqual(set(injected), {"CURSOR_CONFIG_DIR"})
                self.assertEqual(flags[0], "--disable-project-configs")
                self.assertNotIn("--force", flags)
                self.assertNotIn("--approve-mcps", flags)
                self.assertEqual((overlay / "cli-config.json").stat().st_mode & 0o777, 0o600)
            finally:
                profile.close()
            self.assertFalse(overlay.exists())
            self.assertEqual((config / "cli-config.json").read_text(), original)
            self.assertTrue(((config / "projects").resolve()).is_dir())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires a native FIFO")
    def test_repository_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cli.json"
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                read_config(path)

    def test_config_roots_preserve_native_override(self):
        self.assertEqual(config_root({"HOME": "/home/test"}), Path("/home/test/.cursor").resolve())
        self.assertEqual(config_root({"HOME": "/home/test", "XDG_CONFIG_HOME": "/tmp/cfg"}), Path("/tmp/cfg/cursor").resolve())
        self.assertEqual(config_root({"CURSOR_CONFIG_DIR": "/tmp/private"}), Path("/tmp/private").resolve())


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = AsyncMock(return_value=('accepted: message-id', False))
        self.broker = CursorToolBroker({"name": "run"}, self.executor)
        self.env = await self.broker.start()
        self.addAsyncCleanup(self.broker.close)

    async def send(self, request):
        reader, writer = await asyncio.open_connection("127.0.0.1", int(self.env[ENV_PORT]))
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        result = await asyncio.wait_for(reader.readline(), 2)
        writer.close()
        await writer.wait_closed()
        return json.loads(result)

    async def test_wrong_instance_secret_never_executes(self):
        result = await self.send({"secret": "different-server", "method": "call", "key": "k", "arguments": {}})
        self.assertIn("error", result)
        self.executor.assert_not_awaited()
        self.assertNotIn("different-server", json.dumps(result))

    async def test_exact_broker_passes_arguments_and_call_identity(self):
        args = {"helper": "chats", "arguments": ["inbox"]}
        result = await self.send({"secret": self.env[ENV_SECRET], "method": "call", "key": "stable", "arguments": args})
        self.executor.assert_awaited_once_with(args, "stable")
        self.assertFalse(result["result"]["isError"])

    async def test_expired_run_failure_not_reported_as_delivery(self):
        self.executor.side_effect = RuntimeError("sensitive internal value")
        result = await self.send({"secret": self.env[ENV_SECRET], "method": "call", "key": "k", "arguments": {}})
        self.assertIn("error", result)
        self.assertNotIn("sensitive", json.dumps(result))

    async def test_close_cancels_pending_call_and_revokes_endpoint(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def pending(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.executor.side_effect = pending
        reader, writer = await asyncio.open_connection("127.0.0.1", int(self.env[ENV_PORT]))
        writer.write((json.dumps({"secret": self.env[ENV_SECRET], "method": "call", "key": "k", "arguments": {}}) + "\n").encode())
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
        await self.broker.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(await reader.read(), b"")
        writer.close()
        await writer.wait_closed()
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", int(self.env[ENV_PORT]))


class CursorOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import agent_server as server
        self.server = server
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ready = asyncio.Event()
        self.ready.set()
        self.active = {"run_id": "run_test", "backend": "cursor", "transport": "exec",
                       "proc": SimpleNamespace(returncode=None), "provider_turn_ready": True,
                       "provider_tools_ready": self.ready, "cursor_mcp_owner_token": "owner"}
        state = {"ACTIVE": {"chat": self.active}, "CURRENT_TURNS": {"chat": {"run_id": "run_test"}},
                 "BUSY_SESSIONS": {"chat"}, "STOPPED_RUNS": set(), "DELETING_SESSIONS": set(),
                 "DELETED_SESSION_TOMBSTONES": set(), "STORE": SimpleNamespace(sessions={"chat": {}}),
                 "ACTIVE_LOCK": asyncio.Lock(), "CROSS_CHAT_CAPABILITY_LOCK": asyncio.Lock(),
                 "CROSS_CHAT_CAPABILITIES": {"token": {"source_session_id": "chat", "source_run_id": "run_test",
                   "authority_path": str(server.CROSS_CHAT_AUTHORITY_ROOT / ('run_test-' + '0' * 32 + '.json')),
                   "provider_runtime_env": {}}}}
        for name, value in state.items():
            self.stack.enter_context(patch.object(server, name, value))

    async def snapshot(self, **kwargs):
        return await self.server.provider_tool_capability_snapshot("chat", "run_test", backend="cursor", cursor_owner_token=kwargs.get("owner", "owner"))

    async def test_live_owner_and_capability_both_required(self):
        await self.snapshot()
        self.server.CROSS_CHAT_CAPABILITIES.clear()
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot()

    async def test_stopped_replaced_exited_or_foreign_owner_rejected(self):
        for key, value in (("stop_requested", True), ("run_id", "run_other"),
                           ("transport", "acp"), ("cursor_mcp_owner_token", "other"),
                           ("proc", SimpleNamespace(returncode=0)), ("provider_turn_ready", False)):
            original = self.active.get(key)
            self.active[key] = value
            with self.subTest(key=key), self.assertRaises(self.server.ProviderToolError):
                await self.snapshot()
            self.active[key] = original
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot(owner="stale-run")
        self.server.STOPPED_RUNS.add("run_test")
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot()

    async def test_ambiguous_capability_and_other_chat_denied(self):
        self.server.CROSS_CHAT_CAPABILITIES["second"] = dict(self.server.CROSS_CHAT_CAPABILITIES["token"])
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot()
        self.server.CROSS_CHAT_CAPABILITIES.pop("second")
        self.server.CURRENT_TURNS["chat"]["run_id"] = "run_successor"
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot()


if __name__ == "__main__":
    unittest.main()
