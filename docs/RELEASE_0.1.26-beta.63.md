# AgentsServer 0.1.26-beta.63

- Add explicitly confirmed read-only snapshots and single-use interactive chat
  invitations. Interactive sharing reuses the AgentsDock browser chat UI and
  grants control of that one chat: prompts, guest uploads, queue, Stop/steering,
  settings, approvals, goals, and schedules. Direct file/download, terminal,
  server-administration, and other-chat APIs remain unavailable.
- Fix large read-only snapshots: stream the reviewed durable prefix without a
  64 MiB raw-history ceiling or 1,000-message ceiling. Preserve all eligible text
  within the 2 MiB serialized snapshot bound. Existing snapshot databases upgrade
  transactionally without losing snapshots, token hashes, or revocations.
- Preserve authenticated browser reload, uncertain-action acknowledgment safety,
  jobs-access policy, exact-participant message expansion, and stream cleanup on
  early disconnect. Interactive sharing requires a deliberately configured HTTPS
  origin; this release does not configure ingress or create shares automatically.
- Expose body-only, expected-version agent edits for existing bulletin posts, and
  retain exact-source repair of decorated native assistant history replays.

Interactive sharing is trusted collaboration, not an agent sandbox: the existing
agent retains its normal tools and context. Revocation stops future access but
does not undo accepted work or remove already configured jobs.

API contract remains 28. The desktop sharing fixes require a matching updated
desktop beta. Validation uses isolated synthetic stores, source-extracted native
adapters, and browser fixtures; it does not certify real provider runs or public
ingress. Publishing this release does not install it or restart running servers.
