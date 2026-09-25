"""Allowance parsing and account ownership without credentials or model calls."""
import asyncio
from types import SimpleNamespace
import unittest

from provider_usage import ProviderUsage, claude_window, codex_buckets, codex_snapshot
from codex_app_server import CodexAppServerClient
from claude_sdk_client import ClaudeSDKSupervisorManager, _ReceivedMessage
from tests.test_claude_sdk_client import FakeFactory


AT = "2030-01-01T00:00:00Z"
LATER = "2030-01-01T01:00:00Z"


class FakeManager:
    def __init__(self):
        self.client = SimpleNamespace(account_epoch=0)
        self.generation = 1
        self.ready = True
        self.calls = []
        self.account = {"account": {"type": "chatgpt", "email": "private@example.test"}}
        self.payload = {"accountId": "account-a", "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 2000000000}}}

    async def request(self, method, params):
        self.calls.append((method, params))
        return self.account if method == "account/read" else self.payload


class ParsingTests(unittest.TestCase):
    def test_multiple_buckets_have_distinct_windows_and_no_duplicate_legacy_view(self):
        first = {"limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300},
                 "secondary": {"usedPercent": 14, "windowDurationMins": 10080}}
        payload = {"rateLimits": first, "rateLimitsByLimitId": {"codex": first,
            "model-x": {"limitName": "Model X", "primary": {"usedPercent": 82}}}}
        result = codex_snapshot(codex_buckets(payload, AT), AT)
        self.assertEqual([w["id"] for w in result["windows"]], ["codex:primary", "codex:secondary", "model-x:primary"])
        self.assertEqual(result["windows"][2]["label"], "Model X")
        self.assertIsNone(result["windows"][2]["resets_at"])

    def test_rolling_nulls_keep_observed_metadata_and_full_read_can_clear_it(self):
        buckets = codex_buckets({"rateLimits": {"primary": {"usedPercent": 25},
            "secondary": {"usedPercent": 50}, "credits": {"hasCredits": True, "unlimited": False, "balance": "9.5"}}}, AT)
        updated = codex_buckets({"rateLimits": {"primary": {"usedPercent": 35}}}, LATER, previous=buckets)
        result = codex_snapshot(updated, LATER)
        self.assertEqual([w["used_percent"] for w in result["windows"]], [35, 50])
        self.assertEqual([w["observed_at"] for w in result["windows"]], [LATER, AT])
        self.assertEqual(result["credits"]["balance"], "9.5")
        sparse = {"rateLimits": {"secondary": None, "credits": None}}
        preserved = codex_snapshot(codex_buckets(sparse, LATER, previous=updated), LATER)
        self.assertEqual(preserved["windows"], result["windows"])
        self.assertEqual(preserved["credits"], result["credits"])
        cleared = codex_snapshot(codex_buckets(sparse, LATER), LATER)
        self.assertEqual(cleared["status"], "unavailable")
        self.assertNotIn("credits", cleared)
        fresh = codex_snapshot(codex_buckets({"rateLimits": {"primary": {"usedPercent": 5}, "credits": None}}, LATER), LATER)
        self.assertEqual(len(fresh["windows"]), 1)
        self.assertNotIn("credits", fresh)

    def test_sparse_window_keeps_reported_reset_and_duration(self):
        first = codex_buckets({"rateLimits": {"primary": {"usedPercent": 25, "resetsAt": 2000000000, "windowDurationMins": 300}}}, AT)
        cleared = codex_snapshot(codex_buckets({"rateLimits": {"primary": {"usedPercent": 30, "resetsAt": None, "windowDurationMins": None}}}, LATER, previous=first), LATER)
        self.assertEqual(len(cleared["windows"]), 1)
        self.assertEqual(cleared["windows"][0]["resets_at"], 2000000000)
        self.assertEqual(cleared["windows"][0]["window_minutes"], 300)

    def test_malformed_unknown_and_nonfinite_values_never_become_zero(self):
        for invalid in (None, True, "50", float("nan"), float("inf"), -1):
            value = codex_snapshot(codex_buckets({"rateLimits": {"primary": {"usedPercent": invalid}}}, AT), AT)
            self.assertEqual(value["status"], "unavailable")
            self.assertEqual(value["windows"], [])
        self.assertEqual(codex_snapshot(codex_buckets({}, AT), AT)["status"], "unavailable")

    def test_credit_balance_is_native_decimal_not_money_or_raw_text(self):
        for balance, expected in (("12.75", "12.75"), ("$12", None), ("secret-token", None), (None, None)):
            raw = {"rateLimits": {"credits": {"hasCredits": False, "unlimited": True, "balance": balance}}}
            result = codex_snapshot(codex_buckets(raw, AT), AT)
            self.assertEqual(result["credits"], {"has_credits": False, "unlimited": True, "balance": expected})

    def test_claude_fraction_is_percentage_and_unreported_fraction_stays_unknown(self):
        message = SimpleNamespace(rate_limit_info=SimpleNamespace(status="allowed_warning", rate_limit_type="five_hour", utilization=.85, resets_at=2000000000))
        self.assertEqual(claude_window(message, AT)["used_percent"], 85)
        message.rate_limit_info.utilization = None
        self.assertIsNone(claude_window(message, AT)["used_percent"])
        self.assertEqual(claude_window(message, AT)["status"], "allowed_warning")
        self.assertIsNone(claude_window({"rate_limit_info": {"status": "allowed"}}, AT))
        self.assertIsNone(claude_window({"rate_limit_info": {"rate_limit_type": [], "status": {}}}, AT))


class UsageStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_read_is_cached_and_refresh_uses_only_native_read_rpcs(self):
        usage, manager = ProviderUsage(), FakeManager()
        first = await usage.read_codex(manager)
        first["windows"][0]["used_percent"] = 99
        self.assertEqual((await usage.read_codex(manager))["windows"][0]["used_percent"], 25)
        self.assertEqual(manager.calls, [("account/read", {"refreshToken": False}), ("account/rateLimits/read", {})])
        await usage.read_codex(manager, refresh=True)
        self.assertEqual(len(manager.calls), 4)

    async def test_custom_and_native_api_key_accounts_never_fetch_chatgpt_quota(self):
        usage, manager = ProviderUsage(), FakeManager()
        manager._agentsdock_provider_revision = "custom-credential-revision"
        self.assertEqual((await usage.read_codex(manager))["account_kind"], "custom")
        self.assertEqual(manager.calls, [])
        del manager._agentsdock_provider_revision
        manager.account = {"account": {"type": "apiKey"}}
        self.assertEqual((await usage.read_codex(manager))["account_kind"], "api_key")
        self.assertEqual([call[0] for call in manager.calls], ["account/read"])

    async def test_native_custom_provider_with_retained_chatgpt_login_has_no_chatgpt_quota(self):
        usage, manager = ProviderUsage(), FakeManager()
        manager.account["requiresOpenaiAuth"] = False
        self.assertEqual((await usage.read_codex(manager))["account_kind"], "custom")
        self.assertEqual([call[0] for call in manager.calls], ["account/read"])

    async def test_native_update_refreshes_snapshot_without_rpc_or_raw_account_fields(self):
        usage, manager = ProviderUsage(), FakeManager()
        await usage.read_codex(manager)
        before = len(manager.calls)
        self.assertTrue(usage.observe_codex(manager, {"params": {"accountId": "account-a", "rateLimits": {"primary": {"usedPercent": 40}}}}))
        result = await usage.read_codex(manager)
        self.assertEqual(result["windows"][0]["used_percent"], 40)
        self.assertEqual(len(manager.calls), before)
        self.assertNotIn("account-a", repr(result))
        self.assertNotIn("private@example", repr(result))

    async def test_auth_switch_invalidates_cache_and_old_notifications(self):
        usage, manager = ProviderUsage(), FakeManager()
        await usage.read_codex(manager)
        manager.client.account_epoch += 1
        manager.account = {"account": {"type": "apiKey"}}
        self.assertTrue(usage.observe_codex(manager, {"params": manager.payload}))
        self.assertEqual((await usage.read_codex(manager))["account_kind"], "api_key")
        self.assertEqual(len(manager.calls), 3)

    async def test_native_process_restart_does_not_reuse_previous_account_usage(self):
        usage, manager = ProviderUsage(), FakeManager()
        await usage.read_codex(manager)
        manager.generation += 1
        manager.account = {"account": None}
        self.assertEqual((await usage.read_codex(manager))["status"], "unavailable")

    async def test_account_change_during_rate_read_discards_delayed_result(self):
        usage, manager = ProviderUsage(), FakeManager()
        original = manager.request
        async def switch(method, params):
            value = await original(method, params)
            if method == "account/rateLimits/read":
                manager.client.account_epoch += 1
                usage.invalidate_codex(manager)
            return value
        manager.request = switch
        self.assertEqual((await usage.read_codex(manager))["reason"], "account_changed")

    async def test_error_is_sanitized_and_next_user_read_can_retry(self):
        usage, manager = ProviderUsage(), FakeManager()
        original = manager.request
        async def broken(*args):
            raise RuntimeError("secret-auth-token")
        manager.request = broken
        result = await usage.read_codex(manager)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret-auth-token", repr(result))
        manager.request = original
        self.assertEqual((await usage.read_codex(manager))["status"], "available")

    async def test_concurrent_first_reads_share_one_snapshot(self):
        usage, manager = ProviderUsage(), FakeManager()
        result = await asyncio.gather(*(usage.read_codex(manager) for _ in range(10)))
        self.assertTrue(all(value["status"] == "available" for value in result))
        self.assertEqual(len(manager.calls), 2)

    def test_claude_windows_are_observed_only_and_scoped_to_native_connection(self):
        usage = ProviderUsage()
        self.assertEqual(usage.read_claude("chat-a", "owner-a:1")["status"], "unavailable")
        for kind in ("five_hour", "seven_day"):
            self.assertTrue(usage.observe_claude("chat-a", "owner-a:1", {"rate_limit_info": {
                "rate_limit_type": kind, "utilization": .5, "status": "allowed"}}))
        self.assertEqual(len(usage.read_claude("chat-a", "owner-a:1")["windows"]), 2)
        self.assertEqual(usage.read_claude("chat-b", "owner-b:1")["status"], "unavailable")
        self.assertEqual(usage.read_claude("chat-a", "owner-a:2")["status"], "unavailable")
        self.assertFalse(usage.observe_claude("chat-a", None, {"rate_limit_info": {"rate_limit_type": "five_hour"}}))


class NativeUsageObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_broadcast_does_not_delay_account_invalidation_or_reorder_cache_updates(self):
        client = CodexAppServerClient("unused", cwd="/tmp", env_factory=lambda: {})
        self.addAsyncCleanup(client.close)
        manager, usage = FakeManager(), ProviderUsage()
        manager.client = client
        await usage.read_codex(manager)
        release = asyncio.Event()
        seen = []
        def handler(notification):
            seen.append(notification["method"])
            if notification["method"] == "account/changed":
                usage.invalidate_codex(manager)
            else:
                usage.observe_codex(manager, notification)
            return release.wait()
        client.add_account_usage_handler(handler)
        client._route_notification({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 80}}}})
        client._route_notification({"method": "account/updated", "params": {"authMode": "apikey"}})
        self.assertEqual(seen, ["account/rateLimits/updated", "account/changed"])
        manager.account = {"account": {"type": "apiKey"}}
        self.assertEqual((await usage.read_codex(manager))["account_kind"], "api_key")
        release.set()

    async def test_codex_auth_payload_stays_out_of_normal_handlers_and_usage_callback(self):
        client = CodexAppServerClient("unused", cwd="/tmp", env_factory=lambda: {})
        normal, usage = [], []
        client.add_notification_handler(normal.append)
        client.add_account_usage_handler(usage.append)
        client._route_notification({"method": "account/updated", "params": {"privateAccount": "secret"}})
        self.assertEqual(client.account_epoch, 1)
        self.assertEqual(normal, [])
        self.assertEqual(usage, [{"method": "account/changed", "params": {}}])
        self.assertEqual(client.unmatched_notifications, [])

    async def test_claude_events_before_ack_and_after_result_are_observed_without_chat_messages(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"rate_limit_type": "five_hour", "utilization": .1}}
        factory = FakeFactory()
        factory.query_prefix_messages = [event]
        observed = []
        arrived = asyncio.Event()
        async def observer(chat_id, generation, message):
            observed.append((chat_id, generation, message))
            arrived.set()
        manager = ClaudeSDKSupervisorManager(client_factory=factory, usage_observer=observer)
        self.addAsyncCleanup(manager.close_all)
        handle = await manager.start_run("chat-a", "hello", run_id="run-a", options={}, configuration_key="a")
        await asyncio.wait_for(arrived.wait(), 1)
        generation = manager.usage_generation("chat-a")
        self.assertEqual(observed, [("chat-a", generation, event)])
        result = {"type": "result", "subtype": "success", "result": "done", "is_error": False}
        await factory.clients[0].emit(result)
        self.assertEqual(await asyncio.wait_for(handle.wait_result(), 1), result)
        self.assertEqual([message async for message in handle], [result])
        arrived.clear()
        await factory.clients[0].emit(event)
        await asyncio.wait_for(arrived.wait(), 1)
        self.assertEqual(len(observed), 2)
        supervisor = manager._supervisors["chat-a"]
        await supervisor._handle_received(_ReceivedMessage(supervisor.snapshot().generation - 1, event))
        self.assertEqual(len(observed), 2)


if __name__ == "__main__":
    unittest.main()
