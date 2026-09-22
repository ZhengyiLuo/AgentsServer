# Recovering a Team Network host address change

A healthy main AgentsServer does not prove that its separate secure-peer
listener is healthy. If the configured IPv4 address is no longer assigned to
the host, the listener cannot bind. Restarting with the same configuration
cannot fix that. Existing members retain their approved credentials, but their
saved endpoint still points to the previous address.

## Host recovery

Use the authenticated native `PUT /api/admin/secure-peers/v1/host` control to
select an address actually assigned to the host and reachable by its members.
The request requires the current server identity/instance and confirmation.
The address-unavailable error now explains what happened and permits an
explicit reconfiguration without restarting the main AgentsServer. It never
silently binds every interface, selects a different network, or resets trust.

On older servers whose failed attachment prevents direct reconfiguration,
disable only the secure-peer listener through that control, then enable it at
the correct address. This does not change the Team Hub role or revoke members.
Changing the host endpoint alone does **not** migrate existing member servers.

## Member recovery without rejoining

On a member release advertising `secure_peer_v1.endpoint_update_version: 1`,
use the authenticated native control:

```
PUT /api/admin/secure-peers/v1/connections/{connection_id}/endpoint
```

The JSON body contains:

- `request_id`: a new canonical UUIDv4 for this control request;
- `expected_server_identity` and `expected_server_instance_id`: the current
  **member** AgentsServer, not the host;
- `confirmed: true`;
- `expected_host_server_identity` and `expected_hub_id`: the already trusted
  remote host and Hub;
- `expected_host_ip` and `expected_port`: the currently saved endpoint;
- `host_ip` and `port`: the explicitly selected replacement endpoint.

Read identities and the current endpoint from the member's authenticated
health/status controls. Never copy another user's access token or private keys.
This operation requires that member's operator credentials; host approval
does not authorize administration of a member's own AgentsServer.

Before writing, the member contacts the candidate using its existing client
certificate and pinned host CA. TLS and the authenticated health response must
identify the same host, Hub, peer, team and client certificate. Wrong hosts,
bad certificates and unreachable endpoints leave the old endpoint untouched.
Concurrent connection, credential, renewal and endpoint changes invalidate the
attempt. Successful migration preserves approval, routes and active/inactive
selection; it does not grant permissions or reactivate a disconnected member.

After a lost response, read status before retrying: if the saved endpoint is
already the requested replacement, migration succeeded. The old-endpoint
precondition intentionally rejects a stale repeated control request.

Verify authenticated member health and read-only Team Network access after
migration. Do not test delivery by silently sending real mail or granting new
routes. Members still using an older release need this client-side support
before using the endpoint control; upgrading only the host is insufficient.

## Avoiding another address change

Use an address kept stable by the network administrator (for example, a DHCP
reservation) and reachable by every intended member. A private overlay address
is suitable only if all those members can reach that network. Never assign an
old DHCP address merely because a probe finds no response: it may belong to
another device or be reassigned later.

The recovery controls add no inbox polling, discovery scans or automatic
endpoint switching. They use explicit operator actions and existing transport
maintenance; agent chats and goals are not restarted.
