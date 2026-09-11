"""Full-source import matching; only AST-selected helpers, never server startup."""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock

from claude_history_repair import _text_key

from test_claude_history_metadata_isolated import load_projection as claude_projection, user_event
from test_codex_history_metadata_isolated import projection as codex_projection, assistant
from test_codex_goal_history_isolated import source_user


class FullTextHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.claude = claude_projection()
        cls.codex = codex_projection()
        for namespace in (cls.claude, cls.codex):
            namespace["MAX_IMPORTED_TEXT_CHARS"] = 12_000

    def parse(self, backend, texts, role="user"):
        if backend == "claude":
            events = [user_event(text) if role == "user" else {
                "type": "assistant", "message": {"content": text},
            } for text in texts]
        else:
            events = [source_user(text) if role == "user" else assistant(text) for text in texts]
        namespace = getattr(self, backend)
        return namespace[f"parse_{backend}_history_events"](events, 400)

    def test_both_providers_capture_full_user_and_assistant_text_before_compaction(self):
        text = "Scheduled monitor\n" + "full instruction " * 1_500 + "unique end"
        expected = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
        for backend in ("claude", "codex"):
            for role in ("user", "assistant"):
                with self.subTest(backend=backend, role=role):
                    item = self.parse(backend, [text], role)[0]
                    self.assertLessEqual(len(item["text"]), 12_018)
                    self.assertEqual(item["source_text_sha256"], expected)
                    self.assertEqual(self.codex["history_dedup_key"](
                        role, item["text"], source_text_sha256=item["source_text_sha256"],
                    ), self.codex["history_dedup_key"](role, text))

    def test_common_display_prefix_does_not_collapse_distinct_source_messages(self):
        prefix = "shared " * 2_000
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                items = self.parse(backend, [prefix + "first ending", prefix + "second ending"])
                self.assertEqual(len(items), 2)
                self.assertEqual(items[0]["text"], items[1]["text"])
                self.assertNotEqual(items[0]["source_text_sha256"], items[1]["source_text_sha256"])

    def test_escaped_lone_surrogates_remain_lossless_and_distinct(self):
        # Escaped lone surrogates are legal JSON payloads. A fingerprint must
        # neither crash history reconciliation nor replace distinct content.
        texts = ["prefix \ud800", "prefix \ud801", "prefix \ufffd"]
        for backend in ("claude", "codex"):
            for role in ("user", "assistant"):
                with self.subTest(backend=backend, role=role):
                    items = self.parse(backend, texts, role)
                    self.assertEqual(len(items), 3)
                    keys = [getattr(self, backend)["history_dedup_key"](
                        role, item["text"],
                    )[1] for item in items]
                    self.assertEqual(len(set(keys)), 3)
                    self.assertEqual(keys, [hashlib.sha256(
                        text.encode("utf-8", errors="surrogatepass")
                    ).hexdigest() for text in texts])
                    self.assertEqual(keys, [_text_key(text) for text in texts])
                    digests = [getattr(self, backend)["history_item_cursor_digest"](item)
                               for item in items]
                    self.assertEqual(len(set(digests)), 3)

    def test_ordered_timeline_credit_consumes_only_one_exact_full_source(self):
        scheduled = "monitor " * 2_000 + "scheduled ending"
        genuine = "monitor " * 2_000 + "genuine different ending"
        items = [self.parse("claude", [text])[0] for text in (genuine, scheduled, scheduled)]
        self.codex["history_timeline_message_keys"] = Mock(return_value=(
            [(2, self.codex["history_dedup_key"]("user", scheduled))], True, False,
        ))
        fresh, consumed = self.codex["reconcile_cursor_history_items"](
            "fixture", items, timeline_after_seq=1, timeline_through_seq=2,
        )
        self.assertEqual(fresh, [items[0], items[2]])
        self.assertEqual(consumed, 2)

    def test_delta_uses_full_source_proof_without_changing_legacy_digest(self):
        first, second = ["common " * 2_000 + ending for ending in ("first", "second")]
        for backend in ("claude", "codex"):
            namespace = getattr(self, backend)
            previous = self.parse(backend, [first])[0]
            legacy_digest = namespace["history_item_cursor_digest"]({"kind": "user", "text": previous["text"]})
            self.assertEqual(namespace["history_item_cursor_digest"](previous), legacy_digest)
            for text, context, count in (
                (first, {"sha256": previous["source_text_sha256"]}, 0),
                (second, {"sha256": previous["source_text_sha256"]}, 1),
                (first, {}, 1),
            ):
                with self.subTest(backend=backend, known_source=bool(context), changed=text != first):
                    event = user_event(text) if backend == "claude" else source_user(text)
                    namespace["bounded_jsonl_records_range"] = Mock(return_value=iter([(event, 10)]))
                    items, offset, digest, overflow = namespace["parse_provider_history_delta"](
                        Path("unused"), backend, 0, 10, limit=400, expected_stat={},
                        previous_last_item_digest=legacy_digest, source_text_context=context,
                    )
                    self.assertEqual(len(items), count)
                    self.assertEqual((offset, digest, overflow), (10, legacy_digest, False))


if __name__ == "__main__":
    unittest.main()
