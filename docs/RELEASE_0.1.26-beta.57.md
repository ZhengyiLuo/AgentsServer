# AgentsServer 0.1.26-beta.57

Includes the narrow history and goal fixes described in beta.56's candidate
notes, plus interrupted Claude background-work reconciliation. API contract 28
and Hub schema 19 remain unchanged. No inbox polling or subscriptions are added.

- Retain bounded, structured background-task facts across Claude steering and
  the next turn. Unobserved completion is `tracking_lost`, not a claim that work
  was killed or remains running. Preserve observed terminal outcomes.
- Deliver pending reconciliation through a request-bound SDK runtime hook,
  not a synthetic user message. Consume it only after the matching request was
  accepted and provider progress was observed; stale callbacks cannot consume
  a later request's checkpoint. Do not automatically rerun mutating work.
- Keep source-proven Claude interruption/sidechain records out of human
  history. Native steering metadata can establish the interruption cause;
  generic provider wording alone cannot establish that a user pressed Stop.
- Preserve source timestamps and exact full-text identity, including escaped
  Unicode. Read-time repair does not rewrite user ledgers or provider files.
- Correct isolated runner fixtures for goal ownership and prevent lifecycle
  locks leaking between test event loops. Production Stop/Pause remains
  authoritative.

Beta.55 was cancelled before publication. Beta.56 failed the full release gate
before packaging; neither published release assets, and their tags are retained
unchanged. This new candidate fixes those gate failures and adds the Claude
lifecycle correction. Individual durable `@@` mail routes are separate work
and are not included in beta.57.

Focused validation runs pure modules and selected AST helpers with production
state, server imports, external processes and network access blocked. Desktop
build 174 contains the corresponding inactive-task presentation. Signed release
acceptance still requires the complete GitHub workflow. Publication does not
restart or force-update users' running servers or research jobs.
