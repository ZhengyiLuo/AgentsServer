"""Team Messages V2 service tests: messages, receipts, attachments, skills."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from agentsdock_team_hub.service import create_app
from agentsdock_team_hub.store import (
    MAX_TEAM_MESSAGE_BODY_BYTES,
    TEAM_ATTACHMENT_CHUNK_BYTES,
)


def _key() -> str:
    return "key_" + uuid.uuid4().hex


class TeamMessagesServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.app = create_app(self.data_dir, allowed_hosts={"testserver", "localhost"})
        self.client = TestClient(
            self.app,
            base_url="http://localhost",
            client=("127.0.0.1", 41000),
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.owner = self.bootstrap()
        self.team_id = self.owner["teams"][0]["id"]
        self.member = self.invite_and_redeem(self.owner, "member@example.com", "member")
        self.guest = self.invite_and_redeem(self.owner, "guest@example.com", "guest")
        self.base = f"/v1/teams/{self.team_id}/network"

    # -- helpers ------------------------------------------------------------

    def bootstrap(self) -> dict:
        proof = (self.data_dir / "bootstrap-owner.proof").read_text().strip()
        response = self.client.post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": proof},
            json={
                "email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Owner Mac",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def auth(bundle: dict) -> dict[str, str]:
        return {"Authorization": f"Bearer {bundle['access_token']}"}

    def invite_and_redeem(self, owner: dict, email: str, role: str) -> dict:
        issued = self.client.post(
            f"/v1/teams/{self.team_id if hasattr(self, 'team_id') else owner['teams'][0]['id']}/invitations",
            headers=self.auth(owner),
            json={"invitee_email": email, "role": role},
        )
        self.assertEqual(issued.status_code, 200, issued.text)
        redeemed = self.client.post(
            "/v1/invitations/redeem",
            json={
                "token": issued.json()["token"],
                "email": email,
                "display_name": email.split("@", 1)[0].title(),
                "device_label": f"{role} device",
            },
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        return redeemed.json()

    def post(self, bundle: dict, path: str, body: dict, expected: int = 200) -> dict:
        response = self.client.post(path, headers=self.auth(bundle), json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def get(self, bundle: dict, path: str, expected: int = 200) -> dict:
        response = self.client.get(path, headers=self.auth(bundle))
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def send(self, bundle: dict, recipients: list[dict], body: str = "Hello team", **extra) -> dict:
        payload = {
            "kind": "message",
            "body": body,
            "body_format": "markdown",
            "recipients": recipients,
            "idempotency_key": _key(),
        }
        payload.update(extra)
        return self.post(bundle, f"{self.base}/messages", payload)["message"]

    def put_chunk(self, bundle: dict, attachment_id: str, payload: bytes, start: int, end: int):
        return self.client.put(
            f"{self.base}/attachments/{attachment_id}/content",
            headers={
                **self.auth(bundle),
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            },
            content=payload[start : end + 1],
        )

    def declare(self, bundle: dict, payload: bytes, *, name: str = "demo.bin", digest: str | None = None) -> dict:
        return self.post(
            bundle,
            f"{self.base}/attachments",
            {
                "file_name": name,
                "media_type": "application/octet-stream",
                "byte_size": len(payload),
                "sha256": digest or hashlib.sha256(payload).hexdigest(),
                "idempotency_key": _key(),
            },
        )

    # -- health -------------------------------------------------------------

    def test_health_advertises_team_messages_as_sibling_capability(self) -> None:
        health = self.client.get("/v1/health").json()
        capabilities = health["capabilities"]
        self.assertEqual(set(capabilities["team_network_v1"]), {
            "available", "version", "logical_servers", "agent_registry", "bulletin",
            "mailbox", "delivery_receipts", "passive_requests", "server_invites",
            "skill_attachments", "dispatch", "max_agents_per_server", "max_page_items",
            "max_body_bytes",
        })
        messages = capabilities["team_messages_v1"]
        self.assertTrue(messages["available"])
        self.assertEqual(messages["kinds"], ["message", "skill"])
        self.assertEqual(messages["recipient_kinds"], ["server", "human", "all"])
        self.assertEqual(messages["max_body_bytes"], MAX_TEAM_MESSAGE_BODY_BYTES)
        self.assertEqual(messages["attachments"]["chunk_bytes"], TEAM_ATTACHMENT_CHUNK_BYTES)
        self.assertTrue(messages["attachments"]["range_downloads"])

    # -- feed ---------------------------------------------------------------

    def test_feed_post_is_visible_to_all_and_guest_is_read_only(self) -> None:
        created = self.send(self.owner, [{"kind": "all"}], body="Hello **team**")
        self.assertEqual(created["kind"], "message")
        self.assertEqual(created["body"], "Hello **team**")
        self.assertEqual(created["recipients"][0]["kind"], "all")
        self.assertEqual(created["sender"], {
            "kind": "human",
            "id": self.owner["principal"]["id"],
            "display_name": "Owner",
        })
        self.assertEqual(created["attachments"], [])
        self.assertIsNone(created["skill"])

        for bundle in (self.owner, self.member, self.guest):
            feed = self.get(bundle, f"{self.base}/messages?box=feed")
            self.assertEqual([item["id"] for item in feed["messages"]], [created["id"]])
            self.assertEqual(feed["messages"][0]["preview"], "Hello **team**")
            self.assertNotIn("body", feed["messages"][0])
            self.assertFalse(feed["has_more"])
            self.assertEqual(feed["next_after_sequence"], created["sequence"])
            detail = self.get(bundle, f"{self.base}/messages/{created['id']}")["message"]
            self.assertEqual(detail["body"], "Hello **team**")

        # Guests may read but cannot post.
        response = self.client.post(
            f"{self.base}/messages",
            headers=self.auth(self.guest),
            json={
                "kind": "message",
                "body": "nope",
                "recipients": [{"kind": "all"}],
                "idempotency_key": _key(),
            },
        )
        self.assertEqual(response.status_code, 404, response.text)

        # Paging cursor excludes already-seen items.
        second = self.send(self.member, [{"kind": "all"}], body="Second")
        page = self.get(
            self.owner, f"{self.base}/messages?box=feed&after_sequence={created['sequence']}"
        )
        self.assertEqual([item["id"] for item in page["messages"]], [second["id"]])

    def test_idempotent_replay_returns_same_message_and_conflicts_on_changed_body(self) -> None:
        key = _key()
        payload = {
            "kind": "message",
            "body": "once",
            "recipients": [{"kind": "all"}],
            "idempotency_key": key,
        }
        first = self.post(self.owner, f"{self.base}/messages", payload)["message"]
        replay = self.post(self.owner, f"{self.base}/messages", payload)["message"]
        self.assertEqual(first["id"], replay["id"])
        changed = self.post(
            self.owner, f"{self.base}/messages", {**payload, "body": "twice"}, expected=409
        )
        self.assertEqual(changed["error"]["code"], "idempotency_conflict")
        feed = self.get(self.owner, f"{self.base}/messages?box=feed")
        self.assertEqual(len(feed["messages"]), 1)

    # -- direct mail --------------------------------------------------------

    def test_direct_message_inbox_receipts_sent_and_visibility(self) -> None:
        member_id = self.member["principal"]["id"]
        sent = self.send(self.owner, [{"kind": "human", "id": member_id}], body="For you")
        self.assertEqual(sent["recipients"][0]["id"], member_id)
        self.assertEqual(sent["recipients"][0]["state"], "available")

        inbox = self.get(self.member, f"{self.base}/messages?box=inbox")
        self.assertEqual(inbox["address"], {"kind": "human", "id": member_id})
        self.assertEqual([item["id"] for item in inbox["messages"]], [sent["id"]])
        self.assertEqual(inbox["messages"][0]["delivery"]["state"], "available")

        self.assertEqual(self.get(self.owner, f"{self.base}/messages?box=inbox")["messages"], [])
        owner_sent = self.get(self.owner, f"{self.base}/messages?box=sent")
        self.assertEqual([item["id"] for item in owner_sent["messages"]], [sent["id"]])
        self.assertEqual(self.get(self.member, f"{self.base}/messages?box=sent")["messages"], [])

        unread = self.get(self.member, f"{self.base}/messages?box=inbox&unread=1")
        self.assertEqual(len(unread["messages"]), 1)

        delivered = self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            {"state": "delivered", "idempotency_key": _key()},
        )
        self.assertEqual(delivered["recipients"][0]["state"], "delivered")
        read = self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            {"state": "read", "idempotency_key": _key()},
        )
        self.assertEqual(read["recipients"][0]["state"], "read")
        self.assertIsNotNone(read["recipients"][0]["delivered_at"])
        self.assertEqual(
            self.get(self.member, f"{self.base}/messages?box=inbox&unread=1")["messages"], []
        )
        # Receipts are monotonic: going back to delivered is a no-op.
        again = self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            {"state": "delivered", "idempotency_key": _key()},
        )
        self.assertEqual(again["recipients"][0]["state"], "read")

        # The sender sees the recipient state; a third party sees nothing.
        detail = self.get(self.owner, f"{self.base}/messages/{sent['id']}")["message"]
        self.assertEqual(detail["recipients"][0]["state"], "read")
        self.get(self.guest, f"{self.base}/messages/{sent['id']}", expected=404)
        denied = self.post(
            self.guest,
            f"{self.base}/messages/{sent['id']}/receipts",
            {"state": "read", "idempotency_key": _key()},
            expected=403,
        )
        self.assertEqual(denied["error"]["code"], "forbidden")

        # Only owned mailboxes can be listed.
        other = self.client.get(
            f"{self.base}/messages?box=inbox&address_kind=human&address_id={self.owner['principal']['id']}",
            headers=self.auth(self.member),
        )
        self.assertEqual(other.status_code, 403, other.text)

        # Unknown or non-member recipients fail closed.
        missing = self.post(
            self.owner,
            f"{self.base}/messages",
            {
                "kind": "message",
                "body": "?",
                "recipients": [{"kind": "human", "id": "principal_missing"}],
                "idempotency_key": _key(),
            },
            expected=404,
        )
        self.assertEqual(missing["error"]["code"], "recipient_unavailable")

        # Sender filter and since filter.
        filtered = self.get(
            self.member,
            f"{self.base}/messages?box=inbox&from_kind=human&from_id={self.owner['principal']['id']}",
        )
        self.assertEqual(len(filtered["messages"]), 1)
        future = self.get(self.member, f"{self.base}/messages?box=inbox&since=2999-01-01T00:00:00Z")
        self.assertEqual(future["messages"], [])

    def test_receipt_outbox_is_atomic_per_recipient_and_idempotent(self) -> None:
        member_id = self.member["principal"]["id"]
        sent = self.send(self.owner, [{"kind": "human", "id": member_id}])
        store = self.app.state.store
        connection = store.connect()
        try:
            recipient_id = str(
                connection.execute(
                    "SELECT id FROM team_message_recipients WHERE message_id=?",
                    (sent["id"],),
                ).fetchone()[0]
            )
        finally:
            connection.close()

        delivered_key = _key()
        delivered_request = {
            "state": "delivered",
            "idempotency_key": delivered_key,
        }
        with mock.patch.object(
            store,
            "_outbox",
            side_effect=RuntimeError("forced receipt outbox failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced receipt outbox failure"):
                self.client.post(
                    f"{self.base}/messages/{sent['id']}/receipts",
                    headers=self.auth(self.member),
                    json=delivered_request,
                )

        connection = store.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM team_message_recipients WHERE id=?",
                    (recipient_id,),
                ).fetchone()[0],
                "available",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action='team.message.receipt'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM request_idempotency "
                    "WHERE operation='team.message.receipt'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE aggregate_type='team_message_recipient' AND aggregate_id=?",
                    (recipient_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

        first = self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            delivered_request,
        )
        replay = self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            delivered_request,
        )
        self.assertEqual(first, replay)
        read_key = _key()
        read_request = {"state": "read", "idempotency_key": read_key}
        self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            read_request,
        )
        self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            read_request,
        )
        # A monotonic no-op under a distinct request key is audited but does
        # not claim another state-change effect in the outbox.
        self.post(
            self.member,
            f"{self.base}/messages/{sent['id']}/receipts",
            {"state": "delivered", "idempotency_key": _key()},
        )

        connection = store.connect()
        try:
            events = connection.execute(
                """
                SELECT aggregate_type,aggregate_id,event_type,state
                FROM outbox_events
                WHERE aggregate_type='team_message_recipient' AND aggregate_id=?
                ORDER BY created_at,event_type
                """,
                (recipient_id,),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in events],
                [
                    (
                        "team_message_recipient",
                        recipient_id,
                        "team.message.delivered",
                        "pending",
                    ),
                    (
                        "team_message_recipient",
                        recipient_id,
                        "team.message.read",
                        "pending",
                    ),
                ],
            )
        finally:
            connection.close()

    def test_inbox_delivery_matches_the_requested_owned_address(self) -> None:
        member_id = self.member["principal"]["id"]
        guest_id = self.guest["principal"]["id"]
        sent = self.send(
            self.owner,
            [
                {"kind": "human", "id": member_id},
                {"kind": "human", "id": guest_id},
            ],
            body="Address-specific delivery",
        )

        store = self.app.state.store
        original_owned_addresses = store._team_owned_addresses
        store._team_owned_addresses = lambda *_args, **_kwargs: [
            ("human", member_id),
            ("human", guest_id),
        ]
        self.addCleanup(setattr, store, "_team_owned_addresses", original_owned_addresses)

        inbox = self.get(
            self.member,
            f"{self.base}/messages?box=inbox&address_kind=human&address_id={guest_id}",
        )
        self.assertEqual([item["id"] for item in inbox["messages"]], [sent["id"]])
        self.assertEqual(inbox["messages"][0]["delivery"]["id"], guest_id)

    def test_reply_links_to_an_existing_message_only(self) -> None:
        root = self.send(self.owner, [{"kind": "all"}], body="root")
        reply = self.send(
            self.member, [{"kind": "all"}], body="reply", in_reply_to_message_id=root["id"]
        )
        self.assertEqual(reply["in_reply_to_message_id"], root["id"])
        self.post(
            self.member,
            f"{self.base}/messages",
            {
                "kind": "message",
                "body": "orphan",
                "recipients": [{"kind": "all"}],
                "in_reply_to_message_id": "tmsg_doesnotexist0000",
                "idempotency_key": _key(),
            },
            expected=422,
        )

    # -- skills -------------------------------------------------------------

    def skill_post(self, bundle: dict, slug: str, title: str, body: str, expected: int = 200, **skill) -> dict:
        return self.post(
            bundle,
            f"{self.base}/messages",
            {
                "kind": "skill",
                "title": title,
                "body": body,
                "recipients": [{"kind": "all"}],
                "skill": {"slug": slug, **skill},
                "idempotency_key": _key(),
            },
            expected=expected,
        )

    def test_skill_lifecycle_create_update_cas_pin_archive_restore(self) -> None:
        first = self.skill_post(
            self.owner,
            "deploy-sonic",
            "Deploy SONIC",
            "# Steps\n1. build\n2. ship",
            summary="How we deploy SONIC",
            tags=["Sonic", "deploy", "sonic"],
        )["message"]
        self.assertEqual(first["kind"], "skill")
        self.assertEqual(first["skill"]["slug"], "deploy-sonic")
        self.assertEqual(first["skill"]["version"], 1)
        skill_id = first["skill"]["id"]

        listed = self.get(self.member, f"{self.base}/skills")["skills"]
        self.assertEqual(len(listed), 1)
        skill = listed[0]
        self.assertEqual(skill["slug"], "deploy-sonic")
        self.assertEqual(skill["tags"], ["sonic", "deploy"])
        self.assertEqual(skill["version"], 1)
        self.assertEqual(skill["versions_count"], 1)
        self.assertFalse(skill["pinned"])
        self.assertTrue(skill["permissions"]["edit"])
        self.assertNotIn("body", skill)
        guest_view = self.get(self.guest, f"{self.base}/skills")["skills"][0]
        self.assertFalse(guest_view["permissions"]["edit"])
        by_slug = self.get(self.guest, f"{self.base}/skills?slug=deploy-sonic")["skills"]
        self.assertEqual([item["id"] for item in by_slug], [skill_id])

        # Updating requires the current version.
        conflict = self.skill_post(
            self.member, "deploy-sonic", "Deploy SONIC", "# v2", expected=409
        )
        self.assertEqual(conflict["error"]["code"], "skill_version_conflict")
        stale = self.skill_post(
            self.member, "deploy-sonic", "Deploy SONIC", "# v2", expected=409, expected_version=5
        )
        self.assertEqual(stale["error"]["code"], "skill_version_conflict")
        second = self.skill_post(
            self.member,
            "deploy-sonic",
            "Deploy SONIC v2",
            "# v2 steps",
            expected_version=1,
            change_note="Added rollback",
        )["message"]
        self.assertEqual(second["skill"]["version"], 2)
        self.assertEqual(second["skill"]["id"], skill_id)

        detail = self.get(self.guest, f"{self.base}/skills/{skill_id}")["skill"]
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["title"], "Deploy SONIC v2")
        self.assertEqual(detail["body"], "# v2 steps")
        self.assertEqual(detail["author"]["display_name"], "Member")
        self.assertEqual(detail["current"]["change_note"], "Added rollback")
        self.assertEqual(detail["attachments"], [])

        versions = self.get(self.guest, f"{self.base}/skills/{skill_id}/versions")["versions"]
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertNotIn("body", versions[0])
        version_one = self.get(self.guest, f"{self.base}/skills/{skill_id}/versions/1")["version"]
        self.assertEqual(version_one["body"], "# Steps\n1. build\n2. ship")
        self.get(self.guest, f"{self.base}/skills/{skill_id}/versions/9", expected=404)

        # Feed shows both skill posts.
        feed = self.get(self.guest, f"{self.base}/messages?box=feed")["messages"]
        self.assertEqual([item["kind"] for item in feed], ["skill", "skill"])

        # Pinning orders the library; another skill stays below.
        other = self.skill_post(self.owner, "run-groot", "Run GR00T", "# groot")["message"]
        pinned = self.post(
            self.member,
            f"{self.base}/skills/{skill_id}/pin",
            {"pinned": True, "idempotency_key": _key()},
        )["skill"]
        self.assertTrue(pinned["pinned"])
        order = [item["slug"] for item in self.get(self.guest, f"{self.base}/skills")["skills"]]
        self.assertEqual(order, ["deploy-sonic", "run-groot"])
        self.assertEqual(other["skill"]["version"], 1)

        # Guests cannot pin.
        self.post(
            self.guest,
            f"{self.base}/skills/{skill_id}/pin",
            {"pinned": False, "idempotency_key": _key()},
            expected=404,
        )

        # Archiving unpins and hides; edits and pins are blocked until restore.
        archived = self.post(
            self.owner,
            f"{self.base}/skills/{skill_id}/archive",
            {"archived": True, "idempotency_key": _key()},
        )["skill"]
        self.assertTrue(archived["archived"])
        self.assertFalse(archived["pinned"])
        self.assertFalse(archived["permissions"]["edit"])
        visible = [item["slug"] for item in self.get(self.guest, f"{self.base}/skills")["skills"]]
        self.assertEqual(visible, ["run-groot"])
        everything = [
            item["slug"]
            for item in self.get(self.guest, f"{self.base}/skills?include_archived=1")["skills"]
        ]
        self.assertEqual(sorted(everything), ["deploy-sonic", "run-groot"])
        blocked = self.skill_post(
            self.owner, "deploy-sonic", "Deploy SONIC v3", "# v3", expected=409, expected_version=2
        )
        self.assertEqual(blocked["error"]["code"], "skill_archived")
        repin = self.post(
            self.owner,
            f"{self.base}/skills/{skill_id}/pin",
            {"pinned": True, "idempotency_key": _key()},
            expected=409,
        )
        self.assertEqual(repin["error"]["code"], "skill_archived")
        restored = self.post(
            self.owner,
            f"{self.base}/skills/{skill_id}/archive",
            {"archived": False, "idempotency_key": _key()},
        )["skill"]
        self.assertFalse(restored["archived"])
        third = self.skill_post(
            self.owner, "deploy-sonic", "Deploy SONIC v3", "# v3", expected_version=2
        )["message"]
        self.assertEqual(third["skill"]["version"], 3)

    def test_skill_posts_require_title_all_recipient_and_skill_details(self) -> None:
        base = {
            "kind": "skill",
            "body": "# x",
            "recipients": [{"kind": "all"}],
            "skill": {"slug": "needs-title"},
            "idempotency_key": _key(),
        }
        self.post(self.owner, f"{self.base}/messages", base, expected=422)
        self.post(
            self.owner,
            f"{self.base}/messages",
            {
                **base,
                "title": "T",
                "recipients": [{"kind": "human", "id": self.member["principal"]["id"]}],
                "idempotency_key": _key(),
            },
            expected=422,
        )
        self.post(
            self.owner,
            f"{self.base}/messages",
            {**base, "title": "T", "skill": {"slug": "Bad Slug!"}, "idempotency_key": _key()},
            expected=422,
        )
        self.post(
            self.owner,
            f"{self.base}/messages",
            {
                "kind": "message",
                "body": "plain",
                "recipients": [{"kind": "all"}],
                "skill": {"slug": "not-allowed"},
                "idempotency_key": _key(),
            },
            expected=422,
        )

    # -- attachments --------------------------------------------------------

    def test_attachment_chunked_upload_range_download_and_binding(self) -> None:
        payload = bytes(range(256)) * 40  # 10 240 bytes, two chunks in this test
        declared = self.declare(self.owner, payload)
        attachment = declared["attachment"]
        self.assertEqual(declared["chunk_bytes"], TEAM_ATTACHMENT_CHUNK_BYTES)
        self.assertEqual(attachment["state"], "uploading")
        self.assertEqual(attachment["received_bytes"], 0)
        self.assertIsNone(attachment["message_id"])
        url = f"{self.base}/attachments/{attachment['id']}/content"

        # Nothing to download yet, and other members cannot see an unbound upload.
        self.assertEqual(
            self.client.get(url, headers=self.auth(self.owner)).status_code, 409
        )
        self.assertEqual(
            self.client.get(
                f"{self.base}/attachments/{attachment['id']}", headers=self.auth(self.member)
            ).status_code,
            404,
        )

        first = self.put_chunk(self.owner, attachment["id"], payload, 0, 4095)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["attachment"]["received_bytes"], 4096)
        self.assertEqual(first.json()["attachment"]["state"], "uploading")

        gap = self.put_chunk(self.owner, attachment["id"], payload, 8192, len(payload) - 1)
        self.assertEqual(gap.status_code, 409, gap.text)

        replay = self.put_chunk(self.owner, attachment["id"], payload, 0, 4095)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["attachment"]["received_bytes"], 4096)

        stranger = self.put_chunk(self.member, attachment["id"], payload, 4096, len(payload) - 1)
        self.assertEqual(stranger.status_code, 404, stranger.text)

        final = self.put_chunk(self.owner, attachment["id"], payload, 4096, len(payload) - 1)
        self.assertEqual(final.status_code, 200, final.text)
        ready = final.json()["attachment"]
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["received_bytes"], len(payload))
        self.assertIsNotNone(ready["ready_at"])
        stored = self.data_dir / "attachments" / ready["sha256"][:2] / ready["sha256"]
        self.assertEqual(stored.read_bytes(), payload)
        self.assertFalse((self.data_dir / "attachments" / "uploads" / f"{attachment['id']}.part").exists())

        # Full download, range, suffix range, invalid range, HEAD.
        full = self.client.get(url, headers=self.auth(self.owner))
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(full.content, payload)
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["content-type"], "application/octet-stream")
        self.assertEqual(full.headers["content-length"], str(len(payload)))
        self.assertIn(ready["sha256"], full.headers["etag"])
        part = self.client.get(url, headers={**self.auth(self.owner), "Range": "bytes=5-9"})
        self.assertEqual(part.status_code, 206, part.text)
        self.assertEqual(part.content, payload[5:10])
        self.assertEqual(part.headers["content-range"], f"bytes 5-9/{len(payload)}")
        tail = self.client.get(url, headers={**self.auth(self.owner), "Range": "bytes=-4"})
        self.assertEqual(tail.status_code, 206)
        self.assertEqual(tail.content, payload[-4:])
        open_ended = self.client.get(
            url, headers={**self.auth(self.owner), "Range": f"bytes={len(payload) - 3}-"}
        )
        self.assertEqual(open_ended.status_code, 206)
        self.assertEqual(open_ended.content, payload[-3:])
        bad = self.client.get(url, headers={**self.auth(self.owner), "Range": "bytes=999999-"})
        self.assertEqual(bad.status_code, 416)
        self.assertEqual(bad.headers["content-range"], f"bytes */{len(payload)}")
        head = self.client.head(url, headers=self.auth(self.owner))
        self.assertEqual(head.status_code, 200, head.text)
        self.assertEqual(head.headers["content-length"], str(len(payload)))
        self.assertEqual(head.content, b"")

        # Binding to a message makes it visible to recipients, and single-use.
        member_id = self.member["principal"]["id"]
        message = self.send(
            self.owner,
            [{"kind": "human", "id": member_id}],
            body="see attached",
            attachment_ids=[attachment["id"]],
        )
        self.assertEqual([item["id"] for item in message["attachments"]], [attachment["id"]])
        self.assertEqual(message["attachments"][0]["message_id"], message["id"])
        reused = self.post(
            self.owner,
            f"{self.base}/messages",
            {
                "kind": "message",
                "body": "again",
                "recipients": [{"kind": "all"}],
                "attachment_ids": [attachment["id"]],
                "idempotency_key": _key(),
            },
            expected=409,
        )
        self.assertEqual(reused["error"]["code"], "attachment_unavailable")
        self.assertEqual(self.client.get(url, headers=self.auth(self.member)).status_code, 200)
        self.assertEqual(self.client.get(url, headers=self.auth(self.guest)).status_code, 404)
        metadata = self.get(self.member, f"{self.base}/attachments/{attachment['id']}")
        self.assertEqual(metadata["attachment"]["file_name"], "demo.bin")

        # Same bytes declared again are ready immediately.
        duplicate = self.declare(self.owner, payload, name="copy.bin")["attachment"]
        self.assertEqual(duplicate["state"], "ready")
        self.assertEqual(duplicate["received_bytes"], len(payload))

    def test_attachment_declaration_outbox_is_atomic_and_idempotent(self) -> None:
        payload = b"declaration outbox"
        request = {
            "file_name": "outbox.txt",
            "media_type": "text/plain",
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "idempotency_key": _key(),
        }
        store = self.app.state.store
        with mock.patch.object(
            store,
            "_outbox",
            side_effect=RuntimeError("forced declaration outbox failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced declaration outbox failure"):
                self.client.post(
                    f"{self.base}/attachments",
                    headers=self.auth(self.owner),
                    json=request,
                )

        connection = store.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM team_attachments").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE action='team.attachment.declare'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM request_idempotency "
                    "WHERE operation='team.attachment.declare'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

        first = self.post(self.owner, f"{self.base}/attachments", request)
        replay = self.post(self.owner, f"{self.base}/attachments", request)
        self.assertEqual(first, replay)
        attachment_id = first["attachment"]["id"]
        connection = store.connect()
        try:
            events = connection.execute(
                """
                SELECT aggregate_type,aggregate_id,event_type,state
                FROM outbox_events
                WHERE aggregate_type='team_attachment' AND aggregate_id=?
                  AND event_type='team.attachment.declared'
                """,
                (attachment_id,),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in events],
                [
                    (
                        "team_attachment",
                        attachment_id,
                        "team.attachment.declared",
                        "pending",
                    )
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE action='team.attachment.declare' AND resource_id=?",
                    (attachment_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_attachment_hash_mismatch_fails_closed(self) -> None:
        payload = b"x" * 3000
        wrong = hashlib.sha256(b"different").hexdigest()
        declared = self.declare(self.owner, payload, digest=wrong)["attachment"]
        response = self.put_chunk(self.owner, declared["id"], payload, 0, len(payload) - 1)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "attachment_hash_mismatch")
        metadata = self.get(self.owner, f"{self.base}/attachments/{declared['id']}")["attachment"]
        self.assertEqual(metadata["state"], "failed")
        self.assertFalse((self.data_dir / "attachments" / wrong[:2] / wrong).exists())
        # A failed upload cannot be attached.
        denied = self.post(
            self.owner,
            f"{self.base}/messages",
            {
                "kind": "message",
                "body": "broken",
                "recipients": [{"kind": "all"}],
                "attachment_ids": [declared["id"]],
                "idempotency_key": _key(),
            },
            expected=409,
        )
        self.assertEqual(denied["error"]["code"], "attachment_unavailable")

    def test_attachment_declaration_and_chunk_validation(self) -> None:
        payload = b"y" * 10
        digest = hashlib.sha256(payload).hexdigest()
        for bad in (
            {"file_name": "../evil", "media_type": "text/plain", "byte_size": 10, "sha256": digest},
            {"file_name": "a/b", "media_type": "text/plain", "byte_size": 10, "sha256": digest},
            {"file_name": "ok.txt", "media_type": "not a type", "byte_size": 10, "sha256": digest},
            {"file_name": "ok.txt", "media_type": "text/plain", "byte_size": 10, "sha256": "zz"},
        ):
            self.post(
                self.owner,
                f"{self.base}/attachments",
                {**bad, "idempotency_key": _key()},
                expected=422,
            )
        declared = self.declare(self.owner, payload, name="ok.txt")["attachment"]
        url = f"{self.base}/attachments/{declared['id']}/content"
        wrong_type = self.client.put(
            url,
            headers={
                **self.auth(self.owner),
                "Content-Type": "application/json",
                "Content-Range": "bytes 0-9/10",
            },
            content=payload,
        )
        self.assertEqual(wrong_type.status_code, 415, wrong_type.text)
        no_range = self.client.put(
            url,
            headers={**self.auth(self.owner), "Content-Type": "application/octet-stream"},
            content=payload,
        )
        self.assertEqual(no_range.status_code, 400, no_range.text)
        mismatch = self.client.put(
            url,
            headers={
                **self.auth(self.owner),
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-4/10",
            },
            content=payload,
        )
        self.assertEqual(mismatch.status_code, 422, mismatch.text)
        wrong_total = self.client.put(
            url,
            headers={
                **self.auth(self.owner),
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-9/11",
            },
            content=payload,
        )
        self.assertEqual(wrong_total.status_code, 422, wrong_total.text)
        guest = self.declare  # guests cannot declare uploads (write scope)
        response = self.client.post(
            f"{self.base}/attachments",
            headers=self.auth(self.guest),
            json={
                "file_name": "g.txt",
                "media_type": "text/plain",
                "byte_size": 10,
                "sha256": digest,
                "idempotency_key": _key(),
            },
        )
        self.assertEqual(response.status_code, 404, response.text)
        del guest

    def test_expired_incomplete_uploads_are_purged_but_ready_files_stay(self) -> None:
        store = self.app.state.store
        payload = b"z" * 100
        stale = self.declare(self.owner, payload, name="stale.bin")["attachment"]
        self.put_chunk(self.owner, stale["id"], payload, 0, 49)
        finished = self.declare(self.owner, b"q" * 20, name="done.bin")["attachment"]
        self.assertEqual(
            self.put_chunk(self.owner, finished["id"], b"q" * 20, 0, 19).status_code, 200
        )
        far_future = 10**10
        removed = store.purge_expired_team_attachments(far_future)
        self.assertEqual(removed, 1)
        self.get(self.owner, f"{self.base}/attachments/{stale['id']}", expected=404)
        self.assertEqual(
            self.get(self.owner, f"{self.base}/attachments/{finished['id']}")["attachment"]["state"],
            "ready",
        )


if __name__ == "__main__":
    unittest.main()
