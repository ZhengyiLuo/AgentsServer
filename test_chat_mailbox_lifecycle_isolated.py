"""Multi-step mailbox journeys using actual handlers, route guards and SQLite.

No provider or AgentsServer is imported/started. Every ledger and identity is
synthetic; lifecycle entry points come from the existing AST-only fixtures.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import chat_mailbox
import test_chat_mailbox_archived_sender_isolated as archived
import tests.test_chat_mailbox_runtime_isolated as runtime


class MailboxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = archived.ArchivedSenderMailboxTests()
        await self.case.asyncSetUp()
        self._cleanups.extend(self.case._cleanups)
        self.case._cleanups.clear()

    async def read_page(self, request_id, after_seq=0):
        return await self.case.ns['read_provider_chat_mailbox'](SimpleNamespace(
            source_session_id='sender', request_id=request_id, after_seq=after_seq, limit=25,
        ), SimpleNamespace(owner='recipient'))

    async def reopen(self):
        ledger = self.case.ns['Ledger'](self.case.ledger.path)
        await ledger.initialize()
        self.case.ledger = ledger
        self.case.ns['CROSS_CHAT'] = ledger
        return ledger

    async def test_busy_goal_backlog_retry_reconnect_pages_and_late_arrivals(self):
        case = self.case
        before = case.work_snapshot()
        # Concurrent sends and retry storms stay messages, not eighty queued
        # execution turns. The durable idempotency key owns message identity.
        receipts = await asyncio.gather(*[
            case.send(f'backlog-{index % 60}') for index in range(80)
        ])
        self.assertEqual(len({row['message_id'] for row in receipts}), 60)
        self.assertEqual(sum(row['duplicate'] for row in receipts), 20)
        self.assertEqual(case.work_snapshot(), before)
        self.assertEqual((await case.inbox())['senders'][0]['unread_count'], 60)
        case.ns['schedule_next_queued_turn'].assert_not_called()

        first = await self.read_page('backlog-snapshot')
        self.assertEqual(len(first['messages']), 25)
        self.assertTrue(first['has_more'])
        late = await asyncio.gather(*[case.send(f'late-{index}') for index in range(7)])
        late_ids = {row['message_id'] for row in late}
        pending = await case.ledger.mailbox_call(
            'list_messages', 'recipient', 'sender', [runtime.PAIR],
            after_seq=first['next_after_seq'], limit=25, unread_only=True,
        )
        cancelled_id, deleted_id = [row['message_id'] for row in pending['messages'][:2]]
        await case.ledger.mailbox_call('cancel_message', cancelled_id, now=runtime.NOW)
        await case.ns['delete_chat_mailbox_message']('recipient', deleted_id)

        await self.reopen()
        case.issue_normal_recipient_capability('recipient-after-disconnect')
        new_owner = case.work_snapshot()
        replay = await self.read_page('backlog-snapshot')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['messages'], first['messages'])
        self.assertEqual(replay['read_id'], first['read_id'])
        consumed = [row['message_id'] for row in first['messages']]
        cursor = first['next_after_seq']
        while cursor is not None:
            page = await self.read_page('backlog-snapshot', cursor)
            self.assertEqual(page['read_id'], first['read_id'])
            self.assertEqual(page['snapshot_seq'], first['snapshot_seq'])
            consumed.extend(row['message_id'] for row in page['messages'])
            cursor = page['next_after_seq']
        self.assertEqual(len(consumed), 58)
        self.assertEqual(len(set(consumed)), 58)
        self.assertFalse(set(consumed) & (late_ids | {cancelled_id, deleted_id}))
        self.assertEqual((await case.inbox())['senders'][0]['unread_count'], 7)
        fresh = await self.read_page('fresh-after-backlog')
        self.assertEqual({row['message_id'] for row in fresh['messages']}, late_ids)
        self.assertFalse(fresh['mail_pending'])
        self.assertEqual((await case.inbox())['senders'], [])
        self.assertEqual(case.work_snapshot(), new_owner)
        self.assertEqual(case.ns['STORE'].sessions['recipient']['codex_goal']['status'], 'active')
        with case.ledger._transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 67)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM cross_chat_exchanges').fetchone()[0], 0)
        case.assert_no_execution()

    async def test_restart_recovers_unadmitted_wake_but_never_replays_admitted_attempt(self):
        case = self.case
        case.set_recipient('idle')
        goal = deepcopy(case.ns['STORE'].sessions['recipient']['codex_goal'])
        for index in range(12):
            await case.send(f'pre-restart-{index}')
        claim = await case.ledger.mailbox_call('claim_wake', 'recipient', [runtime.PAIR], now=runtime.NOW)
        self.assertEqual(claim['through_seq'], 12)
        self.assertEqual(claim['state'], 'reserved')
        await self.reopen()
        self.assertEqual(await case.ledger.mailbox_call('recover_wakes'), 1)
        self.assertEqual(await case.ledger.mailbox_call('unread_targets'), ['recipient'])
        case.enable_wake_admission()
        await asyncio.gather(*[
            case.ns['_start_next_queued_turn_locked']('recipient', admission_backend='codex')
            for _ in range(8)
        ])
        self.assertEqual(len(case.launches), 1)
        self.assertEqual(case.wake_state()['state'], 'admitted')
        self.assertNotEqual(case.wake_state()['claim_id'], claim['claim_id'])
        self.assertEqual(case.ns['STORE'].sessions['recipient']['codex_goal'], goal)

        # After a crash the previous execution may already have done work.
        # Recover pending storage without silently launching it twice.
        await self.reopen()
        case.set_recipient('idle')
        self.assertEqual(await case.ledger.mailbox_call('recover_wakes'), 0)
        await case.ns['_start_next_queued_turn_locked']('recipient', admission_backend='codex')
        self.assertEqual(len(case.launches), 1)
        for row in await case.ledger.mailbox_envelopes():
            self.assertIsNone(row['read_at'])
        # A genuinely newer arrival may wake the idle recipient again.
        await case.send('post-restart-new-message')
        await case.ns['_start_next_queued_turn_locked']('recipient', admission_backend='codex')
        self.assertEqual([row['through_seq'] for row in case.launches], [12, 13])
        case.assert_no_execution()

    async def test_independent_sqlite_workers_cancel_and_read_have_one_atomic_winner(self):
        case = self.case
        other = case.ns['Ledger'](case.ledger.path)
        await other.initialize()
        for index in range(12):
            receipt = await case.send(f'raced-message-{index}')
            message_id = receipt['message_id']
            cancelled, page = await asyncio.gather(
                other.mailbox_call('cancel_message', message_id, now=runtime.NOW),
                self.read_page(f'race-read-{index}'), return_exceptions=True,
            )
            self.assertIsInstance(page, dict, page)
            row = (await case.ledger.mailbox_envelopes(message_id=message_id))[0]
            if cancelled is True:
                self.assertEqual(page['messages'], [])
                self.assertEqual(row['excluded_reason'], 'cancelled')
                self.assertIsNone(row['read_at'])
            else:
                self.assertIsInstance(cancelled, chat_mailbox.MailboxConflict)
                self.assertEqual([item['message_id'] for item in page['messages']], [message_id])
                self.assertIsNotNone(row['read_at'])
                self.assertIsNone(row['excluded_reason'])
            replay = await case.send(f'raced-message-{index}')
            self.assertTrue(replay['duplicate'])
            self.assertEqual(replay['message_id'], message_id)
            self.assertEqual(replay['state'], 'cancelled' if cancelled is True else 'read')
        with case.ledger._transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 12)
        case.assert_no_execution()

    async def test_cancelled_retry_retains_identity_and_obeys_live_route_and_body_fences(self):
        case = self.case
        first = await case.send('cancelled-stable-key')
        await case.ledger.mailbox_call('cancel_message', first['message_id'], now=runtime.NOW)
        case.set_recipient('idle')
        case.ns['schedule_next_queued_turn'].reset_mock()
        replay = await case.send('cancelled-stable-key')
        self.assertEqual((replay['message_id'], replay['state'], replay['duplicate'], replay['execution_started']),
                         (first['message_id'], 'cancelled', True, False))
        case.ns['schedule_next_queued_turn'].assert_not_called()
        for event in case.ns['append_cross_chat_event_once'].await_args_list[-2:]:
            self.assertEqual(event.args[2], 'chat_conversation_message_cancelled')
        changed_body = SimpleNamespace(
            mode='async_route_v1', action='instruction', artifact_grants=[],
            body='Different synthetic body', idempotency_key='cancelled-stable-key',
            wait_for_response=False, response_timeout_seconds=None, reply_to_message_id=None,
        )
        with self.assertRaises(runtime.HTTPException) as conflict:
            await case.ns['submit_provider_route_handoff'](runtime.ROUTE, changed_body, SimpleNamespace(owner='sender'))
        self.assertEqual(conflict.exception.status_code, 409)
        # A new explicit send is distinct; cancellation is not route revocation.
        newer = await case.send('distinct-explicit-message')
        self.assertNotEqual(newer['message_id'], first['message_id'])
        self.assertFalse(newer['duplicate'])
        self.assertEqual(newer['state'], 'unread')
        case.ns['STORE'].sessions['sender']['_revoked_provider_cross_chat_route_ids'] = [runtime.ROUTE]
        with self.assertRaises(runtime.HTTPException) as revoked:
            await case.send('cancelled-stable-key')
        self.assertEqual(revoked.exception.status_code, 403)
        with case.ledger._transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 2)
        case.assert_no_execution()

    async def test_legacy_cancelled_and_failed_mailbox_still_reject_without_execution(self):
        case = self.case
        envelope_id = 'handoff_' + 'd' * 32
        body = 'Synthetic legacy message'
        await case.ledger.create_instruction(
            envelope_id=envelope_id, source_session_id='sender', source_run_id='sender-run',
            target_session_id='recipient', body=body, idempotency_key='legacy-cancelled',
            authorization_kind='configured_route', authorization_route_id=runtime.ROUTE,
        )
        await case.ledger.update(envelope_id, status='cancelled')
        case.ns['reserve_provider_route_handoff'] = AsyncMock(return_value=({
            'envelope_id': envelope_id, 'source_run_id': 'sender-run',
            'target_session_id': 'recipient',
        }, True))
        request = SimpleNamespace(
            mode=None, action='instruction', artifact_grants=[], body=body,
            idempotency_key='legacy-cancelled', wait_for_response=False,
            response_timeout_seconds=None, reply_to_message_id=None,
        )
        with self.assertRaises(runtime.HTTPException) as rejected:
            await case.ns['submit_provider_route_handoff'](runtime.ROUTE, request, SimpleNamespace(owner='sender'))
        self.assertEqual(rejected.exception.status_code, 409)
        failed = await case.send('failed-mailbox')
        await case.ledger.update(failed['message_id'], status='failed')
        with self.assertRaises(runtime.HTTPException) as rejected:
            await case.send('failed-mailbox')
        self.assertEqual(rejected.exception.status_code, 409)
        case.assert_no_execution()


if __name__ == '__main__':
    unittest.main()
