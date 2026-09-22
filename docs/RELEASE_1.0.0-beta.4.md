# AgentsServer 1.0.0-beta.4

## Active-goal steering

- Allow Send now to steer text and uploaded attachments during the first
  Codex turn that creates a goal as well as subsequent goal continuations.
- Preserve the running goal, provider turn, authority and background work;
  steering does not fall through to Stop or create a replacement run.
- Hold a follow-up arriving between owned native goal turns until the next
  ready turn on the existing notification stream. No polling is added.
- Preserve accepted input across first-turn/continuation acknowledgement
  races, record it once and prevent stale original-prompt recovery from
  replaying work after accepted or uncertain steering.
- Keep explicit Pause and Stop authoritative. Incompatible model/runtime
  changes or new structured route grants remain separate-work operations.

## Compatibility and validation

API contract 28, dependencies and Team Hub schema 22 are unchanged from beta.3.
The existing desktop goal-steering controls are compatible; no new app build
is required for this correction. This release also includes beta.3's Chats
stdin reply correction and quiet Team Mail/Bulletin notification support.

Focused isolated tests exercise the real runner, native consumer and steering
handlers with a controlled provider transport, including attachments, goal
activation races, acknowledgement rollover, cancellation and duplicate
prevention. These checks do not substitute for live-provider acceptance.

Upgrade through the managed updater. When upgrading from beta.66 or another
pre-schema-22 runtime, retain the pre-upgrade Hub snapshot: rollback requires
restoring the older database together with its runtime. Never interrupt active
work solely to install this release without explicit operator approval.

This is a beta release, not 1.0 stable acceptance.

## Accepted release

Published from `b5fa0728ede6e99f228685f625198b5bdcde20a0` after the full
release gate completed successfully (3,883 tests run, two skipped). Downloaded
assets passed Ed25519 signature validation, public asset digest checks and
exact source comparison for all 76 packaged files. Archive SHA-256:
`5d4d70ed26e03a4f5bcbc13de57172db483123926a4e3cda2cc7a8239ef8f0f4`.

Publication and accepting an idle update schedule do not establish live
installation or provider acceptance. Confirm the new running version and Hub
health after activation, then use a disposable chat for the real steering
journey. Stable qualification also needs the separately documented idle-goal
mailbox-authority limitation resolved.
