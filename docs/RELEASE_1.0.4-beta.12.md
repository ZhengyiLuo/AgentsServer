# AgentsServer 1.0.4-beta.12

This beta introduces npm installation and coordinated updates with AgentsDock.
The app requests its matching server release automatically; existing managed
servers retain the signed legacy update path during migration.

- Download and prepare dependencies while agents work. Activate the update once
  current work finishes, preserving server identity, configuration and chat data.
- Run the public gateway separately from execution so a gateway restart leaves
  agents, tools and pending approvals running.
- Register independent native recovery before stopping either main service.
  Recover an interrupted activation without requiring an app connection, and
  distinguish a verified rollback from a completed update.
- Verify both running component versions and released admission before reporting
  update success. Pin authenticated local probes to the native process owning
  the connected socket.
- Preserve an installation's managed runtime outside the npm cache. Retry failed
  first installs safely and retain data when uninstalling the managed service.

Execution-runtime replacement still waits for idle; simultaneous old and new
execution runtimes are not supported in this beta. Older custom-path macOS
installations need one migration with their original installation, configuration
and state directory settings. Default-path installations migrate automatically.
