import unittest

from fastapi.responses import JSONResponse

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
        self.assertTrue(all("subagent_limit" not in session for session in summaries))
        self.assertTrue(all(session["subagent_limit_control"] == {"supported": True} for session in summaries))
        full_sessions = [public_session(session) for session in raw_sessions]
        # Match the compact UTF-8 response body returned by the session route.
        summary_bytes = len(JSONResponse({"sessions": summaries}).body)
        full_bytes = len(JSONResponse({"sessions": full_sessions}).body)

        # Endpoint switching added this explicit active/requested state to
        # list snapshots so the composer can show a saved pending selection.
        # Account for exactly that 151-byte field per ordinary Codex chat;
        # retain the existing budget for every other summary field.
        default_provider_control = {
            "pending": False,
            "requested_provider": "default",
            "active_provider": "default",
            "requested_base_url": None,
            "active_base_url": None,
        }
        base_summaries = []
        for summary in summaries:
            base = dict(summary)
            self.assertEqual(base.pop("codex_provider_control"), default_provider_control)
            base_summaries.append(base)
        base_bytes = len(JSONResponse({"sessions": base_summaries}).body)
        self.assertLess(base_bytes, 150_000)
        self.assertEqual(summary_bytes - base_bytes, 151 * len(raw_sessions))
        self.assertLess(summary_bytes, full_bytes * 0.05)


if __name__ == "__main__":
    unittest.main()
