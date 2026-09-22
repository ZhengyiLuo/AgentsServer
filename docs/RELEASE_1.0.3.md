# AgentsServer 1.0.3

Stable update from **1.0.2**. Team Network remains a beta feature.

## Workspace Changes

- Support the desktop's new **Changes** tab with on-demand repository-wide
  status, staged and unstaged diffs, whole-file staging and unstaging, and
  commits of the reviewed staged set.
- Review base, current and incoming text conflicts; save and stage a resolution;
  continue an existing merge, rebase, cherry-pick or revert. Abort requires an
  explicit confirmation. A further conflict remains visible rather than being
  reported as a completed operation.
- Restrict these endpoints to authenticated native operators. Shared-chat
  guests do not gain repository access. Check repository revisions and Git's
  index lock before mutations to reject stale reviews and concurrent changes.
- Preserve recoverable state on storage failures and report recovery guidance
  if an operation changed references before its index could be published.

This first cut requires **AgentsDock 1.0.3**. It does not add branch creation,
push, PR/MR management, or background repository polling. Binary conflicts and
operations requiring executable Git hooks or custom filters need the terminal;
the server does not silently bypass those hooks or filters.

## Codex Side chat startup

- Stop creating a fresh Codex runtime database for every side question, which
  could trigger provider-history re-indexing and time out before answering.
  Use the configured provider runtime and sign-in without copying credentials.
- Keep each side question in its own temporary thread and owned process, with
  workspace access and tools disabled. Cancellation closes that side process;
  it does not stop, steer or resume the main conversation.

## Compatibility and updating

- API contract remains **28**; no Team Hub schema, dependency or signing-key
  change from 1.0.2. Existing clients retain their current behavior.
- Install through the existing Stable server updater. Publishing a release
  does not restart or automatically replace any running server.
