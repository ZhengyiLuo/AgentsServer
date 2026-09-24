"""Exact user mention capture without importing the server or creating authority files."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
import unittest

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator


def extracted():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
    names = {
        "TeamReference", "team_reference_dicts", "validate_team_references",
        "team_reference_marker", "utf16_length", "utf16_slice",
        "chat_reference_has_token_boundaries",
    }
    selected = [node for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = {
        "__name__": __name__, "Any": Any, "Literal": Literal,
        "BaseModel": BaseModel, "Field": Field, "model_validator": model_validator,
        "HTTPException": HTTPException,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, *selected], type_ignores=[])),
                 "<isolated-team-mention-validation>", "exec"), namespace)
    namespace["TeamReference"].model_rebuild(_types_namespace=namespace)
    issuer = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "issue_cross_chat_capability")
    filter_index = next(index for index, node in enumerate(issuer.body)
                        if isinstance(node, ast.If)
                        and ast.unparse(node.test) == "team_mail_route_snapshot is not None")
    capture = ast.Module(body=issuer.body[:filter_index + 1], type_ignores=[])
    record = next(node.value for node in ast.walk(issuer)
                  if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                  and any(isinstance(target, ast.Name) and target.id == "capability_record"
                          for target in node.targets))
    stored_mentions = next(value for key, value in zip(record.keys, record.values)
                           if isinstance(key, ast.Constant) and key.value == "team_read_mentions")
    payload = next(node.value for node in issuer.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "payload"
                           for target in node.targets))
    return namespace, capture, stored_mentions, payload


class TeamMentionCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace, cls.capture, cls.stored_mentions, cls.payload = extracted()

    def capture_mentions(self, *, count=1, token=True, read=True, actions=None, **overrides):
        prompt = " ".join("@@Pat" for _ in range(count))
        references = [{
            "kind": "recipient", "recipient_kind": "server", "team_id": "team-private",
            "target_id": f"node-private-{index}", "display_name_snapshot": "Pat",
            "source_text_start": index * 6, "source_text_end": index * 6 + 5,
            "grant_intent": True, **overrides,
        } for index in range(count)]
        scope = {
            **self.namespace, "source_user_instruction": prompt, "references": [],
            "team_references": references, "AGENT_TOKEN": token, "team_read_enabled": read,
            "team_mail_route_snapshot": [],
            "team_mail_grants": SimpleNamespace(snapshot=lambda _: []),
            "effective_actions": {"team_read"} if actions is None else actions,
        }
        exec(compile(self.capture, "<isolated-issuer-mention-capture>", "exec"), scope)
        stored = eval(compile(ast.Expression(self.stored_mentions),
                              "<isolated-issuer-mention-record>", "eval"), scope)
        return scope, stored

    def test_revoked_send_grant_does_not_remove_current_exact_read_selection(self):
        scope, stored = self.capture_mentions(count=2)
        self.assertEqual(scope["validated_team_references"], [])
        self.assertEqual([reference["target_id"] for reference in stored],
                         ["node-private-0", "node-private-1"])
        self.assertEqual([reference["display_name_snapshot"] for reference in stored], ["Pat", "Pat"])
        scope["team_references"][0]["target_id"] = "mutated-input"
        self.assertEqual(stored[0]["target_id"], "node-private-0")

    def test_capture_and_storage_require_existing_read_authority(self):
        for options in ({"token": False}, {"read": False}, {"actions": {"team_send"}}):
            with self.subTest(options=options):
                _scope, stored = self.capture_mentions(**options)
                self.assertEqual(stored, [])

    def test_private_mention_snapshot_is_bounded(self):
        _scope, stored = self.capture_mentions(count=17)
        self.assertEqual(len(stored), 16)

    def test_false_grant_or_mismatched_visible_token_cannot_enter_read_snapshot(self):
        for overrides in ({"grant_intent": False}, {"display_name_snapshot": "Different"},
                          {"source_text_start": 1}, {"recipient_kind": "external"}):
            with self.subTest(overrides=overrides), self.assertRaises(HTTPException):
                self.capture_mentions(**overrides)

    def test_private_mention_state_is_absent_from_authority_file_payload(self):
        names = {node.id for node in ast.walk(self.payload) if isinstance(node, ast.Name)}
        values = {node.value for node in ast.walk(self.payload)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for private in ("team_read_mentions", "team_references", "team_id", "target_id"):
            self.assertNotIn(private, names | values)


if __name__ == "__main__":
    unittest.main()
