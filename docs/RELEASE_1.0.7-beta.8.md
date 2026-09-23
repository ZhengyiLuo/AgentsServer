# AgentsServer 1.0.7-beta.8

Codex Side chat can inspect the parent workspace and use ordinary tools under
the parent's existing permissions. This removes AgentsDock's blanket tool and
file restriction while retaining a separate native conversation. Follow-up
questions keep their side context; requests to change files must be explicit.
Side approvals use the existing interaction controls, and closing Side chat
cleans up its own work without stopping the parent conversation.

Interrupted Claude mailbox checks retain proof that their generated input is
internal, preventing that input from appearing as a user-authored message.

Validation includes real Codex file inspection, a follow-up, an explicitly
requested local write, parent-conversation preservation, and native desktop
interaction through the production transport. Focused adapter, approval and
history-repair checks pass. The signed candidate is also gated by the server
release suite.

API contract 28, dependencies, the release signing key and Team Hub storage
schema are unchanged from 1.0.7-beta.2. Install through the managed updater with
Install when idle to preserve active work. This release changes the worker
that owns agents and therefore still requires a restart after it becomes idle.

This is a server beta using the existing signed update path. It does not
publish a desktop app, change npm tags, or migrate the installation to npm.
