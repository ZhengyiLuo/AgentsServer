import unittest

from unittest.mock import patch

from agent_server import (
    CodexExecMessageBuffer,
    codex_exec_agent_message,
    codex_exec_raw_event_text,
)


class CodexExecMessagePhaseTests(unittest.TestCase):
    def test_reads_phase_from_rollout_event_shapes(self) -> None:
        self.assertEqual(
            codex_exec_agent_message({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Working through it.",
                },
            }),
            ("Working through it.", ""),
        )
        self.assertEqual(
            codex_exec_agent_message({
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "Working through it.",
                    "phase": "commentary",
                },
            }),
            ("Working through it.", "commentary"),
        )
        self.assertEqual(
            codex_exec_agent_message({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            }),
            ("Done.", "final_answer"),
        )

    def test_reconciles_phase_less_cli_copy_with_phased_rollout_copy(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(buffer.observe("Working through it."), [])
        self.assertEqual(
            buffer.observe("Working through it.", "commentary"),
            [("commentary", "Working through it.")],
        )
        self.assertEqual(buffer.observe("Working through it.", "commentary"), [])

    def test_only_unclassified_terminal_message_becomes_final(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(buffer.observe("First progress update."), [])
        self.assertEqual(
            buffer.observe("Second progress update."),
            [("commentary", "First progress update.")],
        )
        self.assertEqual(
            buffer.flush(final=True),
            [("final_answer", "Second progress update.")],
        )

    def test_tool_boundary_flushes_unknown_message_as_commentary(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(buffer.observe("I am checking that now."), [])
        self.assertEqual(
            buffer.flush(final=False),
            [("commentary", "I am checking that now.")],
        )

    def test_delayed_phase_markers_do_not_lose_the_final_answer(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(buffer.observe("Completed result."), [])
        self.assertEqual(
            buffer.observe("Still working.", "commentary"),
            [
                ("commentary", "Completed result."),
                ("commentary", "Still working."),
            ],
        )
        self.assertEqual(
            buffer.observe("Completed result.", "final_answer"),
            [("final_answer", "Completed result.")],
        )
        self.assertEqual(buffer.flush(final=True), [])

    def test_same_text_can_be_promoted_from_commentary_to_final(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(
            buffer.observe("Result text.", "commentary"),
            [("commentary", "Result text.")],
        )
        self.assertEqual(buffer.observe("Result text."), [])
        self.assertEqual(
            buffer.flush(final=True),
            [("final_answer", "Result text.")],
        )

    def test_only_one_explicit_final_is_emitted(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(
            buffer.observe("Done.", "final_answer"),
            [("final_answer", "Done.")],
        )
        self.assertEqual(buffer.observe("Late progress.", "commentary"), [])
        self.assertEqual(buffer.observe("Different final.", "final_answer"), [])
        self.assertEqual(buffer.observe("Phase-less late copy."), [])

    def test_pending_commentary_precedes_a_different_explicit_final(self) -> None:
        buffer = CodexExecMessageBuffer()

        self.assertEqual(buffer.observe("Checking now."), [])
        self.assertEqual(
            buffer.observe("Finished.", "final_answer"),
            [
                ("commentary", "Checking now."),
                ("final_answer", "Finished."),
            ],
        )

    def test_recognized_raw_events_are_not_persisted_twice(self) -> None:
        self.assertEqual(
            codex_exec_raw_event_text('{"type":"item.completed"}', handled=True),
            "",
        )
        self.assertEqual(
            codex_exec_raw_event_text('{"type":"future.packet"}', handled=False),
            '{"type":"future.packet"}',
        )

    def test_unrecognized_raw_events_are_bounded(self) -> None:
        with patch("agent_server.CODEX_EXEC_RAW_EVENT_MAX_CHARS", 8):
            value = codex_exec_raw_event_text("abcdefghijkl", handled=False)
        self.assertTrue(value.startswith("abcdefgh"))
        self.assertIn("omitted 4 characters", value)


if __name__ == "__main__":
    unittest.main()
