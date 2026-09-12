# AgentsServer 0.1.26-beta.64

This replaces the unpublished beta.63 candidate; its tag is retained unchanged.
The sharing implementation and browser renderer are unchanged from that candidate.

- Add explicitly confirmed read-only chat snapshots and one-use interactive
  invitations with the shared AgentsDock browser UI. Interactive access controls
  that one chat, including prompts, guest uploads, queue, Stop/steering, settings,
  approvals, goals and schedules. It does not expose native file browsing,
  downloads, terminal, server administration or other-chat APIs.
- Stream large read-only histories without the former 64 MiB raw-log or
  1,000-message ceilings, retaining a 2 MiB serialized snapshot bound and explicit
  review. Upgrade existing snapshot storage transactionally without losing
  snapshots, token hashes or revocations.
- Repair exact-source-proven decorated native history duplicates on read,
  including scheduled outputs imported later as ordinary assistant messages.
  Preserve the original scheduled runs, timestamps and genuine messages;
  do not rewrite provider transcripts or infer duplication from text alone.
- Expose expected-version, body-only agent edits of existing bulletin posts.
- Correct the isolated steering test's incidental deadline under full-suite
  scheduling pressure while retaining its non-starvation and complete-drain
  assertions. No production scheduling or timeout behavior is changed.

API contract remains 28. Interactive sharing needs a compatible desktop and a
deliberately configured HTTPS origin. Sharing grants trusted use of the existing
agent, not a provider sandbox. Revocation does not undo accepted work or jobs.

Validation uses isolated native adapters, synthetic browser fixtures, and
read-only source-proven repair checks. It does not certify real provider runs or
public ingress. Publishing does not install, restart, configure ingress, or
create a share on any running server.
