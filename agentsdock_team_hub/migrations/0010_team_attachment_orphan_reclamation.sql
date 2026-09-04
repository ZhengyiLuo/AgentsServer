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
