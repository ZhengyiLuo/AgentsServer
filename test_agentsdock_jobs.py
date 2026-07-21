import argparse
import os
import unittest
from unittest.mock import patch

import agentsdock_jobs


class AgentsDockJobsCLITests(unittest.TestCase):
    def environment(self) -> dict[str, str]:
        return {
            "AGENTSDOCK_SERVER_URL": "http://127.0.0.1:17850",
            "AGENTSDOCK_CHAT_ID": "sess/chat",
            "AGENTSDOCK_AGENT_TOKEN": "token",
        }

    def test_list_uses_chat_scoped_endpoint_and_returns_current_jobs(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            return {"jobs": [{"id": "job_1", "session_id": "sess/chat", "schedule_kind": "cron"}]}

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(agentsdock_jobs, "api_request", request):
            result = agentsdock_jobs.command_list(argparse.Namespace())

        self.assertEqual(calls, [("GET", "/api/sessions/sess%2Fchat/jobs", None)])
        self.assertEqual(result["jobs"][0]["id"], "job_1")

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
        self.assertEqual(seen["path"], "/api/sessions/sess%2Fchat/jobs")
        payload = seen["payload"]
        self.assertNotIn("session_id", payload)
        self.assertEqual(payload["schedule_kind"], "cron")
        self.assertEqual(payload["cron_expression"], "0 9 * * MON-FRI")
        self.assertEqual(payload["timezone"], "America/Los_Angeles")
        self.assertEqual(result["job"]["id"], "job_1")

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
        self.assertEqual(path, "/api/sessions/sess%2Fchat/jobs/job_1")
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
