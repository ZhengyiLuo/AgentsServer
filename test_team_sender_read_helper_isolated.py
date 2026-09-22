"""Pure mocked Team helper reads: no server import, transport, or provider run."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import agentsdock_team as helper


def message(sequence: int, sender: str = "Other") -> dict:
    return {
        "id": f"tmsg_synthetic_{sequence}", "sequence": sequence,
        "sender": {"kind": "server", "id": f"sender_{sender}", "display_name": sender},
    }


def page(messages: list[dict], *, more: bool = False, after: int = 0) -> dict:
    return {
        "team_id": "synthetic-team", "box": "inbox", "messages": messages,
        "next_after_sequence": messages[-1]["sequence"] if messages else after,
        "has_more": more, "notice": "Synthetic content notice",
    }


class TeamSenderReadHelperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(
            helper, "_provider_authority", return_value=("synthetic-capability", "synthetic-chat"),
        ))
        self.enterContext(mock.patch.object(
            helper.urllib.request, "build_opener", side_effect=AssertionError("No live transport"),
        ))
        self.request = self.enterContext(mock.patch.object(helper, "_request_json"))

    def execute(self, *arguments):
        args = helper.parser().parse_args(list(arguments))
        return args.handler(args)

    def queries(self):
        result = []
        for call in self.request.call_args_list:
            self.assertEqual(call.args[0], "GET")
            self.assertEqual(call.args[2], "synthetic-capability")
            self.assertEqual(urlsplit(call.args[1]).path, "/api/agent/team/messages")
            self.assertEqual(len(call.args), 3, "Reads must not submit a payload")
            result.append(parse_qs(urlsplit(call.args[1]).query))
        return result

    def test_mentions_lists_selected_identities_with_one_read_only_request(self):
        response = {"mentions": [
            {"mention_index": 1, "kind": "recipient", "recipient_kind": "server", "display_name": "Dave"},
            {"mention_index": 2, "kind": "recipient", "recipient_kind": "all", "display_name": "Bulletin"},
        ]}
        self.request.return_value = response
        self.assertEqual(self.execute("mentions"), response)
        self.request.assert_called_once_with("GET", "/api/agent/team/mentions", "synthetic-capability")

    def test_mentions_rejects_invalid_response(self):
        for response in ({}, {"mentions": None}, {"mentions": {}}):
            with self.subTest(response=response):
                self.request.return_value = response
                with self.assertRaisesRegex(helper.TeamCLIError, "invalid.*mention list"):
                    self.execute("mentions")

    def test_selected_mention_is_one_server_filtered_page_without_name_matching(self):
        for command in ("inbox", "feed", "bulletin"):
            with self.subTest(command=command):
                self.request.reset_mock()
                response = page([message(43, "Renamed sender")], more=True)
                self.request.return_value = response
                result = self.execute(command, "--mention", "2", "--limit", "7", "--after", "42",
                    "--since", "2026-09-01", "--include-mail-subject")
                self.assertEqual(result, response)
                self.assertNotIn("scan_complete", result)
                self.request.assert_called_once()
                query = self.queries()[0]
                self.assertEqual(query["mention"], ["2"])
                self.assertEqual(query["limit"], ["7"])
                self.assertEqual(query["after_sequence"], ["42"])
                self.assertEqual(query["since"], ["2026-09-01"])
                self.assertEqual(query["include_mail_subject"], ["1"])
                self.assertEqual(query["box"], ["feed" if command == "bulletin" else command])

    def test_selected_mention_and_name_filters_are_mutually_exclusive(self):
        for command in ("inbox", "feed", "bulletin"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.execute(command, "--mention", "1", "--from", "Dave")
        self.request.assert_not_called()

    def test_selected_mention_requires_a_positive_index(self):
        for index in ("0", "-1"):
            with self.subTest(index=index), self.assertRaisesRegex(helper.TeamCLIError, "positive mention index"):
                self.execute("inbox", "--mention", index)
        self.request.assert_not_called()

    def test_sender_match_is_exact_case_insensitive_with_optional_mention_prefix(self):
        messages = [message(1, "David"), message(2, "Dave"), message(3, "DAVE"),
            message(4, "SuperDave"), message(5, "Dave's server")]
        for name in ("Dave", "dAvE", " @@Dave "):
            with self.subTest(name=name):
                self.request.return_value = page(messages)
                result = self.execute("inbox", "--from", name)
                self.assertEqual([item["sequence"] for item in result["messages"]], [2, 3])
                self.assertTrue(result["scan_complete"])
                self.assertFalse(result["has_more"])
                self.assertEqual(result["scan_stop_reason"], "exhausted")
                self.assertEqual(result["next_after_sequence"], 5)
        self.queries()

    def test_sender_read_reaches_beyond_first_page_and_preserves_scope_options(self):
        first = page([message(i) for i in range(1, 51)], more=True)
        second = page([message(51), message(52, "Dave")])
        self.request.side_effect = [first, second]
        result = self.execute("inbox", "--from", "@@Dave", "--unread", "--since", "2026-09-01",
            "--include-mail-subject")
        self.assertEqual(result["messages"], [message(52, "Dave")])
        self.assertEqual(result["scanned_messages"], 52)
        self.assertEqual(result["scanned_pages"], 2)
        self.assertEqual(result["notice"], first["notice"])
        self.assertEqual(result["next_after_sequence"], 52)
        queries = self.queries()
        for query in queries:
            self.assertEqual(query["box"], ["inbox"])
            self.assertEqual(query["limit"], ["50"])
            self.assertEqual(query["unread"], ["1"])
            self.assertEqual(query["since"], ["2026-09-01"])
            self.assertEqual(query["include_mail_subject"], ["1"])
        self.assertNotIn("team", queries[0])
        self.assertEqual(queries[1]["team"], ["synthetic-team"])
        self.assertEqual(queries[1]["after_sequence"], ["50"])

    def test_result_limit_cursor_never_skips_unexamined_matches_in_same_page(self):
        messages = [message(i, "Dave" if i in {3, 8} else "Other") for i in range(1, 11)]

        def respond(_method, path, _capability, **_kwargs):
            query = parse_qs(urlsplit(path).query)
            after = int(query.get("after_sequence", ["0"])[0])
            return page([item for item in messages if item["sequence"] > after], after=after)

        self.request.side_effect = respond
        first = self.execute("inbox", "--from", "Dave", "--limit", "1")
        second = self.execute("inbox", "--from", "Dave", "--limit", "1", "--after", str(first["next_after_sequence"]))
        last = self.execute("inbox", "--from", "Dave", "--limit", "1", "--after", str(second["next_after_sequence"]))
        self.assertEqual([item["sequence"] for item in first["messages"] + second["messages"]], [3, 8])
        self.assertEqual((first["next_after_sequence"], second["next_after_sequence"]), (3, 8))
        for result in (first, second):
            self.assertTrue(result["has_more"])
            self.assertFalse(result["scan_complete"])
            self.assertEqual(result["scan_stop_reason"], "result_limit")
        self.assertEqual(last["messages"], [])
        self.assertEqual(last["next_after_sequence"], 10)
        self.assertTrue(last["scan_complete"])
        self.queries()

    def test_large_requested_limit_is_capped_at_fifty_matching_messages(self):
        self.request.return_value = page([message(i, "Dave") for i in range(1, 51)], more=True)
        result = self.execute("inbox", "--from", "Dave", "--limit", "1000000")
        self.assertEqual(len(result["messages"]), 50)
        self.assertEqual(result["next_after_sequence"], 50)
        self.assertTrue(result["has_more"])
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["scan_stop_reason"], "result_limit")
        self.request.assert_called_once()

    def test_output_budget_bounds_pretty_json_bytes_and_preserves_next_match(self):
        messages = [
            {**message(1, "Dave"), "body": "🙂" * 10000}, message(2),
            {**message(3, "Dave"), "body": "🙂" * 10000}, message(4),
            {**message(5, "Dave"), "body": "🙂" * 10000},
        ]

        def respond(_method, path, _capability, **_kwargs):
            query = parse_qs(urlsplit(path).query)
            after = int(query.get("after_sequence", ["0"])[0])
            remaining = [item for item in messages if item["sequence"] > after]
            return page(remaining[:2], more=len(remaining) > 2, after=after)

        self.request.side_effect = respond
        first = self.execute("inbox", "--from", "Dave")
        second = self.execute("inbox", "--from", "Dave", "--after", str(first["next_after_sequence"]))
        self.assertEqual([item["sequence"] for item in first["messages"]], [1, 3])
        self.assertEqual(first["next_after_sequence"], 4)
        self.assertEqual(first["scanned_messages"], 4)
        self.assertEqual(first["scan_stop_reason"], "output_limit")
        self.assertTrue(first["has_more"])
        self.assertFalse(first["scan_complete"])
        self.assertEqual([item["sequence"] for item in second["messages"]], [5])
        self.assertTrue(second["scan_complete"])
        for result in (first, second):
            printed = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            self.assertLessEqual(len(printed.encode("utf-8")), helper.SENDER_SCAN_OUTPUT_MAX_BYTES)
            self.assertLess(len(printed.encode("utf-8")), 128 * 1024)
        self.queries()

    def test_output_budget_counts_response_metadata_and_formatting(self):
        self.request.return_value = {
            **page([{**message(i, "Dave"), "body": "x" * 400} for i in (1, 2)]),
            "notice": "N" * 3000,
        }
        with mock.patch.object(helper, "SENDER_SCAN_OUTPUT_MAX_BYTES", 4096):
            result = self.execute("inbox", "--from", "Dave")
        self.assertEqual([item["sequence"] for item in result["messages"]], [1])
        self.assertEqual(result["next_after_sequence"], 1)
        self.assertEqual(result["scan_stop_reason"], "output_limit")
        self.assertLessEqual(len((json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")), 4096)

    def test_individually_oversized_message_fails_honestly_even_after_prior_matches(self):
        oversized = {**message(2, "Dave"), "body": "x" * helper.SENDER_SCAN_OUTPUT_MAX_BYTES}
        for preceding in ([], [message(1, "Dave")]):
            with self.subTest(preceding=preceding):
                self.request.reset_mock()
                self.request.return_value = page([*preceding, oversized])
                with self.assertRaisesRegex(helper.TeamCLIError, "matching message exceeds.*output limit"):
                    self.execute("inbox", "--from", "Dave")
                self.request.assert_called_once()

    def test_oversized_response_metadata_is_an_error_even_without_matches(self):
        self.request.return_value = {
            **page([message(1)]), "notice": "N" * helper.SENDER_SCAN_OUTPUT_MAX_BYTES,
        }
        with self.assertRaisesRegex(helper.TeamCLIError, "metadata exceeds.*output limit"):
            self.execute("inbox", "--from", "Dave")

    def test_byte_shortened_page_uses_has_more_instead_of_assuming_end(self):
        self.request.side_effect = [page([message(1)], more=True), page([message(9, "Dave")])]
        result = self.execute("bulletin", "--from", "Dave")
        self.assertEqual(result["messages"], [message(9, "Dave")])
        self.assertEqual(result["next_after_sequence"], 9)
        self.assertEqual([query["box"] for query in self.queries()], [["feed"], ["feed"]])

    def test_page_bound_reports_incomplete_even_when_no_matches_found(self):
        self.request.side_effect = [page([message(i) for i in range(1, 51)], more=True),
            page([message(i) for i in range(51, 101)], more=True)]
        with mock.patch.object(helper, "SENDER_SCAN_MAX_PAGES", 2):
            result = self.execute("inbox", "--from", "Dave")
        self.assertEqual(result["messages"], [])
        self.assertTrue(result["has_more"])
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["scan_stop_reason"], "page_limit")
        self.assertEqual(result["next_after_sequence"], 100)
        self.assertEqual(self.request.call_count, 2)

    def test_time_bound_preserves_last_scanned_cursor_and_does_not_poll(self):
        self.request.return_value = page([message(17)], more=True)
        with mock.patch.object(helper.time, "monotonic", side_effect=[100.0, 100.0, 146.0]):
            result = self.execute("inbox", "--from", "Dave")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["scan_stop_reason"], "time_limit")
        self.assertEqual(result["next_after_sequence"], 17)
        self.assertFalse(result["scan_complete"])
        self.assertTrue(result["has_more"])
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.kwargs["timeout"], 45.0)

    def test_later_requests_use_remaining_scan_time_budget(self):
        self.request.side_effect = [page([message(1)], more=True), page([message(2, "Dave")])]
        with mock.patch.object(helper.time, "monotonic", side_effect=[0.0, 5.0, 15.0]):
            self.execute("inbox", "--from", "Dave")
        self.assertEqual([call.kwargs["timeout"] for call in self.request.call_args_list], [40.0, 30.0])

    def test_invalid_or_nonadvancing_pages_fail_without_empty_success(self):
        malformed = [
            {"messages": None}, {"messages": ["not a message"]},
            {"next_after_sequence": None}, {"next_after_sequence": 3},
            {"next_after_sequence": True}, {"has_more": None}, {"has_more": "false"},
            {"messages": [message(2), message(1)], "next_after_sequence": 1},
            {"messages": [{**message(2), "sender": None}]},
            {"messages": [{**message(2), "sequence": True}], "next_after_sequence": True},
            {"messages": [], "next_after_sequence": 0, "has_more": True},
            {"messages": [message(0)], "next_after_sequence": 0},
        ]
        for fields in malformed:
            with self.subTest(fields=fields):
                self.request.return_value = {**page([message(2)], more=True), **fields}
                with self.assertRaisesRegex(helper.TeamCLIError, "invalid|nonadvancing"):
                    self.execute("inbox", "--from", "Dave")

    def test_team_cannot_change_between_pages(self):
        self.request.side_effect = [page([message(1)], more=True),
            {**page([message(2, "Dave")]), "team_id": "different-team"}]
        with self.assertRaisesRegex(helper.TeamCLIError, "changed teams"):
            self.execute("inbox", "--from", "Dave")

    def test_empty_sender_or_invalid_limit_fails_before_request(self):
        for arguments in (("--from", ""), ("--from", "@@"), ("--from", "  "),
            ("--from", "Dave", "--limit", "0")):
            with self.subTest(arguments=arguments), self.assertRaises(helper.TeamCLIError):
                self.execute("inbox", *arguments)
        self.request.assert_not_called()

    def test_unfiltered_commands_remain_single_page_and_bulletin_alias_uses_feed(self):
        for command in ("inbox", "sent", "feed", "bulletin"):
            with self.subTest(command=command):
                self.request.reset_mock()
                response = {"messages": [], "notice": "Unmodified legacy page"}
                self.request.return_value = copy.deepcopy(response)
                self.assertEqual(self.execute(command, "--limit", "7", "--after", "42",
                    "--team", "chosen-team"), response)
                self.request.assert_called_once()
                query = self.queries()[0]
                self.assertEqual(query["box"], ["feed" if command == "bulletin" else command])
                self.assertEqual(query["limit"], ["7"])
                self.assertEqual(query["after_sequence"], ["42"])
                self.assertEqual(query["team"], ["chosen-team"])

    def test_empty_exhausted_page_preserves_explicit_start_cursor(self):
        self.request.return_value = page([], after=42)
        result = self.execute("feed", "--from", "Dave", "--after", "42")
        self.assertEqual(result["next_after_sequence"], 42)
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["scanned_messages"], 0)
        self.assertEqual(result["scanned_pages"], 1)


if __name__ == "__main__":
    unittest.main()
