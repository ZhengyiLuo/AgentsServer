"""Independent pair messages: AST-only server, memory SQLite, mocked CLI I/O."""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agentsdock_chats as cli


TREE = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
FUNCTIONS = {
    "is_async_route_message", "async_route_conversation_fields",
    "cross_chat_message_event_type", "cross_chat_lifecycle_fields",
    "async_message_target_fields",
    "public_cross_chat_envelope", "reserve_async_provider_route_message",
    "submit_provider_route_handoff", "append_cross_chat_event_once",
    "append_cross_chat_lifecycle", "finish_cross_chat_delivery", "cross_chat_delivery_state",
    "issued_provider_capability_snapshot", "provider_authority_runtime_env",
    "resolve_provider_tool_arguments", "provider_tool_argument_value",
}
ROUTE = "route_" + "a" * 32
PAIR = "pair_" + "b" * 32
MESSAGE = "handoff_" + "c" * 32


class HTTPException(Exception):
    def __init__(self, status_code, detail, **kwargs):
        self.status_code = status_code
        self.detail = detail


def server_namespace():
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            copied = deepcopy(node)
            copied.decorator_list = []
            nodes.append(copied)
    namespace = {
        "asyncio": asyncio, "hashlib": hashlib, "suppress": suppress,
        "HTTPException": HTTPException,
        "PROVIDER_CROSS_CHAT_ROUTE_ID_RE": re.compile(r"route_[0-9a-f]{32}"),
        "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE": re.compile(r"pair_[0-9a-f]{32}"),
        "STORE": SimpleNamespace(sessions={"a": {"title": "Alice"}, "b": {"title": "Bob"}}),
        "sanitized_provider_route_label": lambda value: str(value or "Untitled chat"),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-async-route-transport>", "exec"), namespace)
    return namespace


def envelope(**overrides):
    return {
        "id": MESSAGE, "source_session_id": "a", "target_session_id": "b",
        "source_run_id": "run_one", "kind": "instruction", "action": "instruction",
        "authorization_kind": "configured_route", "authorization_route_id": ROUTE,
        "authorization_pair_id": PAIR, "body": "Prepared message", "status": "ready",
        **overrides,
    }


class AsyncRouteProjectionTests(unittest.TestCase):
    def setUp(self):
        self.ns = server_namespace()

    def test_legacy_rows_keep_original_event_and_dto(self):
        for record in (envelope(authorization_pair_id=""), envelope(kind="final_result"),
                       envelope(authorization_kind="explicit_prompt"), envelope(authorization_pair_id="invalid")):
            self.assertFalse(self.ns["is_async_route_message"](record))
            self.assertEqual(self.ns["cross_chat_message_event_type"](record, "cross_chat_handoff_started"),
                             "cross_chat_handoff_started")
            self.assertNotIn("conversation_mode", self.ns["public_cross_chat_envelope"](record))

    def test_every_paired_lifecycle_has_same_message_and_safe_title_identity(self):
        record = envelope(body="x" * 5000)
        for phase in ("registered", "received", "queued", "started", "delivered", "cancelled", "failed"):
            self.assertEqual(self.ns["cross_chat_message_event_type"](record, "cross_chat_handoff_" + phase),
                             "chat_conversation_message_" + phase)
            fields = self.ns["cross_chat_lifecycle_fields"](record, phase)
            self.assertEqual(fields["message_id"], fields["cross_chat_envelope_id"])
            self.assertEqual(fields["conversation_id"], PAIR)
            self.assertEqual(fields["conversation_mode"], "async_route_v1")
            self.assertEqual((fields["source_title"], fields["target_title"]), ("Alice", "Bob"))
            self.assertEqual(len(fields["handoff_preview"]), 4096)
            self.assertTrue(fields["handoff_body_truncated"])
        full = self.ns["public_cross_chat_envelope"](record, include_body=True)
        self.assertEqual(full["body"], record["body"])
        self.assertEqual(full["body_sha256"], fields["handoff_body_sha256"])
        self.assertNotIn("body", self.ns["public_cross_chat_envelope"](record))

    def test_native_goal_control_reservation_has_no_delivery_local_dependency(self):
        function = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "acquire_codex_control_thread")
        expression = next(node.value for node in ast.walk(function) if isinstance(node, ast.Assign)
                          and isinstance(node.value, ast.Dict)
                          and any(isinstance(key, ast.Constant) and key.value == "codex_control_reservation_id"
                                  for key in node.value.keys))
        result = eval(compile(ast.Expression(expression), "<native-goal-control-reservation>", "eval"),
                      {"BACKEND_CODEX": "codex", "reservation_id": "control_goal", **self.ns})
        self.assertEqual(result["purpose"], "codex_native_control")
        self.assertNotIn("conversation_mode", result)

    def test_actual_started_and_current_metadata_project_only_delivery_messages(self):
        function = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "_start_turn_locked")
        dictionaries = [node.value for node in ast.walk(function)
                        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                        and any(isinstance(target, ast.Name) and target.id in {"started_payload", "run_metadata"}
                                or isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                                and target.value.id == "CURRENT_TURNS" for target in node.targets)]
        self.assertEqual(len(dictionaries), 3)
        for metadata in dictionaries:
            expansions = [value for key, value in zip(metadata.keys, metadata.values)
                          if key is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                          and value.func.id == "async_route_conversation_fields"]
            self.assertEqual(len(expansions), 1)
            expression = compile(ast.Expression(expansions[0]), "<actual-conversation-metadata>", "eval")
            self.assertEqual(eval(expression, {**self.ns, "delivery_record": None}), {})
            fields = eval(expression, {**self.ns, "delivery_record": envelope()})
            self.assertEqual(fields["message_id"], MESSAGE)
            self.assertEqual(fields["conversation_mode"], "async_route_v1")

    def test_restart_projection_allows_empty_success_only_for_async_messages(self):
        event = {"type": "turn_finished", "purpose": "cross_chat_handoff_delivery", "exit_code": 0,
                 "result_text": "", "run_id": "delivery"}
        self.ns["cross_chat_events"] = lambda *a, **k: [event]
        self.ns["clean_assistant_text"] = lambda value: value
        self.assertEqual(self.ns["cross_chat_delivery_state"]("b", MESSAGE)["status"], "failed")
        event["conversation_mode"] = "async_route_v1"
        self.assertEqual(self.ns["cross_chat_delivery_state"]("b", MESSAGE)["status"], "delivered")
        event["stopped"] = True
        self.assertEqual(self.ns["cross_chat_delivery_state"]("b", MESSAGE)["status"], "failed")


class AsyncRouteLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = server_namespace()
        self.events = []
        self.seen = set()
        self.record = envelope(status="queued")
        self.ns.update({
            "CrossChatStore": SimpleNamespace(TERMINAL_STATUSES={"delivered", "failed", "cancelled"}),
            "CROSS_CHAT": SimpleNamespace(get=AsyncMock(side_effect=lambda _: self.record)),
            "cross_chat_lifecycle_lock": self.lock,
            "cross_chat_event_exists_async": AsyncMock(side_effect=lambda sid, eid, kind, **kw: (sid, eid, kind) in self.seen),
            "remember_cross_chat_event_types": lambda sid, eid, kinds: self.seen.update((sid, eid, kind) for kind in kinds),
            "append_durable_event": AsyncMock(side_effect=lambda sid, kind, body: self.events.append((sid, kind, body))),
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
        })

    @asynccontextmanager
    async def lock(self, *_args):
        yield

    async def test_registered_and_queued_replay_use_one_new_event_per_owner(self):
        for _ in range(2):
            await self.ns["append_cross_chat_event_once"]("a", self.record, "cross_chat_handoff_registered", "registered", "accepted")
            await self.ns["append_cross_chat_lifecycle"](self.record, "cross_chat_handoff_queued", "queued", "queued")
        self.assertEqual(len(self.events), 3)
        self.assertEqual(self.events[0][1], "chat_conversation_message_registered")
        self.assertEqual({event[0] for event in self.events[1:]}, {"a", "b"})
        self.assertTrue(all(event[2]["message_id"] == MESSAGE for event in self.events))

    async def test_cancel_winning_before_queued_append_suppresses_stale_state(self):
        stale = dict(self.record)
        self.record = {**self.record, "status": "cancelled"}
        await self.ns["append_cross_chat_lifecycle"](stale, "cross_chat_handoff_queued", "queued", "stale")
        self.assertEqual(self.events, [])
        await self.ns["append_cross_chat_lifecycle"](self.record, "cross_chat_handoff_cancelled", "cancelled", "cancelled")
        self.assertEqual(len(self.events), 2)
        self.assertTrue(all(event[1] == "chat_conversation_message_cancelled" for event in self.events))


class AsyncRouteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = server_namespace()
        self.path = Path("/isolated/run_authority.json")
        self.capability = {
            "source_run_id": "run_one", "authority_path": str(self.path),
            "actions": {"agent_cross_chat_routes"}, "provider_jobs_access": "blocked",
            "async_route_v1": True, "async_route_response_route_id": ROUTE,
        }
        self.ns.update({
            "CROSS_CHAT_CAPABILITY_LOCK": asyncio.Lock(),
            "CROSS_CHAT_CAPABILITIES": {"isolated": self.capability},
            "PROVIDER_JOBS_ACCESS_MODE_SET": {"blocked", "full", "read_only"},
            "provider_helper_server_origin": lambda: "http://127.0.0.1:1234",
            "validate_provider_runtime_env": lambda value: dict(value),
            "ProviderToolError": RuntimeError,
        })

    async def test_negotiated_mode_and_exact_response_route_stay_in_private_runtime(self):
        runtime = await self.ns["provider_authority_runtime_env"]("run_one", self.path, "a", [])
        self.assertEqual(runtime["AGENTSDOCK_CROSS_CHAT_MODE"], "async_route_v1")
        self.assertEqual(runtime["AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID"], ROUTE)
        self.assertEqual(runtime["AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE"], "async_route_v1")
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_RESPONSE_EXCHANGE_ID", runtime)
        arguments = ["respond-current", "--message", "Reply"]
        self.assertEqual(self.ns["resolve_provider_tool_arguments"]("chats", arguments, runtime), arguments)

    async def test_legacy_runtime_never_advertises_async_mode_or_reverse_route(self):
        self.capability["async_route_v1"] = False
        runtime = await self.ns["provider_authority_runtime_env"]("run_one", self.path, "a", [])
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_MODE", runtime)
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE", runtime)
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID", runtime)


class AsyncRouteAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = server_namespace()
        route = {"route_id": ROUTE, "pair_id": PAIR, "target_session_id": "b", "actions": ["instruction"]}
        self.capability = {"source_session_id": "a", "source_run_id": "run_one", "async_route_v1": True,
                           "provider_route_grants": {ROUTE: route}, "provider_route_handoff_count": 4,
                           "provider_route_consumed": {ROUTE: {"legacy": True}}}
        self.ns.update({
            "authorize_provider_action": AsyncMock(side_effect=lambda *a, **k: deepcopy(self.capability)),
            "live_provider_cross_chat_route": Mock(side_effect=lambda source, issued: dict(issued) or None),
            "provider_capability_is_attached_to_live_run": Mock(return_value=True),
            "provider_cross_chat_route_availability": Mock(return_value=(True, None)),
            "cross_chat_delivery_client_capabilities": Mock(return_value=[]),
            "provider_route_capability_source": AsyncMock(return_value="a"),
            "session_lifecycle_lock": self.lock,
            "provider_cross_chat_route_body_exceeds_limit": lambda body: len(body) > 16000,
            "prime_cross_chat_event_cache": Mock(),
            "append_cross_chat_event_once": AsyncMock(),
            "submit_cross_chat_delivery": AsyncMock(side_effect=self.deliver),
            "generic_provider_route_delivery_error": lambda: HTTPException(409, "delivery failed"),
            "join_task_despite_caller_cancellation": lambda task: task,
            "reserve_provider_route_handoff": AsyncMock(side_effect=AssertionError("legacy reservation called")),
        })
        ledger = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "CrossChatStore")
        schema = next(node.args[0].value for node in ast.walk(ledger)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "executescript" and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and "CREATE TABLE IF NOT EXISTS cross_chat_envelopes" in str(node.args[0].value))
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(schema)
        methods = ast.ClassDef(name="Ledger", bases=[], keywords=[], decorator_list=[], body=[
            deepcopy(node) for node in ledger.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"create_instruction", "get", "_row"}
        ])
        self.ns.update({"time": time, "now_iso": lambda: "2026-09-10T00:00:00Z",
                        "validated_cross_chat_source_user_instruction": lambda value: value})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), methods,
        ], type_ignores=[])), "<isolated-async-ledger>", "exec"), self.ns)
        self.ledger = self.ns["Ledger"]()
        self.ledger._transaction = lambda: self.connection
        self.ledger._call = self.call_operation
        self.ledger._initialized = True
        self.ledger.create_route_exchange_request = AsyncMock(side_effect=AssertionError("exchange created"))
        self.ns["CROSS_CHAT"] = self.ledger

    @staticmethod
    async def call_operation(operation):
        return operation()

    @asynccontextmanager
    async def lock(self, *_args):
        yield

    async def deliver(self, record):
        self.connection.execute("UPDATE cross_chat_envelopes SET status='queued' WHERE id=?", (record["id"],))
        self.connection.commit()
        return {**record, "status": "queued"}

    def request(self, key="message-key-one", **extra):
        return SimpleNamespace(mode="async_route_v1", action="instruction", artifact_grants=[],
                               body="Prepared message", idempotency_key=key, wait_for_response=False,
                               response_timeout_seconds=None, **extra)

    async def send(self, key="message-key-one", body="Prepared message"):
        request = self.request(key)
        request.body = body
        return await self.ns["submit_provider_route_handoff"](ROUTE, request, object())

    async def test_repeated_sends_are_individual_messages_without_spending_legacy_permission(self):
        before = deepcopy(self.capability)
        receipts = [await self.send(f"message-key-{index}") for index in range(6)]
        self.assertEqual(len({receipt["message_id"] for receipt in receipts}), 6)
        self.assertEqual(self.capability, before)
        self.assertTrue(all(receipt["mode"] == "async_route_v1" for receipt in receipts))
        self.ledger.create_route_exchange_request.assert_not_awaited()
        self.ns["reserve_provider_route_handoff"].assert_not_awaited()

    async def test_same_key_replay_is_single_sqlite_effect_and_single_queue_admission(self):
        first = await self.send()
        rate_records_before_replay = self.connection.execute(
            "SELECT COUNT(*) FROM cross_chat_route_rate_events").fetchone()[0]
        duplicate = await self.send()
        self.assertEqual(first["message_id"], duplicate["message_id"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.ns["submit_cross_chat_delivery"].assert_awaited_once()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM cross_chat_route_rate_events").fetchone()[0],
                         rate_records_before_replay)
        record = await self.ledger.get(first["message_id"])
        self.assertEqual(record["authorization_pair_id"], PAIR)
        self.assertEqual(record["source_user_instruction"], "")

    async def test_same_key_changed_body_or_pair_cannot_rebind_message(self):
        await self.send()
        with self.assertRaises(HTTPException) as conflict:
            await self.send(body="different")
        self.assertEqual(conflict.exception.status_code, 409)
        self.capability["provider_route_grants"][ROUTE]["pair_id"] = "pair_" + "d" * 32
        with self.assertRaises(HTTPException) as conflict:
            await self.send()
        self.assertEqual(conflict.exception.status_code, 409)

    async def test_unnegotiated_or_revoked_or_expired_run_fails_before_new_effect(self):
        self.capability["async_route_v1"] = False
        with self.assertRaises(HTTPException) as rejected:
            await self.send()
        self.assertEqual(rejected.exception.status_code, 409)
        self.capability["async_route_v1"] = True
        self.ns["live_provider_cross_chat_route"].return_value = None
        self.ns["live_provider_cross_chat_route"].side_effect = None
        with self.assertRaises(HTTPException):
            await self.send()
        self.ns["live_provider_cross_chat_route"].side_effect = lambda source, issued: issued
        self.ns["provider_capability_is_attached_to_live_run"].return_value = False
        with self.assertRaises(HTTPException):
            await self.send()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM cross_chat_envelopes").fetchone()[0], 0)

    async def test_many_messages_have_no_hourly_cap_but_replay_and_revocation_stay_fenced(self):
        receipts = [await self.send(f"message-key-{index}") for index in range(40)]
        self.assertEqual(len({receipt["message_id"] for receipt in receipts}), 40)
        duplicate = await self.send("message-key-0")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["message_id"], receipts[0]["message_id"])
        self.assertEqual(self.ns["submit_cross_chat_delivery"].await_count, 40)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM cross_chat_envelopes").fetchone()[0], 40)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM cross_chat_route_rate_events").fetchone()[0], 0)

        self.ns["live_provider_cross_chat_route"].side_effect = None
        self.ns["live_provider_cross_chat_route"].return_value = None
        for key in ("message-key-0", "message-key-after-revoke"):
            with self.assertRaises(HTTPException) as rejected:
                await self.send(key)
            self.assertEqual(rejected.exception.status_code, 403)
        self.assertEqual(self.ns["submit_cross_chat_delivery"].await_count, 40)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM cross_chat_envelopes").fetchone()[0], 40)

    async def test_http_cancellation_joins_one_durable_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.deliver

        async def delayed(record):
            entered.set()
            await release.wait()
            return await original(record)

        self.ns["submit_cross_chat_delivery"].side_effect = delayed
        task = asyncio.create_task(self.send())
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue((await self.send())["duplicate"])
        self.ns["submit_cross_chat_delivery"].assert_awaited_once()

    async def test_cancelled_message_retry_does_not_restart_delivery(self):
        receipt = await self.send()
        self.connection.execute("UPDATE cross_chat_envelopes SET status='cancelled' WHERE id=?", (receipt["message_id"],))
        self.connection.commit()
        with self.assertRaises(HTTPException):
            await self.send()
        self.ns["submit_cross_chat_delivery"].assert_awaited_once()

    async def test_successful_empty_final_only_completes_message_and_never_sends_reply(self):
        record = envelope(status="running")
        self.ns["CROSS_CHAT"] = SimpleNamespace(get=AsyncMock(return_value=record), update=AsyncMock(return_value={**record, "status": "delivered"}))
        terminal = self.ns["append_cross_chat_terminal_lifecycle"] = AsyncMock()
        self.ns["clean_assistant_text"] = lambda value: value
        await self.ns["finish_cross_chat_delivery"]({"cross_chat_envelope_id": MESSAGE, "result_text": "", "exit_code": 0})
        self.assertEqual(self.ns["CROSS_CHAT"].update.await_args.kwargs["status"], "delivered")
        terminal.assert_awaited_once()
        self.ns["submit_cross_chat_delivery"].assert_not_awaited()


class AsyncRouteHelperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(cli.os.environ, {"AGENTSDOCK_CROSS_CHAT_MODE": "async_route_v1"}, clear=True))
        self.enterContext(patch.object(cli, "authority", return_value="isolated-only"))
        self.get = self.enterContext(patch.object(cli, "get_json", return_value={
            "routes": [{"route_id": ROUTE, "mode": "async_route_v1", "available": True}],
        }))
        self.receipt = {"ok": True, "route_id": ROUTE, "action": "instruction", "accepted": True,
                        "mode": "async_route_v1", "message_id": MESSAGE, "duplicate": False}
        self.post = self.enterContext(patch.object(cli, "post_json", return_value=self.receipt))
        self.wait = self.enterContext(patch.object(cli, "await_live_response", side_effect=AssertionError("async send waited")))

    def execute(self, *args):
        parsed = cli.parser().parse_args(list(args))
        return parsed.handler(parsed)

    def test_send_and_ask_both_commit_one_message_and_return_immediately(self):
        for action in ("send", "ask"):
            self.assertEqual(self.execute(action, "--route", ROUTE, "--message", "Hello"), self.receipt)
            payload = self.post.call_args.args[1]
            self.assertEqual(payload["mode"], "async_route_v1")
            self.assertEqual(payload["action"], "instruction")
            self.assertNotIn("wait_for_response", payload)
        self.wait.assert_not_called()

    def test_explicit_async_mode_requires_supported_route_before_post(self):
        self.get.return_value = {"routes": [{"route_id": ROUTE, "available": True}]}
        with self.assertRaises(cli.ChatsCLIError):
            self.execute("ask", "--route", ROUTE, "--mode", "async_route_v1", "--message", "Hello")
        self.post.assert_not_called()

    def test_unavailable_or_mismatched_receipt_never_opens_a_wait(self):
        self.post.return_value = {"ok": True, "route_id": ROUTE, "action": "instruction", "accepted": True}
        with self.assertRaises(cli.ChatsCLIError):
            self.execute("ask", "--route", ROUTE, "--message", "Hello")
        self.wait.assert_not_called()

    def test_legacy_route_keeps_legacy_payload(self):
        self.get.return_value = {"routes": [{"route_id": ROUTE, "available": True}]}
        self.post.return_value = {"ok": True, "route_id": ROUTE, "action": "instruction", "accepted": True}
        self.execute("send", "--route", ROUTE, "--message", "Hello")
        self.assertNotIn("mode", self.post.call_args.args[1])

    def test_respond_current_is_new_exact_route_message_not_exchange_response(self):
        with patch.dict(cli.os.environ, {"AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE": "async_route_v1",
                                        "AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID": ROUTE}, clear=True):
            self.assertEqual(self.execute("respond-current", "--message", "Reply"), self.receipt)
            first_key = self.post.call_args.args[1]["idempotency_key"]
            self.execute("respond-current", "--message", "Another reply")
            self.assertNotEqual(self.post.call_args.args[1]["idempotency_key"], first_key)
        self.assertEqual(self.post.call_args.args[0], f"/api/agent/cross-chat/routes/{ROUTE}/handoffs")
        self.assertNotIn("exchange", self.post.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
