# AgentsServer 1.0.7-beta.10

Side chats can finish long answers without an AgentsDock-imposed answer
deadline. Remove the shared 150-second cutoff and Claude's separate native
control cutoff. Codex side chats use the same startup and request settings as
ordinary Codex chats.

Stop, Clear, disconnection and server shutdown still cancel their owned side
work. A long answer no longer loses its native conversation just because time
has passed. Follow-up context and the main conversation remain separate.

The desktop correction also removes its former 210-second request cutoff;
both the corrected app and server are needed to remove both limits.

API contract 28, release signing and existing installation methods are
unchanged. Install through the normal managed updater when current work is
idle.
