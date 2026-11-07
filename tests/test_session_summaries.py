import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse

from agent_server import (
    HISTORY_MAINTENANCE_STATE,
    public_session,
    session_provider_id,
    session_unread,
)


class SessionSummaryTests(unittest.TestCase):
    def test_gemini_identity_is_available_in_detail_not_summary(self):
        native_id = "11111111-1111-4111-8111-111111111111"
        session = {"id": "chat-gemini", "backend": "gemini", "gemini_session_id": native_id}
        self.assertEqual(public_session(session).get("gemini_session_id"), native_id)
        self.assertEqual(session_provider_id(session), native_id)
        self.assertNotIn("gemini_session_id", public_session(session, summary=True))
        self.assertIsNone(public_session({"id": "failed-first-turn", "backend": "gemini"}).get("gemini_session_id"))

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
        self.assertNotIn("provider_connection", summary)
        self.assertEqual(full["provider_connection"], "default")
        self.assertNotIn("history_maintenance", summary)
        self.assertEqual(full["history_maintenance"], {"status": None, "reason": None, "retry_at": None})

    def test_maintenance_failure_and_cleared_status_survive_summary(self):
        session = {"id": "maintenance-summary", "backend": "codex"}
        with patch.dict(HISTORY_MAINTENANCE_STATE, {}, clear=True):
            state = {"status": "deferred", "reason": "native_history_proof_unavailable", "retry_at": 123}
            HISTORY_MAINTENANCE_STATE[session["id"]] = state
            self.assertEqual(public_session(session, summary=True)["history_maintenance"], state)
            state.update(status="idle", reason=None, retry_at=None)
            self.assertEqual(public_session(session, summary=True)["history_maintenance"], state)

    def test_explicit_native_connection_survives_summary_as_a_cache_tombstone(self):
        summary = public_session({"id": "changed-chat", "backend": "cursor", "provider_connection": "default"}, summary=True)
        self.assertEqual(summary["provider_connection"], "default")

    def test_notification_fields_are_sparse_but_read_clears_are_explicit(self):
        self.assertNotIn("unread_notification_level", public_session({"id": "ordinary-chat"}, summary=True))
        notified = public_session({
            "id": "notified-chat", "latest_notification_level": "important",
            "latest_notification_message": "Review this", "unread_notification_level": "important",
            "unread_notification_seq": 12,
        }, summary=True)
        self.assertEqual(notified["unread_notification_level"], "important")
        self.assertEqual(notified["unread_notification_seq"], 12)
        read = public_session({
            "id": "notified-chat", "latest_notification_level": "important",
            "latest_notification_message": "Review this",
        }, summary=True)
        self.assertIsNone(read["unread_notification_level"])
        self.assertIsNone(read["unread_notification_seq"])

    def test_non_job_unread_projection_is_sparse_until_known_or_recovering(self):
        session = {"id": "unread-summary"}
        counter = session_unread.COUNTER
        with patch("agent_server.NON_JOB_AGENT_RECOVERY_PENDING", set()) as pending:
            self.assertNotIn(counter, public_session(session, summary=True))
            self.assertIsNone(public_session(session)[counter])

            pending.add(session["id"])
            self.assertIsNone(public_session(session, summary=True)[counter])
            pending.clear()

            session.update({counter: 0, session_unread.CHECKPOINT: 3})
            self.assertEqual(public_session(session, summary=True)[counter], 0)
            session[counter] = 2
            self.assertEqual(public_session(session, summary=True)[counter], 2)

            pending.add(session["id"])
            self.assertIsNone(public_session(session, summary=True)[counter])
            pending.clear()
            del session[session_unread.CHECKPOINT]
            self.assertIsNone(public_session(session, summary=True)[counter])

            del session[counter]
            session[session_unread.CHECKPOINT] = 3
            self.assertIsNone(public_session(session, summary=True)[counter])

    def test_summary_keeps_compact_goal_status_and_clear_tombstone(self):
        goal = {"status": "complete", "updatedAt": 123, "objective": "x" * 2000}
        summary = public_session({"id": "goal-chat", "backend": "codex", "codex_goal": goal}, summary=True)
        self.assertEqual(summary["codex_goal_status"], "complete")
        self.assertEqual(summary["codex_goal_updated_at"], 123)
        self.assertNotIn("codex_goal", summary)
        self.assertEqual(public_session({"id": "goal-chat", "backend": "codex", "codex_goal": None}, summary=True)["codex_goal_status"], None)

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

        self.assertLess(summary_bytes, 150_000)
        self.assertLess(summary_bytes, full_bytes * 0.05)


if __name__ == "__main__":
    unittest.main()
