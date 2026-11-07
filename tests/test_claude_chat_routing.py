import asyncio
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from claude_chat_routing import ClaudeChatRouting, print_routing_settings
from claude_sdk_client import claude_background_tracking_hooks


def started(session="provider-a", agent="a123456789abcdef0"):
    return {"hook_event_name": "SubagentStart", "session_id": session,
            "agent_id": agent, "agent_type": "general-purpose"}


def send(target, **fields):
    return {"hook_event_name": "PreToolUse", "session_id": "provider-a",
            "tool_name": "SendMessage", "tool_input": {"to": target, "message": "hello"}, **fields}


class ClaudeChatRoutingTests(unittest.TestCase):
    def assertDenied(self, result):
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_external_names_addresses_ids_and_discovery_are_denied_in_every_mode(self):
        policy = ClaudeChatRouting()
        policy.record_child(started())
        for mode in ["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk", "auto"]:
            for target in ["Review", "@Review", "sess_123", "uds:/tmp/peer.sock", "bridge:abc",
                           "did:abc", "a9999999999999999", "*", "main", None, {}, ""]:
                with self.subTest(mode=mode, target=target):
                    self.assertDenied(policy.before_tool(send(target, permission_mode=mode)))
            for name in ["ListAgents", "ListPeers"]:
                self.assertDenied(policy.before_tool({"tool_name": name, "permission_mode": mode}))

    def test_only_recorded_child_of_exact_session_and_child_reply_to_main_pass(self):
        policy = ClaudeChatRouting()
        child = started()["agent_id"]
        self.assertDenied(policy.before_tool(send(child)))
        policy.record_child(started())
        for mode in ["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"]:
            self.assertEqual(policy.before_tool(send(child, permission_mode=mode)), {})
            self.assertEqual(policy.before_tool(send("main", agent_id=child, permission_mode=mode)), {})
        self.assertDenied(policy.before_tool(send(child, session_id="provider-b")))
        self.assertDenied(policy.before_tool(send("main", agent_id="unknown-child")))
        self.assertDenied(ClaudeChatRouting().before_tool(send(child)))

    def test_same_run_sibling_and_legacy_child_messages_keep_native_permissions(self):
        policy = ClaudeChatRouting()
        child = started()["agent_id"]
        sibling = "aabcdef0123456789"
        policy.record_child(started())
        policy.record_child(started(agent=sibling))
        self.assertEqual(policy.before_tool(send(sibling, agent_id=child)), {})
        event = send(child, tool_input={"recipient": child, "type": "message", "content": "hello"})
        self.assertEqual(policy.before_tool(event), {})
        event["tool_input"]["to"] = "Wrong chat"
        self.assertDenied(policy.before_tool(event))

    def test_message_text_cannot_register_a_child_and_other_tools_are_unchanged(self):
        policy = ClaudeChatRouting()
        policy.record_child({**started(), "hook_event_name": "PreToolUse"})
        self.assertDenied(policy.before_tool(send(started()["agent_id"])))
        for tool in ["Agent", "Task", "Bash", "CronCreate", "AskUserQuestion", "mcp__custom__SendMessage"]:
            self.assertEqual(policy.before_tool({"tool_name": tool}), {})

    def test_sdk_wiring_shares_registry_only_with_one_options_instance(self):
        async def check():
            hooks = claude_background_tracking_hooks()
            await hooks["SubagentStart"][0].hooks[0](started(), None, {})
            guard = hooks["PreToolUse"][1].hooks[0]
            self.assertEqual(await guard(send(started()["agent_id"]), None, {}), {})
            other = claude_background_tracking_hooks()["PreToolUse"][1].hooks[0]
            self.assertDenied(await other(send(started()["agent_id"]), None, {}))
            self.assertDenied(await guard(send("Same display name"), None, {}))
        asyncio.run(check())

    def test_print_hook_real_subprocess_registers_and_fences_native_identity(self):
        with tempfile.TemporaryDirectory(prefix="claude routing ") as temporary:
            settings = print_routing_settings(Path(temporary) / "manifest.json")
            command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            def call(event):
                result = subprocess.run(shlex.split(command), input=json.dumps(event),
                                        text=True, capture_output=True, check=True)
                return json.loads(result.stdout)
            self.assertDenied(call(send("Wrong chat")))
            self.assertEqual(call(started()), {})
            self.assertEqual(call(send(started()["agent_id"])), {})
            self.assertEqual(call(send("main", agent_id=started()["agent_id"])), {})
            self.assertDenied(call(send(started()["agent_id"], session_id="provider-b")))
            self.assertDenied(call({"tool_name": "ListAgents"}))
            self.assertDenied(call(None))

    def test_sdk_reconnect_retains_children_but_other_chat_cannot_borrow_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "children"
            async def check():
                first = claude_background_tracking_hooks(registry)
                await first["SubagentStart"][0].hooks[0](started(), None, {})
                reconnected = claude_background_tracking_hooks(registry)
                guard = reconnected["PreToolUse"][1].hooks[0]
                self.assertEqual(await guard(send(started()["agent_id"]), None, {}), {})
                self.assertDenied(await guard(send(started()["agent_id"], session_id="forked-provider"), None, {}))
                other = claude_background_tracking_hooks(Path(temporary) / "other-chat")
                self.assertDenied(await other["PreToolUse"][1].hooks[0](send(started()["agent_id"]), None, {}))
            asyncio.run(check())

    def test_disk_registry_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir(mode=0o700)
            link = root / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            policy = ClaudeChatRouting(link)
            self.assertDenied(policy.before_tool(send(started()["agent_id"])))
            policy = ClaudeChatRouting(real)
            policy.record_child(started())
            marker = next(real.iterdir())
            marker.unlink()
            empty = root / "empty"
            empty.touch()
            marker.symlink_to(empty)
            self.assertDenied(policy.before_tool(send(started()["agent_id"])))
