import asyncio
import unittest
from unittest.mock import patch

import agent_server
from agent_server import TerminalOutputCredit


class TerminalOutputCreditTests(unittest.IsolatedAsyncioTestCase):
    def test_post_scroll_input_does_not_spawn_a_redundant_existence_check(self):
        with patch.object(agent_server.shutil, 'which', return_value='/usr/bin/tmux'), patch.object(agent_server, 'run_tmux') as run:
            agent_server.exit_terminal_auto_scroll('test')
        run.assert_called_once_with(['send-keys', '-X', '-t', agent_server.terminal_session_name('test'), 'cancel'], check=False)

    async def test_stops_until_client_parses_then_resumes(self):
        self.assertEqual(TerminalOutputCredit(True).window, 64 * 1024)
        credit = TerminalOutputCredit(True, 8)
        credit.record(8)
        waiter = asyncio.create_task(credit.wait())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        credit.acknowledge(4)
        await asyncio.wait_for(waiter, 0.2)
        self.assertEqual(credit.sent - credit.consumed, 4)

    async def test_duplicate_invalid_and_future_acks_do_not_create_credit(self):
        credit = TerminalOutputCredit(True, 8)
        credit.record(8)
        credit.acknowledge(3)
        for offset in (3, 2, -1, 9, True, "8", 8.0, None):
            credit.acknowledge(offset)
        self.assertEqual(credit.consumed, 3)

    async def test_legacy_clients_never_wait_for_acks(self):
        credit = TerminalOutputCredit(False, 8)
        credit.record(100)
        await asyncio.wait_for(credit.wait(), 0.2)

    async def test_disconnect_can_cancel_a_full_window(self):
        credit = TerminalOutputCredit(True, 8)
        credit.record(8)
        waiter = asyncio.create_task(credit.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
