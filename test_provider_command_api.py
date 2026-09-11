import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import agent_server
from provider_commands import ProviderCommandRecord, codex_provider_command_inventory


class ProviderCommandAPIContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_snapshot_is_explicitly_unsupported_and_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = {
                "cursor-chat": {
                    "id": "cursor-chat",
                    "backend": "cursor",
                    "cwd": tmp,
                }
            }
            with patch.object(agent_server.STORE, "sessions", sessions):
                snapshot = await agent_server.get_session_provider_commands(
                    "cursor-chat"
                )
                with self.assertRaises(HTTPException) as missing:
                    await agent_server.get_session_provider_commands("missing")

        self.assertEqual(snapshot["backend"], "cursor")
        self.assertEqual(snapshot["commands"], [])
        self.assertEqual(snapshot["support"]["available"], False)
        self.assertEqual(snapshot["support"]["mode"], "unsupported")
        self.assertEqual(missing.exception.status_code, 404)

    async def test_selector_key_failure_degrades_to_unavailable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = {"id": "codex-chat", "backend": "codex", "cwd": tmp}
            with patch.object(
                agent_server,
                "provider_command_selector_secret",
                side_effect=RuntimeError("private state unavailable"),
            ):
                snapshot, inventory = (
                    await agent_server.discover_session_provider_commands(
                        "codex-chat",
                        session,
                    )
                )

        self.assertEqual(snapshot["backend"], "codex")
        self.assertEqual(snapshot["commands"], [])
        self.assertEqual(snapshot["support"]["available"], False)
        self.assertEqual(snapshot["support"]["mode"], "unavailable")
        self.assertEqual(inventory.records, ())

    async def test_selection_is_revalidated_and_stale_or_mismatched_is_409(self) -> None:
        record = ProviderCommandRecord(
            public={
                "id": "pcmd_" + "a" * 32,
                "name": "review",
                "label": "Review",
                "description": "",
                "scope": "project",
                "source": "codex",
                "kind": "skill",
                "invocation": "/review",
            },
            native={"name": "review", "path": "/private/skill/SKILL.md"},
        )
        inventory = agent_server.ProviderCommandInventory(
            backend="codex",
            revision="pcmdrev_" + "b" * 32,
            records=(record,),
        )
        snapshot = {
            "backend": "codex",
            "revision": inventory.revision,
            "support": {"available": True, "mode": "native"},
            "commands": inventory.commands,
        }
        selection = agent_server.SkillSelection(
            id=record.public["id"],
            revision=inventory.revision,
        )
        session = {"id": "chat-a", "backend": "codex", "cwd": "/tmp"}

        with patch.object(
            agent_server,
            "discover_session_provider_commands",
            AsyncMock(return_value=(snapshot, inventory)),
        ):
            resolved = await agent_server.resolve_provider_command_selection(
                "chat-a",
                session,
                selection,
                prompt="/review staged files",
                purpose=None,
                provider_context_mode="chat",
                client_capabilities=[
                    agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                ],
            )
            self.assertIs(resolved, record)

            with self.assertRaises(agent_server.ProviderCommandSelectionInvalid) as stale:
                await agent_server.resolve_provider_command_selection(
                    "chat-a",
                    session,
                    agent_server.SkillSelection(
                        id=record.public["id"],
                        revision="pcmdrev_" + "c" * 32,
                    ),
                    prompt="/review",
                    purpose=None,
                    provider_context_mode="chat",
                    client_capabilities=[
                        agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                    ],
                )
            self.assertEqual(stale.exception.status_code, 409)

            with self.assertRaises(agent_server.ProviderCommandSelectionInvalid) as mismatch:
                await agent_server.resolve_provider_command_selection(
                    "chat-a",
                    session,
                    selection,
                    prompt="/review-more",
                    purpose=None,
                    provider_context_mode="chat",
                    client_capabilities=[
                        agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                    ],
                )
            self.assertEqual(mismatch.exception.status_code, 409)

    async def test_unavailable_selection_is_typed_for_terminal_queue_handling(self) -> None:
        session = {"id": "chat-a", "backend": "codex", "cwd": "/tmp"}
        empty = agent_server.empty_provider_command_inventory(
            "codex",
            cwd="/tmp",
            selector_secret="a" * 64,
            binding_context="chat-a",
        )
        with patch.object(
            agent_server,
            "discover_session_provider_commands",
            AsyncMock(return_value=(
                {
                    "backend": "codex",
                    "revision": empty.revision,
                    "support": {"available": False, "mode": "unavailable"},
                    "commands": [],
                },
                empty,
            )),
        ):
            with self.assertRaises(
                agent_server.ProviderCommandSelectionUnavailable
            ) as unavailable:
                await agent_server.resolve_provider_command_selection(
                    "chat-a",
                    session,
                    agent_server.SkillSelection(
                        id="pcmd_" + "a" * 32,
                        revision="pcmdrev_" + "b" * 32,
                    ),
                    prompt="/review",
                    purpose=None,
                    provider_context_mode="chat",
                    client_capabilities=[
                        agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                    ],
                )

        self.assertEqual(unavailable.exception.status_code, 503)

    async def test_transport_ineligible_selection_is_permanently_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_EXEC,
        ):
            with self.assertRaises(
                agent_server.ProviderCommandSelectionInvalid
            ) as invalid:
                await agent_server.resolve_provider_command_selection(
                    "chat-a",
                    {"id": "chat-a", "backend": "codex", "cwd": tmp},
                    agent_server.SkillSelection(
                        id="pcmd_" + "a" * 32,
                        revision="pcmdrev_" + "b" * 32,
                    ),
                    prompt="/review",
                    purpose=None,
                    provider_context_mode="chat",
                    client_capabilities=[
                        agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                    ],
                )

        self.assertEqual(invalid.exception.status_code, 409)

    async def test_process_secret_rotation_makes_old_selection_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            native = {
                "data": [{
                    "cwd": tmp,
                    "skills": [{
                        "name": "review",
                        "path": str(Path(tmp, "review", "SKILL.md").resolve()),
                    }],
                }]
            }
            previous = codex_provider_command_inventory(
                native,
                cwd=tmp,
                selector_secret="a" * 64,
                binding_context="chat-a",
            )
            current = codex_provider_command_inventory(
                native,
                cwd=tmp,
                selector_secret="b" * 64,
                binding_context="chat-a",
            )
            snapshot = {
                "backend": "codex",
                "revision": current.revision,
                "support": {"available": True, "mode": "native"},
                "commands": current.commands,
            }
            with patch.object(
                agent_server,
                "discover_session_provider_commands",
                AsyncMock(return_value=(snapshot, current)),
            ):
                with self.assertRaises(
                    agent_server.ProviderCommandSelectionInvalid
                ) as stale:
                    await agent_server.resolve_provider_command_selection(
                        "chat-a",
                        {"id": "chat-a", "backend": "codex", "cwd": tmp},
                        agent_server.SkillSelection(
                            id=previous.commands[0]["id"],
                            revision=previous.revision,
                        ),
                        prompt="/review",
                        purpose=None,
                        provider_context_mode="chat",
                        client_capabilities=[
                            agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY
                        ],
                    )

        self.assertEqual(stale.exception.status_code, 409)

    def test_selection_schema_rejects_paths_names_and_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            agent_server.SkillSelection.model_validate({
                "id": "pcmd_" + "a" * 32,
                "revision": "pcmdrev_" + "b" * 32,
                "path": "/private/skill/SKILL.md",
                "name": "review",
            })

    def test_codex_native_input_uses_text_and_structured_skill_without_client_path(self) -> None:
        command = ProviderCommandRecord(
            public={"invocation": "/review"},
            native={"name": "review", "path": "/private/skill/SKILL.md"},
        )

        self.assertEqual(
            agent_server.codex_provider_command_turn_input("/review", command),
            [
                {"type": "text", "text": "$review", "text_elements": []},
                {
                    "type": "skill",
                    "name": "review",
                    "path": "/private/skill/SKILL.md",
                },
            ],
        )
        self.assertEqual(
            agent_server.codex_provider_command_turn_input(
                "/review\nFocus on auth",
                command,
            )[0]["text"],
            "$review\nFocus on auth",
        )


class ProviderCommandSelectorProcessScopeTests(unittest.TestCase):
    def test_selector_secret_is_process_private_and_rotation_changes_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                agent_server,
                "PROVIDER_COMMAND_SELECTOR_SECRET",
                "a" * 64,
            ):
                first_key = agent_server.provider_command_selector_secret()
                first = codex_provider_command_inventory(
                    {
                        "data": [{
                            "cwd": tmp,
                            "skills": [{
                                "name": "review",
                                "path": str(Path(tmp, "skill", "SKILL.md").resolve()),
                            }],
                        }]
                    },
                    cwd=tmp,
                    selector_secret=first_key,
                    binding_context="chat-a",
                )
                second_key = agent_server.provider_command_selector_secret()
                second = codex_provider_command_inventory(
                    {
                        "data": [{
                            "cwd": tmp,
                            "skills": [{
                                "name": "review",
                                "path": str(Path(tmp, "skill", "SKILL.md").resolve()),
                            }],
                        }]
                    },
                    cwd=tmp,
                    selector_secret=second_key,
                    binding_context="chat-a",
                )

            with patch.object(
                agent_server,
                "PROVIDER_COMMAND_SELECTOR_SECRET",
                "b" * 64,
            ):
                rotated_key = agent_server.provider_command_selector_secret()
                rotated = codex_provider_command_inventory(
                    {
                        "data": [{
                            "cwd": tmp,
                            "skills": [{
                                "name": "review",
                                "path": str(
                                    Path(tmp, "skill", "SKILL.md").resolve()
                                ),
                            }],
                        }]
                    },
                    cwd=tmp,
                    selector_secret=rotated_key,
                    binding_context="chat-a",
                )

            self.assertEqual(first_key, second_key)
            self.assertNotEqual(first_key, rotated_key)
            self.assertEqual(first.revision, second.revision)
            self.assertEqual(first.commands[0]["id"], second.commands[0]["id"])
            self.assertNotEqual(first.revision, rotated.revision)
            self.assertNotEqual(
                first.commands[0]["id"],
                rotated.commands[0]["id"],
            )
            self.assertNotIn(first_key, json.dumps(first.commands))


if __name__ == "__main__":
    unittest.main()
