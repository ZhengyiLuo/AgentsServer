"""Isolated real HTTP server + optional real Gemini CLI (loopback API only)."""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import httpx
from tests.test_gemini_native_contract import API


class DefaultAvailability(unittest.TestCase):
    def test_no_flag_required_but_missing_runtime_and_api_are_not_ready(self):
        """A fresh ordinary server exposes setup without implying readiness."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
            token = secrets.token_hex(32)
            env = {"PATH": os.environ["PATH"], "HOME": str(root),
                "PYTHONDONTWRITEBYTECODE": "1", "AGENTSDOCK_STATE_DIR": str(root / "state"),
                "AGENTSDOCK_AGENT_TOKEN": token, "AGENTSDOCK_AGENT_CWD": str(root),
                "AGENTSDOCK_GEMINI_BIN": str(root / "not-installed"),
                "AGENTSDOCK_CODEX_TRANSPORT": "exec", "AGENTSDOCK_CLAUDE_TRANSPORT": "print"}
            self.assertNotIn("AGENTSDOCK_GEMINI_PREVIEW", env)
            with (root / "server.log").open("w+") as log:
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "agent_server:app",
                    "--host", "127.0.0.1", "--port", str(port)],
                    cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log)
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}",
                            headers={"X-AgentsDock-Token": token}, timeout=30, trust_env=False) as client:
                        for _ in range(150):
                            if process.poll() is not None:
                                self.fail("isolated server exited before health was ready")
                            try:
                                response = client.get("/api/health")
                                if response.status_code == 200: break
                            except httpx.HTTPError: pass
                            time.sleep(.1)
                        else: self.fail("isolated server did not start")
                        caps = response.json()["capabilities"]
                        self.assertTrue(caps["gemini_backend"]["available"])
                        self.assertIn("gemini", caps["provider_connections_v1"]["backends"])
                        catalog = client.get("/api/runtime/catalog")
                        self.assertEqual(catalog.status_code, 200)
                        gemini = catalog.json()["backends"]["gemini"]
                        self.assertFalse(gemini["available"])
                        self.assertEqual(gemini["diagnostic"]["status"], "missing")
                        self.assertFalse(gemini["custom_provider"]["configured"])
                        created = client.post("/api/sessions", json={"backend": "gemini", "cwd": str(root)})
                        self.assertEqual(created.status_code, 200, created.text)
                        sid = created.json()["session"]["id"]
                        rejected = client.post(f"/api/sessions/{sid}/turns", json={
                            "prompt": "Must not invoke a provider", "client_capabilities": ["gemini_acp_v1"]})
                        self.assertEqual(rejected.status_code, 409, rejected.text)
                finally:
                    process.terminate()
                    try: process.wait(timeout=15)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN"), "optional real server/CLI contract")
class ServerContract(unittest.TestCase):
    def test_api_chat_resume_forget_reconnect_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); project = root / "project"; project.mkdir()
            skill = root / '.gemini/skills/fixture-skill/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: fixture-skill\ndescription: Harmless HTTP acceptance skill.\n---\nReply OK only.')
            state = root / "state"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
            fixture = ThreadingHTTPServer(("127.0.0.1", 0), API)
            thread = threading.Thread(target=fixture.serve_forever, daemon=True); thread.start()
            token = secrets.token_hex(32)
            env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "en_US.UTF-8",
                "AGENTSDOCK_STATE_DIR": str(state), "AGENTSDOCK_AGENT_TOKEN": token,
                "AGENTSDOCK_AGENT_PORT": str(port), "AGENTSDOCK_AGENT_CWD": str(project),
                "AGENTSDOCK_GEMINI_BIN": os.environ["AGENTSDOCK_TEST_GEMINI_BIN"],
                "AGENTSDOCK_CODEX_TRANSPORT": "exec", "AGENTSDOCK_CLAUDE_TRANSPORT": "print"}
            with (root / "server.log").open("w+") as log:
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "agent_server:app", "--host", "127.0.0.1", "--port", str(port)],
                    cwd=Path(__file__).resolve().parents[1], env=env, stdout=log, stderr=log)
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-AgentsDock-Token": token}, timeout=30, trust_env=False) as client:
                        for _ in range(100):
                            if process.poll() is not None:
                                log.seek(0); self.fail(log.read()[-4000:].replace(token, "[token]"))
                            try:
                                if client.get("/api/health").status_code == 200: break
                            except httpx.HTTPError: pass
                            time.sleep(.1)
                        else:
                            log.seek(0); self.fail("isolated test server did not start: " + log.read()[-3000:].replace(token, "[token]"))
                        capability = client.get("/api/health").json()["capabilities"]["gemini_backend"]
                        self.assertTrue(capability["available"])
                        features = capability["features"]
                        for enabled in ("scheduled_jobs", "interactive_shares", "approvals", "skills_picker", "permission_mode_control"):
                            self.assertIs(features[enabled], True)
                        for unsupported in ("fork", "native_goals", "native_side_questions", "managed_subagents",
                                            "history_reconciliation", "native_compaction_control"):
                            self.assertIs(features[unsupported], False)
                        endpoint = {"base_url": f"http://127.0.0.1:{fixture.server_port}", "api_key": "local-fixture-key",
                            "model": "gemini-2.5-flash", "protocol": "gemini", "auth_header": "x-goog-api-key", "expected_revision": 0}
                        response = client.put("/api/admin/provider-connections/gemini", json=endpoint)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(response.json()["ok"], response.text)
                        response = client.post("/api/sessions", json={"backend": "gemini", "cwd": str(project), "provider_connection": "custom"})
                        self.assertEqual(response.status_code, 200, response.text)
                        sid = response.json()["session"]["id"]
                        route = f"/api/sessions/{sid}"
                        prompt = {"prompt": "Reply OK only, no tools.", "client_capabilities": ["gemini_acp_v1"]}
                        self.assertEqual(client.post(route + "/turns", json={"prompt": "old client"}).status_code, 409)
                        def completed(count):
                            for _ in range(150):
                                files = list(state.rglob("events.jsonl"))
                                events = [json.loads(line) for file in files for line in file.read_text().splitlines()]
                                turns = [event for event in events if event.get("type") == "turn_finished"]
                                if len(turns) >= count:
                                    self.assertFalse(turns[-1].get("is_error"), [e for e in events if e.get("type") == "error"])
                                    return events
                                time.sleep(.1)
                            self.fail("Gemini turn did not finish")
                        for count in (1, 2):
                            response = client.post(route + "/turns", json=prompt)
                            self.assertEqual(response.status_code, 200, response.text)
                            events = completed(count)
                        self.assertEqual(len([e for e in events if e.get("type") == "turn_started"]), 2)
                        self.assertEqual(len([e for e in events if e.get("type") == "assistant_text"]), 2)
                        self.assertEqual([e["text"] for e in events if e.get("type") == "assistant_text"], ["Gemini local contract OK"] * 2)
                        commands = client.get(route + '/provider-commands').json()
                        self.assertTrue(commands['support']['available'], commands)
                        self.assertEqual(commands['commands'][0]['name'], 'fixture-skill')
                        selected = {'id': commands['commands'][0]['id'], 'revision': commands['revision']}
                        skill_prompt = {**prompt, 'prompt': '/fixture-skill FIXTURE_SKILL', 'skill_selection': selected}
                        original = skill.read_text(); skill.write_text(original + '\nChanged')
                        self.assertEqual(client.post(route + '/turns', json=skill_prompt).status_code, 409)
                        skill.write_text(original)
                        response = client.post(route + '/turns', json=skill_prompt)
                        self.assertEqual(response.status_code, 200, response.text)
                        for _ in range(150):
                            pending = client.get(route + '/gemini/runtime').json()['pending_interactions']
                            if pending: break
                            time.sleep(.1)
                        else: self.fail('Native skill activation did not reach the server approval boundary')
                        self.assertEqual(pending[0]['params']['availableDecisions'], ['accept', 'decline'])
                        response = client.post(route + '/gemini/interactions/' + pending[0]['id'] + '/resolve', json={'response': {'decision': 'accept'}})
                        self.assertEqual(response.status_code, 200, response.text)
                        completed(3)
                        sessions = json.loads((state / "sessions.json").read_text())
                        session = sessions[sid] if isinstance(sessions, dict) and sid in sessions else next(s for s in sessions if s["id"] == sid)
                        native_id = session["gemini_session_id"]
                        self.assertTrue(native_id)
                        response = client.request("DELETE", "/api/admin/provider-connections/gemini", json={"expected_revision": 1})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(client.post(route + "/turns", json=prompt).status_code, 409)
                        endpoint["expected_revision"] = 2
                        self.assertTrue(client.put("/api/admin/provider-connections/gemini", json=endpoint).json()["ok"])
                        self.assertEqual(client.post(route + "/turns", json=prompt).status_code, 200)
                        events = completed(4)
                        self.assertEqual(len([e for e in events if e.get('type') == 'turn_started']), 4)
                        self.assertEqual(len([e for e in events if e.get('type') == 'assistant_text']), 4)
                        self.assertEqual(client.post(route + "/turns", json=prompt).status_code, 200)
                        events = completed(5)
                        self.assertEqual(len([e for e in events if e.get('type') == 'turn_started']), 5)
                        self.assertEqual(len([e for e in events if e.get('type') == 'assistant_text']), 5)
                        histories = list((state / "admin/provider-connections").rglob("session-*"))
                        self.assertTrue(histories)
                        self.assertEqual(client.delete(route).status_code, 200)
                        self.assertTrue(all(path.exists() for path in histories), "Delete must preserve provider-native history")
                finally:
                    process.terminate()
                    try: process.wait(timeout=15)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                    fixture.shutdown(); fixture.server_close(); thread.join(timeout=2)
