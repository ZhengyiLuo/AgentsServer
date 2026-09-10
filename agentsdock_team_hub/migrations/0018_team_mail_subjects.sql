-- Subjects are opt-in on the wire. Never backfill the legacy title column:
-- old ordinary-message titles were intentionally hidden from strict clients.
-- The existing whole-row immutability trigger also protects this new column.
ALTER TABLE team_messages ADD COLUMN mail_subject TEXT
    CHECK(mail_subject IS NULL OR (
        kind = 'message' AND typeof(mail_subject) = 'text'
        AND length(mail_subject) BETWEEN 1 AND 160
    ));
