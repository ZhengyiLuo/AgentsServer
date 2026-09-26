# AgentsServer 1.0.7-beta.9

After a Codex CLI upgrade, Recheck CLI makes the installed version available
to new chats. Running chats retain their original process until their work
finishes; idle chats resume their existing native history on the updated CLI.
This avoids restarting the whole server just to pick up a provider upgrade.

Server updates also accept existing user-owned Python installations created
with group-write permissions. Preparation and activation preserve those
permissions instead of requiring manual changes to a shared Python runtime.

Validation includes concurrent real GPT-6 Sol requests through the desktop
app with ChatGPT authentication, an uninterrupted older turn, and a contextual
follow-up preserving its native thread ID. A controlled launcher-version change
exercises process handoff around the installed CLI. Focused tests also cover
late callbacks, approvals, goals, shutdown and existing Python permissions.

API contract 28, dependencies, the release signing key and Team Hub storage
schema are unchanged. Installing this server correction still requires the
agent-owning worker to restart after it becomes idle. Subsequent Codex CLI
upgrades use the new process handoff.

This is a server beta using the existing signed update path. It does not
publish a desktop app, change npm tags, or migrate the installation to npm.
