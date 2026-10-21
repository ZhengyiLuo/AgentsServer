import unittest

from agent_server import (
    codex_compaction_retry_mode,
    is_codex_resume_failure,
    is_silent_codex_completion,
    should_recover_codex_resume,
)


class CodexResumeRecoveryTests(unittest.TestCase):
    def recovery(self, **overrides: object) -> bool:
        values: dict[str, object] = {
            "allow_rollover": True,
            "resumed_provider_id": "thread-123",
            "stopped": False,
            "stream_error": None,
            "idle_killed": False,
            "resume_stalled": False,
            "returncode": 0,
            "produced_activity": False,
            "terminal_error": "",
        }
        values.update(overrides)
        return should_recover_codex_resume(**values)  # type: ignore[arg-type]

    def test_silent_resumed_turn_recovers(self) -> None:
        self.assertTrue(self.recovery())

    def test_silent_fresh_turn_does_not_roll_over(self) -> None:
        self.assertFalse(self.recovery(resumed_provider_id=None))

    def test_missing_provider_thread_recovers(self) -> None:
        self.assertTrue(
            self.recovery(
                returncode=1,
                terminal_error="No conversation found with session ID: thread-123",
            )
        )

    def test_stalled_resume_recovers(self) -> None:
        self.assertTrue(self.recovery(returncode=-15, resume_stalled=True))

    def test_unrelated_provider_failure_does_not_replay(self) -> None:
        self.assertFalse(
            self.recovery(
                returncode=1,
                terminal_error="Auth(AuthorizationRequired)",
            )
        )

    def test_compaction_failure_never_forks_history(self) -> None:
        for returncode in (0, 1, None, -15):
            for resume_stalled in (False, True):
                with self.subTest(returncode=returncode, resume_stalled=resume_stalled):
                    self.assertFalse(self.recovery(
                        returncode=returncode,
                        resume_stalled=resume_stalled,
                        terminal_error="Error running remote compact task: stream disconnected",
                    ))

    def test_retry_requires_full_latest_failed_compaction_only_turn(self) -> None:
        turn = {"id": "turn", "status": "failed", "itemsView": "full",
                "items": [{"type": "contextCompaction", "id": "compact"}]}
        self.assertEqual(codex_compaction_retry_mode([turn], "turn", "run"), "resubmit")
        for field, value in (("id", "later"), ("status", "inProgress"),
                             ("itemsView", "summary"), ("items", []),
                             ("items", [{"type": "userMessage"}]),
                             ("items", [{"type": "contextCompaction"}, {"type": "unknown"}])):
            with self.subTest(field=field, value=value):
                self.assertIsNone(codex_compaction_retry_mode([{**turn, field:value}], "turn", "run"))
        for value in (None, [], {}, [None]):
            self.assertIsNone(codex_compaction_retry_mode(value, "turn", "run"))
        user = {"type": "userMessage", "clientId": "run"}
        self.assertEqual(codex_compaction_retry_mode([{**turn, "items":[user]}], "turn", "run"), "continue")
        for items in ([user,user], [user,{"type":"commandExecution"}],
                      [{**user,"clientId":"other"}], [user,{"type":"agentMessage"}]):
            self.assertIsNone(codex_compaction_retry_mode([{**turn,"items":items}], "turn", "run"))

    def test_activity_prevents_replay(self) -> None:
        self.assertFalse(self.recovery(produced_activity=True))

    def test_stopped_or_broken_stream_does_not_replay(self) -> None:
        self.assertFalse(self.recovery(stopped=True))
        self.assertFalse(self.recovery(stream_error="connection reset"))
        self.assertFalse(self.recovery(idle_killed=True))

    def test_silent_completion_classifier(self) -> None:
        self.assertTrue(
            is_silent_codex_completion(
                stopped=False,
                stream_error=None,
                idle_killed=False,
                returncode=0,
                produced_activity=False,
            )
        )
        self.assertFalse(
            is_silent_codex_completion(
                stopped=False,
                stream_error=None,
                idle_killed=False,
                returncode=0,
                produced_activity=True,
            )
        )

    def test_resume_error_classifier_is_narrow(self) -> None:
        self.assertTrue(is_codex_resume_failure("thread not found"))
        self.assertTrue(is_codex_resume_failure("unknown thread id"))
        self.assertTrue(is_codex_resume_failure("thread is not loaded"))
        self.assertTrue(is_codex_resume_failure("Unable to resume session"))
        self.assertFalse(is_codex_resume_failure("timed out waiting for cloud requirements"))


if __name__ == "__main__":
    unittest.main()
