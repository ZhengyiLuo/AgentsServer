-- Inbox attention is independent of historical delivery/read receipts.
ALTER TABLE team_message_recipients ADD COLUMN inbox_unread INTEGER
    CHECK(inbox_unread IS NULL OR inbox_unread IN (0, 1));
ALTER TABLE team_message_recipients ADD COLUMN mailbox_state_version INTEGER NOT NULL DEFAULT 0
    CHECK(mailbox_state_version >= 0);
