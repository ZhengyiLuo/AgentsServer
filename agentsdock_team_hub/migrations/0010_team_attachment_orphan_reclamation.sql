-- Team attachment declarations are intentionally retryable for a bounded
-- window, but an unbound ready upload is not durable message content. Permit
-- the store's expiry collector to reclaim those orphan rows while continuing
-- to make message-bound attachments undeletable.

DROP TRIGGER team_attachments_bound_cannot_be_deleted;

CREATE TRIGGER team_attachments_bound_cannot_be_deleted
BEFORE DELETE ON team_attachments
FOR EACH ROW WHEN OLD.message_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'bound team attachments cannot be deleted');
END;

-- The opportunistic collector always walks oldest expired declarations first.
-- Partial indexes keep both its team-scoped hot path and its maintenance-wide
-- path bounded by the requested LIMIT without sorting or scanning live rows.
CREATE INDEX team_attachments_reclaim_expired_by_team
ON team_attachments(team_id, expires_at, id)
WHERE message_id IS NULL;

CREATE INDEX team_attachments_reclaim_expired_global
ON team_attachments(expires_at, id)
WHERE message_id IS NULL;

-- Metadata deletion and physical deletion are separate commit points. Record
-- exact, validated relative-path components before deleting metadata so a
-- failed unlink (or a process crash after commit) remains durably retryable.
-- A later collector rechecks active metadata references before touching bytes.
CREATE TABLE team_attachment_cleanup_queue (
    path_kind TEXT NOT NULL CHECK(path_kind IN ('staging', 'content')),
    path_key TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    PRIMARY KEY(path_kind, path_key),
    CHECK(
        (
            path_kind = 'staging'
            AND length(path_key) = 37
            AND substr(path_key, 1, 5) = 'tatt_'
            AND substr(path_key, 6) NOT GLOB '*[^0-9a-f]*'
        )
        OR (
            path_kind = 'content'
            AND length(path_key) = 64
            AND path_key NOT GLOB '*[^0-9a-f]*'
        )
    )
);

CREATE INDEX team_attachment_cleanup_queue_oldest
ON team_attachment_cleanup_queue(attempt_count, created_at, path_kind, path_key);
