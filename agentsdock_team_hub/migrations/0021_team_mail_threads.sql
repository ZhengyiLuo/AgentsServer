-- Exact parent-link traversal must not scan the team's mailbox. Message
-- identity and parent pointers remain immutable; no data rewrite is needed.
CREATE INDEX team_messages_parent_order
ON team_messages(team_id, in_reply_to_message_id, queue_ordinal);
