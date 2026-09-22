# AgentsServer 1.0.2

Stable update from **1.0.0**, including the changes validated in
1.0.1-beta.1 and 1.0.1-beta.2, plus secure Team Network endpoint recovery.
Team Network remains a beta feature within this stable server release.

## Recover Team Network after an address change

- Report when the host's configured IPv4 address is no longer assigned to
  its machine. An authenticated operator can select a current local address
  without restarting the main server, replacing its CA or revoking members.
- Let an approved member update its saved host endpoint without rejoining.
  The candidate must pass TLS verification with the existing pinned CA and
  client certificate and identify the same host, Hub, peer and team before
  the endpoint is saved.
- Preserve approved credentials, routes and active/inactive selection. Failed
  probes and concurrent trust, renewal or endpoint changes cannot silently
  replace the saved endpoint or reconnect a deactivated member.
- Retain the prior host configuration after a failed change and restore a
  previously live listener when possible. Preserve notification callbacks
  through reconfiguration and rollback.

Recovery is an explicit operator action; no address discovery, automatic
network switching or inbox polling is added. Updating the host does not change
addresses already saved by members. Members need this release's endpoint
control to migrate their own saved connection. See
[endpoint recovery](https://github.com/ZhengyiLuo/AgentsServer/blob/v1.0.2/docs/SECURE_PEER_ENDPOINT_RECOVERY.md).

## More reliable Team Network collaboration

- Fix valid `@@` mail from an approved joined server being rejected as
  unavailable by the receiving host's gateway. Retain exact recipient identity,
  revocation and idempotent retry checks. Previously rejected mail is not
  automatically resent; the sender can submit it again after the host updates.
- Keep new join requests pending until approved, rejected or cancelled when
  both host and joining server support durable approval. Waiting survives
  restarts and lost responses without granting access automatically. Remove
  the 16-pending-request limit shared by teammates behind one source IP while
  retaining overall resource and flood bounds.
- Add indexed, permission-scoped Mail and Bulletin search with explicit
  queries and pagination.
- Let agents use native Team Network tools to read the exact recipient or
  Bulletin selected with `@@`, including sender-filtered history pages. Reading
  does not grant permission to send or post.
- Clarify sender formatting guidance so ordinary text and technical values
  keep their whitespace. Existing message bodies are not rewritten.

Already expired joins need a fresh request; rejected or cancelled requests are
not revived. Legacy peers retain their original deadlines. Durable pending
joins are not supported by older server binaries.

## Independent Side chat

- Answer temporary side questions and follow-ups for Codex and Claude using
  recent visible conversation text and the side conversation's own history.
  Side chat does not send, steer, stop or queue a main turn, pause its goal, or
  add messages to the main chat's history.
- Give each question separate provider execution and cancellation, with
  bounded context and authenticated requests. Exclude hidden reasoning,
  attachments and tool results from its context; isolate inherited runtime
  instructions and workspace access. Unsupported provider isolation fails
  explicitly.
- Include Side chat cleanup in the cooperative shutdown budget and align the
  installer's launchd wait with that complete budget.

Side chat history is temporary, and each follow-up uses a fresh isolated
provider invocation. Side chat and indexed search need a supporting desktop
build. Provider tools must be installed and authenticated for the service user.

## Compatibility and updating

- API contract remains **28**. Team Hub schema advances from **22** in 1.0.0
  to **23** for indexed message search, as already shipped in 1.0.1-beta.2.
  Back up before upgrading; a schema-crossing downgrade requires a compatible
  backup rather than simply replacing the runtime.
- No dependency or release signing-key change from the preceding stable and
  beta releases. Older clients retain their negotiated behavior; new controls
  require the corresponding advertised server capability.
- Install through the existing server updater on the Stable channel. Both
  1.0.0 and 1.0.1-beta installations can upgrade to 1.0.2. Publishing the
  package alone does not install it or restart a running server.
