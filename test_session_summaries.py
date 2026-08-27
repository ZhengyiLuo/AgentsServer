import json
import unittest

from agent_server import public_session


class SessionSummaryTests(unittest.TestCase):
    def test_summary_omits_provider_and_prompt_payloads(self):
        session = {
            "id": "chat-1",
            "title": "Large chat",
            "cwd": "/workspace",
            "backend": "codex",
            "system_prompt": "x" * 24_000,
            "session_id": "provider-claude",
            "claude_session_id": "provider-claude",
            "codex_thread_id": "provider-codex",
            "latest_agent_event_seq": 42,
        }

        summary = public_session(session, summary=True)
        full = public_session(session)

        self.assertNotIn("system_prompt", summary)
        self.assertNotIn("session_id", summary)
        self.assertNotIn("claude_session_id", summary)
        self.assertNotIn("codex_thread_id", summary)
        self.assertEqual(summary["cwd"], "/workspace")
        self.assertEqual(summary["latest_agent_event_seq"], 42)
        self.assertNotIn("emergency_alert", summary)
        self.assertNotIn("unacknowledged_emergency_count", summary)
        self.assertEqual(full["system_prompt"], session["system_prompt"])
        self.assertEqual(full["codex_thread_id"], "provider-codex")
        self.assertIsNone(full["emergency_alert"])
        self.assertEqual(full["unacknowledged_emergency_count"], 0)

    def test_summary_keeps_large_session_lists_bounded(self):
        raw_sessions = [
            {
                "id": f"chat-{index}",
                "title": f"Chat {index}",
                "backend": "codex",
                "system_prompt": "x" * 24_000,
                "codex_thread_id": f"thread-{index}",
            }
            for index in range(182)
        ]
        summaries = [public_session(session, summary=True) for session in raw_sessions]
        full_sessions = [public_session(session) for session in raw_sessions]
        summary_bytes = len(json.dumps({"sessions": summaries}))
        full_bytes = len(json.dumps({"sessions": full_sessions}))

        self.assertLess(summary_bytes, 150_000)
        self.assertLess(summary_bytes, full_bytes * 0.05)


if __name__ == "__main__":
    unittest.main()
