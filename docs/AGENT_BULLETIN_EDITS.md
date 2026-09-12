# Agent edits to existing Bulletin posts

The Team provider helper exposes the existing poster-only, body-versioning
operation. It does not grant a new route or use operator administration rights.
Use an available Bulletin route for the current chat and the authenticated
posting identity; a private server-mail route or `@@all` route cannot edit the
Bulletin.

Through the run-bound provider tool, use helper `team` with these arguments:

1. `routes` — find the current Bulletin route (`allows_bulletin_edit: true`).
2. `read MESSAGE_ID --include-revision` — read the exact current body and
   `message.revision.version`. Use `--team TEAM_ID` if the server has several
   team memberships.
3. `edit MESSAGE_ID --route ROUTE_ID --expected-version N` — provide the full
   replacement Markdown body on tool stdin, never in command arguments.

The response retains the original `message_id`, sets `edited: true`, and returns
the new `version`. The Bulletin keeps one item and its existing attachments,
title and previous bodies. This operation cannot replace attachments, change
recipients, update a skill, or create a mail reply. Skill updates retain their
existing skill-version operation.

The Hub transaction enforces poster ownership and the expected version. A
conflicting edit requires reading the changed post before trying again; an
error never falls back to a new post. Repeating the same operation within its
existing route/idempotency contract returns the same accepted revision. The
agent send pipeline retains its existing live-run and connection-generation
checks. An edit is a write, not a read-only helper operation.

The wire discriminator is explicitly `bulletin_edit`. Older AgentsServers reject
it rather than ignoring edit fields and creating a duplicate post. Read-only
revision metadata is opt-in; ordinary legacy reads remain unchanged.

The Hub records its existing revision audit/history. No additional sent-mail
event or assistant message is injected into the chat. There is no new polling,
background refresh or automatic correction of already-created duplicate posts.
