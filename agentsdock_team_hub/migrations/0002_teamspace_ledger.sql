-- Teamspace collaboration ledger. Messages are passive records. Explicit
-- dispatch requests live in a separate table and are the only future source
-- of agent execution admission.

CREATE TABLE channels (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('direct', 'board', 'announcements')),
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private', 'team')),
    slug TEXT CHECK(slug IS NULL OR (
        slug = lower(trim(slug)) AND length(slug) BETWEEN 1 AND 80
    )),
    display_name TEXT CHECK(display_name IS NULL OR length(trim(display_name)) BETWEEN 1 AND 160),
    direct_pair_key BLOB CHECK(direct_pair_key IS NULL OR (
        typeof(direct_pair_key) = 'blob' AND length(direct_pair_key) = 32
    )),
    created_by_principal_id TEXT NOT NULL,
    next_message_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_message_sequence >= 1),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    archived_at INTEGER CHECK(archived_at IS NULL OR archived_at >= created_at),
    FOREIGN KEY(team_id, created_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    UNIQUE(team_id, slug),
    UNIQUE(team_id, direct_pair_key),
    CHECK(
        (kind = 'direct' AND direct_pair_key IS NOT NULL AND slug IS NULL)
        OR (kind <> 'direct' AND direct_pair_key IS NULL AND slug IS NOT NULL)
    )
);

CREATE TABLE channel_acl_entries (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('principal', 'role')),
    subject_principal_id TEXT REFERENCES principals(id) ON DELETE CASCADE,
    subject_role TEXT CHECK(subject_role IN ('owner', 'admin', 'member', 'guest', 'automation')),
    can_read INTEGER NOT NULL CHECK(can_read IN (0, 1)),
    can_post INTEGER NOT NULL CHECK(can_post IN (0, 1)),
    can_manage INTEGER NOT NULL CHECK(can_manage IN (0, 1)),
    can_dispatch INTEGER NOT NULL CHECK(can_dispatch IN (0, 1)),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE CASCADE,
    CHECK(
        (subject_kind = 'principal' AND subject_principal_id IS NOT NULL AND subject_role IS NULL)
        OR (subject_kind = 'role' AND subject_principal_id IS NULL AND subject_role IS NOT NULL)
    )
);

CREATE UNIQUE INDEX channel_acl_one_principal_entry
ON channel_acl_entries(channel_id, subject_principal_id)
WHERE subject_kind = 'principal';

CREATE UNIQUE INDEX channel_acl_one_role_entry
ON channel_acl_entries(channel_id, subject_role)
WHERE subject_kind = 'role';

CREATE TRIGGER channel_acl_require_team_principal
BEFORE INSERT ON channel_acl_entries
FOR EACH ROW WHEN NEW.subject_kind = 'principal' AND NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.subject_principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'channel ACL principal does not belong to this team');
END;

CREATE TRIGGER channel_acl_subject_is_immutable
BEFORE UPDATE OF id, team_id, channel_id, subject_kind,
    subject_principal_id, subject_role, created_at
ON channel_acl_entries
BEGIN
    SELECT RAISE(ABORT, 'channel ACL subject is immutable');
END;

CREATE TABLE channel_participants (
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    participant_role TEXT NOT NULL DEFAULT 'member' CHECK(participant_role IN ('member', 'manager')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'left', 'removed')),
    last_read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_read_sequence >= 0),
    joined_at INTEGER NOT NULL CHECK(joined_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= joined_at),
    PRIMARY KEY(channel_id, principal_id),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE CASCADE,
    UNIQUE(team_id, channel_id, principal_id)
);

CREATE TRIGGER channel_participants_require_team_principal
BEFORE INSERT ON channel_participants
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR (
            p.scope_team_id IS NULL
            AND EXISTS (
                SELECT 1 FROM memberships AS m
                WHERE m.team_id = NEW.team_id
                  AND m.principal_id = p.id
                  AND m.status = 'active'
            )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'channel participant does not belong to this team');
END;

CREATE TRIGGER channel_participant_identity_is_immutable
BEFORE UPDATE OF team_id, channel_id, principal_id, joined_at
ON channel_participants
BEGIN
    SELECT RAISE(ABORT, 'channel participant identity is immutable');
END;

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_sequence INTEGER NOT NULL CHECK(channel_sequence >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('post', 'announcement', 'system')),
    thread_root_message_id TEXT,
    parent_message_id TEXT,
    author_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    acting_human_principal_id TEXT,
    provenance_node_id TEXT,
    provenance_agent_id TEXT,
    provenance_chat_id TEXT,
    provenance_run_id TEXT,
    body_format TEXT NOT NULL DEFAULT 'markdown' CHECK(body_format IN ('plain', 'markdown')),
    body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 65536),
    idempotency_key BLOB NOT NULL
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    edited_at INTEGER CHECK(edited_at IS NULL OR edited_at >= created_at),
    deleted_at INTEGER CHECK(deleted_at IS NULL OR deleted_at >= created_at),
    expires_at INTEGER CHECK(expires_at IS NULL OR expires_at > created_at),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, channel_id, thread_root_message_id)
        REFERENCES messages(team_id, channel_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, channel_id, parent_message_id)
        REFERENCES messages(team_id, channel_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, acting_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_chat_id) REFERENCES chats(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    UNIQUE(channel_id, channel_sequence),
    UNIQUE(team_id, idempotency_key),
    UNIQUE(team_id, id),
    UNIQUE(team_id, channel_id, id),
    CHECK(parent_message_id IS NULL OR thread_root_message_id IS NOT NULL),
    CHECK(parent_message_id IS NULL OR parent_message_id <> id),
    CHECK(thread_root_message_id IS NULL OR thread_root_message_id <> id),
    CHECK(
        (
            acting_human_principal_id IS NULL
            AND provenance_agent_id IS NULL
            AND provenance_node_id IS NULL
            AND provenance_chat_id IS NULL
            AND provenance_run_id IS NULL
        )
        OR (
            acting_human_principal_id IS NOT NULL
            AND provenance_agent_id IS NOT NULL
            AND provenance_node_id IS NOT NULL
            AND provenance_chat_id IS NOT NULL
            AND provenance_run_id IS NOT NULL
        )
    )
);

CREATE TRIGGER messages_require_team_author
BEFORE INSERT ON messages
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.author_principal_id
      AND p.kind IN ('human', 'service', 'agent')
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR (
            p.scope_team_id IS NULL
            AND EXISTS (
                SELECT 1 FROM memberships AS m
                WHERE m.team_id = NEW.team_id
                  AND m.principal_id = p.id
                  AND m.status = 'active'
            )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'message author does not belong to this team');
END;

CREATE TRIGGER messages_reject_false_agent_provenance
BEFORE INSERT ON messages
FOR EACH ROW WHEN NEW.provenance_agent_id IS NOT NULL AND COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.author_principal_id), ''
) <> 'agent'
BEGIN
    SELECT RAISE(ABORT, 'only an agent author may claim agent provenance');
END;

CREATE TRIGGER messages_require_agent_provenance
BEFORE INSERT ON messages
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.author_principal_id), ''
) = 'agent' AND NOT EXISTS (
    SELECT 1
    FROM agents AS a
    JOIN chats AS c ON c.team_id = a.team_id AND c.id = NEW.provenance_chat_id
    JOIN runs AS r ON r.team_id = a.team_id AND r.id = NEW.provenance_run_id
    WHERE a.team_id = NEW.team_id
      AND a.id = NEW.provenance_agent_id
      AND a.principal_id = NEW.author_principal_id
      AND a.node_id = NEW.provenance_node_id
      AND c.node_id = a.node_id
      AND c.agent_id = a.id
      AND r.node_id = a.node_id
      AND r.agent_id = a.id
      AND r.chat_id = c.id
      AND r.acting_human_principal_id = NEW.acting_human_principal_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent-authored message requires exact node, agent, chat, and run provenance');
END;

CREATE TRIGGER message_authority_is_immutable
BEFORE UPDATE OF id, team_id, channel_id, channel_sequence, kind,
    thread_root_message_id, parent_message_id, author_principal_id,
    acting_human_principal_id, provenance_node_id, provenance_agent_id,
    provenance_chat_id, provenance_run_id, idempotency_key, created_at
ON messages
BEGIN
    SELECT RAISE(ABORT, 'message routing and provenance are immutable');
END;

CREATE TABLE message_recipients (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    recipient_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL CHECK(reason IN ('direct', 'mention', 'subscription', 'assignment')),
    delivery_key BLOB NOT NULL UNIQUE
        CHECK(typeof(delivery_key) = 'blob' AND length(delivery_key) = 32),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'available', 'delivered', 'expired')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER CHECK(expires_at IS NULL OR expires_at > created_at),
    FOREIGN KEY(team_id, message_id) REFERENCES messages(team_id, id) ON DELETE CASCADE,
    UNIQUE(message_id, recipient_principal_id, reason),
    UNIQUE(team_id, id)
);

CREATE TRIGGER message_recipients_require_team_principal
BEFORE INSERT ON message_recipients
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.recipient_principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'message recipient does not belong to this team');
END;

CREATE TRIGGER message_recipient_identity_is_immutable
BEFORE UPDATE OF id, team_id, message_id, recipient_principal_id,
    reason, delivery_key, created_at
ON message_recipients
BEGIN
    SELECT RAISE(ABORT, 'message recipient identity is immutable');
END;

CREATE TABLE message_receipts (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL UNIQUE,
    delivered_at INTEGER CHECK(delivered_at IS NULL OR delivered_at >= 0),
    read_at INTEGER CHECK(read_at IS NULL OR read_at >= 0),
    acknowledged_at INTEGER CHECK(acknowledged_at IS NULL OR acknowledged_at >= 0),
    FOREIGN KEY(team_id, recipient_id) REFERENCES message_recipients(team_id, id) ON DELETE CASCADE,
    CHECK(read_at IS NULL OR delivered_at IS NOT NULL),
    CHECK(acknowledged_at IS NULL OR delivered_at IS NOT NULL)
);

CREATE TABLE message_mentions (
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    PRIMARY KEY(message_id, principal_id, start_offset),
    FOREIGN KEY(team_id, message_id) REFERENCES messages(team_id, id) ON DELETE CASCADE
);

CREATE TRIGGER message_mentions_require_team_principal
BEFORE INSERT ON message_mentions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'mentioned principal does not belong to this team');
END;

CREATE TRIGGER message_mentions_are_immutable
BEFORE UPDATE ON message_mentions
BEGIN
    SELECT RAISE(ABORT, 'message mentions are immutable');
END;

CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(name = lower(trim(name))) CHECK(length(name) BETWEEN 1 AND 64),
    color TEXT CHECK(color IS NULL OR length(color) BETWEEN 1 AND 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    UNIQUE(team_id, name),
    UNIQUE(team_id, id)
);

CREATE TABLE message_tags (
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    added_by_principal_id TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(message_id, tag_id),
    FOREIGN KEY(team_id, message_id) REFERENCES messages(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, tag_id) REFERENCES tags(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, added_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT
);

CREATE TABLE pins (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    pinned_by_principal_id TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, channel_id, message_id)
        REFERENCES messages(team_id, channel_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, pinned_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(channel_id, message_id)
);

CREATE TABLE assignments (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    assignee_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    assigned_by_principal_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'in_progress', 'done', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(team_id, message_id) REFERENCES messages(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, assigned_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(message_id, assignee_principal_id)
);

CREATE TRIGGER assignments_require_team_principal
BEFORE INSERT ON assignments
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.assignee_principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'assignee does not belong to this team');
END;

CREATE TRIGGER assignment_identity_is_immutable
BEFORE UPDATE OF id, team_id, message_id, assignee_principal_id,
    assigned_by_principal_id, created_at
ON assignments
BEGIN
    SELECT RAISE(ABORT, 'assignment identity is immutable');
END;

CREATE TABLE dispatch_requests (
    queue_ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    requester_kind TEXT NOT NULL DEFAULT 'human' CHECK(requester_kind IN ('human', 'agent')),
    requested_by_human_principal_id TEXT NOT NULL,
    requesting_node_id TEXT,
    requesting_agent_id TEXT,
    requesting_chat_id TEXT,
    requesting_run_id TEXT,
    authorization_capability_id TEXT,
    source_message_id TEXT,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('agent', 'chat')),
    target_node_id TEXT NOT NULL,
    target_agent_id TEXT,
    target_chat_id TEXT,
    request_text TEXT NOT NULL CHECK(length(request_text) BETWEEN 1 AND 65536),
    idempotency_key BLOB NOT NULL
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK(status IN ('registered', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired')),
    target_run_id TEXT,
    causation_dispatch_id TEXT,
    root_dispatch_id TEXT,
    hop_count INTEGER NOT NULL DEFAULT 0 CHECK(hop_count BETWEEN 0 AND 4),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    terminal_at INTEGER CHECK(terminal_at IS NULL OR terminal_at >= created_at),
    FOREIGN KEY(team_id, requested_by_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, requesting_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, requesting_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, requesting_chat_id) REFERENCES chats(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, requesting_run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, authorization_capability_id)
        REFERENCES turn_capabilities(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, source_message_id) REFERENCES messages(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_node_id, target_agent_id)
        REFERENCES agents(team_id, node_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_node_id, target_chat_id)
        REFERENCES chats(team_id, node_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, causation_dispatch_id)
        REFERENCES dispatch_requests(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, root_dispatch_id)
        REFERENCES dispatch_requests(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, idempotency_key),
    UNIQUE(team_id, id),
    CHECK(
        (
            requester_kind = 'human'
            AND requesting_node_id IS NULL
            AND requesting_agent_id IS NULL
            AND requesting_chat_id IS NULL
            AND requesting_run_id IS NULL
            AND authorization_capability_id IS NULL
        )
        OR (
            requester_kind = 'agent'
            AND requesting_node_id IS NOT NULL
            AND requesting_agent_id IS NOT NULL
            AND requesting_chat_id IS NOT NULL
            AND requesting_run_id IS NOT NULL
            AND authorization_capability_id IS NOT NULL
        )
    ),
    CHECK(requester_kind = 'human' OR causation_dispatch_id IS NOT NULL),
    CHECK(
        (target_kind = 'agent' AND target_agent_id IS NOT NULL AND target_chat_id IS NULL)
        OR (
            target_kind = 'chat'
            AND target_agent_id IS NULL
            AND target_chat_id IS NOT NULL
        )
    ),
    CHECK(causation_dispatch_id IS NULL OR causation_dispatch_id <> id),
    CHECK(root_dispatch_id IS NULL OR root_dispatch_id <> id),
    CHECK(
        (
            causation_dispatch_id IS NULL
            AND root_dispatch_id IS NULL
            AND hop_count = 0
        )
        OR (
            causation_dispatch_id IS NOT NULL
            AND root_dispatch_id IS NOT NULL
            AND hop_count BETWEEN 1 AND 4
        )
    )
);

CREATE TRIGGER dispatch_requires_active_human_requester
BEFORE INSERT ON dispatch_requests
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM memberships AS m
    JOIN principals AS p ON p.id = m.principal_id
    WHERE m.team_id = NEW.team_id
      AND m.principal_id = NEW.requested_by_human_principal_id
      AND m.status = 'active'
      AND p.kind = 'human'
      AND p.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'dispatch requires an active human requester');
END;

CREATE TRIGGER dispatch_agent_request_requires_exact_provenance
BEFORE INSERT ON dispatch_requests
FOR EACH ROW WHEN NEW.requester_kind = 'agent' AND NOT EXISTS (
    SELECT 1
    FROM agents AS a
    JOIN chats AS c
      ON c.team_id = a.team_id
     AND c.node_id = a.node_id
     AND c.agent_id = a.id
     AND c.id = NEW.requesting_chat_id
    JOIN runs AS r
      ON r.team_id = a.team_id
     AND r.node_id = a.node_id
     AND r.agent_id = a.id
     AND r.chat_id = c.id
     AND r.id = NEW.requesting_run_id
    JOIN turn_capabilities AS capability
      ON capability.team_id = r.team_id
     AND capability.issued_to_run_id = r.id
     AND capability.id = NEW.authorization_capability_id
    WHERE a.team_id = NEW.team_id
      AND a.id = NEW.requesting_agent_id
      AND a.node_id = NEW.requesting_node_id
      AND r.acting_human_principal_id = NEW.requested_by_human_principal_id
      AND capability.issued_by_principal_id = NEW.requested_by_human_principal_id
      AND capability.action = 'dispatch'
      AND capability.resource_type = NEW.target_kind
      AND capability.resource_id = CASE NEW.target_kind
          WHEN 'agent' THEN NEW.target_agent_id
          ELSE NEW.target_chat_id
      END
)
BEGIN
    SELECT RAISE(ABORT, 'agent dispatch requires exact delegated provenance');
END;

CREATE TRIGGER dispatch_target_run_must_match
BEFORE INSERT ON dispatch_requests
FOR EACH ROW WHEN NEW.target_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM runs AS r
    WHERE r.team_id = NEW.team_id
      AND r.id = NEW.target_run_id
      AND r.node_id = NEW.target_node_id
      AND (
        (NEW.target_kind = 'agent' AND r.agent_id = NEW.target_agent_id)
        OR (NEW.target_kind = 'chat' AND r.chat_id = NEW.target_chat_id)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'dispatch target run does not match its exact target');
END;

CREATE TRIGGER dispatch_target_run_update_must_match
BEFORE UPDATE OF target_run_id ON dispatch_requests
FOR EACH ROW WHEN NEW.target_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM runs AS r
    WHERE r.team_id = NEW.team_id
      AND r.id = NEW.target_run_id
      AND r.node_id = NEW.target_node_id
      AND (
        (NEW.target_kind = 'agent' AND r.agent_id = NEW.target_agent_id)
        OR (NEW.target_kind = 'chat' AND r.chat_id = NEW.target_chat_id)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'dispatch target run does not match its exact target');
END;

CREATE TRIGGER dispatch_target_run_is_one_way
BEFORE UPDATE OF target_run_id ON dispatch_requests
FOR EACH ROW WHEN OLD.target_run_id IS NOT NULL AND NEW.target_run_id IS NOT OLD.target_run_id
BEGIN
    SELECT RAISE(ABORT, 'dispatch target run binding is one-way');
END;

CREATE TRIGGER dispatch_causation_must_be_bounded
BEFORE INSERT ON dispatch_requests
FOR EACH ROW WHEN NEW.causation_dispatch_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM dispatch_requests AS parent
    WHERE parent.team_id = NEW.team_id
      AND parent.id = NEW.causation_dispatch_id
      AND NEW.root_dispatch_id = COALESCE(parent.root_dispatch_id, parent.id)
      AND NEW.hop_count = parent.hop_count + 1
      AND parent.hop_count < 4
      AND (
        NEW.requester_kind = 'human'
        OR parent.target_run_id = NEW.requesting_run_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'dispatch causation must extend its bounded team-local chain');
END;

CREATE TRIGGER dispatch_authority_is_immutable
BEFORE UPDATE OF queue_ordinal, id, team_id, requester_kind,
    requested_by_human_principal_id,
    requesting_node_id, requesting_agent_id, requesting_chat_id,
    requesting_run_id, authorization_capability_id,
    source_message_id, target_kind, target_node_id, target_agent_id,
    target_chat_id, request_text, idempotency_key, causation_dispatch_id,
    root_dispatch_id, hop_count, created_at, expires_at
ON dispatch_requests
BEGIN
    SELECT RAISE(ABORT, 'dispatch authority is immutable');
END;

CREATE INDEX dispatch_requests_target_fifo
ON dispatch_requests(team_id, target_node_id, target_chat_id, queue_ordinal)
WHERE status IN ('registered', 'queued');

CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    subscriber_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('mentions', 'all')),
    delivery_mode TEXT NOT NULL DEFAULT 'inbox' CHECK(delivery_mode = 'inbox'),
    wake_mode TEXT NOT NULL DEFAULT 'never' CHECK(wake_mode = 'never'),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'revoked')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(team_id, channel_id) REFERENCES channels(team_id, id) ON DELETE CASCADE,
    UNIQUE(channel_id, subscriber_principal_id),
    UNIQUE(team_id, id)
);

CREATE TRIGGER subscriptions_require_team_principal
BEFORE INSERT ON subscriptions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.subscriber_principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'subscriber does not belong to this team');
END;

CREATE TRIGGER subscription_identity_is_immutable
BEFORE UPDATE OF id, team_id, subscriber_principal_id, channel_id,
    delivery_mode, wake_mode, created_at
ON subscriptions
BEGIN
    SELECT RAISE(ABORT, 'subscription identity is immutable');
END;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('attachment', 'runbook', 'skill', 'diff', 'signature')),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 240),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    retention_until INTEGER CHECK(retention_until IS NULL OR retention_until > created_at),
    UNIQUE(team_id, id)
);

CREATE TRIGGER artifacts_require_team_creator
BEFORE INSERT ON artifacts
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.created_by_principal_id
      AND p.status = 'active'
      AND p.kind IN ('human', 'service', 'agent')
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'artifact creator does not belong to this team');
END;

CREATE TRIGGER artifact_identity_is_immutable
BEFORE UPDATE OF id, team_id, kind, created_by_principal_id, created_at
ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact identity is immutable');
END;

CREATE TABLE artifact_versions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    media_type TEXT NOT NULL CHECK(length(trim(media_type)) BETWEEN 3 AND 160),
    byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 0 AND 10485760),
    sha256 BLOB NOT NULL CHECK(typeof(sha256) = 'blob' AND length(sha256) = 32),
    storage_key TEXT NOT NULL UNIQUE CHECK(length(storage_key) BETWEEN 8 AND 512),
    signature_artifact_version_id TEXT,
    scan_state TEXT NOT NULL DEFAULT 'pending' CHECK(scan_state IN ('pending', 'clean', 'rejected')),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    acting_human_principal_id TEXT,
    provenance_node_id TEXT,
    provenance_agent_id TEXT,
    provenance_chat_id TEXT,
    provenance_run_id TEXT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, artifact_id) REFERENCES artifacts(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, signature_artifact_version_id)
        REFERENCES artifact_versions(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, acting_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_chat_id) REFERENCES chats(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    UNIQUE(artifact_id, version),
    UNIQUE(team_id, id),
    CHECK(
        (
            acting_human_principal_id IS NULL
            AND provenance_agent_id IS NULL
            AND provenance_node_id IS NULL
            AND provenance_chat_id IS NULL
            AND provenance_run_id IS NULL
        )
        OR (
            acting_human_principal_id IS NOT NULL
            AND provenance_agent_id IS NOT NULL
            AND provenance_node_id IS NOT NULL
            AND provenance_chat_id IS NOT NULL
            AND provenance_run_id IS NOT NULL
        )
    )
);

CREATE TRIGGER artifact_versions_require_team_creator
BEFORE INSERT ON artifact_versions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.created_by_principal_id
      AND p.status = 'active'
      AND p.kind IN ('human', 'service', 'agent')
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'artifact version creator does not belong to this team');
END;

CREATE TRIGGER artifact_versions_reject_false_agent_provenance
BEFORE INSERT ON artifact_versions
FOR EACH ROW WHEN NEW.provenance_agent_id IS NOT NULL AND COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.created_by_principal_id), ''
) <> 'agent'
BEGIN
    SELECT RAISE(ABORT, 'only an agent creator may claim agent provenance');
END;

CREATE TRIGGER artifact_versions_require_agent_provenance
BEFORE INSERT ON artifact_versions
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.created_by_principal_id), ''
) = 'agent' AND NOT EXISTS (
    SELECT 1
    FROM agents AS a
    JOIN chats AS c ON c.team_id = a.team_id AND c.id = NEW.provenance_chat_id
    JOIN runs AS r ON r.team_id = a.team_id AND r.id = NEW.provenance_run_id
    WHERE a.team_id = NEW.team_id
      AND a.id = NEW.provenance_agent_id
      AND a.principal_id = NEW.created_by_principal_id
      AND a.node_id = NEW.provenance_node_id
      AND c.node_id = a.node_id
      AND c.agent_id = a.id
      AND r.node_id = a.node_id
      AND r.agent_id = a.id
      AND r.chat_id = c.id
      AND r.acting_human_principal_id = NEW.acting_human_principal_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent-created artifact version requires exact provenance');
END;

CREATE TRIGGER artifact_version_content_is_immutable
BEFORE UPDATE OF id, team_id, artifact_id, version, media_type, byte_size,
    sha256, storage_key, signature_artifact_version_id,
    created_by_principal_id, acting_human_principal_id, provenance_node_id,
    provenance_agent_id, provenance_chat_id, provenance_run_id, created_at
ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact version content and provenance are immutable');
END;

CREATE TABLE artifact_acl_entries (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('principal', 'role')),
    subject_principal_id TEXT REFERENCES principals(id) ON DELETE CASCADE,
    subject_role TEXT CHECK(subject_role IN ('owner', 'admin', 'member', 'guest', 'automation')),
    can_read INTEGER NOT NULL CHECK(can_read IN (0, 1)),
    can_manage INTEGER NOT NULL CHECK(can_manage IN (0, 1)),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, artifact_id) REFERENCES artifacts(team_id, id) ON DELETE CASCADE,
    CHECK(
        (subject_kind = 'principal' AND subject_principal_id IS NOT NULL AND subject_role IS NULL)
        OR (subject_kind = 'role' AND subject_principal_id IS NULL AND subject_role IS NOT NULL)
    )
);

CREATE TRIGGER artifact_acl_require_team_principal
BEFORE INSERT ON artifact_acl_entries
FOR EACH ROW WHEN NEW.subject_kind = 'principal' AND NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.subject_principal_id
      AND p.status = 'active'
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'artifact ACL principal does not belong to this team');
END;

CREATE TRIGGER artifact_acl_subject_is_immutable
BEFORE UPDATE OF id, team_id, artifact_id, subject_kind,
    subject_principal_id, subject_role, created_at
ON artifact_acl_entries
BEGIN
    SELECT RAISE(ABORT, 'artifact ACL subject is immutable');
END;

CREATE UNIQUE INDEX artifact_acl_one_principal_entry
ON artifact_acl_entries(artifact_id, subject_principal_id)
WHERE subject_kind = 'principal';

CREATE UNIQUE INDEX artifact_acl_one_role_entry
ON artifact_acl_entries(artifact_id, subject_role)
WHERE subject_kind = 'role';

CREATE TABLE message_attachments (
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    artifact_version_id TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(message_id, artifact_version_id),
    FOREIGN KEY(team_id, message_id) REFERENCES messages(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, artifact_version_id)
        REFERENCES artifact_versions(team_id, id) ON DELETE RESTRICT
);

CREATE TABLE library_items (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('runbook', 'skill')),
    slug TEXT NOT NULL CHECK(slug = lower(trim(slug))) CHECK(length(slug) BETWEEN 1 AND 100),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 200),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    archived_at INTEGER CHECK(archived_at IS NULL OR archived_at >= created_at),
    UNIQUE(team_id, slug),
    UNIQUE(team_id, id)
);

CREATE TRIGGER library_items_require_team_creator
BEFORE INSERT ON library_items
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.created_by_principal_id
      AND p.status = 'active'
      AND p.kind IN ('human', 'service', 'agent')
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'library creator does not belong to this team');
END;

CREATE TRIGGER library_item_identity_is_immutable
BEFORE UPDATE OF id, team_id, kind, slug, created_by_principal_id, created_at
ON library_items
BEGIN
    SELECT RAISE(ABORT, 'library item identity is immutable');
END;

CREATE TABLE library_versions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    library_item_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    artifact_version_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft', 'in_review', 'approved', 'rejected', 'withdrawn')),
    permissions_json TEXT NOT NULL DEFAULT '{}'
        CHECK(json_valid(permissions_json) AND json_type(permissions_json) = 'object'),
    provenance_node_id TEXT,
    provenance_agent_id TEXT,
    provenance_chat_id TEXT,
    provenance_run_id TEXT,
    acting_human_principal_id TEXT,
    published_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    reviewed_at INTEGER CHECK(reviewed_at IS NULL OR reviewed_at >= created_at),
    FOREIGN KEY(team_id, library_item_id) REFERENCES library_items(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, artifact_version_id)
        REFERENCES artifact_versions(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_chat_id) REFERENCES chats(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, provenance_run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, acting_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(library_item_id, version),
    UNIQUE(team_id, id),
    CHECK(
        (
            acting_human_principal_id IS NULL
            AND provenance_agent_id IS NULL
            AND provenance_node_id IS NULL
            AND provenance_chat_id IS NULL
            AND provenance_run_id IS NULL
        )
        OR (
            acting_human_principal_id IS NOT NULL
            AND provenance_agent_id IS NOT NULL
            AND provenance_node_id IS NOT NULL
            AND provenance_chat_id IS NOT NULL
            AND provenance_run_id IS NOT NULL
        )
    )
);

CREATE TRIGGER library_versions_require_team_publisher
BEFORE INSERT ON library_versions
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.published_by_principal_id
      AND p.status = 'active'
      AND p.kind IN ('human', 'service', 'agent')
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
              AND m.status = 'active'
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'library publisher does not belong to this team');
END;

CREATE TRIGGER library_versions_reject_false_agent_provenance
BEFORE INSERT ON library_versions
FOR EACH ROW WHEN NEW.provenance_agent_id IS NOT NULL AND COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.published_by_principal_id), ''
) <> 'agent'
BEGIN
    SELECT RAISE(ABORT, 'only an agent publisher may claim agent provenance');
END;

CREATE TRIGGER library_versions_require_agent_provenance
BEFORE INSERT ON library_versions
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.published_by_principal_id), ''
) = 'agent' AND NOT EXISTS (
    SELECT 1
    FROM agents AS a
    JOIN chats AS c ON c.team_id = a.team_id AND c.id = NEW.provenance_chat_id
    JOIN runs AS r ON r.team_id = a.team_id AND r.id = NEW.provenance_run_id
    WHERE a.team_id = NEW.team_id
      AND a.id = NEW.provenance_agent_id
      AND a.principal_id = NEW.published_by_principal_id
      AND a.node_id = NEW.provenance_node_id
      AND c.node_id = a.node_id
      AND c.agent_id = a.id
      AND r.node_id = a.node_id
      AND r.agent_id = a.id
      AND r.chat_id = c.id
      AND r.acting_human_principal_id = NEW.acting_human_principal_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent-published library version requires exact provenance');
END;

CREATE TRIGGER library_version_content_is_immutable
BEFORE UPDATE OF id, team_id, library_item_id, version, artifact_version_id,
    permissions_json, provenance_node_id, provenance_agent_id,
    provenance_chat_id, provenance_run_id, acting_human_principal_id,
    published_by_principal_id, created_at
ON library_versions
BEGIN
    SELECT RAISE(ABORT, 'library version content and provenance are immutable');
END;

CREATE TABLE library_reviews (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    library_version_id TEXT NOT NULL,
    reviewer_principal_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'changes_requested', 'rejected')),
    review_note TEXT CHECK(review_note IS NULL OR length(review_note) <= 16000),
    reviewed_at INTEGER NOT NULL CHECK(reviewed_at >= 0),
    FOREIGN KEY(team_id, library_version_id)
        REFERENCES library_versions(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, reviewer_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(library_version_id, reviewer_principal_id)
);

CREATE TABLE skill_install_requests (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    library_version_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    target_agent_id TEXT,
    diff_artifact_version_id TEXT NOT NULL,
    requested_by_principal_id TEXT NOT NULL,
    approved_by_principal_id TEXT,
    status TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested', 'approved', 'rejected', 'installed', 'failed', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(team_id, library_version_id)
        REFERENCES library_versions(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, target_node_id, target_agent_id)
        REFERENCES agents(team_id, node_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, diff_artifact_version_id)
        REFERENCES artifact_versions(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, requested_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, approved_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    CHECK(status NOT IN ('approved', 'installed') OR approved_by_principal_id IS NOT NULL)
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    actor_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    acting_human_principal_id TEXT,
    node_id TEXT,
    agent_id TEXT,
    chat_id TEXT,
    run_id TEXT,
    action TEXT NOT NULL CHECK(length(trim(action)) BETWEEN 1 AND 160),
    resource_type TEXT NOT NULL CHECK(length(trim(resource_type)) BETWEEN 1 AND 80),
    resource_id TEXT NOT NULL CHECK(length(trim(resource_id)) BETWEEN 1 AND 240),
    outcome TEXT NOT NULL CHECK(outcome IN ('accepted', 'denied', 'succeeded', 'failed', 'cancelled')),
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    previous_event_hash BLOB CHECK(previous_event_hash IS NULL OR (
        typeof(previous_event_hash) = 'blob' AND length(previous_event_hash) = 32
    )),
    event_hash BLOB NOT NULL CHECK(typeof(event_hash) = 'blob' AND length(event_hash) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, acting_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, agent_id) REFERENCES agents(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, chat_id) REFERENCES chats(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, run_id) REFERENCES runs(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, event_hash),
    CHECK(
        (
            acting_human_principal_id IS NULL
            AND
            agent_id IS NULL
            AND node_id IS NULL
            AND chat_id IS NULL
            AND run_id IS NULL
        )
        OR (
            acting_human_principal_id IS NULL
            AND node_id IS NOT NULL
            AND agent_id IS NULL
            AND chat_id IS NULL
            AND run_id IS NULL
        )
        OR (
            acting_human_principal_id IS NOT NULL
            AND agent_id IS NOT NULL
            AND node_id IS NOT NULL
            AND chat_id IS NOT NULL
            AND run_id IS NOT NULL
        )
    )
);

CREATE TRIGGER audit_events_require_team_actor
BEFORE INSERT ON audit_events
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.actor_principal_id
      AND p.kind IN ('human', 'service', 'node', 'agent')
      AND (
        p.scope_team_id = NEW.team_id
        OR EXISTS (
            SELECT 1 FROM memberships AS m
            WHERE m.team_id = NEW.team_id
              AND m.principal_id = p.id
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'audit actor does not belong to this team');
END;

CREATE TRIGGER audit_events_reject_false_node_provenance
BEFORE INSERT ON audit_events
FOR EACH ROW WHEN NEW.node_id IS NOT NULL AND COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.actor_principal_id), ''
) NOT IN ('node', 'agent')
BEGIN
    SELECT RAISE(ABORT, 'only a node or agent actor may claim node provenance');
END;

CREATE TRIGGER audit_events_require_node_provenance
BEFORE INSERT ON audit_events
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.actor_principal_id), ''
) = 'node' AND NOT EXISTS (
    SELECT 1 FROM nodes AS n
    WHERE n.team_id = NEW.team_id
      AND n.id = NEW.node_id
      AND n.principal_id = NEW.actor_principal_id
)
BEGIN
    SELECT RAISE(ABORT, 'node audit event requires exact node provenance');
END;

CREATE TRIGGER audit_events_reject_false_agent_provenance
BEFORE INSERT ON audit_events
FOR EACH ROW WHEN NEW.agent_id IS NOT NULL AND COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.actor_principal_id), ''
) <> 'agent'
BEGIN
    SELECT RAISE(ABORT, 'only an agent actor may claim agent provenance');
END;

CREATE TRIGGER audit_events_require_agent_provenance
BEFORE INSERT ON audit_events
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.actor_principal_id), ''
) = 'agent' AND NOT EXISTS (
    SELECT 1
    FROM agents AS a
    JOIN chats AS c ON c.team_id = a.team_id AND c.id = NEW.chat_id
    JOIN runs AS r ON r.team_id = a.team_id AND r.id = NEW.run_id
    WHERE a.team_id = NEW.team_id
      AND a.id = NEW.agent_id
      AND a.principal_id = NEW.actor_principal_id
      AND a.node_id = NEW.node_id
      AND c.node_id = a.node_id
      AND c.agent_id = a.id
      AND r.node_id = a.node_id
      AND r.agent_id = a.id
      AND r.chat_id = c.id
      AND r.acting_human_principal_id = NEW.acting_human_principal_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent audit event requires exact provenance');
END;

CREATE TRIGGER audit_events_are_immutable_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TRIGGER audit_events_are_immutable_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TABLE outbox_events (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    aggregate_type TEXT NOT NULL CHECK(length(trim(aggregate_type)) BETWEEN 1 AND 80),
    aggregate_id TEXT NOT NULL CHECK(length(trim(aggregate_id)) BETWEEN 1 AND 240),
    event_type TEXT NOT NULL CHECK(length(trim(event_type)) BETWEEN 1 AND 160),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    idempotency_key BLOB NOT NULL UNIQUE
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'leased', 'delivered', 'dead_letter')),
    available_at INTEGER NOT NULL CHECK(available_at >= 0),
    lease_owner TEXT,
    lease_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 100),
    delivered_at INTEGER CHECK(delivered_at IS NULL OR delivered_at >= available_at),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    CHECK(
        (state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'leased')
    )
);

CREATE TRIGGER outbox_event_identity_is_immutable
BEFORE UPDATE OF id, team_id, aggregate_type, aggregate_id, event_type,
    metadata_json, idempotency_key, created_at
ON outbox_events
BEGIN
    SELECT RAISE(ABORT, 'outbox event identity and payload are immutable');
END;

CREATE TRIGGER outbox_delivery_state_is_monotonic
BEFORE UPDATE ON outbox_events
FOR EACH ROW WHEN
    NEW.attempt_count < OLD.attempt_count
    OR (OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS NOT OLD.delivered_at)
BEGIN
    SELECT RAISE(ABORT, 'outbox delivery counters are monotonic');
END;

CREATE INDEX outbox_pending_delivery
ON outbox_events(state, available_at, id)
WHERE state IN ('pending', 'leased');

CREATE TABLE rate_limit_buckets (
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    subject_key TEXT NOT NULL CHECK(length(subject_key) BETWEEN 1 AND 240),
    action TEXT NOT NULL CHECK(length(action) BETWEEN 1 AND 120),
    window_started_at INTEGER NOT NULL CHECK(window_started_at >= 0),
    count INTEGER NOT NULL CHECK(count >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= window_started_at),
    PRIMARY KEY(team_id, subject_key, action)
);

CREATE VIRTUAL TABLE message_search USING fts5(
    team_id UNINDEXED,
    channel_id UNINDEXED,
    id UNINDEXED,
    body,
    content = 'messages',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER messages_search_insert
AFTER INSERT ON messages
FOR EACH ROW WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO message_search(rowid, team_id, channel_id, id, body)
    VALUES (NEW.rowid, NEW.team_id, NEW.channel_id, NEW.id, NEW.body);
END;

CREATE TRIGGER messages_search_delete
AFTER DELETE ON messages
FOR EACH ROW WHEN OLD.deleted_at IS NULL
BEGIN
    INSERT INTO message_search(message_search, rowid, team_id, channel_id, id, body)
    VALUES ('delete', OLD.rowid, OLD.team_id, OLD.channel_id, OLD.id, OLD.body);
END;

CREATE TRIGGER messages_search_update_remove
AFTER UPDATE ON messages
FOR EACH ROW WHEN OLD.deleted_at IS NULL
BEGIN
    INSERT INTO message_search(message_search, rowid, team_id, channel_id, id, body)
    VALUES ('delete', OLD.rowid, OLD.team_id, OLD.channel_id, OLD.id, OLD.body);
END;

CREATE TRIGGER messages_search_update_add
AFTER UPDATE ON messages
FOR EACH ROW WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO message_search(rowid, team_id, channel_id, id, body)
    VALUES (NEW.rowid, NEW.team_id, NEW.channel_id, NEW.id, NEW.body);
END;
