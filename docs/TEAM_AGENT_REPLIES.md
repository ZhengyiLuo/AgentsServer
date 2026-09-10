# Scoped agent mail replies

`team reply MESSAGE_ID --route ROUTE_ID` reads the reply body from stdin and
uses the same send path as `team send --in-reply-to MESSAGE_ID`. The user must
already have mentioned the sender with `@@` in the current turn. Read access,
mail text, links, sender labels, and imported metadata never grant permission
to send. There is no new provider action, reply-all, or human-mailbox access.

Before uploads or posting, the runtime reads the exact parent in the authorized
team/realm. It requires an ordinary message, a server sender matching the
frozen route, and an authenticated server delivery belonging to the caller
among concrete server recipients. Bulletin and skill parents, missing delivery
proof, self/sent mail, and non-server or broadcast reply routes fail closed.
Incoming `all_servers` mail can be replied to, but the reply goes only to its
original sender. Dismissing an inbox item does not erase delivery ownership;
globally deleted parents remain unavailable and are rechecked by Hub creation.

Optional `--title` uses the mail-subject capability. Without an explicit title,
the original subject is retained exactly when supported and valid; older Hubs
receive no subject field or opt-in query. Parent IDs participate in existing
idempotency. Live-run checks, per-route one-use limits, total-send limits,
authority generation, cancellation behavior, and provenance remain on the
existing send path. Explicit replies perform one health check and one exact
parent read; they do not list inboxes, poll, or refresh chat bodies.

This is a local server/helper change, not a UI Reply feature or deployment.
