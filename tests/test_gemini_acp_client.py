"""Protocol fixtures use real pipes, not real credentials or user history."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gemini_acp_client import GeminiACPClient, GeminiProtocolError, normalize_update, session_models, safe_rpc_error
from gemini_runtime import build_environment, GeminiConfigurationError, native_credentials_present, probe
from gemini_history import fingerprint

SESSION = "12345678-1234-1234-1234-123456789abc"

FIXTURE = '''
import json, sys
session = "12345678-1234-1234-1234-123456789abc"
def emit(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)
def update(kind, text):
    emit({"method":"session/update", "params":{"sessionId":session,"update":{
        "sessionUpdate":kind,"content":{"type":"text","text":text}}}})
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    key = request.get("id")
    if method == "initialize":
        result = {"protocolVersion":1, "agentCapabilities":{"loadSession":True}}
    elif method == "session/new":
        result = {"sessionId":session}
    elif method == "session/load":
        update("user_message_chunk", "old user text")
        update("agent_message_chunk", "old answer")
        result = {}
    elif method == "session/set_mode":
        update("agent_message_chunk", "[MODE_UPDATE] " + request["params"]["modeId"])
        result = {}
    elif method == "session/prompt":
        if request["params"]["prompt"][0].get("text") == "literal mode text":
            update("agent_message_chunk", "[MODE_UPDATE] plan")
            emit({"id":key,"result":{"stopReason":"end_turn"}})
            continue
        update("agent_thought_chunk", "native thought")
        emit({"id":"permission-1","method":"session/request_permission", "params":{
            "sessionId":session,"options":[{"optionId":"once","kind":"allow_once"}]}})
        answer = json.loads(sys.stdin.readline())
        update("agent_message_chunk", json.dumps(answer["result"]))
        result = {"stopReason":"end_turn"}
    elif method == "session/cancel":
        continue
    else:
        result = {}
    emit({"id":key,"result":result})
'''


class GeminiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_ack_is_runtime_state_but_identical_model_text_is_kept(self):
        await self.client.start([sys.executable, str(self.script)], cwd=str(self.root), env=dict(os.environ))
        await self.client.open_session(cwd=str(self.root))
        await self.client.request("session/set_mode", {"sessionId": SESSION, "modeId": "plan"})
        self.assertEqual(self.events, [{"type": "permission_mode", "mode": "plan"}])
        await self.client.prompt([{"type": "text", "text": "literal mode text"}])
        self.assertEqual(self.events[-1], {"type": "text", "text": "[MODE_UPDATE] plan"})

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / "fixture.py"
        self.script.write_text(FIXTURE)
        self.events = []
        async def update(value):
            self.events.append(value)
        self.client = GeminiACPClient(on_update=update)

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def start(self):
        return await self.client.start([sys.executable, str(self.script)], cwd=str(self.root), env=dict(os.environ))

    async def test_new_prompt_denies_permission_without_explicit_handler(self):
        await self.start()
        await self.client.open_session(cwd=str(self.root))
        result = await self.client.prompt([{"type":"text", "text":"hello"}])
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(self.events[0], {"type":"reasoning", "text":"native thought"})
        self.assertEqual(json.loads(self.events[1]["text"]), {"outcome":{"outcome":"cancelled"}})

    async def test_load_discards_historical_replay_and_user_reinjections(self):
        await self.start()
        replay = [fingerprint({"sessionUpdate": kind, "content": {"type": "text", "text": text}})
                  for kind, text in [("user_message_chunk", "old user text"), ("agent_message_chunk", "old answer")]]
        await self.client.open_session(cwd=str(self.root), session_id=SESSION, replay=replay)
        self.assertEqual(self.events, [])
        self.assertEqual(self.client.session_id, SESSION)
        await self.client.prompt([{"type":"text","text":"follow up"}])
        self.assertEqual(len(self.events), 2)

    async def test_authenticate_cannot_mutate_shared_login(self):
        await self.start()
        with self.assertRaisesRegex(GeminiProtocolError, "Authentication mutation"):
            await self.client.request("authenticate", {"methodId":"gemini-api-key"})

    async def test_load_waits_for_history_even_after_rpc_response(self):
        self.script.write_text(FIXTURE.replace('update("user_message_chunk", "old user text")',
            'emit({"id":key,"result":{}})\n        import time; time.sleep(.05)\n        update("user_message_chunk", "old user text")'))
        await self.start()
        replay = [fingerprint({"sessionUpdate": kind, "content": {"type": "text", "text": text}})
                  for kind, text in [("user_message_chunk", "old user text"), ("agent_message_chunk", "old answer")]]
        await self.client.open_session(cwd=str(self.root), session_id=SESSION, replay=replay)
        self.assertEqual(self.events, [])
        await self.client.prompt([{"type":"text", "text":"next"}])
        self.assertEqual(len(self.events), 2)

    async def test_changed_replay_fails_before_prompt(self):
        await self.start()
        with self.assertRaisesRegex(GeminiProtocolError, "replay changed"):
            await self.client.open_session(cwd=str(self.root), session_id=SESSION, replay=[("user_message_chunk", "wrong")])

    async def test_permission_round_trip_is_explicit(self):
        async def approve(params):
            self.assertEqual(params["sessionId"], SESSION)
            return {"outcome":{"outcome":"selected","optionId":"once"}}
        self.client.on_permission = approve
        await self.start()
        await self.client.open_session(cwd=str(self.root))
        await self.client.prompt([{"type":"text","text":"test"}])
        self.assertEqual(json.loads(self.events[-1]["text"])["outcome"]["optionId"], "once")

    async def test_malformed_stdout_fails_pending_request_without_echoing_it(self):
        self.script.write_text('import sys\nsys.stdin.readline()\nprint("secret-not-json", flush=True)\n')
        with self.assertRaisesRegex(GeminiProtocolError, "invalid JSON-RPC"):
            await self.start()
        self.assertNotIn("secret", str(self.client.failure))

    async def test_close_reaps_owned_process(self):
        await self.start()
        proc = self.client.process
        await self.client.close()
        self.assertIsNotNone(proc.returncode)

    async def test_stop_immediately_after_spawn_keeps_verified_cleanup(self):
        async def stopped(_proc):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.client.start([sys.executable, str(self.script)], cwd=str(self.root),
                                    env=dict(os.environ), on_started=stopped)
        await self.client.close()
        self.assertTrue(self.client.cleanup_complete)
        self.assertIn(self.client.process.returncode, {0, 71, 143})

    async def test_cancel_during_spawn_retains_process_for_verified_cleanup(self):
        original = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()
        async def delayed(*args, **kwargs):
            proc = await original(*args, **kwargs)
            spawned.set()
            await release.wait()
            return proc
        with patch("gemini_acp_client.asyncio.create_subprocess_exec", side_effect=delayed):
            starting = asyncio.create_task(self.start())
            await asyncio.wait_for(spawned.wait(), 5)
            starting.cancel()
            await asyncio.sleep(0)
            starting.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await starting
        self.assertIsNotNone(self.client.process)
        await self.client.close()
        self.assertTrue(self.client.cleanup_complete)

    async def test_provider_exec_failure_does_not_leave_a_cleanup_fence(self):
        with self.assertRaises(GeminiProtocolError):
            await self.client.start([str(self.root / "missing-provider")],
                                    cwd=str(self.root), env=dict(os.environ))
        await self.client.close()
        self.assertTrue(self.client.cleanup_complete)
        self.assertEqual(self.client.process.returncode, 71)

    async def test_many_immediate_stops_never_accept_unverified_exit(self):
        async def cycle():
            client = GeminiACPClient(on_update=self.client.on_update)
            async def stopped(_proc):
                raise asyncio.CancelledError()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await client.start([sys.executable, str(self.script)], cwd=str(self.root),
                                       env=dict(os.environ), on_started=stopped)
            finally:
                await client.close()
            self.assertTrue(client.cleanup_complete)
            self.assertIn(client.process.returncode, {0, 71, 143})
        for _ in range(5):
            await asyncio.gather(*(cycle() for _ in range(4)))

    async def test_overlong_stdout_is_bounded_and_secret_safe(self):
        self.script.write_text('import sys\nsys.stdin.readline()\nprint("private" * 800000, flush=True)\n')
        with self.assertRaises(GeminiProtocolError): await self.start()
        self.assertNotIn("private", str(self.client.failure))

    async def test_request_timeout_and_cleanup_failure_are_not_success(self):
        await self.start()
        self.script.write_text(FIXTURE)  # This process already owns its program.
        with patch.object(self.client, "_send", return_value=None):
            with self.assertRaises(asyncio.TimeoutError):
                await self.client.request("fixture/no-response", {}, timeout=.01)
        self.assertFalse(self.client.pending)
        await self.client.close()
        self.client.cleanup_complete = False
        with patch.object(type(self.client.process), "returncode", new_callable=lambda: property(lambda _: 70)):
            with self.assertRaisesRegex(GeminiProtocolError, "could not be verified"):
                await self.client.close()
            self.assertFalse(self.client.cleanup_complete)
        await self.client.close()  # Cleanup is retryable, not an early-return no-op.
        self.assertTrue(self.client.cleanup_complete)


class GeminiMappingTests(unittest.TestCase):
    def test_known_restriction_is_actionable_without_echoing_credentials(self):
        message = safe_rpc_error({"code": -32000, "message": "This client is no longer supported for Gemini Code Assist for individuals. secret-key user@example.com"})
        self.assertIn("Developer API", message)
        self.assertNotIn("secret-key", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn("secret", safe_rpc_error({"message": "unexpected secret"}))

    def test_wrong_session_fails_closed(self):
        with self.assertRaises(GeminiProtocolError):
            normalize_update({"sessionId":"other", "update":{}}, SESSION)

    def test_user_frame_is_never_a_new_turn(self):
        self.assertIsNone(normalize_update({"sessionId":SESSION,
            "update":{"sessionUpdate":"user_message_chunk", "content":{"type":"text", "text":"internal"}}}, SESSION))

    def test_tool_identity_is_required(self):
        with self.assertRaises(GeminiProtocolError):
            normalize_update({"sessionId":SESSION,"update":{"sessionUpdate":"tool_call","toolCallId":""}}, SESSION)

    def test_models_are_native_not_invented(self):
        result = session_models({"models":{"currentModelId":"native-auto","availableModels":[
            {"modelId":"native-auto","name":"Auto"},{"modelId":"future-model","name":"Future"}]}})
        self.assertEqual(result["default_model"], "native-auto")
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(session_models({})["models"], [])


class GeminiIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "native"
        self.project = self.root / "project"
        self.project.mkdir()
        self.gemini = self.home / ".gemini"
        self.gemini.mkdir(parents=True)
        (self.gemini / "settings.json").write_text(json.dumps({"security":{"auth":{"selectedType":"oauth-personal"}}}))
        (self.gemini / "oauth_creds.json").write_text("{}")
        self.env = {"HOME":str(self.home), "GEMINI_API_KEY":"unrelated-key", "GOOGLE_GENAI_USE_VERTEXAI":"true"}

    def test_native_login_is_not_an_agentsdock_connection_even_with_credentials(self):
        before = (self.gemini / "settings.json").read_bytes()
        self.assertTrue(native_credentials_present(self.env))
        with self.assertRaisesRegex(GeminiConfigurationError, "API connections only"):
            build_environment(self.env, cwd=str(self.project))
        self.assertEqual((self.gemini / "settings.json").read_bytes(), before)
        self.assertTrue((self.gemini / "oauth_creds.json").exists())

    def test_api_mode_has_separate_home_and_never_overwrites_native_settings(self):
        before = (self.gemini / "settings.json").read_bytes()
        isolated = self.root / "api-connection"
        result = build_environment(self.env, cwd=str(self.project), api_home=isolated,
                                   api_key="test-key", base_url="https://example.invalid")
        self.assertEqual(result["GEMINI_CLI_HOME"], str(isolated))
        self.assertEqual(result["GEMINI_API_KEY"], "test-key")
        self.assertEqual((self.gemini / "settings.json").read_bytes(), before)
        self.assertFalse((isolated / ".gemini/oauth_creds.json").exists())
        self.assertNotIn("test-key", (isolated / ".gemini/settings.json").read_text())

    def test_api_forget_cannot_fall_back_to_google(self):
        with self.assertRaises(GeminiConfigurationError):
            build_environment(self.env, cwd=str(self.project), api_home=self.root / "api", api_key=None)

    def test_native_import_selects_explicit_api_only_in_process_without_mutating_login(self):
        before = (self.gemini / "settings.json").read_bytes()
        result = build_environment(self.env, cwd=str(self.project), api_home=self.root / "unused",
            api_key="selected-api-key", base_url="https://example.invalid/v1beta", native_history=True)
        self.assertEqual(result["GEMINI_CLI_HOME"],str(self.home))
        self.assertEqual(result["GEMINI_CLI_AUTH_OVERRIDE"],"gemini-api-key")
        self.assertEqual(result["GEMINI_API_KEY"],"selected-api-key")
        self.assertNotIn("GOOGLE_GENAI_USE_VERTEXAI",result)
        self.assertEqual((self.gemini / "settings.json").read_bytes(),before)
        self.assertEqual((self.gemini / "oauth_creds.json").read_text(),"{}")
        self.assertFalse((self.root / "unused").exists())

    def test_native_import_cannot_bypass_enforced_or_malformed_auth(self):
        for settings in ({"security":{"auth":{"enforcedType":"oauth-personal"}}}, {"security":None}):
            (self.gemini / "settings.json").write_text(json.dumps(settings))
            with self.assertRaises(GeminiConfigurationError):
                build_environment(self.env,cwd=str(self.project),api_home=self.root / "unused",
                    api_key="selected",base_url="https://example.invalid",native_history=True)

    def test_native_import_missing_key_never_falls_back_to_cached_google_auth(self):
        with self.assertRaises(GeminiConfigurationError):
            build_environment(self.env,cwd=str(self.project),api_home=self.root / "unused",native_history=True)

    def test_native_home_cannot_be_reused_as_api_home(self):
        with self.assertRaises(GeminiConfigurationError):
            build_environment(self.env, cwd=str(self.project), api_home=self.home,
                              api_key="test", base_url="https://example.invalid")

    def test_project_auth_override_does_not_silently_switch_account(self):
        (self.project / ".gemini").mkdir()
        (self.project / ".gemini/settings.json").write_text('{"security":{"auth":{"selectedType":"vertex-ai"}}}')
        with self.assertRaises(GeminiConfigurationError):
            build_environment(self.env, cwd=str(self.project))

    def test_managed_auth_cannot_be_bypassed_by_private_api_profile(self):
        settings = self.root / "managed.json"
        settings.write_text('{"security":{"auth":{"enforcedType":"oauth-personal"}}}')
        with self.assertRaises(GeminiConfigurationError):
            build_environment({**self.env, "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(settings)}, cwd=str(self.project),
                api_home=self.root / "api", api_key="synthetic", base_url="https://example.invalid")

    def test_versioned_endpoint_does_not_double_the_api_prefix(self):
        result = build_environment(self.env, cwd=str(self.project), api_home=self.root / "api",
            api_key="synthetic", base_url="https://example.invalid/v1beta/")
        self.assertEqual(result["GOOGLE_GEMINI_BASE_URL"], "https://example.invalid")
        self.assertEqual(result["GOOGLE_GENAI_API_VERSION"], "v1beta")

    def test_probe_bounds_wrapper_output_and_duration(self):
        wrapper = self.root / "gemini"
        for code, expected in [("print('0.64.0')", "ready"), ("print('x' * 8000)", "error"),
                               ("import time; time.sleep(10)", "error")]:
            wrapper.write_text(f"#!{sys.executable}\n{code}\n")
            wrapper.chmod(0o700)
            # A cold Python wrapper can take >0.5s on a loaded build host.
            # Keep the timeout failure case short without racing valid startup.
            timeout = .5 if 'sleep' in code else 3
            self.assertEqual(probe({**self.env, "AGENTSDOCK_GEMINI_BIN": str(wrapper)}, timeout=timeout)["status"], expected)
