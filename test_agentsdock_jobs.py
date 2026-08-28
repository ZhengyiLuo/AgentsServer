import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agentsdock_jobs


class AgentsDockJobsCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.authority_path = Path(self.temporary.name) / "authority.json"
        self.authority_path.write_text(json.dumps({
            "provider_capability": "provider-token",
            "source_session_id": "sess/chat",
        }))
        self.authority_path.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        return {
            "AGENTSDOCK_SERVER_URL": "http://127.0.0.1:17850",
            "AGENTSDOCK_CHAT_ID": "sess/chat",
            "AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(self.authority_path),
        }

    def test_list_uses_chat_scoped_endpoint_and_returns_current_jobs(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_1", "session_id": "sess/chat", "schedule_kind": "cron"}]}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            result = agentsdock_jobs.command_list(argparse.Namespace())

        self.assertEqual(calls, [("GET", "/api/agent/sessions/sess%2Fchat/jobs", None)])
        self.assertEqual(result["jobs"][0]["id"], "job_1")

    def test_main_without_chat_id_preserves_environment_fallback(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = agentsdock_jobs.main(["list"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("GET", "/api/agent/sessions/sess%2Fchat/jobs", None)])
        self.assertIn('"job_1"', output.getvalue())

    def test_get_returns_one_owned_job_without_a_mutation(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            result = agentsdock_jobs.command_get(
                parser.parse_args(["get", "job_1"]),
            )

        self.assertEqual(result["job"]["id"], "job_1")
        self.assertEqual(
            calls,
            [("GET", "/api/agent/sessions/sess%2Fchat/jobs", None)],
        )

    def test_all_job_projections_strip_raw_chat_references(self) -> None:
        secret_target_id = "internal-target-session-secret"
        raw_job = {
            "id": "job_1",
            "session_id": "sess/chat",
            "schedule_kind": "interval",
            "chat_references": [{
                "session_id": secret_target_id,
                "display_title_snapshot": "Mobile",
                "action": "instruction",
            }],
        }

        def request(method: str, _path: str, payload=None):
            if method == "GET":
                return {"jobs": [raw_job]}
            return {"job": raw_job}

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            projections = [
                agentsdock_jobs.command_list(argparse.Namespace())["jobs"][0],
                agentsdock_jobs.command_get(
                    parser.parse_args(["get", "job_1"]),
                )["job"],
                agentsdock_jobs.command_create(parser.parse_args([
                    "create",
                    "--title", "Create",
                    "--prompt", "Run",
                    "--interval-seconds", "60",
                ]))["job"],
                agentsdock_jobs.command_update(parser.parse_args([
                    "update",
                    "job_1",
                    "--title", "Rename",
                ]))["job"],
            ]

        for projection in projections:
            with self.subTest(projection=projection):
                self.assertNotIn("chat_references", projection)
                self.assertNotIn(secret_target_id, json.dumps(projection))
                self.assertEqual(projection["chat_target_count"], 1)

    def test_runs_reads_scoped_status_history(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if path.endswith("/jobs"):
                return {"jobs": [{"id": "job 1", "session_id": "sess/chat"}]}
            return {
                "session_id": "sess/chat",
                "job_id": "job 1",
                "runs": [{"job_status": "completed"}],
                "total": 1,
            }

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            result = agentsdock_jobs.command_runs(
                parser.parse_args([
                    "runs",
                    "job 1",
                    "--before-seq",
                    "42",
                    "--limit",
                    "5",
                ]),
            )

        self.assertEqual(result["runs"][0]["job_status"], "completed")
        self.assertEqual(calls, [
            ("GET", "/api/agent/sessions/sess%2Fchat/jobs", None),
            (
                "GET",
                "/api/agent/sessions/sess%2Fchat/jobs/job%201/runs?before_seq=42&limit=5",
                None,
            ),
        ])

    def test_runs_rejects_foreign_history_response(self) -> None:
        def request(_method: str, path: str, payload=None):
            if path.endswith("/jobs"):
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}
            return {
                "session_id": "other/chat",
                "job_id": "job_1",
                "runs": [],
            }

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            self.assertRaisesRegex(
                agentsdock_jobs.JobsCLIError,
                "outside the active chat scope",
            ),
        ):
            agentsdock_jobs.command_runs(parser.parse_args(["runs", "job_1"]))

    def test_runs_rejects_private_chat_target_fields(self) -> None:
        def request(_method: str, path: str, payload=None):
            if path.endswith("/jobs"):
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}
            return {
                "session_id": "sess/chat",
                "job_id": "job_1",
                "runs": [{
                    "job_status": "running",
                    "job": {
                        "chat_references": [{
                            "session_id": "private-target-id",
                        }],
                    },
                }],
            }

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            self.assertRaisesRegex(
                agentsdock_jobs.JobsCLIError,
                "private chat target data",
            ),
        ):
            agentsdock_jobs.command_runs(parser.parse_args(["runs", "job_1"]))

    def test_runs_rejects_private_cross_chat_effect_ids(self) -> None:
        def request(_method: str, path: str, payload=None):
            if path.endswith("/jobs"):
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}
            return {
                "session_id": "sess/chat",
                "job_id": "job_1",
                "runs": [{
                    "job_status": "running",
                    "cross_chat_direct_message_ids": [
                        "handoff_private_delivery",
                    ],
                    "delivery": {"id": "leg_private_delivery"},
                }],
            }

        parser = agentsdock_jobs.build_parser()
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            self.assertRaisesRegex(
                agentsdock_jobs.JobsCLIError,
                "private chat target data",
            ),
        ):
            agentsdock_jobs.command_runs(parser.parse_args(["runs", "job_1"]))

    def test_global_chat_id_cannot_override_authority_scope(self) -> None:
        calls: list[tuple[str, str, object]] = []
        environment = self.environment()

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_2", "session_id": "sess/chat"}]}

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            redirect_stdout(io.StringIO()),
        ):
            result = agentsdock_jobs.main(["--chat-id", " other/chat ", "list"])
            self.assertEqual(os.environ["AGENTSDOCK_CHAT_ID"], "sess/chat")

        self.assertEqual(result, 1)
        self.assertEqual(calls, [])

    def test_global_chat_id_supplies_missing_environment_scope(self) -> None:
        calls: list[tuple[str, str, object]] = []
        environment = self.environment()
        del environment["AGENTSDOCK_CHAT_ID"]

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_2", "session_id": "sess/chat"}]}

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
            redirect_stdout(io.StringIO()),
        ):
            result = agentsdock_jobs.main(["--chat-id", "sess/chat", "list"])
            self.assertNotIn("AGENTSDOCK_CHAT_ID", os.environ)

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("GET", "/api/agent/sessions/sess%2Fchat/jobs", None)])

    def test_global_chat_id_rejects_empty_values(self) -> None:
        parser = agentsdock_jobs.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--chat-id", "  ", "list"])

    def test_create_cron_sends_expression_and_timezone_without_session_body(self) -> None:
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args([
            "create",
            "--title", "Morning status",
            "--prompt", "Report status",
            "--cron", "0 9 * * MON-FRI",
            "--timezone", "America/Los_Angeles",
        ])
        seen: dict[str, object] = {}

        def request(method: str, path: str, payload=None):
            seen.update(method=method, path=path, payload=payload)
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            result = args.handler(args)

        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/api/agent/sessions/sess%2Fchat/jobs")
        payload = seen["payload"]
        self.assertNotIn("session_id", payload)
        self.assertEqual(payload["schedule_kind"], "cron")
        self.assertEqual(payload["cron_expression"], "0 9 * * MON-FRI")
        self.assertEqual(payload["timezone"], "America/Los_Angeles")
        self.assertEqual(payload["context_mode"], "chat")
        self.assertEqual(result["job"]["id"], "job_1")

    def test_create_chat_route_defaults_to_instruction(self) -> None:
        route_id = "route_0123456789abcdef0123456789abcdef"
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args([
            "create",
            "--title", "Route report",
            "--prompt", "Send the report to @Mobile",
            "--cron", "0 9 * * *",
            "--chat-route", route_id,
        ])
        seen: dict[str, object] = {}

        def request(method: str, path: str, payload=None):
            seen.update(method=method, path=path, payload=payload)
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            args.handler(args)

        self.assertEqual(seen["payload"]["chat_routes"], [{
            "route_id": route_id,
            "action": "instruction",
        }])

    def test_create_chat_route_supports_request_reply(self) -> None:
        route_id = "route_fedcba9876543210fedcba9876543210"
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args([
            "create",
            "--title", "Route question",
            "--prompt", "Ask @Mobile for an answer",
            "--cron", "0 9 * * *",
            "--chat-route", f"{route_id}=request_reply",
        ])
        seen: dict[str, object] = {}

        def request(method: str, path: str, payload=None):
            seen.update(method=method, path=path, payload=payload)
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            args.handler(args)

        self.assertEqual(seen["payload"]["chat_routes"], [{
            "route_id": route_id,
            "action": "request_reply",
        }])

    def test_update_chat_route_replaces_persisted_routes(self) -> None:
        route_id = "route_11111111111111111111111111111111"
        parser = agentsdock_jobs.build_parser()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{
                    "id": "job_1",
                    "session_id": "sess/chat",
                    "schedule_kind": "cron",
                }]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            agentsdock_jobs.command_update(parser.parse_args([
                "update",
                "job_1",
                "--chat-route", f"{route_id}=request_reply",
            ]))

        self.assertEqual(calls[-1][0], "PATCH")
        self.assertEqual(calls[-1][2], {"chat_routes": [{
            "route_id": route_id,
            "action": "request_reply",
        }]})

    def test_update_can_clear_persisted_chat_routes(self) -> None:
        parser = agentsdock_jobs.build_parser()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{
                    "id": "job_1",
                    "session_id": "sess/chat",
                    "schedule_kind": "cron",
                }]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            agentsdock_jobs.command_update(parser.parse_args([
                "update",
                "job_1",
                "--clear-chat-routes",
            ]))

        self.assertEqual(calls[-1][0], "PATCH")
        self.assertEqual(calls[-1][2], {"chat_routes": []})

    def test_chat_route_rejects_raw_ids_nonopaque_ids_and_invalid_actions(self) -> None:
        parser = agentsdock_jobs.build_parser()
        invalid_routes = (
            "sess/mobile",
            "route_too_short",
            "route_0123456789abcdef0123456789abcdef=final_result",
        )

        for invalid_route in invalid_routes:
            with self.subTest(chat_route=invalid_route), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parser.parse_args([
                    "create",
                    "--title", "Bad route",
                    "--prompt", "Do work",
                    "--cron", "0 9 * * *",
                    "--chat-route", invalid_route,
                ])

    def test_create_and_update_support_standalone_context(self) -> None:
        parser = agentsdock_jobs.build_parser()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {
                    "jobs": [{
                        "id": "job_1",
                        "session_id": "sess/chat",
                        "schedule_kind": "interval",
                    }]
                }
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            agentsdock_jobs.command_create(parser.parse_args([
                "create",
                "--title", "Fresh report",
                "--prompt", "Report",
                "--interval-seconds", "3600",
                "--context-mode", "standalone",
            ]))
            self.assertEqual(calls[-1][2]["context_mode"], "standalone")

            agentsdock_jobs.command_update(parser.parse_args([
                "update",
                "job_1",
                "--context-mode", "chat",
            ]))
            self.assertEqual(calls[-1][2], {"context_mode": "chat"})

    def test_create_and_update_accept_cursor_backend(self) -> None:
        parser = agentsdock_jobs.build_parser()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(agentsdock_jobs, "api_request", request),
        ):
            agentsdock_jobs.command_create(parser.parse_args([
                "create",
                "--title", "Cursor report",
                "--prompt", "Report",
                "--interval-seconds", "3600",
                "--backend", "cursor",
            ]))
            self.assertEqual(calls[-1][2]["backend"], "cursor")

            agentsdock_jobs.command_update(parser.parse_args([
                "update",
                "job_1",
                "--backend", "cursor",
            ]))
            self.assertEqual(calls[-1][2], {"backend": "cursor"})

    def test_update_rrule_uses_scoped_endpoint_and_sets_kind(self) -> None:
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args([
            "update", "job_1",
            "--rrule", "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8",
            "--timezone", "Europe/London",
        ])
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat"}]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            args.handler(args)

        method, path, payload = calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "/api/agent/sessions/sess%2Fchat/jobs/job_1")
        self.assertEqual(payload["schedule_kind"], "rrule")
        self.assertEqual(payload["rrule"], "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8")
        self.assertEqual(payload["timezone"], "Europe/London")

    def test_create_schedule_options_are_mutually_exclusive(self) -> None:
        parser = agentsdock_jobs.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "create", "--title", "Bad", "--prompt", "Bad",
                "--cron", "0 9 * * *", "--rrule", "FREQ=DAILY",
            ])

    def test_one_time_job_rejects_repeating_options_without_a_schedule(self) -> None:
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args([
            "create", "--title", "Bad", "--prompt", "Bad",
            "--first-run-at", "2026-07-22T09:00:00Z", "--loop",
        ])
        with patch.dict(os.environ, self.environment(), clear=True), self.assertRaisesRegex(agentsdock_jobs.JobsCLIError, "cannot loop"):
            args.handler(args)

    def test_update_can_clear_a_finite_run_limit(self) -> None:
        parser = agentsdock_jobs.build_parser()
        args = parser.parse_args(["update", "job_1", "--unlimited"])
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat", "schedule_kind": "rrule"}]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            args.handler(args)
        self.assertEqual(calls[-1][2], {"max_runs": None})

    def test_interval_run_limit_flags_set_the_loop_mode(self) -> None:
        parser = agentsdock_jobs.build_parser()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return {"jobs": [{"id": "job_1", "session_id": "sess/chat", "schedule_kind": "interval"}]}
            return {"job": {"id": "job_1", "session_id": "sess/chat"}}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            agentsdock_jobs.command_update(parser.parse_args(["update", "job_1", "--max-runs", "4"]))
            self.assertEqual(calls[-1][2], {"max_runs": 4, "loop": False})
            agentsdock_jobs.command_update(parser.parse_args(["update", "job_1", "--unlimited"]))
            self.assertEqual(calls[-1][2], {"max_runs": None, "loop": True})


if __name__ == "__main__":
    unittest.main()
