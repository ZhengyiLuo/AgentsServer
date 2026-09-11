"""AST-isolated existing-Host guards; never import or start AgentsServer."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
import unicodedata
import uuid
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock
from pydantic import BaseModel, Field, ValidationError, field_validator


def load_guards():
    source = Path(__file__).with_name("agent_server.py")
    parsed = ast.parse(source.read_text())
    selected = {"TeamHubHostControlFailure", "TeamHubHostEnableRequest", "canonical_server_display_name",
        "enable_team_hub_host", "disable_team_hub_host", "team_hub_host_control_capability"}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *[node for node in parsed.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected]], type_ignores=[])
    namespace = {"BaseModel": BaseModel, "Field": Field, "field_validator": field_validator,
        "Any": Any, "Literal": Literal, "uuid": uuid, "unicodedata": unicodedata,
        "SERVER_DISPLAY_NAME_MAX_BYTES": 160, "SERVER_DISPLAY_NAME_ERROR": "Invalid server name",
        "TEAM_HUB_HOST_CONTROL_LOCK": asyncio.Lock(), "TEAM_HUB_RUNTIME": SimpleNamespace(designated_host=True),
        "AGENT_TOKEN": "isolated-test-token", "require_team_hub_host_control_target": Mock(),
        "reconcile_pending_team_hub_host_control": AsyncMock(side_effect=RuntimeError("existing host path reached")),
        "read_team_hub_host_control_status": Mock(side_effect=AssertionError("unexpected journal access"))}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    namespace["TeamHubHostEnableRequest"].model_rebuild(_types_namespace=namespace)
    return namespace


class ExistingHostRenameGuardTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **extra):
        return SimpleNamespace(request_id="synthetic-request", require_existing_host=True, network_name=None, **extra)

    def test_existing_host_field_accepts_only_json_booleans_with_legacy_false_default(self):
        model = load_guards()["TeamHubHostEnableRequest"]
        body = {"request_id": str(uuid.uuid4()), "expected_server_identity": "server-synthetic",
            "expected_server_instance_id": "instance-synthetic", "confirmed": True, "server_name": "Synthetic host"}
        self.assertFalse(model(**body).require_existing_host)
        for value in (False, True):
            self.assertIs(model(**body, require_existing_host=value).require_existing_host, value)
        for value in ("true", "false", 1, 0, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                model(**body, require_existing_host=value)

    async def test_nonhost_guard_is_inside_lock_and_precedes_reconciliation_or_journal_access(self):
        scope = load_guards()
        scope["TEAM_HUB_RUNTIME"].designated_host = False
        scope["require_team_hub_host_control_target"].side_effect = lambda _body: self.assertTrue(scope["TEAM_HUB_HOST_CONTROL_LOCK"].locked())
        with self.assertRaises(scope["TeamHubHostControlFailure"]) as caught:
            await scope["enable_team_hub_host"](self.request())
        self.assertEqual(caught.exception.status_code, 409)
        scope["reconcile_pending_team_hub_host_control"].assert_not_called()
        scope["read_team_hub_host_control_status"].assert_not_called()

    async def test_rename_never_creates_a_team_or_disables_a_host(self):
        scope = load_guards()
        body = self.request()
        body.network_name = "Other team"
        with self.assertRaises(scope["TeamHubHostControlFailure"]) as caught:
            await scope["enable_team_hub_host"](body)
        self.assertEqual(caught.exception.status_code, 422)
        with self.assertRaises(scope["TeamHubHostControlFailure"]):
            await scope["disable_team_hub_host"](self.request())
        scope["reconcile_pending_team_hub_host_control"].assert_not_called()

    async def test_valid_intent_reaches_existing_path_but_reconciled_role_change_is_rejected(self):
        scope = load_guards()
        with self.assertRaisesRegex(RuntimeError, "existing host path reached"):
            await scope["enable_team_hub_host"](self.request())
        async def change_role():
            scope["TEAM_HUB_RUNTIME"].designated_host = False
        scope["reconcile_pending_team_hub_host_control"] = change_role
        with self.assertRaises(scope["TeamHubHostControlFailure"]) as caught:
            await scope["enable_team_hub_host"](self.request())
        self.assertEqual(caught.exception.status_code, 409)
        scope["read_team_hub_host_control_status"].assert_not_called()
        self.assertTrue(scope["team_hub_host_control_capability"]()["rename_existing_host"])


if __name__ == "__main__":
    unittest.main()
