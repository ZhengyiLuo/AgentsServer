"""Exact historical goal-steer aliases; temporary ledgers and native sources only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from codex_history_repair import CodexNativeHistoryRepairCache
from tests.test_codex_goal_history_isolated import load_projection
from tests import test_codex_native_history_repair as native_fixture

PROVIDER = native_fixture.PROVIDER


class GoalSteerReplayRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / 'events.jsonl'
        self.source = self.root / f'rollout-{PROVIDER}.jsonl'
        self.cache = CodexNativeHistoryRepairCache()
        self.parse = load_projection()['codex_history_event_item']
        self.native = [
            {'seq': 1, 'id': 'provider', 'type': 'provider_session', 'backend': 'codex',
             'run_id': 'goal-run', 'provider_session_id': PROVIDER},
            self.steer(2, 'Use this input', 'turn-one'),
            self.steer(3, 'A later input', 'turn-two'),
        ]
        self.raw = [self.source_input('Use this input', 'turn-one', 1),
                    self.source_input('A later input', 'turn-two', 2)]
        self.fixture()

    def steer(self, seq, text, turn):
        return {'seq': seq, 'id': f'steer-{seq}', 'run_id': 'goal-run', 'backend': 'codex',
                'type': 'turn_steered', 'purpose': 'codex_goal_resume', 'prompt': text,
                'native_steer': True, 'native_goal_steer': True, 'provider_user_authored': True,
                'queued_id': f'queued-{seq}', 'provider_turn_id': turn}

    def source_input(self, text, turn, number):
        return {'type': 'response_item', 'timestamp': f'2026-09-11T12:01:{number:02d}.123Z',
                'payload': {'type': 'message', 'role': 'user', 'id': f'input-{number}',
                            'content': [{'type': 'input_text', 'text': text}],
                            'internal_chat_message_metadata_passthrough': {
                                'turn_id': turn, 'content_item_kinds': ['user.text']}}}

    def fixture(self, mutate_checkpoint=None):
        native_fixture.CodexNativeHistoryRepairTests.fixture(self, mutate_checkpoint)

    def prepare(self):
        self.cache.forget('chat')
        self.cache.prepare('chat', PROVIDER, self.events, self.source, self.root, self.parse)
        return [self.cache.project_event('chat', row) for row in self.imports]

    def test_aliases_only_imported_copies_without_completed_goal_owner(self):
        before = self.events.read_bytes(), self.source.read_bytes()
        projected = self.prepare()
        self.assertTrue(all(projected))
        self.assertEqual([row['provider_origin']['native_event_id'] for row in projected], ['steer-2', 'steer-3'])
        self.assertTrue(all(row['prompt'] == '' and row['metadata_only'] for row in projected))
        self.assertTrue(all(row['provider_user_authored'] for row in projected))
        self.assertTrue(all(self.cache.project_event('chat', event) is None for event in self.native))
        self.assertEqual(before, (self.events.read_bytes(), self.source.read_bytes()))

    def test_different_turn_thread_or_full_body_stays_visible(self):
        for field, value in (('provider_turn_id', 'other-turn'), ('prompt', 'Use this input with extra text')):
            with self.subTest(field=field):
                old = self.native[1][field]
                self.native[1][field] = value
                self.fixture()
                self.assertIsNone(self.prepare()[0])
                self.native[1][field] = old
        self.native[0]['provider_session_id'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.fixture()
        self.assertTrue(all(row is None for row in self.prepare()))

    def test_each_native_authorship_marker_is_required(self):
        for field, value in (('native_steer', False), ('native_goal_steer', False),
                             ('provider_user_authored', False), ('purpose', 'other'),
                             ('backend', 'claude'), ('queued_id', ''), ('id', ''),
                             ('imported', True), ('forked', True), ('source_text_sha256', '0' * 64)):
            with self.subTest(field=field):
                original = copy.deepcopy(self.native[1])
                self.native[1][field] = value
                self.fixture()
                self.assertIsNone(self.prepare()[0])
                self.native[1] = original

    def test_provider_binding_must_precede_receipt_and_remain_unambiguous(self):
        binding = self.native.pop(0)
        binding['seq'] = 4
        self.native.append(binding)
        self.fixture()
        self.assertTrue(all(row is None for row in self.prepare()))
        binding['seq'] = 1
        self.native = [binding, *self.native[:-1], {**binding, 'seq': 4, 'id': 'other-binding',
                       'provider_session_id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}]
        self.fixture()
        self.assertTrue(all(row is None for row in self.prepare()))

    def test_extra_genuine_same_turn_repeat_is_not_aliased(self):
        self.raw.append(self.source_input('Use this input', 'turn-one', 3))
        self.fixture()
        result = self.prepare()
        self.assertIsNone(result[0])
        self.assertIsNone(result[2])
        self.assertIsNotNone(result[1])

    def test_two_receipts_keep_two_originals_and_alias_only_two_exact_copies(self):
        self.native.append(self.steer(4, 'Use this input', 'turn-one'))
        self.raw.append(self.source_input('Use this input', 'turn-one', 3))
        self.fixture()
        result = self.prepare()
        self.assertTrue(all(result))
        self.assertEqual([row['provider_origin']['native_event_id'] for row in result],
                         ['steer-2', 'steer-3', 'steer-4'])
        self.assertTrue(all(self.cache.project_event('chat', event) is None for event in self.native))

    def test_duplicate_ack_identity_and_ambiguous_source_timestamp_stay_visible(self):
        self.raw.append(self.source_input('Use this input', 'turn-one', 3))
        self.native.append(self.steer(4, 'Use this input', 'turn-one'))
        for field in ('queued_id', 'id'):
            original = self.native[-1][field]
            self.native[-1][field] = self.native[1][field]
            self.fixture()
            result = self.prepare()
            self.assertIsNone(result[0])
            self.assertIsNone(result[2])
            self.native[-1][field] = original
        self.raw[-1]['timestamp'] = self.raw[0]['timestamp']
        self.fixture()
        result = self.prepare()
        self.assertIsNone(result[0])
        self.assertIsNone(result[2])

    def test_tampered_checkpoint_and_late_receipts_stay_visible(self):
        self.fixture(lambda checkpoint: checkpoint['cursor'].update(source_digest='0' * 64))
        self.assertTrue(all(row is None for row in self.prepare()))
        self.native[1]['seq'] = 202
        self.fixture()
        events = sorted((json.loads(line) for line in self.events.read_text().splitlines()), key=lambda event: event['seq'])
        self.events.write_text(''.join(json.dumps(event) + '\n' for event in events))
        self.assertIsNone(self.prepare()[0])


if __name__ == '__main__':
    unittest.main()
