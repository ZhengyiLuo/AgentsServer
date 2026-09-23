# AgentsServer 1.0.7-beta.6

Draft release notes — candidate validation is not complete; not published.

- Add optional OpenCode CLI 1.18.29 support: chats, native resume, streamed
  tools/text, uploads, Stop, queued follow-ups and validated local skills.
- Expose OpenCode models and permission modes to the matching desktop beta.
  Keep unsupported operations explicit and reset unsafe resume bindings.
- Retain the Claude Goals, interruption, history and mailbox corrections from
  the previous beta candidates, including stopped-run input ownership repair.

Install and authenticate OpenCode separately on the server host. OpenCode has
no live steering, native goals, public forks, side chats, external-history
import or cross-chat routes in this beta. Plan only is a tool policy, not an
OS sandbox. Free-provider restrictions may require a different authenticated
provider for permission-controlled turns and selected skills.

The planned server beta uses the signed GitHub update path; it does not require
npm or block installation of the desktop app. Publishing is not host deployment.
