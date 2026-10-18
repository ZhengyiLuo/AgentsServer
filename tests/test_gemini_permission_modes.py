import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from gemini_acp_client import GeminiProtocolError, normalize_update
from gemini_permissions import MODES, effective_mode, startup_mode, available_modes, apply_mode
from tests import test_gemini_lifecycle as lifecycle


class ModeContract(unittest.IsolatedAsyncioTestCase):
    async def test_native_ids_and_fail_closed_handshake(self):
        self.assertEqual(effective_mode({}), "default")
        with self.assertRaises(GeminiProtocolError): effective_mode({"gemini_permission_mode": "full_access"})
        self.assertEqual([startup_mode(m) for m in MODES], ["default", "auto_edit", "yolo", "default"])
        client = SimpleNamespace(session_id="owned", request=AsyncMock())
        for mode in MODES:
            await apply_mode(client, {"modes": {"availableModes": [{"id": m} for m in MODES]}}, mode)
            client.request.assert_awaited_with("session/set_mode", {"sessionId": "owned", "modeId": mode})
        client.request.reset_mock()
        with self.assertRaisesRegex(GeminiProtocolError, "not enabled"):
            await apply_mode(client, {"modes": {"availableModes": [{"id": "default"}]}}, "plan")
        client.request.assert_not_awaited()
        for value in ({}, {"modes": []}, {"modes": {"availableModes": [None]}}):
            with self.assertRaises(GeminiProtocolError): available_modes(value)
        self.assertEqual(normalize_update({"sessionId": "s", "update": {
            "sessionUpdate": "current_mode_update", "currentModeId": "plan"}}, "s"),
            {"type": "permission_mode", "mode": "plan"})


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN"), "optional real CLI permissions")
class NativeModes(unittest.TestCase):
    # Reuse the disposable real server/CLI fixture, not its unrelated test cases.
    for _name in ("setUp", "start_server", "ready", "cleanup", "wait", "new", "events", "send", "finished"):
        locals()[_name] = getattr(lifecycle.Lifecycle, _name)

    def trust(self):
        path = self.root / ".gemini/trustedFolders.json"; path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({str(self.project): "TRUST_FOLDER"}))

    def settings(self, value):
        path = self.root / ".gemini/settings.json"; path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def mode(self, sid, mode):
        response = self.client.patch(f"/api/sessions/{sid}", json={"gemini_permission_mode": mode})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["session"]["gemini_permission_mode"], mode)

    def test_default_asks_and_mode_change_is_next_turn_with_same_native_identity(self):
        self.trust(); sid = self.new(); route = f"/api/sessions/{sid}"
        self.assertEqual(self.client.get(route).json()["session"]["gemini_permission_mode"], "default")
        self.assertEqual(self.client.patch(route, json={"gemini_permission_mode": "full_access"}).status_code, 422)
        self.fixture_type.mode = "tool"; self.send(sid)
        pending = self.wait(lambda: self.client.get(route + "/gemini/runtime").json()["pending_interactions"])
        native_id = self.client.get(route).json()["session"]["gemini_session_id"]
        self.mode(sid, "autoEdit")
        current = self.client.get(route + "/gemini/runtime").json()
        self.assertEqual(current["active_permission_mode"], "default")
        self.assertEqual(current["permission_mode"], "autoEdit")
        self.assertEqual(current["pending_interactions"], pending)
        self.assertFalse((self.project / "must-not-exist.txt").exists())
        self.assertEqual(self.client.post(route + "/gemini/interactions/" + pending[0]["id"] + "/resolve", json={"response": {"decision": "decline"}}).status_code, 200)
        self.finished(sid)
        self.fixture_type.mode = "normal"; self.send(sid)
        self.assertFalse(self.finished(sid, 2)[-1]["is_error"], self.events(sid))
        self.assertEqual(self.client.get(route).json()["session"]["gemini_session_id"], native_id)
        self.assertEqual([e["mode"] for e in self.events(sid) if e["type"] == "gemini_permission_mode"][-1], "autoEdit")

    def test_auto_edit_and_yolo_actually_write_without_approval(self):
        self.trust(); self.fixture_type.mode = "tool"
        for mode in ("autoEdit", "yolo"):
            with self.subTest(mode=mode):
                sid = self.new(); self.mode(sid, mode); self.send(sid)
                self.assertFalse(self.finished(sid)[-1]["is_error"], self.events(sid))
                self.assertEqual((self.project / "must-not-exist.txt").read_text(), "test")
                self.assertFalse(any(e["type"] == "gemini_interaction_requested" for e in self.events(sid)))
                (self.project / "must-not-exist.txt").unlink()

    def test_plan_blocks_project_write_and_disabled_plan_never_sends_prompt(self):
        self.trust(); self.fixture_type.mode = "tool"
        sid = self.new(); self.mode(sid, "plan"); self.send(sid)
        self.finished(sid)
        self.assertFalse((self.project / "must-not-exist.txt").exists())
        self.assertTrue(any(e.get("mode") == "plan" for e in self.events(sid)))
        self.assertFalse(any("[MODE_UPDATE]" in e.get("text", "") for e in self.events(sid) if e["type"] in {"assistant_text", "reasoning_summary"}))
        self.settings({"general": {"plan": {"enabled": False}}})
        sid = self.new(); self.mode(sid, "plan"); before = len(self.fixture_type.bodies); self.send(sid)
        self.assertTrue(self.finished(sid)[-1]["is_error"])
        self.assertEqual(len(self.fixture_type.bodies), before)
        self.assertTrue(any("not enabled" in str(e) for e in self.events(sid)))
        self.assertNotIn("plan", self.client.get(f"/api/sessions/{sid}/gemini/runtime").json()["permission_modes"])

    def test_auto_edit_still_asks_for_shell_but_yolo_runs_bounded_command(self):
        self.trust()
        marker = self.project / "shell-ran.txt"
        (self.project / "owned-tool.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('done')\n")
        self.fixture_type.mode = "shell"
        sid = self.new(); self.mode(sid, "autoEdit"); self.send(sid)
        route = f"/api/sessions/{sid}"
        pending = self.wait(lambda: self.client.get(route + "/gemini/runtime").json()["pending_interactions"])
        self.assertFalse(marker.exists())
        self.client.post(route + "/gemini/interactions/" + pending[0]["id"] + "/resolve", json={"response": {"decision": "decline"}}).raise_for_status()
        self.finished(sid); self.assertFalse(marker.exists())
        sid = self.new(); self.mode(sid, "yolo"); self.send(sid)
        self.assertFalse(self.finished(sid)[-1]["is_error"], self.events(sid))
        self.assertEqual(marker.read_text(), "done")

    def test_native_admin_disable_yolo_and_untrusted_auto_edit_are_not_bypassed(self):
        sid = self.new(); self.mode(sid, "autoEdit"); before = len(self.fixture_type.bodies); self.send(sid)
        self.assertTrue(self.finished(sid)[-1]["is_error"])
        self.assertEqual(len(self.fixture_type.bodies), before)
        self.trust()
        path = self.settings({"security": {"disableYoloMode": True}}); original = path.read_bytes()
        sid = self.new(); self.mode(sid, "yolo"); self.send(sid)
        self.assertTrue(self.finished(sid)[-1]["is_error"])
        self.assertEqual(len(self.fixture_type.bodies), before)
        self.assertEqual(path.read_bytes(), original)

    def test_native_deny_policy_survives_auto_edit_and_yolo(self):
        self.trust(); policy = self.root / ".gemini/policies/deny-write.toml"
        policy.parent.mkdir(parents=True)
        policy.write_text('[[rule]]\ntoolName = "write_file"\ndecision = "deny"\npriority = 900\n')
        self.fixture_type.mode = "tool"
        for mode in ("autoEdit", "yolo"):
            before = len(self.fixture_type.bodies)
            sid = self.new(); self.mode(sid, mode); self.send(sid); self.finished(sid)
            self.assertFalse((self.project / "must-not-exist.txt").exists())
            bodies = self.fixture_type.bodies[before:]
            # Native deny removes this tool from the registry rather than
            # producing an ACP tool_finished event. Our fixture still attempts
            # the call; Gemini returns a tool error without executing it.
            self.assertFalse(any(f.get("name") == "write_file" for b in bodies for t in b.get("tools", []) for f in t.get("functionDeclarations", [])))
            responses = [p["functionResponse"] for b in bodies for c in b.get("contents", []) for p in c.get("parts", []) if "functionResponse" in p]
            self.assertTrue(any(r.get("name") == "write_file" and "error" in str(r).lower() for r in responses), responses)
