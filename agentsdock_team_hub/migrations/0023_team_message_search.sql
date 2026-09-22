-- Search current Team Messages content, never immutable old revision bodies.
-- SQLite performs the one-time backfill inside the migration transaction;
-- no Python body collection or request-time lazy backfill is involved.
CREATE VIRTUAL TABLE team_message_search USING fts5(
    scope, subject, body,
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4'
);

INSERT INTO team_message_search(rowid, scope, subject, body)
SELECT m.queue_ordinal, m.team_id,
       CASE WHEN m.kind='skill' THEN m.title ELSE m.mail_subject END,
       COALESCE((SELECT r.body FROM team_message_revisions AS r
                 WHERE r.team_id=m.team_id AND r.message_id=m.id
                 ORDER BY r.version DESC LIMIT 1), m.body)
FROM team_messages AS m
WHERE NOT EXISTS (SELECT 1 FROM network_content_deletions AS d
                  WHERE d.team_id=m.team_id AND d.resource_kind='message'
                    AND d.resource_id=m.id);

CREATE TRIGGER team_message_search_insert AFTER INSERT ON team_messages
BEGIN
    INSERT INTO team_message_search(rowid, scope, subject, body)
    VALUES (NEW.queue_ordinal, NEW.team_id,
            CASE WHEN NEW.kind='skill' THEN NEW.title ELSE NEW.mail_subject END,
            NEW.body);
END;

CREATE TRIGGER team_message_search_revision AFTER INSERT ON team_message_revisions
WHEN NEW.version=(SELECT MAX(r.version) FROM team_message_revisions AS r
                  WHERE r.team_id=NEW.team_id AND r.message_id=NEW.message_id)
BEGIN
    UPDATE team_message_search SET body=NEW.body
    WHERE rowid=(SELECT queue_ordinal FROM team_messages
                 WHERE team_id=NEW.team_id AND id=NEW.message_id);
END;

CREATE TRIGGER team_message_search_delete AFTER INSERT ON network_content_deletions
WHEN NEW.resource_kind='message'
BEGIN
    DELETE FROM team_message_search
    WHERE rowid=(SELECT queue_ordinal FROM team_messages
                 WHERE team_id=NEW.team_id AND id=NEW.resource_id);
END;

-- Names are indexed once per identity, not copied onto every message. Renaming
-- one server/person stays a small transaction and immediately changes search.
CREATE VIRTUAL TABLE team_message_sender_nodes USING fts5(
    display_name, content='nodes', content_rowid='rowid',
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4'
);
INSERT INTO team_message_sender_nodes(team_message_sender_nodes) VALUES ('rebuild');

CREATE TRIGGER team_message_sender_nodes_insert AFTER INSERT ON nodes
BEGIN
    INSERT INTO team_message_sender_nodes(rowid, display_name) VALUES (NEW.rowid, NEW.display_name);
END;
CREATE TRIGGER team_message_sender_nodes_update AFTER UPDATE OF display_name ON nodes
BEGIN
    INSERT INTO team_message_sender_nodes(team_message_sender_nodes, rowid, display_name)
    VALUES ('delete', OLD.rowid, OLD.display_name);
    INSERT INTO team_message_sender_nodes(rowid, display_name) VALUES (NEW.rowid, NEW.display_name);
END;
CREATE TRIGGER team_message_sender_nodes_delete AFTER DELETE ON nodes
BEGIN
    INSERT INTO team_message_sender_nodes(team_message_sender_nodes, rowid, display_name)
    VALUES ('delete', OLD.rowid, OLD.display_name);
END;

CREATE VIRTUAL TABLE team_message_sender_principals USING fts5(
    display_name, content='principals', content_rowid='rowid',
    tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3 4'
);
INSERT INTO team_message_sender_principals(team_message_sender_principals) VALUES ('rebuild');

CREATE TRIGGER team_message_sender_principals_insert AFTER INSERT ON principals
BEGIN
    INSERT INTO team_message_sender_principals(rowid, display_name) VALUES (NEW.rowid, NEW.display_name);
END;
CREATE TRIGGER team_message_sender_principals_update AFTER UPDATE OF display_name ON principals
BEGIN
    INSERT INTO team_message_sender_principals(team_message_sender_principals, rowid, display_name)
    VALUES ('delete', OLD.rowid, OLD.display_name);
    INSERT INTO team_message_sender_principals(rowid, display_name) VALUES (NEW.rowid, NEW.display_name);
END;
CREATE TRIGGER team_message_sender_principals_delete AFTER DELETE ON principals
BEGIN
    INSERT INTO team_message_sender_principals(team_message_sender_principals, rowid, display_name)
    VALUES ('delete', OLD.rowid, OLD.display_name);
END;

CREATE INDEX team_messages_sender_node_order
ON team_messages(team_id, sender_node_id, queue_ordinal)
WHERE sender_kind='server';
