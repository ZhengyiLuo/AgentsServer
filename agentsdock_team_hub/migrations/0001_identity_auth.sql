-- Team Hub identity and authentication roots. Secret material is represented
-- only by one-way hashes; node private keys and AgentsServer bearers never
-- belong in this database.

CREATE TABLE principals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('human', 'service', 'node', 'agent', 'chat')),
    scope_team_id TEXT,
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suspended', 'revoked')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(scope_team_id, id),
    CHECK(
        (kind IN ('human', 'service') AND scope_team_id IS NULL)
        OR (kind IN ('node', 'agent', 'chat') AND scope_team_id IS NOT NULL)
    ),
    FOREIGN KEY(scope_team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TRIGGER principals_scope_is_immutable
BEFORE UPDATE OF id, kind, scope_team_id, created_at ON principals
BEGIN
    SELECT RAISE(ABORT, 'principal identity and scope are immutable');
END;

CREATE TRIGGER principal_revocation_is_terminal
BEFORE UPDATE OF status ON principals
FOR EACH ROW WHEN OLD.status = 'revoked' AND NEW.status <> 'revoked'
BEGIN
    SELECT RAISE(ABORT, 'principal revocation is terminal');
END;

CREATE TABLE human_accounts (
    principal_id TEXT PRIMARY KEY REFERENCES principals(id) ON DELETE RESTRICT,
    email_normalized TEXT NOT NULL UNIQUE
        CHECK(email_normalized = lower(trim(email_normalized)))
        CHECK(length(email_normalized) BETWEEN 3 AND 320),
    created_at INTEGER NOT NULL CHECK(created_at >= 0)
);

CREATE TRIGGER human_accounts_require_human_principal
BEFORE INSERT ON human_accounts
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
) <> 'human'
BEGIN
    SELECT RAISE(ABORT, 'human account requires a human principal');
END;

CREATE TRIGGER human_account_identity_is_immutable
BEFORE UPDATE OF principal_id, email_normalized, created_at ON human_accounts
BEGIN
    SELECT RAISE(ABORT, 'human account identity is immutable');
END;

CREATE TABLE service_accounts (
    principal_id TEXT PRIMARY KEY REFERENCES principals(id) ON DELETE RESTRICT,
    service_identifier TEXT NOT NULL UNIQUE
        CHECK(length(service_identifier) BETWEEN 1 AND 160),
    created_at INTEGER NOT NULL CHECK(created_at >= 0)
);

CREATE TRIGGER service_accounts_require_service_principal
BEFORE INSERT ON service_accounts
FOR EACH ROW WHEN COALESCE(
    (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
) <> 'service'
BEGIN
    SELECT RAISE(ABORT, 'service account requires a service principal');
END;

CREATE TRIGGER service_account_identity_is_immutable
BEFORE UPDATE OF principal_id, service_identifier, created_at ON service_accounts
BEGIN
    SELECT RAISE(ABORT, 'service account identity is immutable');
END;

CREATE TABLE teams (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('personal', 'shared')),
    slug TEXT NOT NULL UNIQUE
        CHECK(slug = lower(trim(slug)))
        CHECK(length(slug) BETWEEN 3 AND 80),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
    personal_owner_principal_id TEXT REFERENCES human_accounts(principal_id) ON DELETE RESTRICT,
    retention_days INTEGER NOT NULL DEFAULT 365 CHECK(retention_days BETWEEN 1 AND 3650),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(personal_owner_principal_id),
    UNIQUE(id, personal_owner_principal_id),
    CHECK(
        (kind = 'personal' AND personal_owner_principal_id IS NOT NULL)
        OR (kind = 'shared' AND personal_owner_principal_id IS NULL)
    )
);

CREATE TRIGGER team_identity_is_immutable
BEFORE UPDATE OF id, kind, slug, personal_owner_principal_id,
    created_by_principal_id, created_at
ON teams
BEGIN
    SELECT RAISE(ABORT, 'team identity and personal owner are immutable');
END;

CREATE TABLE memberships (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'member', 'guest', 'automation')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suspended', 'revoked')),
    invited_by_principal_id TEXT REFERENCES principals(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(team_id, principal_id),
    UNIQUE(team_id, id)
);

CREATE UNIQUE INDEX memberships_one_active_owner_per_team
ON memberships(team_id)
WHERE role = 'owner' AND status = 'active';

CREATE TRIGGER memberships_require_compatible_principal
BEFORE INSERT ON memberships
FOR EACH ROW WHEN
    (NEW.role = 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) <> 'service')
    OR
    (NEW.role <> 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) <> 'human')
BEGIN
    SELECT RAISE(ABORT, 'membership role is incompatible with principal kind');
END;

CREATE TRIGGER memberships_update_requires_compatible_principal
BEFORE UPDATE OF principal_id, role ON memberships
FOR EACH ROW WHEN
    (NEW.role = 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) <> 'service')
    OR
    (NEW.role <> 'automation' AND COALESCE(
        (SELECT kind FROM principals WHERE id = NEW.principal_id), ''
    ) <> 'human')
BEGIN
    SELECT RAISE(ABORT, 'membership role is incompatible with principal kind');
END;

CREATE TRIGGER memberships_identity_is_immutable
BEFORE UPDATE OF id, team_id, principal_id, invited_by_principal_id, created_at
ON memberships
BEGIN
    SELECT RAISE(ABORT, 'membership identity is immutable');
END;

CREATE TRIGGER membership_revocation_is_terminal
BEFORE UPDATE OF status ON memberships
FOR EACH ROW WHEN OLD.status = 'revoked' AND NEW.status <> 'revoked'
BEGIN
    SELECT RAISE(ABORT, 'membership revocation is terminal');
END;

CREATE TRIGGER memberships_cannot_be_deleted
BEFORE DELETE ON memberships
BEGIN
    SELECT RAISE(ABORT, 'membership ledger rows cannot be deleted');
END;

CREATE TABLE invitations (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    invitee_email_normalized TEXT
        CHECK(invitee_email_normalized IS NULL OR (
            invitee_email_normalized = lower(trim(invitee_email_normalized))
            AND length(invitee_email_normalized) BETWEEN 3 AND 320
        )),
    role TEXT NOT NULL CHECK(role IN ('admin', 'member', 'guest')),
    issued_by_principal_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL CHECK(expires_at >= 0),
    created_at INTEGER NOT NULL CHECK(created_at >= 0 AND expires_at > created_at),
    redeemed_at INTEGER CHECK(redeemed_at IS NULL OR redeemed_at >= created_at),
    redeemed_by_principal_id TEXT REFERENCES human_accounts(principal_id) ON DELETE RESTRICT,
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, issued_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    CHECK((redeemed_at IS NULL) = (redeemed_by_principal_id IS NULL)),
    CHECK(redeemed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER invitation_authority_is_immutable
BEFORE UPDATE OF id, team_id, token_hash, invitee_email_normalized,
    role, issued_by_principal_id, expires_at, created_at
ON invitations
BEGIN
    SELECT RAISE(ABORT, 'invitation authority is immutable');
END;

CREATE TRIGGER invitation_terminal_state_is_one_way
BEFORE UPDATE ON invitations
FOR EACH ROW WHEN
    (
        OLD.redeemed_at IS NOT NULL
        AND (
            NEW.redeemed_at IS NOT OLD.redeemed_at
            OR NEW.redeemed_by_principal_id IS NOT OLD.redeemed_by_principal_id
        )
    )
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'invitation terminal state is one-way');
END;

CREATE TRIGGER invitations_cannot_be_deleted
BEFORE DELETE ON invitations
BEGIN
    SELECT RAISE(ABORT, 'invitation ledger rows cannot be deleted');
END;

CREATE INDEX invitations_team_expiry
ON invitations(team_id, expires_at)
WHERE redeemed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE device_sessions (
    id TEXT PRIMARY KEY,
    human_principal_id TEXT NOT NULL REFERENCES human_accounts(principal_id) ON DELETE CASCADE,
    device_label TEXT NOT NULL CHECK(length(trim(device_label)) BETWEEN 1 AND 160),
    device_public_key TEXT CHECK(device_public_key IS NULL OR length(device_public_key) BETWEEN 32 AND 16384),
    refresh_generation INTEGER NOT NULL DEFAULT 0 CHECK(refresh_generation >= 0),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    last_seen_at INTEGER NOT NULL CHECK(last_seen_at >= created_at),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE TRIGGER device_session_authority_is_immutable
BEFORE UPDATE OF id, human_principal_id, device_public_key, created_at, expires_at
ON device_sessions
BEGIN
    SELECT RAISE(ABORT, 'device session authority is immutable');
END;

CREATE TRIGGER device_session_counters_are_monotonic
BEFORE UPDATE ON device_sessions
FOR EACH ROW WHEN
    NEW.refresh_generation < OLD.refresh_generation
    OR NEW.last_seen_at < OLD.last_seen_at
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'device session counters and revocation are monotonic');
END;

CREATE TRIGGER device_sessions_cannot_be_deleted
BEFORE DELETE ON device_sessions
BEGIN
    SELECT RAISE(ABORT, 'device session ledger rows cannot be deleted');
END;

CREATE TABLE refresh_tokens (
    id TEXT PRIMARY KEY,
    device_session_id TEXT NOT NULL REFERENCES device_sessions(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER CHECK(consumed_at IS NULL OR consumed_at >= created_at),
    replaced_by_token_id TEXT,
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    UNIQUE(device_session_id, generation),
    UNIQUE(device_session_id, id),
    FOREIGN KEY(device_session_id, replaced_by_token_id)
        REFERENCES refresh_tokens(device_session_id, id) ON DELETE RESTRICT,
    CHECK((consumed_at IS NULL) = (replaced_by_token_id IS NULL)),
    CHECK(consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER refresh_token_authority_is_immutable
BEFORE UPDATE OF id, device_session_id, token_hash, generation, created_at, expires_at
ON refresh_tokens
BEGIN
    SELECT RAISE(ABORT, 'refresh token authority is immutable');
END;

CREATE TRIGGER refresh_token_terminal_state_is_one_way
BEFORE UPDATE ON refresh_tokens
FOR EACH ROW WHEN
    (
        OLD.consumed_at IS NOT NULL
        AND (
            NEW.consumed_at IS NOT OLD.consumed_at
            OR NEW.replaced_by_token_id IS NOT OLD.replaced_by_token_id
        )
    )
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'refresh token terminal state is one-way');
END;

CREATE TRIGGER refresh_token_rotation_stays_in_session
BEFORE UPDATE OF consumed_at, replaced_by_token_id ON refresh_tokens
FOR EACH ROW WHEN NEW.consumed_at IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM refresh_tokens AS replacement
    WHERE replacement.device_session_id = OLD.device_session_id
      AND replacement.id = NEW.replaced_by_token_id
      AND replacement.generation = OLD.generation + 1
      AND replacement.consumed_at IS NULL
      AND replacement.revoked_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'refresh token rotation requires the next token in this session');
END;

CREATE TRIGGER refresh_tokens_cannot_be_deleted
BEFORE DELETE ON refresh_tokens
BEGIN
    SELECT RAISE(ABORT, 'refresh token ledger rows cannot be deleted');
END;

CREATE TABLE access_token_revocations (
    jti_hash BLOB PRIMARY KEY
        CHECK(typeof(jti_hash) = 'blob' AND length(jti_hash) = 32),
    device_session_id TEXT NOT NULL REFERENCES device_sessions(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL CHECK(expires_at >= 0),
    revoked_at INTEGER NOT NULL CHECK(revoked_at >= 0),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 240)
);

CREATE TRIGGER access_token_revocations_are_immutable_update
BEFORE UPDATE ON access_token_revocations
BEGIN
    SELECT RAISE(ABORT, 'access token revocations are immutable');
END;

CREATE TRIGGER access_token_revocations_are_immutable_delete
BEFORE DELETE ON access_token_revocations
BEGIN
    SELECT RAISE(ABORT, 'access token revocations are immutable');
END;

CREATE TABLE node_enrollment_grants (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    issued_by_principal_id TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER CHECK(consumed_at IS NULL OR consumed_at >= created_at),
    consumed_by_node_id TEXT,
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, issued_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, consumed_by_node_id)
        REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    CHECK((consumed_at IS NULL) = (consumed_by_node_id IS NULL)),
    CHECK(consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER node_enrollment_authority_is_immutable
BEFORE UPDATE OF id, team_id, token_hash, issued_by_principal_id,
    created_at, expires_at
ON node_enrollment_grants
BEGIN
    SELECT RAISE(ABORT, 'node enrollment authority is immutable');
END;

CREATE TRIGGER node_enrollment_terminal_state_is_one_way
BEFORE UPDATE ON node_enrollment_grants
FOR EACH ROW WHEN
    (
        OLD.consumed_at IS NOT NULL
        AND (
            NEW.consumed_at IS NOT OLD.consumed_at
            OR NEW.consumed_by_node_id IS NOT OLD.consumed_by_node_id
        )
    )
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'node enrollment terminal state is one-way');
END;

CREATE TRIGGER node_enrollment_grants_cannot_be_deleted
BEFORE DELETE ON node_enrollment_grants
BEGIN
    SELECT RAISE(ABORT, 'node enrollment ledger rows cannot be deleted');
END;

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id) ON DELETE RESTRICT,
    server_identity TEXT NOT NULL UNIQUE CHECK(length(trim(server_identity)) BETWEEN 8 AND 240),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'offline', 'suspended', 'revoked')),
    enrolled_at INTEGER NOT NULL CHECK(enrolled_at >= 0),
    last_seen_at INTEGER CHECK(last_seen_at IS NULL OR last_seen_at >= enrolled_at),
    UNIQUE(team_id, id),
    UNIQUE(team_id, principal_id)
);

CREATE TRIGGER nodes_identity_is_immutable
BEFORE UPDATE OF id, team_id, principal_id, server_identity, enrolled_at ON nodes
BEGIN
    SELECT RAISE(ABORT, 'node identity is immutable');
END;

CREATE TRIGGER node_revocation_is_terminal
BEFORE UPDATE OF status ON nodes
FOR EACH ROW WHEN OLD.status = 'revoked' AND NEW.status <> 'revoked'
BEGIN
    SELECT RAISE(ABORT, 'node revocation is terminal');
END;

CREATE TRIGGER nodes_require_scoped_node_principal
BEFORE INSERT ON nodes
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals
    WHERE id = NEW.principal_id AND kind = 'node' AND scope_team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'node requires a node principal scoped to the same team');
END;

CREATE TABLE node_credentials (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    credential_kind TEXT NOT NULL CHECK(credential_kind = 'ed25519'),
    public_material TEXT NOT NULL CHECK(length(public_material) BETWEEN 32 AND 16384),
    fingerprint_sha256 BLOB NOT NULL UNIQUE
        CHECK(typeof(fingerprint_sha256) = 'blob' AND length(fingerprint_sha256) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER CHECK(expires_at IS NULL OR expires_at > created_at),
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE CASCADE,
    UNIQUE(team_id, id)
);

CREATE TRIGGER node_credential_authority_is_immutable
BEFORE UPDATE OF id, team_id, node_id, credential_kind, public_material,
    fingerprint_sha256, created_at, expires_at
ON node_credentials
BEGIN
    SELECT RAISE(ABORT, 'node credential authority is immutable');
END;

CREATE TRIGGER node_credential_revocation_is_one_way
BEFORE UPDATE OF revoked_at ON node_credentials
FOR EACH ROW WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN
    SELECT RAISE(ABORT, 'node credential revocation is one-way');
END;

CREATE TRIGGER node_credentials_cannot_be_deleted
BEFORE DELETE ON node_credentials
BEGIN
    SELECT RAISE(ABORT, 'node credential ledger rows cannot be deleted');
END;

CREATE TABLE legacy_server_bindings (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    server_identity TEXT NOT NULL UNIQUE CHECK(length(trim(server_identity)) BETWEEN 8 AND 240),
    node_id TEXT,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    UNIQUE(team_id, id)
);

CREATE TRIGGER legacy_server_binding_identity_is_immutable
BEFORE UPDATE OF id, team_id, server_identity, created_at ON legacy_server_bindings
BEGIN
    SELECT RAISE(ABORT, 'legacy server binding identity is immutable');
END;

CREATE TRIGGER legacy_server_binding_node_is_one_way
BEFORE UPDATE OF node_id ON legacy_server_bindings
FOR EACH ROW WHEN OLD.node_id IS NOT NULL OR NEW.node_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'legacy server node binding is one-way');
END;

CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL,
    external_agent_id TEXT NOT NULL CHECK(length(trim(external_agent_id)) BETWEEN 1 AND 240),
    backend TEXT NOT NULL CHECK(backend IN ('codex', 'claude', 'other')),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'offline', 'suspended', 'retired')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    UNIQUE(node_id, external_agent_id),
    UNIQUE(team_id, id),
    UNIQUE(team_id, node_id, id),
    UNIQUE(team_id, principal_id)
);

CREATE TRIGGER agents_identity_is_immutable
BEFORE UPDATE OF id, team_id, principal_id, node_id, external_agent_id, backend, created_at
ON agents
BEGIN
    SELECT RAISE(ABORT, 'agent identity is immutable');
END;

CREATE TRIGGER agent_retirement_is_terminal
BEFORE UPDATE OF status ON agents
FOR EACH ROW WHEN OLD.status = 'retired' AND NEW.status <> 'retired'
BEGIN
    SELECT RAISE(ABORT, 'agent retirement is terminal');
END;

CREATE TRIGGER agents_require_scoped_agent_principal
BEFORE INSERT ON agents
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals
    WHERE id = NEW.principal_id AND kind = 'agent' AND scope_team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent requires an agent principal scoped to the same team');
END;

CREATE TABLE chats (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    external_chat_id TEXT NOT NULL CHECK(length(trim(external_chat_id)) BETWEEN 1 AND 240),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 240),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived', 'deleted')),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, node_id, agent_id)
        REFERENCES agents(team_id, node_id, id) ON DELETE RESTRICT,
    UNIQUE(node_id, external_chat_id),
    UNIQUE(team_id, id),
    UNIQUE(team_id, node_id, id),
    UNIQUE(team_id, node_id, agent_id, id),
    UNIQUE(team_id, principal_id)
);

CREATE TRIGGER chats_identity_is_immutable
BEFORE UPDATE OF id, team_id, principal_id, node_id, agent_id, external_chat_id, created_at
ON chats
BEGIN
    SELECT RAISE(ABORT, 'chat identity is immutable');
END;

CREATE TRIGGER chat_deletion_is_terminal
BEFORE UPDATE OF status ON chats
FOR EACH ROW WHEN OLD.status = 'deleted' AND NEW.status <> 'deleted'
BEGIN
    SELECT RAISE(ABORT, 'chat deletion is terminal');
END;

CREATE TRIGGER chats_require_scoped_chat_principal
BEFORE INSERT ON chats
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM principals
    WHERE id = NEW.principal_id AND kind = 'chat' AND scope_team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'chat requires a chat principal scoped to the same team');
END;

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    acting_human_principal_id TEXT,
    external_run_id TEXT NOT NULL CHECK(length(trim(external_run_id)) BETWEEN 1 AND 240),
    status TEXT NOT NULL CHECK(status IN ('registered', 'running', 'succeeded', 'failed', 'cancelled')),
    started_at INTEGER NOT NULL CHECK(started_at >= 0),
    finished_at INTEGER CHECK(finished_at IS NULL OR finished_at >= started_at),
    FOREIGN KEY(team_id, node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, node_id, agent_id)
        REFERENCES agents(team_id, node_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, node_id, chat_id)
        REFERENCES chats(team_id, node_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, node_id, agent_id, chat_id)
        REFERENCES chats(team_id, node_id, agent_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, acting_human_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(node_id, external_run_id),
    UNIQUE(team_id, id)
);

CREATE TRIGGER runs_require_active_human_actor
BEFORE INSERT ON runs
FOR EACH ROW WHEN NEW.acting_human_principal_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM memberships AS m
    JOIN principals AS p ON p.id = m.principal_id
    WHERE m.team_id = NEW.team_id
      AND m.principal_id = NEW.acting_human_principal_id
      AND m.status = 'active'
      AND p.kind = 'human'
      AND p.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'run acting user must be an active human member');
END;

CREATE TRIGGER runs_provenance_is_immutable
BEFORE UPDATE OF id, team_id, node_id, agent_id, chat_id,
    acting_human_principal_id, external_run_id, started_at
ON runs
BEGIN
    SELECT RAISE(ABORT, 'run provenance is immutable');
END;

CREATE TABLE turn_capabilities (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    issued_to_run_id TEXT NOT NULL,
    issued_by_principal_id TEXT NOT NULL,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    action TEXT NOT NULL CHECK(length(trim(action)) BETWEEN 1 AND 120),
    resource_type TEXT NOT NULL CHECK(length(trim(resource_type)) BETWEEN 1 AND 80),
    resource_id TEXT NOT NULL CHECK(length(trim(resource_id)) BETWEEN 1 AND 240),
    nonce_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(nonce_hash) = 'blob' AND length(nonce_hash) = 32),
    max_uses INTEGER NOT NULL CHECK(max_uses BETWEEN 1 AND 16),
    used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count BETWEEN 0 AND max_uses),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, issued_to_run_id) REFERENCES runs(team_id, id) ON DELETE CASCADE,
    FOREIGN KEY(team_id, issued_by_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    UNIQUE(team_id, id)
);

CREATE TRIGGER turn_capabilities_require_active_human_issuer
BEFORE INSERT ON turn_capabilities
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1
    FROM memberships AS m
    JOIN principals AS p ON p.id = m.principal_id
    WHERE m.team_id = NEW.team_id
      AND m.principal_id = NEW.issued_by_principal_id
      AND m.status = 'active'
      AND p.kind = 'human'
      AND p.status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'turn capability issuer must be an active human member');
END;

CREATE TRIGGER turn_capability_authority_is_immutable
BEFORE UPDATE OF id, team_id, issued_to_run_id, issued_by_principal_id,
    token_hash, action, resource_type, resource_id, nonce_hash, max_uses,
    created_at, expires_at
ON turn_capabilities
BEGIN
    SELECT RAISE(ABORT, 'turn capability authority is immutable');
END;

CREATE TRIGGER turn_capability_use_and_revocation_are_monotonic
BEFORE UPDATE ON turn_capabilities
FOR EACH ROW WHEN
    NEW.used_count < OLD.used_count
    OR NEW.used_count > OLD.used_count + 1
    OR (NEW.revoked_at IS NOT NULL AND NEW.used_count <> OLD.used_count)
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'turn capability use and revocation are monotonic');
END;

CREATE TRIGGER turn_capabilities_cannot_be_deleted
BEFORE DELETE ON turn_capabilities
BEGIN
    SELECT RAISE(ABORT, 'turn capability ledger rows cannot be deleted');
END;

CREATE INDEX turn_capabilities_live_lookup
ON turn_capabilities(token_hash, expires_at)
WHERE revoked_at IS NULL;
