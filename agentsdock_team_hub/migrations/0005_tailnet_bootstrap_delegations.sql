-- Bind first-owner bootstrap proofs issued through the parent AgentsServer to
-- one verified Tailscale Serve identity and one live managed-server instance.
-- The proof itself is never stored; bootstrap_claims retains only its digest.

CREATE TABLE bootstrap_delegations (
    bootstrap_claim_id TEXT PRIMARY KEY
        REFERENCES bootstrap_claims(id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL UNIQUE
        CHECK(length(request_id) = 36),
    request_fingerprint BLOB NOT NULL
        CHECK(typeof(request_fingerprint) = 'blob'
            AND length(request_fingerprint) = 32),
    server_identity TEXT NOT NULL
        CHECK(length(trim(server_identity)) BETWEEN 8 AND 240),
    server_instance_id TEXT NOT NULL
        CHECK(length(trim(server_instance_id)) BETWEEN 8 AND 240),
    hub_id TEXT NOT NULL REFERENCES hub_metadata(hub_id) ON DELETE RESTRICT,
    hub_url TEXT NOT NULL
        CHECK(length(hub_url) BETWEEN 16 AND 2048),
    tailnet_login_normalized TEXT NOT NULL
        CHECK(length(tailnet_login_normalized) BETWEEN 3 AND 320),
    recipient_email_normalized TEXT NOT NULL
        CHECK(length(recipient_email_normalized) BETWEEN 3 AND 320),
    display_name TEXT NOT NULL
        CHECK(length(trim(display_name)) BETWEEN 1 AND 160),
    device_label TEXT NOT NULL
        CHECK(length(trim(device_label)) BETWEEN 1 AND 160),
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at)
);

CREATE TRIGGER bootstrap_delegation_is_immutable
BEFORE UPDATE ON bootstrap_delegations
BEGIN
    SELECT RAISE(ABORT, 'bootstrap delegation authority is immutable');
END;

CREATE TRIGGER bootstrap_delegations_cannot_be_deleted
BEFORE DELETE ON bootstrap_delegations
BEGIN
    SELECT RAISE(ABORT, 'bootstrap delegation ledger rows cannot be deleted');
END;

CREATE TRIGGER bootstrap_delegation_matches_claim_expiry
BEFORE INSERT ON bootstrap_delegations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM bootstrap_claims
    WHERE id = NEW.bootstrap_claim_id
      AND created_at = NEW.created_at
      AND expires_at = NEW.expires_at
)
BEGIN
    SELECT RAISE(ABORT, 'bootstrap delegation must match its claim lifetime');
END;

CREATE TRIGGER bootstrap_delegation_matches_hub
BEFORE INSERT ON bootstrap_delegations
FOR EACH ROW WHEN NOT EXISTS (
    SELECT 1 FROM hub_metadata WHERE hub_id = NEW.hub_id
)
BEGIN
    SELECT RAISE(ABORT, 'bootstrap delegation must match its Hub');
END;
