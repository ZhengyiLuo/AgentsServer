-- Passive Team Networks foundation.  The existing channel ledger remains the
-- storage engine for one reserved Bulletin per team; addressed mailbox items
-- are deliberately separate from channels and from dispatch_requests.

CREATE TRIGGER network_agents_limit_per_server
BEFORE INSERT ON agents
FOR EACH ROW WHEN (
    SELECT COUNT(*) FROM agents WHERE node_id = NEW.node_id
) >= 256
BEGIN
    SELECT RAISE(ABORT, 'network server agent limit exceeded');
END;

CREATE TABLE network_peer_bindings (
    peer_id TEXT PRIMARY KEY CHECK(length(peer_id) = 36),
    team_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    service_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    peer_server_identity TEXT NOT NULL CHECK(length(trim(peer_server_identity)) BETWEEN 8 AND 240),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'revoked')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, peer_id)
);

CREATE UNIQUE INDEX network_one_active_peer_per_server
ON network_peer_bindings(team_id, node_id)
WHERE status = 'active';

CREATE TRIGGER network_peer_binding_requires_exact_identity
BEFORE INSERT ON network_peer_bindings
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM nodes AS n
    JOIN principals AS p ON p.id = NEW.service_principal_id
    JOIN service_accounts AS s ON s.principal_id = p.id
    JOIN memberships AS m
      ON m.team_id = NEW.team_id AND m.principal_id = p.id
    WHERE n.team_id = NEW.team_id
      AND n.id = NEW.node_id
      AND n.server_identity = NEW.peer_server_identity
      AND n.status <> 'revoked'
      AND p.kind = 'service'
      AND p.status = 'active'
      AND s.service_identifier = 'agentsdock.secure-peer.' || NEW.peer_id
      AND m.role = 'automation'
      AND m.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'network peer binding requires exact server authority');
END;

CREATE TRIGGER network_peer_binding_identity_is_immutable
BEFORE UPDATE OF peer_id, team_id, node_id, service_principal_id,
    peer_server_identity, created_at
ON network_peer_bindings
BEGIN
    SELECT RAISE(ABORT, 'network peer binding identity is immutable');
END;

CREATE TRIGGER network_peer_binding_revocation_is_terminal
BEFORE UPDATE ON network_peer_bindings
FOR EACH ROW WHEN
    (OLD.status = 'revoked' AND NEW.status <> 'revoked')
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
    OR (NEW.status = 'revoked' AND NEW.revoked_at IS NULL)
    OR (NEW.status = 'active' AND NEW.revoked_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'network peer revocation is terminal');
END;

CREATE TABLE network_boards (
    team_id TEXT PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE RESTRICT
);

CREATE TRIGGER network_board_requires_shared_board
BEFORE INSERT ON network_boards
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM channels AS c
    WHERE c.team_id = NEW.team_id
      AND c.id = NEW.channel_id
      AND c.kind = 'board'
      AND c.visibility = 'team'
      AND c.archived_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'network Bulletin requires one live shared board');
END;

CREATE TRIGGER network_board_binding_replacement_is_guarded
BEFORE UPDATE ON network_boards
FOR EACH ROW WHEN
    NEW.team_id IS NOT OLD.team_id
    OR NEW.created_at IS NOT OLD.created_at
    OR (
        NEW.channel_id IS NOT OLD.channel_id
        AND (
            EXISTS (
                SELECT 1 FROM channels AS old_channel
                WHERE old_channel.team_id = OLD.team_id
                  AND old_channel.id = OLD.channel_id
                  AND old_channel.kind = 'board'
                  AND old_channel.visibility = 'team'
                  AND old_channel.archived_at IS NULL
            )
            OR NOT EXISTS (
                SELECT 1 FROM channels AS new_channel
                WHERE new_channel.team_id = NEW.team_id
                  AND new_channel.id = NEW.channel_id
                  AND new_channel.kind = 'board'
                  AND new_channel.visibility = 'team'
                  AND new_channel.archived_at IS NULL
            )
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'network Bulletin binding replacement is not permitted');
END;

CREATE TRIGGER network_bulletin_body_limit_on_insert
BEFORE INSERT ON messages
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM network_boards AS b
    WHERE b.team_id = NEW.team_id AND b.channel_id = NEW.channel_id
) AND NOT length(CAST(NEW.body AS BLOB)) BETWEEN 1 AND 8192
BEGIN
    SELECT RAISE(ABORT, 'network Bulletin body exceeds its byte limit');
END;

CREATE TRIGGER network_bulletin_body_limit_on_update
BEFORE UPDATE OF body ON messages
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM network_boards AS b
    WHERE b.team_id = NEW.team_id AND b.channel_id = NEW.channel_id
) AND NOT length(CAST(NEW.body AS BLOB)) BETWEEN 1 AND 8192
BEGIN
    SELECT RAISE(ABORT, 'network Bulletin body exceeds its byte limit');
END;

CREATE TABLE network_mailbox_items (
    queue_ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('message', 'request', 'reply')),
    sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human', 'server', 'agent')),
    sender_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    sender_node_id TEXT,
    sender_agent_id TEXT,
    recipient_kind TEXT NOT NULL CHECK(recipient_kind IN ('human', 'server', 'agent')),
    recipient_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    recipient_node_id TEXT,
    recipient_agent_id TEXT,
    root_request_item_id TEXT,
    body_format TEXT NOT NULL DEFAULT 'markdown' CHECK(body_format IN ('plain', 'markdown')),
    body TEXT NOT NULL CHECK(length(CAST(body AS BLOB)) BETWEEN 1 AND 8192),
    idempotency_key BLOB NOT NULL
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER CHECK(expires_at IS NULL OR expires_at > created_at),
    FOREIGN KEY(team_id, sender_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, sender_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, recipient_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, recipient_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, root_request_item_id)
        REFERENCES network_mailbox_items(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    UNIQUE(team_id, idempotency_key),
    CHECK(
        (sender_kind = 'human' AND sender_node_id IS NULL AND sender_agent_id IS NULL)
        OR (sender_kind = 'server' AND sender_node_id IS NOT NULL AND sender_agent_id IS NULL)
        OR (sender_kind = 'agent' AND sender_node_id IS NOT NULL AND sender_agent_id IS NOT NULL)
    ),
    CHECK(
        (recipient_kind = 'human' AND recipient_node_id IS NULL AND recipient_agent_id IS NULL)
        OR (recipient_kind = 'server' AND recipient_node_id IS NOT NULL AND recipient_agent_id IS NULL)
        OR (recipient_kind = 'agent' AND recipient_node_id IS NOT NULL AND recipient_agent_id IS NOT NULL)
    ),
    CHECK(
        (kind IN ('message', 'request') AND root_request_item_id IS NULL)
        OR (kind = 'reply' AND root_request_item_id IS NOT NULL)
    ),
    CHECK(kind = 'request' OR expires_at IS NULL)
);

CREATE UNIQUE INDEX network_one_reply_per_request
ON network_mailbox_items(root_request_item_id)
WHERE kind = 'reply';

CREATE INDEX network_mailbox_recipient_fifo
ON network_mailbox_items(
    team_id, recipient_kind, recipient_node_id, recipient_agent_id, queue_ordinal
);

CREATE TRIGGER network_mailbox_sender_is_authorized
BEFORE INSERT ON network_mailbox_items
FOR EACH ROW WHEN NOT (
    (
        NEW.sender_kind = 'human'
        AND EXISTS (
            SELECT 1
            FROM principals AS p
            JOIN memberships AS m
              ON m.team_id = NEW.team_id AND m.principal_id = p.id
            WHERE p.id = NEW.sender_principal_id
              AND p.kind = 'human'
              AND p.status = 'active'
              AND m.status = 'active'
        )
    )
    OR (
        NEW.sender_kind IN ('server', 'agent')
        AND EXISTS (
            SELECT 1
            FROM network_peer_bindings AS b
            JOIN nodes AS n
              ON n.team_id = b.team_id AND n.id = b.node_id
            WHERE b.team_id = NEW.team_id
              AND b.node_id = NEW.sender_node_id
              AND b.service_principal_id = NEW.sender_principal_id
              AND b.status = 'active'
              AND n.status = 'active'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'network mailbox sender is not authorized');
END;

CREATE TRIGGER network_mailbox_sender_agent_matches_server
BEFORE INSERT ON network_mailbox_items
FOR EACH ROW WHEN NEW.sender_kind = 'agent' AND NOT EXISTS (
    SELECT 1 FROM agents AS a
    JOIN principals AS p ON p.id = a.principal_id
    WHERE a.team_id = NEW.team_id
      AND a.node_id = NEW.sender_node_id
      AND a.id = NEW.sender_agent_id
      AND a.status = 'active'
      AND p.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'network mailbox sender agent does not belong to server');
END;

CREATE TRIGGER network_mailbox_recipient_is_available
BEFORE INSERT ON network_mailbox_items
FOR EACH ROW WHEN NOT (
    (
        NEW.recipient_kind = 'human'
        AND EXISTS (
            SELECT 1 FROM principals AS p
            JOIN memberships AS m
              ON m.team_id = NEW.team_id AND m.principal_id = p.id
            WHERE p.id = NEW.recipient_principal_id
              AND p.kind = 'human'
              AND p.status = 'active'
              AND m.status = 'active'
        )
    )
    OR (
        NEW.recipient_kind = 'server'
        AND EXISTS (
            SELECT 1 FROM nodes AS n
            WHERE n.team_id = NEW.team_id
              AND n.id = NEW.recipient_node_id
              AND n.principal_id = NEW.recipient_principal_id
              AND n.status = 'active'
        )
    )
    OR (
        NEW.recipient_kind = 'agent'
        AND EXISTS (
            SELECT 1 FROM agents AS a
            JOIN principals AS p ON p.id = a.principal_id
            JOIN nodes AS n ON n.team_id = a.team_id AND n.id = a.node_id
            WHERE a.team_id = NEW.team_id
              AND a.node_id = NEW.recipient_node_id
              AND a.id = NEW.recipient_agent_id
              AND a.principal_id = NEW.recipient_principal_id
              AND a.status = 'active'
              AND p.status = 'active'
              AND n.status = 'active'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'network mailbox recipient is unavailable');
END;

CREATE TRIGGER network_mailbox_reply_requires_request
BEFORE INSERT ON network_mailbox_items
FOR EACH ROW WHEN NEW.kind = 'reply' AND NOT EXISTS (
    SELECT 1 FROM network_mailbox_items AS request_item
    WHERE request_item.team_id = NEW.team_id
      AND request_item.id = NEW.root_request_item_id
      AND request_item.kind = 'request'
)
BEGIN
    SELECT RAISE(ABORT, 'network reply requires an exact passive request');
END;

CREATE TRIGGER network_mailbox_authority_is_immutable
BEFORE UPDATE ON network_mailbox_items
BEGIN
    SELECT RAISE(ABORT, 'network mailbox items are immutable');
END;

CREATE TRIGGER network_mailbox_items_cannot_be_deleted
BEFORE DELETE ON network_mailbox_items
BEGIN
    SELECT RAISE(ABORT, 'network mailbox items cannot be deleted');
END;

CREATE TABLE network_deliveries (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    item_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'available'
        CHECK(state IN ('available', 'delivered', 'read')),
    available_at INTEGER NOT NULL CHECK(available_at >= 0),
    delivered_at INTEGER CHECK(delivered_at IS NULL OR delivered_at >= available_at),
    read_at INTEGER CHECK(read_at IS NULL OR read_at >= delivered_at),
    FOREIGN KEY(team_id, item_id)
        REFERENCES network_mailbox_items(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    CHECK(
        (state = 'available' AND delivered_at IS NULL AND read_at IS NULL)
        OR (state = 'delivered' AND delivered_at IS NOT NULL AND read_at IS NULL)
        OR (state = 'read' AND delivered_at IS NOT NULL AND read_at IS NOT NULL)
    )
);

CREATE TRIGGER network_delivery_identity_is_immutable
BEFORE UPDATE OF id, team_id, item_id, available_at ON network_deliveries
BEGIN
    SELECT RAISE(ABORT, 'network delivery identity is immutable');
END;

CREATE TRIGGER network_delivery_state_is_monotonic
BEFORE UPDATE ON network_deliveries
FOR EACH ROW WHEN
    (OLD.state = 'delivered' AND NEW.state = 'available')
    OR (OLD.state = 'read' AND NEW.state <> 'read')
    OR (OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS NOT OLD.delivered_at)
    OR (OLD.read_at IS NOT NULL AND NEW.read_at IS NOT OLD.read_at)
BEGIN
    SELECT RAISE(ABORT, 'network delivery receipts are monotonic');
END;

CREATE TRIGGER network_deliveries_cannot_be_deleted
BEFORE DELETE ON network_deliveries
BEGIN
    SELECT RAISE(ABORT, 'network deliveries cannot be deleted');
END;

CREATE TABLE network_passive_requests (
    request_item_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'replied', 'expired')),
    reply_item_id TEXT UNIQUE,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    replied_at INTEGER CHECK(replied_at IS NULL OR replied_at >= created_at),
    FOREIGN KEY(team_id, request_item_id)
        REFERENCES network_mailbox_items(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, reply_item_id)
        REFERENCES network_mailbox_items(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, request_item_id),
    CHECK(
        (status = 'open' AND reply_item_id IS NULL AND replied_at IS NULL)
        OR (status = 'replied' AND reply_item_id IS NOT NULL AND replied_at IS NOT NULL)
        OR (status = 'expired' AND reply_item_id IS NULL AND replied_at IS NULL)
    )
);

CREATE TRIGGER network_request_requires_request_item
BEFORE INSERT ON network_passive_requests
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM network_mailbox_items AS item
    WHERE item.team_id = NEW.team_id
      AND item.id = NEW.request_item_id
      AND item.kind = 'request'
      AND item.created_at = NEW.created_at
      AND item.expires_at = NEW.expires_at
)
BEGIN
    SELECT RAISE(ABORT, 'passive request metadata requires its exact item');
END;

CREATE TRIGGER network_request_reply_is_exact
BEFORE UPDATE OF status, reply_item_id, replied_at ON network_passive_requests
FOR EACH ROW WHEN NEW.status = 'replied' AND NOT EXISTS (
    SELECT 1 FROM network_mailbox_items AS reply
    WHERE reply.team_id = NEW.team_id
      AND reply.id = NEW.reply_item_id
      AND reply.kind = 'reply'
      AND reply.root_request_item_id = NEW.request_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'passive request reply does not match');
END;

CREATE TRIGGER network_request_state_is_forward_only
BEFORE UPDATE ON network_passive_requests
FOR EACH ROW WHEN
    OLD.status <> 'open'
    OR NEW.status NOT IN ('replied', 'expired')
    OR NEW.request_item_id IS NOT OLD.request_item_id
    OR NEW.team_id IS NOT OLD.team_id
    OR NEW.created_at IS NOT OLD.created_at
    OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'passive request state is forward-only');
END;

CREATE TRIGGER network_requests_cannot_be_deleted
BEFORE DELETE ON network_passive_requests
BEGIN
    SELECT RAISE(ABORT, 'passive requests cannot be deleted');
END;
