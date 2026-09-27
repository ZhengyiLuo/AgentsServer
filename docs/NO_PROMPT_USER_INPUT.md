# User questions in no-prompt modes

Codex turns using approval policy `never` and Claude SDK turns using permission
mode `dontAsk` or `bypassPermissions` skip native user-input questions without
installing a desktop interaction or waiting for an answer.

- Codex `item/tool/requestUserInput` receives the existing least-privilege
  response, `{"answers": {}}`.
- Claude `AskUserQuestion` receives `PermissionResultDeny` with
  `interrupt=False` and an explicit skipped-question explanation, matching the
  existing empty-answer path. It does not receive an invented answer or tool
  approval. The native agent may continue or finish within existing authority.
- Ownership, Stop and deletion checks remain ahead of the skip decision. No
  pending-interaction entry, handler task, user-action count or requested/resolved
  interaction event is created for a skipped question.
- The decision uses the policy captured for the active turn. Settings edited
  during a turn apply to the next turn. A Codex side conversation uses its own
  captured policy, independently of a concurrent parent turn.
- Normal prompting modes retain question cards and answer/skip handling.
  Other tool approval behavior is unchanged. This does not rewrite ordinary
  assistant text or automatically authorize a blocked operation.

## Verification and local acceptance

Focused regressions exercise real server handlers with isolated state and
synthetic provider callbacks. They cover immediate skip, no pending state or
interaction events, normal answering, mode changes, side-chat policy ownership,
runner policy capture and the existing timeout/answer race. Native CLI/App
acceptance remains separate; the source tests do not establish a live-provider
round trip.

On an explicitly authorized test server with this source:

1. Create disposable Codex and Claude chats. Set Codex to No prompts (`never`),
   and test Claude with both Don't ask (`dontAsk`) and Full access
   (`bypassPermissions`).
2. Ask each agent to invoke its native question tool to choose between two
   harmless labels, then finish with a fixed marker if the question is skipped.
   Confirm a real question invocation; a model that never invokes the tool does
   not establish this boundary. The turn should continue or finish without a
   question card or waiting-for-user badge, and without inventing an answer.
3. Repeat under Codex `on-request` and Claude `default`: the question must appear,
   accept a response and resume the same turn. Also exercise the existing Skip.
4. Change the mode while a turn runs: the original turn keeps its captured
   policy, while the next turn uses the new setting. Reopen the chat and check
   that no skipped question remains pending.

This change does not install, restart or publish a server. Actual desktop and
provider acceptance must be recorded after the authorized local test.
