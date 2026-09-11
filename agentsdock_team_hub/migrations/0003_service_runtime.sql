-- Runnable Team Hub V1 security and service ledgers. This migration tightens
-- the pre-service foundation without changing its public identity keys.

UPDATE invitations
SET revoked_at = created_at
WHERE invitee_email_normalized IS NULL
  AND redeemed_at IS NULL
  AND revoked_at IS NULL;

CREATE TRIGGER invitations_require_bound_recipient
BEFORE INSERT ON invitations
FOR EACH ROW WHEN NEW.invitee_email_normalized IS NULL
BEGIN
    SELECT RAISE(ABORT, 'invitations require a bound recipient email');
END;

CREATE TRIGGER active_owner_cannot_be_removed
BEFORE UPDATE OF role, status ON memberships
FOR EACH ROW WHEN OLD.role = 'owner' AND OLD.status = 'active'
    AND (NEW.role <> 'owner' OR NEW.status <> 'active')
BEGIN
    SELECT RAISE(ABORT, 'an active team owner cannot be removed');
END;

CREATE TRIGGER active_owner_principal_cannot_be_disabled
BEFORE UPDATE OF status ON principals
FOR EACH ROW WHEN NEW.status <> 'active' AND EXISTS (
    SELECT 1 FROM memberships
    WHERE principal_id = OLD.id AND role = 'owner' AND status = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'an active team owner principal cannot be disabled');
END;

CREATE TRIGGER message_recipient_state_is_monotonic
BEFORE UPDATE OF state ON message_recipients
FOR EACH ROW WHEN
    (OLD.state = 'available' AND NEW.state NOT IN ('available', 'delivered', 'expired'))
    OR (OLD.state = 'delivered' AND NEW.state <> 'delivered')
    OR (OLD.state = 'expired' AND NEW.state <> 'expired')
BEGIN
    SELECT RAISE(ABORT, 'message recipient delivery state is monotonic');
END;

CREATE TRIGGER message_receipt_times_are_monotonic
BEFORE UPDATE ON message_receipts
FOR EACH ROW WHEN
    (OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS NOT OLD.delivered_at)
    OR (OLD.read_at IS NOT NULL AND NEW.read_at IS NOT OLD.read_at)
    OR (OLD.acknowledged_at IS NOT NULL AND NEW.acknowledged_at IS NOT OLD.acknowledged_at)
BEGIN
    SELECT RAISE(ABORT, 'message receipt timestamps are monotonic');
END;

CREATE TRIGGER outbox_state_is_forward_only
BEFORE UPDATE OF state ON outbox_events
FOR EACH ROW WHEN
    (OLD.state = 'delivered' AND NEW.state <> 'delivered')
    OR (OLD.state = 'dead_letter' AND NEW.state <> 'dead_letter')
    OR (OLD.state = 'pending' AND NEW.state NOT IN ('pending', 'leased', 'dead_letter'))
    OR (OLD.state = 'leased' AND NEW.state NOT IN ('leased', 'pending', 'delivered', 'dead_letter'))
BEGIN
    SELECT RAISE(ABORT, 'outbox state transition is invalid');
END;

CREATE TABLE bootstrap_claims (
    id TEXT PRIMARY KEY,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER CHECK(consumed_at IS NULL OR consumed_at >= created_at),
    consumed_by_principal_id TEXT REFERENCES human_accounts(principal_id) ON DELETE RESTRICT,
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    CHECK((consumed_at IS NULL) = (consumed_by_principal_id IS NULL)),
    CHECK(consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER bootstrap_claim_authority_is_immutable
BEFORE UPDATE OF id, token_hash, created_at, expires_at ON bootstrap_claims
BEGIN
    SELECT RAISE(ABORT, 'bootstrap claim authority is immutable');
END;

CREATE TRIGGER bootstrap_claim_consumption_is_one_way
BEFORE UPDATE ON bootstrap_claims
FOR EACH ROW WHEN
    (OLD.consumed_at IS NOT NULL AND (
        NEW.consumed_at IS NOT OLD.consumed_at
        OR NEW.consumed_by_principal_id IS NOT OLD.consumed_by_principal_id
    ))
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'bootstrap claim consumption is one-way');
END;

CREATE TRIGGER bootstrap_claims_cannot_be_deleted
BEFORE DELETE ON bootstrap_claims
BEGIN
    SELECT RAISE(ABORT, 'bootstrap claim ledger rows cannot be deleted');
END;

CREATE TABLE node_enrollment_bindings (
    grant_id TEXT PRIMARY KEY REFERENCES node_enrollment_grants(id) ON DELETE RESTRICT,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    expected_server_identity TEXT NOT NULL
        CHECK(length(trim(expected_server_identity)) BETWEEN 8 AND 240),
    expected_display_name TEXT NOT NULL
        CHECK(length(trim(expected_display_name)) BETWEEN 1 AND 160),
    expected_public_material TEXT NOT NULL
        CHECK(length(expected_public_material) BETWEEN 32 AND 16384),
    expected_public_key_fingerprint BLOB NOT NULL
        CHECK(typeof(expected_public_key_fingerprint) = 'blob'
            AND length(expected_public_key_fingerprint) = 32),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    UNIQUE(team_id, grant_id)
);

CREATE TRIGGER node_enrollment_binding_is_immutable
BEFORE UPDATE ON node_enrollment_bindings
BEGIN
    SELECT RAISE(ABORT, 'node enrollment binding is immutable');
END;

CREATE TRIGGER node_enrollment_binding_requires_same_team
BEFORE INSERT ON node_enrollment_bindings
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM node_enrollment_grants
    WHERE id = NEW.grant_id AND team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'node enrollment binding must match its grant team');
END;

CREATE TRIGGER node_enrollment_bindings_cannot_be_deleted
BEFORE DELETE ON node_enrollment_bindings
BEGIN
    SELECT RAISE(ABORT, 'node enrollment binding rows cannot be deleted');
END;

CREATE TABLE node_enrollment_challenges (
    id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL UNIQUE REFERENCES node_enrollment_grants(id) ON DELETE RESTRICT,
    team_id TEXT NOT NULL,
    public_material TEXT NOT NULL CHECK(length(public_material) BETWEEN 32 AND 16384),
    public_key_fingerprint BLOB NOT NULL
        CHECK(typeof(public_key_fingerprint) = 'blob' AND length(public_key_fingerprint) = 32),
    nonce_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(nonce_hash) = 'blob' AND length(nonce_hash) = 32),
    signing_payload TEXT NOT NULL CHECK(length(signing_payload) BETWEEN 64 AND 4096),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER CHECK(consumed_at IS NULL OR consumed_at >= created_at),
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, grant_id)
        REFERENCES node_enrollment_bindings(team_id, grant_id) ON DELETE RESTRICT,
    CHECK(consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER node_enrollment_challenge_authority_is_immutable
BEFORE UPDATE OF id, grant_id, team_id, public_material,
    public_key_fingerprint, nonce_hash, signing_payload, created_at, expires_at
ON node_enrollment_challenges
BEGIN
    SELECT RAISE(ABORT, 'node enrollment challenge authority is immutable');
END;

CREATE TRIGGER node_enrollment_challenge_terminal_state_is_one_way
BEFORE UPDATE ON node_enrollment_challenges
FOR EACH ROW WHEN
    (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS NOT OLD.consumed_at)
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'node enrollment challenge terminal state is one-way');
END;

CREATE TRIGGER node_enrollment_challenges_cannot_be_deleted
BEFORE DELETE ON node_enrollment_challenges
BEGIN
    SELECT RAISE(ABORT, 'node enrollment challenge rows cannot be deleted');
END;

CREATE TABLE request_idempotency (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
    operation TEXT NOT NULL CHECK(length(operation) BETWEEN 1 AND 120),
    key_hash BLOB NOT NULL CHECK(typeof(key_hash) = 'blob' AND length(key_hash) = 32),
    request_fingerprint BLOB NOT NULL
        CHECK(typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32),
    resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 1 AND 80),
    resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 240),
    response_json TEXT NOT NULL CHECK(json_valid(response_json) AND json_type(response_json) = 'object'),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    UNIQUE(team_id, principal_id, operation, key_hash)
);

CREATE TRIGGER request_idempotency_is_immutable_update
BEFORE UPDATE ON request_idempotency
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END;

CREATE TRIGGER request_idempotency_is_immutable_delete
BEFORE DELETE ON request_idempotency
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END;

CREATE TRIGGER request_idempotency_requires_team_principal
BEFORE INSERT ON request_idempotency
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
    SELECT RAISE(ABORT, 'idempotency principal does not belong to its team');
END;

CREATE TRIGGER message_receipt_times_are_ordered_insert
BEFORE INSERT ON message_receipts
FOR EACH ROW WHEN
    (NEW.read_at IS NOT NULL AND NEW.read_at < NEW.delivered_at)
    OR (NEW.acknowledged_at IS NOT NULL AND NEW.acknowledged_at < NEW.delivered_at)
BEGIN
    SELECT RAISE(ABORT, 'message receipt timestamps are out of order');
END;

CREATE TRIGGER message_receipt_times_are_ordered_update
BEFORE UPDATE ON message_receipts
FOR EACH ROW WHEN
    (NEW.read_at IS NOT NULL AND NEW.read_at < NEW.delivered_at)
    OR (NEW.acknowledged_at IS NOT NULL AND NEW.acknowledged_at < NEW.delivered_at)
BEGIN
    SELECT RAISE(ABORT, 'message receipt timestamps are out of order');
END;

CREATE TRIGGER outbox_row_shape_is_valid_insert
BEFORE INSERT ON outbox_events
FOR EACH ROW WHEN
    (NEW.state <> 'leased' AND (NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL))
    OR (NEW.state = 'delivered' AND NEW.delivered_at IS NULL)
    OR (NEW.state <> 'delivered' AND NEW.delivered_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'outbox row shape is invalid');
END;

CREATE TRIGGER outbox_row_shape_is_valid_update
BEFORE UPDATE ON outbox_events
FOR EACH ROW WHEN
    (NEW.state <> 'leased' AND (NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL))
    OR (NEW.state = 'delivered' AND NEW.delivered_at IS NULL)
    OR (NEW.state <> 'delivered' AND NEW.delivered_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'outbox row shape is invalid');
END;

CREATE TABLE hub_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    hub_id TEXT NOT NULL UNIQUE CHECK(length(hub_id) BETWEEN 12 AND 80),
    created_at INTEGER NOT NULL CHECK(created_at >= 0)
);

CREATE TRIGGER hub_metadata_is_immutable_update
BEFORE UPDATE ON hub_metadata
BEGIN
    SELECT RAISE(ABORT, 'Hub identity is immutable');
END;

CREATE TRIGGER hub_metadata_is_immutable_delete
BEFORE DELETE ON hub_metadata
BEGIN
    SELECT RAISE(ABORT, 'Hub identity is immutable');
END;

CREATE TABLE owner_recovery_claims (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
    owner_principal_id TEXT NOT NULL,
    token_hash BLOB NOT NULL UNIQUE
        CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    device_label TEXT NOT NULL CHECK(length(trim(device_label)) BETWEEN 1 AND 160),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER CHECK(consumed_at IS NULL OR consumed_at >= created_at),
    consumed_by_session_id TEXT REFERENCES device_sessions(id) ON DELETE RESTRICT,
    revoked_at INTEGER CHECK(revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(team_id, owner_principal_id)
        REFERENCES memberships(team_id, principal_id) ON DELETE RESTRICT,
    CHECK((consumed_at IS NULL) = (consumed_by_session_id IS NULL)),
    CHECK(consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE TRIGGER owner_recovery_claim_authority_is_immutable
BEFORE UPDATE OF id, team_id, owner_principal_id, token_hash,
    device_label, created_at, expires_at ON owner_recovery_claims
BEGIN
    SELECT RAISE(ABORT, 'owner recovery claim authority is immutable');
END;

CREATE TRIGGER owner_recovery_claim_terminal_state_is_one_way
BEFORE UPDATE ON owner_recovery_claims
FOR EACH ROW WHEN
    (OLD.consumed_at IS NOT NULL AND (
        NEW.consumed_at IS NOT OLD.consumed_at
        OR NEW.consumed_by_session_id IS NOT OLD.consumed_by_session_id
    ))
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'owner recovery claim terminal state is one-way');
END;

CREATE TRIGGER owner_recovery_claims_cannot_be_deleted
BEFORE DELETE ON owner_recovery_claims
BEGIN
    SELECT RAISE(ABORT, 'owner recovery claim ledger rows cannot be deleted');
END;

CREATE TABLE audit_chain_heads (
    team_id TEXT PRIMARY KEY REFERENCES teams(id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL REFERENCES audit_events(id) ON DELETE RESTRICT,
    event_hash BLOB NOT NULL
        CHECK(typeof(event_hash) = 'blob' AND length(event_hash) = 32),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
);

CREATE TRIGGER audit_chain_head_requires_team_event_insert
BEFORE INSERT ON audit_chain_heads
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM audit_events WHERE id = NEW.event_id AND team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit chain head event must belong to its team');
END;

CREATE TRIGGER audit_chain_head_requires_team_event_update
BEFORE UPDATE OF event_id ON audit_chain_heads
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM audit_events WHERE id = NEW.event_id AND team_id = NEW.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit chain head event must belong to its team');
END;

CREATE TRIGGER audit_chain_head_is_monotonic
BEFORE UPDATE ON audit_chain_heads
FOR EACH ROW WHEN NEW.sequence <> OLD.sequence + 1 OR NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'audit chain head must advance exactly once');
END;
