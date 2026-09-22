"""Archive-after-delivery regressions: actual route guards and SQLite, no server import."""
from __future__ import annotations

import ast
from contextlib import closing
from copy import deepcopy
import re
from types import SimpleNamespace
import unittest

import tests.test_chat_mailbox_runtime_isolated as runtime


ROUTE_FUNCTIONS = {
    "canonical_provider_cross_chat_route_alias", "canonical_provider_cross_chat_route_actions",
    "normalized_provider_cross_chat_routes", "stored_provider_cross_chat_routes",
    "provider_cross_chat_routes", "normalized_provider_cross_chat_route_snapshot",
    "provider_cross_chat_route_snapshot_for_hints", "initial_provider_cross_chat_route_snapshot",
    "provider_cross_chat_route_snapshot_for_authority", "provider_cross_chat_route_id_is_revoked",
    "provider_cross_chat_pair_is_live", "provider_cross_chat_route_availability",
    "live_provider_cross_chat_route", "live_provider_chat_mailbox_route",
    "cross_chat_target_backend_supported", "provider_cross_chat_delivery_pair_is_live",
    "is_async_route_message", "async_route_delivery_snapshot",
}
ROUTE_CONSTANTS = {
    "PROVIDER_CROSS_CHAT_ROUTE_ACTIONS", "PROVIDER_CROSS_CHAT_ROUTE_ACTION_SET",
    "PROVIDER_CROSS_CHAT_ROUTE_KIND_AMBIENT", "PROVIDER_CROSS_CHAT_ROUTE_KIND_REFERENCE",
    "PROVIDER_CROSS_CHAT_ROUTE_ALIAS_RE", "PROVIDER_CROSS_CHAT_ROUTE_ID_RE",
    "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE", "PROVIDER_CROSS_CHAT_ROUTE_REVISION_RE",
    "PROVIDER_CROSS_CHAT_RECIPROCAL_EFFECT_ID_RE", "PENDING_PROVIDER_CROSS_CHAT_GRANT_KEY",
}


def install_actual_route_guards(namespace):
    # Share the existing AST tree and inert runtime/real-ledger fixture, but
    # replace EVERY route lambda at the regression boundary with actual code.
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in runtime.TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ROUTE_FUNCTIONS:
            copied = deepcopy(node)
            copied.decorator_list = []
            nodes.append(copied)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in ROUTE_CONSTANTS for target in node.targets
        ):
            nodes.append(deepcopy(node))
    start = next(node for node in runtime.TREE.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "_start_turn_locked")
    snapshot_blocks = [node for node in ast.walk(start) if isinstance(node, ast.If)
        and ast.unparse(node.test) == "mailbox_wake_claim is not None"
        and any(isinstance(child, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == "provider_route_snapshot" for target in child.targets)
                for child in node.body)]
    if len(snapshot_blocks) != 1:
        raise AssertionError("Exact mailbox wake capability snapshot boundary changed")
    wrapper = ast.parse("def mailbox_wake_route_snapshot(session_id, sess, mailbox_wake_claim):\n"
                        "    provider_route_snapshot = []\n"
                        "    return provider_route_snapshot").body[0]
    wrapper.body.insert(1, deepcopy(snapshot_blocks[0]))
    nodes.append(wrapper)
    namespace.update(re=re, VALID_BACKENDS={"codex", "claude"},
                     AGENT_AMBIENT_LOCAL_HANDOFFS_ENABLED=False,
                     cross_chat_supported_target_backends=lambda: ["codex", "claude"])
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-archived-sender-route-guards>", "exec"), namespace)


class ArchivedSenderMailboxTests(unittest.IsolatedAsyncioTestCase):
    lifecycle_lock = runtime.ChatMailboxRuntimeTests.lifecycle_lock
    set_recipient = runtime.ChatMailboxRuntimeTests.set_recipient
    work_snapshot = runtime.ChatMailboxRuntimeTests.work_snapshot
    send = runtime.ChatMailboxRuntimeTests.send
    read = runtime.ChatMailboxRuntimeTests.read
    assert_no_execution = runtime.ChatMailboxRuntimeTests.assert_no_execution
    enable_wake_admission = runtime.ChatMailboxRuntimeTests.enable_wake_admission
    wake_state = runtime.ChatMailboxRuntimeTests.wake_state

    async def asyncSetUp(self):
        await runtime.ChatMailboxRuntimeTests.asyncSetUp(self)
        install_actual_route_guards(self.ns)
        self.source.update(revision="rev_" + "d" * 32, alias="recipient", paired_route_id=runtime.RETURN_ROUTE)
        self.reverse.update(revision="rev_" + "e" * 32, alias="sender", paired_route_id=runtime.ROUTE)
        for session_id in ("sender", "recipient"):
            self.ns["STORE"].sessions[session_id].update(
                backend="codex", archived=False, provider_cross_chat_routes=self.routes[session_id],
            )
        self.set_recipient("goal")
        self.issue_normal_recipient_capability("recipient-before-archive")

    def issue_normal_recipient_capability(self, run_id):
        initial = self.ns["initial_provider_cross_chat_route_snapshot"](
            "recipient", SimpleNamespace(purpose=None, chat_references=[]), "chat",
        )
        return self.issue_recipient_ceiling(initial, run_id)

    def issue_recipient_ceiling(self, ceiling, run_id):
        routes = self.ns["provider_cross_chat_route_snapshot_for_authority"](
            ceiling, [], source_session_id="recipient",
        )
        capability = {"source_run_id": run_id, "async_route_v1": True,
                      "provider_route_grants": {route["route_id"]: route for route in routes}}
        self.capabilities["recipient"] = capability
        self.ns["ACTIVE"]["recipient"]["run_id"] = run_id
        self.ns["CURRENT_TURNS"]["recipient"]["run_id"] = run_id
        return deepcopy(capability)

    async def inbox(self):
        return await self.ns["get_provider_chat_mailbox"](
            SimpleNamespace(owner="recipient", query_params={}),
        )

    def archive_sender(self):
        self.ns["STORE"].sessions["sender"]["archived"] = True

    async def assert_stored_message_read_once(self, receipt):
        before = self.work_snapshot()
        page = await self.inbox()
        self.assertEqual([(row["source_session_id"], row["unread_count"]) for row in page["senders"]],
                         [("sender", 1)])
        first = await self.read("archive-read-once")
        self.assertEqual([(row["message_id"], row["body"]) for row in first["messages"]],
                         [(receipt["message_id"], "Exact synthetic peer message.")])
        self.assertFalse(first["replayed"])
        self.assertEqual(first["reply_routes"], [])
        self.assertFalse(first["automatic_reply"])
        again = await self.read("archive-read-once")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["read_id"], first["read_id"])
        self.assertEqual(again["messages"], first["messages"])
        self.assertEqual((await self.read("distinct-read"))["messages"], [])
        self.assertEqual((await self.inbox())["senders"], [])
        with closing(self.ledger._connect()) as connection:
            reader = connection.execute("""SELECT r.id,r.reader_run_id FROM chat_mailbox_reads r
                JOIN chat_mailbox_messages m ON m.read_id=r.id WHERE m.message_id=?""",
                (receipt["message_id"],)).fetchone()
        self.assertEqual(reader["id"], first["read_id"])
        self.assertEqual(reader["reader_run_id"], self.capabilities["recipient"]["source_run_id"])
        self.assertEqual(self.work_snapshot(), before)
        self.assertTrue(self.ns["STORE"].sessions["sender"]["archived"])
        self.assert_no_execution()

    async def test_new_ordinary_run_after_archive_reads_delivered_body_once(self):
        receipt = await self.send()
        self.assertEqual((await self.inbox())["senders"][0]["unread_count"], 1)
        self.archive_sender()
        self.issue_normal_recipient_capability("recipient-new-normal-run")
        await self.assert_stored_message_read_once(receipt)

    async def test_prearchive_issued_grant_reads_after_archive_without_reissue(self):
        receipt = await self.send()
        issued = deepcopy(self.capabilities["recipient"])
        self.archive_sender()
        await self.assert_stored_message_read_once(receipt)
        self.assertEqual(self.capabilities["recipient"], issued)

    async def test_archived_peer_never_becomes_send_reply_or_idle_wake_authority(self):
        receipt = await self.send()
        self.archive_sender()
        self.issue_normal_recipient_capability("recipient-new-normal-run")
        self.assertIsNone(self.ns["live_provider_cross_chat_route"]("recipient", self.reverse))
        self.assertFalse(self.ns["provider_cross_chat_pair_is_live"]("recipient", self.reverse))
        for reply_to in (None, receipt["message_id"]):
            with self.subTest(reply=bool(reply_to)), self.assertRaises(runtime.HTTPException) as caught:
                await self.ns["submit_provider_route_handoff"](runtime.RETURN_ROUTE, SimpleNamespace(
                    mode="async_route_v1", action="instruction", artifact_grants=[], body="Synthetic reply",
                    idempotency_key="must-not-send", wait_for_response=False, response_timeout_seconds=None,
                    reply_to_message_id=reply_to,
                ), SimpleNamespace(owner="recipient"))
            self.assertEqual(caught.exception.status_code, 403)
        self.ns["CHAT_MAILBOX_PENDING"].add("sender")
        self.assertFalse(await self.ns["maybe_start_chat_mailbox_locked"]("sender"))
        self.assert_no_execution()
        self.assertTrue(self.ns["STORE"].sessions["sender"]["archived"])

    async def test_idle_recipient_wakes_once_and_reads_archived_sender_without_reply(self):
        receipt = await self.send()
        self.archive_sender()
        sender_before = deepcopy(self.ns["STORE"].sessions["sender"])
        self.set_recipient("idle")
        goal_before = deepcopy(self.ns["STORE"].sessions["recipient"]["codex_goal"])
        captured_routes = []

        async def inspect_actual_snapshot(claim):
            routes = self.ns["mailbox_wake_route_snapshot"](
                "recipient", self.ns["STORE"].sessions["recipient"], claim,
            )
            self.assertEqual([route["route_id"] for route in routes], [runtime.RETURN_ROUTE])
            self.assertIsNone(self.ns["live_provider_cross_chat_route"]("recipient", routes[0]))
            captured_routes.extend(routes)

        self.enable_wake_admission(inspect_actual_snapshot)
        self.assertTrue(await self.ns["maybe_start_chat_mailbox_locked"]("recipient"))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.wake_state()["state"], "admitted")
        self.issue_recipient_ceiling(captured_routes, self.launches[0]["run_id"])
        await self.assert_stored_message_read_once(receipt)
        self.set_recipient("idle")
        self.assertFalse(await self.ns["maybe_start_chat_mailbox_locked"]("recipient"))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.ns["STORE"].sessions["sender"], sender_before)
        self.assertEqual(self.ns["STORE"].sessions["recipient"]["codex_goal"], goal_before)
        self.assert_no_execution()

    async def test_explicit_revocation_denies_unread_and_stable_read_replay(self):
        await self.send()
        self.archive_sender()
        self.issue_normal_recipient_capability("recipient-new-normal-run")
        first = await self.read("read-before-revoke")
        self.assertEqual(len(first["messages"]), 1)
        self.ns["STORE"].sessions["sender"]["_revoked_provider_cross_chat_route_ids"] = [runtime.ROUTE]
        self.assertEqual((await self.inbox())["senders"], [])
        replay = await self.read("read-before-revoke")
        self.assertEqual(replay["messages"], [])
        self.assertEqual(replay["unavailable_count"], 1)
        self.issue_normal_recipient_capability("recipient-after-revocation")
        self.assertEqual(self.capabilities["recipient"]["provider_route_grants"], {})
        self.assertEqual((await self.read("new-after-revoke"))["messages"], [])
        self.assert_no_execution()

    async def test_exact_legacy_delivery_ceiling_cannot_gain_another_sender(self):
        receipt = await self.send()
        delivery = await self.ledger.get(receipt["message_id"])
        ceiling = self.ns["async_route_delivery_snapshot"]("recipient", delivery)
        self.assertEqual([route["route_id"] for route in ceiling], [runtime.RETURN_ROUTE])
        other_source = {**self.source, "route_id": "route_" + "1" * 32,
                        "pair_id": "pair_" + "2" * 32, "paired_route_id": "route_" + "3" * 32}
        other_reverse = {**self.reverse, "route_id": other_source["paired_route_id"],
                         "pair_id": other_source["pair_id"], "paired_route_id": other_source["route_id"],
                         "alias": "other", "target_session_id": "other"}
        self.routes["recipient"].append(other_reverse)
        self.ns["STORE"].sessions["other"] = {"title": "Synthetic unrelated sender", "backend": "codex",
            "provider_cross_chat_routes": [other_source]}
        self.capabilities["other"] = {"source_run_id": "other-run", "async_route_v1": True,
                                      "provider_route_grants": {other_source["route_id"]: other_source}}
        self.ns["ACTIVE"]["other"] = self.ns["CURRENT_TURNS"]["other"] = {"run_id": "other-run"}
        await self.ns["submit_provider_route_handoff"](other_source["route_id"], SimpleNamespace(
            mode="async_route_v1", action="instruction", artifact_grants=[], body="Other synthetic body",
            idempotency_key="other-message", wait_for_response=False, response_timeout_seconds=None,
            reply_to_message_id=None,
        ), SimpleNamespace(owner="other"))
        self.archive_sender()
        self.issue_recipient_ceiling(ceiling, "legacy-delivery-run")
        self.assertEqual(set(self.capabilities["recipient"]["provider_route_grants"]), {runtime.RETURN_ROUTE})
        self.assertEqual([row["source_session_id"] for row in (await self.inbox())["senders"]], ["sender"])
        other_page = await self.ns["read_provider_chat_mailbox"](SimpleNamespace(
            source_session_id="other", request_id="forbidden-other", after_seq=0, limit=25,
        ), SimpleNamespace(owner="recipient"))
        self.assertEqual(other_page["messages"], [])
        await self.assert_stored_message_read_once(receipt)

    async def test_archive_exception_does_not_ignore_identity_or_lifecycle_fences(self):
        await self.send()
        self.archive_sender()
        resolver = self.ns["live_provider_chat_mailbox_route"]
        self.assertIsNotNone(resolver("recipient", self.reverse))
        for key, value in (("revision", "rev_" + "f" * 32), ("alias", "changed"),
                           ("pair_id", "pair_" + "f" * 32), ("paired_route_id", "route_" + "f" * 32),
                           ("target_session_id", "missing"), ("actions", []),
                           ("route_kind", "prompt_reference")):
            with self.subTest(field=key):
                self.assertIsNone(resolver("recipient", {**self.reverse, key: value}))
        for owner in ("sender", "recipient"):
            for collection in ("DELETING_SESSIONS", "DELETED_SESSION_TOMBSTONES"):
                with self.subTest(owner=owner, fence=collection):
                    self.ns[collection].add(owner)
                    self.assertIsNone(resolver("recipient", self.reverse))
                    self.ns[collection].remove(owner)
            self.ns["STORE"].sessions[owner]["_revoked_provider_cross_chat_route_ids"] = [
                runtime.ROUTE if owner == "sender" else runtime.RETURN_ROUTE]
            self.assertIsNone(resolver("recipient", self.reverse))
            self.ns["STORE"].sessions[owner].pop("_revoked_provider_cross_chat_route_ids")
        self.assertEqual(self.ns["provider_cross_chat_route_snapshot_for_authority"](
            [], [], source_session_id="recipient"), [])
        self.assert_no_execution()


if __name__ == "__main__":
    unittest.main()
