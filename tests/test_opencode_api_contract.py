"""HTTP-level contract tests for the OpenCode backend.

These drive the real ASGI app the AgentsDock client talks to, over the same
endpoints, rather than calling runner internals: capability negotiation, the
runtime catalog, session creation, permission-mode plumbing, and a full turn
whose timeline the client would render. The fake CLI answers both `models` and
`run` with shapes captured from opencode 1.18.29.
"""

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import agent_server

SESSION = "ses_f86f2df67ffep5CKpMOXIyR44R"

FAKE_CLI = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]

if args and args[0] == "models":
    print("opencode/big-pickle")
    print("opencode/mimo-v2.5-free")
    sys.exit(0)

if args and args[0] == "run" and "--help" in args:
    sys.stderr.write("opencode run [message..]\\n --format --dir --session --fork --model --agent --file\\n")
    sys.exit(0)

if args and args[0] == "--version":
    print("1.18.29")
    sys.exit(0)

if args and args[0] == "auth":
    print("0 credentials")
    sys.exit(0)

record = os.environ.get("FAKE_CLI_RECORD")
if record:
    config_content = os.environ.get("OPENCODE_CONFIG_CONTENT")
    config = json.loads(config_content) if config_content else {}
    instruction_contents = []
    for instruction_path in config.get("instructions", []):
        if os.path.isfile(instruction_path):
            with open(instruction_path, encoding="utf-8") as instruction:
                instruction_contents.append({
                    "path": instruction_path,
                    "content": instruction.read(),
                })
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({"argv": args, "stdin": sys.stdin.read(),
                   "config_content": config_content,
                   "instruction_contents": instruction_contents,
                   "jobs_access": os.environ.get("AGENTSDOCK_PROVIDER_JOBS_ACCESS")},
                  handle)

SID = "%s"
events = [
    {"type": "tool_use", "sessionID": SID, "part": {
        "type": "tool", "tool": "glob", "callID": "call-1",
        "state": {"status": "completed", "input": {"pattern": "**/a.py"},
                  "output": "/tmp/a.py"}}},
    {"type": "step_finish", "sessionID": SID, "part": {
        "type": "step-finish", "reason": "tool-calls",
        "tokens": {"total": 10, "input": 9, "output": 1,
                   "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "cost": 0}},
    {"type": "text", "sessionID": SID, "part": {
        "type": "text", "text": "Found it."}},
    {"type": "step_finish", "sessionID": SID, "part": {
        "type": "step-finish", "reason": "stop",
        "tokens": {"total": 5, "input": 3, "output": 2,
                   "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "cost": 0}},
]
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(0)
''' % SESSION


class OpenCodeApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.cwd = root / "workspace"
        self.cwd.mkdir()
        self.script = root / "fake_opencode"
        self.script.write_text(FAKE_CLI, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC)
        os.environ["FAKE_CLI_RECORD"] = str(root / "record.json")

        self.previous = {
            "sessions": agent_server.STORE.sessions,
            "state_dir": agent_server.STATE_DIR,
            "sessions_file": agent_server.SESSIONS_FILE,
            "files_root": agent_server.FILES_ROOT,
            "code_diffs_root": agent_server.CODE_DIFFS_ROOT,
            "cross_chat_authority_root": agent_server.CROSS_CHAT_AUTHORITY_ROOT,
            "busy": agent_server.BUSY_SESSIONS,
            "active": agent_server.ACTIVE,
            "current": agent_server.CURRENT_TURNS,
            "queued": agent_server.QUEUED_TURNS,
            "run_now": agent_server.RUN_NOW_TURNS,
            "run_metadata": agent_server.RUN_METADATA,
            "stopped": agent_server.STOPPED_RUNS,
            "stop_requests": agent_server.STOP_REQUESTS,
            "diagnostics": dict(agent_server.RUNTIME_DIAGNOSTICS),
        }
        agent_server.STATE_DIR = root / "state"
        agent_server.SESSIONS_FILE = agent_server.STATE_DIR / "sessions.json"
        agent_server.FILES_ROOT = agent_server.STATE_DIR / "files"
        agent_server.CODE_DIFFS_ROOT = agent_server.STATE_DIR / "code_diffs"
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = (
            agent_server.STATE_DIR / "cross_chat_authority"
        )
        agent_server.STORE.sessions = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.ACTIVE = {}
        agent_server.CURRENT_TURNS = {}
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.RUN_METADATA = {}
        agent_server.STOPPED_RUNS = set()
        agent_server.STOP_REQUESTS = set()

        # probe_runtime resolves the executable itself, so the fake has to be
        # injected at the resolution seam both it and the catalog share.
        self.patches = [
            patch.object(
                agent_server, "opencode_executable_resolution",
                lambda: (str(self.script), str(self.script), (), ""),
            ),
            patch.object(agent_server, "AGENT_TOKEN", "test-token"),
        ]
        for item in self.patches:
            item.start()
        agent_server.RUNTIME_DIAGNOSTICS.pop(agent_server.BACKEND_OPENCODE, None)

        transport = httpx.ASGITransport(app=agent_server.app)
        self.client = httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"x-agentsdock-token": "test-token"},
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for item in self.patches:
            item.stop()
        agent_server.STORE.sessions = self.previous["sessions"]
        agent_server.STATE_DIR = self.previous["state_dir"]
        agent_server.SESSIONS_FILE = self.previous["sessions_file"]
        agent_server.FILES_ROOT = self.previous["files_root"]
        agent_server.CODE_DIFFS_ROOT = self.previous["code_diffs_root"]
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.previous[
            "cross_chat_authority_root"
        ]
        agent_server.BUSY_SESSIONS = self.previous["busy"]
        agent_server.ACTIVE = self.previous["active"]
        agent_server.CURRENT_TURNS = self.previous["current"]
        agent_server.QUEUED_TURNS = self.previous["queued"]
        agent_server.RUN_NOW_TURNS = self.previous["run_now"]
        agent_server.RUN_METADATA = self.previous["run_metadata"]
        agent_server.STOPPED_RUNS = self.previous["stopped"]
        agent_server.STOP_REQUESTS = self.previous["stop_requests"]
        agent_server.RUNTIME_DIAGNOSTICS.clear()
        agent_server.RUNTIME_DIAGNOSTICS.update(self.previous["diagnostics"])
        os.environ.pop("FAKE_CLI_RECORD", None)
        self.tempdir.cleanup()

    async def _create_session(self, **extra) -> str:
        response = await self.client.post("/api/sessions", json={
            "backend": "opencode", "cwd": str(self.cwd), **extra,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session"]["id"]

    async def _drain_turn_tasks(self, session_id: str) -> None:
        for _ in range(200):
            tasks = list(agent_server.SESSION_TURN_TASKS.get(session_id) or ())
            if not tasks:
                await asyncio.sleep(0.05)
                continue
            await asyncio.wait(tasks, timeout=60)
            if not agent_server.BUSY_SESSIONS:
                return
            await asyncio.sleep(0.05)

    def _events(self, session_id: str) -> list[dict]:
        path = agent_server.events_path(session_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    # --- capability negotiation -------------------------------------------

    async def test_health_advertises_the_opencode_capability(self) -> None:
        response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200, response.text)
        capability = response.json()["capabilities"]["opencode_backend"]
        self.assertTrue(capability["available"])
        self.assertEqual(capability["version"], 1)
        self.assertFalse(capability["required"])
        commands = response.json()["capabilities"]["local_provider_commands_v1"]
        self.assertIn("opencode", commands["supported_backends"])
        self.assertEqual(
            commands["client_capability_by_backend"]["opencode"],
            "opencode_provider_commands_v1",
        )
        self.assertEqual(
            commands["supported_kinds_by_backend"]["opencode"],
            ["skill"],
        )

    # --- runtime catalog ---------------------------------------------------

    async def test_catalog_exposes_models_and_permission_modes(self) -> None:
        response = await self.client.get("/api/runtime/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        backend = response.json()["backends"]["opencode"]
        self.assertEqual(
            backend["permission_modes"], ["default", "full_access", "plan"]
        )
        self.assertEqual(backend["default_permission_mode"], "default")
        values = [option["value"] for option in backend["models"]]
        self.assertIn("opencode/mimo-v2.5-free", values)
        # The empty value is the "server default" option every backend offers.
        self.assertIn("", values)

    async def test_catalog_reports_availability_separately(self) -> None:
        response = await self.client.get("/api/runtime/catalog")
        backend = response.json()["backends"]["opencode"]
        self.assertIn("available", backend)
        self.assertIn("diagnostic", backend)

    # --- session lifecycle -------------------------------------------------

    async def test_session_can_be_created_on_the_opencode_backend(self) -> None:
        session_id = await self._create_session()
        response = await self.client.get(f"/api/sessions/{session_id}")
        session = response.json()["session"]
        self.assertEqual(session["backend"], "opencode")
        self.assertEqual(session["opencode_permission_mode"], "default")

    async def test_permission_mode_round_trips_through_the_api(self) -> None:
        session_id = await self._create_session(opencode_permission_mode="plan")
        response = await self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(
            response.json()["session"]["opencode_permission_mode"], "plan"
        )
        patched = await self.client.patch(
            f"/api/sessions/{session_id}",
            json={"opencode_permission_mode": "full_access"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        response = await self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(
            response.json()["session"]["opencode_permission_mode"], "full_access"
        )

    async def test_permission_change_rotates_a_resumed_provider_session(self) -> None:
        session_id = await self._create_session(
            opencode_permission_mode="plan",
            provider_session_id=SESSION,
            import_history=False,
        )
        patched = await self.client.patch(
            f"/api/sessions/{session_id}",
            json={"opencode_permission_mode": "full_access"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        session = patched.json()["session"]
        self.assertIsNone(session["opencode_session_id"])
        self.assertIsNone(session["session_id"])
        resets = [
            event
            for event in self._events(session_id)
            if event["type"] == "provider_session_reset"
        ]
        self.assertTrue(resets)
        self.assertIn("tool set", resets[-1]["message"])

        response = await self.client.post(
            f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self._drain_turn_tasks(session_id)
        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertNotIn("-s", record["argv"])
        config = json.loads(record["config_content"])
        agent_name = record["argv"][record["argv"].index("--agent") + 1]
        self.assertEqual(
            config["agent"][agent_name]["permission"]["bash"], "allow"
        )

    async def test_invalid_opencode_workspaces_are_rejected_not_fallbacked(self) -> None:
        missing = Path(self.tempdir.name) / "does-not-exist"
        response = await self.client.post("/api/sessions", json={
            "backend": "opencode",
            "cwd": str(missing),
        })
        self.assertEqual(response.status_code, 400, response.text)

        ordinary_file = Path(self.tempdir.name) / "not-a-directory"
        ordinary_file.write_text("file", encoding="utf-8")
        response = await self.client.post("/api/sessions", json={
            "backend": "opencode",
            "cwd": str(ordinary_file),
        })
        self.assertEqual(response.status_code, 400, response.text)

        session_id = await self._create_session()
        patched = await self.client.patch(
            f"/api/sessions/{session_id}",
            json={"cwd": str(missing)},
        )
        self.assertEqual(patched.status_code, 400, patched.text)
        self.assertEqual(
            agent_server.STORE.sessions[session_id]["cwd"], str(self.cwd)
        )

        # Cover a corrupt/legacy persisted path at the actual turn boundary.
        agent_server.STORE.sessions[session_id]["cwd"] = str(missing)
        turn = await self.client.post(
            f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
        )
        self.assertEqual(turn.status_code, 409, turn.text)
        self.assertNotIn(session_id, agent_server.BUSY_SESSIONS)

    async def test_unknown_permission_mode_is_rejected(self) -> None:
        response = await self.client.post("/api/sessions", json={
            "backend": "opencode", "cwd": str(self.cwd),
            "opencode_permission_mode": "yolo",
        })
        self.assertEqual(response.status_code, 422, response.text)

    # --- a whole turn over HTTP -------------------------------------------

    async def test_turn_over_http_produces_the_timeline_a_client_renders(self) -> None:
        session_id = await self._create_session()
        response = await self.client.post(
            f"/api/sessions/{session_id}/turns", json={"prompt": "find a.py"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self._drain_turn_tasks(session_id)

        events = self._events(session_id)
        types = [event["type"] for event in events]
        self.assertIn("process_started", types)
        self.assertIn("tool_started", types)
        self.assertIn("tool_finished", types)
        self.assertIn("assistant_text", types)
        self.assertNotIn("error", types)

        terminal = [e for e in events if e["type"] == "turn_finished"][-1]
        self.assertFalse(terminal["is_error"])
        self.assertIn("Found it.", terminal["result_text"])
        self.assertEqual(terminal["backend"], "opencode")
        # Usage is summed across both steps, not taken from the last one.
        self.assertEqual(terminal["input_tokens"], 12)
        self.assertEqual(terminal["output_tokens"], 3)

    async def test_turn_binds_the_resumable_session_visible_to_the_client(self) -> None:
        session_id = await self._create_session()
        await self.client.post(
            f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
        )
        await self._drain_turn_tasks(session_id)
        response = await self.client.get(f"/api/sessions/{session_id}")
        session = response.json()["session"]
        self.assertEqual(session["opencode_session_id"], SESSION)

    async def test_permission_mode_reaches_the_process_environment(self) -> None:
        session_id = await self._create_session(opencode_permission_mode="plan")
        await self.client.post(
            f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
        )
        await self._drain_turn_tasks(session_id)
        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
        config = json.loads(record["config_content"])
        agent_name = record["argv"][record["argv"].index("--agent") + 1]
        permissions = config["agent"][agent_name]["permission"]
        self.assertEqual(permissions["bash"], "deny")
        self.assertEqual(permissions["task"], "deny")
        # And the workspace the session was created with reached --dir.
        self.assertEqual(
            record["argv"][record["argv"].index("--dir") + 1], str(self.cwd)
        )

    async def test_attachment_uses_native_file_without_permission_grant(self) -> None:
        session_id = await self._create_session(
            opencode_permission_mode="plan"
        )
        uploaded = await self.client.post(
            f"/api/sessions/{session_id}/files",
            files={"file": ("note.txt", b"ATTACHMENT-MARKER", "text/plain")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        file_record = uploaded.json()["file"]

        response = await self.client.post(
            f"/api/sessions/{session_id}/turns",
            json={"prompt": "read the attachment", "file_ids": [file_record["id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self._drain_turn_tasks(session_id)
        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
        config = json.loads(record["config_content"])
        agent_name = record["argv"][record["argv"].index("--agent") + 1]
        permissions = config["agent"][agent_name]["permission"]
        self.assertNotIn("external_directory", permissions)
        self.assertEqual(permissions["bash"], "deny")
        self.assertEqual(permissions["write"], "deny")
        self.assertIn(file_record["path"], record["stdin"])
        self.assertEqual(
            record["argv"][record["argv"].index("--file") + 1],
            file_record["path"],
        )
        process_started = [
            event for event in self._events(session_id)
            if event["type"] == "process_started"
        ][-1]
        self.assertNotIn(file_record["path"], process_started["argv"])
        self.assertEqual(
            process_started["argv"][
                process_started["argv"].index("--file") + 1
            ],
            "<file>",
        )

    async def test_opencode_attachment_count_is_bounded_before_acceptance(self) -> None:
        session_id = await self._create_session()
        file_ids = []
        for name in ("one.txt", "two.txt"):
            uploaded = await self.client.post(
                f"/api/sessions/{session_id}/files",
                files={"file": (name, b"x", "text/plain")},
            )
            file_ids.append(uploaded.json()["file"]["id"])
        with patch.object(agent_server, "MAX_OPENCODE_ATTACHMENT_FILES", 1):
            response = await self.client.post(
                f"/api/sessions/{session_id}/turns",
                json={"prompt": "read", "file_ids": file_ids},
            )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertNotIn(session_id, agent_server.BUSY_SESSIONS)
        self.assertFalse([
            event for event in self._events(session_id)
            if event["type"] == "turn_started"
        ])

    async def test_opencode_attachment_sizes_are_bounded_before_acceptance(self) -> None:
        for limit_kind in ("per_file", "aggregate"):
            with self.subTest(limit_kind=limit_kind):
                session_id = await self._create_session()
                file_ids = []
                for name in ("one.txt", "two.txt"):
                    uploaded = await self.client.post(
                        f"/api/sessions/{session_id}/files",
                        files={"file": (name, b"abc", "text/plain")},
                    )
                    file_ids.append(uploaded.json()["file"]["id"])
                per_file = 2 if limit_kind == "per_file" else 3
                aggregate = 100 if limit_kind == "per_file" else 5
                with patch.object(
                    agent_server, "MAX_OPENCODE_ATTACHMENT_FILE_BYTES", per_file
                ), patch.object(
                    agent_server, "MAX_OPENCODE_ATTACHMENT_TOTAL_BYTES", aggregate
                ):
                    response = await self.client.post(
                        f"/api/sessions/{session_id}/turns",
                        json={"prompt": "read", "file_ids": file_ids},
                    )
                self.assertEqual(response.status_code, 413, response.text)
                self.assertNotIn(session_id, agent_server.BUSY_SESSIONS)
                self.assertFalse([
                    event for event in self._events(session_id)
                    if event["type"] == "turn_started"
                ])

    async def test_selected_skill_keeps_body_and_authority_private_but_includes_attachment(self) -> None:
        (self.cwd / ".git").mkdir()
        skill_dir = self.cwd / ".opencode" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        skill_body = "PRIVATE-SKILL-BODY\nUse resources/checklist.md relative to this skill."
        (skill_dir / "SKILL.md").write_text(
            "---\nname: review\n"
            "description: Metadata /private/frontmatter-only\n"
            f"---\n{skill_body}\n",
            encoding="utf-8",
        )
        home = Path(self.tempdir.name) / "isolated-home"
        home.mkdir()
        session_id = await self._create_session(opencode_permission_mode="plan")
        uploaded = await self.client.post(
            f"/api/sessions/{session_id}/files",
            files={"file": ("note.txt", b"ATTACHMENT", "text/plain")},
        )
        file_record = uploaded.json()["file"]
        with patch.dict(
            os.environ,
            {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config")},
        ):
            discovered = await self.client.get(
                f"/api/sessions/{session_id}/provider-commands"
            )
            self.assertEqual(discovered.status_code, 200, discovered.text)
            command_snapshot = discovered.json()
            command = command_snapshot["commands"][0]
            with patch.object(
                agent_server,
                "cross_chat_provider_authority_block",
                return_value="PRIVATE-RUNTIME-AUTHORITY",
            ):
                response = await self.client.post(
                    f"/api/sessions/{session_id}/turns",
                    json={
                        "prompt": "/review focus on auth",
                        "file_ids": [file_record["id"]],
                        "skill_selection": {
                            "id": command["id"],
                            "revision": command_snapshot["revision"],
                        },
                        "client_capabilities": [
                            "opencode_provider_commands_v1"
                        ],
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                await self._drain_turn_tasks(session_id)

        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertIn("focus on auth", record["stdin"])
        self.assertIn(file_record["path"], record["stdin"])
        self.assertNotIn(skill_body, record["stdin"])
        self.assertNotIn("PRIVATE-RUNTIME-AUTHORITY", record["stdin"])
        self.assertNotIn("--command", record["argv"])
        self.assertEqual(len(record["instruction_contents"]), 1)
        instruction = record["instruction_contents"][0]
        self.assertIn(skill_body, instruction["content"])
        self.assertIn(str(skill_dir), instruction["content"])
        self.assertIn("PRIVATE-RUNTIME-AUTHORITY", instruction["content"])
        self.assertNotIn("frontmatter-only", instruction["content"])
        self.assertNotIn(session_id, instruction["path"])
        config = json.loads(record["config_content"])
        agent_name = record["argv"][record["argv"].index("--agent") + 1]
        permissions = config["agent"][agent_name]["permission"]
        self.assertEqual(permissions["skill"], "deny")
        self.assertEqual(permissions["task"], "deny")
        self.assertEqual(permissions["bash"], "deny")
        self.assertNotIn("read", permissions)
        self.assertNotIn("external_directory", permissions)
        self.assertEqual(
            record["argv"][record["argv"].index("--file") + 1],
            file_record["path"],
        )
        timeline = json.dumps(self._events(session_id))
        self.assertNotIn(skill_body, timeline)
        self.assertNotIn(str(skill_dir), timeline)

    async def test_corrupt_attachment_metadata_aborts_before_prompt_or_grant(self) -> None:
        session_id = await self._create_session(opencode_permission_mode="plan")
        uploaded = await self.client.post(
            f"/api/sessions/{session_id}/files",
            files={"file": ("note.txt", b"SAFE", "text/plain")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        file_record = uploaded.json()["file"]
        corrupt = dict(agent_server.load_file_meta(file_record["id"]))
        outside = Path(self.tempdir.name) / "outside" / "private.txt"
        outside.parent.mkdir()
        outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
        corrupt["path"] = str(outside)
        record_path = Path(os.environ["FAKE_CLI_RECORD"])
        with patch.object(agent_server, "load_file_meta", return_value=corrupt):
            response = await self.client.post(
                f"/api/sessions/{session_id}/turns",
                json={"prompt": "read it", "file_ids": [file_record["id"]]},
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("metadata is invalid", response.text)
        self.assertFalse(record_path.exists())
        self.assertNotIn(session_id, agent_server.BUSY_SESSIONS)

    async def test_shared_resumed_provider_is_rejected_across_wrappers(self) -> None:
        first = await self._create_session(
            provider_session_id=SESSION,
            import_history=False,
        )
        second_cwd = Path(self.tempdir.name) / "second-workspace"
        second_cwd.mkdir()
        second = await self._create_session(
            provider_session_id=SESSION,
            import_history=False,
            cwd=str(second_cwd),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_runner(
            session_id, run_id, prompt, sess, manifest_path, **kwargs
        ):
            started.set()
            await release.wait()
            await agent_server.finalize_owned_turn_finished(
                session_id,
                run_id,
                stopped=False,
                payload={
                    "run_id": run_id,
                    "backend": agent_server.BACKEND_OPENCODE,
                    "exit_code": 0,
                    "result_text": "done",
                    "is_error": False,
                },
            )

        try:
            with patch.object(agent_server, "run_opencode", blocking_runner):
                response = await self.client.post(
                    f"/api/sessions/{first}/turns",
                    json={"prompt": "hold the provider"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                await asyncio.wait_for(started.wait(), timeout=2)
                conflict = await self.client.post(
                    f"/api/sessions/{second}/turns",
                    json={"prompt": "race the same provider"},
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn("OpenCode provider session", conflict.text)
                self.assertNotIn(second, agent_server.BUSY_SESSIONS)
                queued = await agent_server.enqueue_turn(
                    second,
                    agent_server.TurnRequest(prompt="queued behind provider lock"),
                    agent_server.STORE.sessions[second],
                )
                queued_id = queued["queued_id"]
                with patch.object(agent_server, "schedule_queued_turn_retry"):
                    await agent_server.start_next_queued_turn(second)
                self.assertEqual(
                    agent_server.QUEUED_TURNS[second][0]["queued_id"],
                    queued_id,
                )
                permission_change = await self.client.patch(
                    f"/api/sessions/{first}",
                    json={"opencode_permission_mode": "full_access"},
                )
                self.assertEqual(
                    permission_change.status_code,
                    409,
                    permission_change.text,
                )
                release.set()
                await self._drain_turn_tasks(first)
                await agent_server.start_next_queued_turn(second)
                await self._drain_turn_tasks(second)
                self.assertFalse(agent_server.QUEUED_TURNS.get(second))
        finally:
            release.set()

    async def test_scoped_runtime_context_and_environment_reach_the_cli(self) -> None:
        session_id = await self._create_session()
        from unittest.mock import AsyncMock
        with patch.object(agent_server, "provider_authority_runtime_env", AsyncMock(
            return_value={"AGENTSDOCK_PROVIDER_JOBS_ACCESS": "none"},
        )), patch.object(agent_server, "cross_chat_provider_authority_block", return_value="READINESS-RUNTIME-CONTEXT"):
            await self.client.post(
                f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
            )
            await self._drain_turn_tasks(session_id)
        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["jobs_access"], "none")
        self.assertIn("READINESS-RUNTIME-CONTEXT", record["stdin"])
        self.assertNotIn("READINESS-RUNTIME-CONTEXT", " ".join(record["argv"]))

    async def test_permission_override_preserves_operator_inline_configuration(self) -> None:
        session_id = await self._create_session(opencode_permission_mode="plan")
        config = {"model": "custom/test", "permission": {"webfetch": "deny"}}
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}):
            await self.client.post(
                f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
            )
            await self._drain_turn_tasks(session_id)
        with open(os.environ["FAKE_CLI_RECORD"], encoding="utf-8") as handle:
            record = json.load(handle)
            actual = json.loads(record["config_content"])
        self.assertEqual(actual["model"], "custom/test")
        self.assertEqual(actual["permission"]["webfetch"], "deny")
        agent_name = record["argv"][record["argv"].index("--agent") + 1]
        self.assertEqual(
            actual["agent"][agent_name]["permission"]["bash"], "deny"
        )

    async def test_invalid_inline_config_fails_cleanly_without_exposing_content(self) -> None:
        session_id = await self._create_session(opencode_permission_mode="plan")
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "MALFORMED-PRIVATE-TEST"}):
            await self.client.post(
                f"/api/sessions/{session_id}/turns", json={"prompt": "hi"}
            )
            await self._drain_turn_tasks(session_id)
        events = self._events(session_id)
        self.assertTrue([e for e in events if e["type"] == "turn_finished"][-1]["is_error"])
        self.assertNotIn(session_id, agent_server.BUSY_SESSIONS)
        self.assertNotIn("MALFORMED-PRIVATE-TEST", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
