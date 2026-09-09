-- Inbox removal belongs to one addressed mailbox, never the shared message.
ALTER TABLE team_message_recipients ADD COLUMN dismissed_at INTEGER
    CHECK(dismissed_at IS NULL OR dismissed_at >= 0);
