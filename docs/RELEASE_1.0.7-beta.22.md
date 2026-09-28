# AgentsServer 1.0.7-beta.22

Fix shared conversations going blank after token entry when their history
contains file edits whose private paths were omitted from the shared response.
The file-change card now preserves edit counts and shows available filenames
without crashing the conversation.

This beta includes the server fixes from beta.21 for shared-chat availability,
Codex endpoint switching, startup queue recovery, and asynchronous inter-chat
messages. It also packages the current shared web renderer.

The fix is served by the server; shared-chat viewers can reload their browser
after installation. No new desktop app installation is required.

The server updater installs when the worker is idle. Forcing installation
restarts the worker and interrupts active agent turns. Existing conversations,
credentials, installation locations, and release signing are retained.
This GitHub server release does not itself publish an npm package.
