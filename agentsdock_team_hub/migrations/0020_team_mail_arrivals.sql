-- A durable existence watermark, not an unread count. Soft deletion, dismissal,
-- receipts, revisions, and membership changes cannot remove an arrival anchor.
-- No notification transport or outbox dispatcher is enabled by this migration.
CREATE TABLE team_mail_arrivals (
    team_id TEXT NOT NULL,
    recipient_node_id TEXT NOT NULL,
    through_sequence INTEGER NOT NULL CHECK(through_sequence > 0),
    arrival_id TEXT NOT NULL,
    PRIMARY KEY(team_id, recipient_node_id),
    FOREIGN KEY(team_id, recipient_node_id) REFERENCES nodes(team_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(team_id, arrival_id) REFERENCES team_messages(team_id, id) ON DELETE RESTRICT
);

-- Reconnect validates one immutable sequence/id/recipient anchor, including
-- dismissed/deleted copies, without scanning mailbox history or message bodies.
CREATE INDEX team_mail_server_arrival_lookup
ON team_message_recipients(team_id, recipient_node_id, message_id)
WHERE recipient_kind='server';

INSERT INTO team_mail_arrivals(team_id, recipient_node_id, through_sequence, arrival_id)
SELECT latest.team_id, latest.recipient_node_id, latest.through_sequence, m.id
FROM (
    SELECT r.team_id, r.recipient_node_id, MAX(m.queue_ordinal) AS through_sequence
    FROM team_message_recipients AS r
    JOIN team_messages AS m ON m.team_id=r.team_id AND m.id=r.message_id
    WHERE r.recipient_kind='server' AND m.kind='message'
    GROUP BY r.team_id, r.recipient_node_id
) AS latest
JOIN team_messages AS m ON m.queue_ordinal=latest.through_sequence;

-- The exact resolved server copies are created in the same message transaction.
-- A replay creates no recipients, hence no arrival or notification side effect.
CREATE TRIGGER team_mail_arrival_on_server_recipient
AFTER INSERT ON team_message_recipients
FOR EACH ROW WHEN NEW.recipient_kind='server'
BEGIN
    INSERT INTO team_mail_arrivals(team_id, recipient_node_id, through_sequence, arrival_id)
    SELECT NEW.team_id, NEW.recipient_node_id, m.queue_ordinal, m.id
    FROM team_messages AS m
    WHERE m.team_id=NEW.team_id AND m.id=NEW.message_id AND m.kind='message'
    ON CONFLICT(team_id, recipient_node_id) DO UPDATE
    SET through_sequence=excluded.through_sequence, arrival_id=excluded.arrival_id
    WHERE excluded.through_sequence > team_mail_arrivals.through_sequence;
END;
