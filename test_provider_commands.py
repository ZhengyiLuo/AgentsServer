import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from provider_commands import (
    MAX_PROVIDER_COMMANDS,
    canonical_provider_command_name,
    claude_provider_command_inventory,
    codex_provider_command_inventory,
    opencode_provider_skill_inventory,
    sanitize_provider_command_text,
    validate_opencode_provider_skill_record,
)


class ProviderCommandInventoryTests(unittest.TestCase):
    @staticmethod
    def _write_skill(root: Path, name: str, description: str = "Run safely") -> Path:
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nBody\n",
            encoding="utf-8",
        )
        return path

    def test_opencode_scans_only_documented_roots_and_keeps_native_paths_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            cwd = project / "nested"
            cwd.mkdir(parents=True)
            (project / ".git").mkdir()
            home = base / "home"
            home.mkdir()
            project_path = self._write_skill(
                project / ".opencode" / "skills", "review", "Review /secret/path"
            )
            self._write_skill(home / ".config" / "opencode" / "skills", "global-skill")
            self._write_skill(home / ".opencode" / "skills", "undocumented")

            inventory = opencode_provider_skill_inventory(
                cwd=str(cwd),
                home=str(home),
                xdg_config_home=None,
                selector_secret="a" * 64,
                binding_context="chat-a",
            )

            self.assertEqual(
                [record.public["name"] for record in inventory.records],
                ["global-skill", "review"],
            )
            review = next(
                record for record in inventory.records
                if record.public["name"] == "review"
            )
            self.assertEqual(review.public["source"], "opencode")
            self.assertEqual(review.public["kind"], "skill")
            self.assertEqual(review.public["scope"], "project")
            self.assertEqual(review.public["description"], "Review <path>")
            self.assertEqual(review.native["path"], str(project_path.resolve()))
            self.assertNotIn(str(project_path), json.dumps(inventory.commands))
            self.assertNotIn("undocumented", json.dumps(inventory.commands))

    def test_opencode_omits_duplicates_invalid_metadata_and_symlinked_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            project.mkdir()
            (project / ".git").mkdir()
            home = base / "home"
            home.mkdir()
            self._write_skill(project / ".opencode" / "skills", "duplicate")
            self._write_skill(home / ".agents" / "skills", "duplicate")
            mismatch = self._write_skill(
                project / ".agents" / "skills", "wrong-directory"
            )
            mismatch.rename(mismatch.parent / "renamed.md")
            invalid = project / ".claude" / "skills" / "Bad_Name" / "SKILL.md"
            invalid.parent.mkdir(parents=True)
            invalid.write_text(
                "---\nname: Bad_Name\ndescription: no\n---\n",
                encoding="utf-8",
            )
            outside = self._write_skill(base / "outside", "linked")
            link_parent = project / ".claude" / "skills" / "linked"
            link_parent.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(outside.parent, link_parent)

            inventory = opencode_provider_skill_inventory(
                cwd=str(project),
                home=str(home),
                xdg_config_home=None,
                selector_secret="a" * 64,
                binding_context="chat-a",
            )

            self.assertEqual(inventory.records, ())

    def test_opencode_revision_binds_content_and_validation_rejects_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            project.mkdir()
            (project / ".git").mkdir()
            home = base / "home"
            home.mkdir()
            skill = self._write_skill(project / ".opencode" / "skills", "review")
            kwargs = {
                "cwd": str(project),
                "home": str(home),
                "xdg_config_home": None,
                "selector_secret": "a" * 64,
                "binding_context": "chat-a",
            }
            first = opencode_provider_skill_inventory(**kwargs)
            _name, _directory, content = validate_opencode_provider_skill_record(
                first.records[0]
            )
            self.assertEqual(content, "Body")
            skill.write_text(
                "---\nname: review\ndescription: Changed\n---\nBody\n",
                encoding="utf-8",
            )
            second = opencode_provider_skill_inventory(**kwargs)

            self.assertNotEqual(first.revision, second.revision)
            self.assertNotEqual(first.commands[0]["id"], second.commands[0]["id"])
            with self.assertRaisesRegex(Exception, "changed before launch"):
                validate_opencode_provider_skill_record(first.records[0])

    def test_opencode_resolves_an_admitted_symlinked_cwd_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "real-project"
            project.mkdir()
            (project / ".git").mkdir()
            home = base / "home"
            home.mkdir()
            self._write_skill(project / ".opencode" / "skills", "review")
            alias = base / "project-alias"
            os.symlink(project, alias)

            inventory = opencode_provider_skill_inventory(
                cwd=str(alias),
                home=str(home),
                xdg_config_home=None,
                selector_secret="a" * 64,
                binding_context="chat-a",
            )

            self.assertEqual([item["name"] for item in inventory.commands], ["review"])

    def test_opencode_accepts_bounded_yaml_block_descriptions(self) -> None:
        for indicator in ("|", "|-", "|+", ">", ">-", ">+"):
            with self.subTest(indicator=indicator), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                project = base / "project"
                skill = project / ".opencode" / "skills" / "review" / "SKILL.md"
                skill.parent.mkdir(parents=True)
                (project / ".git").mkdir()
                home = base / "home"
                home.mkdir()
                skill.write_text(
                    "---\nname: review\n"
                    f"description: {indicator}\n"
                    "  Review the current changes.\n"
                    "  Keep findings concise.\n"
                    "---\nBody\n",
                    encoding="utf-8",
                )

                inventory = opencode_provider_skill_inventory(
                    cwd=str(project),
                    home=str(home),
                    xdg_config_home=None,
                    selector_secret="a" * 64,
                    binding_context="chat-a",
                )

                self.assertEqual(len(inventory.records), 1)
                self.assertEqual(
                    inventory.commands[0]["description"],
                    "Review the current changes. Keep findings concise.",
                )

    def test_canonical_names_accept_provider_syntax_but_reject_ambiguous_text(self) -> None:
        for value in ("review", "_private", "plugin:skill", "a.b-c_d"):
            with self.subTest(value=value):
                self.assertEqual(canonical_provider_command_name(value), value)
        for value in ("", "/review", " review", "review ", "a/b", "é", "bad\nname"):
            with self.subTest(value=value):
                self.assertIsNone(canonical_provider_command_name(value))

    def test_display_text_redacts_every_local_path_form_and_format_controls(self) -> None:
        cases = {
            "posix": "Review /Users/private/My Project/skill.md after lunch",
            "home": "Review ~/Secret Folder/config after lunch",
            "relative": "Review ../Secret Folder/config after lunch",
            "drive": r"Review C:\Users\private\My Project\skill.md after lunch",
            "unc": r"Review \\server\share\My Project\skill.md after lunch",
            "file": "Review file:///Users/private/My Project/skill.md after lunch",
            "ssh": "Review ssh://private-host/secret after lunch",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                safe = sanitize_provider_command_text(value, 800)
                self.assertEqual(safe, "Review <url>" if "://" in value else "Review <path>")

        sensitive = sanitize_provider_command_text(
            "Mail private.user@example.test then read "
            "https://alice:secret@example.test/private?token=abc#key",
            800,
        )
        self.assertEqual(sensitive, "Mail <email> then read <url>")
        self.assertNotIn("private.user", sensitive)
        self.assertNotIn("alice", sensitive)
        self.assertNotIn("secret", sensitive)
        self.assertNotIn("token", sensitive)
        self.assertNotIn("#key", sensitive)
        self.assertEqual(
            sanitize_provider_command_text("zero\u200bwidth", 800),
            "zerowidth",
        )

    def test_codex_projection_is_session_bound_opaque_and_keeps_path_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp).resolve())
            private_path = str(Path(tmp, "private", "review", "SKILL.md").resolve())
            value = {
                "data": [
                    {
                        "cwd": cwd,
                        "skills": [
                            {
                                "name": "review",
                                "path": private_path,
                                "description": f"Read {private_path}",
                                "scope": "project",
                                "pluginId": "private-plugin-id",
                                "interface": {
                                    "displayName": "Review changes",
                                    "shortDescription": f"Inspect {private_path}",
                                },
                            },
                            {
                                "name": "disabled",
                                "path": str(Path(tmp, "disabled", "SKILL.md").resolve()),
                                "enabled": False,
                            },
                            {"name": "bad/name", "path": private_path},
                        ],
                    }
                ]
            }
            first = codex_provider_command_inventory(
                value,
                cwd=cwd,
                selector_secret="a" * 64,
                binding_context="session-a",
            )
            same = codex_provider_command_inventory(
                value,
                cwd=cwd,
                selector_secret="a" * 64,
                binding_context="session-a",
            )
            other_session = codex_provider_command_inventory(
                value,
                cwd=cwd,
                selector_secret="a" * 64,
                binding_context="session-b",
            )
            other_secret = codex_provider_command_inventory(
                value,
                cwd=cwd,
                selector_secret="b" * 64,
                binding_context="session-a",
            )

        self.assertEqual(first.revision, same.revision)
        self.assertNotEqual(first.revision, other_session.revision)
        self.assertNotEqual(first.revision, other_secret.revision)
        self.assertEqual(len(first.records), 1)
        public = first.records[0].public
        self.assertRegex(public["id"], r"^pcmd_[0-9a-f]{32}$")
        self.assertEqual(public["invocation"], "/review")
        self.assertEqual(public["kind"], "skill")
        self.assertEqual(public["source"], "plugin")
        self.assertEqual(public["scope"], "project")
        self.assertIn("<path>", public["description"])
        self.assertEqual(first.records[0].native, {
            "name": "review",
            "path": private_path,
        })
        serialized = json.dumps(first.commands)
        self.assertNotIn(private_path, serialized)
        self.assertNotIn("private-plugin-id", serialized)
        self.assertNotEqual(public["id"], other_session.records[0].public["id"])
        self.assertNotEqual(public["id"], other_secret.records[0].public["id"])

    def test_claude_projection_drops_private_top_level_data_and_binds_generation(self) -> None:
        value = {
            "commands": [
                {
                    "name": "_private",
                    "description": "Review /Users/private/project",
                    "argumentHint": "[focus]",
                    "aliases": ["secret-alias"],
                },
                {"name": "plugin:task", "description": "Run task"},
            ],
            "account": {
                "email": "private@example.test",
                "organization": "Private Org",
            },
            "models": [{"id": "private-model"}],
            "pid": 1234,
        }
        first = claude_provider_command_inventory(
            value,
            cwd="/tmp/project",
            selector_secret="a" * 64,
            binding_context="session-a",
            control_generation="claudemcp_generation-a",
        )
        next_generation = claude_provider_command_inventory(
            value,
            cwd="/tmp/project",
            selector_secret="a" * 64,
            binding_context="session-a",
            control_generation="claudemcp_generation-b",
        )

        self.assertEqual([item["name"] for item in first.commands], [
            "_private",
            "plugin:task",
        ])
        # Connection generation is a private launch fence, not semantic
        # inventory identity. Reconnecting with the same commands must not
        # invalidate a durable queued selection.
        self.assertEqual(first.revision, next_generation.revision)
        self.assertEqual(first.records[0].native, {
            "name": "_private",
            "control_generation": "claudemcp_generation-a",
        })
        self.assertEqual(
            next_generation.records[0].native["control_generation"],
            "claudemcp_generation-b",
        )
        serialized = json.dumps(first.commands)
        self.assertNotIn("private@example.test", serialized)
        self.assertNotIn("Private Org", serialized)
        self.assertNotIn("private-model", serialized)
        self.assertNotIn("secret-alias", serialized)
        self.assertNotIn("/Users/private", serialized)
        self.assertIn("<path>", first.commands[0]["description"])

    def test_inventory_is_bounded_and_marks_truncation(self) -> None:
        value = {
            "commands": [
                {"name": f"command-{index}", "description": "ok"}
                for index in range(MAX_PROVIDER_COMMANDS + 10)
            ]
        }

        inventory = claude_provider_command_inventory(
            value,
            cwd="/tmp/project",
            selector_secret="a" * 64,
            binding_context="session-a",
            control_generation="claudemcp_generation-a",
        )

        self.assertEqual(len(inventory.records), MAX_PROVIDER_COMMANDS)
        self.assertTrue(inventory.truncated)
        self.assertTrue(re.fullmatch(r"pcmdrev_[0-9a-f]{32}", inventory.revision))

    def test_revision_is_insensitive_to_provider_display_order(self) -> None:
        commands = [
            {"name": "first", "description": "One"},
            {"name": "second", "description": "Two"},
        ]
        first = claude_provider_command_inventory(
            {"commands": commands},
            cwd="/tmp/project",
            selector_secret="a" * 64,
            binding_context="session-a",
            control_generation="claudemcp_generation-a",
        )
        reversed_inventory = claude_provider_command_inventory(
            {"commands": list(reversed(commands))},
            cwd="/tmp/project",
            selector_secret="a" * 64,
            binding_context="session-a",
            control_generation="claudemcp_generation-b",
        )

        self.assertEqual(first.revision, reversed_inventory.revision)
        self.assertEqual(
            [item["name"] for item in first.commands],
            ["first", "second"],
        )
        self.assertEqual(
            [item["name"] for item in reversed_inventory.commands],
            ["second", "first"],
        )


if __name__ == "__main__":
    unittest.main()
