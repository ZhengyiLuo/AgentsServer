# AgentsServer 0.1.26-beta.66

- Fix successful asynchronous cross-chat sends being reported as an invalid
  receipt. Accepted-mail responses remain compatible with older helpers, and
  current helpers also accept the beta.65 receipt. Retrying with the same
  idempotency key returns the original message instead of another delivery.
- Return token-free sharing URLs and a separate access token. Interactive
  tokens are reusable by multiple collaborators; each browser enters the same
  token and receives a scoped HttpOnly cookie. Revocation disables all access
  through that share. Existing browser cookies remain valid until expiry or
  revocation. Interactive links never consume tokens from URL parameters.
- Remove the whole-conversation 2 MiB snapshot limit. New snapshots stream into
  bounded immutable pages and open at the end of the latest page. Older/newer
  navigation preserves message order and checks access on every page.
- Add a token-entry page for view-only snapshots. A token-in-link shortcut
  remains available only as an explicit view-only sharing option.

API contract remains 28. The separate-token desktop sharing interface requires
AgentsDock 0.2.13-beta.37 or later. HTTP remains supported for trusted networks;
it is unencrypted, so use HTTPS on untrusted networks. No firewall, ingress,
Team Hub role, polling, file-browsing or terminal permissions are changed.

Snapshot storage adds an immutable page table (schema version 3). Existing
snapshots remain readable by this release. After a new snapshot is created,
older server versions cannot read the upgraded snapshot store; retain a
pre-update backup if a downgrade is needed. Provider histories are not rewritten.

Validation covers actual server-handler/SQLite receipts through both send and
ask helpers, duplicate retry prevention, token/expiry/revocation boundaries,
streamed snapshots larger than 2 MiB, and isolated browser/desktop sharing
journeys. Synthetic fixtures do not run providers or mutate real chats.
Publication alone does not install or restart a running server.
