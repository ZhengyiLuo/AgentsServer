-- Team Messages V2.  One team messaging model with attachments plus a
-- versioned Skills library.  Messages are passive records: creating one never
-- starts, steers, or authorizes an agent.  Messages and skill versions are
-- immutable; skills carry only pin, archive, and current-version pointers.
-- Attachment bytes live outside SQLite in a content-addressed directory; this
-- schema stores their metadata and upload state only.

CREATE TABLE team_skills (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    slug TEXT NOT NULL
        CHECK(slug = lower(trim(slug)))
        CHECK(length(slug) BETWEEN 1 AND 64),
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 160),
    summary TEXT NOT NULL DEFAULT '' CHECK(length(summary) <= 280),
    tags_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(tags_json) AND json_type(tags_json) = 'array'),
    current_version INTEGER NOT NULL DEFAULT 1 CHECK(current_version >= 1),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    pinned_at INTEGER CHECK(pinned_at IS NULL OR pinned_at >= 0),
    pinned_by_principal_id TEXT REFERENCES principals(id) ON DELETE RESTRICT,
    archived_at INTEGER CHECK(archived_at IS NULL OR archived_at >= created_at),
    archived_by_principal_id TEXT REFERENCES principals(id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(team_id, slug),
    UNIQUE(team_id, id),
    CHECK((pinned_at IS NULL) = (pinned_by_principal_id IS NULL)),
    CHECK((archived_at IS NULL) = (archived_by_principal_id IS NULL))
);

CREATE INDEX team_skills_listing
ON team_skills(team_id, archived_at, pinned_at, updated_at);

CREATE TRIGGER team_skills_require_team_creator
BEFORE INSERT ON team_skills
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
    SELECT RAISE(ABORT, 'team skill creator does not belong to this team');
END;

CREATE TRIGGER team_skills_limit_per_team
BEFORE INSERT ON team_skills
FOR EACH ROW WHEN (
    SELECT COUNT(*) FROM team_skills WHERE team_id = NEW.team_id
) >= 500
BEGIN
    SELECT RAISE(ABORT, 'team skill limit exceeded');
END;

CREATE TRIGGER team_skill_identity_is_immutable
BEFORE UPDATE OF id, team_id, slug, created_by_principal_id, created_at
ON team_skills
BEGIN
    SELECT RAISE(ABORT, 'team skill identity is immutable');
END;

CREATE TRIGGER team_skills_cannot_be_deleted
BEFORE DELETE ON team_skills
BEGIN
    SELECT RAISE(ABORT, 'team skills are archived, never deleted');
END;

CREATE TABLE team_messages (
    queue_ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('message', 'skill')),
    title TEXT CHECK(title IS NULL OR length(trim(title)) BETWEEN 1 AND 160),
    body_format TEXT NOT NULL DEFAULT 'markdown' CHECK(body_format IN ('plain', 'markdown')),
    body TEXT NOT NULL CHECK(length(CAST(body AS BLOB)) BETWEEN 1 AND 49152),
    body_sha256 BLOB NOT NULL
        CHECK(typeof(body_sha256) = 'blob' AND length(body_sha256) = 32),
    sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human', 'server')),
    sender_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    sender_node_id TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}'
        CHECK(
            json_valid(provenance_json)
            AND json_type(provenance_json) = 'object'
            AND length(provenance_json) <= 2048
        ),
    in_reply_to_message_id TEXT,
    skill_id TEXT,
    skill_version INTEGER CHECK(skill_version IS NULL OR skill_version >= 1),
    attachment_count INTEGER NOT NULL DEFAULT 0 CHECK(attachment_count BETWEEN 0 AND 16),
    attachment_bytes INTEGER NOT NULL DEFAULT 0 CHECK(attachment_bytes >= 0),
    idempotency_key BLOB NOT NULL
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, sender_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, in_reply_to_message_id)
        REFERENCES team_messages(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, skill_id) REFERENCES team_skills(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    UNIQUE(team_id, idempotency_key),
    CHECK(
        (sender_kind = 'human' AND sender_node_id IS NULL)
        OR (sender_kind = 'server' AND sender_node_id IS NOT NULL)
    ),
    CHECK(
        (kind = 'skill' AND title IS NOT NULL AND skill_id IS NOT NULL AND skill_version IS NOT NULL)
        OR (kind = 'message' AND skill_id IS NULL AND skill_version IS NULL)
    )
);

CREATE INDEX team_messages_team_order ON team_messages(team_id, queue_ordinal);
CREATE INDEX team_messages_sender ON team_messages(team_id, sender_principal_id, queue_ordinal);

CREATE TRIGGER team_messages_require_team_sender
BEFORE INSERT ON team_messages
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals AS p
    WHERE p.id = NEW.sender_principal_id
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
    SELECT RAISE(ABORT, 'team message sender does not belong to this team');
END;

CREATE TRIGGER team_messages_are_immutable
BEFORE UPDATE ON team_messages
BEGIN
    SELECT RAISE(ABORT, 'team messages are immutable');
END;

CREATE TRIGGER team_messages_cannot_be_deleted
BEFORE DELETE ON team_messages
BEGIN
    SELECT RAISE(ABORT, 'team messages cannot be deleted');
END;

CREATE TABLE team_message_recipients (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    recipient_kind TEXT NOT NULL CHECK(recipient_kind IN ('server', 'human', 'all')),
    recipient_node_id TEXT,
    recipient_principal_id TEXT REFERENCES principals(id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'available'
        CHECK(state IN ('available', 'delivered', 'read')),
    delivered_at INTEGER CHECK(delivered_at IS NULL OR delivered_at >= 0),
    read_at INTEGER CHECK(read_at IS NULL OR read_at >= 0),
    FOREIGN KEY(team_id, message_id) REFERENCES team_messages(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, recipient_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    CHECK(
        (recipient_kind = 'server' AND recipient_node_id IS NOT NULL AND recipient_principal_id IS NULL)
        OR (recipient_kind = 'human' AND recipient_principal_id IS NOT NULL AND recipient_node_id IS NULL)
        OR (recipient_kind = 'all' AND recipient_node_id IS NULL AND recipient_principal_id IS NULL)
    ),
    CHECK(
        (state = 'available' AND delivered_at IS NULL AND read_at IS NULL)
        OR (state = 'delivered' AND delivered_at IS NOT NULL AND read_at IS NULL)
        OR (state = 'read' AND delivered_at IS NOT NULL AND read_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX team_message_recipients_one_per_target
ON team_message_recipients(
    message_id,
    recipient_kind,
    COALESCE(recipient_node_id, recipient_principal_id, '')
);

CREATE INDEX team_message_recipients_inbox
ON team_message_recipients(team_id, recipient_kind, recipient_node_id, recipient_principal_id, state);

CREATE TRIGGER team_message_recipient_identity_is_immutable
BEFORE UPDATE OF id, team_id, message_id, recipient_kind, recipient_node_id,
    recipient_principal_id
ON team_message_recipients
BEGIN
    SELECT RAISE(ABORT, 'team message recipient identity is immutable');
END;

CREATE TRIGGER team_message_recipient_state_is_monotonic
BEFORE UPDATE OF state, delivered_at, read_at ON team_message_recipients
FOR EACH ROW WHEN
    (OLD.state = 'delivered' AND NEW.state = 'available')
    OR (OLD.state = 'read' AND NEW.state <> 'read')
    OR (OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS NOT OLD.delivered_at)
    OR (OLD.read_at IS NOT NULL AND NEW.read_at IS NOT OLD.read_at)
    OR OLD.recipient_kind = 'all'
BEGIN
    SELECT RAISE(ABORT, 'team message receipts are monotonic');
END;

CREATE TRIGGER team_message_recipients_cannot_be_deleted
BEFORE DELETE ON team_message_recipients
BEGIN
    SELECT RAISE(ABORT, 'team message recipients cannot be deleted');
END;

CREATE TABLE team_attachments (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    message_id TEXT,
    file_name TEXT NOT NULL
        CHECK(length(file_name) BETWEEN 1 AND 255)
        CHECK(instr(file_name, '/') = 0 AND instr(file_name, '\') = 0)
        CHECK(file_name NOT IN ('.', '..')),
    media_type TEXT NOT NULL CHECK(length(trim(media_type)) BETWEEN 3 AND 160),
    byte_size INTEGER NOT NULL CHECK(byte_size >= 1),
    sha256 BLOB NOT NULL CHECK(typeof(sha256) = 'blob' AND length(sha256) = 32),
    storage_key TEXT NOT NULL CHECK(length(storage_key) = 64 AND storage_key = lower(storage_key)),
    state TEXT NOT NULL DEFAULT 'uploading' CHECK(state IN ('uploading', 'ready', 'failed')),
    received_bytes INTEGER NOT NULL DEFAULT 0 CHECK(received_bytes >= 0),
    uploaded_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    uploader_node_id TEXT,
    idempotency_key BLOB NOT NULL
        CHECK(typeof(idempotency_key) = 'blob' AND length(idempotency_key) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    ready_at INTEGER CHECK(ready_at IS NULL OR ready_at >= created_at),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    FOREIGN KEY(team_id, message_id) REFERENCES team_messages(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, uploader_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id),
    UNIQUE(team_id, idempotency_key),
    CHECK(received_bytes <= byte_size),
    CHECK(
        (state = 'ready' AND ready_at IS NOT NULL AND received_bytes = byte_size)
        OR (state <> 'ready' AND ready_at IS NULL)
    ),
    CHECK(message_id IS NULL OR state = 'ready')
);

CREATE INDEX team_attachments_by_message ON team_attachments(team_id, message_id);
CREATE INDEX team_attachments_by_storage ON team_attachments(storage_key, state);
CREATE INDEX team_attachments_pending ON team_attachments(state, expires_at);

CREATE TRIGGER team_attachment_identity_is_immutable
BEFORE UPDATE OF id, team_id, file_name, media_type, byte_size, sha256,
    storage_key, uploaded_by_principal_id, uploader_node_id, idempotency_key,
    created_at
ON team_attachments
BEGIN
    SELECT RAISE(ABORT, 'team attachment identity is immutable');
END;

CREATE TRIGGER team_attachment_binding_is_permanent
BEFORE UPDATE OF message_id ON team_attachments
FOR EACH ROW WHEN OLD.message_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'team attachment is already bound to a message');
END;

CREATE TRIGGER team_attachment_state_is_forward_only
BEFORE UPDATE OF state ON team_attachments
FOR EACH ROW WHEN OLD.state <> 'uploading' AND NEW.state <> OLD.state
BEGIN
    SELECT RAISE(ABORT, 'team attachment state is terminal');
END;

CREATE TRIGGER team_attachments_bound_cannot_be_deleted
BEFORE DELETE ON team_attachments
FOR EACH ROW WHEN OLD.message_id IS NOT NULL OR OLD.state = 'ready'
BEGIN
    SELECT RAISE(ABORT, 'ready or bound team attachments cannot be deleted');
END;

CREATE TABLE team_skill_versions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    message_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 160),
    summary TEXT NOT NULL DEFAULT '' CHECK(length(summary) <= 280),
    tags_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(tags_json) AND json_type(tags_json) = 'array'),
    change_note TEXT NOT NULL DEFAULT '' CHECK(length(change_note) <= 280),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, skill_id) REFERENCES team_skills(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, message_id) REFERENCES team_messages(team_id, id) ON DELETE RESTRICT,
    UNIQUE(skill_id, version),
    UNIQUE(message_id),
    UNIQUE(team_id, id)
);

CREATE TRIGGER team_skill_versions_limit
BEFORE INSERT ON team_skill_versions
FOR EACH ROW WHEN (
    SELECT COUNT(*) FROM team_skill_versions WHERE skill_id = NEW.skill_id
) >= 200
BEGIN
    SELECT RAISE(ABORT, 'team skill version limit exceeded');
END;

CREATE TRIGGER team_skill_versions_are_immutable
BEFORE UPDATE ON team_skill_versions
BEGIN
    SELECT RAISE(ABORT, 'team skill versions are immutable');
END;

CREATE TRIGGER team_skill_versions_cannot_be_deleted
BEFORE DELETE ON team_skill_versions
BEGIN
    SELECT RAISE(ABORT, 'team skill versions cannot be deleted');
END;
