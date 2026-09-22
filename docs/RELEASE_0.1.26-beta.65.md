# AgentsServer 0.1.26-beta.65

- Support direct HTTP or HTTPS chat-sharing links using the connected server's
  address. Compatible desktop builds offer two actions: View only and
  Interactive. No separate domain configuration or preview/digest step is
  required for that workflow; legacy snapshot clients remain supported.
- Serve styled read-only snapshots and the scoped interactive browser UI.
  Interactive invitations remain one-use, origin-bound and CSRF-protected;
  HTTP uses a separate HttpOnly, SameSite cookie. Interactive access does not
  expose file browsing, terminal, administration or other chats.
- Wake an idle recipient once for unread cross-chat mail through the existing
  queue. Coalesce arrivals, respect Stop and user queue priority, and keep busy
  recipients on their existing non-interrupting notification path. No new
  background polling is introduced.
- Keep previously delivered mail readable after its sender is archived, when
  the exact permanent pair remains authorized. This also permits an idle
  recipient to wake and read that mail. Archival still blocks new sends and
  replies to the archived chat; revocation and deletion remain enforced.

API contract remains 28. The desktop sharing workflow requires a compatible
desktop build (0.2.13-beta.36 or later). HTTP is unencrypted and is intended for
trusted networks; use HTTPS on untrusted networks. Links must still reach the
server: this release does not open firewall ports, configure proxies or tunnels,
create shares, or change existing ingress.

Validation includes isolated mailbox lifecycle and authorization regressions,
sharing storage/route tests, and real HTTP browser checks of synthetic chat
fixtures: Join, reload, queued send, Send now, Stop, origin/CSRF rejection,
one-use redemption, and desktop/mobile snapshot rendering. Tests do not modify
real chat histories or run users' providers. Release publication alone does not
install or restart any running server.
